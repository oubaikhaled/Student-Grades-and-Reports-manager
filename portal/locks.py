import threading
import time
import uuid

import streamlit as st

from portal.config import LOCK_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Concurrent Grading Locks & Shared Resources
# ---------------------------------------------------------------------------
def get_grader_session_id() -> str:
    """Unique ID for the current browser tab/session."""
    if "grader_session_id" not in st.session_state:
        admin_label = st.session_state.get("admin_id", "Admin")
        st.session_state["grader_session_id"] = f"{admin_label}_{uuid.uuid4().hex[:8]}"
    return st.session_state["grader_session_id"]


@st.cache_resource(show_spinner=False)
def get_grading_locks():
    """
    Server-wide singleton shared across ALL concurrent admin browser sessions.
    Maps (kind, int(item_id), str(student_id)) -> {"session_id": str, "timestamp": float}
    """
    return {"locks": {}, "mutex": threading.Lock()}


def sync_and_claim_student_lock(kind: str, item_id: int, selected_student_id: str | None) -> tuple[bool, set[str]]:
    """
    1. Purges expired locks (>10 mins old).
    2. Releases any previous lock held by THIS session on this assignment if the admin switched rows or unchecked.
    3. Attempts to claim the lock for `selected_student_id`.
    Returns: (claimed_by_me: bool, all_locked_student_ids_for_this_item: set[str])
    """
    mgr = get_grading_locks()
    my_session = get_grader_session_id()
    now = time.time()
    item_id = int(item_id)

    with mgr["mutex"]:
        locks = mgr["locks"]

        # 1. Remove expired locks and any lock held by THIS session for a different student
        to_delete = []
        for (k, iid, sid), info in locks.items():
            if now - info["timestamp"] > LOCK_TIMEOUT_SECONDS:
                to_delete.append((k, iid, sid))
            elif info["session_id"] == my_session and (k != kind or iid != item_id or sid != str(selected_student_id)):
                to_delete.append((k, iid, sid))

        for key in to_delete:
            locks.pop(key, None)

        # 2. Try to claim the newly clicked student
        claimed_by_me = False
        if selected_student_id is not None:
            target_key = (kind, item_id, str(selected_student_id))
            existing = locks.get(target_key)
            if existing is None or existing["session_id"] == my_session:
                locks[target_key] = {"session_id": my_session, "timestamp": now}
                claimed_by_me = True

        # 3. Collect all currently locked student IDs for this assignment
        locked_sids = {
            sid for (k, iid, sid) in locks.keys()
            if k == kind and iid == item_id
        }

        return claimed_by_me, locked_sids


def release_my_student_locks():
    """Releases all active grading locks held by the current admin session (on Save, Cancel, or Logout)."""
    mgr = get_grading_locks()
    my_session = st.session_state.get("grader_session_id")
    if not my_session:
        return
    with mgr["mutex"]:
        locks = mgr["locks"]
        to_delete = [k for k, v in locks.items() if v["session_id"] == my_session]
        for k in to_delete:
            locks.pop(k, None)
