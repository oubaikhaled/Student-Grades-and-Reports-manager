import urllib.parse
import streamlit as st
import pandas as pd
import psycopg2
from database import DatabaseManager
from pdf_utils import PDFGenerator
from auth import AuthManager

st.set_page_config(page_title="Eng.Mahmoud Adel Grade Portal", layout="wide")


class GradePortalApp:
    def __init__(self):
        self.db = DatabaseManager(st.secrets["DATABASE_URL"])
        self.auth = AuthManager()
        self.db.init_db()

    def run(self):
        if not st.session_state.logged_in:
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

                        if img_bytes:
                            st.image(img_bytes, caption="Attached Feedback Image", use_container_width=True)

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
                    st.dataframe(qz_df[["Quiz", "Score", "Out Of", "Percentage"]], hide_index=True,
                                 use_container_width=True)

                    st.markdown("**📄 Download Feedback Report**")
                    sel_qz = st.selectbox("Select Quiz", qz_df["Quiz"].tolist(), key=f"dl_qz_{student['id']}")
                    if sel_qz:
                        row_qz = qz_df[qz_df["Quiz"] == sel_qz].iloc[0]
                        img_bytes_qz = bytes(row_qz["Image"]) if pd.notna(row_qz.get("Image")) and row_qz[
                            "Image"] else None

                        if img_bytes_qz:
                            st.image(img_bytes_qz, caption="Attached Quiz Feedback Image", use_container_width=True)

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
        menu = st.sidebar.radio("Navigation",
                                ["Manage Homeworks", "Record Quiz Grades",
                                 "Manage Students", "WhatsApp Parents"])

        if menu == "Manage Homeworks":
            self._admin_manage_homeworks()
        elif menu == "Record Quiz Grades":
            self._admin_record_quizzes()
        elif menu == "Manage Students":
            self._admin_manage_students()
        elif menu == "WhatsApp Parents":
            self._admin_whatsapp_parents()

    def _admin_manage_homeworks(self):
        st.subheader("📝 Manage Homeworks")

        with st.expander("➕ Create a New Homework"):
            with st.form("new_homework_form"):
                title = st.text_input("Homework Title", placeholder="e.g. Homework 54").strip()
                total_q = st.number_input("Total Number of Questions", min_value=1, value=50, step=1)
                if st.form_submit_button("Create Homework"):
                    if not title:
                        st.error("Please enter a title.")
                    else:
                        try:
                            with self.db.get_connection() as conn:
                                with conn.cursor() as c:
                                    c.execute(
                                        "INSERT INTO homeworks (title, total_questions) VALUES (%s, %s) RETURNING homework_id",
                                        (title, total_q))
                                    hw_id = c.fetchone()[0]
                                    c.execute("SELECT id FROM students")
                                    sids = c.fetchall()
                                    for (sid,) in sids:
                                        c.execute(
                                            "INSERT INTO homework_grades (homework_id, student_id) VALUES (%s, %s)",
                                            (hw_id, sid))
                                    conn.commit()
                                    st.success(f"Successfully created '{title}' with {len(sids)} students enrolled!")
                                    st.rerun()
                        except psycopg2.IntegrityError:
                            st.error(f"A homework named '{title}' already exists.")

        hw_df = self.db.fetch_dataframe(
            "SELECT homework_id, title, total_questions FROM homeworks ORDER BY homework_id DESC")
        if hw_df.empty:
            st.info("No homeworks created yet.")
            return

        st.divider()
        st.subheader("📊 Enter Grades & Feedback")
        sel_hw_title = st.selectbox("Select Homework Assignment", hw_df["title"].tolist(), key="sel_hw")
        hw_row = hw_df[hw_df["title"] == sel_hw_title].iloc[0]
        hw_id, total_q = int(hw_row["homework_id"]), int(hw_row["total_questions"])

        with st.expander("⚠️ Danger Zone: Delete Homework"):
            if st.button("🚨 Yes, Delete This Homework"):
                try:
                    self.db.execute_query("DELETE FROM homework_grades WHERE homework_id = %s", (hw_id,))
                    self.db.execute_query("DELETE FROM homeworks WHERE homework_id = %s", (hw_id,))
                    st.success(f"'{sel_hw_title}' deleted!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")

        grades_df = self.db.fetch_dataframe("""
            SELECT s.id, s.name, g.correct_answers, g.report 
            FROM students s
            LEFT JOIN homework_grades g ON s.id = g.student_id AND g.homework_id = %s ORDER BY s.name ASC
        """, (hw_id,))

        st.caption(f"Total Questions: **{total_q}** | Edit 'Correct Answers' and 'Feedback Report' below.")
        edited_df = st.data_editor(
            grades_df, hide_index=True, use_container_width=True,
            column_config={
                "id": st.column_config.TextColumn("Student ID", disabled=True),
                "name": st.column_config.TextColumn("Student Name", disabled=True),
                "correct_answers": st.column_config.NumberColumn("Correct Answers", min_value=0, max_value=total_q,
                                                                 step=1),
                "report": st.column_config.TextColumn("Feedback Report")
            }
        )

        col1, col2 = st.columns([1, 4])
        with col1:
            if st.button("💾 Save Grades & Text Reports", type="primary"):
                with self.db.get_connection() as conn:
                    with conn.cursor() as c:
                        for _, row in edited_df.iterrows():
                            score = row["correct_answers"]
                            rep_val = row["report"] if pd.notna(row.get("report")) else None
                            if pd.notna(score):
                                perc = (float(score) / float(total_q)) * 100.0
                                c.execute("""
                                    INSERT INTO homework_grades (homework_id, student_id, correct_answers, percentage, report)
                                    VALUES (%s, %s, %s, %s, %s) ON CONFLICT (homework_id, student_id) 
                                    DO UPDATE SET correct_answers = EXCLUDED.correct_answers, percentage = EXCLUDED.percentage, report = EXCLUDED.report
                                """, (hw_id, str(row["id"]), int(score), perc, rep_val))
                            else:
                                c.execute(
                                    "UPDATE homework_grades SET correct_answers = NULL, percentage = NULL, report = %s WHERE homework_id = %s AND student_id = %s",
                                    (rep_val, hw_id, str(row["id"])))
                        conn.commit()
                st.success("Grades & Text saved!")
                st.rerun()

        with col2:
            pdf_data = [(r["id"], r["name"], r["correct_answers"] if pd.notna(r["correct_answers"]) else None,
                         (float(r["correct_answers"]) / total_q) * 100 if pd.notna(r["correct_answers"]) else None) for
                        _, r in edited_df.iterrows()]
            pdf_buf = PDFGenerator.generate_master_report(sel_hw_title, total_q, pdf_data)
            st.download_button("📄 Download Master Report (PDF)", data=pdf_buf,
                               file_name=f"{sel_hw_title.replace(' ', '_')}_Master.pdf", mime="application/pdf")

        st.divider()
        st.subheader("📝 Individual Feedback & Attachments")
        st.caption("Write a detailed text report or attach an image for a specific student.")

        report_df = self.db.fetch_dataframe("""
            SELECT s.id, s.name, g.report 
            FROM students s
            LEFT JOIN homework_grades g ON s.id = g.student_id AND g.homework_id = %s
            ORDER BY s.name ASC
        """, (hw_id,))

        student_to_attach = st.selectbox("Select Student", report_df["name"].tolist(), key="hw_stu_sel")
        if student_to_attach:
            sel_student = report_df[report_df["name"] == student_to_attach].iloc[0]
            sel_sid = sel_student["id"]
            current_report = sel_student["report"] if pd.notna(sel_student["report"]) else ""

            with st.form("hw_feedback_form"):
                new_report = st.text_area("Teacher's Text Report", value=current_report, height=150)
                up_file = st.file_uploader("Upload Image Attachment (Optional)", type=["png", "jpg", "jpeg"])

                if st.form_submit_button("💾 Save Feedback Details"):
                    with self.db.get_connection() as conn:
                        with conn.cursor() as c:
                            c.execute(
                                "UPDATE homework_grades SET report = %s WHERE homework_id = %s AND student_id = %s",
                                (new_report, hw_id, str(sel_sid)))
                            if up_file:
                                c.execute(
                                    "UPDATE homework_grades SET report_image = %s WHERE homework_id = %s AND student_id = %s",
                                    (psycopg2.Binary(up_file.read()), hw_id, str(sel_sid)))
                            conn.commit()
                    st.success(f"Feedback safely stored for {student_to_attach}!")
                    st.rerun()

    def _admin_record_quizzes(self):
        st.subheader("📝 Record External Quiz Grades")
        with st.expander("➕ Create a New Quiz"):
            with st.form("create_quiz_form"):
                q_title = st.text_input("Quiz Title").strip()
                q_max = st.number_input("Maximum Score", min_value=1.0, value=10.0, step=1.0)
                if st.form_submit_button("Create Quiz"):
                    if not q_title:
                        st.error("Title required.")
                    else:
                        try:
                            with self.db.get_connection() as conn:
                                with conn.cursor() as c:
                                    c.execute(
                                        "INSERT INTO quizzes (title, max_score) VALUES (%s, %s) RETURNING quiz_id",
                                        (q_title, q_max))
                                    q_id = c.fetchone()[0]
                                    c.execute("SELECT id FROM students")
                                    for (sid,) in c.fetchall():
                                        c.execute("INSERT INTO quiz_grades (quiz_id, student_id) VALUES (%s, %s)",
                                                  (q_id, sid))
                                    conn.commit()
                                    st.success(f"Created '{q_title}'!")
                                    st.rerun()
                        except psycopg2.IntegrityError:
                            st.error("Quiz already exists.")

        qz_df = self.db.fetch_dataframe("SELECT quiz_id, title, max_score FROM quizzes ORDER BY quiz_id DESC")
        if not qz_df.empty:
            st.divider()
            st.subheader("📊 Enter Quiz Grades & Feedback")
            sel_q = st.selectbox("Select Quiz", qz_df["title"].tolist())
            q_row = qz_df[qz_df["title"] == sel_q].iloc[0]
            q_id, q_max = int(q_row["quiz_id"]), float(q_row["max_score"])

            with st.expander("⚠️ Danger Zone: Delete Quiz"):
                if st.button("🚨 Yes, Delete This Quiz"):
                    try:
                        self.db.execute_query("DELETE FROM quiz_grades WHERE quiz_id = %s", (q_id,))
                        self.db.execute_query("DELETE FROM quizzes WHERE quiz_id = %s", (q_id,))
                        st.success(f"'{sel_q}' deleted!")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error: {e}")

            grades_df = self.db.fetch_dataframe("""
                SELECT s.id, s.name, qg.score, qg.report 
                FROM students s 
                LEFT JOIN quiz_grades qg ON s.id = qg.student_id AND qg.quiz_id = %s 
                ORDER BY s.name ASC
            """, (q_id,))

            st.caption(f"Maximum Score: **{q_max}** | Edit 'Final Score' and 'Feedback Report' below.")
            edited_df = st.data_editor(
                grades_df, hide_index=True, use_container_width=True,
                column_config={
                    "id": st.column_config.TextColumn("Student ID", disabled=True),
                    "name": st.column_config.TextColumn("Student Name", disabled=True),
                    "score": st.column_config.NumberColumn("Final Score", min_value=0.0, max_value=q_max),
                    "report": st.column_config.TextColumn("Feedback Report")
                }
            )

            if st.button("💾 Save Quiz Grades & Text Reports", type="primary"):
                with self.db.get_connection() as conn:
                    with conn.cursor() as c:
                        for _, row in edited_df.iterrows():
                            score = row["score"]
                            rep_val = row["report"] if pd.notna(row.get("report")) else None
                            if pd.notna(score):
                                c.execute("""
                                    INSERT INTO quiz_grades (quiz_id, student_id, score, percentage, report) VALUES (%s, %s, %s, %s, %s)
                                    ON CONFLICT (quiz_id, student_id) DO UPDATE SET score = EXCLUDED.score, percentage = EXCLUDED.percentage, report = EXCLUDED.report
                                """, (
                                    q_id, str(row["id"]), float(score),
                                    (float(score) / q_max) * 100.0 if q_max > 0 else 0, rep_val))
                            else:
                                c.execute(
                                    "UPDATE quiz_grades SET score = NULL, percentage = NULL, report = %s WHERE quiz_id = %s AND student_id = %s",
                                    (rep_val, q_id, str(row["id"])))
                        conn.commit()
                st.success("Quiz grades & Text saved!")
                st.rerun()

            st.divider()
            st.subheader("📝 Individual Feedback & Attachments")
            st.caption("Write a detailed text report or attach an image for a specific student's quiz.")

            report_df = self.db.fetch_dataframe("""
                SELECT s.id, s.name, qg.report 
                FROM students s
                LEFT JOIN quiz_grades qg ON s.id = qg.student_id AND qg.quiz_id = %s
                ORDER BY s.name ASC
            """, (q_id,))

            student_to_attach = st.selectbox("Select Student", report_df["name"].tolist(), key="qz_stu_sel")
            if student_to_attach:
                sel_student = report_df[report_df["name"] == student_to_attach].iloc[0]
                sel_sid = sel_student["id"]
                current_report = sel_student["report"] if pd.notna(sel_student["report"]) else ""

                with st.form("qz_feedback_form"):
                    new_report = st.text_area("Teacher's Text Report", value=current_report, height=150)
                    up_file = st.file_uploader("Upload Image Attachment (Optional)", type=["png", "jpg", "jpeg"])

                    if st.form_submit_button("💾 Save Feedback Details"):
                        with self.db.get_connection() as conn:
                            with conn.cursor() as c:
                                c.execute(
                                    "UPDATE quiz_grades SET report = %s WHERE quiz_id = %s AND student_id = %s",
                                    (new_report, q_id, str(sel_sid)))
                                if up_file:
                                    c.execute(
                                        "UPDATE quiz_grades SET report_image = %s WHERE quiz_id = %s AND student_id = %s",
                                        (psycopg2.Binary(up_file.read()), q_id, str(sel_sid)))
                                conn.commit()
                        st.success(f"Feedback safely stored for {student_to_attach}!")
                        st.rerun()

    def _admin_manage_students(self):
        st.subheader("👥 Manage Registered Students")

        with st.expander("➕ Add a New Student"):
            with st.form("add_student_form"):
                st.caption("Parent Phone is required so parents can log in to view grades.")
                col1, col2 = st.columns(2)
                with col1:
                    s_id = st.text_input("Student ID (Unique)", placeholder="e.g. 101").strip()
                    s_name = st.text_input("Student Name", placeholder="Full Name").strip()
                    s_region = st.text_input("Region (Optional)", placeholder="e.g. Maadi")
                with col2:
                    s_phone = st.text_input("Student Phone (Optional)", placeholder="e.g. 011...")
                    s_parent = st.text_input("Parent Phone (Required)", placeholder="e.g. 010...")

                if st.form_submit_button("Add Student", type="primary"):
                    if not s_id or not s_name or not s_parent:
                        st.error("Student ID, Name, and Parent Phone are required fields.")
                    else:
                        try:
                            with self.db.get_connection() as conn:
                                with conn.cursor() as c:
                                    c.execute("""
                                        INSERT INTO students (id, name, phone, phone_parent, region)
                                        VALUES (%s, %s, %s, %s, %s)
                                    """, (s_id, s_name, s_phone, s_parent, s_region))

                                    # Enroll new student into existing homeworks and quizzes
                                    c.execute("SELECT homework_id FROM homeworks")
                                    for (hw_id,) in c.fetchall():
                                        c.execute(
                                            "INSERT INTO homework_grades (homework_id, student_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                                            (hw_id, s_id))

                                    c.execute("SELECT quiz_id FROM quizzes")
                                    for (q_id,) in c.fetchall():
                                        c.execute(
                                            "INSERT INTO quiz_grades (quiz_id, student_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                                            (q_id, s_id))

                                    conn.commit()
                            st.success(f"Successfully registered {s_name}!")
                            st.rerun()
                        except psycopg2.IntegrityError:
                            st.error(f"A student with ID '{s_id}' already exists in the system.")

        st.divider()
        st.subheader("📋 Registered Students List")
        df = self.db.fetch_dataframe("SELECT id, name, phone, phone_parent, region FROM students ORDER BY name ASC")
        st.dataframe(df, use_container_width=True, hide_index=True)

    def _admin_whatsapp_parents(self):
        st.subheader("💬 WhatsApp & Report Broadcasting")
        st.caption("Bulk download PDFs and message parents directly for graded assignments.")

        type_choice = st.radio("Select Assignment Type", ["Homework", "Quiz"], horizontal=True)

        if type_choice == "Homework":
            hw_df = self.db.fetch_dataframe(
                "SELECT homework_id, title, total_questions FROM homeworks ORDER BY homework_id DESC")
            if hw_df.empty:
                st.info("No homeworks found.")
                return
            sel_title = st.selectbox("Select Homework", hw_df["title"].tolist())
            hw_row = hw_df[hw_df["title"] == sel_title].iloc[0]
            item_id = int(hw_row["homework_id"])
            total_q = int(hw_row["total_questions"])

            grades_df = self.db.fetch_dataframe("""
                SELECT s.id, s.name, s.phone_parent, g.correct_answers as score, g.percentage, g.report, g.report_image 
                FROM students s
                JOIN homework_grades g ON s.id = g.student_id
                WHERE g.homework_id = %s AND g.correct_answers IS NOT NULL
                ORDER BY s.name ASC
            """, (item_id,))

        else:
            qz_df = self.db.fetch_dataframe("SELECT quiz_id, title, max_score FROM quizzes ORDER BY quiz_id DESC")
            if qz_df.empty:
                st.info("No quizzes found.")
                return
            sel_title = st.selectbox("Select Quiz", qz_df["title"].tolist())
            qz_row = qz_df[qz_df["title"] == sel_title].iloc[0]
            item_id = int(qz_row["quiz_id"])
            total_q = float(qz_row["max_score"])

            grades_df = self.db.fetch_dataframe("""
                SELECT s.id, s.name, s.phone_parent, g.score, g.percentage, g.report, g.report_image 
                FROM students s
                JOIN quiz_grades g ON s.id = g.student_id
                WHERE g.quiz_id = %s AND g.score IS NOT NULL
                ORDER BY s.name ASC
            """, (item_id,))

        if grades_df.empty:
            st.warning(f"No grades have been recorded for '{sel_title}' yet.")
            return

        st.success(f"Found {len(grades_df)} students with recorded grades.")
        st.divider()

        for _, row in grades_df.iterrows():
            col1, col2, col3, col4 = st.columns([3, 2, 3, 3])

            col1.markdown(f"**{row['name']}**")
            col2.write(f"Score: **{row['score']}** / {total_q}")

            with col3:
                img_bytes = bytes(row["report_image"]) if pd.notna(row.get("report_image")) and row[
                    "report_image"] else None
                pdf_buf = PDFGenerator.generate_student_report(
                    row["name"], sel_title, row["score"], total_q,
                    row["percentage"], row.get("report"), img_bytes
                )

                st.download_button(
                    label="📄 Download Report",
                    data=pdf_buf,
                    file_name=f"{row['name']}_{sel_title}.pdf".replace(" ", "_"),
                    mime="application/pdf",
                    key=f"dl_{type_choice}_{row['id']}",
                    use_container_width=True
                )

            with col4:
                raw_phone = str(row["phone_parent"]).strip()
                formatted_phone = "2" + raw_phone if raw_phone.startswith("0") else raw_phone
                wa_msg = f"Hello, the {type_choice.lower()} report for '{sel_title}' for {row['name']} is ready on the Eng. Mahmoud Adel portal. Attached is the PDF."
                encoded_msg = urllib.parse.quote(wa_msg)
                wa_url = f"https://wa.me/{formatted_phone}?text={encoded_msg}"

                st.link_button("💬 Send WhatsApp", wa_url, key=f"wa_{type_choice}_{row['id']}", use_container_width=True)

            st.divider()


if __name__ == "__main__":
    app = GradePortalApp()
    app.run()