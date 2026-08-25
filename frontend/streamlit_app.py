"""Give Exit consumer frontend.

The Streamlit application is a pure HTTP client of the FastAPI backend.

Run:
    streamlit run frontend/streamlit_app.py
"""

import os
import sys
from pathlib import Path

import streamlit as st

# The Streamlit launcher does not guarantee that the repository root is importable.
_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from frontend.consumer_view import render_consumer_app  # noqa: E402

API_URL = os.getenv("LITIGATION_API_URL", "http://localhost:8000")
API_AUTH_KEY = os.getenv("LITIGATION_API_AUTH_KEY", "").strip() or None

st.set_page_config(
    page_title="Give Exit — Assistente do consumidor",
    page_icon=":material/contract_edit:",
    layout="wide",
)

render_consumer_app(API_URL, api_key=API_AUTH_KEY)
