import psycopg2

from portal.config import FULL_MARK_CONGRATS, KINDS
from portal.data import invalidate_data_caches
from whatsapp_automation import trigger_post_grade_whatsapp


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
    invalidate_data_caches()


def apply_full_mark_congrats(report, perc):
    """Puts the congratulations line at the top of the report for a 100% score.

    Safe to call on every save: the line is never duplicated, and it is removed again
    if a grade that was 100% is later corrected to something lower.
    """
    text = str(report or "")
    is_full_mark = perc is not None and float(perc) >= 99.999
    if not is_full_mark and FULL_MARK_CONGRATS not in text:
        return report
    body = "\n".join(
        line for line in text.split("\n") if line.strip() != FULL_MARK_CONGRATS
    ).strip()
    if not is_full_mark:
        return body
    return f"{FULL_MARK_CONGRATS}\n\n{body}" if body else FULL_MARK_CONGRATS


def save_grade(db, kind, item_id, student_id, score, perc, report, img_bytes):
    report = apply_full_mark_congrats(report, perc)
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
    invalidate_data_caches()
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
