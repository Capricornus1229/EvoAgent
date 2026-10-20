"""Nine GitHub delivery, publication and trusted repair contracts; no real HTTP."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

from helpers.lifecycle import DIFF, WebhookFixture, close_service, make_service
from evoagent.api import ApiHandler
from evoagent.github import GitHubClient, verify_signature
from evoagent.models import ReviewReport, TaskState, TraceEvent
from evoagent.patching import VerifiedPatchFixer, apply_file_patch, parse_unified_patch
from evoagent.report import to_markdown
from evoagent.repository_safety import trusted_root
from evoagent.service import DispatchUnavailable
from evoagent.store import TaskStore, utc_now
from evoagent.task_queue import PermanentTaskError
from evoagent.verifier import RepairVerifier


class GitHubRepairTests(unittest.TestCase):
    source = 'import subprocess\nsubprocess.run(command, shell=True)\n'
    generated_patch = ('--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n import subprocess\n'
                       '-subprocess.run(command, shell=True)\n+subprocess.run(command, shell=False)\n')

    def test_signed_webhook_is_validated_and_delivery_creation_is_atomic_and_idempotent(self):
        body = b'{"pull_request":{"updated_at":"2000-01-01T00:00:00Z"}}'
        signature = 'sha256=' + hmac.new(b'secret', body, hashlib.sha256).hexdigest()
        for secret, token, valid in (('secret', signature, True), ('wrong', signature, False),
                                     ('secret', '', False), ('', signature, False)):
            with self.subTest(secret=secret, token=token):
                self.assertEqual(valid, verify_signature(secret, body, token))
        for valid in (True, False):
            handler = ApiHandler.__new__(ApiHandler)
            handler.path = '/webhooks/github'
            handler.service = Mock()
            handler.service.handle_github_pull_request.return_value = {'task_id': 'task', 'state': 'PENDING'}
            handler.settings = SimpleNamespace(auth_required=False, default_tenant_id='tenant', github_webhook_secret='secret')
            handler.headers = {'X-GitHub-Event': 'pull_request', 'X-GitHub-Delivery': 'delivery',
                               'X-Hub-Signature-256': signature if valid else 'sha256=invalid'}
            handler._read_body, handler._send_json = lambda: body, Mock()
            handler.do_POST()
            self.assertEqual(202 if valid else 401, handler._send_json.call_args.args[0])
            self.assertEqual(1 if valid else 0, handler.service.handle_github_pull_request.call_count)
        with tempfile.TemporaryDirectory() as directory:
            fixture = WebhookFixture(directory)
            first, again = fixture.webhook(), fixture.webhook()
            self.assertEqual(first['task_id'], again['task_id'])
            self.assertTrue(again['duplicate'])
            self.assertEqual(1, len(fixture.service.queue.calls))
            with self.assertRaises(ValueError):
                fixture.webhook(digest='different')
            with self.assertRaises(TypeError):
                fixture.store.create_webhook_task('broken', 'tenant', 'hash', 'broken-task',
                    'org/repo', 7, {'unserializable': object()})
            self.assertIsNone(fixture.store.get_webhook('broken'))
            self.assertIsNone(fixture.store.get('broken-task'))
            barrier = threading.Barrier(2)
            def create(identity):
                store = TaskStore(fixture.store.path)
                barrier.wait(timeout=2)
                return store.create_webhook_task('concurrent', 'tenant', 'same-hash', identity, 'org/repo', 7, {})
            with ThreadPoolExecutor(max_workers=2) as pool:
                one, two = pool.submit(create, 'one'), pool.submit(create, 'two')
                results = [one.result(timeout=5), two.result(timeout=5)]
            self.assertEqual(results[0][0], results[1][0])
            self.assertEqual(1, sum(result[1] for result in results))

    def test_download_is_fixed_to_saved_head_and_base_before_and_after_fetch(self):
        for boundary in ('matching', 'before-head', 'before-base', 'during-head', 'during-base', 'retry'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                fixture = WebhookFixture(directory)
                task_id = fixture.webhook()['task_id']
                task = fixture.store.get(task_id)
                for key in ('installation_id', 'base_sha', 'head_sha', 'mode', 'enabled_agents',
                            'enabled_skills', 'repository_root', 'diff_url', 'publish_review'):
                    self.assertIn(key, task['input'])
                fixture.service.queue.active.clear()
                if boundary.startswith(('before', 'during')):
                    moved = fixture.github.pull('other-base' if boundary.endswith('base') else 'base',
                                                'other-head' if boundary.endswith('head') else 'head')
                    fixture.github.revisions = ([fixture.github.pull('base', 'head')] if boundary.startswith('during') else []) + [moved]
                    with self.assertRaises(PermanentTaskError):
                        fixture.drain(task_id)
                    self.assertIsNone(fixture.store.get_task_payload(task_id))
                    self.assertEqual([], fixture.review_calls)
                else:
                    if boundary == 'retry':
                        fixture.github.download_error = RuntimeError('temporary download failure')
                        with self.assertRaises(RuntimeError):
                            fixture.drain(task_id)
                        fixture.github.download_error = None
                    fixture.service.resume_task(task_id, 'tenant')
                    fixture.drain(task_id)
                    saved = fixture.store.get(task_id)
                    self.assertEqual('SUCCESS', saved['state'])
                    revision = {'base_sha': 'base', 'head_sha': 'head'}
                    self.assertEqual(revision, saved['input']['reviewed_revision'])
                    self.assertEqual(revision, saved['report']['execution']['reviewed_revision'])
                    self.assertTrue(all(call.args == (42,) for call in fixture.service.github_client_for_installation.call_args_list))
                    fixture.service._download_task_diff = Mock(side_effect=AssertionError('saved diff must be reused'))
                    fixture.service._process_queued({'task_id': task_id})
                    self.assertEqual(1, len(fixture.review_calls))

    def test_failed_dispatch_redelivery_reuses_the_durable_task(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = WebhookFixture(directory)
            fixture.service.queue.fail = True
            with self.assertRaises(DispatchUnavailable):
                fixture.webhook()
            task_id = fixture.store.get_webhook('delivery')['task_id']
            self.assertEqual('failed', fixture.store.get_webhook('delivery')['dispatch_status'])
            self.assertEqual('FAILED', fixture.store.get(task_id)['state'])
            fixture.service.queue.fail = False
            self.assertEqual(task_id, fixture.webhook()['task_id'])
            fixture.drain(task_id)
            self.assertEqual('SUCCESS', fixture.webhook()['state'])
            self.assertEqual(1, len(fixture.service.queue.calls))
            self.assertEqual(1, len(fixture.store.list_tasks()))

    def test_old_or_unknown_revisions_never_publish_a_comment(self):
        for revision in ('head', 'base', 'unknown', 'legacy'):
            with self.subTest(revision=revision), tempfile.TemporaryDirectory() as directory:
                fixture = WebhookFixture(directory)
                task_id = fixture.webhook()['task_id']
                if revision != 'legacy':
                    fixture.service._download_task_diff(fixture.store.get(task_id))
                report = fixture.service._run_review(task_id, 'org/repo', 7, DIFF, 'tenant')
                if revision == 'head':
                    fixture.github.revisions = [fixture.github.pull('base', 'new-head')]
                elif revision == 'base':
                    fixture.github.revisions = [fixture.github.pull('new-base', 'head')]
                elif revision == 'unknown':
                    with fixture.store._connect() as connection:
                        connection.execute('UPDATE tasks SET input_json=?,report_json=? WHERE id=?',
                            (json.dumps({'publish_review': True, 'github_issue_url': 'https://api.github.com/repos/org/repo/issues/7'}),
                             json.dumps(ReviewReport('org/repo', 7, 'old', 'low').to_dict()), task_id))
                else:
                    fixture.store.set_publication(task_id, 'not_requested')
                fixture.service.queue.active.clear()
                fixture.service._process_queued({'task_id': task_id})
                saved = fixture.store.get(task_id)
                self.assertEqual('SUCCESS', saved['state'])
                self.assertEqual({'head': 'superseded', 'base': 'superseded', 'unknown': 'failed',
                                  'legacy': 'not_requested'}[revision], saved['publication_status'])
                self.assertEqual([], fixture.github.comments)
                self.assertEqual(1, len(fixture.review_calls))
                if revision in {'head', 'base'}:
                    before = len(fixture.service.queue.calls)
                    self.assertEqual('superseded', fixture.service.resume_task(task_id)['publication_status'])
                    self.assertEqual(before, len(fixture.service.queue.calls))
        report = ReviewReport('org/repo', 7, 'complete', 'low')
        report.execution['reviewed_revision'] = {'head_sha': 'a' * 40, 'base_sha': 'b' * 40}
        self.assertIn('a' * 40, to_markdown(report.to_dict()))

    def test_comment_failure_retries_only_publication_and_marker_lookup_prevents_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = WebhookFixture(directory)
            task_id = fixture.webhook()['task_id']
            fixture.github.comment_error = RuntimeError('comment denied')
            fixture.drain(task_id)
            task = fixture.store.get(task_id)
            self.assertEqual(('SUCCESS', 'failed'), (task['state'], task['publication_status']))
            marker = fixture.github.comments[0][2]
            fixture.github.comment_error = None
            fixture.service.resume_task(task_id, 'tenant')
            fixture.drain(task_id)
            self.assertEqual(1, len(fixture.review_calls))
            self.assertEqual(1, fixture.github.downloads)
            self.assertEqual(marker, fixture.github.comments[1][2])
            self.assertEqual('sent', fixture.store.get(task_id)['publication_status'])
            fixture.service.reviewer.reset_failed_assignments.assert_not_called()
        client = GitHubClient('fake')
        marker = '<!-- task-id -->'
        stored = {'body': marker + '\nold', 'url': 'https://api.github.com/comments/1'}
        client._json = Mock(side_effect=[[{'body': 'unrelated'}] * 100, [stored], {'id': 1}])
        client.upsert_comment('https://api.github.com/issues/7', 'new', marker)
        self.assertIn('&page=2', client._json.call_args_list[1].args[1])
        self.assertEqual('PATCH', client._json.call_args.args[0])
        self.assertFalse(any(call.args[0] == 'POST' for call in client._json.call_args_list))

    def test_verified_repair_pins_reads_tests_and_commit_and_rechecks_head(self):
        self._repair_pinned_sha()
        self._repair_head_moved_before()
        self._repair_head_moved_during()

    def test_repair_requires_structural_and_before_after_test_evidence_in_a_trusted_repository(self):
        self._repair_unknown_or_untrusted()
        for failed in ('baseline', 'patched', 'missing-tests', 'syntax'):
            with self.subTest(failed=failed):
                github, verifier, llm, report = self.fixtures()
                if failed == 'syntax':
                    llm.complete_json.return_value = {'patch': self.generated_patch.replace('shell=False)', 'shell=False')}
                else:
                    successful = {'passed': True, 'checks': [{'name': 'repository-tests', 'passed': True}]}
                    failure = {'passed': False, 'checks': [{'name': 'repository-tests', 'passed': False}]}
                    if failed == 'baseline':
                        verifier.verify_archive.side_effect = [failure, successful]
                    elif failed == 'patched':
                        verifier.verify_archive.side_effect = [successful, failure]
                    else:
                        verifier.verify_archive.return_value = {'passed': True, 'checks': []}
                result = VerifiedPatchFixer(llm, verifier).create_fix_commits(github, 'org/repo', 7, report)
                self.assertEqual('blocked', result['status'])
                github.create_atomic_commit.assert_not_called()
                github.create_draft_pull_request.assert_not_called()
        github, verifier, llm, report = self.fixtures()
        verifier.test_command = ''
        self.assertEqual('suggestion-only', VerifiedPatchFixer(llm, verifier).create_fix_commits(github, 'org/repo', 7, report)['status'])
        github.download_archive.assert_not_called()
        github.create_atomic_commit.assert_not_called()

    def test_archive_execution_bits_and_repository_trust_boundaries_are_preserved(self):
        if os.name == 'posix':
            self._archive_executable()
        self._archive_symlink()
        with tempfile.TemporaryDirectory() as directory:
            allowed, nested, sibling = Path(directory, 'allowed'), Path(directory, 'allowed', 'nested'), Path(directory, 'sibling')
            nested.mkdir(parents=True)
            sibling.mkdir()
            for root, expected in ((allowed, True), (nested, False), (sibling, False)):
                with self.subTest(root=root):
                    self.assertEqual(expected, trusted_root(str(root), (str(allowed),)))
            service, _ = make_service(directory, trusted_repository_roots=(str(allowed),))
            try:
                service._validate_repository_root(str(allowed))
                for root in (nested, sibling):
                    with self.assertRaisesRegex(ValueError, 'trusted repository root'):
                        service.create_review('org/repo', DIFF, repository_root=str(root))
                self.assertEqual([], service.store.list_tasks())
                result = service.create_review(str(sibling), DIFF)
                self.assertFalse(result['report']['execution']['repository_context']['available'])
            finally:
                close_service(service)
            untrusted = RepairVerifier('./scripts/test.sh')
            with patch('evoagent.verifier.subprocess.run') as run:
                with self.assertRaises(PermissionError):
                    untrusted.verify_archive(self.archive(), {}, 'org/repo')
            run.assert_not_called()
            bundle = BytesIO()
            with zipfile.ZipFile(bundle, 'w') as archive:
                archive.writestr('repo/../../escape.py', 'value=1')
            with self.assertRaises(ValueError):
                untrusted.verify_archive(bundle.getvalue(), {}, 'org/repo')

    def test_git_commit_preserves_modes_and_generated_patch_is_path_and_context_bounded(self):
        self._git_modes()
        self._git_nonregular_or_truncated()
        text = '--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n'
        file_patch = parse_unified_patch(text, ['app.py'])[0]
        self.assertEqual('value = 2\n', apply_file_patch('value = 1\n', file_patch))
        with self.assertRaisesRegex(ValueError, 'context does not match'):
            apply_file_patch('value = 3\n', file_patch)
        for path in ('other.py', '../app.py'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                parse_unified_patch(text.replace('app.py', path), ['app.py'])
    def fixtures(self, *, trusted=True, head='pinned', later_head='pinned', reviewed='pinned'):
        pull = {'head': {'ref': 'feature', 'sha': head, 'repo': {'full_name': 'org/source'}},
                'base': {'ref': 'main'}}
        later = {'head': {'sha': later_head}}
        github = Mock()
        github.get_pull_request.side_effect = [pull, later]
        github.get_file.return_value = {'decoded_content': self.source}
        github.download_archive.return_value = b'fake-archive'
        github.create_atomic_commit.return_value = {'sha': 'commit-sha'}
        github.create_draft_pull_request.return_value = {'number': 8, 'html_url': 'https://example.test/pr/8'}
        verifier = RepairVerifier('fake-tests', trusted_github_repositories=('org/source',) if trusted else ())
        verifier.verify_archive = Mock(return_value={'passed': True,
            'checks': [{'name': 'repository-tests', 'passed': True}], 'duration_seconds': 0})
        llm = Mock()
        llm.complete_json.return_value = {'patch': self.generated_patch}
        findings = [{'rule_id': 'SEC-SUBPROCESS-SHELL', 'path': 'app.py', 'line': 2,
                     'fix': 'Disable shell', 'gate': {'passed': True}}]
        report = {'findings': findings, 'execution': {'reviewed_revision':
                  {'head_sha': reviewed} if reviewed else None}}
        return github, verifier, llm, report


    def _repair_pinned_sha(self):
        github, verifier, llm, report = self.fixtures()
        result = VerifiedPatchFixer(llm, verifier).create_fix_commits(github, 'org/repo', 7, report)
        self.assertEqual('pinned', result['source_sha'])
        github.get_file.assert_called_once_with('org/source', 'app.py', 'pinned')
        github.download_archive.assert_called_once_with('org/source', 'pinned')
        self.assertEqual(2, verifier.verify_archive.call_count)
        self.assertTrue(all(call.args[2] == 'org/source' for call in verifier.verify_archive.call_args_list))
        self.assertEqual('pinned', github.create_atomic_commit.call_args.args[2])
        github.create_draft_pull_request.assert_called_once()


    def _repair_head_moved_before(self):
        github, verifier, llm, report = self.fixtures(head='new-head')
        fixer = VerifiedPatchFixer(llm, verifier)
        with self.assertRaisesRegex(ValueError, 'changed since review'):
            fixer.create_fix_commits(github, 'org/repo', 7, report)
        github.get_file.assert_not_called()
        github.create_atomic_commit.assert_not_called()
        llm.complete_json.assert_not_called()


    def _repair_head_moved_during(self):
        github, verifier, llm, report = self.fixtures(later_head='new-head')
        fixer = VerifiedPatchFixer(llm, verifier)
        with self.assertRaisesRegex(ValueError, 'changed during repair'):
            fixer.create_fix_commits(github, 'org/repo', 7, report)
        github.download_archive.assert_called_once_with('org/source', 'pinned')
        github.create_atomic_commit.assert_not_called()
        github.create_draft_pull_request.assert_not_called()


    def _repair_unknown_or_untrusted(self):
        for trusted, reviewed in ((True, None), (False, 'pinned')):
            with self.subTest(trusted=trusted, reviewed=reviewed):
                github, verifier, llm, report = self.fixtures(trusted=trusted, reviewed=reviewed)
                fixer = VerifiedPatchFixer(llm, verifier)
                self.assertEqual('suggestion-only', fixer.create_fix_commits(github, 'org/repo', 7, report)['status'])
                github.download_archive.assert_not_called()
                github.create_atomic_commit.assert_not_called()


    def github(self, root_entries=None, truncated=False):
        client = GitHubClient("fake")

        def request(method, url, payload=None):
            if method == "GET" and "/git/commits/" in url:
                return {"tree": {"sha": "root-tree"}}
            if method == "GET" and url.endswith("/git/trees/root-tree"):
                return {"truncated": truncated, "tree": root_entries if root_entries is not None else [
                    {"path": "app.py", "type": "blob", "mode": "100644"},
                    {"path": "scripts", "type": "tree", "mode": "040000", "sha": "scripts-tree"},
                ]}
            if method == "GET" and url.endswith("/git/trees/scripts-tree"):
                return {"tree": [{"path": "main.py", "type": "blob", "mode": "100755"}]}
            if method == "POST" and url.endswith("/git/trees"):
                return {"sha": "new-tree"}
            if method == "POST" and url.endswith("/git/commits"):
                return {"sha": "new-commit"}
            if method == "POST" and url.endswith("/git/refs"):
                return {"ref": "refs/heads/fix"}
            raise AssertionError((method, url, payload))

        client._json = Mock(side_effect=request)
        return client


    def _git_modes(self):
        client = self.github()
        commit = client.create_atomic_commit("org/repo", "fix", "parent", {
            "app.py": "value = 1\n", "scripts/main.py": "print(1)\n", "scripts/new.py": "print(2)\n",
        }, "fix")
        self.assertEqual("new-commit", commit["sha"])
        tree = next(call.args[2] for call in client._json.call_args_list
                    if call.args[0] == "POST" and call.args[1].endswith("/git/trees"))
        self.assertEqual({"app.py": "100644", "scripts/main.py": "100755", "scripts/new.py": "100644"},
                         {entry["path"]: entry["mode"] for entry in tree["tree"]})
        self.assertEqual("root-tree", tree["base_tree"])
        self.assertEqual(2, sum(call.args[0] == "GET" and "/git/trees/" in call.args[1]
                                for call in client._json.call_args_list))


    def _git_nonregular_or_truncated(self):
        for client in (self.github(truncated=True), self.github(root_entries=[
                {"path": "app.py", "type": "blob", "mode": "120000"}])):
            with self.subTest(client=client):
                with self.assertRaises((RuntimeError, ValueError)):
                    client.create_atomic_commit("org/repo", "fix", "parent", {"app.py": "value=1\n"}, "fix")
                self.assertFalse(any(call.args[0] == "POST" for call in client._json.call_args_list))


    def archive(self, mode=stat.S_IFREG | 0o6755):
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            member = zipfile.ZipInfo("repo/scripts/test.sh")
            member.create_system = 3
            member.external_attr = mode << 16
            bundle.writestr(member, "#!/bin/sh\nexit 0\n")
        return buffer.getvalue()


    def _archive_executable(self):
        seen_modes = []

        class InspectVerifier(RepairVerifier):
            def verify_worktree(self, root, repository=""):
                seen_modes.append(stat.S_IMODE(Path(root, "scripts/test.sh").stat().st_mode))
                return super().verify_worktree(root, repository)

        verifier = InspectVerifier("./scripts/test.sh", trusted_github_repositories=("org/repo",))
        before = verifier.verify_archive(self.archive(), {}, "org/repo")
        after = verifier.verify_archive(self.archive(), {"scripts/test.sh": "#!/bin/sh\n# patched\nexit 0\n"}, "org/repo")
        self.assertTrue(verifier.compare(before, after)["passed"])
        self.assertEqual(2, len(seen_modes))
        self.assertTrue(all(mode & 0o111 == 0o111 and mode & 0o7000 == 0 for mode in seen_modes))


    def _archive_symlink(self):
        verifier = RepairVerifier("./scripts/test.sh", trusted_github_repositories=("org/repo",))
        with patch("evoagent.verifier.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "symlink"):
                verifier.verify_archive(self.archive(stat.S_IFLNK | 0o777), {}, "org/repo")
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
