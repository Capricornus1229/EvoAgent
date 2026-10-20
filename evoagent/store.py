import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .models import ReviewReport, TaskState, TraceEvent


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


from .skill_activation_store import AtomicSkillStore


class TaskStore(AtomicSkillStore):
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    pull_request INTEGER,
                    input_json TEXT NOT NULL,
                    report_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS failure_cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS skill_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    skill_name TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    prompt TEXT NOT NULL,
                    score REAL NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0,
                    parent_version INTEGER,
                    created_at TEXT NOT NULL,
                    UNIQUE(skill_name, version)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS installations (
                    installation_id INTEGER PRIMARY KEY,
                    account_login TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS trace_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    step INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS evaluation_cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    split TEXT NOT NULL,
                    diff TEXT NOT NULL,
                    expected_json TEXT NOT NULL,
                    source TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS evolution_runs (
                    id TEXT PRIMARY KEY,
                    skill_name TEXT NOT NULL,
                    candidate_version INTEGER NOT NULL,
                    baseline_version INTEGER,
                    decision TEXT NOT NULL,
                    candidate_score REAL NOT NULL,
                    baseline_score REAL NOT NULL,
                    metrics_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS skill_artifact_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    skill_name TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    artifact_json TEXT NOT NULL,
                    artifact_sha256 TEXT NOT NULL,
                    score REAL NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0,
                    parent_version INTEGER,
                    created_at TEXT NOT NULL,
                    UNIQUE(tenant_id, skill_name, version)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS skill_evolution_runs (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    skill_name TEXT NOT NULL,
                    candidate_version INTEGER NOT NULL,
                    baseline_version INTEGER,
                    decision TEXT NOT NULL,
                    candidate_score REAL NOT NULL,
                    baseline_score REAL NOT NULL,
                    metrics_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            self._ensure_column(
                conn, "skill_artifact_versions", "tenant_id", "TEXT NOT NULL DEFAULT 'default'"
            )
            self._ensure_column(
                conn, "skill_evolution_runs", "tenant_id", "TEXT NOT NULL DEFAULT 'default'"
            )
            self._ensure_column(conn, "tasks", "tenant_id", "TEXT NOT NULL DEFAULT 'default'")
            self._ensure_column(conn, "tasks", "cancel_requested", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "tasks", "publication_status", "TEXT NOT NULL DEFAULT 'not_requested'")
            self._ensure_column(conn, "tasks", "publication_error", "TEXT")
            self._ensure_column(conn, "installations", "tenant_id", "TEXT NOT NULL DEFAULT 'default'")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS checkpoints (
                    task_id TEXT NOT NULL,
                    node TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    state_json TEXT NOT NULL,
                    error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(task_id, node),
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS task_payloads (
                    task_id TEXT PRIMARY KEY,
                    diff TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS agent_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS webhook_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    task_id TEXT,
                    received_at TEXT NOT NULL
                )"""
            )
            self._ensure_column(conn, "webhook_deliveries", "dispatch_status", "TEXT NOT NULL DEFAULT 'pending'")
            self._ensure_column(conn, "webhook_deliveries", "dispatch_error", "TEXT")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS memberships (
                    user_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    PRIMARY KEY(user_id, tenant_id),
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS repository_grants (
                    tenant_id TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    auto_fix INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(tenant_id, repository)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    alert_key TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(tenant_id, alert_key, status)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS agent_memories (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT '',
                    agent TEXT NOT NULL DEFAULT '',
                    scope TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    keywords_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    importance REAL NOT NULL DEFAULT 0.5,
                    created_at TEXT NOT NULL,
                    expires_at TEXT
                )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_tenant_created ON tasks(tenant_id, created_at)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_memories_lookup "
                "ON agent_memories(tenant_id, repository, scope, created_at)"
            )

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}
        if column not in columns:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, declaration))

    def create(
        self, task_id: str, repository: str, pull_request: Optional[int],
        payload: Dict[str, Any], tenant_id: str = "default",
    ) -> None:
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO tasks(id,state,repository,pull_request,input_json,report_json,error,"
                "created_at,updated_at,tenant_id,cancel_requested) "
                "VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, 0)",
                (task_id, TaskState.PENDING.value, repository, pull_request,
                 json.dumps(payload), now, now, tenant_id),
            )

    def transition(self, task_id: str, event: TraceEvent) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET state=?,updated_at=? WHERE id=? AND cancel_requested=0 AND state NOT IN ('SUCCESS','CANCELLED')",
                (event.state.value, event.created_at, task_id),
            )
            if cursor.rowcount == 0:
                return False
            conn.execute(
                "INSERT INTO trace_events(task_id,step,state,message,created_at) VALUES (?,?,?,?,?)",
                (task_id, event.step, event.state.value, event.message, event.created_at),
            )
        return True

    def succeed(self, task_id: str, report: ReviewReport, event: TraceEvent) -> bool:
        """Commit completion only if cancellation has not won the same row lock."""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET state=?,report_json=?,error=NULL,updated_at=? "
                "WHERE id=? AND cancel_requested=0 AND state NOT IN ('CANCELLED','SUCCESS')",
                (TaskState.SUCCESS.value, json.dumps(report.to_dict(), ensure_ascii=False), event.created_at, task_id),
            )
            if cursor.rowcount == 0:
                return False
            conn.execute(
                "INSERT INTO trace_events(task_id,step,state,message,created_at) VALUES (?,?,?,?,?)",
                (task_id, event.step, event.state.value, event.message, event.created_at),
            )
        return True

    def fail(self, task_id: str, error: str, event: TraceEvent) -> None:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET state=?,error=?,updated_at=? WHERE id=? AND cancel_requested=0 AND state NOT IN ('SUCCESS','CANCELLED')",
                (TaskState.FAILED.value, error[:2000], event.created_at, task_id),
            )
            if cursor.rowcount:
                conn.execute(
                    "INSERT INTO trace_events(task_id,step,state,message,created_at) VALUES (?,?,?,?,?)",
                    (task_id, event.step, event.state.value, event.message, event.created_at),
                )

    def get(self, task_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            if tenant_id is None:
                row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE id = ? AND tenant_id = ?", (task_id, tenant_id)
                ).fetchone()
            if row is None:
                return None
            events = conn.execute(
                "SELECT step, state, message, created_at FROM trace_events WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
            messages = conn.execute(
                "SELECT sender,recipient,kind,correlation_id,content_json,created_at "
                "FROM agent_messages WHERE task_id=? ORDER BY id", (task_id,)
            ).fetchall()
        value = dict(row)
        value["input"] = json.loads(value.pop("input_json"))
        report_json = value.pop("report_json")
        value["report"] = json.loads(report_json) if report_json else None
        value["trace"] = [dict(item) for item in events]
        value["collaboration"] = []
        for message in messages:
            item = dict(message)
            item["content"] = json.loads(item.pop("content_json"))
            value["collaboration"].append(item)
        return value

    def save_agent_memory(self, memory: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO agent_memories(id,tenant_id,repository,task_id,agent,scope,kind,"
                "content,keywords_json,metadata_json,importance,created_at,expires_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "importance=MAX(agent_memories.importance,excluded.importance),"
                "expires_at=excluded.expires_at",
                (
                    memory["id"], memory["tenant_id"], memory["repository"],
                    memory.get("task_id", ""), memory.get("agent", ""), memory["scope"],
                    memory["kind"], memory["content"],
                    json.dumps(memory.get("keywords", []), ensure_ascii=False),
                    json.dumps(memory.get("metadata", {}), ensure_ascii=False),
                    float(memory.get("importance", 0.5)), memory["created_at"],
                    memory.get("expires_at"),
                ),
            )
            row = conn.execute(
                "SELECT * FROM agent_memories WHERE id=?", (memory["id"],)
            ).fetchone()
        return self._memory_from_row(row)

    def list_agent_memories(
        self, tenant_id: str, repository: str, scopes: tuple,
        limit: int = 100,
    ) -> list:
        placeholders = ",".join("?" for _ in scopes)
        params = [tenant_id, repository, *scopes, utc_now(), max(1, min(limit, 500))]
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_memories WHERE tenant_id=? AND repository=? "
                "AND scope IN (%s) AND (expires_at IS NULL OR expires_at>?) "
                "ORDER BY importance DESC,created_at DESC LIMIT ?" % placeholders,
                params,
            ).fetchall()
        return [self._memory_from_row(row) for row in rows]

    def delete_agent_memories(self, task_id: str = "", scope: str = "") -> int:
        clauses = []
        params = []
        if task_id:
            clauses.append("task_id=?")
            params.append(task_id)
        if scope:
            clauses.append("scope=?")
            params.append(scope)
        if not clauses:
            raise ValueError("memory deletion requires task_id or scope")
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM agent_memories WHERE " + " AND ".join(clauses), params
            )
            return cursor.rowcount

    def purge_expired_agent_memories(self) -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM agent_memories WHERE expires_at IS NOT NULL AND expires_at<=?",
                (utc_now(),),
            )
            return cursor.rowcount

    @staticmethod
    def _memory_from_row(row) -> Dict[str, Any]:
        value = dict(row)
        value["keywords"] = json.loads(value.pop("keywords_json"))
        value["metadata"] = json.loads(value.pop("metadata_json"))
        return value

    def list_tasks(self, limit: int = 50, tenant_id: Optional[str] = None) -> list:
        with self._connect() as conn:
            if tenant_id is None:
                rows = conn.execute(
                    "SELECT id,state,repository,pull_request,error,created_at,updated_at,tenant_id "
                    "FROM tasks ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 200)),)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id,state,repository,pull_request,error,created_at,updated_at,tenant_id "
                    "FROM tasks WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
                    (tenant_id, max(1, min(limit, 200))),
                ).fetchall()
        return [dict(item) for item in rows]

    def record_failure_case(self, task_id: str, category: str, payload: Dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO failure_cases(task_id, category, payload_json, resolved, created_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, category, json.dumps(payload, ensure_ascii=False), int(category == "accepted"), utc_now()),
            )

    def list_failure_cases(
        self, unresolved_only: bool = False, limit: int = 100,
        tenant_id: Optional[str] = None,
    ) -> list:
        query = "SELECT f.* FROM failure_cases f"
        params = []
        clauses = []
        if tenant_id is not None:
            query += " JOIN tasks t ON t.id=f.task_id"
            clauses.append("t.tenant_id = ?")
            params.append(tenant_id)
        if unresolved_only:
            clauses.append("f.resolved = 0")
            clauses.append("f.category <> 'accepted'")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY f.id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        values = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            values.append(item)
        return values

    def list_task_failure_cases(
        self, task_id: str, tenant_id: Optional[str] = None,
    ) -> list:
        query = "SELECT f.* FROM failure_cases f"
        params = []
        if tenant_id is not None:
            query += " JOIN tasks t ON t.id=f.task_id"
            query += " WHERE f.task_id=? AND t.tenant_id=?"
            params.extend([task_id, tenant_id])
        else:
            query += " WHERE f.task_id=?"
            params.append(task_id)
        query += " ORDER BY f.id DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        values = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            values.append(item)
        return values

    def resolve_failure_cases(self, case_ids: list) -> None:
        ids = [int(value) for value in case_ids]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE failure_cases SET resolved = 1 WHERE id IN (%s)" % placeholders,
                ids,
            )

    def save_evaluation_case(
        self, name: str, split: str, diff: str, expected: list,
        source: str = "manual", active: bool = True,
    ) -> Dict[str, Any]:
        expected_json = json.dumps(expected, ensure_ascii=False)
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM evaluation_cases WHERE name = ?", (name,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["split"] != split
                    or existing["diff"] != diff
                    or json.loads(existing["expected_json"]) != expected
                ):
                    raise ValueError(
                        "evaluation case names are immutable; use a new name for revised content"
                    )
                row = existing
            else:
                conn.execute(
                    "INSERT INTO evaluation_cases(name,split,diff,expected_json,source,active,created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (name, split, diff, expected_json, source, int(active), utc_now()),
                )
                row = conn.execute(
                    "SELECT * FROM evaluation_cases WHERE name = ?", (name,)
                ).fetchone()
        value = dict(row)
        value["expected"] = json.loads(value.pop("expected_json"))
        value["active"] = bool(value["active"])
        return value

    def list_evaluation_cases(
        self, split: Optional[str] = None, active_only: bool = True, limit: int = 100,
    ) -> list:
        clauses = []
        params = []
        if split:
            clauses.append("split = ?")
            params.append(split)
        if active_only:
            clauses.append("active = 1")
        query = "SELECT * FROM evaluation_cases"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        # Feedback-driven cases must remain visible after the optional builtin
        # smoke corpus is imported. Keep the same selection for every caller.
        query += " ORDER BY CASE WHEN source = 'builtin' THEN 1 ELSE 0 END, id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["expected"] = json.loads(value.pop("expected_json"))
            value["active"] = bool(value["active"])
            values.append(value)
        return values

    def count_evaluation_cases(
        self, split: Optional[str] = None, active_only: bool = True,
    ) -> int:
        clauses, params = [], []
        if split:
            clauses.append("split = ?")
            params.append(split)
        if active_only:
            clauses.append("active = 1")
        query = "SELECT COUNT(*) AS n FROM evaluation_cases"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with self._connect() as conn:
            return int(conn.execute(query, params).fetchone()["n"])

    def save_evolution_run(self, run: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO evolution_runs(id,skill_name,candidate_version,baseline_version,decision,"
                "candidate_score,baseline_score,metrics_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    run["id"], run["skill_name"], run["candidate_version"], run.get("baseline_version"),
                    run["decision"], run["candidate_score"], run["baseline_score"],
                    json.dumps(run["metrics"], ensure_ascii=False), run["created_at"],
                ),
            )
        return run

    def list_evolution_runs(self, limit: int = 50) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM evolution_runs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["metrics"] = json.loads(value.pop("metrics_json"))
            values.append(value)
        return values

    def update_evolution_run(self, run_id: str, decision: str, metrics: Dict[str, Any]) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE evolution_runs SET decision = ?, metrics_json = ? WHERE id = ?",
                (decision, json.dumps(metrics, ensure_ascii=False), run_id),
            )
            return cursor.rowcount == 1

    def save_skill_version(self, skill_name: str, prompt: str, score: float, activate: bool = False) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM skill_versions WHERE skill_name = ?", (skill_name,)
            ).fetchone()
            version = int(row["version"]) + 1
            parent = self.get_active_skill_version(skill_name)
            if activate:
                conn.execute("UPDATE skill_versions SET active = 0 WHERE skill_name = ?", (skill_name,))
            conn.execute(
                "INSERT INTO skill_versions(skill_name, version, prompt, score, active, parent_version, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (skill_name, version, prompt, score, int(activate), parent["version"] if parent else None, utc_now()),
            )
        return {"skill_name": skill_name, "version": version, "score": score, "active": activate}

    def get_active_skill_version(self, skill_name: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM skill_versions WHERE skill_name = ? AND active = 1 ORDER BY version DESC LIMIT 1",
                (skill_name,),
            ).fetchone()
        return dict(row) if row else None

    def list_skill_versions(self, skill_name: str) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM skill_versions WHERE skill_name = ? ORDER BY version DESC", (skill_name,)
            ).fetchall()
        return [dict(item) for item in rows]

    def save_skill_artifact(
        self, skill_name: str, artifact: Dict[str, Any], score: float,
        activate: bool = False, tenant_id: str = "default",
    ) -> Dict[str, Any]:
        artifact_json = json.dumps(
            artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        artifact_sha256 = hashlib.sha256(artifact_json.encode("utf-8")).hexdigest()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version),0) AS version FROM skill_artifact_versions "
                "WHERE tenant_id=? AND skill_name=?", (tenant_id, skill_name),
            ).fetchone()
            version = int(row["version"]) + 1
            parent = conn.execute(
                "SELECT version FROM skill_artifact_versions WHERE tenant_id=? AND skill_name=? "
                "AND active=1 ORDER BY version DESC LIMIT 1", (tenant_id, skill_name),
            ).fetchone()
            if activate:
                conn.execute(
                    "UPDATE skill_artifact_versions SET active=0 WHERE tenant_id=? AND skill_name=?",
                    (tenant_id, skill_name),
                )
            created_at = utc_now()
            conn.execute(
                "INSERT INTO skill_artifact_versions(tenant_id,skill_name,version,artifact_json,"
                "artifact_sha256,score,active,parent_version,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (tenant_id, skill_name, version, artifact_json, artifact_sha256, float(score),
                 int(activate), parent["version"] if parent else None, created_at),
            )
        return {
            "tenant_id": tenant_id, "skill_name": skill_name, "version": version, "score": float(score),
            "active": activate, "parent_version": parent["version"] if parent else None,
            "artifact_sha256": artifact_sha256, "created_at": created_at,
        }

    @staticmethod
    def _decode_skill_artifact(row) -> Dict[str, Any]:
        value = dict(row)
        value["artifact"] = json.loads(value.pop("artifact_json"))
        value["active"] = bool(value["active"])
        return value

    def get_active_skill_artifact(
        self, skill_name: str, tenant_id: str = "default",
    ) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM skill_artifact_versions WHERE tenant_id=? AND skill_name=? "
                "AND active=1 ORDER BY version DESC LIMIT 1", (tenant_id, skill_name),
            ).fetchone()
        return self._decode_skill_artifact(row) if row else None

    def list_active_skill_artifacts(self, tenant_id: str = "default") -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM skill_artifact_versions WHERE tenant_id=? AND active=1 "
                "ORDER BY skill_name", (tenant_id,)
            ).fetchall()
        return [self._decode_skill_artifact(row) for row in rows]

    def list_skill_artifact_versions(
        self, skill_name: str, tenant_id: str = "default",
    ) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM skill_artifact_versions WHERE tenant_id=? AND skill_name=? "
                "ORDER BY version DESC", (tenant_id, skill_name),
            ).fetchall()
        return [self._decode_skill_artifact(row) for row in rows]

    def save_skill_evolution_run(self, run: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO skill_evolution_runs(id,tenant_id,skill_name,candidate_version,baseline_version,"
                "decision,candidate_score,baseline_score,metrics_json,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (run["id"], run.get("tenant_id", "default"), run["skill_name"], run["candidate_version"],
                 run.get("baseline_version"), run["decision"], run["candidate_score"],
                 run["baseline_score"], json.dumps(run["metrics"], ensure_ascii=False),
                 run["created_at"]),
            )
        return run

    def list_skill_evolution_runs(
        self, limit: int = 50, tenant_id: Optional[str] = None,
    ) -> list:
        with self._connect() as conn:
            if tenant_id is None:
                rows = conn.execute(
                    "SELECT * FROM skill_evolution_runs ORDER BY created_at DESC LIMIT ?",
                    (max(1, min(limit, 200)),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM skill_evolution_runs WHERE tenant_id=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (tenant_id, max(1, min(limit, 200))),
                ).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["metrics"] = json.loads(value.pop("metrics_json"))
            values.append(value)
        return values

    def save_installation(
        self, installation_id: int, account_login: str, tenant_id: str = "default"
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO installations"
                "(installation_id,account_login,created_at,tenant_id) VALUES (?, ?, ?, ?)",
                (installation_id, account_login, utc_now(), tenant_id),
            )

    def installation_tenant(self, installation_id: int) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT tenant_id FROM installations WHERE installation_id=?", (installation_id,)
            ).fetchone()
        return str(row["tenant_id"]) if row else None

    def save_checkpoint(
        self, task_id: str, node: str, state: Dict[str, Any], status: str = "completed",
        attempt: int = 1, error: str = "",
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO checkpoints(task_id,node,status,attempt,state_json,error,updated_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(task_id,node) DO UPDATE SET "
                "status=excluded.status,attempt=excluded.attempt,state_json=excluded.state_json,"
                "error=excluded.error,updated_at=excluded.updated_at",
                (task_id, node, status, attempt, json.dumps(state, ensure_ascii=False),
                 error[:2000] or None, utc_now()),
            )

    def load_checkpoints(self, task_id: str) -> Dict[str, Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT node,status,attempt,state_json,error,updated_at FROM checkpoints "
                "WHERE task_id=? ORDER BY updated_at", (task_id,)
            ).fetchall()
        result = {}
        for row in rows:
            item = dict(row)
            item["state"] = json.loads(item.pop("state_json"))
            result[item.pop("node")] = item
        return result

    def save_task_payload(self, task_id: str, diff: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO task_payloads(task_id,diff,created_at) VALUES (?,?,?)",
                (task_id, diff, utc_now()),
            )

    def get_task_payload(self, task_id: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT diff FROM task_payloads WHERE task_id=?", (task_id,)
            ).fetchone()
        return str(row["diff"]) if row else None

    def request_cancel(self, task_id: str, tenant_id: Optional[str] = None) -> bool:
        query = "UPDATE tasks SET cancel_requested=1,state='CANCELLED',updated_at=? WHERE id=? AND state <> 'SUCCESS'"
        values = [utc_now(), task_id]
        if tenant_id is not None:
            query += " AND tenant_id=?"
            values.append(tenant_id)
        with self._lock, self._connect() as conn:
            cursor = conn.execute(query, values)
            return cursor.rowcount > 0

    def is_cancelled(self, task_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cancel_requested,state FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
        return bool(row and (row["cancel_requested"] or row["state"] == 'CANCELLED'))

    def cancel(self, task_id: str, event: TraceEvent) -> None:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET cancel_requested=1,state=?,updated_at=? WHERE id=? AND state <> 'SUCCESS'",
                (TaskState.CANCELLED.value, event.created_at, task_id),
            )
            if cursor.rowcount:
                conn.execute(
                    "INSERT INTO trace_events(task_id,step,state,message,created_at) VALUES (?,?,?,?,?)",
                    (task_id, event.step, event.state.value, event.message, event.created_at),
                )

    def set_publication(self, task_id: str, status: str, error: str = "") -> None:
        if status not in {'not_requested', 'pending', 'sent', 'failed', 'superseded'}:
            raise ValueError('invalid publication status')
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET publication_status=?,publication_error=?,updated_at=? WHERE id=?",
                (status, error[:2000] or None, utc_now(), task_id),
            )

    def save_downloaded_payload(self, task_id: str, diff: str, updates: Dict[str, Any]) -> None:
        """Persist verified diff and its revision in the same transaction."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT input_json FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if not row:
                raise ValueError('task not found')
            value = json.loads(row['input_json'])
            value.update(updates)
            conn.execute(
                "INSERT INTO task_payloads(task_id,diff,created_at) VALUES (?,?,?) "
                "ON CONFLICT(task_id) DO UPDATE SET diff=excluded.diff,created_at=excluded.created_at",
                (task_id, diff, utc_now()),
            )
            conn.execute(
                "UPDATE tasks SET input_json=?,updated_at=? WHERE id=?",
                (json.dumps(value, ensure_ascii=False), utc_now(), task_id),
            )

    def claim_webhook(
        self, delivery_id: str, tenant_id: str, event_type: str, payload_sha256: str,
    ) -> bool:
        if not delivery_id:
            raise ValueError("X-GitHub-Delivery is required")
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO webhook_deliveries"
                    "(delivery_id,tenant_id,event_type,payload_sha256,received_at) VALUES (?,?,?,?,?)",
                    (delivery_id, tenant_id, event_type, payload_sha256, utc_now()),
                )
                return True
            except sqlite3.IntegrityError:
                row = conn.execute(
                    "SELECT payload_sha256 FROM webhook_deliveries WHERE delivery_id=?",
                    (delivery_id,),
                ).fetchone()
                if row and row["payload_sha256"] != payload_sha256:
                    raise ValueError("delivery id was already used with a different payload")
                return False

    def complete_webhook(self, delivery_id: str, task_id: Optional[str]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE webhook_deliveries SET task_id=?,dispatch_status=?,dispatch_error=NULL WHERE delivery_id=?",
                (task_id, 'queued' if task_id else 'ignored', delivery_id),
            )

    def mark_webhook_dispatch(self, delivery_id: str, status: str, error: str = "") -> None:
        if status not in {'pending', 'queued', 'failed', 'ignored'}:
            raise ValueError('invalid dispatch status')
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE webhook_deliveries SET dispatch_status=?,dispatch_error=? WHERE delivery_id=?",
                (status, error[:2000] or None, delivery_id),
            )

    def create_webhook_task(
        self, delivery_id: str, tenant_id: str, payload_sha256: str,
        task_id: str, repository: str, pull_request: int, payload: Dict[str, Any],
    ) -> tuple:
        """Associate delivery and validated task atomically; retries use the original id."""
        if not delivery_id:
            raise ValueError('X-GitHub-Delivery is required')
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute(
                "INSERT OR IGNORE INTO webhook_deliveries(delivery_id,tenant_id,event_type,payload_sha256,received_at) "
                "VALUES (?,?,'pull_request',?,?)",
                (delivery_id, tenant_id, payload_sha256, now),
            )
            row = conn.execute(
                "SELECT * FROM webhook_deliveries WHERE delivery_id=?", (delivery_id,)
            ).fetchone()
            if row['payload_sha256'] != payload_sha256 or row['tenant_id'] != tenant_id:
                raise ValueError('delivery id was already used with a different payload or tenant')
            if row['task_id']:
                return row['task_id'], False
            conn.execute(
                "INSERT INTO tasks(id,state,repository,pull_request,input_json,created_at,updated_at,tenant_id,cancel_requested,publication_status) "
                "VALUES (?,'PENDING',?,?,?,?,?,?,0,?)",
                (task_id, repository, pull_request, json.dumps(payload, ensure_ascii=False),
                 now, now, tenant_id, 'pending' if payload.get('publish_review') else 'not_requested'),
            )
            conn.execute(
                "UPDATE webhook_deliveries SET task_id=?,dispatch_status='pending',dispatch_error=NULL WHERE delivery_id=?",
                (task_id, delivery_id),
            )
            return task_id, True

    def get_webhook(self, delivery_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM webhook_deliveries WHERE delivery_id=?", (delivery_id,)
            ).fetchone()
        return dict(row) if row else None

    def create_user(
        self, user_id: str, username: str, password_hash: str,
        tenant_id: str, role: str,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO users(id,username,password_hash,created_at) VALUES (?,?,?,?)",
                (user_id, username, password_hash, utc_now()),
            )
            row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
            conn.execute(
                "INSERT INTO memberships(user_id,tenant_id,role) VALUES (?,?,?) "
                "ON CONFLICT(user_id,tenant_id) DO UPDATE SET role=excluded.role",
                (row["id"], tenant_id, role),
            )

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id,username,password_hash,active FROM users WHERE username=?", (username,)
            ).fetchone()
            if not row:
                return None
            memberships = conn.execute(
                "SELECT tenant_id,role FROM memberships WHERE user_id=?", (row["id"],)
            ).fetchall()
        value = dict(row)
        value["memberships"] = [dict(item) for item in memberships]
        return value

    def grant_repository(self, tenant_id: str, repository: str, auto_fix: bool = False) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO repository_grants(tenant_id,repository,auto_fix) VALUES (?,?,?) "
                "ON CONFLICT(tenant_id,repository) DO UPDATE SET auto_fix=excluded.auto_fix",
                (tenant_id, repository, int(auto_fix)),
            )

    def repository_allowed(
        self, tenant_id: str, repository: str, require_auto_fix: bool = False,
    ) -> bool:
        with self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM repository_grants WHERE tenant_id=?", (tenant_id,)
            ).fetchone()["n"]
            row = conn.execute(
                "SELECT auto_fix FROM repository_grants WHERE tenant_id=? AND repository=?",
                (tenant_id, repository),
            ).fetchone()
        if total == 0:
            return True
        return bool(row and (not require_auto_fix or row["auto_fix"]))

    def audit(
        self, tenant_id: str, actor: str, action: str, resource: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO audit_log(tenant_id,actor,action,resource,detail_json,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (tenant_id, actor, action, resource,
                 json.dumps(detail or {}, ensure_ascii=False), utc_now()),
            )

    def list_audit(self, tenant_id: str, limit: int = 100) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT actor,action,resource,detail_json,created_at FROM audit_log "
                "WHERE tenant_id=? ORDER BY id DESC LIMIT ?",
                (tenant_id, max(1, min(limit, 500))),
            ).fetchall()
        values = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            values.append(item)
        return values

    def create_alert(
        self, tenant_id: str, alert_key: str, severity: str, message: str,
    ) -> None:
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO alerts"
                "(tenant_id,alert_key,severity,message,status,created_at,updated_at) "
                "VALUES (?,?,?,?, 'open',?,?)",
                (tenant_id, alert_key, severity, message[:1000], now, now),
            )

    def list_alerts(self, tenant_id: str, limit: int = 100) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM alerts WHERE tenant_id=? ORDER BY id DESC LIMIT ?",
                (tenant_id, max(1, min(limit, 500))),
            ).fetchall()
        return [dict(row) for row in rows]

    def dashboard_stats(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        with self._connect() as conn:
            clause = " WHERE tenant_id=?" if tenant_id is not None else ""
            params = (tenant_id,) if tenant_id is not None else ()
            total = conn.execute("SELECT COUNT(*) AS n FROM tasks" + clause, params).fetchone()["n"]
            state_prefix = clause + (" AND " if clause else " WHERE ")
            success = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks" + state_prefix + "state='SUCCESS'", params
            ).fetchone()["n"]
            failed = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks" + state_prefix + "state='FAILED'", params
            ).fetchone()["n"]
            if tenant_id is None:
                failures = conn.execute(
                    "SELECT COUNT(*) AS n FROM failure_cases WHERE resolved=0 AND category <> 'accepted'"
                ).fetchone()["n"]
            else:
                failures = conn.execute(
                    "SELECT COUNT(*) AS n FROM failure_cases f JOIN tasks t ON t.id=f.task_id "
                    "WHERE f.resolved=0 AND f.category <> 'accepted' AND t.tenant_id=?", (tenant_id,)
                ).fetchone()["n"]
            active_skills = conn.execute(
                "SELECT COUNT(*) AS n FROM skill_versions WHERE active = 1"
            ).fetchone()["n"]
            if tenant_id is None:
                active_skills += conn.execute(
                    "SELECT COUNT(*) AS n FROM skill_artifact_versions WHERE active=1"
                ).fetchone()["n"]
            else:
                active_skills += conn.execute(
                    "SELECT COUNT(*) AS n FROM skill_artifact_versions "
                    "WHERE tenant_id=? AND active=1", (tenant_id,)
                ).fetchone()["n"]
        return {
            "tasks_total": total, "tasks_success": success, "tasks_failed": failed,
            "success_rate": round(success / total, 4) if total else 0.0,
            "unresolved_failure_cases": failures, "active_skill_versions": active_skills,
        }
