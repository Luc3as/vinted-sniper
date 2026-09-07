"""Playwright checks against a running dashboard.

These need a live server; they are skipped entirely when none is reachable, so the
ordinary `pytest` run and CI stay green without one. Run them with:

    VINTED_SNIPER_UI_BASE_URL=http://127.0.0.1:8000 pytest tests/ui -p no:cacheprovider
"""

from __future__ import annotations

import os
import urllib.request

import pytest

BASE_URL = os.environ.get("VINTED_SNIPER_UI_BASE_URL", "http://127.0.0.1:8000")


def _server_is_up() -> bool:
    try:
        with urllib.request.urlopen(BASE_URL + "/", timeout=3) as resp:
            return resp.status == 200
    except OSError:
        return False


pytestmark = [
    pytest.mark.skipif(not _server_is_up(), reason=f"no dashboard at {BASE_URL}"),
]

PAGES = ["/", "/searches", "/history", "/help"]

VIEWPORTS = [
    pytest.param({"width": 1280, "height": 800}, id="desktop"),
    pytest.param({"width": 390, "height": 844}, id="mobile"),
]


@pytest.fixture
def errors_page(page):
    """A page that records console errors and failed requests."""
    console_errors: list[str] = []
    failed_requests: list[str] = []
    page.on(
        "console",
        lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
    )
    page.on("requestfailed", lambda req: failed_requests.append(req.url))
    page.console_errors = console_errors
    page.failed_requests = failed_requests
    return page


# --- Every page loads cleanly ------------------------------------------------------


@pytest.mark.parametrize("path", PAGES)
def test_page_loads_without_errors(errors_page, path):
    page = errors_page
    resp = page.goto(BASE_URL + path)
    assert resp.status == 200
    page.wait_for_load_state("networkidle")
    assert "vinted-sniper" in page.title()
    assert page.console_errors == []
    assert page.failed_requests == []


def test_navigation_links(page):
    page.goto(BASE_URL + "/")
    page.get_by_role("link", name="Searches").click()
    assert page.url.rstrip("/").endswith("/searches")
    page.get_by_role("link", name="History").click()
    assert page.url.rstrip("/").endswith("/history")
    page.get_by_role("link", name="Help").click()
    assert page.url.rstrip("/").endswith("/help")


def test_nav_and_section_icons_present(page):
    page.goto(BASE_URL + "/searches")
    assert page.locator("nav a svg").count() == 4  # Found, Searches, History, Help
    assert page.locator("h2 svg").count() >= 2  # Searches, Destinations


# --- Dashboard structure ------------------------------------------------------------


def test_found_page_is_the_landing_page(page):
    page.goto(BASE_URL + "/")
    assert page.get_by_role("heading", name="Found").is_visible()


def test_settings_sections_present(page):
    page.goto(BASE_URL + "/searches")
    for heading in ["Searches", "Add a search", "Destinations", "Add a destination"]:
        assert page.get_by_role("heading", name=heading).is_visible(), heading


def test_heading_hierarchy(page):
    """h2 must be meaningfully larger than body text (≥ 25 %)."""
    page.goto(BASE_URL + "/")
    sizes = page.evaluate(
        """() => ({
            body: parseFloat(getComputedStyle(document.body).fontSize),
            h2: parseFloat(getComputedStyle(document.querySelector('h2')).fontSize),
        })"""
    )
    assert sizes["h2"] >= sizes["body"] * 1.25, sizes


def test_add_search_form_fields(page):
    page.goto(BASE_URL + "/searches")
    form = page.locator("form:has(input[name=url])").first
    assert form.locator("input[name=url]").first.is_visible()
    for name in ["name", "interval", "max_total_price"]:
        assert form.locator(f"input[name={name}]").count() >= 1, name


def test_destination_type_options(page):
    page.goto(BASE_URL + "/searches")
    options = page.locator("form select[name=kind] option").all_text_contents()
    joined = " ".join(options)
    for kind in ["Telegram", "Discord", "ntfy", "webhook"]:
        assert kind in joined, f"missing destination type {kind}: {options}"


def test_explainers_toggle(page):
    page.goto(BASE_URL + "/searches")
    explain = page.locator("details.explain").first
    summary = explain.locator("summary").first
    assert not explain.get_attribute("open")
    summary.click()
    assert explain.get_attribute("open") is not None


def test_action_buttons_are_labeled(page):
    """Icon-only row buttons must carry an accessible name and a tooltip."""
    page.goto(BASE_URL + "/searches")
    buttons = page.locator("td.actions button.iconbtn")
    assert buttons.count() > 0, "expected icon action buttons in the searches table"
    for i in range(buttons.count()):
        b = buttons.nth(i)
        assert b.get_attribute("aria-label"), f"iconbtn #{i} missing aria-label"
        assert b.get_attribute("data-tip"), f"iconbtn #{i} missing data-tip"


# --- Tables fit their container -----------------------------------------------------


def test_tables_fit_without_horizontal_scroll(page):
    """At desktop width no table should force its wrapper to scroll sideways."""
    page.set_viewport_size({"width": 1280, "height": 800})
    overflowing = []
    for path in ["/searches", "/history", "/help"]:
        page.goto(BASE_URL + path)
        wraps = page.locator(".table-wrap")
        for i in range(wraps.count()):
            data = wraps.nth(i).evaluate(
                """(wrap) => {
                    const t = wrap.querySelector('table');
                    return { tableW: t ? t.scrollWidth : 0, wrapW: wrap.clientWidth };
                }"""
            )
            if data["tableW"] > data["wrapW"]:
                overflowing.append(f"{path} table #{i}: {data['tableW']}px in {data['wrapW']}px")
    assert not overflowing, "tables overflow their wrapper:\n" + "\n".join(overflowing)


def test_search_name_is_single_line(page):
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(BASE_URL + "/searches")
    name = page.locator("td .name").first
    if name.count() == 0:
        pytest.skip("no searches in the database")
    lines = name.evaluate(
        """(el) => Math.round(
            el.getBoundingClientRect().height / parseFloat(getComputedStyle(el).lineHeight))"""
    )
    assert lines == 1, f"search name wraps to {lines} lines"


# --- Real data renders (prod DB copy) ----------------------------------------------


def test_searches_table_has_rows(page):
    page.goto(BASE_URL + "/searches")
    body = page.locator("body").inner_text()
    assert "Nothing is being watched yet" not in body, "expected prod searches, dashboard is empty"


def test_history_shows_listings(page):
    page.goto(BASE_URL + "/history")
    body = page.locator("body").inner_text()
    assert "Nothing" not in body.split("\n")[0] or len(body) > 200


# --- Recently found: filter and paging ---------------------------------------------


def _visible_cards(page) -> int:
    return page.evaluate(
        "() => [...document.querySelectorAll('.listing')].filter(c => !c.hidden).length"
    )


def test_recent_paging(page):
    page.goto(BASE_URL + "/")
    total = page.locator(".listing").count()
    if total == 0:
        pytest.skip("no recent listings in the database")
    first_page = _visible_cards(page)
    assert first_page <= 12
    more = page.locator("#recent-more")
    if total > first_page:
        assert more.is_visible()
        more.click()
        assert _visible_cards(page) > first_page
    else:
        assert not more.is_visible()


def test_recent_filter(page):
    page.goto(BASE_URL + "/")
    if page.locator(".listing").count() == 0:
        pytest.skip("no recent listings in the database")
    # A word from the first card's text must narrow the grid to matching cards only.
    word = page.locator(".listing .title").first.inner_text().split()[0].lower()
    page.locator("#recent-filter").fill(word)
    shown = page.evaluate(
        "() => [...document.querySelectorAll('.listing')].filter(c => !c.hidden)"
        ".map(c => c.textContent.toLowerCase())"
    )
    assert shown, f"filter {word!r} hid everything"
    assert all(word in text for text in shown)
    page.locator("#recent-filter").fill("")
    assert _visible_cards(page) > 0


def test_recent_drops_only(page):
    page.goto(BASE_URL + "/")
    if page.locator(".listing").count() == 0:
        pytest.skip("no recent listings in the database")
    page.locator("#recent-drops").check()
    result = page.evaluate(
        """() => {
            const shown = [...document.querySelectorAll('.listing')].filter(c => !c.hidden);
            return { n: shown.length, allDrop: shown.every(c => c.querySelector('.tag.drop')) };
        }"""
    )
    assert result["allDrop"], "a non-drop card survived the drops-only filter"


# --- Tooltips: visible and inside the viewport -------------------------------------


@pytest.mark.parametrize("viewport", VIEWPORTS)
@pytest.mark.parametrize("path", PAGES)
def test_tooltips_visible_in_viewport(page, path, viewport):
    """Hover every ? icon; the shared #tip element must appear fully on screen.

    #tip is position:fixed on <body>, so this also proves no scroll container
    (like .table-wrap) clips it — the regression that hid every table tooltip.
    """
    page.set_viewport_size(viewport)
    page.goto(BASE_URL + path)
    helps = page.locator(".help")
    problems = []
    for i in range(helps.count()):
        el = helps.nth(i)
        if not el.is_visible():
            continue
        # Synthetic hover: a real pointer hover can land on the sticky header when
        # Playwright scrolls the icon underneath it. Pointer reachability is covered
        # by test_tooltip_inside_table_not_clipped; this sweep checks geometry.
        el.scroll_into_view_if_needed()
        el.evaluate("(el) => el.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}))")
        box = page.evaluate(
            """() => {
                const tip = document.getElementById('tip');
                if (!tip || tip.hidden) return null;
                const r = tip.getBoundingClientRect();
                return { left: r.left, right: r.right, top: r.top, bottom: r.bottom,
                         vw: innerWidth, vh: innerHeight, text: tip.textContent.slice(0, 40) };
            }"""
        )
        if box is None:
            problems.append(f"{path} [{viewport['width']}px] tip #{i}: never became visible")
            continue
        if (
            box["left"] < 0
            or box["right"] > box["vw"]
            or box["top"] < 0
            or box["bottom"] > box["vh"]
        ):
            problems.append(
                f"{path} [{viewport['width']}px] tip #{i} ({box['text']!r}): "
                f"x {box['left']:.0f}..{box['right']:.0f} y {box['top']:.0f}..{box['bottom']:.0f} "
                f"outside {box['vw']}x{box['vh']}"
            )
    assert not problems, "tooltips broken:\n" + "\n".join(problems)


def test_tooltip_inside_table_not_clipped(page):
    """The specific regression: a ? icon inside a scrollable table wrapper."""
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(BASE_URL + "/searches")
    table_help = page.locator(".table-wrap .help").first
    if table_help.count() == 0:
        pytest.skip("no tooltip icons inside a table")
    table_help.hover()
    visible = page.evaluate(
        """() => {
            const tip = document.getElementById('tip');
            if (!tip || tip.hidden) return false;
            const r = tip.getBoundingClientRect();
            return r.width > 0 && r.height > 0
                && r.left >= 0 && r.right <= innerWidth && r.top >= 0 && r.bottom <= innerHeight;
        }"""
    )
    assert visible, "tooltip inside .table-wrap is hidden or clipped"


# --- Theme toggle and state-pill colours -------------------------------------------


def test_paused_pill_not_amber(page):
    """Paused is the user's choice; it must not share the warning colour with stale."""
    page.goto(BASE_URL + "/searches")
    if page.locator(".pill.paused").count() == 0 or page.locator(".pill.stale").count() == 0:
        pytest.skip("need both a paused and a stale search to compare")
    colours = page.evaluate(
        """() => ({
            paused: getComputedStyle(document.querySelector('.pill.paused')).color,
            stale: getComputedStyle(document.querySelector('.pill.stale')).color,
        })"""
    )
    assert colours["paused"] != colours["stale"], f"paused and stale share {colours['stale']}"


def test_theme_toggle_cycles_and_persists(page):
    """system → light → dark → system; the choice survives a reload."""
    page.goto(BASE_URL + "/")
    state = """() => ({
        mode: document.documentElement.dataset.theme || 'auto',
        stored: localStorage.getItem('theme'),
        bg: getComputedStyle(document.body).backgroundColor,
        icon: [...document.querySelectorAll('#theme-toggle svg')]
            .filter(s => getComputedStyle(s).display !== 'none')
            .map(s => s.dataset.mode).join(','),
    })"""
    toggle = page.locator("#theme-toggle")
    assert toggle.is_visible(), "theme toggle missing from the header"

    light_bg = page.evaluate(state)["bg"]
    toggle.click()
    after_light = page.evaluate(state)
    assert after_light["mode"] == "light" and after_light["icon"] == "light"

    toggle.click()
    after_dark = page.evaluate(state)
    assert after_dark["mode"] == "dark" and after_dark["stored"] == "dark"
    assert after_dark["icon"] == "dark"
    assert after_dark["bg"] != light_bg, "forcing dark did not change the background"

    page.reload()
    survived = page.evaluate(state)
    assert survived["mode"] == "dark" and survived["bg"] == after_dark["bg"], (
        "dark theme did not survive a reload"
    )

    page.locator("#theme-toggle").click()  # back to system
    reset = page.evaluate(state)
    assert reset["mode"] == "auto" and reset["stored"] is None


# --- History filter form -----------------------------------------------------------


def test_history_filter_all_all_returns_the_page(page):
    """Regression: search= (empty, meaning "all") used to 422 with a raw JSON error."""
    page.goto(BASE_URL + "/history")
    page.get_by_role("button", name="Filter").click()
    page.wait_for_load_state("networkidle")
    assert "search=" in page.url, "the form submits an empty search param for 'all'"
    assert page.get_by_role("heading", name="Delivery history").is_visible()
    assert "int_parsing" not in page.locator("body").inner_text()


def test_history_filter_by_search_keeps_the_choice(page):
    page.goto(BASE_URL + "/history")
    options = page.locator("select[name=search] option:not([value=''])")
    if options.count() == 0:
        pytest.skip("no searches in the database to filter by")
    value = options.first.get_attribute("value")
    page.locator("select[name=search]").select_option(value)
    page.get_by_role("button", name="Filter").click()
    page.wait_for_load_state("networkidle")
    assert page.locator("select[name=search]").input_value() == value
    assert page.get_by_role("heading", name="Delivery history").is_visible()


def test_brand_lockup_renders_full_size(page):
    """The header carries the full lockup SVG, not the old 1rem mini-mark."""
    page.goto(BASE_URL + "/")
    box = page.locator(".brand svg.lockup").bounding_box()
    assert box is not None, "lockup missing from header"
    assert box["height"] >= 24, f"lockup squashed to {box['height']}px — generic a-svg rule wins?"
    assert box["width"] > box["height"] * 4, "lockup aspect wrong"


def test_listing_price_hierarchy(page):
    """The total (what you pay) leads in bold; the bare asking price is a muted footnote."""
    page.goto(BASE_URL + "/")
    if page.locator(".listing .price .total").count() == 0:
        pytest.skip("no recent listings in the database")
    result = page.evaluate(
        """() => {
            const price = document.querySelector('.listing .price');
            const total = price.querySelector('.total');
            const base = price.querySelector('.base');
            const ts = getComputedStyle(total);
            const out = { totalWeight: +ts.fontWeight, totalSize: parseFloat(ts.fontSize) };
            if (base) {
                const bs = getComputedStyle(base);
                out.baseSize = parseFloat(bs.fontSize);
                out.baseMuted = bs.color !== ts.color;
                const tr = total.getBoundingClientRect();
                out.baseBelow = base.getBoundingClientRect().top >= tr.bottom - 2;
            }
            return out;
        }"""
    )
    assert result["totalWeight"] >= 700, "total price is not bold"
    if "baseSize" in result:
        assert result["totalSize"] > result["baseSize"], "total is not larger than base price"
        assert result["baseMuted"], "base price is not muted"
        assert result["baseBelow"], "base price is not on its own line below the total"


def test_listing_tags_carry_icons(page):
    """Brand/size/condition tags each show an inline glyph; the size tag is explicit."""
    page.goto(BASE_URL + "/")
    if page.locator(".listing").count() == 0:
        pytest.skip("no recent listings in the database")
    result = page.evaluate(
        """() => {
            const card = [...document.querySelectorAll('.listing')]
                .find(c => c.querySelector('.tag.size'));
            if (!card) return null;
            const svg = card.querySelector('.tag.size svg');
            const r = svg && svg.getBoundingClientRect();
            return {
                sizeIcon: !!svg,
                iconVisible: !!r && r.width > 5 && r.height > 5,
                brandIcon: !!card.querySelector('.tag.brand svg'),
            };
        }"""
    )
    if result is None:
        pytest.skip("no listing with a size tag")
    assert result["sizeIcon"], "size tag has no icon"
    assert result["iconVisible"], "size icon rendered at zero size"
    assert result["brandIcon"], "brand tag has no icon"


def test_destination_rows_have_edit_and_delete(page):
    """Every destination row — active or disabled — offers edit and delete; the edit
    row opens with the current name and target prefilled."""
    page.goto(BASE_URL + "/searches")
    actions = page.locator("#destinations td.actions")
    if actions.count() == 0:
        pytest.skip("no destinations in the database")
    first = actions.first
    assert first.locator("button[data-edit]").count() == 1, "destination edit button missing"
    assert first.locator("form[action$='/delete'] button").count() == 1, "delete button missing"
    edit = first.locator("button[data-edit]")
    edit.click()
    row_id = "edit-" + edit.get_attribute("data-edit")
    edit_row = page.locator(f"#{row_id}")
    assert edit_row.is_visible(), "destination edit row did not open"
    assert edit_row.locator("input[name=name]").input_value(), "name not prefilled"
    assert edit_row.locator("input[name=target]").count() == 1
