"""One-time setup: create the roles and the database using an admin login.

Safe to re-run: existing roles get their passwords reset to the values in
the environment, and existing databases are left alone.
"""

from __future__ import annotations

import logging

from psycopg import sql

from . import db
from .config import APP_ROLE, RO_ROLE, Settings

log = logging.getLogger(__name__)


def bootstrap(settings: Settings) -> None:
    missing = [name for name, value in (
        ("PG_ADMIN_USER", settings.admin_user),
        ("PG_ADMIN_PASSWORD", settings.admin_password),
        ("EDDN_APP_PASSWORD", settings.app_password),
        ("EDDN_RO_PASSWORD", settings.ro_password),
    ) if not value]
    if missing:
        raise SystemExit(f"bootstrap needs these set in .env: {', '.join(missing)}")

    with db.connect(settings, application_name="bootstrap", autocommit=True,
                    dbname=settings.admin_database, user=settings.admin_user,
                    password=settings.admin_password) as admin:
        for role, password in ((APP_ROLE, settings.app_password),
                               (RO_ROLE, settings.ro_password)):
            exists = admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s",
                                   (role,)).fetchone()
            verb = "ALTER" if exists else "CREATE"
            admin.execute(sql.SQL("{} ROLE {} WITH LOGIN PASSWORD {}").format(
                sql.SQL(verb), sql.Identifier(role), sql.Literal(password)))
            log.info("%s role %s", "updated" if exists else "created", role)

        database = settings.pgdatabase
        if not admin.execute("SELECT 1 FROM pg_database WHERE datname = %s",
                             (database,)).fetchone():
            admin.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(database), sql.Identifier(APP_ROLE)))
            log.info("created database %s", database)
        admin.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
            sql.Identifier(database)))
        admin.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(database), sql.Identifier(RO_ROLE)))

    with db.connect(settings, application_name="bootstrap", autocommit=True,
                    user=settings.admin_user, password=settings.admin_password) as admin:
        admin.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
            sql.Identifier(RO_ROLE)))
    log.info("bootstrap complete")
