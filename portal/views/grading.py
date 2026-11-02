import pandas as pd
import streamlit as st

from ai_grading import (
    fetch_pdf_from_drive,
    get_gemini_api_key,
    grade_full_homework,
    parse_api_keys,
)
from portal.config import KINDS
from portal.data import load_item_grades, load_students, merge_grades
from portal.grades import mark_homework_submitted, save_grade
from portal.images import stitch_images
from portal.locks import release_my_student_locks, sync_and_claim_student_lock
from portal.reports import get_cached_master_report
from portal.utils import build_drive_hw_filename, clean, fmt, set_flag


def format_submission_status(score, max_score, submitted=False, is_locked=False):
    if pd.notna(score):
        base = f"✅ {fmt(score)} / {fmt(max_score)}"
        return f"{base} (⏳ Editing...)" if is_locked else base
    if is_locked:
        return "⏳ Being Graded..."
    if bool(submitted):
        return "📩 Submitted"
    return "❌ Missing"


def filter_students(df, key_prefix):
    col_f1, col_f2 = st.columns(2)
    groups = sorted({str(g).strip() for g in df["group_number"].dropna().unique() if str(g).strip()})
    filter_group = col_f1.selectbox("Filter by Group", ["All Groups"] + groups, key=f"{key_prefix}_grp_sel")
    term = col_f2.text_input("Search Name, ID, or Phone", key=f"{key_prefix}_srch_sel").strip()

    out = df
    if filter_group != "All Groups":
        out = out[out["group_number"].astype(str).str.strip() == filter_group]
    if term:
        mask = (
            out["name"].astype(str).str.contains(term, case=False, na=False, regex=False)
            | out["id"].astype(str).str.contains(term, case=False, na=False, regex=False)
            | out["phone"].astype(str).str.contains(term, case=False, na=False, regex=False)
        )
        out = out[mask]
    return out, filter_group


@st.fragment
def grading_fragment(db, kind, item_id, item_title, max_score, df, group_label, saved_rubric=""):
    cfg = KINDS[kind]
    ver_key = f"roster_ver_{kind}"
    st.session_state.setdefault(ver_key, 0)
    roster_key = f"roster_{kind}_{item_id}_{st.session_state[ver_key]}"

    # Detect which row is currently selected in this session BEFORE rendering the table
    current_widget_state = st.session_state.get(roster_key, {})
    sel_rows = []
    if hasattr(current_widget_state, "selection"):
        sel_rows = current_widget_state.selection.rows
    elif isinstance(current_widget_state, dict):
        sel_rows = current_widget_state.get("selection", {}).get("rows", [])

    selected_stu_id = str(df.iloc[sel_rows[0]]["id"]) if (sel_rows and sel_rows[0] < len(df)) else None

    # Synchronize lock state across all concurrent admins
    claimed_by_me, locked_sids = sync_and_claim_student_lock(kind, item_id, selected_stu_id)

    panel = st.container()

    col_r1, col_r2 = st.columns([4, 1])
    with col_r1:
        st.markdown("### 📋 Class Roster")
        st.caption(
            "Click a row's selection box to claim and grade that student. "
            "Students with a tick in **In Progress** are currently open in an admin session."
        )
    with col_r2:
        st.write("")
        if st.button("🔄 Refresh Locks", key=f"refresh_locks_{kind}_{item_id}", use_container_width=True):
            st.rerun(scope="fragment")

    view = pd.DataFrame({
        "In Progress": [str(sid) in locked_sids for sid in df["id"]],
        "ID": df["id"].astype(str).values,
        "Name": df["name"].values,
        "Group": df["group_number"].fillna("").astype(str).values,
        "Status": [
            format_submission_status(s, max_score, sub, is_locked=(str(sid) in locked_sids))
            for sid, s, sub in zip(df["id"], df["score"], df["submitted"])
        ],
    })
    event = st.dataframe(
        view,
        hide_index=True,
        use_container_width=True,
        height=min(420, 38 + 35 * len(view)),
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "In Progress": st.column_config.CheckboxColumn(
                "In Progress",
                help="Ticked when an admin currently has this student open for grading.",
                default=False,
            )
        },
        key=roster_key,
    )
    rows = event.selection.rows if event and event.selection else []

    if rows:
        stu = df.iloc[rows[0]]
        stu_id = str(stu["id"])

        # Re-verify lock in case rows changed during this rerun cycle
        claimed_by_me, locked_sids = sync_and_claim_student_lock(kind, item_id, stu_id)

        with panel:
            if not claimed_by_me:
                st.warning(
                    f"⏳ **{stu['name']} (ID: {stu_id})** is currently being graded by another admin! "
                    f"Please select a different student or click **🔄 Refresh Locks** once they finish."
                )
            else:
                st.markdown(f"### 📝 Grading Panel: {stu['name']} (ID: {stu_id}) — `☑️ Claimed by You`")
                st.info(f"🎯 **Full Mark for this {cfg['noun']}:** {fmt(max_score)}")

                score_key = f"ai_score_{kind}_{item_id}_{stu_id}"
                report_key = f"ai_report_{kind}_{item_id}_{stu_id}"
                images_key = f"ai_images_{kind}_{item_id}_{stu_id}"

                if kind == "homework":
                    col_manual, col_ai = st.columns([3, 2])
                else:
                    col_manual = st.container()
                    col_ai = None

                # -----------------------------------------------------------------
                # RIGHT COLUMN: 1-CLICK AI AUTO-GRADING (SIDE-BY-SIDE)
                # -----------------------------------------------------------------
                if kind == "homework" and col_ai is not None:
                    with col_ai:
                        with st.container(border=True):
                            st.markdown("#### 🤖 AI Auto-Grading")
                            st.caption(
                                "Fetch this student's submitted homework from Google Drive "
                                "and automatically fill the score and feedback report on the left."
                            )

                            grade_clicked = st.button(
                                "✨ Auto-Grade with AI",
                                type="primary",
                                key=f"btn_ai_grade_{stu_id}_{item_id}",
                                use_container_width=True,
                            )

                            if grade_clicked:
                                active_keys = parse_api_keys(get_gemini_api_key(st.secrets))
                                if not active_keys:
                                    st.error("AI grading is not configured. Please grade manually on the left.")
                                else:
                                    new_filename = build_drive_hw_filename(stu_id, item_id, item_title)
                                    legacy_filename = f"{stu_id},{item_id}.pdf"

                                    prog_placeholder = st.empty()
                                    prog_bar = prog_placeholder.progress(
                                        0.02, text="📥 Retrieving homework from Google Drive..."
                                    )

                                    pdf_data = None
                                    try:
                                        try:
                                            pdf_data = fetch_pdf_from_drive(new_filename, subfolder=item_title)
                                        except TypeError:
                                            # Backwards-compatible fallback if fetch_pdf_from_drive doesn't take subfolder yet
                                            pdf_data = fetch_pdf_from_drive(new_filename)
                                        except FileNotFoundError:
                                            if new_filename != legacy_filename:
                                                try:
                                                    pdf_data = fetch_pdf_from_drive(legacy_filename, subfolder=item_title)
                                                except TypeError:
                                                    pdf_data = fetch_pdf_from_drive(legacy_filename)
                                            else:
                                                raise
                                    except FileNotFoundError:
                                        prog_placeholder.empty()
                                        st.warning(
                                            f"No submission found on Google Drive for **{stu['name']}** yet. "
                                            f"You can enter their grade manually on the left."
                                        )
                                    except Exception as e:
                                        prog_placeholder.empty()
                                        st.error(f"Could not retrieve submission: {e}")

                                    if pdf_data:
                                        if not bool(stu.get("submitted", False)):
                                            mark_homework_submitted(db, item_id, stu_id)

                                        def on_progress(current_step, total_steps, msg):
                                            pct = min(1.0, max(0.0, current_step / max(1, total_steps)))
                                            prog_bar.progress(pct, text=msg)

                                        try:
                                            ai_res = grade_full_homework(
                                                pdf_bytes=pdf_data,
                                                assignment_title=item_title,
                                                max_score=float(max_score),
                                                api_key=active_keys,
                                                rubric=saved_rubric or "",
                                                model_name="auto",
                                                progress_callback=on_progress,
                                            )
                                            prog_placeholder.empty()
                                            st.session_state[score_key] = ai_res["final_score"]
                                            st.session_state[report_key] = ai_res["consolidated_report"]
                                            st.session_state[images_key] = [p["image_bytes"] for p in ai_res["page_results"]]
                                            st.toast(f"✅ AI Grading Complete: {ai_res['final_score']} / {max_score}")
                                            st.rerun(scope="fragment")
                                        except Exception as e:
                                            prog_placeholder.empty()
                                            st.error(f"AI Grading failed: {e}")

                            if images_key in st.session_state and st.session_state[images_key]:
                                page_imgs = st.session_state[images_key]
                                st.success(
                                    f"✅ **Graded {len(page_imgs)} page(s)!** "
                                    f"Review the score & report on the left, then click **Save Grade**."
                                )
                                with st.expander(f"📄 View Submitted Pages ({len(page_imgs)})", expanded=False):
                                    cols_p = st.columns(2)
                                    for idx, p_bytes in enumerate(page_imgs):
                                        with cols_p[idx % 2]:
                                            st.image(p_bytes, caption=f"Page {idx + 1}", use_container_width=True)

                # -----------------------------------------------------------------
                # LEFT COLUMN: CLASSIC MANUAL GRADING FORM (SIDE-BY-SIDE)
                # -----------------------------------------------------------------
                cur = stu["score"]
                default_score = st.session_state.get(
                    score_key,
                    int(cur) if (pd.notna(cur) and cfg["is_int"]) else (float(cur) if pd.notna(cur) else (0 if cfg["is_int"] else 0.0))
                )
                default_report = st.session_state.get(
                    report_key,
                    stu["report"] if pd.notna(stu["report"]) else ""
                )

                ai_rev = "ai" if score_key in st.session_state else "man"
                with col_manual:
                    with st.container(border=True):
                        st.markdown("#### ✍️ Grade & Feedback Report")
                        with st.form(f"grade_form_{kind}_{item_id}_{stu_id}_{ai_rev}"):
                            if cfg["is_int"]:
                                score = st.number_input(
                                    cfg["score_label"],
                                    min_value=0,
                                    max_value=int(max_score),
                                    value=int(round(float(default_score))),
                                    step=1,
                                )
                            else:
                                score = st.number_input(
                                    cfg["score_label"],
                                    min_value=0.0,
                                    max_value=float(max_score),
                                    value=float(default_score),
                                    step=0.5,
                                )
                            report = st.text_area("Feedback Report", value=default_report, height=140)
                            files = st.file_uploader(
                                "Upload Attachments (Multiple images will be merged vertically)",
                                type=["png", "jpg", "jpeg"],
                                accept_multiple_files=True,
                            )

                            attach_ai_pages = False
                            if images_key in st.session_state and st.session_state[images_key]:
                                attach_ai_pages = st.checkbox(
                                    "📎 Attach AI-inspected homework pages to student's report card",
                                    value=True,
                                )

                            col_save, col_cancel = st.columns([3, 1])
                            save = col_save.form_submit_button("💾 Save Grade", type="primary", use_container_width=True)
                            cancel = col_cancel.form_submit_button("❌ Cancel", use_container_width=True)

                if save:
                    if files:
                        img_bytes = stitch_images(files)
                    elif attach_ai_pages and images_key in st.session_state:
                        img_bytes = stitch_images(st.session_state[images_key])
                    else:
                        img_bytes = None

                    perc = (float(score) / float(max_score)) * 100.0 if max_score > 0 else 0
                    save_grade(db, kind, item_id, stu_id, score, perc, report, img_bytes)

                    # Release the in-progress lock now that the grade is saved
                    release_my_student_locks()

                    st.session_state.pop(score_key, None)
                    st.session_state.pop(report_key, None)
                    st.session_state.pop(images_key, None)

                    st.session_state[ver_key] += 1
                    st.session_state["flash"] = f"Grade saved for {stu['name']}!"
                    st.rerun()

                if cancel:
                    # Release the in-progress lock if the admin cancels without saving
                    release_my_student_locks()

                    st.session_state.pop(score_key, None)
                    st.session_state.pop(report_key, None)
                    st.session_state.pop(images_key, None)
                    st.session_state[ver_key] += 1
                    st.rerun(scope="fragment")

    st.divider()

    master_key = f"gen_master_{kind}_{item_id}_{group_label}"
    if not st.session_state.get(master_key):
        st.button("📄 Prepare Master PDF Report", key=f"btn_{master_key}",
                  on_click=set_flag, args=(master_key,))
    else:
        pdf_data = [
            (
                r.id, 
                r.name, 
                clean(r.score) if pd.notna(r.score) else ("Submitted" if getattr(r, "submitted", False) else None),
                (float(r.score) / float(max_score)) * 100 if pd.notna(r.score) else None
            )
            for r in df.itertuples(index=False)
        ]
        pdf = get_cached_master_report(item_title, group_label, max_score, pdf_data)
        st.download_button(
            "⬇️ Download Master PDF Report", data=pdf,
            file_name=f"{item_title}_{group_label}_Master.pdf".replace(" ", "_"),
            mime="application/pdf",
        )


def grading_section(db, kind, item_id, item_title, max_score, saved_rubric=""):
    st.divider()
    st.subheader("🔍 Search & Grade Students")

    students_df = load_students(db)
    df = merge_grades(students_df, load_item_grades(db, kind, item_id))
    filtered, filter_group = filter_students(df, kind)

    if filtered.empty:
        st.warning("No students found matching your filters.")
        return
    grading_fragment(db, kind, item_id, item_title, max_score, filtered, filter_group, saved_rubric=saved_rubric)
