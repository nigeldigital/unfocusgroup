# The Unfocused Group

The anti-focus group: a public, Reddit-style board where anyone can leave honest
feedback for any brand. No moderator, no agenda, no one-way glass. Just honest
feedback for every brand, in one place.

## What it does

- **Public feed** of all feedback, sortable by **New** or **Top**
- **Brand pages** (`/b/{brand}`): a brand becomes a community the moment someone posts about it
- **Upvote / downvote** on every piece of feedback (toggleable, one vote per person)
- **Comments** on each feedback detail page
- **Accounts**: sign up, log in, and post under your name

## Stack

- **FastAPI** + **Jinja2** for server-rendered pages
- **SQLite** for storage (created automatically on first run)
- **Tailwind CSS** via CDN for styling
- Stdlib **PBKDF2** password hashing + a server-side session cookie, no auth library
- Only voting uses JavaScript; everything else works as plain HTML forms

## Project layout

```
app/
  main.py            Routes: feed, brand pages, submit, vote, comment, auth
  db.py              SQLite schema and connection
  auth.py            Password hashing + session helpers
  templates/         base, feed, detail, submit, login, signup
  static/            style.css, vote.js
requirements.txt
```

## Run it locally

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000.

## Notes

Tailwind is loaded from the CDN for fast iteration. Swap to a compiled build
before production. The session cookie isn't `Secure`-flagged yet since local
development runs over HTTP; that flips on behind HTTPS.
