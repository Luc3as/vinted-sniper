"""The title-filter-only baseline: the gate a sweep switched off, re-applied on read.

The load-bearing test is `test_a_partial_keyword_match_ranks_but_does_not_pass_the_gate`.
A sweep keeps a listing whose title says only half of what was asked for — `score_title()`
gives it a non-zero *rank*. The baseline is a *gate*: half is a miss. Those two answers
disagreeing on the same candidate is the entire reason the milestone can claim the sweep
beat the title filter, so it is pinned here rather than left to the reader.
"""

from __future__ import annotations

from typing import Any

from vinted_sniper.db.repo import SweepCandidate, SweepRun
from vinted_sniper.engine import filters, sweep
from vinted_sniper.engine.sweep_report import baseline_title_hits, build_report

KEYWORDS = ["patagonia", "torrentshell"]


def candidate(item_id: int, title: str, **overrides: Any) -> SweepCandidate:
    fields: dict[str, Any] = {
        "item_id": item_id,
        "title": title,
        "url": f"https://www.vinted.sk/items/{item_id}",
        "rank_score": sweep.score_title(title, KEYWORDS),
        "price": 45.0,
        "total_price": 50.2,
        "currency": "EUR",
    }
    fields.update(overrides)
    return SweepCandidate(**fields)


def run(**overrides: Any) -> SweepRun:
    fields: dict[str, Any] = {
        "id": 7,
        "tld": "sk",
        "params": {"search_text": "patagonia torrentshell", "order": "relevance"},
        "keywords": list(KEYWORDS),
        "started_at": 1_760_000_000,
        "status": "done",
        "pages_fetched": 3,
        "items_seen": 180,
        "candidates": 24,
        "funnel": {"seen": 180, "kept": 24, "triaged": 24},
        "tokens": 4_200,
        "cost_eur": 0.0431,
    }
    fields.update(overrides)
    return SweepRun(**fields)


# --- The gate itself ----------------------------------------------------------------


def test_every_keyword_present_in_any_case_is_a_baseline_hit() -> None:
    hits = baseline_title_hits([candidate(1, "PATAGONIA Torrentshell 3L bunda")], KEYWORDS)

    assert [c.item_id for c in hits] == [1]


def test_a_partial_keyword_match_ranks_but_does_not_pass_the_gate() -> None:
    """The distinction the whole baseline rests on: ranking is not eliminating.

    "Patagonia bunda" says one of the two words. `score_title` scores it 0.5, so the sweep
    keeps it and ranks it mid-list. The title filter alone would have thrown it away.
    """
    partial = candidate(2, "Patagonia bunda nepremokava")

    assert partial.rank_score == 0.5
    assert baseline_title_hits([partial], KEYWORDS) == []


def test_no_keywords_means_no_gate_so_every_candidate_is_a_baseline_hit() -> None:
    """Same answer `filters._missing_keyword` gives an empty `required` list."""
    candidates = [candidate(1, "Patagonia Torrentshell"), candidate(2, "Nike windbreaker")]

    assert baseline_title_hits(candidates, []) == candidates


def test_an_accented_title_answers_exactly_as_the_filter_gate_does() -> None:
    """The extraction and the gate cannot drift, because there is one implementation.

    `score_title` folds accents, the gate does not. "Kurtká" is a keyword miss for the
    gate and would be a match for the ranker — this asserts the baseline follows the gate.
    """
    title = "Patagonia Kurtká Torrentshell"

    assert baseline_title_hits([candidate(3, title)], ["kurtka"]) == []
    assert filters.missing_keyword_in_title(title, ["kurtka"]) == "kurtka"
    assert sweep.score_title(title, ["kurtka"]) == 1.0


def test_a_blank_required_word_is_skipped_rather_than_matching_nothing() -> None:
    """`.strip()` on each word, empty ones ignored — the gate's boundary behaviour."""
    assert baseline_title_hits([candidate(4, "Patagonia Torrentshell")], ["  ", ""]) != []


# --- The report ---------------------------------------------------------------------


def test_build_report_orders_candidates_by_triage_not_by_stored_position() -> None:
    """Stored `position` is the funnel's order; the report must re-derive the triage one.

    Item 1 was stored first with the better title rank. Item 2 was stored second but the
    photo check recognised it, and `matches_target` outranks `rank_score`.
    """
    stored = [
        candidate(1, "Patagonia Torrentshell", position=0, matches_target=False, confidence=0.9),
        candidate(2, "Patagonia bunda", position=1, matches_target=True, confidence=0.8),
    ]

    report = build_report(run(), stored)

    assert [c.item_id for c in report.candidates] == [2, 1]
    assert [c.item_id for c in report.candidates] == [c.item_id for c in sweep.triage_order(stored)]


def test_build_report_carries_the_run_counts_and_what_the_run_spent() -> None:
    report = build_report(run(), [])

    assert (report.sweep_id, report.status) == (7, "done")
    assert (report.pages_fetched, report.items_seen, report.candidates_kept) == (3, 180, 24)
    assert report.funnel == {"seen": 180, "kept": 24, "triaged": 24}
    assert (report.tokens, report.cost_eur) == (4_200, 0.0431)
    assert report.baseline_count == 0


def test_build_report_computes_the_baseline_over_the_same_candidate_set() -> None:
    stored = [
        candidate(1, "Patagonia Torrentshell 3L", matches_target=True, confidence=0.9),
        candidate(2, "Patagonia bunda", matches_target=True, confidence=0.8),
        candidate(3, "Torrentshell jacket", matches_target=False),
    ]

    report = build_report(run(), stored)

    assert len(report.candidates) == 3
    assert [c.item_id for c in report.baseline] == [1]
    assert report.baseline_count == 1


def test_build_report_does_not_mutate_the_run_or_the_candidate_list_it_was_given() -> None:
    """It is a reader. Callers hand it live rows and must get them back untouched."""
    stored = [
        candidate(1, "Patagonia Torrentshell", matches_target=False),
        candidate(2, "Patagonia bunda", matches_target=True),
    ]
    source = run()

    build_report(source, stored)

    assert [c.item_id for c in stored] == [1, 2]
    assert source.keywords == KEYWORDS
