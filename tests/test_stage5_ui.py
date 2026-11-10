"""Stage 5: the three pages, run headless with Streamlit's AppTest (no browser needed)."""
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import ui
from procurement.models import Status, validate_form
from procurement.store import Store

R01 = dict(requester="Lina Odeh", department="Programs", item="Winter Kit", quantity="200", unit_price_usd="42",
           justification="Replace the cancelled winter kit distribution in Khan Younis.",
           supplier="Al-Noor Trading", quotes="3", budget_line="BL-WIN-26", manager="Samir Haddad",
           evidence="DST-001")


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Every test gets its own empty database, also inside the app."""
    monkeypatch.setattr(ui, "DB_PATH", tmp_path / "ui.db")
    monkeypatch.setattr(ui, "pipeline", lambda: {})          # never call a real API from the tests,
    monkeypatch.setattr(ui, "ai_enabled", lambda: False)     # even if a key file exists on this computer
    st.cache_resource.clear()
    s = Store(tmp_path / "ui.db")
    yield s
    s.close()
    st.cache_resource.clear()


ROOT = Path(__file__).parent.parent


def page(path: str) -> AppTest:
    return AppTest.from_file(str(ROOT / path), default_timeout=15)


def fill(at: AppTest, prefix: str, values: dict) -> None:
    for field, value in values.items():
        at.session_state[f"{prefix}_{field}"] = value


def submit(at: AppTest, label: str) -> AppTest:
    next(b for b in at.button if b.label.startswith(label)).click()
    return at.run()


def texts(elements) -> str:
    return " ".join(e.value for e in elements)


# --- New request -----------------------------------------------------------------------------
def test_page_shows_the_policy_before_anything_is_submitted(store):
    at = page("views/new_request.py").run()
    assert not at.exception
    html = " ".join(m.value for m in at.markdown)
    for rule in ("Catalogue", "Unit price", "Quotations", "Budget", "Manager", "Evidence", "Quantity", "Supplier"):
        assert rule in html


def test_missing_details_are_asked_for_and_saved_as_v1(store):
    at = page("views/new_request.py").run()
    fill(at, "new", {**R01, "supplier": "", "quotes": "", "manager": ""})
    at = submit(at, "Check and submit")
    assert "More information needed: Number of quotations, Approving manager, Supplier." in texts(at.warning)
    rec = store.get("PR-0001")
    assert rec.status is Status.NEEDS_INFO and rec.current_version == 1


def test_form_error_saves_nothing_and_hides_the_previous_result(store):
    at = page("views/new_request.py").run()
    fill(at, "new", R01)
    at = submit(at, "Check and submit")
    assert "Sent for human review" in texts(at.success)
    fill(at, "new", {**R01, "quantity": "-5"})
    at = submit(at, "Check and submit")
    assert "Nothing was saved" in texts(at.error)
    assert not at.success                                    # the PR-0001 result is not shown under the error
    assert len(store.list()) == 1


def test_arabic_justification_gets_the_arabic_headline(store):
    at = page("views/new_request.py").run()
    fill(at, "new", {**R01, "justification": "استبدال توزيعة الحقائب الشتوية الملغاة في خان يونس."})
    at = submit(at, "Check and submit")
    assert "أُرسل لمراجعة بشرية" in texts(at.success)


# --- My requests ------------------------------------------------------------------------------
def test_fix_form_is_prefilled_and_resubmits_as_v2(store):
    request, _ = validate_form(**{**R01, "supplier": "", "quotes": "", "manager": ""})
    store.submit(request)
    at = page("views/my_requests.py")
    at.session_state["my_selected"] = "PR-0001"
    at.run()
    assert at.session_state["fix_PR-0001_v1_evidence"] == "DST-001"         # kept, not retyped
    fill(at, "fix_PR-0001_v1", {"supplier": "Al-Noor Trading", "quotes": "3", "manager": "Samir Haddad"})
    at = submit(at, "Resubmit as v2")
    assert not at.exception
    rec = store.get("PR-0001")
    assert (rec.current_version, rec.status) == (2, Status.AWAITING_REVIEW)


def test_a_request_under_review_cannot_be_edited(store):
    request, _ = validate_form(**R01)
    store.submit(request)
    at = page("views/my_requests.py")
    at.session_state["my_selected"] = "PR-0001"
    at.run()
    assert "a person is reviewing it" in texts(at.info)
    assert not [b for b in at.button if b.label.startswith("Resubmit")]


# --- Review queue ----------------------------------------------------------------------------
def test_reviewer_approves_with_their_name(store):
    request, _ = validate_form(**R01)
    store.submit(request)
    at = page("views/review_queue.py")
    at.session_state["review_selected"] = "PR-0001"
    at.run()
    assert "not an approval" in texts(at.info)
    at = submit(at, "Record decision as Huda Saleh")
    rec = store.get("PR-0001")
    assert (rec.status, rec.decided_by) == (Status.APPROVED, "Huda Saleh")


def test_reviewer_is_warned_before_clicking_when_the_budget_was_used(store):
    first, _ = validate_form(**R01)
    second, _ = validate_form(**{**R01, "requester": "Omar Nassar"})
    store.submit(first)
    store.submit(second)
    store.decide("PR-0001", "approve", "Huda Saleh")
    at = page("views/review_queue.py")
    at.session_state["review_selected"] = "PR-0002"
    at.run()
    warning = texts(at.warning)
    assert "Something changed" in warning and "2,100.00 USD left" in warning
    at = submit(at, "Record decision")
    assert "Cannot approve" in texts(at.error)
    assert store.get("PR-0002").status is Status.AWAITING_REVIEW


def test_the_requester_cannot_approve_in_the_ui_either(store):
    request, _ = validate_form(**R01)
    store.submit(request)
    at = page("views/review_queue.py")
    at.session_state["review_selected"] = "PR-0001"
    at.session_state["reviewer"] = "Lina Odeh"
    at.run()
    at = submit(at, "Record decision")
    assert "own request" in texts(at.error)
    assert store.get("PR-0001").status is Status.AWAITING_REVIEW


def test_pasted_arabic_request_fills_the_form_for_review(store):
    at = page("views/new_request.py").run()
    at.session_state["paste_text"] = ("معك لينا عودة من البرامج. بدنا ١٥٠ حقيبة نظافة بسعر 15.5 دولار للحقيبة "
                                      "من Al-Noor Trading، عندنا ٣ عروض أسعار، بند BL-HYG-26، بموافقة المدير "
                                      "سمير حداد. حسب الملاحظة FB-0017.")
    at = submit(at, "Fill the form from this text")
    assert "Check them below before you submit" in texts(at.success)
    assert (at.session_state["new_quantity"], at.session_state["new_item"], at.session_state["new_manager"]) == \
           ("150", "Hygiene Kit", "سمير حداد")
    assert store.list() == []                                    # filling the form submits nothing
    at = submit(at, "Check and submit")
    assert "أُرسل لمراجعة بشرية" in texts(at.success)
    assert "Explained by: template" in " ".join(c.value for c in at.caption)
