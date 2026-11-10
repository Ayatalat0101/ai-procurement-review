"""
New request: the requester's page.

Form on the left, the written policy on the right, so the rules are visible
BEFORE anyone submits. On submit: validate (form errors save nothing), then
the agent runs the 8 checks and picks one action, and the store saves v1.
"""
import streamlit as st

from procurement.extract import FIELDS
from procurement.llm import extract
from ui import (CSS, ai_status, check_table, explanation_box, fill_form, get_store, outcome_banner, pipeline,
                policy_panel, request_form, request_line, sample_rows, validate)

st.markdown(CSS, unsafe_allow_html=True)
store = get_store()
PREFIX = "new"

st.title("🧾 New purchase request")
st.caption("Relief items only. The agent checks the request against the written policy and tells you the next "
           "step. **It never approves**: compliant requests go to a person.")
st.caption(ai_status())


def run_extraction():
    """Button callback: runs BEFORE the form is drawn, so the widgets can be filled."""
    text = st.session_state.get("paste_text", "").strip()
    if len(text) < 10:
        st.session_state.extraction = None
        return
    e = extract(text, pipeline())
    fill_form(PREFIX, {f: e.fields.get(f, "") for f in FIELDS})
    st.session_state.extraction = e
    st.session_state.new_result = None


with st.expander("📋 Paste a request (email, WhatsApp, Arabic or English) and fill the form from it",
                 expanded=False):
    st.text_area("Request text", key="paste_text", height=110, max_chars=3000,
                 placeholder="e.g. معك لينا عودة من البرامج، بدنا ٢٠٠ حقيبة شتوية بسعر 42 دولار ...")
    st.button("Fill the form from this text", on_click=run_extraction)
    e = st.session_state.get("extraction")
    if e:
        found = [f for f in FIELDS if e.fields.get(f) and f != "justification"]
        st.success(f"Filled {len(found)} fields ({e.source}). **Check them below before you submit.**")
        empty = [f for f in e.empty if f != "justification"]
        if empty:
            st.caption("Not in the text, left empty: " + ", ".join(empty))
        for note in e.notes:
            st.warning(note)

# --- demo helper: load one of the 22 test requests into the form --------------------------------
samples = {r["request_id"]: r for r in sample_rows()}
with st.expander("🧪 Load a sample request (demo)"):
    pick = st.selectbox("Sample", list(samples),
                        format_func=lambda rid: f"{rid} · {samples[rid]['item']} · {samples[rid]['why']}")
    st.button("Load into the form", on_click=lambda: fill_form(PREFIX, samples[pick]))

left, right = st.columns([2.2, 1], gap="large")
with left:
    submitted, fields = request_form(PREFIX, "Check and submit")
with right:
    st.subheader("Policy")
    policy_panel()

if submitted:
    st.session_state.new_result = None               # never show the previous request under a new error
    request, errors = validate(fields)
    if request:
        ref, a = store.submit(request)
        st.session_state.new_result = (ref, a)

if st.session_state.get("new_result"):
    ref, a = st.session_state.new_result
    st.divider()
    st.subheader(f"{ref} · version 1")
    st.markdown(request_line(a.request))
    outcome_banner(a)
    check_table(a.results)
    st.caption("Explanation")
    explanation_box(a, f"{ref}-v1")
    with st.expander("How the agent decided"):
        for step in a.steps:
            st.markdown(f"- {step}")
    if a.action.value != "send_for_review":
        st.info(f"Fix the request under **My requests → {ref}** and resubmit: the form keeps what you typed.")
        if st.button("Open it in My requests", type="primary"):
            st.session_state.my_name = a.request.requester
            st.session_state.my_selected = ref
            st.switch_page("views/my_requests.py")
