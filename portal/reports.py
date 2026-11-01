import streamlit as st

from pdf_utils import PDFGenerator
from portal.data import fetch_report_image
from portal.utils import to_bytes


@st.cache_data(show_spinner=False, ttl=600, max_entries=200)
def build_report_pdf(_db, kind, item_id, student_id, student_name, title,
                     score, out_of, pct, report, video_link):
    img = fetch_report_image(_db, kind, item_id, student_id)
    extra = {"video_link": video_link} if video_link else {}
    buf = PDFGenerator.generate_student_report(
        student_name, title, score, out_of, pct, report, img, **extra
    )
    return to_bytes(buf)


@st.cache_data(show_spinner=False, ttl=600)
def get_cached_master_report(title, group, total, pdf_data):
    return to_bytes(PDFGenerator.generate_master_report(f"{title} ({group})", total, pdf_data))
