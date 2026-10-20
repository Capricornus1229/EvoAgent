"""Durable task delivery with ACK, leases, retry backoff and a dead-letter queue."""
import json
import queue
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Callable, Dict, Optional
from .runtime import RuntimeCancelled


class PermanentTaskError(RuntimeError):
    """An error that must not be retried."""


class DeliveryAlreadyRunning(RuntimeError):
    """A task already has a reservation or running execution in this process."""


class TaskQueue:
    STREAM = "evoagent:review:stream"
    DLQ = "evoagent:review:dlq"
    GROUP = "evoagent-workers"

    def __init__(
        self, handler: Callable[[Dict[str, Any]], None], workers: int = 2,
        redis_url: str = "", max_attempts: int = 3, lease_seconds: int = 60,
        on_dead_letter: Optional[Callable[[Dict[str, Any], str], None]] = None,
    ):
        self.handler = handler
        self.redis_url = redis_url
        self.max_attempts = max_attempts
        self.lease_seconds = lease_seconds
        self.on_dead_letter = on_dead_letter
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="evoagent-worker")
        self._redis = None
        self._memory: queue.Queue = queue.Queue()
        self._memory_dlq = []
        self._lock = threading.Lock()
        # A reservation spans waiting, execution and retry backoff. It is local
        # process protection, not a distributed Redis lease.
        self._reservations = set()
        self._running = set()
        self._stop = threading.Event()
        self.consumer = "%s-%s" % (socket.gethostname(), uuid.uuid4().hex[:8])
        if redis_url:
            try:
                import redis
            except ImportError as exc:
                raise RuntimeError("Redis mode requires: pip install redis") from exc
            self._redis = redis.Redis.from_url(
                redis_url, decode_responses=True,
                socket_connect_timeout=5, socket_timeout=5,
            )
            self._redis.ping()
            try:
                self._redis.xgroup_create(self.STREAM, self.GROUP, id="0", mkstream=True)
            except redis.ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
            for _ in range(workers):
                self._executor.submit(self._redis_worker)

    @property
    def backend(self) -> str:
        return "redis-streams" if self._redis else "memory-acked"

    def is_active(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._reservations

    @contextmanager
    def inline_execution(self, task_id: str):
        """Register synchronous work under the same ownership as queued work."""
        with self._lock:
            if self._stop.is_set():
                raise RuntimeError("task queue is closed")
            if task_id in self._reservations or task_id in self._running:
                raise DeliveryAlreadyRunning("task %s is already active locally" % task_id)
            self._reservations.add(task_id)
            self._running.add(task_id)
        try:
            yield
        finally:
            with self._lock:
                self._running.discard(task_id)
                self._reservations.discard(task_id)

    def submit(
        self, payload: Dict[str, Any], message_id: str = "",
        prepare: Optional[Callable[[], None]] = None,
    ) -> str:
        identity = message_id or str(payload.get("task_id") or uuid.uuid4())
        with self._lock:
            if identity in self._reservations:
                return identity
            if self._stop.is_set():
                raise RuntimeError("task queue is closed")
            self._reservations.add(identity)
        envelope = {
            "message_id": identity,
            "attempt": 0,
            "payload": payload,
            "submitted_at": time.time(),
        }
        try:
            if prepare:
                prepare()
            if self._redis:
                self._redis.xadd(self.STREAM, {"envelope": json.dumps(envelope, ensure_ascii=False)})
            else:
                self._executor.submit(self._deliver, envelope)
        except Exception:
            self._release(envelope)
            raise
        return envelope["message_id"]

    def _release(self, envelope: Dict[str, Any]) -> None:
        with self._lock:
            self._reservations.discard(envelope.get("message_id"))

    def _deliver(self, envelope: Dict[str, Any], redis_id: str = "") -> bool:
        identity = envelope.get("message_id")
        with self._lock:
            if identity in self._running:
                raise DeliveryAlreadyRunning("task %s is already running locally" % identity)
            self._running.add(identity)
            self._reservations.add(identity)
        terminal = False
        try:
            envelope["attempt"] = int(envelope.get("attempt", 0)) + 1
            try:
                with self._lease_heartbeat(redis_id):
                    self.handler(envelope["payload"])
                terminal = True
                return True
            except RuntimeCancelled:
                # Cancellation is an acknowledged terminal outcome, not a failure.
                terminal = True
                return True
            except PermanentTaskError as exc:
                terminal = True
                self._dead_letter(envelope, str(exc))
                return False
            except Exception as exc:
                if envelope["attempt"] >= self.max_attempts:
                    terminal = True
                    self._dead_letter(envelope, str(exc))
                elif self._redis:
                    self._redis.xadd(self.STREAM, {
                        "envelope": json.dumps(envelope, ensure_ascii=False)
                    })
                else:
                    delay = min(2 ** (envelope["attempt"] - 1), 10)
                    timer = threading.Timer(delay, self._submit_memory, args=(envelope,))
                    timer.daemon = True
                    timer.start()
                return False
        finally:
            # Drop the running attempt and terminal reservation together so a
            # new delivery cannot race their release. Backoff keeps its reservation.
            with self._lock:
                self._running.discard(identity)
                if terminal:
                    self._reservations.discard(identity)

    def _submit_memory(self, envelope: Dict[str, Any]) -> None:
        if not self._stop.is_set():
            self._executor.submit(self._deliver, envelope)

    @contextmanager
    def _lease_heartbeat(self, redis_id: str):
        """Keep a running stream entry pending with this consumer until completion."""
        if self._redis is None or not redis_id:
            yield
            return
        finished = threading.Event()

        def renew():
            while not finished.wait(max(0.01, self.lease_seconds / 3)):
                if self._stop.is_set():
                    return
                try:
                    # JUSTID renews idle time without increasing delivery count.
                    self._redis.xclaim(
                        self.STREAM, self.GROUP, self.consumer,
                        min_idle_time=0, message_ids=[redis_id], justid=True,
                    )
                except Exception:
                    # A brief outage must not fail an otherwise healthy review.
                    # The next heartbeat retries; the worker leaves failed ACKs pending.
                    continue

        heartbeat = threading.Thread(target=renew, name="evoagent-lease", daemon=True)
        heartbeat.start()
        try:
            yield
        finally:
            finished.set()
            heartbeat.join(timeout=1)

    def _process_redis_entry(self, redis_id: str, fields: dict) -> None:
        try:
            envelope = json.loads(fields["envelope"])
            if not isinstance(envelope, dict):
                raise ValueError("queue envelope must be an object")
        except Exception as exc:
            envelope = {
                "message_id": redis_id, "attempt": self.max_attempts,
                "payload": {}, "submitted_at": time.time(),
            }
            self._dead_letter(envelope, "invalid queue envelope: %s" % exc)
        else:
            self._deliver(envelope, redis_id=redis_id)
        # ACK only after work completed or was safely requeued/DLQed.
        self._redis.xack(self.STREAM, self.GROUP, redis_id)

    def _redis_worker(self) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                self._reclaim_stale()
                messages = self._redis.xreadgroup(
                    self.GROUP, self.consumer, {self.STREAM: ">"}, count=1, block=1000
                )
            except Exception:
                failures += 1
                self._stop.wait(min(2 ** min(failures - 1, 4), 10))
                continue
            failures = 0
            for _stream, entries in messages:
                for redis_id, fields in entries:
                    try:
                        self._process_redis_entry(redis_id, fields)
                    except Exception:
                        # Infrastructure failure: leave pending for lease recovery.
                        continue

    def _reclaim_stale(self) -> None:
        try:
            result = self._redis.xautoclaim(
                self.STREAM, self.GROUP, self.consumer,
                min_idle_time=self.lease_seconds * 1000, start_id="0-0", count=10,
            )
            entries = result[1] if len(result) > 1 else []
            for redis_id, fields in entries:
                try:
                    self._process_redis_entry(redis_id, fields)
                except Exception:
                    # A local duplicate or one failed entry must not starve the rest.
                    continue
        except Exception:
            # Redis versions without XAUTOCLAIM still process new entries.
            return

    def _dead_letter(self, envelope: Dict[str, Any], error: str) -> None:
        item = {**envelope, "error": error[:2000], "failed_at": time.time()}
        if self._redis:
            self._redis.xadd(self.DLQ, {"envelope": json.dumps(item, ensure_ascii=False)})
        else:
            with self._lock:
                self._memory_dlq.append(item)
        if self.on_dead_letter:
            self.on_dead_letter(envelope.get("payload") or {}, item["error"])

    def dead_letters(self, limit: int = 100) -> list:
        if self._redis:
            rows = self._redis.xrevrange(self.DLQ, count=max(1, min(limit, 500)))
            return [json.loads(fields["envelope"]) for _id, fields in rows]
        with self._lock:
            return list(reversed(self._memory_dlq[-limit:]))

    def close(self) -> None:
        self._stop.set()
        self._executor.shutdown(wait=False)
