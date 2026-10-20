"""Twelve regressions for evaluated Prompt/Skill changes and feedback lifecycle.

All services, replay stores and CLI imports use temporary databases. The fake
model follows the real Lead/Workers/Critic protocol; no model or GitHub request
leaves the process.
"""
from contextlib import redirect_stdout
from dataclasses import replace
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from helpers.skills import artifact, skill_markdown, SkillAwareClient, RISK_DIFF, CLEAN_DIFF
from evoagent.agentic_core import AgenticReviewer
from evoagent.api import ApiHandler
from evoagent.config import Settings
from evoagent.diff_parser import parse_unified_diff
from evoagent.evolution import EvolutionEngine, RegressionEvaluator, seed_builtin_evaluation_cases
from evoagent.evolution_v2 import RootCauseEvolutionGenerator, validate_structured_candidate
from evoagent.evaluation_v2 import SingleModelReviewer, ProductArmReviewer, ProductionEvaluationHarness
from evoagent.evaluation_experiments import SkillEvolutionExperimentSuite
from evoagent.models import Finding, Severity
from evoagent.repository_tools import RepositoryToolSuite
from evoagent.runtime import RuntimeBudgetExceeded, ToolRegistry
from evoagent.service import ReviewService
from evoagent.skill_evolution import SkillEvolutionEngine, AgentSkillReplayReviewer
from evoagent.skills import AgentSkill, SkillRegistry
from evoagent.store import TaskStore


SKILL = "review-dangerous-calls"
BASE_PROMPT = "Review diff and return JSON findings with severity, fix and test."
PROMPT = BASE_PROMPT + " Confirm SEC-DANGEROUS-CALL."
EXPECTED = [{"path": "a.py", "line": 1, "rule_id": "SEC-DANGEROUS-CALL", "min_severity": "high"}]


class EvolutionClient(SkillAwareClient):
    """The candidate catches one defect; optional modes reproduce real failures."""
    def __init__(self):
        self.fail = False
        self.regress_clean = False
        self.calls = []

    def complete_json(self, role, system, user, ledger=None, max_tokens=None):
        self.calls.append(role)
        if self.fail and role in {"security", "correctness-reliability"}:
            if ledger:
                ledger.record_model(role, self.provider, self.model,
                                    {"prompt_tokens": 10, "completion_tokens": 5}, 1,
                                    False, "professional worker unavailable")
            raise RuntimeError("professional worker unavailable")
        result = super().complete_json(role, system, user, ledger, max_tokens)
        if role != "security":
            return result
        task = json.loads(json.loads(user)["task"])
        enabled = "Confirm SEC-DANGEROUS-CALL." in system or "dangerous_call(data)" in system
        diff = json.dumps(task["diff"])
        risk = "dangerous_call(data)" in diff
        if enabled and (risk or self.regress_clean):
            finding = {
                "rule_id": "SEC-DANGEROUS-CALL", "severity": "high", "title": "Dangerous call",
                "explanation": "Confirmed unsafe API was added.", "path": "a.py" if risk else "b.py",
                "line": 1, "evidence": "dangerous_call(data)" if risk else "safe_call(data)",
                "fix": "Use the safe API.", "test": "Exercise the unsafe input.", "confidence": .9,
            }
            return {"action": "final", "findings": [finding]}
        return result


class FakeClock:
    def __init__(self):
        self.value = 100.0
        self.lock = threading.Lock()

    def now(self):
        with self.lock:
            return self.value

    def advance(self, amount):
        with self.lock:
            self.value += amount


class EvolutionTests(unittest.TestCase):
    def test_candidates_without_shared_budget_proof_require_reevaluation(self):
        store = Mock()
        store.get_prompt_run.return_value = {"decision": "ready", "metrics": {
            "production": {"schema": "prompt-shared-runtime-v1", "snapshot": {}},
        }}
        prompt = EvolutionEngine(store, replay_factory=Mock())
        with self.assertRaisesRegex(ValueError, "reevaluate"):
            prompt.activate("llm-review", 2)
        store.activate_skill_version.assert_not_called()
        skill = SkillEvolutionEngine(store, runtime_snapshot_provider=Mock())
        skill._evaluated_run = Mock(return_value={"decision": "ready", "metrics": {
            "reproducibility": {"runtime_snapshot": {"task_execution": {
                "max_steps": 8, "node_retries": 2, "timeout_seconds": 120,
            }}},
        }})
        with self.assertRaisesRegex(ValueError, "reevaluate"):
            skill.activate(SKILL, 2)
        store.commit_skill_activation.assert_not_called()

    def test_experiment_arms_share_input_output_pool_and_lead_cannot_claim_twice(self):
        class MeteredClient(SkillAwareClient):
            def complete_json(self, role, system, user, ledger=None, max_tokens=None):
                result = super().complete_json(role, system, user, None, max_tokens)
                ledger.record_model(role, self.provider, self.model,
                                    {"prompt_tokens": 2800, "completion_tokens": 200}, 1)
                return result

        client = MeteredClient()
        single = SingleModelReviewer("single-llm", client, 12000)
        full = ProductArmReviewer("full-agentic", client, 12000)
        replay = AgentSkillReplayReviewer(artifact(), client, 12000, total_token_budget=12000)
        self.addCleanup(replay.close)
        for reviewer in (single, full, replay):
            config = reviewer.evaluation_config()
            self.assertEqual(12000, config["token_budget_policy"]["total_limit"])
            self.assertEqual("input+output", config["token_budget_policy"]["unit"])
            self.assertTrue(all(value == 12000 for value in config["token_budget_policy"]["role_limits"].values()))
        # The formerly successful 5 x 3000 execution cannot finish within 12000.
        with self.assertRaises(RuntimeBudgetExceeded):
            full.review(RISK_DIFF, parse_unified_diff(RISK_DIFF))
        execution = full.evaluation_execution()
        self.assertGreater(execution["llm_calls"], 0)
        self.assertEqual(execution["llm_calls"] * 3000, execution["total_tokens"])

    def test_failed_evaluation_and_invalid_json_retain_this_cases_real_usage(self):
        class FailingClient:
            provider, model = "fake", "failure-meter"
            count = 0

            def complete_json(self, role, system, user, ledger=None, max_tokens=None):
                self.count += 1
                usage = {"prompt_tokens": 1600, "completion_tokens": 100, "cost": .01}
                if self.count == 1:
                    ledger.record_model(role, self.provider, self.model, usage, 1, False, "invalid JSON")
                    raise ValueError("invalid JSON")
                ledger.record_model(role, self.provider, self.model,
                                    {"prompt_tokens": 20, "completion_tokens": 10}, 1)
                return {"findings": []}

        reviewer = SingleModelReviewer("single-llm", FailingClient(), 12000)
        case = {"id": "json-failure", "repository": "demo/a", "pull_request": 1,
                "split": "validation", "diff": RISK_DIFF, "expected_findings": []}
        harness = ProductionEvaluationHarness()
        failed = harness._run_case(reviewer, case)
        self.assertFalse(failed["execution_success"])
        self.assertEqual((1, 1700, .01), (failed["llm_calls"], failed["total_tokens"], failed["cost_usd"]))
        succeeded = harness._run_case(reviewer, {**case, "id": "next-case"})
        self.assertTrue(succeeded["execution_success"])
        self.assertEqual((1, 30), (succeeded["llm_calls"], succeeded["total_tokens"]))
        malformed = harness._run_case(reviewer, {**case, "id": "bad-diff", "diff": None})
        self.assertFalse(malformed["execution_success"])
        self.assertEqual((0, 0), (malformed["llm_calls"], malformed["total_tokens"]))
        training_reviewer = SingleModelReviewer('single-llm', FailingClient(), 12000)
        suite = SkillEvolutionExperimentSuite(lambda _: training_reviewer)
        training = suite._collect_feedback(artifact(), [case, {**case, 'diff': None}])
        self.assertEqual(2, len(training['errors']))
        self.assertEqual((1, 1700, .01), (training['execution']['llm_calls'],
            training['execution']['total_tokens'], training['execution']['cost_usd']))

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.services = []

    def service(self, *, seed=True, baseline=False):
        root = Path(self.directory.name) / str(len(self.services))
        package = root / "skills" / SKILL
        (package / "references").mkdir(parents=True)
        (package / "SKILL.md").write_text(skill_markdown(include_rule=False))
        (package / "references" / "policy.md").write_text("Original disk resource")
        client = EvolutionClient()
        settings = Settings(
            host="127.0.0.1", port=0, db_path=str(root / "temporary.db"),
            max_diff_bytes=10000, max_steps=8, timeout_seconds=10,
            llm_base_url="https://invalid.test", llm_api_key="fake-key", llm_model=client.model,
            github_webhook_secret="", github_token="", auto_post_review=False,
            skills_dir=str(package.parent), eval_min_cases=1, eval_min_holdout_cases=1,
            eval_max_cases=2, eval_min_improvement=.01,
        )
        with patch("evoagent.service.JsonChatClient", return_value=client):
            service = ReviewService(settings)
        self.services.append(service)
        self.addCleanup(service.queue.close)
        if seed:
            service.store.save_evaluation_case("human-risk", "validation", RISK_DIFF, EXPECTED, "manual")
            service.store.save_evaluation_case("human-clean", "holdout", CLEAN_DIFF, [], "manual")
        if baseline:
            service.store.save_skill_version("llm-review", BASE_PROMPT, 0, True)
            service.store.save_skill_artifact(SKILL, artifact(include_rule=False), 0, True)
            service.reload_skills()
        return service, client

    def post(self, service, path, payload=None):
        handler = ApiHandler.__new__(ApiHandler)
        handler.service, handler.settings, handler.path = service, service.settings, path
        handler._read_body = lambda: json.dumps(payload or {}).encode()
        handler._send_json = Mock()
        handler.do_POST()
        handler._send_json.assert_called_once()
        return handler._send_json.call_args.args

    def propose(self, service, kind, *, manual=False):
        if kind == "prompt":
            return service.evolution._propose("llm-review", PROMPT, None,
                activation_policy="manual" if manual else "auto")
        result = service.skill_evolution.propose(SKILL, artifact())
        return result if manual else service.finish_skill_proposal(result, "default", True)

    def active(self, service, kind):
        return (service.store.get_active_skill_version("llm-review") if kind == "prompt"
                else service.store.get_active_skill_artifact(SKILL))

    def activate(self, service, kind, version):
        engine = service.evolution if kind == "prompt" else service.skill_evolution
        return engine.activate("llm-review" if kind == "prompt" else SKILL, version)

    def test_prompt_shared_replay_auto_activation_new_task_and_rollback(self):
        service, client = self.service()
        self.assertEqual([], service.create_review("org/repo", RISK_DIFF)["report"]["findings"])
        before_tasks = service.store.list_tasks()
        before_memory = service.store.list_agent_memories("default", "", ("semantic", "episodic", "working"), 200)
        status, result = self.post(service, "/v1/evolution/propose", {"skill_name": "llm-review", "prompt": PROMPT})
        self.assertEqual(201, status, result)
        self.assertEqual("activated", result["decision"], result)
        self.assertTrue(result["version"]["active"])
        self.assertGreater(result["candidate"]["score"], result["baseline"]["score"])
        self.assertEqual(before_tasks, service.store.list_tasks())
        self.assertEqual(before_memory, service.store.list_agent_memories("default", "", ("semantic", "episodic", "working"), 200))
        self.assertEqual(PROMPT, service.reviewer.prompt_overlay)
        run = service.store.get_prompt_run("llm-review", result["version"]["version"])
        snapshot = run["metrics"]["production"]["snapshot"]
        self.assertEqual(service._task_execution(), snapshot["task_execution"])
        self.assertTrue({"harness.py", "runtime.py"}.issubset(snapshot["runtime_revision"]))
        report = service.create_review("org/repo", RISK_DIFF)["report"]
        self.assertEqual(["SEC-DANGEROUS-CALL"], [item["rule_id"] for item in report["findings"]])
        self.assertEqual({"lead", "security", "correctness-reliability", "critic"}, set(client.calls))
        status, response = self.post(service, "/v1/evolution/rollback", {"skill_name": "llm-review"})
        self.assertEqual((200, True), (status, response["rolled_back"]))
        self.assertIsNone(self.active(service, "prompt"))
        self.assertEqual("", service.reviewer.prompt_overlay)
        self.assertEqual([], service.create_review("org/repo", RISK_DIFF)["report"]["findings"])

    def test_skill_shared_replay_auto_activation_new_task_and_exact_rollback(self):
        service, _ = self.service()
        unrelated = artifact("other-review", include_rule=False)
        service.store.save_skill_artifact("other-review", unrelated, 0, True)
        before_tasks = service.store.list_tasks()
        status, result = self.post(service, "/v1/skill-evolution/propose", {"skill_name": SKILL, "artifact": artifact()})
        self.assertEqual(201, status, result)
        self.assertEqual("activated", result["decision"], result)
        self.assertTrue(result["version"]["active"])
        self.assertEqual(before_tasks, service.store.list_tasks())
        run = service.store.list_skill_evolution_runs()[0]
        proof = run["metrics"]["reproducibility"]
        self.assertEqual("Original disk resource", proof["baseline_artifact"]["files"]["references/policy.md"])
        self.assertEqual(service._task_execution(), proof["runtime_snapshot"]["task_execution"])
        findings = service.create_review("org/repo", RISK_DIFF)["report"]["findings"]
        self.assertEqual(["SEC-DANGEROUS-CALL"], [item["rule_id"] for item in findings])
        status, response = self.post(service, f"/v1/skill-evolution/{SKILL}/rollback")
        self.assertEqual((200, True), (status, response["activated"]))
        self.assertEqual(proof["baseline_artifact"], self.active(service, "skill")["artifact"])
        self.assertEqual(unrelated, service.store.get_active_skill_artifact("other-review")["artifact"])
        self.assertEqual([], service.create_review("org/repo", RISK_DIFF)["report"]["findings"])

    def test_failed_professional_replay_never_replaces_either_active_version(self):
        for kind in ("prompt", "skill"):
            with self.subTest(kind=kind):
                service, client = self.service(baseline=True)
                baseline = copy.deepcopy(self.active(service, kind))
                client.fail = True
                result = self.propose(service, kind)
                self.assertEqual("rejected", result["decision"], result)
                self.assertFalse(result["gates"]["evaluation_success"])
                self.assertTrue(result["candidate"]["errors"])
                self.assertEqual(0, result["candidate"]["success_rate"])
                self.assertEqual(baseline, self.active(service, kind))
                self.assertEqual([], service.store.list_tasks())

    def test_holdout_regression_blocks_both_candidates_despite_validation_gain(self):
        for kind in ("prompt", "skill"):
            with self.subTest(kind=kind):
                service, client = self.service(baseline=True)
                baseline = copy.deepcopy(self.active(service, kind))
                client.regress_clean = True
                result = self.propose(service, kind)
                self.assertGreater(result["candidate"]["score"], result["baseline"]["score"])
                self.assertTrue(result["gates"]["validation_improvement"])
                self.assertFalse(result["gates"]["holdout_non_regression"])
                self.assertEqual("rejected", result["decision"])
                self.assertEqual(baseline, self.active(service, kind))
                self.assertNotIn("case_results", result["candidate_holdout"])

    def test_activation_smoke_failure_preserves_both_exact_active_versions(self):
        for kind in ("prompt", "skill"):
            with self.subTest(kind=kind):
                service, client = self.service(baseline=True)
                baseline = copy.deepcopy(self.active(service, kind))
                result = self.propose(service, kind, manual=True)
                self.assertEqual("ready", result["decision"], result)
                self.assertFalse(result["version"]["active"])
                client.fail = True
                with self.assertRaisesRegex(ValueError, "smoke failed"):
                    self.activate(service, kind, result["version"]["version"])
                self.assertEqual(baseline, self.active(service, kind))
                self.assertEqual([], service.store.list_tasks())

    def test_total_task_budget_failures_are_replay_errors_and_preserve_live_state(self):
        for kind in ("prompt", "skill"):
            for budget in ("steps", "timeout"):
                with self.subTest(kind=kind, budget=budget):
                    service, client = self.service(baseline=True)
                    service.settings = replace(service.settings, max_steps=2 if budget == "steps" else 8,
                                               timeout_seconds=1 if budget == "timeout" else 10)
                    service.reload_skills()
                    baseline = copy.deepcopy(self.active(service, kind))
                    clock = FakeClock()
                    original = client.complete_json
                    def slow_model(*args, **kwargs):
                        clock.advance(.30)
                        return original(*args, **kwargs)
                    with patch("evoagent.runtime.time.monotonic", side_effect=clock.now), \
                            patch.object(client, "complete_json", side_effect=slow_model):
                        with self.assertRaisesRegex(RuntimeBudgetExceeded, "task execution budget"):
                            service.create_review("org/repo", RISK_DIFF)
                        before = service.store.list_tasks()
                        result = self.propose(service, kind)
                    self.assertEqual("rejected", result["decision"], result)
                    self.assertFalse(result["gates"]["evaluation_success"])
                    self.assertTrue(result["candidate"]["errors"])
                    self.assertEqual(0, result["candidate_holdout"]["clean_accuracy"])
                    self.assertEqual(baseline, self.active(service, kind))
                    self.assertEqual(before, service.store.list_tasks())

    def test_changed_runtime_data_or_candidate_cannot_activate_stale_proofs(self):
        for kind in ("prompt", "skill"):
            for change in ("configuration", "dataset", "candidate"):
                with self.subTest(kind=kind, change=change):
                    service, _ = self.service(baseline=True)
                    baseline = copy.deepcopy(self.active(service, kind))
                    result = self.propose(service, kind, manual=True)
                    self.assertEqual("ready", result["decision"], result)
                    version = result["version"]["version"]
                    if change == "configuration":
                        service.settings = replace(service.settings, max_steps=9)
                    elif change == "dataset":
                        service.store.save_evaluation_case("new-human", "holdout", CLEAN_DIFF, [], "manual")
                    else:
                        with service.store._connect() as connection:
                            if kind == "prompt":
                                connection.execute("UPDATE skill_versions SET prompt=? WHERE version=?", ("tampered", version))
                            else:
                                connection.execute("UPDATE skill_artifact_versions SET artifact_json=? WHERE version=? AND skill_name=?",
                                                   (json.dumps(artifact(include_rule=False)), version, SKILL))
                    with self.assertRaisesRegex(ValueError, "changed|configuration|artifact"):
                        self.activate(service, kind, version)
                    self.assertEqual(baseline, self.active(service, kind))

    def test_empty_defaults_and_explicit_seed_cli_are_idempotent(self):
        service, _ = self.service(seed=False)
        self.assertEqual(0, service.store.count_evaluation_cases())
        self.assertFalse(service.evolution.status()["ready"])
        self.assertFalse(service.skill_evolution.status()["ready"])
        path = Path(__file__).resolve().parents[1] / "scripts" / "seed_evaluation_cases.py"
        spec = importlib.util.spec_from_file_location("explicit_seed", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        output = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), redirect_stdout(output):
            for _ in range(2):
                self.assertEqual(0, module.main(["--builtin", "--db-path", service.settings.db_path]))
        self.assertEqual(7, service.store.count_evaluation_cases())
        self.assertEqual([7, 7], [json.loads(line)["cases"] for line in output.getvalue().splitlines()])
        seed_builtin_evaluation_cases(service.store)
        self.assertEqual((5, 2), (service.store.count_evaluation_cases("validation"), service.store.count_evaluation_cases("holdout")))
        pg = Mock()
        pg.save_evaluation_case.side_effect = lambda name, split, *args: {"name": name, "split": split}
        with patch.object(module, "create_store", return_value=pg) as factory, redirect_stdout(io.StringIO()):
            module.main(["--builtin", "--database-url", "postgresql://local/test", "--db-path", "ignored.db"])
        factory.assert_called_once_with("postgresql://local/test", "ignored.db")
        self.assertEqual(7, pg.save_evaluation_case.call_count)

    def test_manual_newest_selection_status_fingerprints_and_severity_aliases(self):
        store = TaskStore(str(Path(self.directory.name) / "selection.db"))
        seed_builtin_evaluation_cases(store)
        store.save_evaluation_case("old-human", "validation", RISK_DIFF, EXPECTED, "manual")
        newest = store.save_evaluation_case("new-human", "validation", RISK_DIFF, EXPECTED, "api")
        store.save_evaluation_case("inactive", "validation", RISK_DIFF, EXPECTED, "api", False)
        selected = store.list_evaluation_cases("validation", True, 2)
        self.assertEqual(["new-human", "old-human"], [case["name"] for case in selected])
        self.assertEqual(newest["id"], selected[0]["id"])
        prompt, skill = EvolutionEngine(store, max_cases=2), SkillEvolutionEngine(store, max_cases=2)
        for status in (prompt.status(), skill.status()):
            self.assertEqual((7, 2, 5), (status["validation_total_cases"], status["validation_selected_cases"], status["validation_omitted_cases"]))
        self.assertEqual(prompt._dataset_fingerprint(selected), prompt.status()["validation_dataset_fingerprint"])
        for outcome in ("hit", "miss", "error"):
            with self.subTest(outcome=outcome):
                reviewer = Mock(spec=["review"])
                line = parse_unified_diff(RISK_DIFF).added_lines[0]
                reviewer.review.return_value = [Finding("SEC-DANGEROUS-CALL", Severity.HIGH, "Risk", "Unsafe call",
                    line.path, line.line, line.content, "Use safe_call", "Regression", .9)] if outcome == "hit" else []
                if outcome == "error":
                    reviewer.review.side_effect = RuntimeError("provider unavailable")
                metrics = [RegressionEvaluator(lambda _prompt: reviewer).run("", [{"name": "same-case", "diff": RISK_DIFF,
                    "expected": [{"path": "a.py", "line": 1, field: "high"}]}]) for field in ("severity", "min_severity")]
                self.assertEqual(metrics[0], metrics[1])
                self.assertEqual(1 if outcome == "hit" else 0, metrics[0]["high_severity_recall"])

    def test_accepted_and_resolved_feedback_agree_with_history_and_pending_counts(self):
        service, _ = self.service()
        task_id = service.create_review("org/repo", RISK_DIFF)["task_id"]
        finding = {"rule_id": "SEC-DANGEROUS-CALL", "path": "a.py", "line": 1,
                   "severity": "high", "evidence": "dangerous_call(data)"}
        for category in ("accepted", "missed_issue", "false_positive"):
            service.record_feedback(task_id, category, finding, "Confirmed feedback")
        history = service.store.list_task_failure_cases(task_id)
        self.assertTrue(next(case for case in history if case["category"] == "accepted")["resolved"])
        self.assertEqual(2, service.store.dashboard_stats()["unresolved_failure_cases"])
        # Historical acknowledgements are ignored even before their resolved flag was fixed.
        with service.store._connect() as connection:
            connection.execute("UPDATE failure_cases SET resolved=0 WHERE category='accepted'")
        self.assertEqual({"missed_issue", "false_positive"}, {case["category"] for case in service.store.list_failure_cases(True)})
        result = service.skill_evolution.auto_propose(SKILL)
        self.assertEqual("ready", result["decision"], result)
        service.finish_skill_proposal(result, "default", True)
        pending = service.store.list_failure_cases(True)
        self.assertEqual(["false_positive"], [case["category"] for case in pending])
        service.store.resolve_failure_cases([case["id"] for case in pending])
        self.assertEqual(0, service.store.dashboard_stats()["unresolved_failure_cases"])
        self.assertEqual([], service.store.list_failure_cases(True))
        self.assertEqual(3, len(service.store.list_task_failure_cases(task_id)))

    def test_root_cause_receives_real_bounded_error_phase_and_validates_structure(self):
        service, client = self.service()
        client.fail = True
        with self.assertRaises(ValueError) as failure_error:
            service.create_review("org/repo", RISK_DIFF)
        failure = service.store.list_failure_cases(True)[0]
        self.assertEqual(str(failure_error.exception)[:1000], failure["payload"]["error"])
        self.assertEqual("EXECUTING", failure["payload"]["phase"])
        generator_client = Mock(provider="fake", model="fake")
        generator_client.complete_json.return_value = {"candidate": {}}
        generator = RootCauseEvolutionGenerator(generator_client)
        generator.generate([failure, {"id": 2, "task_id": "bounded", "category": "execution_error",
            "payload": {"error": "critic missing decisions: " + "x" * 4000, "phase": "critic" + "y" * 500}}], BASE_PROMPT)
        sent = json.loads(generator_client.complete_json.call_args.args[2])["failure_cases"]
        self.assertEqual(failure["payload"]["error"], sent[0]["error"])
        self.assertEqual("EXECUTING", sent[0]["phase"])
        self.assertTrue(sent[1]["error"].startswith("critic missing decisions"))
        self.assertEqual((2000, 200), (len(sent[1]["error"]), len(sent[1]["phase"])))
        for invalid in ({"budget_parameters": {"lead": True}}, {"budget_parameters": {"security": "infinite"}},
                        {"tool_selection_policy": [{"hypothesis": "check", "preferred_tools": ["shell"]}]}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_structured_candidate(invalid)

    def test_skill_loading_hot_reload_resources_and_composed_permissions(self):
        service, _ = self.service()
        package = Path(service.settings.skills_dir) / SKILL
        (package / "skill.py").write_text("raise RuntimeError('must never execute')\n")
        loaded = service.registry.get_agent_skill(SKILL)
        self.assertEqual("Original disk resource", loaded.read_resource("references/policy.md"))
        with self.assertRaises(ValueError):
            loaded.read_resource("../outside.txt")
        with self.assertRaisesRegex(ValueError, "frontmatter"):
            AgentSkill.from_markdown("# Missing metadata")
        with self.assertRaisesRegex(ValueError, "match"):
            AgentSkill.from_markdown(skill_markdown(), expected_name="different-name")
        (package / "SKILL.md").write_text(skill_markdown())
        self.assertEqual([], service.create_review("org/repo", RISK_DIFF)["report"]["findings"])
        service.reload_skills()
        self.assertEqual(["SEC-DANGEROUS-CALL"], [item["rule_id"] for item in service.create_review("org/repo", RISK_DIFF)["report"]["findings"]])
        restricted = [AgentSkill.from_markdown(skill_markdown(name).replace("description:", "allowed-tools: [" + tool + "]\ndescription:"))
                      for name, tool in (("line-only", "changed_line"), ("symbol-only", "symbol"))]
        permissions = AgenticReviewer._skill_tool_permissions("security", restricted)
        self.assertEqual({"changed_line", "symbol"}, permissions)
        suite = RepositoryToolSuite("", RISK_DIFF, parse_unified_diff(RISK_DIFF))
        self.assertTrue(suite.registry("security", None).names())
        self.assertEqual([], suite.registry("security", set()).names())
        self.assertEqual(["changed_line", "symbol"], suite.registry("security", permissions).names())
        tools = ToolRegistry()
        AgenticReviewer._register_skill_resource_tool(tools, [loaded])
        self.assertEqual("Original disk resource", tools.invoke("read_skill_resource", {"skill": SKILL, "path": "references/policy.md"})["content"])
        with self.assertRaises(PermissionError):
            tools.invoke("read_skill_resource", {"skill": "unselected", "path": "references/policy.md"})
        bundled = SkillRegistry(str(Path(__file__).resolve().parents[1] / "skills"))
        bundled.reload()
        self.assertEqual(9, len(bundled.catalog()))


if __name__ == "__main__":
    unittest.main()
