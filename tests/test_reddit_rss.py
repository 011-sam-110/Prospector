"""Offline tests for :mod:`prospector.reddit_rss` and the RSS fallback.

The feeds in ``tests/fixtures/rss`` copy the element structure of feeds that
www.reddit.com served on 2026-10-04. Their ids, names and text are invented, so
the public repo holds no Reddit content. No test touches the network: a fake
HTTP object replays responses, and the clock and sleep are fakes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from prospector.errors import RedditError
from prospector.models import Item
from prospector.reddit_client import DEFAULT_USER_AGENT, RedditClient
from prospector.reddit_rss import RSS_BASE, RssTransport, html_to_text, parse_feed

FIXTURES = Path(__file__).parent / "fixtures" / "rss"


def _feed(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeResp:
    def __init__(self, status=200, content=b"", headers=None, json_data=None, text=""):
        self.status_code = status
        self.content = content
        self.headers = headers or {}
        self._json = json_data
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class FakeHttp:
    """Replays queued responses and records each GET."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    def post(self, *a, **k):  # pragma: no cover - never used anonymously
        raise AssertionError("no OAuth in these tests")

    def close(self):
        pass


class FakeClock:
    """Monotonic clock that only moves when the code sleeps."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _transport(responses, min_interval=20.0):
    http = FakeHttp(responses)
    rss = RssTransport("test-agent/1.0", min_interval=min_interval, http=http)
    clock = FakeClock()
    rss._monotonic = clock.monotonic
    rss._sleep = clock.sleep
    return rss, http, clock


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_listing_entries_become_json_shaped_posts():
    posts = parse_feed(_feed("listing_new.xml"))
    assert [p["name"] for p in posts] == ["t3_aaa111", "t3_bbb222", "t3_ccc333"]
    first = posts[0]
    assert first["id"] == "aaa111"
    assert first["subreddit"] == "examplenursing"
    assert first["author"] == "test_author_one"
    assert first["title"] == "Still faxing forms in 2026"
    assert first["permalink"] == (
        "https://www.reddit.com/r/examplenursing/comments/aaa111/still_faxing_forms/"
    )
    assert first["created_utc"] == 1791130522  # 2026-10-04T16:15:22+00:00
    assert first["score"] == 0 and first["num_comments"] == 0


def test_body_keeps_user_text_and_drops_the_feed_footer():
    first = parse_feed(_feed("listing_new.xml"))[0]
    assert first["selftext"] == (
        "We still fax discharge forms & I wish there was a tool for it.\n"
        "Second paragraph with a link and it's fine."
    )
    assert "submitted by" not in first["selftext"]
    assert "[comments]" not in first["selftext"]


def test_image_post_without_self_text_has_empty_body():
    image = parse_feed(_feed("listing_new.xml"))[1]
    assert image["selftext"] == ""
    assert image["title"] == "A photo of the ward"


def test_missing_author_reads_as_deleted():
    removed = parse_feed(_feed("listing_new.xml"))[2]
    assert removed["author"] == "[deleted]"
    assert removed["selftext"] == "[removed]"


def test_thread_feed_gives_comments_with_link_id():
    entries = parse_feed(_feed("thread.xml"))
    comments = [e for e in entries if e["name"].startswith("t1_")]
    assert [c["name"] for c in comments] == ["t1_cm0001", "t1_cm0002", "t1_cm0003"]
    first = comments[0]
    assert first["link_id"] == "t3_aaa111"
    assert first["link_title"] == "Still faxing forms in 2026"
    assert first["body"] == (
        "Same here. Our unit prints every chart twice.\n"
        "one copy for the ward\none copy for pharmacy"
    )
    assert first["created_utc"] == 1791130800  # <updated>, comments have no <published>
    assert comments[1]["author"] == "[deleted]"
    assert comments[1]["body"] == "[deleted]"


def test_parsed_entries_feed_item_from_reddit():
    entries = parse_feed(_feed("thread.xml"))
    comment = Item.from_reddit(entries[1], kind="comment", profile="p", fetched_at=5)
    assert comment.id == "t1_cm0001"
    assert comment.link_id == "t3_aaa111"
    assert comment.title is None
    assert comment.permalink.endswith("/cm0001/")
    post = Item.from_reddit(entries[0], kind="post")
    assert post.id == "t3_aaa111"
    assert post.body.startswith("We still fax")


def test_login_wall_html_is_a_blocked_error():
    with pytest.raises(RedditError) as info:
        parse_feed(b"<!doctype html><html><body>Log in</body></html>", "/r/x/new/.rss")
    assert info.value.blocked is True


def test_html_to_text_handles_entities_and_breaks():
    assert html_to_text("<p>a &amp; b</p><p>c<br/>d</p>") == "a & b\nc\nd"


# --------------------------------------------------------------------------- #
# Transport: URLs, User-Agent, pacing, retries
# --------------------------------------------------------------------------- #
def test_endpoints_build_www_reddit_urls_and_send_the_user_agent():
    rss, http, _clock = _transport(
        [
            FakeResp(content=_feed("listing_new.xml")),
            FakeResp(content=_feed("listing_new.xml")),
            FakeResp(content=_feed("thread.xml")),
            FakeResp(content=_feed("info.xml")),
        ]
    )
    posts = rss.listing("examplenursing", limit=100)
    found = rss.search('"still fax" OR "manual"', subreddit="examplenursing", time_filter="week")
    comments = rss.comments("t3_aaa111", limit=40)
    info = rss.info(["t3_aaa111", "t1_cm0002", "t1_cm0003"])

    assert len(posts) == 3 and len(found) == 3
    assert [c["name"] for c in comments] == ["t1_cm0001", "t1_cm0002", "t1_cm0003"]
    assert [e["name"] for e in info] == ["t3_aaa111", "t1_cm0002"]

    urls = [c[0] for c in http.calls]
    assert urls == [
        f"{RSS_BASE}/r/examplenursing/new/.rss",
        f"{RSS_BASE}/r/examplenursing/search.rss",
        f"{RSS_BASE}/comments/aaa111/.rss",
        f"{RSS_BASE}/api/info.rss",
    ]
    assert http.calls[1][1] == {
        "q": '"still fax" OR "manual"',
        "sort": "relevance",
        "t": "week",
        "limit": 100,
        "restrict_sr": "on",
    }
    assert http.calls[2][1] == {"limit": 40}
    assert http.calls[3][1] == {"id": "t3_aaa111,t1_cm0002,t1_cm0003"}
    for _url, _params, headers in http.calls:
        assert headers["User-Agent"] == "test-agent/1.0"
    assert rss.requests_made == 4


def test_minimum_interval_between_requests():
    rss, _http, clock = _transport(
        [FakeResp(content=_feed("info.xml")), FakeResp(content=_feed("info.xml"))],
        min_interval=20.0,
    )
    rss.info(["t3_aaa111"])
    rss.info(["t3_aaa111"])
    assert clock.sleeps == [20.0]


def test_rate_limit_headers_hold_the_next_request_until_reset():
    used_up = {"x-ratelimit-remaining": "0.0", "x-ratelimit-reset": "52", "x-ratelimit-used": "1"}
    rss, _http, clock = _transport(
        [
            FakeResp(content=_feed("info.xml"), headers=used_up),
            FakeResp(content=_feed("info.xml")),
        ],
        min_interval=20.0,
    )
    rss.info(["t3_aaa111"])
    rss.info(["t3_aaa111"])
    # reset (52 s) + 1 s margin, which is longer than the 20 s interval
    assert clock.sleeps == [53.0]


def test_429_waits_for_the_reset_then_retries():
    rss, http, clock = _transport(
        [
            FakeResp(status=429, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "21"}),
            FakeResp(content=_feed("info.xml")),
        ],
        min_interval=0.0,
    )
    entries = rss.info(["t3_aaa111"])
    assert len(entries) == 2
    assert len(http.calls) == 2
    assert clock.sleeps == [22.0]


def test_empty_200_is_retried_as_a_soft_rate_limit():
    rss, http, _clock = _transport(
        [FakeResp(content=b"  "), FakeResp(content=_feed("info.xml"))], min_interval=0.0
    )
    assert len(rss.info(["t3_aaa111"])) == 2
    assert len(http.calls) == 2


def test_403_on_a_feed_is_a_hard_error_with_status():
    rss, _http, _clock = _transport([FakeResp(status=403, content=b"blocked")])
    with pytest.raises(RedditError) as info:
        rss.listing("examplenursing")
    assert info.value.status == 403


def test_info_rejects_more_than_100_ids():
    rss, _http, _clock = _transport([])
    with pytest.raises(ValueError):
        rss.info([f"t3_{i}" for i in range(101)])


# --------------------------------------------------------------------------- #
# RedditClient fallback (transport="auto")
# --------------------------------------------------------------------------- #
def _client(tmp_path, json_responses, rss_responses, transport="auto"):
    notices: list[str] = []
    client = RedditClient(
        cache_dir=tmp_path / "cache",
        cache_ttl=0,
        min_interval=0,
        transport=transport,
        log=notices.append,
    )
    client._http = FakeHttp(json_responses)
    client._sleep = lambda *_: None
    rss, rss_http, _clock = _transport(rss_responses, min_interval=0.0)
    client._rss = rss
    return client, client._http, rss_http, notices


def test_auto_switches_to_rss_after_a_json_403_and_stays_there(tmp_path):
    client, json_http, rss_http, notices = _client(
        tmp_path,
        [FakeResp(status=403, text="<html>blocked</html>")],
        [FakeResp(content=_feed("listing_new.xml")), FakeResp(content=_feed("thread.xml"))],
    )
    posts = client.listing("examplenursing")
    comments = client.comments("t3_aaa111", limit=40)

    assert [p["name"] for p in posts] == ["t3_aaa111", "t3_bbb222", "t3_ccc333"]
    assert len(comments) == 3
    assert len(json_http.calls) == 1  # one refused .json call, then RSS only
    assert len(rss_http.calls) == 2
    assert client.using_rss is True
    assert client.transport_in_use == "rss"
    assert len(notices) == 1 and "RSS" in notices[0]


def test_json_transport_never_falls_back(tmp_path):
    client, _json_http, rss_http, _notices = _client(
        tmp_path, [FakeResp(status=403, text="blocked")], [], transport="json"
    )
    with pytest.raises(RedditError) as info:
        client.listing("examplenursing")
    assert info.value.status == 403
    assert rss_http.calls == []


def test_rss_transport_never_calls_json(tmp_path):
    client, json_http, rss_http, _notices = _client(
        tmp_path, [], [FakeResp(content=_feed("info.xml"))], transport="rss"
    )
    entries = client.info(["t3_aaa111", "t1_cm0002", "t1_cm0003"])
    assert len(entries) == 2
    assert json_http.calls == []
    assert len(rss_http.calls) == 1


def test_a_404_does_not_trigger_the_fallback(tmp_path):
    client, _json_http, rss_http, _notices = _client(
        tmp_path, [FakeResp(status=404, text="not found")], []
    )
    with pytest.raises(RedditError):
        client.listing("examplenursing")
    assert rss_http.calls == []
    assert client.using_rss is False


def test_default_user_agent_is_descriptive():
    assert "prospector" in DEFAULT_USER_AGENT
    assert "github.com/011-sam-110/Prospector" in DEFAULT_USER_AGENT


# --------------------------------------------------------------------------- #
# Pacing after failures, unavailable comment feeds, request counts
# (2026-10-04 live run: from 18:52 every request got 429 then 403)
# --------------------------------------------------------------------------- #
def test_a_403_makes_the_next_request_wait_a_full_window():
    rss, http, clock = _transport(
        [FakeResp(status=403, content=b"blocked"), FakeResp(content=_feed("info.xml"))],
        min_interval=20.0,
    )
    with pytest.raises(RedditError):
        rss.listing("examplenursing")
    rss.info(["t3_aaa111"])
    # No rate headers on the 403: wait a full 60 s window, not the 20 s interval.
    assert clock.sleeps == [60.0]
    assert len(http.calls) == 2


def test_consecutive_failures_double_the_wait():
    rss, http, clock = _transport(
        [
            FakeResp(status=429),
            FakeResp(status=429),
            FakeResp(status=429),
            FakeResp(content=_feed("info.xml")),
        ],
        min_interval=0.0,
    )
    assert len(rss.info(["t3_aaa111"])) == 2
    assert clock.sleeps == [60.0, 120.0, 240.0]


def test_failure_wait_uses_the_reset_header_and_is_capped_at_10_minutes():
    rss, _http, _clock = _transport([], min_interval=0.0)
    waits = []
    for _ in range(6):
        before = rss._monotonic()
        rss._record_failure(FakeResp(status=429, headers={"x-ratelimit-reset": "52"}))
        waits.append(rss._not_before - before)
    assert waits == [53.0, 106.0, 212.0, 424.0, 600.0, 600.0]


def test_a_success_resets_the_failure_count():
    rss, _http, clock = _transport(
        [
            FakeResp(status=429),
            FakeResp(content=_feed("info.xml")),
            FakeResp(status=429),
            FakeResp(content=_feed("info.xml")),
        ],
        min_interval=0.0,
    )
    rss.info(["t3_aaa111"])
    rss.info(["t3_aaa111"])
    assert clock.sleeps == [60.0, 60.0]


def test_a_403_on_a_comment_feed_means_unavailable_and_is_not_retried():
    from prospector.errors import CommentsUnavailable

    rss, http, _clock = _transport([FakeResp(status=403, content=b"blocked")])
    with pytest.raises(CommentsUnavailable):
        rss.comments("t3_aaa111", limit=40)
    assert len(http.calls) == 1


def test_requests_are_counted_by_outcome():
    rss, _http, _clock = _transport(
        [
            FakeResp(status=429),
            FakeResp(content=_feed("info.xml")),
            FakeResp(status=403, content=b"blocked"),
        ],
        min_interval=0.0,
    )
    rss.info(["t3_aaa111"])
    with pytest.raises(RedditError):
        rss.listing("examplenursing")
    assert rss.request_counts() == {"ok": 1, "http_403": 1, "http_429": 1, "other": 0}


def test_the_transport_stops_after_six_refusals_in_a_row():
    rss, http, _clock = _transport(
        [FakeResp(status=403, content=b"blocked") for _ in range(6)], min_interval=0.0
    )
    for _ in range(6):
        with pytest.raises(RedditError):
            rss.listing("examplenursing")
    with pytest.raises(RedditError) as info:
        rss.listing("examplenursing")
    assert info.value.blocked is True
    assert len(http.calls) == 6  # the seventh call sent nothing


def test_the_switch_to_rss_waits_a_full_window_after_the_json_403(tmp_path):
    client, _json_http, rss_http, _notices = _client(
        tmp_path,
        [FakeResp(status=403, text="<html>blocked</html>")],
        [FakeResp(content=_feed("listing_new.xml"))],
    )
    clock = FakeClock()
    client.rss._monotonic = clock.monotonic
    client.rss._sleep = clock.sleep
    client.listing("examplenursing")
    assert clock.sleeps == [60.0]
    assert client.request_stats() == {"ok": 1, "http_403": 1, "http_429": 0, "other": 0}
