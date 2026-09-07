"""What can go wrong turning a sentence into a search.

Deliberately not a `VintedError`: nothing here has talked to Vinted. A mapping that fails
means the model was unreachable, answered with something we cannot use, or named an id
that does not exist — three problems a human fixes by rewording or by fixing the flow,
none of which are helped by anything the Vinted error types would prompt.
"""

from __future__ import annotations


class MappingError(Exception):
    """A shopping intent could not be turned into a usable search."""
