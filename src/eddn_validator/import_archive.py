"""Import EDDN history from the daily archive at https://edgalaxydata.space/EDDN/.

The archive holds one bzip2 JSON-lines file per category per day
(`<YYYY-MM>/<Category>-<YYYY-MM-DD>.jsonl.bz2`), each line a relayed envelope
exactly as the live listener receives it. Files are streamed and decompressed
in memory; nothing is written to disk.

What is kept:
  * messages from non-reference applications, stored like live traffic;
  * from reference applications, only their static facts (systems, bodies,
    stations) for pairing: their messages themselves would be removed again at
    once by retention (newest 3000 per event type).

Only gaps in the data already held are filled: everything before the first
stored message, and the stretch between the earlier listener narrowing to ED
Outrider only and the live listener starting (minus ED Outrider, already held).

Each file is imported in one transaction and recorded in archive_imports, so a
re-run skips finished files and redoes an interrupted one. While an import runs
the pairing job pauses (job_state 'archive_import_heartbeat'); during the second
(reference) pass only the size guard pauses ('archive_reference_heartbeat').
"""

from __future__ import annotations

import bz2
import json
import logging
import multiprocessing as mp
import re
import time
import urllib.request
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import psycopg

from . import db, facts, partitions
from .classify import game_branch, parse_ts
from .config import Settings
from .outrider import OUTRIDER
from .store import add_version_counts, copy_rows, decode, register_apps, version_counts

log = logging.getLogger(__name__)

BASE = "https://edgalaxydata.space/EDDN/"
FILE_RE = re.compile(r'href="((?P<cat>[^"/]+)-(?P<day>\d{4}-\d{2}-\d{2})\.jsonl\.bz2)"')
NAME_RE = re.compile(r'"softwareName"\s*:\s*"((?:[^"\\]|\\.)*)"')
GW_RE = re.compile(r'"gatewayTimestamp"\s*:\s*"([^"]+)"')
# Reference-app messages in these categories feed the fact tables; in the rest
# they are skipped without being parsed.
FACT_CATEGORIES = {"Journal.FSDJump", "Journal.Location", "Journal.CarrierJump",
                   "Journal.Scan", "Journal.Docked"}
FACTS_FLUSH = 50_000
REFERENCE_CHUNK = 20_000   # rows per COPY in reference mode (big files stay in bounded memory)
HEARTBEAT = "archive_import_heartbeat"            # first pass: pauses pairing and the size guard
REFERENCE_HEARTBEAT = "archive_reference_heartbeat"  # second pass: pauses only the size guard
USER_AGENT = "eddn-validator archive import (https://github.com/EDCD/EDDN consumer)"


@dataclass(frozen=True)
class Window:
    """Where the database already has full traffic, so the archive must not add it again."""
    full_start: datetime | None        # first stored message
    full_end: datetime | None          # last non-Outrider message before the live listener
    outrider_end: datetime | None      # last Outrider message before the live listener
    live_start: datetime | None        # continuous collection began

    def wanted(self, gw_ts: datetime, name: str) -> bool:
        if self.full_start is None or gw_ts < self.full_start:
            return True
        if self.live_start is None or gw_ts >= self.live_start:
            return False
        if name == OUTRIDER:
            return self.outrider_end is not None and gw_ts > self.outrider_end
        return self.full_end is not None and gw_ts > self.full_end


def coverage(conn: psycopg.Connection) -> Window:
    """Find the stretches of full traffic already held.

    The live listener records per-minute counts (ingest_stats) from when it
    started. Before that, full traffic came from the earlier listener's file,
    which narrowed to ED Outrider only at some point. full_end is the last
    non-Outrider message clearly before the live listener (10 minutes' margin, since
    the listener's first messages precede its first per-minute record), and
    live_start is the first non-Outrider message after that gap.

    Rows this importer stored are ignored (their received_at equals gw_ts, as the
    archive has no receive time), so the window stays the same across re-runs;
    finished files are skipped through archive_imports instead.
    """
    stats_start = conn.execute("SELECT min(minute) FROM ingest_stats").fetchone()[0]
    full_start = conn.execute(
        "SELECT min(gw_ts) FROM messages WHERE received_at <> gw_ts").fetchone()[0]
    if stats_start is None:
        return Window(full_start, None, None, None)
    full_end, outrider_end = conn.execute(
        """
        SELECT max(m.gw_ts) FILTER (WHERE a.software_name <> %s),
               max(m.gw_ts) FILTER (WHERE a.software_name = %s)
          FROM messages m JOIN apps a ON a.id = m.app_id
         WHERE m.gw_ts < %s - interval '10 minutes'
           AND m.gw_ts > %s - interval '30 days'
           AND m.received_at <> m.gw_ts
        """, (OUTRIDER, OUTRIDER, stats_start, stats_start)).fetchone()
    live_start = conn.execute(
        """
        SELECT min(m.gw_ts) FROM messages m JOIN apps a ON a.id = m.app_id
         WHERE a.software_name <> %s AND m.gw_ts > %s AND m.received_at <> m.gw_ts
        """, (OUTRIDER, full_end or stats_start)).fetchone()[0] if full_end else stats_start
    return Window(full_start, full_end, outrider_end, live_start or stats_start)


def _get_listing(url: str, attempts: int = 5) -> str:
    """A month's directory listing, retried: one network blip must not end an import."""
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read().decode("utf-8", "replace")
        except OSError as exc:
            if attempt == attempts - 1:
                raise
            log.warning("listing %s failed (%s); retrying", url, exc)
            time.sleep(30 * (attempt + 1))
    raise AssertionError("unreachable")


def list_files(since: date, until: date, include_test: bool) -> list[tuple[str, str, date]]:
    """(url, category, day) for every archive file with since <= day <= until."""
    files = []
    month = date(since.year, since.month, 1)
    while month <= until:
        url = f"{BASE}{month:%Y-%m}/"
        listing = _get_listing(url)
        for m in FILE_RE.finditer(listing):
            day = date.fromisoformat(m["day"])
            if since <= day <= until and (include_test or not m["cat"].startswith("Test-")):
                files.append((url + m.group(1), m["cat"], day))
        month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
    return sorted(set(files), key=lambda f: (f[2], f[1]))


def newest_first(files: list[tuple[str, str, date]]) -> list[tuple[str, str, date]]:
    return sorted(files, key=lambda f: (f[2], f[1]), reverse=True)


class IncompleteDownload(OSError):
    """The connection closed before the whole file arrived."""


def stream_lines(url: str):
    """Lines of a (possibly multi-stream) bzip2 file, decompressed as it downloads.

    Raises IncompleteDownload if the body is shorter than announced or the bzip2 stream
    is cut off: http.client returns a short body silently when the server closes early,
    and the file must then be retried, never recorded as imported.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    decompressor = bz2.BZ2Decompressor()
    pending = b""
    received = 0
    with urllib.request.urlopen(request, timeout=120) as response:
        announced = response.getheader("Content-Length")
        while chunk := response.read(1 << 20):
            received += len(chunk)
            data = b""
            while chunk:
                if decompressor.eof:
                    # A new bzip2 stream starts here (concatenated file), possibly exactly at
                    # a read boundary: start a fresh decompressor for it.
                    decompressor = bz2.BZ2Decompressor()
                data += decompressor.decompress(chunk)
                chunk = decompressor.unused_data if decompressor.eof else b""
            pending += data
            *lines, pending = pending.split(b"\n")
            yield from lines
    if announced and announced.isdigit() and received != int(announced):
        raise IncompleteDownload(f"received {received} of {announced} bytes")
    if received and not decompressor.eof:
        raise IncompleteDownload(f"bzip2 stream cut off after {received} bytes")
    if pending.strip():
        yield pending


def _software_name(m: re.Match | None) -> str:
    """The softwareName captured by NAME_RE, unescaped; "" if the escape is malformed."""
    if not m:
        return ""
    try:
        return json.loads(f'"{m.group(1)}"')
    except ValueError:
        return ""


# ----------------------------------------------------------------- worker

_settings: Settings | None = None
_window: Window | None = None
_facts_lock = None


def _init(settings: Settings, window: Window, lock) -> None:
    global _settings, _window, _facts_lock
    _settings, _window, _facts_lock = settings, window, lock
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _flush_facts(conn: psycopg.Connection, batch: facts.Batch) -> None:
    if not (batch.systems or batch.bodies or batch.stations):
        return
    with _facts_lock, conn.transaction():
        facts.store(conn, batch)


def import_file(job: tuple[str, str, date]) -> tuple[str, int, int, int, str | None]:
    """Returns (url, lines, stored, folded into facts, error)."""
    url, category, _ = job
    refs = _settings.reference_apps
    for attempt in range(3):
        lines = stored = folded = 0
        try:
            # Three connections: facts are flushed (committed) as they build up, the file's
            # messages go out in chunks inside one transaction that also records the file as
            # done, and app/version rows are registered in short transactions of their own
            # (held inside the file's transaction they would block the live listener).
            with db.connect(_settings, application_name="import-archive") as conn, \
                    db.connect(_settings, application_name="import-archive") as msg_conn, \
                    db.connect(_settings, application_name="import-archive apps") as apps_conn, \
                    msg_conn.transaction():
                chunk, counts, fact_batch, fact_n = [], {}, facts.Batch(), 0
                for raw in stream_lines(url):
                    if not raw.strip():
                        continue
                    lines += 1
                    text = raw.decode("utf-8", "replace")
                    m = NAME_RE.search(text)
                    name = _software_name(m)
                    group = refs.group_for(name)
                    if group is not None:
                        if category not in FACT_CATEGORIES:
                            continue
                        try:   # one unparseable line must not fail the whole file
                            envelope = json.loads(text)
                            header = envelope.get("header", {}) if isinstance(envelope, dict) else {}
                            message = envelope.get("message") if isinstance(envelope, dict) else None
                            gw = parse_ts(header.get("gatewayTimestamp"))
                            if not (isinstance(message, dict) and gw):
                                continue
                            event = message.get("event")
                            event_type = f"journal/1:{event}" if isinstance(event, str) else "?"
                            fact_batch.add(event_type, message, game_branch(header.get("gameversion")),
                                           "journal", group, gw, None)
                        except (ValueError, TypeError, AttributeError):
                            continue
                        fact_n += 1
                        if fact_n >= FACTS_FLUSH:
                            _flush_facts(conn, fact_batch)
                            folded += fact_n
                            fact_batch, fact_n = facts.Batch(), 0
                        continue
                    g = GW_RE.search(text)
                    gw = parse_ts(g.group(1)) if g else None
                    if gw is None or not _window.wanted(gw, name):
                        continue
                    try:
                        chunk.append(decode(text, gw, refs))
                    except ValueError:
                        continue
                    if len(chunk) >= REFERENCE_CHUNK:
                        stored += _copy_counted(msg_conn, apps_conn, chunk, counts)
                        chunk = []
                _flush_facts(conn, fact_batch)
                folded += fact_n
                if chunk:
                    stored += _copy_counted(msg_conn, apps_conn, chunk, counts)
                msg_conn.execute(
                    "INSERT INTO archive_imports (url, lines, stored, folded)"
                    " VALUES (%s, %s, %s, %s) ON CONFLICT (url) DO UPDATE SET"
                    " lines = EXCLUDED.lines, stored = EXCLUDED.stored,"
                    " folded = EXCLUDED.folded, finished_at = now()",
                    (url, lines, stored, folded))
                # In the file's own transaction, as its last step: the counts commit with the
                # rows or not at all (app_versions rows are locked only for the commit).
                add_version_counts(msg_conn, counts)
            # Committed: from here on nothing may send the file round the retry loop again.
            _beat(HEARTBEAT)
            return url, lines, stored, folded, None
        except Exception as exc:   # network trouble, a bad stream: retry the whole file
            log.warning("%s attempt %d failed: %s", url.rsplit("/", 1)[-1], attempt + 1, exc)
            time.sleep(10 * (attempt + 1))
            error = str(exc)
    return url, lines, stored, folded, error


def import_reference_file(job: tuple[str, str, date]) -> tuple[str, int, int, int, str | None]:
    """Reference mode: store every reference-app message of one file (no facts:
    those were folded in by the first pass). One transaction per file, rows sent in
    chunks; app/version rows registered in short separate transactions."""
    url, _, _ = job
    refs = _settings.reference_apps
    error = None
    for attempt in range(3):
        lines = stored = 0
        try:
            # App/version rows go through a second, autocommit connection: registered inside
            # the file's transaction they would stay locked (and block the live listener)
            # until the whole file commits.
            with db.connect(_settings, application_name="import-archive") as conn, \
                    db.connect(_settings, application_name="import-archive apps") as apps_conn:
                counts: dict[tuple[int, str], int] = {}
                with conn.transaction():
                    chunk: list = []
                    for raw in stream_lines(url):
                        if not raw.strip():
                            continue
                        lines += 1
                        text = raw.decode("utf-8", "replace")
                        m = NAME_RE.search(text)
                        name = _software_name(m)
                        if refs.group_for(name) is None:
                            continue
                        g = GW_RE.search(text)
                        gw = parse_ts(g.group(1)) if g else None
                        if gw is None:
                            continue
                        try:
                            chunk.append(decode(text, gw, refs))
                        except ValueError:
                            continue
                        if len(chunk) >= REFERENCE_CHUNK:
                            stored += _copy_counted(conn, apps_conn, chunk, counts)
                            chunk = []
                    if chunk:
                        stored += _copy_counted(conn, apps_conn, chunk, counts)
                    conn.execute(
                        "INSERT INTO archive_reference_imports (url, lines, stored)"
                        " VALUES (%s, %s, %s) ON CONFLICT (url) DO UPDATE SET"
                        " lines = EXCLUDED.lines, stored = EXCLUDED.stored, finished_at = now()",
                        (url, lines, stored))
                    # Last step of the file's transaction: counts commit with the rows or not at all.
                    add_version_counts(conn, counts)
            # Committed: from here on nothing may send the file round the retry loop again.
            _beat(REFERENCE_HEARTBEAT)
            return url, lines, stored, 0, None
        except Exception as exc:
            log.warning("%s attempt %d failed: %s", url.rsplit("/", 1)[-1], attempt + 1, exc)
            time.sleep(10 * (attempt + 1))
            error = str(exc)
    return url, lines, stored, 0, error


def _beat(name: str) -> None:
    """Refresh an import heartbeat after a file commits. Best effort: a failure here must not
    retry (and so re-import) a file that is already committed."""
    try:
        with db.connect(_settings, application_name="import-archive") as conn:
            conn.execute("INSERT INTO job_state (name, value) VALUES (%s, %s) ON CONFLICT (name)"
                         " DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                         (name, int(time.time())))
    except Exception as exc:
        log.warning("heartbeat %s not refreshed: %s", name, exc)


def _copy_counted(conn, apps_conn, chunk: list, counts: dict) -> int:
    app_ids = register_apps(apps_conn, chunk, count=False)
    for key, n in version_counts(chunk, app_ids).items():
        counts[key] = counts.get(key, 0) + n
    return copy_rows(conn, chunk, app_ids)[0]


# ------------------------------------------------------------------- main

def import_archive(settings: Settings, since: date, until: date, workers: int = 6,
                   include_test: bool = True, categories: set[str] | None = None,
                   reference: bool = False) -> int:
    """Returns the number of files that failed (they stay unrecorded, so a re-run retries them)."""
    if reference and until >= datetime.now(timezone.utc).date():
        raise SystemExit("reference mode replaces the listener's data for whole days:"
                         " --until must be before today (UTC)")
    if reference and (categories or not include_test):
        # The reference pass first deletes every reference-app message for the range; with
        # only some files re-imported the rest would be lost.
        raise SystemExit("--reference re-imports whole days: it cannot be combined with"
                         " --category or --no-test")
    # List the files first: if the archive is unreachable, nothing has been deleted yet.
    listed = list_files(since, until, include_test)
    with db.connect(settings, application_name="import-archive") as conn:
        window = coverage(conn)
        table = "archive_reference_imports" if reference else "archive_imports"
        done = {r[0] for r in conn.execute(f"SELECT url FROM {table}")}
        if reference:
            # The reference apps' messages held for these days are partial (they used to be
            # cut to the newest 3000 per event type); the archive has them all. Remove the
            # partial ones first. Rows this mode imported (received_at = gw_ts) are kept,
            # so a re-run resumes instead of starting over.
            lo = datetime.combine(since, datetime.min.time(), timezone.utc)
            hi = datetime.combine(until + timedelta(days=1), datetime.min.time(), timezone.utc)
            with conn.transaction():
                deleted = conn.execute(
                    """
                    WITH d AS (DELETE FROM messages WHERE is_big3 AND received_at <> gw_ts
                                AND gw_ts >= %s AND gw_ts < %s
                                RETURNING app_id, software_version)
                    SELECT app_id, software_version, count(*) FROM d GROUP BY 1, 2
                    """, (lo, hi)).fetchall()
                # Those rows were counted when the listener stored them; the archive copies that
                # replace them are counted again as they are imported. Same transaction as the
                # DELETE, so the subtraction happens exactly when the deletion does.
                add_version_counts(conn, {(a, v): -n for a, v, n in deleted})
            log.info("reference mode: removed %d partial reference-app messages for %s to %s",
                     sum(n for _, _, n in deleted), since, until)
        partitions.ensure_partitions(conn, int(settings.retention["partitions_ahead_days"]),
                                     since=since - timedelta(days=1))
        heartbeat = REFERENCE_HEARTBEAT if reference else HEARTBEAT
        conn.execute("INSERT INTO job_state (name, value) VALUES (%s, %s) ON CONFLICT (name)"
                     " DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                     (heartbeat, int(time.time())))
    log.info("already held: full traffic from %s; gap %s to %s (Outrider to %s)",
             window.full_start, window.full_end, window.live_start, window.outrider_end)

    files = [f for f in listed if f[0] not in done and (not categories or f[1] in categories)]
    if reference:
        # The partial reference data for the whole range was just removed: refill the most
        # recent days first, since those are what profiles and scorecards read.
        files = newest_first(files)
    log.info("%d archive files to import (%s to %s), %d already done, %d workers",
             len(files), since, until, len(done), workers)
    started = time.monotonic()
    totals = [0, 0, 0]
    failed = []
    ctx = mp.get_context("spawn")
    try:
        # A process pool that reports a killed worker (BrokenProcessPool) instead of waiting
        # for it forever, as multiprocessing.Pool does after an out-of-memory kill.
        with ProcessPoolExecutor(workers, mp_context=ctx, initializer=_init,
                                 initargs=(settings, window, ctx.Lock())) as pool:
            worker = import_reference_file if reference else import_file
            futures = [pool.submit(worker, f) for f in files]
            for i, future in enumerate(as_completed(futures), 1):
                url, lines, stored, folded, error = future.result()
                name = url.rsplit("/", 1)[-1]
                if error:
                    failed.append(name)
                    log.error("%s failed after retries: %s", name, error)
                    continue
                totals[0] += lines
                totals[1] += stored
                totals[2] += folded
                elapsed = time.monotonic() - started
                log.info("[%d/%d] %s: %d lines, %d stored, %d into facts (%.0f min, ~%.0f min left)",
                         i, len(files), name, lines, stored, folded, elapsed / 60,
                         elapsed / i * (len(files) - i) / 60)
    finally:
        with db.connect(settings, application_name="import-archive") as conn:
            conn.execute("DELETE FROM job_state WHERE name = %s", (heartbeat,))
    log.info("archive import finished: %d lines read, %d messages stored, %d reference messages"
             " folded into facts, %d files failed%s", *totals, len(failed),
             f" ({', '.join(failed[:10])})" if failed else "")
    return len(failed)
