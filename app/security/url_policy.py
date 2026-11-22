"""SSRF and same-origin policy.

Loopback and private addresses are allowed only when the user explicitly
authorizes that exact host and port. Link-local and cloud-metadata addresses
are never targets, including after a redirect. Page URLs are not file paths.
"""

from __future__ import annotations

import ipaddress
import posixpath
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from app.security.text import sanitize_text

METADATA_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata.internal",
        "instance-data",
    }
)
ALWAYS_BLOCKED_IPS = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("169.254.170.2"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)


class ScopeError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(sanitize_text(message, 300))


@dataclass(frozen=True)
class Origin:
    scheme: str
    host: str
    port: int

    @property
    def netloc(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.netloc}"


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _canonical_host(hostname: str) -> str:
    if hostname.endswith("."):
        raise ScopeError("host must not end with a dot")
    try:
        return ipaddress.ip_address(hostname).compressed
    except ValueError:
        pass
    try:
        idna = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ScopeError("host is not valid") from exc
    labels = idna.split(".")
    if not labels or any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not all(ch.isalnum() or ch == "-" for ch in label)
        for label in labels
    ):
        raise ScopeError("host is not a valid name or address")
    if len(labels) == 1 and labels[0].isdigit():
        raise ScopeError("numeric host is not allowed")
    return idna


def _is_metadata_or_link_local(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip in ALWAYS_BLOCKED_IPS:
        return True
    if ip.version == 4:
        return ip in ipaddress.ip_network("169.254.0.0/16") or ip in ipaddress.ip_network("0.0.0.0/8")
    return ip in ipaddress.ip_network("fe80::/10") or ip.is_unspecified


def _is_special(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return [ipaddress.ip_address(host)]
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ScopeError("host did not resolve") from exc
    found: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if ip not in found:
            found.append(ip)
    if not found:
        raise ScopeError("host did not resolve")
    return found


def _ip_allowed(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
    origin: Origin,
    origin_ips: set[str],
) -> bool:
    if _is_metadata_or_link_local(ip) or ip.is_multicast or ip.is_reserved:
        return False
    if not _is_special(ip):
        return ip.compressed in origin_ips
    # Private and loopback addresses match only the authorized host's own addresses.
    return ip.compressed in origin_ips


def _split(url: str) -> tuple[str, str, int, str, str]:
    if not isinstance(url, str) or not url or len(url) > 2000:
        raise ScopeError("URL is empty or too long")
    if "\x00" in url or "\\" in url or any(ch.isspace() for ch in url):
        raise ScopeError("URL contains a forbidden character")
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:
        raise ScopeError("URLs must not contain credentials")
    scheme = (parts.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise ScopeError("only http and https URLs are allowed")
    if parts.hostname is None:
        raise ScopeError("URL has no host")
    host = _canonical_host(parts.hostname)
    if host in METADATA_HOSTS:
        raise ScopeError("metadata hosts are not allowed")
    try:
        port = parts.port if parts.port is not None else _default_port(scheme)
    except ValueError as exc:
        raise ScopeError("URL port is not valid") from exc
    if port < 1 or port > 65535:
        raise ScopeError("URL port is not valid")
    raw_path = parts.path or "/"
    path = _canonical_path(raw_path)
    query = parts.query
    if len(query) > 200:
        raise ScopeError("URL query is too long")
    if any(ord(ch) < 32 for ch in query):
        raise ScopeError("URL query contains a control character")
    return scheme, host, port, path, query


def _canonical_path(path: str) -> str:
    from urllib.parse import unquote

    if "\\" in path or "\x00" in path:
        raise ScopeError("URL path is not valid")
    decoded = unquote(path)
    if "\x00" in decoded or "\\" in decoded:
        raise ScopeError("URL path is not valid")
    if not decoded.startswith("/"):
        decoded = "/" + decoded
    segments: list[str] = []
    for segment in decoded.split("/"):
        if segment in {"", "."}:
            segments.append(segment)
            continue
        if segment == "..":
            if len(segments) > 1:
                segments.pop()
            continue
        segments.append(segment)
    # posixpath.normpath would also collapse '..'. The walk above keeps the
    # result on this host; it is still an HTTP path, never a filesystem path.
    normalised = posixpath.normpath("/".join(segments) or "/")
    if not normalised.startswith("/"):
        normalised = "/" + normalised
    if decoded.endswith("/") and normalised != "/":
        normalised += "/"
    return normalised


def canonical_url(url: str) -> str:
    scheme, host, port, path, query = _split(url.strip())
    return urlunsplit((scheme, f"{host}:{port}", path, query, ""))


def origin_of(url: str) -> Origin:
    scheme, host, port, _path, _query = _split(url.strip())
    return Origin(scheme, host, port)


def check_target(url: str, *, authorized: Origin | None = None) -> str:
    """Validate a user-supplied target and return its canonical URL.

    When authorized is None, the URL's own origin is the allow-list (intake).
    A later URL must match that origin exactly, then pass the same IP checks.
    """
    canonical = canonical_url(url)
    origin = origin_of(canonical)
    if authorized is not None and (
        origin.scheme != authorized.scheme
        or origin.host != authorized.host
        or origin.port != authorized.port
    ):
        raise ScopeError("URL is outside the authorized origin")
    ips = _resolve(origin.host, origin.port)
    allowed = {ip.compressed for ip in ips}
    for ip in ips:
        if not _ip_allowed(ip, origin, allowed):
            raise ScopeError("URL address is not allowed for this origin")
    return canonical


def same_origin(url: str, authorized: Origin) -> bool:
    try:
        check_target(url, authorized=authorized)
    except ScopeError:
        return False
    return True


def sensitive_query(url: str) -> bool:
    query = urlsplit(url).query.lower()
    keys = ("token=", "password=", "secret=", "api_key=", "apikey=", "auth=", "session=")
    return any(key in query for key in keys)


DESTRUCTIVE_PATH = (
    "/logout",
    "/log-out",
    "/signout",
    "/sign-out",
    "/delete",
    "/checkout",
    "/payment",
    "/purchase",
    "/transfer",
    "/admin",
)


def path_is_destructive(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return any(part in path for part in DESTRUCTIVE_PATH)
