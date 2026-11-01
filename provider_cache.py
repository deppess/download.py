"""Provider-response cache with explicit expiration and retry metadata."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from pipeline_state import utc_now


@dataclass(frozen=True)
class CacheEntry:
    provider: str
    endpoint: str
    request_key: str
    status: str
    response_json: str | None
    error: str | None
    fetched_at: str
    expires_at: str | None
    retry_after_at: str | None
    attempts: int

    @property
    def fresh(self) -> bool:
        if self.retry_after_at:
            return False
        return self.expires_at is None or datetime.fromisoformat(self.expires_at) > datetime.now(
            timezone.utc
        )

    @property
    def retry_due(self) -> bool:
        """Whether a transient failure may be requested again."""
        return bool(
            self.retry_after_at
            and datetime.fromisoformat(self.retry_after_at) <= datetime.now(timezone.utc)
        )


def request_key(parameters: dict[str, object]) -> str:
    """Stable, non-secret JSON cache key for normalized provider parameters."""
    return json.dumps(parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def get(db, provider: str, endpoint: str, key: str) -> CacheEntry | None:
    row = db.execute(
        """SELECT provider, endpoint, request_key, status, response_json, error,
                  fetched_at, expires_at, retry_after_at, attempts
           FROM provider_cache WHERE provider=? AND endpoint=? AND request_key=?""",
        (provider, endpoint, key),
    ).fetchone()
    return CacheEntry(*row) if row else None


def put(
    db,
    provider: str,
    endpoint: str,
    key: str,
    status: str,
    response_json: str | None,
    error: str | None = None,
    expires_at: str | None = None,
    retry_after_at: str | None = None,
) -> None:
    db.execute(
        """INSERT INTO provider_cache(
                provider, endpoint, request_key, status, response_json, error,
                fetched_at, expires_at, retry_after_at, attempts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(provider, endpoint, request_key) DO UPDATE SET
                status=excluded.status, response_json=excluded.response_json,
                error=excluded.error, fetched_at=excluded.fetched_at,
                expires_at=excluded.expires_at, retry_after_at=excluded.retry_after_at,
                attempts=provider_cache.attempts + 1""",
        (
            provider,
            endpoint,
            key,
            status,
            response_json,
            error,
            utc_now(),
            expires_at,
            retry_after_at,
        ),
    )
