"""The Unfocus Group: a public board where anyone can leave honest feedback
for any brand, vote on what others wrote, and talk it through.

Pages are plain server-rendered HTML. Only voting talks back to the server
with a small fetch call, so the rest works without any JavaScript.
"""

from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth
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
            COALESCE((SELECT value FROM votes WHERE feedback_id = f.id AND user_id = ?), 0) AS my_vote
        FROM feedback f
        JOIN users u ON u.id = f.user_id
        {where}
    """


def sort_clause(sort):
    """Top means most agreed-with; new means most recent."""
    return "ORDER BY score DESC, f.id DESC" if sort == "top" else "ORDER BY f.id DESC"


# --- The feed and brand pages -------------------------------------------

@app.get("/", response_class=HTMLResponse)
def home(request: Request, sort: str = "new"):
    user = auth.current_user(request)
    viewer_id = user["id"] if user else 0
    with connect() as db:
        posts = db.execute(
            feedback_query() + sort_clause(sort), (viewer_id,)
        ).fetchall()
        brands = db.execute(
            """
            SELECT brand_slug, brand_name, COUNT(*) AS count
            FROM feedback GROUP BY brand_slug
            ORDER BY count DESC, brand_name LIMIT 12
            """
        ).fetchall()
    return page(request, "feed.html", user, posts=posts, brands=brands,
                sort=sort, heading="All feedback", brand=None)


@app.get("/b/{brand_slug}", response_class=HTMLResponse)
def brand_page(request: Request, brand_slug: str, sort: str = "new"):
    user = auth.current_user(request)
    viewer_id = user["id"] if user else 0
    with connect() as db:
        posts = db.execute(
            feedback_query("WHERE f.brand_slug = ?") + sort_clause(sort),
            (viewer_id, brand_slug),
        ).fetchall()
        brands = db.execute(
            """
            SELECT brand_slug, brand_name, COUNT(*) AS count
            FROM feedback GROUP BY brand_slug
            ORDER BY count DESC, brand_name LIMIT 12
            """
        ).fetchall()
    if not posts:
        return page(request, "feed.html", user, posts=[], brands=brands,
                    sort=sort, heading=brand_slug, brand=brand_slug)
    display_name = posts[0]["brand_name"]
    return page(request, "feed.html", user, posts=posts, brands=brands,
                sort=sort, heading=display_name, brand=brand_slug)


@app.get("/feedback/{feedback_id}", response_class=HTMLResponse)
def feedback_detail(request: Request, feedback_id: int):
    user = auth.current_user(request)
    viewer_id = user["id"] if user else 0
    with connect() as db:
        post = db.execute(
            feedback_query("WHERE f.id = ?"), (viewer_id, feedback_id)
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
    return page(request, "detail.html", user, post=post, comments=comments)


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
                (viewer_id, like, like),
            ).fetchall()
    return page(request, "search.html", user, q=q, results=results)


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
def signup(request: Request, username: str = Form(...), password: str = Form(...),
           next: str = Form("/")):
    username = username.strip()
    if len(username) < 5:
        return page(request, "signup.html", None, next=next,
                    error="Pick a username of 5 or more characters.")

    weak = auth.password_problem(password, username)
    if weak:
        return page(request, "signup.html", None, next=next, error=weak)

    user_id = auth.create_user(username, password)
    if user_id is None:
        return page(request, "signup.html", None, next=next,
                    error="That name is taken. Try another.")

    token = auth.start_session(user_id)
    response = RedirectResponse(next or "/", status_code=303)
    response.set_cookie(auth.COOKIE_NAME, token, httponly=True, samesite="lax")
    return response


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    user = auth.current_user(request)
    if user:
        return RedirectResponse("/", status_code=303)
    return page(request, "login.html", None, next=next, error=None)


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
