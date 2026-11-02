import math
import urllib.parse

import pandas as pd
import streamlit as st

from portal.config import PAGE_SIZE
from portal.data import load_chapters, load_homeworks, load_item_grades, load_quizzes, load_students, merge_grades
from portal.messages import build_wa_messages
from portal.reports import build_report_pdf
from portal.utils import clean, fmt, set_flag
from whatsapp_automation import clean_wa_phone as clean_number


@st.fragment
def whatsapp_fragment(db, kind, item_id, title, total, vid_link, df):
    n = len(df)
    pages = max(1, math.ceil(n / PAGE_SIZE))
    page = 1
    if pages > 1:
        page = int(st.number_input(f"Page (1–{pages})", min_value=1, max_value=pages, value=1, step=1,
                                   key=f"wa_page_{kind}_{item_id}"))
    chunk = df.iloc[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

    for _, row in chunk.iterrows():
        sid = row["id"]
        col1, col2, col3, col4 = st.columns([3, 2, 2, 4])

        group_label = (f" *(Group: {row['group_number']})*"
                       if pd.notna(row["group_number"]) and str(row["group_number"]).strip() else "")
        col1.markdown(f"**{row['name']}**{group_label}")

        score = clean(row["score"])
        is_submitted = bool(row.get("submitted", False))
        has_score = score is not None
        if has_score:
            col2.write(f"Score: **{fmt(score)}** / {fmt(total)}")
        elif is_submitted:
            col2.markdown("Status: 📩 **Submitted**")
        else:
            col2.markdown("Score: ❌ **Missing**")

        with col3:
            if has_score:
                gen_key = f"gen_wa_{kind}_{sid}_{item_id}"
                if not st.session_state.get(gen_key):
                    st.button("📄 Prepare PDF", key=f"btn_{gen_key}", use_container_width=True,
                              on_click=set_flag, args=(gen_key,))
                else:
                    pdf = build_report_pdf(
                        db, kind, int(item_id), str(sid), row["name"], title,
                        score, clean(total), clean(row["percentage"]), clean(row["report"]), vid_link,
                    )
                    st.download_button(
                        label="⬇️ Download Ready", data=pdf,
                        file_name=f"{row['name']}_{title}.pdf".replace(" ", "_"),
                        mime="application/pdf", key=f"dl_{kind}_{item_id}_{sid}",
                        use_container_width=True,
                    )
            else:
                st.button("📄 N/A", disabled=True, key=f"dl_na_{kind}_{item_id}_{sid}", use_container_width=True)

        with col4:
            msg_parent, msg_student = build_wa_messages(kind, row["name"], title, score, total)
            parent_num = clean_number(row["phone_parent"])
            student_num = clean_number(row["phone"])
            sub1, sub2 = st.columns(2)
            with sub1:
                if parent_num:
                    st.link_button(
                        "👨‍👩‍👦 Parent",
                        f"https://api.whatsapp.com/send?phone={parent_num}&text={urllib.parse.quote(msg_parent)}",
                        key=f"wa_p_{kind}_{item_id}_{sid}", use_container_width=True)
                else:
                    st.button("👨‍👩‍👦 N/A", disabled=True, key=f"wa_p_na_{kind}_{item_id}_{sid}",
                              use_container_width=True)
            with sub2:
                if student_num:
                    st.link_button(
                        "🎓 Student",
                        f"https://api.whatsapp.com/send?phone={student_num}&text={urllib.parse.quote(msg_student)}",
                        key=f"wa_s_{kind}_{item_id}_{sid}", use_container_width=True)
                else:
                    st.button("🎓 N/A", disabled=True, key=f"wa_s_na_{kind}_{item_id}_{sid}",
                              use_container_width=True)
        st.divider()


def render_whatsapp_broadcast(db):
    st.subheader("💬 WhatsApp & Report Broadcasting")
    st.caption("Bulk download PDFs and message parents or students directly for graded or missing assignments.")

    type_choice = st.radio("Select Assignment Type", ["Homework", "Quiz"], horizontal=True)
    kind = "homework" if type_choice == "Homework" else "quiz"

    ch_df = load_chapters(db)
    if ch_df.empty:
        st.warning("No chapters exist yet. Please create a chapter in '📚 Manage Homeworks' first.")
        return

    ch_titles = dict(zip(ch_df["chapter_id"], ch_df["title"]))
    ch_id = st.selectbox("📂 Filter by Chapter", list(ch_titles.keys()), format_func=lambda i: ch_titles[i])

    if kind == "homework":
        items = load_homeworks(db)
        items = items[items["chapter_id"] == ch_id]
        if items.empty:
            st.info(f"No homeworks found in chapter '{ch_titles[ch_id]}'.")
            return
        titles = dict(zip(items["homework_id"], items["title"]))
        item_id = int(st.selectbox("Select Homework", list(titles.keys()), format_func=lambda i: titles[i]))
        row = items[items["homework_id"] == item_id].iloc[0]
        total = int(row["total_questions"])
        vid = row["video_link"] if pd.notna(row["video_link"]) and str(row["video_link"]).strip() else None
    else:
        items = load_quizzes(db)
        items = items[items["chapter_id"] == ch_id]
        if items.empty:
            st.info(f"No quizzes found in chapter '{ch_titles[ch_id]}'.")
            return
        titles = dict(zip(items["quiz_id"], items["title"]))
        item_id = int(st.selectbox("Select Quiz", list(titles.keys()), format_func=lambda i: titles[i]))
        row = items[items["quiz_id"] == item_id].iloc[0]
        total = float(row["max_score"])
        vid = None
    title = row["title"]

    students_df = load_students(db)
    if students_df.empty:
        st.warning("No students found in the database.")
        return

    df = merge_grades(students_df, load_item_grades(db, kind, item_id))
    df = (df.assign(_g=df["group_number"].fillna("").astype(str))
            .sort_values(["_g", "name"]).drop(columns="_g"))

    groups = sorted({str(g).strip() for g in df["group_number"].dropna().unique() if str(g).strip()})
    if groups:
        filter_group = st.selectbox("Filter Broadcasting by Group", ["All Groups"] + groups)
        if filter_group != "All Groups":
            df = df[df["group_number"].astype(str).str.strip() == filter_group]

    st.success(f"Found {len(df)} students in this selection.")
    st.divider()

    whatsapp_fragment(db, kind, item_id, title, total, vid, df)
