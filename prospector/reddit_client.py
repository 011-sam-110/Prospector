"""Reddit client — a thin, polite wrapper over Reddit's public ``.json`` API.

The engine's "fetch hands". Everything here is deliberately conservative:

  * one shared :class:`httpx.Client`,
  * a hard minimum interval between requests (so we never hammer Reddit),
  * honour ``429 Retry-After`` with exponential backoff,
  * an on-disk JSON cache keyed by ``(path, params)`` so re-runs are cheap and
    offline-friendly, and
  * optional OAuth (client-credentials) — when app credentials are present we
    talk to ``oauth.reddit.com`` with a higher rate budget, otherwise we use the
    anonymous ``www.reddit.com`` ``.json`` endpoints.

The public surface (``RedditClient`` + :class:`RedditError`) is the frozen
contract in ``INTERFACES.md``; do not change a signature without updating it.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Optional, Union

import httpx

DEFAULT_USER_AGENT = "prospector/0.1 (+https://github.com/011-sam-110/Prospector)"

#: Anonymous public JSON host.
PUBLIC_BASE = "https://www.reddit.com"
#: Authenticated (OAuth) host — higher rate budget.
OAUTH_BASE = "https://oauth.reddit.com"
#: Client-credentials token endpoint (always on www, never oauth host).
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"

#: Statuses we treat as transient and retry with backoff.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class RedditError(Exception):
    """Raised on a hard failure: exhausted retries, a non-JSON/blocked HTML
    response, an OAuth failure, or an error body Reddit returns inline."""


class RedditClient:
    """A polite client over Reddit's public ``.json`` endpoints (optional OAuth).

    Parameters
    ----------
    user_agent:
        Sent on every request — Reddit blocks generic / missing agents.
    cache_dir:
        Directory for the on-disk JSON cache. Defaults to ``./.cache/reddit``.
    cache_ttl:
        Cache lifetime in seconds. ``0`` disables the cache entirely.
    client_id / client_secret:
        OAuth app credentials. Fall back to the ``REDDIT_CLIENT_ID`` /
        ``REDDIT_CLIENT_SECRET`` environment variables. Absent → anonymous.
    min_interval:
        Minimum seconds between outbound requests. Defaults to ``6.0``
        anonymous, ``0.6`` when OAuth credentials are present.
    timeout:
        Per-request timeout in seconds.
    """

    #: Hard ceiling for any single backoff / Retry-After sleep.
    _MAX_BACKOFF = 60.0
    #: Number of *extra* attempts after the first on a transient failure.
    _MAX_RETRIES = 4

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        cache_dir: Union[str, Path, None] = None,
        cache_ttl: int = 3600,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        min_interval: Optional[float] = None,
        timeout: float = 20.0,
    ) -> None:
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.cache_ttl = int(cache_ttl)
        self.timeout = float(timeout)

        self.cache_dir = (
            Path(cache_dir)
            if cache_dir is not None
            else Path.cwd() / ".cache" / "reddit"
        )

        # Credentials: explicit args win, else environment.
        self._client_id = client_id or os.environ.get("REDDIT_CLIENT_ID") or None
        self._client_secret = (
            client_secret or os.environ.get("REDDIT_CLIENT_SECRET") or None
        )
        has_creds = bool(self._client_id and self._client_secret)

        # Default throttle depends only on whether creds are *configured*
        # (we must not do network in __init__ to decide this).
        self.min_interval = (
            float(min_interval)
            if min_interval is not None
            else (0.6 if has_creds else 6.0)
        )

        # In-memory OAuth token cache.
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0

        # Throttle / sleep / clock hooks (instance attrs so tests can fake them).
        self._last_request: Optional[float] = None
        self._monotonic = time.monotonic
        self._sleep = time.sleep
        self._now = time.time

        self._http = httpx.Client(timeout=self.timeout, follow_redirects=True)

    # ------------------------------------------------------------------ #
    # OAuth
    # ------------------------------------------------------------------ #
    @property
    def authenticated(self) -> bool:
        """``True`` if OAuth credentials are present *and* a token was obtained.

        Lazily mints (and caches) a token on first access. Returns ``False`` —
        rather than raising — if credentials are absent or the grant fails, so
        callers can branch on it safely.
        """
        if not (self._client_id and self._client_secret):
            return False
        try:
            return self._ensure_token() is not None
        except RedditError:
            return False

    def _ensure_token(self) -> Optional[str]:
        """Return a valid bearer token, minting/refreshing as needed.

        Returns ``None`` when no credentials are configured. Raises
        :class:`RedditError` if the grant request itself fails.
        """
        if not (self._client_id and self._client_secret):
            return None
        # 30s skew buffer so we never use a token that's about to expire.
        if self._token and self._now() < (self._token_expiry - 30):
            return self._token
        try:
            resp = self._http.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(self._client_id, self._client_secret),
                headers={"User-Agent": self.user_agent},
            )
        except httpx.HTTPError as exc:  # pragma: no cover - network failure path
            raise RedditError(f"OAuth token request failed: {exc}") from exc
        if resp.status_code != 200:
            raise RedditError(
                f"OAuth token request returned HTTP {resp.status_code}"
            )
        try:
            body = resp.json()
        except Exception as exc:
            raise RedditError("OAuth token response was not JSON") from exc
        token = body.get("access_token") if isinstance(body, dict) else None
        if not token:
            raise RedditError(f"OAuth token missing from response: {body!r}")
        self._token = str(token)
        try:
            expires_in = float(body.get("expires_in", 3600))
        except (TypeError, ValueError):
            expires_in = 3600.0
        self._token_expiry = self._now() + expires_in
        return self._token

    # ------------------------------------------------------------------ #
    # Core fetch
    # ------------------------------------------------------------------ #
    def get_json(self, path: str, params: Optional[dict] = None) -> dict:
        """Core fetch for a single Reddit ``.json`` endpoint.

        ``path`` is e.g. ``'/r/nursing/new.json'`` or
        ``'/comments/abc.json'``. Uses ``oauth.reddit.com`` when authenticated,
        else ``www.reddit.com``. Adds the configured User-Agent, throttles to
        ``min_interval``, honours ``429 Retry-After`` with exponential backoff,
        and reads/writes an on-disk JSON cache keyed by ``(path, params)`` with
        TTL ``cache_ttl`` (``0`` disables). Returns the parsed JSON.

        Note: although annotated ``-> dict`` for the common case, Reddit's
        ``/comments`` endpoint legitimately returns a JSON *array*; the raw
        parsed payload is returned verbatim.
        """
        cache_path = self._cache_path(path, params)
        cached = self._read_cache(cache_path)
        if cached is not None:
            return cached
        data = self._fetch(path, params)
        self._write_cache(cache_path, data)
        return data

    def _fetch(self, path: str, params: Optional[dict]) -> dict:
        """Perform the throttled, authenticated, retrying HTTP round-trip.

        Separated from :meth:`get_json` so caching and the network can be
        tested independently.
        """
        if not path.startswith("/"):
            path = "/" + path
        req_params = dict(params or {})
        # ``raw_json=1`` stops Reddit HTML-escaping &, <, > in bodies.
        req_params.setdefault("raw_json", 1)

        backoff = 2.0
        last_exc: Optional[Exception] = None

        for attempt in range(self._MAX_RETRIES + 1):
            self._throttle()

            token = self._ensure_token()
            base = OAUTH_BASE if token else PUBLIC_BASE
            url = base + path
            headers = {
                "User-Agent": self.user_agent,
                "Accept": "application/json",
            }
            if token:
                headers["Authorization"] = f"bearer {token}"

            try:
                resp = self._http.get(url, params=req_params, headers=headers)
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt >= self._MAX_RETRIES:
                    break
                self._sleep(min(backoff, self._MAX_BACKOFF))
                backoff *= 2
                continue

            status = resp.status_code

            # Stale token → drop it and retry once with a fresh grant.
            if status in (401, 403) and headers.get("Authorization"):
                self._token = None
                self._token_expiry = 0.0
                if attempt < self._MAX_RETRIES:
                    continue

            if status in _RETRY_STATUS:
                if attempt >= self._MAX_RETRIES:
                    raise RedditError(
                        f"Reddit returned HTTP {status} for {path} "
                        f"after {attempt + 1} attempts"
                    )
                retry_after = self._retry_after(resp)
                wait = retry_after if retry_after is not None else backoff
                self._sleep(min(wait, self._MAX_BACKOFF))
                backoff *= 2
                continue

            if status >= 400:
                raise RedditError(
                    f"Reddit returned HTTP {status} for {path}: "
                    f"{self._snippet(resp)}"
                )

            data = self._parse(resp, path)

            # Reddit sometimes replies 200 with an inline error code body.
            if isinstance(data, dict) and isinstance(data.get("error"), int):
                err = data["error"]
                if err in _RETRY_STATUS and attempt < self._MAX_RETRIES:
                    self._sleep(min(backoff, self._MAX_BACKOFF))
                    backoff *= 2
                    continue
                if err >= 400:
                    raise RedditError(
                        f"Reddit returned inline error {err} for {path}: "
                        f"{data.get('message', '')}"
                    )
            return data

        raise RedditError(
            f"Request to {path} failed after {self._MAX_RETRIES + 1} attempts: "
            f"{last_exc}"
        )

    @staticmethod
    def _parse(resp: "httpx.Response", path: str) -> dict:
        """Parse a response body as JSON, raising :class:`RedditError` for the
        HTML block / login-wall pages Reddit serves when it throttles bots."""
        try:
            return resp.json()
        except Exception as exc:
            text = (getattr(resp, "text", "") or "")[:400].strip().lower()
            if (
                text.startswith("<")
                or "<html" in text
                or "blocked" in text
                or "whoa there" in text
            ):
                raise RedditError(
                    f"Blocked / non-JSON (HTML) response from Reddit for {path}"
                ) from exc
            raise RedditError(f"Invalid JSON from Reddit for {path}") from exc

    @staticmethod
    def _snippet(resp: "httpx.Response") -> str:
        return (getattr(resp, "text", "") or "")[:160].replace("\n", " ").strip()

    def _retry_after(self, resp: "httpx.Response") -> Optional[float]:
        """Parse a ``Retry-After`` header (seconds or HTTP-date) into seconds."""
        try:
            val = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
        except Exception:
            return None
        if not val:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            pass
        try:  # HTTP-date form
            from email.utils import parsedate_to_datetime

            dt = parsedate_to_datetime(val)
            if dt is None:
                return None
            import datetime as _dt

            now = (
                _dt.datetime.now(dt.tzinfo)
                if dt.tzinfo
                else _dt.datetime.now()
            )
            return max(0.0, (dt - now).total_seconds())
        except Exception:
            return None

    def _throttle(self) -> None:
        """Block until at least ``min_interval`` has elapsed since the last
        request, using the (fake-able) monotonic clock + sleep hooks."""
        if self.min_interval <= 0:
            self._last_request = self._monotonic()
            return
        now = self._monotonic()
        if self._last_request is not None:
            wait = self.min_interval - (now - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._monotonic()

    # ------------------------------------------------------------------ #
    # On-disk cache
    # ------------------------------------------------------------------ #
    def _cache_path(self, path: str, params: Optional[dict]) -> Path:
        key = json.dumps(
            {"path": path, "params": params or {}},
            sort_keys=True,
            default=str,
        )
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def _read_cache(self, cache_path: Path) -> Optional[dict]:
        if self.cache_ttl <= 0:
            return None
        try:
            if not cache_path.is_file():
                return None
            with cache_path.open("r", encoding="utf-8") as fh:
                envelope = json.load(fh)
        except (OSError, ValueError):
            return None
        ts = envelope.get("ts", 0) if isinstance(envelope, dict) else 0
        if (self._now() - float(ts)) > self.cache_ttl:
            return None
        return envelope.get("data")

    def _write_cache(self, cache_path: Path, data: object) -> None:
        if self.cache_ttl <= 0:
            return
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = cache_path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump({"ts": self._now(), "data": data}, fh)
            tmp.replace(cache_path)
        except (OSError, TypeError, ValueError):
            # Cache is best-effort; never fail a fetch over a write error.
            pass

    # ------------------------------------------------------------------ #
    # High-level endpoints
    # ------------------------------------------------------------------ #
    def listing(
        self,
        subreddit: str,
        sort: str = "new",
        limit: int = 100,
        time_filter: str = "year",
        pages: int = 1,
    ) -> list[dict]:
        """Return a flat list of raw post ``data`` dicts (the inner ``t3`` data).

        Pulls ``/r/<subreddit>/<sort>.json`` and follows the ``after`` token for
        up to ``pages`` pages of (at most 100) posts each.
        """
        per_page = max(1, min(int(limit), 100))
        path = f"/r/{subreddit}/{sort}.json"
        out: list[dict] = []
        after: Optional[str] = None

        for _ in range(max(1, int(pages))):
            params: dict = {"limit": per_page, "t": time_filter}
            if after:
                params["after"] = after
            data = self.get_json(path, params)
            listing_data = (data or {}).get("data") or {}
            children = listing_data.get("children") or []
            for child in children:
                if child.get("kind") == "t3" and isinstance(child.get("data"), dict):
                    out.append(child["data"])
            after = listing_data.get("after")
            if not after:
                break
        return out

    def search(
        self,
        query: str,
        subreddit: Optional[str] = None,
        sort: str = "relevance",
        time_filter: str = "year",
        limit: int = 100,
        restrict_sr: bool = True,
    ) -> list[dict]:
        """Return raw post ``data`` dicts from ``search.json``.

        When ``subreddit`` is given the search is scoped to that sub (subject to
        ``restrict_sr``); otherwise it is a site-wide link search.
        """
        params: dict = {
            "q": query,
            "sort": sort,
            "t": time_filter,
            "limit": max(1, min(int(limit), 100)),
            "type": "link",
        }
        if subreddit:
            path = f"/r/{subreddit}/search.json"
            params["restrict_sr"] = "1" if restrict_sr else "0"
        else:
            path = "/search.json"
        data = self.get_json(path, params)
        children = ((data or {}).get("data") or {}).get("children") or []
        return [
            child["data"]
            for child in children
            if child.get("kind") == "t3" and isinstance(child.get("data"), dict)
        ]

    def comments(
        self,
        post_id: str,
        limit: int = 100,
        depth: int = 2,
        min_score: int = 0,
    ) -> list[dict]:
        """Fetch a post's comment tree and return a FLAT list of comment ``data``.

        Walks ``/comments/<id>.json`` up to ``depth`` levels, skipping ``more``
        stubs and dropping comments below ``min_score`` (kept comments still let
        the walk descend into their replies). ``post_id`` may be a fullname
        (``t3_abc``) or a bare id (``abc``).
        """
        bare = post_id.split("_", 1)[1] if post_id.startswith("t3_") else post_id
        params = {"limit": int(limit), "depth": int(depth)}
        data = self.get_json(f"/comments/{bare}.json", params)

        # The comments endpoint returns a 2-element array: [post, comments].
        if not isinstance(data, list) or len(data) < 2:
            return []
        comments_listing = data[1] or {}
        children = (comments_listing.get("data") or {}).get("children") or []

        out: list[dict] = []
        cap = max(0, int(limit))

        def walk(nodes: list, cur_depth: int) -> None:
            if cur_depth > depth:
                return
            for node in nodes:
                if len(out) >= cap:
                    return
                if node.get("kind") != "t1":
                    # 'more' stubs and anything non-comment are skipped.
                    continue
                cdata = node.get("data")
                if not isinstance(cdata, dict):
                    continue
                try:
                    score = int(cdata.get("score", 0) or 0)
                except (TypeError, ValueError):
                    score = 0
                if score >= min_score:
                    out.append(cdata)
                    if len(out) >= cap:
                        return
                replies = cdata.get("replies")
                if isinstance(replies, dict):
                    reply_children = (replies.get("data") or {}).get("children") or []
                    walk(reply_children, cur_depth + 1)

        walk(children, 1)
        return out[:cap] if cap else out

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        try:
            self._http.close()
        except Exception:  # pragma: no cover - best-effort cleanup
            pass

    def __enter__(self) -> "RedditClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
