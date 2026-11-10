"""
Stage 6: turn a pasted request (an email, a WhatsApp message, Arabic or English)
into form fields. The requester then REVIEWS the form and submits it; nothing
is submitted by the extractor.

Two extractors, same output:
  rules_extract(text)   regular expressions + the catalogue's Arabic/English names. Always works.
  LLMExtractor          Gemini / Groq with a JSON schema (procurement/llm.py). Better at free wording.

Grounding: the model may read, not invent
-----------------------------------------
Whatever the LLM returns goes through ground() before it reaches the form:
  * a number (quantity, price, quotations) must appear in the text;
  * every DST/FB reference and the budget line code must appear in the text;
  * a person's or supplier's name must appear in the text as written;
  * an item must be a catalogue item named in the text (in English or Arabic), or appear as written.
A value that fails is dropped, and the form says so ("price not found in the text").
An empty field is safe: the agent will ask for it. An invented one is not.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .data import reference_data
from .models import DEPARTMENTS, parse_refs

FIELDS = ("requester", "department", "item", "quantity", "unit_price_usd", "supplier", "quotes", "budget_line",
          "manager", "evidence", "justification")

DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫", "01234567890123456789.")

ITEM_NAMES = {   # longest first inside each item, so "حقائب شتوية" wins over "شتوية"
    "Winter Kit": ["winter kits", "winter kit", "الحقائب الشتوية", "حقائب شتوية", "حقيبة شتوية", "شنط شتوية"],
    "Food Parcel": ["food parcels", "food parcel", "الطرود الغذائية", "طرود غذائية", "طرد غذائي",
                    "سلال غذائية", "سلة غذائية"],
    "Blanket": ["blankets", "blanket", "البطانيات", "بطانيات", "بطانية", "حرامات"],
    "Kitchen Set": ["kitchen sets", "kitchen set", "أطقم المطبخ", "أطقم مطبخ", "اطقم مطبخ", "طقم مطبخ"],
    "Hygiene Kit": ["hygiene kits", "hygiene kit", "حقائب النظافة", "حقائب نظافة", "حقيبة نظافة", "حقائب صحية"],
}
DEPARTMENT_NAMES = {"Programs": ["programs", "programmes", "البرامج"], "Logistics": ["logistics", "اللوجستيات"],
                    "MEAL": ["meal", "المتابعة والتقييم"]}
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "واحد": 1, "عرض واحد": 1, "عرضين": 2,
                "عرضان": 2, "ثلاثة": 3, "ثلاث": 3, "أربعة": 4, "اربعة": 4}
NUM = r"(\d[\d,]*(?:\.\d{1,2})?)"
AR_NAME = r"([ء-ي]{2,}(?:\s+[ء-ي]{2,})?)"
EN_NAME = r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)(?![\w-])"      # "Al-Noor" is not a name "Al"


def normalise(text: str) -> str:
    """Arabic-Indic digits -> 0-9, one space between words."""
    return re.sub(r"\s+", " ", (text or "").translate(DIGITS)).strip()


def number(value) -> str:
    return str(value).replace(",", "").strip()


@dataclass
class Extraction:
    fields: dict[str, str]                      # only the fields that were found
    source: str                                 # "rules" or the LLM backend that answered
    notes: list[str] = field(default_factory=list)

    @property
    def empty(self) -> list[str]:
        return [f for f in FIELDS if not self.fields.get(f)]


# --- rules ---------------------------------------------------------------------------------
def _item(t: str) -> tuple[str | None, str | None, int]:
    """(catalogue item, the words used, position) for the item mentioned first."""
    low, best = t.casefold(), (None, None, len(t) + 1)
    for item, names in ITEM_NAMES.items():
        for name in names:
            pos = low.find(name.casefold())
            if pos != -1 and pos < best[2]:
                best = (item, t[pos:pos + len(name)], pos)
    return best


def rules_extract(text: str) -> Extraction:
    t = normalise(text)
    low = t.casefold()
    ref = reference_data()
    found: dict[str, str] = {}

    item, words, _ = _item(t)
    if item:
        found["item"] = item
        m = re.search(NUM + r"\s*(?:x\s*)?" + re.escape(words), t, re.I)
        if m:
            found["quantity"] = number(m.group(1))
    if "quantity" not in found:
        m = re.search(r"(?:quantity|qty|الكمية|العدد)\s*[:=]?\s*" + NUM, t, re.I)
        if m:
            found["quantity"] = number(m.group(1))

    for pattern in (r"unit price\s*[:=]?\s*\$?\s*" + NUM,
                    r"سعر الوحدة\s*[:=]?\s*" + NUM,
                    r"\bat\s*\$?\s*" + NUM + r"\s*(?:usd|\$|dollars?)?\s*(?:each|per|/|a piece|apiece)",
                    NUM + r"\s*(?:usd|\$|dollars?)\s*(?:each|per|/|a piece|apiece)",
                    r"\$\s*" + NUM + r"\s*(?:each|per|/|a piece|apiece)",
                    r"بسعر\s*" + NUM,
                    NUM + r"\s*(?:دولار|\$)\s*(?:للواحدة|للقطعة|للوحدة|لكل|للحقيبة|للطرد|للبطانية)"):
        m = re.search(pattern, t, re.I)
        if m:
            found["unit_price_usd"] = number(m.group(1))
            break

    m = re.search(r"(\d+)\s*(?:written\s+)?(?:quotations?|quotes?|price offers?)", t, re.I) or \
        re.search(r"(\d+)\s*(?:عروض|عرض)(?:\s*(?:أسعار|اسعار|سعر))?", t)
    if m:
        found["quotes"] = m.group(1)
    else:
        for word, n in sorted(NUMBER_WORDS.items(), key=lambda kv: -len(kv[0])):
            if re.search(rf"\b{word}\s+(?:written\s+)?(?:quotations?|quotes?)\b", low) or \
                    (word.startswith("عرض") and word in t) or re.search(rf"{word}\s+عروض", t):
                found["quotes"] = str(n)
                break

    for s in ref.suppliers.values():
        if s.name.casefold() in low:
            found["supplier"] = s.name
            break

    m = re.search(r"\bBL-[A-Z]+-\d{2}\b", t, re.I)
    if m:
        found["budget_line"] = m.group(0).upper()

    refs = parse_refs(t)
    if refs:
        found["evidence"] = ", ".join(refs)

    m = re.search(r"(?i:approved by|approving manager\s*[:=]?|manager\s*[:=])\s*" + EN_NAME, t) or \
        re.search(r"(?:بموافقة(?: المدير)?|وافق عليه(?:ا)?(?: المدير)?|المدير الموافق\s*[:=]?)\s*" + AR_NAME, t)
    if m:
        found["manager"] = m.group(1)

    not_a_person = {n.casefold() for names in DEPARTMENT_NAMES.values() for n in names} | \
                   {s.name.casefold() for s in ref.suppliers.values()}
    for pattern in (r"(?i:requester\s*[:=]|my name is|this is|i am|i'm)\s*" + EN_NAME,  # (?i:) = only the prefix
                    r"(?:مقدم الطلب\s*[:=]?|اسمي|معك|أنا)\s*" + AR_NAME,
                    r"\bfrom\s+" + EN_NAME):                      # "from X" last: X is often a place or a team
        m = re.search(pattern, t)
        if m and m.group(1).casefold() not in not_a_person:
            found["requester"] = m.group(1)
            break

    for dept, names in DEPARTMENT_NAMES.items():
        if any(re.search(rf"(?<!\w){re.escape(n)}(?!\w)", low) for n in names):
            found["department"] = dept
            break

    found["justification"] = t[:1000]
    return Extraction(found, "rules")


# --- grounding: the LLM may read, not invent ------------------------------------------------
def _in_text(value: str, t: str) -> bool:
    return normalise(value).casefold() in t.casefold()


def _number_in_text(value: str, t: str) -> bool:
    """42 matches '42', '42.00' and '$42'; 1200 matches '1,200'."""
    try:
        target = float(number(value))
    except ValueError:
        return False
    return any(float(number(n)) == target for n in re.findall(r"(?<![\w-])\d[\d,]*(?:\.\d+)?", t))


def ground(fields: dict, text: str) -> tuple[dict[str, str], list[str]]:
    t = normalise(text)
    kept, notes = {}, []
    for f, value in fields.items():
        if f not in FIELDS or value in (None, ""):
            continue
        value = normalise(str(value))
        if f in ("quantity", "unit_price_usd", "quotes"):
            ok = _number_in_text(value, t)
        elif f == "evidence":
            refs = parse_refs(value)
            ok = bool(refs) and set(refs) <= set(parse_refs(t))
            value = ", ".join(refs)
        elif f == "item":
            named = ITEM_NAMES.get(value)
            ok = _in_text(value, t) or bool(named and any(_in_text(n, t) for n in named))
        elif f == "department":
            ok = value in DEPARTMENTS
        elif f == "justification":
            ok = True
        else:                                                     # requester, manager, supplier, budget line
            ok = _in_text(value, t)
        if ok:
            kept[f] = number(value) if f in ("quantity", "unit_price_usd", "quotes") else value
        else:
            notes.append(f"{f.replace('_usd', '').replace('_', ' ')} '{value}' was not found in the text, "
                         "so it was left empty.")
    return kept, notes
