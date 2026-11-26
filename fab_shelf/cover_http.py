"""Reuse HTTPS connections on the cover workers, independently of file downloads."""
from collections import OrderedDict
import http.client
import ssl
import threading
import time
from urllib.parse import urljoin, urlsplit

from . import network


MAX_COVER_BYTES = 8_000_000
REDIRECTS = {301, 302, 303, 307, 308}
_workers = threading.local()
_tls_lock = threading.Lock()
_tls_context = None


def _tls():
    # Windows certificate-store loading is shared by all six cover workers.
    global _tls_context
    with _tls_lock:
        now = time.monotonic()
        if _tls_context is None or _tls_context[1] <= now:
            context = ssl.create_default_context()
            context.set_alpn_protocols(["http/1.1"])
            _tls_context = (context, now + 60)
        return _tls_context[0]


def _pool():
    if not hasattr(_workers, "connections"):
        _workers.connections = OrderedDict()
    connections = _workers.connections
    cutoff = time.monotonic() - 20
    while connections and next(iter(connections.values()))[1] < cutoff:
        _, (connection, _) = connections.popitem(last=False)
        connection.close()
    return connections


def download(url, mode, proxy, timeout=12):
    connections = _pool()
    deadline = time.monotonic() + timeout
    for _ in range(5):
        try:
            parsed = urlsplit(url)
            port = parsed.port or 443
            if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None:
                return None
            route = network.resolve_route(mode, proxy, url)
        except (OSError, ValueError):
            return None
        key = (route.signature, parsed.hostname, port)
        entry = connections.pop(key, None)
        connection = entry[0] if entry else None
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if connection:
                    connection.close()
                return None
            keep = False
            try:
                if connection is None:
                    if mode == "auto" and not entry:
                        connection, route = network.connect_automatic(url, proxy, remaining, context=_tls())
                        key = (route.signature, parsed.hostname, port)
                    else:
                        connection = network.connection(url, route, remaining, context=_tls())
                elif connection.sock is not None:
                    connection.sock.settimeout(remaining)
                connection.request("GET", target, headers={"User-Agent": "Mozilla/5.0", "Accept": "image/*"})
                with connection.getresponse() as response:
                    if response.status in REDIRECTS:
                        location = response.getheader("Location")
                        if not location:
                            return None
                        url = urljoin(url, location)
                        break
                    if response.status != 200 or (response.length or 0) > MAX_COVER_BYTES:
                        return None
                    expected = response.length
                    data = response.read(MAX_COVER_BYTES + 1)
                    if len(data) > MAX_COVER_BYTES or (expected is not None and len(data) != expected):
                        return None
                    keep = not response.will_close and response.isclosed() and connection.sock is not None
                    return data
            except (OSError, ValueError, http.client.HTTPException):
                # An idle server can close a reusable socket before the next GET.
                if not entry or attempt:
                    return None
                if connection is not None:
                    connection.close()
                connection = None
            finally:
                if keep:
                    if previous := connections.pop(key, None):
                        previous[0].close()
                    connections[key] = (connection, time.monotonic())
                    if len(connections) > 3:
                        _, (evicted, _) = connections.popitem(last=False)
                        evicted.close()
                elif connection is not None:
                    connection.close()
        else:
            return None
    return None
