"""wMaktab - Flask application: pages, JSON API and business rules.

Run with:  python main.py   then open http://127.0.0.1:5000
"""
import os
import re
import sqlite3
from datetime import date, datetime, timedelta

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

import auth
import database as dbm
import stats_bridge
from auth import current_user, login_required, roles_required
from database import (ATTENDANCE_STATUSES, GRADE_KINDS, LESSON_TIMES, MAX_LESSONS, execute,
                      query_all, query_one, quarter_for, school_year_label, school_year_start)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config.update(
    SECRET_KEY=auth.load_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    MAX_CONTENT_LENGTH=1024 * 1024,
    JSON_SORT_KEYS=False,
)
app.teardown_appcontext(dbm.close_db)

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]


# ==========================================================================
# Errors, validation helpers and security headers
# ==========================================================================
class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


@app.errorhandler(ApiError)
def handle_api_error(err):
    return jsonify(error=err.message), err.status


@app.errorhandler(404)
def handle_404(_err):
    if request.path.startswith("/api/"):
        return jsonify(error="Not found."), 404
    return redirect(url_for("index"))


@app.errorhandler(405)
def handle_405(_err):
    return jsonify(error="Method not allowed."), 405


@app.errorhandler(413)
def handle_413(_err):
    return jsonify(error="Request is too large."), 413


@app.errorhandler(500)
def handle_500(_err):
    return jsonify(error="Something went wrong on the server."), 500


@app.before_request
def csrf_protect():
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.path.startswith("/api/"):
        if not auth.verify_csrf():
            return jsonify(error="Your session expired. Please reload the page and try again."), 403
    return None


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    )
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def get_int(src, key, required=True, lo=None, hi=None, label=None):
    label = label or key.replace("_id", "").replace("_", " ")
    value = src.get(key)
    if value is None or value == "":
        if required:
            raise ApiError(f"{label.capitalize()} is required.")
        return None
    try:
        if isinstance(value, bool):
            raise ValueError
        number = int(value)
    except (TypeError, ValueError):
        raise ApiError(f"{label.capitalize()} must be a whole number.")
    if (lo is not None and number < lo) or (hi is not None and number > hi):
        raise ApiError(f"{label.capitalize()} is out of range.")
    return number


def get_text(src, key, label=None, min_len=0, max_len=200, required=False):
    label = label or key.replace("_", " ")
    value = src.get(key)
    value = "" if value is None else str(value).strip()
    if required and not value:
        raise ApiError(f"{label.capitalize()} is required.")
    if value and len(value) < min_len:
        raise ApiError(f"{label.capitalize()} must be at least {min_len} characters.")
    if len(value) > max_len:
        raise ApiError(f"{label.capitalize()} must be at most {max_len} characters.")
    return value


def get_date(src, key, label=None, required=True):
    label = label or key.replace("_", " ")
    value = src.get(key)
    if value in (None, ""):
        if required:
            raise ApiError(f"{label.capitalize()} is required.")
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ApiError(f"{label.capitalize()} is not a valid date.")


def json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError("Invalid request body.")
    return data


def public_user(u):
    keys = ("id", "username", "role", "full_name", "email", "phone", "class_id", "class_name")
    return {k: u.get(k) for k in keys}


def placeholders(items):
    return ",".join("?" for _ in items)


# ==========================================================================
# Permission helpers
# ==========================================================================
def teaches(teacher_id, class_id, subject_id=None):
    if subject_id is None:
        return query_one("SELECT 1 AS ok FROM teaching WHERE teacher_id=? AND class_id=?",
                         (teacher_id, class_id)) is not None
    return query_one("SELECT 1 AS ok FROM teaching WHERE teacher_id=? AND class_id=? AND subject_id=?",
                     (teacher_id, class_id, subject_id)) is not None


def teacher_subject_ids(teacher_id, class_id):
    rows = query_all("SELECT subject_id FROM teaching WHERE teacher_id=? AND class_id=?",
                     (teacher_id, class_id))
    return [r["subject_id"] for r in rows]


def require_class_access(user, class_id, subject_id=None):
    """Admins may use any class. Teachers only classes (and subjects) they teach."""
    if not query_one("SELECT id FROM classes WHERE id=?", (class_id,)):
        raise ApiError("Class not found.", 404)
    if user["role"] == "teacher" and not teaches(user["id"], class_id, subject_id):
        raise ApiError("You do not teach this class or subject.", 403)
    if user["role"] == "student":
        raise ApiError("You do not have permission to do that.", 403)


def get_student(student_id):
    student = query_one(
        "SELECT u.*, c.name AS class_name FROM users u LEFT JOIN classes c ON c.id=u.class_id "
        "WHERE u.id=? AND u.role='student'", (student_id,))
    if not student:
        raise ApiError("Student not found.", 404)
    return student


def check_student_access(user, student_id):
    student = get_student(student_id)
    if user["role"] == "student" and user["id"] != student_id:
        raise ApiError("You can only view your own records.", 403)
    if user["role"] == "teacher" and not (student["class_id"] and teaches(user["id"], student["class_id"])):
        raise ApiError("This student is not in one of your classes.", 403)
    return student


def assignments_for(user):
    sql = ("SELECT t.id, t.class_id, c.name AS class_name, t.subject_id, s.name AS subject_name, "
           "t.teacher_id, u.full_name AS teacher_name FROM teaching t "
           "JOIN classes c ON c.id=t.class_id JOIN subjects s ON s.id=t.subject_id "
           "JOIN users u ON u.id=t.teacher_id")
    if user["role"] == "teacher":
        return query_all(sql + " WHERE t.teacher_id=? ORDER BY c.name, s.name", (user["id"],))
    if user["role"] == "student":
        return query_all(sql + " WHERE t.class_id=? ORDER BY s.name", (user["class_id"],))
    return query_all(sql + " ORDER BY c.name, s.name")


# ==========================================================================
# Statistics helpers (all numbers are computed by the C++ engine)
# ==========================================================================
def summarize_counts(counts):
    present, late = counts.get("present", 0), counts.get("late", 0)
    excused, absent = counts.get("excused", 0), counts.get("absent", 0)
    total = present + late + excused + absent
    rate = round(100.0 * (present + late) / total, 1) if total else None
    return {"present": present, "late": late, "excused": excused, "absent": absent,
            "total": total, "rate": rate}


def attendance_summary(student_id):
    rows = query_all("SELECT status, COUNT(*) AS n FROM attendance WHERE student_id=? GROUP BY status",
                     (student_id,))
    return summarize_counts({r["status"]: r["n"] for r in rows})


def student_summary(student_id, class_id):
    """Per-subject quarter marks, yearly marks and overall figures for one student."""
    rows = query_all("SELECT subject_id, quarter, value FROM grades WHERE student_id=?", (student_id,))
    groups = {}
    for r in rows:
        groups.setdefault(f"S:{r['subject_id']}:{r['quarter']}", []).append(r["value"])
        groups.setdefault("ALL", []).append(r["value"])
        groups.setdefault(f"Q:{r['quarter']}", []).append(r["value"])
    first = stats_bridge.compute(groups)

    # Yearly mark of a subject = average of its quarter marks.
    year_groups = {}
    for key, st in first.items():
        if key.startswith("S:"):
            _, subject_id, _q = key.split(":")
            year_groups.setdefault(f"Y:{subject_id}", []).append(st["mark"])
    year = stats_bridge.compute(year_groups)
    gpa = stats_bridge.compute({"GPA": [v["mark"] for v in year.values()]}).get("GPA")

    subjects = query_all(
        "SELECT DISTINCT s.id, s.name FROM subjects s "
        "WHERE s.id IN (SELECT subject_id FROM teaching WHERE class_id=?) "
        "OR s.id IN (SELECT subject_id FROM grades WHERE student_id=?) ORDER BY s.name",
        (class_id, student_id))
    out = []
    for s in subjects:
        quarters = {str(q): first.get(f"S:{s['id']}:{q}") for q in range(1, 5)}
        out.append({"subject_id": s["id"], "name": s["name"], "quarters": quarters,
                    "year": year.get(f"Y:{s['id']}"),
                    "count": sum(st["count"] for st in quarters.values() if st)})
    return {"subjects": out, "overall": first.get("ALL"),
            "quarters": {str(q): first.get(f"Q:{q}") for q in range(1, 5)},
            "gpa": gpa, "engine": stats_bridge.engine()}


# ==========================================================================
# Pages
# ==========================================================================
@app.get("/")
def index():
    return render_template("index.html", csrf=auth.csrf_token(), user=current_user())


@app.get("/login")
def login_page():
    if current_user():
        return redirect(url_for("dashboard_page"))
    return render_template("login.html", csrf=auth.csrf_token())


@app.get("/dashboard")
@auth.page_login_required
def dashboard_page():
    return render_template("dashboard.html", csrf=auth.csrf_token(), user=current_user())


# ==========================================================================
# Authentication API
# ==========================================================================
@app.post("/api/login")
def api_login():
    data = json_body()
    username = str(data.get("username", "")).strip()[:64]
    password = str(data.get("password", ""))[:256]
    if not username or not password:
        raise ApiError("Enter your username and password.")
    user, error, status = auth.attempt_login(username, password)
    if error:
        return jsonify(error=error), status
    full = query_one("SELECT u.*, c.name AS class_name FROM users u LEFT JOIN classes c "
                     "ON c.id=u.class_id WHERE u.id=?", (user["id"],))
    return jsonify(ok=True, user=public_user(full), csrf=session["csrf"], redirect=url_for("dashboard_page"))


@app.post("/api/logout")
def api_logout():
    auth.logout_user()
    return jsonify(ok=True)


@app.get("/api/me")
@login_required
def api_me():
    return jsonify(user=public_user(current_user()))


@app.post("/api/me/password")
@login_required
def api_change_password():
    user = current_user()
    data = json_body()
    from werkzeug.security import check_password_hash
    if not check_password_hash(user["password_hash"], str(data.get("current", ""))):
        raise ApiError("Your current password is incorrect.", 400)
    new = str(data.get("new", ""))
    problem = auth.password_problem(new)
    if problem:
        raise ApiError(problem)
    if new == str(data.get("current", "")):
        raise ApiError("Choose a password that is different from the current one.")
    execute("UPDATE users SET password_hash=? WHERE id=?", (auth.hash_password(new), user["id"]))
    return jsonify(ok=True)


@app.get("/api/health")
def api_health():
    return jsonify(ok=True, stats_engine=stats_bridge.engine())


# ==========================================================================
# Meta (dropdown data for the front end)
# ==========================================================================
@app.get("/api/meta")
@login_required
def api_meta():
    user = current_user()
    today = date.today()
    meta = {
        "user": public_user(user),
        "classes": query_all("SELECT id, name FROM classes ORDER BY name"),
        "subjects": query_all("SELECT id, name FROM subjects ORDER BY name"),
        "assignments": assignments_for(user),
        "today": today.isoformat(),
        "quarter": quarter_for(today),
        "school_year": school_year_label(today),
        "school_year_start": school_year_start(today).isoformat(),
        "lesson_times": {str(k): v for k, v in LESSON_TIMES.items()},
        "max_lessons": MAX_LESSONS,
        "grade_kinds": GRADE_KINDS,
        "weekdays": WEEKDAYS,
        "engine": stats_bridge.engine(),
    }
    if user["role"] == "admin":
        meta["teachers"] = query_all("SELECT id, full_name FROM users WHERE role='teacher' AND active=1 ORDER BY full_name")
    return jsonify(meta)


# ==========================================================================
# Dashboard
# ==========================================================================
def announcements_for(user, limit=None):
    if user["role"] == "admin":
        where, args = "1=1", []
    elif user["role"] == "teacher":
        where, args = "(a.audience IN ('all','teachers') OR a.author_id=?)", [user["id"]]
    else:
        where, args = "a.audience IN ('all','students')", []
    sql = ("SELECT a.id, a.title, a.body, a.audience, a.pinned, a.author_id, a.created_at, "
           "u.full_name AS author_name FROM announcements a LEFT JOIN users u ON u.id=a.author_id "
           f"WHERE {where} ORDER BY a.pinned DESC, a.created_at DESC, a.id DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return query_all(sql, args)


def school_day(today):
    """Weekday number 1..5 to show on 'today's lessons' (weekends show Monday)."""
    wd = today.isoweekday()
    return (wd, False) if wd <= 5 else (1, True)


@app.get("/api/dashboard")
@login_required
def api_dashboard():
    user = current_user()
    today = date.today()
    day, is_next = school_day(today)
    base = {"role": user["role"], "announcements": announcements_for(user, 4),
            "day": day, "day_name": WEEKDAYS[day - 1], "is_next_school_day": is_next}

    if user["role"] == "student":
        summary = student_summary(user["id"], user["class_id"])
        att = attendance_summary(user["id"])
        pending = query_one(
            "SELECT COUNT(*) AS n FROM homework h WHERE h.class_id=? AND h.due_date>=? AND NOT EXISTS "
            "(SELECT 1 FROM homework_status hs WHERE hs.homework_id=h.id AND hs.student_id=? AND hs.done=1)",
            (user["class_id"], today.isoformat(), user["id"]))["n"]
        overdue = query_one(
            "SELECT COUNT(*) AS n FROM homework h WHERE h.class_id=? AND h.due_date<? AND NOT EXISTS "
            "(SELECT 1 FROM homework_status hs WHERE hs.homework_id=h.id AND hs.student_id=? AND hs.done=1)",
            (user["class_id"], today.isoformat(), user["id"]))["n"]
        overall = summary["overall"] or {}
        base.update({
            "stats": {"average": overall.get("mean"), "quality_rate": overall.get("quality_rate"),
                      "grades_count": overall.get("count", 0), "attendance_rate": att["rate"],
                      "homework_pending": pending, "homework_overdue": overdue},
            "subject_means": [{"name": s["name"], "value": s["year"]["mean"] if s["year"] else None}
                              for s in summary["subjects"]],
            "lessons": query_all(
                "SELECT tt.lesson_no, s.name AS subject_name, tt.room, u.full_name AS teacher_name "
                "FROM timetable tt JOIN subjects s ON s.id=tt.subject_id LEFT JOIN users u ON u.id=tt.teacher_id "
                "WHERE tt.class_id=? AND tt.weekday=? ORDER BY tt.lesson_no", (user["class_id"], day)),
            "recent_grades": query_all(
                "SELECT g.id, g.value, g.kind, g.date, s.name AS subject_name FROM grades g "
                "JOIN subjects s ON s.id=g.subject_id WHERE g.student_id=? "
                "ORDER BY g.date DESC, g.id DESC LIMIT 6", (user["id"],)),
            "homework": query_all(
                "SELECT h.id, h.title, h.due_date, s.name AS subject_name FROM homework h "
                "JOIN subjects s ON s.id=h.subject_id WHERE h.class_id=? AND NOT EXISTS "
                "(SELECT 1 FROM homework_status hs WHERE hs.homework_id=h.id AND hs.student_id=? AND hs.done=1) "
                "ORDER BY h.due_date ASC LIMIT 4", (user["class_id"], user["id"])),
        })

    elif user["role"] == "teacher":
        rows = query_all(
            "SELECT t.class_id, c.name AS class_name, s.name AS subject_name FROM teaching t "
            "JOIN classes c ON c.id=t.class_id JOIN subjects s ON s.id=t.subject_id "
            "WHERE t.teacher_id=? ORDER BY c.name, s.name", (user["id"],))
        class_ids = sorted({r["class_id"] for r in rows})
        counts = {r["class_id"]: r["n"] for r in query_all(
            "SELECT class_id, COUNT(*) AS n FROM users WHERE role='student' AND active=1 "
            f"AND class_id IN ({placeholders(class_ids)}) GROUP BY class_id", class_ids)} if class_ids else {}
        grades = query_all(
            "SELECT u.class_id, g.value FROM grades g JOIN users u ON u.id=g.student_id "
            "JOIN teaching t ON t.class_id=u.class_id AND t.subject_id=g.subject_id "
            "WHERE t.teacher_id=? AND u.active=1", (user["id"],))
        groups = {"ALL": [g["value"] for g in grades]}
        for g in grades:
            groups.setdefault(f"C:{g['class_id']}", []).append(g["value"])
        st = stats_bridge.compute(groups)
        classes = []
        for cid in class_ids:
            names = [r["subject_name"] for r in rows if r["class_id"] == cid]
            classes.append({"class_id": cid, "class_name": next(r["class_name"] for r in rows if r["class_id"] == cid),
                            "subjects": names, "students": counts.get(cid, 0),
                            "mean": (st.get(f"C:{cid}") or {}).get("mean")})
        open_hw = query_one("SELECT COUNT(*) AS n FROM homework WHERE teacher_id=? AND due_date>=?",
                            (user["id"], today.isoformat()))["n"]
        base.update({
            "stats": {"classes": len(class_ids), "students": sum(counts.values()),
                      "average": (st.get("ALL") or {}).get("mean"), "homework_open": open_hw},
            "classes": classes,
            "lessons": query_all(
                "SELECT tt.lesson_no, s.name AS subject_name, tt.room, c.name AS class_name FROM timetable tt "
                "JOIN subjects s ON s.id=tt.subject_id JOIN classes c ON c.id=tt.class_id "
                "WHERE tt.teacher_id=? AND tt.weekday=? ORDER BY tt.lesson_no", (user["id"], day)),
            "recent_grades": query_all(
                "SELECT g.id, g.value, g.kind, g.date, s.name AS subject_name, u.full_name AS student_name "
                "FROM grades g JOIN subjects s ON s.id=g.subject_id JOIN users u ON u.id=g.student_id "
                "WHERE g.teacher_id=? ORDER BY g.created_at DESC, g.id DESC LIMIT 8", (user["id"],)),
        })

    else:  # admin
        counts = {r["role"]: r["n"] for r in query_all(
            "SELECT role, COUNT(*) AS n FROM users WHERE active=1 GROUP BY role")}
        grades = query_all("SELECT u.class_id, g.value FROM grades g JOIN users u ON u.id=g.student_id WHERE u.active=1")
        groups = {"ALL": [g["value"] for g in grades]}
        for g in grades:
            groups.setdefault(f"C:{g['class_id']}", []).append(g["value"])
        st = stats_bridge.compute(groups)
        classes = query_all("SELECT id, name FROM classes ORDER BY name")
        marked = query_all(
            "SELECT a.status, COUNT(*) AS n FROM attendance a JOIN users u ON u.id=a.student_id "
            "WHERE a.date=? AND u.active=1 GROUP BY a.status", (today.isoformat(),))
        today_att = summarize_counts({r["status"]: r["n"] for r in marked})
        base.update({
            "stats": {"students": counts.get("student", 0), "teachers": counts.get("teacher", 0),
                      "classes": len(classes), "average": (st.get("ALL") or {}).get("mean"),
                      "attendance_today": today_att, "is_school_day": today.isoweekday() <= 5},
            "class_means": [{"name": c["name"], "value": (st.get(f"C:{c['id']}") or {}).get("mean")} for c in classes],
            "distribution": (st.get("ALL") or {}).get("dist"),
            "recent_users": query_all(
                "SELECT u.id, u.full_name, u.role, u.created_at, c.name AS class_name FROM users u "
                "LEFT JOIN classes c ON c.id=u.class_id ORDER BY u.id DESC LIMIT 5"),
        })
    base["engine"] = stats_bridge.engine()
    return jsonify(base)


# ==========================================================================
# Profiles
# ==========================================================================
def profile_payload(target):
    profile = {k: target.get(k) for k in ("id", "username", "role", "full_name", "email", "phone", "address",
                                           "birth_date", "class_id", "class_name", "created_at", "active")}
    if target["role"] == "teacher":
        profile["assignments"] = assignments_for(target)
    if target["role"] == "student":
        summary = student_summary(target["id"], target["class_id"])
        att = attendance_summary(target["id"])
        profile["summary"] = {"average": (summary["overall"] or {}).get("mean"),
                              "gpa": (summary["gpa"] or {}).get("mean"), "attendance_rate": att["rate"],
                              "grades_count": (summary["overall"] or {}).get("count", 0)}
    return profile


@app.get("/api/profile/<int:user_id>")
@login_required
def api_profile(user_id):
    viewer = current_user()
    target = query_one("SELECT u.*, c.name AS class_name FROM users u LEFT JOIN classes c ON c.id=u.class_id "
                       "WHERE u.id=?", (user_id,))
    if not target:
        raise ApiError("User not found.", 404)
    if viewer["id"] != user_id:
        if viewer["role"] == "admin":
            pass
        elif target["role"] == "student":
            check_student_access(viewer, user_id)
        else:
            raise ApiError("You do not have permission to view this profile.", 403)
    return jsonify(profile=profile_payload(target))


@app.put("/api/profile/<int:user_id>")
@login_required
def api_profile_update(user_id):
    viewer = current_user()
    if viewer["id"] != user_id and viewer["role"] != "admin":
        raise ApiError("You can only edit your own profile.", 403)
    target = query_one("SELECT * FROM users WHERE id=?", (user_id,))
    if not target:
        raise ApiError("User not found.", 404)
    data = json_body()
    email = get_text(data, "email", max_len=120)
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise ApiError("Enter a valid email address.")
    phone = get_text(data, "phone", max_len=30)
    address = get_text(data, "address", max_len=200)
    fields = {"email": email, "phone": phone, "address": address}
    if viewer["role"] == "admin":
        fields["full_name"] = get_text(data, "full_name", min_len=2, max_len=80, required=True)
        birth = get_date(data, "birth_date", required=False)
        fields["birth_date"] = birth.isoformat() if birth else ""
    sets = ", ".join(f"{k}=?" for k in fields)
    execute(f"UPDATE users SET {sets} WHERE id=?", [*fields.values(), user_id])
    return jsonify(ok=True)


# ==========================================================================
# User management (admin)
# ==========================================================================
@app.get("/api/users")
@roles_required("admin")
def api_users():
    sql = ("SELECT u.id, u.username, u.role, u.full_name, u.email, u.phone, u.address, u.birth_date, "
           "u.class_id, c.name AS class_name, u.active, u.created_at FROM users u "
           "LEFT JOIN classes c ON c.id=u.class_id WHERE 1=1")
    args = []
    role = request.args.get("role", "")
    if role in auth.ROLES:
        sql += " AND u.role=?"
        args.append(role)
    class_id = request.args.get("class_id", type=int)
    if class_id:
        sql += " AND u.class_id=?"
        args.append(class_id)
    q = request.args.get("q", "").strip()[:60]
    if q:
        sql += " AND (u.full_name LIKE ? OR u.username LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY CASE u.role WHEN 'admin' THEN 0 WHEN 'teacher' THEN 1 ELSE 2 END, c.name, u.full_name"
    return jsonify(users=query_all(sql, args))


def validate_class_for_student(data, required=True):
    class_id = get_int(data, "class_id", required=required, label="class")
    if class_id and not query_one("SELECT id FROM classes WHERE id=?", (class_id,)):
        raise ApiError("Class not found.")
    return class_id


@app.post("/api/users")
@roles_required("admin")
def api_users_create():
    data = json_body()
    username = get_text(data, "username", required=True, max_len=32)
    if not USERNAME_RE.match(username):
        raise ApiError("Username must be 3-32 characters: letters, numbers, dot, dash or underscore.")
    role = str(data.get("role", ""))
    if role not in auth.ROLES:
        raise ApiError("Choose a valid role.")
    full_name = get_text(data, "full_name", label="full name", min_len=2, max_len=80, required=True)
    password = str(data.get("password", ""))
    problem = auth.password_problem(password)
    if problem:
        raise ApiError(problem)
    email = get_text(data, "email", max_len=120)
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise ApiError("Enter a valid email address.")
    phone = get_text(data, "phone", max_len=30)
    class_id = validate_class_for_student(data, required=True) if role == "student" else None
    try:
        new_id = execute(
            "INSERT INTO users (username, password_hash, role, full_name, email, phone, class_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (username, auth.hash_password(password), role, full_name, email, phone, class_id))
    except sqlite3.IntegrityError:
        raise ApiError("That username is already taken.", 409)
    return jsonify(ok=True, id=new_id), 201


@app.put("/api/users/<int:user_id>")
@roles_required("admin")
def api_users_update(user_id):
    target = query_one("SELECT * FROM users WHERE id=?", (user_id,))
    if not target:
        raise ApiError("User not found.", 404)
    data = json_body()
    me = current_user()
    fields = {
        "full_name": get_text(data, "full_name", label="full name", min_len=2, max_len=80, required=True),
        "email": get_text(data, "email", max_len=120),
        "phone": get_text(data, "phone", max_len=30),
        "address": get_text(data, "address", max_len=200),
    }
    if fields["email"] and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", fields["email"]):
        raise ApiError("Enter a valid email address.")
    birth = get_date(data, "birth_date", required=False)
    fields["birth_date"] = birth.isoformat() if birth else ""
    if target["role"] == "student":
        fields["class_id"] = validate_class_for_student(data, required=True)
    if "active" in data:
        active = 1 if data.get("active") else 0
        if user_id == me["id"] and not active:
            raise ApiError("You cannot disable your own account.")
        fields["active"] = active
    sets = ", ".join(f"{k}=?" for k in fields)
    execute(f"UPDATE users SET {sets} WHERE id=?", [*fields.values(), user_id])
    return jsonify(ok=True)


@app.delete("/api/users/<int:user_id>")
@roles_required("admin")
def api_users_delete(user_id):
    if user_id == current_user()["id"]:
        raise ApiError("You cannot delete your own account.")
    if not query_one("SELECT id FROM users WHERE id=?", (user_id,)):
        raise ApiError("User not found.", 404)
    execute("DELETE FROM users WHERE id=?", (user_id,))
    return jsonify(ok=True)


@app.post("/api/users/<int:user_id>/password")
@roles_required("admin")
def api_users_password(user_id):
    if not query_one("SELECT id FROM users WHERE id=?", (user_id,)):
        raise ApiError("User not found.", 404)
    password = str(json_body().get("password", ""))
    problem = auth.password_problem(password)
    if problem:
        raise ApiError(problem)
    execute("UPDATE users SET password_hash=? WHERE id=?", (auth.hash_password(password), user_id))
    return jsonify(ok=True)


# ==========================================================================
# School setup (admin): classes, subjects, teaching assignments
# ==========================================================================
@app.get("/api/setup")
@roles_required("admin")
def api_setup():
    return jsonify(
        classes=query_all("SELECT c.id, c.name, (SELECT COUNT(*) FROM users u WHERE u.class_id=c.id "
                          "AND u.role='student') AS students FROM classes c ORDER BY c.name"),
        subjects=query_all("SELECT id, name FROM subjects ORDER BY name"),
        assignments=assignments_for(current_user()),
        teachers=query_all("SELECT id, full_name FROM users WHERE role='teacher' AND active=1 ORDER BY full_name"))


@app.post("/api/classes")
@roles_required("admin")
def api_classes_create():
    name = get_text(json_body(), "name", label="class name", required=True, max_len=20)
    try:
        new_id = execute("INSERT INTO classes (name) VALUES (?)", (name,))
    except sqlite3.IntegrityError:
        raise ApiError("A class with that name already exists.", 409)
    return jsonify(ok=True, id=new_id), 201


@app.delete("/api/classes/<int:class_id>")
@roles_required("admin")
def api_classes_delete(class_id):
    if not query_one("SELECT id FROM classes WHERE id=?", (class_id,)):
        raise ApiError("Class not found.", 404)
    execute("DELETE FROM classes WHERE id=?", (class_id,))
    return jsonify(ok=True)


@app.post("/api/subjects")
@roles_required("admin")
def api_subjects_create():
    name = get_text(json_body(), "name", label="subject name", required=True, max_len=40)
    try:
        new_id = execute("INSERT INTO subjects (name) VALUES (?)", (name,))
    except sqlite3.IntegrityError:
        raise ApiError("A subject with that name already exists.", 409)
    return jsonify(ok=True, id=new_id), 201


@app.delete("/api/subjects/<int:subject_id>")
@roles_required("admin")
def api_subjects_delete(subject_id):
    if not query_one("SELECT id FROM subjects WHERE id=?", (subject_id,)):
        raise ApiError("Subject not found.", 404)
    execute("DELETE FROM subjects WHERE id=?", (subject_id,))
    return jsonify(ok=True)


@app.post("/api/teaching")
@roles_required("admin")
def api_teaching_set():
    data = json_body()
    teacher_id = get_int(data, "teacher_id", label="teacher")
    class_id = get_int(data, "class_id", label="class")
    subject_id = get_int(data, "subject_id", label="subject")
    if not query_one("SELECT id FROM users WHERE id=? AND role='teacher' AND active=1", (teacher_id,)):
        raise ApiError("Teacher not found.")
    if not query_one("SELECT id FROM classes WHERE id=?", (class_id,)):
        raise ApiError("Class not found.")
    if not query_one("SELECT id FROM subjects WHERE id=?", (subject_id,)):
        raise ApiError("Subject not found.")
    existing = query_one("SELECT id FROM teaching WHERE class_id=? AND subject_id=?", (class_id, subject_id))
    if existing:
        execute("UPDATE teaching SET teacher_id=? WHERE id=?", (teacher_id, existing["id"]))
        execute("UPDATE timetable SET teacher_id=? WHERE class_id=? AND subject_id=?",
                (teacher_id, class_id, subject_id))
    else:
        execute("INSERT INTO teaching (teacher_id, class_id, subject_id) VALUES (?, ?, ?)",
                (teacher_id, class_id, subject_id))
    return jsonify(ok=True), 201


@app.delete("/api/teaching/<int:teaching_id>")
@roles_required("admin")
def api_teaching_delete(teaching_id):
    row = query_one("SELECT * FROM teaching WHERE id=?", (teaching_id,))
    if not row:
        raise ApiError("Assignment not found.", 404)
    execute("DELETE FROM timetable WHERE class_id=? AND subject_id=?", (row["class_id"], row["subject_id"]))
    execute("DELETE FROM teaching WHERE id=?", (teaching_id,))
    return jsonify(ok=True)


# ==========================================================================
# Students list (teacher / admin)
# ==========================================================================
@app.get("/api/students")
@roles_required("teacher", "admin")
def api_students():
    user = current_user()
    class_id = request.args.get("class_id", type=int)
    if not class_id:
        raise ApiError("Choose a class.")
    require_class_access(user, class_id)
    students = query_all("SELECT id, username, full_name, email, phone FROM users "
                         "WHERE role='student' AND active=1 AND class_id=? ORDER BY full_name", (class_id,))
    sql = "SELECT g.student_id, g.value FROM grades g JOIN users u ON u.id=g.student_id WHERE u.class_id=?"
    args = [class_id]
    if user["role"] == "teacher":
        subject_ids = teacher_subject_ids(user["id"], class_id)
        sql += f" AND g.subject_id IN ({placeholders(subject_ids)})"
        args += subject_ids
    groups = {}
    for g in query_all(sql, args):
        groups.setdefault(f"S:{g['student_id']}", []).append(g["value"])
    st = stats_bridge.compute(groups)
    att = {}
    if students:
        ids = [s["id"] for s in students]
        for r in query_all(f"SELECT student_id, status, COUNT(*) AS n FROM attendance "
                           f"WHERE student_id IN ({placeholders(ids)}) GROUP BY student_id, status", ids):
            att.setdefault(r["student_id"], {})[r["status"]] = r["n"]
    for s in students:
        stat = st.get(f"S:{s['id']}")
        s["mean"] = stat["mean"] if stat else None
        s["grades_count"] = stat["count"] if stat else 0
        s["attendance_rate"] = summarize_counts(att.get(s["id"], {}))["rate"]
    return jsonify(students=students)


# ==========================================================================
# Grades
# ==========================================================================
@app.get("/api/grades")
@login_required
def api_grades():
    user = current_user()
    student_id = user["id"] if user["role"] == "student" else request.args.get("student_id", type=int)
    if not student_id:
        raise ApiError("Choose a student.")
    check_student_access(user, student_id)
    sql = ("SELECT g.id, g.value, g.kind, g.comment, g.date, g.quarter, g.subject_id, s.name AS subject_name, "
           "t.full_name AS teacher_name FROM grades g JOIN subjects s ON s.id=g.subject_id "
           "LEFT JOIN users t ON t.id=g.teacher_id WHERE g.student_id=?")
    args = [student_id]
    subject_id = request.args.get("subject_id", type=int)
    if subject_id:
        sql += " AND g.subject_id=?"
        args.append(subject_id)
    quarter = request.args.get("quarter", type=int)
    if quarter in (1, 2, 3, 4):
        sql += " AND g.quarter=?"
        args.append(quarter)
    sql += " ORDER BY g.date DESC, g.id DESC"
    return jsonify(grades=query_all(sql, args))


@app.get("/api/grades/summary")
@login_required
def api_grades_summary():
    user = current_user()
    student_id = user["id"] if user["role"] == "student" else request.args.get("student_id", type=int)
    if not student_id:
        raise ApiError("Choose a student.")
    student = check_student_access(user, student_id)
    return jsonify(student={"id": student["id"], "full_name": student["full_name"], "class_name": student["class_name"]},
                   attendance=attendance_summary(student_id),
                   **student_summary(student_id, student["class_id"]))


def validate_grade_input(data, user, existing=None):
    value = get_int(data, "value", lo=1, hi=5, label="grade")
    kind = str(data.get("kind", "Classwork"))
    if kind not in GRADE_KINDS:
        raise ApiError("Choose a valid grade type.")
    comment = get_text(data, "comment", max_len=200)
    when = get_date(data, "date")
    if when > date.today():
        raise ApiError("A grade cannot be dated in the future.")
    if when < school_year_start():
        raise ApiError("The date is before the start of this school year.")
    return value, kind, comment, when


@app.post("/api/grades")
@roles_required("teacher", "admin")
def api_grades_create():
    user = current_user()
    data = json_body()
    student = get_student(get_int(data, "student_id", label="student"))
    subject_id = get_int(data, "subject_id", label="subject")
    if not student["active"]:
        raise ApiError("This student's account is disabled.")
    assignment = query_one("SELECT teacher_id FROM teaching WHERE class_id=? AND subject_id=?",
                           (student["class_id"], subject_id))
    if not assignment:
        raise ApiError("This subject is not taught in the student's class.")
    if user["role"] == "teacher" and assignment["teacher_id"] != user["id"]:
        raise ApiError("You do not teach this subject in this class.", 403)
    value, kind, comment, when = validate_grade_input(data, user)
    new_id = execute(
        "INSERT INTO grades (student_id, subject_id, teacher_id, value, quarter, kind, comment, date) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (student["id"], subject_id, assignment["teacher_id"], value, quarter_for(when), kind, comment,
         when.isoformat()))
    return jsonify(ok=True, id=new_id), 201


def load_editable_grade(grade_id, user):
    grade = query_one("SELECT g.*, u.class_id FROM grades g JOIN users u ON u.id=g.student_id WHERE g.id=?",
                      (grade_id,))
    if not grade:
        raise ApiError("Grade not found.", 404)
    if user["role"] == "teacher" and not teaches(user["id"], grade["class_id"], grade["subject_id"]):
        raise ApiError("You do not teach this subject in this class.", 403)
    return grade


@app.put("/api/grades/<int:grade_id>")
@roles_required("teacher", "admin")
def api_grades_update(grade_id):
    user = current_user()
    load_editable_grade(grade_id, user)
    value, kind, comment, when = validate_grade_input(json_body(), user)
    execute("UPDATE grades SET value=?, kind=?, comment=?, date=?, quarter=? WHERE id=?",
            (value, kind, comment, when.isoformat(), quarter_for(when), grade_id))
    return jsonify(ok=True)


@app.delete("/api/grades/<int:grade_id>")
@roles_required("teacher", "admin")
def api_grades_delete(grade_id):
    load_editable_grade(grade_id, current_user())
    execute("DELETE FROM grades WHERE id=?", (grade_id,))
    return jsonify(ok=True)


@app.get("/api/gradebook")
@roles_required("teacher", "admin")
def api_gradebook():
    user = current_user()
    class_id = request.args.get("class_id", type=int)
    subject_id = request.args.get("subject_id", type=int)
    quarter = request.args.get("quarter", default=quarter_for(date.today()), type=int)
    if not class_id or not subject_id:
        raise ApiError("Choose a class and a subject.")
    require_class_access(user, class_id, subject_id)
    if not query_one("SELECT id FROM teaching WHERE class_id=? AND subject_id=?", (class_id, subject_id)):
        raise ApiError("This subject is not taught in this class.")
    students = query_all("SELECT id, full_name, username FROM users WHERE role='student' AND active=1 "
                         "AND class_id=? ORDER BY full_name", (class_id,))
    rows = query_all(
        "SELECT g.id, g.student_id, g.value, g.kind, g.comment, g.date, g.quarter FROM grades g "
        "JOIN users u ON u.id=g.student_id WHERE u.class_id=? AND g.subject_id=? AND u.active=1 "
        "ORDER BY g.date, g.id", (class_id, subject_id))
    by_student = {s["id"]: [] for s in students}
    for r in rows:
        by_student.setdefault(r["student_id"], []).append(r)

    if quarter in (1, 2, 3, 4):
        groups = {"ALL": []}
        for sid, grades in by_student.items():
            for g in grades:
                if g["quarter"] == quarter:
                    groups.setdefault(f"S:{sid}", []).append(g["value"])
                    groups["ALL"].append(g["value"])
        st = stats_bridge.compute(groups)
        for s in students:
            s["grades"] = [g for g in by_student[s["id"]] if g["quarter"] == quarter]
            stat = st.get(f"S:{s['id']}")
            s["mean"], s["mark"], s["count"] = (stat["mean"], stat["mark"], stat["count"]) if stat else (None, None, 0)
        return jsonify(mode="quarter", quarter=quarter, students=students, summary=st.get("ALL"),
                       engine=stats_bridge.engine())

    # Whole year: quarter marks first, then the yearly mark = average of quarter marks.
    groups = {"ALL": []}
    for sid, grades in by_student.items():
        for g in grades:
            groups.setdefault(f"S:{sid}:{g['quarter']}", []).append(g["value"])
            groups["ALL"].append(g["value"])
    first = stats_bridge.compute(groups)
    year_groups = {}
    for key, stat in first.items():
        if key.startswith("S:"):
            _, sid, _q = key.split(":")
            year_groups.setdefault(f"Y:{sid}", []).append(stat["mark"])
    year = stats_bridge.compute(year_groups)
    for s in students:
        s["quarters"] = {str(q): first.get(f"S:{s['id']}:{q}") for q in range(1, 5)}
        s["year"] = year.get(f"Y:{s['id']}")
        s["count"] = len(by_student[s["id"]])
    return jsonify(mode="year", quarter=0, students=students, summary=first.get("ALL"),
                   engine=stats_bridge.engine())


# ==========================================================================
# Statistics
# ==========================================================================
def class_attendance_rates():
    rows = query_all("SELECT u.class_id, a.status, COUNT(*) AS n FROM attendance a "
                     "JOIN users u ON u.id=a.student_id WHERE u.active=1 GROUP BY u.class_id, a.status")
    by_class = {}
    for r in rows:
        by_class.setdefault(r["class_id"], {})[r["status"]] = r["n"]
    return {cid: summarize_counts(c) for cid, c in by_class.items()}


@app.get("/api/statistics/class")
@roles_required("teacher", "admin")
def api_statistics_class():
    user = current_user()
    class_id = request.args.get("class_id", type=int)
    subject_id = request.args.get("subject_id", type=int)
    quarter = request.args.get("quarter", default=0, type=int)
    if not class_id:
        raise ApiError("Choose a class.")
    require_class_access(user, class_id, subject_id)
    sql = ("SELECT g.student_id, g.subject_id, g.quarter, g.value FROM grades g "
           "JOIN users u ON u.id=g.student_id WHERE u.class_id=? AND u.active=1")
    args = [class_id]
    if subject_id:
        sql += " AND g.subject_id=?"
        args.append(subject_id)
    elif user["role"] == "teacher":
        allowed = teacher_subject_ids(user["id"], class_id)
        sql += f" AND g.subject_id IN ({placeholders(allowed)})"
        args += allowed
    if quarter in (1, 2, 3, 4):
        sql += " AND g.quarter=?"
        args.append(quarter)
    groups = {}
    for r in query_all(sql, args):
        groups.setdefault("ALL", []).append(r["value"])
        groups.setdefault(f"S:{r['student_id']}", []).append(r["value"])
        groups.setdefault(f"J:{r['subject_id']}", []).append(r["value"])
        groups.setdefault(f"Q:{r['quarter']}", []).append(r["value"])
    st = stats_bridge.compute(groups)
    students = query_all("SELECT id, full_name FROM users WHERE role='student' AND active=1 AND class_id=?", (class_id,))
    ranking = sorted(
        [{"id": s["id"], "name": s["full_name"], "mean": st[f"S:{s['id']}"]["mean"],
          "count": st[f"S:{s['id']}"]["count"]} for s in students if f"S:{s['id']}" in st],
        key=lambda x: (-x["mean"], x["name"]))
    subjects = [{"id": s["id"], "name": s["name"], **{k: st[f"J:{s['id']}"][k] for k in ("mean", "count", "quality_rate", "pass_rate")}}
                for s in query_all("SELECT id, name FROM subjects ORDER BY name") if f"J:{s['id']}" in st]
    quarters = [{"quarter": q, "mean": st[f"Q:{q}"]["mean"], "count": st[f"Q:{q}"]["count"]}
                for q in range(1, 5) if f"Q:{q}" in st]
    return jsonify(summary=st.get("ALL"), ranking=ranking, subjects=subjects, quarters=quarters,
                   attendance=class_attendance_rates().get(class_id, summarize_counts({})),
                   students=len(students), engine=stats_bridge.engine())


@app.get("/api/statistics/school")
@roles_required("admin")
def api_statistics_school():
    quarter = request.args.get("quarter", default=0, type=int)
    sql = ("SELECT u.class_id, g.subject_id, g.quarter, g.value FROM grades g "
           "JOIN users u ON u.id=g.student_id WHERE u.active=1")
    args = []
    if quarter in (1, 2, 3, 4):
        sql += " AND g.quarter=?"
        args.append(quarter)
    groups = {}
    for r in query_all(sql, args):
        groups.setdefault("ALL", []).append(r["value"])
        groups.setdefault(f"C:{r['class_id']}", []).append(r["value"])
        groups.setdefault(f"J:{r['subject_id']}", []).append(r["value"])
        groups.setdefault(f"Q:{r['quarter']}", []).append(r["value"])
    st = stats_bridge.compute(groups)
    attendance = class_attendance_rates()
    classes = []
    for c in query_all("SELECT c.id, c.name, (SELECT COUNT(*) FROM users u WHERE u.class_id=c.id AND u.role='student' "
                       "AND u.active=1) AS students FROM classes c ORDER BY c.name"):
        stat = st.get(f"C:{c['id']}") or {}
        classes.append({**c, "mean": stat.get("mean"), "count": stat.get("count", 0),
                        "quality_rate": stat.get("quality_rate"), "pass_rate": stat.get("pass_rate"),
                        "attendance_rate": attendance.get(c["id"], {}).get("rate")})
    subjects = [{"id": s["id"], "name": s["name"], **{k: st[f"J:{s['id']}"][k] for k in ("mean", "count", "quality_rate", "pass_rate")}}
                for s in query_all("SELECT id, name FROM subjects ORDER BY name") if f"J:{s['id']}" in st]
    quarters = [{"quarter": q, "mean": st[f"Q:{q}"]["mean"], "count": st[f"Q:{q}"]["count"]}
                for q in range(1, 5) if f"Q:{q}" in st]
    totals = summarize_counts({})
    for rates in attendance.values():
        for k in ("present", "late", "excused", "absent"):
            totals[k] += rates[k]
    totals = summarize_counts(totals)
    return jsonify(summary=st.get("ALL"), classes=classes, subjects=subjects, quarters=quarters,
                   attendance=totals, engine=stats_bridge.engine())


# ==========================================================================
# Attendance
# ==========================================================================
def month_bounds(month):
    try:
        first = datetime.strptime(month, "%Y-%m").date()
    except (TypeError, ValueError):
        first = date.today().replace(day=1)
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, nxt


@app.get("/api/attendance/student")
@login_required
def api_attendance_student():
    user = current_user()
    student_id = user["id"] if user["role"] == "student" else request.args.get("student_id", type=int)
    if not student_id:
        raise ApiError("Choose a student.")
    check_student_access(user, student_id)
    first, nxt = month_bounds(request.args.get("month", ""))
    records = query_all("SELECT date, status, note FROM attendance WHERE student_id=? AND date>=? AND date<? "
                        "ORDER BY date", (student_id, first.isoformat(), nxt.isoformat()))
    monthly = {}
    for r in query_all("SELECT substr(date,1,7) AS month, status, COUNT(*) AS n FROM attendance WHERE student_id=? "
                       "GROUP BY month, status ORDER BY month", (student_id,)):
        monthly.setdefault(r["month"], {})[r["status"]] = r["n"];
    by_month = [{"month": m, **summarize_counts(c)} for m, c in sorted(monthly.items())]
    return jsonify(month=first.strftime("%Y-%m"), records=records, summary=attendance_summary(student_id),
                   by_month=by_month)


@app.get("/api/attendance/class")
@roles_required("teacher", "admin")
def api_attendance_class():
    user = current_user()
    class_id = request.args.get("class_id", type=int)
    if not class_id:
        raise ApiError("Choose a class.")
    require_class_access(user, class_id)
    try:
        when = date.fromisoformat(request.args.get("date", date.today().isoformat()))
    except ValueError:
        raise ApiError("Choose a valid date.")
    students = query_all(
        "SELECT u.id, u.full_name, a.status, a.note FROM users u LEFT JOIN attendance a "
        "ON a.student_id=u.id AND a.date=? WHERE u.role='student' AND u.active=1 AND u.class_id=? "
        "ORDER BY u.full_name", (when.isoformat(), class_id))
    return jsonify(date=when.isoformat(), students=students, is_school_day=when.isoweekday() <= 5,
                   marked=any(s["status"] for s in students))


@app.post("/api/attendance/class")
@roles_required("teacher", "admin")
def api_attendance_save():
    user = current_user()
    data = json_body()
    class_id = get_int(data, "class_id", label="class")
    require_class_access(user, class_id)
    when = get_date(data, "date")
    if when > date.today():
        raise ApiError("You cannot mark attendance for a future date.")
    if when.isoweekday() > 5:
        raise ApiError("School is closed on weekends.")
    records = data.get("records")
    if not isinstance(records, list) or not records:
        raise ApiError("There is no attendance to save.")
    valid_ids = {r["id"] for r in query_all("SELECT id FROM users WHERE role='student' AND active=1 AND class_id=?",
                                            (class_id,))}
    db = dbm.get_db()
    saved = 0
    for rec in records:
        if not isinstance(rec, dict):
            raise ApiError("Invalid attendance record.")
        sid = get_int(rec, "student_id", label="student")
        status = str(rec.get("status", ""))
        if sid not in valid_ids:
            raise ApiError("A student in the list is not in this class.")
        if status not in ATTENDANCE_STATUSES:
            raise ApiError("Invalid attendance status.")
        note = get_text(rec, "note", max_len=200)
        db.execute(
            "INSERT INTO attendance (student_id, date, status, note, marked_by) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(student_id, date) DO UPDATE SET status=excluded.status, note=excluded.note, "
            "marked_by=excluded.marked_by",
            (sid, when.isoformat(), status, note, user["id"]))
        saved += 1
    db.commit()
    return jsonify(ok=True, saved=saved)


# ==========================================================================
# Timetable
# ==========================================================================
@app.get("/api/timetable")
@login_required
def api_timetable():
    user = current_user()
    class_id = request.args.get("class_id", type=int)
    mine = request.args.get("mine")
    sql = ("SELECT tt.id, tt.class_id, c.name AS class_name, tt.weekday, tt.lesson_no, tt.subject_id, "
           "s.name AS subject_name, tt.teacher_id, u.full_name AS teacher_name, tt.room FROM timetable tt "
           "JOIN classes c ON c.id=tt.class_id JOIN subjects s ON s.id=tt.subject_id "
           "LEFT JOIN users u ON u.id=tt.teacher_id")
    if user["role"] == "student":
        entries = query_all(sql + " WHERE tt.class_id=? ORDER BY tt.weekday, tt.lesson_no", (user["class_id"],))
        title = f"Class {user['class_name']}"
    elif mine or (user["role"] == "teacher" and not class_id):
        if user["role"] != "teacher":
            raise ApiError("Choose a class.")
        entries = query_all(sql + " WHERE tt.teacher_id=? ORDER BY tt.weekday, tt.lesson_no", (user["id"],))
        title = "My schedule"
    else:
        if not class_id:
            raise ApiError("Choose a class.")
        cls = query_one("SELECT name FROM classes WHERE id=?", (class_id,))
        if not cls:
            raise ApiError("Class not found.", 404)
        entries = query_all(sql + " WHERE tt.class_id=? ORDER BY tt.weekday, tt.lesson_no", (class_id,))
        title = f"Class {cls['name']}"
    return jsonify(entries=entries, title=title, mode="teacher" if (mine or (user["role"] == "teacher" and not class_id)) else "class")


@app.post("/api/timetable")
@roles_required("admin")
def api_timetable_set():
    data = json_body()
    class_id = get_int(data, "class_id", label="class")
    weekday = get_int(data, "weekday", lo=1, hi=5)
    lesson_no = get_int(data, "lesson_no", lo=1, hi=MAX_LESSONS, label="lesson")
    subject_id = get_int(data, "subject_id", label="subject")
    room = get_text(data, "room", max_len=20)
    assignment = query_one("SELECT teacher_id FROM teaching WHERE class_id=? AND subject_id=?", (class_id, subject_id))
    if not assignment:
        raise ApiError("No teacher is assigned to this subject in this class. Add the assignment in School setup first.")
    clash = query_one(
        "SELECT c.name FROM timetable tt JOIN classes c ON c.id=tt.class_id WHERE tt.teacher_id=? AND tt.weekday=? "
        "AND tt.lesson_no=? AND tt.class_id<>?", (assignment["teacher_id"], weekday, lesson_no, class_id))
    if clash:
        raise ApiError(f"That teacher already teaches class {clash['name']} at this time.", 409)
    existing = query_one("SELECT id FROM timetable WHERE class_id=? AND weekday=? AND lesson_no=?",
                         (class_id, weekday, lesson_no))
    if existing:
        execute("UPDATE timetable SET subject_id=?, teacher_id=?, room=? WHERE id=?",
                (subject_id, assignment["teacher_id"], room, existing["id"]))
    else:
        execute("INSERT INTO timetable (class_id, weekday, lesson_no, subject_id, teacher_id, room) "
                "VALUES (?, ?, ?, ?, ?, ?)", (class_id, weekday, lesson_no, subject_id, assignment["teacher_id"], room))
    return jsonify(ok=True)


@app.delete("/api/timetable/<int:entry_id>")
@roles_required("admin")
def api_timetable_delete(entry_id):
    if not query_one("SELECT id FROM timetable WHERE id=?", (entry_id,)):
        raise ApiError("Lesson not found.", 404)
    execute("DELETE FROM timetable WHERE id=?", (entry_id,))
    return jsonify(ok=True)


# ==========================================================================
# Homework
# ==========================================================================
HOMEWORK_SELECT = (
    "SELECT h.id, h.class_id, c.name AS class_name, h.subject_id, s.name AS subject_name, h.teacher_id, "
    "u.full_name AS teacher_name, h.title, h.description, h.due_date, h.created_at, "
    "(SELECT COUNT(*) FROM users st WHERE st.class_id=h.class_id AND st.role='student' AND st.active=1) AS total_students, "
    "(SELECT COUNT(*) FROM homework_status hs JOIN users st ON st.id=hs.student_id "
    " WHERE hs.homework_id=h.id AND hs.done=1 AND st.active=1) AS done_count")


@app.get("/api/homework")
@login_required
def api_homework():
    user = current_user()
    if user["role"] == "student":
        sql = (HOMEWORK_SELECT + ", COALESCE((SELECT done FROM homework_status WHERE homework_id=h.id AND student_id=?), 0) AS done "
               "FROM homework h JOIN classes c ON c.id=h.class_id JOIN subjects s ON s.id=h.subject_id "
               "LEFT JOIN users u ON u.id=h.teacher_id WHERE h.class_id=? ORDER BY h.due_date DESC, h.id DESC")
        items = query_all(sql, (user["id"], user["class_id"]))
    else:
        sql = (HOMEWORK_SELECT + " FROM homework h JOIN classes c ON c.id=h.class_id JOIN subjects s ON s.id=h.subject_id "
               "LEFT JOIN users u ON u.id=h.teacher_id WHERE 1=1")
        args = []
        if user["role"] == "teacher":
            sql += " AND h.teacher_id=?"
            args.append(user["id"])
        class_id = request.args.get("class_id", type=int)
        if class_id:
            sql += " AND h.class_id=?"
            args.append(class_id)
        items = query_all(sql + " ORDER BY h.due_date DESC, h.id DESC", args)
    return jsonify(homework=items, today=date.today().isoformat())


def validate_homework(data, user):
    class_id = get_int(data, "class_id", label="class")
    subject_id = get_int(data, "subject_id", label="subject")
    assignment = query_one("SELECT teacher_id FROM teaching WHERE class_id=? AND subject_id=?", (class_id, subject_id))
    if not assignment:
        raise ApiError("This subject is not taught in that class.")
    if user["role"] == "teacher" and assignment["teacher_id"] != user["id"]:
        raise ApiError("You do not teach this subject in this class.", 403)
    title = get_text(data, "title", min_len=3, max_len=120, required=True)
    description = get_text(data, "description", max_len=1500)
    due = get_date(data, "due_date", label="due date")
    return class_id, subject_id, assignment["teacher_id"], title, description, due


def load_editable_homework(homework_id, user):
    hw = query_one("SELECT * FROM homework WHERE id=?", (homework_id,))
    if not hw:
        raise ApiError("Homework not found.", 404)
    if user["role"] == "teacher" and hw["teacher_id"] != user["id"]:
        raise ApiError("You can only change your own homework.", 403)
    return hw


@app.post("/api/homework")
@roles_required("teacher", "admin")
def api_homework_create():
    user = current_user()
    class_id, subject_id, teacher_id, title, description, due = validate_homework(json_body(), user)
    new_id = execute(
        "INSERT INTO homework (class_id, subject_id, teacher_id, title, description, due_date) VALUES (?, ?, ?, ?, ?, ?)",
        (class_id, subject_id, teacher_id, title, description, due.isoformat()))
    return jsonify(ok=True, id=new_id), 201


@app.put("/api/homework/<int:homework_id>")
@roles_required("teacher", "admin")
def api_homework_update(homework_id):
    user = current_user()
    load_editable_homework(homework_id, user)
    class_id, subject_id, teacher_id, title, description, due = validate_homework(json_body(), user)
    execute("UPDATE homework SET class_id=?, subject_id=?, teacher_id=?, title=?, description=?, due_date=? WHERE id=?",
            (class_id, subject_id, teacher_id, title, description, due.isoformat(), homework_id))
    return jsonify(ok=True)


@app.delete("/api/homework/<int:homework_id>")
@roles_required("teacher", "admin")
def api_homework_delete(homework_id):
    load_editable_homework(homework_id, current_user())
    execute("DELETE FROM homework WHERE id=?", (homework_id,))
    return jsonify(ok=True)


@app.post("/api/homework/<int:homework_id>/done")
@roles_required("student")
def api_homework_done(homework_id):
    user = current_user()
    hw = query_one("SELECT * FROM homework WHERE id=?", (homework_id,))
    if not hw or hw["class_id"] != user["class_id"]:
        raise ApiError("Homework not found.", 404)
    done = 1 if json_body().get("done") else 0
    execute(
        "INSERT INTO homework_status (homework_id, student_id, done, done_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(homework_id, student_id) DO UPDATE SET done=excluded.done, done_at=excluded.done_at",
        (homework_id, user["id"], done, datetime.now().strftime("%Y-%m-%d %H:%M:%S") if done else None))
    return jsonify(ok=True, done=bool(done))


@app.get("/api/homework/<int:homework_id>/progress")
@roles_required("teacher", "admin")
def api_homework_progress(homework_id):
    user = current_user()
    hw = load_editable_homework(homework_id, user) if user["role"] == "teacher" else query_one(
        "SELECT * FROM homework WHERE id=?", (homework_id,))
    if not hw:
        raise ApiError("Homework not found.", 404)
    students = query_all(
        "SELECT u.id, u.full_name, COALESCE(hs.done, 0) AS done, hs.done_at FROM users u "
        "LEFT JOIN homework_status hs ON hs.student_id=u.id AND hs.homework_id=? "
        "WHERE u.role='student' AND u.active=1 AND u.class_id=? ORDER BY u.full_name", (homework_id, hw["class_id"]))
    return jsonify(students=students, title=hw["title"])


# ==========================================================================
# Announcements
# ==========================================================================
def validate_announcement(data, user):
    title = get_text(data, "title", min_len=3, max_len=120, required=True)
    body = get_text(data, "body", min_len=3, max_len=3000, required=True)
    audience = str(data.get("audience", "all"))
    allowed = ("all", "students", "teachers") if user["role"] == "admin" else ("all", "students")
    if audience not in allowed:
        raise ApiError("You cannot post to that audience.")
    pinned = 1 if (user["role"] == "admin" and data.get("pinned")) else 0
    return title, body, audience, pinned


def load_editable_announcement(announcement_id, user):
    item = query_one("SELECT * FROM announcements WHERE id=?", (announcement_id,))
    if not item:
        raise ApiError("Announcement not found.", 404)
    if user["role"] == "teacher" and item["author_id"] != user["id"]:
        raise ApiError("You can only change your own announcements.", 403)
    return item


@app.get("/api/announcements")
@login_required
def api_announcements():
    return jsonify(announcements=announcements_for(current_user()))


@app.post("/api/announcements")
@roles_required("teacher", "admin")
def api_announcements_create():
    user = current_user()
    title, body, audience, pinned = validate_announcement(json_body(), user)
    new_id = execute("INSERT INTO announcements (title, body, audience, pinned, author_id, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     (title, body, audience, pinned, user["id"], datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    return jsonify(ok=True, id=new_id), 201


@app.put("/api/announcements/<int:announcement_id>")
@roles_required("teacher", "admin")
def api_announcements_update(announcement_id):
    user = current_user()
    load_editable_announcement(announcement_id, user)
    title, body, audience, pinned = validate_announcement(json_body(), user)
    if user["role"] == "teacher":
        execute("UPDATE announcements SET title=?, body=?, audience=? WHERE id=?",
                (title, body, audience, announcement_id))
    else:
        execute("UPDATE announcements SET title=?, body=?, audience=?, pinned=? WHERE id=?",
                (title, body, audience, pinned, announcement_id))
    return jsonify(ok=True)


@app.delete("/api/announcements/<int:announcement_id>")
@roles_required("teacher", "admin")
def api_announcements_delete(announcement_id):
    load_editable_announcement(announcement_id, current_user())
    execute("DELETE FROM announcements WHERE id=?", (announcement_id,))
    return jsonify(ok=True)


# ==========================================================================
# Start-up
# ==========================================================================
def bootstrap():
    dbm.init_db()
    stats_bridge.build()


bootstrap()

if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("WMAKTAB_DEBUG") == "1",
    )
    