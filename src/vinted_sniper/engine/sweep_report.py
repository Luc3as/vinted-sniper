"""What a finished sweep actually found, measured against the gate it switched off.

The milestone asks a sweep to beat "the title filter alone". That baseline is not a new
rule to invent — it is `filters._required`, the gate `filters.SWEEP_GATES` deliberately
leaves out, re-applied to the candidates the sweep kept. So this module calls
`filters.missing_keyword_in_title()` rather than imitating it: `score_title()` folds
accents and returns a *ranking* fraction where 0.0 still means "kept, ranks last", while
the gate is a plain lowercase containment test that *eliminates*. A hand-rolled
`"word" in title.lower()` would sooner or later disagree with one of the two.

Everything here is pure. It takes rows somebody else has already read — a `SweepRun` and
its `SweepCandidate`s — so there is no database, no network and no clock in the way of
testing it. `dev/sweep_report.py` is the thin read-only shell that supplies the rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vinted_sniper.db.repo import SweepCandidate, SweepRun
from vinted_sniper.engine import filters, sweep


@dataclass(frozen=True, slots=True)
class SweepReport:
    """One sweep's counts, its candidates in triage order, and the title-only baseline."""

    sweep_id: int
    status: str
    keywords: list[str]
    pages_fetched: int
    items_seen: int
    candidates_kept: int
    # Stage name to survivors, in the order the sweep applied the stages.
    funnel: dict[str, int] = field(default_factory=dict)
    tokens: int = 0
    cost_eur: float = 0.0
    # Best first, by the same rule `judge_sweep()` handed them over in.
    candidates: list[SweepCandidate] = field(default_factory=list)
    # The subset a required-keywords filter on the title would have left standing.
    baseline: list[SweepCandidate] = field(default_factory=list)

    @property
    def baseline_count(self) -> int:
        """How many the title filter alone would have found in this same result set."""
        return len(self.baseline)


def baseline_title_hits(
    candidates: list[SweepCandidate], keywords: list[str]
) -> list[SweepCandidate]:
    """The candidates a title-only required-keywords filter would have kept.

    Exactly `filters._required`'s rule: every keyword must appear in the title, matched
    case-insensitively and literally. No keywords means no gate, so everything survives —
    the same answer `_missing_keyword` gives an empty `required` list.
    """
    return [c for c in candidates if filters.missing_keyword_in_title(c.title, keywords) is None]


def build_report(run: SweepRun, candidates: list[SweepCandidate]) -> SweepReport:
    """A report for one sweep, from its run row and the candidates it stored.

    Candidates come back in `sweep.triage_order()` — the triage order is derived on read
    rather than stored (`sweep_candidates.position` keeps the funnel's order), so this
    asks the one helper that knows the rule instead of re-sorting by hand. The baseline is
    computed over the same list, so both numbers describe the same result set.
    """
    ordered = sweep.triage_order(candidates)
    return SweepReport(
        sweep_id=run.id,
        status=run.status,
        keywords=list(run.keywords),
        pages_fetched=run.pages_fetched,
        items_seen=run.items_seen,
        candidates_kept=run.candidates,
        funnel=dict(run.funnel),
        tokens=run.tokens,
        cost_eur=run.cost_eur,
        candidates=ordered,
        baseline=baseline_title_hits(ordered, run.keywords),
    )
