"""One screenshot for the live run view. A capture failure must not fail the run."""

from __future__ import annotations

from collections.abc import Callable

from playwright.async_api import Error as PlaywrightError


async def capture(page, on_screen: Callable[[bytes, str], None] | None, caption: str) -> None:
    if on_screen is None:
        return
    try:
        data = await page.screenshot(type="png")
    except PlaywrightError:
        return
    if not isinstance(data, (bytes, bytearray)) or not bytes(data).startswith(b"\x89PNG"):
        return
    try:
        on_screen(bytes(data), caption)
    except Exception:
        return
