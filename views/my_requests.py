"""
My requests: the requester sees their requests, fixes them and resubmits (T13).

The fix form is prefilled from the latest version, so the requester only adds
what is missing or changes what failed. The resubmission is a new version of
the SAME request (same PR number), checked again from zero.
"""
import pandas as pd
import streamlit as st

from procurement.checks import money
from procurement.store import RESUBMITTABLE, StoreError
from ui import (CSS, STATUS_LABEL, assessment_of, check_table, explanation_box, fill_form, get_store, history_view, outcome_banner,
                request_form, request_line, show_text, validate, versions_view)

st.markdown(CSS, unsafe_allow_html=True)
store = get_store()

# a widget's own state is dropped when you change page, so the name lives in a plain key
st.session_state.setdefault("my_name", "Lina Odeh")
with st.sidebar:
    name = st.text_input("Your name (demo)", value=st.session_state.my_name, max_chars=60,
                         help="A real deployment would take this from the login.").strip()
    st.session_state.my_name = name

st.title("📂 My requests")
records = store.list(requester=name) if name else []
if not records:
    st.info(f"No requests from **{name or '…'}** yet. Submit one on **New request**, or type another name "
            "in the sidebar.")
    st.stop()

table = pd.DataFrame([{"Request": r.ref, "Item": r.latest.request.item,
                       "Total (USD)": money(r.latest.request.total), "Status": STATUS_LABEL[r.status.value],
                       "Version": f"v{r.current_version}", "Updated": r.updated_at.replace("T", " ")[:16]}
                      for r in records])
event = st.dataframe(table, hide_index=True, use_container_width=True, on_select="rerun",
                     selection_mode="single-row", key="my_table")
if event.selection.rows:
    st.session_state.my_selected = table.iloc[event.selection.rows[0]]["Request"]
refs = [r.ref for r in records]
selected = st.session_state.get("my_selected")
if selected not in refs:
    st.caption("Select a request to see its checks and history.")
    st.stop()

record = store.get(selected)
a = assessment_of(record)
st.divider()
st.subheader(f"{record.ref} · v{record.current_version} · {STATUS_LABEL[record.status.value]}")
st.markdown(request_line(a.request))

if record.status.value in ("approved", "rejected", "returned"):
    decided = {"approved": st.success, "rejected": st.error, "returned": st.warning}[record.status.value]
    decided(f"**{STATUS_LABEL[record.status.value]} by {record.decided_by}**"
            + (f"  \nReason: {record.decision_reason}" if record.decision_reason else ""))
else:
    outcome_banner(a)

tab_checks, tab_fix, tab_history = st.tabs(["Checks", "Fix and resubmit", "History and versions"])
with tab_checks:
    check_table(a.results)
    if record.status.value in ("needs_info", "policy_issue", "awaiting_review"):
        st.caption("Explanation")
        explanation_box(a, f"{record.ref}-v{record.current_version}")
    st.caption("Justification")
    show_text(a.request.justification)

with tab_fix:
    if record.status not in RESUBMITTABLE:
        st.info("This request cannot be changed now: "
                + ("a person is reviewing it." if record.status.value == "awaiting_review"
                   else "the decision is final. Submit a new request if needed."))
    else:
        prefix = f"fix_{record.ref}_v{record.current_version}"          # a fresh prefill for every version
        if f"{prefix}_requester" not in st.session_state:
            fill_form(prefix, a.request.model_dump())
        missing = a.missing_fields
        if missing:
            st.caption("Missing: **" + ", ".join(missing) + "**. Everything you typed before is kept.")
        submitted, fields = request_form(prefix, f"Resubmit as v{record.current_version + 1}", lock_requester=True)
        if submitted:
            request, errors = validate(fields)
            if request:
                try:
                    new = store.resubmit(record.ref, request)
                    st.session_state.my_flash = (record.ref, record.current_version + 1, new.headline)
                    st.rerun()
                except StoreError as err:
                    st.error(str(err))

with tab_history:
    history_view(store, record.ref)
    st.caption("Every version, as it was checked")
    versions_view(store, record.ref)

if st.session_state.get("my_flash"):
    ref, v, headline = st.session_state.pop("my_flash")
    st.toast(f"{ref} v{v}: {headline}")
