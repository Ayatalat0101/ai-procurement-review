"""
Reference data the policy checks read. Nothing here decides anything; it only
loads facts, from three sources:

  Project 1   aid_distributions.csv   price benchmarks (catalogue.csv) + distribution evidence
  Project 2   feedback_log.csv        feedback evidence (exported without personal data)
  This desk   catalogue.csv, budget_lines.csv, suppliers.csv

Why a separate module?
----------------------
The checks (stage 2) must be testable with small, hand-made data. They take a
ReferenceData object, so a test can pass its own budget of exactly 1,680 USD
without touching the real files.

Money is Decimal, never float: 42 * 1.10 in float is 46.2000000000000003, and
a price of exactly 46.20 would wrongly fail the 110% rule. Policy boundaries
must be exact.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).parent.parent / "data"


@dataclass(frozen=True)
class CatalogueItem:
    item: str
    unit: str
    budget_category: str
    benchmark: Decimal                     # USD per unit, from Project 1's data


@dataclass(frozen=True)
class BudgetLine:
    code: str
    department: str
    budget_category: str
    description: str
    annual: Decimal
    committed: Decimal

    @property
    def remaining(self) -> Decimal:
        return self.annual - self.committed


@dataclass(frozen=True)
class Supplier:
    name: str
    registered: bool
    conflict_of_interest: bool
    note: str


@dataclass(frozen=True)
class Distribution:                        # a row of Project 1's aid_distributions.csv
    distribution_id: str
    date: str
    governorate: str
    partner: str
    item: str
    quantity: int
    status: str


@dataclass(frozen=True)
class FeedbackRecord:                      # a row of Project 2's feedback_log.csv
    reference: str
    item: str | None
    category: str
    distribution_id: str | None


@dataclass
class ReferenceData:
    catalogue: dict[str, CatalogueItem] = field(default_factory=dict)       # key: item name, lower case
    budget_lines: dict[str, BudgetLine] = field(default_factory=dict)       # key: code, upper case
    suppliers: dict[str, Supplier] = field(default_factory=dict)            # key: name, lower case
    distributions: dict[str, Distribution] = field(default_factory=dict)    # key: DST-xxx
    feedback: dict[str, FeedbackRecord] = field(default_factory=dict)       # key: FB-xxxx

    def catalogue_item(self, name: str) -> CatalogueItem | None:
        return self.catalogue.get((name or "").strip().lower())

    def supplier(self, name: str) -> Supplier | None:
        return self.suppliers.get((name or "").strip().lower())

    def budget_line(self, code: str) -> BudgetLine | None:
        return self.budget_lines.get((code or "").strip().upper())


def _rows(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:          # -sig: the Project 2 export starts with a BOM
        return list(csv.DictReader(f))


def _yes(value: str) -> bool:
    return (value or "").strip().lower() in {"yes", "true", "1"}


def load(folder: Path | str = DATA) -> ReferenceData:
    folder = Path(folder)
    ref = ReferenceData()
    for r in _rows(folder / "catalogue.csv"):
        ref.catalogue[r["item"].lower()] = CatalogueItem(r["item"], r["unit"], r["budget_category"],
                                                         Decimal(r["benchmark_unit_cost_usd"]))
    for r in _rows(folder / "budget_lines.csv"):
        ref.budget_lines[r["budget_line"].upper()] = BudgetLine(
            r["budget_line"], r["department"], r["budget_category"], r["description"],
            Decimal(r["annual_budget_usd"]), Decimal(r["committed_usd"]))
    for r in _rows(folder / "suppliers.csv"):
        ref.suppliers[r["supplier"].lower()] = Supplier(r["supplier"], _yes(r["registered"]),
                                                         _yes(r["conflict_of_interest"]), r["note"])
    for r in _rows(folder / "aid_distributions.csv"):
        ref.distributions[r["distribution_id"]] = Distribution(
            r["distribution_id"], r["date"], r["governorate"], r["partner"], r["item"],
            int(float(r["quantity"])), r["status"])
    feedback_file = folder / "feedback_log.csv"
    if feedback_file.exists():                                    # optional: Project 2 may not be exported yet
        for r in _rows(feedback_file):
            if r["reference"]:                                    # sensitive rows have no reference
                ref.feedback[r["reference"]] = FeedbackRecord(r["reference"], r["item"] or None,
                                                              r["category"], r["distribution_id"] or None)
    return ref


@lru_cache(maxsize=2)
def reference_data(folder: str = str(DATA)) -> ReferenceData:
    """Loaded once per process (the files do not change while the app runs)."""
    return load(folder)
