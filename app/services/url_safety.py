"""SSRF guard for user-supplied URLs (July 2026).

/library/add-url fetches whatever address the user gives it. Without checks
that includes cloud metadata endpoints (169.254.169.254), localhost, and
private-network hosts — i.e. the server can be used to probe its own
infrastructure. Every user link must pass validate_public_url() before any
request is made, and fetch_public_url() re-validates every redirect hop and
caps the download size.
"""
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import requests

# An article page beyond this is either not an article or an attack.
MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5


class UnsafeUrlError(ValueError):
    """Raised with a user-presentable reason when a URL is rejected."""


def _assert_public_host(host: str) -> None:
    # Resolve every record — attacker-controlled DNS can point a friendly
    # hostname at an internal address.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise UnsafeUrlError("That address could not be found.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise UnsafeUrlError("That link doesn't point to a public web page.")


def validate_public_url(url: str) -> None:
    """Reject anything that isn't an http(s) link to a publicly-routable host."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrlError("Only http:// and https:// links are supported.")
    if not parsed.hostname:
        raise UnsafeUrlError("That doesn't look like a valid link.")
    _assert_public_host(parsed.hostname)


def fetch_public_url(url: str, headers: dict = None, timeout: int = 15) -> requests.Response:
    """GET a user-supplied URL with SSRF protections.

    Redirects are followed manually so each hop is re-validated (a public URL
    may redirect into a private network), and the body is streamed with a hard
    byte cap so one huge page can't exhaust the process's memory.
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        validate_public_url(current)
        resp = requests.get(
            current,
            headers=headers,
            timeout=timeout,
            stream=True,
            allow_redirects=False,
        )
        if resp.is_redirect or resp.is_permanent_redirect:
            location = resp.headers.get("Location")
            resp.close()
            if not location:
                raise UnsafeUrlError("That link redirects somewhere unreadable.")
            current = urljoin(current, location)
            continue

        resp.raise_for_status()
        chunks, size = [], 0
        for chunk in resp.iter_content(chunk_size=65536):
            size += len(chunk)
            if size > MAX_DOWNLOAD_BYTES:
                resp.close()
                raise UnsafeUrlError("That page is too large to import.")
            chunks.append(chunk)
        # Hand the capped body back through the normal requests API (.text
        # keeps its charset detection).
        resp._content = b"".join(chunks)
        return resp

    raise UnsafeUrlError("That link redirects too many times.")
