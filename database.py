import os
import pandas as pd
import psycopg2
from contextlib import contextmanager
import streamlit as st

EXCEL_FILE = "students-list.xlsx"

class DatabaseManager:
    def __init__(self, db_url):
        self.db_url = db_url

    @contextmanager
    def get_connection(self):
        conn = psycopg2.connect(self.db_url)
        try:
            yield conn
        finally:
            conn.close()

    def fetch_dataframe(self, query, params=None):
        with self.get_connection() as conn:
            return pd.read_sql_query(query, conn, params=params)

    def execute_query(self, query, params=None, fetch=False):
        with self.get_connection() as conn:
            with conn.cursor() as c:
                c.execute(query, params)
                result = c.fetchall() if fetch else None
                conn.commit()
                return result

    def execute_scalar(self, query, params=None):
        with self.get_connection() as conn:
            with conn.cursor() as c:
                c.execute(query, params)
                result = c.fetchone()
                conn.commit()
                return result[0] if result else None

    def init_db(self):
        with self.get_connection() as conn:
            with conn.cursor() as c:
                c.execute("CREATE TABLE IF NOT EXISTS students (id TEXT PRIMARY KEY, name TEXT, phone TEXT, phone_parent TEXT, region TEXT)")
                c.execute("CREATE TABLE IF NOT EXISTS homeworks (homework_id SERIAL PRIMARY KEY, title TEXT UNIQUE, total_questions INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
                c.execute("CREATE TABLE IF NOT EXISTS homework_grades (grade_id SERIAL PRIMARY KEY, homework_id INTEGER, student_id TEXT, correct_answers INTEGER, percentage REAL, report TEXT, report_image BYTEA, FOREIGN KEY (homework_id) REFERENCES homeworks (homework_id), FOREIGN KEY (student_id) REFERENCES students (id), UNIQUE (homework_id, student_id))")
                c.execute("CREATE TABLE IF NOT EXISTS quizzes (quiz_id SERIAL PRIMARY KEY, title TEXT UNIQUE, max_score REAL DEFAULT 10.0)")
                c.execute("CREATE TABLE IF NOT EXISTS quiz_grades (grade_id SERIAL PRIMARY KEY, quiz_id INTEGER, student_id TEXT, score REAL, percentage REAL, report TEXT, report_image BYTEA, FOREIGN KEY (quiz_id) REFERENCES quizzes (quiz_id), FOREIGN KEY (student_id) REFERENCES students (id), UNIQUE (quiz_id, student_id))")

                # Auto-Migration for missing columns
                try:
                    c.execute("ALTER TABLE homework_grades ADD COLUMN IF NOT EXISTS report TEXT;")
                    c.execute("ALTER TABLE homework_grades ADD COLUMN IF NOT EXISTS report_image BYTEA;")
                    c.execute("ALTER TABLE quiz_grades ADD COLUMN IF NOT EXISTS report TEXT;")
                    c.execute("ALTER TABLE quiz_grades ADD COLUMN IF NOT EXISTS report_image BYTEA;")
                except Exception:
                    conn.rollback()

                c.execute("SELECT COUNT(*) FROM students")
                if c.fetchone()[0] == 0 and os.path.exists(EXCEL_FILE):
                    try:
                        df_data = pd.read_excel(EXCEL_FILE, sheet_name="Data")
                        for _, row in df_data.iterrows():
                            c.execute("INSERT INTO students (id, name, phone, phone_parent, region) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING", (str(row["id"]), str(row["name"]), str(row.get("phone", "")), str(row.get("phone_parent", "")), str(row.get("region", ""))))
                    except Exception as e:
                        st.error(f"Error seeding data from Excel: {e}")
                conn.commit()