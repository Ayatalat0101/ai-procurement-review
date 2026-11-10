"""
AI Procurement Review: entry point.

Three pages: the requester's form, the requester's own requests (fix and
resubmit), and the reviewer's queue. A real deployment would split them by
login; for the demo one link shows the whole flow.

Pages live in views/, not pages/: Streamlit auto-scans a folder named pages/
and adds its own menu (a lesson from Project 2).

Run:  streamlit run app.py
"""
import streamlit as st

st.set_page_config(page_title="AI Procurement Review", page_icon="🧾", layout="wide")

pages = st.navigation([
    st.Page("views/new_request.py", title="New request", icon="🧾", default=True),
    st.Page("views/my_requests.py", title="My requests", icon="📂"),
    st.Page("views/review_queue.py", title="Review queue", icon="👀"),
])
pages.run()
