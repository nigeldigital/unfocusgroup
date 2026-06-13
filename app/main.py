"""The Unfocus Group: a public board where anyone can leave honest feedback
for any brand, vote on what others wrote, and talk it through.

Pages are plain server-rendered HTML. Only voting talks back to the server
with a small fetch call, so the rest works without any JavaScript.
"""

from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, mailer
from .db import connect, make_slug, set_up_database

APP_DIR = Path(__file__).resolve().parent

app = FastAPI(title="The Unfocus Group")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


@app.on_event("startup")
def on_startup():
    set_up_database()


# --- Shared bits ---------------------------------------------------------

def page(request, name, user, **values):
    """Render a template, always handing it the current user."""
    return templates.TemplateResponse(name, {"request": request, "user": user, **values})


def feedback_query(where=""):
    """Build the standard feedback query with score, comment count, and the
    viewer's own vote. `where` adds an optional filter clause."""
    return f"""
        SELECT
            f.id, f.brand_name, f.brand_slug, f.rating, f.feedback_text, f.timestamp,
            u.username AS author,
            COALESCE((SELECT SUM(value) FROM votes WHERE feedback_id = f.id), 0) AS score,
            (SELECT COUNT(*) FROM comments WHERE feedback_id = f.id) AS comment_count,
            COALESCE((SELECT value FROM votes WHERE feedback_id = f.id AND user_id = ?), 0) AS my_vote,
            CASE WHEN EXISTS(SELECT 1 FROM saves WHERE feedback_id = f.id AND user_id = ?)
                 THEN 1 ELSE 0 END AS saved
        FROM feedback f
        JOIN users u ON u.id = f.user_id
        {where}
    """


def sort_clause(sort):
    """Top means most agreed-with; new means most recent."""
    return "ORDER BY score DESC, f.id DESC" if sort == "top" else "ORDER BY f.id DESC"


PER_PAGE = 15

# How many community flags before a post is marked as under review.
FLAG_THRESHOLD = 2


def page_window(sort, page_no):
    """SQL tail for one page of results. Fetches one extra row so the caller
    can tell whether a further page exists, without a separate COUNT."""
    offset = (page_no - 1) * PER_PAGE
    return f"{sort_clause(sort)} LIMIT ? OFFSET ?", (PER_PAGE + 1, offset)


# --- The feed and brand pages -------------------------------------------

@app.get("/", response_class=HTMLResponse)
def home(request: Request, sort: str = "new", page_no: int = Query(1, alias="page", ge=1)):
    user = auth.current_user(request)
    viewer_id = user["id"] if user else 0
    tail, tail_params = page_window(sort, page_no)
    with connect() as db:
        rows = db.execute(feedback_query() + tail, (viewer_id, viewer_id, *tail_params)).fetchall()
        brands = db.execute(
            """
            SELECT brand_slug, brand_name, COUNT(*) AS count
            FROM feedback GROUP BY brand_slug
            ORDER BY count DESC, brand_name LIMIT 12
            """
        ).fetchall()
    return page(request, "feed.html", user, posts=rows[:PER_PAGE], brands=brands,
                sort=sort, heading="All feedback", brand=None,
                page_no=page_no, has_next=len(rows) > PER_PAGE)


@app.get("/b/{brand_slug}", response_class=HTMLResponse)
def brand_page(request: Request, brand_slug: str, sort: str = "new",
               page_no: int = Query(1, alias="page", ge=1)):
    user = auth.current_user(request)
    viewer_id = user["id"] if user else 0
    tail, tail_params = page_window(sort, page_no)
    with connect() as db:
        rows = db.execute(
            feedback_query("WHERE f.brand_slug = ?") + tail,
            (viewer_id, viewer_id, brand_slug, *tail_params),
        ).fetchall()
        brands = db.execute(
            """
            SELECT brand_slug, brand_name, COUNT(*) AS count
            FROM feedback GROUP BY brand_slug
            ORDER BY count DESC, brand_name LIMIT 12
            """
        ).fetchall()
        claim = db.execute(
            """
            SELECT bc.brand_slug, bc.user_id, bc.response, bc.verified,
                   u.username AS owner
            FROM brand_claims bc
            JOIN users u ON u.id = bc.user_id
            WHERE bc.brand_slug = ?
            """,
            (brand_slug,),
        ).fetchone()
        stats = db.execute(
            """
            SELECT COUNT(*) AS n, AVG(rating) AS avg,
                   SUM(rating = 5) AS s5, SUM(rating = 4) AS s4,
                   SUM(rating = 3) AS s3, SUM(rating = 2) AS s2,
                   SUM(rating = 1) AS s1
            FROM feedback WHERE brand_slug = ?
            """,
            (brand_slug,),
        ).fetchone()
    posts = rows[:PER_PAGE]
    heading = posts[0]["brand_name"] if posts else brand_slug
    is_owner = bool(user and claim and claim["user_id"] == user["id"])
    return page(request, "feed.html", user, posts=posts, brands=brands,
                sort=sort, heading=heading, brand=brand_slug,
                claim=claim, is_owner=is_owner, rating_stats=stats,
                page_no=page_no, has_next=len(rows) > PER_PAGE)


@app.post("/b/{brand_slug}/claim")
def claim_brand(request: Request, brand_slug: str):
    """Let a logged-in user claim an as-yet-unclaimed brand. (A production
    version would verify real ownership, e.g. via a company email domain;
    here a claim is granted directly and flagged verified for the demo.)"""
    user = auth.current_user(request)
    if user is None:
        return RedirectResponse(f"/login?next=/b/{brand_slug}", status_code=303)
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as db:
        brand_exists = db.execute(
            "SELECT 1 FROM feedback WHERE brand_slug = ? LIMIT 1", (brand_slug,)
        ).fetchone()
        already_claimed = db.execute(
            "SELECT 1 FROM brand_claims WHERE brand_slug = ?", (brand_slug,)
        ).fetchone()
        if brand_exists and not already_claimed:
            db.execute(
                "INSERT INTO brand_claims (brand_slug, user_id, verified, claimed_at) "
                "VALUES (?, ?, 1, ?)",
                (brand_slug, user["id"], now),
            )
    return RedirectResponse(f"/b/{brand_slug}", status_code=303)


@app.post("/b/{brand_slug}/response")
def brand_response(request: Request, brand_slug: str, response: str = Form("")):
    """The brand's owner posts or edits their official response."""
    user = auth.current_user(request)
    if user is None:
        return RedirectResponse(f"/login?next=/b/{brand_slug}", status_code=303)
    with connect() as db:
        claim = db.execute(
            "SELECT user_id FROM brand_claims WHERE brand_slug = ?", (brand_slug,)
        ).fetchone()
        if claim and claim["user_id"] == user["id"]:
            db.execute(
                "UPDATE brand_claims SET response = ?, updated_at = ? WHERE brand_slug = ?",
                (response.strip() or None, datetime.now().isoformat(timespec="seconds"), brand_slug),
            )
    return RedirectResponse(f"/b/{brand_slug}", status_code=303)


@app.get("/b/{brand_slug}/feed.xml")
def brand_feed(request: Request, brand_slug: str):
    """An RSS feed of recent feedback for a brand, for people who want to
    subscribe. Plain XML, no JavaScript."""
    with connect() as db:
        rows = db.execute(
            """
            SELECT f.id, f.brand_name, f.rating, f.feedback_text, f.timestamp,
                   u.username AS author
            FROM feedback f JOIN users u ON u.id = f.user_id
            WHERE f.brand_slug = ?
            ORDER BY f.id DESC LIMIT 30
            """,
            (brand_slug,),
        ).fetchall()
    base = str(request.base_url).rstrip("/")
    brand_name = rows[0]["brand_name"] if rows else brand_slug

    items = []
    for r in rows:
        link = f"{base}/feedback/{r['id']}"
        try:
            pub = format_datetime(datetime.fromisoformat(r["timestamp"]))
        except ValueError:
            pub = ""
        items.append(
            "    <item>\n"
            f"      <title>{r['rating']} stars from {escape(r['author'])}</title>\n"
            f"      <link>{link}</link>\n"
            f"      <guid>{link}</guid>\n"
            f"      <description>{escape(r['feedback_text'])}</description>\n"
            f"      <pubDate>{pub}</pubDate>\n"
            "    </item>"
        )

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0">\n'
        "  <channel>\n"
        f"    <title>{escape(brand_name)} feedback on The Unfocus Group</title>\n"
        f"    <link>{base}/b/{brand_slug}</link>\n"
        f"    <description>Honest feedback for {escape(brand_name)}, in one place.</description>\n"
        + "\n".join(items)
        + "\n  </channel>\n</rss>\n"
    )
    return Response(content=xml, media_type="application/rss+xml")


@app.get("/feedback/{feedback_id}", response_class=HTMLResponse)
def feedback_detail(request: Request, feedback_id: int):
    user = auth.current_user(request)
    viewer_id = user["id"] if user else 0
    with connect() as db:
        post = db.execute(
            feedback_query("WHERE f.id = ?"), (viewer_id, viewer_id, feedback_id)
        ).fetchone()
        if post is None:
            return RedirectResponse("/", status_code=303)
        comments = db.execute(
            """
            SELECT c.comment_text, c.timestamp, u.username AS author
            FROM comments c JOIN users u ON u.id = c.user_id
            WHERE c.feedback_id = ? ORDER BY c.id ASC
            """,
            (feedback_id,),
        ).fetchall()
        flag_count = db.execute(
            "SELECT COUNT(*) AS c FROM flags WHERE feedback_id = ?", (feedback_id,)
        ).fetchone()["c"]
        my_flag = bool(user and db.execute(
            "SELECT 1 FROM flags WHERE feedback_id = ? AND user_id = ?",
            (feedback_id, user["id"]),
        ).fetchone())
    return page(request, "detail.html", user, post=post, comments=comments,
                my_flag=my_flag, flagged=flag_count >= FLAG_THRESHOLD)


@app.post("/feedback/{feedback_id}/flag")
def flag_feedback(request: Request, feedback_id: int, reason: str = Form("")):
    """Let the community report bad-faith feedback. One flag per person."""
    user = auth.current_user(request)
    if user is None:
        return RedirectResponse(f"/login?next=/feedback/{feedback_id}", status_code=303)
    with connect() as db:
        exists = db.execute("SELECT 1 FROM feedback WHERE id = ?", (feedback_id,)).fetchone()
        if exists:
            db.execute(
                "INSERT OR IGNORE INTO flags (feedback_id, user_id, reason, created_at) "
                "VALUES (?, ?, ?, ?)",
                (feedback_id, user["id"], reason.strip() or None,
                 datetime.now().isoformat(timespec="seconds")),
            )
    return RedirectResponse(f"/feedback/{feedback_id}", status_code=303)


@app.post("/feedback/{feedback_id}/save")
def toggle_save(request: Request, feedback_id: int, next: str = Form("/")):
    """Bookmark or un-bookmark a piece of feedback. Saves show on your profile."""
    user = auth.current_user(request)
    if user is None:
        return RedirectResponse(f"/login?next=/feedback/{feedback_id}", status_code=303)
    with connect() as db:
        existing = db.execute(
            "SELECT 1 FROM saves WHERE user_id = ? AND feedback_id = ?",
            (user["id"], feedback_id),
        ).fetchone()
        if existing:
            db.execute(
                "DELETE FROM saves WHERE user_id = ? AND feedback_id = ?",
                (user["id"], feedback_id),
            )
        elif db.execute("SELECT 1 FROM feedback WHERE id = ?", (feedback_id,)).fetchone():
            db.execute(
                "INSERT OR IGNORE INTO saves (user_id, feedback_id, created_at) VALUES (?, ?, ?)",
                (user["id"], feedback_id, datetime.now().isoformat(timespec="seconds")),
            )
    return RedirectResponse(next or "/", status_code=303)


@app.get("/brands/suggest")
def brand_suggestions(q: str = ""):
    """Return up to eight existing brands matching what the user is typing, so
    people reuse a brand's page instead of spawning near-duplicates."""
    q = q.strip()
    if not q:
        return JSONResponse([])
    # Escape LIKE wildcards so a stray % or _ doesn't match everything.
    safe = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with connect() as db:
        rows = db.execute(
            """
            SELECT brand_name, COUNT(*) AS count
            FROM feedback
            WHERE brand_name LIKE ? ESCAPE '\\'
            GROUP BY brand_slug
            ORDER BY (brand_name LIKE ? ESCAPE '\\') DESC, count DESC, brand_name
            LIMIT 8
            """,
            (f"%{safe}%", f"{safe}%"),
        ).fetchall()
    return JSONResponse([{"name": r["brand_name"], "count": r["count"]} for r in rows])


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = ""):
    """Keyword search across feedback text and brand names."""
    user = auth.current_user(request)
    viewer_id = user["id"] if user else 0
    q = q.strip()
    results = []
    if q:
        # Escape LIKE wildcards, then match against both the text and the brand.
        safe = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{safe}%"
        with connect() as db:
            results = db.execute(
                feedback_query(
                    "WHERE f.feedback_text LIKE ? ESCAPE '\\' OR f.brand_name LIKE ? ESCAPE '\\'"
                ) + " ORDER BY f.id DESC LIMIT 50",
                (viewer_id, viewer_id, like, like),
            ).fetchall()
    return page(request, "search.html", user, q=q, results=results)


@app.get("/u/{username}", response_class=HTMLResponse)
def profile(request: Request, username: str):
    """A person's page: everything they've posted and replied to, plus when
    they joined. Builds accountability for the feedback they leave."""
    user = auth.current_user(request)
    viewer_id = user["id"] if user else 0
    with connect() as db:
        profile_user = db.execute(
            "SELECT id, username, created_at FROM users WHERE username = ?", (username,)
        ).fetchone()
        if profile_user is None:
            return RedirectResponse("/", status_code=303)
        posts = db.execute(
            feedback_query("WHERE f.user_id = ?") + " ORDER BY f.id DESC",
            (viewer_id, viewer_id, profile_user["id"]),
        ).fetchall()
        comments = db.execute(
            """
            SELECT c.comment_text, c.timestamp, c.feedback_id,
                   f.brand_name, f.brand_slug
            FROM comments c
            JOIN feedback f ON f.id = c.feedback_id
            WHERE c.user_id = ?
            ORDER BY c.id DESC
            """,
            (profile_user["id"],),
        ).fetchall()
        # A person's saved feedback is private, so only show it on your own page.
        is_self = bool(user and user["id"] == profile_user["id"])
        saved_posts = []
        if is_self:
            saved_posts = db.execute(
                feedback_query("JOIN saves sv ON sv.feedback_id = f.id AND sv.user_id = ?")
                + " ORDER BY sv.created_at DESC, f.id DESC",
                (viewer_id, viewer_id, profile_user["id"]),
            ).fetchall()
    return page(request, "profile.html", user,
                profile_user=profile_user, posts=posts, comments=comments,
                is_self=is_self, saved_posts=saved_posts)


# --- Leaving feedback ----------------------------------------------------

@app.get("/submit", response_class=HTMLResponse)
def submit_form(request: Request, brand: str = ""):
    user = auth.current_user(request)
    if user is None:
        return RedirectResponse("/login?next=/submit", status_code=303)
    return page(request, "submit.html", user, brand=brand, error=None)


@app.post("/submit")
def create_feedback(
    request: Request,
    brand_name: str = Form(...),
    rating: int = Form(...),
    feedback_text: str = Form(...),
):
    user = auth.current_user(request)
    if user is None:
        return RedirectResponse("/login?next=/submit", status_code=303)

    brand_name = brand_name.strip()
    feedback_text = feedback_text.strip()
    slug = make_slug(brand_name)

    if not brand_name or not slug or not feedback_text or not (1 <= rating <= 5):
        return page(request, "submit.html", user, brand=brand_name,
                    error="Add a brand, a rating, and a few words, then send it.")

    now = datetime.now().isoformat(timespec="seconds")
    with connect() as db:
        db.execute(
            """
            INSERT INTO feedback (user_id, brand_name, brand_slug, rating, feedback_text, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user["id"], brand_name, slug, rating, feedback_text, now),
        )
    return RedirectResponse(f"/b/{slug}", status_code=303)


# --- Voting (the only JSON endpoint) ------------------------------------

@app.post("/feedback/{feedback_id}/vote")
def vote(request: Request, feedback_id: int, value: int = Form(...)):
    user = auth.current_user(request)
    if user is None:
        return JSONResponse({"error": "Log in to vote."}, status_code=401)
    if value not in (1, -1):
        return JSONResponse({"error": "A vote is up or down."}, status_code=400)

    with connect() as db:
        existing = db.execute(
            "SELECT value FROM votes WHERE user_id = ? AND feedback_id = ?",
            (user["id"], feedback_id),
        ).fetchone()

        if existing and existing["value"] == value:
            # Clicking the same arrow again takes the vote back.
            db.execute(
                "DELETE FROM votes WHERE user_id = ? AND feedback_id = ?",
                (user["id"], feedback_id),
            )
            my_vote = 0
        else:
            db.execute(
                """
                INSERT INTO votes (user_id, feedback_id, value) VALUES (?, ?, ?)
                ON CONFLICT(user_id, feedback_id) DO UPDATE SET value = excluded.value
                """,
                (user["id"], feedback_id, value),
            )
            my_vote = value

        score = db.execute(
            "SELECT COALESCE(SUM(value), 0) AS score FROM votes WHERE feedback_id = ?",
            (feedback_id,),
        ).fetchone()["score"]

    return JSONResponse({"score": score, "my_vote": my_vote})


# --- Comments ------------------------------------------------------------

@app.post("/feedback/{feedback_id}/comment")
def add_comment(request: Request, feedback_id: int, comment_text: str = Form(...)):
    user = auth.current_user(request)
    if user is None:
        return RedirectResponse(f"/login?next=/feedback/{feedback_id}", status_code=303)

    comment_text = comment_text.strip()
    if comment_text:
        now = datetime.now().isoformat(timespec="seconds")
        with connect() as db:
            db.execute(
                "INSERT INTO comments (feedback_id, user_id, comment_text, timestamp) VALUES (?, ?, ?, ?)",
                (feedback_id, user["id"], comment_text, now),
            )
    return RedirectResponse(f"/feedback/{feedback_id}", status_code=303)


# --- Accounts ------------------------------------------------------------

@app.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request, next: str = "/"):
    user = auth.current_user(request)
    if user:
        return RedirectResponse("/", status_code=303)
    return page(request, "signup.html", None, next=next, error=None)


@app.post("/signup")
def signup(request: Request, username: str = Form(...), email: str = Form(...),
           password: str = Form(...), next: str = Form("/")):
    username = username.strip()
    email = email.strip()
    if len(username) < 5:
        return page(request, "signup.html", None, next=next, email=email,
                    error="Pick a username of 5 or more characters.")
    if not auth.looks_like_email(email):
        return page(request, "signup.html", None, next=next, email=email,
                    error="Enter a valid email address.")

    weak = auth.password_problem(password, username)
    if weak:
        return page(request, "signup.html", None, next=next, email=email, error=weak)

    user_id = auth.create_user(username, password, email)
    if user_id is None:
        return page(request, "signup.html", None, next=next, email=email,
                    error="That name is taken. Try another.")

    # Send (in dev, log + show) the verification link, then sign them in.
    verify_token = auth.make_email_token(user_id, "verify")
    verify_url = f"{request.base_url}verify?token={verify_token}"
    mailer.send_link(email, "Verify your email for The Unfocus Group", verify_url)

    new_user = auth.find_user(username)
    session_token = auth.start_session(user_id)
    response = page(request, "verify_sent.html", new_user, email=email,
                    dev_link=verify_url if mailer.DEV_SHOW_LINKS else None)
    response.set_cookie(auth.COOKIE_NAME, session_token, httponly=True, samesite="lax")
    return response


@app.get("/verify")
def verify_email(request: Request, token: str = ""):
    """Confirm an email from the link we sent."""
    user_id = auth.consume_email_token(token, "verify")
    if user_id:
        with connect() as db:
            db.execute("UPDATE users SET email_verified = 1 WHERE id = ?", (user_id,))
    return RedirectResponse("/", status_code=303)


@app.get("/resend-verification")
def resend_verification(request: Request):
    """Issue a fresh verification link for the logged-in user."""
    user = auth.current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user["email"] and not user["email_verified"]:
        token = auth.make_email_token(user["id"], "verify")
        url = f"{request.base_url}verify?token={token}"
        mailer.send_link(user["email"], "Verify your email for The Unfocus Group", url)
        return page(request, "verify_sent.html", user, email=user["email"],
                    dev_link=url if mailer.DEV_SHOW_LINKS else None)
    return RedirectResponse("/", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/", reset: int = 0):
    user = auth.current_user(request)
    if user:
        return RedirectResponse("/", status_code=303)
    return page(request, "login.html", None, next=next, error=None, reset=reset)


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...),
          next: str = Form("/")):
    row = auth.find_user(username.strip())
    if row is None or not auth.password_matches(password, row["password_hash"]):
        return page(request, "login.html", None, next=next,
                    error="That name and password don't match.")

    token = auth.start_session(row["id"])
    response = RedirectResponse(next or "/", status_code=303)
    response.set_cookie(auth.COOKIE_NAME, token, httponly=True, samesite="lax")
    return response


@app.post("/logout")
def logout(request: Request):
    auth.end_session(request.cookies.get(auth.COOKIE_NAME))
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(auth.COOKIE_NAME)
    return response


@app.get("/forgot", response_class=HTMLResponse)
def forgot_form(request: Request):
    return page(request, "forgot.html", auth.current_user(request),
                sent=False, dev_link=None, email="")


@app.post("/forgot")
def forgot(request: Request, email: str = Form(...)):
    """Email a password-reset link to the address on file."""
    email = email.strip()
    dev_link = None
    with connect() as db:
        row = db.execute("SELECT id, email FROM users WHERE email = ?", (email,)).fetchone()
    if row and row["email"]:
        token = auth.make_email_token(row["id"], "reset")
        url = f"{request.base_url}reset?token={token}"
        mailer.send_link(row["email"], "Reset your password for The Unfocus Group", url)
        if mailer.DEV_SHOW_LINKS:
            dev_link = url
    # Always show the same message so we don't reveal which emails are registered.
    return page(request, "forgot.html", auth.current_user(request),
                sent=True, dev_link=dev_link, email=email)


@app.get("/reset", response_class=HTMLResponse)
def reset_form(request: Request, token: str = ""):
    valid = auth.email_token_user(token, "reset") is not None
    return page(request, "reset.html", None, token=token, valid=valid, error=None)


@app.post("/reset")
def reset_password(request: Request, token: str = Form(...), password: str = Form(...)):
    user_id = auth.email_token_user(token, "reset")
    if user_id is None:
        return page(request, "reset.html", None, token=token, valid=False, error=None)

    with connect() as db:
        row = db.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    username = row["username"] if row else ""
    weak = auth.password_problem(password, username)
    if weak:
        return page(request, "reset.html", None, token=token, valid=True, error=weak)

    auth.consume_email_token(token, "reset")
    auth.set_password(user_id, password)
    return RedirectResponse("/login?reset=1", status_code=303)
