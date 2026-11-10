"""
The agent: one purchase request in, one next action out.

    observe   the validated request (stage 1)
    check     the 8 policy rules, one result each (stage 2)
    decide    the decision table below: code, not a model
    act       one of the four allowed actions, with a summary a person can act on

The decision table (PROJECT_PLAN.md §7)
---------------------------------------
    at least one check FAILS        -> flag_policy_issue   (missing fields are listed too)
    no failure, something MISSING   -> ask_for_info        (only the missing fields)
    everything passes / n/a         -> send_for_review     (a PERSON decides)
    a person decides                -> finish              (approved / rejected / returned)

Why a failure wins over missing information: if the price is 30% too high,
adding a supplier name will not save the request. Telling the requester only
"add the supplier" would send them round the loop for nothing.

The one rule that is never broken: the agent does not approve. The best it
can do is send_for_review. Only `human_decision` can set APPROVED, and it
needs a named reviewer who is not the requester.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .checks import CheckResult, Outcome, failed, missing, money, run_checks
from .data import ReferenceData, reference_data
from .models import Action, Request, Status

STATUS_FOR = {                                     # what each agent action leaves the request as
    Action.ASK_FOR_INFO: Status.NEEDS_INFO,
    Action.FLAG_POLICY_ISSUE: Status.POLICY_ISSUE,
    Action.SEND_FOR_REVIEW: Status.AWAITING_REVIEW,
}

FIELD_LABEL = {"supplier": "Supplier", "quotes": "Number of quotations", "budget_line": "Budget line",
               "manager": "Approving manager", "evidence": "Evidence (DST-xxx or FB-xxxx)"}
FIELD_LABEL_AR = {"supplier": "المورّد", "quotes": "عدد عروض الأسعار", "budget_line": "بند الميزانية",
                  "manager": "المدير الموافق", "evidence": "الدليل (DST-xxx أو FB-xxxx)"}
CHECK_NAME_AR = {"C1": "الكتالوج", "C2": "سعر الوحدة", "C3": "عروض الأسعار", "C4": "الميزانية",
                 "C5": "موافقة المدير", "C6": "الدليل", "C7": "الكمية", "C8": "المورّد"}

RECOMMENDATION_NOTE = "This is a recommendation, not an approval. A person decides."


def decide(results: list[CheckResult]) -> Action:
    """The decision table. Pure: same results -> same action, every time."""
    if failed(results):
        return Action.FLAG_POLICY_ISSUE
    if missing(results):
        return Action.ASK_FOR_INFO
    return Action.SEND_FOR_REVIEW


@dataclass
class Assessment:
    """Everything the requester and the reviewer see for one version of a request."""
    request: Request
    results: list[CheckResult]
    action: Action
    status: Status
    steps: list[str] = field(default_factory=list)          # the agent's trace, shown in the UI

    @property
    def failed(self) -> list[CheckResult]:
        return failed(self.results)

    @property
    def missing(self) -> list[CheckResult]:
        return missing(self.results)

    @property
    def missing_fields(self) -> list[str]:
        return [r.field for r in self.missing]

    @property
    def headline(self) -> str:
        if self.action is Action.FLAG_POLICY_ISSUE:
            n = len(self.failed)
            return f"Policy issue: {n} rule{'s' if n > 1 else ''} not met. Fix and resubmit."
        if self.action is Action.ASK_FOR_INFO:
            return "More information needed: " + ", ".join(FIELD_LABEL[f] for f in self.missing_fields) + "."
        na = sum(c.outcome is Outcome.NA for c in self.results)
        note = f" ({na} not applicable)" if na else ""
        return f"All applicable checks passed{note}. Sent for human review."

    @property
    def headline_ar(self) -> str:
        if self.action is Action.FLAG_POLICY_ISSUE:
            names = "، ".join(CHECK_NAME_AR[r.code] for r in self.failed)
            return f"الطلب مخالف للسياسة في: {names}. عدّل الطلب وأعد إرساله."
        if self.action is Action.ASK_FOR_INFO:
            return "نحتاج معلومات إضافية: " + "، ".join(FIELD_LABEL_AR[f] for f in self.missing_fields) + "."
        return "نجح الطلب في كل الفحوص المنطبقة عليه وأُرسل لمراجعة بشرية. هذه توصية وليست موافقة."

    def summary(self) -> str:
        """The decision-support summary (PROJECT_PLAN.md §5) as plain text."""
        r = self.request
        icon = {Outcome.PASS: "PASS", Outcome.FAIL: "FAIL", Outcome.MISSING: "MISSING", Outcome.NA: "n/a"}
        lines = [f"{r.item} · {r.quantity} × {money(r.unit_price_usd)} USD = {money(r.total)} USD"
                 f" · {r.supplier or '(no supplier)'} · {r.budget_line or '(no budget line)'}"
                 f" · {r.evidence or '(no evidence)'}", ""]
        lines += [f"{icon[c.outcome]:>7}  {c.code} {c.name}: {c.message}" for c in self.results]
        lines += ["", f"Next action: {self.action.value} — {self.headline}"]
        for c in self.failed + self.missing:
            lines.append(f"  - {c.code}: {c.fix}")
        if self.action is Action.SEND_FOR_REVIEW:
            lines.append(RECOMMENDATION_NOTE)
        return "\n".join(lines)


def assess(request: Request, ref: ReferenceData | None = None) -> Assessment:
    """Run the agent once on a validated request."""
    ref = ref or reference_data()
    steps = [f"Observed: {request.item}, {request.quantity} × {money(request.unit_price_usd)} = "
             f"{money(request.total)} USD, justification in {'Arabic' if request.language == 'ar' else 'English'}."]
    results = run_checks(request, ref)
    counts = {o: sum(c.outcome is o for c in results) for o in Outcome}
    steps.append("Checked 8 rules: " + ", ".join(f"{n} {o.value}" for o, n in counts.items() if n) + ".")
    action = decide(results)
    reason = {Action.FLAG_POLICY_ISSUE: "at least one rule failed",
              Action.ASK_FOR_INFO: "no rule failed, but information is missing",
              Action.SEND_FOR_REVIEW: "every rule passed or does not apply"}[action]
    steps.append(f"Decided: {action.value} ({reason}).")
    status = STATUS_FOR[action]
    assert status is not Status.APPROVED                    # the agent can never approve
    steps.append(f"Status: {status.value}." + (" Waiting for a person." if action is Action.SEND_FOR_REVIEW else ""))
    return Assessment(request, results, action, status, steps)


# --- finish: the human decision --------------------------------------------------------------
DECISIONS = {"approve": Status.APPROVED, "reject": Status.REJECTED, "return": Status.RETURNED}


class DecisionError(ValueError):
    """A reviewer action that the policy does not allow."""


def human_decision(current: Status, decision: str, reviewer: str, requester: str, reason: str = "") -> Status:
    """
    The `finish` action. Only a named person, who is not the requester, can decide, and
    only on a request that the agent sent for review. Reject and return need a reason,
    so the requester knows what to change.
    """
    if decision not in DECISIONS:
        raise DecisionError(f"Unknown decision '{decision}'. Use approve, reject or return.")
    if current is not Status.AWAITING_REVIEW:
        raise DecisionError(f"Only requests awaiting review can be decided (this one is {current.value}).")
    reviewer = " ".join((reviewer or "").split())
    if len(reviewer) < 2:
        raise DecisionError("Enter the reviewer's name.")
    if reviewer.casefold() == " ".join((requester or "").split()).casefold():
        raise DecisionError("The requester cannot decide on their own request.")
    if decision in ("reject", "return") and len((reason or "").strip()) < 5:
        raise DecisionError("Give a reason (at least 5 characters) so the requester knows what to change.")
    return DECISIONS[decision]
