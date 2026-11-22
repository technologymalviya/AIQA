"""Click through a site the way a person would, and say what is happening.

The hands stay on the authorized origin. They do not submit forms, pay, or open an account.
"""

from __future__ import annotations

from urllib.parse import urljoin

from playwright.async_api import Browser, Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.execution.screen import capture
from app.execution.verdict import judge
from app.generation.heuristic import case_id
from app.models import CaseResult, Provenance, StepResult
from app.security.text import redact
from app.security.url_policy import ScopeError, check_target, path_is_destructive

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
)


def hands_off(label: str) -> bool:
    lowered = " ".join(label.lower().split())
    if not lowered or len(lowered) > 60 or lowered.startswith("skip"):
        return True
    return any(part in lowered for part in _CLOSED)


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
        for _ in range(limit):
            if cancel_check and cancel_check():
                break
            controls = await _controls(page, origin)
            safe = [item for item in controls if item["label"] not in tried and not hands_off(item["label"])]
            if not safe:
                on_note("I don't see another safe control on this screen.")
                break
            label = await _pick(choose, [item["label"] for item in safe])
            chosen = next((item for item in safe if item["label"] == label), safe[0])
            tried.add(chosen["label"])
            on_note(_say(f"I'll open {chosen['label']}."))
            await capture(page, on_screen, f"About to open {chosen['label']}")
            opened = await _press(page, chosen, origin)
            if not opened:
                on_note(_say(f"I left {chosen['label']} alone and stayed on this site."))
                await _open(page, start_url, origin)
                continue
            await _dismiss(page)
            title, heading = await _seen(page)
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
    href = control.get("href") or ""
    try:
        if href:
            check_target(href, authorized=_origin(origin))
            if path_is_destructive(href):
                return False
            await page.goto(href, wait_until="domcontentloaded")
        else:
            locator = page.get_by_role("button", name=control["label"], exact=True)
            if await locator.count() == 0:
                locator = page.get_by_role("link", name=control["label"], exact=True)
            await locator.first.click(timeout=3000)
            await page.wait_for_timeout(500)
        check_target(page.url, authorized=_origin(origin))
        return True
    except (PlaywrightError, ScopeError):
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


async def _controls(page, origin: str) -> list[dict]:
    try:
        raw = await page.evaluate(
            """() => {
              const root = document.querySelector("header, nav, [role='navigation']") || document.body;
              const nodes = [...root.querySelectorAll("a[href], button")].slice(0, 30);
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
        if not label or label in seen or hands_off(label):
            continue
        href = str(item.get("href") or "").strip()
        if href and not href.startswith("#"):
            absolute = urljoin(page.url, href)
            try:
                absolute = check_target(absolute, authorized=_origin(origin))
            except ScopeError:
                continue
            if path_is_destructive(absolute):
                continue
            href = absolute
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
