"""Core context and memory regressions, using temporary stores."""
import json

import os

import tempfile

import unittest

from evoagent.agentic_core import AgenticReviewer

from evoagent.context_manager import ContextManager, estimate_tokens

from evoagent.diff_parser import parse_unified_diff

from evoagent.memory import MemoryManager
from evoagent.gates import FindingGate
from evoagent.models import Finding, Severity
from evoagent.runtime import RuntimeBudgetExceeded

from evoagent.store import TaskStore
from evoagent.store import utc_now

def large_diff():
    values = []
    for index in range(12):
        path = "src/module_%02d.py" % index
        content = "value_%d = transform(input_%d)" % (index, index)
        if index == 9:
            path = "src/auth.py"
            content = "result = eval(user_token)"
        values.extend([
            "--- a/%s" % path,
            "+++ b/%s" % path,
            "@@ -1,1 +1,21 @@ function_%d" % index,
            " context = True",
            "+%s" % content,
            "+audit(value_%d)" % index,
            *["+trace_%d_%d = normalize(value_%d)" % (index, line, index) for line in range(18)],
        ])
    return "\n".join(values) + "\n"


def medium_hunk():
    lines = ['value_%03d = "%s"' % (number, 'x' * 42) for number in range(1, 152)]
    lines[119] = 'result = eval(user_input)'
    def render():
        return '--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,151 @@\n' + '\n'.join('+' + line for line in lines) + '\n'
    while estimate_tokens(render()) < 2863:
        lines[-1] += ' '
    return render()

class CapturingClient:
    provider = 'fake'
    model = 'fake-model'

    def __init__(self):
        self.tasks = []

    def complete_json(self, role, _system, user, ledger=None, max_tokens=None):
        managed = json.loads(user)
        task = json.loads(managed['task'])
        self.tasks.append((role, task, managed))
        if ledger:
            ledger.record_model(role, self.provider, self.model, {'prompt_tokens': 20, 'completion_tokens': 5}, 1)
        if role == 'lead' and task['phase'] == 'delegate':
            return {'action': 'final', 'delegations': [{'assignment_id': 'security-1', 'worker': 'security', 'objective': 'Review authentication and dynamic execution.', 'files': ['src/auth.py'], 'risk_domains': ['authorization']}], 'risk_level': 'high'}
        if role == 'lead' and task['phase'] == 'assess-workers':
            return {'action': 'final', 'revision_requests': [], 'critic_objective': 'Verify candidates.'}
        if role == 'lead' and task['phase'] == 'finalize':
            return {'action': 'final', 'accepted_finding_indices': [], 'confidence_adjustments': []}
        if role in {'security', 'correctness-reliability'}:
            return {'action': 'final', 'findings': []}
        if role == 'critic':
            return {'action': 'final', 'decisions': []}
        raise AssertionError((role, task))

class ContextManagerTests(unittest.TestCase):

    def test_large_diff_uses_risk_ranked_hunk_map_reduce(self):
        manager = ContextManager(context_window_tokens=4096, input_token_budget=2500, diff_token_budget=900, map_chunk_tokens=160)
        compressed = manager.compress_diff(large_diff(), 'task', 'security', focus_files=['src/auth.py'], risk_domains=['authorization', 'token'])
        self.assertEqual('semantic-diff-v2', compressed['format'])
        self.assertTrue(compressed['compression']['applied'])
        self.assertGreater(compressed['compression']['map_chunks'], 1)
        self.assertIn('src/auth.py', {item['path'] for item in compressed['selected_hunks']})
        self.assertLess(compressed['compressed_estimated_tokens'], compressed['original_estimated_tokens'])
        self.assertLessEqual(compressed['compressed_estimated_tokens'], 900)
        self.assertEqual(compressed['compressed_estimated_tokens'], manager._diff_payload_cost(compressed))

    def test_medium_hunk_is_complete_when_final_view_fits(self):
        manager = ContextManager()
        diff = medium_hunk()
        self.assertEqual(2863, estimate_tokens(diff))
        payload = manager.compress_diff(diff, 'medium')
        self.assertEqual('full', payload['view'])
        self.assertFalse(payload['compression']['applied'])
        self.assertEqual(151, payload['compression']['retained_lines'])
        self.assertEqual(0, payload['compression']['omitted_lines'])
        self.assertTrue(manager.render_diff_view(payload).endswith(diff))
        self.assertLessEqual(payload['compressed_estimated_tokens'], 12000)

    def test_excerpt_rows_preserve_real_locations_and_omitted_ranges(self):
        manager = ContextManager()
        diff = medium_hunk()
        payload = manager.compress_diff(diff, max_tokens=512)
        view = manager.render_diff_view(payload)
        self.assertEqual('numbered-excerpts', payload['view'])
        self.assertTrue(payload['compression']['applied'])
        self.assertIn('old=- new=120 prefix="+" | result = eval(user_input)', view)
        self.assertNotIn('@@ -0,0 +1,151 @@', view)
        self.assertNotIn('...<omitted>...', view)
        selected = payload['selected_hunks'][0]
        self.assertEqual(151, len(selected['rows']) + sum(gap['lines'] for gap in selected['omitted_ranges']))
        for line, accepted in ((120, True), (2, False)):
            finding = Finding('SEC-EVAL', Severity.HIGH, 'Dynamic execution',
                              'Untrusted input is evaluated.', 'app.py', line,
                              'result = eval(user_input)', 'Use a parser.', 'Test malicious input.')
            self.assertEqual(accepted, bool(FindingGate().apply([finding], parse_unified_diff(diff)).accepted))
        # Deletions increment only old-side positions; additions only new-side.
        body = [' context_%d = "%s"' % (i, 'x' * 40) for i in range(100)]
        body += ['-old_result = eval(user_input)', '+result = eval(user_input)']
        body += [' tail_%d = "%s"' % (i, 'x' * 40) for i in range(50)]
        mixed = '--- a/src/统计 helper.py\n+++ b/src/统计 helper.py\n@@ -40,151 +80,151 @@\n' + '\n'.join(body) + '\n'
        excerpt = manager.compress_diff(mixed, max_tokens=900)
        rows = [row for hunk in excerpt['selected_hunks'] for row in hunk['rows']]
        self.assertIn({'old_line': 140, 'new_line': None, 'prefix': '-',
                       'content': 'old_result = eval(user_input)'}, rows)
        self.assertIn({'old_line': None, 'new_line': 180, 'prefix': '+',
                       'content': 'result = eval(user_input)'}, rows)
        self.assertIn('path="src/统计 helper.py"', manager.render_diff_view(excerpt))
        self.assertLessEqual(excerpt['compressed_estimated_tokens'], 900)
        # The final fit step must also report line loss when source text alone fits.
        short = '--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,200 @@\n' + '+x=1\n' * 200
        self.assertLess(estimate_tokens(short), 512)
        fitted = manager.compress_diff(short, max_tokens=512)
        self.assertTrue(fitted['compression']['applied'])
        self.assertGreater(fitted['compression']['omitted_lines'], 0)
        self.assertNotIn('...<omitted>...', manager.render_diff_view(fitted))

    def test_old_observations_are_summarized_and_evidence_is_retained(self):
        manager = ContextManager(observation_token_budget=350, recent_observations=1)
        observations = [{'step': index, 'tool': 'read_file', 'ok': True, 'result': {'evidence_id': 'read_file:%d' % index, 'tool': 'read_file', 'output': {'path': 'app.py', 'content': 'x' * 1600}}} for index in range(1, 5)]
        compact, stats = manager.compact_observations(observations, 350)
        self.assertGreater(stats['summarized'], 0)
        rendered = json.dumps(compact)
        self.assertIn('read_file:4', rendered)
        self.assertLess(estimate_tokens(compact), 500)

    def test_managed_context_is_trimmed_before_model_call(self):
        manager = ContextManager(context_window_tokens=3000, input_token_budget=1200, observation_token_budget=300, recent_observations=1)
        payload = manager.compress_diff(medium_hunk(), 'managed')
        task = json.dumps({'phase': 'review', 'diff': manager.render_diff_view(payload),
                           'diff_context': manager.diff_metadata(payload), '_diff_payload': payload,
                           'candidate_findings': [{'finding_index': index, 'explanation': 'b' * 1200} for index in range(8)]})
        managed, stats = manager.build_managed_context(task, [], [], 4000, 60, system_prompt='review', max_output_tokens=512,
                                                       max_input_tokens=1200)
        self.assertLessEqual(stats['estimated_input_tokens_after'], stats['input_token_limit'])
        compact_task = json.loads(managed['task'])
        self.assertEqual(8, len(compact_task['candidate_findings']))
        self.assertEqual(7, compact_task['candidate_findings'][7]['finding_index'])
        self.assertNotIn('_diff_payload', compact_task)
        self.assertNotIn('...<omitted>...', compact_task['diff'])
        self.assertIn('new=120', compact_task['diff'])
        self.assertEqual(stats['estimated_input_tokens_after'],
                         estimate_tokens(json.dumps(managed, ensure_ascii=False, default=str)))
        event = manager.summary('managed')['compressions'][-1]
        for key in ('applied', 'omitted_lines', 'retained_lines'):
            self.assertEqual(compact_task['diff_context']['compression'][key], event[key])
        self.assertEqual(compact_task['diff_context']['compressed_estimated_tokens'], event['compressed_tokens'])

    def test_hard_caps_never_invent_partial_code_or_use_token_floors(self):
        manager = ContextManager()
        for cap in (0, 1, 64, 128):
            with self.subTest(cap=cap), self.assertRaises(RuntimeBudgetExceeded):
                manager.build_managed_context(json.dumps({'diff': 'essential evidence' * 300}), [], [], 1000, 60,
                                              max_input_tokens=cap)
        for requested in (0, 1, 32, 127):
            self.assertLessEqual(manager.output_token_limit('review', requested), requested)
        long_line = '--- /dev/null\n+++ b/app.py\n@@ -0,0 +1 @@\n+result = eval("' + 'x' * 8000 + '")\n'
        with self.assertRaisesRegex(RuntimeBudgetExceeded, 'complete diff evidence line'):
            manager.compress_diff(long_line, max_tokens=512)
        opaque = json.dumps({'diff': 'x' * 10000, 'candidate_findings': [
            {'finding_index': 0, 'path': 'app.py', 'line': 120, 'evidence': 'exact code'}]})
        for compact in (manager._compact_task, manager._minimal_task):
            with self.assertRaises(RuntimeBudgetExceeded):
                compact(opaque, 500)

class ContextMemoryIntegrationTests(unittest.TestCase):

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        self.store = TaskStore(self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_review_recall_and_semantic_diff_reach_agent_context(self):
        diff = large_diff()
        self.store.create('task', 'org/repo', 1, {'mode': 'agentic', 'enabled_agents': ['lead', 'security']}, 'tenant-a')
        memory = MemoryManager(self.store)
        memory.remember('tenant-a', 'org/repo', 'semantic', 'review_feedback', 'Dynamic execution in auth token handling was a confirmed missed issue.', importance=0.95)
        client = CapturingClient()
        manager = ContextManager(context_window_tokens=12000, input_token_budget=4000, diff_token_budget=1200)
        reviewer = AgenticReviewer(self.store, client, memory_manager=memory, context_manager=manager)
        reviewer.review_with_context('task', diff, parse_unified_diff(diff), 'org/repo', 'tenant-a')
        summary = reviewer.collaboration_summary('task')
        self.assertTrue(client.tasks)
        for _role, task, managed in client.tasks:
            self.assertIsInstance(task['diff'], str)
            self.assertIn('semantic-diff-v2', task['diff'])
            self.assertEqual('semantic-diff-v2', task['diff_context']['format'])
            self.assertTrue(task['recalled_memory']['items'])
            self.assertLessEqual(estimate_tokens(managed), managed['context_policy']['input_token_limit'])
        context = summary['context_management']
        self.assertTrue(context['semantic_compression_enabled'])
        self.assertGreater(context['compression_calls'], 0)
        self.assertEqual(1, context['memory_recall']['recalled'])
        self.assertIn('context_management', summary['execution'])

    def test_memory_isolation_expiry_and_task_consolidation(self):
        memory = MemoryManager(self.store)
        for tenant, repository, content in (
            ('tenant-a', 'org/repo', 'SEC-EVAL confirmed review feedback'),
            ('tenant-b', 'org/repo', 'SEC-EVAL another tenant'),
            ('tenant-a', 'org/other', 'SEC-EVAL another repository'),
        ):
            memory.remember(tenant, repository, 'semantic', 'review_feedback', content)
        recalled = memory.recall('tenant-a', 'org/repo', 'SEC-EVAL feedback')
        self.assertEqual(1, len(recalled))
        self.assertIn('confirmed', recalled[0]['content'])
        for task, agent in (('task', 'security'), ('task', 'correctness-reliability'),
                            ('other-task', 'security')):
            memory.remember_observation('tenant-a', 'org/repo', task, agent, {
                'step': 1, 'tool': 'lookup', 'ok': True,
                'result': {'evidence_id': task + ':' + agent, 'output': 'verified fact'},
            })
        security = memory.recall_working('tenant-a', 'org/repo', 'task', agent='security')
        self.assertEqual(1, len(security))
        self.assertEqual([], memory.recall_working('tenant-a', 'org/repo', 'task', agent='critic'))
        self.store.save_agent_memory({
            'id': 'expired-memory', 'tenant_id': 'tenant-a', 'repository': 'org/repo',
            'task_id': 'expired-task', 'agent': 'security', 'scope': 'working',
            'kind': 'observation', 'content': 'expired fact', 'keywords': ['expired'],
            'metadata': {}, 'importance': .5, 'created_at': utc_now(),
            'expires_at': '2000-01-01T00:00:00+00:00',
        })
        memory.recall('tenant-a', 'org/repo', 'expired')
        self.assertNotIn('expired-memory', {
            item['id'] for item in self.store.list_agent_memories(
                'tenant-a', 'org/repo', ('working',), 10)
        })
        memory.consolidate_task('tenant-a', 'org/repo', 'task', {'accepted_findings': []})
        self.assertEqual([], memory.recall_working('tenant-a', 'org/repo', 'task'))
        self.assertEqual(1, len(memory.recall_working('tenant-a', 'org/repo', 'other-task')))
        episodes = self.store.list_agent_memories('tenant-a', 'org/repo', ('episodic',), 10)
        self.assertTrue(any(item['kind'] == 'task_summary' for item in episodes))
