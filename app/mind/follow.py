"""Follow the user's request one screen at a time.

The model may only choose a button that is on the screen, fill a form that is open,
or sign in as a role the user named. A password is stored for the next sign-in and
is not spoken back.
"""

from __future__ import annotations

import re
import secrets

from playwright.async_api import Browser, Error as PlaywrightError

from app.execution.screen import capture
from app.mind.advisor import next_action
from app.mind.memory import scrub
from app.security.url_policy import ScopeError, check_target

_CLOSED = (
    "pay",
    "purchase",
    "checkout",
    "delete",
    "logout",
    "sign out",
    "upgrade",
    "ticket",
    "whatsapp",
    "send",
    "remove",
)
_NOT_ROLE = ("continue", "show", "forgot", "signup", "back", "privacy", "accept", "reject", "cancel")
_ROLE_ASK = re.compile(
    r"\b(?:sign[\s-]?in|log[\s-]?in)\b(?:\s+first)?\s+as\s+([a-zA-Z][a-zA-Z ]{0,40})",
    re.IGNORECASE,
)


def too_early(label: str, goal: str, finished: list[str]) -> bool:
    """A later step in the request waits until the earlier one has happened."""
    text = (goal or "").lower()
    lowered = label.lower()
    if "create" in text and "copy" in lowered:
        create_at = text.find("create")
        copy_at = text.find("copy")
        if 0 <= create_at < copy_at and not any("filled the open form" in item for item in finished):
            return True
    return False


def order_for_goal(buttons: list[str], goal: str, work: str = "") -> list[str]:
    """Show the control the current part of the request names, then role controls."""
    roles = [role.lower() for role in named_roles(goal)]
    focus = (work or "").lower()

    def rank(label: str) -> tuple[int, int]:
        lowered = label.lower()
        if focus and re.search(rf"\b{re.escape(lowered)}\b", focus):
            return (0, buttons.index(label))
        if any(role in lowered for role in roles):
            return (1, buttons.index(label))
        if lowered.startswith("add "):
            return (2, buttons.index(label))
        return (3, buttons.index(label))

    return sorted(buttons, key=rank)


def named_roles(goal: str) -> list[str]:
    """Roles the user asked to sign in as. A role named with "first" stays first."""
    found: list[str] = []
    for match in _ROLE_ASK.finditer(goal or ""):
        words = re.split(r"\b(?:and|then|to|for|after)\b", match.group(1), maxsplit=1, flags=re.IGNORECASE)[0]
        role = " ".join(words.split())[:40]
        if role and role.lower() not in {item.lower() for item in found}:
            found.append(role)
    opening = first_role(goal)
    if opening:
        found = [opening] + [role for role in found if role.lower() != opening.lower()]
    return found


def open_work(goal: str, finished: list[str]) -> str:
    """The single part of the request that still has to happen."""
    text = (goal or "").lower()
    lowered = [item.lower() for item in finished]
    filled = any("filled the open form" in item for item in lowered)
    pending = [
        role
        for role in named_roles(goal)
        if f"signed in as {role}".lower() not in set(lowered)
    ]
    if "create" in text and not filled:
        return "create what the user asked for: open its button, then fill the form"
    if "copy" in text and not any("copied the credential" in item for item in lowered):
        return "copy the credential shown for the new login"
    if pending:
        return f"sign in as {pending[0]}"
    if re.search(r"\b(check|flow|test)\b", text):
        last = -1
        for index, item in enumerate(lowered):
            if item.startswith("signed in as "):
                last = index
        if not any(item.startswith("opened ") for item in lowered[last + 1 :]):
            return "open a screen for the signed-in role"
    return "the request is complete"


def off_order(action: dict, work: str) -> bool:
    """A step that skips the part of the request that is still open."""
    if work == "the request is complete":
        return action.get("do") != "done"
    if action.get("do") == "done":
        return True
    if work == "fill the open form":
        return action.get("do") != "fill"
    if work.startswith("sign in as "):
        return action.get("do") != "sign_in" or action.get("target", "").lower() not in work.lower()
    if work.startswith("create ") and action.get("do") not in {"click", "fill"}:
        return True
    if work.startswith(("copy the credential", "do what the page says")) and action.get("do") not in {"click", "fill"}:
        return True
    if work.startswith("open a screen") and action.get("do") != "click":
        return True
    return False


def credentials_from_text(text: str) -> dict | None:
    """A username and password the page showed. Instruction lines are not a password."""
    username = ""
    password = ""
    for raw in (text or "").splitlines():
        if ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        key = key.lower().strip(" •\t-*")
        value = value.strip()
        if not value or value.startswith("(") or value.lower().startswith("use "):
            continue
        if "pass" in key:
            password = value
        elif "user" in key:
            username = value
    if username and password:
        return {"username": username, "password": password}
    return None


def first_role(goal: str) -> str:
    """The role to use for the opening sign-in, when the user named one."""
    named_first = re.search(r"\bfirst\s+as\s+([a-zA-Z][a-zA-Z ]{0,40})", goal or "", re.IGNORECASE)
    match = named_first or _ROLE_ASK.search(goal or "")
    if not match:
        return ""
    words = re.split(r"\b(?:and|then|to|for|after)\b", match.group(1), maxsplit=1, flags=re.IGNORECASE)[0]
    return " ".join(words.split())[:40]


def closed_label(label: str) -> bool:
    lowered = label.lower()
    return any(part in lowered for part in _CLOSED)


def grounded_action(payload, *, buttons: list[str], fields: list[str], goal: str) -> dict | None:
    """Keep a model step only when the screen or the user's own words support it."""
    if not isinstance(payload, dict):
        return None
    action = str(payload.get("do") or "").strip().lower()
    target = " ".join(str(payload.get("target") or "").split())
    if action == "done":
        return {"do": "done", "target": ""}
    if action == "fill":
        if not fields:
            return None
        return {"do": "fill", "target": ""}
    if action == "click":
        match = match_label(target, buttons)
        if match is None or closed_label(match):
            return None
        return {"do": "click", "target": match}
    if action == "sign_in":
        if not target or closed_label(target) or target.lower() not in (goal or "").lower():
            return None
        return {"do": "sign_in", "target": target}
    return None


def match_label(wanted: str, labels: list[str]) -> str | None:
    if not wanted:
        return None
    exact = [label for label in labels if label.lower() == wanted.lower()]
    if exact:
        return exact[0]
    prefix = [label for label in labels if label.lower().startswith(wanted.lower())]
    if len(prefix) == 1:
        return prefix[0]
    return None


def plan_fields(labels: list[str]) -> dict[str, str]:
    """Values for an open form. Optional identity numbers stay empty."""
    phone = "9" + "".join(secrets.choice("0123456789") for _ in range(9))
    password = "Qa" + "".join(secrets.choice("23456789abcdefghijkmnopqrstuvwxyz") for _ in range(8))
    planned: dict[str, str] = {}
    for label in labels:
        lowered = label.lower()
        if any(part in lowered for part in ("aadhar", "aadhaar", "license", "licence", "optional", "search")):
            planned[label] = ""
            continue
        if "mobile" in lowered or "phone" in lowered:
            planned[label] = phone
        elif "password" in lowered:
            planned[label] = password
        elif "email" in lowered:
            planned[label] = "qa@example.com"
        elif "user" in lowered or "login" in lowered:
            planned[label] = "qa" + secrets.token_hex(2)
        elif "address" in lowered:
            planned[label] = "QA Lane"
        elif "name" in lowered:
            planned[label] = "QA Route"
        else:
            planned[label] = ""
    return planned


def login_from_fields(planned: dict[str, str]) -> dict | None:
    password = ""
    username = ""
    for label, value in planned.items():
        lowered = label.lower()
        if not value:
            continue
        if "password" in lowered:
            password = value
        elif any(part in lowered for part in ("mobile", "phone", "user", "login")):
            username = username or value
    if not password or not username:
        return None
    return {"username": username, "password": password}


def credentials_for(memory: dict, role: str) -> dict | None:
    """The remembered owner signs in as admin. Anyone else uses a login this run created."""
    if role.lower().startswith("admin"):
        username = str(memory.get("username") or "")
        password = str(memory.get("password") or "")
        if username and password:
            return {"username": username, "password": password}
        return None
    created = memory.get("created_logins") or []
    if not created or not isinstance(created[-1], dict):
        return None
    last = created[-1]
    if last.get("username") and last.get("password"):
        return {"username": str(last["username"]), "password": str(last["password"])}
    return None


async def follow_request(
    page,
    browser: Browser,
    *,
    settings,
    memory: dict,
    goal: str,
    signin_url: str,
    origin: str,
    on_screen,
    on_note,
    cancel_check,
    ask=None,
    limit: int = 16,
) -> None:
    """Carry out the request. Stop when the model is done or the screen cannot support the step."""
    if ask is None:
        ask = lambda screen, finished: next_action(settings, goal, screen, finished)
    finished: list[str] = []
    opening = first_role(goal)
    if opening:
        finished.append(f"signed in as {opening}")
    repeats: dict[str, int] = {}
    rejected_streak = 0
    repeat_streak = 0
    early_streak = 0
    off_streak = 0
    filled_fields: list[str] | None = None
    current = page
    extra = None
    notices: list[str] = []
    copied_blobs: list[str] = []
    last_label = ""
    created_near = ""

    async def on_dialog(dialog) -> None:
        notices.append(dialog.message or "")
        if dialog.default_value:
            copied_blobs.append(dialog.default_value)
        await dialog.accept()

    page.on("dialog", on_dialog)
    try:
        await page.context.grant_permissions(["clipboard-read", "clipboard-write"])
        await page.evaluate(
            """() => {
                window.__qaCopied = "";
                if (!navigator.clipboard || !navigator.clipboard.writeText) return;
                const orig = navigator.clipboard.writeText.bind(navigator.clipboard);
                navigator.clipboard.writeText = async (text) => {
                    window.__qaCopied = String(text || "");
                    try { return await orig(text); } catch (e) { return; }
                };
            }"""
        )
    except PlaywrightError:
        pass
    try:
        for _ in range(limit):
            if cancel_check and cancel_check():
                break
            notice = notices[-1] if notices else ""
            notices.clear()
            try:
                clip = await current.evaluate("window.__qaCopied || ''")
                if clip:
                    copied_blobs.append(clip)
                    await current.evaluate("window.__qaCopied = ''")
            except PlaywrightError:
                pass
            found = next((credentials_from_text(blob) for blob in copied_blobs if credentials_from_text(blob)), None)
            copied_blobs.clear()
            if found and str(found.get("username") or "") == str(memory.get("username") or ""):
                on_note("That copy is the account I'm already signed in as.")
                if last_label and f"opened {last_label}" not in finished:
                    finished.append(f"opened {last_label}")
                    repeats[last_label] = repeats.get(last_label, 0) + 1
                found = None
                notice = ""
            if found:
                known = memory.setdefault("created_logins", [])
                if not known or known[-1].get("username") != found["username"]:
                    known.append(found)
                if not any(item == "copied the credential" for item in finished):
                    finished.append("copied the credential")
                    on_note("I copied the login for the next sign-in.")
            screen = await read_screen(current)
            if filled_fields is not None and screen["fields"] == filled_fields:
                screen["fields"] = []
            opened = {item[len("opened ") :] for item in finished if item.startswith("opened ")}
            work = open_work(goal, finished)
            if notice and not found:
                work = f"do what the page says: {notice[:180]}"
            if screen["fields"] and work.startswith(("create ", "copy the credential", "do what the page says")):
                work = "fill the open form"
            screen["buttons"] = [
                label for label in order_for_goal(screen["buttons"], goal, work) if label not in opened
            ][:18]
            screen["roles"] = list(dict.fromkeys([*named_roles(goal), *screen["roles"]]))
            screen["notice"] = notice
            screen["still"] = work
            if work.startswith("sign in as "):
                screen["roles"] = [work.split("sign in as ", 1)[1]]
                screen["buttons"] = []
                screen["fields"] = []
            elif work == "fill the open form":
                screen["roles"] = []
                screen["buttons"] = []
            elif work.startswith("open a screen"):
                screen["roles"] = []
                screen["fields"] = []
            try:
                payload = await ask(screen, finished)
            except Exception:
                on_note("I could not decide the next step.")
                break
            action = grounded_action(
                payload,
                buttons=screen["buttons"],
                fields=screen["fields"],
                goal=goal,
            )
            if action is None:
                rejected_streak += 1
                rejected = ""
                if isinstance(payload, dict):
                    rejected = " ".join(str(payload.get("target") or "").split())[:40]
                if rejected:
                    finished.append(f"{rejected} is not a button on this screen")
                else:
                    finished.append("that step was not on the screen")
                if rejected_streak >= 3:
                    on_note("The next step was not on this screen, so I stopped.")
                    break
                continue
            rejected_streak = 0
            if off_order(action, work):
                off_streak += 1
                finished.append(f"still to do: {work}")
                if off_streak >= 3:
                    on_note("The next step was not on this screen, so I stopped.")
                    break
                continue
            off_streak = 0
            if action["do"] == "done":
                on_note("That covers the request.")
                break
            if action["do"] == "click":
                label = action["target"]
                last_label = label
                if too_early(label, goal, finished):
                    early_streak += 1
                    finished.append(f"create comes before {label}")
                    if early_streak >= 3:
                        on_note("The same step came up again, so I stopped.")
                        break
                    continue
                early_streak = 0
                if repeats.get(label, 0) >= 1:
                    repeat_streak += 1
                    finished.append(f"already opened {label}")
                    if repeat_streak >= 2:
                        on_note("The same step came up again, so I stopped.")
                        break
                    continue
                repeat_streak = 0
                if not await _press(current, label, created_near):
                    on_note(f"I could not open {label}.")
                    break
                for _wait in range(8):
                    if notices:
                        break
                    try:
                        if await current.evaluate("window.__qaCopied || ''"):
                            break
                    except PlaywrightError:
                        break
                    await current.wait_for_timeout(200)
                if notices:
                    message = " ".join(notices[-1].split())[:160]
                    on_note(f"The page says: {message}")
                    said = f"page said: {message}"
                    if finished.count(said) >= 2:
                        on_note("The same step came up again, so I stopped.")
                        break
                    finished.append(said)
                    continue
                repeats[label] = repeats.get(label, 0) + 1
                heading = await _heading(current)
                on_note(f"I'll open {label}.")
                on_note(f"Now I see {heading or label}.")
                await capture(current, on_screen, f"Opened {label}")
                finished.append(f"opened {label}")
                continue
            if action["do"] == "fill":
                planned = plan_fields(screen["fields"])
                if not await _fill_open_form(current, planned):
                    on_note("I could not fill the open form.")
                    break
                created = login_from_fields(planned)
                if created:
                    memory.setdefault("created_logins", []).append(created)
                    created_near = created_near or str(created.get("username") or "")
                    on_note("I saved the new login for the next sign-in.")
                on_note("I filled the open form and submitted it.")
                await capture(current, on_screen, "Filled the open form")
                filled_fields = list(screen["fields"])
                finished.append("filled the open form")
                try:
                    await current.keyboard.press("Escape")
                    await current.wait_for_timeout(300)
                except PlaywrightError:
                    pass
                continue
            if any(item.lower() == f"signed in as {action['target'].lower()}" for item in finished):
                finished.append(f"already signed in as {action['target']}")
                continue
            creds = credentials_for(memory, action["target"])
            if creds is None:
                note = f"I need login details before I can sign in as {action['target']}."
                if note in finished:
                    on_note(note)
                    break
                on_note(note)
                finished.append(note)
                continue
            switched, context = await _sign_in_as(
                browser,
                signin_url,
                origin,
                action["target"],
                creds,
                settings,
                on_note,
            )
            if switched is None:
                finished.append(f"sign-in as {action['target']} was rejected")
                if finished.count(f"sign-in as {action['target']} was rejected") >= 2:
                    break
                continue
            if extra is not None:
                await extra.close()
            extra = context
            current = switched
            repeats.clear()
            filled_fields = None
            finished.append(f"signed in as {action['target']}")
            await capture(current, on_screen, f"Signed in as {action['target']}")
    finally:
        if extra is not None:
            try:
                await extra.close()
            except PlaywrightError:
                pass


async def read_screen(page) -> dict:
    heading = await _heading(page)
    buttons = await _buttons(page)
    fields = await _fields(page)
    login = heading.lower() in {"sign in", "signin", "log in", "login"}
    roles = []
    if login:
        roles = [
            label
            for label in buttons
            if not label.lower().startswith(_NOT_ROLE) and not closed_label(label)
        ]
    return {"heading": heading, "buttons": buttons, "fields": fields, "roles": roles}


async def _buttons(page) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    try:
        locator = page.get_by_role("button")
        count = min(await locator.count(), 40)
    except PlaywrightError:
        return []
    for index in range(count):
        try:
            lines = (await locator.nth(index).inner_text()).splitlines()
            text = lines[0].strip() if lines else ""
        except PlaywrightError:
            continue
        label = " ".join(text.split())[:40]
        if not label or label in seen or len(label) > 32 or closed_label(label):
            continue
        seen.add(label)
        found.append(label)
        if len(found) >= 40:
            break
    return found


async def _fields(page) -> list[str]:
    root = page.locator("[role='dialog']")
    try:
        if await root.count() == 0:
            root = page.locator("form")
        if await root.count() == 0:
            return []
        labels = await root.last.locator("label").evaluate_all(
            "els => els.map((el) => (el.innerText || '').trim()).filter(Boolean)"
        )
    except PlaywrightError:
        return []
    found: list[str] = []
    for label in labels if isinstance(labels, list) else []:
        text = " ".join(str(label).split())[:40]
        if text and text not in found and text.lower() not in {"language", "filter"}:
            found.append(text)
    return found[:12]


async def _press(page, label: str, near: str = "") -> bool:
    """Press a button. When several match, press the one beside the record just created."""
    try:
        locator = page.get_by_role("button", name=label, exact=True)
        if await locator.count() == 0:
            locator = page.get_by_role("button", name=label, exact=False)
        count = await locator.count()
        if count == 0:
            return False
        chosen = 0
        if near and count > 1:
            best_len = None
            for index in range(count):
                size = await locator.nth(index).evaluate(
                    """(el, near) => {
                        let node = el;
                        for (let step = 0; step < 6 && node; step += 1, node = node.parentElement) {
                            const text = node.innerText || "";
                            if (text.includes(near) && text.length < 500) return text.length;
                        }
                        return 0;
                    }""",
                    near,
                )
                if size and (best_len is None or size < best_len):
                    chosen = index
                    best_len = size
        await locator.nth(chosen).click(timeout=3000)
        return True
    except PlaywrightError:
        return False


async def _fill_open_form(page, planned: dict[str, str]) -> bool:
    root = page.locator("[role='dialog']")
    try:
        if await root.count() == 0:
            root = page.locator("form")
        if await root.count() == 0:
            return False
        form = root.last
        labels = form.locator("label")
        count = await labels.count()
        filled = 0
        for index in range(count):
            label = labels.nth(index)
            text = " ".join((await label.inner_text()).split())
            value = planned.get(text, "")
            if not value:
                continue
            field = label.locator("input:not([type=checkbox]):not([type=hidden]), textarea").first
            if await field.count() == 0:
                field = form.locator("input:not([type=checkbox]):not([type=hidden])").nth(index)
            await field.fill(value)
            filled += 1
        if filled == 0:
            return False
        button = form.get_by_role("button").filter(has_text=re.compile(r"add|save|create|continue", re.I))
        for index in range(await button.count()):
            name = " ".join((await button.nth(index).inner_text()).split())
            if closed_label(name) or name.lower().startswith("add a "):
                continue
            await button.nth(index).click(timeout=3000)
            await page.wait_for_timeout(800)
            return True
    except PlaywrightError:
        return False
    return filled > 0


async def _sign_in_as(browser, signin_url: str, origin: str, role: str, creds: dict, settings, on_note):
    if not signin_url:
        on_note(f"I could not find the sign-in screen for {role}.")
        return None, None
    context = await browser.new_context(viewport={"width": 1280, "height": 720}, locale="en-GB", timezone_id="UTC")
    page = await context.new_page()
    try:
        check_target(signin_url, authorized=_origin(origin) if origin else None)
        await page.goto(signin_url, wait_until="domcontentloaded")
        await _dismiss(page)
        pattern = re.compile(rf"^{re.escape(role)}\b", re.IGNORECASE)
        for kind in ("radio", "button"):
            role_button = page.get_by_role(kind, name=pattern)
            if await role_button.count():
                await role_button.first.click(timeout=2000)
                await page.wait_for_timeout(200)
                break
        username = page.locator("input[placeholder*='username' i], input[name='username']").first
        password = page.locator("input[type='password']").first
        await username.fill(creds["username"])
        await password.fill(creds["password"])
        submit = page.locator("form button[type='submit']").first
        await submit.click(timeout=3000)
        if not await _left_login(page, min(getattr(settings, "navigation_timeout_ms", 8000), 8000)):
            error = await _error_text(page)
            on_note(scrub(error or f"Sign-in as {role} did not open the app.", {"password": creds["password"]}))
            await context.close()
            return None, None
        heading = await _heading(page)
        on_note(f"I'll sign in as {role}.")
        on_note(f"Now I see {heading or role}.")
        return page, context
    except (PlaywrightError, ScopeError):
        on_note(f"I could not sign in as {role}.")
        try:
            await context.close()
        except PlaywrightError:
            pass
        return None, None


async def _left_login(page, timeout_ms: int) -> bool:
    import time

    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        heading = (await _heading(page)).lower()
        if heading and heading not in {"sign in", "signin", "log in", "login"}:
            return True
        try:
            password = page.locator("input[type='password']")
            if await password.count() == 0 or not await password.first.is_visible():
                return True
        except PlaywrightError:
            return False
        await page.wait_for_timeout(250)
    return False


async def _error_text(page) -> str:
    try:
        locator = page.locator(".auth-error")
        if await locator.count() == 0:
            return ""
        return (await locator.first.inner_text()).strip()
    except PlaywrightError:
        return ""


async def _dismiss(page) -> None:
    for label in ("Reject all", "Accept all"):
        button = page.get_by_role("button", name=label, exact=True)
        try:
            if await button.count():
                await button.first.click(timeout=1500)
                return
        except PlaywrightError:
            return


async def _heading(page) -> str:
    try:
        return " ".join((await page.locator("h1").first.inner_text(timeout=1000)).split())[:80]
    except PlaywrightError:
        return ""


def _origin(value: str):
    from app.security.url_policy import origin_of

    return origin_of(value if "://" in value else f"https://{value}")
