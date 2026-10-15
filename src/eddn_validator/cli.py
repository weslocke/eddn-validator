"""Command line entry point: eddn-validator <command>."""

from __future__ import annotations

import argparse
import logging
import os
from datetime import date, datetime, timedelta, timezone

from . import db, partitions
from .config import Settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="eddn-validator")
    parser.add_argument("--env-file", metavar="PATH",
                        help="read settings from this .env file (for running outside Docker)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("bootstrap", help="create roles and database (admin login), then migrate")
    sub.add_parser("migrate", help="apply pending migrations and create partitions")
    p_listen = sub.add_parser("listener", help="subscribe to the EDDN relay and store messages")
    p_listen.add_argument("--dry-run", action="store_true",
                          help="decode and classify only; do not touch the database")
    p_listen.add_argument("--seconds", type=float, help="stop after this many seconds")
    p_import = sub.add_parser("import-sqlite",
                              help="import the earlier listener's SQLite file (resumable)")
    p_import.add_argument("path", help="path to eddn.sqlite")
    p_web = sub.add_parser("outrider-web", help="serve the private ED Outrider usage page")
    p_web.add_argument("--host", default="0.0.0.0")
    p_web.add_argument("--port", type=int, default=8026)
    p_site = sub.add_parser("web", help="serve the public scorecard site")
    p_site.add_argument("--host", default="0.0.0.0")
    p_site.add_argument("--port", type=int, default=8027)
    p_mine = sub.add_parser("mark-mine",
                            help="mark ED Outrider messages as yours using Outrider's outbox")
    p_mine.add_argument("--outbox", default=os.environ.get("OUTRIDER_OUTBOX"),
                        help="path to ed_outrider.sqlite (default: $OUTRIDER_OUTBOX)")
    p_report = sub.add_parser("rules-report", help="documented-rule findings per app (console)")
    p_report.add_argument("--hours", type=int, default=24)
    p_report.add_argument("--per-bucket", type=int, default=500,
                          help="newest messages sampled per app and event type")
    p_report.add_argument("--app", help="only apps whose name contains this")
    p_card = sub.add_parser("scorecard", help="score one app version now and print a summary")
    p_card.add_argument("app", help="exact softwareName")
    p_card.add_argument("version")
    p_arch = sub.add_parser("import-archive",
                            help="import history from edgalaxydata.space/EDDN (resumable)")
    p_arch.add_argument("--since", type=date.fromisoformat,
                        help="first day, YYYY-MM-DD (default: 180 days ago)")
    p_arch.add_argument("--until", type=date.fromisoformat,
                        help="last day, YYYY-MM-DD (default: yesterday)")
    p_arch.add_argument("--workers", type=int, default=6, help="files imported in parallel")
    p_arch.add_argument("--no-test", action="store_true", help="skip Test-* (/test schema) files")
    p_arch.add_argument("--reference", action="store_true",
                        help="second pass: store the reference apps' own messages (needs"
                             " --until before today)")
    p_arch.add_argument("--category", action="append",
                        help="only this archive category (repeatable), e.g. Journal.FSDJump")
    sub.add_parser("backfill-station-systems",
                   help="fill each station's system from stored reference-app messages")
    p_follow = sub.add_parser("followups", help="one-off: finish the 2026-10 backfill follow-ups"
                                                " unattended (see followups.py)")
    p_follow.add_argument("--from", dest="start", default="wait",
                          choices=("wait", "backfill", "import-13", "repair", "rebuild", "measure",
                                   "import-14", "final"),
                          help="step to start from (resume after a stop)")
    p_missing = sub.add_parser("import-missing-facts",
                               help="read older archive days and store reference facts only for"
                                    " places no reference fact covers; then re-pair, rebuild"
                                    " other apps' cards and measure the gap (DESIGN §24)")
    p_missing.add_argument("--since", type=date.fromisoformat, required=True)
    p_missing.add_argument("--until", type=date.fromisoformat, required=True)
    p_missing.add_argument("--workers", type=int, default=6)
    sub.add_parser("pin-facts", help="pin every fact other apps' messages on file report, so"
                                     " retention keeps it")
    sub.add_parser("analyze-partitions", help="planner statistics on every messages partition,"
                                              " then ANALYZE each (one at a time)")
    sub.add_parser("create-indexes", help="build later-added indexes without blocking writers")
    sub.add_parser("pair-backlog", help="pair every unpaired message now, without the worker's"
                                        " per-run time limit")
    p_worker = sub.add_parser("worker", help="run the scheduled maintenance jobs")
    p_worker.add_argument("--once", metavar="JOB",
                          help="run one job (partitions, releases, retention, pairing, profiles,"
                               " scorecards) and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.env_file:
        load_env_file(args.env_file)
    settings = Settings.from_env()

    if args.command == "bootstrap":
        from .bootstrap import bootstrap
        bootstrap(settings)
        _migrate(settings)
    elif args.command == "migrate":
        _migrate(settings)
    elif args.command == "listener":
        from .listener import Listener
        Listener(settings, dry_run=args.dry_run).run(seconds=args.seconds)
    elif args.command == "import-sqlite":
        from .import_sqlite import import_sqlite
        _migrate(settings)
        import_sqlite(settings, args.path)
    elif args.command == "outrider-web":
        import uvicorn
        uvicorn.run("eddn_validator.outrider_web:app", host=args.host, port=args.port,
                    proxy_headers=False, access_log=False)
    elif args.command == "web":
        import uvicorn
        uvicorn.run("eddn_validator.web:app", host=args.host, port=args.port,
                    proxy_headers=True, forwarded_allow_ips="*", access_log=False)
    elif args.command == "mark-mine":
        from .mark_mine import mark_mine
        outbox = args.outbox or os.environ.get("OUTRIDER_OUTBOX")
        if not outbox:
            raise SystemExit("give --outbox or set OUTRIDER_OUTBOX")
        mark_mine(settings, outbox)
    elif args.command == "rules-report":
        from .report import rules_report
        rules_report(settings, hours=args.hours, per_bucket=args.per_bucket, app=args.app)
    elif args.command == "scorecard":
        from .scorer import score_one
        from .summary import print_card
        with db.connect(settings, application_name="scorecard") as conn:
            cards = score_one(conn, settings, args.app, args.version)
        if not cards:
            raise SystemExit(f"no messages from {args.app} {args.version} on file")
        print_card(next(iter(cards.values())))
    elif args.command == "import-archive":
        from .import_archive import import_archive
        _migrate(settings)
        today = datetime.now(timezone.utc).date()
        import_archive(settings, since=args.since or today - timedelta(days=180),
                       until=args.until or today - timedelta(days=1), workers=args.workers,
                       include_test=not args.no_test,
                       categories=set(args.category) if args.category else None,
                       reference=args.reference)
    elif args.command == "backfill-station-systems":
        from .facts import backfill_station_systems
        _migrate(settings)
        with db.connect(settings, application_name="backfill-station-systems") as conn:
            n = backfill_station_systems(conn)
        logging.getLogger(__name__).info("station systems filled for %d stations", n)
    elif args.command == "create-indexes":
        from .indexes import create_indexes
        with db.connect(settings, application_name="create-indexes") as conn:
            create_indexes(conn)
    elif args.command == "import-missing-facts":
        from .missing_facts import run as run_missing
        run_missing(settings, args.since, args.until, args.workers)
    elif args.command == "pin-facts":
        from .missing_facts import pin_reported
        pin_reported(settings)
    elif args.command == "analyze-partitions":
        from .indexes import analyze_partitions
        with db.connect(settings, application_name="analyze-partitions") as conn:
            analyze_partitions(conn)
    elif args.command == "pair-backlog":
        from . import pairing
        with db.connect(settings, application_name="pair-backlog") as conn:
            pairing.run(conn, max_seconds=24 * 3600, wait=True)
    elif args.command == "followups":
        from .followups import run as run_followups
        run_followups(settings, start=args.start)
    elif args.command == "worker":
        from . import worker
        worker.run(settings, once=args.once)


def load_env_file(path: str) -> None:
    """KEY=VALUE lines, as docker compose reads .env. Variables already set win."""
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            os.environ.setdefault(key, value)
    root = os.path.dirname(os.path.abspath(path))
    os.environ.setdefault("EDDN_ROOT", root)


def _migrate(settings: Settings) -> None:
    from .migrate import migrate
    with db.connect(settings, application_name="migrate") as conn:
        migrate(conn, settings.migrations_dir)
        partitions.ensure_partitions(conn, int(settings.retention["partitions_ahead_days"]))


if __name__ == "__main__":
    main()
