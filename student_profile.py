import streamlit as st
import pandas as pd
from pdf_utils import PDFGenerator

class StudentProfileView:
    def __init__(self, db_manager):
        self.db = db_manager

    def render(self):
        st.subheader("👤 Student Profiles")
        st.caption("Search for a student to view their complete profile, overall status, analytical trends, and grade history.")

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
            stu_id = selected_label.split("(ID: ")[1].replace(")", "")
            student_info = students_df[students_df['id'] == stu_id].iloc[0]

            # Fetch data in ASCENDING order (Oldest -> Newest) to build a chronological sequence
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

            # --- 1. Status Calculation ---
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

            # --- 2. Display Profile Header ---
            col1, col2 = st.columns([2, 1])
            with col1:
                st.markdown(f"### {student_info['name']}")
                st.markdown(f"**ID:** {student_info['id']} | **Group:** {student_info['group_number'] if pd.notna(student_info['group_number']) else 'N/A'}")
                st.markdown(f"**Student Phone:** {student_info['phone'] if pd.notna(student_info['phone']) else 'N/A'} | **Parent Phone:** {student_info['phone_parent'] if pd.notna(student_info['phone_parent']) else 'N/A'}")
            with col2:
                st.markdown(f"<h4 style='text-align: center; color: {status_color};'>{status}</h4>", unsafe_allow_html=True)
                st.markdown(f"<p style='text-align: center;'>Quiz Average: <b>{avg_display}</b></p>", unsafe_allow_html=True)

            st.divider()

            # --- 3. Chronological Mapping & Analytics Processing ---
            hws = hw_df.to_dict('records') if not hw_df.empty else []
            qzs = qz_df.to_dict('records') if not qz_df.empty else []
            
            max_len = max(len(hws), len(qzs))
            paired_data = []
            weaknesses = []
            all_percs = []
            
            for i in range(max_len):
                hw = hws[i] if i < len(hws) else None
                qz = qzs[i] if i < len(qzs) else None
                
                # Assume homework title holds the core lesson name; fallback to quiz title
                lesson_name = hw['Homework'] if hw else (qz['Quiz'] if qz else f"Sequence {i+1}")
                hw_perc = hw['Percentage'] if hw else None
                qz_perc = qz['Percentage'] if qz else None
                
                paired_data.append({"Lesson": lesson_name, "Homework": hw_perc, "Quiz": qz_perc})
                
                # Gather weaknesses and trends
                if hw and pd.notna(hw_perc):
                    all_percs.append(hw_perc)
                    if hw_perc < 70:
                        weaknesses.append({"Topic": f"{lesson_name} (HW)", "Percentage": hw_perc})
                if qz and pd.notna(qz_perc):
                    all_percs.append(qz_perc)
                    if qz_perc < 70:
                        weaknesses.append({"Topic": f"{lesson_name} (Quiz)", "Percentage": qz_perc})

            # --- 4. Unified Progress Graph ---
            st.subheader("📈 Unified Performance Timeline")
            if paired_data:
                chart_df = pd.DataFrame(paired_data).set_index("Lesson")
                st.line_chart(chart_df[['Homework', 'Quiz']])
            else:
                st.info("Not enough data to plot a timeline yet.")

            st.divider()

            # --- 5. Automated Analytics Section ---
            col_w, col_t = st.columns(2)
            
            with col_w:
                st.subheader("🎯 Priority Focus Areas")
                st.caption("Top 3 weakest topics (Below 70%)")
                if weaknesses:
                    # Sort ascending to isolate the absolute lowest scores
                    weaknesses = sorted(weaknesses, key=lambda x: x['Percentage'])
                    for w in weaknesses[:3]:
                        st.error(f"**{w['Topic']}**: {w['Percentage']:.1f}%")
                else:
                    st.success("No critical weaknesses detected! All assignments are at 70% or above.")

            with col_t:
                st.subheader("🤖 Automated Trend Analysis")
                hw_avg = hw_df['Percentage'].mean() if not hw_df.empty else 0
                qz_avg = qz_df['Percentage'].mean() if not qz_df.empty else 0
                
                trend_notes = []
                
                # Check for HW vs Quiz gap
                if hw_avg >= 85 and qz_avg < 70:
                    trend_notes.append("⚠️ **Practice vs. Test Gap:** High homework completion but quiz performance is struggling. Focus on time-management and solving problems completely independently.")
                
                # Momentum detection
                if len(all_percs) >= 5:
                    overall_avg = sum(all_percs) / len(all_percs)
                    recent_avg = sum(all_percs[-3:]) / 3  # Average of the last 3 assignments recorded
                    
                    if recent_avg < overall_avg - 10:
                        trend_notes.append("📉 **Recent Downward Trend:** A drop in recent scores was detected. Please review the material from the last few lessons immediately.")
                    elif recent_avg > overall_avg + 10:
                        trend_notes.append("📈 **Recent Upward Trend:** Great job! Recent scores show strong momentum and improvement.")
                        
                if not trend_notes:
                    trend_notes.append("✅ **Consistent Performance:** The student is maintaining a steady trajectory based on current data.")
                    
                for note in trend_notes:
                    st.info(note)

            st.divider()

            # --- 6. Detailed Tables (Reversed for Newest-First View) ---
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

            # --- 7. PDF Download ---
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
