from typing import Any, Dict


def to_markdown(report: Dict[str, Any], publication: Dict[str, Any] = None) -> str:
    title = "# EvoAgent PR Review"
    if report.get("pull_request") is not None:
        title += " — #%s" % report["pull_request"]
    lines = [
        title,
        "",
        "**Repository:** `%s`  " % report.get("repository", ""),
        "**Risk:** `%s`  " % report.get("risk", "unknown"),
        "**Reviewer:** `%s`" % report.get("reviewer", "unknown"),
        "",
        report.get("summary", ""),
        "",
    ]
    run_mode = report.get("run_mode") or {}
    execution = report.get("execution") or {}
    revision = execution.get("reviewed_revision") or {}
    if revision.get("head_sha"):
        lines.extend(["**Reviewed head SHA:** `%s`" % revision["head_sha"], ""])
    if execution.get("review_status") == "needs_review":
        lines.extend(["**Review status:** `needs_review` — withheld candidates require human verification.", ""])
    counts = execution.get("finding_counts") or {}
    if counts:
        lines.extend([
            "Candidates: raw `%s`; Lead selected `%s`; gate passed `%s`; published `%s`." % (
                counts.get("raw_candidates", 0), counts.get("lead_selected", 0),
                counts.get("gate_accepted", 0), counts.get("final_published", 0)), "",
        ])
    if publication:
        lines.extend(["**GitHub publication:** `%s`" % publication.get("publication_status", "not_requested"), ""])
        if publication.get("publication_error"):
            lines.extend(["Publication error: %s" % publication["publication_error"], ""])
        if publication.get("publication_status") == "superseded":
            lines.extend(["The PR head changed after review; this report was not posted to the newer revision.", ""])
    scope = execution.get("review_scope") or {}
    if scope.get("unreviewed_files"):
        lines.extend(["**Outside added-line coverage:** %s" % ", ".join(
            "`%s` (%s)" % (item["path"], item["reason"]) for item in scope["unreviewed_files"]), ""])
    if run_mode:
        lines.extend([
            "## Execution facts",
            "",
            "- Mode: requested `%s`, effective `%s`" % (
                run_mode.get("requested", "unknown"), run_mode.get("effective", "unknown")
            ),
            "- Model calls: `%s`; tool calls: `%s`" % (
                execution.get("llm_calls", 0), execution.get("tool_calls", 0)
            ),
            "- Tokens: input `%s`, output `%s`, total `%s`" % (
                execution.get("input_tokens", 0), execution.get("output_tokens", 0),
                execution.get("total_tokens", 0),
            ),
            "- Token cost: `$%.8f`; latency: `%s ms`" % (
                float(execution.get("cost_usd", 0) or 0), execution.get("duration_ms", 0)
            ),
            "",
        ])
        if run_mode.get("fallback_reason"):
            lines.extend(["> %s" % run_mode["fallback_reason"], ""])
        context = execution.get("context_management") or {}
        if context:
            memory = context.get("memory_recall") or {}
            lines[-1:-1] = [
                "- Context compression: `%s` view(s), estimated reduction `%.1f%%`; "
                "recalled memories `%s`" % (
                    context.get("compression_calls", 0),
                    max(0.0, float(context.get("estimated_reduction_ratio", 0) or 0) * 100),
                    memory.get("recalled", 0),
                )
            ]
    collaboration = report.get("collaboration") or {}
    coverage = collaboration.get("coverage") or {}
    if coverage:
        lines.extend(["**Professional coverage:** `%s`%s" % (
            ", ".join(coverage.get("required_workers") or []),
            "; reduced coverage" if coverage.get("omitted_workers") else ""), ""])
    if collaboration and run_mode.get("effective") == "agentic" and execution.get("llm_calls", 0):
        lines.extend([
            "## LLM Agent collaboration",
            "",
            "- Protocol: `%s`" % collaboration.get("protocol", "unknown"),
            "- Actual roles: `%s`" % ", ".join(collaboration.get("roles") or []),
            "- Candidate findings: `%s`; critic decisions: `%s`" % (
                collaboration.get("candidate_findings_before_critic", collaboration.get("candidate_findings", 0)),
                len(collaboration.get("critic_decisions") or []),
            ),
            "",
        ])
    findings = report.get("findings", [])
    if not findings:
        rejected = execution.get("rejected_findings") or []
        if rejected:
            lines.extend(["No finding is eligible for publication; %d candidate(s) were withheld by gates." % len(rejected), ""])
            for item in rejected:
                lines.append("- `%s:%s` — %s" % (item.get("path", ""), item.get("line", 0), "; ".join(item.get("reasons") or [])))
            return "\n".join(lines) + "\n"
        lines.append("✅ No actionable issue detected in the added lines.")
        return "\n".join(lines) + "\n"
    rejected = execution.get("rejected_findings") or []
    if rejected:
        lines.extend(["## Withheld candidates", ""])
        for item in rejected:
            lines.append("- `%s:%s` — %s" % (item.get("path", ""), item.get("line", 0), "; ".join(item.get("reasons") or [])))
        lines.append("")
    lines.extend(["## Findings", ""])
    icons = {"critical": "🚨", "high": "🔴", "medium": "🟠", "low": "🟡"}
    for index, item in enumerate(findings, 1):
        severity = item.get("severity", "medium")
        lines.extend(
            [
                "### %d. %s %s" % (index, icons.get(severity, "•"), item.get("title", "Finding")),
                "",
                "`%s:%s` · **%s** · `%s`" % (
                    item.get("path", ""), item.get("line", 0), severity.upper(), item.get("rule_id", "")),
                "",
                item.get("explanation", ""),
                "",
                "**Evidence**",
                "",
                "```text",
                item.get("evidence", ""),
                "```",
                "",
                "**Evidence references:** %s" % (
                    ", ".join(
                        "`%s`" % ref.get("evidence_id", "")
                        for ref in item.get("evidence_refs", []) if ref.get("evidence_id")
                    ) or "none"
                ),
                "",
                "**Suggested fix:** %s" % item.get("fix", ""),
                "",
                "**Suggested test:** %s" % item.get("test", ""),
                "",
            ]
        )
    return "\n".join(lines) + "\n"
