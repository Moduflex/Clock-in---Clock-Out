"""Rate limiting and kiosk authentication.

The rate limiter is deliberately a small in-process one rather than
Flask-Limiter plus Redis: this system runs as a single Waitress process on a
machine in the office, so an extra service to install, monitor and back up would
cost more than it adds. If the deployment ever grows to several processes, swap
:func:`rate_limit` for Flask-Limiter with a shared backend - nothing else here
depends on the implementation.
"""

from __future__ import annotations

import functools
import hmac
import threading
import time
from collections import defaultdict, deque

from flask import current_app, jsonify, request

_buckets: dict[str, deque[float]] = defaultdict(deque)
_buckets_lock = threading.Lock()


def _client_key(scope: str) -> str:
    # remote_addr is the honest answer on a LAN deployment. Behind a hosting
    # platform's load balancer it is the balancer, which would put every user in
    # one bucket - TRUSTED_PROXY_COUNT installs ProxyFix so this reads the real
    # client again. See _trust_proxy_headers in app/__init__.py.
    return f"{scope}:{request.remote_addr or 'unknown'}"


def check_rate(scope: str, limit: int, window_seconds: int) -> bool:
    """Return True if this caller is within *limit* requests per *window*.

    Counts the current request. Use :func:`within_rate` and
    :func:`record_attempt` instead where only some requests should count.
    """
    if limit <= 0:
        return True
    if not within_rate(scope, limit, window_seconds):
        return False
    record_attempt(scope, window_seconds)
    return True


def within_rate(scope: str, limit: int, window_seconds: int) -> bool:
    """Return True if this caller is under *limit*, without counting anything."""
    if limit <= 0:
        return True
    key = _client_key(scope)
    cutoff = time.monotonic() - window_seconds
    with _buckets_lock:
        bucket = _buckets[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        return len(bucket) < limit


def record_attempt(scope: str, window_seconds: int) -> None:
    """Count one attempt against this caller.

    Kept separate so a limit can be spent on the thing worth limiting - a
    *failed* sign-in - rather than on merely opening the page. A limit that
    counts page views locks people out for reloading the login form, which
    looks exactly like the password being wrong.
    """
    key = _client_key(scope)
    cutoff = time.monotonic() - window_seconds
    with _buckets_lock:
        bucket = _buckets[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        bucket.append(time.monotonic())


def reset_rate_limits() -> None:
    """Clear all buckets - used by the test suite."""
    with _buckets_lock:
        _buckets.clear()


def rate_limit(
    scope: str,
    limit_key: str,
    window_key: str,
    *,
    as_json: bool = True,
    methods: set[str] | None = None,
    record: bool = True,
):
    """Decorator applying a configurable rate limit to a view.

    *methods* limits the check to those HTTP methods, so a page that both shows
    a form and receives it does not spend the allowance on being looked at.
    *record* False checks the limit without counting the request; the view then
    calls :func:`record_attempt` itself, for when only failures should count.
    """

    def decorator(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            if methods is not None and request.method not in methods:
                return view(*args, **kwargs)
            limit = current_app.config.get(limit_key, 0)
            window = current_app.config.get(window_key, 60)
            allowed = check_rate(scope, limit, window) if record else within_rate(
                scope, limit, window
            )
            if not allowed:
                if as_json:
                    return (
                        jsonify(
                            ok=False,
                            code="rate_limited",
                            message="Too many attempts. Please wait a moment.",
                        ),
                        429,
                    )
                return ("Too many attempts. Please wait a moment.", 429)
            return view(*args, **kwargs)

        return wrapper

    return decorator


def kiosk_token_valid(supplied: str | None) -> bool:
    """Constant-time comparison of the kiosk shared secret."""
    expected = current_app.config.get("KIOSK_TOKEN") or ""
    if not expected:
        # No token configured: refuse rather than silently run an open endpoint.
        return False
    if not supplied:
        return False
    return hmac.compare_digest(str(supplied), str(expected))


def require_kiosk_token(view):
    """Reject requests that do not carry the kiosk shared secret.

    The kiosk page itself is public (it must be usable without a login), so the
    endpoint that *writes* attendance rows is gated on a token the page is served
    with. That stops anything else on the network posting made-up scans.
    """

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        supplied = request.headers.get("X-Kiosk-Token")
        if supplied is None and request.is_json:
            payload = request.get_json(silent=True) or {}
            supplied = payload.get("kiosk_token")
        if not kiosk_token_valid(supplied):
            return (
                jsonify(
                    ok=False,
                    code="kiosk_unauthorised",
                    message="This device is not authorised. Please see the office.",
                ),
                403,
            )
        return view(*args, **kwargs)

    return wrapper
