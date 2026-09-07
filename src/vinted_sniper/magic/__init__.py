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

The second half of the package is the judging side. `triage.py` asks a flow which of a
sweep's survivors *look* like the thing that was asked for — the whole reason this
milestone exists, since a title score can only rank the words a seller happened to type.
It is built as a deliberate copy of `client.py`: same one-attempt discipline, same single
`MappingError`, same wrapper unwrapping, because both talk to the same n8n and an operator
debugging one should not have to learn a second set of rules. The one thing it adds is a
cost shape — `Usage` on every answer — and the rule that goes with it: triage sends
thumbnails and never full-size photos, which is a property of the request body and is
asserted as one.
"""
