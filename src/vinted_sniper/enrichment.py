"""Letting something smarter than a regex look at a listing before it is sent.

The app itself knows only what the catalog says: a title, a price, a few photos, a seller.
Whether the jacket in the photos is the model the search was after, what it costs new, and
whether the price is a bargain or a warning sign are questions for a model that can look
at pictures and search the web — which is not this app's job, and should not be.

So the loop is: a listing's webhook goes out at once with a callback address; the plain
notification is held for a short while; whatever posts a verdict to the callback in time
gets it woven into the message (a deal score, the retail price, a one-line verdict), and
if nothing arrives the message goes out as it always did. Silence from the outside brain
costs a delay, never an alert.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from vinted_sniper import i18n
from vinted_sniper.i18n import Translator


class EnrichmentIn(BaseModel):
    """What the callback accepts. Everything optional: a partial verdict beats none."""

    score: int | None = Field(default=None, ge=0, le=100, description="Deal score, 0-100.")
    model: str | None = Field(default=None, max_length=200, description="Identified product.")
    retail_price: Decimal | None = Field(default=None, ge=0)
    retail_source: str | None = Field(default=None, max_length=500)
    matches_query: bool | None = Field(
        default=None, description="Is it what the search was actually after?"
    )
    risk: str | None = Field(default=None, max_length=300, description="Authenticity worries.")
    verdict: str | None = Field(default=None, max_length=500, description="One line for a human.")


@dataclass(frozen=True, slots=True)
class Enrichment:
    """What was stored for a listing, as the senders read it."""

    score: int | None
    model: str | None
    retail_price: Decimal | None
    retail_source: str | None
    matches_query: bool | None
    risk: str | None
    verdict: str | None
    enriched_at: int

    @classmethod
    def from_row(cls, row: Any) -> Enrichment | None:
        if row["enriched_at"] is None:
            return None
        retail = row["enrich_retail_price"]
        matches = row["enrich_matches_query"]
        return cls(
            score=row["enrich_score"],
            model=row["enrich_model"],
            retail_price=Decimal(str(retail)) if retail is not None else None,
            retail_source=row["enrich_retail_source"],
            matches_query=bool(matches) if matches is not None else None,
            risk=row["enrich_risk"],
            verdict=row["enrich_verdict"],
            enriched_at=int(row["enriched_at"]),
        )

    @classmethod
    def from_candidate(cls, candidate: Any) -> Enrichment | None:
        """The same answer, read off a sweep candidate instead of an `items` row.

        A sweep's verdict is the same shape as a standing watch's — that is the point of
        reusing the enrichment flow (D009) — but it lives in `sweep_candidates`, because a
        sweep candidate has no `items` row and must never gain one (D005/MEM007). So the
        two stores need two readers and share one renderer: whatever calls this gets the
        same `summary()` and `lines()` every sender already uses, in every language.

        Typed `Any` rather than `db.repo.SweepCandidate` on purpose: `repo.py` imports
        `EnrichmentIn` from this module, so naming its types here would close a cycle.
        `from_row` is loose for the same reason, one level down.
        """
        if candidate.judged_at is None:
            return None
        retail = candidate.verdict_retail_price
        return cls(
            score=candidate.verdict_score,
            model=candidate.verdict_model,
            retail_price=Decimal(str(retail)) if retail is not None else None,
            retail_source=candidate.verdict_retail_source,
            matches_query=candidate.verdict_matches_query,
            risk=candidate.verdict_risk,
            verdict=candidate.verdict_text,
            enriched_at=int(candidate.judged_at),
        )

    def discount_percent(self, payable: Decimal | None) -> int | None:
        if self.retail_price is None or payable is None or self.retail_price <= 0:
            return None
        return round((self.retail_price - payable) / self.retail_price * 100)

    def is_hot(self, threshold: int) -> bool:
        return self.score is not None and self.score >= threshold

    def is_dull(self, threshold: int) -> bool:
        """Not worth a buzz: scored below the line, or not the product searched for."""
        return (self.score is not None and self.score < threshold) or self.matches_query is False

    def lines(
        self, payable: Decimal | None, currency: str | None, t: Translator = i18n.EN
    ) -> tuple[str, list[str]]:
        """The top line (may be empty) and the detail lines a message appends."""
        details: list[str] = []
        if self.model:
            details.append(t("Looks like: {model}", model=self.model))
        if self.verdict:
            details.append(self.verdict)
        return self.summary(payable, currency, t), details

    def summary(
        self, payable: Decimal | None, currency: str | None, t: Translator = i18n.EN
    ) -> str:
        """The compact line that goes at the top of a message."""
        parts: list[str] = []
        if self.score is not None:
            parts.append(t("deal {score}/100", score=self.score))
        if self.retail_price is not None:
            unit = f" {currency}" if currency else ""
            piece = t("retail ~{price}{currency}", price=f"{self.retail_price:.0f}", currency=unit)
            if (off := self.discount_percent(payable)) is not None:
                piece += f" · -{off}%"
            parts.append(piece)
        if self.matches_query is False:
            parts.append(t("not the model searched for"))
        if self.risk:
            parts.append(t("risk: {risk}", risk=self.risk))
        return " · ".join(parts)
