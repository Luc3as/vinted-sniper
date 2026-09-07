"""Confirming that every id the mapper returned is an id Vinted actually has.

A language model asked for a category id will answer with one whether or not it exists,
and the number it invents looks exactly like a real one. Nothing downstream can tell the
difference: a search for catalog 9999 does not fail, it just quietly returns nothing. So
the ids are confirmed here, against Vinted's own taxonomy, before they become a search.

The three tiers run cheapest first (D008). The catalog tier walks the category tree that
`Taxonomy` keeps in the database for a week, so a bogus catalog id — the most common thing
a model gets wrong — is caught without a single Vinted request. Only once a catalog is
confirmed do the brand and size tiers go out, and they go out scoped to that catalog,
which is what makes their answers small.

Brands and sizes have no "does this id exist" endpoint. They can only be looked up by the
text a person would type, which is exactly why the mapper is made to return the name it
thought each id meant: the name is what makes the lookup possible, and it is also what
makes the rejection readable.
"""

from __future__ import annotations

from typing import Any, NoReturn

from vinted_sniper.log import get_logger
from vinted_sniper.magic.errors import MappingError, TaxonomyUnavailableError
from vinted_sniper.magic.models import MappedQuery, Named
from vinted_sniper.vinted.errors import VintedError
from vinted_sniper.vinted.taxonomy import Taxonomy

log = get_logger(__name__)

# How many of the names Vinted did return to quote back in a rejection. Enough to be
# useful when the model was close, short enough to stay one readable line.
_MAX_SUGGESTIONS = 5

# What counts as an actual constraint on the search. `currency` is not here — it is the
# unit `price_to` is counted in, not a thing it narrows — and neither are `keywords`,
# `visual_signature` or `watch_hints`, which rank and describe but never filter (R003/D008).
_FILTERS = ("catalog", "brand", "sizes", "price_to", "search_text")


def find_catalog(tree: list[dict[str, Any]], catalog_id: int) -> dict[str, Any] | None:
    """Return the node with this id anywhere in the tree, or None.

    Pure and offline: `tree` is the `{"id", "title", "children"}` shape
    `taxonomy.compact_tree()` produces, and the whole walk is a few thousand dicts.
    """
    for node in tree:
        if node.get("id") == catalog_id:
            return node
        children = node.get("children")
        if isinstance(children, list) and (found := find_catalog(children, catalog_id)):
            return found
    return None


async def validate(mapped: MappedQuery, *, tld: str, taxonomy: Taxonomy) -> None:
    """Confirm every id in `mapped` exists on vinted.`tld`. Raise `MappingError` if not.

    Returns nothing on success — this is a gate, not a transform. A field left unset skips
    its tier: a text-only search with no brand is a perfectly good search. What is *not* a
    good search is one where every field is unset, so that case is refused first.
    """
    _check_something_is_filtered(mapped)

    catalog_id: int | None = None
    if mapped.catalog is not None:
        await _check_catalog(mapped.catalog.id, mapped.catalog.name, tld=tld, taxonomy=taxonomy)
        catalog_id = mapped.catalog.id

    if mapped.brand is not None:
        await _check_brand(
            mapped.brand.id,
            mapped.brand.name,
            catalog_id=catalog_id,
            tld=tld,
            taxonomy=taxonomy,
        )

    if mapped.sizes:
        await _check_sizes(mapped.sizes, catalog_id=catalog_id, tld=tld, taxonomy=taxonomy)


def _check_something_is_filtered(mapped: MappedQuery) -> None:
    """Refuse a mapping that narrows nothing at all.

    Every field on `MappedQuery` is optional and unknown keys are ignored, which is what
    keeps this app and the n8n flow deployable apart — but it also means a flow answering
    with something unrelated validates into a mapping with nothing set. The per-field tiers
    below then all skip, `to_params()` emits only the sort order, and what comes out is a
    search across the whole of Vinted paid for out of the person's triage budget. That is
    the one thing this gate exists to prevent, so it is caught before any tier runs.
    """
    if any(_is_a_constraint(getattr(mapped, field)) for field in _FILTERS):
        return

    message = (
        "this search would not narrow anything down: the mapper came back without a "
        "category, a brand, a size, a price limit or any words to search for. "
        "Try saying what you are looking for in more detail."
    )
    # Its own `kind`, not `unknown_id`: nothing here was wrong about Vinted, the answer
    # simply had no search in it. An operator grepping the log wants those apart.
    log.warning("magic.rejected", tier="filters", kind="no_filters", reason=message)
    raise MappingError(message)


def _is_a_constraint(value: object) -> bool:
    """Is this field actually narrowing the search?

    Emptiness is what disqualifies, not falsiness: `price_to = 0` is a real ceiling — the
    free listings — while `search_text = ""` and `sizes = []` narrow nothing.
    """
    if value is None:
        return False
    if isinstance(value, str | list):
        return bool(value)
    return True


async def _check_catalog(catalog_id: int, name: str, *, tld: str, taxonomy: Taxonomy) -> None:
    """Tier 1: the category tree, from the week-long DB cache. Usually zero requests."""
    try:
        tree = await taxonomy.categories(tld)
    except VintedError as exc:
        _unavailable("catalog", f"could not read the vinted.{tld} category tree: {exc}")

    if find_catalog(tree, catalog_id) is None:
        _reject("catalog", f"Vinted has no category {catalog_id} ({name!r})")


async def _check_brand(
    brand_id: int, name: str, *, catalog_id: int | None, tld: str, taxonomy: Taxonomy
) -> None:
    """Tier 2: brand autocomplete, scoped to the catalog when there is one.

    The scoped lookup reads a category's brand facet, which only lists brands that
    category currently has items for. A real brand can be missing from it — the model
    named a jacket brand and the catalog is narrower than the model assumed — so an empty
    scoped answer is retried globally before anything is called wrong.
    """
    rows: list[dict[str, Any]] = []
    if catalog_id is not None:
        rows = await _brand_rows(tld, name, str(catalog_id), taxonomy=taxonomy)
    if not rows:
        rows = await _brand_rows(tld, name, "", taxonomy=taxonomy)

    if any(row.get("id") == brand_id for row in rows):
        return

    detail = ""
    if rows:
        titles = ", ".join(str(row.get("title")) for row in rows[:_MAX_SUGGESTIONS])
        detail = f" — searching for {name!r} found: {titles}"
    _reject("brand", f"Vinted has no brand {brand_id} ({name!r}){detail}")


async def _brand_rows(
    tld: str, name: str, catalog_ids: str, *, taxonomy: Taxonomy
) -> list[dict[str, Any]]:
    try:
        return await taxonomy.brands(tld, name, catalog_ids=catalog_ids)
    except VintedError as exc:
        _unavailable("brand", f"could not look up brand {name!r} on vinted.{tld}: {exc}")


async def _check_sizes(
    sizes: list[Named], *, catalog_id: int | None, tld: str, taxonomy: Taxonomy
) -> None:
    """Tier 3: the size facet of the mapped catalog.

    Sizes are only meaningful inside a category — "M" is a different id for a jacket than
    for a pair of shoes — and the facet endpoint needs a catalog to answer at all, so a
    size without one cannot be confirmed and is refused rather than passed through.
    """
    if catalog_id is None:
        named = ", ".join(f"{size.id} ({size.name!r})" for size in sizes)
        _reject(
            "size",
            f"sizes can only be checked inside a category, and this search has none: {named}",
        )

    try:
        options = await taxonomy.facet_options(tld, "size", str(catalog_id))
    except VintedError as exc:
        _unavailable("size", f"could not read the size options for category {catalog_id}: {exc}")

    known = {option.get("id") for option in options}
    unknown = [size for size in sizes if size.id not in known]
    if unknown:
        named = ", ".join(f"{size.id} ({size.name!r})" for size in unknown)
        _reject("size", f"category {catalog_id} has no size {named}")


def _reject(tier: str, message: str) -> NoReturn:
    """The id is wrong. Log which tier caught it, then raise."""
    log.warning("magic.rejected", tier=tier, kind="unknown_id", reason=message)
    raise MappingError(message)


def _unavailable(tier: str, message: str) -> NoReturn:
    """The check could not run. A different problem from a wrong id, and a different fix.

    Kept distinguishable in both the log and the message because the endpoint answers them
    with different statuses: a wrong id is the caller's to fix by rewording, an unreachable
    Vinted is not.
    """
    full = f"{message} — the ids in this search could not be checked"
    log.warning("magic.rejected", tier=tier, kind="lookup_failed", reason=full)
    raise TaxonomyUnavailableError(full)
