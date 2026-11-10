"""
Review queue: the human-in-the-loop half (T14).

The agent sends compliant requests here with a RECOMMENDATION. A named
reviewer approves, rejects or returns each one, and the decision is saved
with their name and reason. The store refuses what the policy forbids
(deciding your own request, approving an outdated version, approving when
the budget has been used meanwhile), so the buttons are not the only guard.
"""
import pandas as pd
import streamlit as st

from procurement.agent import DecisionError
from procurement.checks import money
from procurement.data import reference_data
from procurement.models import Status, validate_form
from ui import (CSS, FORM_FIELDS, STATUS_LABEL, assessment_of, check_table, get_store, history_view,
                request_line, sample_rows, show_text, versions_view)

st.markdown(CSS, unsafe_allow_html=True)
store = get_store()

st.session_state.setdefault("reviewer", "Huda Saleh")
with st.sidebar:
    reviewer = st.text_input("Reviewer name (demo)", value=st.session_state.reviewer, max_chars=60,
                             help="A real deployment would take this from the login.").strip()
    st.session_state.reviewer = reviewer
    st.divider()
    st.caption("**Demo data**")
    if st.button("Submit the 20 sample requests", use_container_width=True):
        for row in sample_rows(valid_only=True):
            request, _ = validate_form(**{f: row[f] for f in FORM_FIELDS})
            store.submit(request)
        st.toast("20 sample requests submitted and checked")
        st.rerun()
    confirm = st.checkbox("I want to delete all requests")
    if st.button("Delete all requests", disabled=not confirm, use_container_width=True):
        store.reset()
        st.session_state.pop("review_selected", None)
        st.rerun()

st.title("👀 Review queue")
st.caption("The agent only **recommends**. Approving, rejecting or returning is a person's decision, saved with "
           "their name.")

records = store.list()
counts = {s: sum(r.status is s for r in records) for s in Status}
m = st.columns(5)
m[0].metric("👀 Awaiting review", counts[Status.AWAITING_REVIEW])
m[1].metric("⏳ Needs information", counts[Status.NEEDS_INFO])
m[2].metric("⚠️ Policy issue", counts[Status.POLICY_ISSUE])
m[3].metric("✅ Approved", counts[Status.APPROVED])
m[4].metric("Total", len(records))

tab_queue, tab_all, tab_budget = st.tabs(["Queue", "All requests", "Budget"])


def table_of(recs):
    return pd.DataFrame([{"Request": r.ref, "Requester": r.requester, "Item": r.latest.request.item,
                          "Total (USD)": money(r.latest.request.total), "Status": STATUS_LABEL[r.status.value],
                          "Version": f"v{r.current_version}", "Updated": r.updated_at.replace("T", " ")[:16]}
                         for r in recs])


with tab_all:
    if records:
        status = st.selectbox("Status", ["All"] + [s.value for s in Status if s is not Status.DRAFT],
                              format_func=lambda s: STATUS_LABEL.get(s, s))
        shown = [r for r in records if status == "All" or r.status.value == status]
        st.dataframe(table_of(shown), hide_index=True, use_container_width=True)
    else:
        st.info("No requests yet.")

with tab_budget:
    base, now = reference_data(), store.reference()
    st.dataframe(pd.DataFrame([{
        "Budget line": code, "Pays for": line.budget_category, "Description": line.description,
        "Annual": money(line.annual), "Committed before": money(line.committed),
        "Approved here": money(now.budget_lines[code].committed - line.committed),
        "Remaining": money(now.budget_lines[code].remaining)} for code, line in base.budget_lines.items()]),
        hide_index=True, use_container_width=True)
    st.caption("Amounts in USD. Approved requests use up their budget line, so the next request is checked "
               "against what is really left.")

with tab_queue:
    queue = store.queue()
    if not queue:
        st.info("Nothing is waiting for a decision.")
    else:
        table = table_of(queue)
        event = st.dataframe(table, hide_index=True, use_container_width=True, on_select="rerun",
                             selection_mode="single-row",
                             key=f"queue_table_{st.session_state.get('queue_nonce', 0)}")
        if event.selection.rows:
            st.session_state.review_selected = table.iloc[event.selection.rows[0]]["Request"]
        selected = st.session_state.get("review_selected")
        if selected not in [r.ref for r in queue]:
            st.caption("Select a request to review it.")
        else:
            record = store.get(selected)
            a = assessment_of(record)
            r = a.request
            st.divider()
            st.subheader(f"{record.ref} · v{record.current_version} · {record.requester} ({record.department})")
            st.markdown(request_line(r))
            live = store.assess(r)                      # checked again NOW: other approvals may have used the budget
            if live.action.value == "send_for_review":
                st.info("🤖 **Agent recommendation:** all applicable checks passed, and still pass now. "
                        "*This is a recommendation, not an approval.*")
                line = store.reference().budget_line(r.budget_line)
                if line:
                    st.caption(f"Budget {line.code}: {money(line.remaining)} USD left now → "
                               f"{money(line.remaining - r.total)} USD after this request.")
            else:
                st.warning("⚠️ **Something changed since the agent checked this request.** It no longer passes: "
                           + "; ".join(f"{c.code} {c.message}" for c in live.failed + live.missing)
                           + "  \nIt cannot be approved as it is. Return it for changes, or reject it.")
            check_table(live.results)
            st.caption("Checked again when you opened it.")
            st.caption("Justification")
            show_text(r.justification)
            with st.expander("History and versions"):
                history_view(store, record.ref)
                versions_view(store, record.ref)

            with st.form(f"decide_{record.ref}_v{record.current_version}"):
                decision = st.radio("Decision", ["approve", "return", "reject"], horizontal=True,
                                    format_func={"approve": "✅ Approve", "return": "↩️ Return for changes",
                                                 "reject": "⛔ Reject"}.get)
                reason = st.text_area("Reason (required to return or reject)", height=70, max_chars=500)
                if st.form_submit_button(f"Record decision as {reviewer or '…'}", type="primary"):
                    try:
                        status = store.decide(record.ref, decision, reviewer, reason, version=record.current_version)
                        st.session_state.review_flash = f"{record.ref}: {STATUS_LABEL[status.value]} by {reviewer}"
                        st.session_state.pop("review_selected", None)
                        st.session_state.queue_nonce = st.session_state.get("queue_nonce", 0) + 1  # clear the row
                        st.rerun()
                    except DecisionError as err:
                        st.error(str(err))

if st.session_state.get("review_flash"):
    st.toast(st.session_state.pop("review_flash"))
