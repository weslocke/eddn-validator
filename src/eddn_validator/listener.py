"""Subscribe to the EDDN relay and store every message.

Two threads: the receiver only pulls frames off the ZeroMQ socket into a
bounded queue, so the relay never sees a slow subscriber (it drops messages
past its high-water mark). The writer decodes, classifies and COPYs batches
into Postgres, retrying while the database is unreachable.
"""

from __future__ import annotations

import logging
import queue
import signal
import threading
import time
import zlib
from datetime import datetime, timezone

import psycopg
import zmq

from . import db
from .config import Settings
from .store import Decoded, decode, store

log = logging.getLogger(__name__)

QUEUE_MAX = 200_000       # frames buffered while the database is slow or down
BATCH_MAX = 2000
BATCH_WAIT = 0.5          # seconds to gather a batch after its first frame
SILENCE_RECONNECT = 300   # seconds without a frame before reconnecting
STATS_EVERY = 60


PENDING_STATS_MAX = 1440   # a day of minutes kept while the database is unreachable


class Counters:
    FIELDS = ("received", "written", "dropped", "parse_errors", "db_errors")

    def __init__(self):
        self._lock = threading.Lock()
        self._values = dict.fromkeys(self.FIELDS, 0)

    def add(self, **amounts: int) -> None:
        with self._lock:
            for key, n in amounts.items():
                self._values[key] += n

    def take(self) -> dict[str, int]:
        with self._lock:
            snapshot, self._values = self._values, dict.fromkeys(self.FIELDS, 0)
        return snapshot


class Listener:
    def __init__(self, settings: Settings, *, dry_run: bool = False):
        self.settings = settings
        self.refs = settings.reference_apps
        self.dry_run = dry_run
        self.queue: queue.Queue[tuple[float, bytes]] = queue.Queue(QUEUE_MAX)
        self.stop = threading.Event()
        self.counters = Counters()
        self._last_stats = time.monotonic()
        self._pending_stats: list[dict] = []

    # ---------------------------------------------------------------- receive

    def receive(self) -> None:
        ctx = zmq.Context.instance()
        while not self.stop.is_set():
            try:
                self._receive_once(ctx)
            except Exception:
                # Any socket error: log it and reconnect, never leave the listener running
                # with no intake.
                log.exception("relay connection failed; reconnecting in 5 s")
                self.stop.wait(5)

    def _receive_once(self, ctx: zmq.Context) -> None:
        sock = ctx.socket(zmq.SUB)
        try:
            sock.setsockopt(zmq.SUBSCRIBE, b"")
            sock.setsockopt(zmq.RCVHWM, 100_000)
            sock.setsockopt(zmq.LINGER, 0)
            sock.setsockopt(zmq.TCP_KEEPALIVE, 1)
            sock.setsockopt(zmq.TCP_KEEPALIVE_IDLE, 60)
            sock.setsockopt(zmq.TCP_KEEPALIVE_INTVL, 15)
            sock.setsockopt(zmq.TCP_KEEPALIVE_CNT, 4)
            sock.connect(self.settings.relay)
            log.info("subscribed to %s", self.settings.relay)
            last = time.monotonic()
            while not self.stop.is_set():
                if sock.poll(1000):
                    frame = sock.recv()
                    last = time.monotonic()
                    try:
                        self.queue.put_nowait((time.time(), frame))
                        self.counters.add(received=1)
                    except queue.Full:
                        self.counters.add(received=1, dropped=1)
                elif time.monotonic() - last > SILENCE_RECONNECT:
                    log.warning("no messages for %ds, reconnecting", SILENCE_RECONNECT)
                    break
        finally:
            sock.close()

    # ------------------------------------------------------------------ write

    def write(self) -> None:
        conn: psycopg.Connection | None = None
        pending: list[Decoded] = []
        backoff = 1.0
        while True:
            if not pending:
                pending = self._next_batch()
                if not pending and self.stop.is_set() and self.queue.empty():
                    break
            if pending:
                if self.dry_run:
                    self.counters.add(written=len(pending))
                    pending = []
                else:
                    try:
                        if conn is None or conn.closed:
                            conn = db.connect(self.settings, application_name="listener")
                            log.info("connected to database")
                        self._store(conn, pending)
                        pending = []
                        backoff = 1.0
                    except psycopg.OperationalError as exc:
                        self.counters.add(db_errors=1)
                        log.error("database unavailable (%s); retrying in %.0fs, %d queued",
                                  exc, backoff, self.queue.qsize() + len(pending))
                        if conn is not None:
                            conn.close()
                        conn = None
                        if self.stop.is_set():
                            self.counters.add(dropped=len(pending) + self.queue.qsize())
                            break
                        self.stop.wait(backoff)
                        backoff = min(backoff * 2, 60)
                    except Exception:
                        # Anything else is about this batch, not the database: drop the batch
                        # and keep ingesting rather than letting the writer thread die.
                        log.exception("batch of %d messages could not be stored; skipped",
                                      len(pending))
                        self.counters.add(parse_errors=len(pending))
                        pending = []
                        if conn is not None and conn.info.transaction_status != 0:
                            conn.rollback()
            self._maybe_report(conn)
        self._maybe_report(conn, force=True)
        if conn is not None:
            conn.close()

    def _next_batch(self) -> list[Decoded]:
        try:
            return self._collect_batch()
        except Exception:
            log.exception("could not decode a batch; skipped")
            return []

    def _collect_batch(self) -> list[Decoded]:
        try:
            items = [self.queue.get(timeout=1.0)]
        except queue.Empty:
            return []
        deadline = time.monotonic() + BATCH_WAIT
        while len(items) < BATCH_MAX:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                items.append(self.queue.get(timeout=remaining))
            except queue.Empty:
                break
        batch = []
        for received, frame in items:
            decoded = self._decode(received, frame)
            if decoded is not None:
                batch.append(decoded)
        return batch

    def _decode(self, received: float, frame: bytes) -> Decoded | None:
        try:
            text = zlib.decompress(frame).decode("utf-8")
            return decode(text, datetime.fromtimestamp(received, timezone.utc), self.refs)
        except (zlib.error, UnicodeDecodeError, ValueError, OverflowError, RecursionError) as exc:
            self.counters.add(parse_errors=1)
            log.debug("undecodable frame: %s", exc)
            return None

    def _store(self, conn: psycopg.Connection, batch: list[Decoded]) -> None:
        written, unstorable = store(conn, batch)
        self.counters.add(written=written, parse_errors=unstorable)

    # ------------------------------------------------------------------ stats

    def _maybe_report(self, conn: psycopg.Connection | None, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_stats < STATS_EVERY:
            return
        self._last_stats = now
        stats = self.counters.take()
        log.info("received=%d written=%d dropped=%d parse_errors=%d db_errors=%d queued=%d",
                 stats["received"], stats["written"], stats["dropped"],
                 stats["parse_errors"], stats["db_errors"], self.queue.qsize())
        if self.dry_run:
            return
        # Kept under their own minute until written: the minutes of a database outage are
        # exactly the ones ingest_stats exists to show.
        minute = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        self._pending_stats.append({**stats, "minute": minute})
        del self._pending_stats[:-PENDING_STATS_MAX]
        if conn is None or conn.closed:
            return
        try:
            with conn.transaction(), conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO ingest_stats AS s
                           (minute, received, written, dropped, parse_errors, db_errors)
                    VALUES (%(minute)s, %(received)s, %(written)s,
                            %(dropped)s, %(parse_errors)s, %(db_errors)s)
                    ON CONFLICT (minute) DO UPDATE
                       SET received = s.received + EXCLUDED.received,
                           written = s.written + EXCLUDED.written,
                           dropped = s.dropped + EXCLUDED.dropped,
                           parse_errors = s.parse_errors + EXCLUDED.parse_errors,
                           db_errors = s.db_errors + EXCLUDED.db_errors
                    """,
                    self._pending_stats,
                )
            self._pending_stats.clear()
        except psycopg.Error as exc:
            log.warning("could not record ingest stats (%d minutes kept for later): %s",
                        len(self._pending_stats), exc)

    # -------------------------------------------------------------------- run

    def run(self, seconds: float | None = None) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop.set())
        receiver = threading.Thread(target=self.receive, name="receiver", daemon=True)
        writer = threading.Thread(target=self.write, name="writer")
        receiver.start()
        writer.start()
        deadline = None if seconds is None else time.monotonic() + seconds
        while writer.is_alive() and receiver.is_alive() and not self.stop.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                self.stop.set()
            time.sleep(0.5)
        receiver_died = not receiver.is_alive() and not self.stop.is_set()
        log.info("stopping; flushing %d queued messages", self.queue.qsize())
        self.stop.set()
        writer.join(timeout=25)
        receiver.join(timeout=3)
        if receiver_died:
            # Exit non-zero so the container's restart policy brings the listener back.
            raise SystemExit("the relay receiver stopped unexpectedly")
