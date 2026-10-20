"""Path confinement and administrator-owned execution trust."""
import os
from pathlib import Path


def confined_path(root: str, relative: str) -> str:
    root_path = os.path.abspath(root)
    if os.path.islink(root_path) or os.path.realpath(root_path) != root_path:
        raise ValueError("repository root must not contain symlinks")
    relative = str(relative).replace("\\", "/")
    if os.path.isabs(relative) or any(part == ".." for part in relative.split("/")):
        raise ValueError("path escapes repository root")
    target = os.path.abspath(os.path.join(root_path, relative))
    if os.path.commonpath([root_path, target]) != root_path:
        raise ValueError("path escapes repository root")
    current = root_path
    for part in Path(os.path.relpath(target, root_path)).parts:
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise ValueError("repository symlinks are not supported")
    if os.path.commonpath([root_path, os.path.realpath(target)]) != root_path:
        raise ValueError("real path escapes repository root")
    return target


def trusted_root(root: str, allowed) -> bool:
    return bool(root) and os.path.realpath(root) in {os.path.realpath(path) for path in allowed}
