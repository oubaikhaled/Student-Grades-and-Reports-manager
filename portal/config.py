# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# Read caches are cleared on every write (see portal.data.invalidate_data_caches), so the TTL is
# only a safety net. A long TTL lets the Neon compute scale to zero while people browse.
CACHE_TTL = 1800
PAGE_SIZE = 20               # rows per page on the WhatsApp screen
MAX_IMG_WIDTH = 1200         # uploaded feedback images are downscaled to this width
LOCK_TIMEOUT_SECONDS = 600   # 10 minutes: auto-releases if an admin closes their tab without saving
MAX_PHOTO_MB = 30            # per homework photo; full-size photos from 50-200 MP phone cameras fit
MAX_TOTAL_UPLOAD_MB = 200    # all files in one homework submission together

DEFAULT_DRIVE_SCRIPT_URL = "https://script.google.com/macros/s/AKfycbyLOJdL4kViRmyqtjETxu6qjjVi98MaqymDvemG8mA6PPGGuhf9WR0dqDrh8sVa5VBTug/exec"

KINDS = {
    "homework": {
        "table": "homework_grades", "item_col": "homework_id", "score_col": "correct_answers",
        "name_col": "Assignment", "select_label": "Select Assignment",
        "heading": "**Homework Assignments**", "empty_msg": "No homework grades recorded yet.",
        "noun": "Assignment", "score_label": "Correct Answers", "is_int": True,
    },
    "quiz": {
        "table": "quiz_grades", "item_col": "quiz_id", "score_col": "score",
        "name_col": "Quiz", "select_label": "Select Quiz",
        "heading": "**Quiz Scores**", "empty_msg": "No quiz grades recorded yet.",
        "noun": "Quiz", "score_label": "Final Score", "is_int": False,
    },
}


def get_drive_script_url() -> str:
    """Apps Script web-app URL; set DRIVE_SCRIPT_URL in secrets to override the built-in one."""
    import streamlit as st

    try:
        return str(st.secrets.get("DRIVE_SCRIPT_URL", "") or DEFAULT_DRIVE_SCRIPT_URL).strip()
    except Exception:
        return DEFAULT_DRIVE_SCRIPT_URL
