"""Decide what this application is, and which steps are worth doing once."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.models import DiscoveredPage


@dataclass
class FlowDecision:
    summary: str
    signup_url: str = ""
    signin_url: str = ""
    create_account: bool = False
    sign_in: bool = False
    unique_pages: list[DiscoveredPage] = field(default_factory=list)
    mind: str = "built-in"


def screen_signature(page: DiscoveredPage) -> str:
    title = (page.title or "").strip().lower()
    heading = (page.primary_heading or "").strip().lower()
    if title or heading:
        return f"{title}|{heading}"
    return page.url


def unique_pages(pages: list[DiscoveredPage]) -> list[DiscoveredPage]:
    """Keep one URL per screen. Alias routes that render the same screen are dropped."""
    chosen: dict[str, DiscoveredPage] = {}
    for page in pages:
        if page.redirect_escape:
            continue
        key = screen_signature(page)
        current = chosen.get(key)
        if current is None or _shorter(page, current):
            chosen[key] = page
    return list(chosen.values())


def role_of(page: DiscoveredPage) -> str:
    path = urlsplit(page.url).path.lower()
    heading = (page.primary_heading or "").strip().lower()
    title = (page.title or "").strip().lower()
    if "saas" in path:
        return "other"
    if "/signup" in path or path.endswith("/signup") or heading in {"create account", "sign up"}:
        return "signup"
    if "/login" in path or "/signin" in path or heading in {"sign in", "signin"}:
        return "signin"
    if "privacy" in path or "privacy" in heading:
        return "privacy"
    if "thank" in path or heading.startswith("complete signup") or "thank you" in title:
        return "thanks"
    return "other"


def decide(pages: list[DiscoveredPage], memory: dict) -> FlowDecision:
    unique = unique_pages(pages)
    signup = next((page.url for page in unique if role_of(page) == "signup"), "")
    signin = next((page.url for page in unique if role_of(page) == "signin"), "")
    account = bool(memory.get("signup_done") and memory.get("username"))
    aliases = len([page for page in pages if not page.redirect_escape]) - len(unique)
    if signup and not account:
        summary = (
            "The app has a signup screen, and no account is remembered. "
            "One test account will be created, then used to sign in. "
            "That signup will not be repeated."
        )
        create_account = True
        sign_in = False
    elif account and (signin or memory.get("entered_url")):
        summary = (
            f"An account named {memory.get('username')} is already remembered. "
            "The run will reuse that session, or sign in once. It will not sign up again."
        )
        create_account = False
        sign_in = True
    else:
        summary = "No signup screen was found, so the run checks the public screens it opened."
        create_account = False
        sign_in = False
    if aliases > 0:
        summary += f" {aliases} repeated views of the same screen were skipped."
    return FlowDecision(
        summary=summary,
        signup_url=signup,
        signin_url=signin,
        create_account=create_account,
        sign_in=sign_in,
        unique_pages=unique,
    )


def _shorter(candidate: DiscoveredPage, current: DiscoveredPage) -> bool:
    return len(urlsplit(candidate.url).path) < len(urlsplit(current.url).path)
