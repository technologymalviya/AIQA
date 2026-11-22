"""Click through a site the way a person would, and say what is happening.

The hands stay on the authorized site. They can follow a requested signup until the site asks for a phone or refuses the account. They do not pay, delete, or send a message.
"""

from __future__ import annotations

from secrets import token_hex
from urllib.parse import urljoin, urlsplit

from playwright.async_api import Browser, Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.execution.screen import capture
from app.execution.verdict import judge
from app.generation.heuristic import case_id
from app.models import CaseResult, Provenance, StepResult
from app.security.text import redact
from app.mind.plan import goal_wants_fresh_account
from app.security.url_policy import ScopeError, check_target, path_is_destructive

# Gmail's own sign-in screen lives on accounts.google.com. That host stays in bounds.
_ACCOUNT_HOSTS = {
    "gmail.com": frozenset({"accounts.google.com", "mail.google.com", "gmail.com"}),
    "mail.google.com": frozenset({"accounts.google.com", "mail.google.com", "gmail.com"}),
}

_CLOSED = (
    "pay",
    "buy",
    "quote",
    "purchase",
    "checkout",
    "cart",
    "delete",
    "sign up",
    "signup",
    "log in",
    "login",
    "sign in",
    "register",
    "submit",
    "apply",
    "bind",
    "enroll",
    "claim",
    "account",
    "send",
    "message",
    "forgot",
)


def hands_off(label: str, *, allow_account: bool = False) -> bool:
    lowered = " ".join(label.lower().split())
    if not lowered or len(lowered) > 60 or lowered.startswith("skip"):
        return True
    closed = _CLOSED
    if allow_account:
        closed = tuple(part for part in _CLOSED if part not in {"account", "sign up", "signup", "register"})
    return any(part in lowered for part in closed)


async def look_around(
    browser: Browser,
    *,
    start_url: str,
    origin: str,
    settings,
    environment: dict[str, str],
    on_screen,
    on_note,
    cancel_check,
    choose=None,
    goal: str = "",
    limit: int = 5,
) -> list[CaseResult]:
    """Open the start page and follow a few safe controls, narrating each one."""
    context = await browser.new_context(
        viewport={"width": 1280, "height": 720},
        locale="en-GB",
        timezone_id="UTC",
        accept_downloads=False,
    )
    context.set_default_navigation_timeout(settings.navigation_timeout_ms)
    steps: list[StepResult] = []
    try:
        page = await context.new_page()
        await _open(page, start_url, origin)
        await _dismiss(page)
        title, heading = await _seen(page)
        await capture(page, on_screen, f"Looking at {title or 'the page'}")
        on_note(_say(f"I opened {title or 'the page'}. The heading is {heading or 'blank'}."))
        steps.append(StepResult(action="look", passed=True, actual=redact(title, 120)))
        tried: set[str] = set()
        allow_account = goal_wants_fresh_account(goal)
        if allow_account:
            limit = max(limit, 8)
        for _ in range(limit):
            if cancel_check and cancel_check():
                break
            wall = await _verification_wall(page)
            if wall:
                on_note(wall)
                if "send" in goal.lower():
                    on_note("The message was not sent. There is no mailbox to send it from.")
                break
            if allow_account:
                await _fill_signup_fields(page)
            controls = await _controls(page, origin, allow_account=allow_account)
            safe = [
                item
                for item in controls
                if item["label"] not in tried and not hands_off(item["label"], allow_account=allow_account)
            ]
            if not safe:
                on_note("I don't see another safe control on this screen.")
                break
            chosen = _prefer_account(safe) if allow_account else None
            if chosen is None and allow_account:
                chosen = _prefer_next(safe)
            if chosen is None:
                label = await _pick(choose, [item["label"] for item in safe])
                chosen = next((item for item in safe if item["label"] == label), safe[0])
            if chosen["label"].lower() != "next":
                tried.add(chosen["label"])
            on_note(_say(f"I'll open {chosen['label']}."))
            await capture(page, on_screen, f"About to open {chosen['label']}")
            opened = await _press(page, chosen, origin)
            if opened and chosen["label"].lower() == "next":
                try:
                    await page.wait_for_function(
                        """(previous) => {
                          const node = document.querySelector("h1");
                          const text = node ? node.innerText.trim() : "";
                          return text && text !== previous;
                        }""",
                        arg=heading,
                        timeout=2500,
                    )
                except PlaywrightTimeoutError:
                    pass
            if not opened:
                on_note(_say(f"I left {chosen['label']} alone and stayed on this site."))
                await _open(page, start_url, origin)
                continue
            await _dismiss(page)
            after_title, after_heading = await _seen(page)
            if chosen["label"].lower() == "next" and after_heading == heading:
                on_note("The form stayed on this step, so I stopped.")
                break
            title, heading = after_title, after_heading
            await capture(page, on_screen, f"Now showing {title or chosen['label']}")
            on_note(_say(f"Now I see {title or chosen['label']}. The heading is {heading or 'blank'}."))
            steps.append(StepResult(action="look", passed=True, actual=redact(chosen["label"], 80)))
        verdict, classification, kind, reason = judge(steps)
        return [
            CaseResult(
                test_case_id=case_id(origin or start_url, "hands"),
                title="Look through the app the way a person would",
                provenance=Provenance.HEURISTIC,
                verdict=verdict,
                classification=classification,
                failure_kind=kind,
                reason=reason,
                steps=steps,
                environment=environment,
            )
        ]
    except (PlaywrightError, ScopeError) as exc:
        steps.append(StepResult(action="look", passed=False, error=redact(exc, 200), kind="runner"))
        verdict, classification, kind, reason = judge(steps)
        return [
            CaseResult(
                test_case_id=case_id(origin or start_url, "hands"),
                title="Look through the app the way a person would",
                provenance=Provenance.HEURISTIC,
                verdict=verdict,
                classification=classification,
                failure_kind=kind,
                reason=reason,
                steps=steps,
                environment=environment,
            )
        ]
    finally:
        await context.close()


async def _pick(choose, labels: list[str]) -> str:
    if choose is None or not labels:
        return labels[0] if labels else ""
    try:
        picked = await choose(labels, [])
    except Exception:
        return labels[0]
    if isinstance(picked, list):
        for item in picked:
            if item in labels:
                return item
    return labels[0]


async def _press(page, control: dict, origin: str) -> bool:
    label = control["label"]
    href = control.get("href") or ""
    try:
        locator = page.get_by_role("link", name=label, exact=True)
        if await locator.count() == 0:
            locator = page.get_by_role("button", name=label, exact=True)
        if await locator.count() == 0:
            if not href or not _host_ok(origin, href) or path_is_destructive(href):
                return False
            await page.goto(href, wait_until="domcontentloaded")
        else:
            await locator.first.click(timeout=3000)
            await page.wait_for_timeout(800)
        return _host_ok(origin, page.url)
    except PlaywrightError:
        try:
            await page.go_back(wait_until="domcontentloaded", timeout=4000)
        except PlaywrightError:
            pass
        return False


async def _open(page, url: str, origin: str) -> None:
    check_target(url, authorized=_origin(origin))
    try:
        await page.goto(url, wait_until="domcontentloaded")
    except PlaywrightTimeoutError:
        pass


def _prefer_next(controls: list[dict]) -> dict | None:
    for item in controls:
        if item["label"].lower() == "next":
            return item
    return None


def _prefer_account(controls: list[dict]) -> dict | None:
    for needle in ("create account", "for my personal use", "for myself"):
        for item in controls:
            if needle in item["label"].lower():
                return item
    return None


def _host_ok(origin: str, url: str) -> bool:
    """Stay on the start host, or on Gmail's own account host.

    The account URL's query is long. Host is read directly so that length is not a rejection.
    """
    try:
        start = _origin(origin)
    except ScopeError:
        return False
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme not in {"http", "https"} or not host:
        return False
    if host == start.host:
        return True
    return host in _ACCOUNT_HOSTS.get(start.host, frozenset())


async def _verification_wall(page) -> str:
    try:
        text = (await page.locator("body").inner_text(timeout=1000)).lower()
    except PlaywrightError:
        return ""
    if "captcha" in text or "i'm not a robot" in text or "verify it's you" in text:
        return "Google asked for a verification check. The new user was not created."
    if "verify your phone" in text or "enter the code" in text or "add a phone" in text:
        return "Google asked for a phone number. The new user was not created."
    if "phone number" in text and "email or phone" not in text:
        return "Google asked for a phone number. The new user was not created."
    if "could not create your google account" in text:
        return "Google could not create the account. The new user was not created."
    return ""


async def _fill_signup_fields(page) -> None:
    """Fill a visible account form. Phone numbers and message fields stay empty."""
    try:
        text = (await page.locator("body").inner_text(timeout=1000)).lower()
    except PlaywrightError:
        return
    if "first name" in text or "username" in text:
        await _fill_named(page)
    if "gmail address" in text:
        await _pick_suggested_address(page)
    await _fill_birthday(page)
    await _fill_password(page)


async def _pick_suggested_address(page) -> None:
    """Use one address Google offers. Do not invent a mailbox the user asked to write to."""
    try:
        radios = page.locator("input[type='radio']")
        for index in range(await radios.count()):
            value = (await radios.nth(index).get_attribute("value") or "").strip().lower()
            if value and value != "custom":
                await radios.nth(index).check()
                return
    except PlaywrightError:
        return


async def _fill_named(page) -> None:
    values = {
        "first name": "QA",
        "last name": "Tester",
        "username": "qa" + token_hex(3),
    }
    for hint, value in values.items():
        field = page.get_by_label(hint, exact=False)
        try:
            if await field.count() == 0:
                continue
            current = await field.first.input_value()
            if current:
                continue
            await field.first.fill(value)
        except PlaywrightError:
            continue


async def _fill_birthday(page) -> None:
    try:
        day = page.get_by_label("Day", exact=True)
        year = page.get_by_label("Year", exact=True)
        if await day.count() and not (await day.first.input_value()):
            await day.first.fill("1")
        if await year.count() and not (await year.first.input_value()):
            await year.first.fill("1990")
        for box, choice in (("Month", "January"), ("Gender", "Rather not say")):
            combo = page.get_by_role("combobox").filter(has_text=box)
            if await combo.count() == 0:
                continue
            if choice.lower() in (await combo.first.inner_text()).lower():
                continue
            await combo.first.click(timeout=2000)
            option = page.get_by_role("option", name=choice, exact=True)
            if await option.count():
                await option.first.click(timeout=2000)
    except PlaywrightError:
        return


async def _fill_password(page) -> None:
    try:
        text = (await page.locator("body").inner_text(timeout=1000)).lower()
        if "password" not in text:
            return
        boxes = page.locator("input[type='password']")
        await boxes.first.wait_for(state="visible", timeout=2000)
        secret = "QaTester1990!"
        for index in range(await boxes.count()):
            box = boxes.nth(index)
            if await box.input_value():
                continue
            await box.fill(secret)
    except PlaywrightError:
        return


async def _controls(page, origin: str, *, allow_account: bool = False) -> list[dict]:
    try:
        raw = await page.evaluate(
            """() => {
              const header = document.querySelector("header, nav, [role='navigation']");
              let nodes = header ? [...header.querySelectorAll("a[href], button")] : [];
              if (nodes.length < 2) nodes = [...document.body.querySelectorAll("a[href], button")];
              nodes = nodes.slice(0, 30);
              return nodes.map((node) => ({
                label: (node.innerText || node.getAttribute("aria-label") || "").replace(/\\s+/g, " ").trim().slice(0, 80),
                href: node.tagName === "A" ? (node.getAttribute("href") || "") : ""
              }));
            }"""
        )
    except PlaywrightError:
        return []
    found: list[dict] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        label = " ".join(str(item.get("label") or "").split())
        if not label or label in seen or hands_off(label, allow_account=allow_account):
            continue
        href = str(item.get("href") or "").strip()
        if href and not href.startswith("#"):
            absolute = urljoin(page.url, href)
            if not _host_ok(origin, absolute) or path_is_destructive(absolute):
                continue
            href = absolute.split("#", 1)[0]
        seen.add(label)
        found.append({"label": label, "href": href})
        if len(found) >= 12:
            break
    return found


async def _dismiss(page) -> None:
    for label in ("Reject all", "Accept all", "Got it"):
        button = page.get_by_role("button", name=label, exact=True)
        try:
            if await button.count():
                await button.first.click(timeout=1500)
                return
        except PlaywrightError:
            return


async def _seen(page) -> tuple[str, str]:
    try:
        title = " ".join((await page.title()).split())[:120]
    except PlaywrightError:
        title = ""
    try:
        heading = " ".join((await page.locator("h1").first.inner_text(timeout=1000)).split())[:120]
    except PlaywrightError:
        heading = ""
    return title, heading


def _say(text: str) -> str:
    return redact(text, 180)


def _origin(value: str):
    from app.security.url_policy import origin_of

    return origin_of(value if "://" in value else f"https://{value}")
