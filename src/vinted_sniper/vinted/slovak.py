"""Say Vinted's French labels in Slovak.

svc-catalogue and svc-filters (2026-09) answer in French for every session this app can
mint — cookies, Accept-Language and the anon id all leave them unmoved; the real site
shows Slovak because its frontend translates client-side. This module is that missing
translation step. The vocabularies are closed sets: conditions and facet options carry
stable ids, sizes reuse a handful of French phrases. The Slovak wording is Vinted's own,
read off the vinted.sk frontend. Anything unknown passes through untouched.
"""

from __future__ import annotations

import re
from typing import Final

# --- Conditions (item parsing goes by text: the payload carries no status id) ---------

CONDITIONS: Final[dict[str, str]] = {
    "Neuf avec étiquette": "Nové s visačkou",
    "Neuf sans étiquette": "Nové bez visačky",
    "Très bon état": "Veľmi dobré",
    "Bon état": "Dobré",
    "Satisfaisant": "Uspokojivé",
}


def condition(text: str) -> str:
    """A listing's condition, in Slovak."""
    return CONDITIONS.get(text, text)


# --- Facet options (the picker goes by id: ids are stable, wire titles are French) ----

_STATUS_TITLES: Final[dict[int, str]] = {
    6: "Nové s visačkou",
    1: "Nové bez visačky",
    2: "Veľmi dobré",
    3: "Dobré",
    4: "Uspokojivé",
}

_COLOR_TITLES: Final[dict[int, str]] = {
    1: "Čierna",
    2: "Hnedá",
    3: "Sivá",
    4: "Béžová",
    5: "Ružová",
    6: "Fialová",
    7: "Červená",
    8: "Žltá",
    9: "Modrá",
    10: "Zelená",
    11: "Oranžová",
    12: "Biela",
    13: "Strieborná",
    14: "Zlatá",
    15: "Multi",
    16: "Khaki",
    17: "Tyrkysová",
    20: "Krémová",
    21: "Marhuľová",
    22: "Koralovoružová",
    23: "Burgundská",
    24: "Ruža",
    25: "Fialová",
    26: "Svetlomodrá",
    27: "Námornícka",
    28: "Tmavozelená",
    29: "Horčicová",
    30: "Mätová",
    32: "Číra",
}

_MATERIAL_TITLES: Final[dict[int, str]] = {
    43: "Koža",
    44: "Bavlna",
    45: "Polyester",
    46: "Vlna",
    48: "Viskóza",
    49: "Hodváb",
    52: "Nylon",
    53: "Elastan",
    120: "Flís",
    121: "Merino",
    122: "Alpaka",
    123: "Kašmír",
    146: "Ľan",
    149: "Akryl",
    152: "Mohér",
    177: "Velúr",
    178: "Neoprén",
    226: "Flitre",
    298: "Semiš",
    299: "Menčester",
    300: "Plast",
    301: "Guma",
    302: "Latex",
    303: "Denim",
    305: "Lakovaná koža",
    311: "Satén",
    440: "Bambus",
    441: "Plátno",
    442: "Kartón",
    443: "Keramika",
    444: "Šifón",
    445: "Páperie",
    446: "Umelá kožušina",
    447: "Umelá koža",
    448: "Plsť",
    449: "Molitan",
    451: "Flanel",
    452: "Sklo",
    453: "Zlato",
    454: "Juta",
    455: "Čipka",
    456: "Sieťovina",
    457: "Kov",
    458: "Papier",
    459: "Porcelán",
    460: "Ratan",
    461: "Striebro",
    462: "Kameň",
    463: "Slama",
    464: "Tyl",
    465: "Tvíd",
    466: "Zamat",
    467: "Drevo",
    468: "Oceľ",
    470: "Silikón",
    3642: "Borosilikátové sklo",
}

_FACET_TITLES: Final[dict[str, dict[int, str]]] = {
    "status": _STATUS_TITLES,
    "color": _COLOR_TITLES,
    "material": _MATERIAL_TITLES,
}


def facet_title(code: str, option_id: int, wire_title: str) -> str:
    """A facet option's title, in Slovak when the id is a known one."""
    return _FACET_TITLES.get(code, {}).get(option_id, wire_title)


# --- Sizes (mostly numbers; the French phrases and units get said in Slovak) ----------

_SIZE_PHRASES: Final[dict[str, str]] = {
    "Taille unique": "Univerzálna",
    "Autre": "Iné",
    "Ajustable": "Nastaviteľná",
    "Peu importe": "Nezáleží",
    "Universel": "Univerzálne",
    "Berceau": "Kolíska",
    "Lit bébé": "Detská postieľka",
    "Lit de voyage bébé": "Cestovná postieľka",
    "Lit nouveau né": "Postieľka pre novorodenca",
}


def _plural(count: int, forms: tuple[str, str, str]) -> str:
    """Slovak plurals: 1 / 2-4 / 0 and 5+ — the same rule i18n uses."""
    if count == 1:
        return forms[0]
    return forms[1] if 2 <= count <= 4 else forms[2]  # noqa: PLR2004


_YEARS: Final = ("rok", "roky", "rokov")
_MONTHS: Final = ("mesiac", "mesiace", "mesiacov")


def _age(match: re.Match[str], forms: tuple[str, str, str]) -> str:
    low, high = match.group(1), match.group(2)
    word = _plural(int(high or low), forms)
    # Ranges use the spaced dash the vinted.sk frontend itself renders.
    return f"{low} – {high} {word}" if high else f"{low} {word}"  # noqa: RUF001


_SIZE_RULES: Final[list[tuple[re.Pattern[str], str | object]]] = [
    # "Jusqu'à N mois" needs the genitive that follows Slovak's "do".
    (
        re.compile(r"[Jj]usqu'à (\d+) mois"),
        lambda m: f"Do {m.group(1)} " + ("mesiaca" if int(m.group(1)) == 1 else "mesiacov"),
    ),
    (re.compile(r"(\d+)(?:\s*-\s*(\d+))? ans\b"), lambda m: _age(m, _YEARS)),
    (re.compile(r"(\d+)(?:\s*-\s*(\d+))? mois\b"), lambda m: _age(m, _MONTHS)),
    (re.compile(r"Jusqu'à"), "Do"),
    (re.compile(r"jusqu'à"), "do"),
    (re.compile(r" et plus\b"), " a viac"),
    (re.compile(r" et moins\b"), " a menej"),
    (re.compile(r"\bNaissance\b"), "Novorodenec"),
    (re.compile(r"\bPrématurés\b"), "Predčasne narodení"),
    (re.compile(r"\bPrématuré\b"), "Predčasne narodené"),
    (re.compile(r"\bSimple \("), "Jednolôžko ("),
    (re.compile(r"\bDouble \("), "Dvojlôžko ("),
]


def size_label(text: str) -> str:
    """A size, in Slovak. Plain numeric and letter sizes come out unchanged."""
    known = _SIZE_PHRASES.get(text.strip())
    if known is not None:
        return known
    for pattern, replacement in _SIZE_RULES:
        text = pattern.sub(replacement, text)  # type: ignore[arg-type]
    return text


# --- Size charts (the group headers the picker shows over grouped sizes) --------------

_SIZE_GROUPS: Final[dict[str, str]] = {
    "Tailles": "Veľkosti",
    "Tailles hommes": "Pánske veľkosti",
    "Tailles enfants et bébés": "Detské a dojčenské veľkosti",
    "Chaussures": "Obuv",
    "Chaussures hommes": "Pánska obuv",
    "Tailles de chaussures pour enfants": "Detské veľkosti obuvi",
    "Soutiens-gorge": "Podprsenky",
    "Chemises homme": "Pánske košele",
    "Pantalons homme": "Pánske nohavice",
    "Vestes de costume pour homme": "Pánske saká",
    "Bagues": "Prstene",
    "Montres": "Hodinky",
    "Chapeaux adulte": "Klobúky pre dospelých",
    "Chapeaux enfant": "Detské klobúky",
    "Gants adulte": "Rukavice pre dospelých",
    "Ceintures femme": "Dámske opasky",
    "Ceintures homme": "Pánske opasky",
    "Ceintures enfant": "Detské opasky",
    "Ceintures de soutien maternité": "Tehotenské podporné pásy",
    "Chaussettes femme": "Dámske ponožky",
    "Chaussettes homme": "Pánske ponožky",
    "Chaussettes enfant": "Detské ponožky",
    "Âge enfant": "Vek dieťaťa",
    "Couches": "Plienky",
    "Sièges auto bébé": "Detské autosedačky",
    "Literie": "Posteľná bielizeň",
    "Literie bébé": "Detská posteľná bielizeň",
    "Tailles de couette": "Veľkosti paplónov",
    "Tailles de coussins": "Veľkosti vankúšov",
    "Tailles de couvertures": "Veľkosti prikrývok",
    "Tailles de taies d'oreiller": "Veľkosti obliečok na vankúš",
    "Longueur de rideau": "Dĺžka závesu",
    "Articles pour chiens": "Potreby pre psov",
}


def size_group(label: str) -> str:
    """A size chart's name, in Slovak."""
    return _SIZE_GROUPS.get(label, label)
