"""Stage 1: reference data from projects 1 and 2, and the request form (T12)."""
import csv
from decimal import Decimal
from pathlib import Path

import pytest

from procurement.data import load, reference_data
from procurement.models import FRIENDLY, Request, detect_language, parse_refs, validate_form

DATA = Path(__file__).parent.parent / "data"
ROWS = list(csv.DictReader(open(DATA / "sample_requests.csv", encoding="utf-8")))
FORM_FIELDS = ["requester", "department", "item", "quantity", "unit_price_usd", "justification",
               "supplier", "quotes", "budget_line", "manager", "evidence"]


def form(row: dict) -> dict:
    """A sample row as the form would send it (strings, empty strings for empty fields)."""
    return {f: row[f] for f in FORM_FIELDS}


GOOD = form(ROWS[0])                                            # R01, complete and compliant


# --- reference data -----------------------------------------------------------------------
def test_price_benchmarks_come_from_project_1():
    ref = reference_data()
    assert {k: v.benchmark for k, v in ref.catalogue.items()} == {
        "winter kit": Decimal("42.0"), "food parcel": Decimal("28.0"), "blanket": Decimal("9.0"),
        "kitchen set": Decimal("35.0"), "hygiene kit": Decimal("15.5")}
    # the same numbers, recomputed straight from Project 1's file
    rows = list(csv.DictReader(open(DATA / "aid_distributions.csv", encoding="utf-8")))
    costs = [Decimal(r["unit_cost_usd"]) for r in rows if r["item"] == "Winter Kit"]
    assert sum(costs) / len(costs) == ref.catalogue["winter kit"].benchmark


def test_distributions_and_feedback_are_loaded_as_evidence():
    ref = reference_data()
    d = ref.distributions["DST-001"]
    assert (d.item, d.quantity, d.status) == ("Winter Kit", 250, "Cancelled")
    assert len(ref.distributions) == 60
    assert ref.feedback["FB-0005"].item == "Winter Kit"
    assert all(k.startswith("FB-") for k in ref.feedback)          # sensitive rows (no reference) are skipped


def test_budget_remaining_is_exact():
    ref = reference_data()
    assert ref.budget_line("bl-food-26").remaining == Decimal("1680")
    assert ref.budget_line("BL-WIN-26").remaining == Decimal("10500")
    assert ref.budget_line("BL-NOPE") is None


def test_lookups_ignore_case_and_spaces():
    ref = reference_data()
    assert ref.catalogue_item("  winter KIT ").item == "Winter Kit"
    s = ref.supplier("sabeel wholesale")
    assert s.registered and s.conflict_of_interest


def test_feedback_file_is_optional(tmp_path):
    for name in ("catalogue.csv", "budget_lines.csv", "suppliers.csv", "aid_distributions.csv"):
        (tmp_path / name).write_bytes((DATA / name).read_bytes())
    assert load(tmp_path).feedback == {}


# --- T12: invalid input never becomes a request ------------------------------------------------
@pytest.mark.parametrize("row", [r for r in ROWS if r["expected_action"] == "reject_form"], ids=lambda r: r["request_id"])
def test_T12_sample_invalid_requests_are_rejected(row):
    req, errors = validate_form(**form(row))
    assert req is None and errors


@pytest.mark.parametrize("row", [r for r in ROWS if r["expected_action"] != "reject_form"], ids=lambda r: r["request_id"])
def test_every_other_sample_is_a_valid_form(row):
    req, errors = validate_form(**form(row))
    assert errors == {}, errors


@pytest.mark.parametrize("field, value", [
    ("quantity", "-5"), ("quantity", "0"), ("quantity", "2.5"), ("quantity", "many"),
    ("unit_price_usd", "0"), ("unit_price_usd", "-3"), ("unit_price_usd", "abc"), ("unit_price_usd", "10000.01"),
    ("unit_price_usd", "4.555"), ("department", "Finance"), ("justification", "short"), ("requester", ""),
    ("quotes", "-1"), ("quotes", "three"),
])
def test_invalid_values_get_a_clear_message(field, value):
    req, errors = validate_form(**{**GOOD, field: value})
    assert req is None and errors[field] == FRIENDLY[field]


def test_details_may_be_empty_at_submission():
    req, errors = validate_form(**{**GOOD, "supplier": "", "quotes": "", "manager": "", "evidence": "",
                                   "budget_line": ""})
    assert errors == {} and req.missing_details() == ["supplier", "quotes", "budget_line", "manager", "evidence"]


def test_zero_quotations_is_a_value_not_a_missing_field():
    req, _ = validate_form(**{**GOOD, "quotes": "0"})
    assert req.quotes == 0 and "quotes" not in req.missing_details()


# --- money, references, language -------------------------------------------------------------------
def test_totals_are_exact_decimals():
    req, _ = validate_form(**{**GOOD, "quantity": "100", "unit_price_usd": "46.2"})
    assert req.total == Decimal("4620.00")
    req, _ = validate_form(**{**GOOD, "quantity": "3", "unit_price_usd": "0.10"})
    assert req.total == Decimal("0.30")                              # 0.1 * 3 in float is 0.30000000000000004


def test_quantity_accepts_thousands_separator():
    assert validate_form(**{**GOOD, "quantity": "1,200"})[0].quantity == 1200


@pytest.mark.parametrize("text, refs", [
    ("DST-001", ["DST-001"]), ("dst 001 and FB-0005", ["DST-001", "FB-0005"]),
    ("see fb0017, FB-0017", ["FB-0017"]), ("needs assessment", []), ("DST-1", [])])
def test_evidence_references_are_parsed(text, refs):
    assert parse_refs(text) == refs


def test_budget_line_is_normalised():
    assert validate_form(**{**GOOD, "budget_line": " bl-win-26 "})[0].budget_line == "BL-WIN-26"


def test_language_of_the_justification():
    assert detect_language(next(r for r in ROWS if r["request_id"] == "R19")["justification"]) == "ar"
    assert detect_language(GOOD["justification"]) == "en"
