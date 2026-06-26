"""Deterministic weighted-lexicon scoring.

The scorer is the explainable, reproducible heart of prospector: a post's
``pain_score`` is simply the sum of the weights of the lexicon rules that fire
on its text, and the firing rules are kept around as :class:`Match` objects so
every score can be traced back to the exact patterns that produced it.

Everything here is pure (no I/O, no globals) and case-insensitive. Each rule
counts **at most once** per item, regardless of how many times its pattern
occurs — the lexicon measures *which* signals are present, not how loud.
"""

from __future__ import annotations

import logging
import re

from prospector.models import Item, LexiconRule, Match

__all__ = ["compile_lexicon", "score_text", "score_item"]

logger = logging.getLogger(__name__)


def compile_lexicon(rules: list[LexiconRule]) -> list[tuple[re.Pattern, float]]:
    """Compile each rule's regex (case-insensitive) into ``(pattern, weight)``.

    A rule whose ``pattern`` is not a valid regular expression is skipped with a
    logged warning rather than aborting the run — a single fat-fingered profile
    entry should never take down a whole sweep. The compiled :class:`re.Pattern`
    carries the raw source string in ``pattern.pattern``, which is what gets
    recorded in each :class:`Match`.
    """
    compiled: list[tuple[re.Pattern, float]] = []
    for rule in rules:
        raw = rule.pattern
        if raw is None:
            logger.warning("Skipping lexicon rule with no pattern: %r", rule)
            continue
        try:
            pattern = re.compile(str(raw), re.IGNORECASE)
        except re.error as exc:
            logger.warning(
                "Skipping invalid lexicon regex %r: %s", raw, exc
            )
            continue
        try:
            weight = float(rule.weight)
        except (TypeError, ValueError):
            logger.warning(
                "Skipping lexicon rule with non-numeric weight %r: %r",
                rule.weight,
                raw,
            )
            continue
        compiled.append((pattern, weight))
    return compiled


def score_text(
    text: str, compiled: list[tuple[re.Pattern, float]]
) -> tuple[float, list[Match]]:
    """Score ``text`` against a compiled lexicon.

    Returns ``(total_score, matches)`` where ``total_score`` is the sum of the
    weights of every rule that finds at least one match in ``text``, and
    ``matches`` is the list of :class:`Match` objects (one per firing rule, in
    lexicon order). Each rule contributes its weight **once** even if its
    pattern occurs multiple times. ``Match.pattern`` is the rule's raw regex
    source string.
    """
    if not text:
        return 0.0, []

    total = 0.0
    matches: list[Match] = []
    for pattern, weight in compiled:
        if pattern.search(text) is not None:
            total += weight
            matches.append(Match(pattern=pattern.pattern, weight=weight))
    return total, matches


def score_item(item: Item, compiled: list[tuple[re.Pattern, float]]) -> Item:
    """Score ``item.text`` in place and return the item.

    Sets ``item.pain_score`` and ``item.matches`` from the compiled lexicon and
    returns the same :class:`Item` for convenient chaining. Mutates the item
    rather than copying so callers can score large batches cheaply.
    """
    total, matches = score_text(item.text, compiled)
    item.pain_score = total
    item.matches = matches
    return item
