import streamlit as st

class AuthManager:
    def __init__(self):
        if "logged_in" not in st.session_state:
            self.logout()

    @staticmethod
    def logout():
        st.session_state.update({"logged_in": False, "role": None, "identifier": None})

    def login_parent(self, db_manager, phone):
        count = db_manager.execute_scalar("SELECT COUNT(*) FROM students WHERE phone_parent = %s", (phone,))
        if count > 0:
            st.session_state.update({"logged_in": True, "role": "parent", "identifier": phone})
            st.rerun()
        else:
            st.error("Phone number not found in the database.")

    def login_admin(self, admin_num, admin_pass):
        valid = any(
            admin_num == str(a["number"]) and admin_pass == a["password"]
            for a in st.secrets.get("admins", [])
        )
        if valid:
            st.session_state.update({"logged_in": True, "role": "admin"})
            st.rerun()
        else:
            st.error("Invalid admin credentials.")