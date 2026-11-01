"""Cached, conservative HTTP clients for music enrichment evidence.

The clients retrieve and cache provider responses only. They never modify media,
tags, destinations, or the Apple Music library.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import provider_cache


class ProviderError(RuntimeError):
    """Base class for an expected external-provider failure."""


class ProviderConfigurationError(ProviderError):
    pass


class ProviderNetworkError(ProviderError):
    pass


class ProviderRateLimitError(ProviderError):
    def __init__(self, message: str, retry_after_seconds: int = 300):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ProviderAPIError(ProviderError):
    pass


class ProviderNotFoundError(ProviderError):
    pass


@dataclass(frozen=True)
class ProviderResponse:
    status: str
    payload: dict[str, Any] | None
    cached: bool
    error: str | None = None


def _timestamp_after(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def _identity_key(artist: str, title: str, album: str = "") -> str:
    """Use the rebuild cache identity format to preserve its existing evidence."""
    return json.dumps((artist, title, album), ensure_ascii=False, separators=(",", ":"))


class CachedProvider:
    provider: str
    success_ttl = timedelta(days=90)
    failure_retry_seconds = 300

    def __init__(
        self,
        db,
        opener: Callable[..., Any] = urllib.request.urlopen,
        request_interval_seconds: float = 0.0,
    ):
        self.db = db
        self.opener = opener
        self.request_interval_seconds = request_interval_seconds
        self._last_request = 0.0

    def _request_json(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        wait = self.request_interval_seconds - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        request = urllib.request.Request(url, headers=headers)
        try:
            with self.opener(request, timeout=30) as response:
                self._last_request = time.monotonic()
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            self._last_request = time.monotonic()
            if exc.code == 429:
                retry = int(exc.headers.get("Retry-After", self.failure_retry_seconds))
                raise ProviderRateLimitError("HTTP 429 rate limited", retry) from exc
            if exc.code == 404:
                raise ProviderNotFoundError("HTTP 404 not found") from exc
            raise ProviderNetworkError(f"HTTP {exc.code}") from exc
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            self._last_request = time.monotonic()
            raise ProviderNetworkError(f"{type(exc).__name__}: {exc}") from exc

    def _cached(self, endpoint: str, key: str, refresh: bool) -> ProviderResponse | None:
        entry = provider_cache.get(self.db, self.provider, endpoint, key)
        if not entry or refresh:
            return None
        if entry.retry_after_at and not entry.retry_due:
            return ProviderResponse(entry.status, None, True, entry.error)
        if not entry.fresh:
            return None
        try:
            payload = json.loads(entry.response_json) if entry.response_json else None
        except json.JSONDecodeError:
            return None
        return ProviderResponse(entry.status, payload, True, entry.error)

    def _save_success(self, endpoint: str, key: str, payload: dict[str, Any]) -> ProviderResponse:
        provider_cache.put(
            self.db,
            self.provider,
            endpoint,
            key,
            "ok",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            expires_at=(datetime.now(UTC) + self.success_ttl).isoformat(),
        )
        self.db.commit()
        return ProviderResponse("ok", payload, False)

    def _save_failure(
        self, endpoint: str, key: str, status: str, error: ProviderError
    ) -> ProviderResponse:
        retry = getattr(error, "retry_after_seconds", self.failure_retry_seconds)
        provider_cache.put(
            self.db,
            self.provider,
            endpoint,
            key,
            status,
            None,
            str(error),
            retry_after_at=_timestamp_after(retry),
        )
        self.db.commit()
        return ProviderResponse(status, None, False, str(error))

    def _save_not_found(
        self, endpoint: str, key: str, error: ProviderNotFoundError
    ) -> ProviderResponse:
        provider_cache.put(
            self.db,
            self.provider,
            endpoint,
            key,
            "not_found",
            None,
            str(error),
            expires_at=(datetime.now(UTC) + self.success_ttl).isoformat(),
        )
        self.db.commit()
        return ProviderResponse("not_found", None, False, str(error))


class LastFMClient(CachedProvider):
    provider = "lastfm"
    base_url = "https://ws.audioscrobbler.com/2.0/"

    def __init__(self, db, api_key: str | None = None, **kwargs):
        super().__init__(db, **kwargs)
        self.api_key = api_key or os.environ.get("LASTFM_API_KEY", "").strip()
        if not self.api_key:
            raise ProviderConfigurationError("LASTFM_API_KEY is not configured")

    def top_tags(
        self, endpoint: str, artist: str, title: str = "", album: str = "", refresh: bool = False
    ) -> ProviderResponse:
        methods = {
            "track": ("track.getTopTags", {"artist": artist, "track": title}),
            "artist": ("artist.getTopTags", {"artist": artist}),
            "album": ("album.getTopTags", {"artist": artist, "album": album}),
        }
        if endpoint not in methods:
            raise ValueError(f"Unsupported Last.fm endpoint: {endpoint}")
        if (
            not artist.strip()
            or (endpoint == "track" and not title.strip())
            or (endpoint == "album" and not album.strip())
        ):
            raise ValueError(f"Missing identity required for Last.fm {endpoint} evidence")
        method, params = methods[endpoint]
        key = _identity_key(artist, title, album)
        cached = self._cached(endpoint, key, refresh)
        if cached:
            return cached
        query = {
            "method": method,
            "api_key": self.api_key,
            "format": "json",
            "autocorrect": "0",
            **params,
        }
        try:
            payload = self._request_json(
                self.base_url + "?" + urllib.parse.urlencode(query),
                {"User-Agent": "download.py music pipeline/2"},
            )
            if "error" in payload:
                raise ProviderAPIError(
                    f"Last.fm {payload.get('error')}: {payload.get('message', 'unknown error')}"
                )
            return self._save_success(endpoint, key, payload)
        except ProviderRateLimitError as exc:
            return self._save_failure(endpoint, key, "rate_limited", exc)
        except ProviderNotFoundError as exc:
            return self._save_not_found(endpoint, key, exc)
        except ProviderNetworkError as exc:
            return self._save_failure(endpoint, key, "network_error", exc)
        except ProviderAPIError as exc:
            return self._save_failure(endpoint, key, "api_error", exc)


class LRCLIBClient(CachedProvider):
    provider = "lrclib"
    base_url = "https://lrclib.net/api/get"
    success_ttl = timedelta(days=30)

    def lyrics(
        self,
        artist: str,
        title: str,
        album: str = "",
        duration: int | None = None,
        refresh: bool = False,
    ) -> ProviderResponse:
        if not artist.strip() or not title.strip():
            raise ValueError("Artist and title are required for LRCLIB evidence")
        key = provider_cache.request_key(
            {"artist": artist, "title": title, "album": album, "duration": duration}
        )
        cached = self._cached("get", key, refresh)
        if cached:
            return cached
        params: dict[str, str | int] = {"artist_name": artist, "track_name": title}
        if album:
            params["album_name"] = album
        if duration is not None:
            params["duration"] = duration
        try:
            payload = self._request_json(
                self.base_url + "?" + urllib.parse.urlencode(params),
                {"User-Agent": "download.py music pipeline/2 (personal library)"},
            )
            return self._save_success("get", key, payload)
        except ProviderRateLimitError as exc:
            return self._save_failure("get", key, "rate_limited", exc)
        except ProviderNotFoundError as exc:
            return self._save_not_found("get", key, exc)
        except ProviderNetworkError as exc:
            return self._save_failure("get", key, "network_error", exc)
