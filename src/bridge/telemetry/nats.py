"""NATS plumbing shared by the reporter and the collector.

The bus is the one the lab already runs (fleet-bus, ``nats://…:4222``,
JetStream); bridge gets its own stream (``BRIDGE``) covering ``bridge.>``
alongside the existing ``LAB`` stream. Stream creation is idempotent and done
by whoever connects first (reporter or collector).

Delivery guarantees, end to end:

* publisher -> stream: **at-least-once**. ``js.publish`` only returns once the
  stream has durably stored the message (that is the "durable receipt"). If
  the ack does not arrive, the event goes to the local outbox and is retried;
  ``event_id`` makes retries harmless downstream.
* stream -> collector: **at-least-once** (durable consumer, explicit acks).
  The collector store keys on ``event_id`` and ignores duplicates, so its
  table converges to exactly-once *effect*.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

DEFAULT_STREAM = "BRIDGE"
DEFAULT_WILDCARD = "bridge.>"
CONNECT_TIMEOUT = 2.0
PUBLISH_TIMEOUT = 2.0

_MAX_AGE = timedelta(days=30)
_MAX_BYTES = 1 << 30  # 1 GiB, same shape as the LAB stream


def _cfg_kwargs(name: str, wildcard: str) -> dict:
    from nats.js.api import RetentionPolicy, StorageType

    return dict(
        name=name,
        subjects=[wildcard],
        storage=StorageType.File,
        retention=RetentionPolicy.limits,
        max_age=_MAX_AGE,
        max_bytes=_MAX_BYTES,
        max_msgs_per_subject=-1,
    )


async def ensure_stream(nc, stream: str, wildcard: str) -> None:
    jsm = nc.jetstream()
    try:
        await jsm.streams.info(stream)
        return
    except Exception:
        pass
    try:
        await jsm.add_stream(**_cfg_kwargs(stream, wildcard))
    except Exception:
        # Lost a race with another process, or an existing stream with a
        # different config: only accept it if the stream is really there.
        await jsm.streams.info(stream)


async def publish_async(
    url: str,
    subject: str,
    payload: bytes,
    *,
    stream: str = DEFAULT_STREAM,
    wildcard: str = DEFAULT_WILDCARD,
    timeout: float = PUBLISH_TIMEOUT,
) -> None:
    """Publish one message and wait for the JetStream ack (durable receipt)."""
    import nats

    nc = await nats.connect(url, connect_timeout=timeout, name="bridge-reporter")
    try:
        await _ensure_stream(nc, stream, wildcard)
        import json
        try:
            event_id = str(json.loads(payload).get("event_id", ""))
        except (ValueError, AttributeError, TypeError):
            event_id = ""
        headers = {"Nats-Msg-Id": event_id} if event_id else None
        await nc.jetstream().publish(subject, payload, timeout=timeout, headers=headers)
    finally:
        await nc.drain()


def publish(url: str, subject: str, payload: bytes, **kw) -> None:
    """Synchronous wrapper: one short-lived connection per publish.

    Deliberately simple — bridge emits a handful of events per session, not
    thousands per second — and a failed connection *is* the outage signal the
    reporter turns into an outbox write.
    """
    asyncio.run(publish_async(url, subject, payload, **kw))


async def connect(url: str, *, timeout: float = CONNECT_TIMEOUT, name: str = "bridge"):
    import nats

    return await nats.connect(url, connect_timeout=timeout, name=name)
