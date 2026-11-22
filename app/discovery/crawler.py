"""Bounded Playwright crawl. The extractor is fixed code, not model input.

It records control metadata and visible text. It does not read input values.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any
from urllib.parse import urljoin

from playwright.async_api import Browser, Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.discovery.challenge import challenge_reason
from app.discovery.routes import routes_from_script
from app.execution.screen import capture
from app.models import Discovery, DiscoveredPage, FormField, FormMeta, LinkInfo
from app.security.text import noise_console, normalize_label, redact
from app.security.url_policy import (
    Origin,
    ScopeError,
    canonical_url,
    check_target,
    origin_of,
    path_is_destructive,
    sensitive_query,
)

# Safety boundary: this script is reviewed with the pilot. It must not read
# `.value` from inputs. Button labels come from text or aria-label only.
_EXTRACT_JS = """
() => {
  const textOf = (el) => (el.innerText || el.textContent || "").trim();
  const headings = [...document.querySelectorAll("h1, h2, h3")].slice(0, 30).map((h) =>
    textOf(h).slice(0, 200)
  );
  const h1 = document.querySelector("h1");
  const primaryHeading = h1 ? textOf(h1).slice(0, 200) : "";
  const links = [...document.querySelectorAll("a[href]")].slice(0, 100).map((a) => ({
    label: textOf(a).slice(0, 120),
    href: a.getAttribute("href") || ""
  }));
  const buttons = [...document.querySelectorAll("button")].slice(0, 40).map((b) =>
    (b.innerText || b.getAttribute("aria-label") || "").trim().slice(0, 120)
  );
  const forms = [...document.forms].slice(0, 20).map((f) => ({
    method: (f.getAttribute("method") || "get").slice(0, 12),
    action: f.getAttribute("action") || "",
    fields: [...f.elements].slice(0, 40).map((el) => ({
      tag: (el.tagName || "").toLowerCase(),
      type: (el.getAttribute("type") || "").slice(0, 40),
      name: (el.getAttribute("name") || "").slice(0, 80),
      required: !!el.required
    }))
  }));
  const meta = document.querySelector('meta[name="app-version"]');
  const appVersion = meta ? (meta.getAttribute("content") || "").slice(0, 40) : "";
  const bug = document.querySelector("[data-qa-bug]");
  const declaredBug = bug ? (bug.getAttribute("data-qa-bug") || "").slice(0, 200) : "";
  const scripts = [...document.querySelectorAll("script[src]")].slice(0, 20).map((s) => s.src || "");
  return { headings, primaryHeading, links, buttons, forms, appVersion, declaredBug, scripts };
}
"""


_CONSENT_JS = """
() => {
  const labels = ["reject all", "decline all", "only essential cookies", "accept all", "allow all", "accept cookies", "i agree", "got it"];
  const nodes = [...document.querySelectorAll("button, [role='button']")];
  for (const wanted of labels) {
    const match = nodes.find((node) => ((node.innerText || node.getAttribute("aria-label") || "").trim().toLowerCase()) === wanted);
    if (match) {
      match.click();
      return wanted;
    }
  }
  return "";
}
"""


def _scrub_forms(raw: list[dict[str, Any]]) -> list[FormMeta]:
    forms: list[FormMeta] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        fields: list[FormField] = []
        for field in item.get("fields") or []:
            if not isinstance(field, dict):
                continue
            field.pop("value", None)
            fields.append(
                FormField(
                    tag=normalize_label(field.get("tag", ""), 40),
                    type=normalize_label(field.get("type", ""), 40),
                    name=normalize_label(field.get("name", ""), 80),
                    required=bool(field.get("required")),
                )
            )
        forms.append(
            FormMeta(
                method=normalize_label(item.get("method", "get"), 12).lower() or "get",
                action=normalize_label(item.get("action", ""), 300),
                fields=fields,
            )
        )
    return forms


async def crawl(
    browser: Browser,
    target_url: str,
    *,
    max_pages: int,
    max_depth: int,
    navigation_timeout_ms: int,
    cancel_check: Any | None = None,
    on_screen: Any | None = None,
) -> Discovery:
    origin = origin_of(target_url)
    discovery = Discovery(target_url=target_url, origin=origin.origin)
    context = await browser.new_context(
        viewport={"width": 1280, "height": 720},
        locale="en-GB",
        timezone_id="UTC",
        ignore_https_errors=False,
        accept_downloads=False,
    )
    queue: deque[tuple[str, int]] = deque([(target_url, 0)])
    seen: set[str] = set()
    scripts_read = False
    try:
        while queue and len(discovery.pages) < max_pages:
            if cancel_check and cancel_check():
                discovery.stopped_reason = "cancelled"
                break
            url, depth = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            page_record = await _visit(
                context,
                url,
                origin,
                depth,
                navigation_timeout_ms,
                on_screen,
            )
            discovery.pages.append(page_record)
            if page_record.redirect_escape or page_record.load_error:
                continue
            if not scripts_read:
                scripts_read = True
                await _enqueue_script_routes(
                    context,
                    getattr(page_record, "_script_srcs", []),
                    origin,
                    depth,
                    seen,
                    queue,
                    discovery,
                )
            if depth >= max_depth:
                continue
            for link in page_record.links:
                if not link.same_origin or link.skipped_reason:
                    if link.href and not link.same_origin:
                        discovery.external_links.append(link.href)
                    if link.skipped_reason.startswith("denylist"):
                        discovery.denylisted.append(link.href)
                    continue
                if link.href not in seen:
                    queue.append((link.href, depth + 1))
    finally:
        await context.close()
    return discovery


async def _visit(
    context: Any,
    url: str,
    origin: Origin,
    depth: int,
    timeout_ms: int,
    on_screen: Any | None = None,
) -> DiscoveredPage:
    started = time.monotonic()
    page = await context.new_page()
    console_errors: list[str] = []
    page_errors: list[str] = []

    def on_console(msg: Any) -> None:
        if getattr(msg, "type", "") == "error" and not noise_console(msg.text):
            console_errors.append(redact(msg.text, 300))

    def on_pageerror(exc: Any) -> None:
        page_errors.append(redact(exc, 300))

    page.on("console", on_console)
    page.on("pageerror", on_pageerror)
    record = DiscoveredPage(url=url, depth=depth)
    try:
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            record.load_error = "navigation timeout"
            record.elapsed_ms = int((time.monotonic() - started) * 1000)
            await capture(page, on_screen, f"Timed out opening {url}")
            return record
        except PlaywrightError as exc:
            record.load_error = redact(exc, 300) or "browser error"
            record.elapsed_ms = int((time.monotonic() - started) * 1000)
            return record
        final = page.url
        record.final_url = redact(final, 500)
        record.http_status = response.status if response is not None else None
        try:
            check_target(final, authorized=origin)
        except ScopeError:
            # Safety boundary: do not keep body text from a page that left the origin.
            record.redirect_escape = True
            record.text_excerpt = ""
            record.elapsed_ms = int((time.monotonic() - started) * 1000)
            return record
        try:
            await page.evaluate(_CONSENT_JS)
        except PlaywrightError:
            pass
        try:
            await page.wait_for_function(
                "() => (document.body && (document.body.innerText || '')).trim().length > 20",
                timeout=min(timeout_ms, 4000),
            )
        except PlaywrightTimeoutError:
            pass
        record.title = normalize_label(await page.title(), 200)
        try:
            raw = await page.evaluate(_EXTRACT_JS)
        except PlaywrightError:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        record.app_version = normalize_label(raw.get("appVersion", ""), 40)
        headings = []
        for heading in raw.get("headings") or []:
            label = normalize_label(heading, 200)
            if label:
                headings.append(label)
        record.headings = headings
        record.primary_heading = normalize_label(raw.get("primaryHeading", ""), 200)
        try:
            excerpt = await page.locator("body").inner_text(timeout=2000)
        except PlaywrightError:
            excerpt = ""
        record.text_excerpt = redact(excerpt, 1200)
        record.challenge = challenge_reason(record.title, record.primary_heading, record.text_excerpt)
        record.buttons = [normalize_label(item, 120) for item in (raw.get("buttons") or []) if normalize_label(item, 120)]
        record.forms = _scrub_forms(raw.get("forms") or [])
        record.links = _links(url, origin, raw.get("links") or [])
        record._script_srcs = [str(item) for item in (raw.get("scripts") or []) if isinstance(item, str)]  # type: ignore[attr-defined]
        await capture(page, on_screen, f"Opened {record.title or url}")
        # Give a late console.error a moment to land, then copy the lists.
        await page.wait_for_timeout(200)
        record.console_errors = list(console_errors)
        record.page_errors = list(page_errors)
        declared = normalize_label(raw.get("declaredBug") or "", 200)
        if declared and declared not in record.page_errors:
            record.page_errors.append(declared)
        record.elapsed_ms = int((time.monotonic() - started) * 1000)
        return record
    finally:
        await page.close()


async def _enqueue_script_routes(
    context: Any,
    script_srcs: list[str],
    origin: Origin,
    depth: int,
    seen: set[str],
    queue: deque[tuple[str, int]],
    discovery: Discovery,
) -> None:
    """Open public screens the app navigates to, even when they are not <a href>."""
    found: list[str] = []
    for src in script_srcs[:8]:
        try:
            script_url = check_target(src, authorized=origin)
        except ScopeError:
            continue
        if not script_url.lower().split("?", 1)[0].endswith(".js"):
            continue
        try:
            response = await context.request.get(script_url, timeout=15000)
            body = await response.body()
        except PlaywrightError:
            continue
        if len(body) > 3_000_000:
            continue
        keep, skip = routes_from_script(body.decode("utf-8", "replace"))
        for path in skip:
            absolute = _join_route(origin, path)
            if absolute and absolute not in discovery.denylisted:
                discovery.denylisted.append(absolute)
        found.extend(keep)
    if not found:
        return
    discovery.coverage_notes.append(
        "Public screens include routes the app itself navigates to, not only ordinary links."
    )
    for path in found:
        absolute = _join_route(origin, path)
        if not absolute or absolute in seen:
            continue
        try:
            check_target(absolute, authorized=origin)
        except ScopeError:
            continue
        if path_is_destructive(absolute):
            if absolute not in discovery.denylisted:
                discovery.denylisted.append(absolute)
            continue
        queue.append((absolute, depth + 1))


def _join_route(origin: Origin, path: str) -> str:
    try:
        return canonical_url(f"{origin.origin}{path}")
    except ScopeError:
        return ""


def _links(page_url: str, origin: Origin, raw_links: list[Any]) -> list[LinkInfo]:
    found: list[LinkInfo] = []
    seen: set[str] = set()
    for item in raw_links:
        if not isinstance(item, dict):
            continue
        label = normalize_label(item.get("label", ""), 120)
        href = str(item.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        absolute = urljoin(page_url, href)
        try:
            canonical = canonical_url(absolute)
            check_target(canonical, authorized=origin)
        except ScopeError:
            safe_href = redact(absolute.split("#", 1)[0], 300)
            if safe_href and safe_href not in seen:
                seen.add(safe_href)
                found.append(LinkInfo(label=label or "(unlabelled)", href=safe_href, same_origin=False, skipped_reason="off-origin"))
            continue
        if sensitive_query(canonical):
            found.append(
                LinkInfo(label=label or "(unlabelled)", href=origin.origin, same_origin=True, skipped_reason="sensitive query")
            )
            continue
        reason = ""
        if path_is_destructive(canonical):
            reason = "denylist"
        if canonical in seen:
            continue
        seen.add(canonical)
        found.append(LinkInfo(label=label or "(unlabelled)", href=canonical, same_origin=True, skipped_reason=reason))
    return found
