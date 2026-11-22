"""One account journey. Signup happens once. Later runs reuse it."""

from __future__ import annotations

import secrets
from urllib.parse import urlsplit

from playwright.async_api import Browser, Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.config import Settings
from app.execution.screen import capture
from app.execution.verdict import judge
from app.generation.heuristic import case_id
from app.mind.memory import scrub, storage_file
from app.mind.plan import FlowDecision
from app.models import CaseResult, Provenance, StepResult
from app.security.text import redact
from app.security.url_policy import ScopeError, check_target

_SKIP_CLICK = (
    "delete",
    "pay",
    "purchase",
    "logout",
    "sign out",
    "sign in",
    "sign up",
    "send",
    "whatsapp",
    "ticket",
    "message",
    "remove",
    "start free",
    "privacy",
)
_FIELDS = (
    ("confirm", ("confirm password", "re-enter")),
    ("password", ("password",)),
    ("email", ("email",)),
    ("phone", ("phone", "mobile")),
    ("company", ("company", "dairy")),
    ("username", ("username",)),
    ("name", ("admin name", "your name", "name")),
)


async def run_journey(
    browser: Browser,
    *,
    decision: FlowDecision,
    memory: dict,
    settings: Settings,
    environment: dict[str, str],
    on_screen,
    on_note,
    cancel_check,
    choose_nav=None,
) -> tuple[list[CaseResult], set[str], dict]:
    """Return case results, URLs already covered, and the updated memory."""
    handled = {url for url in (decision.signup_url, decision.signin_url) if url}
    if not decision.create_account and not decision.sign_in:
        on_note(decision.summary)
        return [], handled, memory
    origin = memory.get("origin") or ""
    state_path = storage_file(settings.runs_dir, origin)
    context = None
    steps: list[StepResult] = []
    try:
        kwargs = {"viewport": {"width": 1280, "height": 720}, "locale": "en-GB", "timezone_id": "UTC"}
        if state_path.is_file() and memory.get("signin_done"):
            kwargs["storage_state"] = str(state_path)
        context = await browser.new_context(**kwargs)
        context.set_default_navigation_timeout(settings.navigation_timeout_ms)
        page = await context.new_page()
        if cancel_check and cancel_check():
            return [], handled, memory
        signed_in = False
        if memory.get("signin_done") and memory.get("entered_url"):
            await _goto(page, memory["entered_url"], origin)
            await capture(page, on_screen, "Opening the remembered screen")
            signed_in = await _shell(page)
            if signed_in:
                on_note("Already signed in from the remembered session.")
                steps.append(StepResult(action="signin", passed=True, actual="remembered session"))
        if not signed_in and decision.create_account:
            on_note("Creating one account.")
            account = _new_account()
            outcome = await _signup(page, decision.signup_url, origin, account, settings)
            steps.append(outcome)
            await capture(page, on_screen, f"Signup for {account['username']}")
            if outcome.passed:
                memory.update(account)
                memory["signup_done"] = True
                memory["entered_url"] = page.url
                signed_in = await _shell(page)
                if signed_in:
                    memory["signin_done"] = True
                    on_note(f"Signup signed in as {account['username']}. Sign-in will not be repeated.")
                elif decision.signin_url:
                    signed_in = await _do_signin(page, decision, memory, settings, steps, on_note, on_screen)
            else:
                on_note(scrub(outcome.error or "Signup did not finish.", memory))
        elif not signed_in and decision.sign_in:
            signed_in = await _do_signin(page, decision, memory, settings, steps, on_note, on_screen)
        if signed_in:
            await _walk_nav(page, memory, on_screen, on_note, cancel_check, choose_nav)
            try:
                state_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(state_path))
            except PlaywrightError:
                pass
            if page.url:
                memory["entered_url"] = page.url
                handled.add(page.url)
        verdict, classification, kind, reason = judge(steps)
        result = CaseResult(
            test_case_id=case_id(origin or decision.signup_url or decision.signin_url, "account-flow"),
            title="Use one remembered account",
            provenance=Provenance.HEURISTIC,
            verdict=verdict,
            classification=classification,
            failure_kind=kind,
            reason=scrub(reason, memory),
            steps=steps,
            environment=environment,
        )
        return [result], handled, memory
    except (PlaywrightError, ScopeError) as exc:
        steps.append(
            StepResult(action="journey", passed=False, error=redact(scrub(str(exc), memory), 300), kind="runner")
        )
        verdict, classification, kind, reason = judge(steps)
        result = CaseResult(
            test_case_id=case_id(origin or "journey", "account-flow"),
            title="Use one remembered account",
            provenance=Provenance.HEURISTIC,
            verdict=verdict,
            classification=classification,
            failure_kind=kind,
            reason=scrub(reason, memory),
            steps=steps,
            environment=environment,
        )
        return [result], handled, memory
    finally:
        if context is not None:
            try:
                await context.close()
            except PlaywrightError:
                pass


async def _do_signin(page, decision: FlowDecision, memory: dict, settings: Settings, steps, on_note, on_screen) -> bool:
    on_note("Signing in with the remembered account.")
    outcome = await _signin(page, decision.signin_url, memory, settings)
    steps.append(outcome)
    await capture(page, on_screen, f"Sign in as {memory.get('username')}")
    if not outcome.passed:
        on_note(scrub(outcome.error or "Sign-in did not finish.", memory))
        return False
    memory["signin_done"] = True
    memory["entered_url"] = page.url
    return True


async def _signup(page, url: str, origin: str, account: dict, settings: Settings) -> StepResult:
    try:
        await _goto(page, url, origin)
        values = {
            "name": "QA Tester",
            "username": account["username"],
            "password": account["password"],
            "confirm": account["password"],
            "company": "QA Dairy",
            "email": f"{account['username']}@example.com",
            "phone": account["phone"],
        }
        await _dismiss_banner(page)
        if not await _fill_form(page, values):
            return StepResult(action="signup", passed=False, error="signup form fields were not found", kind="runner")
        terms = await _accept_terms(page)
        if terms:
            return StepResult(action="signup", passed=False, error=terms, kind="assertion")
        await _submit(page, settings.navigation_timeout_ms)
        error = await _form_error(page)
        if error:
            return StepResult(action="signup", passed=False, error=redact(error, 300), kind="assertion")
        if await _shell(page) or await _has_session(page) or await _account_ready(page):
            await _open_ready(page)
            return StepResult(action="signup", passed=True, actual=account["username"])
        heading = await _heading(page)
        detail = f"signup did not open the app ({heading})" if heading else "signup did not open the app"
        return StepResult(action="signup", passed=False, error=detail, kind="assertion")
    except PlaywrightTimeoutError:
        return StepResult(action="signup", passed=False, error="navigation timeout", kind="runner")
    except (PlaywrightError, ScopeError) as exc:
        return StepResult(action="signup", passed=False, error=redact(exc, 300), kind="runner")


async def _signin(page, url: str, memory: dict, settings: Settings) -> StepResult:
    try:
        await _goto(page, url, memory.get("origin") or "")
        await _dismiss_banner(page)
        values = {"username": memory.get("username") or "", "password": memory.get("password") or ""}
        if not await _fill_form(page, values):
            return StepResult(action="signin", passed=False, error="sign-in form fields were not found", kind="runner")
        await _submit(page, settings.navigation_timeout_ms)
        error = await _form_error(page)
        if error:
            return StepResult(action="signin", passed=False, error=redact(error, 300), kind="assertion")
        if await _shell(page) or await _has_session(page):
            return StepResult(action="signin", passed=True, actual=str(memory.get("username") or ""))
        return StepResult(action="signin", passed=False, error="sign-in did not open the app", kind="assertion")
    except PlaywrightTimeoutError:
        return StepResult(action="signin", passed=False, error="navigation timeout", kind="runner")
    except (PlaywrightError, ScopeError) as exc:
        return StepResult(action="signin", passed=False, error=redact(exc, 300), kind="runner")


async def _walk_nav(page, memory: dict, on_screen, on_note, cancel_check, choose_nav=None) -> None:
    checked = {str(item) for item in memory.get("nav_checked") or []}
    labels = await _nav_labels(page)
    if choose_nav is not None and labels:
        try:
            picked = await choose_nav(labels, sorted(checked))
        except Exception:
            picked = None
        if picked:
            allowed = set(labels)
            labels = [label for label in picked if label in allowed]
    fresh = 0
    for label in labels:
        if cancel_check and cancel_check():
            break
        if label in checked:
            on_note(f"Skipping {label}. That step is already remembered.")
            continue
        if fresh >= 8:
            break
        try:
            await page.get_by_role("button", name=label, exact=True).first.click(timeout=3000)
        except PlaywrightError:
            continue
        fresh += 1
        checked.add(label)
        on_note(f"Opened {label} once.")
        await capture(page, on_screen, f"Opened {label}")
        await page.wait_for_timeout(300)
    memory["nav_checked"] = sorted(checked)


async def _nav_labels(page) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    locator = page.locator("nav button, [role=navigation] button, [role=tablist] button")
    count = await locator.count()
    if count == 0:
        locator = page.get_by_role("button")
        count = min(await locator.count(), 30)
    for index in range(count):
        try:
            text = (await locator.nth(index).inner_text()).strip().splitlines()[0].strip()
        except PlaywrightError:
            continue
        label = text[:40]
        lowered = label.lower()
        if not label or label in seen or len(label) > 28:
            continue
        if any(part in lowered for part in _SKIP_CLICK):
            continue
        seen.add(label)
        found.append(label)
    return found


async def _fill_form(page, values: dict[str, str]) -> bool:
    labels = page.locator("form label")
    count = await labels.count()
    used: set[int] = set()
    filled = 0
    for kind, hints in _FIELDS:
        if kind not in values:
            continue
        for index in range(count):
            if index in used:
                continue
            label = labels.nth(index)
            try:
                text = (await label.inner_text()).lower()
            except PlaywrightError:
                continue
            if not any(hint in text for hint in hints):
                continue
            field = label.locator("input:not([type=checkbox]):not([type=hidden]), textarea").first
            try:
                if await field.count() == 0:
                    continue
                await field.fill(values[kind])
            except PlaywrightError:
                continue
            used.add(index)
            filled += 1
            break
    if filled < len(values):
        filled += await _fill_placeholders(page, values)
    return filled >= len(values)


async def _fill_placeholders(page, values: dict[str, str]) -> int:
    hints = {
        "name": ("your name",),
        "username": ("username", "login"),
        "password": ("at least 8", "enter password"),
        "confirm": ("re-enter",),
        "company": ("dairy", "company"),
        "email": ("email",),
        "phone": ("mobile", "phone", "10-digit"),
    }
    filled = 0
    inputs = page.locator("form input:not([type=checkbox]):not([type=hidden])")
    count = await inputs.count()
    used: set[int] = set()
    for kind, words in hints.items():
        if kind not in values:
            continue
        for index in range(count):
            if index in used:
                continue
            field = inputs.nth(index)
            try:
                placeholder = ((await field.get_attribute("placeholder")) or "").lower()
                current = await field.input_value()
            except PlaywrightError:
                continue
            if current or not any(word in placeholder for word in words):
                continue
            try:
                await field.fill(values[kind])
            except PlaywrightError:
                continue
            used.add(index)
            filled += 1
            break
    return filled


async def _dismiss_banner(page) -> None:
    for label in ("Reject all", "Accept all"):
        button = page.get_by_role("button", name=label, exact=True)
        try:
            if await button.count():
                await button.first.click(timeout=2000)
                return
        except PlaywrightError:
            return


async def _accept_terms(page) -> str:
    box = page.locator("#signup-privacy").first
    try:
        if await box.count() == 0:
            box = page.locator("form input[type=checkbox]").first
        if await box.count() == 0:
            return ""
        await box.click(force=True, timeout=3000)
        await page.wait_for_function(
            """() => {
              const box = document.querySelector('#signup-privacy, form input[type=checkbox]');
              const button = document.querySelector('form button[type=submit]');
              return (!box || box.checked) && button && !button.disabled;
            }""",
            timeout=3000,
        )
    except PlaywrightTimeoutError:
        return "the signup button stayed disabled"
    except PlaywrightError as exc:
        return redact(exc, 180) or "could not accept the signup terms"
    return ""


async def _submit(page, timeout_ms: int) -> None:
    button = page.locator("form button[type=submit]").first
    text = ""
    try:
        text = (await button.inner_text()).lower()
    except PlaywrightError:
        text = ""
    if any(part in text for part in ("delete", "pay", "purchase", "send")):
        raise PlaywrightError("submit label is not a signup or sign-in action")
    try:
        async with page.expect_navigation(wait_until="domcontentloaded", timeout=min(timeout_ms, 4000)):
            await button.click()
    except PlaywrightTimeoutError:
        await page.wait_for_timeout(400)
    try:
        await page.wait_for_function(
            """() => {
              const err = document.querySelector('.auth-error');
              if (err && (err.innerText || '').trim()) return true;
              if (localStorage.getItem('milkman_token')) return true;
              const text = document.body ? document.body.innerText : '';
              if (text.includes('Your dairy is ready') || text.includes('Account created')) return true;
              const buttons = [...document.querySelectorAll('button, a')];
              return buttons.some((b) => (b.innerText || '').trim() === 'Customer');
            }""",
            timeout=min(timeout_ms, 8000),
        )
    except PlaywrightTimeoutError:
        return


async def _form_error(page) -> str:
    locator = page.locator(".auth-error")
    try:
        if await locator.count() == 0:
            return ""
        return (await locator.first.inner_text()).strip()
    except PlaywrightError:
        return ""


async def _heading(page) -> str:
    try:
        return (await page.locator("h1").first.inner_text(timeout=1000)).strip()[:80]
    except PlaywrightError:
        return ""


async def _account_ready(page) -> bool:
    try:
        text = (await page.locator("body").inner_text(timeout=1000)).lower()
    except PlaywrightError:
        return False
    return "your dairy is ready" in text or "account created" in text


async def _open_ready(page) -> None:
    for role in ("button", "link"):
        control = page.get_by_role(role, name="Open your dairy", exact=True)
        try:
            if await control.count() == 0:
                continue
            await control.first.click(timeout=3000)
            await page.wait_for_timeout(600)
            return
        except PlaywrightError:
            return


async def _shell(page) -> bool:
    try:
        if await page.get_by_role("button", name="Customer", exact=True).count():
            return True
    except PlaywrightError:
        return False
    try:
        heading = (await page.locator("h1").first.inner_text(timeout=1000)).strip().lower()
    except PlaywrightError:
        return False
    return heading in {"dairy desk", "signed in"}


async def _has_session(page) -> bool:
    try:
        return bool(await page.evaluate("() => !!localStorage.getItem('milkman_token')"))
    except PlaywrightError:
        return False


async def _goto(page, url: str, origin: str) -> None:
    check_target(url, authorized=_origin(origin) if origin else None)
    await page.goto(url, wait_until="domcontentloaded")


def _origin(value: str):
    from app.security.url_policy import origin_of

    return origin_of(value if "://" in value else f"https://{value}")


def _new_account() -> dict:
    username = "qa" + secrets.token_hex(3)
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    password = "".join(secrets.choice(alphabet) for _ in range(12))
    phone = "9" + "".join(secrets.choice("0123456789") for _ in range(9))
    return {"username": username, "password": password, "phone": phone}


# Imported only for the type checker path of check_target's authorized origin.
_ = urlsplit
