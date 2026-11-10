"""Stage 2: the 8 policy checks (T1-T11 of PROJECT_PLAN.md §9).

Three kinds of tests:
  1. every sample request gives EXACTLY the expected failed checks and missing fields;
  2. every boundary of the written policy, on both sides (46.20 / 46.30, 1,680 / 1,708 ...);
  3. the shape of every result: one per rule, numbers in the message, a fix when it is not a pass.
"""
import csv
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from procurement.checks import (CHECKS, Outcome, c1_catalogue, c2_price, c3_quotations, c4_budget, c5_approval,
                                c6_evidence, c7_quantity, c8_supplier, failed, missing, required_quotes,
                                run_checks)
from procurement.data import BudgetLine, reference_data
from procurement.models import Request, validate_form

DATA = Path(__file__).parent.parent / "data"
ROWS = list(csv.DictReader(open(DATA / "sample_requests.csv", encoding="utf-8")))
FORM_FIELDS = ["requester", "department", "item", "quantity", "unit_price_usd", "justification",
               "supplier", "quotes", "budget_line", "manager", "evidence"]
REF = reference_data()

GOOD = dict(requester="Lina Odeh", department="Programs", item="Winter Kit", quantity=200, unit_price_usd="42",
            justification="Replace the cancelled winter kit distribution in Khan Younis.",
            supplier="Al-Noor Trading", quotes=3, budget_line="BL-WIN-26", manager="Samir Haddad",
            evidence="DST-001")


def req(**changes) -> Request:
    return Request(**{**GOOD, **changes})


def split(value: str) -> list[str]:
    return [v for v in value.split(";") if v]


# --- 1. every sample request -----------------------------------------------------------------
CHECKED = [r for r in ROWS if r["expected_action"] != "reject_form"]


@pytest.mark.parametrize("row", CHECKED, ids=[r["request_id"] for r in CHECKED])
def test_sample_gives_exactly_the_expected_failed_and_missing(row):
    request, errors = validate_form(**{f: row[f] for f in FORM_FIELDS})
    assert not errors, errors
    results = run_checks(request, REF)
    assert [r.code for r in failed(results)] == split(row["expected_failed"]), row["why"]
    assert [r.field for r in missing(results)] == split(row["expected_missing"]), row["why"]


def test_form_errors_never_reach_the_checks():
    for row in ROWS:
        if row["expected_action"] == "reject_form":
            request, errors = validate_form(**{f: row[f] for f in FORM_FIELDS})
            assert request is None and errors


def test_the_samples_cover_every_rule_failing_at_least_once():
    codes = {c for r in ROWS for c in split(r["expected_failed"])}
    assert codes == {f"C{i}" for i in range(1, 9)}


# --- 2. boundaries ---------------------------------------------------------------------------
@pytest.mark.parametrize("price, outcome", [("46.20", Outcome.PASS), ("46.21", Outcome.FAIL),
                                            ("46.30", Outcome.FAIL), ("41.99", Outcome.PASS)])
def test_c2_price_limit_is_exactly_110_percent(price, outcome):
    r = c2_price(req(unit_price_usd=price, quantity=100), REF)
    assert r.outcome is outcome
    assert "46.20" in r.message and "42.00" in r.message


def test_c2_item_matching_ignores_case_and_spaces():
    assert c2_price(req(item="  winter KIT "), REF).outcome is Outcome.PASS


@pytest.mark.parametrize("total, need", [("0.01", 1), ("999.99", 1), ("1000", 3), ("10000", 3),
                                         ("10000.01", None)])
def test_required_quotes_boundaries(total, need):
    assert required_quotes(Decimal(total)) == need


@pytest.mark.parametrize("quantity, quotes, outcome", [(111, 1, Outcome.PASS),     # 999 USD: one is enough
                                                       (112, 1, Outcome.FAIL),     # 1,008 USD: needs three
                                                       (112, 3, Outcome.PASS)])
def test_c3_one_quote_below_1000(quantity, quotes, outcome):
    r = c3_quotations(req(item="Blanket", unit_price_usd="9", quantity=quantity, quotes=quotes), REF)
    assert r.outcome is outcome


def test_c3_exactly_10000_is_allowed_but_one_cent_more_needs_a_tender():
    at_limit = req(quantity=250, unit_price_usd="40")                              # 10,000.00
    over = req(quantity=101, unit_price_usd="99.01")                               # 10,000.01
    assert at_limit.total == Decimal("10000.00") and over.total == Decimal("10000.01")
    assert c3_quotations(at_limit, REF).outcome is Outcome.PASS
    r = c3_quotations(over, REF)
    assert r.outcome is Outcome.FAIL and "tender" in r.message


def test_c3_tender_is_a_failure_even_when_quotes_are_missing():
    """More quotations would not fix it, so the agent must not just ask for them."""
    assert c3_quotations(req(quantity=101, unit_price_usd="99.01", quotes=None), REF).outcome is Outcome.FAIL


def test_c3_missing_quotes_is_missing_not_fail():
    r = c3_quotations(req(quotes=None), REF)
    assert (r.outcome, r.field) == (Outcome.MISSING, "quotes") and "3" in r.message


def test_c3_zero_quotes_is_a_real_zero():
    assert c3_quotations(req(quotes=0), REF).outcome is Outcome.FAIL


@pytest.mark.parametrize("quantity, outcome", [(60, Outcome.PASS), (61, Outcome.FAIL)])
def test_c4_exactly_the_remaining_budget_passes(quantity, outcome):
    r = c4_budget(req(item="Food Parcel", unit_price_usd="28", quantity=quantity, budget_line="BL-FOOD-26",
                      evidence="DST-006"), REF)
    assert r.outcome is outcome
    assert "1,680.00" in r.message


def test_c4_budget_line_for_another_category_fails():
    r = c4_budget(req(item="Blanket", unit_price_usd="9", quantity=100, budget_line="BL-MEAL-26"), REF)
    assert r.outcome is Outcome.FAIL and "cannot pay for Blanket" in r.message


def test_c4_code_is_case_insensitive_and_unknown_code_is_missing():
    assert c4_budget(req(budget_line="bl-win-26"), REF).outcome is Outcome.PASS
    r = c4_budget(req(budget_line="BL-XYZ"), REF)
    assert (r.outcome, r.field) == (Outcome.MISSING, "budget_line") and "BL-WIN-26" in r.fix


def test_c4_remaining_is_annual_minus_committed():
    ref = replace(REF, budget_lines={**REF.budget_lines, "BL-WIN-26": BudgetLine(
        "BL-WIN-26", "Programs", "winter", "test", Decimal("9000"), Decimal("600.00"))})   # 8,400 left
    assert c4_budget(req(quantity=200), ref).outcome is Outcome.PASS                  # 8,400
    assert c4_budget(req(quantity=201), ref).outcome is Outcome.FAIL                  # 8,442


def test_c5_same_person_ignores_case_and_extra_spaces():
    r = c5_approval(req(manager="  lina   ODEH "), REF)
    assert r.outcome is Outcome.FAIL and "requester" in r.message


def test_c5_missing_manager_is_missing():
    assert c5_approval(req(manager=""), REF).field == "manager"


@pytest.mark.parametrize("evidence", ["", "see the attached report", "DST-999", "FB-9999"])
def test_c6_absent_or_unknown_evidence_is_missing(evidence):
    r = c6_evidence(req(evidence=evidence), REF)
    assert (r.outcome, r.field) == (Outcome.MISSING, "evidence")


def test_c6_evidence_about_another_item_fails():
    r = c6_evidence(req(evidence="FB-0003"), REF)                                   # Kitchen Set feedback
    assert r.outcome is Outcome.FAIL and "Kitchen Set" in r.message


def test_c6_accepts_project_2_feedback_and_mixed_references():
    r = c6_evidence(req(evidence="dst 001 and fb-0005"), REF)
    assert r.outcome is Outcome.PASS and "DST-001" in r.message and "FB-0005" in r.message


def test_c6_one_bad_reference_among_good_ones_fails():
    assert c6_evidence(req(evidence="DST-001, FB-0003"), REF).outcome is Outcome.FAIL


@pytest.mark.parametrize("quantity, outcome", [(275, Outcome.PASS), (276, Outcome.FAIL)])
def test_c7_quantity_limit_is_110_percent_of_the_distribution(quantity, outcome):
    # DST-001 planned 250 -> 275; budget and price are not the question here
    r = c7_quantity(req(quantity=quantity), REF)
    assert r.outcome is outcome and "250" in r.message and "275" in r.message


def test_c7_does_not_apply_without_a_distribution():
    assert c7_quantity(req(evidence="FB-0005"), REF).outcome is Outcome.NA


def test_c7_adds_up_several_distributions():
    total = REF.distributions["DST-001"].quantity + REF.distributions["DST-017"].quantity
    r = c7_quantity(req(evidence="DST-001 DST-017"), REF)
    assert str(total) in r.message


@pytest.mark.parametrize("supplier, outcome", [("Al-Noor Trading", Outcome.PASS), ("al-noor trading", Outcome.PASS),
                                               ("Fast Deals LLC", Outcome.FAIL), ("Sabeel Wholesale", Outcome.FAIL)])
def test_c8_supplier(supplier, outcome):
    assert c8_supplier(req(supplier=supplier), REF).outcome is outcome


def test_c8_conflict_is_explained_to_the_reviewer():
    assert "conflict of interest" in c8_supplier(req(supplier="Sabeel Wholesale"), REF).message


def test_item_outside_the_catalogue_fails_c1_and_the_item_rules_do_not_apply():
    results = {r.code: r for r in run_checks(req(item="Solar Lamp", evidence="DST-046"), REF)}
    assert results["C1"].outcome is Outcome.FAIL
    assert results["C2"].outcome is Outcome.NA and results["C6"].outcome is Outcome.NA


# --- 3. the shape of every result ------------------------------------------------------------
ALL_RESULTS = [res for row in CHECKED
               for res in run_checks(validate_form(**{f: row[f] for f in FORM_FIELDS})[0], REF)]


def test_one_result_per_rule_in_order():
    assert [r.code for r in run_checks(req(), REF)] == [f"C{i}" for i in range(1, 9)]
    assert len(CHECKS) == 8


def test_every_non_pass_tells_the_requester_what_to_do():
    for r in ALL_RESULTS:
        if r.outcome in (Outcome.FAIL, Outcome.MISSING):
            assert r.fix, r
        if r.outcome is Outcome.MISSING:
            assert r.field in {"supplier", "quotes", "budget_line", "manager", "evidence"}, r


def test_number_rules_show_their_numbers():
    for r in ALL_RESULTS:
        if r.code in {"C2", "C3", "C4", "C7"} and r.outcome in (Outcome.PASS, Outcome.FAIL):
            assert any(ch.isdigit() for ch in r.message), r


def test_arabic_names_work():
    r = c5_approval(req(requester="لينا عودة", manager="سمير حداد"), REF)
    assert r.outcome is Outcome.PASS
