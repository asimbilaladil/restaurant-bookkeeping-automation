"""
R365 occasionally shows a Pendo onboarding-tour overlay (#pendo-base /
.pendo-backdrop-region-*) that intercepts pointer events, blocking a real
(non-forced) Playwright click on the DSS date filter -- the exact failure
recorded for LCF Katy / 2026-10-28 ("pendo-backdrop-3 ... intercepts pointer
events"). These tests mock Page/Locator entirely; no browser, no R365
session, no live reconciliation.
"""

from unittest.mock import MagicMock

import pytest

from r365.journal_entry import _dismiss_pendo_overlay_if_present, _PENDO_CLOSE_SELECTORS


def _make_page(overlay_visible: bool, close_selector_visible: str | None):
    """Build a fake Page whose .locator(selector) returns a fake Locator.
    `close_selector_visible` is the one selector (from _PENDO_CLOSE_SELECTORS)
    whose Locator reports visible=True and is clickable; every other
    close-selector Locator reports not visible. None means no close control
    is visible at all."""
    page = MagicMock()
    locators_by_selector: dict[str, MagicMock] = {}

    def locator_factory(selector):
        # Cache so repeated page.locator(selector) calls -- one from the
        # production code, one from a test's own verification -- return the
        # *same* mock object instead of a fresh, call-history-free one.
        if selector in locators_by_selector:
            return locators_by_selector[selector]
        loc = MagicMock()
        loc.first = loc
        if selector == close_selector_visible:
            loc.is_visible.return_value = True
        elif selector in _PENDO_CLOSE_SELECTORS:
            loc.is_visible.return_value = False
        else:
            # the overlay-presence selector itself
            loc.is_visible.return_value = overlay_visible
        locators_by_selector[selector] = loc
        return loc

    page.locator.side_effect = locator_factory
    return page


# ─── overlay absent: no-op, no click attempted ──────────────────────────────

def test_overlay_absent_is_a_noop():
    page = _make_page(overlay_visible=False, close_selector_visible=None)

    _dismiss_pendo_overlay_if_present(page)

    # Snapshot first: page.locator(selector) below reuses the cached mock
    # (see _make_page) and must not itself grow call_args_list while we
    # iterate it.
    for call_args in list(page.locator.call_args_list):
        selector = call_args.args[0]
        loc = page.locator(selector)
        assert loc.click.called is False


def test_overlay_absent_never_raises():
    page = _make_page(overlay_visible=False, close_selector_visible=None)
    _dismiss_pendo_overlay_if_present(page)  # must not raise


# ─── overlay present: dismiss via its real close control ───────────────────

def test_overlay_present_clicks_first_visible_close_control():
    target_selector = _PENDO_CLOSE_SELECTORS[2]
    page = _make_page(overlay_visible=True, close_selector_visible=target_selector)

    _dismiss_pendo_overlay_if_present(page)

    clicked_locator = page.locator(target_selector)
    clicked_locator.click.assert_called_once()
    # never a forced click bypassing Playwright's actionability checks
    click_kwargs = clicked_locator.click.call_args.kwargs
    assert click_kwargs.get("force") is not True


def test_overlay_present_tries_selectors_in_order_until_one_is_visible():
    target_selector = _PENDO_CLOSE_SELECTORS[-1]  # only the last one is visible
    page = _make_page(overlay_visible=True, close_selector_visible=target_selector)

    _dismiss_pendo_overlay_if_present(page)

    # every earlier selector's locator must have been checked but NOT clicked
    for selector in _PENDO_CLOSE_SELECTORS[:-1]:
        loc = page.locator(selector)
        assert loc.click.called is False
    page.locator(target_selector).click.assert_called_once()


def test_overlay_present_no_close_control_found_raises_clear_error():
    page = _make_page(overlay_visible=True, close_selector_visible=None)

    with pytest.raises(RuntimeError, match="Pendo onboarding overlay"):
        _dismiss_pendo_overlay_if_present(page)

    # must never have attempted a force click through it
    for selector in _PENDO_CLOSE_SELECTORS:
        assert page.locator(selector).click.called is False


def test_overlay_present_but_close_click_raises_is_treated_as_not_found():
    """If the only 'visible' close control's own .click() blows up
    (e.g. detached mid-animation), fall through to the clear error rather
    than crashing with that internal exception or forcing through."""
    target_selector = _PENDO_CLOSE_SELECTORS[0]
    page = _make_page(overlay_visible=True, close_selector_visible=target_selector)
    page.locator(target_selector).click.side_effect = RuntimeError("element detached")

    with pytest.raises(RuntimeError, match="Pendo onboarding overlay"):
        _dismiss_pendo_overlay_if_present(page)
