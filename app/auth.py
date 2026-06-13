"""Sign-up, login, and 'who is this?' helpers.

Passwords are hashed with PBKDF2 from the standard library, so there is no
extra dependency to install. Logged-in users are tracked with a random
token stored in an httponly cookie and matched against the sessions table.
"""

import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime

from .db import connect

COOKIE_NAME = "session"
# How many rounds of hashing: higher is slower for an attacker.
HASH_ROUNDS = 200_000

# Password rules. Length does the heavy lifting; the rest stops the easy guesses.
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 128  # cap the work an attacker can force us to do per hash

# The passwords attackers try first. Anything here is cracked in seconds no matter
# how long it is, so we turn it away outright.
COMMON_PASSWORDS = {
    "password", "password1", "password123", "passw0rd", "p@ssw0rd",
    "123456", "1234567", "12345678", "123456789", "1234567890",
    "qwerty", "qwertyuiop", "qwerty123", "abc123", "a1b2c3d4",
    "111111", "000000", "iloveyou", "admin123", "welcome1",
    "letmein", "monkey123", "dragon123", "sunshine", "princess1",
    "trustno1", "starwars", "football1", "baseball1", "changeme",
    "secret123", "unfocus", "unfocusgroup", "theunfocusgroup",
}


def password_problem(password, username=""):
    """Return a plain-language reason the password is too weak, or None if it's fine.

    The aim is real resistance to guessing and credential-stuffing, not box-ticking:
    decent length, a mix of character types, nothing already on every attacker's
    wordlist, and nothing built out of the person's own username.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters. Longer is stronger."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Keep it under {MAX_PASSWORD_LENGTH} characters."

    kinds = sum([
        bool(re.search(r"[a-z]", password)),
        bool(re.search(r"[A-Z]", password)),
        bool(re.search(r"[0-9]", password)),
        bool(re.search(r"[^A-Za-z0-9]", password)),
    ])
    if kinds < 3:
        return "Mix at least three of: lowercase, uppercase, numbers, and symbols."

    if len(set(password)) < 5:
        return "Use a wider variety of characters, not the same few repeated."

    lowered = password.lower()
    if lowered in COMMON_PASSWORDS:
        return "That password is too common. Pick something less guessable."

    if username and len(username) >= 3 and username.lower() in lowered:
        return "Don't build your password out of your username."

    return None


def hash_password(password):
    """Scramble a password into a salted hash we can safely store."""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, HASH_ROUNDS)
    return f"{salt.hex()}${digest.hex()}"


def password_matches(password, stored):
    """Check a typed password against the stored hash, in constant time."""
    try:
        salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    salt = bytes.fromhex(salt_hex)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, HASH_ROUNDS)
    return hmac.compare_digest(digest.hex(), digest_hex)


def create_user(username, password):
    """Add a new user. Returns the new id, or None if the name is taken."""
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as db:
        try:
            cursor = db.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, hash_password(password), now),
            )
            return cursor.lastrowid
        except Exception:
            return None


def find_user(username):
    """Look up a user row by name, or None."""
    with connect() as db:
        return db.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()


def start_session(user_id):
    """Hand out a fresh session token for a user who just logged in."""
    token = secrets.token_urlsafe(32)
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as db:
        db.execute(
            "INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
            (token, user_id, now),
        )
    return token


def end_session(token):
    """Forget a session token when someone logs out."""
    if not token:
        return
    with connect() as db:
        db.execute("DELETE FROM sessions WHERE token = ?", (token,))


def current_user(request):
    """Return the logged-in user for this request, or None."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    with connect() as db:
        return db.execute(
            """
            SELECT users.* FROM users
            JOIN sessions ON sessions.user_id = users.id
            WHERE sessions.token = ?
            """,
            (token,),
        ).fetchone()
