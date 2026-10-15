"""The owner's private ED Outrider usage page. LAN only: never put this behind the public proxy."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from . import db
from .config import Settings
from .outrider import LOCAL, OUTRIDER, Stats

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
settings = Settings.from_env()
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def local_time(value: dt.datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return value.astimezone(LOCAL).strftime(fmt) if value else "–"


templates.env.filters["local"] = local_time


@app.get("/", response_class=HTMLResponse)
def page(request: Request, days: int = 14):
    days = 14 if days not in (7, 14, 30, 60, 90) else days
    now = dt.datetime.now(dt.timezone.utc)
    with db.connect(settings, application_name="outrider-web", autocommit=True) as conn:
        stats = Stats(conn)
        if stats.app_id is None:
            return templates.TemplateResponse(request, "outrider.html",
                                              {"empty": True, "name": OUTRIDER})
        today = dt.datetime.now(LOCAL).date()
        today_messages, today_groups = stats.commanders(today)
        context = {
            "empty": False,
            "name": OUTRIDER,
            "now": now,
            "days": days,
            "totals": stats.totals(),
            "today_messages": today_messages,
            "today_groups": today_groups,
            "daily": stats.daily(days, (today, today_messages, today_groups)),
            "versions": stats.versions(now - dt.timedelta(days=30)),
            "event_types": stats.event_types(now - dt.timedelta(days=30)),
        }
    return templates.TemplateResponse(request, "outrider.html", context)


@app.get("/health", response_class=PlainTextResponse)
def health() -> str:
    return "ok"
