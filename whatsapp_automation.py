import datetime
import io
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

# Thread-safe runtime diagnostics for the background daemon
_DAEMON_LOCK = threading.Lock()
_DAEMON_STATE = {
    "running": False,
    "last_tick": None,
    "last_error": None,
    "last_action": "Waiting for first tick...",
    "cached_url": DEFAULT_GOWA_URL,
    "cached_auth": "",
    "cached_teacher_phone": "",
}


def _sync_secrets_to_cache_and_db(db=None):
    """
    Safely reads st.secrets from the main Streamlit thread and caches them in memory
    (and persists fallback defaults into bot_settings) so background threads never fail
    when accessing credentials without a Streamlit ScriptRunContext.
    """
    try:
        s_url = st.secrets.get("WHATSAPP_API_URL", "")
        s_auth = st.secrets.get("WHATSAPP_API_AUTH", "")
        s_tphone = st.secrets.get("TEACHER_PHONE", "")
        with _DAEMON_LOCK:
            if s_url:
                _DAEMON_STATE["cached_url"] = str(s_url).strip()
            if s_auth:
                _DAEMON_STATE["cached_auth"] = str(s_auth).strip()
            if s_tphone:
                _DAEMON_STATE["cached_teacher_phone"] = str(s_tphone).strip()
    except Exception:
        pass

    if db is not None:
        try:
            if not get_bot_setting(db, "gowa_url", "") and _DAEMON_STATE["cached_url"]:
                set_bot_setting(db, "gowa_url", _DAEMON_STATE["cached_url"])
            if not get_bot_setting(db, "gowa_auth", "") and _DAEMON_STATE["cached_auth"]:
                set_bot_setting(db, "gowa_auth", _DAEMON_STATE["cached_auth"])
            if not get_bot_setting(db, "teacher_phone", "") and _DAEMON_STATE["cached_teacher_phone"]:
                set_bot_setting(db, "teacher_phone", _DAEMON_STATE["cached_teacher_phone"])
        except Exception:
            pass


def clean_wa_phone(p_str: str) -> str | None:
    """Normalizes Egyptian phone numbers to international format without '+' (e.g. '201012345678')."""
    if p_str is None or pd.isna(p_str):
        return None
    s = str(p_str).strip()
    if not s or s.lower() == "nan" or s.lower() == "none":
        return None
    if "@g.us" in s or "@s.whatsapp.net" in s:
        return s
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
        with _DAEMON_LOCK:
            url = _DAEMON_STATE.get("cached_url") or DEFAULT_GOWA_URL
            auth = _DAEMON_STATE.get("cached_auth") or ""

        try:
            url = st.secrets.get("WHATSAPP_API_URL", url)
            auth = st.secrets.get("WHATSAPP_API_AUTH", auth)
        except Exception:
            pass

        if db is not None:
            saved_url = get_bot_setting(db, "gowa_url", "")
            saved_auth = get_bot_setting(db, "gowa_auth", "")
            if saved_url:
                url = saved_url
            if saved_auth:
                auth = saved_auth
        return cls(base_url=url, basic_auth=auth)

    def ensure_awake(self, max_wait_seconds: int = 75) -> tuple[bool, str]:
        """
        Wakes up the Render GOWA instance if it is hibernating (cold start can take 45-60s).
        Retries until the server responds or max_wait_seconds is reached.
        """
        deadline = time.time() + max_wait_seconds
        last_err = "Unknown error"
        while time.time() < deadline:
            ok, msg = self.check_status(timeout=25)
            if ok:
                return True, msg
            last_err = msg
            # If the server is online but explicitly says no device linked, don't keep looping
            if "no WhatsApp device is linked" in msg:
                return False, msg
            time.sleep(5)
        return False, f"GOWA wake-up timed out: {last_err}"

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

    def send_text(self, recipient: str, message: str, retries: int = 2) -> tuple[bool, str]:
        """Sends a WhatsApp text message to a phone number or group JID (@g.us) with retry for Render cold starts."""
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
                    timeout=65,
                )
                if resp.status_code in (200, 201):
                    return True, "Sent"
                last_err = f"HTTP {resp.status_code}: {resp.text[:150]}"
            except Exception as e:
                last_err = str(e)
            if attempt < retries:
                time.sleep(4)
        return False, last_err

    def send_pdf(self, recipient: str, pdf_bytes: bytes, filename: str, caption: str = "", retries: int = 2) -> tuple[bool, str]:
        """Sends a PDF file with an optional caption via GOWA /send/file with fresh BytesIO on each retry."""
        target = clean_wa_phone(recipient)
        if not target:
            return False, f"Invalid phone/recipient: {recipient}"

        data = {
            "phone": target,
            "caption": caption,
        }
        last_err = ""
        for attempt in range(retries + 1):
            files = {
                "file": (filename, io.BytesIO(pdf_bytes), "application/pdf"),
            }
            try:
                resp = requests.post(
                    f"{self.base_url}/send/file",
                    data=data,
                    files=files,
                    auth=self.auth,
                    timeout=75,
                )
                if resp.status_code in (200, 201):
                    return True, "PDF Sent"
                last_err = f"HTTP {resp.status_code}: {resp.text[:150]}"
            except Exception as e:
                last_err = str(e)
            if attempt < retries:
                time.sleep(4)

        # Fallback: if PDF upload failed, still attempt to deliver caption text
        if caption:
            self.send_text(target, caption, retries=1)
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
    """Gets teacher phone from DB first, then memory cache, then st.secrets."""
    t_phone = get_bot_setting(db, "teacher_phone", "")
    if not t_phone:
        with _DAEMON_LOCK:
            t_phone = _DAEMON_STATE.get("cached_teacher_phone", "")
    if not t_phone:
        try:
            t_phone = st.secrets.get("TEACHER_PHONE", "")
        except Exception:
            pass
    return clean_wa_phone(t_phone)


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
    Runs in a background thread immediately after an admin grades a student
    so saving a grade stays instant while the parent receives the grade + PDF report.
    """
    # Sync secrets while still on the main Streamlit thread before spawning worker
    _sync_secrets_to_cache_and_db(db)
    client = GowaClient.from_secrets(db)

    def _worker():
        try:
            auto_enabled = get_bot_setting(db, "auto_send_on_grade", "true").lower() == "true"
            if not auto_enabled:
                return

            stu_df = db.fetch_dataframe(
                "SELECT name, phone, phone_parent FROM students WHERE id = %s",
                (str(student_id),),
            )
            if stu_df.empty:
                return
            stu = stu_df.iloc[0]
            student_name = str(stu["name"])
            parent_phone = clean_wa_phone(stu.get("phone_parent"))
            student_phone = clean_wa_phone(stu.get("phone"))

            if not parent_phone and not student_phone:
                return

            video_link = None
            if kind == "homework":
                hw_df = db.fetch_dataframe(
                    "SELECT video_link FROM homeworks WHERE homework_id = %s", (int(item_id),)
                )
                if not hw_df.empty and pd.notna(hw_df.iloc[0]["video_link"]):
                    video_link = str(hw_df.iloc[0]["video_link"]).strip() or None

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

            extra = {"video_link": video_link} if video_link else {}
            pdf_buf = PDFGenerator.generate_student_report(
                student_name, item_title, score, max_score, perc, report_text, img_bytes, **extra
            )
            pdf_bytes = pdf_buf.getvalue()
            pdf_filename = f"{student_name}_{item_title}_Report.pdf".replace(" ", "_")

            # Wake GOWA if sleeping on Render
            client.ensure_awake(max_wait_seconds=60)

            if parent_phone:
                ok, err = client.send_pdf(parent_phone, pdf_bytes, pdf_filename, caption=parent_msg)
                with _DAEMON_LOCK:
                    _DAEMON_STATE["last_action"] = f"Post-grade parent send ({student_name}): {'OK' if ok else err}"

            if student_phone and get_bot_setting(db, "also_send_grade_to_student", "false").lower() == "true":
                stu_msg = emoji.emojize(
                    f":bar_chart: درجة الـ {item_title}\n\n"
                    f"أهلاً بك يا *{student_name}*،\n"
                    f"لقد حصلت على *{score_str} / {max_str}* في الـ {type_label} الأخير.\n"
                    f"مرفق تقرير الملاحظات الخاص بك. استمر في الاجتهاد! :glowing_star:\n\n"
                    f"Mathematics Team – Eng. Mahmoud Adel"
                )
                client.send_pdf(student_phone, pdf_bytes, pdf_filename, caption=stu_msg)
        except Exception as e:
            with _DAEMON_LOCK:
                _DAEMON_STATE["last_error"] = f"Post-Grade Error: {e}"
            print(f"[WhatsApp Auto-Grade Send Error]: {e}")

    threading.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Reusable Morning Schedule Builder & Sender
# ---------------------------------------------------------------------------
def send_morning_schedule_to_teacher(db, client: GowaClient, teacher_phone_clean: str, now: datetime.datetime) -> tuple[bool, str]:
    """Builds and sends today's morning schedule + exciting sentence to the teacher."""
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

    client.ensure_awake(max_wait_seconds=60)
    return client.send_text(teacher_phone_clean, msg)


# ---------------------------------------------------------------------------
# Background Scheduler Tick (Checks Morning Schedule, 10-Min Teacher Reports, 6-Hr Reminders)
# ---------------------------------------------------------------------------
def run_scheduled_checks_once(db):
    """
    Evaluates current Cairo time against all scheduled group timings:
    1. Morning Teacher Schedule + Random Exciting Sentence.
    2. 10-Minute Pre-Group Homework & Quiz Master PDF Reports to Teacher.
    3. 6-Hour Pre-Group Homework & Quiz Reminders to Students who haven't submitted.
    """
    now = datetime.datetime.now(CAIRO_TZ)
    today_str = now.strftime("%Y-%m-%d")
    today_day = now.strftime("%a")  # 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'

    with _DAEMON_LOCK:
        _DAEMON_STATE["last_tick"] = now.strftime("%Y-%m-%d %H:%M:%S")

    teacher_phone_clean = get_resolved_teacher_phone(db)
    client = GowaClient.from_secrets(db)

    # -----------------------------------------------------------------------
    # WORKFLOW 1: Daily Morning Schedule to Teacher
    # -----------------------------------------------------------------------
    morning_time_str = get_bot_setting(db, "morning_schedule_time", "08:00")
    try:
        m_hr, m_min = [int(x) for x in morning_time_str.strip().split(":")[:2]]
        morning_dt = now.replace(hour=m_hr, minute=m_min, second=0, microsecond=0)
    except Exception:
        morning_dt = now.replace(hour=8, minute=0, second=0, microsecond=0)

    morning_key = f"morning_sched_{today_str}"
    # Allow catch-up anytime within 6 hours after scheduled morning time if not yet sent today
    if teacher_phone_clean and now >= morning_dt and (now - morning_dt).total_seconds() <= 21600:
        if not has_dispatched(db, morning_key):
            ok, err = send_morning_schedule_to_teacher(db, client, teacher_phone_clean, now)
            if ok:
                mark_dispatched(db, morning_key)
                with _DAEMON_LOCK:
                    _DAEMON_STATE["last_action"] = f"Sent Morning Schedule ({today_str})"
            else:
                with _DAEMON_LOCK:
                    _DAEMON_STATE["last_error"] = f"Morning Schedule Failed: {err}"

    # Fetch all configured meetings and group configs using TRIM() so whitespace never breaks the join
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
        return

    hw_map = {}
    hw_df = db.fetch_dataframe("SELECT homework_id, title, total_questions FROM homeworks")
    for _, r in hw_df.iterrows():
        hw_map[int(r["homework_id"])] = (str(r["title"]), int(r["total_questions"]))

    qz_map = {}
    qz_df = db.fetch_dataframe("SELECT quiz_id, title, max_score FROM quizzes")
    for _, r in qz_df.iterrows():
        qz_map[int(r["quiz_id"])] = (str(r["title"]), float(r["max_score"]))

    # -----------------------------------------------------------------------
    # Check Each of Today's Meetings for 10-Min Teacher Report & 6-Hr Student Reminders
    # -----------------------------------------------------------------------
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

        # -------------------------------------------------------------------
        # WORKFLOW 2: 10 Minutes Before Group -> Send HW & Quiz Reports to Teacher
        # (Triggers when minutes_until_lecture is between -5 and 15 minutes)
        # -------------------------------------------------------------------
        pre10_key = f"teacher_10min_{today_str}_grp_{grp}_{mtime}"
        if teacher_phone_clean and -5 <= minutes_until_lecture <= 15 and not has_dispatched(db, pre10_key):
            ok = send_group_master_reports_to_teacher(
                db, client, teacher_phone_clean, grp, mtime, hw_id, qz_id, hw_map, qz_map
            )
            if ok:
                mark_dispatched(db, pre10_key)
                with _DAEMON_LOCK:
                    _DAEMON_STATE["last_action"] = f"Sent 10-min Report for Group {grp}"
            else:
                with _DAEMON_LOCK:
                    _DAEMON_STATE["last_error"] = f"10-min Report failed for Group {grp}"

        # -------------------------------------------------------------------
        # WORKFLOW 3: 6 Hours (360 mins) Before Group -> Remind Students Who Didn't Submit HW
        # (Triggers when minutes_until_lecture is between 300 and 380 minutes)
        # -------------------------------------------------------------------
        rem6h_key = f"student_6hr_{today_str}_grp_{grp}_{mtime}"
        if (hw_id or qz_id) and 300 <= minutes_until_lecture <= 380 and not has_dispatched(db, rem6h_key):
            ok = send_6hr_unsubmitted_reminders(
                db, client, grp, mtime, hw_id, qz_id, hw_map, qz_map
            )
            if ok:
                mark_dispatched(db, rem6h_key)
                with _DAEMON_LOCK:
                    _DAEMON_STATE["last_action"] = f"Sent 6-hr Reminders for Group {grp}"
            else:
                with _DAEMON_LOCK:
                    _DAEMON_STATE["last_error"] = f"6-hr Reminders failed for Group {grp}"


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

    client.ensure_awake(max_wait_seconds=60)

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
        ok_hw, _ = client.send_pdf(
            teacher_phone,
            pdf_buf.getvalue(),
            f"Group_{grp}_{hw_title}_HW_Report.pdf".replace(" ", "_"),
            caption=caption,
        )
        any_sent = any_sent or ok_hw

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
        ok_qz, _ = client.send_pdf(
            teacher_phone,
            pdf_buf_q.getvalue(),
            f"Group_{grp}_{qz_title}_Quiz_Report.pdf".replace(" ", "_"),
            caption=caption_q,
        )
        any_sent = any_sent or ok_qz

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
        return True  # Everyone submitted; nothing to remind

    client.ensure_awake(max_wait_seconds=60)
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
            time.sleep(5)  # Safe anti-ban pacing between messages

    return sent_any


@st.cache_resource(show_spinner=False)
def start_background_automation_daemon(_db):
    """Starts a single daemon thread per server process that checks the schedule every 60 seconds."""
    init_automation_tables(_db)
    _sync_secrets_to_cache_and_db(_db)

    with _DAEMON_LOCK:
        if _DAEMON_STATE["running"]:
            return True
        _DAEMON_STATE["running"] = True

    def _loop():
        while True:
            try:
                run_scheduled_checks_once(_db)
            except Exception as e:
                with _DAEMON_LOCK:
                    _DAEMON_STATE["last_error"] = str(e)
                print(f"[Automation Daemon Error]: {e}")
            time.sleep(60)

    t = threading.Thread(target=_loop, daemon=True, name="WhatsAppAutomationDaemon")
    t.start()
    return True


# ---------------------------------------------------------------------------
# UI Renderer for the New "🤖 Automated Messages" Tab
# ---------------------------------------------------------------------------
class AutomatedMessagesView:
    def __init__(self, db):
        self.db = db
        init_automation_tables(self.db)
        _sync_secrets_to_cache_and_db(self.db)
        start_background_automation_daemon(self.db)

    def render(self):
        st.subheader("🤖 Automated WhatsApp Bot & Group Schedules")
        st.caption(
            "Configure group lecture timings, assign each group's latest Homework & Quiz, "
            "and manage automated teacher/student/parent WhatsApp notifications."
        )

        # Live Daemon Status Bar
        now_cairo = datetime.datetime.now(CAIRO_TZ).strftime("%Y-%m-%d %H:%M:%S")
        with _DAEMON_LOCK:
            d_running = _DAEMON_STATE["running"]
            d_tick = _DAEMON_STATE["last_tick"] or "Starting..."
            d_action = _DAEMON_STATE["last_action"]
            d_err = _DAEMON_STATE["last_error"]

        status_col1, status_col2, status_col3 = st.columns(3)
        status_col1.caption(f"🟢 **Daemon:** {'Active' if d_running else 'Stopped'} | 🕒 **Cairo Time:** `{now_cairo}`")
        status_col2.caption(f"🔄 **Last Check:** `{d_tick}`")
        status_col3.caption(f"📌 **Last Event:** `{d_action}`")
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

                        # Show existing timings for this group with delete buttons
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
                            with st.spinner("Waking bot and sending Morning Schedule to Teacher..."):
                                now_c = datetime.datetime.now(CAIRO_TZ)
                                ok, err = send_morning_schedule_to_teacher(
                                    self.db, GowaClient.from_secrets(self.db), t_phone, now_c
                                )
                                if ok:
                                    mark_dispatched(self.db, f"morning_sched_{now_c.strftime('%Y-%m-%d')}")
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
                                    time.sleep(4)  # Anti-ban pacing
                            prog.empty()
                            st.success(f"🎉 Broadcast complete! Successfully sent {sent_count} WhatsApp message(s).")

        # ===================================================================
        # TAB 3: BOT & TEACHER SETTINGS
        # ===================================================================
        with tab_settings:
            st.markdown("#### ⚙️ GOWA Bot Connection & Teacher Preferences")
            cur_url = get_bot_setting(self.db, "gowa_url", _DAEMON_STATE.get("cached_url", DEFAULT_GOWA_URL))
            cur_auth = get_bot_setting(self.db, "gowa_auth", _DAEMON_STATE.get("cached_auth", ""))
            cur_t_phone = get_bot_setting(self.db, "teacher_phone", _DAEMON_STATE.get("cached_teacher_phone", ""))
            cur_m_time = get_bot_setting(self.db, "morning_schedule_time", "08:00")
            cur_auto_grade = get_bot_setting(self.db, "auto_send_on_grade", "true").lower() == "true"
            cur_also_stu = get_bot_setting(self.db, "also_send_grade_to_student", "false").lower() == "true"

            col_st1, col_st2 = st.columns([1, 1])
            with col_st1:
                if st.button("🔌 Test GOWA Connection", use_container_width=True):
                    with st.spinner("Pinging GOWA server (may take up to 50s if Render is waking up)..."):
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
                in_m_time = st.text_input(
                    "☀️ Daily Morning Schedule Send Time (24h Cairo Time, e.g. 08:00)",
                    value=cur_m_time,
                )
                in_auto_grade = st.checkbox(
                    "✅ Automatically send Grade + PDF Report to Parent on WhatsApp right after saving a grade",
                    value=cur_auto_grade,
                )
                in_also_stu = st.checkbox(
                    "🎓 Also send Grade + PDF Report to Student's phone right after saving a grade",
                    value=cur_also_stu,
                )

                if st.form_submit_button("💾 Save Bot Settings", type="primary", use_container_width=True):
                    set_bot_setting(self.db, "gowa_url", in_url.strip() or DEFAULT_GOWA_URL)
                    set_bot_setting(self.db, "gowa_auth", in_auth.strip())
                    set_bot_setting(self.db, "teacher_phone", in_t_phone.strip())
                    set_bot_setting(self.db, "morning_schedule_time", in_m_time.strip() or "08:00")
                    set_bot_setting(self.db, "auto_send_on_grade", "true" if in_auto_grade else "false")
                    set_bot_setting(self.db, "also_send_grade_to_student", "true" if in_also_stu else "false")
                    _sync_secrets_to_cache_and_db(self.db)
                    st.success("✅ Bot settings saved!")
                    st.rerun()
