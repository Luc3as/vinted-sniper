"""The shape of a mapped search, and how it becomes params.

The mapper answers with names next to every id — `{"id": 90804, "name": "Patagonia"}`, not
a bare `90804`. That redundancy is the whole point: an id alone can only be checked for
existence, but an id *and* the name the model thought it meant can be checked against each
other, and when they disagree the error can say which brand it actually asked for (D008).

`to_params()` is the one place this shape meets the rest of the app. What comes out is the
same `dict[str, str]` a stored `Query.params` holds, so a mapped search can be handed
straight to `VintedClient.search()` or saved as a query without a translation step in
between.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class Named(BaseModel):
    """A taxonomy entry as the mapper reported it: the id, and what it thought it was."""

    id: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=200)


class WatchHints(BaseModel):
    """Title rules for later, when a sweep is promoted to a standing watch.

    Vinted's search cannot express "the title must actually say Torrentshell", so these
    ride along unused until S04 turns a sweep into a saved query. Nothing that builds a
    search request may read them — a sweep ranks on the title, it never filters on it.
    """

    required_keywords: list[str] = Field(default_factory=list, max_length=10)
    title_pattern: str | None = Field(default=None, max_length=200)


class MappedQuery(BaseModel):
    """One shopping intent, as structured search terms.

    Extra keys are ignored rather than rejected so the n8n flow can start returning a new
    field before this app knows about it, and neither side breaks on the other's deploy.
    """

    model_config = ConfigDict(extra="ignore")

    catalog: Named | None = None
    brand: Named | None = None
    sizes: list[Named] = Field(default_factory=list, max_length=10)
    price_to: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, max_length=3)
    search_text: str | None = Field(default=None, max_length=200)
    # What the sweep ranks titles against, and what it looks for in the photos.
    keywords: list[str] = Field(default_factory=list, max_length=10)
    visual_signature: str | None = Field(default=None, max_length=1000)
    watch_hints: WatchHints = Field(default_factory=WatchHints)

    def to_params(self) -> dict[str, str]:
        """Return this as a `Query.params` dict, ready for a search request.

        `order` is always `newest_first`, matching what `urls.parse_search_params` pins on
        every stored URL: a mapped query has to be storable as-is, and the sweep overrides
        the order per request rather than in the saved params (D002/D006).

        Nothing from `watch_hints` appears here. Those are title rules for a later watch,
        not search parameters, and sending them would narrow a sweep that is meant to see
        everything (R003).
        """
        params: dict[str, str] = {}
        if self.catalog is not None:
            params["catalog_ids"] = str(self.catalog.id)
        if self.brand is not None:
            params["brand_ids"] = str(self.brand.id)
        if self.sizes:
            params["size_ids"] = ",".join(str(size.id) for size in self.sizes)
        if self.price_to is not None:
            params["price_to"] = str(self.price_to)
        if self.currency is not None:
            params["currency"] = self.currency
        if self.search_text is not None:
            params["search_text"] = self.search_text
        params["order"] = "newest_first"
        return params


class TriageTarget(BaseModel):
    """What the photo check compares a listing against: the thing you asked for.

    Built from a `MappedQuery` — `keywords` and `visual_signature` are the two fields S02
    added for exactly this moment — plus `labels`, the human-readable names of the ids the
    search already filtered on. The model never sees a catalog id; it sees "Patagonia" and
    "Jackets & Coats", because those are what a photo can be judged against.
    """

    keywords: list[str] = Field(default_factory=list)
    visual_signature: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class Usage(BaseModel):
    """What one call to a judging flow cost, as the flow reported it.

    Optional everywhere: a flow that reports nothing is not an error, it just means the
    sweep prices the call from the configured per-million rates instead. `cost_eur` is the
    flow's own figure and wins over that arithmetic when present (T06).
    """

    model_config = ConfigDict(extra="ignore")

    input_tokens: int = 0
    output_tokens: int = 0
    cost_eur: float | None = None


class TriageItem(BaseModel):
    """One listing's thumbnail verdict: is this the thing, and how sure is the model.

    `confidence` is bounded rather than free: it ranks candidates, so a flow answering 87
    when it meant 0.87 would silently dominate every real match. Out of range is a schema
    failure the operator sees, not a number that quietly reorders a sweep.

    This satisfies `db.repo.TriageOutcome` structurally — the repo writer takes it as-is
    with no adapter, and the db layer never imports this module (MEM027).
    """

    model_config = ConfigDict(extra="ignore")

    id: int
    matches_target: bool
    confidence: float = Field(ge=0, le=1)
    reason: str | None = None


class TriageBatch(BaseModel):
    """One batch's answer: a verdict per listing, and what the batch cost.

    Extra keys ignored for the same reason as `MappedQuery`: the flow can start returning a
    new field before this app knows about it, so the two deploys are not coupled.
    """

    model_config = ConfigDict(extra="ignore")

    results: list[TriageItem] = Field(default_factory=list)
    usage: Usage | None = None
