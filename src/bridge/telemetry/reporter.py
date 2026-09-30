"""The reporter: puts events on the bus, or in the outbox if it can't.

Receipts distinguish the two guarantees:

* ``Receipt("durable")``  — JetStream acknowledged: the event is in the stream.
* ``Receipt("outbox")``   — bus unreachable/timeout: the event is on local
  disk and will be retried; it is *not* yet durable on the bus.
* ``Receipt("off")``      — telemetry disabled (``BRIDGE_TELEMETRY=off``).
* ``Receipt("dropped")``  — outbox write itself failed (disk full/permissions).

Memory and agent work never block on the bus: publishing failures are caught,
recorded and retried, never raised at the caller.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .. import paths
from ..util import redact, redact_deep
from . import nats as bus
from . import outbox
from .events import subject_for

Publisher = Callable[[str, bytes], None]  # (subject, payload) -> None or raise


@dataclass
class Receipt:
    status: str  # durable | outbox | off | dropped
    event_id: str
    detail: str = ""

    @property
    def durable(self) -> bool:
        return self.status == "durable"


class Reporter:
    def __init__(
        self,
        nats_url: str,
        *,
        stream: str = bus.DEFAULT_STREAM,
        wildcard: str = bus.DEFAULT_WILDCARD,
        publisher: Publisher | None = None,
        retry_interval: float = 5.0,
        background_retry: bool = True,
    ) -> None:
        self.nats_url = nats_url
        self.stream = stream
        self.wildcard = wildcard
        self._publisher = publisher or self._bus_publish
        self.retry_interval = retry_interval
        self.background_retry = background_retry
        self._bus_ok = True  # optimistic: try before falling back
        self._last_failure = 0.0
        self._flush_lock = threading.Lock()
        self._retry_lock = threading.Lock()
        self._retry_thread: threading.Thread | None = None

    # -- config ---------------------------------------------------------------

    @staticmethod
    def enabled() -> bool:
        return os.environ.get(paths.ENV_TELEMETRY, "").strip().lower() not in ("off", "0", "false")

    def _bus_publish(self, subject: str, payload: bytes) -> None:
        bus.publish(self.nats_url, subject, payload, stream=self.stream, wildcard=self.wildcard)

    # -- emit -----------------------------------------------------------------

    def emit(self, event: dict[str, Any]) -> Receipt:
        event = redact_deep(event)
        event_id = str(event.get("event_id") or "")
        subject = event.get("subject") or subject_for(str(event.get("type", "unknown")))
        payload = json.dumps(event, default=str)

        if not self.enabled():
            return Receipt("off", event_id, "BRIDGE_TELEMETRY=off")

        # Keep chronology: drain what is already waiting before the new event.
        if outbox.count() and self._may_retry():
            self.flush()

        if not self._may_retry():
            return self._to_outbox(event_id, subject, payload, "bus marked down; not retried yet")

        try:
            self._publisher(subject, payload.encode("utf-8"))
            self._bus_ok = True
            return Receipt("durable", event_id)
        except Exception as e:  # noqa: BLE001 — a bus error must never break the caller
            self._bus_ok = False
            self._last_failure = time.monotonic()
            return self._to_outbox(event_id, subject, payload, str(e))

    def _may_retry(self) -> bool:
        if self._bus_ok:
            return True
        return (time.monotonic() - self._last_failure) >= self.retry_interval

    def _to_outbox(self, event_id: str, subject: str, payload: str, error: str) -> Receipt:
        try:
            safe_error = redact(error)
            outbox.add(event_id, subject, payload, safe_error)
            self._schedule_retry()
            return Receipt("outbox", event_id, safe_error[:200])
        except Exception as e:  # noqa: BLE001
            return Receipt("dropped", event_id, f"outbox write failed: {e}")

    # -- retry ----------------------------------------------------------------

    def flush(self) -> tuple[int, int]:
        """Retry everything in the outbox. Returns (sent, still_failing)."""
        with self._flush_lock:
            return self._flush_locked()

    def _flush_locked(self) -> tuple[int, int]:
        sent = 0
        failed = 0
        for row in outbox.pending():
            try:
                self._publisher(row["subject"], row["payload"].encode("utf-8"))
                outbox.remove([row["event_id"]])
                sent += 1
                self._bus_ok = True
            except Exception as e:  # noqa: BLE001
                outbox.mark_failed(row["event_id"], redact(str(e)))
                failed += 1
                self._bus_ok = False
                self._last_failure = time.monotonic()
                break  # bus is down again: stop hammering it
        if not failed and not outbox.count():
            self._bus_ok = True
        return sent, failed

    def _schedule_retry(self) -> None:
        if not self.background_retry:
            return
        with self._retry_lock:
            if self._retry_thread and self._retry_thread.is_alive():
                return
            self._retry_thread = threading.Thread(
                target=self._retry_until_empty,
                name="bridge-outbox-retry",
                daemon=True,
            )
            self._retry_thread.start()

    def _retry_until_empty(self) -> None:
        delay = max(self.retry_interval, 0.25)
        while True:
            time.sleep(delay)
            try:
                self.flush()
                if not outbox.count():
                    return
            except Exception:  # noqa: BLE001 — keep durable rows for the next process
                pass
            delay = min(delay * 2, 60.0)
