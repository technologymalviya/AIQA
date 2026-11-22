"""Local fixture application. One route is missing and one page throws."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter(prefix="/demo")
_ACCOUNTS: dict[str, str] = {}

_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="app-version" content="demo-0.1.0">
  <title>{title} · Harbor Demo</title>
  <link rel="icon" href="data:,">
  <style>
    :root {{ color-scheme: light; }}
    body {{ margin: 0; font-family: "Iowan Old Style", Palatino, Georgia, serif;
            background: #f6f3ec; color: #1d1a16; }}
    header, main, footer {{ max-width: 42rem; margin: 0 auto; padding: 1.25rem; }}
    nav a {{ margin-right: 1rem; color: #7c2d12; }}
    h1 {{ font-weight: 500; letter-spacing: -0.02em; }}
    .card {{ background: #fffdf8; border: 1px solid #e4dccb; padding: 1rem; margin: 0.75rem 0; }}
    footer {{ color: #5c564c; font-size: 0.95rem; }}
  </style>
</head>
<body>
  <header>
    <nav>
      <a href="/demo/">Home</a>
      <a href="/demo/pricing">Pricing</a>
      <a href="/demo/help">Help Centre</a>
      <a href="/demo/signup">Create account</a>
      <a href="/demo/login">Sign in</a>
      <a href="https://example.com/outside">External docs</a>
    </nav>
  </header>
  <main>
    {body}
  </main>
  <footer>
    <a href="/demo/missing">API reference (missing page)</a>
  </footer>
  {script}
</body>
</html>
"""


def _page(title: str, body: str, script: str = "") -> str:
    return _PAGE.format(title=title, body=body, script=script)


@router.get("/")
def home() -> HTMLResponse:
    body = """
    <h1>Welcome</h1>
    <p>Harbor Demo is a fixture shop used by the QA pilot. Nothing here takes payment.</p>
    <div class="card"><h2>Notebooks</h2><p>Plain paper, listed for the crawl.</p></div>
    <div class="card"><h2>Ink</h2><p>A second section so the home page has structure.</p></div>
    """
    return HTMLResponse(_page("Home", body))


@router.get("/pricing")
def pricing() -> HTMLResponse:
    body = """
    <h1>Pricing</h1>
    <p role="alert">Prices unavailable</p>
    <p data-qa-bug="pricing catalog failed to load">The catalog request failed. This page is the runtime-error fixture.</p>
    """
    # Safety boundary: this script is the known product bug. It is not model output.
    script = """
    <script>
      console.error("pricing catalog failed to load");
      throw new Error("pricing catalog failed to load");
    </script>
    """
    return HTMLResponse(_page("Pricing", body, script))


@router.get("/help")
def help_centre() -> HTMLResponse:
    # The password value is a redaction fixture. The crawler must not record it.
    body = """
    <h1>Help Centre</h1>
    <p>Search the articles, or read the shipping note below.</p>
    <form method="get" action="/demo/help">
      <label for="q">Search help</label>
      <input id="q" name="q" type="search" value="">
      <input type="password" name="token" value="demo-input-value-should-not-leak" autocomplete="off">
      <button type="submit">Search</button>
    </form>
    <h2>Shipping</h2>
    <p>Orders in this fixture do not ship.</p>
    """
    return HTMLResponse(_page("Help Centre", body))


@router.get("/missing")
def missing() -> HTMLResponse:
    body = """
    <h1>Page not found</h1>
    <p>This route is intentionally missing.</p>
    """
    return HTMLResponse(_page("Page not found", body), status_code=404)


@router.get("/fixtures/slow")
async def slow() -> HTMLResponse:
    """Environment fixture. The navigation timeout fires before this returns."""
    await asyncio.sleep(15)
    body = "<h1>Slow fixture</h1><p>This response is late on purpose.</p>"
    return HTMLResponse(_page("Slow fixture", body))


@router.get("/fixtures/redirect-internal")
def redirect_internal() -> RedirectResponse:
    return RedirectResponse(url="/demo/help", status_code=302)


@router.get("/fixtures/redirect-external")
def redirect_external() -> RedirectResponse:
    return RedirectResponse(url="https://example.com/", status_code=302)


def _account_form(title: str, heading: str, body: str, error: str = "") -> HTMLResponse:
    alert = f'<p class="auth-error" role="alert">{error}</p>' if error else ""
    return HTMLResponse(_page(title, f"<h1>{heading}</h1>{alert}{body}"))


@router.get("/signup")
def signup_form() -> HTMLResponse:
    body = """
    <form class="auth-form signup-form" method="post" action="/demo/signup">
      <label>Admin name <input name="name" placeholder="Your name" required></label>
      <label>Username <input name="username" placeholder="Login username" required></label>
      <label>Password <input type="password" name="password" placeholder="At least 8 characters" required></label>
      <label>Confirm Password <input type="password" name="confirm" placeholder="Re-enter password" required></label>
      <label>Company name <input name="company" placeholder="Your dairy's name" required></label>
      <label>Email <input name="email" placeholder="Email" required></label>
      <label>Phone <input name="phone" placeholder="10-digit mobile" required></label>
      <label for="signup-privacy"><input id="signup-privacy" type="checkbox"> I agree to the Privacy Policy</label>
      <button class="auth-submit" type="submit">Sign Up</button>
    </form>
    """
    return _account_form("Create account", "Create Account", body)


@router.post("/signup")
async def signup_submit(request: Request):
    form = await request.form()
    username = str(form.get("username") or "").strip()
    password = str(form.get("password") or "")
    if len(username) < 3 or len(password) < 8:
        return _account_form("Create account", "Create Account", "", "Enter the account fields.")
    if username in _ACCOUNTS:
        return _account_form("Create account", "Create Account", "", "That username is already registered.")
    _ACCOUNTS[username] = password
    response = RedirectResponse(url="/demo/app", status_code=303)
    response.set_cookie("demo_user", username, httponly=True)
    return response


@router.get("/login")
def login_form() -> HTMLResponse:
    body = """
    <form class="auth-form" method="post" action="/demo/login">
      <label>Username <input name="username" placeholder="Enter username" required></label>
      <label>Password <input type="password" name="password" placeholder="Enter password" required></label>
      <button class="auth-submit" type="submit">Continue</button>
    </form>
    """
    return _account_form("Sign in", "Sign in", body)


@router.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = str(form.get("username") or "").strip()
    password = str(form.get("password") or "")
    if _ACCOUNTS.get(username) != password:
        body = """
        <form class="auth-form" method="post" action="/demo/login">
          <label>Username <input name="username" placeholder="Enter username" required></label>
          <label>Password <input type="password" name="password" placeholder="Enter password" required></label>
          <button class="auth-submit" type="submit">Continue</button>
        </form>
        """
        return _account_form("Sign in", "Sign in", body, "Invalid credentials")
    response = RedirectResponse(url="/demo/app", status_code=303)
    response.set_cookie("demo_user", username, httponly=True)
    return response


@router.get("/app")
def app_desk(request: Request) -> HTMLResponse:
    user = request.cookies.get("demo_user", "")
    if not user or user not in _ACCOUNTS:
        return RedirectResponse(url="/demo/login", status_code=303)
    body = f"""
    <h1>Dairy desk</h1>
    <p>Signed in as {user}</p>
    <nav>
      <button type="button">Customer</button>
      <button type="button">Billing</button>
      <button type="button">Raise Ticket</button>
    </nav>
    <p id="desk">Customer book</p>
    <script>
      document.querySelectorAll("nav button").forEach(function (button) {{
        button.addEventListener("click", function () {{
          var desk = document.getElementById("desk");
          if (desk) desk.textContent = button.textContent;
        }});
      }});
    </script>
    """
    return HTMLResponse(_page("Dairy desk", body))


@router.get("/{rest:path}")
def unknown(rest: str) -> HTMLResponse:
    del rest
    body = "<h1>Page not found</h1><p>No demo route matches this path.</p>"
    return HTMLResponse(_page("Page not found", body), status_code=404)


def demo_redirect() -> RedirectResponse:
    return RedirectResponse(url="/demo/", status_code=307)


def empty_favicon() -> Response:
    return Response(status_code=204)
