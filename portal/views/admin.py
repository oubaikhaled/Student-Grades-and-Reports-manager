import streamlit as st

from portal.locks import release_my_student_locks
from portal.views.broadcast import render_whatsapp_broadcast
from portal.views.homeworks import render_manage_homeworks
from portal.views.quizzes import render_manage_quizzes
from portal.views.students import render_manage_students
from student_profile import StudentProfileView
from whatsapp_automation import AutomatedMessagesView


def render_admin_portal(db, auth):
    nav_options = [
        "📚 Manage Homeworks",
        "📝 Manage Quizzes",
        "👥 Manage Students",
        "👤 Student Profiles",
        "🤖 Automated Messages",
        "💬 WhatsApp Broadcast",
    ]

    st.session_state.setdefault("admin_nav_menu", nav_options[0])

    def _sync_from_sidebar():
        st.session_state["admin_nav_menu"] = st.session_state["nav_sidebar_key"]

    def _sync_from_top():
        st.session_state["admin_nav_menu"] = st.session_state["nav_top_key"]

    def _admin_logout():
        release_my_student_locks()
        auth.logout()

    # 1. Sidebar Navigation (for Desktop users)
    st.sidebar.markdown("### 🎓 Eng. Mahmoud Adel")
    st.sidebar.caption("Grade Management Portal")
    st.sidebar.radio(
        "Navigation",
        nav_options,
        index=nav_options.index(st.session_state["admin_nav_menu"]),
        key="nav_sidebar_key",
        on_change=_sync_from_sidebar,
    )
    st.sidebar.divider()
    st.sidebar.button("🚪 Logout", on_click=_admin_logout, type="primary", use_container_width=True, key="logout_side")

    # 2. Top Navigation Bar (Always visible on Mobile even when sidebar is collapsed)
    col_title, col_nav, col_out = st.columns([2.2, 2, 0.8])
    with col_title:
        st.subheader("🎓 Eng. Mahmoud Adel — Portal")
    with col_nav:
        st.selectbox(
            "📌 Quick Menu (Mobile & Desktop)",
            nav_options,
            index=nav_options.index(st.session_state["admin_nav_menu"]),
            key="nav_top_key",
            on_change=_sync_from_top,
            label_visibility="collapsed",
        )
    with col_out:
        st.button("🚪 Logout", on_click=_admin_logout, type="secondary", use_container_width=True, key="logout_top")

    menu = st.session_state["admin_nav_menu"]

    flash = st.session_state.pop("flash", None)
    if flash:
        st.success(flash)

    if menu == "📚 Manage Homeworks":
        render_manage_homeworks(db)
    elif menu == "📝 Manage Quizzes":
        render_manage_quizzes(db)
    elif menu == "👥 Manage Students":
        render_manage_students(db)
    elif menu == "👤 Student Profiles":
        StudentProfileView(db).render()
    elif menu == "💬 WhatsApp Broadcast":
        render_whatsapp_broadcast(db)
    elif menu == "🤖 Automated Messages":
        AutomatedMessagesView(db).render()
