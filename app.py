import base64
import io
import math
import re
import threading
import time
import urllib.parse
import uuid
import emoji
import pandas as pd
import psycopg2
import requests
import streamlit as st

from ai_grading import (
    fetch_pdf_from_drive,
    get_gemini_api_key,
    grade_full_homework,
    parse_api_keys,
)
from auth import AuthManager
from database import DatabaseManager
from pdf_utils import PDFGenerator
from student_profile import StudentProfileView
from whatsapp_automation import (
    AutomatedMessagesView,
    trigger_post_grade_whatsapp,
)

# 1. Page Config MUST be the very first Streamlit command
st.set_page_config(
    page_title="Eng. Mahmoud Adel — Grade Portal",
    layout="wide",
    initial_sidebar_state="expanded",
)
# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CACHE_TTL = 120              # seconds; every write also clears the cache immediately
PAGE_SIZE = 20               # rows per page on the WhatsApp screen
MAX_IMG_WIDTH = 1200         # uploaded feedback images are downscaled to this width
LOCK_TIMEOUT_SECONDS = 600   # 10 minutes: auto-releases if an admin closes their tab without saving
MAX_PHOTO_MB = 30            # per homework photo; full-size photos from 50-200 MP phone cameras fit
MAX_TOTAL_UPLOAD_MB = 200    # all files in one homework submission together

KINDS = {
    "homework": {
        "table": "homework_grades", "item_col": "homework_id", "score_col": "correct_answers",
        "name_col": "Assignment", "select_label": "Select Assignment",
        "heading": "**Homework Assignments**", "empty_msg": "No homework grades recorded yet.",
        "noun": "Assignment", "score_label": "Correct Answers", "is_int": True,
    },
    "quiz": {
        "table": "quiz_grades", "item_col": "quiz_id", "score_col": "score",
        "name_col": "Quiz", "select_label": "Select Quiz",
        "heading": "**Quiz Scores**", "empty_msg": "No quiz grades recorded yet.",
        "noun": "Quiz", "score_label": "Final Score", "is_int": False,
    },
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def build_drive_hw_filename(student_id: str, homework_id: int, homework_title: str) -> str:
    """
    Builds a clean, filesystem-safe Google Drive filename that includes student_id,
    homework_id, and the homework title (e.g., '176109,5_Homework_1_Derivatives.pdf').
    """
    safe_title = re.sub(r'[\\/*?:"<>|,]', "", str(homework_title or "")).strip()
    safe_title = re.sub(r"\s+", "_", safe_title)
    if safe_title:
        return f"{student_id},{homework_id}_{safe_title}.pdf"
    return f"{student_id},{homework_id}.pdf"


def clean(v):
    """numpy -> python scalar, NaN/NA -> None (also keeps cache keys hashable)."""
    if v is None:
        return None
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):
        try:
            v = v.item()
        except Exception:
            pass
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def fmt(v):
    v = clean(v)
    if v is None:
        return ""
    try:
        f = float(v)
        return str(int(f)) if f.is_integer() else str(f)
    except (TypeError, ValueError):
        return str(v)


def set_flag(key):
    st.session_state[key] = True


def to_bytes(buf):
    return buf.getvalue() if hasattr(buf, "getvalue") else bytes(buf)


def clean_number(p_str):
    if pd.isna(p_str) or str(p_str).strip() in ["", "None", "nan"]:
        return None
    c_phone = re.sub(r"\D", "", str(p_str))
    if not c_phone:
        return None
    if len(c_phone) == 10 and c_phone.startswith("1"):
        return "20" + c_phone
    elif c_phone.startswith("0"):
        return "2" + c_phone
    elif not c_phone.startswith("20"):
        return "20" + c_phone
    return c_phone


def stitch_images(uploaded_files):
    """Merge uploaded images into a clean 1- or 2-column contact sheet so large multi-page homeworks fit in PDF."""
    if not uploaded_files:
        return None
    from PIL import Image as PILImage, ImageOps
    try:
        images = []
        target_w = 850 if len(uploaded_files) > 3 else MAX_IMG_WIDTH
        for f in uploaded_files[:24]:
            if isinstance(f, bytes):
                f = io.BytesIO(f)
            im = ImageOps.exif_transpose(PILImage.open(f)).convert("RGB")
            if im.width > target_w:
                new_h = int(im.height * target_w / im.width)
                im = im.resize((target_w, new_h), PILImage.Resampling.LANCZOS)
            images.append(im)

        cols = 2 if len(images) >= 4 else 1
        rows = math.ceil(len(images) / cols)
        col_w = max(i.width for i in images)
        row_heights = [
            max(images[r * cols + c].height for c in range(cols) if r * cols + c < len(images))
            for r in range(rows)
        ]

        stitched = PILImage.new("RGB", (col_w * cols, sum(row_heights)), color=(255, 255, 255))
        y = 0
        for r in range(rows):
            for c in range(cols):
                idx = r * cols + c
                if idx < len(images):
                    stitched.paste(images[idx], (c * col_w, y))
            y += row_heights[r]

        out = io.BytesIO()
        stitched.save(out, format="JPEG", quality=78, optimize=True)
        return out.getvalue()
    except Exception as e:
        st.error(f"Image processing failed: {e}")
        return None


def register_heif_opener():
    """Register the HEIC/HEIF opener for iPhone photos if pillow-heif is installed."""
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass


def is_pdf_upload(f) -> bool:
    """Phone pickers may drop the file extension, so also trust the browser's MIME type."""
    return str(f.name).lower().endswith(".pdf") or getattr(f, "type", "") == "application/pdf"


@st.cache_data(show_spinner=False, max_entries=200)
def _upload_thumbnail(file_id: str, _raw_bytes: bytes):
    """Small JPEG preview, rendered on the server so HEIC photos preview in every browser."""
    from PIL import Image as PILImage, ImageOps

    register_heif_opener()
    try:
        with PILImage.open(io.BytesIO(_raw_bytes)) as raw_im:
            # Decode JPEGs at reduced scale: a 50 MP phone photo would otherwise take
            # hundreds of MB of server memory just to make a small preview.
            raw_im.draft("RGB", (660, 660))
            im = ImageOps.exif_transpose(raw_im).convert("RGB")
            im.thumbnail((330, 330))
            out = io.BytesIO()
            im.save(out, format="JPEG", quality=80)
            return out.getvalue()
    except Exception:
        return None


@st.cache_data(show_spinner=False)
def _placeholder_tile(label: str):
    from PIL import Image as PILImage, ImageDraw

    im = PILImage.new("RGB", (330, 330), color=(235, 235, 235))
    ImageDraw.Draw(im).text((165, 165), label, fill=(90, 90, 90), anchor="mm", font_size=48)
    out = io.BytesIO()
    im.save(out, format="PNG")
    return out.getvalue()


def upload_preview(f):
    """Preview image for one uploaded file (a plain tile for PDFs and unreadable images)."""
    if is_pdf_upload(f):
        return _placeholder_tile("PDF")
    return _upload_thumbnail(f.file_id, f.getvalue()) or _placeholder_tile("?")


def convert_uploads_to_pdf_bytes(uploaded_files) -> bytes:
    """
    Converts mobile phone photos (JPG, PNG, WEBP, HEIC/HEIF) and/or PDFs
    into a single compressed, memory-safe multi-page PDF.
    """
    import pymupdf
    from PIL import Image as PILImage, ImageOps

    register_heif_opener()

    if not isinstance(uploaded_files, (list, tuple)):
        uploaded_files = [uploaded_files]

    if len(uploaded_files) == 1 and is_pdf_upload(uploaded_files[0]):
        uploaded_files[0].seek(0)
        return uploaded_files[0].read()

    merged_doc = pymupdf.open()
    try:
        for f in uploaded_files:
            f.seek(0)
            raw_bytes = f.read()
            if not raw_bytes:
                continue

            if is_pdf_upload(f):
                with pymupdf.open(stream=raw_bytes, filetype="pdf") as src_pdf:
                    merged_doc.insert_pdf(src_pdf)
            else:
                with PILImage.open(io.BytesIO(raw_bytes)) as raw_im:
                    im = ImageOps.exif_transpose(raw_im).convert("RGB")
                    max_dim = 1500
                    if max(im.width, im.height) > max_dim:
                        scale = max_dim / float(max(im.width, im.height))
                        im = im.resize(
                            (int(im.width * scale), int(im.height * scale)),
                            PILImage.Resampling.LANCZOS,
                        )
                    img_buf = io.BytesIO()
                    im.save(img_buf, format="JPEG", quality=82, optimize=True)
                    w, h = im.width, im.height
                    img_bytes = img_buf.getvalue()

                rect = pymupdf.Rect(0, 0, w, h)
                page = merged_doc.new_page(width=w, height=h)
                page.insert_image(rect, stream=img_bytes)

        if len(merged_doc) == 0:
            raise ValueError("No valid pages could be read from the uploaded files.")

        return merged_doc.tobytes(deflate=True)
    finally:
        merged_doc.close()

def build_wa_messages(kind, name, title, score, total):
    """Returns (parent_message, student_message)."""
    if score is not None:
        type_ar = "Quiz" if kind == "quiz" else "Homework"
        parent = emoji.emojize(
            f":bar_chart: درجة الـ {title}\n\n"
            f"ولي الأمر الكريم،\n"
            f"نحيط حضرتكم علمًا بأن الطالب {name} حصل على {fmt(score)} / {fmt(total)} في الـ {type_ar} الأخير.\n\n"
            f"نتمنى له مزيدًا من التقدم والنجاح، ونسعى دائمًا لمتابعة مستوى الطالب بشكل مستمر وتحسين نقاط الضعف أولًا بأول. :glowing_star:\n\n"
            f"Mathematics Team – Mahmoud Adel"
        )
        student = emoji.emojize(
            f":bar_chart: درجة الـ {title}\n\n"
            f"أهلاً بك يا {name}،\n"
            f"لقد حصلت على {fmt(score)} / {fmt(total)} في الـ {type_ar} الأخير.\n\n"
            f"استمر في المذاكرة والتدريب، ونتمنى لك دوام التفوق والنجاح! :glowing_star:\n\n"
            f"Mathematics Team – Mahmoud Adel"
        )
    elif kind == "homework":
        parent = emoji.emojize(
            f":bar_chart: homework\n\n"
            f"نحيط حضرتكم علمًا بأن\n"
            f"الطالب: {name}\n"
            f"لم يقم بأداء واجب  : {title}\n\n"
            f"برجاء الالتزام بحضور وأداء الواجبات في المواعيد المحددة، ومتابعة جميع التقييمات أولًا بأول.\n\n"
            f"Mathematics Team – Mahmoud Adel"
        )
        student = emoji.emojize(
            f":bell: تذكير بـ homework\n\n"
            f"أهلاً بك يا {name}،\n"
            f"نذكرك بأنه لم يتم تسجيل أداءك في واجب : {title}\n\n"
            f"برجاء سرعة إتمام الواجب والالتزام بالمواعيد المحددة.\n\n"
            f"Mathematics Team – Mahmoud Adel"
        )
    else:
        parent = emoji.emojize(
            f":bar_chart:  quiz\n\n"
            f"نحيط حضرتكم علمًا بأن\n"
            f"الطالب: {name}\n"
            f"لم يقم بأداء امتحان : {title}\n\n"
            f"برجاء الالتزام بحضور وأداء الاختبارات في المواعيد المحددة، ومتابعة جميع التقييمات أولًا بأول.\n\n"
            f"Mathematics Team – Mahmoud Adel"
        )
        student = emoji.emojize(
            f":bell: تذكير بـ quiz\n\n"
            f"أهلاً بك يا {name}،\n"
            f"نذكرك بأنه لم يتم تسجيل أداءك في كويز : {title}\n\n"
            f"برجاء سرعة إتمام الاختبار والالتزام بالمواعيد المحددة.\n\n"
            f"Mathematics Team – Mahmoud Adel"
        )
    return parent, student


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


@st.cache_resource(show_spinner=False)
def get_db():
    """Created once per server process with a persistent connection pool."""
    db = DatabaseManager(st.secrets["DATABASE_URL"])
    db.init_db()
    return db


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_chapters(_db):
    return _db.fetch_dataframe("SELECT chapter_id, title FROM chapters ORDER BY chapter_id ASC")


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_homeworks(_db):
    return _db.fetch_dataframe(
        "SELECT homework_id, title, total_questions, video_link, chapter_id, rubric FROM homeworks ORDER BY homework_id DESC"
    )


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_quizzes(_db):
    return _db.fetch_dataframe(
        "SELECT quiz_id, title, max_score, chapter_id FROM quizzes ORDER BY quiz_id DESC"
    )


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_students(_db):
    return _db.fetch_dataframe(
        "SELECT id, name, phone, phone_parent, group_number FROM students ORDER BY name ASC"
    )


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_item_grades(_db, kind, item_id):
    cfg = KINDS[kind]
    if kind == "homework":
        return _db.fetch_dataframe(
            f"SELECT student_id, {cfg['score_col']} AS score, percentage, report, "
            f"COALESCE(submitted, FALSE) AS submitted "
            f"FROM {cfg['table']} WHERE {cfg['item_col']} = %s",
            (item_id,),
        )
    return _db.fetch_dataframe(
        f"SELECT student_id, {cfg['score_col']} AS score, percentage, report, "
        f"FALSE AS submitted "
        f"FROM {cfg['table']} WHERE {cfg['item_col']} = %s",
        (item_id,),
    )


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_parent_students(_db, phone):
    return _db.fetch_dataframe(
        "SELECT id, name, phone, phone_parent, group_number, notes FROM students WHERE phone_parent = %s ORDER BY name ASC",
        (phone,),
    )


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_parent_grades(_db, kind, student_ids):
    if kind == "homework":
        sql = """
            SELECT g.student_id, h.homework_id AS item_id, h.title AS "Assignment",
                   g.correct_answers AS "Score", h.total_questions AS "Out Of",
                   g.percentage AS "Percentage", g.report AS "Report"
            FROM homework_grades g JOIN homeworks h ON g.homework_id = h.homework_id
            WHERE g.student_id IN %s AND g.correct_answers IS NOT NULL
            ORDER BY h.homework_id DESC
        """
    else:
        sql = """
            SELECT g.student_id, q.quiz_id AS item_id, q.title AS "Quiz",
                   g.score AS "Score", q.max_score AS "Out Of",
                   g.percentage AS "Percentage", g.report AS "Report"
            FROM quiz_grades g JOIN quizzes q ON g.quiz_id = q.quiz_id
            WHERE g.student_id IN %s AND g.score IS NOT NULL
            ORDER BY q.quiz_id DESC
        """
    return _db.fetch_dataframe(sql, (tuple(student_ids),))


def merge_grades(students_df, grades_df):
    out = students_df.copy()
    if grades_df.empty:
        out["score"] = float("nan")
        out["percentage"] = float("nan")
        out["report"] = None
        out["submitted"] = False
        return out
    out["_sid"] = out["id"].astype(str)
    g = grades_df.copy()
    g["_sid"] = g["student_id"].astype(str)
    g = g.drop(columns=["student_id"])
    merged = out.merge(g, on="_sid", how="left").drop(columns="_sid")
    merged["submitted"] = merged["submitted"].fillna(False).astype(bool)
    return merged


def format_submission_status(score, max_score, submitted=False, is_locked=False):
    if pd.notna(score):
        base = f"✅ {fmt(score)} / {fmt(max_score)}"
        return f"{base} (⏳ Editing...)" if is_locked else base
    if is_locked:
        return "⏳ Being Graded..."
    if bool(submitted):
        return "📩 Submitted"
    return "❌ Missing"


def fetch_report_image(db, kind, item_id, student_id):
    cfg = KINDS[kind]
    df = db.fetch_dataframe(
        f"SELECT report_image FROM {cfg['table']} WHERE {cfg['item_col']} = %s AND student_id = %s",
        (item_id, str(student_id)),
    )
    if df.empty:
        return None
    v = df.iloc[0, 0]
    if v is None or isinstance(v, float):
        return None
    return bytes(v) or None


@st.cache_data(show_spinner=False, ttl=600, max_entries=200)
def build_report_pdf(_db, kind, item_id, student_id, student_name, title,
                     score, out_of, pct, report, video_link):
    img = fetch_report_image(_db, kind, item_id, student_id)
    extra = {"video_link": video_link} if video_link else {}
    buf = PDFGenerator.generate_student_report(
        student_name, title, score, out_of, pct, report, img, **extra
    )
    return to_bytes(buf)


@st.cache_data(show_spinner=False, ttl=600)
def get_cached_master_report(title, group, total, pdf_data):
    return to_bytes(PDFGenerator.generate_master_report(f"{title} ({group})", total, pdf_data))


def mark_homework_submitted(db, homework_id: int, student_id: str):
    """Marks a homework as submitted in the database when a student uploads their PDF."""
    sql = """
        INSERT INTO homework_grades (homework_id, student_id, submitted)
        VALUES (%s, %s, TRUE)
        ON CONFLICT (homework_id, student_id) DO UPDATE SET submitted = TRUE
    """
    with db.get_connection() as conn:
        with conn.cursor() as c:
            c.execute(sql, (int(homework_id), str(student_id)))
        conn.commit()
    st.cache_data.clear()


def save_grade(db, kind, item_id, student_id, score, perc, report, img_bytes):
    cfg = KINDS[kind]
    t, ic, sc = cfg["table"], cfg["item_col"], cfg["score_col"]
    cols = [ic, "student_id", sc, "percentage", "report"]
    vals = [int(item_id), str(student_id), score, perc, report]
    updates = [f"{sc} = EXCLUDED.{sc}", "percentage = EXCLUDED.percentage", "report = EXCLUDED.report"]
    if kind == "homework":
        cols.append("submitted")
        vals.append(True)
        updates.append("submitted = TRUE")
    if img_bytes:
        cols.append("report_image")
        vals.append(psycopg2.Binary(img_bytes))
        updates.append("report_image = EXCLUDED.report_image")
    sql = (
        f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(vals))}) "
        f"ON CONFLICT ({ic}, student_id) DO UPDATE SET {', '.join(updates)}"
    )
    with db.get_connection() as conn:
        with conn.cursor() as c:
            c.execute(sql, tuple(vals))
        conn.commit()
    st.cache_data.clear()
    # Automatically send the grade + PDF report card to the parent on WhatsApp right after grading
    title_col_table = "homeworks" if kind == "homework" else "quizzes"
    id_col = "homework_id" if kind == "homework" else "quiz_id"
    max_col = "total_questions" if kind == "homework" else "max_score"
    item_df = db.fetch_dataframe(f"SELECT title, {max_col} FROM {title_col_table} WHERE {id_col} = %s", (int(item_id),))
    if not item_df.empty and score is not None:
        trigger_post_grade_whatsapp(
            db=db,
            kind=kind,
            item_id=int(item_id),
            item_title=str(item_df.iloc[0]["title"]),
            student_id=str(student_id),
            score=float(score),
            max_score=float(item_df.iloc[0][max_col]),
            perc=float(perc),
            report_text=report or "",
            img_bytes=img_bytes,
        )


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


# ---------------------------------------------------------------------------
# Fragments
# ---------------------------------------------------------------------------
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
            (r.id, r.name, clean(r.score),
             (float(r.score) / float(max_score)) * 100 if pd.notna(r.score) else None)
            for r in df.itertuples(index=False)
        ]
        pdf = get_cached_master_report(item_title, group_label, max_score, pdf_data)
        st.download_button(
            "⬇️ Download Master PDF Report", data=pdf,
            file_name=f"{item_title}_{group_label}_Master.pdf".replace(" ", "_"),
            mime="application/pdf",
        )


@st.fragment
def whatsapp_fragment(db, kind, item_id, title, total, vid_link, df):
    n = len(df)
    pages = max(1, math.ceil(n / PAGE_SIZE))
    page = 1
    if pages > 1:
        page = int(st.number_input(f"Page (1–{pages})", min_value=1, max_value=pages, value=1, step=1,
                                   key=f"wa_page_{kind}_{item_id}"))
    chunk = df.iloc[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

    for _, row in chunk.iterrows():
        sid = row["id"]
        col1, col2, col3, col4 = st.columns([3, 2, 2, 4])

        group_label = (f" *(Group: {row['group_number']})*"
                       if pd.notna(row["group_number"]) and str(row["group_number"]).strip() else "")
        col1.markdown(f"**{row['name']}**{group_label}")

        score = clean(row["score"])
        is_submitted = bool(row.get("submitted", False))
        has_score = score is not None
        if has_score:
            col2.write(f"Score: **{fmt(score)}** / {fmt(total)}")
        elif is_submitted:
            col2.markdown("Status: 📩 **Submitted**")
        else:
            col2.markdown("Score: ❌ **Missing**")

        with col3:
            if has_score:
                gen_key = f"gen_wa_{kind}_{sid}_{item_id}"
                if not st.session_state.get(gen_key):
                    st.button("📄 Prepare PDF", key=f"btn_{gen_key}", use_container_width=True,
                              on_click=set_flag, args=(gen_key,))
                else:
                    pdf = build_report_pdf(
                        db, kind, int(item_id), str(sid), row["name"], title,
                        score, clean(total), clean(row["percentage"]), clean(row["report"]), vid_link,
                    )
                    st.download_button(
                        label="⬇️ Download Ready", data=pdf,
                        file_name=f"{row['name']}_{title}.pdf".replace(" ", "_"),
                        mime="application/pdf", key=f"dl_{kind}_{item_id}_{sid}",
                        use_container_width=True,
                    )
            else:
                st.button("📄 N/A", disabled=True, key=f"dl_na_{kind}_{item_id}_{sid}", use_container_width=True)

        with col4:
            msg_parent, msg_student = build_wa_messages(kind, row["name"], title, score, total)
            parent_num = clean_number(row["phone_parent"])
            student_num = clean_number(row["phone"])
            sub1, sub2 = st.columns(2)
            with sub1:
                if parent_num:
                    st.link_button(
                        "👨‍👩‍👦 Parent",
                        f"https://api.whatsapp.com/send?phone={parent_num}&text={urllib.parse.quote(msg_parent)}",
                        key=f"wa_p_{kind}_{item_id}_{sid}", use_container_width=True)
                else:
                    st.button("👨‍👩‍👦 N/A", disabled=True, key=f"wa_p_na_{kind}_{item_id}_{sid}",
                              use_container_width=True)
            with sub2:
                if student_num:
                    st.link_button(
                        "🎓 Student",
                        f"https://api.whatsapp.com/send?phone={student_num}&text={urllib.parse.quote(msg_student)}",
                        key=f"wa_s_{kind}_{item_id}_{sid}", use_container_width=True)
                else:
                    st.button("🎓 N/A", disabled=True, key=f"wa_s_na_{kind}_{item_id}_{sid}",
                              use_container_width=True)
        st.divider()


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
                self._render_admin_login()
            else:
                self._render_public_login()
        elif st.session_state.role == "parent":
            self._render_parent_portal()
        elif st.session_state.role == "admin":
            self._render_admin_portal()

    def _write(self, ops):
        with self.db.get_connection() as conn:
            with conn.cursor() as c:
                for sql, params in ops:
                    c.execute(sql, params)
            conn.commit()
        st.cache_data.clear()

    def _render_public_login(self):
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            try:
                st.image("assets/images/logo.png", use_container_width=True)
            except Exception:
                pass
            st.title("Eng. Mahmoud Adel")
            st.caption("Student Portal & Homework Submission")

        tab_parent, tab_student = st.tabs(["👨‍👩‍👦 Parent Access", "📤 Student Submission"])

        with tab_parent:
            st.subheader("View Student Grades")
            parent_phone = st.text_input(
                "Parent Phone Number", placeholder="e.g. 01030007000", key="parent_phone_login"
            ).strip()
            if st.button("View Grades", type="primary", key="btn_parent_login") and parent_phone:
                self.auth.login_parent(self.db, parent_phone)

        with tab_student:
            st.subheader("📤 Submit Homework")
            st.caption(
                "1. Select your homework  •  2. Enter your Student ID & Parent Phone  •  "
                "3. Select your homework photos or PDF and tap Submit."
            )

            hw_df = load_homeworks(self.db)
            if hw_df.empty:
                st.info("No active homeworks available for submission.")
            else:
                hw_options = {row["homework_id"]: f"{row['title']}" for _, row in hw_df.iterrows()}
                selected_hw_id = st.selectbox(
                    "1️⃣ Select Homework *",
                    options=list(hw_options.keys()),
                    index=None,
                    placeholder="Choose the homework you want to submit...",
                    format_func=lambda x: hw_options[x],
                    key="student_hw_sel",
                )
                selected_hw_title = hw_options[selected_hw_id] if selected_hw_id is not None else ""

                col1, col2 = st.columns(2)
                student_id = col1.text_input(
                    "2️⃣ Enter your Student ID *",
                    placeholder="e.g. 176109",
                    key="student_id_input",
                ).strip()
                verify_phone = col2.text_input(
                    "3️⃣ Enter Parent Phone Number *",
                    placeholder="e.g. 010...",
                    key="student_verify_phone_input",
                ).strip()

                student_name = ""
                profile_complete = False
                is_verified = False

                def _is_missing(val):
                    if val is None or pd.isna(val):
                        return True
                    return str(val).strip().lower() in ("", "none", "nan", "null", "n/a", "non")

                def _last10(p_val):
                    digits = re.sub(r"\D", "", str(p_val or ""))
                    return digits[-10:] if len(digits) >= 10 else ""

                # Keep every message that depends on verification inside ONE container so the
                # uploader below always sits at the same position in the page. If the number of
                # elements above it changes (e.g. the "Verified" banner appears when the phone
                # field commits on blur as the student taps Upload), Streamlit re-mounts the
                # uploader and photos picked into the old, detached input are silently lost.
                with st.container():
                    if student_id and verify_phone:
                        all_stu_df = load_students(self.db)
                        match = all_stu_df[all_stu_df["id"].astype(str) == student_id]
                        if match.empty:
                            st.error("❌ Student ID not found. Please check your ID.")
                        else:
                            stu_row = match.iloc[0]
                            cur_name = "" if _is_missing(stu_row.get("name")) else str(stu_row["name"]).strip()
                            cur_phone = "" if _is_missing(stu_row.get("phone")) else str(stu_row["phone"]).strip()
                            cur_parent = "" if _is_missing(stu_row.get("phone_parent")) else str(stu_row["phone_parent"]).strip()
                            cur_group = "" if _is_missing(stu_row.get("group_number")) else str(stu_row["group_number"]).strip()

                            entered_10 = _last10(verify_phone)
                            db_parent_10 = _last10(cur_parent)
                            db_student_10 = _last10(cur_phone)

                            if not entered_10:
                                st.warning("Please enter a valid 11-digit phone number to verify your identity.")
                            elif db_parent_10 and entered_10 == db_parent_10:
                                is_verified = True
                            elif not db_parent_10 and db_student_10 and entered_10 == db_student_10:
                                is_verified = True
                            elif not db_parent_10 and not db_student_10:
                                is_verified = True
                            else:
                                st.error(
                                    "❌ Verification failed: The phone number entered does not match the Parent Phone Number on file for this ID."
                                )

                            if is_verified:
                                missing_fields = []
                                if not cur_name:
                                    missing_fields.append("Full Name")
                                if not cur_phone:
                                    missing_fields.append("Student Phone")
                                if not cur_parent:
                                    missing_fields.append("Parent Phone")
                                if not cur_group:
                                    missing_fields.append("Group Number")

                                if not missing_fields:
                                    student_name = cur_name
                                    profile_complete = True
                                    st.success(f"✅ Verified! Welcome, **{student_name}** (Group: {cur_group})")
                                else:
                                    st.warning(
                                        f"⚠️ Identity verified! Please complete your missing profile details (**{', '.join(missing_fields)}**) below first."
                                    )
                                    with st.container(border=True):
                                        st.markdown("#### 📋 Complete Your Student Profile")
                                        with st.form(f"complete_profile_form_{student_id}"):
                                            c_p1, c_p2 = st.columns(2)
                                            with c_p1:
                                                in_name = st.text_input(
                                                    "Full Name *",
                                                    value=cur_name,
                                                    placeholder="Enter your full name",
                                                ).strip()
                                                in_group = st.text_input(
                                                    "Group Number *",
                                                    value=cur_group,
                                                    placeholder="e.g. 1, 2, Online...",
                                                ).strip()
                                            with c_p2:
                                                in_phone = st.text_input(
                                                    "Your Phone Number *",
                                                    value=cur_phone,
                                                    placeholder="010...",
                                                ).strip()
                                                in_parent = st.text_input(
                                                    "Parent Phone Number *",
                                                    value=cur_parent or verify_phone,
                                                    placeholder="010...",
                                                ).strip()

                                            if st.form_submit_button(
                                                "💾 Save Profile",
                                                type="primary",
                                                use_container_width=True,
                                            ):
                                                digits_stu = re.sub(r"\D", "", in_phone)
                                                digits_par = re.sub(r"\D", "", in_parent)
                                                if not (in_name and in_group and in_phone and in_parent):
                                                    st.error("All fields are required.")
                                                elif len(digits_stu) < 10 or len(digits_par) < 10:
                                                    st.error("Please enter valid phone numbers (at least 10 digits).")
                                                elif digits_stu[-10:] == digits_par[-10:]:
                                                    st.error("Parent Phone Number must be different from Student Phone Number.")
                                                else:
                                                    try:
                                                        self._write([(
                                                            "UPDATE students SET name = %s, phone = %s, phone_parent = %s, group_number = %s WHERE id = %s",
                                                            (in_name, in_phone, in_parent, in_group, student_id),
                                                        )])
                                                        st.success("✅ Profile saved!")
                                                        st.rerun()
                                                    except Exception as e:
                                                        st.error(f"Could not save profile: {e}")

                st.divider()

                st.markdown("#### 📄 4️⃣ Upload Homework Photos or PDF")
                st.caption(
                    "**How to upload multiple pages:** Take the photos with your phone camera first. "
                    "Then tap **Upload** and pick **up to 4 photos at a time**, in order. "
                    "Tap **+** to add the next photos, then tap Submit."
                )

                # No st.form here: inside a form the script never sees the selected files until
                # Submit, so it cannot show previews. Each pick reruns the script and the files
                # come back in the order they were added. MIME types (not extensions) are used so
                # phone photos whose names have no extension (some Android gallery pickers) are
                # accepted. HEIC/HEIF photos are image/* too; the server converts them.
                # Photos only: with PDF in the accept list, Android Chrome opens the full Gallery /
                # Photos app instead of its lightweight photo picker. While that app is open,
                # Android can kill the Chrome tab to free memory, and selecting several large
                # photos then reloads the page and loses them. PDFs get their own uploader below.
                # Don't add file extensions here: Streamlit then adds a non-image type to the
                # input's accept list and Chrome falls back to the Gallery app again.
                photo_files = st.file_uploader(
                    "Select Homework Photos",
                    type=["image/*"],
                    accept_multiple_files=True,
                    key="hw_upload_widget",
                    max_upload_size=MAX_PHOTO_MB,
                )
                pdf_files = st.file_uploader(
                    "Or upload a PDF instead",
                    type=["application/pdf"],
                    accept_multiple_files=True,
                    key="hw_pdf_widget",
                )
                # Streamlit caps the file list at a small scroll box (about 4 files on a phone),
                # so newly added photos land out of sight and it looks as if they weren't added.
                # Let the list grow to show every file. (Style-only st.html takes no space.)
                st.html(
                    "<style>"
                    ".st-key-hw_upload_widget [data-testid='stFileChips'],"
                    ".st-key-hw_upload_widget [data-testid='stFileChips'] > div"
                    "{max-height:none !important; overflow-y:visible !important;}"
                    "</style>"
                )
                uploaded_files = (photo_files or []) + (pdf_files or [])
                total_upload_mb = sum(f.size for f in uploaded_files) / (1024 * 1024)

                if uploaded_files:
                    st.caption(
                        f"📎 **{len(uploaded_files)} file(s) attached** ({total_upload_mb:.1f} MB of "
                        f"{MAX_TOTAL_UPLOAD_MB} MB), merged into one PDF in this order. "
                        "Tap **+** to add more, or **✕** next to a file to remove it."
                    )
                    if total_upload_mb > MAX_TOTAL_UPLOAD_MB:
                        st.error(
                            f"⚠️ Your files add up to {total_upload_mb:.0f} MB. The limit is "
                            f"{MAX_TOTAL_UPLOAD_MB} MB in total, so please remove some files."
                        )
                    st.image(
                        [upload_preview(f) for f in uploaded_files],
                        caption=[f"{i}. {f.name}" for i, f in enumerate(uploaded_files, 1)],
                        width=110,
                    )

                submit_hw_clicked = st.button(
                    "🚀 Submit Homework",
                    type="primary",
                    key="btn_submit_hw_main",
                    use_container_width=True,
                )

                if submit_hw_clicked:
                    if selected_hw_id is None:
                        st.error("⚠️ Please select the **Homework** from dropdown 1️⃣ at the top first.")
                    elif not student_id or not verify_phone:
                        st.error("⚠️ Please enter both your **Student ID** and **Parent Phone Number** above.")
                    elif not is_verified:
                        st.error("⚠️ Your Student ID and Parent Phone Number could not be verified.")
                    elif not profile_complete:
                        st.error("⚠️ Please complete and save your missing profile details in the box above first.")
                    elif not uploaded_files:
                        st.error("⚠️ Please attach your homework photos or PDF file before clicking Submit.")
                    elif total_upload_mb > MAX_TOTAL_UPLOAD_MB:
                        st.error(
                            f"⚠️ Your files are over the {MAX_TOTAL_UPLOAD_MB} MB total limit. "
                            "Please remove some files before submitting."
                        )
                    else:
                        with st.spinner("Converting pages to PDF & uploading to Google Drive..."):
                            try:
                                pdf_bytes = convert_uploads_to_pdf_bytes(uploaded_files)
                                base64_data = base64.b64encode(pdf_bytes).decode("utf-8")
                                file_name = build_drive_hw_filename(
                                    student_id, selected_hw_id, selected_hw_title
                                )

                                script_url = "https://script.google.com/macros/s/AKfycbyLOJdL4kViRmyqtjETxu6qjjVi98MaqymDvemG8mA6PPGGuhf9WR0dqDrh8sVa5VBTug/exec"
                                payload = {
                                    "filename": file_name,
                                    "subfolder": selected_hw_title,
                                    "mimeType": "application/pdf",
                                    "base64": base64_data,
                                }

                                response = requests.post(script_url, json=payload, timeout=90)
                                if response.status_code == 200:
                                    res_json = response.json()
                                    if res_json.get("status") == "success":
                                        mark_homework_submitted(self.db, selected_hw_id, student_id)
                                        st.success(
                                            f"✅ Homework **{selected_hw_title}** submitted successfully!"
                                        )
                                        st.balloons()
                                    else:
                                        st.error(
                                            f"Error from Google Drive: {res_json.get('message', 'Unknown error')}"
                                        )
                                else:
                                    st.error(
                                        f"Failed to connect to Google Drive. Status Code: {response.status_code}"
                                    )
                            except Exception as e:
                                st.error(f"An error occurred while processing your files: {e}")

    def _render_admin_login(self):
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            st.title("Teacher & Admin Portal")
            st.caption("Please log in to access the management dashboard.")

            with st.container(border=True):
                admin_num = st.text_input("Admin Number")
                admin_pass = st.text_input("Password", type="password")
                if st.button("Login Securely", type="primary", key="btn_admin_login", use_container_width=True):
                    self.auth.login_admin(admin_num, admin_pass)

    def _render_parent_portal(self):
        st.sidebar.button("🚪 Logout", on_click=self.auth.logout, type="primary", use_container_width=True)
        st.title("👤 Student Profile & Grades Overview")

        students_df = load_parent_students(self.db, st.session_state.identifier)
        if students_df.empty:
            st.warning("No students found linked to this phone number.")
            return

        ids = tuple(str(i) for i in students_df["id"])
        hw_all = load_parent_grades(self.db, "homework", ids)
        qz_all = load_parent_grades(self.db, "quiz", ids)

        def subset(df, sid):
            if df.empty:
                return df
            return df[df["student_id"].astype(str) == sid]

        for _, student in students_df.iterrows():
            sid = str(student["id"])
            parent_student_fragment(self.db, student, subset(hw_all, sid), subset(qz_all, sid))

    def _render_admin_portal(self):
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
            self.auth.logout()

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
            self._admin_manage_homeworks()
        elif menu == "📝 Manage Quizzes":
            self._admin_record_quizzes()
        elif menu == "👥 Manage Students":
            self._admin_manage_students()
        elif menu == "👤 Student Profiles":
            StudentProfileView(self.db).render()
        elif menu == "💬 WhatsApp Broadcast":
            self._admin_whatsapp_parents()
        elif menu == "🤖 Automated Messages":
            AutomatedMessagesView(self.db).render()
    def _grading_section(self, kind, item_id, item_title, max_score, saved_rubric=""):
        st.divider()
        st.subheader("🔍 Search & Grade Students")

        students_df = load_students(self.db)
        df = merge_grades(students_df, load_item_grades(self.db, kind, item_id))
        filtered, filter_group = filter_students(df, kind)

        if filtered.empty:
            st.warning("No students found matching your filters.")
            return
        grading_fragment(self.db, kind, item_id, item_title, max_score, filtered, filter_group, saved_rubric=saved_rubric)

    def _admin_manage_homeworks(self):
        st.subheader("📚 Manage Curriculum & Homeworks")
        tab_hw, tab_ch = st.tabs(["📝 Homeworks", "📑 Manage Chapters"])
        ch_df = load_chapters(self.db)

        with tab_ch:
            with st.form("add_chapter_form"):
                title = st.text_input("Chapter Title (e.g., 'Unit 1: Integration')").strip()
                if st.form_submit_button("➕ Add Chapter", type="primary") and title:
                    try:
                        self._write([("INSERT INTO chapters (title) VALUES (%s)", (title,))])
                        st.session_state["flash"] = f"Chapter '{title}' added!"
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error: {e}")

            if not ch_df.empty:
                st.divider()
                st.dataframe(ch_df, hide_index=True, use_container_width=True)
                with st.expander("⚠️ Delete a Chapter"):
                    ch_titles = dict(zip(ch_df["chapter_id"], ch_df["title"]))
                    del_id = st.selectbox("Select Chapter to Delete", list(ch_titles.keys()),
                                          format_func=lambda i: ch_titles[i], key="del_ch_hw")
                    if st.button("🚨 Delete Chapter", key="btn_del_ch_hw"):
                        self._write([("DELETE FROM chapters WHERE chapter_id = %s", (int(del_id),))])
                        st.rerun()

        with tab_hw:
            if ch_df.empty:
                st.warning("Please create at least one Chapter in the 'Manage Chapters' tab first.")
                return

            hw_df = load_homeworks(self.db)
            ch_titles = dict(zip(ch_df["chapter_id"], ch_df["title"]))

            with st.expander("🛠️ Add or Delete Homework Assignments", expanded=False):
                with st.form("add_homework_form"):
                    ch_id = st.selectbox("Assign to Chapter", list(ch_titles.keys()),
                                         format_func=lambda i: f"{ch_titles[i]} (ID: {i})")
                    title = st.text_input("Homework Title")
                    total_q = st.number_input("Total Questions (Max Score)", min_value=1, step=1)
                    video_link = st.text_input("Homework Video Link (Optional)", placeholder="https://youtube.com/...")
                    rubric = st.text_area(
                        "AI Grading Answer Key / Rubric (Optional)",
                        placeholder="Paste the model answers here so AI can auto-grade with one click (e.g., Q1: B (-1), Q2: C (4), Q3: dy/dx = 2x + 1)...",
                        height=110,
                    )

                    if st.form_submit_button("➕ Add Homework", type="primary") and title.strip():
                        try:
                            clean_link = video_link.strip() or None
                            clean_rubric = rubric.strip() or None
                            self._write([(
                                "INSERT INTO homeworks (title, total_questions, video_link, chapter_id, rubric) VALUES (%s, %s, %s, %s, %s)",
                                (title.strip(), int(total_q), clean_link, int(ch_id), clean_rubric),
                            )])
                            st.session_state["flash"] = f"Homework '{title.strip()}' created!"
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error: {e}")

                if not hw_df.empty:
                    st.divider()
                    hw_titles = dict(zip(hw_df["homework_id"], hw_df["title"]))
                    del_id = st.selectbox("Select Homework to Delete", list(hw_titles.keys()),
                                          format_func=lambda i: hw_titles[i], key="del_hw_sel")
                    if st.button("🚨 Delete Selected Homework", key="btn_del_hw"):
                        self._write([
                            ("DELETE FROM homework_grades WHERE homework_id = %s", (int(del_id),)),
                            ("DELETE FROM homeworks WHERE homework_id = %s", (int(del_id),)),
                        ])
                        st.rerun()

            if hw_df.empty:
                st.info("No homeworks created yet.")
                return

            st.divider()
            st.subheader("📊 Select Assignment to Grade")
            hw_titles = dict(zip(hw_df["homework_id"], hw_df["title"]))
            hw_id = int(st.selectbox("Current Homework", list(hw_titles.keys()),
                                     format_func=lambda i: hw_titles[i], key="sel_hw_grade"))
            hw_row = hw_df[hw_df["homework_id"] == hw_id].iloc[0]
            saved_rubric = str(hw_row["rubric"]).strip() if "rubric" in hw_row and pd.notna(hw_row["rubric"]) else ""

            with st.expander("📋 View or Update Answer Key / Rubric for this Homework", expanded=False):
                with st.form(f"edit_rubric_form_{hw_id}"):
                    updated_rubric = st.text_area(
                        "Answer Key / Rubric used for 1-Click AI Grading",
                        value=saved_rubric,
                        placeholder="e.g., Q1: B (-1), Q2: C (4), Q3: dy/dx = 2x + 1...",
                        height=100,
                    )
                    if st.form_submit_button("💾 Save Answer Key", type="primary"):
                        self._write([
                            ("UPDATE homeworks SET rubric = %s WHERE homework_id = %s", (updated_rubric.strip() or None, hw_id))
                        ])
                        st.session_state["flash"] = "Answer Key updated!"
                        st.rerun()

            self._grading_section(
                "homework",
                hw_id,
                hw_row["title"],
                int(hw_row["total_questions"]),
                saved_rubric=saved_rubric,
            )

    def _admin_record_quizzes(self):
        st.subheader("📝 Manage Curriculum & Quizzes")

        ch_df = load_chapters(self.db)
        if ch_df.empty:
            st.warning("Please create at least one Chapter in '📚 Manage Homeworks' -> 'Manage Chapters' tab first.")
            return
        ch_titles = dict(zip(ch_df["chapter_id"], ch_df["title"]))
        qz_df = load_quizzes(self.db)

        with st.expander("🛠️ Add or Delete Quizzes", expanded=False):
            with st.form("create_quiz_form"):
                ch_id = st.selectbox("Assign to Chapter", list(ch_titles.keys()),
                                     format_func=lambda i: f"{ch_titles[i]} (ID: {i})")
                q_title = st.text_input("Quiz Title").strip()
                q_max_in = st.number_input("Maximum Score", min_value=1.0, value=10.0, step=1.0)
                if st.form_submit_button("➕ Create Quiz", type="primary") and q_title:
                    try:
                        self._write([(
                            """
                            WITH q AS (
                                INSERT INTO quizzes (title, max_score, chapter_id)
                                VALUES (%s, %s, %s) RETURNING quiz_id
                            )
                            INSERT INTO quiz_grades (quiz_id, student_id)
                            SELECT q.quiz_id, s.id FROM q CROSS JOIN students s
                            """,
                            (q_title, q_max_in, int(ch_id)),
                        )])
                        st.rerun()
                    except psycopg2.IntegrityError:
                        st.error("Quiz already exists.")

            if not qz_df.empty:
                st.divider()
                qz_titles = dict(zip(qz_df["quiz_id"], qz_df["title"]))
                del_id = st.selectbox("Select Quiz to Delete", list(qz_titles.keys()),
                                      format_func=lambda i: qz_titles[i], key="del_qz_sel")
                if st.button("🚨 Delete Selected Quiz", key="btn_del_qz"):
                    self._write([
                        ("DELETE FROM quiz_grades WHERE quiz_id = %s", (int(del_id),)),
                        ("DELETE FROM quizzes WHERE quiz_id = %s", (int(del_id),)),
                    ])
                    st.rerun()

        if qz_df.empty:
            st.info("No quizzes created yet.")
            return

        st.divider()
        st.subheader("📊 Select Quiz to Grade")
        qz_titles = dict(zip(qz_df["quiz_id"], qz_df["title"]))
        q_id = int(st.selectbox("Current Quiz", list(qz_titles.keys()),
                                format_func=lambda i: qz_titles[i], key="sel_qz_grade"))
        q_row = qz_df[qz_df["quiz_id"] == q_id].iloc[0]
        self._grading_section("quiz", q_id, q_row["title"], float(q_row["max_score"]))

    def _admin_manage_students(self):
        st.subheader("👥 Manage Students")

        with st.expander("➕ Add New Student", expanded=False):
            with st.form("add_student_form"):
                col1, col2 = st.columns(2)
                with col1:
                    add_id = st.text_input("Student ID (Unique)")
                    add_name = st.text_input("Student Name")
                    add_group = st.text_input("Group Number (Optional)")
                with col2:
                    add_phone = st.text_input("Student Phone (Optional)", placeholder="010...")
                    add_parent_phone = st.text_input("Parent Phone", placeholder="010...")

                if st.form_submit_button("💾 Register Student", type="primary"):
                    if not add_id.strip() or not add_name.strip():
                        st.error("Student ID and Name are required.")
                    else:
                        try:
                            self._write([(
                                "INSERT INTO students (id, name, phone, phone_parent, group_number) VALUES (%s, %s, %s, %s, %s)",
                                (add_id.strip(), add_name.strip(), add_phone.strip(),
                                 add_parent_phone.strip(), add_group.strip()),
                            )])
                            st.session_state["flash"] = f"Student '{add_name}' added successfully!"
                            st.rerun()
                        except psycopg2.IntegrityError:
                            st.error(f"Error: A student with ID '{add_id}' already exists.")
                        except Exception as e:
                            st.error(f"Failed to add student. Error: {e}")

        st.divider()

        students_df = load_students(self.db)
        if students_df.empty:
            st.info("No students registered yet.")
            return

        st.caption(f"Current Enrolled Students ({len(students_df)})")
        st.dataframe(students_df, hide_index=True, use_container_width=True)

        st.divider()

        st.subheader("🛠️ Modify Student Records")
        search_term = st.text_input(
            "🔍 Search Student by Name or ID", placeholder="Start typing to filter the dropdowns below..."
        ).strip()

        filtered_df = students_df
        if search_term:
            mask = (
                students_df["name"].astype(str).str.contains(search_term, case=False, na=False, regex=False)
                | students_df["id"].astype(str).str.contains(search_term, case=False, na=False, regex=False)
            )
            filtered_df = students_df[mask]

        if filtered_df.empty:
            st.warning("No students match your search criteria.")
            return

        labels = {str(r.id): f"{r.name} (ID: {r.id})" for r in filtered_df.itertuples(index=False)}
        all_ids = set(students_df["id"].astype(str))

        with st.expander("✏️ Edit Student Data"):
            edit_id = st.selectbox("Select Student to Edit", list(labels.keys()),
                                   format_func=lambda i: labels[i], key="edit_student_sel_v2")
            if edit_id:
                student_row = students_df[students_df["id"].astype(str) == edit_id].iloc[0]

                with st.form(f"edit_student_form_{edit_id}"):
                    new_id = st.text_input("Student ID", value=str(student_row["id"]))
                    new_name = st.text_input("Student Name", value=str(student_row["name"]))
                    new_phone = st.text_input("Student Phone", value=str(student_row["phone"]) if pd.notna(student_row["phone"]) else "")
                    new_parent_phone = st.text_input("Parent Phone", value=str(student_row["phone_parent"]) if pd.notna(student_row["phone_parent"]) else "")
                    new_group = st.text_input("Group Number", value=str(student_row["group_number"]) if pd.notna(student_row["group_number"]) else "")

                    if st.form_submit_button("💾 Save Changes", type="primary"):
                        new_id = new_id.strip()
                        if not new_name.strip() or not new_id:
                            st.error("Student ID and Name cannot be empty.")
                        elif new_id != edit_id and new_id in all_ids:
                            st.error(f"The ID {new_id} is already assigned to another student.")
                        else:
                            try:
                                if new_id != edit_id:
                                    ops = [
                                        ("INSERT INTO students (id, name, phone, phone_parent, group_number) VALUES (%s, %s, %s, %s, %s)",
                                         (new_id, new_name, new_phone, new_parent_phone, new_group)),
                                        ("UPDATE homework_grades SET student_id = %s WHERE student_id = %s", (new_id, edit_id)),
                                        ("UPDATE quiz_grades SET student_id = %s WHERE student_id = %s", (new_id, edit_id)),
                                        ("DELETE FROM students WHERE id = %s", (edit_id,)),
                                    ]
                                else:
                                    ops = [(
                                        "UPDATE students SET name = %s, phone = %s, phone_parent = %s, group_number = %s WHERE id = %s",
                                        (new_name, new_phone, new_parent_phone, new_group, edit_id),
                                    )]
                                self._write(ops)
                                st.session_state["flash"] = "Student details updated successfully!"
                                st.rerun()
                            except psycopg2.IntegrityError:
                                st.error(f"The ID {new_id} is already assigned to another student.")
                            except Exception as e:
                                st.error(f"Failed to update student. Error: {e}")

        with st.expander("⚠️ Danger Zone: Delete Student"):
            st.warning("Deleting a student will permanently remove all their recorded homework and quiz grades. This action cannot be undone.")

            delete_id = st.selectbox("Select Student to Delete", list(labels.keys()),
                                     format_func=lambda i: labels[i], key="delete_student_sel_v2")

            if st.button("🚨 Yes, Permanently Delete Student"):
                try:
                    self._write([
                        ("DELETE FROM homework_grades WHERE student_id = %s", (delete_id,)),
                        ("DELETE FROM quiz_grades WHERE student_id = %s", (delete_id,)),
                        ("DELETE FROM students WHERE id = %s", (delete_id,)),
                    ])
                    st.session_state["flash"] = "Student successfully deleted!"
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to delete student. Error: {e}")

    def _admin_whatsapp_parents(self):
        st.subheader("💬 WhatsApp & Report Broadcasting")
        st.caption("Bulk download PDFs and message parents or students directly for graded or missing assignments.")

        type_choice = st.radio("Select Assignment Type", ["Homework", "Quiz"], horizontal=True)
        kind = "homework" if type_choice == "Homework" else "quiz"

        ch_df = load_chapters(self.db)
        if ch_df.empty:
            st.warning("No chapters exist yet. Please create a chapter in '📚 Manage Homeworks' first.")
            return

        ch_titles = dict(zip(ch_df["chapter_id"], ch_df["title"]))
        ch_id = st.selectbox("📂 Filter by Chapter", list(ch_titles.keys()), format_func=lambda i: ch_titles[i])

        if kind == "homework":
            items = load_homeworks(self.db)
            items = items[items["chapter_id"] == ch_id]
            if items.empty:
                st.info(f"No homeworks found in chapter '{ch_titles[ch_id]}'.")
                return
            titles = dict(zip(items["homework_id"], items["title"]))
            item_id = int(st.selectbox("Select Homework", list(titles.keys()), format_func=lambda i: titles[i]))
            row = items[items["homework_id"] == item_id].iloc[0]
            total = int(row["total_questions"])
            vid = row["video_link"] if pd.notna(row["video_link"]) and str(row["video_link"]).strip() else None
        else:
            items = load_quizzes(self.db)
            items = items[items["chapter_id"] == ch_id]
            if items.empty:
                st.info(f"No quizzes found in chapter '{ch_titles[ch_id]}'.")
                return
            titles = dict(zip(items["quiz_id"], items["title"]))
            item_id = int(st.selectbox("Select Quiz", list(titles.keys()), format_func=lambda i: titles[i]))
            row = items[items["quiz_id"] == item_id].iloc[0]
            total = float(row["max_score"])
            vid = None
        title = row["title"]

        students_df = load_students(self.db)
        if students_df.empty:
            st.warning("No students found in the database.")
            return

        df = merge_grades(students_df, load_item_grades(self.db, kind, item_id))
        df = (df.assign(_g=df["group_number"].fillna("").astype(str))
                .sort_values(["_g", "name"]).drop(columns="_g"))

        groups = sorted({str(g).strip() for g in df["group_number"].dropna().unique() if str(g).strip()})
        if groups:
            filter_group = st.selectbox("Filter Broadcasting by Group", ["All Groups"] + groups)
            if filter_group != "All Groups":
                df = df[df["group_number"].astype(str).str.strip() == filter_group]

        st.success(f"Found {len(df)} students in this selection.")
        st.divider()

        whatsapp_fragment(self.db, kind, item_id, title, total, vid, df)


if __name__ == "__main__":
    app = GradePortalApp()
    app.run()
