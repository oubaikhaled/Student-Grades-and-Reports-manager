import streamlit as st
import pandas as pd
from pdf_utils import PDFGenerator

CACHE_TTL = 120


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_all_students_with_notes(_db):
    return _db.fetch_dataframe(
        "SELECT id, name, phone, phone_parent, group_number, notes FROM students ORDER BY name ASC"
    )


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_student_profile_grades(_db, stu_id: str):
    hw_df = _db.fetch_dataframe(
        """
        SELECT h.homework_id as "item_id", h.title as "Homework", hg.correct_answers as "Score",
               h.total_questions as "Out Of", hg.percentage as "Percentage", hg.report as "Report"
        FROM homework_grades hg
        JOIN homeworks h ON hg.homework_id = h.homework_id
        WHERE hg.student_id = %s AND hg.correct_answers IS NOT NULL
        ORDER BY h.homework_id ASC
        """,
        (stu_id,),
    )
    qz_df = _db.fetch_dataframe(
        """
        SELECT q.quiz_id as "item_id", q.title as "Quiz", qg.score as "Score",
               q.max_score as "Out Of", qg.percentage as "Percentage", qg.report as "Report"
        FROM quiz_grades qg
        JOIN quizzes q ON qg.quiz_id = q.quiz_id
        WHERE qg.student_id = %s AND qg.score IS NOT NULL
        ORDER BY q.quiz_id ASC
        """,
        (stu_id,),
    )
    return hw_df, qz_df


@st.cache_data(ttl=600, show_spinner=False)
def build_cached_profile_pdf(
    student_dict, status_text, avg_display, hw_records, qz_records,
    hw_avg_display, weaknesses, hw_chart_data, qz_chart_data
):
    buf = PDFGenerator.generate_student_profile_report(
        student_info=student_dict,
        status_text=status_text,
        avg_display=avg_display,
        hw_records=hw_records,
        qz_records=qz_records,
        hw_avg_display=hw_avg_display,
        weaknesses=weaknesses,
        hw_chart_data=hw_chart_data,
        qz_chart_data=qz_chart_data,
    )
    return buf.getvalue()


class StudentProfileView:
    def __init__(self, db_manager):
        self.db = db_manager

    def render_single_student_profile(self, student_info, is_admin: bool = True):
        """Renders the complete Student Profile for either Admin or Parent Portal."""
        stu_id = str(student_info["id"])
        hw_df, qz_df = load_student_profile_grades(self.db, stu_id)

        if not qz_df.empty and not qz_df["Percentage"].isna().all():
            avg_quiz_perc = qz_df["Percentage"].mean()
            if avg_quiz_perc >= 85:
                status = "🌟 Excellent Student"
            elif avg_quiz_perc >= 70:
                status = "👍 Average Student"
            else:
                status = "⚠️ Below Average"
            avg_display = f"{avg_quiz_perc:.1f}%"
        else:
            status = "⚪ No Quiz Data Yet"
            avg_display = "N/A"

        hw_avg_display = (
            f"{hw_df['Percentage'].mean():.1f}%"
            if not hw_df.empty and not hw_df["Percentage"].isna().all()
            else "N/A"
        )

        # 1. Unified Summary Card
        with st.container(border=True):
            col1, col2, col3 = st.columns([2.5, 1.2, 1.2])
            with col1:
                st.markdown(f"### 🎓 {student_info['name']}")
                grp = (
                    student_info["group_number"]
                    if pd.notna(student_info.get("group_number")) and str(student_info.get("group_number")).strip()
                    else "N/A"
                )
                s_phone = (
                    student_info["phone"]
                    if pd.notna(student_info.get("phone")) and str(student_info.get("phone")).strip()
                    else "N/A"
                )
                p_phone = (
                    student_info["phone_parent"]
                    if pd.notna(student_info.get("phone_parent")) and str(student_info.get("phone_parent")).strip()
                    else "N/A"
                )
                st.markdown(f"**ID:** `{stu_id}` &nbsp;|&nbsp; **Group:** `{grp}`")
                st.markdown(f"**Student Phone:** `{s_phone}` &nbsp;|&nbsp; **Parent Phone:** `{p_phone}`")
            with col2:
                st.metric("Quiz Average", avg_display, f"{len(qz_df)} Quizzes")
            with col3:
                st.metric("Homework Average", hw_avg_display, status)

        # 2. Performance Timelines
        weaknesses = []
        hw_chart_data = []
        if not hw_df.empty:
            for row in hw_df.to_dict("records"):
                perc = row["Percentage"]
                if pd.notna(perc):
                    hw_chart_data.append({"Assignment": row["Homework"], "Score": float(perc)})
                    if perc < 70:
                        weaknesses.append({"Topic": f"{row['Homework']} (HW)", "Percentage": float(perc)})

        qz_chart_data = []
        if not qz_df.empty:
            for row in qz_df.to_dict("records"):
                perc = row["Percentage"]
                if pd.notna(perc):
                    qz_chart_data.append({"Assignment": row["Quiz"], "Score": float(perc)})
                    if perc < 70:
                        weaknesses.append({"Topic": f"{row['Quiz']} (Quiz)", "Percentage": float(perc)})

        weaknesses = sorted(weaknesses, key=lambda x: x["Percentage"])

        st.subheader("📈 Performance Timelines")
        col_g1, col_g2 = st.columns(2)

        with col_g1:
            st.markdown("**Homework Trajectory**")
            if hw_chart_data:
                st.line_chart(pd.DataFrame(hw_chart_data).set_index("Assignment"), color="#1f77b4")
            else:
                st.info("Not enough homework data to plot.")

        with col_g2:
            st.markdown("**Quiz Trajectory**")
            if qz_chart_data:
                st.line_chart(pd.DataFrame(qz_chart_data).set_index("Assignment"), color="#ff7f0e")
            else:
                st.info("Not enough quiz data to plot.")

        st.divider()

        # 3. Priority Focus Areas & Teacher's Improvement Plan
        col_w, col_t = st.columns(2)
        with col_w:
            st.subheader("🎯 Priority Focus Areas")
            st.caption("Top 3 weakest topics (Below 70%)")
            if weaknesses:
                for w in weaknesses[:3]:
                    st.error(f"**{w['Topic']}**: {w['Percentage']:.1f}%")
            else:
                st.success("No critical weaknesses detected! All assignments are at 70% or above.")

        with col_t:
            st.subheader("✍️ Teacher's Improvement Plan")
            current_notes = student_info.get("notes", "")
            if pd.isna(current_notes):
                current_notes = ""

            if is_admin:
                with st.form(f"teacher_notes_form_{stu_id}"):
                    new_notes = st.text_area(
                        "Write actionable advice, goals, or notes for this student:",
                        value=current_notes,
                        height=125,
                    )
                    if st.form_submit_button("💾 Save Notes", type="primary"):
                        try:
                            self.db.execute_query(
                                "UPDATE students SET notes = %s WHERE id = %s",
                                (new_notes.strip(), stu_id),
                            )
                            st.cache_data.clear()
                            st.success("Notes saved successfully!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Failed to save notes: {e}")
            else:
                st.caption("Actionable advice, goals, and notes from the teacher")
                if str(current_notes).strip():
                    st.info(current_notes)
                else:
                    st.info("No teacher notes recorded yet.")

        st.divider()

        # 4. Homework & Quiz History Tables
        col_hw_tbl, col_qz_tbl = st.columns(2)
        hw_disp_cols = ["Homework", "Score", "Out Of", "Percentage"]
        qz_disp_cols = ["Quiz", "Score", "Out Of", "Percentage"]

        hw_records_desc = []
        qz_records_desc = []

        with col_hw_tbl:
            st.subheader("📚 Homework History")
            if hw_df.empty:
                st.info("No homework grades recorded.")
            else:
                hw_disp = hw_df[hw_disp_cols].iloc[::-1].copy()
                hw_disp["Percentage"] = hw_disp["Percentage"].apply(
                    lambda x: f"{x:.1f}%" if pd.notna(x) else "N/A"
                )
                hw_records_desc = hw_disp.to_dict("records")
                st.dataframe(hw_disp, hide_index=True, use_container_width=True)

        with col_qz_tbl:
            st.subheader("📝 Quiz History")
            if qz_df.empty:
                st.info("No quiz grades recorded.")
            else:
                qz_disp = qz_df[qz_disp_cols].iloc[::-1].copy()
                qz_disp["Percentage"] = qz_disp["Percentage"].apply(
                    lambda x: f"{x:.1f}%" if pd.notna(x) else "N/A"
                )
                qz_records_desc = qz_disp.to_dict("records")
                st.dataframe(qz_disp, hide_index=True, use_container_width=True)

        st.divider()

        # 5. Full Student Profile PDF Download
        col_b1, col_b2, col_b3 = st.columns([1, 2, 1])
        with col_b2:
            prep_key = f"prep_prof_pdf_{stu_id}_{'adm' if is_admin else 'par'}"
            if not st.session_state.get(prep_key):
                if st.button(
                    "📄 Prepare Full Student Profile (PDF)",
                    key=f"btn_{prep_key}",
                    type="primary",
                    use_container_width=True,
                ):
                    st.session_state[prep_key] = True
                    st.rerun()
            else:
                pdf_bytes = build_cached_profile_pdf(
                    student_info.to_dict() if hasattr(student_info, "to_dict") else dict(student_info),
                    status,
                    avg_display,
                    hw_records_desc,
                    qz_records_desc,
                    hw_avg_display,
                    weaknesses,
                    hw_chart_data,
                    qz_chart_data,
                )
                st.download_button(
                    label="⬇️ Download Full Student Profile (PDF)",
                    data=pdf_bytes,
                    file_name=f"{student_info['name']}_Profile.pdf".replace(" ", "_"),
                    mime="application/pdf",
                    key=f"dl_{prep_key}",
                    use_container_width=True,
                )

    def render(self):
        st.subheader("👤 Student Profiles")
        st.caption(
            "Search for a student to view their complete profile, overall status, performance timelines, and teacher notes."
        )

        students_df = load_all_students_with_notes(self.db)
        if students_df.empty:
            st.info("No students registered yet.")
            return

        col_s1, col_s2 = st.columns([1, 2])
        with col_s1:
            search_term = st.text_input(
                "🔍 Filter by Name or ID", placeholder="Type to filter..."
            ).strip()

        if search_term:
            mask = (
                students_df["name"].astype(str).str.contains(search_term, case=False, na=False, regex=False)
                | students_df["id"].astype(str).str.contains(search_term, case=False, na=False, regex=False)
            )
            filtered_df = students_df[mask]
        else:
            filtered_df = students_df

        if filtered_df.empty:
            st.warning("No students match your search.")
            return

        labels = {
            str(r.id): f"{r.name} (ID: {r.id})"
            for r in filtered_df.itertuples(index=False)
        }
        with col_s2:
            stu_id = st.selectbox(
                "Select Student",
                options=list(labels.keys()),
                format_func=lambda k: labels[k],
                key="profile_student_sel",
            )

        if stu_id:
            student_info = filtered_df[filtered_df["id"].astype(str) == str(stu_id)].iloc[0]
            self.render_single_student_profile(student_info, is_admin=True)
