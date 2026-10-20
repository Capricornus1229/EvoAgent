"""Eleven lifecycle contracts: review, recovery, cancellation, budgets and delivery."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from io import BytesIO
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from helpers import FakeAgenticClient
from helpers.lifecycle import (DIFF, CountingClient, WebhookFixture, close_service,
                               finish_queue, make_service)
from evoagent.agentic_core import AgenticReviewer, BoundedRole
from evoagent.diff_parser import parse_unified_diff
from evoagent.harness import ReviewHarness
from evoagent.llm import JsonChatClient
from evoagent.models import ReviewReport, TaskState, TraceEvent
from evoagent.repository_tools import RepositoryToolSuite
from evoagent.runtime import (AgentRuntime, AgentTool, RuntimeBudgetExceeded,
    RuntimeCancelled, RuntimeNode, ToolRegistry, check_execution, execution_scope,
    execution_timeout)
from evoagent.service import TaskConflictError
from evoagent.store import TaskStore, utc_now
from evoagent.task_queue import DeliveryAlreadyRunning, PermanentTaskError, TaskQueue
from evoagent.telemetry import ExecutionLedger, make_token_budget_policy


def envelope(task_id='task'):
    return {'message_id': task_id, 'attempt': 0, 'payload': {'task_id': task_id}}


class FakeClock:
    def __init__(self):
        self.value, self.lock = 100.0, threading.Lock()

    def now(self):
        with self.lock:
            return self.value

    def advance(self, seconds):
        with self.lock:
            self.value += seconds


class TaskLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()

    @contextmanager
    def clock_scope(self):
        with patch('evoagent.runtime.time.monotonic', side_effect=self.clock.now):
            yield

    def test_sync_and_async_reviews_commit_complete_reports(self):
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous), tempfile.TemporaryDirectory() as directory:
                service, client = make_service(directory, async_workers=1)
                try:
                    if asynchronous:
                        result = service.enqueue_review('org/repo', DIFF)
                        finish_queue(service.queue)
                        task_id = result['task_id']
                    else:
                        task_id = service.create_review('org/repo', DIFF)['task_id']
                    task = service.store.get(task_id)
                    self.assertEqual('SUCCESS', task['state'])
                    self.assertEqual(['PLANNING', 'EXECUTING', 'REVIEWING', 'SUCCESS'],
                                     [event['state'] for event in task['trace']])
                    self.assertEqual(DIFF, service.store.get_task_payload(task_id))
                    self.assertTrue(task['report']['collaboration']['coverage']['complete_professional_review'])
                    self.assertTrue(service.store.load_checkpoints(task_id))
                    self.assertEqual((1, 1, 2), (client.calls['security'],
                        client.calls['correctness-reliability'], client.calls['lead']))
                    before = dict(client.calls)
                    self.assertEqual('SUCCESS', service.resume_task(task_id)['state'])
                    self.assertEqual(before, dict(client.calls))
                finally:
                    close_service(service)

    def test_active_and_just_persisted_sync_reviews_cannot_resume_twice(self):
        for case in ('sync-running', 'async-running', 'just-persisted'):
            visible_after_save = case == 'just-persisted'
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                client = CountingClient(block=not visible_after_save)
                service, _ = make_service(directory, client, async_workers=1)
                try:
                    with patch.object(service.reviewer, 'reset_failed_assignments',
                            wraps=service.reviewer.reset_failed_assignments) as reset:
                        if visible_after_save:
                            original_save = service.store.save_task_payload
                            responses = []
                            def save_and_resume(task_id, diff):
                                original_save(task_id, diff)
                                responses.append(service.resume_task(task_id))
                            with patch.object(service.store, 'save_task_payload', side_effect=save_and_resume), \
                                    patch.object(service.queue, 'submit') as submit:
                                result = service.create_review('org/repo', DIFF)
                            self.assertEqual(1, len(responses))
                            self.assertTrue(responses[0]['active'])
                            submit.assert_not_called()
                        elif case == 'async-running':
                            pending = service.enqueue_review('org/repo', DIFF)
                            try:
                                self.assertTrue(client.started.wait(2))
                                self.assertTrue(service.resume_task(pending['task_id'])['active'])
                                self.assertEqual('EXECUTING', service.store.get(pending['task_id'])['state'])
                            finally:
                                client.release.set()
                            finish_queue(service.queue)
                            result = service.store.get(pending['task_id'])
                        else:
                            with ThreadPoolExecutor(max_workers=1) as pool:
                                future = pool.submit(service.create_review, 'org/repo', DIFF)
                                try:
                                    self.assertTrue(client.started.wait(2))
                                    task = service.store.list_tasks()[0]
                                    self.assertTrue(service.resume_task(task['id'])['active'])
                                    self.assertEqual('EXECUTING', service.store.get(task['id'])['state'])
                                finally:
                                    client.release.set()
                                result = future.result(timeout=5)
                        reset.assert_not_called()
                    self.assertEqual('SUCCESS', result['state'])
                    self.assertEqual(1, client.calls['security'])
                    self.assertEqual(1, client.calls['correctness-reliability'])
                finally:
                    client.release.set()
                    close_service(service)

    def test_failed_recovery_reuses_successful_worker_and_preserves_dead_letter_history(self):
        with tempfile.TemporaryDirectory() as directory:
            client = CountingClient()
            client.fail_security = True
            service, _ = make_service(directory, client, async_workers=1, queue_max_attempts=1)
            try:
                result = service.enqueue_review('org/repo', DIFF)
                finish_queue(service.queue)
                task_id = result['task_id']
                self.assertEqual('FAILED', service.store.get(task_id)['state'])
                history = copy.deepcopy(service.queue.dead_letters())
                self.assertEqual(1, len(history))
                session = service.store.load_checkpoints(task_id)['agentic-lead-session']['state']['session']
                good = next(item for item in session['worker_results'].values()
                            if item['worker'] == 'correctness-reliability')
                good_attempt = copy.deepcopy(session['assignment_attempts'][good['run_id']])
                client.fail_security = False
                self.assertEqual('PENDING', service.resume_task(task_id)['state'])
                finish_queue(service.queue)
                self.assertEqual('SUCCESS', service.store.get(task_id)['state'])
                self.assertIsNone(service.store.get(task_id)['error'])
                self.assertEqual(1, client.calls['correctness-reliability'])
                self.assertEqual(4, client.calls['security'])
                restored = service.store.load_checkpoints(task_id)['agentic-lead-session']['state']['session']
                self.assertEqual(good, restored['worker_results'][good['run_id']])
                self.assertEqual(good_attempt, restored['assignment_attempts'][good['run_id']])
                self.assertEqual(history, service.queue.dead_letters())
            finally:
                close_service(service)

    def test_cancellation_is_terminal_and_cannot_be_overwritten_or_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = WebhookFixture(directory)
            service, store = fixture.service, fixture.store
            identity = fixture.webhook()['task_id']
            self.assertTrue(service.cancel_task(identity, 'tenant'))
            with self.assertRaises(TaskConflictError):
                service.resume_task(identity, 'tenant')
            with self.assertRaises(RuntimeCancelled):
                fixture.drain(identity)
            self.assertFalse(store.succeed(identity, ReviewReport('org/repo', 7, 'late', 'low'),
                TraceEvent(1, TaskState.SUCCESS, 'late', utc_now())))
            store.fail(identity, 'late error', TraceEvent(2, TaskState.FAILED, 'late', utc_now()))
            self.assertEqual('CANCELLED', store.get(identity)['state'])
            self.assertEqual([], fixture.review_calls)
            store.create('cancel-error', 'org/repo', 7, {})
            class CancellingReviewer:
                name = 'fake'
                after_success = Mock()
                def review(self, diff, parsed):
                    store.request_cancel('cancel-error')
                    raise RuntimeError('tool failed after cancellation')
            reviewer = CancellingReviewer()
            with self.assertRaises(RuntimeCancelled):
                ReviewHarness(store, reviewer, node_retries=0).run('cancel-error', 'org/repo', 7, DIFF)
            self.assertEqual([], store.list_task_failure_cases('cancel-error'))
            reviewer.after_success.assert_not_called()
            # Separate connections race completion against cancellation; the
            # stored state, cancel flag and report must describe one winner.
            other = TaskStore(store.path)
            for index in range(2):
                identity = 'race-%d' % index
                store.create(identity, 'org/repo', 7, {})
                barrier = threading.Barrier(2)
                def succeed():
                    barrier.wait(timeout=2)
                    return store.succeed(identity, ReviewReport('org/repo', 7, 'ok', 'low'),
                        TraceEvent(1, TaskState.SUCCESS, 'ok', utc_now()))
                def cancel():
                    barrier.wait(timeout=2)
                    return other.request_cancel(identity)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    completed, cancelled = pool.submit(succeed), pool.submit(cancel)
                    completed.result(timeout=5)
                    cancelled.result(timeout=5)
                task = store.get(identity)
                self.assertIn(task['state'], {'SUCCESS', 'CANCELLED'})
                self.assertEqual(task['state'] == 'CANCELLED', bool(task['cancel_requested']))
                self.assertEqual(task['state'] == 'SUCCESS', task['report'] is not None)
        self._cancel_cached_checkpoint()
        self._cancel_after_node()
        self._queue_cancel_is_acked()

    def test_task_deadline_and_step_budget_stop_later_roles_tools_and_retries(self):
        with self.clock_scope():
            self._worker_inherits_deadline()
            self._worker_budget_is_not_retried()
            with tempfile.TemporaryDirectory() as directory:
                service, _ = make_service(directory, max_steps=2)
                try:
                    with self.assertRaises(RuntimeBudgetExceeded):
                        service.create_review('org/repo', DIFF)
                    task = service.store.get(service.store.list_tasks()[0]['id'])
                    self.assertEqual('FAILED', task['state'])
                    self.assertIsNone(task['report'])
                    self.assertNotIn('SUCCESS', [event['state'] for event in task['trace']])
                finally:
                    close_service(service)

    def test_nested_role_deadlines_and_parallel_task_contexts_remain_independent(self):
        with self.clock_scope():
            self._parallel_contexts()
            self._nested_role_deadline()

    def test_shared_token_budget_reservations_usage_and_interrupted_resume(self):
        atomic = ExecutionLedger('agentic', token_budget_policy=make_token_budget_policy(100, {'lead': 100}))
        barrier = threading.Barrier(2)
        def compete():
            barrier.wait(timeout=2)
            try:
                atomic.reserve('lead', 60, 10)
                return True
            except RuntimeBudgetExceeded:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: compete(), range(2)))
        self.assertEqual(1, sum(results))
        never_called = Mock()
        with self.assertRaises(RuntimeBudgetExceeded):
            atomic.complete_json(never_called, 'lead', 'x' * 1000, 'task', 1)
        never_called.complete_json.assert_not_called()
        failed_save = ExecutionLedger('agentic', token_budget_policy=make_token_budget_policy(100, {'lead': 100}),
                                      persist=Mock(side_effect=OSError('disk unavailable')))
        with self.assertRaises(OSError):
            failed_save.complete_json(never_called, 'lead', 'system', 'task', 10)
        self.assertEqual(100, failed_save.remaining_tokens('lead'))
        never_called.complete_json.assert_not_called()

        policy = make_token_budget_policy(1000, {"lead": 1000, "security": 1000})
        saved = []
        ledger = ExecutionLedger("agentic", token_budget_policy=policy, persist=lambda value: saved.append(value))
        first, output = ledger.reserve("lead", 100, 600)
        second, other_output = ledger.reserve("security", 100, 600)
        self.assertEqual((600, 200), (output, other_output))
        self.assertEqual(1000, saved[-1]["token_budget"]["charged_tokens"])
        with self.assertRaises(RuntimeBudgetExceeded):
            ledger.reserve("lead", 1, 1)
        restored = ExecutionLedger("agentic", token_budget_policy=policy)
        restored.restore({**saved[-1], 'duration_ms': 1234})
        self.assertGreaterEqual(restored.summary()['duration_ms'], 1234)
        self.assertEqual(0, restored.remaining_tokens("lead"))
        self.assertEqual(2, restored.summary()["estimated_usage_calls"])
        self.assertEqual(1000, restored.summary()["total_tokens"])

        class PaidFinal:
            provider, model = "fake", "fake"
            def __init__(self):
                self.calls = 0
            def complete_json(self, role, system, user, ledger=None, max_tokens=None):
                self.calls += 1
                ledger.record_model(role, self.provider, self.model,
                    {"prompt_tokens": 600, "completion_tokens": 100}, 0)
                return {"action": "final"}
        client = PaidFinal()
        shared = ExecutionLedger("agentic", token_budget_policy=policy)
        role = BoundedRole("lead", "Review diff JSON.", client, 1000, 60, max_steps=1)
        role.run("{}", ToolRegistry(), shared)
        with self.assertRaises(RuntimeBudgetExceeded):
            role.run("{}", ToolRegistry(), shared)
        self.assertEqual(1400, shared.summary()["total_tokens"])
        with self.assertRaises(RuntimeBudgetExceeded):
            role.run("{}", ToolRegistry(), shared)
        self.assertEqual(2, client.calls)

        real_client = JsonChatClient("https://invalid.test", "fake", "fake")
        failure = BytesIO(json.dumps({"usage": {"prompt_tokens": 123, "completion_tokens": 45},
            "choices": [{"message": {"content": "invalid JSON"}}]}).encode())
        accounted = ExecutionLedger("agentic", token_budget_policy=policy)
        with patch("evoagent.llm.urllib.request.urlopen", return_value=failure):
            with self.assertRaises(RuntimeError):
                accounted.complete_json(real_client, "lead", "system", "task", 200)
        self.assertEqual(168, accounted.summary()["total_tokens"])
        self.assertEqual(1, accounted.summary()["failed_model_calls"])
        self.assertEqual("reported", accounted.summary()["model_call_log"][0]["usage_source"])
        invalid_cost = BytesIO(json.dumps({'usage': {'prompt_tokens': 17, 'completion_tokens': 5, 'cost': 'invalid'},
            'choices': [{'message': {'content': '{"action":"final"}'}}]}).encode())
        with patch('evoagent.llm.urllib.request.urlopen', return_value=invalid_cost):
            self.assertEqual('final', accounted.complete_json(real_client, 'lead', 'system', 'task', 30)['action'])
        self.assertEqual(190, accounted.summary()['total_tokens'])
        self.assertEqual(2, accounted.summary()['llm_calls'])

        class MissingUsage:
            provider, model = "fake", "fake"
            def complete_json(self, *args, **kwargs):
                return {"action": "final"}
        accounted.complete_json(MissingUsage(), "lead", "system", "task", 100)
        self.assertEqual("estimated-unknown", accounted.summary()["model_call_log"][-1]["usage_source"])
        self.assertGreater(accounted.summary()["token_budget"]["charged_tokens"], 268)

    def test_http_and_subprocess_timeouts_use_remaining_task_budget(self):
        with self.clock_scope():
            self._http_timeout_is_capped()
            self._expired_http_never_starts()
            self._subprocess_timeout_is_capped()

    def test_redis_retries_and_dead_letters_are_durable_before_ack(self):
        self._retry_persisted_before_ack()
        self._failed_retry_stays_pending()
        self._malformed_entry_goes_to_dlq()
        for dead_letter_fails in (False, True):
            with self.subTest(dead_letter_fails=dead_letter_fails):
                queue = TaskQueue(Mock(side_effect=PermanentTaskError('permanent')), workers=1)
                self.addCleanup(queue.close)
                queue._redis = Mock()
                if dead_letter_fails:
                    queue._redis.xadd.side_effect = ConnectionError('DLQ unavailable')
                    with self.assertRaises(ConnectionError):
                        queue._process_redis_entry('1-0', {'envelope': json.dumps(envelope())})
                    queue._redis.xack.assert_not_called()
                else:
                    queue._process_redis_entry('1-0', {'envelope': json.dumps(envelope())})
                    self.assertEqual(queue.DLQ, queue._redis.xadd.call_args.args[0])
                    queue._redis.xack.assert_called_once()
                self.assertFalse(queue.is_active('task'))

    def test_local_reservation_covers_waiting_execution_and_retry_backoff(self):
        self._reservation_spans_backoff()
        queue = TaskQueue(Mock(), workers=1)
        self.addCleanup(queue.close)
        with queue.inline_execution('task'):
            prepare = Mock()
            queue.submit({'task_id': 'task'}, prepare=prepare)
            prepare.assert_not_called()
            with self.assertRaises(DeliveryAlreadyRunning):
                with queue.inline_execution('task'):
                    pass
            with self.assertRaises(DeliveryAlreadyRunning):
                queue._deliver(envelope())
        self.assertFalse(queue.is_active('task'))
        with self.assertRaises(RuntimeError):
            with queue.inline_execution('task'):
                raise RuntimeError('failed synchronously')
        self.assertFalse(queue.is_active('task'))
    def test_redis_worker_reconnects_after_read_failures(self):
        queue = TaskQueue(lambda _payload: queue._stop.set(), workers=1)
        self.addCleanup(queue.close)
        redis = Mock()
        queue._redis = redis
        queue._reclaim_stale = Mock()
        redis.xreadgroup.side_effect = [
            ConnectionError("temporary disconnect"),
            ConnectionError("still unavailable"),
            [(queue.STREAM, [("1-0", {"envelope": json.dumps(envelope())})])],
        ]
        with patch.object(queue._stop, "wait", return_value=False) as backoff:
            queue._redis_worker()
        self.assertEqual([1, 2], [call.args[0] for call in backoff.call_args_list])
        self.assertEqual(3, redis.xreadgroup.call_count)
        redis.xack.assert_called_once_with(queue.STREAM, queue.GROUP, "1-0")
        self.assertFalse(queue.is_active("task"))


    def test_redis_running_entry_renews_lease_without_duplicate_delivery(self):
        started, release, renewed = threading.Event(), threading.Event(), threading.Event()

        class LeaseRedis:
            def __init__(self):
                self.now = 0
                self.last_touch = 0
                self.claim_calls = []
                self.acks = []
                self.lock = threading.Lock()

            def xclaim(self, stream, group, consumer, **options):
                with self.lock:
                    self.claim_calls.append(options)
                    if len(self.claim_calls) == 1:
                        raise ConnectionError("one failed heartbeat")
                    self.last_touch = self.now
                    renewed.set()
                return options["message_ids"]

            def xautoclaim(self, stream, group, consumer, min_idle_time, **options):
                with self.lock:
                    stale = (self.now - self.last_touch) * 1000 >= min_idle_time
                return ["0-0", [("1-0", {"envelope": json.dumps(envelope())})] if stale else []]

            def xack(self, stream, group, identity):
                self.acks.append(identity)

        redis = LeaseRedis()
        owner = TaskQueue(lambda _payload: (started.set(), release.wait(2)), workers=1, lease_seconds=.03)
        other_handler = Mock()
        other = TaskQueue(other_handler, workers=1, lease_seconds=.03)
        self.addCleanup(other.close)
        self.addCleanup(owner.close)
        owner._redis = other._redis = redis
        future = owner._executor.submit(owner._process_redis_entry, "1-0", {"envelope": json.dumps(envelope())})
        try:
            self.assertTrue(started.wait(2))
            with redis.lock:
                # The original delivery would now be stale without renewal.
                redis.now = 1
                renewed.clear()
            self.assertTrue(renewed.wait(2))
            other._reclaim_stale()
            other_handler.assert_not_called()
            self.assertTrue(owner.is_active("task"))
            self.assertEqual([], redis.acks)
        finally:
            release.set()
            future.result(timeout=2)
        self.assertEqual(["1-0"], redis.acks)
        self.assertGreaterEqual(len(redis.claim_calls), 2)
        self.assertTrue(all(call == {"min_idle_time": 0, "message_ids": ["1-0"], "justid": True}
                            for call in redis.claim_calls))
        self.assertFalse(owner.is_active("task"))


    def _worker_inherits_deadline(self):
        clock = self.clock

        class ExpiringWorker(FakeAgenticClient):
            def __init__(self):
                self.calls = []
                self.worker_timeout = None

            def complete_json(self, role, system, user, ledger=None, max_tokens=None):
                task = json.loads(json.loads(user)["task"])
                self.calls.append((role, task.get("phase", "worker")))
                if role == "security":
                    self.worker_timeout = execution_timeout(120)
                    clock.advance(2)
                    return {"action": "tool", "tool": "changed_line", "arguments": {"path": "app.py", "line": 1}}
                result = super().complete_json(role, system, user, ledger, max_tokens)
                if role == "lead" and task["phase"] == "delegate":
                    result["risk_level"] = "high"
                return result

        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(os.path.join(directory, "test.db"))
            store.create("task", "org/repo", None, {
                "mode": "agentic", "enabled_agents": ["lead", "security", "critic"],
            })
            client = ExpiringWorker()
            reviewer = AgenticReviewer(store, client, default_time_budget=60)
            harness = ReviewHarness(store, reviewer, timeout_seconds=1, node_retries=2)
            with patch.object(RepositoryToolSuite, "changed_line") as factual_tool:
                with self.assertRaises(RuntimeBudgetExceeded):
                    harness.run("task", "org/repo", None, DIFF)
            factual_tool.assert_not_called()
            self.assertLessEqual(client.worker_timeout, 1)
            self.assertEqual([("lead", "delegate"), ("security", "worker")], client.calls)
            self.assertEqual("FAILED", store.get("task")["state"])
            self.assertIsNone(store.get("task")["report"])


    def _worker_budget_is_not_retried(self):
        class BudgetFailure(FakeAgenticClient):
            def __init__(self):
                self.security_calls = 0

            def complete_json(self, role, *args, **kwargs):
                if role == "security":
                    self.security_calls += 1
                    raise RuntimeBudgetExceeded("worker deadline exhausted")
                return super().complete_json(role, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(os.path.join(directory, "test.db"))
            store.create("task", "org/repo", None, {"mode": "agentic", "enabled_agents": ["lead", "security"]})
            client = BudgetFailure()
            harness = ReviewHarness(store, AgenticReviewer(store, client), node_retries=2)
            with self.assertRaises(RuntimeBudgetExceeded):
                harness.run("task", "org/repo", None, DIFF)
            self.assertEqual(1, client.security_calls)
            self.assertEqual("FAILED", store.get("task")["state"])


    def _parallel_contexts(self):
        ready = threading.Barrier(3)
        release = threading.Event()

        def run(seconds):
            try:
                with execution_scope(seconds):
                    ready.wait(timeout=2)
                    if not release.wait(2):
                        raise AssertionError("test did not release tasks")
                    check_execution()
                    return execution_timeout(100)
            except RuntimeBudgetExceeded:
                return "expired"

        with ThreadPoolExecutor(max_workers=2) as pool:
            short, long = pool.submit(run, 5), pool.submit(run, 50)
            try:
                ready.wait(timeout=2)
                self.clock.advance(6)
            finally:
                release.set()
            self.assertEqual("expired", short.result(timeout=2))
            self.assertEqual(44, long.result(timeout=2))
        self.assertEqual(100, execution_timeout(100))
        check_execution()


    def _nested_role_deadline(self):
        tool = Mock(side_effect=RuntimeBudgetExceeded("tool exhausted deadline"))
        registry = ToolRegistry([AgentTool("work", "test tool", {"properties": {}}, tool)])
        client = Mock()
        observed_limits = []

        def complete(*args, **kwargs):
            observed_limits.append(execution_timeout(120))
            return {"action": "tool", "tool": "work", "arguments": {}}

        client.complete_json.side_effect = complete
        role = BoundedRole("security", "review", client, 8000, 3, max_steps=3)
        with self.assertRaises(RuntimeBudgetExceeded):
            with execution_scope(10):
                role.run("{}", registry, ExecutionLedger("agentic"))
        self.assertEqual([3], observed_limits)
        self.assertEqual(1, client.complete_json.call_count)
        tool.assert_called_once()


    def _http_timeout_is_capped(self):
        client = JsonChatClient("https://invalid.test", "fake", "fake-model", timeout=60)
        response = BytesIO(json.dumps({"choices": [{"message": {"content": '{"ok":true}'}}]}).encode())
        with patch("evoagent.llm.urllib.request.urlopen", return_value=response) as request:
            with execution_scope(10):
                self.clock.advance(3)
                self.assertEqual({"ok": True}, client.complete_json("lead", "system", "task"))
        self.assertEqual(7, request.call_args.kwargs["timeout"])
        self.assertEqual(60, client.timeout)
        self.assertEqual(60, execution_timeout(client.timeout))


    def _expired_http_never_starts(self):
        client = JsonChatClient("https://invalid.test", "fake", "fake-model")
        with patch("evoagent.llm.urllib.request.urlopen") as request:
            with self.assertRaises(RuntimeBudgetExceeded):
                with execution_scope(1):
                    self.clock.advance(1)
                    client.complete_json("lead", "system", "task")
        request.assert_not_called()


    def _subprocess_timeout_is_capped(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "app.py").write_text("value = 1\n")
            suite = RepositoryToolSuite(root, DIFF, parse_unified_diff(DIFF),
                                        test_command="fake-tests", execution_allowed=True)
            registry = suite.registry("security", {"run_repository_checks"})
            with patch("evoagent.repository_tools.subprocess.run", return_value=SimpleNamespace(
                    returncode=0, stdout="passed", stderr="")) as run:
                with execution_scope(5):
                    self.clock.advance(1)
                    result = registry.invoke("run_repository_checks", {"kind": "tests"})
                self.assertTrue(result["output"]["passed"])
                self.assertEqual(4, run.call_args.kwargs["timeout"])
            with patch("evoagent.repository_tools.subprocess.run") as run:
                with self.assertRaises(RuntimeBudgetExceeded):
                    with execution_scope(1):
                        self.clock.advance(1)
                        registry.invoke("run_repository_checks", {"kind": "tests"})
            run.assert_not_called()


    def _retry_persisted_before_ack(self):
        handler = Mock(side_effect=[RuntimeError("retry once"), None])
        queue = TaskQueue(handler, workers=1, max_attempts=2)
        self.addCleanup(queue.close)
        queue._redis = Mock()
        queue._process_redis_entry("1-0", {"envelope": json.dumps(envelope())})
        retry_call = queue._redis.xadd.call_args
        self.assertEqual(queue.STREAM, retry_call.args[0])
        retry = json.loads(retry_call.args[1]["envelope"])
        self.assertEqual(("task", 1), (retry["message_id"], retry["attempt"]))
        self.assertTrue(queue.is_active("task"))
        queue._process_redis_entry("2-0", {"envelope": json.dumps(retry)})
        self.assertEqual(2, handler.call_count)
        self.assertEqual(2, queue._redis.xack.call_count)
        self.assertEqual(1, queue._redis.xadd.call_count)
        self.assertFalse(queue.is_active("task"))


    def _failed_retry_stays_pending(self):
        queue = TaskQueue(Mock(side_effect=RuntimeError("retry")), workers=1)
        self.addCleanup(queue.close)
        queue._redis = Mock()
        queue._redis.xadd.side_effect = ConnectionError("retry could not be persisted")
        with self.assertRaises(ConnectionError):
            queue._process_redis_entry("1-0", {"envelope": json.dumps(envelope())})
        queue._redis.xack.assert_not_called()
        self.assertTrue(queue.is_active("task"))
        self.assertNotIn("task", queue._running)


    def _malformed_entry_goes_to_dlq(self):
        queue = TaskQueue(Mock(), workers=1)
        self.addCleanup(queue.close)
        queue._redis = Mock()
        queue._redis.xautoclaim.return_value = ["0-0", [("bad-0", {"envelope": "[]"})]]
        queue._reclaim_stale()
        queue.handler.assert_not_called()
        args = queue._redis.xadd.call_args.args
        self.assertEqual(queue.DLQ, args[0])
        self.assertIn("queue envelope must be an object", json.loads(args[1]["envelope"])["error"])
        queue._redis.xack.assert_called_once_with(queue.STREAM, queue.GROUP, "bad-0")


    def _cancel_cached_checkpoint(self):
        store = Mock()
        store.load_checkpoints.return_value = {'cached': {'status': 'completed', 'state': {'done': True}}}
        handler = Mock(return_value={})
        with self.assertRaises(RuntimeCancelled):
            AgentRuntime().execute({}, [RuntimeNode('cached', handler)], task_id='task',
                                   checkpoint_store=store, cancel_check=lambda: True)
        handler.assert_not_called()
        store.save_checkpoint.assert_not_called()


    def _cancel_after_node(self):
        store = Mock()
        store.load_checkpoints.return_value = {}
        cancelled = [False]
        def handler(state):
            cancelled[0] = True
            return {'done': True}
        with self.assertRaises(RuntimeCancelled):
            AgentRuntime().execute({}, [RuntimeNode('work', handler)], task_id='task',
                                   checkpoint_store=store, cancel_check=lambda: cancelled[0])
        store.save_checkpoint.assert_not_called()


    def _queue_cancel_is_acked(self):
        done = threading.Event()
        calls = []
        def handler(payload):
            calls.append(payload)
            done.set()
            raise RuntimeCancelled('cancelled')
        queue = TaskQueue(handler, workers=1, max_attempts=3)
        try:
            queue.submit({'task_id': 'cancel'})
            self.assertTrue(done.wait(2))
            queue._executor.submit(lambda: None).result(timeout=2)
            self.assertEqual([], queue.dead_letters())
            self.assertEqual(1, len(calls))
            self.assertFalse(queue.is_active('cancel'))
        finally:
            queue.close()


    def _reservation_spans_backoff(self):
        started, release = threading.Event(), threading.Event()
        attempts = []
        timers = []
        class CapturedTimer:
            def __init__(self, delay, target, args):
                self.target, self.args = target, args
                timers.append(self)
            def start(self):
                return None
        def handler(payload):
            attempts.append(payload)
            started.set()
            if len(attempts) == 1:
                release.wait(2)
                raise RuntimeError('retry')
        queue = TaskQueue(handler, workers=1, max_attempts=2)
        try:
            with patch('evoagent.task_queue.threading.Timer', CapturedTimer):
                prepare = Mock()
                queue.submit({'task_id': 'task'}, prepare=prepare)
                self.assertTrue(started.wait(2))
                queue.submit({'task_id': 'task'}, prepare=prepare)
                self.assertEqual(1, len(attempts))
                release.set()
                queue._executor.submit(lambda: None).result(timeout=2)
                self.assertTrue(queue.is_active('task'))
                self.assertEqual(set(), queue._running)
                queue.submit({'task_id': 'task'}, prepare=prepare)
                self.assertEqual(1, len(attempts))
                prepare.assert_called_once()
                timers[0].target(*timers[0].args)
                queue._executor.submit(lambda: None).result(timeout=2)
                self.assertEqual(2, len(attempts))
                self.assertFalse(queue.is_active('task'))
                self.assertEqual([], queue.dead_letters())
        finally:
            release.set()
            queue.close()


if __name__ == '__main__':
    unittest.main()
