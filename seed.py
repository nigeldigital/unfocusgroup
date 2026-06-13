"""Fill the board with demo feedback for ten well-known brands.

Safe to run more than once: it skips any brand that's already on the board,
and reuses seed users instead of making duplicates. The database itself isn't
committed, so this is how you get a populated board after a fresh clone.

    ./venv/bin/python seed.py
"""

from datetime import datetime, timedelta

from app.auth import create_user, find_user
from app.db import connect, make_slug, set_up_database


def get_or_make_user(username):
    """Return a seed user's id, creating the account if it's new."""
    row = find_user(username)
    if row:
        return row["id"]
    return create_user(username, "demo-password")


# Each line: who said it, the brand, a 1-5 rating, and the one thing they'd change.
DEMO_FEEDBACK = [
    ("mara",  "Apple",      4, "Stop soldering the storage in. Let me upgrade it myself."),
    ("devin", "Nike",       3, "Bring back the wide sizes you quietly dropped."),
    ("priya", "Coca-Cola",  3, "Make the zero-sugar one taste like the real thing, without the aftertaste."),
    ("tomas", "Amazon",     2, "Fix the fake reviews. I can't trust the star ratings anymore."),
    ("lena",  "Google",     3, "Stop killing products people depend on. Commit to what you launch."),
    ("mara",  "Samsung",    4, "Cut the duplicate pre-installed apps. I don't need two of everything."),
    ("devin", "McDonald's", 2, "Just fix the ice cream machines. That's the whole feedback."),
    ("priya", "Netflix",    3, "Stop cancelling shows after one season before they find an audience."),
    ("tomas", "Starbucks",  3, "Balance the mobile orders so the in-store line isn't always last."),
    ("lena",  "Spotify",    4, "Pay artists more. The per-stream rate is insulting."),
    ("mara",  "Tesla",      3, "Bring back physical buttons for the wipers and climate."),
    ("devin", "Adidas",     4, "Stop the constant app-only drops. Let me just buy the shoes."),
    ("priya", "Microsoft",  2, "Quit forcing OS updates that reboot mid-work."),
    ("tomas", "Uber",       3, "Show the real price up front, not after I book."),
    ("lena",  "Airbnb",     2, "Ban the surprise cleaning fees. Put it all in the nightly rate."),
]


def run():
    set_up_database()
    added = 0
    # Spread the timestamps across the last ten days so the feed looks lived-in.
    base_time = datetime.now()

    # Make the seed users up front, one connection at a time, so we never open
    # a second connection while the feedback transaction below is in flight.
    user_ids = {name: get_or_make_user(name) for name in {row[0] for row in DEMO_FEEDBACK}}

    with connect() as db:
        for offset, (username, brand_name, rating, feedback_text) in enumerate(DEMO_FEEDBACK):
            slug = make_slug(brand_name)
            already_here = db.execute(
                "SELECT 1 FROM feedback WHERE brand_slug = ? AND feedback_text = ?",
                (slug, feedback_text),
            ).fetchone()
            if already_here:
                continue

            user_id = user_ids[username]
            timestamp = (base_time - timedelta(days=offset, hours=offset)).isoformat(timespec="seconds")
            db.execute(
                """
                INSERT INTO feedback (user_id, brand_name, brand_slug, rating, feedback_text, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, brand_name, slug, rating, feedback_text, timestamp),
            )
            added += 1

    print(f"Added {added} pieces of feedback ({len(DEMO_FEEDBACK) - added} already on the board).")


if __name__ == "__main__":
    run()
