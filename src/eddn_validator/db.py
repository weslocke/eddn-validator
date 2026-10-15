"""Database connections."""

from __future__ import annotations

import psycopg

from .config import APP_ROLE, Settings


def connect(settings: Settings, *, application_name: str, autocommit: bool = True,
            dbname: str | None = None, user: str = APP_ROLE,
            password: str | None = None) -> psycopg.Connection:
    """Connect as the application role (or another role when given).

    Autocommit by default: every write goes inside an explicit
    `with conn.transaction():` block, which then really commits. Without
    autocommit, the first plain query opens an implicit transaction and every
    later `transaction()` block silently becomes a savepoint inside it, so
    nothing commits (and any locks are held) until the connection closes.
    """
    return psycopg.connect(
        host=settings.pghost,
        port=settings.pgport,
        dbname=dbname or settings.pgdatabase,
        user=user,
        password=settings.app_password if password is None else password,
        application_name=f"eddn-validator {application_name}",
        connect_timeout=10,
        keepalives=1,
        keepalives_idle=60,
        keepalives_interval=15,
        keepalives_count=4,
        # Server-side keepalives: if a client dies without closing its connection (a
        # container killed mid-COPY), Postgres drops the session and its locks within
        # about two minutes instead of the system default of two hours.
        options="-c tcp_keepalives_idle=60 -c tcp_keepalives_interval=10"
                " -c tcp_keepalives_count=6 -c client_connection_check_interval=30s",
        autocommit=autocommit,
    )
