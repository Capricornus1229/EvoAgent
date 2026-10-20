"""Replay-gated evolution of standard Agent Skill ``SKILL.md`` packages."""
import hashlib
import json
import re
import threading
import tempfile
import uuid
from typing import Any, Callable, Dict, Optional

from .diff_parser import parse_unified_diff
from .evolution import RegressionEvaluator, evaluation_selection_status
from .reviewer import Reviewer
from .review_runtime import DEFAULT_TASK_EXECUTION, build_review_harness, validate_task_execution
from .skills import AgentSkill, SKILL_NAME
from .store import utc_now
from .telemetry import make_token_budget_policy, validate_token_budget_policy


ARTIFACT_SCHEMA_VERSION = 2
RULE_ID = re.compile(r"[A-Z][A-Z0-9_-]{1,79}")


def _canonical_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: dict) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _default_skill_markdown(name: str) -> str:
    return """---
name: %s
description: Apply replay-validated review guidance learned from confirmed feedback. Use during code review when project-specific failure patterns may apply.
---

# Review project-specific failure patterns

Inspect the current change using repository evidence. Report only actionable defects introduced by added lines, and include a concrete fix and regression test.
""" % name


def _migrate_legacy_artifact(artifact: dict, expected_name: str) -> dict:
    name = str(artifact.get("name", expected_name)).strip().lower()
    description = str(artifact.get("description") or (
        "Apply migrated project-specific guidance during code review."
    )).replace("\n", " ").strip()
    lines = [
        "---", "name: %s" % name,
        "description: %s" % json.dumps(description, ensure_ascii=False), "---", "",
        "# Migrated review guidance", "",
        "Apply these checks to added production lines and verify repository context.", "",
    ]
    for rule in artifact.get("rules") or []:
        rule_id = str(rule.get("rule_id", "REVIEW")).strip().upper()
        match = str(rule.get("match", "")).replace("`", "'").strip()
        if not match:
            continue
        lines.extend([
            "<!-- evoagent:learned:%s:start -->" % rule_id,
            "## %s" % rule_id, "",
            "Inspect added behavior equivalent to `%s`." % match,
            "Report `%s` at `%s` severity only when context confirms the defect."
            % (rule_id, str(rule.get("severity", "medium"))),
            "Cite the changed line, explain impact, propose a fix, and require a test.",
            "<!-- evoagent:learned:%s:end -->" % rule_id, "",
        ])
    return {"name": name, "files": {"SKILL.md": "\n".join(lines).strip() + "\n"}}


def validate_artifact(artifact: Any, expected_name: str = "") -> dict:
    """Validate and canonicalize one versioned Agent Skill package."""
    expected_name = str(expected_name).strip().lower()
    if expected_name and not SKILL_NAME.fullmatch(expected_name):
        raise ValueError("invalid Agent Skill name")
    if isinstance(artifact, str):
        artifact = {"name": expected_name, "files": {"SKILL.md": artifact}}
    if not isinstance(artifact, dict):
        raise ValueError("agent skill artifact must be an object")
    if "rules" in artifact and "files" not in artifact and "skill_md" not in artifact:
        artifact = _migrate_legacy_artifact(artifact, expected_name)
    if "skill_md" in artifact and "files" not in artifact:
        artifact = {
            **artifact,
            "files": {
                "SKILL.md": artifact.get("skill_md"),
                **dict(artifact.get("supporting_files") or {}),
            },
        }
    name = str(artifact.get("name", expected_name)).strip().lower()
    if expected_name and name != expected_name:
        raise ValueError("agent skill artifact name must match skill_name")
    skill = AgentSkill.from_artifact({
        "name": name, "files": dict(artifact.get("files") or {}),
    })
    normalized = skill.to_artifact()
    normalized["schema_version"] = ARTIFACT_SCHEMA_VERSION
    return normalized


class _ReplayTaskStore:
    """A real temporary store: checkpoints and memory never reach production."""

    def __init__(self):
        from .store import TaskStore
        self.directory = tempfile.TemporaryDirectory(prefix="evoagent-skill-replay-")
        self.store = TaskStore(self.directory.name + "/replay.db")

    def __getattr__(self, name):
        return getattr(self.store, name)

    def close(self):
        self.directory.cleanup()


class AgentSkillReplayReviewer(Reviewer):
    """Replay a candidate through the product Lead/worker Skill runtime."""

    def __init__(
        self, artifact: Optional[dict], client, token_budget: int = 8000,
        time_budget_seconds: int = 60, runtime_snapshot: Optional[dict] = None,
        runtime_builder=None, skill_name: str = "",
        total_token_budget: Optional[int] = None,
    ):
        self.skill = AgentSkill.from_artifact(artifact) if artifact else None
        self.skill_name = skill_name or (self.skill.name if self.skill else "")
        if not self.skill_name:
            raise ValueError("replay requires the target Skill name")
        self.name = "%s-agent-skill-replay" % self.skill_name
        self._sequence = 0
        self._last_task_id = ""
        self.snapshot = json.loads(_canonical_json(runtime_snapshot if runtime_snapshot is not None else {
            "roles": ["lead", "security", "correctness-reliability", "critic"],
            "token_budget": token_budget, "time_budget_seconds": time_budget_seconds,
            "token_budget_policy": make_token_budget_policy(
                total_token_budget if total_token_budget is not None else token_budget * 4,
                {role: token_budget for role in ("lead", "security", "correctness-reliability", "critic")},
            ),
            "run_options": {"enabled_skills": [self.skill_name]},
            "task_execution": dict(DEFAULT_TASK_EXECUTION),
        }))
        self.snapshot["task_execution"] = validate_task_execution(self.snapshot.get("task_execution"))
        self.snapshot["token_budget_policy"] = validate_token_budget_policy(self.snapshot.get("token_budget_policy"))
        self.token_budget = int(self.snapshot.get("token_budget", token_budget))
        self.time_budget_seconds = int(self.snapshot.get("time_budget_seconds", time_budget_seconds))
        self.client, self.runtime_builder = client, runtime_builder
        self.agentic = None
        self.harness = None
        self.report = None
        self._replay_store = None

    def review(self, diff: str, parsed) -> list:
        return self.review_case({"diff": diff, "repository": ""}, parsed)

    def review_case(self, case: dict, parsed) -> list:
        from .memory import MemoryManager
        from .review_runtime import build_agentic_runtime

        if self._replay_store:
            self._replay_store.close()
        self._replay_store = _ReplayTaskStore()
        self.report = None
        self.agentic = None
        self.harness = None
        self._sequence += 1
        self._last_task_id = "skill-replay:%s:%d" % (self.skill_name, self._sequence)
        tenant_id = str(self.snapshot.get("tenant_id") or "default")
        repository = str(case.get("repository_root") or case.get("repository") or "")
        options = {"mode": "agentic", "enabled_agents": self.snapshot["roles"],
                   **dict(self.snapshot.get("run_options") or {})}
        self._replay_store.create(self._last_task_id, repository, None, options, tenant_id)
        self._replay_store.save_task_payload(self._last_task_id, case["diff"])
        memory_config = dict(self.snapshot.get("memory") or {})
        for entry in memory_config.get("entries") or []:
            if entry.get("scope") != "working":
                # Capture eligibility once; TTL must not change the two arms'
                # inputs while sequential model calls are in progress.
                self._replay_store.save_agent_memory({**entry, "expires_at": None})
        memory = MemoryManager(
            self._replay_store, memory_config.get("enabled", False),
            memory_config.get("recall_limit", 6), memory_config.get("working_ttl_seconds", 86400),
        )
        skills = {item["name"]: AgentSkill.from_artifact(item)
                  for item in self.snapshot.get("skills") or []}
        skills.pop(self.skill_name, None)
        if self.skill:
            skills[self.skill_name] = self.skill
        frozen_skills = [skills[name] for name in sorted(skills)]
        if self.runtime_builder:
            self.agentic = self.runtime_builder(
                self._replay_store, frozen_skills, memory, self.snapshot,
            )
        else:
            self.agentic = build_agentic_runtime(
                self._replay_store, self.client, self.snapshot,
                memory_manager=memory, skill_provider=lambda _tenant: frozen_skills,
            )
        self.harness = build_review_harness(
            self._replay_store, self.agentic, self.snapshot["task_execution"],
        )
        self.report = self.harness.run(
            self._last_task_id, repository, None, case["diff"], tenant_id,
        )
        return self.report.findings

    def close(self):
        if self._replay_store:
            self._replay_store.close()
            self._replay_store = None

    def evaluation_execution(self) -> dict:
        if self.agentic is None:
            return {}
        return dict(
            self.agentic.collaboration_summary(self._last_task_id).get("execution") or {}
        )

    def evaluation_collaboration(self) -> dict:
        if self.agentic is None:
            return {}
        return dict(
            self.agentic.collaboration_summary(self._last_task_id).get("collaboration") or {}
        )

    def evaluation_config(self) -> dict:
        return {
            "mode": "agentic", "roles": self.snapshot["roles"],
            "skill": self.skill_name,
            "per_role_token_budget": self.token_budget,
            "total_token_budget_per_pr": self.snapshot["token_budget_policy"]["total_limit"],
            "token_budget_policy": self.snapshot["token_budget_policy"],
            "per_role_time_budget_seconds": self.time_budget_seconds,
            "task_execution": dict(self.snapshot["task_execution"]),
            "runtime_snapshot_sha256": _sha256(self.snapshot),
        }

    def __del__(self):
        if getattr(self, "_replay_store", None):
            self._replay_store.close()


class SkillEvolutionEngine:
    """Create, replay, activate and roll back Agent Skill package versions."""

    def __init__(
        self, store, reviewer_factory: Optional[Callable[[dict], Reviewer]] = None,
        min_cases: int = 3, max_cases: int = 100, min_improvement: float = .01,
        min_holdout_cases: int = 2, max_metric_regression: float = 0.0,
        runtime_snapshot_provider=None, replay_factory=None,
    ):
        self.store = store
        self.reviewer_factory = reviewer_factory
        self.min_cases = min_cases
        self.max_cases = max_cases
        self.min_improvement = min_improvement
        self.min_holdout_cases = min_holdout_cases
        self.max_metric_regression = max_metric_regression
        self.runtime_snapshot_provider = runtime_snapshot_provider
        self.replay_factory = replay_factory
        self._lock = threading.RLock()

    @staticmethod
    def empty_artifact(skill_name: str) -> dict:
        return validate_artifact(_default_skill_markdown(skill_name), skill_name)

    @classmethod
    def build_candidate_artifact(
        cls, skill_name: str, base_artifact: dict, feedback: list,
    ) -> dict:
        """Pure artifact mutation used by controlled and production experiments.

        Feedback must already contain concrete evidence; no task-store lookup is
        performed here. This keeps experiment arms isolated and reproducible.
        """
        artifact = validate_artifact(base_artifact, skill_name)
        content = artifact["files"]["SKILL.md"]
        learned, removed, used = [], [], []
        for index, case in enumerate(feedback):
            finding = (case.get("payload") or {}).get("finding") or {}
            rule_id = str(finding.get("rule_id", "")).strip().upper()
            if not RULE_ID.fullmatch(rule_id):
                continue
            category = str(case.get("category", ""))
            if category == "false_positive":
                updated = cls._remove_learned_block(content, rule_id)
                if updated != content:
                    content = updated
                    removed.append(rule_id)
                    used.append(case.get("id", index))
                continue
            if category != "missed_issue":
                continue
            evidence = str(finding.get("evidence", "")).strip()
            if not evidence or len(evidence) > 240 or "\n" in evidence or "\r" in evidence:
                continue
            if "evoagent:learned:%s:start" % rule_id in content:
                continue
            content = content.rstrip() + "\n\n" + cls._learned_block(
                rule_id, finding, evidence,
            )
            learned.append(rule_id)
            used.append(case.get("id", index))
        candidate = validate_artifact({
            "name": skill_name,
            "files": {**artifact["files"], "SKILL.md": content},
        }, skill_name)
        return {
            "artifact": candidate,
            "learned_rule_ids": sorted(set(learned)),
            "removed_rule_ids": sorted(set(removed)),
            "used_feedback_ids": used,
        }

    def _factory(self, serialized: str) -> Reviewer:
        payload = json.loads(serialized)
        if self.replay_factory is not None:
            return self.replay_factory(payload["artifact"], payload["snapshot"])
        if self.reviewer_factory is None:
            raise RuntimeError("Agent Skill replay requires a configured model")
        return self.reviewer_factory(payload["artifact"] or self.empty_artifact(payload["skill_name"]))

    def _snapshot(self, tenant_id: str) -> dict:
        if self.runtime_snapshot_provider:
            snapshot = json.loads(_canonical_json(self.runtime_snapshot_provider(tenant_id)))
            validate_task_execution(snapshot.get("task_execution"))
            validate_token_budget_policy(snapshot.get("token_budget_policy"))
            return snapshot
        # Backward-compatible isolated reviewer factories can still be used by
        # tests and experiments, without claiming a production-equivalent replay.
        return {
            "tenant_id": tenant_id, "skills": [item["artifact"] for item in
                self.store.list_active_skill_artifacts(tenant_id)],
            "reviewer_factory": str(getattr(self.reviewer_factory, "__qualname__", "")),
            "task_execution": dict(DEFAULT_TASK_EXECUTION),
        }

    @staticmethod
    def _runtime_config_hash(snapshot: dict) -> str:
        value = dict(snapshot)
        value["memory"] = {key: child for key, child in (snapshot.get("memory") or {}).items()
                           if key != "entries"}
        return _sha256(value)

    @staticmethod
    def _background_hash(snapshot: dict, skill_name: str) -> str:
        value = dict(snapshot)
        value["target_skill"] = skill_name
        value["skills"] = [item for item in snapshot.get("skills") or []
                           if item["name"] != skill_name]
        return SkillEvolutionEngine._runtime_config_hash(value)

    @staticmethod
    def _dataset_hash(cases: list) -> str:
        # Repository context and provenance participate in replay as well as
        # Diff/labels; changing any scored input invalidates activation.
        return _sha256({"cases": [{key: case.get(key) for key in (
            "id", "name", "split", "diff", "expected", "repository",
            "repository_root", "source",
        )} for case in cases]})

    @staticmethod
    def _redact_holdout(metrics: dict) -> dict:
        return {key: value for key, value in metrics.items() if key not in {"case_results", "errors"}}

    def metrics_non_regressing(self, candidate: dict, baseline: dict) -> bool:
        protected = ["score", "precision", "recall", "high_severity_recall", "success_rate"]
        if baseline.get("positive_cases", 0):
            protected.append("severity_accuracy")
        if baseline.get("clean_cases", 0):
            protected.append("clean_accuracy")
        return all(
            float(candidate.get(key, 0)) + self.max_metric_regression
            >= float(baseline.get(key, 0)) for key in protected
        )

    def _non_regressing(self, candidate: dict, baseline: dict) -> bool:
        return self.metrics_non_regressing(candidate, baseline)

    def status(self, skill_name: str = "evolved-review", tenant_id: str = "default") -> dict:
        validation = self.store.list_evaluation_cases("validation", True, self.max_cases)
        holdout = self.store.list_evaluation_cases("holdout", True, self.max_cases)
        active = self.store.get_active_skill_artifact(skill_name, tenant_id)
        return {
            **evaluation_selection_status(self.store, validation, holdout),
            "tenant_id": tenant_id, "skill_name": skill_name, "format": "agent-skill",
            "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            "active_version": active.get("version") if active else None,
            "active_artifact_sha256": active.get("artifact_sha256") if active else None,
            "validation_cases": len(validation), "holdout_cases": len(holdout),
            "minimum_cases": self.min_cases, "minimum_holdout_cases": self.min_holdout_cases,
            "minimum_improvement": self.min_improvement,
            "maximum_metric_regression": self.max_metric_regression,
            "activation_policy": "evaluate-then-activate", "auto_activate_default": True,
            "production_replay_frozen": bool(self.runtime_snapshot_provider),
            "model_configured": self.reviewer_factory is not None or self.replay_factory is not None,
            "ready": (self.reviewer_factory is not None or self.replay_factory is not None)
            and len(validation) >= self.min_cases and len(holdout) >= self.min_holdout_cases,
        }

    def propose(self, skill_name: str, artifact: Any, tenant_id: str = "default") -> dict:
        skill_name = skill_name.strip().lower()
        candidate = validate_artifact(artifact, skill_name)
        with self._lock:
            return self._propose(skill_name, candidate, tenant_id)

    def _propose(self, skill_name: str, artifact: dict, tenant_id: str) -> dict:
        active = self.store.get_active_skill_artifact(skill_name, tenant_id)
        snapshot = self._snapshot(tenant_id)
        snapshot["target_skill"] = skill_name
        baseline_artifact = next((validate_artifact(item, skill_name)
                                  for item in snapshot.get("skills") or []
                                  if item["name"] == skill_name), None)
        if baseline_artifact and _sha256(baseline_artifact) == _sha256(artifact):
            return {
                "version": self._public_version(active) if active else None, "decision": "deferred",
                "reason": "candidate Agent Skill is identical to the active version",
                "candidate": {}, "baseline": {}, "candidate_holdout": {},
                "baseline_holdout": {}, "gates": {}, "run_id": None,
            }
        validation = self.store.list_evaluation_cases("validation", True, self.max_cases)
        holdout = self.store.list_evaluation_cases("holdout", True, self.max_cases)
        baseline_metrics = self._empty_metrics(len(validation))
        candidate_metrics = self._empty_metrics(len(validation))
        baseline_holdout = self._empty_metrics(len(holdout))
        candidate_holdout = self._empty_metrics(len(holdout))
        decision, reason = "deferred", ""
        gates = {
            "artifact_valid": True, "runtime_is_agent_skill": True,
            "model_configured": self.reviewer_factory is not None or self.replay_factory is not None,
            "validation_dataset_ready": len(validation) >= self.min_cases,
            "holdout_dataset_ready": len(holdout) >= self.min_holdout_cases,
            "evaluation_success": None, "validation_improvement": None,
            "validation_non_regression": None, "holdout_non_regression": None,
            "frozen_runtime_current": None,
        }
        if self.reviewer_factory is None and self.replay_factory is None:
            reason = "candidate saved but no model-backed Agent Skill replay is configured"
        elif len(validation) < self.min_cases:
            reason = "candidate saved but the validation dataset is smaller than the activation minimum"
        elif len(holdout) < self.min_holdout_cases:
            reason = "candidate saved but the holdout dataset is smaller than the activation minimum"
        else:
            evaluator = RegressionEvaluator(self._factory)
            baseline_input = _canonical_json({"artifact": baseline_artifact, "snapshot": snapshot,
                                             "skill_name": skill_name})
            candidate_input = _canonical_json({"artifact": artifact, "snapshot": snapshot,
                                              "skill_name": skill_name})
            baseline_metrics = evaluator.run(baseline_input, validation)
            candidate_metrics = evaluator.run(candidate_input, validation)
            baseline_holdout = evaluator.run(baseline_input, holdout)
            candidate_holdout = evaluator.run(candidate_input, holdout)
            no_errors = not (
                baseline_metrics["errors"] or candidate_metrics["errors"]
                or baseline_holdout["errors"] or candidate_holdout["errors"]
            )
            improved = candidate_metrics["score"] >= baseline_metrics["score"] + self.min_improvement
            validation_safe = self._non_regressing(candidate_metrics, baseline_metrics)
            holdout_safe = self._non_regressing(candidate_holdout, baseline_holdout)
            gates.update({
                "evaluation_success": no_errors, "validation_improvement": improved,
                "validation_non_regression": validation_safe,
                "holdout_non_regression": holdout_safe,
            })
            checked = self._snapshot(tenant_id)
            checked["target_skill"] = skill_name
            runtime_current = self._runtime_config_hash(checked) == self._runtime_config_hash(snapshot)
            gates["frozen_runtime_current"] = runtime_current
            if no_errors and improved and validation_safe and holdout_safe and runtime_current:
                decision = "ready"
                reason = "candidate passed frozen replay gates; manual activation and smoke are required"
            else:
                decision = "rejected"
                failures = []
                if not no_errors:
                    failures.append("evaluation failed")
                if not improved:
                    failures.append("validation improvement was below threshold")
                if not validation_safe:
                    failures.append("a protected validation metric regressed")
                if not holdout_safe:
                    failures.append("a protected holdout metric regressed")
                if not runtime_current:
                    failures.append("production configuration changed during frozen replay")
                reason = "; ".join(failures)
        # Disk baselines are archived as immutable inactive artifacts too. This
        # preserves resources and lets rollback restore the exact reviewed input.
        baseline_version = active
        if baseline_artifact is not None and active is None:
            baseline_version = self.store.save_skill_artifact(
                skill_name, baseline_artifact, baseline_metrics.get("score", 0.0), False, tenant_id,
            )
        version = self.store.save_skill_artifact(
            skill_name, artifact, candidate_metrics.get("score", 0.0),
            False, tenant_id,
        )
        candidate_change = self._skill_diff(baseline_artifact or {}, artifact)
        run = {
            "id": str(uuid.uuid4()), "tenant_id": tenant_id, "skill_name": skill_name,
            "candidate_version": version["version"],
            "baseline_version": baseline_version.get("version") if baseline_version else None,
            "decision": decision, "candidate_score": candidate_metrics.get("score", 0.0),
            "baseline_score": baseline_metrics.get("score", 0.0), "created_at": utc_now(),
            "metrics": {
                "candidate": candidate_metrics, "baseline": baseline_metrics,
                "candidate_holdout": self._redact_holdout(candidate_holdout),
                "baseline_holdout": self._redact_holdout(baseline_holdout),
                "gates": gates, "reason": reason, "candidate_change": candidate_change,
                "reproducibility": {
                    "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
                    "candidate_artifact_sha256": version["artifact_sha256"],
                    "baseline_artifact_sha256": _sha256(baseline_artifact) if baseline_artifact else None,
                    "runtime_snapshot": snapshot, "runtime_snapshot_sha256": _sha256(snapshot),
                    "runtime_config_sha256": self._runtime_config_hash(snapshot),
                    "background_sha256": self._background_hash(snapshot, skill_name),
                    "validation_dataset_sha256": self._dataset_hash(validation),
                    "holdout_dataset_sha256": self._dataset_hash(holdout),
                    "baseline_artifact": baseline_artifact,
                    "baseline_override_version": active.get("version") if active else None,
                },
            },
        }
        self.store.save_skill_evolution_run(run)
        return {
            "version": version, "decision": decision, "reason": reason,
            "candidate": candidate_metrics, "baseline": baseline_metrics,
            "candidate_holdout": self._redact_holdout(candidate_holdout),
            "baseline_holdout": self._redact_holdout(baseline_holdout),
            "gates": gates, "run_id": run["id"], "candidate_change": candidate_change,
        }

    def auto_propose(
        self, skill_name: str = "evolved-review", tenant_id: Optional[str] = None,
    ) -> dict:
        skill_name = skill_name.strip().lower()
        if not SKILL_NAME.fullmatch(skill_name):
            raise ValueError("invalid Agent Skill name")
        failures = self.store.list_failure_cases(True, 100, tenant_id)
        tenant_id = tenant_id or "default"
        artifact = next((validate_artifact(item, skill_name)
                         for item in self._snapshot(tenant_id).get("skills") or []
                         if item["name"] == skill_name), self.empty_artifact(skill_name))
        content = artifact["files"]["SKILL.md"]
        used_ids, learned, removed = [], [], []
        for case in failures:
            finding = (case.get("payload") or {}).get("finding") or {}
            rule_id = str(finding.get("rule_id", "")).strip().upper()
            if not RULE_ID.fullmatch(rule_id):
                continue
            if case.get("category") == "false_positive":
                updated = self._remove_learned_block(content, rule_id)
                if updated != content:
                    content = updated
                    removed.append(rule_id)
                    used_ids.append(case["id"])
                continue
            if case.get("category") != "missed_issue":
                continue
            evidence = str(finding.get("evidence", "")).strip()
            if not evidence:
                evidence = self._evidence_from_task(case["task_id"], finding)
            if not evidence or len(evidence) > 240 or "\n" in evidence or "\r" in evidence:
                continue
            if "evoagent:learned:%s:start" % rule_id in content:
                continue
            content = content.rstrip() + "\n\n" + self._learned_block(rule_id, finding, evidence)
            learned.append(rule_id)
            used_ids.append(case["id"])
        if not used_ids:
            return {
                "version": None, "decision": "deferred",
                "reason": "no supported SKILL.md mutation signal was found in unresolved feedback",
                "failure_cases_used": len(failures), "learned_rule_ids": [],
                "removed_rule_ids": [], "run_id": None,
            }
        candidate = validate_artifact({
            "name": skill_name,
            "files": {**artifact["files"], "SKILL.md": content},
        }, skill_name)
        result = self.propose(skill_name, candidate, tenant_id)
        result.update({
            "failure_cases_used": len(used_ids),
            "learned_rule_ids": sorted(set(learned)),
            "removed_rule_ids": sorted(set(removed)),
        })
        if result.get("run_id"):
            update = getattr(self.store, "update_skill_evolution_run", None)
            if update:
                run = next(item for item in self.store.list_skill_evolution_runs(1000, tenant_id)
                           if item["id"] == result["run_id"])
                metrics = dict(run["metrics"])
                metrics["feedback_ids"] = used_ids
                update(run["id"], result["decision"], metrics, tenant_id)
        return result

    @staticmethod
    def _learned_block(rule_id: str, finding: dict, evidence: str) -> str:
        evidence = evidence.replace("`", "'")
        severity = str(finding.get("severity", "medium")).lower()
        return "\n".join([
            "<!-- evoagent:learned:%s:start -->" % rule_id,
            "## Confirmed %s guidance" % rule_id, "",
            "Inspect added behavior equivalent to `%s`." % evidence,
            "Report `%s` at `%s` severity only when repository context confirms the defect."
            % (rule_id, severity),
            "Cite the changed line, explain impact, propose a minimal fix, and require a regression test.",
            "<!-- evoagent:learned:%s:end -->" % rule_id, "",
        ])

    @staticmethod
    def _remove_learned_block(content: str, rule_id: str) -> str:
        pattern = re.compile(
            r"\n*<!-- evoagent:learned:%s:start -->.*?"
            r"<!-- evoagent:learned:%s:end -->\n*" % (re.escape(rule_id), re.escape(rule_id)),
            re.DOTALL,
        )
        return pattern.sub("\n", content).rstrip() + "\n"

    def _evidence_from_task(self, task_id: str, finding: dict) -> str:
        diff = self.store.get_task_payload(task_id)
        if not diff:
            return ""
        try:
            path, line = str(finding.get("path", "")), int(finding.get("line", 0))
            for changed in parse_unified_diff(diff).added_lines:
                if changed.path == path and changed.line == line:
                    return changed.content.strip()
        except (TypeError, ValueError):
            return ""
        return ""

    def _evaluated_run(self, skill_name: str, version: int, tenant_id: str) -> dict:
        run = next((item for item in self.store.list_skill_evolution_runs(1000, tenant_id)
                    if item["skill_name"] == skill_name
                    and int(item["candidate_version"]) == int(version)), None)
        if not run:
            raise ValueError("Skill version has no frozen replay evaluation")
        return run

    def activate(self, skill_name: str, version: int, tenant_id: str = "default") -> bool:
        """Smoke a previously evaluated candidate, then atomically activate it."""
        with self._lock:
            run = self._evaluated_run(skill_name, version, tenant_id)
            if run["decision"] not in {"ready", "activated"}:
                raise ValueError("Skill version did not pass replay gates")
            provenance = run["metrics"].get("reproducibility") or {}
            if not provenance.get("runtime_snapshot"):
                raise ValueError("legacy Skill evaluation must be rerun before activation")
            try:
                validate_task_execution(provenance["runtime_snapshot"].get("task_execution"))
                if self.runtime_snapshot_provider:
                    validate_token_budget_policy(provenance["runtime_snapshot"].get("token_budget_policy"))
            except ValueError as exc:
                raise ValueError("legacy Skill evaluation lacks complete execution/budget policy; reevaluate the Skill") from exc
            current = self._snapshot(tenant_id)
            current["target_skill"] = skill_name
            active = self.store.get_active_skill_artifact(skill_name, tenant_id)
            if active and int(active["version"]) == int(version) and run["decision"] == "activated":
                return True
            if self._runtime_config_hash(current) != provenance.get("runtime_config_sha256"):
                raise ValueError("production configuration changed; reevaluate the Skill")
            validation = self.store.list_evaluation_cases("validation", True, self.max_cases)
            holdout = self.store.list_evaluation_cases("holdout", True, self.max_cases)
            if (self._dataset_hash(validation) != provenance.get("validation_dataset_sha256")
                    or self._dataset_hash(holdout) != provenance.get("holdout_dataset_sha256")):
                raise ValueError("evaluation dataset changed; reevaluate the Skill")
            candidate = next((item for item in self.store.list_skill_artifact_versions(skill_name, tenant_id)
                              if int(item["version"]) == int(version)), None)
            if not candidate or _sha256(candidate["artifact"]) != provenance.get("candidate_artifact_sha256"):
                raise ValueError("evaluated candidate artifact is missing or changed")
            smoke_cases = validation[:1] + holdout[:1]
            if not smoke_cases:
                raise ValueError("activation smoke requires evaluation cases")
            evaluator = RegressionEvaluator(self._factory)
            def replay_input(artifact):
                return _canonical_json({"artifact": artifact, "snapshot": provenance["runtime_snapshot"],
                                        "skill_name": skill_name})
            baseline = evaluator.run(replay_input(provenance.get("baseline_artifact")), smoke_cases)
            smoke = evaluator.run(replay_input(candidate["artifact"]), smoke_cases)
            if baseline["errors"] or smoke["errors"] or not self._non_regressing(smoke, baseline):
                raise ValueError("activation smoke failed; active Skill was preserved")
            # Close the race between smoke and commit by checking background and
            # datasets again; the storage hook separately compare-and-swaps the override.
            checked = self._snapshot(tenant_id)
            checked["target_skill"] = skill_name
            if self._runtime_config_hash(checked) != provenance["runtime_config_sha256"]:
                raise ValueError("production configuration changed during smoke")
            if (self._dataset_hash(self.store.list_evaluation_cases("validation", True, self.max_cases))
                    != provenance["validation_dataset_sha256"]
                    or self._dataset_hash(self.store.list_evaluation_cases("holdout", True, self.max_cases))
                    != provenance["holdout_dataset_sha256"]):
                raise ValueError("evaluation dataset changed during smoke")
            commit = getattr(self.store, "commit_skill_activation", None)
            if not commit:
                raise RuntimeError("store lacks atomic Skill activation support")
            if not commit(skill_name, int(version), run["id"], tenant_id,
                          expected_active_version=provenance.get("baseline_override_version")):
                raise ValueError("active Skill changed before activation; reevaluate")
            if run["metrics"].get("feedback_ids"):
                self.store.resolve_failure_cases(run["metrics"]["feedback_ids"])
            return True

    def rollback(self, skill_name: str, version: int, tenant_id: str = "default") -> bool:
        """Undo this candidate by restoring its exact archived baseline or absence."""
        with self._lock:
            run = self._evaluated_run(skill_name, version, tenant_id)
            if run["decision"] != "activated":
                raise ValueError("rollback requires an activated Skill evaluation")
            provenance = run["metrics"].get("reproducibility") or {}
            if self._background_hash(self._snapshot(tenant_id), skill_name) != provenance.get("background_sha256"):
                raise ValueError("production background changed; reevaluation is required")
            active = self.store.get_active_skill_artifact(skill_name, tenant_id)
            if not active or int(active["version"]) != int(version):
                raise ValueError("the requested Skill version is no longer active")
            restore = getattr(self.store, "restore_skill_baseline", None)
            if not restore:
                raise RuntimeError("store lacks atomic Skill rollback support")
            baseline = provenance.get("baseline_artifact")
            if not restore(skill_name, int(version), run["id"],
                           int(run["baseline_version"]) if baseline is not None else None, tenant_id):
                raise ValueError("active Skill changed before rollback")
            return True

    @staticmethod
    def _skill_diff(baseline: dict, candidate: dict) -> dict:
        before, after = baseline.get("files") or {}, candidate.get("files") or {}
        names = sorted(set(before).union(after))
        return {
            "changed_files": [name for name in names if before.get(name) != after.get(name)],
            "baseline_skill_md_sha256": hashlib.sha256(
                str(before.get("SKILL.md", "")).encode("utf-8")
            ).hexdigest(),
            "candidate_skill_md_sha256": hashlib.sha256(
                str(after.get("SKILL.md", "")).encode("utf-8")
            ).hexdigest(),
        }

    @staticmethod
    def _empty_metrics(cases: int) -> Dict[str, Any]:
        return {
            "schema_version": 2, "reviewer": "", "score": 0.0,
            "precision": 0.0, "recall": 0.0, "f1": 0.0,
            "severity_accuracy": 0.0, "high_severity_recall": 0.0,
            "clean_accuracy": 0.0, "cases": cases, "positive_cases": 0,
            "clean_cases": 0, "expected_findings": 0, "predicted_findings": 0,
            "successful_cases": 0, "success_rate": 0.0, "errors": [],
            "case_results": [],
        }

    @staticmethod
    def _public_version(value: dict) -> dict:
        return {
            key: value[key] for key in (
                "tenant_id", "skill_name", "version", "score", "active", "parent_version",
                "artifact_sha256", "created_at",
            ) if key in value
        }
