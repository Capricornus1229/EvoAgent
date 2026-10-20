"""Shared, deterministic Skill artifacts and model protocol fixture."""

import json

from evoagent.skill_evolution import validate_artifact

RISK_DIFF = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+dangerous_call(data)\n"

CLEAN_DIFF = "--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-old\n+safe_call(data)\n"

def skill_markdown(name="review-dangerous-calls", include_rule=True):
    guidance = (
        "\n## Confirmed SEC-DANGEROUS-CALL guidance\n\n"
        "Inspect added behavior equivalent to `dangerous_call(data)`.\n"
        "Report `SEC-DANGEROUS-CALL` at `high` severity when context confirms it.\n"
        if include_rule else ""
    )
    return """---
name: %s
description: Review added code for confirmed project-specific dangerous calls.
---

# Review dangerous calls

Use changed-line evidence and report only actionable defects.%s
""" % (name, guidance)

def artifact(name="review-dangerous-calls", include_rule=True):
    return validate_artifact(skill_markdown(name, include_rule), name)

class SkillAwareClient:
    provider = "fake"
    model = "skill-aware"

    def complete_json(self, role, _system, user, ledger=None, max_tokens=None):
        if ledger:
            ledger.record_model(role, self.provider, self.model, {
                "prompt_tokens": 10, "completion_tokens": 5,
            }, 1)
        managed = json.loads(user)
        task = json.loads(managed["task"])
        if role == "lead":
            if task["phase"] == "delegate":
                requested = task.get("requested_agent_skills") or []
                selected = requested or [
                    item["name"] for item in task.get("available_agent_skills") or []
                ]
                return {"action": "final", "delegations": [{
                    "assignment_id": "security-1", "worker": "security",
                    "objective": "Review project-specific dangerous calls",
                    "skills": selected,
                }, {
                    "assignment_id": "reliability-1", "worker": "correctness-reliability",
                    "objective": "Review correctness and reliability", "skills": selected,
                }], "risk_level": "normal"}
            if task["phase"] == "assess-workers":
                return {"action": "final", "revision_requests": [], "critic_objective": "Verify"}
            if task["phase"] == "finalize":
                return {
                    "action": "final",
                    "accepted_finding_indices": list(range(len(task["candidate_findings"]))),
                    "confidence_adjustments": [],
                }
        if role == "security":
            instructions = "\n".join(
                item.get("instructions", "") for item in task.get("active_agent_skills") or []
            )
            rendered_diff = json.dumps(task["diff"], ensure_ascii=False)
            if "dangerous_call(data)" in instructions and "dangerous_call(data)" in rendered_diff:
                return {"action": "final", "findings": [{
                    "rule_id": "SEC-DANGEROUS-CALL", "severity": "high",
                    "title": "Dangerous call", "explanation": "Confirmed unsafe API was added.",
                    "path": "a.py", "line": 1, "evidence": "dangerous_call(data)",
                    "fix": "Use safe_call instead.", "test": "Add a regression test.",
                    "confidence": .9,
                    "call_chain": [{"path": "a.py", "line": 1, "symbol": "dangerous_call"}],
                }]}
            return {"action": "final", "findings": []}
        if role == "correctness-reliability":
            return {"action": "final", "findings": []}
        if role == "critic":
            return {"action": "final", "decisions": [{
                "finding_index": index, "accepted": True, "objections": [],
                "confidence_adjustment": 0.0,
            } for index, _item in enumerate(task["candidates"])]}
        raise AssertionError(role)
