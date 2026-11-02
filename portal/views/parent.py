import streamlit as st

from portal.config import KINDS
from portal.data import load_parent_grades, load_parent_students
from portal.reports import build_report_pdf
from portal.utils import clean, set_flag
from student_profile import StudentProfileView


@st.fragment
def parent_student_fragment(db, student_row, hw_df, qz_df):
    sid = str(student_row["id"])
    name = student_row["name"]

    StudentProfileView(db).render_single_student_profile(student_row, is_admin=False)

    with st.expander(f"📄 Download Individual Homework & Quiz Feedback Reports for {name}", expanded=False):
        col1, col2 = st.columns(2)
        with col1:
            render_parent_grades(db, "homework", sid, name, hw_df)
        with col2:
            render_parent_grades(db, "quiz", sid, name, qz_df)
    st.divider()


def render_parent_grades(db, kind, sid, student_name, df):
    cfg = KINDS[kind]
    st.markdown(cfg["heading"])
    if df.empty:
        st.info(cfg["empty_msg"])
        return

    name_col = cfg["name_col"]
    st.dataframe(df[[name_col, "Score", "Out Of", "Percentage"]], hide_index=True, use_container_width=True)

    st.markdown("**📄 Download Feedback Report**")
    sel = st.selectbox(cfg["select_label"], df[name_col].tolist(), key=f"sel_{kind}_{sid}")
    if not sel:
        return
    row = df[df[name_col] == sel].iloc[0]
    item_id = int(row["item_id"])
    gen_key = f"gen_{kind}_{sid}_{item_id}"

    if not st.session_state.get(gen_key):
        st.button("📄 Prepare PDF Report", key=f"btn_{gen_key}", use_container_width=True,
                  on_click=set_flag, args=(gen_key,))
    else:
        pdf = build_report_pdf(
            db, kind, item_id, sid, student_name, sel,
            clean(row["Score"]), clean(row["Out Of"]), clean(row["Percentage"]),
            clean(row["Report"]), None,
        )
        st.download_button(
            label=f"⬇️ Download {sel} Report", data=pdf,
            file_name=f"{student_name}_{sel}_Report.pdf".replace(" ", "_"),
            mime="application/pdf", key=f"dl_btn_{gen_key}", use_container_width=True,
        )


def render_parent_portal(db, auth):
    st.sidebar.button("🚪 Logout", on_click=auth.logout, type="primary", use_container_width=True)
    st.title("👤 Student Profile & Grades Overview")

    students_df = load_parent_students(db, st.session_state.identifier)
    if students_df.empty:
        st.warning("No students found linked to this phone number.")
        return

    ids = tuple(str(i) for i in students_df["id"])
    hw_all = load_parent_grades(db, "homework", ids)
    qz_all = load_parent_grades(db, "quiz", ids)

    def subset(df, sid):
        if df.empty:
            return df
        return df[df["student_id"].astype(str) == sid]

    for _, student in students_df.iterrows():
        sid = str(student["id"])
        parent_student_fragment(db, student, subset(hw_all, sid), subset(qz_all, sid))
