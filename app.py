import streamlit as st

from auth import AuthManager
from portal.data import get_db
from portal.views.admin import render_admin_portal
from portal.views.parent import render_parent_portal
from portal.views.public import render_admin_login, render_public_login
from whatsapp_automation import start_background_automation_daemon

# 1. Page Config MUST be the very first Streamlit command
st.set_page_config(
    page_title="Eng. Mahmoud Adel — Grade Portal",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
class GradePortalApp:
    def __init__(self):
        self.db = get_db()
        self.auth = AuthManager()
        start_background_automation_daemon(self.db)

    def run(self):
        query_params = st.query_params
        is_admin_route = "admin" in query_params

        if "logged_in" not in st.session_state or not st.session_state.logged_in:
            if is_admin_route:
                render_admin_login(self.auth)
            else:
                render_public_login(self.db, self.auth)
        elif st.session_state.role == "parent":
            render_parent_portal(self.db, self.auth)
        elif st.session_state.role == "admin":
            render_admin_portal(self.db, self.auth)


if __name__ == "__main__":
    app = GradePortalApp()
    app.run()
