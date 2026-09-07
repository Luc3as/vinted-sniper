"""The browser personas requests go out as."""

from __future__ import annotations

import random

from vinted_sniper.vinted import headers as hdr


def test_impersonated_transport_only_claims_chromium_browsers() -> None:
    """The impersonated transport shakes hands like Chrome no matter what the headers
    say. Claiming Firefox or Safari on top of that handshake is exactly the
    contradiction fingerprinting looks for."""
    rng = random.Random(1234)
    brands = {hdr.pick_identity(rng, chromium_only=True).brand for _ in range(200)}
    assert brands
    assert brands <= {"Chromium", "Microsoft Edge"}


def test_the_full_persona_pool_still_includes_other_browsers() -> None:
    rng = random.Random(1234)
    brands = {hdr.pick_identity(rng).brand for _ in range(200)}
    assert not brands <= {"Chromium", "Microsoft Edge"}
