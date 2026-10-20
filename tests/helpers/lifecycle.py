"""Temporary-service fixtures shared by lifecycle and GitHub regression tests."""
from collections import Counter
from contextlib import contextmanager, nullcontext
import os
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

from helpers import FakeAgenticClient
from evoagent.config import Settings
from evoagent.models import ReviewReport, TaskState, TraceEvent
from evoagent.runtime import RuntimeCancelled
from evoagent.service import ReviewService
from evoagent.store import TaskStore, utc_now
from evoagent.task_queue import DeliveryAlreadyRunning


DIFF = '--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+value = 1\n'


class CountingClient(FakeAgenticClient):
    def __init__(self, block=False):
        self.calls = Counter()
        self.block = block
        self.fail_security = False
        self.started, self.release = threading.Event(), threading.Event()
        self.lock = threading.Lock()

    def complete_json(self, role, *args, **kwargs):
        with self.lock:
            self.calls[role] += 1
        if role == 'security':
            if self.fail_security:
                ledger = kwargs.get('ledger') or (args[2] if len(args) > 2 else None)
                if ledger:
                    ledger.record_model(role, self.provider, self.model,
                                        {'prompt_tokens': 10, 'completion_tokens': 5}, 1,
                                        False, 'security model unavailable')
                raise RuntimeError('security model unavailable')
            if self.block:
                self.started.set()
                if not self.release.wait(5):
                    raise RuntimeError('test did not release worker')
        return super().complete_json(role, *args, **kwargs)


def make_service(directory, client=None, **changes):
    client = client or CountingClient()
    values = dict(
        host='127.0.0.1', port=8080, db_path=os.path.join(directory, 'tasks.db'),
        max_diff_bytes=10000, max_steps=8, timeout_seconds=20,
        llm_base_url='https://invalid.test', llm_api_key='fake', llm_model='fake-model',
        github_webhook_secret='', github_token='', auto_post_review=True,
        skills_dir=os.path.join(directory, 'skills'), memory_enabled=False,
    )
    values.update(changes)
    with patch('evoagent.service.JsonChatClient', return_value=client):
        service = ReviewService(Settings(**values))
    return service, client


def finish_queue(queue):
    queue._executor.submit(lambda: None).result(timeout=5)


def close_service(service):
    service.queue.close()
    service.queue._executor.shutdown(wait=True)


class RecordingQueue:
    backend = 'recording'

    def __init__(self):
        self.calls, self.active, self.fail = [], set(), False

    def is_active(self, task_id):
        return task_id in self.active

    @contextmanager
    def inline_execution(self, task_id):
        if task_id in self.active:
            raise DeliveryAlreadyRunning(task_id)
        self.active.add(task_id)
        try:
            yield
        finally:
            self.active.discard(task_id)

    def submit(self, payload, message_id='', prepare=None):
        if self.fail:
            raise RuntimeError('queue unavailable')
        if message_id in self.active:
            return message_id
        self.active.add(message_id)
        if prepare:
            prepare()
        self.calls.append(dict(payload))
        return message_id


class FakeGitHub:
    def __init__(self):
        self.revisions = [self.pull('base', 'head')]
        self.downloads, self.comments = 0, []
        self.download_error, self.comment_error = None, None

    @staticmethod
    def pull(base, head):
        return {'base': {'sha': base}, 'head': {'sha': head}}

    def ensure_repository_access(self, repository):
        if repository != 'org/repo':
            raise AssertionError(repository)

    def get_pull_request(self, repository, number):
        return self.revisions.pop(0) if len(self.revisions) > 1 else self.revisions[0]

    def fetch_diff(self, url):
        self.downloads += 1
        if self.download_error:
            raise self.download_error
        return DIFF

    def upsert_comment(self, url, markdown, marker):
        self.comments.append((url, markdown, marker))
        if self.comment_error:
            raise self.comment_error


class WebhookFixture:
    """Use real durable lifecycle methods, replacing only queue/network/review."""
    def __init__(self, directory):
        self.store = TaskStore(os.path.join(directory, 'webhooks.db'))
        self.github = FakeGitHub()
        service = self.service = ReviewService.__new__(ReviewService)
        service.store = self.store
        service.settings = SimpleNamespace(default_tenant_id='tenant', max_diff_bytes=10000,
            auto_post_review=True, enabled_agents='lead,security,correctness-reliability,critic')
        service.chat_client = object()
        service.queue = RecordingQueue()
        service.reviewer, service.alerts = Mock(), Mock()
        service.observability = SimpleNamespace(span=lambda *args, **kwargs: nullcontext())
        service.github_client_for_installation = Mock(return_value=self.github)
        service._authorize_repository = Mock()
        self.review_calls = []

        def review(task_id, repository, pull_request, diff, tenant_id):
            self.review_calls.append((task_id, repository, pull_request, diff, tenant_id))
            report = ReviewReport(repository, pull_request, 'review complete', 'low')
            report.execution['reviewed_revision'] = self.store.get(task_id)['input'].get('reviewed_revision')
            if not self.store.succeed(task_id, report, TraceEvent(1, TaskState.SUCCESS, 'done', utc_now())):
                raise RuntimeCancelled('Task was cancelled')
            return report
        service._run_review = review

    def payload(self):
        return {'action': 'synchronize', 'number': 7, 'installation': {'id': 42},
            'repository': {'full_name': 'org/repo'}, 'pull_request': {
                'diff_url': 'https://github.com/org/repo/pull/7.diff',
                'issue_url': 'https://api.github.com/repos/org/repo/issues/7',
                'base': {'sha': 'base'}, 'head': {'sha': 'head'}}}

    def webhook(self, delivery='delivery', digest='digest'):
        return self.service.handle_github_pull_request(self.payload(), delivery, digest, 'tenant')

    def drain(self, task_id):
        try:
            self.service._process_queued({'task_id': task_id})
        finally:
            self.service.queue.active.discard(task_id)
