"""
Measure how well the form is filled from pasted text: rules vs Gemini.

    python benchmark.py            rules only (and the LLM too if a key is set)

Two sets:
  data/sample_texts.csv    10 requests used while writing the rules (dev set)
  data/holdout_texts.csv    6 requests written BEFORE the rules were run on them, never used to
                            tune them. Only this set says how the extractor does on new wording.

A field counts as correct when it equals the expected value (numbers compared as
numbers, references as a set, names ignoring case). An expected empty field is
correct only if the extractor left it empty: filling it would be inventing.
"""
from __future__ import annotations

import csv
from pathlib import Path

from procurement.config import load_secrets
from procurement.extract import rules_extract
from procurement.llm import build_pipeline, extract
from procurement.models import parse_refs

DATA = Path(__file__).parent / "data"
SCORED = ["requester", "department", "item", "quantity", "unit_price_usd", "quotes", "supplier", "budget_line",
          "manager", "evidence"]


def same(field: str, got: str, expected: str) -> bool:
    got, expected = (got or "").strip(), (expected or "").strip()
    if not expected or not got:
        return got == expected
    if field in ("quantity", "unit_price_usd", "quotes"):
        try:
            return float(got.replace(",", "")) == float(expected.replace(",", ""))
        except ValueError:
            return False
    if field == "evidence":
        return set(parse_refs(got)) == set(parse_refs(expected))
    return got.casefold() == expected.casefold()


def score(rows: list[dict], extractor) -> tuple[dict[str, int], list[str]]:
    correct = {f: 0 for f in SCORED}
    misses = []
    for row in rows:
        fields = extractor(row["text"]).fields
        for f in SCORED:
            if same(f, fields.get(f, ""), row[f]):
                correct[f] += 1
            else:
                misses.append(f"{row['text_id']} {f}: got '{fields.get(f, '')}', expected '{row[f]}'")
    return correct, misses


def report(name: str, rows: list[dict], extractors: dict) -> None:
    print(f"\n## {name} ({len(rows)} requests)\n")
    results = {label: score(rows, fn) for label, fn in extractors.items()}
    print("| Field | " + " | ".join(results) + " |")
    print("|---|" + "---|" * len(results))
    for f in SCORED:
        print(f"| {f} | " + " | ".join(f"{c[f]}/{len(rows)}" for c, _ in results.values()) + " |")
    total = len(rows) * len(SCORED)
    print("| **all fields** | " + " | ".join(f"**{sum(c.values())}/{total}**" for c, _ in results.values()) + " |")
    for label, (_, misses) in results.items():
        if misses:
            print(f"\n{label} misses:")
            for m in misses:
                print("  -", m)


def main() -> None:
    extractors = {"rules": rules_extract}
    pipe = build_pipeline(load_secrets())
    if pipe:
        extractors["LLM"] = lambda text: extract(text, pipe)
    else:
        print("No API key: rules only. Run  python setup_key.py  to compare with Gemini.")
    for name, file in (("Dev set", "sample_texts.csv"), ("Held-out set", "holdout_texts.csv")):
        rows = list(csv.DictReader(open(DATA / file, encoding="utf-8")))
        report(name, rows, extractors)


if __name__ == "__main__":
    main()
