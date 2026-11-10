"""Stage 4: storage, versions, resubmission (T13) and the reviewer's decision (T14)."""
import csv
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from procurement.agent import DecisionError
from procurement.models import Action, Request, Status, validate_form
from procurement.store import Store, StoreError, changes

DATA = Path(__file__).parent.parent / "data"
ROWS = {r["request_id"]: r for r in csv.DictReader(open(DATA / "sample_requests.csv", encoding="utf-8"))}
FORM_FIELDS = ["requester", "department", "item", "quantity", "unit_price_usd", "justification",
               "supplier", "quotes", "budget_line", "manager", "evidence"]


def sample(rid: str, **changes_) -> Request:
    request, errors = validate_form(**{**{f: ROWS[rid][f] for f in FORM_FIELDS}, **changes_})
    assert not errors, errors
    return request


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


# --- submit ---------------------------------------------------------------------------------
def test_submit_saves_version_1_with_the_agents_action(store):
    ref, a = store.submit(sample("R01"))
    assert ref == "PR-0001" and a.action is Action.SEND_FOR_REVIEW
    rec = store.get(ref)
    assert (rec.status, rec.current_version, rec.requester) == (Status.AWAITING_REVIEW, 1, "Lina Odeh")
    assert rec.latest.request == sample("R01")
    assert [c.outcome for c in rec.latest.results] == [c.outcome for c in a.results]


def test_references_are_sequential_and_lookup_ignores_case(store):
    refs = [store.submit(sample(r))[0] for r in ("R01", "R02", "R03")]
    assert refs == ["PR-0001", "PR-0002", "PR-0003"]
    assert store.get(" pr-0002 ").ref == "PR-0002"
    with pytest.raises(StoreError, match="No request"):
        store.get("PR-9999")


def test_money_comes_back_as_the_exact_decimal(store):
    ref, _ = store.submit(sample("R06"))                                     # 46.20, the boundary
    price = store.get(ref).latest.request.unit_price_usd
    assert isinstance(price, Decimal) and price == Decimal("46.20")
    assert store.conn.execute("SELECT total_usd FROM versions").fetchone()[0] == "4620.00"


def test_history_starts_with_submitted_then_the_agent(store):
    ref, _ = store.submit(sample("R02"))
    h = store.history(ref)
    assert [(e["actor"], e["event"]) for e in h] == [("Lina Odeh", "submitted"), ("agent", "ask_for_info")]
    assert "Number of quotations" in h[1]["detail"]


def test_an_arabic_request_is_stored_as_written(store):
    ref, _ = store.submit(sample("R19"))
    assert store.get(ref).latest.request.requester == "لينا عودة"


def test_injection_text_is_stored_as_text(store):
    ref, _ = store.submit(sample("R01", justification="x'); DROP TABLE requests; -- need kits now"))
    assert store.get(ref).latest.request.justification.startswith("x'); DROP TABLE")
    assert store.conn.execute("SELECT count(*) FROM requests").fetchone()[0] == 1


# --- T13: resubmission ----------------------------------------------------------------------
def test_t13_add_the_missing_fields_and_resubmit_without_retyping(store):
    ref, a = store.submit(sample("R02"))
    assert a.action is Action.ASK_FOR_INFO
    # the form is prefilled from the latest version; the requester only adds the three fields
    prefilled = store.get(ref).latest.request.model_dump()
    fixed = Request(**{**prefilled, "supplier": "Al-Noor Trading", "quotes": 3, "manager": "Samir Haddad"})
    a2 = store.resubmit(ref, fixed)
    assert a2.action is Action.SEND_FOR_REVIEW
    rec = store.get(ref)
    assert (rec.ref, rec.current_version, rec.status) == (ref, 2, Status.AWAITING_REVIEW)
    assert len(store.list()) == 1                                             # same request, not a new one
    assert [v.action for v in store.versions(ref)] == [Action.ASK_FOR_INFO, Action.SEND_FOR_REVIEW]
    detail = store.history(ref)[2]["detail"]
    assert detail.startswith("v2 ·") and "supplier: (empty) -> Al-Noor Trading" in detail
    assert "quotes: (empty) -> 3" in detail and "manager: (empty) -> Samir Haddad" in detail


def test_version_1_is_never_overwritten(store):
    ref, _ = store.submit(sample("R03"))                                       # price 12, policy issue
    store.resubmit(ref, sample("R03", unit_price_usd="9.50"))
    v1, v2 = store.versions(ref)
    assert v1.request.unit_price_usd == Decimal("12") and v1.action is Action.FLAG_POLICY_ISSUE
    assert v2.request.unit_price_usd == Decimal("9.50") and v2.action is Action.SEND_FOR_REVIEW


def test_a_fix_can_still_fail_and_go_round_again(store):
    ref, _ = store.submit(sample("R03"))
    a = store.resubmit(ref, sample("R03", unit_price_usd="10"))               # still above 9.90
    assert a.action is Action.FLAG_POLICY_ISSUE and store.get(ref).current_version == 2


def test_nothing_changed_is_refused(store):
    ref, _ = store.submit(sample("R02"))
    with pytest.raises(StoreError, match="Nothing changed"):
        store.resubmit(ref, sample("R02"))


def test_the_requester_cannot_change_on_resubmission(store):
    ref, _ = store.submit(sample("R02"))
    with pytest.raises(StoreError, match="requester cannot change"):
        store.resubmit(ref, sample("R02", requester="Omar Nassar", supplier="Al-Noor Trading"))


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_final_and_waiting_requests_cannot_be_resubmitted(store, decision):
    ref, _ = store.submit(sample("R01"))
    with pytest.raises(StoreError, match="awaiting_review"):                   # a person is looking at it
        store.resubmit(ref, sample("R01", quantity=150))
    store.decide(ref, decision, "Huda Saleh", "Not needed this month.")
    with pytest.raises(StoreError, match="only requests that need information"):
        store.resubmit(ref, sample("R01", quantity=150))


def test_a_returned_request_can_be_fixed_and_the_old_decision_is_cleared(store):
    ref, _ = store.submit(sample("R01"))
    store.decide(ref, "return", "Huda Saleh", "Please reduce to 150 kits.")
    assert store.get(ref).decided_by == "Huda Saleh"
    store.resubmit(ref, sample("R01", quantity=150))
    rec = store.get(ref)
    assert (rec.status, rec.current_version, rec.decided_by) == (Status.AWAITING_REVIEW, 2, None)


# --- T14: the reviewer's decision -----------------------------------------------------------
@pytest.mark.parametrize("decision, status, reason", [
    ("approve", Status.APPROVED, ""),
    ("reject", Status.REJECTED, "Duplicate of PR-0004."),
    ("return", Status.RETURNED, "Attach the three quotations."),
])
def test_t14_the_persons_name_and_reason_are_saved(store, decision, status, reason):
    ref, _ = store.submit(sample("R01"))
    assert store.decide(ref, decision, "  Huda   Saleh ", reason, version=1) is status
    rec = store.get(ref)
    assert (rec.status, rec.decided_by, rec.decision_reason) == (status, "Huda Saleh", reason or None)
    last = store.history(ref)[-1]
    assert (last["actor"], last["event"], last["detail"]) == ("Huda Saleh", status.value, reason)


def test_a_request_with_a_policy_issue_cannot_be_approved(store):
    ref, _ = store.submit(sample("R03"))
    with pytest.raises(DecisionError, match="awaiting review"):
        store.decide(ref, "approve", "Huda Saleh")
    assert store.get(ref).status is Status.POLICY_ISSUE


def test_the_requester_cannot_approve_their_own_request(store):
    ref, _ = store.submit(sample("R01"))
    with pytest.raises(DecisionError, match="own request"):
        store.decide(ref, "approve", "lina odeh")


def test_reviewer_must_decide_on_the_version_they_saw(store):
    ref, _ = store.submit(sample("R02"))
    store.resubmit(ref, sample("R02", supplier="Al-Noor Trading", quotes=3, manager="Samir Haddad"))
    with pytest.raises(DecisionError, match="changed while you were reviewing"):
        store.decide(ref, "approve", "Huda Saleh", version=1)
    assert store.decide(ref, "approve", "Huda Saleh", version=2) is Status.APPROVED


def test_a_refused_decision_writes_nothing(store):
    ref, _ = store.submit(sample("R01"))
    before = store.history(ref)
    with pytest.raises(DecisionError):
        store.decide(ref, "reject", "Huda Saleh", "")
    assert store.history(ref) == before and store.get(ref).status is Status.AWAITING_REVIEW


# --- rules 4 and 5: approved requests use the budget ----------------------------------------
def test_approved_requests_reduce_the_remaining_budget(store):
    """BL-WIN-26 has 10,500 left. 8,400 is approved, so a second 8,400 no longer fits."""
    first, _ = store.submit(sample("R01"))
    store.decide(first, "approve", "Huda Saleh")
    assert store.committed_by_line() == {"BL-WIN-26": Decimal("8400.00")}
    assert store.reference().budget_line("BL-WIN-26").remaining == Decimal("2100.00")
    _, a = store.submit(sample("R01", requester="Omar Nassar"))
    assert a.action is Action.FLAG_POLICY_ISSUE and [c.code for c in a.failed] == ["C4"]
    assert "2,100.00 USD left" in a.failed[0].message


def test_waiting_and_rejected_requests_do_not_use_the_budget(store):
    a_ref, _ = store.submit(sample("R01"))
    b_ref, _ = store.submit(sample("R01", requester="Omar Nassar"))
    store.decide(b_ref, "reject", "Huda Saleh", "Not a priority.")
    assert store.committed_by_line() == {}
    assert store.get(a_ref).status is Status.AWAITING_REVIEW


def test_approval_re_checks_the_budget_at_the_moment_of_approval(store):
    """Both passed when submitted; approving the first leaves too little for the second."""
    a_ref, _ = store.submit(sample("R01"))
    b_ref, b = store.submit(sample("R01", requester="Omar Nassar"))
    assert b.action is Action.SEND_FOR_REVIEW
    store.decide(a_ref, "approve", "Huda Saleh")
    with pytest.raises(DecisionError, match="no longer passes the checks.*C4"):
        store.decide(b_ref, "approve", "Huda Saleh")
    assert store.get(b_ref).status is Status.AWAITING_REVIEW               # a person can still reject or return
    assert store.decide(b_ref, "return", "Huda Saleh", "Budget used by PR-0001; reduce to 50.") is Status.RETURNED


# --- lists and the queue --------------------------------------------------------------------
def test_queue_holds_only_requests_awaiting_a_person(store):
    for rid in ("R01", "R02", "R03", "R05", "R19"):
        store.submit(sample(rid))
    assert [r.ref for r in store.queue()] == ["PR-0001", "PR-0004", "PR-0005"]
    assert [r.ref for r in store.list(requester="omar nassar")] == ["PR-0003"]


def test_changes_lists_only_what_changed():
    assert changes({"a": "", "b": "1", "c": None}, {"a": "x", "b": "1", "c": ""}) == ["a: (empty) -> x"]


def test_the_database_survives_a_restart(tmp_path):
    path = tmp_path / "restart.db"
    s = Store(path)
    ref, _ = s.submit(sample("R01"))
    s.close()
    s2 = Store(path)
    assert s2.get(ref).status is Status.AWAITING_REVIEW and len(s2.history(ref)) == 2
    assert s2.conn.execute("PRAGMA user_version").fetchone()[0] == 1
    s2.close()


def test_history_can_not_point_to_a_missing_request(store):
    with pytest.raises(sqlite3.IntegrityError):
        with store.conn:
            store.conn.execute("INSERT INTO events (ref, at, actor, event) VALUES ('PR-0404', 'x', 'y', 'z')")


def test_reset_deletes_everything_and_numbers_start_again(store):
    store.submit(sample("R01"))
    store.reset()
    assert store.list() == [] and store.committed_by_line() == {}
    assert store.submit(sample("R02"))[0] == "PR-0001"


# --- concurrency: one shared connection, two sessions at once (found on the live server) --------
def test_review_queue_while_another_session_submits(tmp_path):
    """get_store() is cached, so one connection is shared by every session. A reviewer opening
    the queue while another session is inside submit() (row inserted, ref not set yet) must never
    crash or see a half-written request."""
    import threading
    store = Store(tmp_path / "race.db")
    rid = next(iter(ROWS))
    errors, done = [], threading.Event()

    def writer():
        for _ in range(150):
            store.submit(sample(rid))
        done.set()

    def reader():
        while not done.is_set():
            try:
                for rec in store.list():
                    assert rec.ref and rec.ref.startswith("PR-")      # never a NULL / half-written ref
            except Exception as err:                                   # noqa: BLE001 - any error is the bug
                errors.append(repr(err))

    threads = [threading.Thread(target=reader), threading.Thread(target=writer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert errors == []
    assert len(store.list()) == 150
    store.close()


def test_a_request_without_its_reference_is_never_listed(tmp_path):
    store = Store(tmp_path / "half.db")
    with store.conn:
        store.conn.execute("INSERT INTO requests (requester, department, status, current_version, "
                           "created_at, updated_at) VALUES ('x','Programs','awaiting_review',1,'t','t')")
    assert store.list() == []                                          # the NULL-ref row is skipped
    store.close()
