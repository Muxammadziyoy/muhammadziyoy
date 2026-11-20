"""wMaktab - SQLite database layer.

Holds the schema, connection helpers and the demo-data seeder.

    python database.py            # create tables (and demo data if the database is empty)
    python database.py --reset    # delete the database and rebuild it with demo data

All demo people are fictional. Nothing here comes from a real school or
from eMaktab.
"""
import os
import random
import sqlite3
import sys
from datetime import date, datetime, timedelta

from flask import g
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("WMAKTAB_DB", os.path.join(BASE_DIR, "instance", "wmaktab.db"))

LESSON_TIMES = {
    1: "08:00 - 08:45",
    2: "08:55 - 09:40",
    3: "09:50 - 10:35",
    4: "10:55 - 11:40",
    5: "11:50 - 12:35",
    6: "12:45 - 13:30",
}
MAX_LESSONS = 6
GRADE_KINDS = ["Classwork", "Homework", "Quiz", "Test", "Exam"]
ATTENDANCE_STATUSES = ("present", "late", "excused", "absent")

SCHEMA = """
CREATE TABLE IF NOT EXISTS classes (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS subjects (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('student', 'teacher', 'admin')),
    full_name     TEXT NOT NULL,
    email         TEXT NOT NULL DEFAULT '',
    phone         TEXT NOT NULL DEFAULT '',
    address       TEXT NOT NULL DEFAULT '',
    birth_date    TEXT NOT NULL DEFAULT '',
    class_id      INTEGER REFERENCES classes(id) ON DELETE SET NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Which teacher teaches which subject in which class.
CREATE TABLE IF NOT EXISTS teaching (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    teacher_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    class_id   INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    UNIQUE (class_id, subject_id)
);

CREATE TABLE IF NOT EXISTS grades (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    teacher_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    value      INTEGER NOT NULL CHECK (value BETWEEN 1 AND 5),
    quarter    INTEGER NOT NULL CHECK (quarter BETWEEN 1 AND 4),
    kind       TEXT NOT NULL DEFAULT 'Classwork',
    comment    TEXT NOT NULL DEFAULT '',
    date       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_grades_student ON grades (student_id, subject_id, quarter);
CREATE INDEX IF NOT EXISTS idx_grades_subject ON grades (subject_id, date);

CREATE TABLE IF NOT EXISTS attendance (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date       TEXT NOT NULL,
    status     TEXT NOT NULL CHECK (status IN ('present', 'late', 'excused', 'absent')),
    note       TEXT NOT NULL DEFAULT '',
    marked_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    UNIQUE (student_id, date)
);
CREATE INDEX IF NOT EXISTS idx_attendance_date ON attendance (date);

CREATE TABLE IF NOT EXISTS timetable (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    class_id   INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    weekday    INTEGER NOT NULL CHECK (weekday BETWEEN 1 AND 5),
    lesson_no  INTEGER NOT NULL CHECK (lesson_no BETWEEN 1 AND 6),
    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    teacher_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    room       TEXT NOT NULL DEFAULT '',
    UNIQUE (class_id, weekday, lesson_no)
);

CREATE TABLE IF NOT EXISTS homework (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    class_id    INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    subject_id  INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    teacher_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    due_date    TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS homework_status (
    homework_id INTEGER NOT NULL REFERENCES homework(id) ON DELETE CASCADE,
    student_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    done        INTEGER NOT NULL DEFAULT 0,
    done_at     TEXT,
    PRIMARY KEY (homework_id, student_id)
);

CREATE TABLE IF NOT EXISTS announcements (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    audience   TEXT NOT NULL DEFAULT 'all' CHECK (audience IN ('all', 'students', 'teachers')),
    pinned     INTEGER NOT NULL DEFAULT 0,
    author_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


# --------------------------------------------------------------------------
# School calendar helpers
# --------------------------------------------------------------------------
def quarter_for(d):
    """School quarter for a date: Sep-Oct = 1, Nov-Dec = 2, Jan-Mar = 3, Apr-Aug = 4."""
    if d.month in (9, 10):
        return 1
    if d.month in (11, 12):
        return 2
    if d.month in (1, 2, 3):
        return 3
    return 4


def school_year_start(today=None):
    today = today or date.today()
    year = today.year if today.month >= 9 else today.year - 1
    return date(year, 9, 1)


def school_year_label(today=None):
    start = school_year_start(today)
    return f"{start.year}-{start.year + 1}"


# --------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------
def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get_db():
    """One connection per request (stored on flask.g)."""
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def query_all(sql, args=()):
    return [dict(row) for row in get_db().execute(sql, args).fetchall()]


def query_one(sql, args=()):
    row = get_db().execute(sql, args).fetchone()
    return dict(row) if row else None


def execute(sql, args=()):
    """Run a write statement, commit, and return the new row id."""
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur.lastrowid


# --------------------------------------------------------------------------
# Initialisation
# --------------------------------------------------------------------------
def init_db():
    """Create tables and seed demo data when the database is empty."""
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            seed_demo(conn)
    finally:
        conn.close()


def reset_db():
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(DB_PATH + suffix)
        except FileNotFoundError:
            pass
    init_db()


# --------------------------------------------------------------------------
# Demo data (all fictional)
# --------------------------------------------------------------------------
CLASSES = ["9-A", "9-B", "10-A"]

# (subject, lessons per week). The quotas add up to 25 = 5 days x 5 lessons.
SUBJECTS = [
    ("Mathematics", 4),
    ("Native Language", 4),
    ("English", 4),
    ("Physics", 2),
    ("History", 3),
    ("Informatics", 3),
    ("Biology", 3),
    ("Chemistry", 2),
]

TEACHERS = [
    ("teacher", "Madina Rakhimova", "Mathematics"),
    ("t.uzbek", "Gulnora Saidova", "Native Language"),
    ("t.english", "Sardor Nazarov", "English"),
    ("t.physics", "Bobur Tursunov", "Physics"),
    ("t.history", "Zilola Umarova", "History"),
    ("t.informatics", "Timur Akhmedov", "Informatics"),
    ("t.biology", "Nodira Ismoilova", "Biology"),
    ("t.chemistry", "Rustam Kodirov", "Chemistry"),
]

STUDENTS = {
    "9-A": ["Jasur Abdullayev", "Malika Yusupova", "Aziz Karimov", "Sevara Tashpulatova",
            "Doniyor Mirzaev", "Laylo Hamidova", "Otabek Sobirov", "Zarina Nuriddinova"],
    "9-B": ["Sherzod Qodirov", "Madina Ergasheva", "Bekzod Ismoilov", "Nigora Rahmonova",
            "Javlon Xolmatov", "Dildora Azimova", "Ulugbek Normatov", "Feruza Kamolova"],
    "10-A": ["Sanjar Rashidov", "Kamola Ibragimova", "Abbos Haydarov", "Shahlo Mamatova",
             "Muzaffar Yuldashev", "Iroda Ortiqova", "Humoyun Salimov", "Munisa Boboeva"],
}

HOMEWORK_TEXT = {
    "Mathematics": [("Quadratic equations", "Solve exercises 1-10 on page 84 and show every step."),
                    ("Functions and graphs", "Plot y = x^2 - 4x + 3 and mark the vertex and the zeros.")],
    "Native Language": [("Essay: my school", "Write a short essay (150 words) about your school day."),
                        ("Parts of speech", "Complete the table of nouns, verbs and adjectives from the text.")],
    "English": [("Unit 5 vocabulary", "Learn the 20 new words and write a sentence for each one."),
                ("Reading comprehension", "Read the article on page 52 and answer questions 1-6.")],
    "Physics": [("Newton's laws", "Solve problems 3-8 about force and acceleration."),
                ("Measuring speed", "Record the time it takes to walk 20 metres three times and calculate your speed.")],
    "History": [("The Silk Road", "Prepare a one-page summary of the main trade routes."),
                ("Timeline task", "Build a timeline with ten key events from the chapter.")],
    "Informatics": [("Algorithms", "Draw a flowchart for finding the largest of three numbers."),
                    ("Spreadsheet practice", "Create a table that calculates the class average for five subjects.")],
    "Biology": [("The cell", "Label the parts of an animal cell and describe each function."),
                ("Photosynthesis", "Explain the process in your own words using a diagram.")],
    "Chemistry": [("Periodic table", "Learn the first 20 elements with their symbols."),
                  ("Chemical reactions", "Balance the six equations from the worksheet.")],
}

ANNOUNCEMENTS = [
    ("Welcome to the new school year",
     "Dear students and teachers, welcome back! Classes follow the weekly timetable in this portal. "
     "Please check your grades, attendance and homework here every day.", "all", 1, 70),
    ("Parent-teacher meetings",
     "Parent-teacher meetings for all classes take place at the end of the second quarter. "
     "Class teachers will share the exact times with families.", "all", 0, 6),
    ("Mathematics olympiad registration is open",
     "Students in grades 9 and 10 can register for the school mathematics olympiad with Ms. Rakhimova "
     "until the end of this month.", "students", 0, 3),
    ("Quarter grading reminder",
     "Teachers: please make sure every student has at least three grades per subject before the quarter "
     "closes. Quarter marks are calculated automatically from the grade book.", "teachers", 0, 2),
    ("Library hours extended",
     "The school library is now open until 17:00 on weekdays. New books for grades 9-10 have arrived.",
     "all", 0, 1),
]


def _build_timetable(rng, class_ids, quotas, teacher_of, lessons_per_day=5):
    """Create a clash-free weekly timetable: {(class_id, weekday, lesson): subject_id}."""
    for _attempt in range(500):
        table = {}
        remaining = {cid: dict(quotas) for cid in class_ids}
        ok = True
        for day in range(1, 6):
            day_subjects = {}
            for cid in class_ids:
                rem = remaining[cid]
                chosen = sorted(rem, key=lambda s: (-rem[s], rng.random()))[:lessons_per_day]
                for s in chosen:
                    rem[s] -= 1
                day_subjects[cid] = list(chosen)
            for lesson in range(1, lessons_per_day + 1):
                busy = set()
                for cid in class_ids:
                    options = [s for s in day_subjects[cid] if teacher_of[(cid, s)] not in busy]
                    if not options:
                        ok = False
                        break
                    pick = rng.choice(options)
                    day_subjects[cid].remove(pick)
                    busy.add(teacher_of[(cid, pick)])
                    table[(cid, day, lesson)] = pick
                if not ok:
                    break
            if not ok:
                break
        if ok:
            return table
    raise RuntimeError("Could not build a demo timetable")


def seed_demo(conn):
    """Fill an empty database with fictional demo data."""
    rng = random.Random(2026)
    today = date.today()
    start = school_year_start(today)
    end = min(today - timedelta(days=1), date(start.year + 1, 5, 31))

    # Hashing is slow on purpose, so hash each demo password once and reuse it.
    hash_cache = {}

    def hashed(password):
        if password not in hash_cache:
            hash_cache[password] = generate_password_hash(password)
        return hash_cache[password]

    def add_user(username, password, role, full_name, class_id=None, birth=""):
        n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] + 1
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role, full_name, email, phone, address, "
            "birth_date, class_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (username, hashed(password), role, full_name, f"{username}@wmaktab.example",
             f"+998 90 000 00 {n % 100:02d}", "Demo Street, Demo City", birth, class_id),
        )
        return cur.lastrowid

    # Classes and subjects
    class_ids = {}
    for name in CLASSES:
        class_ids[name] = conn.execute("INSERT INTO classes (name) VALUES (?)", (name,)).lastrowid
    subject_ids, quotas = {}, {}
    for name, per_week in SUBJECTS:
        sid = conn.execute("INSERT INTO subjects (name) VALUES (?)", (name,)).lastrowid
        subject_ids[name] = sid
        quotas[sid] = per_week

    # People
    add_user("admin", "admin123", "admin", "Aziza Bekmuratova")
    teacher_of_subject = {}
    for username, full_name, subject in TEACHERS:
        teacher_of_subject[subject_ids[subject]] = add_user(username, "teacher123", "teacher", full_name)

    teacher_of = {}
    for cid in class_ids.values():
        for sid, tid in teacher_of_subject.items():
            conn.execute("INSERT INTO teaching (teacher_id, class_id, subject_id) VALUES (?, ?, ?)",
                         (tid, cid, sid))
            teacher_of[(cid, sid)] = tid

    students = []  # (id, class_id, ability)
    for cname, names in STUDENTS.items():
        cid = class_ids[cname]
        year = 2011 if cname.startswith("9") else 2010
        for index, full_name in enumerate(names):
            first, last = full_name.lower().split()
            username = "student" if (cname == "9-A" and index == 0) else f"{first}.{last}"
            birth = date(year, rng.randint(1, 12), rng.randint(1, 28)).isoformat()
            sid = add_user(username, "student123", "student", full_name, cid, birth)
            ability = 4.1 if username == "student" else rng.uniform(3.0, 4.8)
            students.append((sid, cid, ability))

    # Timetable
    table = _build_timetable(rng, list(class_ids.values()), quotas, teacher_of)
    subject_index = {sid: i for i, sid in enumerate(subject_ids.values())}
    lessons_by_day = {}
    for (cid, day, lesson), sid in sorted(table.items()):
        conn.execute(
            "INSERT INTO timetable (class_id, weekday, lesson_no, subject_id, teacher_id, room) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (cid, day, lesson, sid, teacher_of[(cid, sid)], f"Room {101 + subject_index[sid]}"),
        )
        lessons_by_day.setdefault((cid, day), []).append(sid)

    # Grades and attendance for every school day so far this school year
    kinds = rng.choices(GRADE_KINDS, weights=[40, 20, 20, 15, 5], k=4000)
    kind_i = 0
    grade_rows, attendance_rows = [], []
    day = start + timedelta(days=1)  # 1 September is a public holiday
    while day <= end:
        if day.isoweekday() <= 5:
            for sid, cid, ability in students:
                roll = rng.random()
                status = ("present" if roll < 0.91 else "late" if roll < 0.95
                          else "absent" if roll < 0.98 else "excused")
                attendance_rows.append((sid, day.isoformat(), status, "", teacher_of_subject[subject_ids["Mathematics"]]))
                if status in ("absent", "excused"):
                    continue
                for subj in lessons_by_day.get((cid, day.isoweekday()), []):
                    if rng.random() < 0.18:
                        mean = ability + (hash((sid, subj)) % 7 - 3) * 0.08
                        value = max(2, min(5, round(rng.gauss(mean, 0.75))))
                        kind = kinds[kind_i % len(kinds)]
                        kind_i += 1
                        grade_rows.append((sid, subj, teacher_of[(cid, subj)], value, quarter_for(day),
                                           kind, "", day.isoformat()))
        day += timedelta(days=1)
    conn.executemany(
        "INSERT INTO grades (student_id, subject_id, teacher_id, value, quarter, kind, comment, date) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", grade_rows)
    conn.executemany(
        "INSERT INTO attendance (student_id, date, status, note, marked_by) VALUES (?, ?, ?, ?, ?)",
        attendance_rows)

    # Homework: one assignment that is already due and one that is still open
    students_by_class = {}
    for sid, cid, _ in students:
        students_by_class.setdefault(cid, []).append(sid)
    for cid in class_ids.values():
        for subject, items in HOMEWORK_TEXT.items():
            subj_id = subject_ids[subject]
            for position, (title, description) in enumerate(items):
                due = today - timedelta(days=rng.randint(2, 6)) if position == 0 else today + timedelta(days=rng.randint(1, 7))
                created = due - timedelta(days=5)
                hid = conn.execute(
                    "INSERT INTO homework (class_id, subject_id, teacher_id, title, description, due_date, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (cid, subj_id, teacher_of[(cid, subj_id)], title, description, due.isoformat(),
                     created.isoformat() + " 09:00:00"),
                ).lastrowid
                chance = 0.8 if position == 0 else 0.25
                for sid in students_by_class[cid]:
                    if rng.random() < chance:
                        conn.execute(
                            "INSERT INTO homework_status (homework_id, student_id, done, done_at) VALUES (?, ?, 1, ?)",
                            (hid, sid, (due - timedelta(days=1)).isoformat() + " 18:00:00"))

    # News and announcements
    admin_id = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()[0]
    for title, body, audience, pinned, days_ago in ANNOUNCEMENTS:
        stamp = datetime.combine(today - timedelta(days=days_ago), datetime.min.time()).replace(hour=9)
        conn.execute(
            "INSERT INTO announcements (title, body, audience, pinned, author_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (title, body, audience, pinned, admin_id, stamp.strftime("%Y-%m-%d %H:%M:%S")))

    conn.commit()


if __name__ == "__main__":
    if "--reset" in sys.argv:
        reset_db()
        print(f"Database rebuilt with demo data: {DB_PATH}")
    else:
        init_db()
        print(f"Database ready: {DB_PATH}")
