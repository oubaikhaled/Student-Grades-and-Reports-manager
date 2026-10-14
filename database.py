import os
import warnings
import pandas as pd
import psycopg2
from psycopg2 import pool
from contextlib import contextmanager
import streamlit as st

# Suppress pandas SQLAlchemy warning for raw psycopg2 connections
warnings.filterwarnings("ignore", category=UserWarning, module="pandas")

EXCEL_FILE = "students-list.xlsx"


class DatabaseManager:
    def __init__(self, db_url):
        self.db_url = db_url
        self._pool = None
        self._init_pool()

    def _init_pool(self):
        try:
            if self._pool is not None:
                try:
                    self._pool.closeall()
                except Exception:
                    pass
            self._pool = pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=10,
                dsn=self.db_url,
                keepalives=1,
                keepalives_idle=20,
                keepalives_interval=5,
                keepalives_count=3,
            )
        except Exception:
            self._pool = None

    def _get_healthy_conn(self):
        """Retrieves a connection from the pool and verifies the SSL socket is actually alive."""
        if self._pool is not None:
            try:
                conn = self._pool.getconn()
                if conn.closed != 0:
                    self._pool.putconn(conn, close=True)
                    return psycopg2.connect(self.db_url), False

                # Ping the server to verify the SSL connection wasn't dropped by Neon/cloud proxy
                with conn.cursor() as c:
                    c.execute("SELECT 1")
                return conn, True
            except (psycopg2.OperationalError, psycopg2.InterfaceError, psycopg2.DatabaseError):
                try:
                    self._pool.putconn(conn, close=True)
                except Exception:
                    pass
                # Re-initialize pool and return a fresh direct connection
                self._init_pool()
                return psycopg2.connect(self.db_url), False
            except Exception:
                return psycopg2.connect(self.db_url), False

        return psycopg2.connect(self.db_url), False

    @contextmanager
    def get_connection(self):
        conn, from_pool = self._get_healthy_conn()
        discard = False
        try:
            yield conn
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            discard = True
            raise
        except Exception:
            try:
                if conn and conn.closed == 0:
                    conn.rollback()
            except Exception:
                discard = True
            raise
        finally:
            if conn is not None:
                if from_pool and self._pool is not None:
                    try:
                        self._pool.putconn(conn, close=(discard or conn.closed != 0))
                    except Exception:
                        try:
                            conn.close()
                        except Exception:
                            pass
                else:
                    try:
                        conn.close()
                    except Exception:
                        pass

    def fetch_dataframe(self, query, params=None):
        for attempt in range(2):
            try:
                with self.get_connection() as conn:
                    return pd.read_sql_query(query, conn, params=params)
            except (psycopg2.OperationalError, psycopg2.InterfaceError, pd.errors.DatabaseError):
                if attempt == 0:
                    self._init_pool()
                    continue
                raise

    def execute_query(self, query, params=None, fetch=False):
        for attempt in range(2):
            try:
                with self.get_connection() as conn:
                    with conn.cursor() as c:
                        c.execute(query, params)
                        result = c.fetchall() if fetch else None
                        conn.commit()
                        return result
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                if attempt == 0:
                    self._init_pool()
                    continue
                raise

    def execute_scalar(self, query, params=None):
        for attempt in range(2):
            try:
                with self.get_connection() as conn:
                    with conn.cursor() as c:
                        c.execute(query, params)
                        result = c.fetchone()
                        conn.commit()
                        return result[0] if result else None
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                if attempt == 0:
                    self._init_pool()
                    continue
                raise

    def init_db(self):
        with self.get_connection() as conn:
            with conn.cursor() as c:
                c.execute(
                    "CREATE TABLE IF NOT EXISTS students ("
                    "id TEXT PRIMARY KEY, name TEXT, phone TEXT, phone_parent TEXT, "
                    "region TEXT, group_number TEXT, password_hash TEXT, notes TEXT)"
                )
                c.execute(
                    "CREATE TABLE IF NOT EXISTS chapters ("
                    "chapter_id SERIAL PRIMARY KEY, title TEXT UNIQUE, "
                    "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
                )
                c.execute(
                    "CREATE TABLE IF NOT EXISTS lessons ("
                    "lesson_id SERIAL PRIMARY KEY, title TEXT, description TEXT, "
                    "video_url TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
                )
                c.execute(
                    "CREATE TABLE IF NOT EXISTS homeworks ("
                    "homework_id SERIAL PRIMARY KEY, lesson_id INTEGER, chapter_id INTEGER, "
                    "title TEXT UNIQUE, total_questions INTEGER, video_link TEXT, rubric TEXT, "
                    "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
                )
                c.execute(
                    "CREATE TABLE IF NOT EXISTS homework_grades ("
                    "grade_id SERIAL PRIMARY KEY, homework_id INTEGER, student_id TEXT, "
                    "correct_answers INTEGER, percentage REAL, report TEXT, report_image BYTEA, "
                    "submitted BOOLEAN DEFAULT FALSE, "
                    "UNIQUE (homework_id, student_id))"
                )
                c.execute(
                    "CREATE TABLE IF NOT EXISTS quizzes ("
                    "quiz_id SERIAL PRIMARY KEY, lesson_id INTEGER, chapter_id INTEGER, "
                    "title TEXT UNIQUE, max_score REAL DEFAULT 10.0)"
                )
                c.execute(
                    "CREATE TABLE IF NOT EXISTS quiz_grades ("
                    "grade_id SERIAL PRIMARY KEY, quiz_id INTEGER, student_id TEXT, "
                    "score REAL, percentage REAL, report TEXT, report_image BYTEA, "
                    "UNIQUE (quiz_id, student_id))"
                )

                try:
                    c.execute("ALTER TABLE homework_grades ADD COLUMN IF NOT EXISTS report TEXT;")
                    c.execute("ALTER TABLE homework_grades ADD COLUMN IF NOT EXISTS report_image BYTEA;")
                    c.execute("ALTER TABLE homework_grades ADD COLUMN IF NOT EXISTS submitted BOOLEAN DEFAULT FALSE;")
                    c.execute("ALTER TABLE quiz_grades ADD COLUMN IF NOT EXISTS report TEXT;")
                    c.execute("ALTER TABLE quiz_grades ADD COLUMN IF NOT EXISTS report_image BYTEA;")
                    c.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS group_number TEXT;")
                    c.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS password_hash TEXT;")
                    c.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS notes TEXT;")
                    c.execute("ALTER TABLE homeworks ADD COLUMN IF NOT EXISTS lesson_id INTEGER;")
                    c.execute("ALTER TABLE homeworks ADD COLUMN IF NOT EXISTS chapter_id INTEGER;")
                    c.execute("ALTER TABLE homeworks ADD COLUMN IF NOT EXISTS video_link TEXT;")
                    c.execute("ALTER TABLE homeworks ADD COLUMN IF NOT EXISTS rubric TEXT;")
                    c.execute("ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS lesson_id INTEGER;")
                    c.execute("ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS chapter_id INTEGER;")
                except Exception:
                    conn.rollback()

                c.execute("SELECT COUNT(*) FROM students")
                if c.fetchone()[0] == 0 and os.path.exists(EXCEL_FILE):
                    try:
                        df_data = pd.read_excel(EXCEL_FILE, sheet_name="Data")
                        for _, row in df_data.iterrows():
                            c.execute(
                                "INSERT INTO students (id, name, phone, phone_parent, region, group_number) "
                                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                                (
                                    str(row["id"]),
                                    str(row["name"]),
                                    str(row.get("phone", "")),
                                    str(row.get("phone_parent", "")),
                                    str(row.get("region", "")),
                                    str(row.get("group_number", "")),
                                ),
                            )
                    except Exception as e:
                        st.error(f"Error seeding data from Excel: {e}")
                conn.commit()
