"""Reference versions from the GitHub releases of the big three (docs/DESIGN.md §9).

Only release objects count: tags without a release (e.g. the withdrawn
EDDiscovery 19.1.7), drafts and pre-releases are ignored.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime

import psycopg

from .classify import parse_ts

log = logging.getLogger(__name__)

API = "https://api.github.com/repos/{repo}/releases?per_page=50"


@dataclass(frozen=True)
class RefVersion:
    version: str
    tag: str
    published_at: datetime | None
    is_current: bool


def fetch_releases(repo: str, token: str = "") -> list[dict]:
    request = urllib.request.Request(API.format(repo=repo), headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "eddn-validator",
        "X-GitHub-Api-Version": "2022-11-28",
        **({"Authorization": f"Bearer {token}"} if token else {}),
    })
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def select_versions(releases: list[dict], group_cfg: dict) -> list[RefVersion]:
    """Map releases to header versions; the newest keep_releases are current."""
    pattern = re.compile(group_cfg["tag_pattern"])
    fmt = group_cfg["version_format"]
    keep = int(group_cfg.get("keep_releases", 3))
    aliases: dict[str, list[str]] = group_cfg.get("aliases") or {}

    candidates = []
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        m = pattern.match(release.get("tag_name") or "")
        if not m:
            continue
        candidates.append((parse_ts(release.get("published_at")), fmt.format(v=m["v"]),
                           release["tag_name"]))
    candidates.sort(key=lambda c: (c[0] is not None, c[0]), reverse=True)

    result: dict[str, RefVersion] = {}
    for rank, (published, version, tag) in enumerate(candidates):
        current = rank < keep
        for v in (version, *aliases.get(version, [])):
            existing = result.get(v)
            if existing is None or (current and not existing.is_current):
                result[v] = RefVersion(v, tag, published, current)
    return list(result.values())


def store_versions(conn: psycopg.Connection, group: str, versions: list[RefVersion]) -> None:
    with conn.transaction():
        conn.execute("UPDATE ref_versions SET is_current = false, updated_at = now()"
                     " WHERE ref_group = %s", (group,))
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO ref_versions (ref_group, version, tag, published_at, is_current)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (ref_group, version) DO UPDATE
                   SET tag = EXCLUDED.tag, published_at = EXCLUDED.published_at,
                       is_current = EXCLUDED.is_current, updated_at = now()
                """,
                [(group, v.version, v.tag, v.published_at, v.is_current) for v in versions],
            )


def poll(conn: psycopg.Connection, groups: dict[str, dict], token: str = "") -> None:
    for group, cfg in groups.items():
        try:
            releases = fetch_releases(cfg["repo"], token)
        except Exception as exc:  # network or API trouble: keep the previous list
            log.warning("release poll for %s failed: %s", group, exc)
            continue
        versions = select_versions(releases, cfg)
        if not any(v.is_current for v in versions):
            log.warning("release poll for %s found no usable releases; keeping previous list",
                        group)
            continue
        store_versions(conn, group, versions)
        log.info("%s reference versions: %s", group,
                 ", ".join(sorted(v.version for v in versions if v.is_current)))
