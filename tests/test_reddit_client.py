"""Offline tests for :mod:`prospector.reddit_client`.

No live network: every HTTP round-trip is replaced by a fake transport or by
monkeypatching ``get_json`` / ``_fetch``, and the throttle clock + sleep are
faked so nothing actually blocks.
"""

from __future__ import annotations

import json

import pytest

from prospector.reddit_client import (
    DEFAULT_USER_AGENT,
    OAUTH_BASE,
    PUBLIC_BASE,
    RedditClient,
    RedditError,
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeResp:
    """Minimal stand-in for an httpx.Response."""

    def __init__(self, status=200, json_data=None, text="", headers=None):
        self.status_code = status
        self._json = json_data
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class FakeHttp:
    """Records calls and replays a queued list of responses (or raises)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.get_calls = []
        self.post_calls = []
        self.token_resp = None

    def get(self, url, params=None, headers=None):
        self.get_calls.append((url, params, headers))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def post(self, url, data=None, auth=None, headers=None):
        self.post_calls.append((url, data, auth, headers))
        return self.token_resp

    def close(self):
        pass


def _no_real_io(client):
    """Disable real sleeping; throttle uses a trivial fake clock."""
    client._sleep = lambda *_: None
    client.min_interval = 0


# --------------------------------------------------------------------------- #
# Fixtures (canned Reddit JSON shapes)
# --------------------------------------------------------------------------- #
def _post_child(pid, title, body="", num_comments=0, score=1):
    return {
        "kind": "t3",
        "data": {
            "id": pid,
            "name": f"t3_{pid}",
            "subreddit": "nursing",
            "author": "alice",
            "created_utc": 1700000000,
            "permalink": f"/r/nursing/comments/{pid}/x/",
            "title": title,
            "selftext": body,
            "score": score,
            "num_comments": num_comments,
        },
    }


def _listing(children, after=None):
    return {"kind": "Listing", "data": {"after": after, "children": children}}


def _comment_child(cid, body, score=1, replies=None):
    return {
        "kind": "t1",
        "data": {
            "id": cid,
            "name": f"t1_{cid}",
            "subreddit": "nursing",
            "author": "bob",
            "created_utc": 1700000001,
            "permalink": f"/r/nursing/comments/p1/x/{cid}/",
            "body": body,
            "score": score,
            "replies": replies if replies is not None else "",
        },
    }


# --------------------------------------------------------------------------- #
# Construction / defaults
# --------------------------------------------------------------------------- #
def test_default_user_agent_and_unauth_interval(monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    c = RedditClient(cache_ttl=0)
    assert c.user_agent == DEFAULT_USER_AGENT
    assert c.min_interval == 6.0
    assert c.authenticated is False
    c.close()


def test_oauth_creds_lower_default_interval(monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    c = RedditClient(client_id="id", client_secret="sec", cache_ttl=0)
    assert c.min_interval == 0.6
    c.close()


def test_explicit_min_interval_overrides():
    c = RedditClient(min_interval=1.5, cache_ttl=0)
    assert c.min_interval == 1.5
    c.close()


# --------------------------------------------------------------------------- #
# Parsing: listing / search / comments (get_json monkeypatched)
# --------------------------------------------------------------------------- #
def test_listing_parses_t3_children(monkeypatch):
    c = RedditClient(cache_ttl=0)
    payload = _listing([_post_child("p1", "first"), _post_child("p2", "second")])
    monkeypatch.setattr(c, "get_json", lambda path, params=None: payload)
    posts = c.listing("nursing")
    assert [p["id"] for p in posts] == ["p1", "p2"]
    assert posts[0]["title"] == "first"
    c.close()


def test_listing_follows_after_across_pages(monkeypatch):
    c = RedditClient(cache_ttl=0)
    pages = [
        _listing([_post_child("p1", "a")], after="t3_p1"),
        _listing([_post_child("p2", "b")], after=None),
    ]
    calls = []

    def fake_get(path, params=None):
        calls.append((path, dict(params or {})))
        return pages[len(calls) - 1]

    monkeypatch.setattr(c, "get_json", fake_get)
    posts = c.listing("nursing", pages=3)
    # Stops after page 2 because `after` became None.
    assert len(calls) == 2
    assert [p["id"] for p in posts] == ["p1", "p2"]
    assert calls[1][1].get("after") == "t3_p1"
    c.close()


def test_listing_limit_capped_at_100(monkeypatch):
    c = RedditClient(cache_ttl=0)
    seen = {}

    def fake_get(path, params=None):
        seen.update(params or {})
        return _listing([])

    monkeypatch.setattr(c, "get_json", fake_get)
    c.listing("nursing", limit=500)
    assert seen["limit"] == 100
    c.close()


def test_search_restrict_sr_and_path(monkeypatch):
    c = RedditClient(cache_ttl=0)
    captured = {}

    def fake_get(path, params=None):
        captured["path"] = path
        captured["params"] = dict(params or {})
        return _listing([_post_child("s1", "hit")])

    monkeypatch.setattr(c, "get_json", fake_get)
    posts = c.search("fax machine", subreddit="nursing")
    assert captured["path"] == "/r/nursing/search.json"
    assert captured["params"]["restrict_sr"] == "1"
    assert captured["params"]["q"] == "fax machine"
    assert [p["id"] for p in posts] == ["s1"]
    c.close()


def test_search_sitewide_when_no_subreddit(monkeypatch):
    c = RedditClient(cache_ttl=0)
    captured = {}

    def fake_get(path, params=None):
        captured["path"] = path
        return _listing([])

    monkeypatch.setattr(c, "get_json", fake_get)
    c.search("anything")
    assert captured["path"] == "/search.json"
    c.close()


def test_comments_flatten_skip_more_and_min_score(monkeypatch):
    c = RedditClient(cache_ttl=0)
    deep_reply = _comment_child("c2", "nested", score=5)
    replies_listing = _listing([deep_reply, {"kind": "more", "data": {"count": 3}}])
    top = _comment_child("c1", "top", score=10, replies=replies_listing)
    low = _comment_child("c3", "ignored", score=-2)
    more_stub = {"kind": "more", "data": {"count": 50}}
    post_listing = _listing([_post_child("p1", "the post")])
    comments_listing = _listing([top, low, more_stub])
    payload = [post_listing, comments_listing]

    monkeypatch.setattr(c, "get_json", lambda path, params=None: payload)
    out = c.comments("t3_p1", depth=2, min_score=0)
    ids = [x["id"] for x in out]
    # top + its nested reply + low (>= 0? no, -2 dropped). 'more' stubs skipped.
    assert "c1" in ids
    assert "c2" in ids  # depth 2 reply included
    assert "c3" not in ids  # below min_score 0
    assert all(x.get("kind") != "more" for x in out)
    c.close()


def test_comments_depth_limits_recursion(monkeypatch):
    c = RedditClient(cache_ttl=0)
    level2 = _comment_child("c2", "lvl2", score=5)
    level1_replies = _listing([level2])
    level1 = _comment_child("c1", "lvl1", score=5, replies=level1_replies)
    payload = [_listing([]), _listing([level1])]
    monkeypatch.setattr(c, "get_json", lambda path, params=None: payload)
    out = c.comments("p1", depth=1)
    ids = [x["id"] for x in out]
    assert ids == ["c1"]  # depth=1 → no descent into c2
    c.close()


def test_comments_accepts_bare_id(monkeypatch):
    c = RedditClient(cache_ttl=0)
    captured = {}

    def fake_get(path, params=None):
        captured["path"] = path
        return [_listing([]), _listing([])]

    monkeypatch.setattr(c, "get_json", fake_get)
    c.comments("abc123")
    assert captured["path"] == "/comments/abc123.json"
    c.close()


def test_comments_strips_fullname_prefix(monkeypatch):
    c = RedditClient(cache_ttl=0)
    captured = {}

    def fake_get(path, params=None):
        captured["path"] = path
        return [_listing([]), _listing([])]

    monkeypatch.setattr(c, "get_json", fake_get)
    c.comments("t3_abc123")
    assert captured["path"] == "/comments/abc123.json"
    c.close()


def test_comments_non_list_payload_returns_empty(monkeypatch):
    c = RedditClient(cache_ttl=0)
    monkeypatch.setattr(c, "get_json", lambda path, params=None: {"error": "weird"})
    assert c.comments("p1") == []
    c.close()


# --------------------------------------------------------------------------- #
# On-disk cache
# --------------------------------------------------------------------------- #
def test_cache_hit_avoids_second_fetch(tmp_path, monkeypatch):
    c = RedditClient(cache_dir=tmp_path, cache_ttl=3600)
    calls = {"n": 0}

    def fake_fetch(path, params):
        calls["n"] += 1
        return {"data": {"after": None, "children": []}, "marker": calls["n"]}

    monkeypatch.setattr(c, "_fetch", fake_fetch)
    first = c.get_json("/r/x/new.json", {"limit": 100})
    second = c.get_json("/r/x/new.json", {"limit": 100})
    assert calls["n"] == 1  # second call served from disk
    assert first == second
    # A cache file was actually written.
    assert list(tmp_path.glob("*.json"))
    c.close()


def test_cache_disabled_when_ttl_zero(tmp_path, monkeypatch):
    c = RedditClient(cache_dir=tmp_path, cache_ttl=0)
    calls = {"n": 0}

    def fake_fetch(path, params):
        calls["n"] += 1
        return {"n": calls["n"]}

    monkeypatch.setattr(c, "_fetch", fake_fetch)
    c.get_json("/r/x/new.json")
    c.get_json("/r/x/new.json")
    assert calls["n"] == 2
    assert not list(tmp_path.glob("*.json"))
    c.close()


def test_cache_expiry_refetches(tmp_path, monkeypatch):
    c = RedditClient(cache_dir=tmp_path, cache_ttl=100)
    fake_time = {"t": 1000.0}
    c._now = lambda: fake_time["t"]
    calls = {"n": 0}

    def fake_fetch(path, params):
        calls["n"] += 1
        return {"n": calls["n"]}

    monkeypatch.setattr(c, "_fetch", fake_fetch)
    c.get_json("/r/x/new.json")
    fake_time["t"] = 1000.0 + 200  # past TTL
    c.get_json("/r/x/new.json")
    assert calls["n"] == 2
    c.close()


def test_cache_keyed_by_params(tmp_path, monkeypatch):
    c = RedditClient(cache_dir=tmp_path, cache_ttl=3600)
    calls = {"n": 0}

    def fake_fetch(path, params):
        calls["n"] += 1
        return {"n": calls["n"]}

    monkeypatch.setattr(c, "_fetch", fake_fetch)
    c.get_json("/r/x/new.json", {"limit": 100})
    c.get_json("/r/x/new.json", {"limit": 50})  # different params → miss
    assert calls["n"] == 2
    c.close()


# --------------------------------------------------------------------------- #
# Throttle (fake clock)
# --------------------------------------------------------------------------- #
def test_throttle_sleeps_for_remaining_interval():
    c = RedditClient(cache_ttl=0)
    c.min_interval = 6.0
    times = {"t": 0.0}
    slept = []
    c._monotonic = lambda: times["t"]
    c._sleep = lambda s: slept.append(s)

    c._throttle()  # first call: no prior request → no sleep
    assert slept == []
    times["t"] = 2.0
    c._throttle()  # elapsed 2.0 → must wait 4.0
    assert slept == [4.0]
    c.close()


def test_throttle_no_sleep_when_interval_zero():
    c = RedditClient(cache_ttl=0)
    c.min_interval = 0
    slept = []
    c._sleep = lambda s: slept.append(s)
    c._monotonic = lambda: 0.0
    c._throttle()
    c._throttle()
    assert slept == []
    c.close()


def test_throttle_no_sleep_when_interval_already_elapsed():
    c = RedditClient(cache_ttl=0)
    c.min_interval = 5.0
    times = {"t": 0.0}
    slept = []
    c._monotonic = lambda: times["t"]
    c._sleep = lambda s: slept.append(s)
    c._throttle()
    times["t"] = 100.0  # way past the interval
    c._throttle()
    assert slept == []
    c.close()


# --------------------------------------------------------------------------- #
# _fetch over a fake transport
# --------------------------------------------------------------------------- #
def test_fetch_unauthenticated_uses_public_host(monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    c = RedditClient(cache_ttl=0)
    _no_real_io(c)
    c._http = FakeHttp([FakeResp(200, json_data=_listing([]))])
    data = c.get_json("/r/x/new.json", {"limit": 5})
    url, params, headers = c._http.get_calls[0]
    assert url.startswith(PUBLIC_BASE)
    assert headers["User-Agent"] == DEFAULT_USER_AGENT
    assert params["raw_json"] == 1  # injected
    assert "Authorization" not in headers
    assert data["kind"] == "Listing"
    c.close()


def test_fetch_authenticated_uses_oauth_host_and_bearer(monkeypatch):
    c = RedditClient(client_id="id", client_secret="sec", cache_ttl=0)
    _no_real_io(c)
    http = FakeHttp([FakeResp(200, json_data=_listing([]))])
    http.token_resp = FakeResp(
        200, json_data={"access_token": "tok123", "expires_in": 3600}
    )
    c._http = http
    assert c.authenticated is True
    c.get_json("/r/x/new.json")
    url, _params, headers = http.get_calls[0]
    assert url.startswith(OAUTH_BASE)
    assert headers["Authorization"] == "bearer tok123"
    assert len(http.post_calls) == 1  # token minted once and cached
    assert http.post_calls[0][0].endswith("/api/v1/access_token")
    c.close()


def test_token_failure_yields_unauthenticated(monkeypatch):
    c = RedditClient(client_id="id", client_secret="sec", cache_ttl=0)
    http = FakeHttp([])
    http.token_resp = FakeResp(403, json_data={"error": "invalid"})
    c._http = http
    assert c.authenticated is False
    c.close()


def test_429_then_success_retries_with_backoff(monkeypatch):
    c = RedditClient(cache_ttl=0)
    c.min_interval = 0
    slept = []
    c._sleep = lambda s: slept.append(s)
    c._monotonic = lambda: 0.0
    c._http = FakeHttp(
        [
            FakeResp(429, headers={"Retry-After": "1"}),
            FakeResp(200, json_data={"ok": True}),
        ]
    )
    data = c.get_json("/r/x/new.json")
    assert data == {"ok": True}
    assert slept == [1.0]  # honoured Retry-After
    c.close()


def test_429_exhausts_retries_raises(monkeypatch):
    c = RedditClient(cache_ttl=0)
    c.min_interval = 0
    c._sleep = lambda *_: None
    c._monotonic = lambda: 0.0
    c._http = FakeHttp([FakeResp(429) for _ in range(10)])
    with pytest.raises(RedditError):
        c.get_json("/r/x/new.json")
    c.close()


def test_blocked_html_raises_reddit_error():
    c = RedditClient(cache_ttl=0)
    _no_real_io(c)
    c._http = FakeHttp(
        [FakeResp(200, json_data=None, text="<!DOCTYPE html><html>blocked</html>")]
    )
    with pytest.raises(RedditError):
        c.get_json("/r/x/new.json")
    c.close()


def test_http_error_status_raises():
    c = RedditClient(cache_ttl=0)
    _no_real_io(c)
    c._http = FakeHttp([FakeResp(404, text="not found")])
    with pytest.raises(RedditError):
        c.get_json("/r/x/missing.json")
    c.close()


def test_inline_error_body_raises():
    c = RedditClient(cache_ttl=0)
    _no_real_io(c)
    c._http = FakeHttp([FakeResp(200, json_data={"error": 403, "message": "nope"})])
    with pytest.raises(RedditError):
        c.get_json("/r/x/new.json")
    c.close()


def test_comments_endpoint_returns_list_through_get_json():
    # get_json must transparently return Reddit's array payload for /comments.
    c = RedditClient(cache_ttl=0)
    _no_real_io(c)
    payload = [_listing([]), _listing([_comment_child("c1", "hi", score=3)])]
    c._http = FakeHttp([FakeResp(200, json_data=payload)])
    out = c.comments("p1")
    assert [x["id"] for x in out] == ["c1"]
    c.close()


def test_write_then_read_cache_roundtrip_with_list(tmp_path):
    c = RedditClient(cache_dir=tmp_path, cache_ttl=3600)
    path = c._cache_path("/comments/p1.json", {"limit": 100})
    c._write_cache(path, [{"a": 1}, {"b": 2}])
    got = c._read_cache(path)
    assert got == [{"a": 1}, {"b": 2}]
    # File content is a ts-stamped envelope.
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "ts" in raw and "data" in raw
    c.close()
