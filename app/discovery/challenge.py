"""Recognise a bot wall so it is not reported as the product under test."""

from __future__ import annotations

_PHRASES = (
    "verify you are human",
    "verify you're a human",
    "are you a human",
    "pardon our interruption",
    "checking your browser",
    "enable javascript and cookies to continue",
    "unusual traffic from your computer",
    "just a moment",
    "access denied",
)


def challenge_reason(title: str, heading: str, excerpt: str) -> str:
    blob = " ".join((title or "", heading or "", excerpt or "")).lower()
    if any(phrase in blob for phrase in _PHRASES):
        return "The site showed a browser check, so this screen was not tested as the product."
    return ""
