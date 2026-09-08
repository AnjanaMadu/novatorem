"""Small, fail-open PostgreSQL cache for serverless upstream lookups."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from psycopg import connect


def _database_url() -> str:
    return (
        os.getenv("DATABASE_URL")
        or os.getenv("POSTGRES_URL")
        or os.getenv("POSTGRES_URL_NON_POOLING")
        or ""
    )


def _ttl() -> int:
    try:
        return max(1, int(os.getenv("YOUTUBE_CACHE_TTL_SECONDS", "3600")))
    except ValueError:
        return 3600


def _ensure_table(connection: Any) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS novatorem_cache (
                cache_key TEXT PRIMARY KEY,
                payload JSONB NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )


def get_json(cache_key: str) -> Optional[dict[str, Any]]:
    """Return an unexpired cached object, or None if cache is unavailable/missed."""
    database_url = _database_url()
    if not database_url:
        return None

    try:
        with connect(database_url, connect_timeout=3) as connection:
            _ensure_table(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT payload
                    FROM novatorem_cache
                    WHERE cache_key = %s AND expires_at > NOW()
                    """,
                    (cache_key,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                payload = row[0]
                return payload if isinstance(payload, dict) else json.loads(payload)
    except Exception:
        return None


def set_json(cache_key: str, payload: dict[str, Any]) -> None:
    """Store an object for the configured TTL; silently ignore cache failures."""
    database_url = _database_url()
    if not database_url:
        return

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=_ttl())
    try:
        with connect(database_url, connect_timeout=3) as connection:
            _ensure_table(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO novatorem_cache (cache_key, payload, expires_at, updated_at)
                    VALUES (%s, %s::jsonb, %s, NOW())
                    ON CONFLICT (cache_key) DO UPDATE SET
                        payload = EXCLUDED.payload,
                        expires_at = EXCLUDED.expires_at,
                        updated_at = NOW()
                    """,
                    (cache_key, json.dumps(payload), expires_at),
                )
    except Exception:
        pass