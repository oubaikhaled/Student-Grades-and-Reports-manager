import streamlit as st
import pandas as pd
from pdf_utils import PDFGenerator

class StudentProfileView:
    def __init__(self, db_manager):
        self.db = db_manager

    def render(self):
        st.subheader("👤 Student Profiles")
        st.caption("Search for a student to view their complete profile, overall status, and grade history.")

        # Fetch all students for the search dropdown
        students_df = self.db.fetch_dataframe("SELECT id, name, phone, phone_parent, group_number FROM students ORDER BY name ASC")
        
        if students_df.empty:
            st.info("No students registered yet.")
            return

        # Search bar
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
            # Extract ID
            stu_id = selected_label.split("(ID: ")[1].replace(")", "")
            student_info = students_df[students_df['id'] == stu_id].iloc[0]

            st.divider()
            
            # Fetch Quiz Grades to calculate average
            qz_df = self.db.fetch_dataframe("""
                SELECT q.title, qg.score, q.max_score, qg.percentage
                FROM quiz_grades qg
                JOIN quizzes q ON qg.quiz_id = q.quiz_id
                WHERE qg.student_id = %s AND qg.score IS NOT NULL
                ORDER BY q.quiz_id DESC
            """, (stu_id,))

            # Calculate Status based on Quiz Average
            if not qz_df.empty and not qz_df['percentage'].isna().all():
                avg_quiz_perc = qz_df['percentage'].mean()
                if avg_quiz_perc > 85:
                    status = "🌟 Excellent Student"
                    status_color = "green"
                elif avg_quiz_perc > 60:
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

            # Display Profile Header
            col1, col2 = st.columns([2, 1])
            with col1:
                st.markdown(f"### {student_info['name']}")
                st.markdown(f"**ID:** {student_info['id']} | **Group:** {student_info['group_number'] if pd.notna(student_info['group_number']) else 'N/A'}")
                st.markdown(f"**Student Phone:** {student_info['phone'] if pd.notna(student_info['phone']) else 'N/A'} | **Parent Phone:** {student_info['phone_parent'] if pd.notna(student_info['phone_parent']) else 'N/A'}")
            with col2:
                st.markdown(f"<h4 style='text-align: center; color: {status_color};'>{status}</h4>", unsafe_allow_html=True)
                st.markdown(f"<p style='text-align: center;'>Quiz Average: <b>{avg_display}</b></p>", unsafe_allow_html=True)

            st.divider()

            # Display Grades Dataframes
            col_hw, col_qz = st.columns(2)
            
            hw_records = []
            qz_records = []

            with col_hw:
                st.subheader("📚 Homework History")
                hw_df = self.db.fetch_dataframe("""
                    SELECT h.title as "Homework", hg.correct_answers as "Score", h.total_questions as "Out Of", hg.percentage as "Percentage"
                    FROM homework_grades hg
                    JOIN homeworks h ON hg.homework_id = h.homework_id
                    WHERE hg.student_id = %s AND hg.correct_answers IS NOT NULL
                    ORDER BY h.homework_id DESC
                """, (stu_id,))
                
                if hw_df.empty:
                    st.info("No homework grades recorded.")
                else:
                    hw_df["Percentage"] = hw_df["Percentage"].apply(lambda x: f"{x:.1f}%" if pd.notna(x) else "N/A")
                    st.dataframe(hw_df, hide_index=True, use_container_width=True)
                    hw_records = hw_df.to_dict('records')

            with col_qz:
                st.subheader("📝 Quiz History")
                if qz_df.empty:
                    st.info("No quiz grades recorded.")
                else:
                    qz_display = qz_df.rename(columns={"title": "Quiz", "score": "Score", "max_score": "Out Of", "percentage": "Percentage"})
                    qz_display["Percentage"] = qz_display["Percentage"].apply(lambda x: f"{x:.1f}%" if pd.notna(x) else "N/A")
                    st.dataframe(qz_display, hide_index=True, use_container_width=True)
                    qz_records = qz_display.to_dict('records')

            # PDF Download Generation
            st.divider()
            col_b1, col_b2, col_b3 = st.columns([1, 2, 1])
            with col_b2:
                pdf_buf = PDFGenerator.generate_student_profile_report(
                    student_info=student_info.to_dict(),
                    status_text=status,
                    avg_display=avg_display,
                    hw_records=hw_records,
                    qz_records=qz_records
                )
                st.download_button(
                    label="📄 Download Full Student Profile (PDF)",
                    data=pdf_buf,
                    file_name=f"{student_info['name']}_Profile.pdf".replace(" ", "_"),
                    mime="application/pdf",
                    use_container_width=True
                )
