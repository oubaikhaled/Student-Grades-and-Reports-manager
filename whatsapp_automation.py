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
    # Handle float-formatted strings from pandas like '1012345678.0'
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

    def send_text(self, recipient: str, message: str, retries: int = 2) -> tuple[bool, str]:
        """Sends a WhatsApp text message to a phone number or group JID (@g.us)."""
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
                    return True, "Sent"
                last_err = f"HTTP {resp.status_code}: {resp.text[:150]}"
            except Exception as e:
                last_err = str(e)
            if attempt < retries:
                time.sleep(3)
        return False, last_err

    def send_pdf(self, recipient: str, pdf_bytes: bytes, filename: str, caption: str = "", retries: int = 1) -> tuple[bool, str]:
        """Sends a PDF file via GOWA /send/file using an ASCII-safe filename."""
        target = clean_wa_phone(recipient)
        if not target:
            return False, f"Invalid phone/recipient: {recipient}"

        # Make filename strictly ASCII-safe for multipart/form-data compatibility with GOWA (Go fiber)
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
                    return True, "PDF Sent"
                last_err = f"HTTP {resp.status_code}: {resp.text[:150]}"
            except Exception as e:
                last_err = str(e)
            if attempt < retries:
                time.sleep(3)
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
# Safe PDF Generator Helper (Handles any signature of PDFGenerator.generate_student_report)
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
# Instant Homework Submission Alert to Assistant (Triggered when Student submits HW)
# ---------------------------------------------------------------------------
def trigger_hw_submission_assistant_whatsapp(
    db,
    student_id: str,
    homework_id: int | None = None,
    homework_title: str | None = None,
    student_name: str | None = None,
    group_number: str | None = None,
):
    """
    Runs immediately when a student submits a homework (and is also checked by the background scheduler).
    Sends a WhatsApp notification to the Assistant's phone number so she knows to grade it.
    """
    _sync_secrets_to_cache_and_db(db)
    rt = get_daemon_runtime()

    notify_enabled = get_bot_setting(db, "notify_assistant_on_hw_submit", "true").lower() == "true"
    if not notify_enabled:
        with rt["lock"]:
            rt["last_hw_sub_status"] = "Skipped (notify_assistant_on_hw_submit is disabled in Settings)"
        return

    assistant_phone = get_resolved_assistant_phone(db)
    if not assistant_phone:
        with rt["lock"]:
            rt["last_hw_sub_status"] = "Skipped (Assistant phone is not configured in Settings)"
        return

    # Resolve missing student details synchronously before Streamlit reruns
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

    # Resolve missing homework title synchronously
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

    # Pre-mark dispatch key if homework_id is known so the 5-min cron doesn't send a duplicate alert
    if homework_id is not None:
        sub_key = f"asst_hw_sub_hw_{int(homework_id)}_stu_{str(student_id).strip()}"
        mark_dispatched(db, sub_key)

    client = GowaClient.from_secrets(db)
    submitted_at_str = datetime.datetime.now(CAIRO_TZ).strftime("%Y-%m-%d %H:%M")

    def _worker():
        try:
            msg = emoji.emojize(
                f":inbox_tray: *تسليم واجب جديد للمراجعة والتصحيح*\n\n"
                f"👩‍🏫 *تنبيه للمساعدة:*\n"
                f"قام الطالب *{student_name}* (كود: `{student_id}` — مجموعة *{group_number}*) بتسليم الواجب:\n"
                f"📘 *{homework_title}*\n"
                f"🕒 *وقت التسليم:* {submitted_at_str}\n\n"
                f"برجاء الدخول على المنصة لمراجعة الواجب وتصحيحه. :check_mark_button:\n\n"
                f"Mathematics Team – Eng. Mahmoud Adel"
            )
            ok, err = client.send_text(assistant_phone, msg)
            with rt["lock"]:
                if ok:
                    rt["last_hw_sub_status"] = f"Sent to Assistant ({assistant_phone}) for {student_name} ({homework_title})"
                    rt["last_action"] = f"HW Sub Alert -> Assistant ({student_name})"
                else:
                    rt["last_hw_sub_status"] = f"Failed for {student_name}: {err}"
                    rt["last_error"] = f"Assistant HW Sub Alert Error: {err}"
        except Exception as e:
            with rt["lock"]:
                rt["last_error"] = f"Assistant HW Sub Exception ({student_name}): {e}"
                rt["last_hw_sub_status"] = f"Exception for {student_name}: {e}"

    threading.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Instant Post-Grading Parent & Student Sender (Triggered right after Save Grade)
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
    1. Fetches student & parent phone numbers on the main thread before Streamlit reruns.
    2. Spawns a worker that sends the WhatsApp text notification FIRST (using the proven send_text method),
       and then attaches & sends the PDF report card.
    """
    _sync_secrets_to_cache_and_db(db)
    rt = get_daemon_runtime()

    auto_enabled = get_bot_setting(db, "auto_send_on_grade", "true").lower() == "true"
    if not auto_enabled:
        with rt["lock"]:
            rt["last_grade_status"] = "Skipped (auto_send_on_grade is disabled in Settings)"
        return

    also_student = get_bot_setting(db, "also_send_grade_to_student", "false").lower() == "true"

    # Fetch student data and client credentials synchronously BEFORE st.rerun()
    try:
        stu_df = db.fetch_dataframe(
            "SELECT name, phone, phone_parent FROM students WHERE id = %s",
            (str(student_id),),
        )
        if stu_df.empty:
            with rt["lock"]:
                rt["last_grade_status"] = f"Student ID {student_id} not found in DB."
            return
        stu = stu_df.iloc[0]
        student_name = str(stu["name"])
        parent_phone = clean_wa_phone(stu.get("phone_parent"))
        student_phone = clean_wa_phone(stu.get("phone"))
    except Exception as e:
        with rt["lock"]:
            rt["last_grade_status"] = f"DB fetch error on grade save: {e}"
        return

    if not parent_phone and not (also_student and student_phone):
        with rt["lock"]:
            rt["last_grade_status"] = (
                f"No valid phone for {student_name} (Parent: {stu.get('phone_parent')}, "
                f"Student: {stu.get('phone')}, AlsoSendStudent={also_student})"
            )
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
            score_str = str(int(score)) if float(score).is_integer() else f"{score:.1f}"
            max_str = str(int(max_score)) if float(max_score).is_integer() else f"{max_score:.1f}"
            type_label = "Quiz" if kind == "quiz" else "Homework"

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

            # Build PDF report card safely
            pdf_bytes = _safe_generate_student_pdf(
                student_name, item_title, score, max_score, perc, report_text, img_bytes, video_link
            )
            pdf_filename = f"Student_{student_id}_{kind}_{item_id}_Report.pdf"

            status_parts = []

            # 1. Send to Parent
            if parent_phone:
                # Always send text message first (100% reliable, same endpoint as manual broadcast)
                ok_txt, err_txt = client.send_text(parent_phone, parent_msg)
                if pdf_bytes:
                    ok_pdf, err_pdf = client.send_pdf(
                        parent_phone, pdf_bytes, pdf_filename, caption=f"📄 {student_name} — {item_title}"
                    )
                    status_parts.append(f"Parent({parent_phone}): Text={'OK' if ok_txt else err_txt}, PDF={'OK' if ok_pdf else err_pdf}")
                else:
                    status_parts.append(f"Parent({parent_phone}): Text={'OK' if ok_txt else err_txt} (No PDF)")

            # 2. Send to Student (if enabled OR if parent_phone is missing and student_phone exists)
            if student_phone and (also_student or not parent_phone):
                ok_stxt, err_stxt = client.send_text(student_phone, stu_msg)
                if pdf_bytes:
                    ok_spdf, err_spdf = client.send_pdf(
                        student_phone, pdf_bytes, pdf_filename, caption=f"📄 {student_name} — {item_title}"
                    )
                    status_parts.append(f"Student({student_phone}): Text={'OK' if ok_stxt else err_stxt}, PDF={'OK' if ok_spdf else err_spdf}")
                else:
                    status_parts.append(f"Student({student_phone}): Text={'OK' if ok_stxt else err_stxt}")

            summary = " | ".join(status_parts)
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
               c.latest_hw_id, c.latest_quiz_id
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
        asst_df = db.fetch_dataframe("SELECT name, phone FROM assistants WHERE notify_daily_schedule = TRUE")
        for _, row in asst_df.iterrows():
            asst_phone = clean_wa_phone(row["phone"])
            if asst_phone:
                # Personalize the message greeting for the assistant
                asst_msg = msg.replace("Eng. Mahmoud Adel", str(row["name"]).strip())
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
    """
    Checks `homework_grades` for any rows where `submitted = TRUE` and `correct_answers IS NULL`
    (meaning the student submitted the homework and it is waiting for the assistant to correct it).
    Uses `claim_dispatch` per (homework_id, student_id) so each submission notifies the assistant once.
    """
    notify_enabled = get_bot_setting(db, "notify_assistant_on_hw_submit", "true").lower() == "true"
    if not notify_enabled:
        print("[Scheduler] Assistant HW submission check skipped: disabled in settings.")
        return

    if not assistant_phone_clean:
        print("[Scheduler] Assistant HW submission check skipped: assistant phone not configured.")
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
        print("[Scheduler] Assistant HW check: no new ungraded homework submissions.")
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
            msg = emoji.emojize(
                f":inbox_tray: *تسليم واجب جديد للمراجعة والتصحيح*\n\n"
                f"👩‍🏫 *تنبيه للمساعدة:*\n"
                f"قام الطالب *{stu_name}* (كود: `{stu_id}` — مجموعة *{grp}*) بتسليم الواجب:\n"
                f"📘 *{hw_title}*\n"
                f"🕒 *وقت الإشعار:* {now_fmt}\n\n"
                f"برجاء الدخول على المنصة لمراجعة الواجب وتصحيحه. :check_mark_button:\n\n"
                f"Mathematics Team – Eng. Mahmoud Adel"
            )
            ok, err = client.send_text(assistant_phone_clean, msg)
            if ok:
                action_msg = f"Notified Assistant: HW submitted by {stu_name} ({hw_title})"
                _record_scheduler_action(db, rt, action_msg)
                with rt["lock"]:
                    rt["last_hw_sub_status"] = f"Sent to Assistant ({assistant_phone_clean}) for {stu_name}"
                print(f"[Scheduler] {action_msg}.")
            else:
                release_dispatch(db, sub_key)
                err_msg = f"Assistant HW Sub Alert Failed ({stu_name}): {err}"
                _record_scheduler_error(db, rt, err_msg)
                print(f"[Scheduler] {err_msg} (released '{sub_key}' for retry).")
            time.sleep(3)


# ---------------------------------------------------------------------------
# Background Scheduler Tick (Checks Morning Schedule, Assistant Alerts, 10-Min Teacher Reports, 6-Hr Reminders)
# ---------------------------------------------------------------------------
def _parse_hhmm_tolerant(time_str: str, default_hr: int = 8, default_min: int = 0) -> tuple[int, int]:
    """Tolerantly parses '3:21', '03:21', '15:21', etc. Falls back to default only if unparseable."""
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
    """
    Evaluates current Cairo time against all scheduled group timings & pending submissions:
    1. Morning Teacher Schedule + Random Exciting Sentence.
    2. Pending Uncorrected Homework Submissions -> Notify Assistant to grade them.
    3. 10-Minute Pre-Group Homework & Quiz Master PDF Reports to Teacher.
    4. 6-Hour Pre-Group Homework & Quiz Reminders to Students who haven't submitted.
    """
    rt = get_daemon_runtime()
    now = datetime.datetime.now(CAIRO_TZ)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    today_str = now.strftime("%Y-%m-%d")
    today_day = now.strftime("%a")  # 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'

    with rt["lock"]:
        rt["last_tick"] = now_str
        rt["last_tick_ts"] = time.time()

    try:
        set_bot_setting(db, "last_scheduler_tick", now_str)
    except Exception as e:
        print(f"[Scheduler] Warning: could not persist last_scheduler_tick: {e}")

    print(f"[Scheduler] Tick started at {now_str} Cairo time ({today_day}).")

    teacher_phone_clean = get_resolved_teacher_phone(db)
    assistant_phone_clean = get_resolved_assistant_phone(db)
    client = GowaClient.from_secrets(db)

    # -----------------------------------------------------------------------
    # WORKFLOW 0: Notify Assistant of Any New Homework Submissions to Correct
    # -----------------------------------------------------------------------
    check_and_notify_assistant_pending_submissions(db, client, assistant_phone_clean, now, rt)

    # -----------------------------------------------------------------------
    # WORKFLOW 1: Daily Morning Schedule to Teacher
    # -----------------------------------------------------------------------
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
                print(f"[Scheduler] {action_msg} to {teacher_phone_clean}.")
            else:
                release_dispatch(db, morning_key)
                err_msg = f"Morning Schedule Failed: {err}"
                _record_scheduler_error(db, rt, err_msg)
                print(f"[Scheduler] {err_msg} (released '{morning_key}' for retry).")
        else:
            print(f"[Scheduler] Morning schedule skipped: already dispatched ({morning_key}).")
    else:
        print(f"[Scheduler] Morning schedule skipped: outside time window (target {m_hr:02d}:{m_min:02d}).")

    # Fetch today's meetings
    meetings_df = db.fetch_dataframe(
        """
        SELECT m.meeting_id, TRIM(m.group_number) AS group_number, m.day_of_week, m.meeting_time,
               c.latest_hw_id, c.latest_quiz_id, c.wa_group_jid
        FROM group_meeting_times m
        LEFT JOIN group_automation_config c ON TRIM(m.group_number) = TRIM(c.group_number)
        WHERE m.day_of_week = %s
        ORDER BY m.meeting_time ASC
        """,
        (today_day,),
    )
    if meetings_df.empty:
        print(f"[Scheduler] No group sessions scheduled for {today_day}.")
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
            print(f"[Scheduler] Group {grp}: invalid meeting_time '{mtime}', skipping.")
            continue

        minutes_until_lecture = (lecture_dt - now).total_seconds() / 60.0
        hw_id = int(m_row["latest_hw_id"]) if pd.notna(m_row.get("latest_hw_id")) else None
        qz_id = int(m_row["latest_quiz_id"]) if pd.notna(m_row.get("latest_quiz_id")) else None

        # -------------------------------------------------------------------
        # WORKFLOW 2: 10 Minutes Before Group -> Send HW & Quiz Reports to Teacher
        # -------------------------------------------------------------------
        pre10_key = f"teacher_10min_{today_str}_grp_{grp}_{mtime}"
        if teacher_phone_clean and -5 <= minutes_until_lecture <= 15:
            if claim_dispatch(db, pre10_key):
                ok = send_group_master_reports_to_teacher(
                    db, client, teacher_phone_clean, grp, mtime, hw_id, qz_id, hw_map, qz_map
                )
                if ok:
                    action_msg = f"Sent 10-min Report for Group {grp}"
                    _record_scheduler_action(db, rt, action_msg)
                    print(f"[Scheduler] {action_msg} ({mtime}).")
                else:
                    release_dispatch(db, pre10_key)
                    err_msg = f"10-min Report failed for Group {grp}"
                    _record_scheduler_error(db, rt, err_msg)
                    print(f"[Scheduler] {err_msg} (released '{pre10_key}' for retry).")
            else:
                print(f"[Scheduler] Group {grp} ({mtime}) 10-min report skipped: already dispatched.")
        else:
            print(f"[Scheduler] Group {grp} ({mtime}) 10-min report skipped: {minutes_until_lecture:.1f} min until lecture.")

        # -------------------------------------------------------------------
        # WORKFLOW 3: 6 Hours (360 mins) Before Group -> Remind Students Who Didn't Submit HW
        # -------------------------------------------------------------------
        rem6h_key = f"student_6hr_{today_str}_grp_{grp}_{mtime}"
        if (hw_id or qz_id) and 300 <= minutes_until_lecture <= 380:
            if claim_dispatch(db, rem6h_key):
                ok = send_6hr_unsubmitted_reminders(
                    db, client, grp, mtime, hw_id, qz_id, hw_map, qz_map
                )
                if ok:
                    action_msg = f"Sent 6-hr Reminders for Group {grp}"
                    _record_scheduler_action(db, rt, action_msg)
                    print(f"[Scheduler] {action_msg} ({mtime}).")
                else:
                    release_dispatch(db, rem6h_key)
                    err_msg = f"6-hr Reminders failed for Group {grp}"
                    _record_scheduler_error(db, rt, err_msg)
                    print(f"[Scheduler] {err_msg} (released '{rem6h_key}' for retry).")
            else:
                print(f"[Scheduler] Group {grp} ({mtime}) 6-hr reminders skipped: already dispatched.")
        else:
            print(f"[Scheduler] Group {grp} ({mtime}) 6-hr reminders skipped: {minutes_until_lecture:.1f} min until lecture (hw_id={hw_id}, qz_id={qz_id}).")


def send_group_master_reports_to_teacher(
    db, client: GowaClient, teacher_phone: str, grp: str, mtime: str,
    hw_id: int | None, qz_id: int | None, hw_map: dict, qz_map: dict
) -> bool:
    """Generates and sends the Master Homework PDF and Master Quiz PDF for a group to the teacher."""
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
    """Finds students in Group `grp` who haven't submitted `hw_id` and sends a 6-hr reminder."""
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

        # Live External Scheduler & Post-Grade Status Bar
        rt = get_daemon_runtime()
        now_dt = datetime.datetime.now(CAIRO_TZ)
        now_cairo = now_dt.strftime("%Y-%m-%d %H:%M:%S")

        db_last_tick = get_bot_setting(self.db, "last_scheduler_tick", "")
        db_last_action = get_bot_setting(self.db, "last_scheduler_action", "")
        db_last_error = get_bot_setting(self.db, "last_scheduler_error", "")

        with rt["lock"]:
            d_tick = db_last_tick or rt["last_tick"] or "No tick recorded yet"
            d_action = db_last_action or rt["last_action"]
            d_grade = rt["last_grade_status"]
            d_hw_sub = rt["last_hw_sub_status"]
            d_err = db_last_error or rt["last_error"]

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
            else "🔴 **Scheduler: No recent tick (check GitHub Actions)**"
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

        # Load common data
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

        hw_options = {0: "— None —"}
        for _, r in hw_df.iterrows():
            hw_options[int(r["homework_id"])] = str(r["title"])

        qz_options = {0: "— None —"}
        for _, r in qz_df.iterrows():
            qz_options[int(r["quiz_id"])] = str(r["title"])

        # ===================================================================
        # TAB 1: GROUP SCHEDULE TABLE (4 COLUMNS + MULTI-TIMING MANAGER)
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

                # --- 4-COLUMN MASTER SCHEDULE OVERVIEW TABLE ---
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
                    cur_hw_id = int(cfg_row["latest_hw_id"]) if pd.notna(cfg_row.get("latest_hw_id")) else 0
                    cur_qz_id = int(cfg_row["latest_quiz_id"]) if pd.notna(cfg_row.get("latest_quiz_id")) else 0
                    overview_rows.append({
                        "Group": f"Group {g}",
                        "Scheduled Timings": timings_str,
                        "Latest Homework (Manual)": hw_options.get(cur_hw_id, "— None —"),
                        "Latest Quiz (Manual)": qz_options.get(cur_qz_id, "— None —"),
                    })
                st.dataframe(pd.DataFrame(overview_rows), hide_index=True, use_container_width=True)

                st.divider()

                # --- EDIT GROUP TIMINGS, LATEST HW, AND LATEST QUIZ ---
                col_left, col_right = st.columns([1.2, 1.3])

                with col_left:
                    with st.container(border=True):
                        st.markdown("#### 🕒 Add / Remove Group Lecture Timings")
                        st.caption("You can add multiple meeting days and times to the same group.")
                        sel_grp_time = st.selectbox("Select Group", groups, key="sched_grp_sel")

                        c_d, c_t = st.columns(2)
                        with c_d:
                            sel_days = st.multiselect(
                                "Day(s) of Week",
                                DAYS_OF_WEEK,
                                default=["Sat"],
                                format_func=lambda d: f"{d} ({DAY_FULL_NAMES[d]})",
                            )
                        with c_t:
                            sel_time = st.time_input(
                                "Lecture Start Time",
                                value=datetime.time(16, 0),
                                step=datetime.timedelta(minutes=15),
                            )

                        if st.button("➕ Add Timing(s) to Group", type="primary", use_container_width=True):
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
                                        (sel_grp_time.strip(), d, t_str),
                                    )
                                st.success(f"Added {', '.join(sel_days)} at {t_str} to Group {sel_grp_time}!")
                                st.rerun()

                        grp_m = meetings_df[meetings_df["group_number"].astype(str).str.strip() == sel_grp_time]
                        if not grp_m.empty:
                            st.markdown(f"**Current Timings for Group {sel_grp_time}:**")
                            for _, mr in grp_m.iterrows():
                                mc1, mc2 = st.columns([3, 1])
                                mc1.write(f"• **{DAY_FULL_NAMES.get(mr['day_of_week'], mr['day_of_week'])}** at `{mr['meeting_time']}`")
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
                            "Used for the 10-min pre-lecture Teacher Reports and the 6-hour Student Reminders."
                        )
                        with st.form("update_group_pace_form"):
                            target_grp = st.selectbox("Select Group to Configure", groups, key="cfg_grp_sel")
                            cur_cfg = configs_map.get(target_grp, {})
                            init_hw = int(cur_cfg["latest_hw_id"]) if pd.notna(cur_cfg.get("latest_hw_id")) and int(cur_cfg["latest_hw_id"]) in hw_options else 0
                            init_qz = int(cur_cfg["latest_quiz_id"]) if pd.notna(cur_cfg.get("latest_quiz_id")) and int(cur_cfg["latest_quiz_id"]) in qz_options else 0
                            init_jid = str(cur_cfg.get("wa_group_jid") or "") if pd.notna(cur_cfg.get("wa_group_jid")) else ""

                            new_hw_id = st.selectbox(
                                "3rd Column: Latest Homework for this Group",
                                options=list(hw_options.keys()),
                                index=list(hw_options.keys()).index(init_hw),
                                format_func=lambda k: hw_options[k],
                            )
                            new_qz_id = st.selectbox(
                                "4th Column: Latest Quiz for this Group",
                                options=list(qz_options.keys()),
                                index=list(qz_options.keys()).index(init_qz),
                                format_func=lambda k: qz_options[k],
                            )
                            new_jid = st.text_input(
                                "Optional: WhatsApp Group JID (for Group Chat Broadcasts)",
                                value=init_jid,
                                placeholder="e.g. 12036304...@g.us",
                            ).strip()

                            if st.form_submit_button("💾 Save Group Assignment Pace", type="primary", use_container_width=True):
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
                                        target_grp.strip(),
                                        int(new_hw_id) if new_hw_id else None,
                                        int(new_qz_id) if new_qz_id else None,
                                        new_jid or None,
                                    ),
                                )
                                st.success(f"Saved Latest Homework & Quiz for Group {target_grp}!")
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
                                hid = int(cfg_r["latest_hw_id"]) if pd.notna(cfg_r.get("latest_hw_id")) else None
                                qid = int(cfg_r["latest_quiz_id"]) if pd.notna(cfg_r.get("latest_quiz_id")) else None
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
                        hid = int(cfg_r["latest_hw_id"]) if pd.notna(cfg_r.get("latest_hw_id")) else None
                        qid = int(cfg_r["latest_quiz_id"]) if pd.notna(cfg_r.get("latest_quiz_id")) else None
                        if not hid:
                            st.error(f"Please assign a Latest Homework to Group {test_grp} first!")
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
        # TAB 2: MANUAL BROADCAST TAB (PRIVATE OR WHATSAPP GROUP)
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
