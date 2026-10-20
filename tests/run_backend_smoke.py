"""Exercise disposable loopback Redis/PostgreSQL without loading local secrets.

Usage: python tests/run_backend_smoke.py --redis-url redis://127.0.0.1:PORT/0
       [--database-url postgresql://USER:PASSWORD@127.0.0.1:PORT/DATABASE]

Each run owns unique Redis keys and, for PostgreSQL, a temporary schema. Cleanup
only removes those objects. No containers, Docker settings, or other data change.
Without --database-url the service uses temporary SQLite; PostgreSQL is SKIPPED.
"""
import argparse
import builtins
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from unittest.mock import Mock, patch
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid


ROOT = Path(__file__).resolve().parents[1]
DIFF = "--- a/app.py\n+++ b/app.py\n@@ -0,0 +1 @@\n+value = 1\n"


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def wait_until(predicate, message, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError(message)


def emit(check, **values):
    print(json.dumps({"check": check, "status": "passed", **values}), flush=True)


def loopback_url(value, schemes):
    parsed = urlsplit(value)
    if parsed.scheme not in schemes or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("backend smoke requires an explicit loopback backend URL")
    return value


@contextmanager
def isolated_imports():
    original_open, original_connect = builtins.open, sqlite3.connect
    blocked = {ROOT / ".env", ROOT / "evoagent" / ".env"}

    def safe_open(path, *args, **kwargs):
        if isinstance(path, (str, bytes, Path)) and Path(path).absolute() in blocked:
            raise FileNotFoundError(str(path))
        return original_open(path, *args, **kwargs)

    def safe_connect(path, *args, **kwargs):
        if isinstance(path, (str, bytes, Path)) and Path(path).absolute() == ROOT / "evoagent.db":
            raise RuntimeError("backend smoke must not open the live database")
        return original_connect(path, *args, **kwargs)

    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests"))
    with patch("builtins.open", safe_open), patch("sqlite3.connect", safe_connect):
        yield


@contextmanager
def postgres_schema(database_url, identity):
    if not database_url:
        yield ""
        return
    import psycopg
    from psycopg import sql
    schema = "evoagent_smoke_" + identity
    with psycopg.connect(database_url) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    parsed = urlsplit(database_url)
    query = dict(parse_qsl(parsed.query))
    # Only this randomly named schema is visible to the application's SQL.
    query["options"] = "-csearch_path=" + schema
    scoped_url = urlunsplit(parsed._replace(query=urlencode(query)))
    try:
        yield scoped_url
    finally:
        with psycopg.connect(database_url) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def store_contract(store, reopen):
    from evoagent.evolution import seed_builtin_evaluation_cases
    require(store.count_evaluation_cases() == 0, "new test schema must have no cases")
    seed_builtin_evaluation_cases(store)
    expected = [{"path": "app.py", "line": 1, "min_severity": "high"}]
    store.save_evaluation_case("manual-old", "validation", DIFF, expected, "manual")
    store.save_evaluation_case("manual-new", "validation", DIFF, expected, "api")
    store.save_evaluation_case("inactive", "validation", DIFF, expected, "api", False)
    require([case["name"] for case in store.list_evaluation_cases("validation", True, 2)]
            == ["manual-new", "manual-old"], "manual/newest selection policy differs")
    require(store.count_evaluation_cases("validation") == 7, "validation count differs")
    require(store.count_evaluation_cases("holdout") == 2, "holdout count differs")
    identity = "feedback-contract"
    store.create(identity, "org/repo", 7, {}, "smoke")
    for category in ("false_positive", "accepted", "execution_error"):
        store.record_failure_case(identity, category, {"note": category})
    history = store.list_task_failure_cases(identity, "smoke")
    require(len(history) == 3, "feedback history was lost")
    require(next(case for case in history if case["category"] == "accepted")["resolved"],
            "accepted feedback must be resolved")
    pending = store.list_failure_cases(True, 100, "smoke")
    require({case["category"] for case in pending} == {"false_positive", "execution_error"},
            "accepted feedback inflated pending selection")
    require(store.dashboard_stats("smoke")["unresolved_failure_cases"] == 2,
            "pending feedback dashboard count differs")
    store.resolve_failure_cases([case["id"] for case in pending])
    require(store.dashboard_stats("smoke")["unresolved_failure_cases"] == 0,
            "resolved feedback still counted")
    store.set_publication(identity, "superseded", "reviewed revision changed")
    persisted = reopen()
    require(persisted.get(identity)["publication_status"] == "superseded", "publication not persisted")
    require(len(persisted.list_task_failure_cases(identity)) == 3, "reopened store lost history")
    require(persisted.count_evaluation_cases("validation") == 7, "reopened store lost samples")
    emit("store_selection_feedback_publication_persistence", backend=type(store).__name__)


class TransientReadFailure:
    """Inject two connection losses; all subsequent I/O uses the real Redis."""
    def __init__(self, client):
        self.client = client
        self.failures = 0
        self.reads = 0
        self.lock = threading.Lock()

    def __getattr__(self, name):
        return getattr(self.client, name)

    def xreadgroup(self, *args, **kwargs):
        import redis
        with self.lock:
            self.reads += 1
            if self.failures < 2:
                self.failures += 1
                raise redis.ConnectionError("intentional smoke connection loss")
        return self.client.xreadgroup(*args, **kwargs)


def service_contract(redis_url, database_url, directory, redis_client, identity):
    import redis
    from helpers import FakeAgenticClient
    from evoagent.config import Settings
    from evoagent.models import ReviewReport, TaskState, TraceEvent
    from evoagent.postgres_store import create_store
    from evoagent.service import ReviewService
    from evoagent.store import utc_now
    from evoagent.task_queue import TaskQueue

    class BlockingClient(FakeAgenticClient):
        def __init__(self):
            self.started, self.release = threading.Event(), threading.Event()
            self.calls = []
            self.lock = threading.Lock()

        def complete_json(self, role, *args, **kwargs):
            with self.lock:
                self.calls.append(role)
            if role == "security":
                self.started.set()
                if not self.release.wait(15):
                    raise RuntimeError("smoke review was not released")
            return super().complete_json(role, *args, **kwargs)

    llm = BlockingClient()
    reconnecting = TransientReadFailure(redis_client)
    prefix = "evoagent:smoke:" + identity
    keys = {"STREAM": prefix + ":stream", "DLQ": prefix + ":dlq", "GROUP": prefix + ":workers"}
    settings = Settings(
        host="127.0.0.1", port=8080, db_path=str(Path(directory) / "smoke.db"),
        database_url=database_url, redis_url=redis_url, async_workers=2,
        max_diff_bytes=10000, max_steps=8, timeout_seconds=30,
        llm_base_url="https://invalid.test", llm_api_key="fake", llm_model="fake-model",
        github_webhook_secret="", github_token="", auto_post_review=True,
        skills_dir=str(Path(directory) / "skills"), memory_enabled=False,
        queue_lease_seconds=1,
    )
    service = None
    with patch.multiple(TaskQueue, **keys):
        try:
            with patch("evoagent.service.JsonChatClient", return_value=llm), \
                    patch.object(redis.Redis, "from_url", return_value=reconnecting):
                service = ReviewService(settings)
            response = service.enqueue_review("org/repo", DIFF)
            task_id = response["task_id"]
            require(response["queue"] == "redis-streams", "service did not use real Redis")
            require(llm.started.wait(10), "workers failed to reconnect and start queued review")
            resumed = service.resume_task(task_id)
            require(resumed.get("active"), "running queued review was not registered")
            idle_samples = []
            # Keep the task running for more than two full leases while the
            # second consumer tries reclaiming stale work.
            for _ in range(5):
                time.sleep(.5)
                pending = redis_client.xpending_range(keys["STREAM"], keys["GROUP"], "-", "+", 10)
                require(len(pending) == 1, "running review is missing its pending lease")
                idle_samples.append(pending[0]["time_since_delivered"])
                require(pending[0]["times_delivered"] == 1, "running entry was reclaimed twice")
            require(max(idle_samples) < 1000, "running lease was not renewed")
            require(llm.calls.count("security") == 1, "long review executed twice")
            llm.release.set()
            wait_until(lambda: service.store.get(task_id)["state"] in {"SUCCESS", "FAILED"},
                       "review did not reach a terminal state")
            task = service.store.get(task_id)
            require(task["state"] == "SUCCESS", "queued review failed: " + str(task.get("error")))
            wait_until(lambda: redis_client.xpending(keys["STREAM"], keys["GROUP"])["pending"] == 0
                       and not service.queue.is_active(task_id), "completed review was not ACKed")
            require(reconnecting.failures == 2 and reconnecting.reads > 2, "disconnect path was not exercised")
            require(llm.calls.count("security") == 1 and llm.calls.count("correctness-reliability") == 1,
                    "professional workers did not run exactly once")
            require(llm.calls.count("lead") == 2, "Lead ran unexpected extra rounds")
            reopened = create_store(database_url, settings.db_path)
            saved = reopened.get(task_id)
            require(saved["report"] and saved["report"].get("collaboration"),
                    "report or current agent collaboration was not persisted")
            require(reopened.get_task_payload(task_id) == DIFF, "durable diff was lost")
            require(reopened.load_checkpoints(task_id), "runtime checkpoints were not persisted")
            emit("real_redis_async_review_reconnect_lease_ack", backend=type(reopened).__name__,
                 connection_failures=reconnecting.failures, max_pending_idle_ms=max(idle_samples),
                 worker_calls={role: llm.calls.count(role) for role in set(llm.calls)})

            # A completed review from a former revision must stop publication,
            # persist superseded, and remain terminal through resume.
            publication_id = "publication-contract"
            reviewed_head = "a" * 40
            service.store.create(publication_id, "org/repo", 7, {
                "publish_review": True, "installation_id": None,
                "github_issue_url": "https://api.github.com/repos/org/repo/issues/7",
                "head_sha": reviewed_head,
            })
            service.store.save_task_payload(publication_id, DIFF)
            report = ReviewReport("org/repo", 7, "complete", "low")
            report.execution["reviewed_revision"] = {"head_sha": reviewed_head}
            service.store.succeed(publication_id, report, TraceEvent(1, TaskState.SUCCESS, "done", utc_now()))
            service.store.set_publication(publication_id, "pending")
            github = Mock()
            github.get_pull_request.return_value = {"head": {"sha": "b" * 40}}
            before = list(llm.calls)
            service.github_client_for_installation = Mock(return_value=github)
            service._process_queued({"task_id": publication_id})
            require(reopened.get(publication_id)["publication_status"] == "superseded",
                    "changed revision publication did not persist superseded")
            require(service.resume_task(publication_id)["publication_status"] == "superseded",
                    "resume revived a superseded publication")
            github.upsert_comment.assert_not_called()
            require(llm.calls == before, "publication reran model review")
            emit("superseded_publication_never_posts_or_reruns", backend=type(reopened).__name__)
        finally:
            llm.release.set()
            if service is not None:
                service.queue.close()
                # xreadgroup BLOCK is bounded to one second. Join before keys
                # are removed so a late worker cannot touch another test run.
                service.queue._executor.shutdown(wait=True)
            redis_client.delete(keys["STREAM"], keys["DLQ"])


def run(args):
    import redis
    from evoagent.postgres_store import create_store
    identity = uuid.uuid4().hex
    redis_url = loopback_url(args.redis_url, {"redis", "rediss"})
    database_url = loopback_url(args.database_url, {"postgres", "postgresql"}) if args.database_url else ""
    client = redis.Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=5, socket_timeout=5)
    client.ping()
    with tempfile.TemporaryDirectory(prefix="evoagent-backend-smoke-") as directory, \
            postgres_schema(database_url, identity) as scoped_database:
        store_path = str(Path(directory) / "contract.db")
        store = create_store(scoped_database, store_path)
        store_contract(store, lambda: create_store(scoped_database, store_path))
        service_contract(redis_url, scoped_database, directory, client, identity)
    client.close()
    print(json.dumps({"result": "passed", "redis": "real",
                      "postgresql": "real" if database_url else "SKIPPED (temporary SQLite)"}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redis-url", required=True)
    parser.add_argument("--database-url", default="")
    args = parser.parse_args()
    with isolated_imports():
        run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
