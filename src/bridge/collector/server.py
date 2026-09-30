"""The collector: durable NATS consumer -> sqlite, deduplicated by event id.

Run it with ``bridge collector run`` (foreground). It creates/uses the
``BRIDGE`` stream, binds a durable pull consumer (``bridge-collector``), and
explicitly acks each message only after it is stored — crash after store but
before ack replays the message, and ``store_event`` makes that a no-op.
"""

from __future__ import annotations

import asyncio
import json
import signal
from typing import Any

from ..telemetry import nats as bus
from . import store


class Collector:
    def __init__(
        self,
        nats_url: str,
        *,
        stream: str = bus.DEFAULT_STREAM,
        wildcard: str = bus.DEFAULT_WILDCARD,
        durable: str = "bridge-collector",
        db_path=None,
        batch: int = 64,
        poll_timeout: float = 1.0,
    ) -> None:
        self.nats_url = nats_url
        self.stream = stream
        self.wildcard = wildcard
        self.durable = durable
        self.batch = batch
        self.poll_timeout = poll_timeout
        self.conn = store.connect(db_path)
        self.stored = 0
        self.duplicates = 0
        self.malformed = 0
        self._stop = asyncio.Event()

    # -- lifecycle ------------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    # -- main loop ------------------------------------------------------------

    async def run(self, max_events: int | None = None) -> dict[str, int]:
        nc = await bus.connect(self.nats_url, name="bridge-collector")
        try:
            await bus.ensure_stream(nc, self.stream, self.wildcard)
            js = nc.jetstream()
            psub = await js.pull_subscribe(
                self.wildcard, durable=self.durable, stream=self.stream
            )
            while not self._stop.is_set():
                try:
                    msgs = await psub.fetch(batch=self.batch, timeout=self.poll_timeout)
                except asyncio.TimeoutError:
                    msgs = []
                except Exception:  # noqa: BLE001 — reconnect window; retry next loop
                    if self._stop.is_set():
                        break
                    await asyncio.sleep(1.0)
                    continue
                for msg in msgs:
                    self._handle(msg)
                    if max_events is not None and self.stored >= max_events:
                        self._stop.set()
                        break
                if max_events is not None and self.stored >= max_events:
                    break
        finally:
            try:
                await nc.drain()
            except Exception:  # noqa: BLE001
                pass
            self.conn.close()
        return self.stats()

    def _handle(self, msg: Any) -> None:
        try:
            event = json.loads(msg.data.decode("utf-8"))
            if not isinstance(event, dict) or "event_id" not in event:
                raise ValueError("not a bridge event")
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            self.malformed += 1
            msg.ack()  # never replay poison messages
            return
        try:
            if store.store_event(self.conn, event):
                self.conn.commit()
                self.stored += 1
            else:
                self.duplicates += 1
            msg.ack()
        except Exception:  # noqa: BLE001 — storage trouble: redeliver later
            try:
                msg.nak(delay=2000)
            except Exception:  # noqa: BLE001
                pass

    def stats(self) -> dict[str, int]:
        return {
            "stored": self.stored,
            "duplicates": self.duplicates,
            "malformed": self.malformed,
        }


def run(
    nats_url: str,
    *,
    stream: str = bus.DEFAULT_STREAM,
    wildcard: str = bus.DEFAULT_WILDCARD,
    db_path=None,
    max_events: int | None = None,
    install_signals: bool = True,
) -> dict[str, int]:
    """Run the collector until interrupted (or until ``max_events`` stored)."""

    async def _main() -> dict[str, int]:
        collector = Collector(nats_url, stream=stream, wildcard=wildcard, db_path=db_path)
        if install_signals:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, collector.stop)
                except NotImplementedError:  # pragma: no cover
                    pass
        return await collector.run(max_events=max_events)

    return asyncio.run(_main())
