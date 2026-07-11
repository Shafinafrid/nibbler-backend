"""Per-user request throttling (July 2026).

Every Claude/Voyage-backed endpoint costs real money per call, and nothing
stopped a looping client from hammering them. Limits are keyed by the
authenticated Firebase uid (stashed on request.state by get_current_user);
unauthenticated endpoints fall back to the caller's IP.

In-memory storage is fine while Railway runs a single instance — limits reset
on deploy, which is acceptable for cost control.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address


def user_or_ip(request) -> str:
    return getattr(request.state, "user_id", None) or get_remote_address(request)


limiter = Limiter(key_func=user_or_ip)
