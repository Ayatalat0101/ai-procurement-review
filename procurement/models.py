"""
The purchase request: what a valid form looks like.

Two kinds of fields (PROJECT_PLAN.md §4):
  * required to submit  requester, department, item, quantity, unit price, justification.
                        Missing or invalid -> a form error, and NOTHING is saved.
  * details             supplier, quotations, budget line, manager, evidence.
                        They may be empty at submission: the agent then ASKS for
                        exactly the empty ones (ask_for_info). That is the
                        "missing information vs policy failure" distinction.

Money is Decimal with two decimals. Quantity is a whole number: 2.5 winter
kits is a typo, not a request.
"""
from __future__ import annotations

import re
from decimal import Decimal
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

REF_RE = re.compile(r"\b(DST)[-\s]?(\d{3})\b|\b(FB)[-\s]?(\d{4})\b", re.I)
ARABIC_LETTER = re.compile(r"[؀-ۿ]")
DEPARTMENTS = ("Programs", "Logistics", "MEAL")
MAX_UNIT_PRICE = Decimal("10000")
DETAIL_FIELDS = ("supplier", "quotes", "budget_line", "manager", "evidence")


class Status(str, Enum):
    DRAFT = "draft"                          # saved, not checked yet
    NEEDS_INFO = "needs_info"                # ask_for_info
    POLICY_ISSUE = "policy_issue"            # flag_policy_issue
    AWAITING_REVIEW = "awaiting_review"      # send_for_review: a PERSON decides
    APPROVED = "approved"                    # only ever set by a person
    REJECTED = "rejected"
    RETURNED = "returned"                    # the reviewer sent it back for changes


class Action(str, Enum):                     # the agent's four allowed actions (challenge brief)
    ASK_FOR_INFO = "ask_for_info"
    FLAG_POLICY_ISSUE = "flag_policy_issue"
    SEND_FOR_REVIEW = "send_for_review"
    FINISH = "finish"


def parse_refs(text: str | None) -> list[str]:
    """'dst 001, fb-0005' -> ['DST-001', 'FB-0005'] (order kept, no duplicates)."""
    refs = []
    for m in REF_RE.finditer(text or ""):
        ref = f"DST-{m.group(2)}" if m.group(1) else f"FB-{m.group(4)}"
        if ref not in refs:
            refs.append(ref)
    return refs


def detect_language(text: str) -> str:
    letters = [c for c in re.sub(r"\b(DST|FB)-?\d+\b", " ", text or "") if c.isalpha()]
    if not letters:
        return "en"
    return "ar" if sum(bool(ARABIC_LETTER.match(c)) for c in letters) / len(letters) >= 0.5 else "en"


class Request(BaseModel):
    # required to submit
    requester: str = Field(min_length=2, max_length=60)
    department: Literal["Programs", "Logistics", "MEAL"]
    item: str = Field(min_length=2, max_length=60)
    quantity: int = Field(ge=1, le=1_000_000)
    unit_price_usd: Decimal = Field(gt=0, le=MAX_UNIT_PRICE, decimal_places=2)
    justification: str = Field(min_length=10, max_length=1000)
    # details: may be empty; the agent asks for them
    supplier: str = ""
    quotes: Optional[int] = Field(default=None, ge=0, le=20)
    budget_line: str = ""
    manager: str = ""
    evidence: str = ""

    @field_validator("requester", "item", "justification", "supplier", "budget_line", "manager", "evidence",
                     mode="before")
    @classmethod
    def _strip(cls, v):
        return re.sub(r"\s+", " ", str(v or "")).strip()

    @field_validator("budget_line")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @field_validator("quotes", mode="before")
    @classmethod
    def _empty_quotes(cls, v):
        return None if v in ("", None) else v

    @field_validator("quantity", mode="before")
    @classmethod
    def _whole_number(cls, v):
        if isinstance(v, str):
            v = v.strip().replace(",", "")
        if isinstance(v, float) and not v.is_integer():
            raise ValueError("Quantity must be a whole number.")
        return v

    @property
    def total(self) -> Decimal:
        return (self.unit_price_usd * self.quantity).quantize(Decimal("0.01"))

    @property
    def refs(self) -> list[str]:
        return parse_refs(self.evidence)

    @property
    def language(self) -> str:
        return detect_language(self.justification)

    def missing_details(self) -> list[str]:
        """Detail fields left empty (a wrong value is judged by the checks, not here)."""
        empty = {"supplier": not self.supplier, "quotes": self.quotes is None,
                 "budget_line": not self.budget_line, "manager": not self.manager, "evidence": not self.evidence}
        return [f for f in DETAIL_FIELDS if empty[f]]


FRIENDLY = {   # messages a requester understands, per field
    "requester": "Enter your name (2 to 60 characters).",
    "department": f"Choose a department: {', '.join(DEPARTMENTS)}.",
    "item": "Enter the item (2 to 60 characters).",
    "quantity": "Quantity must be a whole number of at least 1.",
    "unit_price_usd": "Unit price must be a number above 0 and at most 10,000 USD, with up to 2 decimals.",
    "justification": "Explain why this purchase is needed (10 to 1,000 characters).",
    "quotes": "Number of quotations must be a whole number from 0 to 20.",
}


def validate_form(**fields) -> tuple[Optional[Request], dict[str, str]]:
    """Form helper: (request, {}) or (None, {field: message}). Nothing is saved on error."""
    try:
        return Request(**fields), {}
    except ValidationError as err:
        errors: dict[str, str] = {}
        for e in err.errors():
            name = str(e["loc"][0]) if e["loc"] else "form"
            errors.setdefault(name, FRIENDLY.get(name, e["msg"].removeprefix("Value error, ")))
        return None, errors
