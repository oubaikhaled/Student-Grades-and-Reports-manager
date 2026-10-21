import datetime
import hashlib
import io
import os
import random
import re
import threading
import time
import uuid
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
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("01"):
        return "2" + digits                 # 010... -> 2010...
    if len(digits) == 10 and digits.startswith("1"):
        return "20" + digits                # 10... -> 2010...
    if digits.startswith("20") and len(digits) == 12:
        return digits
    if len(digits) >= 11 and not digits.startswith("0"):
        return digits                       # already international; do not mangle
    return None


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
                rt = get_daemon_runtime()
                with rt["lock"]:
                    rt["gowa_last_ok"] = time.time()
                return True, msg
            last_err = msg
            if "no WhatsApp device is linked" in msg:
                return False, msg
            time.sleep(4)
        return False, f"Wake-up timeout: {last_err}"

    def send_text(self, recipient: str, message: str, retries: int = 4) -> tuple[bool, str]:
        """
        Sends a WhatsApp text message with automatic wake-up and progressive retry on failure.
        Note: ReadTimeouts are treated as sent (unconfirmed) to avoid duplicate messages when
        GOWA delivers the message on a slow/cold instance before the HTTP response times out.
        """
        target = clean_wa_phone(recipient)
        if not target:
            return False, f"Invalid phone/recipient: {recipient}"

        rt = get_daemon_runtime()
        with rt["lock"]:
            last_ok = rt.get("gowa_last_ok", 0.0)
        if time.time() - last_ok > 300:
            self.ensure_awake(max_wait_seconds=45)

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
                            with rt["lock"]:
                                rt["gowa_last_ok"] = time.time()
                            return True, "Sent"
                    except Exception:
                        with rt["lock"]:
                            rt["gowa_last_ok"] = time.time()
                        return True, "Sent"
                elif resp.status_code == 429 or resp.status_code >= 500:
                    last_err = f"HTTP {resp.status_code}: {resp.text[:150]}"
                else:
                    return False, f"HTTP {resp.status_code}: {resp.text[:150]}"
            except requests.exceptions.ReadTimeout:
                # request reached the server; retrying would likely duplicate it
                print("[GOWA] read timeout, treating as sent (unconfirmed)")
                return True, "Sent (unconfirmed: read timeout)"
            except (requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError) as e:
                last_err = str(e)          # never reached the server: safe to retry
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
        """
        Sends a PDF file via GOWA /send/file with automatic wake-up and progressive retry on failure.
        Note: ReadTimeouts are treated as sent (unconfirmed) to avoid duplicate PDFs.
        """
        target = clean_wa_phone(recipient)
        if not target:
            return False, f"Invalid phone/recipient: {recipient}"

        rt = get_daemon_runtime()
        with rt["lock"]:
            last_ok = rt.get("gowa_last_ok", 0.0)
        if time.time() - last_ok > 300:
            self.ensure_awake(max_wait_seconds=45)

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
                            with rt["lock"]:
                                rt["gowa_last_ok"] = time.time()
                            return True, "PDF Sent"
                    except Exception:
                        with rt["lock"]:
                            rt["gowa_last_ok"] = time.time()
                        return True, "PDF Sent"
                elif resp.status_code == 429 or resp.status_code >= 500:
                    last_err = f"HTTP {resp.status_code}: {resp.text[:150]}"
                else:
                    return False, f"HTTP {resp.status_code}: {resp.text[:150]}"
            except requests.exceptions.ReadTimeout:
                # request reached the server; retrying would likely duplicate it
                print("[GOWA] read timeout, treating as sent (unconfirmed)")
                return True, "Sent (unconfirmed: read timeout)"
            except (requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError) as e:
                last_err = str(e)          # never reached the server: safe to retry
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
            c.execute("ALTER TABLE whatsapp_retry_queue ADD COLUMN IF NOT EXISTS dedup_key TEXT")
            c.execute("UPDATE whatsapp_retry_queue SET dedup_key = NULL WHERE dedup_key = ''")
            c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_wa_queue_dedup "
                "ON whatsapp_retry_queue (dedup_key) WHERE dedup_key IS NOT NULL"
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
    except Exception as e:
        print(f"[BotSettings] Error reading key '{key}': {e}")
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
    for attempt in range(2):
        try:
            db.execute_query(
                "DELETE FROM automated_dispatch_log WHERE dispatch_key = %s",
                (key,),
            )
            return
        except Exception as e:
            if attempt == 0:
                time.sleep(1)
            else:
                print(f"[Dispatch] Failed to release key '{key}' after retry: {e}")


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
# Outbound Queue & Retry Engine
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
    attempts: int = 1,
    dedup_key: str | None = None,
):
    """Saves a text or PDF report into the DB so the daemon sends/retries it until delivered."""
    clean_dedup = str(dedup_key).strip() if dedup_key else None
    try:
        if clean_dedup:
            existing = db.execute_scalar(
                "SELECT COUNT(*) FROM whatsapp_retry_queue WHERE dedup_key = %s",
                (clean_dedup,),
            )
            if existing and int(existing) > 0:
                return

        db.execute_query(
            """
            INSERT INTO whatsapp_retry_queue (
                recipient, message_type, message_text, kind, item_id, student_id,
                student_name, item_title, score, max_score, perc, report_text, video_link,
                attempts, last_error, dedup_key
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                recipient, message_type, message_text, kind, int(item_id or 0), str(student_id or ""),
                student_name, item_title, float(score or 0), float(max_score or 0), float(perc or 0),
                report_text or "", video_link or "", int(attempts), str(last_error or ""), clean_dedup,
            ),
        )
    except Exception as e:
        print(f"[RetryQueue] Could not enqueue message for {recipient}: {e}")


def enqueue_outbound(db, recipient, message_type, **kw):
    """Put a fresh outbound message in the queue. The daemon sends it (attempts starts at 0)."""
    enqueue_failed_whatsapp(db, recipient, message_type, attempts=0, **kw)


def process_whatsapp_retry_queue(db, client: GowaClient, rt):
    """
    Called every 60 seconds by `run_scheduled_checks_once` (and immediately via `trigger_queue_drain_async`).
    Drains `whatsapp_retry_queue` safely one row at a time up to 15 attempts.
    """
    with rt["lock"]:
        qlock = rt.setdefault("queue_lock", threading.Lock())
    if not qlock.acquire(blocking=False):
        return
    try:
        try:
            q_df = db.fetch_dataframe(
                "SELECT * FROM whatsapp_retry_queue WHERE attempts < 15 ORDER BY queue_id ASC LIMIT 25"
            )
        except Exception as e:
            _record_scheduler_error(db, rt, f"Queue fetch error: {e}")
            return

        if q_df.empty:
            return

        ok_awake, awake_msg = client.ensure_awake(max_wait_seconds=55)
        if not ok_awake:
            _record_scheduler_error(db, rt, f"Queue paused (GOWA not awake): {awake_msg}")
            return

        t0 = time.time()
        for idx, (_, row) in enumerate(q_df.iterrows()):
            if time.time() - t0 > 45:
                break

            try:
                qid = int(row["queue_id"])
                recipient = str(row["recipient"])
                mtype = str(row["message_type"])
                attempts = int(row.get("attempts") or 0)

                db.execute_query(
                    "UPDATE whatsapp_retry_queue SET attempts = attempts + 1 WHERE queue_id = %s",
                    (qid,),
                )

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
                    msg_txt = str(row.get("message_text") or "")
                    caption_str = msg_txt if msg_txt else f"📄 {student_name} — {item_title}"

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
                            recipient, pdf_bytes, pdf_filename, caption=caption_str, retries=1
                        )
                    else:
                        ok, err = client.send_text(recipient, caption_str, retries=1)
                else:
                    ok, err = True, "Unknown type"

                if ok:
                    db.execute_query("DELETE FROM whatsapp_retry_queue WHERE queue_id = %s", (qid,))
                    _record_scheduler_action(db, rt, f"Queue Sent -> {recipient} ({mtype})")
                else:
                    db.execute_query(
                        "UPDATE whatsapp_retry_queue SET last_error = %s WHERE queue_id = %s",
                        (str(err)[:200], qid),
                    )
                    _record_scheduler_error(db, rt, f"Attempt #{attempts + 1} failed for {recipient}: {err}")
            except Exception as row_err:
                _record_scheduler_error(db, rt, f"Row drain exception: {row_err}")

            if idx < len(q_df) - 1:
                time.sleep(3)
    finally:
        qlock.release()


def trigger_queue_drain_async(db):
    """Immediately starts a background drain of whatsapp_retry_queue if one isn't already running."""
    rt = get_daemon_runtime()
    client = GowaClient.from_secrets(db)
    threading.Thread(
        target=process_whatsapp_retry_queue,
        args=(db, client, rt),
        daemon=True,
    ).start()


# ---------------------------------------------------------------------------
# Instant Homework Submission Alert to Assistant (Atomic Single-Send)
# ---------------------------------------------------------------------------
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
    clean_hw_id = int(homework_id) if homework_id is not None else 0
    clean_stu_id = str(student_id or "").strip()
    dedup_id = f"hw_{clean_hw_id}_stu_{clean_stu_id}"
    now_ts = time.time()
    with rt["lock"]:
        recent_subs = rt.setdefault("recent_hw_subs", {})
        if now_ts - recent_subs.get(dedup_id, 0.0) < 60.0:
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
            client.ensure_awake(max_wait_seconds=45)
            sent_to, errors = [], []
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
                    enqueue_outbound(db, asst_phone, "text", message_text=msg, last_error=err)
            with rt["lock"]:
                if sent_to:
                    rt["last_hw_sub_status"] = f"Sent to {chr(44).join(sent_to)} for {student_name} ({homework_title})"
                    rt["last_action"] = f"HW Sub Alert -> {len(sent_to)} Assistant(s)"
                if errors:
                    rt["last_hw_sub_status"] = f"Partly queued for retry for {student_name}: {chr(59).join(errors)}"
        except Exception as e:
            with rt["lock"]:
                rt["last_error"] = f"Assistant HW Sub Exception ({student_name}): {e}"

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
    Enqueues deduplicated grade reports into `whatsapp_retry_queue` synchronously
    and triggers an immediate background queue drain.
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

    score_str = str(int(score)) if float(score).is_integer() else f"{score:.1f}"
    max_str = str(int(max_score)) if float(max_score).is_integer() else f"{max_score:.1f}"
    type_label = "Quiz" if kind == "quiz" else "Homework"

    prefix_key = f"grade_{kind}_{item_id}_{student_id}_"
    try:
        prev = db.execute_scalar(
            "SELECT COUNT(*) FROM automated_dispatch_log WHERE dispatch_key LIKE %s",
            (prefix_key + "%",),
        )
        is_correction = bool(prev and int(prev) > 0)
    except Exception:
        is_correction = False
    corr_prefix = "🔁 *تصحيح للدرجة السابقة*\n\n" if is_correction else ""

    queued = 0

    if auto_enabled:
        parent_msg = corr_prefix + emoji.emojize(
            f":bar_chart: درجة الـ {item_title}\n\n"
            f"ولي الأمر الكريم،\n"
            f"نحيط حضرتكم علمًا بأن الطالب *{student_name}* حصل على *{score_str} / {max_str}* ({perc:.1f}%) في الـ {
