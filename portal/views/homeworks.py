import pandas as pd
import streamlit as st

from portal.data import load_chapters, load_homeworks, run_write
from portal.views.grading import grading_section


def render_manage_homeworks(db):
    st.subheader("📚 Manage Curriculum & Homeworks")
    tab_hw, tab_ch = st.tabs(["📝 Homeworks", "📑 Manage Chapters"])
    ch_df = load_chapters(db)

    with tab_ch:
        with st.form("add_chapter_form"):
            title = st.text_input("Chapter Title (e.g., 'Unit 1: Integration')").strip()
            if st.form_submit_button("➕ Add Chapter", type="primary") and title:
                try:
                    run_write(db, [("INSERT INTO chapters (title) VALUES (%s)", (title,))])
                    st.session_state["flash"] = f"Chapter '{title}' added!"
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")

        if not ch_df.empty:
            st.divider()
            st.dataframe(ch_df, hide_index=True, use_container_width=True)
            with st.expander("⚠️ Delete a Chapter"):
                ch_titles = dict(zip(ch_df["chapter_id"], ch_df["title"]))
                del_id = st.selectbox("Select Chapter to Delete", list(ch_titles.keys()),
                                      format_func=lambda i: ch_titles[i], key="del_ch_hw")
                if st.button("🚨 Delete Chapter", key="btn_del_ch_hw"):
                    run_write(db, [("DELETE FROM chapters WHERE chapter_id = %s", (int(del_id),))])
                    st.rerun()

    with tab_hw:
        if ch_df.empty:
            st.warning("Please create at least one Chapter in the 'Manage Chapters' tab first.")
            return

        hw_df = load_homeworks(db)
        ch_titles = dict(zip(ch_df["chapter_id"], ch_df["title"]))

        with st.expander("🛠️ Add or Delete Homework Assignments", expanded=False):
            with st.form("add_homework_form"):
                ch_id = st.selectbox("Assign to Chapter", list(ch_titles.keys()),
                                     format_func=lambda i: f"{ch_titles[i]} (ID: {i})")
                title = st.text_input("Homework Title")
                total_q = st.number_input("Total Questions (Max Score)", min_value=1, step=1)
                video_link = st.text_input("Homework Video Link (Optional)", placeholder="https://youtube.com/...")
                rubric = st.text_area(
                    "AI Grading Answer Key / Rubric (Optional)",
                    placeholder="Paste the model answers here so AI can auto-grade with one click (e.g., Q1: B (-1), Q2: C (4), Q3: dy/dx = 2x + 1)...",
                    height=110,
                )

                if st.form_submit_button("➕ Add Homework", type="primary") and title.strip():
                    try:
                        clean_link = video_link.strip() or None
                        clean_rubric = rubric.strip() or None
                        run_write(db, [(
                            "INSERT INTO homeworks (title, total_questions, video_link, chapter_id, rubric) VALUES (%s, %s, %s, %s, %s)",
                            (title.strip(), int(total_q), clean_link, int(ch_id), clean_rubric),
                        )])
                        st.session_state["flash"] = f"Homework '{title.strip()}' created!"
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error: {e}")

            if not hw_df.empty:
                st.divider()
                hw_titles = dict(zip(hw_df["homework_id"], hw_df["title"]))
                del_id = st.selectbox("Select Homework to Delete", list(hw_titles.keys()),
                                      format_func=lambda i: hw_titles[i], key="del_hw_sel")
                if st.button("🚨 Delete Selected Homework", key="btn_del_hw"):
                    run_write(db, [
                        ("DELETE FROM homework_grades WHERE homework_id = %s", (int(del_id),)),
                        ("DELETE FROM homeworks WHERE homework_id = %s", (int(del_id),)),
                    ])
                    st.rerun()

        if hw_df.empty:
            st.info("No homeworks created yet.")
            return

        st.divider()
        st.subheader("📊 Select Assignment to Grade")
        hw_titles = dict(zip(hw_df["homework_id"], hw_df["title"]))
        hw_id = int(st.selectbox("Current Homework", list(hw_titles.keys()),
                                 format_func=lambda i: hw_titles[i], key="sel_hw_grade"))
        hw_row = hw_df[hw_df["homework_id"] == hw_id].iloc[0]
        saved_rubric = str(hw_row["rubric"]).strip() if "rubric" in hw_row and pd.notna(hw_row["rubric"]) else ""

        with st.expander("📋 View or Update Answer Key / Rubric for this Homework", expanded=False):
            with st.form(f"edit_rubric_form_{hw_id}"):
                updated_rubric = st.text_area(
                    "Answer Key / Rubric used for 1-Click AI Grading",
                    value=saved_rubric,
                    placeholder="e.g., Q1: B (-1), Q2: C (4), Q3: dy/dx = 2x + 1...",
                    height=100,
                )
                if st.form_submit_button("💾 Save Answer Key", type="primary"):
                    run_write(db, [
                        ("UPDATE homeworks SET rubric = %s WHERE homework_id = %s", (updated_rubric.strip() or None, hw_id))
                    ])
                    st.session_state["flash"] = "Answer Key updated!"
                    st.rerun()

        grading_section(db, 
            "homework",
            hw_id,
            hw_row["title"],
            int(hw_row["total_questions"]),
            saved_rubric=saved_rubric,
        )
