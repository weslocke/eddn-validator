"""Settings from the environment and the YAML files in config/."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import yaml

APP_ROLE = "eddn_app"
RO_ROLE = "eddn_ro"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    pghost: str
    pgport: int
    pgdatabase: str
    app_password: str
    ro_password: str
    admin_user: str
    admin_password: str
    admin_database: str
    relay: str
    github_token: str
    config_dir: Path
    migrations_dir: Path

    @classmethod
    def from_env(cls) -> Settings:
        root = Path(_env("EDDN_ROOT", str(Path.cwd())))
        return cls(
            pghost=_env("PGHOST", "localhost"),
            pgport=int(_env("PGPORT", "5432")),
            pgdatabase=_env("PGDATABASE", "eddn_validator"),
            app_password=_env("EDDN_APP_PASSWORD"),
            ro_password=_env("EDDN_RO_PASSWORD"),
            admin_user=_env("PG_ADMIN_USER"),
            admin_password=_env("PG_ADMIN_PASSWORD"),
            admin_database=_env("PG_ADMIN_DATABASE", "postgres"),
            relay=_env("EDDN_RELAY", "tcp://eddn.edcd.io:9500"),
            github_token=_env("GITHUB_TOKEN"),
            config_dir=Path(_env("EDDN_CONFIG_DIR", str(root / "config"))),
            migrations_dir=Path(_env("EDDN_MIGRATIONS_DIR", str(root / "migrations"))),
        )

    def load_yaml(self, name: str) -> dict:
        with open(self.config_dir / name, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}

    @cached_property
    def reference_apps(self) -> ReferenceApps:
        return ReferenceApps(self.load_yaml("reference_apps.yaml"))

    @cached_property
    def retention(self) -> dict:
        return self.load_yaml("retention.yaml")


class ReferenceApps:
    """The big-three groups and the softwareName values that belong to them."""

    def __init__(self, data: dict):
        self.groups: dict[str, dict] = data.get("groups", {})
        self.group_by_name: dict[str, str] = {
            name: group
            for group, cfg in self.groups.items()
            for name in cfg.get("software_names", [])
        }

    def group_for(self, software_name: str) -> str | None:
        return self.group_by_name.get(software_name)


def outage_ranges(cfg: dict) -> list[str]:
    """scoring.yaml eddn_outages as tstzrange literals (start included, end excluded)."""
    return [f"[{start},{end})" for start, end in cfg.get("eddn_outages") or []]
