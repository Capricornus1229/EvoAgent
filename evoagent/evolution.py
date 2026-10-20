import hashlib
import json
import re
import threading
import uuid
from typing import Any, Callable, Dict, List, Optional

from .diff_parser import parse_unified_diff
from .evaluation_harness import one_to_one_match
from .finding_identity import canonical_identity, is_valid_cwe
from .store import utc_now
from .telemetry import validate_token_budget_policy


DEFAULT_PROMPT = (
    "Review the unified diff. Return JSON findings with severity, fix and test. "
    "Report only actionable defects introduced by added lines."
)

DEFAULT_EVALUATION_CASES = [
    {
        "name": "security-dynamic-execution",
        "diff": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+result = eval(user_input)\n",
        "expected": [{"path": "app.py", "line": 1, "min_severity": "high"}],
    },
    {
        "name": "security-hardcoded-secret",
        "diff": "--- a/config.py\n+++ b/config.py\n@@ -1 +1 @@\n-old\n+api_key = \"production-secret\"\n",
        "expected": [{"path": "config.py", "line": 1, "min_severity": "high"}],
    },
    {
        "name": "reliability-swallowed-error",
        "diff": "--- a/jobs.py\n+++ b/jobs.py\n@@ -1 +1 @@\n-old\n+except Exception: pass\n",
        "expected": [{"path": "jobs.py", "line": 1, "min_severity": "medium"}],
    },
    {
        "name": "clean-parameterized-query",
        "diff": "--- a/db.py\n+++ b/db.py\n@@ -1 +1 @@\n-old\n+cursor.execute(\"SELECT * FROM users WHERE id = ?\", (user_id,))\n",
        "expected": [],
    },
    {
        "name": "clean-input-validation",
        "diff": "--- a/api.py\n+++ b/api.py\n@@ -1 +1 @@\n-old\n+limit = min(max(int(raw_limit), 1), 100)\n",
        "expected": [],
    },
    {
        "name": "holdout-security-shell-execution",
        "split": "holdout",
        "diff": "--- a/runner.py\n+++ b/runner.py\n@@ -1 +1 @@\n-old\n+subprocess.run(command, shell=True)\n",
        "expected": [{"path": "runner.py", "line": 1, "min_severity": "high"}],
    },
    {
        "name": "holdout-clean-environment-secret",
        "split": "holdout",
        "diff": "--- a/config.py\n+++ b/config.py\n@@ -1 +1 @@\n-old\n+api_key = os.environ[\"API_KEY\"]\n",
        "expected": [],
    },
]

SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def seed_builtin_evaluation_cases(store) -> List[dict]:
    """Explicitly import the small builtin smoke corpus, idempotently."""
    return [store.save_evaluation_case(
        case["name"], case.get("split", "validation"), case["diff"],
        case["expected"], "builtin", True,
    ) for case in DEFAULT_EVALUATION_CASES]


def evaluation_selection_status(store, validation: list, holdout: list) -> dict:
    """Describe the exact capped case selection used for replay and activation."""
    result = {"evaluation_selection_policy": "non-builtin-first,newest-id-first"}
    for split, cases in (("validation", validation), ("holdout", holdout)):
        total = store.count_evaluation_cases(split, True)
        result.update({
            split + "_total_cases": total,
            split + "_selected_cases": len(cases),
            split + "_omitted_cases": max(0, total - len(cases)),
        })
    return result


def _minimum_severity(item: dict) -> str:
    return str(item.get("min_severity", item.get("severity", "low"))).lower()


class RegressionEvaluator:
    """Replay a fixed dataset against one prompt and compute objective review metrics."""

    def __init__(self, reviewer_factory: Callable[[str], object], line_tolerance: int = 2):
        self.reviewer_factory = reviewer_factory
        self.line_tolerance = max(0, int(line_tolerance))

    def run(self, prompt: str, cases: List[dict]) -> Dict[str, Any]:
        reviewer = self.reviewer_factory(prompt)
        reviewer_name = str(getattr(reviewer, "name", reviewer.__class__.__name__))
        true_positive = false_positive = false_negative = 0
        severity_hits = matched = clean_hits = clean_total = 0
        high_severity_hits = high_severity_total = 0
        expected_total = predicted_total = 0
        errors = []
        case_results = []

        for case in cases:
            expected_items = list(case.get("expected", []))
            expected_total += len(expected_items)
            clean_total += int(not expected_items)
            try:
                parsed = parse_unified_diff(case["diff"])
                review_case = getattr(reviewer, "review_case", None)
                findings = (review_case(case, parsed) if review_case
                            else reviewer.review(case["diff"], parsed))
                predicted = {}
                predicted_findings_by_key = {}
                for finding in findings:
                    key = (finding.path, int(finding.line), finding.rule_id)
                    current = predicted.get(key)
                    severity = finding.severity.value
                    if current is None or SEVERITY_RANK[severity] > SEVERITY_RANK[current]:
                        predicted[key] = severity
                        predicted_findings_by_key[key] = finding
                predicted_total += len(predicted)

                # Use the same canonical CWE/path/location matcher as the
                # production accuracy harness. Legacy cases without a CWE or
                # rule_id remain wildcard-by-location for compatibility.
                match_expected = [
                    {
                        "path": item["path"],
                        "start_line": int(item.get("line", item.get("start_line"))),
                        "end_line": int(item.get("end_line", item.get("line", item.get("start_line")))),
                        "cwe": canonical_identity(
                            item.get("rule_id", ""), item.get("cwe", "")
                        ) if item.get("rule_id") or item.get("cwe") else "",
                    }
                    for item in expected_items
                ]
                predicted_findings = [predicted_findings_by_key[key] for key in predicted]
                matches = one_to_one_match(
                    match_expected, predicted_findings,
                    line_tolerance=self.line_tolerance,
                )
                matched_predicted = {item.predicted_index for item in matches}
                tp = len(matches)
                severity_ok = high_hits = 0
                for match in matches:
                    expected = expected_items[match.expected_index]
                    finding = predicted_findings[match.predicted_index]
                    minimum = _minimum_severity(expected)
                    severity_passed = SEVERITY_RANK[finding.severity.value] >= SEVERITY_RANK[minimum]
                    severity_ok += int(severity_passed)
                    if SEVERITY_RANK[minimum] >= SEVERITY_RANK["high"]:
                        high_hits += int(severity_passed)

                fp = len(predicted_findings) - len(matched_predicted)
                fn = len(expected_items) - tp
                true_positive += tp
                false_positive += fp
                false_negative += fn
                matched += tp
                severity_hits += severity_ok
                case_high_total = sum(
                    SEVERITY_RANK[_minimum_severity(item)]
                    >= SEVERITY_RANK["high"]
                    for item in expected_items
                )
                high_severity_total += case_high_total
                high_severity_hits += high_hits
                if not expected_items:
                    clean_hits += int(not predicted)
                case_results.append({
                    "id": case.get("id"), "name": case["name"], "tp": tp, "fp": fp, "fn": fn,
                    "findings": len(predicted), "severity_hits": severity_ok, "error": None,
                })
            except Exception as exc:
                # A failed replay is a missed positive (or a failed clean case), not a
                # successful empty prediction. This keeps partial outages from inflating scores.
                false_negative += len(expected_items)
                high_severity_total += sum(
                    SEVERITY_RANK.get(_minimum_severity(item), 0)
                    >= SEVERITY_RANK["high"]
                    for item in expected_items
                )
                errors.append({"name": case["name"], "error": str(exc)[:500]})
                case_results.append({
                    "id": case.get("id"), "name": case["name"], "tp": 0, "fp": 0,
                    "fn": len(expected_items), "findings": 0, "severity_hits": 0,
                    "error": str(exc)[:500],
                })

        precision = (
            true_positive / (true_positive + false_positive)
            if true_positive + false_positive else (1.0 if true_positive + false_negative == 0 else 0.0)
        )
        recall = (
            true_positive / (true_positive + false_negative)
            if true_positive + false_negative else 1.0
        )
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        severity_accuracy = severity_hits / matched if matched else (1.0 if not false_negative else 0.0)
        clean_accuracy = clean_hits / clean_total if clean_total else 1.0
        high_severity_recall = (
            high_severity_hits / high_severity_total if high_severity_total else 1.0
        )
        successful_cases = len(cases) - len(errors)
        success_rate = successful_cases / len(cases) if cases else 0.0
        components = []
        if expected_total:
            components.extend(((f1, 0.65), (severity_accuracy, 0.15)))
        if clean_total:
            components.append((clean_accuracy, 0.20))
        score = (
            sum(value * weight for value, weight in components)
            / sum(weight for _, weight in components)
            if components else 0.0
        )
        score *= success_rate
        close = getattr(reviewer, "close", None)
        if close:
            close()
        return {
            "schema_version": 2,
            "reviewer": reviewer_name,
            "score": round(score, 4),
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "severity_accuracy": round(severity_accuracy, 4),
            "high_severity_recall": round(high_severity_recall, 4),
            "clean_accuracy": round(clean_accuracy, 4),
            "cases": len(cases),
            "positive_cases": sum(bool(case.get("expected")) for case in cases),
            "clean_cases": clean_total,
            "expected_findings": expected_total,
            "predicted_findings": predicted_total,
            "successful_cases": successful_cases,
            "success_rate": round(success_rate, 4),
            "errors": errors,
            "case_results": case_results,
        }


class EvolutionEngine:
    """Replay-gated Prompt versions; live activation requires the shared runtime."""

    FORBIDDEN = ("ignore previous", "disable safety", "bypass", "直接执行生产")
    FEEDBACK_RULE_ID = re.compile(r"^[A-Z][A-Z0-9_-]{1,79}$")

    def __init__(
        self, store, reviewer_factory: Optional[Callable[[str], object]] = None,
        min_cases: int = 3, max_cases: int = 5, min_improvement: float = 0.01,
        min_holdout_cases: int = 0, max_metric_regression: float = 0.0,
        seed_defaults: bool = False, candidate_generator=None,
        runtime_snapshot_provider=None, replay_factory=None, on_activate=None,
    ):
        self.store = store
        self.reviewer_factory = reviewer_factory
        self.min_cases = min_cases
        self.max_cases = max_cases
        self.min_improvement = min_improvement
        self.min_holdout_cases = min_holdout_cases
        self.max_metric_regression = max_metric_regression
        self.candidate_generator = candidate_generator
        self.runtime_snapshot_provider = runtime_snapshot_provider
        self.replay_factory = replay_factory
        self.on_activate = on_activate
        self._lock = threading.RLock()
        if seed_defaults:
            self._seed_default_cases()

    def _seed_default_cases(self) -> None:
        seed_builtin_evaluation_cases(self.store)

    @staticmethod
    def validate_case(name: str, diff: str, expected: list, split: str = "validation") -> None:
        if not name or len(name) > 120:
            raise ValueError("evaluation case name is required and must be at most 120 characters")
        if split not in {"train", "validation", "holdout"}:
            raise ValueError("evaluation split must be train, validation or holdout")
        if len(diff.encode("utf-8")) > 1024 * 1024:
            raise ValueError("evaluation case diff must be at most 1 MiB")
        parsed = parse_unified_diff(diff)
        if not parsed.added_lines:
            raise ValueError("evaluation case must contain a valid unified diff with added lines")
        valid_locations = {(line.path, line.line) for line in parsed.added_lines}
        if not isinstance(expected, list):
            raise ValueError("expected_findings must be an array")
        if len(expected) > 100:
            raise ValueError("expected_findings must contain at most 100 items")
        seen = set()
        for item in expected:
            if not isinstance(item, dict):
                raise ValueError("each expected finding must be an object")
            try:
                path = str(item.get("path", ""))
                start_line = int(item.get("line", item.get("start_line", 0)))
                end_line = int(item.get("end_line", start_line))
            except (TypeError, ValueError) as exc:
                raise ValueError("expected finding line must be an integer") from exc
            if start_line > end_line:
                raise ValueError("expected finding line range is inverted")
            if not any(
                changed_path == path and start_line <= changed_line <= end_line
                for changed_path, changed_line in valid_locations
            ):
                raise ValueError("expected finding must point to an added line: %s:%s" % (path, start_line))
            minimum = item.get("min_severity", item.get("severity", "low"))
            if str(minimum).lower() not in SEVERITY_RANK:
                raise ValueError("invalid min_severity")
            rule_id = str(item.get("rule_id", "")).strip()
            if "rule_id" in item and (not rule_id or len(rule_id) > 80):
                raise ValueError("rule_id must be non-empty and at most 80 characters")
            if "cwe" in item and not is_valid_cwe(item.get("cwe")):
                raise ValueError("cwe must use the CWE-<number> format")
            identity = (path, start_line, end_line, rule_id)
            if identity in seen:
                raise ValueError("duplicate expected finding: %s:%s:%s" % identity)
            seen.add(identity)

    def add_evaluation_case(
        self, name: str, diff: str, expected: list, split: str = "validation",
        source: str = "manual",
    ) -> dict:
        name = name.strip()
        split = split.strip().lower()
        self.validate_case(name, diff, expected, split)
        return self.store.save_evaluation_case(name, split, diff, expected, source[:120], True)

    def safety_evaluate(self, prompt: str) -> Dict[str, Any]:
        normalized = prompt.strip()
        lowered = normalized.lower()
        safety = bool(normalized) and len(normalized) <= 12000 and not any(
            token in lowered for token in self.FORBIDDEN
        )
        required = ("diff", "severity", "fix", "test", "json")
        completeness = sum(token in lowered for token in required) / len(required)
        return {
            "experimental": self.replay_factory is None,
            "production_activation_allowed": self.replay_factory is not None,
            "safety_passed": safety,
            "completeness": round(completeness, 4),
            "missing_terms": [token for token in required if token not in lowered],
        }

    def status(self) -> Dict[str, Any]:
        cases = self.store.list_evaluation_cases("validation", True, self.max_cases)
        holdout = self.store.list_evaluation_cases("holdout", True, self.max_cases)
        return {
            **evaluation_selection_status(self.store, cases, holdout),
            "experimental": self.replay_factory is None,
            "production_activation_allowed": self.replay_factory is not None,
            "model_configured": self.reviewer_factory is not None,
            "activation_policy": "evaluate-then-activate",
            "auto_activate_default": True,
            "validation_cases": len(cases),
            "holdout_cases": len(holdout),
            "minimum_cases": self.min_cases,
            "minimum_holdout_cases": self.min_holdout_cases,
            "maximum_cases_per_run": self.max_cases,
            "minimum_improvement": self.min_improvement,
            "maximum_metric_regression": self.max_metric_regression,
            "validation_dataset_fingerprint": self._dataset_fingerprint(cases),
            "holdout_dataset_fingerprint": self._dataset_fingerprint(holdout),
            "ready": (
                self.reviewer_factory is not None
                and len(cases) >= self.min_cases
                and len(holdout) >= self.min_holdout_cases
            ),
        }

    def propose(
        self, skill_name: str, prompt: str, regression_score: Optional[float] = None,
    ) -> Dict[str, Any]:
        skill_name = skill_name.strip()
        if not skill_name or len(skill_name) > 120:
            raise ValueError("skill_name is required and must be at most 120 characters")
        with self._lock:
            return self._propose(skill_name, prompt, regression_score)

    def _propose(
        self, skill_name: str, prompt: str, regression_score: Optional[float],
        activation_policy: str = "auto", candidate_config: Optional[dict] = None,
    ) -> Dict[str, Any]:
        prompt = prompt.strip()
        if activation_policy not in {"auto", "manual", "shadow"}:
            raise ValueError("invalid Prompt activation policy")
        # Legacy 'shadow' means evaluate without activating, not a fake shadow run.
        live = self.replay_factory is not None and skill_name == "llm-review"
        frozen = self.runtime_snapshot_provider() if live else None
        if live:
            validate_token_budget_policy(frozen.get("token_budget_policy"))
        if candidate_config is not None:
            from .evolution_v2 import validate_structured_candidate
            candidate_config = validate_structured_candidate(candidate_config)
        safety = self.safety_evaluate(prompt)
        active = self.store.get_active_skill_version(skill_name)
        if (active and prompt.strip() == active["prompt"].strip()
                and (not live or candidate_config is None
                     or candidate_config == frozen.get("structured_config", {}))):
            return {
                "version": {
                    "skill_name": active["skill_name"],
                    "version": active["version"],
                    "score": active["score"],
                    "active": bool(active["active"]),
                },
                "decision": "deferred",
                "reason": "candidate prompt is identical to the active version",
                "candidate": self._empty_metrics(0),
                "baseline": self._empty_metrics(0),
                "candidate_holdout": self._redact_holdout_metrics(self._empty_metrics(0)),
                "baseline_holdout": self._redact_holdout_metrics(self._empty_metrics(0)),
                "safety": safety,
                "run_id": None,
            }
        baseline_prompt = (frozen.get("prompt_overlay", "") if live
                           else active["prompt"] if active else DEFAULT_PROMPT)
        effective_config = (candidate_config if candidate_config is not None
                            else (frozen or {}).get("structured_config", {}))
        cases = self.store.list_evaluation_cases("validation", True, self.max_cases)
        holdout_cases = self.store.list_evaluation_cases("holdout", True, self.max_cases)

        decision = "deferred"
        reason = ""
        candidate_metrics = self._empty_metrics(len(cases))
        baseline_metrics = self._empty_metrics(len(cases))
        candidate_holdout = self._empty_metrics(len(holdout_cases))
        baseline_holdout = self._empty_metrics(len(holdout_cases))
        gates = {
            "safety": safety["safety_passed"] and safety["completeness"] == 1.0,
            "validation_dataset_ready": len(cases) >= self.min_cases,
            "holdout_dataset_ready": len(holdout_cases) >= self.min_holdout_cases,
            "evaluation_success": None,
            "validation_improvement": None,
            "validation_non_regression": None,
            "holdout_non_regression": None,
        }
        if not safety["safety_passed"] or safety["completeness"] < 1.0:
            decision = "rejected"
            reason = "candidate prompt failed the deterministic safety/completeness gate"
        elif self.reviewer_factory is None:
            reason = "candidate saved but no LLM provider is configured; replay evaluation was not run"
        elif len(cases) < self.min_cases:
            reason = "candidate saved but the validation dataset is smaller than the activation minimum"
        elif len(holdout_cases) < self.min_holdout_cases:
            reason = "candidate saved but the holdout dataset is smaller than the activation minimum"
        else:
            if live:
                baseline_evaluator = RegressionEvaluator(
                    lambda text: self.replay_factory(text, frozen, frozen.get("structured_config", {})))
                candidate_evaluator = RegressionEvaluator(
                    lambda text: self.replay_factory(text, frozen, effective_config))
            else:
                baseline_evaluator = candidate_evaluator = RegressionEvaluator(self.reviewer_factory)
            baseline_metrics = baseline_evaluator.run(baseline_prompt, cases)
            candidate_metrics = candidate_evaluator.run(prompt, cases)
            if holdout_cases:
                baseline_holdout = baseline_evaluator.run(baseline_prompt, holdout_cases)
                candidate_holdout = candidate_evaluator.run(prompt, holdout_cases)
            no_errors = not (
                baseline_metrics["errors"] or candidate_metrics["errors"]
                or baseline_holdout["errors"] or candidate_holdout["errors"]
            )
            improved = candidate_metrics["score"] >= baseline_metrics["score"] + self.min_improvement
            validation_safe = self._non_regressing(candidate_metrics, baseline_metrics)
            holdout_safe = self._non_regressing(candidate_holdout, baseline_holdout)
            gates.update({
                "evaluation_success": no_errors,
                "validation_improvement": improved,
                "validation_non_regression": validation_safe,
                "holdout_non_regression": holdout_safe,
            })
            if no_errors and improved and validation_safe and holdout_safe:
                decision = "ready" if live else "experimental_passed"
                reason = ("Prompt candidate passed shared-runtime validation and holdout replay" if live
                          else "Prompt candidate passed experimental replay; no production runtime was supplied")
            else:
                decision = "rejected"
                reasons = []
                if not no_errors:
                    reasons.append("one or more baseline or candidate evaluations failed")
                if not improved:
                    reasons.append("score improvement was below the configured threshold")
                if not validation_safe:
                    reasons.append("a protected validation metric regressed")
                if not holdout_safe:
                    reasons.append("a protected holdout metric regressed")
                reason = "; ".join(reasons)

        version = self.store.save_skill_version(
            skill_name, prompt.strip(), candidate_metrics["score"], False
        )
        run = {
            "id": str(uuid.uuid4()),
            "skill_name": skill_name,
            "candidate_version": version["version"],
            "baseline_version": active["version"] if active else None,
            "decision": decision,
            "candidate_score": candidate_metrics["score"],
            "baseline_score": baseline_metrics["score"],
            "metrics": {
                "candidate": candidate_metrics,
                "baseline": baseline_metrics,
                "candidate_holdout": self._redact_holdout_metrics(candidate_holdout),
                "baseline_holdout": self._redact_holdout_metrics(baseline_holdout),
                "safety": safety,
                "gates": gates,
                "reason": reason,
                "reproducibility": {
                    "evaluation_schema_version": 2,
                    "candidate_prompt_sha256": self._sha256(prompt.strip()),
                    "baseline_prompt_sha256": self._sha256(baseline_prompt),
                    "validation_dataset_sha256": self._dataset_fingerprint(cases),
                    "holdout_dataset_sha256": self._dataset_fingerprint(holdout_cases),
                    "validation_case_ids": [case.get("id") for case in cases],
                    "holdout_case_count": len(holdout_cases),
                },
                "external_regression_score_ignored": regression_score is not None,
            },
            "created_at": utc_now(),
        }
        if live:
            run["metrics"]["production"] = {
                "schema": "prompt-shared-runtime-v1", "snapshot": frozen,
                "runtime_hash": self._runtime_hash(frozen),
                "environment_hash": self._runtime_hash(frozen, include_prompt=False),
                "candidate_config": effective_config,
                "candidate_config_sha256": self._sha256(json.dumps(effective_config, sort_keys=True)),
                "baseline_prompt": baseline_prompt,
            }
        self.store.save_evolution_run(run)
        if live and decision == "ready" and activation_policy == "auto":
            try:
                if self.activate(skill_name, version["version"]):
                    decision, reason = "activated", "Shared-runtime replay and smoke passed; new tasks use this Prompt"
                    version["active"] = True
            except ValueError as exc:
                # A candidate may be retained, but never falsely labelled active.
                reason = "candidate retained without activation: %s" % exc
        return {
            "version": version,
            "decision": decision,
            "reason": reason,
            "candidate": candidate_metrics,
            "baseline": baseline_metrics,
            "candidate_holdout": self._redact_holdout_metrics(candidate_holdout),
            "baseline_holdout": self._redact_holdout_metrics(baseline_holdout),
            "safety": safety,
            "gates": gates,
            "run_id": run["id"],
        }

    @classmethod
    def _runtime_hash(cls, snapshot: dict, include_prompt: bool = True) -> str:
        public = json.loads(json.dumps(snapshot))
        public.get("memory", {}).pop("entries", None)
        if not include_prompt:
            public.pop("prompt_overlay", None)
            public.pop("structured_config", None)
        return cls._sha256(json.dumps(public, sort_keys=True, ensure_ascii=False))

    def _verified_run(self, skill_name: str, version: int) -> dict:
        if self.replay_factory is None or skill_name != "llm-review":
            raise ValueError("experimental Prompt has no verified production runtime")
        run = self.store.get_prompt_run(skill_name, version)
        production = (run or {}).get("metrics", {}).get("production", {})
        if not run or production.get("schema") != "prompt-shared-runtime-v1":
            raise ValueError("version has no shared-runtime evaluation; submit it for evaluation first")
        try:
            validate_token_budget_policy((production.get("snapshot") or {}).get("token_budget_policy"))
        except ValueError as exc:
            raise ValueError("legacy Prompt evaluation lacks a shared token budget policy; reevaluate the Prompt") from exc
        return run

    def _activation_checks(self, run: dict, prompt: str, config: dict, current: dict,
                           compare_baseline: bool = True) -> None:
        proof = run["metrics"]["reproducibility"]
        cases = self.store.list_evaluation_cases("validation", True, self.max_cases)
        holdout = self.store.list_evaluation_cases("holdout", True, self.max_cases)
        if (proof["validation_dataset_sha256"] != self._dataset_fingerprint(cases)
                or proof["holdout_dataset_sha256"] != self._dataset_fingerprint(holdout)):
            raise ValueError("evaluation datasets changed; evaluate the candidate again")
        safety = self.safety_evaluate(prompt) if prompt else {"safety_passed": True, "completeness": 1.0}
        if not safety["safety_passed"] or safety["completeness"] != 1.0:
            raise ValueError("Prompt no longer satisfies the safety/completeness gate")
        from .evolution_v2 import validate_structured_candidate
        validate_structured_candidate(config)
        smoke_cases = cases[:1] + holdout[:1]
        smoke = RegressionEvaluator(lambda text: self.replay_factory(text, current, config)).run(prompt, smoke_cases)
        if smoke["errors"] or not smoke["cases"]:
            raise ValueError("shared-runtime activation smoke failed")
        if compare_baseline:
            baseline = RegressionEvaluator(lambda text: self.replay_factory(
                text, current, current.get("structured_config", {}))).run(current.get("prompt_overlay", ""), smoke_cases)
            if baseline["errors"] or not self._non_regressing(smoke, baseline):
                raise ValueError("shared-runtime activation smoke regressed from baseline")

    def activate(self, skill_name: str, version: int) -> bool:
        with self._lock:
            run = self._verified_run(skill_name, version)
            active = self.store.get_active_skill_version(skill_name)
            if active and int(active["version"]) == version:
                return True
            if run["decision"] != "ready":
                raise ValueError("candidate is not ready for activation")
            candidate = self.store.get_prompt_version(skill_name, version)
            proof = run["metrics"]["reproducibility"]
            if not candidate or self._sha256(candidate["prompt"]) != proof["candidate_prompt_sha256"]:
                raise ValueError("candidate content changed since evaluation")
            production = run["metrics"]["production"]
            if self._sha256(json.dumps(production["candidate_config"], sort_keys=True)) != production.get("candidate_config_sha256"):
                raise ValueError("candidate policy changed since evaluation")
            current = self.runtime_snapshot_provider()
            if self._runtime_hash(current) != production["runtime_hash"]:
                raise ValueError("runtime changed since evaluation; evaluate again")
            self._activation_checks(run, candidate["prompt"], production["candidate_config"], current)
            if self._runtime_hash(self.runtime_snapshot_provider()) != production["runtime_hash"]:
                raise ValueError("runtime changed during smoke; activation refused")
            if (self._dataset_fingerprint(self.store.list_evaluation_cases("validation", True, self.max_cases)) != proof["validation_dataset_sha256"]
                    or self._dataset_fingerprint(self.store.list_evaluation_cases("holdout", True, self.max_cases)) != proof["holdout_dataset_sha256"]):
                raise ValueError("evaluation datasets changed during smoke")
            if not self.store.commit_prompt_activation(skill_name, version, run["id"], run["baseline_version"]):
                raise ValueError("active Prompt changed concurrently; activation refused")
            if self.on_activate:
                self.on_activate()
            return True

    def rollback(self, skill_name: str, version: int) -> bool:
        """Restore the exact baseline of the specified *currently active* version."""
        with self._lock:
            run = self._verified_run(skill_name, version)
            production = run["metrics"]["production"]
            current = self.runtime_snapshot_provider()
            if self._runtime_hash(current, include_prompt=False) != production["environment_hash"]:
                raise ValueError("runtime changed since activation; evaluate the desired baseline again")
            baseline = production["snapshot"]
            self._activation_checks(run, production["baseline_prompt"], baseline.get("structured_config", {}), current,
                                    compare_baseline=False)
            if not self.store.restore_prompt_baseline(skill_name, version, run["id"], run["baseline_version"]):
                raise ValueError("active Prompt or rollback baseline changed")
            if self.on_activate:
                self.on_activate()
            return True

    def auto_propose(
        self, skill_name: str = "llm-review", tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        cases = self.store.list_failure_cases(True, 100, tenant_id)
        active = self.store.get_active_skill_version(skill_name)
        base = active["prompt"] if active else DEFAULT_PROMPT
        if self.candidate_generator is not None and cases:
            generated = self.candidate_generator.generate(cases, base)
            candidate = generated["candidate_prompt"]
            if (candidate.strip() == base.strip() and (self.replay_factory is None
                    or generated["candidate"] == self.runtime_snapshot_provider().get("structured_config", {}))):
                return {
                    "version": None, "decision": "deferred",
                    "reason": "root-cause analysis produced no prompt change",
                    "candidate_change": generated,
                    "failure_cases_used": len(cases), "run_id": None,
                }
            with self._lock:
                result = self._propose(skill_name, candidate, None,
                    activation_policy="auto", candidate_config=generated["candidate"])
            result["candidate_change"] = generated
            result["failure_cases_used"] = len(cases)
            result["rollback_point"] = (
                {"skill_name": skill_name, "version": active["version"]}
                if active else None
            )
            result["evaluation_data"] = {
                "validation_sha256": self.status()["validation_dataset_fingerprint"],
                "holdout_sha256": self.status()["holdout_dataset_fingerprint"],
            }
            if result.get("run_id"):
                runs = self.store.list_evolution_runs(200)
                run = next((item for item in runs if item["id"] == result["run_id"]), None)
                if run:
                    metrics = dict(run["metrics"])
                    metrics["structured_candidate"] = {
                        "generator": generated["generator"],
                        "failure_cases": generated["failure_cases"],
                        "clusters": generated["clusters"],
                        "change_diff": generated["change_diff"],
                        "candidate": generated["candidate"],
                        "generation_execution": generated["generation"],
                        "evaluation_data": result["evaluation_data"],
                        "rollback_point": result["rollback_point"],
                        "source_code_changes_allowed": False,
                    }
                    self.store.update_evolution_run(
                        result["run_id"], result["decision"], metrics
                    )
            return result
        counts = {}
        for case in cases:
            counts[case["category"]] = counts.get(case["category"], 0) + 1
        directives = []
        if counts.get("false_positive"):
            directives.append("Avoid style-only findings and require direct evidence from an added line.")
        if counts.get("missed_issue"):
            directives.append("Check boundary conditions, authorization, input validation and error paths explicitly.")
        if counts.get("bad_fix"):
            directives.append("Propose minimal fixes that preserve behavior and always include a regression test.")
        if counts.get("execution_error"):
            directives.append("Keep output valid JSON and follow the requested schema exactly.")
        # A missed-issue feedback item may carry the reviewer rule identifier that a
        # human confirmed.  Preserve that signal in the prompt without accepting
        # arbitrary feedback text as an instruction.  The bracketed marker is both
        # human-readable to an LLM and machine-auditable in offline replay.
        learned_rule_ids = sorted({
            str((case.get("payload", {}).get("finding") or {}).get("rule_id", "")).strip()
            for case in cases
            if case.get("category") == "missed_issue"
        })
        learned_rule_ids = [
            rule_id for rule_id in learned_rule_ids
            if self.FEEDBACK_RULE_ID.fullmatch(rule_id)
        ]
        directives.extend(
            "Explicitly check added lines for confirmed rule %s [focus-rule:%s]."
            % (rule_id, rule_id)
            for rule_id in learned_rule_ids
        )
        additions = [directive for directive in directives if directive.lower() not in base.lower()]
        if not additions:
            return {
                "version": None,
                "decision": "deferred",
                "reason": "no new supported learning signal was found in unresolved feedback",
                "candidate": self._empty_metrics(0),
                "baseline": self._empty_metrics(0),
                "candidate_holdout": self._empty_metrics(0),
                "baseline_holdout": self._empty_metrics(0),
                "safety": self.safety_evaluate(base),
                "run_id": None,
                "failure_cases_used": len(cases),
                "learned_categories": counts,
            }
        candidate = base.rstrip() + (
            "\n\nLearned constraints:\n- " + "\n- ".join(additions)
        )
        result = self.propose(skill_name, candidate)
        result["failure_cases_used"] = len(cases)
        result["learned_categories"] = counts
        result["learned_rule_ids"] = learned_rule_ids
        return result

    @staticmethod
    def _empty_metrics(case_count: int) -> Dict[str, Any]:
        return {
            "schema_version": 2,
            "reviewer": "",
            "score": 0.0, "precision": 0.0, "recall": 0.0, "f1": 0.0,
            "severity_accuracy": 0.0, "high_severity_recall": 0.0, "clean_accuracy": 0.0,
            "cases": case_count, "positive_cases": 0, "clean_cases": 0,
            "expected_findings": 0, "predicted_findings": 0,
            "successful_cases": 0, "success_rate": 0.0, "errors": [], "case_results": [],
        }

    def _non_regressing(self, candidate: Dict[str, Any], baseline: Dict[str, Any]) -> bool:
        protected = ["score", "precision", "recall", "high_severity_recall"]
        if baseline.get("positive_cases", 0):
            protected.append("severity_accuracy")
        if baseline.get("clean_cases", 0):
            protected.append("clean_accuracy")
        return all(
            float(candidate.get(metric, 0.0)) + self.max_metric_regression
            >= float(baseline.get(metric, 0.0))
            for metric in protected
        )

    @staticmethod
    def _redact_holdout_metrics(metrics: Dict[str, Any]) -> Dict[str, Any]:
        redacted = {
            key: value for key, value in metrics.items()
            if key not in {"errors", "case_results"}
        }
        redacted["error_count"] = len(metrics.get("errors", []))
        return redacted

    @staticmethod
    def _sha256(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @classmethod
    def _dataset_fingerprint(cls, cases: List[dict]) -> str:
        canonical = [
            {
                "name": case.get("name"),
                "split": case.get("split"),
                "diff": case.get("diff"),
                "expected": case.get("expected", []),
                "repository": case.get("repository"),
                "repository_root": case.get("repository_root"),
                "source": case.get("source"),
            }
            for case in cases
        ]
        payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return cls._sha256(payload)
