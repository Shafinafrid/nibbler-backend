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
import ssl
from urllib.parse import urljoin, urlparse
from typing import Optional

import requests
import urllib3
from requests.structures import CaseInsensitiveDict

# An article page beyond this is either not an article or an attack.
MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5


class UnsafeUrlError(ValueError):
    """Raised with a user-presentable reason when a URL is rejected."""


def _resolve_public_addresses(host: str, port: int) -> list[str]:
    # Resolve every record — attacker-controlled DNS can point a friendly
    # hostname at an internal address.
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise UnsafeUrlError("That address could not be found.")
    addresses = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise UnsafeUrlError("That link doesn't point to a public web page.")
        if str(ip) not in addresses:
            addresses.append(str(ip))
    if not addresses:
        raise UnsafeUrlError("That address could not be found.")
    return addresses


def validate_public_url(url: str) -> None:
    """Reject anything that isn't an http(s) link to a publicly-routable host."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrlError("Only http:// and https:// links are supported.")
    if not parsed.hostname:
        raise UnsafeUrlError("That doesn't look like a valid link.")
    _resolve_public_addresses(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))


def _pinned_get(url: str, headers: Optional[dict], timeout: int):
    """Connect to an address from the exact validated DNS answer.

    The URL hostname is retained for Host and TLS SNI/certificate checks, but
    is never resolved again by the transport. This closes the validation /
    connection DNS-rebinding window.
    """
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = _resolve_public_addresses(host, port)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    request_headers = dict(headers or {})
    default_port = 443 if parsed.scheme == "https" else 80
    request_headers["Host"] = host if port == default_port else f"{host}:{port}"

    last_error = None
    for address in addresses:
        pool = None
        try:
            if parsed.scheme == "https":
                pool = urllib3.HTTPSConnectionPool(
                    address,
                    port=port,
                    timeout=urllib3.Timeout(connect=timeout, read=timeout),
                    retries=False,
                    cert_reqs=ssl.CERT_REQUIRED,
                    ca_certs=requests.certs.where(),
                    assert_hostname=host,
                    server_hostname=host,
                )
            else:
                pool = urllib3.HTTPConnectionPool(
                    address,
                    port=port,
                    timeout=urllib3.Timeout(connect=timeout, read=timeout),
                    retries=False,
                )
            raw = pool.urlopen(
                "GET", path, headers=request_headers,
                redirect=False, preload_content=False,
            )
            return raw, pool
        except Exception as exc:
            last_error = exc
            if pool is not None:
                pool.close()
    raise requests.RequestException(f"Could not connect to that public page: {last_error}")


def fetch_public_url(url: str, headers: dict = None, timeout: int = 15) -> requests.Response:
    """GET a user-supplied URL with SSRF protections.

    Redirects are followed manually so each hop is re-validated (a public URL
    may redirect into a private network), and the body is streamed with a hard
    byte cap so one huge page can't exhaust the process's memory.
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        validate_public_url(current)
        raw, pool = _pinned_get(current, headers, timeout)
        if raw.status in {301, 302, 303, 307, 308}:
            location = raw.headers.get("Location")
            raw.close()
            pool.close()
            if not location:
                raise UnsafeUrlError("That link redirects somewhere unreadable.")
            current = urljoin(current, location)
            continue

        resp = requests.Response()
        resp.status_code = raw.status
        resp.headers = CaseInsensitiveDict(raw.headers)
        resp.url = current
        chunks, size = [], 0
        try:
            for chunk in raw.stream(65536):
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise UnsafeUrlError("That page is too large to import.")
                chunks.append(chunk)
        finally:
            raw.close()
            pool.close()
        # Hand the capped body back through the normal requests API (.text
        # keeps its charset detection).
        resp._content = b"".join(chunks)
        resp.encoding = requests.utils.get_encoding_from_headers(resp.headers)
        resp.raise_for_status()
        return resp

    raise UnsafeUrlError("That link redirects too many times.")
