"""Database for The Unfocus Group. SQLite, opened fresh per request.

Brands are not a table of their own. A brand "exists" the moment someone
leaves feedback for it, the same way a subreddit fills up from posts. We
keep a normalized slug on each row so different spellings of the same brand
("Acme Coffee", "acme coffee") land on the same page.
"""

import re
import sqlite3
from pathlib import Path

DB_FILE = Path(__file__).resolve().parent / "feedback.db"


def connect():
    """Open a connection where rows behave like dictionaries."""
    db = sqlite3.connect(DB_FILE)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def make_slug(brand_name):
    """Turn a brand name into a clean, lowercase URL piece."""
    slug = re.sub(r"[^a-z0-9]+", "-", brand_name.strip().lower())
    return slug.strip("-")


def set_up_database():
    """Create every table the first time the app runs."""
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT    NOT NULL UNIQUE,
                password_hash TEXT    NOT NULL,
                created_at    TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token      TEXT    PRIMARY KEY,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS feedback (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                brand_name    TEXT    NOT NULL,
                brand_slug    TEXT    NOT NULL,
                rating        INTEGER NOT NULL,
                feedback_text TEXT    NOT NULL,
                timestamp     TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS votes (
                user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                feedback_id INTEGER NOT NULL REFERENCES feedback(id) ON DELETE CASCADE,
                value       INTEGER NOT NULL,
                PRIMARY KEY (user_id, feedback_id)
            );

            CREATE TABLE IF NOT EXISTS comments (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                feedback_id  INTEGER NOT NULL REFERENCES feedback(id) ON DELETE CASCADE,
                user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                comment_text TEXT    NOT NULL,
                timestamp    TEXT    NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_feedback_brand ON feedback(brand_slug);
            CREATE INDEX IF NOT EXISTS idx_votes_feedback ON votes(feedback_id);
            CREATE INDEX IF NOT EXISTS idx_comments_feedback ON comments(feedback_id);
            """
        )
