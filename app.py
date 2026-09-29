import urllib.parse
import streamlit as st
import pandas as pd
import psycopg2
import re
from database import DatabaseManager
from pdf_utils import PDFGenerator
from auth import AuthManager
import emoji
from student_profile import StudentProfileView

st.set_page_config(page_title="Eng.Mahmoud Adel Grade Portal", layout="wide")

class GradePortalApp:
    def __init__(self):
        self.db = DatabaseManager(st.secrets["DATABASE_URL"])
        self.auth = AuthManager()
        self.db.init_db()

    def run(self):
        if 'logged_in' not in st.session_state or not st.session_state.logged_in:
            self._render_login()
        elif st.session_state.role == "parent":
            self._render_parent_portal()
        elif st.session_state.role == "admin":
            self._render_admin_portal()

    def _render_login(self):
        st.title("📚 Eng.Mahmoud Adel Grade Portal")
        tab_parent, tab_admin = st.tabs(["👨‍👩‍👧 Parent Access", "🔐 Admin Access"])

        with tab_parent:
            st.subheader("View Student Grades")
            parent_phone = st.text_input("Parent Phone Number", placeholder="e.g. 01030007000").strip()
            if st.button("View Grades", type="primary") and parent_phone:
                self.auth.login_parent(self.db, parent_phone)

        with tab_admin:
            st.subheader("Teacher & Admin Login")
            admin_num = st.text_input("Admin Number")
            admin_pass = st.text_input("Password", type="password")
            if st.button("Login as Admin", type="primary"):
                self.auth.login_admin(admin_num, admin_pass)

    def _render_parent_portal(self):
        st.sidebar.button("🚪 Logout", on_click=self.auth.logout, type="primary")
        st.title("📊 Student Grades Overview")

        students_df = self.db.fetch_dataframe(
            "SELECT id, name FROM students WHERE phone_parent = %s",
            (st.session_state.identifier,)
        )

        for _, student in students_df.iterrows():
            st.subheader(f"🎓 {student['name']}")
            hw_df = self.db.fetch_dataframe("""
                SELECT h.title as "Assignment", g.correct_answers as "Score", h.total_questions as "Out Of", 
                       g.percentage as "Percentage", g.report as "Report", g.report_image as "Image"
                FROM homework_grades g JOIN homeworks h ON g.homework_id = h.homework_id
                WHERE g.student_id = %s AND g.correct_answers IS NOT NULL ORDER BY h.homework_id DESC
            """, (str(student['id']),))

            qz_df = self.db.fetch_dataframe("""
                SELECT q.title as "Quiz", g.score as "Score", q.max_score as "Out Of", 
                       g.percentage as "Percentage", g.report as "Report", g.report_image as "Image"
                FROM quiz_grades g JOIN quizzes q ON g.quiz_id = q.quiz_id
                WHERE g.student_id = %s AND g.score IS NOT NULL ORDER BY q.quiz_id DESC
            """, (str(student['id']),))

            col1, col2 = st.columns(2)
            with col1:
                st.markdown("**Homework Assignments**")
                if hw_df.empty:
                    st.info("No homework grades recorded yet.")
                else:
                    st.dataframe(hw_df[["Assignment", "Score", "Out Of", "Percentage"]], hide_index=True,
                                 use_container_width=True)

                    st.markdown("**📄 Download Feedback Report**")
                    sel_hw = st.selectbox("Select Assignment", hw_df["Assignment"].tolist(), key=f"dl_{student['id']}")
                    if sel_hw:
                        row = hw_df[hw_df["Assignment"] == sel_hw].iloc[0]
                        img_bytes = bytes(row["Image"]) if pd.notna(row.get("Image")) and row["Image"] else None

                        pdf_buf = PDFGenerator.generate_student_report(
                            student["name"], sel_hw, row["Score"], row["Out Of"],
                            row["Percentage"], row["Report"], img_bytes
                        )

                        st.download_button(
                            label=f"Download {sel_hw} Report", data=pdf_buf,
                            file_name=f"{student['name']}_{sel_hw}_Report.pdf".replace(" ", "_"),
                            mime="application/pdf", key=f"btn_{student['id']}", use_container_width=True
                        )

            with col2:
                st.markdown("**Quiz Scores**")
                if qz_df.empty:
                    st.info("No quiz grades recorded yet.")
                else:
                    st.dataframe(qz_df[["Quiz", "Score", "Out Of", "Percentage"]], hide_index=True, use_container_width=True)

                    st.markdown("**📄 Download Feedback Report**")
                    sel_qz = st.selectbox("Select Quiz", qz_df["Quiz"].tolist(), key=f"dl_qz_{student['id']}")
                    if sel_qz:
                        row_qz = qz_df[qz_df["Quiz"] == sel_qz].iloc[0]
                        img_bytes_qz = bytes(row_qz["Image"]) if pd.notna(row_qz.get("Image")) and row_qz["Image"] else None

                        pdf_buf_qz = PDFGenerator.generate_student_report(
                            student["name"], sel_qz, row_qz["Score"], row_qz["Out Of"],
                            row_qz["Percentage"], row_qz["Report"], img_bytes_qz
                        )

                        st.download_button(
                            label=f"Download {sel_qz} Report", data=pdf_buf_qz,
                            file_name=f"{student['name']}_{sel_qz}_Report.pdf".replace(" ", "_"),
                            mime="application/pdf", key=f"btn_qz_{student['id']}", use_container_width=True
                        )
            st.divider()

    def _render_admin_portal(self):
        st.sidebar.button("🚪 Logout", on_click=self.auth.logout, type="primary")
        st.title("📚 Homework & Grade Manager")
        st.title("Eng.Mahmoud Adel Grade Portal")
        menu = st.sidebar.radio("Navigation",
                                ["Manage Homeworks", "Manage Quizzes", 
                                 "Manage Students", "Student Profiles", "WhatsApp Students and parents"])

        if menu == "Manage Homeworks":
            self._admin_manage_homeworks()
        elif menu == "Manage Quizzes":
            self._admin_record_quizzes()
        elif menu == "Manage Students":
            self._admin_manage_students()
        elif menu == "Student Profiles":
            StudentProfileView(self.db).render()
        elif menu == "WhatsApp Students and parents":
            self._admin_whatsapp_parents()

    def _admin_manage_homeworks(self):
        st.subheader("📚 Manage Curriculum & Homeworks")
        
        tab_hw, tab_ch = st.tabs(["📝 Homeworks", "📑 Manage Chapters"])
        
        # --- CHAPTER TAB ---
        with tab_ch:
            with st.form("add_chapter_form"):
                title = st.text_input("Chapter Title (e.g., 'Unit 1: Integration')").strip()
                if st.form_submit_button("➕ Add Chapter", type="primary") and title:
                    try:
                        with self.db.get_connection() as conn:
                            with conn.cursor() as c:
                                c.execute("INSERT INTO chapters (title) VALUES (%s)", (title,))
                            conn.commit()
                        st.success(f"Chapter '{title}' added!")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error: {e}")

            ch_df = self.db.fetch_dataframe("SELECT chapter_id, title FROM chapters ORDER BY chapter_id ASC")
            if not ch_df.empty:
                st.divider()
                st.dataframe(ch_df, hide_index=True, use_container_width=True)
                with st.expander("⚠️ Delete a Chapter"):
                    ch_to_delete = st.selectbox("Select Chapter to Delete", ch_df['title'].tolist())
                    if st.button("🚨 Delete Chapter"):
                        del_id = ch_df[ch_df['title'] == ch_to_delete].iloc[0]['chapter_id']
                        with self.db.get_connection() as conn:
                            with conn.cursor() as c:
                                c.execute("DELETE FROM chapters WHERE chapter_id = %s", (int(del_id),))
                            conn.commit()
                        st.rerun()

        # --- HOMEWORK TAB ---
        with tab_hw:
            if ch_df.empty:
                st.warning("Please create at least one Chapter in the 'Manage Chapters' tab first.")
                return
            chapter_options = ch_df.apply(lambda x: f"{x['title']} (ID: {x['chapter_id']})", axis=1).tolist()
            
            # 1. ADD / DELETE SECTION
            with st.expander("🛠️ Add or Delete Homework Assignments", expanded=False):
                with st.form("add_homework_form"):
                    sel_chapter = st.selectbox("Assign to Chapter", chapter_options)
                    title = st.text_input("Homework Title")
                    total_q = st.number_input("Total Questions (Max Score)", min_value=1, step=1)
                    video_link = st.text_input("Homework Video Link (Optional)", placeholder="https://youtube.com/...")

                    if st.form_submit_button("➕ Add Homework", type="primary"):
                        if title.strip():
                            try:
                                clean_link = video_link.strip() if video_link.strip() else None
                                ch_id = sel_chapter.split("(ID: ")[1].replace(")", "")
                                with self.db.get_connection() as conn:
                                    with conn.cursor() as c:
                                        c.execute("INSERT INTO homeworks (title, total_questions, video_link, chapter_id) VALUES (%s, %s, %s, %s)", 
                                                  (title.strip(), total_q, clean_link, ch_id))
                                    conn.commit()
                                st.rerun()
                            except Exception as e:
                                st.error(f"Error: {e}")
                
                hw_df_all = self.db.fetch_dataframe("SELECT homework_id, title FROM homeworks ORDER BY homework_id DESC")
                if not hw_df_all.empty:
                    st.divider()
                    del_hw_title = st.selectbox("Select Homework to Delete", hw_df_all["title"].tolist())
                    if st.button("🚨 Delete Selected Homework"):
                        hw_del_id = hw_df_all[hw_df_all["title"] == del_hw_title].iloc[0]["homework_id"]
                        with self.db.get_connection() as conn:
                            with conn.cursor() as c:
                                c.execute("DELETE FROM homework_grades WHERE homework_id = %s", (int(hw_del_id),))
                                c.execute("DELETE FROM homeworks WHERE homework_id = %s", (int(hw_del_id),))
                            conn.commit()
                        st.rerun()

            hw_df = self.db.fetch_dataframe("SELECT homework_id, title, total_questions FROM homeworks ORDER BY homework_id DESC")
            if hw_df.empty:
                st.info("No homeworks created yet.")
                return

            st.divider()
            
            # 2. SELECT ASSIGNMENT TO GRADE
            st.subheader("📊 Select Assignment to Grade")
            sel_hw_title = st.selectbox("Current Homework", hw_df["title"].tolist())
            hw_row = hw_df[hw_df["title"] == sel_hw_title].iloc[0]
            hw_id, total_q = int(hw_row["homework_id"]), int(hw_row["total_questions"])

            grades_df = self.db.fetch_dataframe("""
                SELECT s.id, s.name, s.phone, s.group_number, g.correct_answers, g.report 
                FROM students s
                LEFT JOIN homework_grades g ON s.id = g.student_id AND g.homework_id = %s ORDER BY s.name ASC
            """, (hw_id,))

            st.divider()

            # 3. STUDENT FILTER & SELECTOR
            st.subheader("🔍 Select Student")
            col_f1, col_f2 = st.columns(2)
            
            available_groups = [g for g in grades_df['group_number'].dropna().unique() if str(g).strip()]
            filter_group = col_f1.selectbox("Filter by Group", ["All Groups"] + sorted(available_groups))
            search_term = col_f2.text_input("Search Name, ID, or Phone").strip()

            filtered_df = grades_df
            if filter_group != "All Groups":
                filtered_df = filtered_df[filtered_df['group_number'] == filter_group]
            if search_term:
                mask = (
                    filtered_df['name'].str.contains(search_term, case=False, na=False) |
                    filtered_df['id'].astype(str).str.contains(search_term, case=False, na=False) |
                    filtered_df['phone'].astype(str).str.contains(search_term, case=False, na=False)
                )
                filtered_df = filtered_df[mask]

            if filtered_df.empty:
                st.warning("No students found matching your filters.")
                return

            student_options = filtered_df.apply(lambda x: f"{x['name']} (Score: {x['correct_answers'] if pd.notna(x['correct_answers']) else 'Missing'})", axis=1).tolist()
            selected_student_label = st.selectbox("Select Student to Open Grading Panel", student_options)
            
            selected_student_name = selected_student_label.split(" (Score")[0]
            student_data = filtered_df[filtered_df['name'] == selected_student_name].iloc[0]

            if st.button("✏️ Open Grading Panel", type="primary"):
                self._hw_grading_dialog(
                    student_data['name'], student_data['id'], hw_id, total_q, 
                    student_data['correct_answers'], student_data['report']
                )

            st.divider()
            
            # Master PDF Download mapped to current group filter
            pdf_data = [(r["id"], r["name"], r["correct_answers"] if pd.notna(r["correct_answers"]) else None,
                         (float(r["correct_answers"]) / total_q) * 100 if pd.notna(r["correct_answers"]) else None) for
                        _, r in filtered_df.iterrows()]
            pdf_buf = PDFGenerator.generate_master_report(f"{sel_hw_title} ({filter_group})", total_q, pdf_data)
            st.download_button("📄 Download Master PDF Report", data=pdf_buf, file_name=f"{sel_hw_title}_{filter_group}_Master.pdf".replace(' ', '_'), mime="application/pdf")

    def _admin_record_quizzes(self):
        st.subheader("📝 Manage Curriculum & Quizzes")
        
        ch_df = self.db.fetch_dataframe("SELECT chapter_id, title FROM chapters ORDER BY chapter_id ASC")
        if ch_df.empty:
            st.warning("Please create at least one Chapter in 'Manage Homeworks' -> 'Manage Chapters' tab first.")
            return
        chapter_options = ch_df.apply(lambda x: f"{x['title']} (ID: {x['chapter_id']})", axis=1).tolist()
        
        # 1. ADD / DELETE SECTION
        with st.expander("🛠️ Add or Delete Quizzes", expanded=False):
            with st.form("create_quiz_form"):
                sel_chapter = st.selectbox("Assign to Chapter", chapter_options)
                q_title = st.text_input("Quiz Title").strip()
                q_max = st.number_input("Maximum Score", min_value=1.0, value=10.0, step=1.0)
                if st.form_submit_button("➕ Create Quiz", type="primary") and q_title:
                    try:
                        ch_id = sel_chapter.split("(ID: ")[1].replace(")", "")
                        with self.db.get_connection() as conn:
                            with conn.cursor() as c:
                                c.execute("INSERT INTO quizzes (title, max_score, chapter_id) VALUES (%s, %s, %s) RETURNING quiz_id",
                                          (q_title, q_max, ch_id))
                                q_id = c.fetchone()[0]
                                c.execute("SELECT id FROM students")
                                for (sid,) in c.fetchall():
                                    c.execute("INSERT INTO quiz_grades (quiz_id, student_id) VALUES (%s, %s)", (q_id, sid))
                            conn.commit()
                        st.rerun()
                    except psycopg2.IntegrityError:
                        st.error("Quiz already exists.")
            
            qz_df_all = self.db.fetch_dataframe("SELECT quiz_id, title FROM quizzes ORDER BY quiz_id DESC")
            if not qz_df_all.empty:
                st.divider()
                del_qz_title = st.selectbox("Select Quiz to Delete", qz_df_all["title"].tolist())
                if st.button("🚨 Delete Selected Quiz"):
                    qz_del_id = qz_df_all[qz_df_all["title"] == del_qz_title].iloc[0]["quiz_id"]
                    with self.db.get_connection() as conn:
                        with conn.cursor() as c:
                            c.execute("DELETE FROM quiz_grades WHERE quiz_id = %s", (int(qz_del_id),))
                            c.execute("DELETE FROM quizzes WHERE quiz_id = %s", (int(qz_del_id),))
                        conn.commit()
                    st.rerun()

        qz_df = self.db.fetch_dataframe("SELECT quiz_id, title, max_score FROM quizzes ORDER BY quiz_id DESC")
        if qz_df.empty:
            st.info("No quizzes created yet.")
            return

        st.divider()

        # 2. SELECT ASSIGNMENT TO GRADE
        st.subheader("📊 Select Quiz to Grade")
        sel_q = st.selectbox("Current Quiz", qz_df["title"].tolist())
        q_row = qz_df[qz_df["title"] == sel_q].iloc[0]
        q_id, q_max = int(q_row["quiz_id"]), float(q_row["max_score"])

        grades_df = self.db.fetch_dataframe("""
            SELECT s.id, s.name, s.phone, s.group_number, qg.score, qg.report 
            FROM students s 
            LEFT JOIN quiz_grades qg ON s.id = qg.student_id AND qg.quiz_id = %s 
            ORDER BY s.name ASC
        """, (q_id,))

        st.divider()

        # 3. STUDENT FILTER & SELECTOR
        st.subheader("🔍 Select Student")
        col_f1, col_f2 = st.columns(2)
        
        available_groups = [g for g in grades_df['group_number'].dropna().unique() if str(g).strip()]
        filter_group = col_f1.selectbox("Filter by Group", ["All Groups"] + sorted(available_groups), key="qz_grp")
        search_term = col_f2.text_input("Search Name, ID, or Phone", key="qz_srch").strip()

        filtered_df = grades_df
        if filter_group != "All Groups":
            filtered_df = filtered_df[filtered_df['group_number'] == filter_group]
        if search_term:
            mask = (
                filtered_df['name'].str.contains(search_term, case=False, na=False) |
                filtered_df['id'].astype(str).str.contains(search_term, case=False, na=False) |
                filtered_df['phone'].astype(str).str.contains(search_term, case=False, na=False)
            )
            filtered_df = filtered_df[mask]

        if filtered_df.empty:
            st.warning("No students found matching your filters.")
            return

        student_options = filtered_df.apply(lambda x: f"{x['name']} (Score: {x['score'] if pd.notna(x['score']) else 'Missing'})", axis=1).tolist()
        selected_student_label = st.selectbox("Select Student to Open Grading Panel", student_options, key="qz_sel")
        
        selected_student_name = selected_student_label.split(" (Score")[0]
        student_data = filtered_df[filtered_df['name'] == selected_student_name].iloc[0]

        if st.button("✏️ Open Grading Panel", type="primary", key="qz_btn"):
            self._qz_grading_dialog(
                student_data['name'], student_data['id'], q_id, q_max, 
                student_data['score'], student_data['report']
            )

        st.divider()
        
        pdf_data = [(r["id"], r["name"], r["score"] if pd.notna(r["score"]) else None,
                     (float(r["score"]) / q_max) * 100 if pd.notna(r["score"]) else None) for
                    _, r in filtered_df.iterrows()]
        pdf_buf = PDFGenerator.generate_master_report(f"{sel_q} ({filter_group})", q_max, pdf_data)
        st.download_button("📄 Download Master PDF Report", data=pdf_buf, file_name=f"{sel_q}_{filter_group}_Master.pdf".replace(' ', '_'), mime="application/pdf")
    def _admin_manage_students(self):
        st.subheader("👥 Manage Students")

        # --- RESTORED: ADD STUDENT FEATURE ---
        with st.expander("➕ Add New Student", expanded=False):
            with st.form("add_student_form"):
                col1, col2 = st.columns(2)
                with col1:
                    add_id = st.text_input("Student ID (Unique)")
                    add_name = st.text_input("Student Name")
                    add_group = st.text_input("Group Number (Optional)")
                with col2:
                    add_phone = st.text_input("Student Phone (Optional)", placeholder="010...")
                    add_parent_phone = st.text_input("Parent Phone", placeholder="010...")

                if st.form_submit_button("💾 Register Student", type="primary"):
                    if not add_id.strip() or not add_name.strip():
                        st.error("Student ID and Name are required.")
                    else:
                        try:
                            with self.db.get_connection() as conn:
                                with conn.cursor() as c:
                                    c.execute(
                                        "INSERT INTO students (id, name, phone, phone_parent, group_number) VALUES (%s, %s, %s, %s, %s)",
                                        (add_id.strip(), add_name.strip(), add_phone.strip(), add_parent_phone.strip(), add_group.strip())
                                    )
                                conn.commit()
                            st.success(f"Student '{add_name}' added successfully!")
                            st.rerun()
                        except psycopg2.IntegrityError:
                            st.error(f"Error: A student with ID '{add_id}' already exists.")
                        except Exception as e:
                            st.error(f"Failed to add student. Error: {e}")

        st.divider()

        # Fetch current students
        students_df = self.db.fetch_dataframe("SELECT id, name, phone, phone_parent, group_number FROM students ORDER BY name ASC")

        if students_df.empty:
            st.info("No students registered yet.")
            return

        st.caption("Current Enrolled Students")
        st.dataframe(students_df, hide_index=True, use_container_width=True)

        st.divider()

        # Real-time search filter for expandability
        st.subheader("🛠️ Modify Student Records")
        search_term = st.text_input("🔍 Search Student by Name or ID", placeholder="Start typing to filter the dropdowns below...").strip()

        if search_term:
            # Filter the dataframe dynamically (case-insensitive)
            mask = (
                students_df['name'].str.contains(search_term, case=False, na=False) |
                students_df['id'].astype(str).str.contains(search_term, case=False, na=False)
            )
            filtered_df = students_df[mask]
        else:
            filtered_df = students_df

        if filtered_df.empty:
            st.warning("No students match your search criteria.")
            return

        # Generate options based ONLY on the filtered results
        student_options = filtered_df.apply(lambda x: f"{x['name']} (ID: {x['id']})", axis=1).tolist()

        # Edit Student Data
        with st.expander("✏️ Edit Student Data"):
            selected_edit_label = st.selectbox("Select Student to Edit", student_options, key="edit_student_sel_v2")

            if selected_edit_label:
                edit_id = selected_edit_label.split("(ID: ")[1].replace(")", "")
                student_row = students_df[students_df['id'] == edit_id].iloc[0]

                with st.form("edit_student_form_v2"):
                    new_id = st.text_input("Student ID", value=str(student_row['id']))
                    new_name = st.text_input("Student Name", value=str(student_row['name']))
                    new_phone = st.text_input("Student Phone", value=str(student_row['phone']) if pd.notna(student_row['phone']) else "")
                    new_parent_phone = st.text_input("Parent Phone", value=str(student_row['phone_parent']) if pd.notna(student_row['phone_parent']) else "")
                    new_group = st.text_input("Group Number", value=str(student_row['group_number']) if pd.notna(student_row['group_number']) else "")

                    if st.form_submit_button("💾 Save Changes", type="primary"):
                        new_id = new_id.strip()
                        if not new_name.strip() or not new_id:
                            st.error("Student ID and Name cannot be empty.")
                        else:
                            try:
                                with self.db.get_connection() as conn:
                                    with conn.cursor() as c:
                                        if new_id != edit_id:
                                            # Check if the new ID is already taken
                                            c.execute("SELECT id FROM students WHERE id = %s", (new_id,))
                                            if c.fetchone():
                                                st.error(f"The ID {new_id} is already assigned to another student.")
                                                st.stop()

                                            # Safe transfer
                                            c.execute("INSERT INTO students (id, name, phone, phone_parent, group_number) VALUES (%s, %s, %s, %s, %s)", 
                                                      (new_id, new_name, new_phone, new_parent_phone, new_group))
                                            c.execute("UPDATE homework_grades SET student_id = %s WHERE student_id = %s", (new_id, edit_id))
                                            c.execute("UPDATE quiz_grades SET student_id = %s WHERE student_id = %s", (new_id, edit_id))
                                            c.execute("DELETE FROM students WHERE id = %s", (edit_id,))
                                        else:
                                            c.execute("""
                                                UPDATE students 
                                                SET name = %s, phone = %s, phone_parent = %s, group_number = %s 
                                                WHERE id = %s
                                            """, (new_name, new_phone, new_parent_phone, new_group, edit_id))
                                    conn.commit()
                                st.success("Student details updated successfully!")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Failed to update student. Error: {e}")

        # Danger Zone for Deletion
        with st.expander("⚠️ Danger Zone: Delete Student"):
            st.warning("Deleting a student will permanently remove all their recorded homework and quiz grades. This action cannot be undone.")

            selected_delete_label = st.selectbox("Select Student to Delete", student_options, key="delete_student_sel_v2")

            if st.button("🚨 Yes, Permanently Delete Student"):
                delete_id = selected_delete_label.split("(ID: ")[1].replace(")", "")

                try:
                    with self.db.get_connection() as conn:
                        with conn.cursor() as c:
                            c.execute("DELETE FROM homework_grades WHERE student_id = %s", (delete_id,))
                            c.execute("DELETE FROM quiz_grades WHERE student_id = %s", (delete_id,))
                            c.execute("DELETE FROM students WHERE id = %s", (delete_id,))
                        conn.commit()

                    st.success("Student successfully deleted!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to delete student. Error: {e}")                

    def _admin_whatsapp_parents(self):
        st.subheader("💬 WhatsApp & Report Broadcasting")
        st.caption("Bulk download PDFs and message parents or students directly for graded or missing assignments.")
        
        type_choice = st.radio("Select Assignment Type", ["Homework", "Quiz"], horizontal=True)
        
        # --- NEW: Filter by Chapter First ---
        ch_df = self.db.fetch_dataframe("SELECT chapter_id, title FROM chapters ORDER BY chapter_id ASC")
        if ch_df.empty:
            st.warning("No chapters exist yet. Please create a chapter in 'Manage Homeworks' first.")
            return
            
        filter_chapter = st.selectbox("📂 Filter by Chapter", ch_df['title'].tolist())
        selected_ch_id = ch_df[ch_df['title'] == filter_chapter].iloc[0]['chapter_id']
        
        if type_choice == "Homework":
            hw_df = self.db.fetch_dataframe("SELECT homework_id, title, total_questions, video_link FROM homeworks WHERE chapter_id = %s ORDER BY homework_id DESC", (int(selected_ch_id),))
            if hw_df.empty:
                st.info(f"No homeworks found in chapter '{filter_chapter}'.")
                return
            
            sel_title = st.selectbox("Select Homework", hw_df["title"].tolist())
            hw_row = hw_df[hw_df["title"] == sel_title].iloc[0]
            item_id = int(hw_row["homework_id"])
            total_q = int(hw_row["total_questions"])
            
            vid_link = hw_row["video_link"] if "video_link" in hw_row and pd.notna(hw_row["video_link"]) and str(hw_row["video_link"]).strip() else None
            
            # Using LEFT JOIN to fetch ALL students, even those without grades
            grades_df = self.db.fetch_dataframe("""
                SELECT s.id, s.name, s.phone, s.phone_parent, s.group_number, g.correct_answers as score, g.percentage, g.report, g.report_image 
                FROM students s
                LEFT JOIN homework_grades g ON s.id = g.student_id AND g.homework_id = %s
                ORDER BY s.group_number ASC, s.name ASC
            """, (item_id,))
            
        else:
            qz_df = self.db.fetch_dataframe("SELECT quiz_id, title, max_score FROM quizzes WHERE chapter_id = %s ORDER BY quiz_id DESC", (int(selected_ch_id),))
            if qz_df.empty:
                st.info(f"No quizzes found in chapter '{filter_chapter}'.")
                return
                
            sel_title = st.selectbox("Select Quiz", qz_df["title"].tolist())
            qz_row = qz_df[qz_df["title"] == sel_title].iloc[0]
            item_id = int(qz_row["quiz_id"])
            total_q = float(qz_row["max_score"])
            vid_link = None
            
            # Using LEFT JOIN to fetch ALL students, even those without grades
            grades_df = self.db.fetch_dataframe("""
                SELECT s.id, s.name, s.phone, s.phone_parent, s.group_number, g.score, g.percentage, g.report, g.report_image 
                FROM students s
                LEFT JOIN quiz_grades g ON s.id = g.student_id AND g.quiz_id = %s
                ORDER BY s.group_number ASC, s.name ASC
            """, (item_id,))
            
        if grades_df.empty:
            st.warning("No students found in the database.")
            return
            
        available_groups = [g for g in grades_df['group_number'].dropna().unique() if str(g).strip()]
        if available_groups:
            filter_group = st.selectbox("Filter Broadcasting by Group", ["All Groups"] + sorted(available_groups))
            if filter_group != "All Groups":
                grades_df = grades_df[grades_df['group_number'] == filter_group]

        st.success(f"Found {len(grades_df)} students in this selection.")
        st.divider()
        
        def clean_number(p_str):
            if pd.isna(p_str) or str(p_str).strip() in ["", "None", "nan"]: return None
            c_phone = re.sub(r'\D', '', str(p_str))
            if not c_phone: return None
            if len(c_phone) == 10 and c_phone.startswith("1"): return "20" + c_phone
            elif c_phone.startswith("0"): return "2" + c_phone
            elif not c_phone.startswith("20"): return "20" + c_phone
            return c_phone
        
        for _, row in grades_df.iterrows():
            col1, col2, col3, col4 = st.columns([3, 2, 2, 4]) 
            
            group_label = f" *(Group: {row['group_number']})*" if pd.notna(row['group_number']) and str(row['group_number']).strip() else ""
            col1.markdown(f"**{row['name']}**{group_label}")
            
            # Check if the student actually has a score submitted
            has_score = pd.notna(row['score'])
            
            if has_score:
                col2.write(f"Score: **{row['score']}** / {total_q}")
            else:
                col2.markdown("Score: ❌ **Missing**")
            
            with col3:
                if has_score:
                    img_bytes = bytes(row["report_image"]) if pd.notna(row.get("report_image")) and row["report_image"] else None
                    pdf_buf = PDFGenerator.generate_student_report(
                        row["name"], sel_title, row["score"], total_q, 
                        row["percentage"], row.get("report"), img_bytes,
                        video_link=vid_link
                    )
                        
                    st.download_button(
                        label="📄 Download", 
                        data=pdf_buf,
                        file_name=f"{row['name']}_{sel_title}.pdf".replace(" ", "_"),
                        mime="application/pdf", 
                        key=f"dl_{type_choice}_{row['id']}",
                        use_container_width=True
                    )
                else:
                    st.button("📄 N/A", disabled=True, key=f"dl_na_{type_choice}_{row['id']}", use_container_width=True)
                
            with col4:
                # If they have a score, send the standard grading message
                if has_score:
                    type_ar = "Quiz" if type_choice == "Quiz" else "Homework"
                    
                    wa_msg_parent = emoji.emojize(
                        f":bar_chart: درجة الـ {sel_title}\n\n"
                        f"ولي الأمر الكريم،\n"
                        f"نحيط حضرتكم علمًا بأن الطالب {row['name']} حصل على {row['score']} / {total_q} في الـ {type_ar} الأخير.\n\n"
                        f"نتمنى له مزيدًا من التقدم والنجاح، ونسعى دائمًا لمتابعة مستوى الطالب بشكل مستمر وتحسين نقاط الضعف أولًا بأول. :glowing_star:\n\n"
                        f"Mathematics Team – Mahmoud Adel"
                    )
                    
                    wa_msg_student = emoji.emojize(
                        f":bar_chart: درجة الـ {sel_title}\n\n"
                        f"أهلاً بك يا {row['name']}،\n"
                        f"لقد حصلت على {row['score']} / {total_q} في الـ {type_ar} الأخير.\n\n"
                        f"استمر في المذاكرة والتدريب، ونتمنى لك دوام التفوق والنجاح! :glowing_star:\n\n"
                        f"Mathematics Team – Mahmoud Adel"
                    )
                
                # If they DO NOT have a score, send the separated warning messages
                else:
                    if type_choice == "Homework":
                        wa_msg_parent = emoji.emojize(
                            f":bar_chart: homework\n\n"
                            f"نحيط حضرتكم علمًا بأن\n"
                            f"الطالب: {row['name']}\n"
                            f"لم يقم بأداء واجب  : {sel_title}\n\n"
                            f"برجاء الالتزام بحضور وأداء الواجبات في المواعيد المحددة، ومتابعة جميع التقييمات أولًا بأول.\n\n"
                            f"Mathematics Team – Mahmoud Adel"
                        )
                        wa_msg_student = emoji.emojize(
                            f":bell: تذكير بـ homework\n\n"
                            f"أهلاً بك يا {row['name']}،\n"
                            f"نذكرك بأنه لم يتم تسجيل أداءك في واجب : {sel_title}\n\n"
                            f"برجاء سرعة إتمام الواجب والالتزام بالمواعيد المحددة.\n\n"
                            f"Mathematics Team – Mahmoud Adel"
                        )
                    else:
                        wa_msg_parent = emoji.emojize(
                            f":bar_chart:  quiz\n\n"
                            f"نحيط حضرتكم علمًا بأن\n"
                            f"الطالب: {row['name']}\n"
                            f"لم يقم بأداء امتحان : {sel_title}\n\n"
                            f"برجاء الالتزام بحضور وأداء الاختبارات في المواعيد المحددة، ومتابعة جميع التقييمات أولًا بأول.\n\n"
                            f"Mathematics Team – Mahmoud Adel"
                        )
                        wa_msg_student = emoji.emojize(
                            f":bell: تذكير بـ quiz\n\n"
                            f"أهلاً بك يا {row['name']}،\n"
                            f"نذكرك بأنه لم يتم تسجيل أداءك في كويز : {sel_title}\n\n"
                            f"برجاء سرعة إتمام الاختبار والالتزام بالمواعيد المحددة.\n\n"
                            f"Mathematics Team – Mahmoud Adel"
                        )

                encoded_msg_parent = urllib.parse.quote(wa_msg_parent)
                encoded_msg_student = urllib.parse.quote(wa_msg_student)
                
                parent_num = clean_number(row["phone_parent"])
                student_num = clean_number(row["phone"])
                
                sub1, sub2 = st.columns(2)
                with sub1:
                    if parent_num:
                        st.link_button("👨‍👩‍👦 Parent", f"https://api.whatsapp.com/send?phone={parent_num}&text={encoded_msg_parent}", key=f"wa_p_{type_choice}_{row['id']}", use_container_width=True)
                    else:
                        st.button("👨‍👩‍👦 N/A", disabled=True, key=f"wa_p_na_{type_choice}_{row['id']}", use_container_width=True)
                        
                with sub2:
                    if student_num:
                        st.link_button("🎓 Student", f"https://api.whatsapp.com/send?phone={student_num}&text={encoded_msg_student}", key=f"wa_s_{type_choice}_{row['id']}", use_container_width=True)
                    else:
                        st.button("🎓 N/A", disabled=True, key=f"wa_s_na_{type_choice}_{row['id']}", use_container_width=True)
            
            
            st.divider()
def _stitch_images(self, uploaded_files):
        if not uploaded_files:
            return None
        from PIL import Image as PILImage
        import io
        try:
            images = [PILImage.open(f) for f in uploaded_files]
            widths, heights = zip(*(i.size for i in images))
            total_height = sum(heights)
            max_width = max(widths)
            stitched_img = PILImage.new('RGB', (max_width, total_height), color=(255, 255, 255))
            y_offset = 0
            for im in images:
                stitched_img.paste(im, (0, y_offset))
                y_offset += im.size[1]
            img_byte_arr = io.BytesIO()
            stitched_img.save(img_byte_arr, format='JPEG', quality=85)
            return img_byte_arr.getvalue()
        except Exception as e:
            st.error(f"Image processing failed: {e}")
            return None

    @st.dialog("📝 Grade Homework")
    def _hw_grading_dialog(self, student_name, student_id, hw_id, total_q, current_score, current_report):
        st.write(f"Student: **{student_name}** (ID: {student_id})")
        
        score = st.number_input("Correct Answers", min_value=0, max_value=int(total_q), value=int(current_score) if pd.notna(current_score) else 0, step=1)
        report = st.text_area("Feedback Report", value=current_report if pd.notna(current_report) else "", height=120)
        uploaded_files = st.file_uploader("Upload Attachments (Images will be merged vertically)", type=["png", "jpg", "jpeg"], accept_multiple_files=True)
        
        st.warning("⚠️ You must click 'Save Grade' below to apply changes before closing this window.")
        
        if st.button("💾 Save Grade", type="primary", use_container_width=True):
            img_bytes = self._stitch_images(uploaded_files)
            perc = (float(score) / float(total_q)) * 100.0
            
            with self.db.get_connection() as conn:
                with conn.cursor() as c:
                    if img_bytes:
                        c.execute("""
                            INSERT INTO homework_grades (homework_id, student_id, correct_answers, percentage, report, report_image)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            ON CONFLICT (homework_id, student_id) 
                            DO UPDATE SET correct_answers = EXCLUDED.correct_answers, percentage = EXCLUDED.percentage, report = EXCLUDED.report, report_image = EXCLUDED.report_image
                        """, (hw_id, str(student_id), score, perc, report, psycopg2.Binary(img_bytes)))
                    else:
                        c.execute("""
                            INSERT INTO homework_grades (homework_id, student_id, correct_answers, percentage, report)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (homework_id, student_id) 
                            DO UPDATE SET correct_answers = EXCLUDED.correct_answers, percentage = EXCLUDED.percentage, report = EXCLUDED.report
                        """, (hw_id, str(student_id), score, perc, report))
                conn.commit()
            st.rerun()

    @st.dialog("📝 Grade Quiz")
    def _qz_grading_dialog(self, student_name, student_id, q_id, q_max, current_score, current_report):
        st.write(f"Student: **{student_name}** (ID: {student_id})")
        
        score = st.number_input("Final Score", min_value=0.0, max_value=float(q_max), value=float(current_score) if pd.notna(current_score) else 0.0)
        report = st.text_area("Feedback Report", value=current_report if pd.notna(current_report) else "", height=120)
        uploaded_files = st.file_uploader("Upload Attachments (Images will be merged vertically)", type=["png", "jpg", "jpeg"], accept_multiple_files=True)
        
        st.warning("⚠️ You must click 'Save Grade' below to apply changes before closing this window.")
        
        if st.button("💾 Save Grade", type="primary", use_container_width=True):
            img_bytes = self._stitch_images(uploaded_files)
            perc = (float(score) / float(q_max)) * 100.0 if q_max > 0 else 0
            
            with self.db.get_connection() as conn:
                with conn.cursor() as c:
                    if img_bytes:
                        c.execute("""
                            INSERT INTO quiz_grades (quiz_id, student_id, score, percentage, report, report_image)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            ON CONFLICT (quiz_id, student_id) 
                            DO UPDATE SET score = EXCLUDED.score, percentage = EXCLUDED.percentage, report = EXCLUDED.report, report_image = EXCLUDED.report_image
                        """, (q_id, str(student_id), score, perc, report, psycopg2.Binary(img_bytes)))
                    else:
                        c.execute("""
                            INSERT INTO quiz_grades (quiz_id, student_id, score, percentage, report)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (quiz_id, student_id) 
                            DO UPDATE SET score = EXCLUDED.score, percentage = EXCLUDED.percentage, report = EXCLUDED.report
                        """, (q_id, str(student_id), score, perc, report))
                conn.commit()
            st.rerun()
if __name__ == "__main__":
    app = GradePortalApp()
    app.run()
