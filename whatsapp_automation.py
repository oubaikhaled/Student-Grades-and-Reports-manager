import datetime
import io
import os
import random
import re
import threading
import time
from zoneinfo import ZoneInfo

import emoji
import pandas as pd
import requests
import streamlit as st

from pdf_utils import PDFGenerator

DEFAULT_GOWA_URL = "https://gowa-d0mc.onrender.com"
CAIRO_TZ = ZoneInfo("Africa/Cairo")

DAYS_OF_WEEK = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAY_FULL_NAMES = {
    "Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday",
    "Thu": "Thursday", "Fri": "Friday", "Sat": "Saturday", "Sun": "Sunday"
}

EXCITING_MORNING_SENTENCES = [
    "🚀 Let's make today an unforgettable day of learning and breakthroughs, Eng. Mahmoud!",
    "🌟 Every derivative solved today is a step closer to your students' dreams — have a powerhouse day!",
    "🔥 Another day to inspire the top Calculus minds of 2027 — let's crush today's sessions!",
    "⚡ Great teachers don't just teach math; they build problem-solvers. Have an amazing day ahead!",
    "🏆 Today's energy sets the standard for excellence — wishing you smooth lectures and brilliant students!",
    "✨ Coffee is hot, the markers are ready, and the Calculus legends are waiting — let's go!",
    "🎯 Precision, passion, and progress! Wishing you a super productive and rewarding day of lectures!",
]


def _get_secret_or_env(key: str, default: str = "") -> str:
    """Safely reads from st.secrets when inside Streamlit, falling back to os.environ."""
    val = ""
    try:
        val = st.secrets.get(key, "")
    except Exception:
        val = ""
    if not val:
        val = os.environ.get(key, default)
    return str(val).strip() if val is not None else default


@st.cache_resource(show_spinner=False)
def get_daemon_runtime():
    """
    Server-wide persistent singleton that survives Streamlit script reruns and module reloads.
    Holds the lock, cached secrets, and diagnostic logs.
    """
    return {
        "lock": threading.Lock(),
        "daemon_started": False,
        "last_tick": None,
        "last_tick_ts": 0.0,
        "last_error": None,
        "last_action": "Initialized — waiting for first check...",
        "last_grade_status": "No grade saved in this session yet.",
        "last_hw_sub_status": "No homework submission alert sent in this session yet.",
        "cached_url": DEFAULT_GOWA_URL,
        "cached_auth": "",
        "cached_teacher_phone": "",
        "cached_assistant_phone": "",
    }


def _sync_secrets_to_cache_and_db(db=None):
    """
    Safely reads st.secrets (or environment variables) and stores them in the
    @st.cache_resource singleton + database so background/external processes never lose credentials.
    """
    rt = get_daemon_runtime()
    try:
        s_url = _get_secret_or_env("WHATSAPP_API_URL", "")
        s_auth = _get_secret_or_env("WHATSAPP_API_AUTH", "")
        s_tphone = _get_secret_or_env("TEACHER_PHONE", "")
        s_aphone = _get_secret_or_env("ASSISTANT_PHONE", "")
        with rt["lock"]:
            if s_url:
                rt["cached_url"] = s_url
            if s_auth:
                rt["cached_auth"] = s_auth
            if s_tphone:
                rt["cached_teacher_phone"] = s_tphone
            if s_aphone:
                rt["cached_assistant_phone"] = s_aphone
    except Exception:
        pass

    if db is not None:
        try:
            if not get_bot_setting(db, "gowa_url", "") and rt["cached_url"]:
                set_bot_setting(db, "gowa_url", rt["cached_url"])
            if not get_bot_setting(db, "gowa_auth", "") and rt["cached_auth"]:
                set_bot_setting(db, "gowa_auth", rt["cached_auth"])
            if not get_bot_setting(db, "teacher_phone", "") and rt["cached_teacher_phone"]:
                set_bot_setting(db, "teacher_phone", rt["cached_teacher_phone"])
            if not get_bot_setting(db, "assistant_phone", "") and rt["cached_assistant_phone"]:
                set_bot_setting(db, "assistant_phone", rt["cached_assistant_phone"])
        except Exception:
            pass


def clean_wa_phone(p_str: str) -> str | None:
    """Normalizes Egyptian phone numbers to international format without '+' (e.g. '201012345678')."""
    if p_str is None or pd.isna(p_str):
        return None
    s = str(p_str).strip()
    if not s or s.lower() in ("nan", "none", "null", ""):
        return None
    if "@g.us" in s or "@s.whatsapp.net" in s:
        return s
    if s.endswith(".0"):
        s = s[:-2]
    digits = re.sub(r"\D", "", s)
    if not digits or len(digits) < 10:
        return None
    if len(digits) == 10 and digits.startswith("1"):
        return "20" + digits
    if digits.startswith("0020"):
        return digits[2:]
    if digits.startswith("0"):
        return "2" + digits
    if not digits.startswith("20"):
        return "20" + digits
    return digits


# ---------------------------------------------------------------------------
# GOWA Client (https://gowa-d0mc.onrender.com)
# ---------------------------------------------------------------------------
class GowaClient:
    def __init__(self, base_url: str = DEFAULT_GOWA_URL, basic_auth: str = ""):
        self.base_url = (base_url or DEFAULT_GOWA_URL).rstrip("/")
        self.auth = None
        if basic_auth and ":" in basic_auth:
            u, p = basic_auth.split(":", 1)
            self.auth = (u.strip(), p.strip())

    @classmethod
    def from_secrets(cls, db=None):
        rt = get_daemon_runtime()
        with rt["lock"]:
            url = rt.get("cached_url") or DEFAULT_GOWA_URL
            auth = rt.get("cached_auth") or ""

        s_url = _get_secret_or_env("WHATSAPP_API_URL", "")
        s_auth = _get_secret_or_env("WHATSAPP_API_AUTH", "")
        if s_url and url == DEFAULT_GOWA_URL:
            url = s_url
        if s_auth and not auth:
            auth = s_auth

        if db is not None:
            saved_url = get_bot_setting(db, "gowa_url", "")
            saved_auth = get_bot_setting(db, "gowa_auth", "")
            if saved_url:
                url = saved_url
            if saved_auth:
                auth = saved_auth
        return cls(base_url=url, basic_auth=auth)

    def check_status(self, timeout: int = 20) -> tuple[bool, str]:
        """Pings the GOWA server to check if it is awake and logged in."""
        try:
            resp = requests.get(f"{self.base_url}/app/devices", auth=self.auth, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results")
                if results:
                    return True, f"Connected ({len(results)} active device)"
                return False, "GOWA server is online, but no WhatsApp device is linked yet."
            return False, f"HTTP {resp.status_code}: {resp.text[:120]}"
        except Exception as e:
            return False, f"Connection error: {e}"

    def ensure_awake(self, max_wait_seconds: int = 60) -> tuple[bool, str]:
        """Wakes up Render GOWA instance if hibernating."""
        deadline = time.time() + max_wait_seconds
        last_err = "Unknown error"
        while time.time() < deadline:
            ok, msg = self.check_status(timeout=20)
            if ok:
                return True, msg
            last_err = msg
            if "no WhatsApp device is linked" in msg:
                return False, msg
            time.sleep(4)
        return False, f"Wake-up timeout: {last_err}"

    def send_text(self, recipient: str, message: str, retries: int = 4) -> tuple[bool, str]:
        """Sends a WhatsApp text message with automatic wake-up and progressive retry on failure."""
        target = clean_wa_phone(recipient)
        if not target:
            return False, f"Invalid phone/recipient: {recipient}"

        payload = {
            "phone": target,
            "message": message,
        }
        last_err = ""
        for attempt in range(retries + 1):
            try:
                resp = requests.post(
                    f"{self.base_url}/send/message",
                    json=payload,
                    auth=self.auth,
                    timeout=50,
                )
                if resp.status_code in (200, 201):
                    # Verify GOWA didn't return an internal error inside a 200 JSON payload
                    try:
                        body = resp.json()
                        code = str(body.get("code", "")).upper()
                        if code and code not in ("SUCCESS", "200", "OK", ""):
                            last_err = f"GOWA API error: {body.get('message') or body}"
                        else:
                            return True, "Sent"
                    except Exception:
                        return True, "Sent"
                else:
                    last_err = f"HTTP {resp.status_code}: {resp.text[:150]}"
            except Exception as e:
                last_err = str(e)

            if attempt < retries:
                # Wait progressively longer (4s, 8s, 12s, 16s) and ensure server is awake
                time.sleep(4 * (attempt + 1))
                self.ensure_awake(max_wait_seconds=25)

        return False, last_err

    def send_pdf(
        self, recipient: str, pdf_bytes: bytes, filename: str, caption: str = "", retries: int = 3
    ) -> tuple[bool, str]:
        """Sends a PDF file via GOWA /send/file with automatic wake-up and progressive retry on failure."""
        target = clean_wa_phone(recipient)
        if not target:
            return False, f"Invalid phone/recipient: {recipient}"

        safe_ascii_name = re.sub(r"[^A-Za-z0-9_\-.]", "_", str(filename or "Report.pdf"))
        if not safe_ascii_name.lower().endswith(".pdf"):
            safe_ascii_name += ".pdf"

        data = {
            "phone": target,
            "caption": caption,
        }
        last_err = ""
        for attempt in range(retries + 1):
            files = {
                "file": (safe_ascii_name, io.BytesIO(pdf_bytes), "application/pdf"),
            }
            try:
                resp = requests.post(
                    f"{self.base_url}/send/file",
                    data=data,
                    files=files,
                    auth=self.auth,
                    timeout=65,
                )
                if resp.status_code in (200, 201):
                    try:
                        body = resp.json()
                        code = str(body.get("code", "")).upper()
                        if code and code not in ("SUCCESS", "200", "OK", ""):
                            last_err = f"GOWA PDF error: {body.get('message') or body}"
                        else:
                            return True, "PDF Sent"
                    except Exception:
                        return True, "PDF Sent"
                else:
                    last_err = f"HTTP {resp.status_code}: {resp.text[:150]}"
            except Exception as e:
                last_err = str(e)

            if attempt < retries:
                time.sleep(5 * (attempt + 1))
                self.ensure_awake(max_wait_seconds=25)

        return False, last_err
# ---------------------------------------------------------------------------
# Database Schema & Helpers for Automation
# ---------------------------------------------------------------------------
def init_automation_tables(db):
    with db.get_connection() as conn:
        with conn.cursor() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS group_automation_config (
                    group_number TEXT PRIMARY KEY,
                    latest_hw_id INTEGER,
                    latest_quiz_id INTEGER,
                    wa_group_jid TEXT
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS group_meeting_times (
                    meeting_id SERIAL PRIMARY KEY,
                    group_number TEXT NOT NULL,
                    day_of_week TEXT NOT NULL,
                    meeting_time TEXT NOT NULL,
                    UNIQUE (group_number, day_of_week, meeting_time)
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS automated_dispatch_log (
                    dispatch_key TEXT PRIMARY KEY,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS whatsapp_retry_queue (
                    queue_id SERIAL PRIMARY KEY,
                    recipient TEXT NOT NULL,
                    message_type TEXT NOT NULL, -- 'text' or 'pdf_grade'
                    message_text TEXT,
                    kind TEXT,
                    item_id INTEGER,
                    student_id TEXT,
                    student_name TEXT,
                    item_title TEXT,
                    score REAL,
                    max_score REAL,
                    perc REAL,
                    report_text TEXT,
                    video_link TEXT,
                    attempts INTEGER DEFAULT 0,
                    last_error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS assistants (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    phone TEXT NOT NULL UNIQUE,
                    notify_hw_submit BOOLEAN DEFAULT TRUE,
                    notify_daily_schedule BOOLEAN DEFAULT TRUE,
                    notify_grade_submitted BOOLEAN DEFAULT FALSE
                )
                """
            )
        conn.commit()


def get_bot_setting(db, key: str, default: str = "") -> str:
    try:
        val = db.execute_scalar(
            "SELECT setting_value FROM bot_settings WHERE setting_key = %s", (key,)
        )
        return str(val).strip() if val is not None and str(val).strip() != "" else default
    except Exception:
        return default


def set_bot_setting(db, key: str, value: str):
    db.execute_query(
        """
        INSERT INTO bot_settings (setting_key, setting_value)
        VALUES (%s, %s)
        ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value
        """,
        (key, str(value).strip()),
    )


def get_resolved_teacher_phone(db) -> str | None:
    """Gets teacher phone from DB first, then singleton cache, then st.secrets / env."""
    t_phone = get_bot_setting(db, "teacher_phone", "")
    if not t_phone:
        rt = get_daemon_runtime()
        with rt["lock"]:
            t_phone = rt.get("cached_teacher_phone", "")
    if not t_phone:
        t_phone = _get_secret_or_env("TEACHER_PHONE", "")
    return clean_wa_phone(t_phone)


def get_resolved_assistant_phone(db) -> str | None:
    """Gets assistant phone from DB first, then singleton cache, then st.secrets / env."""
    a_phone = get_bot_setting(db, "assistant_phone", "")
    if not a_phone:
        rt = get_daemon_runtime()
        with rt["lock"]:
            a_phone = rt.get("cached_assistant_phone", "")
    if not a_phone:
        a_phone = _get_secret_or_env("ASSISTANT_PHONE", "")
    return clean_wa_phone(a_phone)


def get_eligible_assistants(db, perm_col: str) -> list[tuple[str, str]]:
    """
    Returns a list of (name, clean_phone) for assistants who have `perm_col` = TRUE.
    Only falls back to legacy single `assistant_phone` if the `assistants` table has 0 rows total.
    """
    results = []
    seen_phones = set()
    total_assistants_in_table = 0
    try:
        cnt = db.execute_scalar("SELECT COUNT(*) FROM assistants")
        total_assistants_in_table = int(cnt or 0)
        df = db.fetch_dataframe(
            f"SELECT name, phone FROM assistants WHERE COALESCE({perm_col}, FALSE) = TRUE ORDER BY id ASC"
        )
        for _, r in df.iterrows():
            p = clean_wa_phone(r.get("phone"))
            if p and p not in seen_phones:
                seen_phones.add(p)
                results.append((str(r.get("name") or "Assistant").strip(), p))
    except Exception as e:
        print(f"[Assistants] Query error ({perm_col}): {e}")

    if total_assistants_in_table == 0 and not results and perm_col == "notify_hw_submit":
        fallback_p = get_resolved_assistant_phone(db)
        if fallback_p and fallback_p not in seen_phones:
            results.append(("Assistant", fallback_p))

    return results


def get_effective_group_pace(
    cfg_row: dict | pd.Series | None,
    default_latest_hw_id: int | None,
    default_latest_qz_id: int | None,
) -> tuple[int | None, int | None, bool, bool]:
    """
    Returns (effective_hw_id, effective_qz_id, is_hw_manual, is_qz_manual).
    Automatically falls back to the newest created HW / Quiz unless manually overridden.
    """
    manual_hw = None
    manual_qz = None
    if cfg_row is not None:
        raw_hw = cfg_row.get("latest_hw_id")
        if pd.notna(raw_hw) and int(raw_hw) > 0:
            manual_hw = int(raw_hw)
        raw_qz = cfg_row.get("latest_quiz_id")
        if pd.notna(raw_qz) and int(raw_qz) > 0:
            manual_qz = int(raw_qz)

    eff_hw = manual_hw if manual_hw is not None else default_latest_hw_id
    eff_qz = manual_qz if manual_qz is not None else default_latest_qz_id
    return eff_hw, eff_qz, (manual_hw is not None), (manual_qz is not None)


def has_dispatched(db, dispatch_key: str) -> bool:
    try:
        cnt = db.execute_scalar(
            "SELECT COUNT(*) FROM automated_dispatch_log WHERE dispatch_key = %s",
            (dispatch_key,),
        )
        return bool(cnt and int(cnt) > 0)
    except Exception:
        return False


def mark_dispatched(db, dispatch_key: str):
    try:
        db.execute_query(
            "INSERT INTO automated_dispatch_log (dispatch_key) VALUES (%s) ON CONFLICT DO NOTHING",
            (dispatch_key,),
        )
    except Exception:
        pass


def claim_dispatch(db, key: str) -> bool:
    """Atomically claims a dispatch key so concurrent or overlapping runs never double-send."""
    try:
        with db.get_connection() as conn:
            with conn.cursor() as c:
                c.execute(
                    "INSERT INTO automated_dispatch_log (dispatch_key) VALUES (%s) ON CONFLICT DO NOTHING",
                    (key,),
                )
                claimed = c.rowcount == 1
            conn.commit()
        return claimed
    except Exception as e:
        print(f"[Dispatch] Failed to claim key '{key}': {e}")
        return False


def release_dispatch(db, key: str):
    """Releases a claimed dispatch key if sending failed so the next tick can retry."""
    try:
        db.execute_query(
            "DELETE FROM automated_dispatch_log WHERE dispatch_key = %s",
            (key,),
        )
    except Exception as e:
        print(f"[Dispatch] Failed to release key '{key}': {e}")


# ---------------------------------------------------------------------------
# Safe PDF Generator Helper
# ---------------------------------------------------------------------------
def _safe_generate_student_pdf(
    student_name: str,
    item_title: str,
    score: float,
    max_score: float,
    perc: float,
    report_text: str,
    img_bytes: bytes | None,
    video_link: str | None = None,
) -> bytes | None:
    try:
        if video_link:
            try:
                buf = PDFGenerator.generate_student_report(
                    student_name, item_title, score, max_score, perc, report_text, img_bytes, video_link=video_link
                )
            except TypeError:
                buf = PDFGenerator.generate_student_report(
                    student_name, item_title, score, max_score, perc, report_text, img_bytes
                )
        else:
            buf = PDFGenerator.generate_student_report(
                student_name, item_title, score, max_score, perc, report_text, img_bytes
            )
        return buf.getvalue() if hasattr(buf, "getvalue") else bytes(buf)
    except Exception as e:
        rt = get_daemon_runtime()
        with rt["lock"]:
            rt["last_error"] = f"PDF Generation Error ({student_name}): {e}"
        return None


# ---------------------------------------------------------------------------
# Instant Homework Submission Alert to Assistant (Atomic Single-Send)
# ---------------------------------------------------------------------------
def enqueue_failed_whatsapp(
    db,
    recipient: str,
    message_type: str,
    message_text: str = "",
    kind: str = "",
    item_id: int = 0,
    student_id: str = "",
    student_name: str = "",
    item_title: str = "",
    score: float = 0.0,
    max_score: float = 0.0,
    perc: float = 0.0,
    report_text: str = "",
    video_link: str = "",
    last_error: str = "",
):
    """Saves a failed text or PDF report into the DB so the 60s daemon retries it until delivered."""
    try:
        db.execute_query(
            """
            INSERT INTO whatsapp_retry_queue (
                recipient, message_type, message_text, kind, item_id, student_id,
                student_name, item_title, score, max_score, perc, report_text, video_link,
                attempts, last_error
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s)
            """,
            (
                recipient, message_type, message_text, kind, int(item_id or 0), str(student_id or ""),
                student_name, item_title, float(score or 0), float(max_score or 0), float(perc or 0),
                report_text or "", video_link or "", str(last_error or ""),
            ),
        )
    except Exception as e:
        print(f"[RetryQueue] Could not enqueue failed message for {recipient}: {e}")


def process_whatsapp_retry_queue(db, client: GowaClient, rt):
    """
    Called every 60 seconds by `run_scheduled_checks_once`.
    Retries any failed WhatsApp texts or PDF grade reports up to 15 times before expiring.
    """
    try:
        q_df = db.fetch_dataframe(
            "SELECT * FROM whatsapp_retry_queue WHERE attempts < 15 ORDER BY queue_id ASC LIMIT 10"
        )
    except Exception:
        return

    if q_df.empty:
        return

    client.ensure_awake(max_wait_seconds=30)

    for _, row in q_df.iterrows():
        qid = int(row["queue_id"])
        recipient = str(row["recipient"])
        mtype = str(row["message_type"])
        attempts = int(row.get("attempts") or 1)

        if mtype == "text":
            ok, err = client.send_text(recipient, str(row.get("message_text") or ""), retries=1)
        elif mtype == "pdf_grade":
            kind = str(row.get("kind") or "homework")
            item_id = int(row.get("item_id") or 0)
            student_id = str(row.get("student_id") or "")
            student_name = str(row.get("student_name") or "")
            item_title = str(row.get("item_title") or "")
            score = float(row.get("score") or 0.0)
            max_score = float(row.get("max_score") or 1.0)
            perc = float(row.get("perc") or 0.0)
            report_text = str(row.get("report_text") or "")
            video_link = str(row.get("video_link") or "") or None

            # Fetch stored report_image from DB if available
            img_bytes = None
            try:
                table = "homework_grades" if kind == "homework" else "quiz_grades"
                icol = "homework_id" if kind == "homework" else "quiz_id"
                img_df = db.fetch_dataframe(
                    f"SELECT report_image FROM {table} WHERE {icol} = %s AND student_id = %s",
                    (item_id, student_id),
                )
                if not img_df.empty and img_df.iloc[0, 0] is not None and not isinstance(img_df.iloc[0, 0], float):
                    img_bytes = bytes(img_df.iloc[0, 0])
            except Exception:
                pass

            pdf_bytes = _safe_generate_student_pdf(
                student_name, item_title, score, max_score, perc, report_text, img_bytes, video_link
            )
            if pdf_bytes:
                pdf_filename = f"Student_{student_id}_{kind}_{item_id}_Report.pdf"
                ok, err = client.send_pdf(
                    recipient, pdf_bytes, pdf_filename, caption=f"📄 {student_name} — {item_title}", retries=1
                )
            else:
                ok, err = False, "PDF generation returned None"
        else:
            ok, err = True, "Unknown type"

        if ok:
            db.execute_query("DELETE FROM whatsapp_retry_queue WHERE queue_id = %s", (qid,))
            _record_scheduler_action(db, rt, f"Retry Succeeded -> {recipient} ({mtype})")
        else:
            db.execute_query(
                "UPDATE whatsapp_retry_queue SET attempts = %s, last_error = %s WHERE queue_id = %s",
                (attempts + 1, str(err)[:200], qid),
            )
            _record_scheduler_error(db, rt, f"Retry #{attempts + 1} failed for {recipient}: {err}")
        time.sleep(2)
def trigger_hw_submission_assistant_whatsapp(
    db,
    student_id: str,
    homework_id: int | None = None,
    homework_title: str | None = None,
    student_name: str | None = None,
    group_number: str | None = None,
):
    _sync_secrets_to_cache_and_db(db)
    rt = get_daemon_runtime()

    # Strict deduplication key
    clean_hw_id = int(homework_id) if homework_id is not None else 0
    clean_stu_id = str(student_id or "").strip()
    dedup_id = f"hw_{clean_hw_id}_stu_{clean_stu_id}"
    
    now_ts = time.time()
    with rt["lock"]:
        recent_subs = rt.setdefault("recent_hw_subs", {})
        # Increased cooldown window to 60 seconds to completely block double-clicks
        if now_ts - recent_subs.get(dedup_id, 0.0) < 60.0:
            print(f"[Deduplication] Blocked duplicate assistant alert for {dedup_id}")
            return
        recent_subs[dedup_id] = now_ts

    recipients = get_eligible_assistants(db, "notify_hw_submit")
    if not recipients:
        with rt["lock"]:
            rt["last_hw_sub_status"] = "Skipped (No assistants have 'HW Submits' enabled in Settings)"
        return
        recent_subs[dedup_id] = now_ts

    recipients = get_eligible_assistants(db, "notify_hw_submit")
    if not recipients:
        with rt["lock"]:
            rt["last_hw_sub_status"] = "Skipped (No assistants have 'HW Submits' enabled in Settings)"
        return

    if not student_name or not group_number:
        try:
            stu_df = db.fetch_dataframe(
                "SELECT name, group_number FROM students WHERE id = %s",
                (str(student_id),),
            )
            if not stu_df.empty:
                student_name = student_name or str(stu_df.iloc[0]["name"])
                group_number = group_number or str(stu_df.iloc[0].get("group_number") or "N/A").strip()
        except Exception:
            pass

    student_name = student_name or f"Student #{student_id}"
    group_number = group_number or "N/A"

    if not homework_title and homework_id is not None:
        try:
            hw_df = db.fetch_dataframe(
                "SELECT title FROM homeworks WHERE homework_id = %s",
                (int(homework_id),),
            )
            if not hw_df.empty:
                homework_title = str(hw_df.iloc[0]["title"])
        except Exception:
            pass

    homework_title = homework_title or (f"Homework #{homework_id}" if homework_id else "Homework")

    # Also mark in DB so if the background daemon still has the old check, it won't double-send
    if homework_id is not None:
        mark_dispatched(db, f"asst_hw_sub_hw_{int(homework_id)}_stu_{str(student_id).strip()}")

    client = GowaClient.from_secrets(db)
    submitted_at_str = datetime.datetime.now(CAIRO_TZ).strftime("%Y-%m-%d %H:%M")

    def _worker():
        try:
            sent_to = []
            errors = []
            for asst_name, asst_phone in recipients:
                msg = emoji.emojize(
                    f":inbox_tray: *تسليم واجب جديد للمراجعة والتصحيح*\n\n"
                    f"👩‍🏫 *تنبيه يا {asst_name}:*\n"
                    f"قام الطالب *{student_name}* (كود: `{student_id}` — مجموعة *{group_number}*) بتسليم الواجب:\n"
                    f"📘 *{homework_title}*\n"
                    f"🕒 *وقت التسليم:* {submitted_at_str}\n\n"
                    f"برجاء الدخول على المنصة لمراجعة الواجب وتصحيحه. :check_mark_button:\n\n"
                    f"Mathematics Team – Eng. Mahmoud Adel"
                )
                ok, err = client.send_text(asst_phone, msg, retries=3)
                if ok:
                    sent_to.append(f"{asst_name}({asst_phone})")
                else:
                    errors.append(f"{asst_name}: {err}")
                    try:
                        enqueue_failed_whatsapp(db, asst_phone, "text", message_text=msg, last_error=err)
                    except Exception:
                        pass

            with rt["lock"]:
                if sent_to:
                    rt["last_hw_sub_status"] = f"Sent to {', '.join(sent_to)} for {student_name} ({homework_title})"
                    rt["last_action"] = f"HW Sub Alert -> {len(sent_to)} Assistant(s)"
                else:
                    rt["last_hw_sub_status"] = f"Failed (Queued Retry) for {student_name}: {'; '.join(errors)}"
                    rt["last_error"] = f"Assistant HW Sub Alert Error: {'; '.join(errors)}"
        except Exception as e:
            with rt["lock"]:
                rt["last_error"] = f"Assistant HW Sub Exception ({student_name}): {e}"
                rt["last_hw_sub_status"] = f"Exception for {student_name}: {e}"

    threading.Thread(target=_worker, daemon=True).start()

    def _worker():
        try:
            client.ensure_awake(max_wait_seconds=45)

            sent_to = []
            errors = []
            for asst_name, asst_phone in recipients:
                msg = emoji.emojize(
                    f":inbox_tray: *تسليم واجب جديد للمراجعة والتصحيح*\n\n"
                    f"👩‍🏫 *تنبيه يا {asst_name}:*\n"
                    f"قام الطالب *{student_name}* (كود: `{student_id}` — مجموعة *{group_number}*) بتسليم الواجب:\n"
                    f"📘 *{homework_title}*\n"
                    f"🕒 *وقت التسليم:* {submitted_at_str}\n\n"
                    f"برجاء الدخول على المنصة لمراجعة الواجب وتصحيحه. :check_mark_button:\n\n"
                    f"Mathematics Team – Eng. Mahmoud Adel"
                )
                ok, err = client.send_text(asst_phone, msg)
                if ok:
                    sent_to.append(f"{asst_name}({asst_phone})")
                else:
                    errors.append(f"{asst_name}: {err}")

            with rt["lock"]:
                if sent_to:
                    rt["last_hw_sub_status"] = f"Sent to {', '.join(sent_to)} for {student_name} ({homework_title})"
                    rt["last_action"] = f"HW Sub Alert -> {len(sent_to)} Assistant(s)"
                else:
                    if sub_key:
                        release_dispatch(db, sub_key)
                    rt["last_hw_sub_status"] = f"Failed for {student_name}: {'; '.join(errors)}"
                    rt["last_error"] = f"Assistant HW Sub Alert Error: {'; '.join(errors)}"
        except Exception as e:
            if sub_key:
                release_dispatch(db, sub_key)
            with rt["lock"]:
                rt["last_error"] = f"Assistant HW Sub Exception ({student_name}): {e}"
                rt["last_hw_sub_status"] = f"Exception for {student_name}: {e}"

    threading.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Instant Post-Grading Parent, Student & Assistant Sender
# ---------------------------------------------------------------------------
def trigger_post_grade_whatsapp(
    db,
    kind: str,
    item_id: int,
    item_title: str,
    student_id: str,
    score: float,
    max_score: float,
    perc: float,
    report_text: str,
    img_bytes: bytes | None = None,
):
    """
    Runs immediately after an admin clicks Save Grade.
    Fetches all DB records on the main thread BEFORE Streamlit reruns,
    then wakes GOWA and dispatches messages + PDF in the background.
    """
    _sync_secrets_to_cache_and_db(db)
    rt = get_daemon_runtime()

    auto_enabled = get_bot_setting(db, "auto_send_on_grade", "true").lower() == "true"
    also_student = get_bot_setting(db, "also_send_grade_to_student", "false").lower() == "true"
    grade_assistants = get_eligible_assistants(db, "notify_grade_submitted")

    if not auto_enabled and not grade_assistants:
        with rt["lock"]:
            rt["last_grade_status"] = "Skipped (auto_send_on_grade is disabled & no assistants subscribed)"
        return

    try:
        stu_df = db.fetch_dataframe(
            "SELECT name, phone, phone_parent, group_number FROM students WHERE id = %s",
            (str(student_id),),
        )
        if stu_df.empty:
            with rt["lock"]:
                rt["last_grade_status"] = f"Student ID {student_id} not found in DB."
            return
        stu = stu_df.iloc[0]
        student_name = str(stu["name"])
        group_number = str(stu.get("group_number") or "N/A").strip()
        parent_phone = clean_wa_phone(stu.get("phone_parent"))
        student_phone = clean_wa_phone(stu.get("phone"))
    except Exception as e:
        with rt["lock"]:
            rt["last_grade_status"] = f"DB fetch error on grade save: {e}"
        return

    video_link = None
    if kind == "homework":
        try:
            hw_df = db.fetch_dataframe(
                "SELECT video_link FROM homeworks WHERE homework_id = %s", (int(item_id),)
            )
            if not hw_df.empty and pd.notna(hw_df.iloc[0]["video_link"]):
                video_link = str(hw_df.iloc[0]["video_link"]).strip() or None
        except Exception:
            pass

    client = GowaClient.from_secrets(db)

    def _worker():
        try:
            client.ensure_awake(max_wait_seconds=45)

            score_str = str(int(score)) if float(score).is_integer() else f"{score:.1f}"
            max_str = str(int(max_score)) if float(max_score).is_integer() else f"{max_score:.1f}"
            type_label = "Quiz" if kind == "quiz" else "Homework"

            status_parts = []

            if auto_enabled:
                parent_msg = emoji.emojize(
                    f":bar_chart: درجة الـ {item_title}\n\n"
                    f"ولي الأمر الكريم،\n"
                    f"نحيط حضرتكم علمًا بأن الطالب *{student_name}* حصل على *{score_str} / {max_str}* ({perc:.1f}%) في الـ {type_label} الأخير.\n\n"
                    f"مرفق لحضرتكم تقرير الأداء التفصيلي (PDF).\n"
                    f"نتمنى له مزيدًا من التقدم والنجاح! :glowing_star:\n\n"
                    f"Mathematics Team – Eng. Mahmoud Adel"
                )

                stu_msg = emoji.emojize(
                    f":bar_chart: درجة الـ {item_title}\n\n"
                    f"أهلاً بك يا *{student_name}*،\n"
                    f"لقد حصلت على *{score_str} / {max_str}* ({perc:.1f}%) في الـ {type_label} الأخير.\n"
                    f"مرفق تقرير الملاحظات الخاص بك. استمر في الاجتهاد! :glowing_star:\n\n"
                    f"Mathematics Team – Eng. Mahmoud Adel"
                )

                pdf_bytes = _safe_generate_student_pdf(
                    student_name, item_title, score, max_score, perc, report_text, img_bytes, video_link
                )
                pdf_filename = f"Student_{student_id}_{kind}_{item_id}_Report.pdf"

                # 1. Send to Parent
                if parent_phone:
                    ok_txt, err_txt = client.send_text(parent_phone, parent_msg)
                    if not ok_txt:
                        enqueue_failed_whatsapp(
                            db, parent_phone, "text", message_text=parent_msg, last_error=err_txt
                        )

                    if pdf_bytes:
                        ok_pdf, err_pdf = client.send_pdf(
                            parent_phone, pdf_bytes, pdf_filename, caption=f"📄 {student_name} — {item_title}"
                        )
                        if not ok_pdf:
                            enqueue_failed_whatsapp(
                                db, parent_phone, "pdf_grade", kind=kind, item_id=item_id,
                                student_id=student_id, student_name=student_name, item_title=item_title,
                                score=score, max_score=max_score, perc=perc, report_text=report_text,
                                video_link=video_link or "", last_error=err_pdf,
                            )
                        status_parts.append(
                            f"Parent({parent_phone}): Txt={'OK' if ok_txt else 'QueuedRetry'}, PDF={'OK' if ok_pdf else 'QueuedRetry'}"
                        )
                    else:
                        status_parts.append(
                            f"Parent({parent_phone}): Txt={'OK' if ok_txt else 'QueuedRetry'}"
                        )
                else:
                    status_parts.append("Parent: No valid phone")

                # 2. Send to Student
                if student_phone and (also_student or not parent_phone):
                    ok_stxt, err_stxt = client.send_text(student_phone, stu_msg)
                    if not ok_stxt:
                        enqueue_failed_whatsapp(
                            db, student_phone, "text", message_text=stu_msg, last_error=err_stxt
                        )

                    if pdf_bytes:
                        ok_spdf, err_spdf = client.send_pdf(
                            student_phone, pdf_bytes, pdf_filename, caption=f"📄 {student_name} — {item_title}"
                        )
                        if not ok_spdf:
                            enqueue_failed_whatsapp(
                                db, student_phone, "pdf_grade", kind=kind, item_id=item_id,
                                student_id=student_id, student_name=student_name, item_title=item_title,
                                score=score, max_score=max_score, perc=perc, report_text=report_text,
                                video_link=video_link or "", last_error=err_spdf,
                            )
                        status_parts.append(
                            f"Student({student_phone}): Txt={'OK' if ok_stxt else 'QueuedRetry'}, PDF={'OK' if ok_spdf else 'QueuedRetry'}"
                        )
                    else:
                        status_parts.append(
                            f"Student({student_phone}): Txt={'OK' if ok_stxt else 'QueuedRetry'}"
                        )

            # 3. Send to Eligible Assistants
            if grade_assistants:
                asst_sent = 0
                asst_queued = 0
                for asst_name, asst_phone in grade_assistants:
                    asst_msg = emoji.emojize(
                        f":check_mark_button: *تم رصد درجة جديدة*\n\n"
                        f"👩‍🏫 *إشعار يا {asst_name}:*\n"
                        f"تم تصحيح {type_label}: *{item_title}*\n"
                        f"الطالب: *{student_name}* (كود: `{student_id}` — مجموعة *{group_number}*)\n"
                        f"الدرجة: *{score_str} / {max_str}* ({perc:.1f}%)"
                    )
                    ok_a, err_a = client.send_text(asst_phone, asst_msg)
                    if ok_a:
                        asst_sent += 1
                    else:
                        asst_queued += 1
                        enqueue_failed_whatsapp(
                            db, asst_phone, "text", message_text=asst_msg, last_error=err_a
                        )
                if asst_queued > 0:
                    status_parts.append(f"Assistants: {asst_sent}/{len(grade_assistants)} sent ({asst_queued} queued for retry)")
                else:
                    status_parts.append(f"Assistants: {asst_sent}/{len(grade_assistants)} sent")

            summary = " | ".join(status_parts) if status_parts else "No recipients configured"
            with rt["lock"]:
                rt["last_grade_status"] = f"{student_name}: {summary}"
                rt["last_action"] = f"Post-Grade -> {student_name}"
        except Exception as e:
            with rt["lock"]:
                rt["last_error"] = f"Post-Grade Exception ({student_name}): {e}"
                rt["last_grade_status"] = f"Error for {student_name}: {e}"

    threading.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Reusable Morning Schedule Builder & Sender
# ---------------------------------------------------------------------------
def send_morning_schedule_to_teacher(db, client: GowaClient, teacher_phone_clean: str, now: datetime.datetime) -> tuple[bool, str]:
    """Builds and sends today's morning schedule + exciting sentence to the teacher and eligible assistants."""
    today_str = now.strftime("%Y-%m-%d")
    today_day = now.strftime("%a")

    meetings_df = db.fetch_dataframe(
        """
        SELECT m.meeting_id, TRIM(m.group_number) AS group_number, m.day_of_week, m.meeting_time,
               COALESCE(c.latest_hw_id, (SELECT MAX(homework_id) FROM homeworks)) AS latest_hw_id,
               COALESCE(c.latest_quiz_id, (SELECT MAX(quiz_id) FROM quizzes)) AS latest_quiz_id
        FROM group_meeting_times m
        LEFT JOIN group_automation_config c ON TRIM(m.group_number) = TRIM(c.group_number)
        WHERE m.day_of_week = %s
        ORDER BY m.meeting_time ASC
        """,
        (today_day,),
    )

    hw_map = {}
    hw_df = db.fetch_dataframe("SELECT homework_id, title FROM homeworks")
    for _, r in hw_df.iterrows():
        hw_map[int(r["homework_id"])] = str(r["title"])

    qz_map = {}
    qz_df = db.fetch_dataframe("SELECT quiz_id, title FROM quizzes")
    for _, r in qz_df.iterrows():
        qz_map[int(r["quiz_id"])] = str(r["title"])

    exciting_line = random.choice(EXCITING_MORNING_SENTENCES)

    if meetings_df.empty:
        msg = (
            f"☀️ *Good Morning, Eng. Mahmoud Adel!*\n"
            f"📅 *Date:* {today_str} ({DAY_FULL_NAMES.get(today_day, today_day)})\n\n"
            f"You have *no scheduled group sessions* registered for today.\n\n"
            f"{exciting_line}"
        )
    else:
        lines = [
            f"☀️ *Good Morning, Eng. Mahmoud Adel!*",
            f"📅 *Today's Schedule — {DAY_FULL_NAMES.get(today_day, today_day)} ({today_str}):*\n",
        ]
        for _, m_row in meetings_df.iterrows():
            grp = m_row["group_number"]
            mtime = m_row["meeting_time"]
            hw_id = m_row.get("latest_hw_id")
            qz_id = m_row.get("latest_quiz_id")
            hw_label = hw_map.get(int(hw_id), "Not Set") if pd.notna(hw_id) else "Not Set"
            qz_label = qz_map.get(int(qz_id), "Not Set") if pd.notna(qz_id) else "Not Set"
            lines.append(
                f"🕒 *{mtime}* — *Group {grp}*\n"
                f"   • 📝 HW: {hw_label}\n"
                f"   • 📊 Quiz: {qz_label}"
            )
        lines.append(f"\n{exciting_line}")
        msg = "\n".join(lines)

    # 1. Send to the Teacher
    ok, err = client.send_text(teacher_phone_clean, msg)

    # 2. Send to eligible Assistants
    try:
        asst_recipients = get_eligible_assistants(db, "notify_daily_schedule")
        for asst_name, asst_phone in asst_recipients:
            if teacher_phone_clean and asst_phone == teacher_phone_clean:
                continue
            asst_msg = msg.replace("Eng. Mahmoud Adel", asst_name)
            client.send_text(asst_phone, asst_msg)
    except Exception as e:
        print(f"Failed to send schedule to assistants: {e}")

    return ok, err


# ---------------------------------------------------------------------------
# Background Check: Pending Uncorrected Homework Submissions for Assistant
# ---------------------------------------------------------------------------
def check_and_notify_assistant_pending_submissions(
    db, client: GowaClient, assistant_phone_clean: str, now: datetime.datetime, rt
):
    recipients = get_eligible_assistants(db, "notify_hw_submit")
    if not recipients:
        return

    try:
        pending_df = db.fetch_dataframe(
            """
            SELECT hg.homework_id, hg.student_id, s.name AS student_name, s.group_number, h.title AS hw_title
            FROM homework_grades hg
            JOIN students s ON s.id = hg.student_id
            JOIN homeworks h ON h.homework_id = hg.homework_id
            WHERE COALESCE(hg.submitted, FALSE) = TRUE
              AND hg.correct_answers IS NULL
            """
        )
    except Exception as e:
        print(f"[Scheduler] Failed to query pending homework submissions: {e}")
        return

    if pending_df.empty:
        return

    now_fmt = now.strftime("%Y-%m-%d %H:%M")
    for _, row in pending_df.iterrows():
        hw_id = int(row["homework_id"])
        stu_id = str(row["student_id"]).strip()
        stu_name = str(row.get("student_name") or f"Student #{stu_id}")
        grp = str(row.get("group_number") or "N/A").strip()
        hw_title = str(row.get("hw_title") or f"Homework #{hw_id}")

        sub_key = f"asst_hw_sub_hw_{hw_id}_stu_{stu_id}"
        if claim_dispatch(db, sub_key):
            any_sent = False
            for asst_name, asst_phone in recipients:
                msg = emoji.emojize(
                    f":inbox_tray: *تسليم واجب جديد للمراجعة والتصحيح*\n\n"
                    f"👩‍🏫 *تنبيه يا {asst_name}:*\n"
                    f"قام الطالب *{stu_name}* (كود: `{stu_id}` — مجموعة *{grp}*) بتسليم الواجب:\n"
                    f"📘 *{hw_title}*\n"
                    f"🕒 *وقت الإشعار:* {now_fmt}\n\n"
                    f"برجاء الدخول على المنصة لمراجعة الواجب وتصحيحه. :check_mark_button:\n\n"
                    f"Mathematics Team – Eng. Mahmoud Adel"
                )
                ok, err = client.send_text(asst_phone, msg)
                if ok:
                    any_sent = True

            if any_sent:
                action_msg = f"Notified Assistants: HW submitted by {stu_name} ({hw_title})"
                _record_scheduler_action(db, rt, action_msg)
                with rt["lock"]:
                    rt["last_hw_sub_status"] = f"Sent to assistants for {stu_name}"
            else:
                release_dispatch(db, sub_key)
                err_msg = f"Assistant HW Sub Alert Failed ({stu_name})"
                _record_scheduler_error(db, rt, err_msg)
            time.sleep(3)


# ---------------------------------------------------------------------------
# Background Scheduler Tick
# ---------------------------------------------------------------------------
def _parse_hhmm_tolerant(time_str: str, default_hr: int = 8, default_min: int = 0) -> tuple[int, int]:
    try:
        m = re.match(r"^\s*(\d{1,2})\s*:\s*(\d{1,2})", str(time_str or ""))
        if m:
            hr, mn = int(m.group(1)), int(m.group(2))
            if 0 <= hr <= 23 and 0 <= mn <= 59:
                return hr, mn
    except Exception:
        pass
    return default_hr, default_min


def _record_scheduler_action(db, rt, action_msg: str):
    with rt["lock"]:
        rt["last_action"] = action_msg
    try:
        set_bot_setting(db, "last_scheduler_action", action_msg)
    except Exception:
        pass


def _record_scheduler_error(db, rt, err_msg: str):
    with rt["lock"]:
        rt["last_error"] = err_msg
    try:
        set_bot_setting(db, "last_scheduler_error", err_msg)
    except Exception:
        pass


def run_scheduled_checks_once(db):
    rt = get_daemon_runtime()
    now = datetime.datetime.now(CAIRO_TZ)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    today_str = now.strftime("%Y-%m-%d")
    today_day = now.strftime("%a")

    with rt["lock"]:
        rt["last_tick"] = now_str
        rt["last_tick_ts"] = time.time()

    try:
        set_bot_setting(db, "last_scheduler_tick", now_str)
    except Exception as e:
        print(f"[Scheduler] Warning: could not persist last_scheduler_tick: {e}")

    teacher_phone_clean = get_resolved_teacher_phone(db)
    assistant_phone_clean = get_resolved_assistant_phone(db)
    client = GowaClient.from_secrets(db)

    

    morning_time_str = get_bot_setting(db, "morning_schedule_time", "08:00")
    m_hr, m_min = _parse_hhmm_tolerant(morning_time_str, default_hr=8, default_min=0)
    morning_dt = now.replace(hour=m_hr, minute=m_min, second=0, microsecond=0)

    morning_key = f"morning_sched_{today_str}"
    if not teacher_phone_clean:
        print("[Scheduler] Morning schedule skipped: teacher phone not configured.")
    elif now >= morning_dt and (now - morning_dt).total_seconds() <= 28800:
        if claim_dispatch(db, morning_key):
            ok, err = send_morning_schedule_to_teacher(db, client, teacher_phone_clean, now)
            if ok:
                action_msg = f"Sent Morning Schedule ({today_str})"
                _record_scheduler_action(db, rt, action_msg)
            else:
                release_dispatch(db, morning_key)
                err_msg = f"Morning Schedule Failed: {err}"
                _record_scheduler_error(db, rt, err_msg)

    meetings_df = db.fetch_dataframe(
        """
        SELECT m.meeting_id, TRIM(m.group_number) AS group_number, m.day_of_week, m.meeting_time,
               COALESCE(c.latest_hw_id, (SELECT MAX(homework_id) FROM homeworks)) AS latest_hw_id,
               COALESCE(c.latest_quiz_id, (SELECT MAX(quiz_id) FROM quizzes)) AS latest_quiz_id,
               c.wa_group_jid
        FROM group_meeting_times m
        LEFT JOIN group_automation_config c ON TRIM(m.group_number) = TRIM(c.group_number)
        WHERE m.day_of_week = %s
        ORDER BY m.meeting_time ASC
        """,
        (today_day,),
    )
    if meetings_df.empty:
        return

    hw_map = {}
    hw_df = db.fetch_dataframe("SELECT homework_id, title, total_questions FROM homeworks")
    for _, r in hw_df.iterrows():
        hw_map[int(r["homework_id"])] = (str(r["title"]), int(r["total_questions"]))

    qz_map = {}
    qz_df = db.fetch_dataframe("SELECT quiz_id, title, max_score FROM quizzes")
    for _, r in qz_df.iterrows():
        qz_map[int(r["quiz_id"])] = (str(r["title"]), float(r["max_score"]))

    for _, m_row in meetings_df.iterrows():
        grp = str(m_row["group_number"]).strip()
        mtime = str(m_row["meeting_time"]).strip()
        try:
            hr, mn = [int(x) for x in mtime.split(":")[:2]]
            lecture_dt = now.replace(hour=hr, minute=mn, second=0, microsecond=0)
        except Exception:
            continue

        minutes_until_lecture = (lecture_dt - now).total_seconds() / 60.0
        hw_id = int(m_row["latest_hw_id"]) if pd.notna(m_row.get("latest_hw_id")) else None
        qz_id = int(m_row["latest_quiz_id"]) if pd.notna(m_row.get("latest_quiz_id")) else None

        pre10_key = f"teacher_10min_{today_str}_grp_{grp}_{mtime}"
        if teacher_phone_clean and -5 <= minutes_until_lecture <= 15:
            if claim_dispatch(db, pre10_key):
                ok = send_group_master_reports_to_teacher(
                    db, client, teacher_phone_clean, grp, mtime, hw_id, qz_id, hw_map, qz_map
                )
                if ok:
                    action_msg = f"Sent 10-min Report for Group {grp}"
                    _record_scheduler_action(db, rt, action_msg)
                else:
                    release_dispatch(db, pre10_key)
                    err_msg = f"10-min Report failed for Group {grp}"
                    _record_scheduler_error(db, rt, err_msg)

        rem6h_key = f"student_6hr_{today_str}_grp_{grp}_{mtime}"
        if (hw_id or qz_id) and 300 <= minutes_until_lecture <= 380:
            if claim_dispatch(db, rem6h_key):
                ok = send_6hr_unsubmitted_reminders(
                    db, client, grp, mtime, hw_id, qz_id, hw_map, qz_map
                )
                if ok:
                    action_msg = f"Sent 6-hr Reminders for Group {grp}"
                    _record_scheduler_action(db, rt, action_msg)
                else:
                    release_dispatch(db, rem6h_key)
                    err_msg = f"6-hr Reminders failed for Group {grp}"
                    _record_scheduler_error(db, rt, err_msg)


def send_group_master_reports_to_teacher(
    db, client: GowaClient, teacher_phone: str, grp: str, mtime: str,
    hw_id: int | None, qz_id: int | None, hw_map: dict, qz_map: dict
) -> bool:
    students_df = db.fetch_dataframe(
        "SELECT id, name FROM students WHERE TRIM(group_number) = %s ORDER BY name ASC",
        (grp,),
    )
    if students_df.empty:
        return False

    intro_msg = (
        f"⏰ *Upcoming Session in 10 Minutes!*\n"
        f"👥 *Group:* {grp} (at {mtime})\n"
        f"👨‍🎓 *Enrolled Students:* {len(students_df)}\n"
        f"Here are the latest Homework & Quiz reports for this group:"
    )
    ok_intro, _ = client.send_text(teacher_phone, intro_msg)
    any_sent = ok_intro

    if hw_id and hw_id in hw_map:
        hw_title, hw_total = hw_map[hw_id]
        g_df = db.fetch_dataframe(
            "SELECT student_id, correct_answers, percentage, COALESCE(submitted, FALSE) as submitted "
            "FROM homework_grades WHERE homework_id = %s",
            (hw_id,),
        )
        merged = students_df.merge(g_df, left_on="id", right_on="student_id", how="left")
        records = [
            (
                r["id"],
                r["name"],
                r["correct_answers"] if pd.notna(r["correct_answers"]) else ("Submitted" if r.get("submitted") else None),
                r["percentage"] if pd.notna(r["percentage"]) else None,
            )
            for _, r in merged.iterrows()
        ]
        submitted_cnt = int(
            (merged["correct_answers"].notna() | merged["submitted"].fillna(False)).sum()
        )
        pdf_buf = PDFGenerator.generate_master_report(
            f"{hw_title} (Group {grp})", hw_total, records
        )
        caption = (
            f"📚 *Homework Report — Group {grp}*\n"
            f"📌 *Assignment:* {hw_title}\n"
            f"✅ *Submitted/Graded:* {submitted_cnt} / {len(students_df)}"
        )
        ok_hw_txt, _ = client.send_text(teacher_phone, caption)
        ok_hw_pdf, _ = client.send_pdf(
            teacher_phone,
            pdf_buf.getvalue(),
            f"Group_{grp}_HW_{hw_id}_Report.pdf",
            caption=caption,
        )
        any_sent = any_sent or ok_hw_txt or ok_hw_pdf

    if qz_id and qz_id in qz_map:
        qz_title, qz_max = qz_map[qz_id]
        qg_df = db.fetch_dataframe(
            "SELECT student_id, score, percentage FROM quiz_grades WHERE quiz_id = %s",
            (qz_id,),
        )
        merged_q = students_df.merge(qg_df, left_on="id", right_on="student_id", how="left")
        records_q = [
            (
                r["id"],
                r["name"],
                r["score"] if pd.notna(r["score"]) else None,
                r["percentage"] if pd.notna(r["percentage"]) else None,
            )
            for _, r in merged_q.iterrows()
        ]
        attended_cnt = int(merged_q["score"].notna().sum())
        pdf_buf_q = PDFGenerator.generate_master_report(
            f"{qz_title} (Group {grp})", qz_max, records_q
        )
        caption_q = (
            f"📝 *Quiz Report — Group {grp}*\n"
            f"📌 *Quiz:* {qz_title}\n"
            f"✅ *Graded:* {attended_cnt} / {len(students_df)}"
        )
        ok_qz_txt, _ = client.send_text(teacher_phone, caption_q)
        ok_qz_pdf, _ = client.send_pdf(
            teacher_phone,
            pdf_buf_q.getvalue(),
            f"Group_{grp}_Quiz_{qz_id}_Report.pdf",
            caption=caption_q,
        )
        any_sent = any_sent or ok_qz_txt or ok_qz_pdf

    return any_sent


def send_6hr_unsubmitted_reminders(
    db, client: GowaClient, grp: str, mtime: str,
    hw_id: int | None, qz_id: int | None, hw_map: dict, qz_map: dict
) -> bool:
    students_df = db.fetch_dataframe(
        "SELECT id, name, phone, phone_parent FROM students WHERE TRIM(group_number) = %s",
        (grp,),
    )
    if students_df.empty or not hw_id or hw_id not in hw_map:
        return False

    hw_title, _ = hw_map[hw_id]
    qz_title = qz_map[qz_id][0] if (qz_id and qz_id in qz_map) else None

    g_df = db.fetch_dataframe(
        "SELECT student_id, correct_answers, COALESCE(submitted, FALSE) AS submitted "
        "FROM homework_grades WHERE homework_id = %s",
        (hw_id,),
    )
    submitted_ids = set()
    for _, r in g_df.iterrows():
        if pd.notna(r["correct_answers"]) or bool(r["submitted"]):
            submitted_ids.add(str(r["student_id"]))

    unsubmitted_rows = [
        stu for _, stu in students_df.iterrows()
        if str(stu["id"]) not in submitted_ids
    ]
    if not unsubmitted_rows:
        return True

    sent_any = False
    for stu in unsubmitted_rows:
        stu_name = str(stu["name"])
        stu_phone = clean_wa_phone(stu.get("phone"))
        quiz_line = f"\n📌 تذكير أيضًا بمراجعة اختبار: *{qz_title}*" if qz_title else ""

        reminder_msg = emoji.emojize(
            f":hourglass_not_done: *تذكير بموعد الحصة والواجب (باقي 6 ساعات)*\n\n"
            f"أهلاً بك يا *{stu_name}*،\n"
            f"نذكرك بموعد حصتك اليوم الساعة *{mtime}* (مجموعة {grp})، ولم يتم حتى الآن تسجيل تسليمك في واجب:\n"
            f"📘 *{hw_title}*{quiz_line}\n\n"
            f"برجاء سرعة رفع الواجب على المنصة قبل موعد الحصة.\n\n"
            f"Mathematics Team – Eng. Mahmoud Adel"
        )
        if stu_phone:
            ok, _ = client.send_text(stu_phone, reminder_msg)
            if ok:
                sent_any = True
            time.sleep(4)

    return sent_any


def start_background_automation_daemon(db):
    """Starts a single background daemon thread across the Streamlit server process."""
    init_automation_tables(db)
    _sync_secrets_to_cache_and_db(db)
    rt = get_daemon_runtime()

    with rt["lock"]:
        if rt.get("daemon_started"):
            return
        rt["daemon_started"] = True

    def _daemon_loop():
        while True:
            try:
                run_scheduled_checks_once(db)
            except Exception as e:
                with rt["lock"]:
                    rt["last_error"] = f"Daemon loop error: {e}"
            time.sleep(60)

    threading.Thread(target=_daemon_loop, daemon=True, name="WhatsAppAutomationDaemon").start()


# ---------------------------------------------------------------------------
# UI Renderer for the "🤖 Automated Messages" Tab
# ---------------------------------------------------------------------------
class AutomatedMessagesView:
    def __init__(self, db):
        self.db = db
        init_automation_tables(self.db)
        _sync_secrets_to_cache_and_db(self.db)

    def render(self):
        st.subheader("🤖 Automated WhatsApp Bot & Group Schedules")
        st.caption(
            "Configure group lecture timings, assign each group's latest Homework & Quiz, "
            "and manage automated teacher/assistant/student/parent WhatsApp notifications."
        )

        rt = get_daemon_runtime()
        now_dt = datetime.datetime.now(CAIRO_TZ)
        now_cairo = now_dt.strftime("%Y-%m-%d %H:%M:%S")

        db_last_tick = get_bot_setting(self.db, "last_scheduler_tick", "")
        db_last_action = get_bot_setting(self.db, "last_scheduler_action", "")
        db_last_error = get_bot_setting(self.db, "last_scheduler_error", "")

        with rt["lock"]:
            d_tick = db_last_tick or rt.get("last_tick") or "No tick recorded yet"
            d_action = db_last_action or rt.get("last_action", "Initialized — waiting for first check...")
            d_grade = rt.get("last_grade_status", "No grade saved in this session yet.")
            d_hw_sub = rt.get("last_hw_sub_status", "No homework submission alert sent in this session yet.")
            d_err = db_last_error or rt.get("last_error")

        scheduler_active = False
        if db_last_tick:
            try:
                tick_dt = datetime.datetime.strptime(db_last_tick, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CAIRO_TZ)
                if (now_dt - tick_dt).total_seconds() < 15 * 60:
                    scheduler_active = True
            except Exception:
                scheduler_active = False

        status_col1, status_col2, status_col3 = st.columns(3)
        status_label = (
            "🟢 **Scheduler: Active**"
            if scheduler_active
            else "🔴 **Scheduler: Idle/Inactive**"
        )
        status_col1.caption(f"{status_label} | 🕒 `{now_cairo}`")
        status_col2.caption(f"🔄 **Last Check:** `{d_tick}`")
        status_col3.caption(f"📌 **Last Event:** `{d_action}`")
        st.caption(
            f"📨 **Last Post-Grade Auto-Send:** `{d_grade}` | "
            f"📥 **Last Assistant HW Sub Alert:** `{d_hw_sub}`"
        )
        if d_err:
            st.warning(f"⚠️ Last Background Warning/Error: `{d_err}`")

        tab_sched, tab_broadcast, tab_settings = st.tabs([
            "📅 Group Schedules & Auto-Bot",
            "📢 Manual Broadcast (Private or Group)",
            "⚙️ Bot & Teacher Settings",
        ])

        students_df = self.db.fetch_dataframe(
            "SELECT id, name, phone, phone_parent, group_number FROM students ORDER BY name ASC"
        )
        groups = sorted({
            str(g).strip()
            for g in students_df["group_number"].dropna().unique()
            if str(g).strip()
        }) if not students_df.empty else []

        hw_df = self.db.fetch_dataframe("SELECT homework_id, title, total_questions FROM homeworks ORDER BY homework_id DESC")
        qz_df = self.db.fetch_dataframe("SELECT quiz_id, title, max_score FROM quizzes ORDER BY quiz_id DESC")

        # Determine the global latest Homework and Quiz IDs (for automatic fallback)
        default_latest_hw_id = int(hw_df.iloc[0]["homework_id"]) if not hw_df.empty else None
        default_latest_qz_id = int(qz_df.iloc[0]["quiz_id"]) if not qz_df.empty else None
        default_hw_title = str(hw_df.iloc[0]["title"]) if not hw_df.empty else "None"
        default_qz_title = str(qz_df.iloc[0]["title"]) if not qz_df.empty else "None"

        hw_options = {0: f"🔄 Auto (Latest: {default_hw_title})"}
        for _, r in hw_df.iterrows():
            hw_options[int(r["homework_id"])] = str(r["title"])

        qz_options = {0: f"🔄 Auto (Latest: {default_qz_title})"}
        for _, r in qz_df.iterrows():
            qz_options[int(r["quiz_id"])] = str(r["title"])

        # ===================================================================
        # TAB 1: GROUP SCHEDULE TABLE
        # ===================================================================
        with tab_sched:
            if not groups:
                st.warning("No student groups exist yet. Add students with group numbers first.")
            else:
                configs_df = self.db.fetch_dataframe("SELECT * FROM group_automation_config")
                configs_map = {
                    str(r["group_number"]).strip(): r
                    for _, r in configs_df.iterrows()
                }
                meetings_df = self.db.fetch_dataframe(
                    "SELECT meeting_id, group_number, day_of_week, meeting_time "
                    "FROM group_meeting_times ORDER BY meeting_time ASC"
                )

                st.markdown("#### 📋 Master Group Schedule & Pace Overview")
                overview_rows = []
                for g in groups:
                    g_meetings = meetings_df[meetings_df["group_number"].astype(str).str.strip() == g]
                    if g_meetings.empty:
                        timings_str = "No timings added yet"
                    else:
                        timings_str = " | ".join(
                            f"{r['day_of_week']} {r['meeting_time']}"
                            for _, r in g_meetings.iterrows()
                        )
                    cfg_row = configs_map.get(g, {})
                    eff_hw, eff_qz, is_hw_man, is_qz_man = get_effective_group_pace(
                        cfg_row, default_latest_hw_id, default_latest_qz_id
                    )
                    hw_disp = hw_options.get(eff_hw, "— None —") if eff_hw else "— None —"
                    qz_disp = qz_options.get(eff_qz, "— None —") if eff_qz else "— None —"
                    if eff_hw and not is_hw_man:
                        hw_disp = f"🔄 {hw_disp} (Auto)"
                    if eff_qz and not is_qz_man:
                        qz_disp = f"🔄 {qz_disp} (Auto)"

                    overview_rows.append({
                        "Group": f"Group {g}",
                        "Scheduled Timings": timings_str,
                        "Active Homework": hw_disp,
                        "Active Quiz": qz_disp,
                    })
                st.dataframe(pd.DataFrame(overview_rows), hide_index=True, use_container_width=True)

                st.divider()

                col_left, col_right = st.columns([1.2, 1.3])

                with col_left:
                    with st.container(border=True):
                        st.markdown("#### 🕒 Add / Remove Group Lecture Timings")
                        st.caption("You can add multiple meeting days and times to the same group.")
                        sel_grp_time = str(
                            st.selectbox("Select Group", groups, key="sched_grp_sel")
                        ).strip()

                        c_d, c_t = st.columns(2)
                        with c_d:
                            sel_days = st.multiselect(
                                "Day(s) of Week",
                                DAYS_OF_WEEK,
                                default=["Sat"],
                                format_func=lambda d: f"{d} ({DAY_FULL_NAMES[d]})",
                                key=f"sched_days_{sel_grp_time}",
                            )
                        with c_t:
                            sel_time = st.time_input(
                                "Lecture Start Time",
                                value=datetime.time(16, 0),
                                step=datetime.timedelta(minutes=15),
                                key=f"sched_time_{sel_grp_time}",
                            )

                        if st.button(
                            f"➕ Add Timing(s) to Group {sel_grp_time}",
                            type="primary",
                            use_container_width=True,
                            key=f"btn_add_time_{sel_grp_time}",
                        ):
                            if not sel_days:
                                st.error("Please select at least one day.")
                            else:
                                t_str = sel_time.strftime("%H:%M")
                                for d in sel_days:
                                    self.db.execute_query(
                                        """
                                        INSERT INTO group_meeting_times (group_number, day_of_week, meeting_time)
                                        VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
                                        """,
                                        (sel_grp_time, d, t_str),
                                    )
                                st.success(f"Added {', '.join(sel_days)} at {t_str} to Group {sel_grp_time}!")
                                st.rerun()

                        grp_m = meetings_df[
                            meetings_df["group_number"].astype(str).str.strip() == sel_grp_time
                        ]
                        st.markdown(f"**Current Timings for Group {sel_grp_time}:**")
                        if grp_m.empty:
                            st.caption(f"No lecture timings added for Group {sel_grp_time} yet.")
                        else:
                            for _, mr in grp_m.iterrows():
                                mc1, mc2 = st.columns([3, 1])
                                mc1.write(
                                    f"• **{DAY_FULL_NAMES.get(mr['day_of_week'], mr['day_of_week'])}** at `{mr['meeting_time']}`"
                                )
                                if mc2.button("🗑️", key=f"del_m_{mr['meeting_id']}"):
                                    self.db.execute_query(
                                        "DELETE FROM group_meeting_times WHERE meeting_id = %s",
                                        (int(mr["meeting_id"]),),
                                    )
                                    st.rerun()

                with col_right:
                    with st.container(border=True):
                        st.markdown("#### 📌 Set Latest Homework & Latest Quiz per Group")
                        st.caption(
                            "Defaults to **🔄 Auto (Latest Created)** automatically unless you manually select a specific Homework or Quiz for a group."
                        )
                        target_grp = str(
                            st.selectbox("Select Group to Configure", groups, key="cfg_grp_sel")
                        ).strip()
                        cur_cfg = configs_map.get(target_grp, {})
                        init_hw = (
                            int(cur_cfg["latest_hw_id"])
                            if pd.notna(cur_cfg.get("latest_hw_id"))
                            and int(cur_cfg["latest_hw_id"]) in hw_options
                            else 0
                        )
                        init_qz = (
                            int(cur_cfg["latest_quiz_id"])
                            if pd.notna(cur_cfg.get("latest_quiz_id"))
                            and int(cur_cfg["latest_quiz_id"]) in qz_options
                            else 0
                        )
                        init_jid = (
                            str(cur_cfg.get("wa_group_jid") or "")
                            if pd.notna(cur_cfg.get("wa_group_jid"))
                            else ""
                        )

                        with st.form(f"update_group_pace_form_{target_grp}"):
                            new_hw_id = st.selectbox(
                                f"3rd Column: Homework for Group {target_grp}",
                                options=list(hw_options.keys()),
                                index=list(hw_options.keys()).index(init_hw),
                                format_func=lambda k: hw_options[k],
                                key=f"cfg_hw_{target_grp}",
                            )
                            new_qz_id = st.selectbox(
                                f"4th Column: Quiz for Group {target_grp}",
                                options=list(qz_options.keys()),
                                index=list(qz_options.keys()).index(init_qz),
                                format_func=lambda k: qz_options[k],
                                key=f"cfg_qz_{target_grp}",
                            )
                            new_jid = st.text_input(
                                "Optional: WhatsApp Group JID (for Group Chat Broadcasts)",
                                value=init_jid,
                                placeholder="e.g. 12036304...@g.us",
                                key=f"cfg_jid_{target_grp}",
                            ).strip()

                            if st.form_submit_button(
                                f"💾 Save Group {target_grp} Assignment Pace",
                                type="primary",
                                use_container_width=True,
                            ):
                                self.db.execute_query(
                                    """
                                    INSERT INTO group_automation_config (group_number, latest_hw_id, latest_quiz_id, wa_group_jid)
                                    VALUES (%s, %s, %s, %s)
                                    ON CONFLICT (group_number) DO UPDATE SET
                                        latest_hw_id = EXCLUDED.latest_hw_id,
                                        latest_quiz_id = EXCLUDED.latest_quiz_id,
                                        wa_group_jid = EXCLUDED.wa_group_jid
                                    """,
                                    (
                                        target_grp,
                                        int(new_hw_id) if new_hw_id else None,
                                        int(new_qz_id) if new_qz_id else None,
                                        new_jid or None,
                                    ),
                                )
                                st.success(f"Saved assignment pace for Group {target_grp}!")
                                st.rerun()

                st.divider()
                st.markdown("#### ⚡ Instant Test / Manual Trigger Buttons")
                col_t1, col_t2, col_t3 = st.columns(3)
                with col_t1:
                    if st.button("☀️ Send Morning Schedule to Teacher Now", use_container_width=True):
                        t_phone = get_resolved_teacher_phone(self.db)
                        if not t_phone:
                            st.error("Please save the Teacher's WhatsApp Phone in the '⚙️ Bot & Teacher Settings' tab first!")
                        else:
                            with st.spinner("Sending Morning Schedule to Teacher..."):
                                now_c = datetime.datetime.now(CAIRO_TZ)
                                ok, err = send_morning_schedule_to_teacher(
                                    self.db, GowaClient.from_secrets(self.db), t_phone, now_c
                                )
                                if ok:
                                    st.success("Morning schedule + exciting quote sent to Teacher!")
                                else:
                                    st.error(f"Failed to send Morning Schedule: {err}")
                with col_t2:
                    test_grp = st.selectbox("Group for Instant Action", groups, key="instant_grp_sel", label_visibility="collapsed")
                    if st.button(f"📊 Send Group {test_grp} Reports to Teacher Now", use_container_width=True):
                        t_phone = get_resolved_teacher_phone(self.db)
                        if not t_phone:
                            st.error("Please set Teacher Phone in Settings first!")
                        else:
                            with st.spinner(f"Generating & sending Group {test_grp} Master PDFs..."):
                                cfg_r = configs_map.get(test_grp, {})
                                hid, qid, _, _ = get_effective_group_pace(
                                    cfg_r, default_latest_hw_id, default_latest_qz_id
                                )
                                hw_m = {int(r["homework_id"]): (str(r["title"]), int(r["total_questions"])) for _, r in hw_df.iterrows()}
                                qz_m = {int(r["quiz_id"]): (str(r["title"]), float(r["max_score"])) for _, r in qz_df.iterrows()}
                                ok = send_group_master_reports_to_teacher(
                                    self.db, GowaClient.from_secrets(self.db), t_phone, test_grp, "Now", hid, qid, hw_m, qz_m
                                )
                                if ok:
                                    st.success(f"Sent Group {test_grp} HW & Quiz Master PDFs to Teacher!")
                                else:
                                    st.error("Failed to send reports. Check GOWA connection in Settings.")
                with col_t3:
                    if st.button(f"🔔 Send Unsubmitted HW Reminders to Group {test_grp} Now", use_container_width=True):
                        cfg_r = configs_map.get(test_grp, {})
                        hid, qid, _, _ = get_effective_group_pace(
                            cfg_r, default_latest_hw_id, default_latest_qz_id
                        )
                        if not hid:
                            st.error(f"No homeworks exist yet for Group {test_grp}!")
                        else:
                            hw_m = {int(r["homework_id"]): (str(r["title"]), int(r["total_questions"])) for _, r in hw_df.iterrows()}
                            qz_m = {int(r["quiz_id"]): (str(r["title"]), float(r["max_score"])) for _, r in qz_df.iterrows()}
                            client_inst = GowaClient.from_secrets(self.db)
                            threading.Thread(
                                target=send_6hr_unsubmitted_reminders,
                                args=(self.db, client_inst, test_grp, "Upcoming", hid, qid, hw_m, qz_m),
                                daemon=True,
                            ).start()
                            st.success(f"Started sending reminders in the background to unsubmitted students in Group {test_grp}!")

        # ===================================================================
        # TAB 2: MANUAL BROADCAST TAB
        # ===================================================================
        with tab_broadcast:
            st.markdown("#### 📢 Custom Automated Broadcast")
            st.caption(
                "Write any announcement or message and send it automatically via the bot to all students, "
                "a specific group privately, or directly into their WhatsApp Group chat."
            )

            col_b1, col_b2 = st.columns(2)
            with col_b1:
                target_audience = st.selectbox(
                    "1️⃣ Select Target Audience",
                    ["All Students", "Specific Group"],
                    key="bc_audience",
                )
                selected_bc_group = None
                if target_audience == "Specific Group" and groups:
                    selected_bc_group = st.selectbox("Select Group", groups, key="bc_grp_sel")

            with col_b2:
                delivery_mode = st.radio(
                    "2️⃣ Delivery Mode",
                    [
                        "💬 Privately to Students",
                        "👨‍👩‍👦 Privately to Parents",
                        "👨‍👩‍👦🎓 Privately to Both Students & Parents",
                        "👥 On Their WhatsApp Group Chat",
                    ],
                    key="bc_mode",
                )

            custom_msg = st.text_area(
                "3️⃣ Write Your Message (Use `{name}` to automatically insert the student's name in private messages)",
                placeholder="أهلاً بك يا {name}، تنبيه هام بخصوص حصة الغد...",
                height=140,
            )

            if st.button("🚀 Send Automated Broadcast Now", type="primary", use_container_width=True):
                if not custom_msg.strip():
                    st.error("Please write a message first.")
                else:
                    client = GowaClient.from_secrets(self.db)
                    if delivery_mode == "👥 On Their WhatsApp Group Chat":
                        cfg_df = self.db.fetch_dataframe("SELECT group_number, wa_group_jid FROM group_automation_config")
                        if target_audience == "Specific Group" and selected_bc_group:
                            cfg_df = cfg_df[cfg_df["group_number"].astype(str).str.strip() == selected_bc_group]
                        jids = [
                            str(r["wa_group_jid"]).strip()
                            for _, r in cfg_df.iterrows()
                            if pd.notna(r.get("wa_group_jid")) and str(r.get("wa_group_jid")).strip()
                        ]
                        if not jids:
                            st.error("No WhatsApp Group JIDs (`...@g.us`) are configured for the selected group(s). Add them in the Schedule tab!")
                        else:
                            sent_c = 0
                            for jid in jids:
                                ok, err = client.send_text(jid, custom_msg.strip())
                                if ok:
                                    sent_c += 1
                                else:
                                    st.warning(f"Failed for {jid}: {err}")
                            st.success(f"✅ Broadcast sent to {sent_c} WhatsApp Group chat(s)!")
                    else:
                        target_df = students_df.copy()
                        if target_audience == "Specific Group" and selected_bc_group:
                            target_df = target_df[target_df["group_number"].astype(str).str.strip() == selected_bc_group]

                        if target_df.empty:
                            st.warning("No students found in the selected audience.")
                        else:
                            prog = st.progress(0, text="Starting automated broadcast...")
                            sent_count = 0
                            total_rows = len(target_df)
                            for idx, (_, s_row) in enumerate(target_df.iterrows()):
                                s_name = str(s_row["name"])
                                personalized = custom_msg.strip().replace("{name}", s_name)
                                s_phone = clean_wa_phone(s_row.get("phone"))
                                p_phone = clean_wa_phone(s_row.get("phone_parent"))

                                if "Students" in delivery_mode and s_phone:
                                    ok, _ = client.send_text(s_phone, personalized)
                                    if ok:
                                        sent_count += 1
                                if "Parents" in delivery_mode and p_phone:
                                    ok, _ = client.send_text(p_phone, personalized)
                                    if ok:
                                        sent_count += 1

                                prog.progress((idx + 1) / total_rows, text=f"Sent to {s_name} ({idx + 1}/{total_rows})...")
                                if idx < total_rows - 1:
                                    time.sleep(4)
                            prog.empty()
                            st.success(f"🎉 Broadcast complete! Successfully sent {sent_count} WhatsApp message(s).")

        # ===================================================================
        # TAB 3: BOT & TEACHER SETTINGS
        # ===================================================================
        with tab_settings:
            st.markdown("#### ⚙️ Bot Connection & Teacher Settings")
            cur_url = get_bot_setting(self.db, "gowa_url", rt.get("cached_url", DEFAULT_GOWA_URL))
            cur_auth = get_bot_setting(self.db, "gowa_auth", rt.get("cached_auth", ""))
            cur_t_phone = get_bot_setting(self.db, "teacher_phone", rt.get("cached_teacher_phone", ""))
            cur_m_time = get_bot_setting(self.db, "morning_schedule_time", "08:00")
            cur_auto_grade = get_bot_setting(self.db, "auto_send_on_grade", "true").lower() == "true"
            cur_also_stu = get_bot_setting(self.db, "also_send_grade_to_student", "false").lower() == "true"

            try:
                parsed_morning_time = datetime.datetime.strptime(cur_m_time.strip(), "%H:%M").time()
            except Exception:
                parsed_morning_time = datetime.time(8, 0)

            col_st1, col_st2 = st.columns([1, 1])
            with col_st1:
                if st.button("🔌 Test GOWA Connection", use_container_width=True):
                    with st.spinner("Pinging GOWA server..."):
                        ok, status_msg = GowaClient.from_secrets(self.db).ensure_awake(max_wait_seconds=60)
                        if ok:
                            st.success(f"✅ {status_msg}")
                        else:
                            st.warning(f"⚠️ {status_msg}")
            with col_st2:
                if st.button("🔄 Force Run Scheduler Check Now", use_container_width=True):
                    with st.spinner("Running scheduled checks..."):
                        run_scheduled_checks_once(self.db)
                        st.success("Scheduler check executed!")
                        st.rerun()

            with st.form("bot_settings_form"):
                in_url = st.text_input("GOWA Render Deployment URL", value=cur_url)
                in_auth = st.text_input(
                    "GOWA Basic Auth (`username:password` if set in Render, else leave blank)",
                    value=cur_auth,
                    type="password",
                )
                in_t_phone = st.text_input(
                    "👨‍🏫 Teacher's WhatsApp Phone Number (Receives Morning Schedule & 10-Min Pre-Group Reports)",
                    value=cur_t_phone,
                    placeholder="e.g. 010...",
                )
                in_m_time = st.time_input(
                    "☀️ Daily Morning Schedule Send Time (Cairo time)",
                    value=parsed_morning_time,
                )
                in_auto_grade = st.checkbox(
                    "✅ Automatically send Grade + PDF Report to Parent on WhatsApp right after saving a grade",
                    value=cur_auto_grade,
                )
                in_also_stu = st.checkbox(
                    "🎓 Also send Grade + PDF Report to Student's phone right after saving a grade",
                    value=cur_also_stu,
                )

                if st.form_submit_button("💾 Save Teacher & Bot Settings", type="primary", use_container_width=True):
                    m_time_formatted = (
                        in_m_time.strftime("%H:%M")
                        if isinstance(in_m_time, datetime.time)
                        else "08:00"
                    )
                    set_bot_setting(self.db, "gowa_url", in_url.strip() or DEFAULT_GOWA_URL)
                    set_bot_setting(self.db, "gowa_auth", in_auth.strip())
                    set_bot_setting(self.db, "teacher_phone", in_t_phone.strip())
                    set_bot_setting(self.db, "morning_schedule_time", m_time_formatted)
                    set_bot_setting(self.db, "auto_send_on_grade", "true" if in_auto_grade else "false")
                    set_bot_setting(self.db, "also_send_grade_to_student", "true" if in_also_stu else "false")
                    _sync_secrets_to_cache_and_db(self.db)
                    st.success("✅ Teacher & Bot settings saved!")
                    st.rerun()

            st.divider()
            st.markdown("#### 👩‍🏫 Manage Assistants & Notification Permissions")

            asst_df = self.db.fetch_dataframe(
                "SELECT id, name, phone, notify_hw_submit, notify_daily_schedule, notify_grade_submitted FROM assistants ORDER BY id ASC"
            )

            if not asst_df.empty:
                st.dataframe(
                    asst_df,
                    hide_index=True,
                    use_container_width=True,
                    column_config={
                        "id": None,
                        "name": "Assistant Name",
                        "phone": "WhatsApp Phone",
                        "notify_hw_submit": "HW Submits",
                        "notify_daily_schedule": "Daily Schedule",
                        "notify_grade_submitted": "Grade Saved"
                    }
                )

                del_asst_dict = {str(row["id"]): f"{row['name']} ({row['phone']})" for _, row in asst_df.iterrows()}
                c_del1, c_del2 = st.columns([3, 1])
                del_sel = c_del1.selectbox("Select Assistant to Remove", list(del_asst_dict.keys()), format_func=lambda x: del_asst_dict[x])
                if c_del2.button("🗑️ Remove Assistant", use_container_width=True):
                    self.db.execute_query("DELETE FROM assistants WHERE id = %s", (int(del_sel),))
                    st.rerun()
            else:
                st.info("No assistants registered yet.")

            with st.expander("➕ Add New Assistant", expanded=True):
                with st.form("add_assistant_form"):
                    a_name = st.text_input("Assistant Name")
                    a_phone = st.text_input("WhatsApp Phone Number", placeholder="010...")

                    st.markdown("**Notification Permissions:**")
                    a_hw = st.checkbox("📥 Receive New Homework Submission Alerts", value=True)
                    a_sched = st.checkbox("☀️ Receive Morning Schedule", value=True)
                    a_grade = st.checkbox("✅ Receive Grade Saved Alerts", value=False)

                    if st.form_submit_button("💾 Add Assistant", type="primary"):
                        if not a_name.strip() or not a_phone.strip():
                            st.error("Name and Phone are required.")
                        else:
                            try:
                                self.db.execute_query(
                                    """
                                    INSERT INTO assistants (name, phone, notify_hw_submit, notify_daily_schedule, notify_grade_submitted) 
                                    VALUES (%s, %s, %s, %s, %s)
                                    """,
                                    (a_name.strip(), a_phone.strip(), a_hw, a_sched, a_grade)
                                )
                                st.success(f"Assistant {a_name} added successfully!")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Failed to add assistant (Phone might already exist): {e}")
