"""Stage 3: the decision table and the agent (T1, T2, and the 'never approves' rule)."""
import csv
import itertools
from pathlib import Path

import pytest

from procurement.agent import (DecisionError, RECOMMENDATION_NOTE, STATUS_FOR, assess, decide,
                               human_decision)
from procurement.checks import CheckResult, Outcome
from procurement.models import Action, Request, Status, validate_form

DATA = Path(__file__).parent.parent / "data"
ROWS = list(csv.DictReader(open(DATA / "sample_requests.csv", encoding="utf-8")))
FORM_FIELDS = ["requester", "department", "item", "quantity", "unit_price_usd", "justification",
               "supplier", "quotes", "budget_line", "manager", "evidence"]
CHECKED = [r for r in ROWS if r["expected_action"] != "reject_form"]


def request_of(row) -> Request:
    return validate_form(**{f: row[f] for f in FORM_FIELDS})[0]


def by_id(rid):
    return next(r for r in ROWS if r["request_id"] == rid)


# --- the decision table, on its own ----------------------------------------------------------
def result(outcome: Outcome) -> CheckResult:
    return CheckResult("C0", "test", outcome, "m", "fix" if outcome in (Outcome.FAIL, Outcome.MISSING) else "",
                       "supplier" if outcome is Outcome.MISSING else None)


@pytest.mark.parametrize("outcomes", list(itertools.product(list(Outcome), repeat=3)))
def test_decision_table_for_every_combination(outcomes):
    """27 combinations of three outcomes: failure wins, then missing, then review."""
    action = decide([result(o) for o in outcomes])
    if Outcome.FAIL in outcomes:
        assert action is Action.FLAG_POLICY_ISSUE
    elif Outcome.MISSING in outcomes:
        assert action is Action.ASK_FOR_INFO
    else:
        assert action is Action.SEND_FOR_REVIEW


def test_the_agent_has_no_way_to_approve():
    assert Status.APPROVED not in STATUS_FOR.values()
    assert set(STATUS_FOR) == {Action.ASK_FOR_INFO, Action.FLAG_POLICY_ISSUE, Action.SEND_FOR_REVIEW}


# --- the agent on every sample ----------------------------------------------------------------
@pytest.mark.parametrize("row", CHECKED, ids=[r["request_id"] for r in CHECKED])
def test_sample_gets_the_expected_action(row):
    a = assess(request_of(row))
    assert a.action.value == row["expected_action"], row["why"]
    assert a.status is STATUS_FOR[a.action] and a.status is not Status.APPROVED


def test_t1_compliant_request_goes_to_a_person_not_approved():
    a = assess(request_of(by_id("R01")))
    assert (a.action, a.status) == (Action.SEND_FOR_REVIEW, Status.AWAITING_REVIEW)
    assert RECOMMENDATION_NOTE in a.summary()
    assert "Waiting for a person" in a.steps[-1]


def test_t2_asks_for_exactly_the_three_missing_fields():
    a = assess(request_of(by_id("R02")))
    assert a.action is Action.ASK_FOR_INFO
    assert a.missing_fields == ["quotes", "manager", "supplier"]
    assert a.headline == "More information needed: Number of quotations, Approving manager, Supplier."
    for label in ("عدد عروض الأسعار", "المدير الموافق", "المورّد"):
        assert label in a.headline_ar


def test_failure_wins_but_missing_fields_are_still_listed():
    """Price too high AND no supplier: flag the policy issue, and say the supplier is missing too."""
    row = {**by_id("R03"), "supplier": ""}
    a = assess(request_of(row))
    assert a.action is Action.FLAG_POLICY_ISSUE
    assert [c.code for c in a.failed] == ["C2"] and a.missing_fields == ["supplier"]
    summary = a.summary()
    assert "C2: Negotiate the price down to 9.90" in summary and "C8: Enter the supplier" in summary


def test_t3_summary_shows_the_price_and_the_limit():
    s = assess(request_of(by_id("R03"))).summary()
    assert "12.00 USD" in s and "9.90 USD" in s


def test_t8_unknown_evidence_is_asked_for_not_invented():
    a = assess(request_of(by_id("R14")))
    assert a.action is Action.ASK_FOR_INFO and a.missing_fields == ["evidence"]
    assert "DST-099 was not found" in a.summary()


def test_headline_counts_rules_and_notes_not_applicable():
    assert assess(request_of(by_id("R07"))).headline.startswith("Policy issue: 1 rule not met")
    assert "(1 not applicable)" in assess(request_of(by_id("R19"))).headline


def test_the_trace_shows_observe_check_decide():
    steps = assess(request_of(by_id("R19"))).steps
    assert [s.split(":")[0] for s in steps] == ["Observed", "Checked 8 rules", "Decided", "Status"]
    assert "justification in Arabic" in steps[0]


def test_same_request_same_answer():
    r = request_of(by_id("R04"))
    assert assess(r).summary() == assess(r).summary()


# --- finish: the human decision (T14 rules; saving comes in stage 4) ------------------------
@pytest.mark.parametrize("decision, status", [("approve", Status.APPROVED), ("reject", Status.REJECTED),
                                              ("return", Status.RETURNED)])
def test_a_named_person_decides(decision, status):
    assert human_decision(Status.AWAITING_REVIEW, decision, "Huda Saleh", "Lina Odeh",
                          "Price is fine, go ahead.") is status


def test_approve_does_not_need_a_reason_but_reject_and_return_do():
    assert human_decision(Status.AWAITING_REVIEW, "approve", "Huda Saleh", "Lina Odeh") is Status.APPROVED
    for decision in ("reject", "return"):
        with pytest.raises(DecisionError, match="reason"):
            human_decision(Status.AWAITING_REVIEW, decision, "Huda Saleh", "Lina Odeh", " ok ")


def test_requester_cannot_decide_their_own_request():
    with pytest.raises(DecisionError, match="own request"):
        human_decision(Status.AWAITING_REVIEW, "approve", " lina  odeh", "Lina Odeh")


@pytest.mark.parametrize("current", [s for s in Status if s is not Status.AWAITING_REVIEW])
def test_only_requests_awaiting_review_can_be_decided(current):
    """A request with a policy issue cannot be approved by skipping the checks."""
    with pytest.raises(DecisionError, match="awaiting review"):
        human_decision(current, "approve", "Huda Saleh", "Lina Odeh")


@pytest.mark.parametrize("reviewer, decision", [("", "approve"), ("H", "approve"), ("Huda", "auto-approve")])
def test_reviewer_name_and_decision_are_required(reviewer, decision):
    with pytest.raises(DecisionError):
        human_decision(Status.AWAITING_REVIEW, decision, reviewer, "Lina Odeh")
