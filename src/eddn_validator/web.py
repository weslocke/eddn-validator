"""Public scorecard site (docs/DESIGN.md §8).

Read-only by construction: it connects as eddn_ro (SELECT on scorecards, apps
and app_versions only) and never computes anything; the worker writes the
cards. The only inputs are an app id, a version that must already have a
card, and one of the fixed ranges. The app picker filters an embedded list in
the browser, so typed text never reaches the server.
"""

from __future__ import annotations

import logging
import re
import secrets
from urllib.parse import quote
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from markupsafe import Markup, escape
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from . import db
from .config import RO_ROLE, Settings
from .scorer import ALL_VERSIONS
from .web_tips import (BEHAVIOUR_TIPS, GAMEVERSION_TIPS, PAIR_TIPS, SEVERITY_TIPS, VALUE_TIPS,
                       annotate)
from .web_tips import STATIC as TIPS

log = logging.getLogger(__name__)

CACHE_SECONDS = 60
settings = Settings.from_env()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
STATIC = Path(__file__).parent / "static"
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.nonce = secrets.token_urlsafe(16)
        response = await call_next(request)
        nonce = request.state.nonce
        response.headers["Content-Security-Policy"] = (
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
            "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers.setdefault("Cache-Control", "public, max-age=300")
        return response


app.add_middleware(SecurityHeaders)


class TTLCache:
    def __init__(self, seconds: float, max_items: int = 2000):
        self.seconds, self.max_items = seconds, max_items
        self._data: dict = {}
        self._lock = threading.Lock()

    def get(self, key, compute):
        now = time.monotonic()
        with self._lock:
            hit = self._data.get(key)
            if hit and now - hit[0] < self.seconds:
                return hit[1]
        value = compute()
        with self._lock:
            if len(self._data) >= self.max_items:
                self._data.clear()
            self._data[key] = (now, value)
        return value


cache = TTLCache(CACHE_SECONDS)
# Cards and version lists are keyed by sender-chosen URLs: a separate cache, so a flood of
# unknown addresses can only evict other cards, never the shared list pages.
card_cache = TTLCache(CACHE_SECONDS)


def _connect():
    return db.connect(settings, application_name="web", user=RO_ROLE,
                      password=settings.ro_password)


VERSION_RE = re.compile(r"\s*v?(\d+(?:[._-]\d+)*)", re.IGNORECASE)
# Words anywhere after the number; the one-letter forms only right before a number ("1.2.3b4"),
# so a build tag like "build 7b" is not taken for a beta.
PRERELEASE_RE = re.compile(r"(?<![a-z])(dev|snapshot|test|alpha|beta|preview|pre|rc|a(?=\s*\.?\d)|b(?=\s*\.?\d))"
                           r"(?![a-z])", re.IGNORECASE)
PRERELEASE_RANK = {"dev": 0, "snapshot": 0, "test": 0, "alpha": 1, "a": 1, "beta": 2, "b": 2,
                   "preview": 3, "pre": 3, "rc": 4}


def version_key(version: str) -> tuple:
    """Sort key putting higher version numbers first when sorted in reverse: 2026.10.22 above
    2026.10.21, 3.3.0.0 equal to 3.3.0, "5.4.3 beta 1" below 5.4.3 and above "5.4.3 alpha 2".
    Any other suffix ("2.0 March", "1.0-hotfix") ties with the bare number, so the version
    seen first ranks lower (newest_first breaks ties by first seen). Strings with no leading
    number sort below every numbered version."""
    m = VERSION_RE.match(version)
    if not m:
        return (0, (), 0, 0, 0)
    main = [int(x) for x in re.split(r"[._-]", m[1])]
    while len(main) > 1 and main[-1] == 0:
        main.pop()
    rest = version[m.end():]
    pre = PRERELEASE_RE.search(rest)
    if pre:
        number = re.search(r"\d+", rest[pre.end():])
        return (1, tuple(main), 0, PRERELEASE_RANK[pre[1].lower()], int(number[0]) if number else 0)
    return (1, tuple(main), 1, 0, 0)


def newest_first(versions: list[dict]) -> list[dict]:
    """Highest version number first; first seen breaks ties (and orders unnumbered versions)."""
    return sorted(versions, key=lambda v: (version_key(v["version"]), v["first_seen"]), reverse=True)


def shown_version(versions: list[dict]) -> dict | None:
    """The version an app list shows: the newest one with enough messages to be graded, else
    the newest with any scorecard, else the newest seen. versions: newest first."""
    return (next((v for v in versions if v["has_card"] and v["grade"] is not None), None)
            or next((v for v in versions if v["has_card"]), None)
            or (versions[0] if versions else None))


def _versions_by_app(conn, app_ids: list[int] | None = None) -> dict[int, list[dict]]:
    """Every version of the given apps (all apps if None), newest first, each with its card's
    grade and score where it has a card."""
    rows = conn.execute(
        """
        SELECT v.app_id, v.version, v.first_seen, v.last_seen, v.msg_count,
               c.version IS NOT NULL, c.grade, c.score, c.messages, c.weak_spots
          FROM app_versions v
          LEFT JOIN LATERAL (
                SELECT s.version, s.grade, s.score, s.messages, s.weak_spots FROM scorecards s
                 WHERE s.app_id = v.app_id AND s.version = v.version
                 ORDER BY s.range_days DESC LIMIT 1) c ON true
         WHERE %s::int[] IS NULL OR v.app_id = ANY(%s::int[])
        """, (app_ids, app_ids)).fetchall()
    out: dict[int, list[dict]] = {}
    for app_id, version, first, last, n, has_card, grade, score, card_n, weak in rows:
        out.setdefault(app_id, []).append({
            "version": version, "first_seen": first, "last_seen": last, "messages": n,
            "has_card": has_card, "grade": grade, "score": score, "card_messages": card_n,
            "weak_spots": weak or []})
    return {app_id: newest_first(vs) for app_id, vs in out.items()}


def load_apps() -> list[dict]:
    with _connect() as conn:
        apps = conn.execute("SELECT id, software_name, ref_group FROM apps"
                            " ORDER BY lower(software_name), software_name").fetchall()
        versions = _versions_by_app(conn)
    out = []
    for app_id, name, group in apps:
        carded = [{"v": v["version"], "last": v["last_seen"].isoformat(), "g": v["grade"],
                   "s": v["score"], "n": v["card_messages"], "w": len(v["weak_spots"])}
                  for v in versions.get(app_id, []) if v["has_card"]]
        if carded:
            out.append({"id": app_id, "name": name, "ref": group, "versions": carded})
    return out


NEW_APP_DAYS = 30


def load_new_apps() -> dict:
    """Applications first seen on EDDN in the last NEW_APP_DAYS days, alphabetically.

    Apps seen during the first day of the data held were already sending when it starts,
    so they never count as new.
    """
    with _connect() as conn:
        data_since = conn.execute("SELECT min(first_seen) FROM apps").fetchone()[0]
        since = datetime.now(timezone.utc) - timedelta(days=NEW_APP_DAYS)
        if data_since is None:
            return {"apps": [], "data_since": None, "since": since}
        rows = conn.execute(
            """
            SELECT a.id, a.software_name, a.first_seen, a.last_seen
              FROM apps a
             WHERE a.ref_group IS NULL
               AND a.first_seen > now() - make_interval(days => %s)
               AND a.first_seen > %s + interval '1 day'
             ORDER BY lower(a.software_name), a.software_name
             LIMIT 200
            """, (NEW_APP_DAYS, data_since)).fetchall()
        versions = _versions_by_app(conn, [r[0] for r in rows])
    apps = []
    for app_id, name, first, last in rows:
        vs = versions.get(app_id)
        if not vs:
            continue
        shown = shown_version(vs)
        apps.append({"id": app_id, "name": name, "first_seen": first, "last_seen": last,
                     "versions": len(vs), "messages": sum(v["messages"] for v in vs),
                     "latest": shown["version"], "has_card": shown["has_card"],
                     "grade": shown["grade"], "score": shown["score"],
                     "weak_spots": shown["weak_spots"], "newest": vs[0]["version"]})
    return {"apps": apps, "data_since": data_since, "since": since}


def load_all_apps() -> list[dict]:
    """Every application, alphabetically, with the version shown_version picks (its newest
    graded version, where there is one) and that version's grade and score."""
    with _connect() as conn:
        apps = conn.execute("SELECT id, software_name, ref_group, last_seen FROM apps"
                            " ORDER BY lower(software_name), software_name").fetchall()
        versions = _versions_by_app(conn)
    recent = datetime.now(timezone.utc) - timedelta(days=180)
    out = []
    for app_id, name, group, last in apps:
        vs = versions.get(app_id, [])
        shown = shown_version(vs)
        graded = shown if shown and shown["has_card"] else None
        out.append({"id": app_id, "name": name, "ref": group, "last_seen": last,
                    "version": graded["version"] if graded else None,
                    "grade": graded["grade"] if graded else None,
                    "score": graded["score"] if graded else None,
                    "weak_spots": graded["weak_spots"] if graded else [],
                    "recent": last is not None and last > recent,
                    "current": vs[0]["version"] if vs else None,
                    # A newest version with nothing left on file will never get a card.
                    "current_on_file": bool(vs) and vs[0]["last_seen"] > recent})
    return out


def split_not_sent(card: dict, others: list[tuple[str, dict, datetime]]) -> None:
    """Split the card's not_sent list: event types an earlier version of the app sent go to
    not_yet (with the highest such version and when it was last seen); the rest, never sent
    by any version with messages on file, stay in not_sent."""
    not_yet, never = [], []
    for item in card.get("not_sent", []):
        senders = [(v, last) for v, types, last in others if item["event_type"] in (types or {})]
        if senders:
            version, last = max(senders, key=lambda s: version_key(s[0]))
            not_yet.append({**item, "version": version, "last_seen": last})
        else:
            never.append(item)
    card["not_sent"], card["not_yet"] = never, not_yet


def load_card(app_id: int, version: str) -> dict | None:
    # One card per version; the widest one wins while cards from the earlier per-range
    # scheme are still being replaced.
    with _connect() as conn:
        row = conn.execute("SELECT card FROM scorecards WHERE app_id = %s AND version = %s"
                           " ORDER BY range_days DESC LIMIT 1", (app_id, version)).fetchone()
        if not row:
            return None
        others = conn.execute(
            "SELECT s.version, s.event_types, v.last_seen FROM scorecards s"
            "  JOIN app_versions v ON v.app_id = s.app_id AND v.version = s.version"
            " WHERE s.app_id = %s AND s.version <> %s", (app_id, version)).fetchall()
        summary = None if version == ALL_VERSIONS else conn.execute(
            "SELECT score, grade, messages, weak_spots, event_types, card->'version_count',"
            " card->>'oldest' FROM scorecards WHERE app_id = %s AND version = %s",
            (app_id, ALL_VERSIONS)).fetchone()
    card = row[0]
    split_not_sent(card, others)
    if summary:
        score, grade, messages, weak, types, count, oldest = summary
        card["all_summary"] = {"score": score, "grade": grade, "messages": messages,
                               "weak_spots": weak or [], "event_types": len(types or {}),
                               "version_count": count, "oldest": oldest}
    return card


def load_versions(app_id: int) -> list[dict]:
    """Every version of an app seen on EDDN, highest version first, with its card's grade.
    Versions with nothing on file (older than retention) have no card."""
    with _connect() as conn:
        return _versions_by_app(conn, [app_id]).get(app_id, [])


def load_annotated_card(app_id: int, version: str) -> dict | None:
    card = load_card(app_id, version)
    return annotate(card) if card else None


def _age(iso: str) -> str:
    then = datetime.fromisoformat(iso)
    minutes = int((datetime.now(timezone.utc) - then).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 90:
        return f"{minutes} min ago"
    return f"{minutes // 60} h ago"


def grade_status(grade: str | None) -> str:
    return {"A": "good", "B": "warning", "C": "warning", "D": "serious", "F": "critical"}.get(
        grade or "", "none")


def score_status(score: float | None) -> str:
    if score is None:
        return "none"
    return "good" if score >= 90 else "warning" if score >= 70 else "serious" if score >= 60 else "critical"


def breakable(text: str) -> Markup:
    """Escape, then allow line breaks after / and : (long event type names on phones)."""
    return Markup(str(escape(text)).replace("/", "/<wbr>").replace(":", ":<wbr>"))


# EDDN documentation cited by config/rules.yaml `source:` (e.g. "commodity-README:48-52",
# "Dev:255-257, 273-275"), linked to the exact lines in the version the rules were written
# from, so the line numbers stay right as the documents change.
EDDN_LIVE = "4ad669bb7bbe1eae080e4c354e786dca4db91f35"       # branch live, 2026-06-17
EDDN_DEVELOP = "15052a271c363e235959604d9ac2d8475a843be3"    # branch develop, adds scanorganic
DOCS = {"Dev": ("docs/Developers.md", "EDDN developers' documentation"),
        "SR": ("schemas/README-EDDN-schemas.md", "EDDN schemas README")}
SOURCE_RE = re.compile(r"\b(Dev|SR|[a-z_]+-README):(\d+(?:-\d+)?(?:, \d+(?:-\d+)?)*)")


def _doc(prefix: str) -> tuple[str, str, str]:
    if prefix in DOCS:
        return (*DOCS[prefix], EDDN_LIVE)
    schema = prefix.removesuffix("-README")
    commit = EDDN_DEVELOP if schema == "scanorganic" else EDDN_LIVE
    return f"schemas/{prefix}.md", f"EDDN {schema} schema README", commit


def weak_tip(spots: list[dict]) -> str:
    """Hover text for a weak-spot flag: each event type below C, with its grade and score."""
    lines = [f"{s['event_type']}{' (' + s['branch'] + ')' if s['branch'] != 'live' else ''}: "
             f"{s['grade']} {s['score']:.1f}, {s['messages']:,} messages" for s in spots]
    return ("Event types scoring below C with enough messages to judge. The grade is weighted by "
            "message count, so a weak low-volume event type barely moves it.\n" + "\n".join(lines))


def source_link(source: str | None) -> dict | None:
    """The first EDDN documentation reference in a rule's source, as {url, tip}; None when the
    source cites no EDDN document (e.g. "implied", "game data")."""
    m = SOURCE_RE.search(source or "")
    if not m:
        return None
    path, title, commit = _doc(m[1])
    first, _, last = m[2].split(", ")[0].partition("-")
    lines = f"line {first}" if not last else f"lines {first}–{last}"
    return {"url": f"https://github.com/EDCD/EDDN/blob/{commit}/{path}?plain=1#L{first}"
                   + (f"-L{last}" if last else ""),
            "tip": f"Where EDDN states this rule: {title}, {lines}. Opens on GitHub."}


def source_links(source: str | None) -> Markup:
    """Escape a rule's source, linking each EDDN documentation reference to its lines."""
    if not source:
        return Markup("")
    out, pos = [], 0
    for m in SOURCE_RE.finditer(source):
        out.append(escape(source[pos:m.start()]))
        path, title, commit = _doc(m[1])
        links = []
        for i, span in enumerate(m[2].split(", ")):
            first, _, last = span.partition("-")
            anchor = f"L{first}" + (f"-L{last}" if last else "")
            lines = f"line {first}" if not last else f"lines {first}–{last}"
            links.append(Markup('<a href="https://github.com/EDCD/EDDN/blob/{}/{}?plain=1#{}" target="_blank"'
                                ' rel="noopener noreferrer" data-tip="{}">{}</a>').format(
                commit, path, anchor, f"{title}, {lines}. Opens on GitHub.",
                f"{m[1]}:{span}" if i == 0 else span))
        out.append(Markup(", ").join(links))
        pos = m.end()
    out.append(escape(source[pos:]))
    return Markup("").join(out)


templates.env.filters["breakable"] = breakable
templates.env.filters["source_links"] = source_links
templates.env.filters["source_link"] = source_link
templates.env.filters["weak_tip"] = weak_tip
templates.env.filters["age"] = _age
templates.env.filters["day"] = lambda d: d.astimezone(timezone.utc).strftime("%Y-%m-%d") if d else "–"
templates.env.filters["when"] = lambda d: d.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if d else "–"
templates.env.filters["pct"] = lambda x: "–" if x is None else f"{100 * float(x):.1f}%"
templates.env.filters["n"] = lambda x: "–" if x is None else f"{int(x):,}"
templates.env.globals.update(grade_status=grade_status, score_status=score_status,
                             TIPS=TIPS, GV_TIPS=GAMEVERSION_TIPS, SEV_TIPS=SEVERITY_TIPS,
                             VALUE_TIPS=VALUE_TIPS, PAIR_TIPS=PAIR_TIPS,
                             BEHAVIOUR_TIPS=BEHAVIOUR_TIPS)


@app.get("/", response_class=HTMLResponse)
def picker(request: Request):
    apps = cache.get("apps", load_apps)
    new = cache.get("new_apps", load_new_apps)
    return templates.TemplateResponse(request, "picker.html", {
        "apps": apps, "new": new, "new_days": NEW_APP_DAYS, "nonce": request.state.nonce})


def _valid_version(v: str) -> bool:
    return len(v) <= 64 and "\x00" not in v


@app.get("/card/{app_id}", response_class=HTMLResponse)
def card(request: Request, app_id: int, v: str):
    if not _valid_version(v):
        raise HTTPException(status_code=404)
    data = card_cache.get(("card", app_id, v), lambda: load_annotated_card(app_id, v))
    if data is None:
        raise HTTPException(status_code=404)
    versions = card_cache.get(("versions", app_id), lambda: load_versions(app_id))
    return templates.TemplateResponse(request, "card.html", {
        "card": data, "versions": versions, "nonce": request.state.nonce})


@app.get("/card/{app_id}/{days}")
def card_with_range(app_id: int, days: int, v: str):
    """Addresses from when each version had cards for several time ranges; shared links
    keep working."""
    if not _valid_version(v):
        raise HTTPException(status_code=404)
    return RedirectResponse(f"/card/{app_id}?v={quote(v, safe='')}", status_code=301)


@app.get("/apps", response_class=HTMLResponse)
def all_apps(request: Request):
    apps = cache.get("all_apps", load_all_apps)
    return templates.TemplateResponse(request, "apps.html", {
        "apps": apps, "nonce": request.state.nonce})


def reference_rule_rates() -> dict:
    """How often each reference group fails each rule: messages failing over messages profiled,
    across every event type the rule applies to. A rule maps to {group: rate}, with None for a
    group that sends none of the rule's schemas."""
    rules_cfg = settings.load_yaml("rules.yaml").get("rules", {})
    with _connect() as conn:
        rows = conn.execute("SELECT event_type, ref_group, n, profile->'rules' FROM ref_profiles"
                            " WHERE subtype = '*'").fetchall()
        built = conn.execute("SELECT max(built_at) FROM ref_profiles").fetchone()[0]
    groups = sorted({g for _, g, _, _ in rows})
    rates: dict[str, dict[str, float | None]] = {}
    for rule_id, cfg in rules_cfg.items():
        schemas = cfg.get("schemas", "*")
        fails: dict[str, float] = dict.fromkeys(groups, 0.0)
        seen: dict[str, int] = dict.fromkeys(groups, 0)
        for event_type, group, n, rule_rates in rows:
            if schemas == "*" or event_type.split(":")[0] in schemas:
                seen[group] += n
                fails[group] += n * (rule_rates or {}).get(rule_id, 0.0)
        rates[rule_id] = {g: (fails[g] / seen[g] if seen[g] else None) for g in groups}
    return {"rates": rates, "groups": groups, "built": built}


@app.get("/about/technical", response_class=HTMLResponse)
def about_technical(request: Request):
    from .about import technical_context
    data = cache.get(("about", "technical"), lambda: technical_context(settings))
    try:
        ref = cache.get(("about", "technical", "reference"), reference_rule_rates)
    except psycopg.Error:
        log.exception("reference rule rates unavailable")
        ref = {"rates": {}, "groups": [], "built": None}
    return templates.TemplateResponse(request, "about_technical.html",
                                      {**data, "ref": ref, "nonce": request.state.nonce})


@app.get("/about/{layer}", response_class=HTMLResponse)
def about_layer(request: Request, layer: str):
    from .about import LAYERS, context
    if layer not in LAYERS:
        raise HTTPException(status_code=404)
    data = cache.get(("about", layer), lambda: context(settings, layer))
    return templates.TemplateResponse(request, "about_layer.html", {**data, "nonce": request.state.nonce})


@app.get("/about", response_class=HTMLResponse)
def about(request: Request):
    return templates.TemplateResponse(request, "about.html", {"nonce": request.state.nonce})


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    # Browsers ask for this path before reading the page's <link rel="icon">.
    return FileResponse(STATIC / "favicon-32.png", media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/health", response_class=PlainTextResponse)
def health() -> PlainTextResponse:
    return PlainTextResponse("ok", headers={"Cache-Control": "no-store"})


@app.exception_handler(RequestValidationError)
@app.exception_handler(404)
async def not_found(request: Request, exc: Exception):
    # Malformed addresses (no ?v=, a non-numeric app id) get the same page as unknown ones.
    nonce = getattr(request.state, "nonce", "")
    return templates.TemplateResponse(request, "notfound.html", {"nonce": nonce},
                                      status_code=404)
