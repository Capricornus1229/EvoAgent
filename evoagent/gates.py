"""Deterministic finding and release gates shared by all run modes."""
from dataclasses import dataclass
import ast
import textwrap
from typing import Any, Dict, Iterable, List

from .diff_parser import ParsedDiff
from .models import Finding, Severity


@dataclass
class GateResult:
    accepted: List[Finding]
    rejected: List[Dict[str, Any]]
    checks: Dict[str, Any]


class FindingGate:
    STRONG_EVIDENCE_TOOLS = {
        "ast_analyze", "symbol", "git_context", "run_scanners",
        "semgrep", "bandit", "eslint", "typecheck", "test", "diff-ast-analyze",
        "declarative-scanner",
    }

    def __init__(self, minimum_confidence: float = 0.55):
        self.minimum_confidence = minimum_confidence

    @staticmethod
    def _anchors(record):
        """Extract locations from factual tool outputs, never model supplied refs."""
        output = record.get("output")
        if isinstance(output, dict) and (
            output.get("available") is False or output.get("found") is False
            or output.get("valid") is False or output.get("valid_python_ast") is False
        ):
            return []
        anchors = []

        def walk(value, path="", symbol=""):
            if isinstance(value, list):
                for entry in value:
                    walk(entry, path, symbol)
            elif isinstance(value, dict):
                current_path = value.get("path", path)
                current_symbol = value.get("symbol", symbol)
                line = value.get("line")
                if isinstance(current_path, str) and current_path and isinstance(line, int) and not isinstance(line, bool) and line > 0:
                    anchors.append({"path": current_path, "line": line, "symbol": current_symbol, "content": str(value.get("content", ""))})
                if isinstance(current_path, str) and isinstance(value.get("start_line"), int) and isinstance(value.get("content"), str):
                    for offset, content in enumerate(value["content"].splitlines()):
                        anchors.append({"path": current_path, "line": value["start_line"] + offset, "symbol": "", "content": content})
                for key, child in value.items():
                    if isinstance(child, (dict, list)):
                        walk(child, current_path, current_symbol)

        walk(output)
        return anchors

    @staticmethod
    def _code_symbols(content):
        try:
            tree = ast.parse(textwrap.dedent(content))
        except SyntaxError:
            return set()
        symbols = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    symbols.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    symbols.add(node.func.attr)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                symbols.add(node.name)
        return symbols

    @staticmethod
    def _diff_scopes(parsed):
        """Find Python ownership in contiguous, actually supplied new-side code.

        A finding at a return statement may name its enclosing function, rather
        than a callee on that one line. Parse real context plus additions; never
        synthesize missing lines, trust hunk-heading text, or execute repository
        code. These spans prove a symbol/location relationship, not a bug or a
        multi-function call relationship.
        """
        by_path = {}
        conflicts = set()
        for hunk in parsed.hunks:
            if not hunk.path.endswith('.py'):
                continue
            lines = by_path.setdefault(hunk.path, {})
            number = hunk.new_start
            for raw in hunk.lines:
                if raw.startswith((' ', '+')):
                    content = raw[1:]
                    if number in lines and lines[number] != content:
                        conflicts.add(hunk.path)
                    lines[number] = content
                    number += 1
        scopes = []
        for path, lines in by_path.items():
            if path in conflicts:
                continue
            blocks = []
            for number in sorted(lines):
                if not blocks or number != blocks[-1][-1] + 1:
                    blocks.append([])
                blocks[-1].append(number)
            for block in blocks:
                source = textwrap.dedent('\n'.join(lines[n] for n in block))
                try:
                    tree = ast.parse(source)
                except (SyntaxError, ValueError, RecursionError):
                    # Partial, non-Python or invalid context is not evidence.
                    continue

                def visit(node, parents=()):
                    owners = parents
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        owners = parents + (node.name,)
                        scopes.append({
                            'path': path, 'line': block[0] + node.lineno - 1,
                            'end_line': block[0] + node.end_lineno - 1,
                            'symbols': {node.name, '.'.join(owners)},
                        })
                    for child in ast.iter_child_nodes(node):
                        visit(child, owners)

                visit(tree)
        return scopes

    def _chain_valid(self, chain, location, valid_locations, registry, task_id, diff_scopes):
        if not chain:
            return False, ""
        if not isinstance(chain, list) or len(chain) > 20:
            return False, "call chain must be an array of at most 20 nodes"
        facts = [record for record in registry.values() if isinstance(record, dict) and record.get("task_id") == task_id]
        anchors = [anchor for record in facts for anchor in self._anchors(record)]
        keys = []
        verified_locations = set()
        for node in chain:
            if not isinstance(node, dict):
                return False, "call chain contains a non-object node"
            path, number, symbol = node.get("path"), node.get("line"), node.get("symbol")
            if (not isinstance(path, str) or not path or path.startswith("/") or ".." in path.split("/")
                or isinstance(number, bool) or not isinstance(number, int) or number < 1
                or not isinstance(symbol, str) or not symbol.strip()):
                return False, "call chain node requires a repository path, positive line and symbol"
            # A symbol node may point to its definition, while the finding
            # points to a statement inside that definition. Derive ownership
            # only from the real, contiguous new-side AST, including supplied
            # context. Never use model-provided end_line/range information.
            definition_scopes = [
                scope for scope in diff_scopes
                if scope['path'] == path and scope['line'] == number
                and symbol in scope['symbols']
            ]
            source = valid_locations.get((path, number), "")
            diff_symbol = symbol in self._code_symbols(source)
            scope_symbol = bool(source.strip()) and any(
                scope['path'] == path and scope['line'] <= number <= scope['end_line']
                and symbol in scope['symbols'] for scope in diff_scopes
            )
            tool_symbol = any(
                anchor["path"] == path and anchor["line"] == number and (
                    anchor["symbol"] == symbol
                    or symbol in self._code_symbols(anchor["content"])
                ) for anchor in anchors
            )
            if not (diff_symbol or tool_symbol or scope_symbol or definition_scopes):
                return False, "call chain node has no verified symbol/location anchor"
            keys.append((path, number, symbol))
            verified_locations.add((path, number))
            if location in valid_locations and any(
                scope['path'] == location[0]
                and scope['line'] <= location[1] <= scope['end_line']
                for scope in definition_scopes
            ):
                verified_locations.add(location)
        if location not in verified_locations:
            return False, "call chain does not anchor the finding location"
        if len(keys) > 1:
            edges = set()
            for record in facts:
                output = record.get("output") or {}
                if not isinstance(output, dict):
                    continue
                # Server-generated edges retain the exact owning definition.
                # A definitions × callees cross-product invents relationships
                # when a file contains more than one same-name definition.
                if record.get("tool") == "symbol":
                    for edge in output.get("call_edges") or []:
                        if not isinstance(edge, dict):
                            continue
                        caller, callee = edge.get("caller"), edge.get("callee")
                        if isinstance(caller, dict) and isinstance(callee, dict):
                            edges.add((
                                (caller.get("path"), caller.get("line"), caller.get("symbol")),
                                (callee.get("path"), callee.get("line"), callee.get("symbol")),
                            ))
            if any((keys[index], keys[index + 1]) not in edges for index in range(len(keys) - 1)):
                return False, "call chain relationship lacks actual tool evidence"
        return True, ""

    def apply(
        self, findings: Iterable[Finding], parsed: ParsedDiff,
        evidence_registry: Dict[str, dict] = None, task_id: str = "",
    ) -> GateResult:
        registry = dict(evidence_registry or {})
        valid_locations = {(item.path, item.line): item.content for item in parsed.added_lines}
        # Canonical diff references are grounded by the server, not by the
        # model's claimed tool name. They are location evidence, not strong AST.
        for (path, number), content in valid_locations.items():
            identity = 'diff:%s:%d' % (path, number)
            registry[identity] = {
                'evidence_id': identity, 'task_id': task_id, 'tool': 'changed_line',
                'output': {'path': path, 'line': number, 'content': content},
            }
        diff_scopes = self._diff_scopes(parsed)
        accepted, rejected = [], []
        counters = {"format": 0, "evidence": 0, "confidence": 0, "release": 0}
        for finding in findings:
            reasons = []
            warnings = []
            location = (finding.path, finding.line)
            if (
                location not in valid_locations or not finding.rule_id.strip()
                or not finding.title.strip() or not finding.explanation.strip()
            ):
                reasons.append("format gate: invalid location or required field")
                counters["format"] += 1

            quoted = finding.evidence.strip()
            line = valid_locations.get(location, "").strip()
            exact_line = bool(quoted and quoted in line)
            refs = list(finding.evidence_refs)
            valid_refs = []
            invalid_refs = []
            for reference in refs:
                identity = reference.get("evidence_id") if isinstance(reference, dict) else None
                registered = registry.get(identity) if isinstance(identity, str) else None
                if not isinstance(registered, dict) or registered.get("task_id") != task_id:
                    invalid_refs.append(str(identity or "missing evidence_id"))
                    continue
                if not any((anchor["path"], anchor["line"]) == location for anchor in self._anchors(registered)):
                    invalid_refs.append(str(identity) + " (no finding-location anchor)")
                    continue
                if not any(item["evidence_id"] == identity for item in valid_refs):
                    valid_refs.append({**registered, "evidence_id": identity})
            strong_refs = [
                item for item in valid_refs
                if str(item.get("tool")) in self.STRONG_EVIDENCE_TOOLS
            ]
            chain_valid, chain_error = self._chain_valid(
                finding.call_chain, location, valid_locations, registry, task_id, diff_scopes,
            )
            if invalid_refs:
                warnings.append("ignored unregistered, cross-task or unanchored evidence: %s" % ", ".join(invalid_refs))
            if chain_error:
                warnings.append("ignored call chain: " + chain_error)
            # Optional model metadata cannot veto independently grounded code.
            # Keep only server-registered facts and a completely verified chain.
            finding.evidence_refs = [dict(item) for item in valid_refs]
            if not chain_valid:
                finding.call_chain = []
            if not exact_line and not valid_refs and not chain_valid:
                reasons.append("evidence gate: no matching code, call chain or tool evidence")
                counters["evidence"] += 1
            if finding.confidence < self.minimum_confidence:
                reasons.append("confidence gate: score below %.2f" % self.minimum_confidence)
                counters["confidence"] += 1
            if finding.severity in {Severity.CRITICAL, Severity.HIGH} and (
                not finding.fix.strip() or not finding.test.strip()
            ):
                reasons.append("release gate: high-risk finding lacks fix or test guidance")
                counters["release"] += 1

            finding.gate = {
                "passed": not reasons, "reasons": reasons,
                "exact_location_evidence": exact_line,
                "strong_evidence_count": len(strong_refs),
                "verified_call_chain": chain_valid,
                "warnings": warnings,
            }
            if reasons:
                rejected.append({
                    "rule_id": finding.rule_id, "path": finding.path,
                    "line": finding.line, "reasons": reasons,
                    "finding": finding.to_dict(),
                })
            else:
                accepted.append(finding)
        return GateResult(
            accepted, rejected,
            {
                "format": {"rejected": counters["format"]},
                "evidence": {"rejected": counters["evidence"]},
                "confidence": {
                    "minimum": self.minimum_confidence,
                    "rejected": counters["confidence"],
                },
                "release": {"rejected": counters["release"]},
                "accepted": len(accepted), "rejected": len(rejected),
            },
        )
