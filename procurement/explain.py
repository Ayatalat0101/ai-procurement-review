"""
Stage 6: explain the result to the requester in the language of their justification.

  template_explanation(a)   fixed sentences per rule, in Arabic or English. Always works.
  LLMExplainer               Gemini / Groq words it naturally (procurement/llm.py).

The explanation only EXPLAINS. The checks and the action are already decided
(stages 2-3) and the LLM receives them as facts. check_explanation() rejects
an LLM explanation that:
  * uses a number that is not in the check results (an invented price, limit or budget);
  * does not name every rule that failed or is missing (C2, C4 ...);
  * says or implies the request is approved (only a person approves);
  * is in the wrong language.
A rejected explanation is replaced by the template, and the page says why.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from .agent import CHECK_NAME_AR, FIELD_LABEL, FIELD_LABEL_AR, Assessment
from .checks import Outcome, money
from .models import Action, detect_language

FIX_AR = {   # (fail, missing) guidance per rule, for the Arabic template
    "C1": ("الصنف غير موجود في كتالوج الإغاثة، وهذه العملية للأصناف المعتمدة فقط.", ""),
    "C2": ("سعر الوحدة أعلى من 110% من السعر المرجعي. فاوض على السعر أو أرفق عروض أسعار مكتوبة تشرح تغيّر السوق.",
           ""),
    "C3": ("عدد عروض الأسعار أقل من المطلوب لهذا المبلغ، أو المبلغ أكبر من 10,000 دولار ويحتاج مناقصة.",
           "اكتب عدد عروض الأسعار المكتوبة التي لديك."),
    "C4": ("بند الميزانية لا يغطي هذا الصنف أو المبلغ المتبقي فيه لا يكفي.", "اختر بند الميزانية الصحيح."),
    "C5": ("لا يوافق مقدّم الطلب على طلبه بنفسه: يجب أن يوافق مدير آخر.", "اكتب اسم المدير الذي وافق على الطلب."),
    "C6": ("الدليل المذكور يخص صنفاً آخر.", "أضف رقم توزيعة (DST-xxx) أو ملاحظة (FB-xxxx) موجودة في السجلات."),
    "C7": ("الكمية أكثر من 110% من كمية التوزيعة المذكورة.", ""),
    "C8": ("المورّد غير مؤهل مسبقاً أو لديه تضارب مصالح مُعلن. اختر مورّداً آخر.", "اكتب اسم المورّد."),
}
APPROVAL_CLAIMS = re.compile(
    r"(request|it|purchase) (is|has been|was) (now )?approved|\bapproved for purchase\b|\byou can (now )?buy\b|"
    r"\bgo ahead and (buy|order|purchase)\b|تمت? الموافقة على (طلبك|الطلب|الشراء)|تم اعتماد|طلبك (مقبول|معتمد)|"
    r"يمكنك (الشراء|البدء بالشراء)", re.I)
NUMBER = re.compile(r"(?<![A-Za-z\d.,-])\d[\d,]*(?:\.\d+)?")
DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def template_explanation(a: Assessment) -> str:
    lang = a.request.language
    problems = a.failed + a.missing
    if lang == "ar":
        lines = [a.headline_ar]
        for c in problems:
            fail_text, missing_text = FIX_AR[c.code]
            text = fail_text if c.outcome is Outcome.FAIL else (missing_text or FIELD_LABEL_AR.get(c.field, ""))
            lines.append(f"• {CHECK_NAME_AR[c.code]} ({c.code}): {text}")
        if a.action is Action.SEND_FOR_REVIEW:
            lines.append("سيراجع شخص مسؤول الطلب ويقرر الموافقة أو الرفض أو الإرجاع مع السبب.")
        else:
            lines.append("صحّح الطلب من صفحة «My requests» وأعد إرساله؛ ستبقى البيانات التي كتبتها.")
        return "\n".join(lines)
    lines = [a.headline]
    for c in problems:
        lines.append(f"• {c.name} ({c.code}): {c.fix}" if c.outcome is Outcome.FAIL
                     else f"• {FIELD_LABEL.get(c.field, c.name)} ({c.code}): {c.fix}")
    if a.action is Action.SEND_FOR_REVIEW:
        lines.append("A reviewer will approve, reject or return it with a reason. This is not an approval.")
    else:
        lines.append("Fix the request under My requests and resubmit: what you typed is kept.")
    return "\n".join(lines)


def facts_for_llm(a: Assessment) -> str:
    """Everything the LLM may use, and nothing else."""
    r = a.request
    lines = [f"REQUEST: {r.item}, {r.quantity} x {money(r.unit_price_usd)} USD = {money(r.total)} USD",
             f"ACTION ALREADY DECIDED BY THE RULES: {a.action.value}"]
    for c in a.results:
        lines.append(f"{c.code} {c.name} | {c.outcome.value} | {c.message}" + (f" | fix: {c.fix}" if c.fix else ""))
    return "\n".join(lines)


def _numbers(text: str) -> set[str]:
    """Every number, written one way: '8,400.00' -> '8400', '46.20' -> '46.2'."""
    out = set()
    for n in NUMBER.findall(text.translate(DIGITS)):
        n = n.replace(",", "").rstrip(".")
        try:
            out.add(format(Decimal(n).normalize(), "f"))
        except InvalidOperation:
            continue
    return out


def check_explanation(text: str, a: Assessment) -> list[str]:
    """Problems with an LLM explanation. Empty list = it can be shown."""
    problems = []
    allowed = _numbers(facts_for_llm(a)) | {str(i) for i in range(1, 9)} | {"110"}
    invented = sorted(_numbers(text) - allowed)
    if invented:
        problems.append(f"numbers not in the check results: {', '.join(invented)}")
    for c in a.failed + a.missing:
        if c.code not in text:
            problems.append(f"does not name {c.code}")
    if APPROVAL_CLAIMS.search(text):
        problems.append("claims or implies approval")
    if detect_language(text) != a.request.language:
        problems.append("wrong language")
    return problems
