import streamlit as st

from database import DatabaseManager
from portal.config import CACHE_TTL, KINDS


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


def run_write(db, ops):
    """Runs (sql, params) statements in one transaction, then refreshes the cached reads."""
    with db.get_connection() as conn:
        with conn.cursor() as c:
            for sql, params in ops:
                c.execute(sql, params)
        conn.commit()
    invalidate_data_caches()


def invalidate_data_caches():
    """Clears every cached database read and report built from it after a write.

    Image thumbnails stay cached: they never come from the database.
    """
    from portal.reports import build_report_pdf, get_cached_master_report
    from student_profile import (
        build_cached_profile_pdf,
        load_all_students_with_notes,
        load_student_profile_grades,
    )

    for fn in (
        load_chapters, load_homeworks, load_quizzes, load_students, load_item_grades,
        load_parent_students, load_parent_grades, build_report_pdf, get_cached_master_report,
        load_all_students_with_notes, load_student_profile_grades, build_cached_profile_pdf,
    ):
        fn.clear()
