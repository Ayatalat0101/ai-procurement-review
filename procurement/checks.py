"""
The written policy (PROJECT_PLAN.md §7) as code: one function per rule.

Every check returns ONE result:
  pass     the rule is met                        (shown with the numbers that prove it)
  fail     a real policy problem                  -> the agent flags it; more info will not fix it
  missing  the information to judge it is absent  -> the agent asks for exactly that field
  n/a      the rule does not apply (e.g. the price rule for an item outside the catalogue)

Why one function per rule, and why a result for EVERY rule?
-----------------------------------------------------------
* The reviewer sees all 8 lines, not just "rejected". A passed check with its
  numbers ("42.00 <= 46.20") is evidence too.
* Each rule can be tested alone, at its exact boundary.
* A rule can change (110% -> 115%) in one place, with a test that shows the effect.
No AI is involved here: a financial decision must be reproducible and auditable.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .data import ReferenceData
from .models import Request

PRICE_TOLERANCE = Decimal("1.10")          # C2: up to 110% of the benchmark
QUANTITY_TOLERANCE = Decimal("1.10")       # C7: up to 110% of the referenced distribution
ONE_QUOTE_BELOW = Decimal("1000")          # C3: total < 1,000 -> 1 quotation
TENDER_ABOVE = Decimal("10000")            # C3: total > 10,000 -> tender, not this process
CENT = Decimal("0.01")


class Outcome(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    MISSING = "missing"
    NA = "n/a"


@dataclass(frozen=True)
class CheckResult:
    code: str                  # C1 ... C8
    name: str
    outcome: Outcome
    message: str               # what was found, with the numbers
    fix: str = ""              # what the requester can do (fail / missing)
    field: str | None = None   # the form field to ask for when missing


def money(x: Decimal) -> str:
    return f"{x.quantize(CENT):,.2f}"


# --- C1 ------------------------------------------------------------------------------------
def c1_catalogue(req: Request, ref: ReferenceData) -> CheckResult:
    item = ref.catalogue_item(req.item)
    if item:
        return CheckResult("C1", "Catalogue", Outcome.PASS, f"{item.item} is in the relief catalogue.")
    names = ", ".join(i.item for i in ref.catalogue.values())
    return CheckResult("C1", "Catalogue", Outcome.FAIL, f"'{req.item}' is not in the relief catalogue.",
                       f"Only catalogue items go through this process ({names}). "
                       "Other purchases need the procurement unit's separate approval.")


# --- C2 ------------------------------------------------------------------------------------
def c2_price(req: Request, ref: ReferenceData) -> CheckResult:
    item = ref.catalogue_item(req.item)
    if not item:
        return CheckResult("C2", "Unit price", Outcome.NA, "No benchmark: the item is not in the catalogue.")
    limit = (item.benchmark * PRICE_TOLERANCE).quantize(CENT)
    share = (req.unit_price_usd / item.benchmark * 100).quantize(Decimal("1"))
    facts = (f"{money(req.unit_price_usd)} USD per {item.unit} is {share}% of the {money(item.benchmark)} USD "
             f"benchmark (average cost in Project 1's distributions); the limit is {money(limit)} USD (110%).")
    if req.unit_price_usd <= limit:
        return CheckResult("C2", "Unit price", Outcome.PASS, facts)
    return CheckResult("C2", "Unit price", Outcome.FAIL, facts,
                       f"Negotiate the price down to {money(limit)} USD or less, or explain the market change "
                       "to the reviewer with written quotations.")


# --- C3 ------------------------------------------------------------------------------------
def required_quotes(total: Decimal) -> int | None:
    """1 below 1,000 · 3 from 1,000 to 10,000 (both included) · None above 10,000 (tender)."""
    if total > TENDER_ABOVE:
        return None
    return 1 if total < ONE_QUOTE_BELOW else 3


def c3_quotations(req: Request, ref: ReferenceData) -> CheckResult:
    need = required_quotes(req.total)
    if need is None:                               # more quotations will not fix this: it is a failure
        return CheckResult("C3", "Quotations", Outcome.FAIL,
                           f"The total of {money(req.total)} USD is above {money(TENDER_ABOVE)} USD: "
                           "it needs a tender by the procurement committee.",
                           "Split the need only if the distributions are really separate, or submit it to the "
                           "procurement committee as a tender.")
    if req.quotes is None:
        return CheckResult("C3", "Quotations", Outcome.MISSING,
                           f"A total of {money(req.total)} USD needs at least {need} quotation(s).",
                           f"Enter how many written quotations you have (at least {need}).", "quotes")
    facts = f"{req.quotes} quotation(s); a total of {money(req.total)} USD needs at least {need}."
    if req.quotes >= need:
        return CheckResult("C3", "Quotations", Outcome.PASS, facts)
    return CheckResult("C3", "Quotations", Outcome.FAIL, facts,
                       f"Collect {need - req.quotes} more written quotation(s) from pre-qualified suppliers.")


# --- C4 ------------------------------------------------------------------------------------
def c4_budget(req: Request, ref: ReferenceData) -> CheckResult:
    if not req.budget_line:
        return CheckResult("C4", "Budget", Outcome.MISSING, "No budget line given.",
                           "Choose the budget line that will pay for this purchase.", "budget_line")
    line = ref.budget_line(req.budget_line)
    if not line:
        codes = ", ".join(ref.budget_lines)
        return CheckResult("C4", "Budget", Outcome.MISSING, f"'{req.budget_line}' is not a known budget line.",
                           f"Use one of: {codes}.", "budget_line")
    problems = []
    item = ref.catalogue_item(req.item)
    if item and item.budget_category != line.budget_category:
        problems.append(f"{line.code} ({line.description}) cannot pay for {item.item} "
                        f"({item.budget_category} items).")
    if req.total > line.remaining:
        problems.append(f"{money(req.total)} USD is above the {money(line.remaining)} USD left on {line.code}.")
    if problems:
        return CheckResult("C4", "Budget", Outcome.FAIL, " ".join(problems),
                           "Use a budget line for this item with enough funds left, or reduce the quantity.")
    return CheckResult("C4", "Budget", Outcome.PASS,
                       f"{money(req.total)} USD fits in the {money(line.remaining)} USD left on {line.code}.")


# --- C5 ------------------------------------------------------------------------------------
def _same_person(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


def c5_approval(req: Request, ref: ReferenceData) -> CheckResult:
    if not req.manager:
        return CheckResult("C5", "Manager approval", Outcome.MISSING, "No approving manager given.",
                           "Enter the name of the manager who approved this request.", "manager")
    if _same_person(req.manager, req.requester):
        return CheckResult("C5", "Manager approval", Outcome.FAIL,
                           f"{req.requester} is both the requester and the approving manager.",
                           "Another manager must approve: nobody approves their own purchase.")
    return CheckResult("C5", "Manager approval", Outcome.PASS,
                       f"Approved by {req.manager}, who is not the requester.")


# --- C6 ------------------------------------------------------------------------------------
def c6_evidence(req: Request, ref: ReferenceData) -> CheckResult:
    if not req.evidence:
        return CheckResult("C6", "Evidence", Outcome.MISSING, "No evidence given.",
                           "Add the distribution (DST-xxx) or the feedback (FB-xxxx) that shows the need.", "evidence")
    refs = req.refs
    if not refs:
        return CheckResult("C6", "Evidence", Outcome.MISSING,
                           f"'{req.evidence}' contains no DST-xxx or FB-xxxx reference.",
                           "Add a distribution (DST-xxx) or feedback (FB-xxxx) reference.", "evidence")
    unknown = [r for r in refs if r not in ref.distributions and r not in ref.feedback]
    if unknown:
        return CheckResult("C6", "Evidence", Outcome.MISSING,
                           f"{', '.join(unknown)} was not found in Project 1's distributions or Project 2's "
                           "feedback log.", "Check the reference number.", "evidence")
    item = ref.catalogue_item(req.item)
    facts, mismatched = [], []
    for r in refs:
        if r.startswith("DST"):
            d = ref.distributions[r]
            facts.append(f"{r}: {d.item}, {d.governorate}, {d.partner}, {d.date}, {d.status}")
            about = d.item
        else:
            f = ref.feedback[r]
            facts.append(f"{r}: {f.category} feedback about {f.item or 'no specific item'}")
            about = f.item
        if item and about != item.item:
            mismatched.append(f"{r} is about {about or 'no specific item'}")
    if not item:
        return CheckResult("C6", "Evidence", Outcome.NA, "; ".join(facts) + ". (Item not in the catalogue.)")
    if mismatched:
        return CheckResult("C6", "Evidence", Outcome.FAIL,
                           f"The request is for {item.item}, but " + "; ".join(mismatched) + ".",
                           f"Give evidence about {item.item}, or correct the item.")
    return CheckResult("C6", "Evidence", Outcome.PASS, "; ".join(facts) + ".")


# --- C7 ------------------------------------------------------------------------------------
def c7_quantity(req: Request, ref: ReferenceData) -> CheckResult:
    dsts = [r for r in req.refs if r in ref.distributions]
    if not dsts:
        return CheckResult("C7", "Quantity", Outcome.NA, "No distribution referenced: nothing to compare with.")
    planned = sum(ref.distributions[r].quantity for r in dsts)
    limit = int(Decimal(planned) * QUANTITY_TOLERANCE)             # whole units, rounded down
    facts = (f"{req.quantity} requested; {', '.join(dsts)} planned {planned}, "
             f"so the limit is {limit} (110%).")
    if req.quantity <= limit:
        return CheckResult("C7", "Quantity", Outcome.PASS, facts)
    return CheckResult("C7", "Quantity", Outcome.FAIL, facts,
                       f"Reduce the quantity to {limit} or less, or add evidence for the extra need.")


# --- C8 ------------------------------------------------------------------------------------
def c8_supplier(req: Request, ref: ReferenceData) -> CheckResult:
    if not req.supplier:
        return CheckResult("C8", "Supplier", Outcome.MISSING, "No supplier given.",
                           "Enter the supplier you intend to buy from.", "supplier")
    s = ref.supplier(req.supplier)
    if not s or not s.registered:
        return CheckResult("C8", "Supplier", Outcome.FAIL, f"'{req.supplier}' is not a pre-qualified supplier.",
                           "Choose a pre-qualified supplier, or ask procurement to pre-qualify this one first.")
    if s.conflict_of_interest:
        return CheckResult("C8", "Supplier", Outcome.FAIL,
                           f"{s.name} has a declared conflict of interest: {s.note}.",
                           "Choose another supplier. The declared conflict is shown to the reviewer.")
    return CheckResult("C8", "Supplier", Outcome.PASS, f"{s.name} is pre-qualified with no declared conflict.")


CHECKS = [c1_catalogue, c2_price, c3_quotations, c4_budget, c5_approval, c6_evidence, c7_quantity, c8_supplier]


def run_checks(req: Request, ref: ReferenceData) -> list[CheckResult]:
    """All 8 rules, always in the same order, always one result each."""
    return [check(req, ref) for check in CHECKS]


def failed(results: list[CheckResult]) -> list[CheckResult]:
    return [r for r in results if r.outcome is Outcome.FAIL]


def missing(results: list[CheckResult]) -> list[CheckResult]:
    return [r for r in results if r.outcome is Outcome.MISSING]
