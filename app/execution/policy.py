"""Re-check every step before a browser action runs."""

from __future__ import annotations

from app.models import TestCase
from app.security.url_policy import Origin, ScopeError, check_target, path_is_destructive

ALLOWED_ACTIONS = frozenset(
    {
        "goto",
        "assert_http_status",
        "assert_title",
        "assert_heading",
        "assert_text",
        "assert_no_page_errors",
    }
)


class PolicyError(Exception):
    pass


def gate(case: TestCase, origin: Origin, allowed_urls: set[str]) -> str:
    """Return a block reason, or an empty string when the case may run."""
    if case.blocked_reason:
        return case.blocked_reason
    if not case.steps:
        return "case has no executable steps"
    for step in case.steps:
        if step.action not in ALLOWED_ACTIONS:
            return "action is not in the allowed set"
        if step.action != "goto":
            continue
        try:
            url = check_target(step.target, authorized=origin)
        except ScopeError:
            return "goto is outside the authorized origin"
        if path_is_destructive(url) or url not in allowed_urls:
            return "goto is not a discovered in-scope URL"
    return ""
