import psycopg2
import streamlit as st

from portal.data import load_chapters, load_quizzes, run_write
from portal.views.grading import grading_section


def render_manage_quizzes(db):
    st.subheader("📝 Manage Curriculum & Quizzes")

    ch_df = load_chapters(db)
    if ch_df.empty:
        st.warning("Please create at least one Chapter in '📚 Manage Homeworks' -> 'Manage Chapters' tab first.")
        return
    ch_titles = dict(zip(ch_df["chapter_id"], ch_df["title"]))
    qz_df = load_quizzes(db)

    with st.expander("🛠️ Add or Delete Quizzes", expanded=False):
        with st.form("create_quiz_form"):
            ch_id = st.selectbox("Assign to Chapter", list(ch_titles.keys()),
                                 format_func=lambda i: f"{ch_titles[i]} (ID: {i})")
            q_title = st.text_input("Quiz Title").strip()
            q_max_in = st.number_input("Maximum Score", min_value=1.0, value=10.0, step=1.0)
            if st.form_submit_button("➕ Create Quiz", type="primary") and q_title:
                try:
                    run_write(db, [(
                        """
                        WITH q AS (
                            INSERT INTO quizzes (title, max_score, chapter_id)
                            VALUES (%s, %s, %s) RETURNING quiz_id
                        )
                        INSERT INTO quiz_grades (quiz_id, student_id)
                        SELECT q.quiz_id, s.id FROM q CROSS JOIN students s
                        """,
                        (q_title, q_max_in, int(ch_id)),
                    )])
                    st.rerun()
                except psycopg2.IntegrityError:
                    st.error("Quiz already exists.")

        if not qz_df.empty:
            st.divider()
            qz_titles = dict(zip(qz_df["quiz_id"], qz_df["title"]))
            del_id = st.selectbox("Select Quiz to Delete", list(qz_titles.keys()),
                                  format_func=lambda i: qz_titles[i], key="del_qz_sel")
            if st.button("🚨 Delete Selected Quiz", key="btn_del_qz"):
                run_write(db, [
                    ("DELETE FROM quiz_grades WHERE quiz_id = %s", (int(del_id),)),
                    ("DELETE FROM quizzes WHERE quiz_id = %s", (int(del_id),)),
                ])
                st.rerun()

    if qz_df.empty:
        st.info("No quizzes created yet.")
        return

    st.divider()
    st.subheader("📊 Select Quiz to Grade")
    qz_titles = dict(zip(qz_df["quiz_id"], qz_df["title"]))
    q_id = int(st.selectbox("Current Quiz", list(qz_titles.keys()),
                            format_func=lambda i: qz_titles[i], key="sel_qz_grade"))
    q_row = qz_df[qz_df["quiz_id"] == q_id].iloc[0]
    grading_section(db, "quiz", q_id, q_row["title"], float(q_row["max_score"]))
