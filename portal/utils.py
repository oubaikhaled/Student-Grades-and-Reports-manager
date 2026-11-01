import re

import pandas as pd
import streamlit as st


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def build_drive_hw_filename(student_id: str, homework_id: int, homework_title: str) -> str:
    """
    Builds a clean, filesystem-safe Google Drive filename that includes student_id,
    homework_id, and the homework title (e.g., '176109,5_Homework_1_Derivatives.pdf').
    """
    safe_title = re.sub(r'[\\/*?:"<>|,]', "", str(homework_title or "")).strip()
    safe_title = re.sub(r"\s+", "_", safe_title)
    if safe_title:
        return f"{student_id},{homework_id}_{safe_title}.pdf"
    return f"{student_id},{homework_id}.pdf"


def clean(v):
    """numpy -> python scalar, NaN/NA -> None (also keeps cache keys hashable)."""
    if v is None:
        return None
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):
        try:
            v = v.item()
        except Exception:
            pass
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def fmt(v):
    v = clean(v)
    if v is None:
        return ""
    try:
        f = float(v)
        return str(int(f)) if f.is_integer() else str(f)
    except (TypeError, ValueError):
        return str(v)


def set_flag(key):
    st.session_state[key] = True


def to_bytes(buf):
    return buf.getvalue() if hasattr(buf, "getvalue") else bytes(buf)
