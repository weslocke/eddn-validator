"""Scheduled maintenance jobs. Phase 1: partitions, release poll, retention."""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import psycopg

from . import db, github, indexes, outrider, pairing, partitions, profiles, retention, scorer
from .config import Settings
from .rules import load_rules

log = logging.getLogger(__name__)


@dataclass
class Job:
    name: str
    interval: float
    func: Callable[[psycopg.Connection], None]
    next_run: float = 0.0


def jobs(settings: Settings) -> list[Job]:
    cfg = settings.retention
    return [
        Job("partitions", 6 * 3600,
            lambda conn: partitions.ensure_partitions(conn, int(cfg["partitions_ahead_days"]))),
        Job("releases", 6 * 3600,
            lambda conn: github.poll(conn, settings.reference_apps.groups, settings.github_token)),
        Job("retention", 3600, lambda conn: retention.run(conn, cfg)),
        Job("pairing", 300, pairing.run),
        Job("profiles", 3600,
            lambda conn: profiles.build(conn, load_rules(settings.load_yaml("rules.yaml")),
                                        settings.load_yaml("scoring.yaml"))),
        Job("scorecards", 900, lambda conn: scorer.run(conn, settings)),
        Job("analyze", 24 * 3600, indexes.analyze_yesterday),
        # Hourly check; the Outrider page's finished days are worked out once a night after 3 AM.
        Job("outrider_days", 3600, outrider.nightly),
    ]


def run_job(settings: Settings, job: Job) -> bool:
    started = time.monotonic()
    try:
        with db.connect(settings, application_name=f"worker {job.name}") as conn:
            job.func(conn)
    except Exception:
        log.exception("job %s failed", job.name)
        return False
    log.info("job %s finished in %.1fs", job.name, time.monotonic() - started)
    return True


def run(settings: Settings, once: str | None = None) -> None:
    all_jobs = jobs(settings)
    if once:
        selected = [j for j in all_jobs if j.name == once]
        if not selected:
            raise SystemExit(f"unknown job {once!r}; choose from "
                             f"{', '.join(j.name for j in all_jobs)}")
        if not run_job(settings, selected[0]):
            raise SystemExit(1)
        return

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    log.info("worker started: %s", ", ".join(f"{j.name} every {j.interval / 60:g} min"
                                               for j in all_jobs))
    while not stop.is_set():
        now = time.monotonic()
        for job in all_jobs:
            if stop.is_set():
                break
            if now >= job.next_run:
                ok = run_job(settings, job)
                # Retry a failed job after five minutes rather than a full interval.
                job.next_run = time.monotonic() + (job.interval if ok else 300)
        wait = min(j.next_run for j in all_jobs) - time.monotonic()
        stop.wait(max(wait, 1.0))
    log.info("worker stopped")
