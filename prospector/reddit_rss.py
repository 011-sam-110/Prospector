"""Reddit RSS transport: read Reddit through its public Atom feeds.

Prospector uses this transport when the ``.json`` endpoints return HTTP 403
(see :class:`prospector.reddit_client.RedditClient`, ``transport="auto"``). The
design comes from the Reddit RSS client in the Real Estate research engine, with
these changes, each measured from a home IP on 2026-10-04:

* The host is ``www.reddit.com``. ``old.reddit.com`` now sends a ``302`` to a
  login page for ``.rss`` paths.
* Reddit sends ``x-ratelimit-remaining`` and ``x-ratelimit-reset`` on feeds. The
  measured budget was one request per clock minute. The transport waits for the
  reset when ``remaining`` is below 1, so the real spacing can be longer than
  ``min_interval``.
* After any failed request (429, 403, 5xx, an empty body, a block page or a
  network error) the next request waits for the full window: the reset from
  the headers plus 1 s, or 60 s when there is no header. Each further failure
  in a row doubles that wait, up to 10 minutes. After 6 failures in a row the
  transport sends nothing more and raises at once. In the 2026-10-04 run,
  after about 47 good requests, every request got 429 and then 403. The cause
  is not proven. A request sent 20 s after a failure, inside the same window,
  possibly kept the block going.
* ``/api/info.rss?id=...`` returns up to 100 posts or comments in one request
  and leaves out ids that no longer exist. :meth:`RssTransport.info` uses it to
  find content that was deleted on Reddit.

Each feed entry becomes a dict with the same keys as the inner ``data`` object
of the ``.json`` API, so :meth:`prospector.models.Item.from_reddit` reads both.
A feed has no vote score and no comment count, so ``score`` and
``num_comments`` are always ``0`` from this transport.
"""

from __future__ import annotations

import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from html.parser import HTMLParser
from typing import Callable, Iterable, Optional

import httpx

from prospector.errors import CommentsUnavailable, RedditError

__all__ = [
    "RSS_BASE",
    "DEFAULT_RSS_INTERVAL",
    "INFO_BATCH",
    "RssTransport",
    "parse_feed",
    "html_to_text",
]

#: The only host that serves the feeds without a login wall (2026-10-04).
RSS_BASE = "https://www.reddit.com"

#: Minimum seconds between two feed requests. The rate-limit headers can make
#: the real gap longer.
DEFAULT_RSS_INTERVAL = 20.0

#: ``/api/info`` accepts at most 100 fullnames per request.
INFO_BATCH = 100

_ATOM = "{http://www.w3.org/2005/Atom}"

# The rendered Markdown of a post or comment sits between these two markers.
# Everything after them (the "submitted by ... [link] [comments]" footer, or an
# image table) is feed chrome, not user text.
_MD_BLOCK = re.compile(r"<!--\s*SC_OFF\s*-->(.*?)<!--\s*SC_ON\s*-->", re.S)

# "/u/someone on Post title" is the title Reddit gives a comment entry.
_COMMENT_TITLE = re.compile(r"^/u/\S+ on (.*)$", re.S)

_THREAD_ID = re.compile(r"/comments/([a-z0-9]+)/", re.I)
_SUBREDDIT_IN_PATH = re.compile(r"^/r/([^/]+)/", re.I)

_BLOCK_TAGS = frozenset(
    {
        "p", "div", "blockquote", "pre", "li", "ul", "ol", "table", "tr",
        "h1", "h2", "h3", "h4", "h5", "h6", "hr",
    }
)


class _TextExtractor(HTMLParser):
    """Collect the text of an HTML fragment. Block tags become line breaks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []

    def handle_starttag(self, tag, attrs):  # noqa: D401 - HTMLParser hook
        if tag == "br":
            self._chunks.append("\n")

    def handle_endtag(self, tag):  # noqa: D401 - HTMLParser hook
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data):  # noqa: D401 - HTMLParser hook
        self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def html_to_text(fragment: str) -> str:
    """Return the plain text of an HTML ``fragment``.

    Paragraphs stay on their own lines. Runs of spaces collapse to one space and
    empty lines are dropped.
    """
    parser = _TextExtractor()
    parser.feed(fragment or "")
    parser.close()
    lines = [" ".join(line.split()) for line in parser.text().splitlines()]
    return "\n".join(line for line in lines if line)


def _body_from_content(content_html: str) -> str:
    """Return the user-written text of an entry's ``<content>``.

    Link and image posts with no self text have no Markdown block, so they get
    an empty body (the ``.json`` API gives them an empty ``selftext`` too).
    """
    match = _MD_BLOCK.search(content_html or "")
    if match is None:
        return ""
    return html_to_text(match.group(1))


def _child_text(elem: ET.Element, tag: str) -> Optional[str]:
    child = elem.find(f"{_ATOM}{tag}")
    if child is None:
        return None
    return child.text


def _author(entry: ET.Element) -> str:
    """Author name without the ``/u/`` prefix. No author means ``[deleted]``."""
    author = entry.find(f"{_ATOM}author")
    name = _child_text(author, "name") if author is not None else None
    name = (name or "").strip()
    if not name:
        return "[deleted]"
    return name[3:] if name.startswith("/u/") else name


def _epoch(stamp: Optional[str]) -> int:
    if not stamp:
        return 0
    try:
        return int(datetime.fromisoformat(stamp.strip()).timestamp())
    except ValueError:
        return 0


def _permalink(entry: ET.Element) -> str:
    """Absolute ``https://www.reddit.com/...`` permalink of an entry."""
    link = entry.find(f"{_ATOM}link")
    href = link.get("href", "") if link is not None else ""
    path = urllib.parse.urlparse(href).path if href else ""
    return RSS_BASE + path if path else ""


def entry_to_data(entry: ET.Element) -> Optional[dict]:
    """Convert one Atom ``<entry>`` into a ``.json``-shaped ``data`` dict.

    Returns ``None`` for entries that are not a post (``t3_``) or a comment
    (``t1_``), for example feed metadata.
    """
    fullname = (_child_text(entry, "id") or "").strip()
    if not fullname.startswith(("t3_", "t1_")):
        return None
    bare = fullname[3:]
    permalink = _permalink(entry)
    path = urllib.parse.urlparse(permalink).path

    category = entry.find(f"{_ATOM}category")
    subreddit = category.get("term", "") if category is not None else ""
    if not subreddit:
        match = _SUBREDDIT_IN_PATH.match(path)
        subreddit = match.group(1) if match else ""

    created = _epoch(_child_text(entry, "published") or _child_text(entry, "updated"))
    content = _child_text(entry, "content") or ""
    body = _body_from_content(content)
    title = (_child_text(entry, "title") or "").strip()

    data: dict = {
        "id": bare,
        "name": fullname,
        "subreddit": subreddit,
        "author": _author(entry),
        "created_utc": created,
        "permalink": permalink,
        "score": 0,
        "num_comments": 0,
    }
    if fullname.startswith("t3_"):
        data["title"] = title
        data["selftext"] = body
    else:
        data["body"] = body
        thread = _THREAD_ID.search(path)
        data["link_id"] = f"t3_{thread.group(1)}" if thread else None
        title_match = _COMMENT_TITLE.match(title)
        data["link_title"] = title_match.group(1).strip() if title_match else ""
    return data


def parse_feed(xml_bytes: bytes, path: str = "") -> list[dict]:
    """Parse an Atom feed into a list of ``.json``-shaped ``data`` dicts.

    Raises :class:`RedditError` (``blocked=True``) when the body is not an Atom
    feed, for example a login wall or a block page served with status 200.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise RedditError(
            f"Reddit sent a body that is not an Atom feed for {path}", blocked=True
        ) from exc
    if root.tag != f"{_ATOM}feed":
        raise RedditError(
            f"Reddit sent a {root.tag!r} document, not an Atom feed, for {path}",
            blocked=True,
        )
    out: list[dict] = []
    for entry in root.findall(f"{_ATOM}entry"):
        data = entry_to_data(entry)
        if data is not None:
            out.append(data)
    return out


def _header_float(resp: "httpx.Response", name: str) -> Optional[float]:
    try:
        raw = resp.headers.get(name)
    except Exception:  # noqa: BLE001 - a fake response may have no headers
        return None
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _bare_id(post_id: str) -> str:
    return post_id.split("_", 1)[1] if post_id.startswith(("t3_", "t1_")) else post_id


class RssTransport:
    """A polite reader of Reddit's public Atom feeds. No API key is used.

    Parameters
    ----------
    user_agent:
        Sent on every request. Use a descriptive, honest value.
    min_interval:
        Minimum seconds between two requests (default 20).
    timeout:
        Per-request timeout in seconds.
    http:
        An object with a ``get(url, params=None, headers=None)`` method. Tests
        pass a fake. The default is an :class:`httpx.Client`.
    log:
        Where progress lines go (default: nowhere).

    ``requests_made`` counts every HTTP request this transport sent.
    """

    #: Number of extra attempts after the first one on a transient failure.
    _MAX_RETRIES = 3
    #: Ceiling for one reset value read from the headers, in seconds.
    _MAX_WAIT = 120.0
    #: Wait after a failure when the response has no reset header.
    _FULL_WINDOW = 60.0
    #: Ceiling for the doubled wait after failures in a row.
    _MAX_FAILURE_WAIT = 600.0
    #: After this many failures in a row, stop sending requests.
    _MAX_FAILURES_IN_A_ROW = 6

    def __init__(
        self,
        user_agent: str,
        min_interval: float = DEFAULT_RSS_INTERVAL,
        timeout: float = 30.0,
        http=None,
        log: Optional[Callable[[str], object]] = None,
    ) -> None:
        self.user_agent = user_agent
        self.min_interval = float(min_interval)
        self._http = http if http is not None else httpx.Client(
            timeout=float(timeout), follow_redirects=True
        )
        self._log = log or (lambda _message: None)
        self._monotonic = time.monotonic
        self._sleep = time.sleep
        self._last_request: Optional[float] = None
        self._not_before: Optional[float] = None
        self._failures_in_a_row = 0
        self.requests_made = 0
        self._counts = {"ok": 0, "http_403": 0, "http_429": 0, "other": 0}

    def request_counts(self) -> dict:
        """Requests sent so far, by outcome: ``ok``, ``http_403``, ``http_429``
        and ``other`` (5xx, other 4xx, empty body, block page, network error)."""
        return dict(self._counts)

    def _count(self, outcome: str) -> None:
        self._counts[outcome if outcome in self._counts else "other"] += 1

    @property
    def stopped(self) -> bool:
        """``True`` after too many failures in a row: no more requests are sent."""
        return self._failures_in_a_row >= self._MAX_FAILURES_IN_A_ROW

    # ------------------------------------------------------------------ #
    # Pacing
    # ------------------------------------------------------------------ #
    def _wait_turn(self) -> None:
        """Sleep until both the minimum interval and the rate window allow a request."""
        now = self._monotonic()
        wait = 0.0
        if self._last_request is not None:
            wait = max(wait, self.min_interval - (now - self._last_request))
        if self._not_before is not None:
            wait = max(wait, self._not_before - now)
        if wait > 0:
            self._sleep(wait)
        self._last_request = self._monotonic()

    def _note_rate_headers(self, resp: "httpx.Response") -> None:
        """After a success: remember when the window resets if it is used up."""
        self._failures_in_a_row = 0
        remaining = _header_float(resp, "x-ratelimit-remaining")
        reset = _header_float(resp, "x-ratelimit-reset")
        if remaining is not None and reset is not None and remaining < 1:
            self._not_before = self._monotonic() + min(max(reset, 0.0), self._MAX_WAIT) + 1.0
        else:
            self._not_before = None

    def _record_failure(self, resp: Optional["httpx.Response"] = None) -> None:
        """After a failure: hold the next request for a full window, doubled for
        each failure in a row and capped at 10 minutes."""
        self._failures_in_a_row += 1
        reset = _header_float(resp, "x-ratelimit-reset") if resp is not None else None
        if reset is not None:
            base = min(max(reset, 0.0), self._MAX_WAIT) + 1.0
        else:
            base = self._FULL_WINDOW
        wait = min(base * 2 ** (self._failures_in_a_row - 1), self._MAX_FAILURE_WAIT)
        self._not_before = self._monotonic() + wait

    def note_external_failure(self) -> None:
        """Another transport just got refused (the ``.json`` 403 that triggers
        the switch to RSS). That request can use up the window too, so wait a
        full window before the first feed request."""
        self._record_failure(None)
        # The switch itself is not a feed failure: do not start the doubling.
        self._failures_in_a_row = 0

    # ------------------------------------------------------------------ #
    # Core fetch
    # ------------------------------------------------------------------ #
    def get_feed(self, path: str, params: Optional[dict] = None) -> list[dict]:
        """Fetch ``RSS_BASE + path`` and return its entries as ``data`` dicts.

        Retries ``429``, ``5xx``, network errors and empty ``200`` bodies (a soft
        rate limit) up to three times, each after the failure wait. Raises
        :class:`RedditError` on any other ``4xx`` (``status`` set) or on a body
        that is not an Atom feed. A ``403`` on a comment feed raises
        :class:`~prospector.errors.CommentsUnavailable` and is not retried.
        """
        if not path.startswith("/"):
            path = "/" + path
        url = RSS_BASE + path
        headers = {"User-Agent": self.user_agent, "Accept": "application/atom+xml"}
        last_problem = "no attempt made"

        for attempt in range(self._MAX_RETRIES + 1):
            if self.stopped:
                raise RedditError(
                    f"Reddit RSS paused: {self._failures_in_a_row} failed requests in a "
                    f"row; {path} was not requested",
                    blocked=True,
                )
            self._wait_turn()
            try:
                resp = self._http.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                self.requests_made += 1
                self._count("other")
                self._record_failure(None)
                last_problem = f"network error: {exc}"
                continue
            self.requests_made += 1
            status = int(resp.status_code)

            if status == 429 or status >= 500:
                self._count("http_429" if status == 429 else "other")
                self._record_failure(resp)
                last_problem = f"HTTP {status}"
                if attempt >= self._MAX_RETRIES:
                    raise RedditError(
                        f"Reddit RSS returned HTTP {status} for {path} "
                        f"after {attempt + 1} attempts",
                        status=status,
                    )
                self._log(f"[rss] HTTP {status} for {path}; waiting, then retrying")
                continue

            if status == 403:
                self._count("http_403")
                self._record_failure(resp)
                if "/comments/" in path:
                    raise CommentsUnavailable(
                        f"Reddit RSS returned HTTP 403 for {path}: comments not available",
                        status=status,
                    )
                raise RedditError(
                    f"Reddit RSS returned HTTP {status} for {path}", status=status
                )

            if status >= 400:
                # 404 and the like: about this path, not about the rate limit.
                self._count("other")
                self._note_rate_headers(resp)
                raise RedditError(
                    f"Reddit RSS returned HTTP {status} for {path}", status=status
                )

            body = resp.content or b""
            if not body.strip():
                self._count("other")
                self._record_failure(resp)
                last_problem = "empty body"
                continue
            try:
                entries = parse_feed(body, path)
            except RedditError:
                self._count("other")
                self._record_failure(resp)
                raise
            self._count("ok")
            self._note_rate_headers(resp)
            return entries

        raise RedditError(
            f"Reddit RSS request for {path} failed after "
            f"{self._MAX_RETRIES + 1} attempts ({last_problem})"
        )

    # ------------------------------------------------------------------ #
    # Endpoints (same shapes as RedditClient)
    # ------------------------------------------------------------------ #
    def listing(
        self,
        subreddit: str,
        sort: str = "new",
        limit: int = 100,
        time_filter: str = "year",
        pages: int = 1,
    ) -> list[dict]:
        """Posts from ``/r/<subreddit>/<sort>/.rss``, newest pages first."""
        per_page = max(1, min(int(limit), 100))
        out: list[dict] = []
        after: Optional[str] = None
        for _ in range(max(1, int(pages))):
            params: dict = {"limit": per_page}
            if sort in ("top", "controversial"):
                params["t"] = time_filter
            if after:
                params["after"] = after
            entries = self.get_feed(f"/r/{subreddit}/{sort}/.rss", params)
            posts = [e for e in entries if e["name"].startswith("t3_")]
            out.extend(posts)
            if len(posts) < per_page:
                break
            after = posts[-1]["name"]
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
        """Posts from ``search.rss``, scoped to ``subreddit`` when it is given."""
        params: dict = {
            "q": query,
            "sort": sort,
            "t": time_filter,
            "limit": max(1, min(int(limit), 100)),
        }
        if subreddit:
            path = f"/r/{subreddit}/search.rss"
            params["restrict_sr"] = "on" if restrict_sr else "off"
        else:
            path = "/search.rss"
        entries = self.get_feed(path, params)
        return [e for e in entries if e["name"].startswith("t3_")]

    def comments(
        self,
        post_id: str,
        limit: int = 100,
        depth: int = 2,
        min_score: int = 0,
    ) -> list[dict]:
        """Comments of one thread, from ``/comments/<id>/.rss``.

        The feed is a flat list with no vote scores, so ``depth`` and
        ``min_score`` have no effect here. They stay in the signature so the
        transport can replace the ``.json`` client.
        """
        cap = max(1, min(int(limit), 100))
        entries = self.get_feed(f"/comments/{_bare_id(post_id)}/.rss", {"limit": cap})
        return [e for e in entries if e["name"].startswith("t1_")][:cap]

    def info(self, fullnames: Iterable[str]) -> list[dict]:
        """Current state of up to 100 posts or comments, from ``/api/info.rss``.

        Reddit leaves out ids that no longer exist. A deleted or removed item
        that still exists comes back with ``[deleted]`` or ``[removed]`` text.
        """
        names = [n for n in dict.fromkeys(fullnames) if n]
        if not names:
            return []
        if len(names) > INFO_BATCH:
            raise ValueError(f"info() takes at most {INFO_BATCH} ids, got {len(names)}")
        return self.get_feed("/api/info.rss", {"id": ",".join(names)})

    def close(self) -> None:
        """Close the HTTP connection pool."""
        try:
            self._http.close()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass
