"""LLM unified-patch generation with structural and trusted-repository verification."""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import List

from .diff_parser import parse_unified_diff, normalize_git_path
from .llm import JsonChatClient
from .telemetry import ExecutionLedger
from .verifier import RepairVerifier


PATCH_PROMPT = """You are the Fix Agent. Produce one minimal unified diff that fixes the supplied
verified findings without unrelated changes. Patch paths must be among allowed_paths. Preserve
behavior except for the defect. Return JSON only: {"patch":"--- a/path\n+++ b/path\n@@ ...",
"behavioral_claims":["..."],"related_tests":["..."]}. Do not use Markdown fences. If evidence is
insufficient, return {"patch":"","reason":"..."}."""


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: List[str]
    old_no_newline: bool = False
    new_no_newline: bool = False


@dataclass
class FilePatch:
    path: str
    hunks: List[Hunk]


HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_unified_patch(value: str, allowed_paths: List[str]) -> List[FilePatch]:
    parsed = parse_unified_diff(value)
    allowed = {normalize_git_path(item, False) for item in allowed_paths}
    patches = []
    for change in parsed.file_changes:
        if change.old_path != change.new_path or change.path not in allowed:
            raise ValueError("patch path is not allowed: %s" % change.path)
        if not change.hunks or change.kind == "binary":
            raise ValueError("file patch contains no supported hunks")
        patches.append(FilePatch(change.path, [Hunk(
            h.old_start, h.old_count, h.new_start, h.new_count, list(h.lines),
            h.old_no_newline, h.new_no_newline,
        ) for h in change.hunks]))
    if not patches:
        raise ValueError("model returned no valid file patches")
    if len({item.path for item in patches}) != len(patches):
        raise ValueError("a file may appear only once in a generated patch")
    return patches


def apply_file_patch(content: str, patch: FilePatch) -> str:
    source = content.replace("\r\n", "\n").splitlines()
    output = []
    cursor = 0
    for hunk in patch.hunks:
        target = hunk.old_start if hunk.old_count == 0 else hunk.old_start - 1
        if target < cursor or target > len(source):
            raise ValueError("overlapping or out-of-range patch hunk")
        output.extend(source[cursor:target])
        cursor = target
        for line in hunk.lines:
            prefix, text = line[0], line[1:]
            if prefix in {" ", "-"}:
                if cursor >= len(source) or source[cursor] != text:
                    raise ValueError("patch context does not match source at line %d" % (cursor + 1))
                if prefix == " ":
                    output.append(text)
                cursor += 1
            elif prefix == "+":
                output.append(text)
    output.extend(source[cursor:])
    trailing = "\n" if content.endswith(("\n", "\r\n")) else ""
    if patch.hunks and patch.hunks[-1].new_no_newline:
        trailing = ""
    elif patch.hunks and patch.hunks[-1].old_no_newline:
        trailing = "\n"
    return "\n".join(output) + trailing


class VerifiedPatchFixer:
    def __init__(self, client: JsonChatClient, verifier: RepairVerifier):
        self.client = client
        self.verifier = verifier

    def create_fix_commits(
        self, client, repository: str, pull_request: int, report: dict,
    ) -> dict:
        pull = client.get_pull_request(repository, pull_request)
        source_sha = pull["head"]["sha"]
        reviewed_sha = ((report.get("execution") or {}).get("reviewed_revision") or {}).get("head_sha")
        if not reviewed_sha:
            return {"status": "suggestion-only", "branch": None, "commits": [],
                    "suggestions": [item.get("fix", "") for item in report.get("findings", [])],
                    "note": "Automatic publication requires a report pinned to a reviewed head SHA."}
        if source_sha != reviewed_sha:
            raise ValueError("PR head changed since review; create a new review before repairing")
        source_repository = pull["head"].get("repo", {}).get("full_name") or repository
        paths = sorted({
            str(item.get("path")) for item in report.get("findings", [])
            if item.get("path") and (item.get("gate") or {}).get("passed", True)
        })
        if not paths:
            return {"status": "suggestion-only", "branch": None, "commits": [],
                    "note": "No verified finding is eligible for patch generation."}
        originals = {}
        for path in paths:
            originals[path] = client.get_file(
                source_repository, path, source_sha
            )["decoded_content"]
        ledger = ExecutionLedger("fix-agent")
        generated = self.client.complete_json(
            "fix-agent", PATCH_PROMPT,
            json.dumps({
                "allowed_paths": paths, "findings": report.get("findings", []),
                "files": originals,
            }, ensure_ascii=False), ledger, 8000,
        )
        patch_text = str(generated.get("patch", ""))
        if not patch_text.strip():
            return {
                "status": "suggestion-only", "branch": None, "commits": [],
                "reason": str(generated.get("reason", "insufficient evidence"))[:1000],
                "execution": ledger.summary(),
                "note": "The Fix Agent did not produce an evidence-backed patch.",
            }
        patches = parse_unified_patch(patch_text, paths)
        files = dict(originals)
        for patch in patches:
            files[patch.path] = apply_file_patch(originals[patch.path], patch)
        changed = {path: content for path, content in files.items() if content != originals[path]}
        if not changed:
            raise ValueError("generated patch makes no change")
        structural = self.verifier.verify_contents(changed)
        if not structural["passed"]:
            return {
                "status": "blocked", "branch": None, "commits": [],
                "patch": patch_text, "verification": {"structural": structural},
                "execution": ledger.summary(),
                "note": "Patch failed AST/CST or compilation checks.",
            }
        if not self.verifier.test_command:
            return {
                "status": "suggestion-only", "branch": None, "commits": [],
                "patch": patch_text, "verification": {"structural": structural},
                "execution": ledger.summary(),
                "note": "No repository test command is configured; this is a suggestion, not a successful automatic fix.",
            }
        if source_repository.lower() not in self.verifier.trusted_github_repositories:
            return {"status": "suggestion-only", "branch": None, "commits": [],
                    "patch": patch_text, "verification": {"structural": structural},
                    "execution": ledger.summary(),
                    "note": "Repository execution and publication require an administrator-trusted source repository."}
        archive = client.download_archive(source_repository, source_sha)
        baseline = self.verifier.verify_archive(archive, {}, source_repository)
        patched = self.verifier.verify_archive(archive, changed, source_repository)
        comparison = self.verifier.compare(baseline, patched)
        if not comparison["passed"]:
            return {
                "status": "blocked", "branch": None, "commits": [],
                "patch": patch_text,
                "verification": {
                    "structural": structural, "before": baseline,
                    "after": patched, "comparison": comparison,
                },
                "execution": ledger.summary(),
                "note": "Patch was blocked by before/after trusted-repository verification.",
            }
        if client.get_pull_request(repository, pull_request)["head"]["sha"] != source_sha:
            raise ValueError("PR head changed during repair; create a new review")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        branch = "evoagent/fix-pr-%d-%s" % (pull_request, stamp)
        commit = client.create_atomic_commit(
            repository, branch, source_sha, changed,
            "fix: apply verified EvoAgent patch for PR #%d" % pull_request,
        )
        draft = client.create_draft_pull_request(
            repository, "fix: verified EvoAgent patch for #%d" % pull_request,
            branch, pull.get("base", {}).get("ref", "main"),
            "LLM-generated patch. AST/CST, compilation and configured tests passed in an isolated checkout. This PR is intentionally a draft.",
        )
        return {
            "status": "verified-draft", "branch": branch, "source_sha": source_sha,
            "commits": [{"sha": commit.get("sha"), "paths": sorted(changed)}],
            "draft_pull_request": {"number": draft.get("number"), "url": draft.get("html_url")},
            "patch": patch_text,
            "behavioral_claims": generated.get("behavioral_claims") or [],
            "related_tests": generated.get("related_tests") or [],
            "verification": {
                "structural": structural, "before": baseline,
                "after": patched, "comparison": comparison,
            },
            "execution": ledger.summary(),
            "note": "Verified patch was published only as a draft pull request.",
        }


class SuggestionOnlyFixer:
    def create_fix_commits(self, client, repository, pull_request, report):
        return {
            "status": "suggestion-only", "branch": None, "commits": [],
            "suggestions": [item.get("fix", "") for item in report.get("findings", [])],
            "note": "No model is configured. Suggestions are not described as an automatic fix.",
        }
