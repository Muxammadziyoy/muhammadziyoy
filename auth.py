"""wMaktab - authentication, sessions, CSRF protection and role-based permissions."""
import functools
import hmac
import os
import secrets
import time

from flask import g, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from database import BASE_DIR, query_one

ROLES = ("student", "teacher", "admin")
MIN_PASSWORD_LENGTH = 8
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 60

# In-memory brute-force protection: (username, ip) -> [failure count, locked-until timestamp]
_failures = {}
# Used to keep timing similar when the username does not exist.
_DUMMY_HASH = generate_password_hash("wmaktab-dummy-password")


# --------------------------------------------------------------------------
# Secret key
# --------------------------------------------------------------------------
def load_secret_key():
    """Use WMAKTAB_SECRET_KEY if set, otherwise create and reuse instance/secret.key."""
    env_key = os.environ.get("WMAKTAB_SECRET_KEY")
    if env_key:
        return env_key
    path = os.path.join(BASE_DIR, "instance", "secret.key")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            key = fh.read().strip()
            if key:
                return key
    key = secrets.token_hex(32)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------
def hash_password(password):
    return generate_password_hash(password)


def password_problem(password):
    """Return a message if the password is not acceptable, otherwise None."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters long."
    if len(password) > 128:
        return "Password is too long (maximum 128 characters)."
    return None


# --------------------------------------------------------------------------
# Login / logout / current user
# --------------------------------------------------------------------------
def _failure_key(username):
    return (username.lower(), request.remote_addr or "?")


def attempt_login(username, password):
    """Check credentials against the database.

    Returns (user_row, error_message, http_status).
    """
    key = _failure_key(username)
    count, locked_until = _failures.get(key, (0, 0.0))
    now = time.time()
    if locked_until > now:
        wait = int(locked_until - now) + 1
        return None, f"Too many failed attempts. Try again in {wait} seconds.", 429

    user = query_one("SELECT * FROM users WHERE username = ?", (username,)) if username else None
    password_ok = check_password_hash(user["password_hash"] if user else _DUMMY_HASH, password or "")

    if not user or not password_ok:
        count += 1
        locked_until = now + LOCKOUT_SECONDS if count >= MAX_FAILED_ATTEMPTS else 0.0
        if count >= MAX_FAILED_ATTEMPTS:
            count = 0
        _failures[key] = (count, locked_until)
        return None, "Incorrect username or password.", 401

    if not user["active"]:
        return None, "This account has been disabled. Please contact the school administrator.", 403

    _failures.pop(key, None)
    session.clear()
    session.permanent = True
    session["uid"] = user["id"]
    session["csrf"] = secrets.token_urlsafe(32)
    return user, None, 200


def logout_user():
    session.clear()


def current_user():
    """The logged-in user (dict with class_name) or None. Cached per request."""
    if "_user" in g:
        return g._user
    user = None
    uid = session.get("uid")
    if uid:
        user = query_one(
            "SELECT u.*, c.name AS class_name FROM users u "
            "LEFT JOIN classes c ON c.id = u.class_id WHERE u.id = ?",
            (uid,),
        )
        if user and not user["active"]:
            user = None
    g._user = user
    return user


# --------------------------------------------------------------------------
# CSRF
# --------------------------------------------------------------------------
def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def verify_csrf():
    sent = request.headers.get("X-CSRF-Token", "")
    expected = session.get("csrf", "")
    return bool(expected) and hmac.compare_digest(sent, expected)


# --------------------------------------------------------------------------
# Decorators (role-based permissions)
# --------------------------------------------------------------------------
def login_required(view):
    """API endpoints: answer 401 JSON when not logged in."""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user():
            return jsonify(error="Please log in to continue."), 401
        return view(*args, **kwargs)
    return wrapper


def roles_required(*roles):
    """API endpoints: only the listed roles may call them (401/403 JSON otherwise)."""
    def decorator(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user:
                return jsonify(error="Please log in to continue."), 401
            if user["role"] not in roles:
                return jsonify(error="You do not have permission to do that."), 403
            return view(*args, **kwargs)
        return wrapper
    return decorator


def page_login_required(view):
    """HTML pages: redirect to the login page when not logged in."""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login_page"))
        return view(*args, **kwargs)
    return wrapper
