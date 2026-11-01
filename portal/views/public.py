import base64
import re

import pandas as pd
import requests
import streamlit as st

from portal.config import MAX_PHOTO_MB, MAX_TOTAL_UPLOAD_MB, get_drive_script_url
from portal.data import load_homeworks, load_students, run_write
from portal.grades import mark_homework_submitted
from portal.images import convert_uploads_to_pdf_bytes, upload_preview
from portal.utils import build_drive_hw_filename
from whatsapp_automation import trigger_hw_submission_assistant_whatsapp


def render_public_login(db, auth):
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        try:
            st.image("assets/images/logo.png", use_container_width=True)
        except Exception:
            pass
        st.title("Eng. Mahmoud Adel")
        st.caption("Student Portal & Homework Submission")

    tab_parent, tab_student = st.tabs(["👨‍👩‍👦 Parent Access", "📤 Student Submission"])

    with tab_parent:
        st.subheader("View Student Grades")
        parent_phone = st.text_input(
            "Parent Phone Number", placeholder="e.g. 01030007000", key="parent_phone_login"
        ).strip()
        if st.button("View Grades", type="primary", key="btn_parent_login") and parent_phone:
            auth.login_parent(db, parent_phone)

    with tab_student:
        st.subheader("📤 Submit Homework")
        st.caption(
            "1. Select your homework  •  2. Enter your Student ID & Parent Phone  •  "
            "3. Select your homework photos or PDF and tap Submit."
        )

        hw_df = load_homeworks(db)
        if hw_df.empty:
            st.info("No active homeworks available for submission.")
        else:
            hw_options = {row["homework_id"]: f"{row['title']}" for _, row in hw_df.iterrows()}
            selected_hw_id = st.selectbox(
                "1️⃣ Select Homework *",
                options=list(hw_options.keys()),
                index=None,
                placeholder="Choose the homework you want to submit...",
                format_func=lambda x: hw_options[x],
                key="student_hw_sel",
            )
            selected_hw_title = hw_options[selected_hw_id] if selected_hw_id is not None else ""

            col1, col2 = st.columns(2)
            student_id = col1.text_input(
                "2️⃣ Enter your Student ID *",
                placeholder="e.g. 176109",
                key="student_id_input",
            ).strip()
            verify_phone = col2.text_input(
                "3️⃣ Enter Parent Phone Number *",
                placeholder="e.g. 010...",
                key="student_verify_phone_input",
            ).strip()

            student_name = ""
            profile_complete = False
            is_verified = False

            def _is_missing(val):
                if val is None or pd.isna(val):
                    return True
                return str(val).strip().lower() in ("", "none", "nan", "null", "n/a", "non")

            def _last10(p_val):
                digits = re.sub(r"\D", "", str(p_val or ""))
                return digits[-10:] if len(digits) >= 10 else ""

            # Keep every message that depends on verification inside ONE container so the
            # uploader below always sits at the same position in the page. If the number of
            # elements above it changes (e.g. the "Verified" banner appears when the phone
            # field commits on blur as the student taps Upload), Streamlit re-mounts the
            # uploader and photos picked into the old, detached input are silently lost.
            with st.container():
                if student_id and verify_phone:
                    all_stu_df = load_students(db)
                    match = all_stu_df[all_stu_df["id"].astype(str) == student_id]
                    if match.empty:
                        st.error("❌ Student ID not found. Please check your ID.")
                    else:
                        stu_row = match.iloc[0]
                        cur_name = "" if _is_missing(stu_row.get("name")) else str(stu_row["name"]).strip()
                        cur_phone = "" if _is_missing(stu_row.get("phone")) else str(stu_row["phone"]).strip()
                        cur_parent = "" if _is_missing(stu_row.get("phone_parent")) else str(stu_row["phone_parent"]).strip()
                        cur_group = "" if _is_missing(stu_row.get("group_number")) else str(stu_row["group_number"]).strip()

                        entered_10 = _last10(verify_phone)
                        db_parent_10 = _last10(cur_parent)
                        db_student_10 = _last10(cur_phone)

                        if not entered_10:
                            st.warning("Please enter a valid 11-digit phone number to verify your identity.")
                        elif db_parent_10 and entered_10 == db_parent_10:
                            is_verified = True
                        elif not db_parent_10 and db_student_10 and entered_10 == db_student_10:
                            is_verified = True
                        elif not db_parent_10 and not db_student_10:
                            is_verified = True
                        else:
                            st.error(
                                "❌ Verification failed: The phone number entered does not match the Parent Phone Number on file for this ID."
                            )

                        if is_verified:
                            missing_fields = []
                            if not cur_name:
                                missing_fields.append("Full Name")
                            if not cur_phone:
                                missing_fields.append("Student Phone")
                            if not cur_parent:
                                missing_fields.append("Parent Phone")
                            if not cur_group:
                                missing_fields.append("Group Number")

                            if not missing_fields:
                                student_name = cur_name
                                profile_complete = True
                                st.success(f"✅ Verified! Welcome, **{student_name}** (Group: {cur_group})")
                            else:
                                st.warning(
                                    f"⚠️ Identity verified! Please complete your missing profile details (**{', '.join(missing_fields)}**) below first."
                                )
                                with st.container(border=True):
                                    st.markdown("#### 📋 Complete Your Student Profile")
                                    with st.form(f"complete_profile_form_{student_id}"):
                                        c_p1, c_p2 = st.columns(2)
                                        with c_p1:
                                            in_name = st.text_input(
                                                "Full Name *",
                                                value=cur_name,
                                                placeholder="Enter your full name",
                                            ).strip()
                                            in_group = st.text_input(
                                                "Group Number *",
                                                value=cur_group,
                                                placeholder="e.g. 1, 2, Online...",
                                            ).strip()
                                        with c_p2:
                                            in_phone = st.text_input(
                                                "Your Phone Number *",
                                                value=cur_phone,
                                                placeholder="010...",
                                            ).strip()
                                            in_parent = st.text_input(
                                                "Parent Phone Number *",
                                                value=cur_parent or verify_phone,
                                                placeholder="010...",
                                            ).strip()

                                        if st.form_submit_button(
                                            "💾 Save Profile",
                                            type="primary",
                                            use_container_width=True,
                                        ):
                                            digits_stu = re.sub(r"\D", "", in_phone)
                                            digits_par = re.sub(r"\D", "", in_parent)
                                            if not (in_name and in_group and in_phone and in_parent):
                                                st.error("All fields are required.")
                                            elif len(digits_stu) < 10 or len(digits_par) < 10:
                                                st.error("Please enter valid phone numbers (at least 10 digits).")
                                            elif digits_stu[-10:] == digits_par[-10:]:
                                                st.error("Parent Phone Number must be different from Student Phone Number.")
                                            else:
                                                try:
                                                    run_write(db, [(
                                                        "UPDATE students SET name = %s, phone = %s, phone_parent = %s, group_number = %s WHERE id = %s",
                                                        (in_name, in_phone, in_parent, in_group, student_id),
                                                    )])
                                                    st.success("✅ Profile saved!")
                                                    st.rerun()
                                                except Exception as e:
                                                    st.error(f"Could not save profile: {e}")

            st.divider()

            st.markdown("#### 📄 4️⃣ Upload Homework Photos or PDF")
            st.caption(
                "**How to upload multiple pages:** Take the photos with your phone camera first. "
                "Then tap **Upload** and pick **up to 4 photos at a time**, in order. "
                "Tap **+** to add the next photos, then tap Submit."
            )

            # No st.form here: inside a form the script never sees the selected files until
            # Submit, so it cannot show previews. Each pick reruns the script and the files
            # come back in the order they were added. MIME types (not extensions) are used so
            # phone photos whose names have no extension (some Android gallery pickers) are
            # accepted. HEIC/HEIF photos are image/* too; the server converts them.
            # Photos only: with PDF in the accept list, Android Chrome opens the full Gallery /
            # Photos app instead of its lightweight photo picker. While that app is open,
            # Android can kill the Chrome tab to free memory, and selecting several large
            # photos then reloads the page and loses them. PDFs get their own uploader below.
            # Don't add file extensions here: Streamlit then adds a non-image type to the
            # input's accept list and Chrome falls back to the Gallery app again.
            photo_files = st.file_uploader(
                "Select Homework Photos",
                type=["image/*"],
                accept_multiple_files=True,
                key="hw_upload_widget",
                max_upload_size=MAX_PHOTO_MB,
            )
            pdf_files = st.file_uploader(
                "Or upload a PDF instead",
                type=["application/pdf"],
                accept_multiple_files=True,
                key="hw_pdf_widget",
            )
            # Streamlit caps the file list at a small scroll box (about 4 files on a phone),
            # so newly added photos land out of sight and it looks as if they weren't added.
            # Let the list grow to show every file. (Style-only st.html takes no space.)
            st.html(
                "<style>"
                ".st-key-hw_upload_widget [data-testid='stFileChips'],"
                ".st-key-hw_upload_widget [data-testid='stFileChips'] > div"
                "{max-height:none !important; overflow-y:visible !important;}"
                "</style>"
            )
            uploaded_files = (photo_files or []) + (pdf_files or [])
            total_upload_mb = sum(f.size for f in uploaded_files) / (1024 * 1024)

            if uploaded_files:
                st.caption(
                    f"📎 **{len(uploaded_files)} file(s) attached** ({total_upload_mb:.1f} MB of "
                    f"{MAX_TOTAL_UPLOAD_MB} MB), merged into one PDF in this order. "
                    "Tap **+** to add more, or **✕** next to a file to remove it."
                )
                if total_upload_mb > MAX_TOTAL_UPLOAD_MB:
                    st.error(
                        f"⚠️ Your files add up to {total_upload_mb:.0f} MB. The limit is "
                        f"{MAX_TOTAL_UPLOAD_MB} MB in total, so please remove some files."
                    )
                st.image(
                    [upload_preview(f) for f in uploaded_files],
                    caption=[f"{i}. {f.name}" for i, f in enumerate(uploaded_files, 1)],
                    width=110,
                )

            submit_hw_clicked = st.button(
                "🚀 Submit Homework",
                type="primary",
                key="btn_submit_hw_main",
                use_container_width=True,
            )

            if submit_hw_clicked:
                if selected_hw_id is None:
                    st.error("⚠️ Please select the **Homework** from dropdown 1️⃣ at the top first.")
                elif not student_id or not verify_phone:
                    st.error("⚠️ Please enter both your **Student ID** and **Parent Phone Number** above.")
                elif not is_verified:
                    st.error("⚠️ Your Student ID and Parent Phone Number could not be verified.")
                elif not profile_complete:
                    st.error("⚠️ Please complete and save your missing profile details in the box above first.")
                elif not uploaded_files:
                    st.error("⚠️ Please attach your homework photos or PDF file before clicking Submit.")
                elif total_upload_mb > MAX_TOTAL_UPLOAD_MB:
                    st.error(
                        f"⚠️ Your files are over the {MAX_TOTAL_UPLOAD_MB} MB total limit. "
                        "Please remove some files before submitting."
                    )
                else:
                    with st.spinner("Converting pages to PDF & uploading to Google Drive..."):
                        try:
                            pdf_bytes = convert_uploads_to_pdf_bytes(uploaded_files)
                            base64_data = base64.b64encode(pdf_bytes).decode("utf-8")
                            file_name = build_drive_hw_filename(
                                student_id, selected_hw_id, selected_hw_title
                            )

                            script_url = get_drive_script_url()
                            payload = {
                                "filename": file_name,
                                "subfolder": selected_hw_title,
                                "mimeType": "application/pdf",
                                "base64": base64_data,
                            }

                            response = requests.post(script_url, json=payload, timeout=90)
                            if response.status_code == 200:
                                res_json = response.json()
                                if res_json.get("status") == "success":
                                    mark_homework_submitted(db, selected_hw_id, student_id)
                                    trigger_hw_submission_assistant_whatsapp(
                                        db=db,
                                        student_id=student_id,
                                        homework_id=selected_hw_id,
                                        homework_title=selected_hw_title,
                                        student_name=student_name,
                                        group_number=cur_group,
                                    )
                                    st.success(
                                        f"✅ Homework **{selected_hw_title}** submitted successfully!"
                                    )
                                    st.balloons()
                                else:
                                    st.error(
                                        f"Error from Google Drive: {res_json.get('message', 'Unknown error')}"
                                    )
                            else:
                                st.error(
                                    f"Failed to connect to Google Drive. Status Code: {response.status_code}"
                                )
                        except Exception as e:
                            st.error(f"An error occurred while processing your files: {e}")


def render_admin_login(auth):
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.title("Teacher & Admin Portal")
        st.caption("Please log in to access the management dashboard.")

        with st.container(border=True):
            admin_num = st.text_input("Admin Number")
            admin_pass = st.text_input("Password", type="password")
            if st.button("Login Securely", type="primary", key="btn_admin_login", use_container_width=True):
                auth.login_admin(admin_num, admin_pass)
