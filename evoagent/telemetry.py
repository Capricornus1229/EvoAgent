"""Per-review accounting for real model/tool calls and complete agent traces."""
from dataclasses import asdict, dataclass
import copy
import math
import threading
import time
import uuid
from typing import Any, Dict, List

from .runtime import RuntimeBudgetExceeded, check_execution


TOKEN_BUDGET_SCHEMA = "task-role-input-output-v1"
DEFAULT_TOKEN_ROLES = ("lead", "security", "correctness-reliability", "critic")


def make_token_budget_policy(total_limit=32000, role_limits=None):
    return validate_token_budget_policy({
        "schema": TOKEN_BUDGET_SCHEMA, "unit": "input+output", "estimator": "chars-v1",
        "unknown_usage": "reserve-estimate", "total_limit": total_limit,
        "role_limits": dict(role_limits) if role_limits is not None else {role: 8000 for role in DEFAULT_TOKEN_ROLES},
    })


def validate_token_budget_policy(policy):
    keys = {"schema", "unit", "estimator", "unknown_usage", "total_limit", "role_limits"}
    if not isinstance(policy, dict) or set(policy) != keys:
        raise ValueError("token_budget_policy requires a complete versioned policy; reevaluate legacy snapshots")
    if (policy["schema"] != TOKEN_BUDGET_SCHEMA or policy["unit"] != "input+output"
            or policy["estimator"] != "chars-v1" or policy["unknown_usage"] != "reserve-estimate"):
        raise ValueError("unsupported token_budget_policy")
    limits = policy["role_limits"]
    if not isinstance(limits, dict) or not limits or not all(isinstance(role, str) and role for role in limits):
        raise ValueError("token_budget_policy role_limits must be a nonempty role map")
    for value in [policy["total_limit"], *limits.values()]:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("token budget limits must be positive integers")
    return copy.deepcopy(policy)


def estimate_model_input(system, user):
    # Count exactly the messages sent, plus Chat Completions framing overhead.
    from .context_manager import estimate_tokens
    return estimate_tokens(system) + estimate_tokens(user) + 16


class _CallLedger:
    def __init__(self, owner, ticket):
        self.owner, self.ticket = owner, ticket

    def __getattr__(self, name):
        return getattr(self.owner, name)

    def record_model(self, role, provider, model, usage, duration_ms, ok=True, error=""):
        self.owner._settle_model(self.ticket, role, provider, model, usage, duration_ms, ok, error)


@dataclass
class ModelCall:
    role: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    duration_ms: int
    ok: bool
    error: str = ""
    usage_source: str = "reported"
    call_id: str = ""
    budget_charged_tokens: int = 0


@dataclass
class ToolCall:
    role: str
    tool: str
    arguments: Dict[str, Any]
    ok: bool
    duration_ms: int
    result_preview: str = ""
    error: str = ""


class ExecutionLedger:
    def __init__(
        self, mode: str, input_cost_per_million: float = 0.0,
        output_cost_per_million: float = 0.0,
        token_budget_policy=None, persist=None,
    ):
        self.mode = mode
        self.input_cost_per_million = max(0.0, input_cost_per_million)
        self.output_cost_per_million = max(0.0, output_cost_per_million)
        self.started = time.monotonic()
        self._restored_duration_ms = 0
        self.model_calls: List[ModelCall] = []
        self.tool_calls: List[ToolCall] = []
        self.agent_traces: Dict[str, List[Dict[str, Any]]] = {}
        self._lock = threading.RLock()
        self.token_budget_policy = validate_token_budget_policy(token_budget_policy) if token_budget_policy is not None else None
        self._reservations = {}
        self._persist = persist

    def ensure_role_budget(self, role, limit):
        """Compatibility for direct BoundedRole users; production passes a frozen policy."""
        with self._lock:
            if self.token_budget_policy is None:
                self.token_budget_policy = make_token_budget_policy(32000, {role: limit})
            elif role not in self.token_budget_policy["role_limits"]:
                raise RuntimeBudgetExceeded("role %s has no token budget" % role)

    def _remaining_locked(self, role):
        if self.token_budget_policy is None:
            raise ValueError("model invocation requires token_budget_policy")
        role_limit = self.token_budget_policy["role_limits"].get(role)
        if role_limit is None:
            raise RuntimeBudgetExceeded("role %s has no token budget" % role)
        total = sum(item["charged_tokens"] for item in self._reservations.values())
        used = sum(item["charged_tokens"] for item in self._reservations.values() if item["role"] == role)
        return max(0, min(self.token_budget_policy["total_limit"] - total, role_limit - used))

    def remaining_tokens(self, role):
        with self._lock:
            return self._remaining_locked(role)

    def reserve(self, role, estimated_input, requested_output):
        """Reserve input and output atomically before any provider side effect."""
        check_execution()
        with self._lock:
            remaining = self._remaining_locked(role)
            output = min(int(requested_output), remaining - int(estimated_input))
            if estimated_input < 0 or output < 1:
                raise RuntimeBudgetExceeded("%s token budget exhausted before model request" % role)
            ticket = uuid.uuid4().hex
            self._reservations[ticket] = {"role": role, "input_tokens": int(estimated_input),
                "output_tokens": output, "charged_tokens": int(estimated_input) + output, "status": "pending"}
            try:
                self._persist_locked()
            except BaseException:
                # No provider request has been sent. A local persistence error
                # must not consume a reservation in the still-running process.
                self._reservations.pop(ticket, None)
                raise
            return ticket, output

    def _persist_locked(self):
        if self._persist is not None:
            self._persist(self.summary())

    def complete_json(self, client, role, system, user, max_tokens):
        ticket, output = self.reserve(role, estimate_model_input(system, user), max_tokens)
        started = time.monotonic()
        try:
            result = client.complete_json(role, system, user, _CallLedger(self, ticket), max_tokens=output)
        except BaseException as exc:
            self._settle_model(ticket, role, getattr(client, "provider", "unknown"), getattr(client, "model", "unknown"),
                {}, int((time.monotonic() - started) * 1000), False, str(exc))
            self.check_token_budget()
            raise
        self._settle_model(ticket, role, getattr(client, "provider", "unknown"), getattr(client, "model", "unknown"),
            {}, int((time.monotonic() - started) * 1000), True, "")
        check_execution()
        self.check_token_budget()
        return result

    @staticmethod
    def _reported_tokens(usage):
        if not isinstance(usage, dict):
            return None
        values = [usage.get("prompt_tokens", usage.get("input_tokens")),
                  usage.get("completion_tokens", usage.get("output_tokens"))]
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) or value < 0 or int(value) != value for value in values):
            return None
        return tuple(int(value) for value in values)

    def _settle_model(self, ticket, role, provider, model, usage, duration_ms, ok, error):
        with self._lock:
            reservation = self._reservations[ticket]
            if reservation["status"] != "pending":
                return
            counts = self._reported_tokens(usage)
            source = "reported" if counts is not None else "estimated-unknown"
            inputs, outputs = counts if counts is not None else (reservation["input_tokens"], reservation["output_tokens"])
            self._append_model(role, provider, model, usage, inputs, outputs, duration_ms, ok, error,
                               source, ticket, inputs + outputs)
            reservation.update(charged_tokens=inputs + outputs, status="settled", usage_source=source)
            self._persist_locked()

    def check_token_budget(self):
        with self._lock:
            if self.token_budget_policy is None:
                return
            used = sum(item["charged_tokens"] for item in self._reservations.values())
            if used > self.token_budget_policy["total_limit"]:
                raise RuntimeBudgetExceeded("task token budget exceeded after model response")
            for role, limit in self.token_budget_policy["role_limits"].items():
                if sum(item["charged_tokens"] for item in self._reservations.values() if item["role"] == role) > limit:
                    raise RuntimeBudgetExceeded("%s token budget exceeded after model response" % role)

    def _append_model(self, role, provider, model, usage, inputs, outputs, duration_ms, ok, error,
                      source="reported", ticket="", charged=0):
        reported_cost = usage.get("cost") if isinstance(usage, dict) else None
        valid_cost = (not isinstance(reported_cost, bool) and isinstance(reported_cost, (int, float))
                      and math.isfinite(reported_cost) and reported_cost >= 0)
        cost = float(reported_cost) if valid_cost else self.estimate_cost(inputs, outputs)
        self.model_calls.append(ModelCall(role, provider, model, inputs, outputs, round(cost, 8),
            int(duration_ms), bool(ok), str(error)[:1000], source, ticket, charged))

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        return round(
            input_tokens * self.input_cost_per_million / 1_000_000
            + output_tokens * self.output_cost_per_million / 1_000_000,
            8,
        )

    def record_model(
        self, role: str, provider: str, model: str, usage: Dict[str, Any],
        duration_ms: int, ok: bool = True, error: str = "",
    ) -> None:
        input_tokens = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
        output_tokens = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
        with self._lock:
            self._append_model(role, provider, model, usage, input_tokens, output_tokens, duration_ms, ok, error)

    def record_tool(
        self, role: str, tool: str, arguments: Dict[str, Any], ok: bool,
        duration_ms: int, result: Any = "", error: str = "",
    ) -> None:
        preview = str(result)[:1000]
        call = ToolCall(
            role, tool, dict(arguments), bool(ok), int(duration_ms), preview,
            str(error)[:1000],
        )
        with self._lock:
            self.tool_calls.append(call)

    def trace(self, role: str, event: str, **detail) -> None:
        item = {
            "sequence": 0, "event": event,
            "elapsed_ms": self._restored_duration_ms + int((time.monotonic() - self.started) * 1000), **detail,
        }
        with self._lock:
            values = self.agent_traces.setdefault(role, [])
            item["sequence"] = len(values) + 1
            values.append(item)

    def restore(self, summary: Dict[str, Any], restore_budget=True) -> None:
        """Restore a checkpointed ledger before resuming an agentic session."""
        models = [
            ModelCall(**item) for item in summary.get("model_call_log") or []
            if isinstance(item, dict)
        ]
        tools = [
            ToolCall(**item) for item in summary.get("tool_call_log") or []
            if isinstance(item, dict)
        ]
        traces = {
            str(role): [dict(item) for item in values if isinstance(item, dict)]
            for role, values in (summary.get("agent_traces") or {}).items()
            if isinstance(values, list)
        }
        with self._lock:
            if restore_budget or self.token_budget_policy is None:
                self.model_calls = models
            self.tool_calls = tools
            self.agent_traces = traces
            if restore_budget:
                self._restored_duration_ms = max(0, int(summary.get("duration_ms", 0) or 0))
                budget = summary.get("token_budget")
                if budget:
                    policy = validate_token_budget_policy(budget["policy"])
                    if self.token_budget_policy is not None and policy != self.token_budget_policy:
                        raise ValueError("task token budget policy changed; create a new task")
                    self.token_budget_policy = policy
                    self._reservations = copy.deepcopy(budget.get("reservations") or {})
                    # A crashed request may have been billed. Keep its full reservation.
                    for ticket, reservation in self._reservations.items():
                        if reservation["status"] == "pending":
                            reservation.update(status="settled", usage_source="estimated-uncertain")
                            self._append_model(reservation["role"], "unknown", "unknown", {},
                                reservation["input_tokens"], reservation["output_tokens"], 0, False,
                                "provider outcome unknown after interrupted execution", "estimated-uncertain",
                                ticket, reservation["charged_tokens"])
                elif models and self.token_budget_policy is not None:
                    raise ValueError("legacy execution has no durable token budget proof; create a new task")

    def summary(self, include_trace: bool = True) -> Dict[str, Any]:
        with self._lock:
            models = [asdict(item) for item in self.model_calls]
            tools = [asdict(item) for item in self.tool_calls]
            traces = {key: list(value) for key, value in self.agent_traces.items()}
            budget = None
            if self.token_budget_policy is not None:
                charged = sum(item["charged_tokens"] for item in self._reservations.values())
                budget = {"policy": copy.deepcopy(self.token_budget_policy), "reservations": copy.deepcopy(self._reservations),
                    "charged_tokens": charged, "remaining_tokens": max(0, self.token_budget_policy["total_limit"] - charged),
                    "role_charged_tokens": {role: sum(item["charged_tokens"] for item in self._reservations.values() if item["role"] == role)
                                            for role in self.token_budget_policy["role_limits"]}}
        return {
            "mode": self.mode,
            "llm_calls": len(models),
            "tool_calls": len(tools),
            "input_tokens": sum(item["input_tokens"] for item in models),
            "output_tokens": sum(item["output_tokens"] for item in models),
            "total_tokens": sum(
                item["input_tokens"] + item["output_tokens"] for item in models
            ),
            "token_budget": budget,
            "estimated_usage_calls": sum(item["usage_source"] != "reported" for item in models),
            "cost_usd": round(sum(item["cost_usd"] for item in models), 8),
            "duration_ms": self._restored_duration_ms + int((time.monotonic() - self.started) * 1000),
            "failed_model_calls": sum(not item["ok"] for item in models),
            "failed_tool_calls": sum(not item["ok"] for item in tools),
            "model_call_log": models,
            "tool_call_log": tools,
            "agent_traces": traces if include_trace else {},
        }
