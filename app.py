import io
import math
import re
import urllib.parse

import emoji
import pandas as pd
import psycopg2
import streamlit as st

from auth import AuthManager
from database import DatabaseManager
from pdf_utils import PDFGenerator
from student_profile import StudentProfileView

# Requires streamlit >= 1.37 (st.fragment, dataframe row selection, st.rerun(scope=...))

# 1. Page Config MUST be the very first Streamlit command
st.set_page_config(page_title="Eng.Mahmoud Adel Grade Portal", layout="wide")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CACHE_TTL = 120          # seconds; every write also clears the cache immediately
PAGE_SIZE = 20           # rows per page on the WhatsApp screen
MAX_IMG_WIDTH = 1200     # uploaded feedback images are downscaled to this width

# Homework and quiz share the same logic; only names differ.
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
    """Merge uploaded images vertically. Downscales wide photos so the BLOB stays small."""
    if not uploaded_files:
        return None
    from PIL import Image as PILImage, ImageOps
    try:
        images = []
        for f in uploaded_files:
            im = ImageOps.exif_transpose(PILImage.open(f)).convert("RGB")
            if im.width > MAX_IMG_WIDTH:
                new_h = int(im.height * MAX_IMG_WIDTH / im.width)
                im = im.resize((MAX_IMG_WIDTH, new_h), PILImage.LANCZOS)
            images.append(im)
        max_width = max(i.width for i in images)
        total_height = sum(i.height for i in images)
        stitched = PILImage.new("RGB", (max_width, total_height), color=(255, 255, 255))
        y = 0
        for im in images:
            stitched.paste(im, (0, y))
            y += im.height
        out = io.BytesIO()
        stitched.save(out, format="JPEG", quality=80, optimize=True)
        return out.getvalue()
    except Exception as e:
        st.error(f"Image processing failed: {e}")
        return None


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
# Shared resources & cached reads
# (`_db` has a leading underscore so Streamlit doesn't try to hash it)
# Every write calls st.cache_data.clear(), so admins always see fresh data.
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def get_db():
    """Created once per server process instead of on every rerun (incl. init_db DDL)."""
    db = DatabaseManager(st.secrets["DATABASE_URL"])
    db.init_db()
    return db


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_chapters(_db):
    return _db.fetch_dataframe("SELECT chapter_id, title FROM chapters ORDER BY chapter_id ASC")


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_homeworks(_db):
    return _db.fetch_dataframe(
        "SELECT homework_id, title, total_questions, video_link, chapter_id FROM homeworks ORDER BY homework_id DESC"
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
    """Grades for ONE homework/quiz. No image column: images are fetched only when a PDF is built."""
    cfg = KINDS[kind]
    return _db.fetch_dataframe(
        f"SELECT student_id, {cfg['score_col']} AS score, percentage, report "
        f"FROM {cfg['table']} WHERE {cfg['item_col']} = %s",
        (item_id,),
    )


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_parent_students(_db, phone):
    return _db.fetch_dataframe("SELECT id, name FROM students WHERE phone_parent = %s", (phone,))


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_parent_grades(_db, kind, student_ids):
    """One query for ALL of a parent's children (no per-child N+1). `student_ids` is a tuple."""
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
    """Left-join students with one item's grades in pandas (students list is cached separately)."""
    out = students_df.copy()
    if grades_df.empty:
        out["score"] = float("nan")
        out["percentage"] = float("nan")
        out["report"] = None
        return out
    out["_sid"] = out["id"].astype(str)
    g = grades_df.copy()
    g["_sid"] = g["student_id"].astype(str)
    g = g.drop(columns=["student_id"])
    return out.merge(g, on="_sid", how="left").drop(columns="_sid")


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
    """Builds (and caches) one student's PDF. The image BLOB is loaded here, only on demand."""
    img = fetch_report_image(_db, kind, item_id, student_id)
    extra = {"video_link": video_link} if video_link else {}
    buf = PDFGenerator.generate_student_report(
        student_name, title, score, out_of, pct, report, img, **extra
    )
    return to_bytes(buf)


@st.cache_data(show_spinner=False, ttl=600)
def get_cached_master_report(title, group, total, pdf_data):
    return to_bytes(PDFGenerator.generate_master_report(f"{title} ({group})", total, pdf_data))


def save_grade(db, kind, item_id, student_id, score, perc, report, img_bytes):
    cfg = KINDS[kind]
    t, ic, sc = cfg["table"], cfg["item_col"], cfg["score_col"]
    cols = [ic, "student_id", sc, "percentage", "report"]
    vals = [int(item_id), str(student_id), score, perc, report]
    updates = [f"{sc} = EXCLUDED.{sc}", "percentage = EXCLUDED.percentage", "report = EXCLUDED.report"]
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


def filter_students(df, key_prefix):
    """Group + search filters. Vectorised, and regex=False so input like '(' can't crash."""
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
# Fragments: interactions inside these rerun ONLY the fragment, not the whole page
# ---------------------------------------------------------------------------
@st.fragment
def parent_student_fragment(db, sid, name, hw_df, qz_df):
    st.subheader(f"🎓 {name}")
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

    # Lazy PDF: nothing is generated until the parent asks for it
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
def grading_fragment(db, kind, item_id, item_title, max_score, df, group_label):
    """Roster + grading panel + master PDF for one homework/quiz."""
    cfg = KINDS[kind]
    ver_key = f"roster_ver_{kind}"
    st.session_state.setdefault(ver_key, 0)

    panel = st.container()  # grading panel is drawn here (above the roster) once a row is picked

    st.markdown("### 📋 Class Roster")
    st.caption("Click a row to grade that student.")
    view = pd.DataFrame({
        "ID": df["id"].astype(str).values,
        "Name": df["name"].values,
        "Group": df["group_number"].fillna("").astype(str).values,
        "Status": [f"✅ {fmt(s)} / {fmt(max_score)}" if pd.notna(s) else "❌ Missing" for s in df["score"]],
    })
    event = st.dataframe(
        view, hide_index=True, use_container_width=True,
        height=min(420, 38 + 35 * len(view)),
        on_select="rerun", selection_mode="single-row",
        key=f"roster_{kind}_{item_id}_{st.session_state[ver_key]}",
    )
    rows = event.selection.rows if event and event.selection else []

    if rows:
        stu = df.iloc[rows[0]]
        stu_id = stu["id"]
        with panel:
            st.markdown(f"### 📝 Grading Panel: {stu['name']} (ID: {stu_id})")
            with st.container(border=True):
                st.info(f"🎯 **Full Mark for this {cfg['noun']}:** {fmt(max_score)}")
                with st.form(f"grade_form_{kind}_{item_id}_{stu_id}"):
                    cur = stu["score"]
                    if cfg["is_int"]:
                        score = st.number_input(cfg["score_label"], min_value=0, max_value=int(max_score),
                                                value=int(cur) if pd.notna(cur) else 0, step=1)
                    else:
                        score = st.number_input(cfg["score_label"], min_value=0.0, max_value=float(max_score),
                                                value=float(cur) if pd.notna(cur) else 0.0, step=0.5)
                    report = st.text_area("Feedback Report",
                                          value=stu["report"] if pd.notna(stu["report"]) else "", height=120)
                    files = st.file_uploader(
                        "Upload Attachments (Multiple images will be merged vertically)",
                        type=["png", "jpg", "jpeg"], accept_multiple_files=True,
                    )
                    col_save, col_cancel = st.columns([3, 1])
                    save = col_save.form_submit_button("💾 Save Grade", type="primary")
                    cancel = col_cancel.form_submit_button("❌ Cancel")

            if save:
                img_bytes = stitch_images(files)
                perc = (float(score) / float(max_score)) * 100.0 if max_score > 0 else 0
                save_grade(db, kind, item_id, stu_id, score, perc, report, img_bytes)
                st.session_state[ver_key] += 1          # clears the row selection
                st.session_state["flash"] = f"Grade saved for {stu['name']}!"
                st.rerun()                               # full rerun so everything shows fresh data
            if cancel:
                st.session_state[ver_key] += 1
                st.rerun(scope="fragment")

    st.divider()

    # --- LAZY MASTER PDF ---
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
    """Paginated broadcast list. Only PAGE_SIZE rows are rendered / built per run."""
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
        has_score = score is not None
        if has_score:
            col2.write(f"Score: **{fmt(score)}** / {fmt(total)}")
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
        self.db = get_db()           # cached: no new connection manager / init_db per rerun
        self.auth = AuthManager()    # cheap, kept per-run in case it holds per-session state

    def run(self):
        if "logged_in" not in st.session_state or not st.session_state.logged_in:
            self._render_login()
        elif st.session_state.role == "parent":
            self._render_parent_portal()
        elif st.session_state.role == "admin":
            self._render_admin_portal()

    # ---- writes -----------------------------------------------------------
    def _write(self, ops):
        """Run several statements in ONE transaction, then invalidate cached reads."""
        with self.db.get_connection() as conn:
            with conn.cursor() as c:
                for sql, params in ops:
                    c.execute(sql, params)
            conn.commit()
        st.cache_data.clear()

    # ---- login ------------------------------------------------------------
    def _render_login(self):
        st.title("📚 Eng.Mahmoud Adel Grade Portal")
        tab_parent, tab_admin = st.tabs(["👨‍👩‍👧 Parent Access", "🔐 Admin Access"])

        with tab_parent:
            st.subheader("View Student Grades")
            parent_phone = st.text_input("Parent Phone Number", placeholder="e.g. 01030007000").strip()
            if st.button("View Grades", type="primary") and parent_phone:
                self.auth.login_parent(self.db, parent_phone)

        with tab_admin:
            st.subheader("Teacher & Admin Login")
            admin_num = st.text_input("Admin Number")
            admin_pass = st.text_input("Password", type="password")
            if st.button("Login as Admin", type="primary"):
                self.auth.login_admin(admin_num, admin_pass)

    # ---- parent -----------------------------------------------------------
    def _render_parent_portal(self):
        st.sidebar.button("🚪 Logout", on_click=self.auth.logout, type="primary")
        st.title("📊 Student Grades Overview")

        students_df = load_parent_students(self.db, st.session_state.identifier)
        if students_df.empty:
            return

        ids = tuple(str(i) for i in students_df["id"])
        hw_all = load_parent_grades(self.db, "homework", ids)   # 2 queries total, not 2 per child
        qz_all = load_parent_grades(self.db, "quiz", ids)

        def subset(df, sid):
            if df.empty:
                return df
            return df[df["student_id"].astype(str) == sid]

        for _, student in students_df.iterrows():
            sid = str(student["id"])
            parent_student_fragment(self.db, sid, student["name"], subset(hw_all, sid), subset(qz_all, sid))

    # ---- admin shell ------------------------------------------------------
    def _render_admin_portal(self):
        st.sidebar.button("🚪 Logout", on_click=self.auth.logout, type="primary")
        st.title("Eng.Mahmoud Adel Grade Portal")
        menu = st.sidebar.radio(
            "Navigation",
            ["Manage Homeworks", "Manage Quizzes", "Manage Students",
             "Student Profiles", "WhatsApp Students and parents"],
        )

        flash = st.session_state.pop("flash", None)
        if flash:
            st.success(flash)

        if menu == "Manage Homeworks":
            self._admin_manage_homeworks()
        elif menu == "Manage Quizzes":
            self._admin_record_quizzes()
        elif menu == "Manage Students":
            self._admin_manage_students()
        elif menu == "Student Profiles":
            StudentProfileView(self.db).render()
        elif menu == "WhatsApp Students and parents":
            self._admin_whatsapp_parents()

    # ---- shared grading section ------------------------------------------
    def _grading_section(self, kind, item_id, item_title, max_score):
        st.divider()
        st.subheader("🔍 Search & Grade Students")

        students_df = load_students(self.db)
        df = merge_grades(students_df, load_item_grades(self.db, kind, item_id))
        filtered, filter_group = filter_students(df, kind)

        if filtered.empty:
            st.warning("No students found matching your filters.")
            return
        grading_fragment(self.db, kind, item_id, item_title, max_score, filtered, filter_group)

    # ---- homeworks --------------------------------------------------------
    def _admin_manage_homeworks(self):
        st.subheader("📚 Manage Curriculum & Homeworks")
        tab_hw, tab_ch = st.tabs(["📝 Homeworks", "📑 Manage Chapters"])
        ch_df = load_chapters(self.db)

        # --- CHAPTER TAB ---
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

        # --- HOMEWORK TAB ---
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

                    if st.form_submit_button("➕ Add Homework", type="primary") and title.strip():
                        try:
                            clean_link = video_link.strip() or None
                            self._write([(
                                "INSERT INTO homeworks (title, total_questions, video_link, chapter_id) VALUES (%s, %s, %s, %s)",
                                (title.strip(), int(total_q), clean_link, int(ch_id)),
                            )])
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
            self._grading_section("homework", hw_id, hw_row["title"], int(hw_row["total_questions"]))

    # ---- quizzes ----------------------------------------------------------
    def _admin_record_quizzes(self):
        st.subheader("📝 Manage Curriculum & Quizzes")

        ch_df = load_chapters(self.db)
        if ch_df.empty:
            st.warning("Please create at least one Chapter in 'Manage Homeworks' -> 'Manage Chapters' tab first.")
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
                        # One statement: create the quiz AND a blank grade row for every student
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

    # ---- students ---------------------------------------------------------
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

        st.caption("Current Enrolled Students")
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

    # ---- whatsapp ---------------------------------------------------------
    def _admin_whatsapp_parents(self):
        st.subheader("💬 WhatsApp & Report Broadcasting")
        st.caption("Bulk download PDFs and message parents or students directly for graded or missing assignments.")

        type_choice = st.radio("Select Assignment Type", ["Homework", "Quiz"], horizontal=True)
        kind = "homework" if type_choice == "Homework" else "quiz"

        ch_df = load_chapters(self.db)
        if ch_df.empty:
            st.warning("No chapters exist yet. Please create a chapter in 'Manage Homeworks' first.")
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
