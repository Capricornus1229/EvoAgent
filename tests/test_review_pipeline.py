"""Review orchestration and evidence regressions; fake models, temporary stores only."""
import copy
import json
import os
import tempfile
import unittest
from collections import Counter

from helpers import FakeAgenticClient
from evoagent.agentic_core import AgenticReviewer, CANDIDATE_MANIFEST_VERSION
from evoagent.diff_parser import parse_unified_diff
from evoagent.gates import FindingGate
from evoagent.harness import ReviewHarness
from evoagent.models import Finding, Severity
from evoagent.store import TaskStore


DIFF = ('--- /dev/null\n+++ b/stats.py\n@@ -0,0 +1,3 @@\n'
        '+def average(values):\n'
        '+    """Return 0.0 when values is empty."""\n'
        '+    return sum(values) / len(values)\n')
FIXED_DIFF = ('--- a/stats.py\n+++ b/stats.py\n@@ -1,3 +1,5 @@\n'
              ' def average(values):\n'
              '     """Return 0.0 when values is empty."""\n'
              '+    if not values:\n+        return 0.0\n'
              '     return sum(values) / len(values)\n')


def issue(**changes):
    value = {
        'rule_id': 'average-empty', 'cwe': 'CWE-369', 'severity': 'high',
        'title': 'Empty input divides by zero',
        'explanation': 'average([]) raises ZeroDivisionError instead of returning 0.0.',
        'path': 'stats.py', 'line': 3, 'evidence': 'return sum(values) / len(values)',
        'fix': 'Return 0.0 before dividing when values is empty.',
        'test': 'assert average([]) == 0.0', 'confidence': 1.0,
        'evidence_ids': [], 'call_chain': [],
    }
    value.update(changes)
    return value


class PipelineClient(FakeAgenticClient):
    """Keep model choices explicit without claiming real-model accuracy."""
    def __init__(self, findings=(), security=(), groups=(), failures=None):
        self.findings = list(findings)
        self.security = list(security)
        self.groups = list(groups)
        self.failures = dict(failures or {})
        self.calls = Counter()
        self.critic_candidates = []

    def complete_json(self, role, system, user, ledger=None, max_tokens=None):
        task = json.loads(json.loads(user)['task'])
        phase = task.get('phase', 'worker')
        self.calls[(role, phase)] += 1
        if ledger:
            failed = isinstance(self.failures.get(role), Exception)
            ledger.record_model(role, self.provider, self.model,
                                {'prompt_tokens': 10, 'completion_tokens': 5}, 1,
                                not failed, str(self.failures.get(role, '')) if failed else '')
        if role in self.failures:
            failure = self.failures[role]
            if isinstance(failure, Exception):
                raise failure
            return copy.deepcopy(failure)
        if role == 'lead' and phase == 'finalize':
            return {'action': 'final', 'accepted_finding_indices': list(range(len(task['candidate_findings']))),
                    'confidence_adjustments': [], 'duplicate_groups': self.groups}
        if role == 'critic':
            self.critic_candidates = copy.deepcopy(task['candidates'])
        if role in ('security', 'correctness-reliability'):
            return {'action': 'final', 'findings': copy.deepcopy(
                self.security if role == 'security' else self.findings)}
        return super().complete_json(role, system, user, None, max_tokens)


class RevisionClient(PipelineClient):
    def complete_json(self, role, system, user, ledger=None, max_tokens=None):
        task = json.loads(json.loads(user)['task'])
        phase = task.get('phase', 'worker')
        if role == 'lead' and phase == 'assess-workers':
            self.calls[(role, phase)] += 1
            if ledger:
                ledger.record_model(role, self.provider, self.model,
                                    {'prompt_tokens': 10, 'completion_tokens': 5}, 1)
            assignment = next(item for item in task['assignments'] if item['worker'] == 'security')
            return {'action': 'final', 'revision_requests': [{
                'assignment_id': assignment['assignment_id'], 'worker': 'security',
                'guidance': 'Report the reproducible empty-input failure.',
                'required_evidence': ['exact changed-line evidence'],
            }], 'critic_objective': 'Verify the observed counterexample.'}
        if role == 'security' and self.calls[(role, 'worker')] == 1:
            self.security = [issue()]
        result = super().complete_json(role, system, user, ledger, max_tokens)
        if role == 'lead' and phase == 'delegate':
            result['risk_level'] = 'high'
        return result


class ReviewPipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = TaskStore(os.path.join(self.directory.name, 'review.db'))
        self.sequence = 0

    def task(self):
        self.sequence += 1
        identity = 'task-%d' % self.sequence
        self.store.create(identity, 'demo/repo', None, {'mode': 'agentic'})
        return identity

    def review(self, client, diff=DIFF, repository='demo/repo'):
        task = self.task()
        reviewer = AgenticReviewer(self.store, client)
        report = ReviewHarness(self.store, reviewer).run(task, repository, None, diff)
        return task, reviewer, report

    def finding(self, **changes):
        value = issue(**changes)
        value.pop('evidence_ids')
        value['severity'] = Severity(value['severity'])
        return Finding(**value)

    def test_high_average_defect_needs_no_optional_tool_reference_or_chain(self):
        _task, _reviewer, report = self.review(PipelineClient([issue()]))
        self.assertEqual(1, len(report.findings))
        self.assertEqual('high', report.risk)
        self.assertEqual('complete', report.execution['review_status'])
        self.assertEqual('findings', report.execution['review_outcome'])
        self.assertTrue(report.findings[0].gate['exact_location_evidence'])
        self.assertEqual([], report.findings[0].evidence_refs)
        self.assertEqual([], report.findings[0].call_chain)

    def test_guard_fix_has_no_old_division_finding_or_checkpoint_leak(self):
        _task, reviewer, broken = self.review(PipelineClient([issue()]))
        self.assertEqual(1, len(broken.findings))
        reviewer.client = PipelineClient()
        fixed_task = self.task()
        # A fake model supplies no findings for the guarded Diff. This exercises
        # task isolation and reporting, not a claim about model accuracy.
        fixed = ReviewHarness(self.store, reviewer).run(fixed_task, 'demo/repo', None, FIXED_DIFF)
        self.assertEqual([], fixed.findings)
        self.assertEqual('clean', fixed.execution['review_outcome'])
        self.assertEqual(0, fixed.execution['finding_counts']['raw_candidates'])
        self.assertIsNone(AgenticReviewer._restore_findings([issue(cwe=None)])[0].cwe)

    def test_same_cwe_and_location_keep_distinct_candidates_and_stable_indices(self):
        client = PipelineClient([issue(confidence=.8), issue(
            rule_id='another-defect', title='A different defect',
            explanation='A different observable failure on this expression.')])
        _task, _reviewer, report = self.review(client)
        self.assertEqual([0, 1], [item['finding_index'] for item in client.critic_candidates])
        self.assertEqual([0, 1], [item.candidate_index for item in report.findings])
        self.assertEqual(2, report.execution['finding_counts']['final_published'])

    def test_exact_and_lead_grouped_duplicates_merge_after_individual_gates(self):
        for explicit_group in (False, True):
            with self.subTest(explicit_group=explicit_group):
                security = issue(rule_id='division-by-zero', severity='medium', confidence=.9,
                                 evidence_ids=['diff:line3']) if explicit_group else issue()
                client = PipelineClient([issue()], security=[security],
                                        groups=[[0, 1]] if explicit_group else [])
                _task, _reviewer, report = self.review(client)
                self.assertEqual(2, len(client.critic_candidates))
                self.assertEqual(1, len(report.findings))
                final = report.findings[0]
                self.assertEqual({'security', 'correctness-reliability'},
                                 {item['source'] for item in final.provenance})
                self.assertTrue(all('provenance' not in item['finding'] for item in final.provenance))
                self.assertEqual({'raw_candidates': 2, 'lead_selected': 2, 'gate_accepted': 2,
                                  'gate_rejected': 0, 'final_published': 1}, report.execution['finding_counts'])
                if explicit_group:
                    original = next(item for item in final.provenance if item['source'] == 'security')
                    self.assertEqual('diff:line3', original['finding']['evidence_refs'][0]['evidence_id'])
                    self.assertTrue(original['gate']['warnings'])
                    self.assertEqual([], final.evidence_refs)

    def test_unsupported_confident_duplicate_cannot_hide_a_valid_report(self):
        _task, _reviewer, report = self.review(PipelineClient([
            issue(confidence=.8), issue(title='Unsupported duplicate', evidence='invented code'),
        ], groups=[[0, 1]]))
        self.assertEqual(1, len(report.findings))
        self.assertEqual(0, report.findings[0].candidate_index)
        self.assertEqual('needs_review', report.execution['review_status'])
        self.assertEqual('partial', report.execution['review_outcome'])
        self.assertEqual(1, report.execution['finding_counts']['gate_rejected'])

    def test_complete_evidence_wins_and_verified_references_never_form_a_fake_chain(self):
        client = PipelineClient([
            issue(),
            issue(rule_id='grounded', severity='medium', confidence=.8,
                  evidence_ids=['diff:stats.py:3'], call_chain=[
                      {'path': 'stats.py', 'line': 3, 'symbol': 'average'}]),
            issue(rule_id='alternate', cwe='CWE-682', severity='medium', confidence=.7,
                  evidence_ids=['ast:one'], call_chain=[
                      {'path': 'stats.py', 'line': 3, 'symbol': 'sum'}]),
        ], groups=[[0, 1, 2]])
        task = self.task()
        reviewer = AgenticReviewer(self.store, client)
        reviewer._register_observation(task, {'ok': True, 'result': {
            'evidence_id': 'ast:one', 'tool': 'ast_analyze',
            'output': {'path': 'stats.py', 'line': 3, 'valid': True}}})
        report = ReviewHarness(self.store, reviewer).run(task, 'demo/repo', None, DIFF)
        final = report.findings[0]
        self.assertEqual(1, final.candidate_index)
        self.assertEqual('grounded', final.rule_id)
        self.assertEqual(Severity.HIGH, final.severity)
        self.assertEqual({'diff:stats.py:3', 'ast:one'}, {ref['evidence_id'] for ref in final.evidence_refs})
        self.assertEqual(1, len(final.call_chain))
        self.assertEqual(3, len(final.provenance))

    def test_invalid_optional_metadata_warns_and_never_substitutes_for_real_evidence(self):
        for grounded in (True, False):
            with self.subTest(grounded=grounded):
                finding = self.finding(evidence='return sum(values) / len(values)' if grounded else 'invented')
                finding.evidence_refs = [{'evidence_id': 'fabricated'}, {'evidence_id': 'foreign'}]
                finding.call_chain = [{'path': 'stats.py', 'line': 3, 'symbol': 'imaginary'}]
                registry = {'foreign': {'task_id': 'other-task', 'tool': 'ast_analyze',
                                        'output': {'path': 'stats.py', 'line': 3}}}
                result = FindingGate().apply([finding], parse_unified_diff(DIFF), registry, 'task')
                self.assertEqual(int(grounded), len(result.accepted))
                self.assertEqual([], finding.evidence_refs)
                self.assertEqual([], finding.call_chain)
                self.assertEqual(2, len(finding.gate['warnings']))
                self.assertEqual(0, finding.gate['strong_evidence_count'])
                if not grounded:
                    self.assertIn('no matching code', result.rejected[0]['reasons'][0])

    def test_gate_requires_location_confidence_and_high_severity_fix_and_test(self):
        for change, reason in (({'fix': ''}, 'release gate'), ({'test': ''}, 'release gate'),
                               ({'line': 99}, 'format gate'), ({'title': ''}, 'format gate'),
                               ({'confidence': .4}, 'confidence gate')):
            with self.subTest(change=change):
                result = FindingGate().apply([self.finding(**change)], parse_unified_diff(DIFF))
                self.assertEqual([], result.accepted)
                self.assertTrue(any(reason in item for item in result.rejected[0]['reasons']))

    def test_withheld_results_are_unknown_and_legal_empty_results_are_clean(self):
        for withheld in (True, False):
            with self.subTest(withheld=withheld):
                client = PipelineClient([issue(evidence='invented')] if withheld else [])
                task, reviewer, report = self.review(client, repository=self.directory.name)
                self.assertEqual([], report.findings)
                self.assertFalse(report.execution['repository_context']['available'])
                self.assertEqual('unknown' if withheld else 'low', report.risk)
                self.assertEqual('needs_review' if withheld else 'complete', report.execution['review_status'])
                self.assertEqual('withheld' if withheld else 'clean', report.execution['review_outcome'])
                calls = dict(client.calls)
                reviewer.review_with_context(task, DIFF, parse_unified_diff(DIFF), 'demo/repo')
                self.assertEqual(calls, dict(client.calls))

    def test_four_roles_execute_one_requested_revision_with_canonical_assignment(self):
        client = RevisionClient()
        task, reviewer, report = self.review(client)
        collaboration = report.collaboration
        self.assertEqual({'lead', 'security', 'correctness-reliability', 'critic'}, set(collaboration['roles']))
        self.assertEqual(2, client.calls[('security', 'worker')])
        self.assertEqual(1, client.calls[('correctness-reliability', 'worker')])
        self.assertEqual(1, client.calls[('lead', 'assess-workers')])
        self.assertEqual(1, client.calls[('critic', 'worker')])
        self.assertEqual(1, len(report.findings))
        self.assertEqual(1, len(collaboration['revision_results']))
        self.assertEqual('high-risk-one-revision-round', collaboration['stop_reason'])
        self.assertTrue(reviewer.validate_completion(task)['completed'])

    def test_failed_or_malformed_workers_cannot_produce_a_success_report(self):
        failures = [
            {'security': RuntimeError('offline'), 'correctness-reliability': RuntimeError('offline')},
            *({'security': value} for value in ({'action': 'final'}, {'action': 'final', 'findings': None},
                                               {'action': 'final', 'findings': [None]})),
        ]
        for failure in failures:
            with self.subTest(failure=failure):
                task = self.task()
                client = PipelineClient(failures=failure)
                reviewer = AgenticReviewer(self.store, client)
                with self.assertRaises(ValueError):
                    ReviewHarness(self.store, reviewer).run(task, 'demo/repo', None, DIFF)
                saved = self.store.get(task)
                self.assertEqual('FAILED', saved['state'])
                self.assertIsNone(saved['report'])
                self.assertEqual(3, client.calls[('security', 'worker')])
                self.assertEqual(0, client.calls[('critic', 'worker')])
                with self.assertRaises(ValueError):
                    reviewer.validate_completion(task)

    def test_explicit_recovery_reuses_successful_workers_and_completed_checkpoints(self):
        task = self.task()
        client = PipelineClient([issue()], failures={'security': RuntimeError('offline')})
        reviewer = AgenticReviewer(self.store, client)
        harness = ReviewHarness(self.store, reviewer)
        with self.assertRaises(ValueError):
            harness.run(task, 'demo/repo', None, DIFF)
        client.failures.clear()
        calls = dict(client.calls)
        with self.assertRaises(ValueError):
            harness.run(task, 'demo/repo', None, DIFF)
        self.assertEqual(calls, dict(client.calls))
        reviewer.reset_failed_assignments(task)
        report = harness.run(task, 'demo/repo', None, DIFF)
        self.assertEqual(1, len(report.findings))
        self.assertEqual(4, client.calls[('security', 'worker')])
        self.assertEqual(1, client.calls[('correctness-reliability', 'worker')])
        self.assertEqual('SUCCESS', self.store.get(task)['state'])
        calls = dict(client.calls)
        harness.run(task, 'demo/repo', None, DIFF)
        self.assertEqual(calls, dict(client.calls))

    def test_old_candidate_manifest_rebuilds_only_critic_and_lead_stages(self):
        task = self.task()
        first = AgenticReviewer(self.store, PipelineClient([issue(), issue()]))
        first.review_with_context(task, DIFF, parse_unified_diff(DIFF), 'demo/repo')
        checkpoint = self.store.load_checkpoints(task)['agentic-lead-session']
        session = checkpoint['state']['session']
        self.assertEqual([0, 1], [item['candidate_index'] for item in session['candidate_manifest']])
        session.pop('candidate_manifest_version')
        session['critic_candidates'] = session['critic_candidates'][:1]
        self.store.save_checkpoint(task, 'agentic-lead-session', checkpoint['state'], 'completed', 1)
        client = PipelineClient()
        reviewer = AgenticReviewer(self.store, client)
        with self.assertRaisesRegex(ValueError, 'candidate manifest'):
            reviewer.validate_completion(task)
        findings = reviewer.review_with_context(task, DIFF, parse_unified_diff(DIFF), 'demo/repo')
        self.assertEqual(1, len(findings))
        self.assertEqual(Counter({('critic', 'worker'): 1, ('lead', 'finalize'): 1}), client.calls)
        self.assertEqual(CANDIDATE_MANIFEST_VERSION,
                         reviewer._completion_session(task)['candidate_manifest_version'])
        self.assertTrue(reviewer.validate_completion(task)['completed'])

    def test_diff_parser_preserves_added_line_numbers_and_rejects_invalid_input(self):
        diff = ('--- a/app.py\n+++ b/app.py\n@@ -2,3 +2,4 @@\n keep = True\n-old = 1\n'
                '+new = 2\n+eval(user_input)\n tail = 3\n')
        parsed = parse_unified_diff(diff)
        self.assertEqual(['app.py'], parsed.files)
        self.assertEqual([(3, 'new = 2'), (4, 'eval(user_input)')],
                         [(line.line, line.content) for line in parsed.added_lines])
        invalid = ('not a diff', '--- a/x.py\n+++ b/x.py\n@@ -1 +1,2 @@\n-old\n+new\n',
                   '--- a/../x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n',
                   '--- a/x.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n')
        for value in invalid:
            with self.subTest(diff=value):
                task = self.task()
                client = PipelineClient()
                with self.assertRaises(ValueError):
                    ReviewHarness(self.store, AgenticReviewer(self.store, client)).run(task, 'demo/repo', None, value)
                self.assertEqual('FAILED', self.store.get(task)['state'])
                self.assertFalse(client.calls)

    def test_critic_lead_and_duplicate_groups_require_explicit_valid_indices(self):
        invalid_decisions = ([], [{'finding_index': True, 'accepted': True}],
                             [{'finding_index': 0, 'accepted': 'false'}],
                             [{'finding_index': 5, 'accepted': True}])
        for decisions in invalid_decisions:
            with self.subTest(decisions=decisions), self.assertRaises(ValueError):
                AgenticReviewer._validate_critic({'decisions': decisions}, 1)
        candidates = [issue(), issue(), issue(line=2)]
        for groups, accepted in (([[0]], [0, 1]), ([[0, 0]], [0, 1]),
                                 ([[0, 1], [1, 2]], [0, 1, 2]),
                                 ([[0, 1]], [0]), ([[0, 2]], [0, 1, 2])):
            with self.subTest(groups=groups), self.assertRaises(ValueError):
                AgenticReviewer._validate_lead_final({
                    'accepted_finding_indices': accepted, 'confidence_adjustments': [],
                    'duplicate_groups': groups}, len(candidates), candidates)
        with self.assertRaises(ValueError):
            AgenticReviewer._validate_lead_final({'confidence_adjustments': []}, 1)
        for invalid in (True, float('nan'), float('inf'), '0.1'):
            with self.subTest(adjustment=invalid), self.assertRaises(ValueError):
                AgenticReviewer._validate_lead_final({
                    'accepted_finding_indices': [0], 'confidence_adjustments': [
                        {'finding_index': 0, 'adjustment': invalid}]}, 1)


if __name__ == '__main__':
    unittest.main()
