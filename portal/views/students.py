import pandas as pd
import psycopg2
import streamlit as st

from portal.data import load_students, run_write


def render_manage_students(db):
    st.subheader("👥 Manage Students")

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
                        run_write(db, [(
                            "INSERT INTO students (id, name, phone, phone_parent, group_number) VALUES (%s, %s, %s, %s, %s)",
                            (add_id.strip(), add_name.strip(), add_phone.strip(),
                             add_parent_phone.strip(), add_group.strip()),
                        )])
                        st.session_state["flash"] = f"Student '{add_name}' added successfully!"
                        st.rerun()
                    except psycopg2.IntegrityError:
                        st.error(f"Error: A student with ID '{add_id}' already exists.")
                    except Exception as e:
                        st.error(f"Failed to add student. Error: {e}")

    st.divider()

    students_df = load_students(db)
    if students_df.empty:
        st.info("No students registered yet.")
        return

    st.caption(f"Current Enrolled Students ({len(students_df)})")
    st.dataframe(students_df, hide_index=True, use_container_width=True)

    st.divider()

    st.subheader("🛠️ Modify Student Records")
    search_term = st.text_input(
        "🔍 Search Student by Name or ID", placeholder="Start typing to filter the dropdowns below..."
    ).strip()

    filtered_df = students_df
    if search_term:
        mask = (
            students_df["name"].astype(str).str.contains(search_term, case=False, na=False, regex=False)
            | students_df["id"].astype(str).str.contains(search_term, case=False, na=False, regex=False)
        )
        filtered_df = students_df[mask]

    if filtered_df.empty:
        st.warning("No students match your search criteria.")
        return

    labels = {str(r.id): f"{r.name} (ID: {r.id})" for r in filtered_df.itertuples(index=False)}
    all_ids = set(students_df["id"].astype(str))

    with st.expander("✏️ Edit Student Data"):
        edit_id = st.selectbox("Select Student to Edit", list(labels.keys()),
                               format_func=lambda i: labels[i], key="edit_student_sel_v2")
        if edit_id:
            student_row = students_df[students_df["id"].astype(str) == edit_id].iloc[0]

            with st.form(f"edit_student_form_{edit_id}"):
                new_id = st.text_input("Student ID", value=str(student_row["id"]))
                new_name = st.text_input("Student Name", value=str(student_row["name"]))
                new_phone = st.text_input("Student Phone", value=str(student_row["phone"]) if pd.notna(student_row["phone"]) else "")
                new_parent_phone = st.text_input("Parent Phone", value=str(student_row["phone_parent"]) if pd.notna(student_row["phone_parent"]) else "")
                new_group = st.text_input("Group Number", value=str(student_row["group_number"]) if pd.notna(student_row["group_number"]) else "")

                if st.form_submit_button("💾 Save Changes", type="primary"):
                    new_id = new_id.strip()
                    if not new_name.strip() or not new_id:
                        st.error("Student ID and Name cannot be empty.")
                    elif new_id != edit_id and new_id in all_ids:
                        st.error(f"The ID {new_id} is already assigned to another student.")
                    else:
                        try:
                            if new_id != edit_id:
                                ops = [
                                    ("INSERT INTO students (id, name, phone, phone_parent, group_number) VALUES (%s, %s, %s, %s, %s)",
                                     (new_id, new_name, new_phone, new_parent_phone, new_group)),
                                    ("UPDATE homework_grades SET student_id = %s WHERE student_id = %s", (new_id, edit_id)),
                                    ("UPDATE quiz_grades SET student_id = %s WHERE student_id = %s", (new_id, edit_id)),
                                    ("DELETE FROM students WHERE id = %s", (edit_id,)),
                                ]
                            else:
                                ops = [(
                                    "UPDATE students SET name = %s, phone = %s, phone_parent = %s, group_number = %s WHERE id = %s",
                                    (new_name, new_phone, new_parent_phone, new_group, edit_id),
                                )]
                            run_write(db, ops)
                            st.session_state["flash"] = "Student details updated successfully!"
                            st.rerun()
                        except psycopg2.IntegrityError:
                            st.error(f"The ID {new_id} is already assigned to another student.")
                        except Exception as e:
                            st.error(f"Failed to update student. Error: {e}")

    with st.expander("⚠️ Danger Zone: Delete Student"):
        st.warning("Deleting a student will permanently remove all their recorded homework and quiz grades. This action cannot be undone.")

        delete_id = st.selectbox("Select Student to Delete", list(labels.keys()),
                                 format_func=lambda i: labels[i], key="delete_student_sel_v2")

        if st.button("🚨 Yes, Permanently Delete Student"):
            try:
                run_write(db, [
                    ("DELETE FROM homework_grades WHERE student_id = %s", (delete_id,)),
                    ("DELETE FROM quiz_grades WHERE student_id = %s", (delete_id,)),
                    ("DELETE FROM students WHERE id = %s", (delete_id,)),
                ])
                st.session_state["flash"] = "Student successfully deleted!"
                st.rerun()
            except Exception as e:
                st.error(f"Failed to delete student. Error: {e}")
