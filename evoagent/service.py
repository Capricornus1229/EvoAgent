import hashlib
import json
import os
from pathlib import Path
import uuid
from typing import Any, Dict, Optional

from .agentic_core import AgenticReviewer
from .auth import AuthManager
from .config import Settings
from .evolution import EvolutionEngine
from .evolution_v2 import RootCauseEvolutionGenerator
from .patching import SuggestionOnlyFixer, VerifiedPatchFixer
from .github import GitHubAppAuthenticator, GitHubClient
from .review_runtime import build_agentic_runtime, build_review_harness
from .metrics import metrics
from .llm import JsonChatClient
from .modes import RunMode
from .memory import MemoryManager
from .models import TaskState, TraceEvent
from .observability import AlertManager, Observability
from .postgres_store import create_store
from .report import to_markdown
from .reviewer import (
    OpenAICompatibleReviewer, ReliabilityRuleReviewer, SecurityRuleReviewer,
)
from .skills import AgentSkill, SkillRegistry
from .skill_evolution import AgentSkillReplayReviewer, SkillEvolutionEngine, validate_artifact
from .store import utc_now
from .task_queue import PermanentTaskError, TaskQueue
from .runtime import RuntimeCancelled
from .telemetry import make_token_budget_policy
from .verifier import RepairVerifier


class TaskConflictError(RuntimeError):
    """The requested lifecycle operation conflicts with a terminal outcome."""


class DispatchUnavailable(RuntimeError):
    """A durable task exists but delivery should be retried by the caller."""

    def __init__(self, message: str, task_id: str = ""):
        super().__init__(message)
        self.task_id = task_id


class ReviewService:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.validate_evolution()
        self.llm_config = settings.resolved_llm()
        self.store = create_store(settings.database_url, settings.db_path)
        self.memory = MemoryManager(
            self.store, settings.memory_enabled, settings.memory_recall_limit,
            settings.memory_working_ttl_seconds,
        )
        self.observability = Observability(settings.otel_service_name, settings.otel_endpoint)
        self.registry = SkillRegistry(settings.skills_dir)
        self.registry.register(
            "security-review", SecurityRuleReviewer(),
            "1.0.0", "Security, injection and secret detection",
        )
        self.registry.register(
            "reliability-review", ReliabilityRuleReviewer(),
            "1.0.0", "Reliability and observability review",
        )
        self.registry.reload()
        self.chat_client = (
            JsonChatClient(
                str(self.llm_config["base_url"]), str(self.llm_config["api_key"]),
                str(self.llm_config["model"]), str(self.llm_config["provider"]),
                settings.timeout_seconds, dict(self.llm_config.get("headers") or {}),
            ) if self.llm_config else None
        )
        self.reviewer = self._build_agentic_reviewer()
        self.harness = build_review_harness(
            self.store, self.reviewer, self._task_execution(),
            observability=self.observability,
        )
        self.github = GitHubClient(settings.github_token)
        repair_verifier = RepairVerifier(
            settings.repair_test_command, settings.repair_verify_timeout_seconds,
            settings.trusted_repository_roots, settings.trusted_github_repositories,
        )
        self.fixer = (
            VerifiedPatchFixer(self.chat_client, repair_verifier)
            if self.chat_client else SuggestionOnlyFixer()
        )
        self.auth = AuthManager(
            self.store, settings.auth_secret, settings.session_ttl_seconds,
            settings.bootstrap_admin_username, settings.bootstrap_admin_password,
            settings.default_tenant_id,
        )
        self.alerts = AlertManager(
            self.store, settings.alert_failure_rate, settings.alert_min_samples
        )
        self.evolution = EvolutionEngine(
            self.store,
            reviewer_factory=self._build_llm_reviewer if self.llm_config else None,
            min_cases=settings.eval_min_cases,
            max_cases=settings.eval_max_cases,
            min_improvement=settings.eval_min_improvement,
            min_holdout_cases=settings.eval_min_holdout_cases,
            max_metric_regression=settings.eval_max_metric_regression,
            seed_defaults=False,
            runtime_snapshot_provider=lambda: self._runtime_snapshot(settings.default_tenant_id),
            replay_factory=self._build_prompt_replay if self.chat_client else None,
            on_activate=self.reload_skills,
            candidate_generator=(
                RootCauseEvolutionGenerator(self.chat_client, max_role_token_budget=settings.agent_token_budget * 4)
                if self.chat_client else None
            ),
        )
        self.skill_evolution = SkillEvolutionEngine(
            self.store,
            runtime_snapshot_provider=self._runtime_snapshot,
            replay_factory=self._build_skill_replay if self.chat_client else None,
            min_cases=settings.eval_min_cases,
            max_cases=settings.eval_max_cases,
            min_improvement=settings.eval_min_improvement,
            min_holdout_cases=settings.eval_min_holdout_cases,
            max_metric_regression=settings.eval_max_metric_regression,
        )
        self.queue = TaskQueue(
            self._process_queued, settings.async_workers, settings.redis_url,
            settings.queue_max_attempts, settings.queue_lease_seconds,
            self._on_dead_letter,
        )

    def _build_llm_reviewer(self, prompt: str = "") -> OpenAICompatibleReviewer:
        if not self.llm_config:
            raise RuntimeError("no LLM provider is configured")
        return OpenAICompatibleReviewer(
            str(self.llm_config["base_url"]),
            str(self.llm_config["api_key"]),
            str(self.llm_config["model"]),
            self.settings.timeout_seconds,
            system_prompt=prompt,
            provider=str(self.llm_config["provider"]),
            extra_headers=dict(self.llm_config.get("headers") or {}),
        )

    def _build_agentic_reviewer(self) -> AgenticReviewer:
        snapshot = self._runtime_snapshot(self.settings.default_tenant_id)
        return build_agentic_runtime(
            self.store, self.chat_client, snapshot,
            scanners=self._runtime_scanners(), memory_manager=self.memory,
            skill_provider=self._active_agent_skills,
        )

    def _runtime_scanners(self):
        return [item for item in self.registry.reviewers()
                if not isinstance(item, OpenAICompatibleReviewer)]

    def _task_execution(self) -> dict:
        return {"max_steps": self.settings.max_steps,
                "timeout_seconds": self.settings.timeout_seconds, "node_retries": 2}

    def _runtime_snapshot(self, tenant_id: str) -> dict:
        # Refresh disk artifacts before capturing the effective baseline.
        self.registry.reload()
        active_prompt = self.store.get_active_skill_version("llm-review")
        settings = self.settings
        roles = [item.strip() for item in settings.enabled_agents.split(",") if item.strip()]
        if (set(roles).difference({"lead", "security", "correctness-reliability", "critic"})
                or "lead" not in roles or not set(roles).intersection({"security", "correctness-reliability"})):
            raise ValueError("review requires Lead and at least one professional Worker")
        public_model = {key: str((self.llm_config or {}).get(key, ""))
                        for key in ("provider", "model", "base_url")}
        root = Path(__file__).parent
        revisions = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                     for name in ("agentic_core.py", "gates.py", "diff_parser.py", "review_runtime.py",
                                  "reviewer.py", "review_rules.py", "repository_tools.py",
                                  "harness.py", "runtime.py", "context_manager.py", "telemetry.py", "llm.py")}
        scanner_ids = [{"class": type(scanner).__module__ + "." + type(scanner).__qualname__,
                        "name": scanner.name} for scanner in self._runtime_scanners()]
        repositories = {case.get("repository", "") for split in ("validation", "holdout")
                        for case in self.store.list_evaluation_cases(split, True, settings.eval_max_cases)}
        memory_entries = []
        if settings.memory_enabled:
            for repository in sorted(repositories):
                memory_entries.extend(self.store.list_agent_memories(
                    tenant_id, repository, ("semantic", "episodic"), 200))
        return {
            "tenant_id": tenant_id, "model": public_model, "roles": roles,
            "prompt_overlay": active_prompt["prompt"] if active_prompt else "",
            "structured_config": (self.store.get_prompt_run("llm-review", active_prompt["version"]) or {})
                                  .get("metrics", {}).get("production", {}).get("candidate_config", {}) if active_prompt else {},
            "token_budget": settings.agent_token_budget,
            "token_budget_policy": make_token_budget_policy(settings.total_token_budget,
                {role: settings.agent_token_budget for role in roles}),
            "time_budget_seconds": settings.agent_time_budget_seconds,
            "task_execution": self._task_execution(),
            "input_cost_per_million": settings.llm_input_cost_per_million,
            "output_cost_per_million": settings.llm_output_cost_per_million,
            "review_test_command": settings.repair_test_command,
            "trusted_repository_roots": list(settings.trusted_repository_roots),
            "trusted_github_repositories": list(settings.trusted_github_repositories),
            "context": {
                "context_window_tokens": settings.agent_context_window_tokens,
                "input_token_budget": settings.agent_context_input_tokens,
                "diff_token_budget": settings.context_diff_token_budget,
                "observation_token_budget": settings.context_observation_token_budget,
                "recent_observations": settings.context_recent_observations,
                "map_chunk_tokens": settings.context_map_chunk_tokens,
            },
            "skills": [skill.to_artifact() for skill in self._active_agent_skills(tenant_id)],
            "scanners": scanner_ids, "runtime_revision": revisions,
            "memory": {"enabled": settings.memory_enabled, "recall_limit": settings.memory_recall_limit,
                       "working_ttl_seconds": settings.memory_working_ttl_seconds, "entries": memory_entries},
        }

    def _build_skill_replay(self, artifact, snapshot):
        def runtime_builder(store, skills, memory, frozen):
            return build_agentic_runtime(
                store, self.chat_client, frozen, scanners=self._runtime_scanners(),
                memory_manager=memory, skill_provider=lambda _tenant: skills,
            )
        return AgentSkillReplayReviewer(
            artifact, self.chat_client, runtime_snapshot=snapshot,
            runtime_builder=runtime_builder, skill_name=snapshot["target_skill"],
        )

    def _build_prompt_replay(self, prompt, snapshot, structured_config):
        frozen = json.loads(json.dumps(snapshot))
        frozen.update(prompt_overlay=prompt, structured_config=structured_config,
                      target_skill="__prompt__", run_options={"enabled_skills": []})
        # The reserved name cannot collide with a valid SKILL.md package name.
        return self._build_skill_replay(None, frozen)

    def _run_review(
        self, task_id: str, repository: str, pull_request: Optional[int],
        diff: str, tenant_id: str,
    ):
        return self.harness.run(task_id, repository, pull_request, diff, tenant_id)

    def reload_skills(self) -> list:
        self.registry.reload()
        skills = self.registry.list()
        self.reviewer = self._build_agentic_reviewer()
        self.harness = build_review_harness(
            self.store, self.reviewer, self._task_execution(),
            observability=self.observability,
        )
        return skills

    def finish_skill_proposal(self, result: dict, tenant_id: str, auto_activate: bool) -> dict:
        """Restore the original evaluate-then-activate option without bypassing smoke/CAS."""
        if auto_activate and result.get("decision") == "ready":
            version = result["version"]
            try:
                if self.skill_evolution.activate(version["skill_name"], int(version["version"]), tenant_id):
                    self.reload_skills()
                    result.update(decision="activated", reason="Shared-runtime replay and activation smoke passed")
                    result["version"] = {**version, "active": True}
            except ValueError as exc:
                result["reason"] = "candidate retained without activation: %s" % exc
        return result

    def _active_agent_skills(self, tenant_id: str) -> list:
        values = {skill.name: skill for skill in self.registry.agent_skills()}
        for version in self.store.list_active_skill_artifacts(tenant_id):
            artifact = validate_artifact(version["artifact"], version["skill_name"])
            values[version["skill_name"]] = AgentSkill.from_artifact(
                artifact, str(version["version"])
            )
        return [values[name] for name in sorted(values)]

    def list_skills(self, tenant_id: str) -> list:
        scanners = [item for item in self.registry.list() if item.get("kind") == "scanner"]
        return scanners + [{
            "name": skill.name, "version": skill.version,
            "description": skill.description, "source": skill.source,
            "kind": "agent-skill", "sandboxed": False,
            "permissions": list(skill.allowed_tools),
            "content_sha256": skill.content_sha256,
            "resources": list(skill.resource_paths),
        } for skill in self._active_agent_skills(tenant_id)]

    def _validate_review(self, repository: str, diff: str) -> None:
        if not repository or len(repository) > 250:
            raise ValueError("repository is required and must be at most 250 characters")
        size = len(diff.encode("utf-8"))
        if size == 0:
            raise ValueError("diff is required")
        if size > self.settings.max_diff_bytes:
            raise ValueError("diff exceeds maximum size of %d bytes" % self.settings.max_diff_bytes)

    def _require_agentic_model(self) -> None:
        if self.chat_client is None:
            raise RuntimeError("agentic review requires a configured model")

    def _task_token_budget_policy(self, roles=None):
        roles = roles if roles is not None else [role.strip() for role in
            self.settings.enabled_agents.split(",") if role.strip()]
        limits = {role: getattr(self.settings, "agent_token_budget", 8000) for role in roles}
        active = self.store.get_active_skill_version("llm-review")
        if active:
            config = (self.store.get_prompt_run("llm-review", active["version"]) or {}).get(
                "metrics", {}).get("production", {}).get("candidate_config", {})
            limits.update(config.get("budget_parameters") or {})
        return make_token_budget_policy(getattr(self.settings, "total_token_budget", 32000), limits)

    def _create_task(
        self, repository: str, diff: str, pull_request: Optional[int], source: str,
        tenant_id: str = "default", repository_root: str = "",
        enabled_agents: Optional[list] = None,
        enabled_skills: Optional[list] = None, envelope: Optional[dict] = None,
        task_id: str = "",
    ) -> str:
        task_id = task_id or str(uuid.uuid4())
        encoded = diff.encode("utf-8")
        self.store.create(task_id, repository, pull_request, {
            "source": source, "repository": repository, "pull_request": pull_request,
            "tenant_id": tenant_id, "diff_url": "", "github_issue_url": "",
            "installation_id": None, "publish_review": False,
            "base_sha": None, "head_sha": None, **(envelope or {}),
            "diff_bytes": len(encoded), "diff_sha256": hashlib.sha256(encoded).hexdigest(),
            "mode": RunMode.AGENTIC.value,
            "repository_root": repository_root,
            "enabled_agents": enabled_agents if enabled_agents is not None else [
                role.strip() for role in self.settings.enabled_agents.split(",") if role.strip()],
            "enabled_skills": enabled_skills or [],
            "token_budget_policy": self._task_token_budget_policy(enabled_agents),
        }, tenant_id)
        self.store.save_task_payload(task_id, diff)
        if (envelope or {}).get("publish_review"):
            self.store.set_publication(task_id, "pending")
        return task_id

    def create_review(
        self, repository: str, diff: str, pull_request: Optional[int] = None,
        source: str = "api", tenant_id: str = "default", mode: str = "",
        repository_root: str = "", enabled_agents: Optional[list] = None,
        enabled_skills: Optional[list] = None,
    ) -> Dict[str, Any]:
        self._validate_review(repository, diff)
        RunMode.parse(mode) if mode else None
        self._require_agentic_model()
        self._validate_repository_root(repository_root)
        self._validate_enabled_agents(enabled_agents)
        self._validate_enabled_skills(enabled_skills, tenant_id)
        self._authorize_repository(tenant_id, repository)
        task_id = str(uuid.uuid4())
        try:
            with self.queue.inline_execution(task_id), self.observability.span(
                "review", task_id, task_id=task_id, tenant_id=tenant_id,
                repository=repository,
            ), metrics.timer("review_duration"):
                # Own execution before the new task becomes visible to resume.
                self._create_task(
                    repository, diff, pull_request, source, tenant_id,
                    repository_root, enabled_agents, enabled_skills, task_id=task_id,
                )
                report = self._run_review(
                    task_id, repository, pull_request, diff, tenant_id
                )
            metrics.inc("reviews_total")
            return {"task_id": task_id, "state": "SUCCESS", "report": report.to_dict()}
        except RuntimeCancelled:
            raise
        except Exception as exc:
            if self.store.is_cancelled(task_id):
                raise RuntimeCancelled("Task was cancelled") from exc
            self.alerts.evaluate(tenant_id)
            raise

    def enqueue_review(
        self, repository: str, diff: str, pull_request: Optional[int] = None,
        source: str = "api", github_issue_url: str = "", installation_id: Optional[int] = None,
        tenant_id: str = "default", mode: str = "", repository_root: str = "",
        enabled_agents: Optional[list] = None,
        enabled_skills: Optional[list] = None,
    ) -> Dict[str, Any]:
        self._validate_review(repository, diff)
        RunMode.parse(mode) if mode else None
        self._require_agentic_model()
        self._validate_repository_root(repository_root)
        self._validate_enabled_agents(enabled_agents)
        self._validate_enabled_skills(enabled_skills, tenant_id)
        self._authorize_repository(tenant_id, repository)
        task_id = self._create_task(
            repository, diff, pull_request, source, tenant_id,
            repository_root, enabled_agents, enabled_skills,
            {"github_issue_url": github_issue_url, "installation_id": installation_id,
             "publish_review": bool(github_issue_url and self.settings.auto_post_review)},
        )
        self._dispatch(task_id)
        return {"task_id": task_id, "state": "PENDING", "queue": self.queue.backend}

    def _dispatch(self, task_id: str, prepare=None) -> None:
        try:
            # All replay-sensitive input comes from durable storage, never Redis.
            self.queue.submit({"task_id": task_id}, message_id=task_id, prepare=prepare)
        except TaskConflictError:
            raise
        except Exception as exc:
            current = self.store.get(task_id) or {}
            if current.get("state") not in {"SUCCESS", "CANCELLED"}:
                step = max([int(item.get("step", 0)) for item in current.get("trace", [])] or [0]) + 1
                self.store.fail(task_id, str(exc), TraceEvent(step, TaskState.FAILED,
                    "Task dispatch failed: %s" % exc, utc_now()))
            raise DispatchUnavailable("task %s could not be queued; retry delivery or resume" % task_id, task_id) from exc
        metrics.inc("reviews_enqueued_total")

    @staticmethod
    def _pull_revision(pull: dict) -> dict:
        return {
            "base_sha": (pull.get("base") or {}).get("sha"),
            "head_sha": (pull.get("head") or {}).get("sha"),
        }

    def _download_task_diff(self, task: dict) -> str:
        task_id = task["id"]
        envelope = task.get("input") or {}
        if str(envelope.get("source", "")).startswith("github") and "installation_id" not in envelope:
            raise PermanentTaskError("saved GitHub installation identity is unknown; create a new task")
        client = self.github_client_for_installation(envelope.get("installation_id"))
        repository, number = task["repository"], task.get("pull_request")
        client.ensure_repository_access(repository)
        before = self._pull_revision(client.get_pull_request(repository, number))
        if not all(before.values()):
            raise PermanentTaskError("GitHub PR revision is unavailable; create a new task")
        for key, actual in before.items():
            expected = envelope.get(key)
            if expected and expected != actual:
                raise PermanentTaskError("GitHub PR version changed before download; create a new task")
        diff = client.fetch_diff(envelope["diff_url"])
        self._validate_review(repository, diff)
        after = self._pull_revision(client.get_pull_request(repository, number))
        if before != after:
            raise PermanentTaskError("GitHub PR version changed during download; create a new task")
        if self.store.is_cancelled(task_id):
            raise RuntimeCancelled("Task was cancelled")
        encoded = diff.encode("utf-8")
        self.store.save_downloaded_payload(task_id, diff, {
            "diff_pending": False, "diff_bytes": len(encoded),
            "diff_sha256": hashlib.sha256(encoded).hexdigest(),
            **before, "reviewed_revision": dict(before),
        })
        return diff

    def _publish_review(self, task: dict) -> None:
        if task.get("publication_status") not in {"pending", "failed"}:
            return
        task_id = task["id"]
        envelope = task.get("input") or {}
        if not envelope.get("publish_review") or not envelope.get("github_issue_url"):
            self.store.set_publication(task_id, "failed", "saved publication envelope is incomplete")
            return
        self.store.set_publication(task_id, "pending")
        try:
            if str(envelope.get("source", "")).startswith("github") and "installation_id" not in envelope:
                raise ValueError("saved GitHub installation identity is unknown; publication cannot be recovered")
            client = self.github_client_for_installation(envelope.get("installation_id"))
            reviewed_revision = (envelope.get("reviewed_revision") or
                                 ((task.get("report") or {}).get("execution") or {}).get("reviewed_revision") or {})
            reviewed_head = reviewed_revision.get("head_sha") or envelope.get("head_sha")
            reviewed_base = reviewed_revision.get("base_sha") or envelope.get("base_sha")
            if not reviewed_head:
                raise ValueError("reviewed PR head is unknown; create a new review before publishing")
            current = client.get_pull_request(task["repository"], task["pull_request"])
            if ((current.get("head") or {}).get("sha") != reviewed_head or
                    (reviewed_base and (current.get("base") or {}).get("sha") != reviewed_base)):
                self.store.set_publication(task_id, "superseded")
                return
            client.upsert_comment(
                envelope["github_issue_url"], to_markdown(task["report"]),
                envelope.get("comment_marker") or "<!-- evoagent-review:%s -->" % task_id,
            )
        except Exception as exc:
            # Publishing is independently resumable and must not rerun the review.
            self.store.set_publication(task_id, "failed", str(exc))
        else:
            self.store.set_publication(task_id, "sent")

    def _process_queued(self, payload: Dict[str, Any]) -> None:
        task_id = payload["task_id"]
        task = self.store.get(task_id)
        if not task:
            raise PermanentTaskError("task record no longer exists")
        if self.store.is_cancelled(task_id):
            raise RuntimeCancelled("Task was cancelled")
        if task.get("state") == TaskState.SUCCESS.value:
            self._publish_review(task)
            return
        tenant_id = task.get("tenant_id") or "default"
        try:
            diff = self.store.get_task_payload(task_id)
            if diff is None and (task.get("input") or {}).get("diff_url"):
                diff = self._download_task_diff(task)
            if diff is None:
                raise PermanentTaskError("task payload no longer exists")
            if self.store.is_cancelled(task_id):
                raise RuntimeCancelled("Task was cancelled")
            with self.observability.span(
                "review.async", task_id, task_id=task_id, tenant_id=tenant_id,
            ), metrics.timer("review_duration"):
                self._run_review(task_id, task["repository"], task.get("pull_request"), diff, tenant_id)
            metrics.inc("reviews_total")
        except RuntimeCancelled:
            raise
        except Exception as exc:
            if self.store.is_cancelled(task_id):
                raise RuntimeCancelled("Task was cancelled") from exc
            current = self.store.get(task_id) or task
            if current.get("state") not in {"SUCCESS", "CANCELLED", "FAILED"}:
                step = max([int(item.get("step", 0)) for item in current.get("trace", [])] or [0]) + 1
                self.store.fail(task_id, str(exc), TraceEvent(
                    step, TaskState.FAILED, "Review failed: %s" % exc, utc_now(),
                ))
            metrics.inc("reviews_failed_total")
            self.alerts.evaluate(tenant_id)
            raise
        self._publish_review(self.store.get(task_id) or task)

    def _on_dead_letter(self, payload: Dict[str, Any], error: str) -> None:
        task_id = payload.get("task_id", "")
        task = self.store.get(task_id) if task_id else None
        tenant_id = (task or {}).get("tenant_id") or "default"
        if task and task.get("state") not in {
            TaskState.SUCCESS.value, TaskState.FAILED.value, TaskState.CANCELLED.value,
        }:
            step = max(
                [int(item.get("step", 0)) for item in task.get("trace", [])] or [0]
            ) + 1
            self.store.fail(
                task_id, error,
                TraceEvent(
                    step, TaskState.FAILED,
                    "Task entered the dead-letter queue: %s" % error, utc_now(),
                ),
            )
        self.store.create_alert(
            tenant_id, "dlq:%s" % (task_id or "unknown"), "critical",
            "Task %s entered the dead-letter queue: %s" % (task_id, error),
        )
        metrics.inc("dead_letters_total")

    def handle_github_pull_request(
        self, payload: Dict[str, Any], delivery_id: str,
        payload_sha256: str, tenant_id: str = "",
    ) -> Dict[str, Any]:
        installation_id = (payload.get("installation") or {}).get("id")
        tenant_id = tenant_id or (
            self.store.installation_tenant(installation_id) if installation_id else None
        ) or self.settings.default_tenant_id
        if not delivery_id:
            raise ValueError("X-GitHub-Delivery is required")
        action = payload.get("action")
        if action not in {"opened", "reopened", "synchronize"}:
            self.store.claim_webhook(delivery_id, tenant_id, "pull_request", payload_sha256)
            self.store.complete_webhook(delivery_id, None)
            return {"ignored": True, "reason": "unsupported pull_request action: %s" % action}
        # Invalid requests never consume a delivery or create an orphaned task.
        self._require_agentic_model()
        pull = payload.get("pull_request") or {}
        repository = (payload.get("repository") or {}).get("full_name", "")
        number, diff_url = payload.get("number"), pull.get("diff_url")
        if not repository or len(repository) > 250 or type(number) is not int or not diff_url:
            raise ValueError("invalid GitHub pull_request payload")
        if installation_id is not None and type(installation_id) is not int:
            raise ValueError("invalid GitHub installation identity")
        self._authorize_repository(tenant_id, repository)
        suggested_id = str(uuid.uuid4())
        envelope = {
            "source": "github-webhook", "repository": repository, "pull_request": number,
            "tenant_id": tenant_id, "diff_pending": True, "diff_url": diff_url,
            "github_issue_url": pull.get("issue_url", ""), "installation_id": installation_id,
            "publish_review": bool(pull.get("issue_url") and self.settings.auto_post_review),
            "repository_root": "", "enabled_agents": [role.strip() for role in
                self.settings.enabled_agents.split(",") if role.strip()], "enabled_skills": [],
            "head_repository": ((pull.get("head") or {}).get("repo") or {}).get("full_name"),
            "mode": RunMode.AGENTIC.value,
            "token_budget_policy": self._task_token_budget_policy(),
            **self._pull_revision(pull),
        }
        task_id, created = self.store.create_webhook_task(
            delivery_id, tenant_id, payload_sha256, suggested_id, repository, number, envelope,
        )
        delivery = self.store.get_webhook(delivery_id) or {}
        if delivery.get("dispatch_status") not in {"queued", "ignored"}:
            try:
                self._dispatch(task_id)
            except DispatchUnavailable as exc:
                self.store.mark_webhook_dispatch(delivery_id, "failed", str(exc))
                raise
            self.store.mark_webhook_dispatch(delivery_id, "queued")
        task = self.store.get(task_id) or {}
        return {
            "task_id": task_id, "state": task.get("state", "PENDING"),
            "duplicate": not created, "queue": self.queue.backend,
            "will_post_to_github": bool((task.get("input") or {}).get("publish_review")),
        }

    def github_client_for_installation(self, installation_id: Optional[int] = None) -> GitHubClient:
        if installation_id is None:
            return self.github
        if not self.settings.github_app_id or not self.settings.github_private_key_path:
            raise ValueError("GitHub App credentials are not configured")
        token = GitHubAppAuthenticator(
            self.settings.github_app_id, self.settings.github_private_key_path
        ).installation_token(installation_id)
        return GitHubClient(token)

    def create_fix(
        self, task_id: str, installation_id: Optional[int] = None,
        tenant_id: Optional[str] = None,
    ) -> dict:
        task = self.store.get(task_id, tenant_id)
        if not task or task.get("state") != "SUCCESS" or not task.get("report"):
            raise ValueError("completed task not found")
        if task.get("pull_request") is None:
            raise ValueError("fix commits require a GitHub pull request task")
        actual_tenant = task.get("tenant_id") or tenant_id or "default"
        if not self.store.repository_allowed(actual_tenant, task["repository"], True):
            raise PermissionError("automatic repair is not enabled for this repository")
        saved_installation = (task.get("input") or {}).get("installation_id")
        envelope = task.get("input") or {}
        if str(envelope.get("source", "")).startswith("github") and "installation_id" not in envelope:
            raise TaskConflictError("saved GitHub installation identity is unknown; create a new review task")
        if installation_id is not None and installation_id != saved_installation:
            raise TaskConflictError("repair must use the task's saved GitHub installation identity")
        result = self.fixer.create_fix_commits(
            self.github_client_for_installation(saved_installation),
            task["repository"], task["pull_request"], task["report"],
        )
        metrics.inc("fix_runs_total")
        return result

    def record_feedback(
        self, task_id: str, category: str, finding: Optional[dict], note: str,
        tenant_id: Optional[str] = None,
    ) -> dict:
        task = self.store.get(task_id, tenant_id)
        if not task:
            raise ValueError("task not found")
        if task.get("state") != "SUCCESS" or not task.get("report"):
            raise ValueError("feedback requires a completed review task")
        if category not in {"false_positive", "missed_issue", "bad_fix", "accepted"}:
            raise ValueError("unsupported feedback category")
        self.store.record_failure_case(task_id, category, {"finding": finding, "note": note[:2000]})
        self.memory.remember_feedback(
            task.get("tenant_id") or tenant_id or "default", task["repository"],
            task_id, category, finding, note[:2000],
        )
        metrics.inc("feedback_total")
        return {"recorded": True, "category": category}

    def resume_task(self, task_id: str, tenant_id: Optional[str] = None) -> dict:
        task = self.store.get(task_id, tenant_id)
        if not task:
            raise ValueError("task not found")
        if task["state"] == "CANCELLED" or self.store.is_cancelled(task_id):
            raise TaskConflictError("CANCELLED is terminal; create a new review task")
        if self.queue.is_active(task_id):
            return {"task_id": task_id, "state": task["state"], "active": True}
        if task["state"] == "SUCCESS":
            if task.get("publication_status") in {"pending", "failed"}:
                self._dispatch(task_id, prepare=lambda: self.store.set_publication(task_id, "pending"))
                current = self.store.get(task_id) or task
                return {"task_id": task_id, "state": "SUCCESS", "publication_status": current["publication_status"],
                        "resumed": True, "report": task["report"]}
            return {"task_id": task_id, "state": "SUCCESS", "report": task["report"],
                    "publication_status": task.get("publication_status", "not_requested")}
        if self.store.get_task_payload(task_id) is None and not (task.get("input") or {}).get("diff_url"):
            raise ValueError("task payload and saved download URL are no longer available")
        def prepare():
            current = self.store.get(task_id) or {}
            step = max([int(item.get("step", 0)) for item in current.get("trace", [])] or [0]) + 1
            if not self.store.transition(task_id, TraceEvent(step, TaskState.PENDING,
                    "Review recovery queued", utc_now())):
                raise TaskConflictError("task became terminal before recovery; reload its state")
            reset = getattr(self.reviewer, "reset_failed_assignments", None)
            if reset:
                reset(task_id)
        self._dispatch(task_id, prepare=prepare)
        current = self.store.get(task_id) or task
        return {"task_id": task_id, "state": current["state"], "resumed": True}

    def cancel_task(self, task_id: str, tenant_id: Optional[str] = None) -> bool:
        return self.store.request_cancel(task_id, tenant_id)

    def _authorize_repository(self, tenant_id: str, repository: str) -> None:
        if not self.store.repository_allowed(tenant_id, repository):
            raise PermissionError("repository is not authorized for this tenant")

    def _validate_repository_root(self, repository_root: str) -> None:
        if not repository_root:
            return
        import os
        if not os.path.isabs(repository_root) or not os.path.isdir(repository_root):
            raise ValueError("repository_root must be an existing absolute directory")
        from .repository_safety import confined_path, trusted_root
        if not trusted_root(repository_root, self.settings.trusted_repository_roots):
            raise ValueError("repository_root must be an administrator-configured trusted repository root")
        confined_path(repository_root, "")

    @staticmethod
    def _validate_enabled_agents(enabled_agents: Optional[list]) -> None:
        if enabled_agents is None:
            return
        allowed = {"lead", "security", "correctness-reliability", "critic"}
        unknown = set(enabled_agents).difference(allowed)
        if unknown:
            raise ValueError("unsupported enabled Agent role(s): %s" % ", ".join(sorted(unknown)))
        if "lead" not in enabled_agents or not set(enabled_agents).intersection({"security", "correctness-reliability"}):
            raise ValueError("review requires Lead and at least one professional Worker")

    def _validate_enabled_skills(
        self, enabled_skills: Optional[list], tenant_id: str,
    ) -> None:
        if enabled_skills is None:
            return
        if not all(isinstance(item, str) for item in enabled_skills):
            raise ValueError("enabled_skills must contain Agent Skill names")
        available = {skill.name for skill in self._active_agent_skills(tenant_id)}
        unknown = set(enabled_skills).difference(available)
        if unknown:
            raise ValueError(
                "unknown enabled Agent Skill(s): %s" % ", ".join(sorted(unknown))
            )
