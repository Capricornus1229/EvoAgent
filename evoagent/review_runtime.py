"""One constructor for production review and frozen Skill replay."""
import math

from .agentic_core import AgenticReviewer
from .context_manager import ContextManager
from .harness import ReviewHarness
from .telemetry import validate_token_budget_policy


DEFAULT_TASK_EXECUTION = {"max_steps": 8, "timeout_seconds": 120, "node_retries": 2}


def validate_task_execution(task_execution, *, allow_defaults: bool = False) -> dict:
    """Validate frozen whole-task limits without silently repairing old snapshots."""
    if task_execution is None and allow_defaults:
        return dict(DEFAULT_TASK_EXECUTION)
    if not isinstance(task_execution, dict) or set(task_execution) != set(DEFAULT_TASK_EXECUTION):
        raise ValueError("task_execution must contain max_steps, timeout_seconds and node_retries")
    limits = dict(task_execution)
    for name, minimum in (("max_steps", 1), ("node_retries", 0)):
        value = limits[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError("task_execution %s must be an integer >= %d" % (name, minimum))
    timeout = limits["timeout_seconds"]
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout < 1):
        raise ValueError("task_execution timeout_seconds must be a finite number >= 1")
    return limits


def build_review_harness(store, reviewer, task_execution, observability=None):
    """Use identical workflow, retry policy and total budgets for live and replay."""
    return ReviewHarness(
        store, reviewer, **validate_task_execution(task_execution),
        observability=observability,
    )


def build_agentic_runtime(
    store, client, snapshot: dict, scanners=None, memory_manager=None,
    skill_provider=None,
):
    """Build from the same explicit configuration in both execution paths.

    The snapshot contains public configuration and artifacts, never credentials.
    Callers supply the authenticated client and the already configured scanners.
    Validated Prompt policies share exactly this constructor during replay and production.
    """
    from .evolution_v2 import validate_structured_candidate
    structured = validate_structured_candidate(dict(snapshot.get("structured_config") or {}))
    kwargs = {
        "default_token_budget": snapshot.get("token_budget", 8000),
        "token_budget_policy": validate_token_budget_policy(snapshot.get("token_budget_policy")),
        "default_time_budget": snapshot.get("time_budget_seconds", 60),
        "input_cost_per_million": snapshot.get("input_cost_per_million", 0.0),
        "output_cost_per_million": snapshot.get("output_cost_per_million", 0.0),
        "enabled_roles": set(snapshot.get("roles") or [
            "lead", "security", "correctness-reliability", "critic",
        ]),
        "scanners": list(scanners or []),
        "review_test_command": snapshot.get("review_test_command", ""),
        "prompt_overlay": snapshot.get("prompt_overlay", ""),
        "structured_config": structured,
        "memory_manager": memory_manager,
        "context_manager": ContextManager(**dict(snapshot.get("context") or {})),
        "skill_provider": skill_provider,
    }
    # The execution trust policy is administrator configuration and is frozen too.
    import inspect
    supported = inspect.signature(AgenticReviewer).parameters
    for name in ("trusted_repository_roots", "trusted_github_repositories"):
        if name in supported:
            kwargs[name] = snapshot.get(name, ())
    return AgenticReviewer(store, client, **kwargs)
