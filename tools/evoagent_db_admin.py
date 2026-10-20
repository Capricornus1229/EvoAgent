#!/usr/bin/env python3
"""Offline-aware SQLite maintenance for the supplied EvoAgent schema.

Python 3.11+, standard library only. Does not import or change EvoAgent.
Read commands never create a database. Mutations are dry-runs unless --apply;
--apply also requires --service-stopped and confirmation, and always backs up.
Run without a subcommand for a numbered interactive console.
This is a host-administrator tool, not a replacement for application RBAC.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

VERSION = "2.0.0"
TERMINAL = ("SUCCESS", "FAILED", "CANCELLED")
STATES = TERMINAL + ("PENDING", "PLANNING", "EXECUTING", "REVIEWING")
MEMORY_SCOPES = ("working", "episodic", "semantic", "procedural")
TASK_CHILDREN = (
    "trace_events", "failure_cases", "checkpoints", "task_payloads",
    "agent_messages", "agent_memories", "release_observations",
)
CONFIG_KEYS = (
    "EVOAGENT_DB_PATH", "EVOAGENT_DATABASE_URL", "EVOAGENT_REDIS_URL",
    "EVOAGENT_SKILLS_DIR", "EVOAGENT_DEFAULT_TENANT_ID",
)
SENSITIVE_KEYS = {"password", "password_hash", "api_key", "access_token", "refresh_token", "secret", "token", "authorization"}


class AdminError(Exception):
    """A safe, actionable maintenance failure."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def project_root(explicit: str | None) -> Path:
    if explicit:
        root = Path(explicit).expanduser().resolve()
        if not root.is_dir():
            raise AdminError("--project-root 必须是已经存在的目录。")
        return root
    for root in (Path.cwd(), Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent):
        if (root / "evoagent" / "config.py").is_file():
            return root
    return Path.cwd().resolve()


def config_values(root: Path) -> dict[str, str]:
    # Match this project's precedence without importing config.py or printing secrets.
    result = {key: os.environ[key] for key in CONFIG_KEYS if key in os.environ}
    for file in (root / ".env", root / "evoagent" / ".env"):
        if not file.exists():
            continue
        try:
            text = file.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise AdminError(f"无法读取配置文件 {file}: {exc.strerror}") from exc
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            if key not in CONFIG_KEYS:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            result.setdefault(key, value)
    return result


def resolve_database(args: argparse.Namespace) -> tuple[Path, Path, dict[str, str]]:
    root = project_root(args.project_root)
    config = config_values(root)
    if config.get("EVOAGENT_DATABASE_URL", "").strip() and not args.db:
        raise AdminError(
            "配置了 EVOAGENT_DATABASE_URL。本脚本仅管理 SQLite，已拒绝自动选择 evoagent.db。"
            "若确实要查看一个独立 SQLite 备份，请显式使用 --db /绝对路径/备份.db。"
        )
    raw = args.db if args.db else config.get("EVOAGENT_DB_PATH", "evoagent.db")
    if not raw or raw == ":memory:" or raw.startswith("file:"):
        raise AdminError("仅接受现有 SQLite 文件路径，不接受内存库或 URI。")
    # Relative paths follow the server's working-directory semantics, not .env location.
    path = Path(raw).expanduser().resolve(strict=True)
    if not path.is_file():
        raise AdminError("数据库路径不是普通文件。")
    with path.open("rb") as handle:
        if handle.read(16) != b"SQLite format 3\x00":
            raise AdminError("目标不是 SQLite 数据库；没有修改目标文件。")
    return path, root, config


def connect(path: Path, writable: bool = False) -> sqlite3.Connection:
    mode = "rw" if writable else "ro"
    conn = sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA trusted_schema=OFF")
    return conn


def tables(conn: sqlite3.Connection) -> dict[str, set[str]]:
    names = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    return {name: {row[1] for row in conn.execute("PRAGMA table_info(" + quote(name) + ")")} for name in names}


def validate_schema(conn: sqlite3.Connection) -> dict[str, set[str]]:
    schema = tables(conn)
    required = {
        "tasks": {"id", "tenant_id", "repository", "state", "created_at", "report_json", "input_json"},
        "users": {"id", "username", "password_hash"},
        "agent_memories": {"id", "tenant_id", "repository", "task_id", "scope", "content", "expires_at"},
    }
    for table, columns in required.items():
        if not columns.issubset(schema.get(table, set())):
            raise AdminError(f"{table} 表结构与本次 EvoAgent 版本不匹配，拒绝继续。")
    return schema


def decoded(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    result = {}
    for key, value in dict(row).items():
        if key.endswith("_json") and isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                pass
        result[key] = value
    return result


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if k.lower() in SENSITIVE_KEYS else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def emit(value: Any) -> None:
    print(json.dumps(redact(value), ensure_ascii=False, indent=2, default=str))


def write_json(path_value: str, value: Any) -> Path:
    path = Path(path_value).expanduser().absolute()
    if not path.parent.is_dir():
        raise AdminError("导出目录不存在，请先创建目录。")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise AdminError(f"拒绝覆盖已有文件：{path}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(redact(value), handle, ensure_ascii=False, indent=2, default=str)
            handle.write("\n")
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def timestamp(raw: str) -> str:
    try:
        date = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期应为 YYYY-MM-DD 或 ISO 8601 时间。") from exc
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return date.astimezone(timezone.utc).isoformat()


def positive(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 10000:
        raise argparse.ArgumentTypeError("数量范围是 1～10000。")
    return number


def tenant(args: argparse.Namespace, config: dict[str, str]) -> str:
    value = getattr(args, "tenant", None) or config.get("EVOAGENT_DEFAULT_TENANT_ID", "default") or "default"
    if not value.strip():
        raise AdminError("tenant 不能为空。")
    return value


def task_conditions(args: argparse.Namespace, config: dict[str, str], mutation: bool) -> tuple[str, list[Any]]:
    conditions, parameters = [], []
    if not getattr(args, "all_tenants", False):
        conditions.append("tenant_id=?")
        parameters.append(tenant(args, config))
    if getattr(args, "id", None):
        ids = list(dict.fromkeys(args.id))
        if len(ids) > 500:
            raise AdminError("单次最多显式指定 500 个任务 ID；更多任务请使用仓库/时间条件。")
        conditions.append("id IN (" + ",".join("?" for _ in ids) + ")")
        parameters.extend(ids)
    if getattr(args, "repository", None):
        conditions.append("repository=?")
        parameters.append(args.repository)
    if getattr(args, "before", None):
        conditions.append("julianday(created_at)<julianday(?)")
        parameters.append(args.before)
    if getattr(args, "state", None):
        conditions.append("state=?")
        parameters.append(args.state)
    elif mutation and not getattr(args, "include_unfinished", False) and not getattr(args, "id", None):
        conditions.append("state IN ('SUCCESS','FAILED','CANCELLED')")
    return " AND ".join(conditions) or "1", parameters


def memory_conditions(args: argparse.Namespace, config: dict[str, str]) -> tuple[str, list[Any]]:
    clauses, values = [], []
    if not getattr(args, "all_tenants", False):
        clauses.append("tenant_id=?")
        values.append(tenant(args, config))
    for arg, column in (("repository", "repository"), ("task_id", "task_id"), ("scope", "scope"), ("id", "id")):
        value = getattr(args, arg, None)
        if value:
            clauses.append(quote(column) + "=?")
            values.append(value)
    if getattr(args, "expired", False):
        clauses.append("expires_at IS NOT NULL AND julianday(expires_at)<=julianday(?)")
        values.append(utc_now())
    if getattr(args, "before", None):
        clauses.append("julianday(created_at)<julianday(?)")
        values.append(args.before)
    return " AND ".join(clauses) or "1", values


def install_selection(conn: sqlite3.Connection, ids: Sequence[str]) -> None:
    conn.execute("DROP TABLE IF EXISTS temp.maintenance_selection")
    conn.execute("CREATE TEMP TABLE maintenance_selection(id TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO maintenance_selection(id) VALUES(?)", [(x,) for x in ids])


def count_selected(conn: sqlite3.Connection, table: str, column: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM " + quote(table) + " WHERE " + quote(column) +
                        " IN (SELECT id FROM temp.maintenance_selection)").fetchone()[0]


def prepare_tasks(conn: sqlite3.Connection, args: argparse.Namespace, config: dict[str, str]) -> dict[str, Any]:
    if not (args.id or args.repository or args.before or args.all_terminal):
        raise AdminError("必须指定 --id、--repository、--before 或 --all-terminal；不会默认清空任务。")
    where, params = task_conditions(args, config, True)
    records = [dict(row) for row in conn.execute(
        "SELECT id,tenant_id,repository,state,created_at FROM tasks WHERE " + where + " ORDER BY created_at,id", params
    )]
    unfinished = [r for r in records if r["state"] not in TERMINAL]
    if unfinished and not args.include_unfinished:
        raise AdminError("所选任务尚未结束，已拒绝删除。真正停止服务及所有 Worker 后，才可用 --include-unfinished 清理遗留任务。")
    if args.all_terminal and args.include_unfinished:
        raise AdminError("--all-terminal 与 --include-unfinished 不能一起使用。")
    ids = [r["id"] for r in records]
    install_selection(conn, ids)
    schema = tables(conn)
    unknown = []
    for name, columns in schema.items():
        if "task_id" in columns and name not in (*TASK_CHILDREN, "webhook_deliveries"):
            if count_selected(conn, name, "task_id"):
                unknown.append(name)
        for fk in conn.execute("PRAGMA foreign_key_list(" + quote(name) + ")"):
            if fk[2] == "tasks" and name not in TASK_CHILDREN and name != "webhook_deliveries":
                if count_selected(conn, name, fk[3]):
                    unknown.append(name)
    if unknown:
        raise AdminError("发现未支持的任务关联表 " + ", ".join(sorted(set(unknown))) + "；为避免孤儿数据，拒绝清理。")
    deleting = {name: count_selected(conn, name, "task_id") for name in TASK_CHILDREN if name in schema}
    deleting["tasks"] = len(ids)
    tombstones = count_selected(conn, "webhook_deliveries", "task_id") if "webhook_deliveries" in schema else 0
    return {
        "operation": "delete-tasks", "tenant": tenant(args, config), "task_count": len(ids),
        "tasks_preview": records[:30], "preview_truncated": len(records) > 30,
        "delete_counts": deleting, "webhook_task_references_to_null": tombstones,
        "selection_sha256": hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest(),
        "preserved": ["users", "memberships", "repository_grants", "audit_log", "webhook delivery IDs",
                      "evaluation_cases", "skill/evolution versions and snapshots", "deployments", "alerts"],
        "note": "只删除按 task_id 直接关联的记录；既有评测/演进快照、备份、日志和 GitHub 评论不会被擦除。",
    }


def prepare_memories(conn: sqlite3.Connection, args: argparse.Namespace, config: dict[str, str]) -> dict[str, Any]:
    if not (args.id or args.repository or args.task_id or args.scope or args.expired or args.before or args.all_memories):
        raise AdminError("必须明确选择记忆范围，或使用 --all-memories；不会默认清空记忆。")
    where, params = memory_conditions(args, config)
    rows = [dict(row) for row in conn.execute(
        "SELECT id,tenant_id,repository,task_id,scope,kind,expires_at FROM agent_memories WHERE " + where + " ORDER BY id", params
    )]
    install_selection(conn, [r["id"] for r in rows])
    return {
        "operation": "clear-memories", "tenant": tenant(args, config), "memory_count": len(rows),
        "memories_preview": rows[:30], "preview_truncated": len(rows) > 30,
        "selection_sha256": hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
        "note": "只删除 agent_memories；任务报告/检查点中已复制的上下文、反馈、演进快照和备份保持不变。",
    }


def make_backup(path: Path, directory: str | None = None) -> Path:
    folder = Path(directory).expanduser().resolve() if directory else path.parent / "db_backups"
    if folder.exists() and not folder.is_dir():
        raise AdminError("备份目录不是文件夹。")
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    target = folder / f"{path.stem}.{stamp}.sqlite3"
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    source = destination = None
    try:
        source = connect(path)
        destination = sqlite3.connect(target)
        source.backup(destination, pages=512, sleep=0.05)
        if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise AdminError("备份 quick_check 失败，原数据库未做清理。")
    except BaseException:
        # Close before removal for Windows; interrupted backups are never
        # presented as complete files. The source database is not changed.
        if destination is not None:
            destination.close()
            destination = None
        if source is not None:
            source.close()
            source = None
        target.unlink(missing_ok=True)
        raise
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()
    return target


def confirm_mutation(args: argparse.Namespace) -> None:
    if not args.service_stopped:
        raise AdminError("执行清理前必须停止 EvoAgent 和所有关联 Worker，并添加 --service-stopped。该标志是操作人确认，不是自动进程检测。")
    if not args.yes:
        if not sys.stdin.isatty():
            raise AdminError("非交互终端不能询问确认；核对预览后显式添加 --yes。")
        reply = input("将先完整备份，再执行上述清理。输入 DELETE 确认： ")
        if reply != "DELETE":
            raise AdminError("操作已取消，未修改数据库。")


def mutate(path: Path, args: argparse.Namespace, config: dict[str, str], plan: dict[str, Any]) -> dict[str, Any]:
    prepare = prepare_tasks if args.command == "delete-tasks" else prepare_memories
    size = plan.get("task_count", plan.get("memory_count", 0))
    if not size:
        return {"applied": False, "reason": "没有匹配记录，不创建空备份。"}
    confirm_mutation(args)
    conn = connect(path, writable=True)
    backup = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        validate_schema(conn)
        existing_fk = conn.execute("PRAGMA foreign_key_check").fetchmany(10)
        if existing_fk:
            raise AdminError("数据库已有外键异常，已停止清理。先运行 check 并处理异常。")
        current = prepare(conn, args, config)
        if current["selection_sha256"] != plan["selection_sha256"]:
            raise AdminError("预览后匹配数据发生变化，已停止；请确认所有服务均已停止并重新预览。")
        # Reserved writer lock prevents another writer while a second read connection
        # creates a consistent backup of committed data. No SQL deletes have run yet.
        backup = make_backup(path, args.backup_dir)
        if args.command == "delete-tasks":
            schema = tables(conn)
            for name in TASK_CHILDREN:
                if name in schema:
                    conn.execute("DELETE FROM " + quote(name) + " WHERE task_id IN (SELECT id FROM temp.maintenance_selection)")
            if "webhook_deliveries" in schema:
                conn.execute("UPDATE webhook_deliveries SET task_id=NULL WHERE task_id IN (SELECT id FROM temp.maintenance_selection)")
            conn.execute("DELETE FROM tasks WHERE id IN (SELECT id FROM temp.maintenance_selection)")
        else:
            conn.execute("DELETE FROM agent_memories WHERE id IN (SELECT id FROM temp.maintenance_selection)")
        if conn.execute("PRAGMA foreign_key_check").fetchone():
            raise AdminError("清理后外键校验失败，事务已回滚。")
        conn.commit()
        return {"applied": True, "backup": str(backup), "records_selected": size,
                "note": "备份包含删除前全部数据与密码哈希，请严格保管；不会自动上传或覆盖源文件。"}
    except Exception as exc:
        conn.rollback()
        if backup:
            print(f"清理失败，事务已回滚；已有备份：{backup}", file=sys.stderr)
        raise exc
    finally:
        conn.close()


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="EvoAgent SQLite 管理工具：直接运行进入数字菜单；也保留原有子命令。所有清理先预览并备份。")
    p.add_argument("--version", action="version", version=VERSION)
    p.add_argument("--project-root", help="查找 .env 的项目根目录；不会改变相对 DB 路径的工作目录语义")
    p.add_argument("--db", help="显式指定现有 SQLite 文件；推荐绝对路径")
    sub = p.add_subparsers(dest="command", required=False)
    sub.add_parser("menu", help="进入交互式数字菜单（不写子命令时默认进入）")
    sub.add_parser("info", help="数据库位置、表行数、记忆分布及外部存储配置状态")
    sub.add_parser("check", help="只读 integrity_check 和外键检查")
    b = sub.add_parser("backup", help="SQLite 在线一致性备份；不会修改业务表")
    b.add_argument("--backup-dir")
    table = sub.add_parser("table", help="分页查看任意现有业务表，常见密码字段自动遮蔽")
    table.add_argument("name")
    table.add_argument("--limit", type=positive, default=20)
    table.add_argument("--offset", type=int, default=0)
    detail = sub.add_parser("task", help="查看单任务及直接关联记录，可导出 JSON")
    detail.add_argument("task_id")
    detail.add_argument("--tenant")
    detail.add_argument("--include-diff", action="store_true")
    detail.add_argument("--output", help="写入一个尚不存在的 JSON 文件，权限 0600")
    for name in ("tasks", "delete-tasks"):
        item = sub.add_parser(name, help="列出任务" if name == "tasks" else "清理所选任务；默认 dry-run")
        item.add_argument("--tenant")
        item.add_argument("--repository", help="精确仓库标识 owner/repo")
        item.add_argument("--id", action="append", help="完整任务 ID，可重复")
        item.add_argument("--before", type=timestamp, help="创建时间早于该值；无时区按 UTC")
        item.add_argument("--state", choices=STATES)
        if name == "tasks":
            item.add_argument("--all-tenants", action="store_true")
            item.add_argument("--limit", type=positive, default=30)
        else:
            item.add_argument("--all-terminal", action="store_true", help="明确选择当前租户所有已结束任务")
            item.add_argument("--include-unfinished", action="store_true", help="仅供停机后清理遗留未完成任务，谨慎使用")
            mutation_flags(item)
    for name in ("memories", "clear-memories"):
        item = sub.add_parser(name, help="查看 Agent 记忆" if name == "memories" else "清理所选 Agent 记忆；默认 dry-run")
        item.add_argument("--tenant")
        item.add_argument("--repository")
        item.add_argument("--task-id")
        item.add_argument("--id", help="完整记忆 ID")
        item.add_argument("--scope", choices=MEMORY_SCOPES)
        item.add_argument("--expired", action="store_true")
        item.add_argument("--before", type=timestamp)
        if name == "memories":
            item.add_argument("--all-tenants", action="store_true")
            item.add_argument("--limit", type=positive, default=30)
            item.add_argument("--full", action="store_true", help="显示完整内容；默认内容预览 160 字符")
        else:
            item.add_argument("--all-memories", action="store_true", help="明确选择当前租户所有记忆")
            mutation_flags(item)
    return p


def mutation_flags(item: argparse.ArgumentParser) -> None:
    item.add_argument("--apply", action="store_true", help="真正执行；省略时只预览")
    item.add_argument("--service-stopped", action="store_true", help="确认已停止服务及所有 Worker，不是自动检测")
    item.add_argument("--yes", action="store_true", help="已经人工核对预览，不再询问 DELETE")
    item.add_argument("--backup-dir", help="默认数据库旁 db_backups；不能关闭备份")


def run(args: argparse.Namespace) -> None:
    path, root, config = resolve_database(args)
    print(f"SQLite: {path}", file=sys.stderr)
    if args.db and config.get("EVOAGENT_DATABASE_URL", "").strip():
        print("注意：当前配置不是 SQLite；本次只操作 --db 显式指定的独立文件。", file=sys.stderr)
    conn = connect(path)
    try:
        schema = validate_schema(conn)
        if args.command == "info":
            emit({"database": str(path), "file_bytes": path.stat().st_size,
                  "working_directory": str(Path.cwd()), "config_root": str(root),
                  "relative_path_warning": "相对路径按启动目录解析；与服务启动目录不一致时请用 --db 绝对路径。",
                  "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
                  "tables": {name: conn.execute("SELECT COUNT(*) FROM " + quote(name)).fetchone()[0] for name in schema},
                  "memory_scopes": [dict(r) for r in conn.execute("SELECT tenant_id,scope,COUNT(*) AS count FROM agent_memories GROUP BY tenant_id,scope")],
                  "external": {"postgres_configured": bool(config.get("EVOAGENT_DATABASE_URL")),
                               "redis_configured": bool(config.get("EVOAGENT_REDIS_URL")),
                               "skills_dir_setting": config.get("EVOAGENT_SKILLS_DIR", "skills")},
                  "note": "这是主机管理员工具。表查看可跨租户；清理默认限定 default 或配置的租户。源码、.env、Skills 文件和备份不在此 DB 内。"})
        elif args.command == "check":
            results = [r[0] for r in conn.execute("PRAGMA integrity_check")]
            fk = [list(r) for r in conn.execute("PRAGMA foreign_key_check")]
            emit({"integrity_check": results, "foreign_key_violations": fk, "ok": results == ["ok"] and not fk})
            if results != ["ok"] or fk:
                raise AdminError("数据库检查未通过。")
        elif args.command == "backup":
            emit({"backup": str(make_backup(path, args.backup_dir)), "contains_sensitive_data": True})
        elif args.command == "table":
            if args.name not in schema:
                raise AdminError("未知业务表；请先运行 info。")
            if args.offset < 0:
                raise AdminError("offset 不能为负数。")
            total = conn.execute("SELECT COUNT(*) FROM " + quote(args.name)).fetchone()[0]
            keys = [r[1] for r in conn.execute("PRAGMA table_info(" + quote(args.name) + ")") if r[5]]
            order = ",".join(quote(k) for k in keys) or "rowid"
            rows = conn.execute("SELECT * FROM " + quote(args.name) + " ORDER BY " + order + " LIMIT ? OFFSET ?", (args.limit, args.offset))
            emit({"table": args.name, "total": total, "offset": args.offset, "rows": [decoded(r) for r in rows]})
        elif args.command == "tasks":
            where, values = task_conditions(args, config, False)
            total = conn.execute("SELECT COUNT(*) FROM tasks WHERE " + where, values).fetchone()[0]
            rows = conn.execute("SELECT id,tenant_id,repository,pull_request,state,created_at,updated_at,error FROM tasks WHERE " + where + " ORDER BY created_at DESC,id LIMIT ?", [*values, args.limit])
            emit({"total_matching": total, "limit": args.limit, "tasks": [dict(r) for r in rows]})
        elif args.command == "task":
            row = conn.execute("SELECT * FROM tasks WHERE id=? AND tenant_id=?", (args.task_id, tenant(args, config))).fetchone()
            if not row:
                raise AdminError("该租户内未找到任务。")
            output = {"task": decoded(row), "related": {}}
            for name in (*TASK_CHILDREN, "webhook_deliveries"):
                if name not in schema or (name == "task_payloads" and not args.include_diff):
                    continue
                related = conn.execute("SELECT * FROM " + quote(name) + " WHERE task_id=?", (args.task_id,))
                output["related"][name] = [decoded(r) for r in related]
            if args.output:
                emit({"export": str(write_json(args.output, output)), "contains_source_code": True})
            else:
                emit(output)
        elif args.command == "memories":
            where, values = memory_conditions(args, config)
            total = conn.execute("SELECT COUNT(*) FROM agent_memories WHERE " + where, values).fetchone()[0]
            rows = [decoded(r) for r in conn.execute("SELECT * FROM agent_memories WHERE " + where + " ORDER BY created_at DESC,id LIMIT ?", [*values, args.limit])]
            if not args.full:
                for row in rows:
                    row["content"] = str(row["content"])[:160]
                    row.pop("metadata_json", None)
                    row.pop("keywords_json", None)
            emit({"total_matching": total, "full_content": args.full, "memories": rows})
        else:
            prepare = prepare_tasks if args.command == "delete-tasks" else prepare_memories
            plan = prepare(conn, args, config)
            emit({"dry_run": not args.apply, **plan})
            conn.close()
            if args.apply:
                emit(mutate(path, args, config, plan))
            else:
                print("仅预览，未修改业务数据。执行需 --apply --service-stopped，并确认 DELETE。", file=sys.stderr)
    finally:
        conn.close()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = make_parser().parse_args(argv)
        if args.command in (None, "menu"):
            return interactive_main(args)
        run(args)
        return 0
    except (AdminError, OSError, sqlite3.Error, ValueError) as exc:
        message = str(exc)
        # Never echo a configured database/Redis URL containing credentials.
        for key in ("EVOAGENT_DATABASE_URL", "EVOAGENT_REDIS_URL"):
            secret = os.environ.get(key)
            if secret:
                message = message.replace(secret, "[REDACTED_URL]")
        print("错误：" + message, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130




# ------------------- Numbered console (no extra dependencies) -------------------
# These handlers reuse the same read-only connections and transactional deletion
# functions as the CLI above. The console never imports EvoAgent or creates users.

class MenuExit(Exception):
    """EOF or a deliberate exit; never treated as confirmation."""


def safe_terminal(value: Any) -> str:
    """Do not execute escape/control sequences embedded in untrusted stored text."""
    text = str(value)
    return "".join(
        char if char in "\n\t" or (ord(char) >= 32 and ord(char) != 127
                                   and not 0x80 <= ord(char) <= 0x9F
                                   and char not in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
        else "\\u%04x" % ord(char) for char in text
    )


class Console:
    """Simple, testable input/output; works over SSH without curses or a GUI."""
    def __init__(self, input_fn=None, output_fn=None):
        self.input = input_fn or input
        self.output = output_fn or print

    def say(self, value: Any = "") -> None:
        self.output(safe_terminal(value))

    def ask(self, prompt: str) -> str:
        try:
            return self.input(safe_terminal(prompt)).strip()
        except EOFError as exc:
            raise MenuExit from exc

    def choose(self, title: str, options: Sequence[tuple[str, str]], default: str = "0") -> str:
        self.say("\n" + "=" * 64)
        self.say(title)
        for key, label in options:
            self.say(f"  {key}. {label}")
        valid = {key for key, _ in options}
        while True:
            answer = self.ask("请输入编号（回车返回/取消）： ") or default
            if answer in valid:
                return answer
            self.say("无效编号，请选择屏幕上显示的选项。")

    def pause(self) -> None:
        self.ask("\n按回车返回……")

    def view(self, value: Any, title: str = "详细内容", page_lines: int = 28) -> None:
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(redact(value), ensure_ascii=False, indent=2, default=str)
        lines = safe_terminal(text).splitlines() or ["（空）"]
        offset = 0
        while True:
            self.say(f"\n--- {title} | 第 {offset + 1}～{min(offset + page_lines, len(lines))} 行 / 共 {len(lines)} 行 ---")
            for line in lines[offset:offset + page_lines]:
                self.say(line)
            options = [("0", "返回")]
            if offset + page_lines < len(lines):
                options.insert(0, ("1", "下一页"))
            if offset:
                options.insert(0, ("2", "上一页"))
            action = self.choose("内容导航", options)
            if action == "0":
                return
            offset = offset + page_lines if action == "1" else max(0, offset - page_lines)


def _assert_sqlite_file(path: Path) -> Path:
    path = path.expanduser().resolve(strict=True)
    if not path.is_file():
        raise AdminError("目标不是普通文件。")
    with path.open("rb") as handle:
        if handle.read(16) != b"SQLite format 3\x00":
            raise AdminError("目标不是 SQLite 数据库。未创建、覆盖或修改任何文件。")
    conn = connect(path)
    try:
        validate_schema(conn)
    finally:
        conn.close()
    return path


def interactive_database(args: argparse.Namespace, ui: Console) -> tuple[Path, Path, dict[str, str]]:
    root = project_root(args.project_root)
    config = config_values(root)
    if config.get("EVOAGENT_DATABASE_URL", "").strip() and not args.db:
        raise AdminError("项目配置了外部数据库；本工具只支持 SQLite，不会自动改管 evoagent.db。"
                         "查看独立 SQLite 备份时可用 --db 指定该文件。")
    if args.db:
        return _assert_sqlite_file(Path(args.db)), root, config
    raw = config.get("EVOAGENT_DB_PATH", "evoagent.db")
    if not raw or raw == ":memory:" or raw.startswith("file:"):
        raise AdminError("配置不是一个可管理的 SQLite 文件路径。")
    desired = Path(raw).expanduser()
    # When launched outside the service directory, present ambiguous candidates
    # instead of silently deleting a same-named, different database.
    candidates = [desired] if desired.is_absolute() else [Path.cwd() / desired, root / desired]
    candidates = list(dict.fromkeys(path.resolve() for path in candidates if path.is_file()))
    if len(candidates) == 1:
        return _assert_sqlite_file(candidates[0]), root, config
    if candidates:
        choice = ui.choose("发现多个同名数据库。请选择与 EvoAgent 服务实际使用的一致的文件：",
                           [(str(i), str(path)) for i, path in enumerate(candidates, 1)] + [("0", "退出，不操作")])
        if choice == "0":
            raise MenuExit
        return _assert_sqlite_file(candidates[int(choice) - 1]), root, config
    ui.say(f"项目目录：{root}\n未找到配置对应的数据库。不会创建一个空数据库代替它。")
    while True:
        raw_path = ui.ask("粘贴现有数据库的完整路径（回车退出）： ")
        if not raw_path:
            raise MenuExit
        try:
            return _assert_sqlite_file(Path(raw_path.strip('"\''))), root, config
        except (AdminError, OSError, sqlite3.Error) as exc:
            ui.say(f"无法使用这个文件：{exc}")


class AdminMenu:
    PAGE = 10

    def __init__(self, path: Path, root: Path, config: dict[str, str], ui: Console | None = None):
        self.path, self.root, self.config = path, root, dict(config)
        self.ui = ui or Console()
        self.tenant = config.get("EVOAGENT_DEFAULT_TENANT_ID", "default") or "default"

    def args(self, command: str, *flags: str) -> argparse.Namespace:
        # Values come from stored rows, not shell commands. Bind option values
        # explicitly so a repository label beginning with '-' remains data.
        bound, index = [], 0
        valued = {"--id", "--repository", "--before", "--task-id", "--scope", "--state"}
        while index < len(flags):
            item = flags[index]
            if item in valued and index + 1 < len(flags):
                bound.append(item + "=" + str(flags[index + 1]))
                index += 2
            else:
                bound.append(item)
                index += 1
        args = make_parser().parse_args(["--db=" + str(self.path), "--project-root=" + str(self.root), command, *bound])
        if hasattr(args, "tenant"):
            args.tenant = self.tenant
        return args

    def rows(self, sql: str, values: Sequence[Any] = ()) -> list[dict[str, Any]]:
        conn = connect(self.path)
        try:
            return [dict(row) for row in conn.execute(sql, values)]
        finally:
            conn.close()

    def main(self) -> None:
        self.ui.say("EvoAgent 数据库管理台 v" + VERSION)
        self.ui.say("这是服务器终端内的交互客户端。除启动命令外，按数字选择即可。")
        self.ui.say("查看可在线进行；删除必须先停服务及全部 Worker。常见敏感字段会遮蔽，代码中的秘密不保证自动识别。")
        while True:
            action = self.ui.choose(
                f"数据库：{self.path}\n当前租户：{self.tenant}",
                [("1", "查看数据（概况 / 任务 / 记忆 / 数据表）"),
                 ("2", "删除或清理（先预览，再确认和备份）"),
                 ("3", "备份数据库 / 查看已有备份"),
                 ("4", "检查数据库完整性"),
                 ("5", "切换数据库或租户"), ("0", "退出")])
            if action == "0":
                return
            try:
                {"1": self.view_menu, "2": self.delete_menu, "3": self.backup_menu,
                 "4": self.check, "5": self.settings_menu}[action]()
            except (AdminError, OSError, sqlite3.Error, ValueError) as exc:
                self.ui.say("操作未完成：" + self.error_message(exc))
                self.ui.pause()
            except KeyboardInterrupt:
                self.ui.say("\n已中断当前操作，返回主菜单；未提交的事务会回滚。")

    def error_message(self, exc: Exception) -> str:
        text = str(exc)
        for key in ("EVOAGENT_DATABASE_URL", "EVOAGENT_REDIS_URL"):
            if self.config.get(key):
                text = text.replace(self.config[key], "[REDACTED_URL]")
        return text

    def view_menu(self) -> None:
        while True:
            action = self.ui.choose("查看数据", [("1", "数据库概况与表数量"), ("2", "审查任务（选编号看详情 / 导出）"),
                                     ("3", "Agent 记忆"), ("4", "其它数据表（只读）"), ("0", "返回主菜单")])
            if action == "0":
                return
            {"1": self.overview, "2": self.browse_tasks, "3": self.browse_memories,
             "4": self.browse_tables}[action]()

    def overview(self) -> None:
        conn = connect(self.path)
        try:
            schema = validate_schema(conn)
            result = {"数据库": str(self.path), "文件大小_字节": self.path.stat().st_size,
                      "当前任务和记忆筛选租户": self.tenant,
                      "各表记录数_全库": {t: conn.execute("SELECT COUNT(*) FROM " + quote(t)).fetchone()[0] for t in schema},
                      "当前租户任务状态": [dict(row) for row in conn.execute(
                          "SELECT state,COUNT(*) AS count FROM tasks WHERE tenant_id=? GROUP BY state", (self.tenant,))],
                      "当前租户记忆分布": [dict(row) for row in conn.execute(
                          "SELECT scope,COUNT(*) AS count FROM agent_memories WHERE tenant_id=? GROUP BY scope", (self.tenant,))],
                      "说明": "代码、.env、磁盘 SKILL.md、Redis 消息、备份和外部日志不在这个 DB 中。"}
        finally:
            conn.close()
        self.ui.view(result, "数据库概况")

    def pick_repository(self, table: str) -> str | None:
        repos = self.rows("SELECT DISTINCT repository FROM " + quote(table) + " WHERE tenant_id=? ORDER BY repository", (self.tenant,))
        if not repos:
            self.ui.say("当前租户没有仓库记录。")
            return None
        return self.pick_value("选择仓库（当前租户）", [r["repository"] for r in repos])

    def pick_value(self, title: str, values: Sequence[str]) -> str | None:
        offset = 0
        while True:
            page = values[offset:offset + self.PAGE]
            opts = [(str(i), value or "（空字符串）") for i, value in enumerate(page, 1)]
            if offset + self.PAGE < len(values):
                opts.append(("91", "下一页"))
            if offset:
                opts.append(("92", "上一页"))
            opts.append(("0", "返回 / 取消"))
            action = self.ui.choose(title, opts)
            if action == "0":
                return None
            if action == "91":
                offset += self.PAGE
            elif action == "92":
                offset -= self.PAGE
            else:
                return page[int(action) - 1]

    def browse_tasks(self, selecting: bool = False, unfinished: bool = False) -> str | None:
        offset, repository, state = 0, None, None
        while True:
            clauses, params = ["tenant_id=?"], [self.tenant]
            if selecting:
                clauses.append("state NOT IN ('SUCCESS','FAILED','CANCELLED')" if unfinished else
                               "state IN ('SUCCESS','FAILED','CANCELLED')")
            if repository is not None:
                clauses.append("repository=?"); params.append(repository)
            if state:
                clauses.append("state=?"); params.append(state)
            where = " AND ".join(clauses)
            total = self.rows("SELECT COUNT(*) AS n FROM tasks WHERE " + where, params)[0]["n"]
            page = self.rows("SELECT id,repository,pull_request,state,created_at FROM tasks WHERE " + where +
                             " ORDER BY created_at DESC,id LIMIT ? OFFSET ?", [*params, self.PAGE, offset])
            self.ui.say(f"\n任务列表 | 租户 {self.tenant} | 匹配 {total} 条 | 第 {offset // self.PAGE + 1} 页")
            opts = []
            for i, row in enumerate(page, 1):
                pr = "手动 Diff" if row["pull_request"] is None else f"PR #{row['pull_request']}"
                label = f"[{row['state']}] {row['repository']} · {pr} · {row['id'][:12]} · {row['created_at'][:19]}"
                opts.append((str(i), label))
            if offset + self.PAGE < total:
                opts.append(("91", "下一页"))
            if offset:
                opts.append(("92", "上一页"))
            opts.extend([("93", "按仓库筛选"), ("94", "按状态筛选"), ("95", "清除筛选 / 刷新"), ("0", "返回")])
            action = self.ui.choose("选择任务编号" + ("，接着预览删除" if selecting else "，查看详情"), opts)
            if action == "0":
                return None
            if action == "91":
                offset += self.PAGE
            elif action == "92":
                offset = max(0, offset - self.PAGE)
            elif action == "93":
                picked = self.pick_repository("tasks")
                if picked is not None:
                    repository, offset = picked, 0
            elif action == "94":
                states = list(STATES) if not selecting else ([s for s in STATES if s not in TERMINAL] if unfinished else list(TERMINAL))
                state, offset = self.pick_value("按任务状态筛选", states), 0
            elif action == "95":
                repository, state, offset = None, None, 0
            else:
                task_id = page[int(action) - 1]["id"]
                if selecting:
                    return task_id
                self.task_detail(task_id)

    def task_data(self, task_id: str, include_diff: bool = True) -> dict[str, Any]:
        rows = self.rows("SELECT * FROM tasks WHERE id=? AND tenant_id=?", (task_id, self.tenant))
        if not rows:
            raise AdminError("任务已不存在，或不属于当前租户。")
        result: dict[str, Any] = {"task": decoded(rows[0]), "related": {}}
        conn = connect(self.path)
        try:
            schema = tables(conn)
            for name in (*TASK_CHILDREN, "webhook_deliveries"):
                if name in schema and (name != "task_payloads" or include_diff):
                    result["related"][name] = [decoded(r) for r in conn.execute(
                        "SELECT * FROM " + quote(name) + " WHERE task_id=?", (task_id,))]
        finally:
            conn.close()
        return result

    def task_detail(self, task_id: str) -> None:
        while True:
            data = self.task_data(task_id)
            task = data["task"]
            report = task.get("report_json") or {}
            action = self.ui.choose(f"任务 {task_id}\n仓库：{task['repository']}  状态：{task['state']}",
                                   [("1", "审查结果与执行状态摘要"), ("2", "完整任务及关联记录（分页）"),
                                    ("3", "原始 Diff"), ("4", "轨迹 / 检查点 / Agent 消息"),
                                    ("5", "导出这条任务为 JSON"), ("0", "返回列表")])
            if action == "0":
                return
            if action == "1":
                execution = (report.get("execution") or {}) if isinstance(report, dict) else {}
                self.ui.view({"id": task_id, "state": task["state"], "error": task.get("error"),
                              "summary": report.get("summary") if isinstance(report, dict) else report,
                              "risk": report.get("risk") if isinstance(report, dict) else None,
                              "execution": {k: execution.get(k) for k in ("llm_calls", "failed_model_calls", "failed_tool_calls", "duration_ms")},
                              "findings": report.get("findings", []) if isinstance(report, dict) else [],
                              "提醒": "SUCCESS 是后端记录；必须结合失败调用、Worker 状态看是否完整执行，工具不会修改后端结论。"}, "任务摘要")
            elif action == "2":
                self.ui.view(data, "完整任务记录")
            elif action == "3":
                payloads = data["related"].get("task_payloads", [])
                self.ui.view(payloads[0]["diff"] if payloads else "没有保存 Diff（可能还未下载，或已清理）。", "原始 Diff")
            elif action == "4":
                self.ui.view({k: data["related"].get(k, []) for k in ("trace_events", "checkpoints", "agent_messages")}, "执行记录")
            else:
                self.export(data, "task-" + task_id)

    def export(self, value: Any, stem: str) -> None:
        folder = self.path.parent / "db_exports"
        folder.mkdir(mode=0o700, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        safe_stem = re.sub(r"[^A-Za-z0-9_-]", "_", stem)[:100]
        output = write_json(str(folder / f"{safe_stem}-{stamp}.json"), value)
        self.ui.say(f"已导出：{output}\n可能含源代码 / 记忆。请保管好，不会自动上传到网络。")
        self.ui.pause()

    def browse_memories(self, selecting: bool = False) -> str | None:
        offset, repository, scope = 0, None, None
        while True:
            clauses, params = ["tenant_id=?"], [self.tenant]
            for key, value in (("repository", repository), ("scope", scope)):
                if value is not None:
                    clauses.append(key + "=?"); params.append(value)
            where = " AND ".join(clauses)
            total = self.rows("SELECT COUNT(*) AS n FROM agent_memories WHERE " + where, params)[0]["n"]
            page = self.rows("SELECT * FROM agent_memories WHERE " + where + " ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
                             [*params, self.PAGE, offset])
            opts = []
            for i, row in enumerate(page, 1):
                preview = str(row.get("content", "")).replace("\n", " ")[:65]
                opts.append((str(i), f"[{row['scope']}] {row['repository']} · {row['id'][:10]} · {preview}"))
            if offset + self.PAGE < total:
                opts.append(("91", "下一页"))
            if offset:
                opts.append(("92", "上一页"))
            opts.extend([("93", "按仓库筛选"), ("94", "按记忆类型筛选"), ("95", "清除筛选 / 刷新"), ("0", "返回")])
            action = self.ui.choose(f"Agent 记忆 | 租户 {self.tenant} | {total} 条 | 第 {offset // self.PAGE + 1} 页", opts)
            if action == "0":
                return None
            if action == "91":
                offset += self.PAGE
            elif action == "92":
                offset = max(0, offset - self.PAGE)
            elif action == "93":
                picked = self.pick_repository("agent_memories")
                if picked is not None:
                    repository, offset = picked, 0
            elif action == "94":
                scope, offset = self.pick_value("记忆类型", list(MEMORY_SCOPES)), 0
            elif action == "95":
                repository, scope, offset = None, None, 0
            else:
                row = page[int(action) - 1]
                if selecting:
                    return row["id"]
                self.ui.view(decoded(row), "记忆详情")

    def browse_tables(self) -> None:
        conn = connect(self.path)
        try:
            names = list(tables(conn))
        finally:
            conn.close()
        self.ui.say("注意：这里是主机管理员的全库只读浏览，可见所有租户；不会提供任意表删除。")
        name = self.pick_value("选择数据表", names)
        if name is None:
            return
        offset = 0
        while True:
            conn = connect(self.path)
            try:
                keys = [r[1] for r in conn.execute("PRAGMA table_info(" + quote(name) + ")") if r[5]]
                order = ",".join(quote(k) for k in keys) or "rowid"
                total = conn.execute("SELECT COUNT(*) FROM " + quote(name)).fetchone()[0]
                rows = [decoded(r) for r in conn.execute("SELECT * FROM " + quote(name) + " ORDER BY " + order +
                                                       " LIMIT ? OFFSET ?", (self.PAGE, offset))]
            finally:
                conn.close()
            self.ui.view({"table": name, "total": total, "offset": offset, "rows": rows}, name)
            opts = [("0", "返回")]
            if offset + self.PAGE < total:
                opts.insert(0, ("1", "下一批记录"))
            if offset:
                opts.insert(0, ("2", "上一批记录"))
            action = self.ui.choose("数据表导航", opts)
            if action == "0":
                return
            offset = offset + self.PAGE if action == "1" else max(0, offset - self.PAGE)

    def ask_before(self) -> str | None:
        self.ui.say("只匹配创建时间更早的记录。输入 YYYY-MM-DD；无时区按 UTC。")
        while True:
            value = self.ui.ask("截止日期（回车取消）： ")
            if not value:
                return None
            try:
                return timestamp(value)
            except argparse.ArgumentTypeError as exc:
                self.ui.say(exc)

    def delete_menu(self) -> None:
        while True:
            action = self.ui.choose(f"清理范围只限当前租户：{self.tenant}",
                [("1", "删除一条已结束任务（按列表编号选）"), ("2", "清理某仓库已结束任务"),
                 ("3", "清理某日期之前的已结束任务"), ("4", "清理当前租户全部已结束任务"),
                 ("5", "清理过期 Agent 记忆"), ("6", "删除一条 Agent 记忆"),
                 ("7", "清理某仓库全部记忆"), ("8", "清理当前租户全部记忆"),
                 ("9", "停机后清理一条卡住的未结束任务"), ("0", "返回主菜单")])
            if action == "0":
                return
            args = None
            if action in {"1", "9"}:
                task_id = self.browse_tasks(selecting=True, unfinished=action == "9")
                if task_id:
                    flags = ["--id", task_id] + (["--include-unfinished"] if action == "9" else [])
                    args = self.args("delete-tasks", *flags)
            elif action == "2":
                repo = self.pick_repository("tasks")
                if repo is not None:
                    args = self.args("delete-tasks", "--repository", repo)
            elif action == "3":
                before = self.ask_before()
                if before:
                    args = self.args("delete-tasks", "--before", before)
            elif action == "4":
                args = self.args("delete-tasks", "--all-terminal")
            elif action == "5":
                args = self.args("clear-memories", "--expired")
            elif action == "6":
                memory_id = self.browse_memories(selecting=True)
                if memory_id:
                    args = self.args("clear-memories", "--id", memory_id)
            elif action == "7":
                repo = self.pick_repository("agent_memories")
                if repo is not None:
                    args = self.args("clear-memories", "--repository", repo)
            elif action == "8":
                args = self.args("clear-memories", "--all-memories")
            if args is not None:
                self.confirm_and_apply(args)

    def confirm_and_apply(self, args: argparse.Namespace) -> None:
        conn = connect(self.path)
        try:
            validate_schema(conn)
            plan = (prepare_tasks if args.command == "delete-tasks" else prepare_memories)(conn, args, self.config)
        finally:
            conn.close()
        count = plan.get("task_count", plan.get("memory_count", 0))
        self.ui.say(f"\n【删除预览：还没有修改数据】\n数据库：{self.path}\n租户：{self.tenant}\n匹配记录：{count}")
        for row in plan.get("tasks_preview", plan.get("memories_preview", [])):
            self.ui.say(f"  {row['id']} | {row.get('repository', '')} | {row.get('state', row.get('scope', ''))}")
        if count > 30:
            self.ui.say(f"仅展示前 30 条；实际将处理匹配的全部 {count} 条，不只是当前页。")
        if plan.get("delete_counts"):
            self.ui.say("关联记录：" + json.dumps(plan["delete_counts"], ensure_ascii=False))
        self.ui.say(plan["note"])
        self.ui.say("保留账号、权限和其它任务。清理不等于擦除备份、日志或 GitHub 评论。")
        if count == 0:
            self.ui.pause()
            return
        if self.config.get("EVOAGENT_REDIS_URL"):
            self.ui.say("警告：此项目配置了 Redis。本工具不清除 Redis 消息；须停止所有消费者。")
        stopped = self.ui.choose("你是否已经停止 EvoAgent 服务和所有关联 Worker？（无法自动检测）",
                                 [("1", "我已全部停止，继续"), ("0", "没有 / 不确定，取消删除")])
        if stopped != "1":
            self.ui.say("已取消，未修改数据库，也没有创建删除备份。")
            return
        confirmed = self.ui.choose(f"最后确认：将先完整备份，然后清理 {count} 条所选任务/记忆及列出的关联数据。",
                                   [("1", "确认执行"), ("0", "取消，保留数据")])
        if confirmed != "1":
            self.ui.say("已取消，未修改数据库。")
            return
        args.apply, args.service_stopped, args.yes = True, True, True
        result = mutate(self.path, args, self.config, plan)
        if result.get("applied"):
            self.ui.say(f"清理完成：{result['records_selected']} 条。\n删除前完整备份：{result['backup']}")
            self.ui.say("备份含删除前所有数据和账号密码哈希，请妥善保管。重启 EvoAgent 后刷新网页。")
        else:
            self.ui.say(result.get("reason", "没有执行清理。"))
        self.ui.pause()

    def backup_menu(self) -> None:
        action = self.ui.choose("备份管理（不会覆盖现有数据库，不提供一键恢复覆盖）",
                               [("1", "现在创建完整备份"), ("2", "查看默认目录的备份列表"), ("0", "返回")])
        if action == "1":
            directory = self.ui.ask("备份目录（回车使用数据库旁 db_backups）： ") or None
            backup = make_backup(self.path, directory)
            self.ui.say(f"备份成功：{backup}\n该文件含完整账号和业务数据，请妥善保管。")
            self.ui.pause()
        elif action == "2":
            directory = self.path.parent / "db_backups"
            paths = sorted(directory.glob("*.sqlite3"), reverse=True) if directory.is_dir() else []
            self.ui.view({"目录": str(directory), "备份": [{"path": str(p), "bytes": p.stat().st_size} for p in paths]}, "已有备份")

    def check(self) -> None:
        conn = connect(self.path)
        try:
            integrity = [r[0] for r in conn.execute("PRAGMA integrity_check")]
            foreign = [list(r) for r in conn.execute("PRAGMA foreign_key_check")]
        finally:
            conn.close()
        self.ui.view({"检查通过": integrity == ["ok"] and not foreign,
                      "integrity_check": integrity, "foreign_key_violations": foreign,
                      "说明": "只读检查数据库结构一致性；不验证审查结论或模型正确性，也不自动修复。"}, "完整性检查")

    def settings_menu(self) -> None:
        action = self.ui.choose("当前连接设置（不修改项目 .env）",
                               [("1", "切换当前租户"), ("2", "打开另一个现有 SQLite 文件"), ("0", "返回")])
        if action == "1":
            rows = self.rows("SELECT tenant_id FROM tasks UNION SELECT tenant_id FROM agent_memories UNION SELECT tenant_id FROM memberships ORDER BY tenant_id")
            values = sorted({self.tenant, *(r["tenant_id"] for r in rows)})
            selected = self.pick_value("选择租户（之后的任务/记忆浏览和清理都按此筛选）", values)
            if selected is not None:
                self.tenant = selected
        elif action == "2":
            path = self.ui.ask("粘贴现有 SQLite 文件完整路径（回车取消）： ")
            if path:
                checked = _assert_sqlite_file(Path(path.strip('"\'')))
                self.path = checked
                self.ui.say(f"当前连接已切换：{self.path}\n这不会改变 EvoAgent 服务使用的数据库。")


def interactive_main(args: argparse.Namespace, ui: Console | None = None) -> int:
    ui = ui or Console()
    try:
        path, root, config = interactive_database(args, ui)
        AdminMenu(path, root, config, ui).main()
    except MenuExit:
        ui.say("\n已退出。未确认的清理不会执行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
