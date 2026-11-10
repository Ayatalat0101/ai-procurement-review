"""
Shared UI helpers for the three pages. Presentation only: every rule, decision and
permission lives in procurement/, where it is tested without a browser.
"""
from __future__ import annotations

import csv
import os
import html
from pathlib import Path

import streamlit as st

from procurement.agent import RECOMMENDATION_NOTE, Assessment
from procurement.checks import CheckResult, Outcome, money
from procurement.data import reference_data
from procurement.models import DEPARTMENTS, Status, validate_form
from procurement.store import Record, Store

ROOT = Path(__file__).parent
DB_PATH = Path(os.environ.get("PROCUREMENT_DB", ROOT / "procurement.db"))
SAMPLES = ROOT / "data" / "sample_requests.csv"
FORM_FIELDS = ["requester", "department", "item", "quantity", "unit_price_usd", "justification",
               "supplier", "quotes", "budget_line", "manager", "evidence"]

STATUS_LABEL = {
    "draft": "📝 Draft", "needs_info": "⏳ Needs information", "policy_issue": "⚠️ Policy issue",
    "awaiting_review": "👀 Awaiting human review", "approved": "✅ Approved", "rejected": "⛔ Rejected",
    "returned": "↩️ Returned for changes",
}
OUTCOME_ICON = {Outcome.PASS: "✅", Outcome.FAIL: "❌", Outcome.MISSING: "⚠️", Outcome.NA: "➖"}
ACTION_LABEL = {"ask_for_info": "Ask for information", "flag_policy_issue": "Flag policy issue",
                "send_for_review": "Send for human review"}

CSS = """
<style>
textarea, input, .msg { unicode-bidi: plaintext; }
[data-testid="stAlert"] p, [data-testid="stCaptionContainer"] p, [data-testid="stMarkdownContainer"] p,
[data-testid="stMarkdownContainer"] li { unicode-bidi: plaintext; text-align: start; }
.msg { white-space: pre-wrap; padding: .6rem .85rem; border-radius: .5rem; text-align: start;
       border: 1px solid rgba(128,128,128,.35); margin: .25rem 0 .75rem; }
.checks { width: 100%; border-collapse: collapse; margin: .25rem 0 .75rem; font-size: .92rem; }
.checks td { padding: .45rem .5rem; border-top: 1px solid rgba(128,128,128,.25); vertical-align: top; }
.checks td.icon { width: 1.8rem; text-align: center; }
.checks td.code { width: 9.5rem; white-space: nowrap; font-weight: 600; }
.checks .fix { display: block; margin-top: .2rem; opacity: .85; font-style: italic; }
.checks tr.fail td { background: rgba(255, 75, 75, .08); }
.checks tr.missing td { background: rgba(255, 170, 0, .09); }
.policy { font-size: .86rem; line-height: 1.55; }
.policy td { padding: .2rem .35rem; vertical-align: top; border-top: 1px solid rgba(128,128,128,.2); }
</style>
"""

POLICY = [   # the written policy, shown next to the form so the requester knows the rules up front
    ("C1", "Catalogue", "Item is in the relief catalogue"),
    ("C2", "Unit price", "≤ 110% of the benchmark (Project 1 average)"),
    ("C3", "Quotations", "< 1,000 USD: 1 · 1,000–10,000: 3 · above 10,000: tender"),
    ("C4", "Budget", "Line pays for this item and has enough left"),
    ("C5", "Manager", "Approved by a manager who is not the requester"),
    ("C6", "Evidence", "DST-xxx (Project 1) or FB-xxxx (Project 2) about the same item"),
    ("C7", "Quantity", "≤ 110% of the referenced distribution"),
    ("C8", "Supplier", "Pre-qualified, no declared conflict of interest"),
]


@st.cache_resource
def get_store() -> Store:
    """One SQLite connection per server process."""
    return Store(DB_PATH)


# --- AI (optional): extraction and explanation --------------------------------------------------
MAX_AI_CALLS_PER_SESSION = 30          # protects the free API quota on the public demo


@st.cache_resource
def _llm_pipeline() -> dict:
    """Built once per server from .streamlit/secrets.toml, env vars, or the Cloud secrets box.
    No key -> {} -> rules extractor + template explanation."""
    from procurement.config import load_secrets
    from procurement.llm import build_pipeline
    try:
        secrets = dict(st.secrets)
    except Exception:                      # no secrets file at all
        secrets = {}
    return build_pipeline({**load_secrets(), **secrets})


def ai_enabled() -> bool:
    return bool(_llm_pipeline())


def pipeline() -> dict:
    """The LLM pipeline while this browser session is under its limit, else {} (rules)."""
    pipe = _llm_pipeline()
    used = st.session_state.get("ai_used", 0)
    if not pipe or used >= MAX_AI_CALLS_PER_SESSION:
        return {}
    st.session_state.ai_used = used + 1
    return pipe


def ai_status() -> str:
    if not ai_enabled():
        return "AI: off (no API key). Rules fill the form and a template explains the result."
    left = MAX_AI_CALLS_PER_SESSION - st.session_state.get("ai_used", 0)
    return (f"AI: Gemini fills the form and explains the result ({max(left, 0)} AI calls left in this session). "
            "It never decides a check or the action.")


def explanation_box(a: Assessment, key: str) -> None:
    """The explanation in the language of the justification, and who wrote it.
    Kept per request version, so a page refresh does not call the AI again."""
    from procurement.llm import explain
    cache = st.session_state.setdefault("explanations", {})
    key = f"{key}-{hash(a.request.model_dump_json())}"      # PR numbers restart after a demo reset
    if key not in cache:
        cache[key] = explain(a, pipeline())
    text, used = cache[key]
    st.markdown(f'<div class="msg" dir="auto">{esc(text)}</div>', unsafe_allow_html=True)
    st.caption(f"Explained by: {used}. The checks and the action above come from the rules, not the AI.")


def sample_rows(valid_only: bool = False) -> list[dict]:
    rows = list(csv.DictReader(open(SAMPLES, encoding="utf-8")))
    return [r for r in rows if r["expected_action"] != "reject_form"] if valid_only else rows


def esc(text) -> str:
    return html.escape(str(text or ""))


def show_text(text: str) -> None:
    """Text typed by a requester: escaped (never rendered as HTML) and direction-aware."""
    st.markdown(f'<div class="msg" dir="auto">{esc(text)}</div>', unsafe_allow_html=True)


def policy_panel() -> None:
    rows = "".join(f"<tr><td><b>{c}</b></td><td><b>{n}</b><br>{esc(r)}</td></tr>" for c, n, r in POLICY)
    st.markdown(f'<table class="policy">{rows}</table>', unsafe_allow_html=True)
    st.caption("Written policy v1 (fictional). The agent checks every rule and never approves: a person decides.")


def check_table(results: list[CheckResult]) -> None:
    rows = []
    for c in results:
        fix = f'<span class="fix">→ {esc(c.fix)}</span>' if c.fix else ""
        rows.append(f'<tr class="{c.outcome.value.replace("/", "")}"><td class="icon">{OUTCOME_ICON[c.outcome]}</td>'
                    f'<td class="code">{c.code} {esc(c.name)}</td><td dir="auto">{esc(c.message)}{fix}</td></tr>')
    st.markdown(f'<table class="checks">{"".join(rows)}</table>', unsafe_allow_html=True)


def outcome_banner(a: Assessment) -> None:
    """The next action, in the language of the justification (the full explanation comes in stage 6)."""
    text = a.headline_ar if a.request.language == "ar" else a.headline
    if a.action.value == "send_for_review":
        st.success(f"**{text}**  \n{RECOMMENDATION_NOTE}")
    elif a.action.value == "ask_for_info":
        st.warning(f"**{text}**")
    else:
        st.error(f"**{text}**")


def request_line(r) -> str:
    return (f"**{esc(r.item)}** · {r.quantity:,} × {money(r.unit_price_usd)} USD = **{money(r.total)} USD** · "
            f"{esc(r.supplier) or '_no supplier_'} · {esc(r.budget_line) or '_no budget line_'} · "
            f"{esc(r.evidence) or '_no evidence_'}")


def assessment_of(record: Record) -> Assessment:
    v = record.latest
    return Assessment(v.request, v.results, v.action, record.status)


def history_view(store: Store, ref: str) -> None:
    for e in store.history(ref):
        when = e["at"].replace("T", " ")[:16]
        who = "🤖 agent" if e["actor"] == "agent" else f"👤 {esc(e['actor'])}"
        label = ACTION_LABEL.get(e["event"], STATUS_LABEL.get(e["event"], e["event"]))
        st.markdown(f"`{when}` {who} · **{esc(label)}**" + (f"  \n{esc(e['detail'])}" if e["detail"] else ""))


def versions_view(store: Store, ref: str) -> None:
    versions = store.versions(ref)
    for v in reversed(versions):
        with st.expander(f"v{v.version} · {v.at.replace('T', ' ')[:16]} · {ACTION_LABEL[v.action.value]}",
                         expanded=False):
            st.markdown(request_line(v.request))
            check_table(v.results)


# --- the request form (new request and resubmission share it) ------------------------------
def _key(prefix: str, field: str) -> str:
    return f"{prefix}_{field}"


def fill_form(prefix: str, values: dict) -> None:
    """Put values into the form's widgets (a sample, or the latest version for a resubmission)."""
    for f in FORM_FIELDS:
        v = values.get(f, "")
        st.session_state[_key(prefix, f)] = "" if v is None else str(v)


def request_form(prefix: str, submit_label: str, lock_requester: bool = False) -> tuple[bool, dict]:
    """Returns (submitted, raw fields). Validation happens in procurement.models, not here."""
    ref = reference_data()
    items = [i.item for i in ref.catalogue.values()]
    lines = [""] + list(ref.budget_lines)
    suppliers = [""] + [s.name for s in ref.suppliers.values()]
    for f in FORM_FIELDS:
        st.session_state.setdefault(_key(prefix, f), "")
    if st.session_state[_key(prefix, "department")] not in DEPARTMENTS:
        st.session_state[_key(prefix, "department")] = "Programs"
    k = lambda f: _key(prefix, f)                                          # noqa: E731
    with st.form(f"{prefix}_form", border=True):
        c = st.columns(3)
        c[0].text_input("Requester *", key=k("requester"), disabled=lock_requester, max_chars=60)
        c[1].selectbox("Department *", DEPARTMENTS, key=k("department"))
        c[2].selectbox("Item *", [""] + items if st.session_state[k("item")] in [""] + items
                       else [""] + items + [st.session_state[k("item")]], key=k("item"),
                       accept_new_options=True, help="Pick from the catalogue, or type another item.")
        c = st.columns(3)
        c[0].text_input("Quantity *", key=k("quantity"), placeholder="e.g. 200")
        c[1].text_input("Unit price (USD) *", key=k("unit_price_usd"), placeholder="e.g. 42.00")
        c[2].text_input("Number of quotations", key=k("quotes"), placeholder="e.g. 3")
        c = st.columns(3)
        c[0].selectbox("Supplier", suppliers if st.session_state[k("supplier")] in suppliers
                       else suppliers + [st.session_state[k("supplier")]], key=k("supplier"),
                       accept_new_options=True, help="Pre-qualified suppliers are listed; you can type another.")
        c[1].selectbox("Budget line", lines if st.session_state[k("budget_line")] in lines
                       else lines + [st.session_state[k("budget_line")]], key=k("budget_line"),
                       accept_new_options=True,
                       format_func=lambda code: f"{code} · {ref.budget_lines[code].description}"
                       if code in ref.budget_lines else code)
        c[2].text_input("Approving manager", key=k("manager"), max_chars=60)
        st.text_input("Evidence", key=k("evidence"), placeholder="DST-001 (Project 1 distribution) or FB-0005 "
                      "(Project 2 feedback)")
        st.text_area("Justification * (Arabic or English)", key=k("justification"), height=90, max_chars=1000)
        st.caption("\\* required to submit. The other fields can be empty: the agent will ask for them.")
        submitted = st.form_submit_button(submit_label, type="primary", use_container_width=True)
    return submitted, {f: st.session_state[_key(prefix, f)] for f in FORM_FIELDS}


def validate(fields: dict):
    """(request, errors) with the requester's own messages; shows the errors."""
    request, errors = validate_form(**fields)
    if errors:
        st.error("**Please correct the form. Nothing was saved.**  \n"
                 + "  \n".join(f"• **{f.replace('_usd', '').replace('_', ' ').capitalize()}**: {m}"
                               for f, m in errors.items()))
    return request, errors


def status_badge(status: Status) -> str:
    return STATUS_LABEL[status.value]

