"""Offline unit tests for the deterministic lexicon scorer.

Pure functions, no network, no LLM — everything here is exact arithmetic over
crafted texts. We assert both the numeric ``pain_score`` and *which* raw
patterns fired, since explainability is the whole point of the scorer.
"""

from __future__ import annotations

import logging
import re

from prospector.models import Item, LexiconRule, Match
from prospector.scorer import compile_lexicon, score_item, score_text


# --------------------------------------------------------------------------- #
# Helpers / fixtures                                                          #
# --------------------------------------------------------------------------- #
def _rules() -> list[LexiconRule]:
    """A small, well-understood lexicon used across several tests."""
    return [
        LexiconRule(pattern="i wish (there was|we had)", weight=3),
        LexiconRule(pattern="still (fax|faxing|paper)", weight=3),
        LexiconRule(pattern="workaround", weight=2),
        LexiconRule(pattern="manual(ly)?", weight=1),
    ]


def _patterns(matches: list[Match]) -> list[str]:
    return [m.pattern for m in matches]


# --------------------------------------------------------------------------- #
# compile_lexicon                                                            #
# --------------------------------------------------------------------------- #
def test_compile_lexicon_preserves_raw_pattern_and_weight():
    compiled = compile_lexicon(_rules())
    assert len(compiled) == 4
    # raw source string round-trips via Pattern.pattern
    assert compiled[0][0].pattern == "i wish (there was|we had)"
    assert compiled[0][1] == 3.0
    # all compiled patterns are case-insensitive
    for pat, _ in compiled:
        assert pat.flags & re.IGNORECASE


def test_compile_lexicon_skips_invalid_regex(caplog):
    rules = [
        LexiconRule(pattern="valid", weight=1),
        LexiconRule(pattern="unbalanced (group", weight=5),  # invalid regex
        LexiconRule(pattern="also[valid", weight=2),  # unterminated char class
        LexiconRule(pattern="another", weight=1),
    ]
    with caplog.at_level(logging.WARNING):
        compiled = compile_lexicon(rules)

    # only the two valid rules survive
    raws = [pat.pattern for pat, _ in compiled]
    assert raws == ["valid", "another"]
    # and we warned (did not crash) about the bad ones
    assert "unbalanced (group" in caplog.text
    assert "also[valid" in caplog.text


def test_compile_lexicon_empty_is_empty():
    assert compile_lexicon([]) == []


# --------------------------------------------------------------------------- #
# score_text                                                                 #
# --------------------------------------------------------------------------- #
def test_score_text_sums_distinct_rule_weights():
    compiled = compile_lexicon(_rules())
    text = "Honestly I wish there was a better tool. We still fax everything."
    total, matches = score_text(text, compiled)

    # 3 (i wish there was) + 3 (still fax) = 6
    assert total == 6.0
    assert _patterns(matches) == [
        "i wish (there was|we had)",
        "still (fax|faxing|paper)",
    ]
    # Match objects carry the matching weights
    assert matches[0] == Match(pattern="i wish (there was|we had)", weight=3.0)


def test_score_text_rule_counts_at_most_once():
    """A pattern appearing many times still contributes its weight only once."""
    compiled = compile_lexicon([LexiconRule(pattern="workaround", weight=2)])
    text = "workaround after workaround after another workaround forever"
    total, matches = score_text(text, compiled)

    assert text.count("workaround") == 3  # sanity: it really repeats
    assert total == 2.0  # but scored once
    assert len(matches) == 1
    assert matches[0].pattern == "workaround"


def test_score_text_is_case_insensitive():
    compiled = compile_lexicon([LexiconRule(pattern="manual(ly)?", weight=1)])
    total, matches = score_text("Everything is done MANUALLY here", compiled)
    assert total == 1.0
    assert _patterns(matches) == ["manual(ly)?"]


def test_score_text_no_match_is_zero():
    compiled = compile_lexicon(_rules())
    total, matches = score_text("a perfectly cheerful sentence", compiled)
    assert total == 0.0
    assert matches == []


def test_score_text_empty_text():
    compiled = compile_lexicon(_rules())
    assert score_text("", compiled) == (0.0, [])
    assert score_text(None, compiled) == (0.0, [])  # type: ignore[arg-type]


def test_score_text_preserves_lexicon_order():
    """Matches come back in lexicon order, not text order."""
    compiled = compile_lexicon(_rules())
    # "workaround" (rule 3) appears in the text BEFORE the "i wish" (rule 1)
    text = "First the workaround, and only later did I wish we had a fix."
    total, matches = score_text(text, compiled)
    assert total == 5.0  # 2 + 3
    assert _patterns(matches) == [
        "i wish (there was|we had)",  # rule index 0 — first in lexicon
        "workaround",  # rule index 2
    ]


# --------------------------------------------------------------------------- #
# score_item                                                                 #
# --------------------------------------------------------------------------- #
def _item(title: str | None, body: str) -> Item:
    return Item(
        id="t3_abc",
        kind="post",
        subreddit="nursing",
        author="someone",
        created_utc=0,
        permalink="https://www.reddit.com/r/nursing/comments/abc/x/",
        title=title,
        body=body,
    )


def test_score_item_sets_pain_and_matches_and_returns_same_object():
    compiled = compile_lexicon(_rules())
    item = _item("I wish there was an app", "We still fax orders manually.")
    returned = score_item(item, compiled)

    assert returned is item  # mutated in place, returned for chaining
    # title+body scored together via Item.text:
    # 3 (i wish there was) + 3 (still fax) + 1 (manually) = 7
    assert item.pain_score == 7.0
    assert _patterns(item.matches) == [
        "i wish (there was|we had)",
        "still (fax|faxing|paper)",
        "manual(ly)?",
    ]


def test_score_item_uses_combined_title_and_body():
    """A rule split across title vs body is found because Item.text joins them."""
    compiled = compile_lexicon([LexiconRule(pattern="fax everything", weight=4)])
    # 'fax' ends the title, 'everything' starts the body — Item.text joins with \n
    item = _item("they still make us fax", "everything in this unit")
    score_item(item, compiled)
    # The join is a newline, so "fax everything" does NOT span the boundary.
    assert item.pain_score == 0.0

    # But a match wholly within the body is found:
    compiled2 = compile_lexicon([LexiconRule(pattern="this unit", weight=4)])
    item2 = _item("they still make us fax", "everything in this unit")
    score_item(item2, compiled2)
    assert item2.pain_score == 4.0


def test_score_item_no_matches_zeroes_out():
    compiled = compile_lexicon(_rules())
    item = _item("All good", "Nothing to complain about today.")
    # seed a stale score/matches to prove they get overwritten
    item.pain_score = 99.0
    item.matches = [Match(pattern="stale", weight=99.0)]
    score_item(item, compiled)
    assert item.pain_score == 0.0
    assert item.matches == []
