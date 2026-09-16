"""The French→Slovak label layer for Vinted's new services."""

from __future__ import annotations

import pytest

from vinted_sniper.vinted import slovak


@pytest.mark.parametrize(
    ("french", "expected"),
    [
        ("Neuf avec étiquette", "Nové s visačkou"),
        ("Satisfaisant", "Uspokojivé"),
        ("Veľmi dobré", "Veľmi dobré"),  # already Slovak passes through
    ],
)
def test_conditions_read_in_slovak(french: str, expected: str) -> None:
    assert slovak.condition(french) == expected


@pytest.mark.parametrize(
    ("french", "expected"),
    [
        # Neutral sizes stay exactly as they are.
        ("S/36/8", "S/36/8"),
        ("40", "40"),
        # Child ages: Slovak counts 1 rok, 2-4 roky, 5+ rokov — same for months.
        ("2 ans", "2 roky"),
        ("10 ans / 140 cm", "10 rokov / 140 cm"),
        ("12-18 mois / 80 cm", "12 – 18 mesiacov / 80 cm"),  # noqa: RUF001 - site's dash
        ("1-3 mois / 56 cm", "1 – 3 mesiace / 56 cm"),  # noqa: RUF001 - site's dash
        ("0 mois", "0 mesiacov"),
        ("12 ans et plus > 56 cm", "12 rokov a viac > 56 cm"),
        # Bounds and the odd fixed phrases.
        ("Jusqu'à 149 cm", "Do 149 cm"),
        ("Jusqu'à 1 mois / 50 cm", "Do 1 mesiaca / 50 cm"),
        ("47 mm et plus", "47 mm a viac"),
        ("15 et moins", "15 a menej"),
        ("Taille unique", "Univerzálna"),
        ("Autre", "Iné"),
        ("Ajustable", "Nastaviteľná"),
        ("Naissance / 44 cm", "Novorodenec / 44 cm"),
        ("Prématuré, jusqu'à 44cm", "Predčasne narodené, do 44cm"),
        ("Double (120-140 x 190-200 cm)", "Dvojlôžko (120-140 x 190-200 cm)"),
    ],
)
def test_size_labels_read_in_slovak(french: str, expected: str) -> None:
    assert slovak.size_label(french) == expected


@pytest.mark.parametrize(
    ("code", "option_id", "expected"),
    [
        ("status", 6, "Nové s visačkou"),
        ("status", 2, "Veľmi dobré"),
        ("color", 1, "Čierna"),
        ("color", 29, "Horčicová"),
        ("material", 123, "Kašmír"),
        ("material", 305, "Lakovaná koža"),
    ],
)
def test_facet_titles_come_from_the_id(code: str, option_id: int, expected: str) -> None:
    assert slovak.facet_title(code, option_id, "whatever the wire said") == expected


def test_an_unknown_facet_id_keeps_the_wire_title() -> None:
    assert slovak.facet_title("color", 99999, "Nouveau ton") == "Nouveau ton"


def test_size_charts_read_in_slovak() -> None:
    assert slovak.size_group("Soutiens-gorge") == "Podprsenky"
    assert slovak.size_group("Tailles hommes") == "Pánske veľkosti"
    assert slovak.size_group("Some new chart") == "Some new chart"
