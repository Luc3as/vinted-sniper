"""Turning a sentence a person would say into a search Vinted understands.

Everywhere else in this app a search starts as a URL somebody already built on Vinted's
own site — the filters were picked by hand, so the ids in the query string are real by
construction. Here they are not. "panska bunda Patagonia Torrentshell M do 60 eur" has to
become a catalog id, a brand id, a size id and a ceiling, and the thing doing that
translation is a language model, which will happily invent a plausible number.

So this package keeps two jobs apart. `models.py` is the shape of an answer — what the
mapper is allowed to say, and how that turns into the params dict the rest of the app
already passes around. Confirming that each id in it actually exists in Vinted's taxonomy
is a separate step, on purpose: a shape can be valid and still be a search for a brand
nobody has ever sold. When that check fails the caller gets a `MappingError` naming what
was wrong, not an empty result page.
"""
