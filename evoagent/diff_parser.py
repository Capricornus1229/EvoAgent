"""A shared, count-driven parser for Git and plain unified diffs."""
import re
from dataclasses import dataclass, field
from typing import List

from .models import ChangedLine


class DiffParseError(ValueError):
    pass


HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


@dataclass
class DiffHunk:
    path: str
    header: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: List[str]
    old_no_newline: bool = False
    new_no_newline: bool = False


@dataclass
class FileChange:
    old_path: str
    new_path: str
    kind: str = "modified"
    hunks: List[DiffHunk] = field(default_factory=list)

    @property
    def path(self):
        return self.old_path if self.new_path == "/dev/null" else self.new_path


@dataclass
class ParsedDiff:
    files: List[str]
    added_lines: List[ChangedLine]
    hunks: List[DiffHunk] = field(default_factory=list)
    file_changes: List[FileChange] = field(default_factory=list)

    @property
    def unreviewed_files(self):
        reviewed = {line.path for line in self.added_lines}
        return [{"path": change.path, "reason": change.kind if change.kind != "modified"
                 else "no added lines"} for change in self.file_changes if change.path not in reviewed]


def normalize_git_path(value: str, strip_prefix: bool = True) -> str:
    """Decode Git C quoting as bytes, then UTF-8 (never unicode_escape)."""
    value = value.split("\t", 1)[0].strip()
    if value.startswith('"'):
        if not value.endswith('"'):
            raise DiffParseError("unterminated quoted Git path")
        raw, data, index = value[1:-1], bytearray(), 0
        escapes = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13,
                   '"': 34, "\\": 92}
        while index < len(raw):
            char = raw[index]
            if char != "\\":
                data.extend(char.encode("utf-8"))
                index += 1
                continue
            index += 1
            if index >= len(raw):
                raise DiffParseError("invalid Git path escape")
            if raw[index] in "01234567":
                match = re.match(r"[0-7]{1,3}", raw[index:])
                number = int(match.group(0), 8)
                if number > 255:
                    raise DiffParseError("invalid Git path byte")
                data.append(number)
                index += len(match.group(0))
            elif raw[index] in escapes:
                data.append(escapes[raw[index]])
                index += 1
            else:
                raise DiffParseError("unsupported Git path escape")
        try:
            value = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DiffParseError("Git path is not UTF-8") from exc
    if value == "/dev/null":
        return value
    if strip_prefix and value.startswith(("a/", "b/")):
        value = value[2:]
    if not value or value.startswith("/") or "\x00" in value or any(
        part in {"", ".", ".."} for part in value.split("/")
    ):
        raise DiffParseError("unsafe or empty diff path")
    return value


def _git_header_paths(value):
    # Git may leave spaces unquoted. The b/ delimiter separates the paths.
    match = re.match(r'^("(?:[^"\\]|\\.)*"|a/.*?) ("(?:[^"\\]|\\.)*"|b/.*)$', value)
    if not match:
        raise DiffParseError("invalid diff --git paths")
    return normalize_git_path(match.group(1)), normalize_git_path(match.group(2))


def parse_unified_diff(diff: str) -> ParsedDiff:
    if not isinstance(diff, str):
        raise DiffParseError("diff must be text")
    lines = diff.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    changes, hunks, added = [], [], []
    current, old_header = None, None
    current_headers = False
    index = 0
    while index < len(lines):
        raw = lines[index]
        if raw.startswith("diff --git "):
            if old_header is not None:
                raise DiffParseError("missing new-file header")
            old, new = _git_header_paths(raw[11:])
            current = FileChange(old, new)
            changes.append(current)
            current_headers = False
        elif raw.startswith("--- "):
            if old_header is not None:
                raise DiffParseError("missing new-file header")
            old_header = normalize_git_path(raw[4:])
        elif raw.startswith("+++ "):
            if old_header is None:
                raise DiffParseError("new-file header has no old-file header")
            new = normalize_git_path(raw[4:])
            if current is None or current_headers:
                current = FileChange(old_header, new)
                changes.append(current)
            else:
                current.old_path, current.new_path = old_header, new
            current.kind = "deleted" if new == "/dev/null" else (
                "added" if old_header == "/dev/null" else current.kind)
            old_header = None
            current_headers = True
        elif raw.startswith("@@"):
            match = HUNK.match(raw)
            if not match or current is None or not current_headers or old_header is not None:
                raise DiffParseError("invalid hunk or missing file headers")
            old_start, old_count = int(match[1]), int(match[2] if match[2] is not None else 1)
            new_start, new_count = int(match[3]), int(match[4] if match[4] is not None else 1)
            if (old_count and old_start < 1) or (new_count and new_start < 1):
                raise DiffParseError("nonempty hunk must start at a positive line")
            hunk = DiffHunk(current.path, raw, old_start, old_count, new_start, new_count, [])
            used_old = used_new = 0
            previous = ""
            index += 1
            while index < len(lines):
                body = lines[index]
                if body == "\\ No newline at end of file":
                    if previous not in {" ", "+", "-"}:
                        raise DiffParseError("newline marker has no content line")
                    hunk.old_no_newline |= previous in {" ", "-"}
                    hunk.new_no_newline |= previous in {" ", "+"}
                    previous = ""
                    index += 1
                    continue
                if used_old == old_count and used_new == new_count:
                    break
                if not body or body[0] not in {" ", "+", "-"}:
                    raise DiffParseError("hunk is truncated or contains an invalid line")
                prefix = body[0]
                if prefix in {" ", "-"}:
                    used_old += 1
                if prefix in {" ", "+"}:
                    if prefix == "+":
                        if current.new_path == "/dev/null":
                            raise DiffParseError("deleted file contains added lines")
                        added.append(ChangedLine(current.path, new_start + used_new, body[1:]))
                    used_new += 1
                if used_old > old_count or used_new > new_count:
                    raise DiffParseError("hunk line counts exceed header")
                hunk.lines.append(body)
                previous = prefix
                index += 1
            if (used_old, used_new) != (old_count, new_count):
                raise DiffParseError("hunk line counts do not match header")
            current.hunks.append(hunk)
            hunks.append(hunk)
            continue
        elif raw.startswith(("Binary files ", "GIT binary patch")):
            if current is None:
                raise DiffParseError("binary diff has no file header")
            current.kind = "binary"
        elif raw.startswith("rename from ") and current:
            current.old_path = normalize_git_path(raw[12:], False)
            current.kind = "renamed"
        elif raw.startswith("rename to ") and current:
            current.new_path = normalize_git_path(raw[10:], False)
            current.kind = "renamed"
        elif raw.startswith(("index ", "old mode ", "new mode ", "new file mode ",
                             "deleted file mode ", "similarity index ", "dissimilarity index ",
                             "copy from ", "copy to ")):
            if current is None:
                raise DiffParseError("Git metadata has no file header")
        elif current and current.kind == "binary":
            pass  # Encoded binary blocks are explicitly outside review coverage.
        elif raw.strip():
            raise DiffParseError("content outside a unified diff hunk: %s" % raw[:80])
        index += 1
    if old_header is not None or not changes:
        raise DiffParseError("diff has no complete file change")
    files = list(dict.fromkeys(change.path for change in changes if change.path != "/dev/null"))
    return ParsedDiff(files, added, hunks, changes)
