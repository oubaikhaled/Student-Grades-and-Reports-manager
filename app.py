import streamlit as st
import pandas as pd
from pdf_utils import PDFGenerator

class StudentProfileView:
    def __init__(self, db_manager):
        self.db = db_manager
        self._ensure_notes_column()

    def _ensure_notes_column(self):
        # Safely add the 'notes' column to the students table if it doesn't exist yet
        try:
            with self.db.get_connection() as conn:
                with conn.cursor() as c:
                    c.execute("ALTER TABLE students ADD COLUMN notes TEXT")
                conn.commit()
        except Exception:
            pass # Column already exists, safe to ignore

    def render(self):
        st.subheader("👤 Student Profiles")
        st.caption("Search for a student to view their complete profile, overall status, performance timelines, and teacher notes.")

        # Fetch all students including the new notes column
        students_df = self.db.fetch_dataframe("SELECT id, name, phone, phone_parent, group_number, notes FROM students ORDER BY name ASC")
        
        if students_df.empty:
            st.info("No students registered yet.")
            return

        search_term = st.text_input("🔍 Search Student by Name or ID", placeholder="Type to filter...").strip()
        
        if search_term:
            mask = (
                students_df['name'].str.contains(search_term, case=False, na=False) |
                students_df['id'].astype(str).str.contains(search_term, case=False, na=False)
            )
            filtered_df = students_df[mask]
        else:
            filtered_df = students_df

        if filtered_df.empty:
            st.warning("No students match your search.")
            return

        student_options = filtered_df.apply(lambda x: f"{x['name']} (ID: {x['id']})", axis=1).tolist()
        selected_label = st.selectbox("Select Student", student_options, key="profile_student_sel")

        if selected_label:
            stu_id = selected_label.split("(ID: ")[1].replace(")", "")
            student_info = students_df[students_df['id'] == stu_id].iloc[0]

            hw_df = self.db.fetch_dataframe("""
                SELECT h.title as "Homework", hg.correct_answers as "Score", h.total_questions as "Out Of", hg.percentage as "Percentage"
                FROM homework_grades hg
                JOIN homeworks h ON hg.homework_id = h.homework_id
                WHERE hg.student_id = %s AND hg.correct_answers IS NOT NULL
                ORDER BY h.homework_id ASC
            """, (stu_id,))
            
            qz_df = self.db.fetch_dataframe("""
                SELECT q.title as "Quiz", qg.score as "Score", q.max_score as "Out Of", qg.percentage as "Percentage"
                FROM quiz_grades qg
                JOIN quizzes q ON qg.quiz_id = q.quiz_id
                WHERE qg.student_id = %s AND qg.score IS NOT NULL
                ORDER BY q.quiz_id ASC
            """, (stu_id,))

            if not qz_df.empty and not qz_df['Percentage'].isna().all():
                avg_quiz_perc = qz_df['Percentage'].mean()
                if avg_quiz_perc >= 85:
                    status = "🌟 Excellent Student"
                    status_color = "green"
                elif avg_quiz_perc >= 70:
                    status = "👍 Average Student"
                    status_color = "orange"
                else:
                    status = "⚠️ Below Average"
                    status_color = "red"
                avg_display = f"{avg_quiz_perc:.1f}%"
            else:
                status = "⚪ No Quiz Data Yet"
                status_color = "gray"
                avg_display = "N/A"

            st.divider()

            col1, col2 = st.columns([2, 1])
            with col1:
                st.markdown(f"### {student_info['name']}")
                st.markdown(f"**ID:** {student_info['id']} | **Group:** {student_info['group_number'] if pd.notna(student_info['group_number']) else 'N/A'}")
                st.markdown(f"**Student Phone:** {student_info['phone'] if pd.notna(student_info['phone']) else 'N/A'} | **Parent Phone:** {student_info['phone_parent'] if pd.notna(student_info['phone_parent']) else 'N/A'}")
            with col2:
                st.markdown(f"<h4 style='text-align: center; color: {status_color};'>{status}</h4>", unsafe_allow_html=True)
                st.markdown(f"<p style='text-align: center;'>Quiz Average: <b>{avg_display}</b></p>", unsafe_allow_html=True)

            st.divider()

            weaknesses = []
            hw_chart_data = []
            if not hw_df.empty:
                for row in hw_df.to_dict('records'):
                    perc = row['Percentage']
                    if pd.notna(perc):
                        hw_chart_data.append({"Assignment": row['Homework'], "Score": perc})
                        if perc < 70: weaknesses.append({"Topic": f"{row['Homework']} (HW)", "Percentage": perc})

            qz_chart_data = []
            if not qz_df.empty:
                for row in qz_df.to_dict('records'):
                    perc = row['Percentage']
                    if pd.notna(perc):
                        qz_chart_data.append({"Assignment": row['Quiz'], "Score": perc})
                        if perc < 70: weaknesses.append({"Topic": f"{row['Quiz']} (Quiz)", "Percentage": perc})

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

            # --- UPDATED: Focus Areas and Manual Teacher Notes ---
            col_w, col_t = st.columns(2)
            
            with col_w:
                st.subheader("🎯 Priority Focus Areas")
                st.caption("Top 3 weakest topics (Below 70%)")
                if weaknesses:
                    weaknesses = sorted(weaknesses, key=lambda x: x['Percentage'])
                    for w in weaknesses[:3]:
                        st.error(f"**{w['Topic']}**: {w['Percentage']:.1f}%")
                else:
                    st.success("No critical weaknesses detected! All assignments are at 70% or above.")

            with col_t:
                st.subheader("✍️ Teacher's Improvement Plan")
                current_notes = student_info.get('notes', '')
                if pd.isna(current_notes): current_notes = ''
                
                with st.form("teacher_notes_form"):
                    new_notes = st.text_area("Write actionable advice, goals, or notes for this student:", value=current_notes, height=125)
                    if st.form_submit_button("💾 Save Notes", type="primary"):
                        try:
                            with self.db.get_connection() as conn:
                                with conn.cursor() as c:
                                    c.execute("UPDATE students SET notes = %s WHERE id = %s", (new_notes.strip(), stu_id))
                                conn.commit()
                            st.success("Notes saved successfully!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Failed to save notes: {e}")

            st.divider()

            col_hw_tbl, col_qz_tbl = st.columns(2)
            hw_records_desc = hw_df.iloc[::-1].to_dict('records') if not hw_df.empty else []
            qz_records_desc = qz_df.iloc[::-1].to_dict('records') if not qz_df.empty else []

            with col_hw_tbl:
                st.subheader("📚 Homework History")
                if hw_df.empty:
                    st.info("No homework grades recorded.")
                else:
                    hw_disp = hw_df.iloc[::-1].copy()
                    hw_disp["Percentage"] = hw_disp["Percentage"].apply(lambda x: f"{x:.1f}%" if pd.notna(x) else "N/A")
                    st.dataframe(hw_disp, hide_index=True, use_container_width=True)

            with col_qz_tbl:
                st.subheader("📝 Quiz History")
                if qz_df.empty:
                    st.info("No quiz grades recorded.")
                else:
                    qz_disp = qz_df.iloc[::-1].copy()
                    qz_disp["Percentage"] = qz_disp["Percentage"].apply(lambda x: f"{x:.1f}%" if pd.notna(x) else "N/A")
                    st.dataframe(qz_disp, hide_index=True, use_container_width=True)

            st.divider()
            col_b1, col_b2, col_b3 = st.columns([1, 2, 1])
            with col_b2:
                pdf_buf = PDFGenerator.generate_student_profile_report(
                    student_info=student_info.to_dict(),
                    status_text=status,
                    avg_display=avg_display,
                    hw_records=hw_records_desc,
                    qz_records=qz_records_desc
                )
                st.download_button(
                    label="📄 Download Full Student Profile (PDF)",
                    data=pdf_buf,
                    file_name=f"{student_info['name']}_Profile.pdf".replace(" ", "_"),
                    mime="application/pdf",
                    use_container_width=True
                )
