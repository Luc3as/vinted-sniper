"""What can go wrong turning a sentence into a search.

Deliberately not a `VintedError`: nothing here has talked to Vinted. A mapping that fails
means the model was unreachable, answered with something we cannot use, or named an id
that does not exist — three problems a human fixes by rewording or by fixing the flow,
none of which are helped by anything the Vinted error types would prompt.
"""

from __future__ import annotations


class MappingError(Exception):
    """A shopping intent could not be turned into a usable search."""


class TaxonomyUnavailableError(MappingError):
    """The ids could not be checked because Vinted itself could not be reached.

    A subclass rather than a sibling on purpose: every handler in this package — and in
    `engine/sweep.py` — treats `MappingError` as the one thing mapping raises, and those
    handlers keep working untouched. Only the endpoint tells the two apart, because only
    there does the difference matter: a wrong id is the caller's to fix by rewording
    (`422`), an unreachable Vinted is nobody's to fix but time's (`502`).
    """
