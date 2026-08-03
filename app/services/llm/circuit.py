"""
A per-provider circuit breaker.

The problem it solves is narrow but real: when a provider is out of credit or
its key has been revoked, EVERY request pays the full round trip to discover
the same 401 before falling through. On the scheduler's pre-generation pass
that is one wasted call per user, all within the same minute.

So a failure that means "this provider is unwell" — not "this request was
unlucky" — opens the breaker, and the provider is skipped without being called
until the cooldown expires. One request then pays the probe; if it succeeds the
breaker closes.

⚠️ **This state is PROCESS-LOCAL.** It lives in one Python process's memory. It
does not coordinate across Railway replicas, does not survive a restart, and
does not persist to the database. Today that is exactly right — the backend
runs as a single uvicorn process (multiple workers are already ruled out,
because the notification scheduler would fire once per process and send
duplicate pushes). If the deployment ever grows replicas, each will keep its
own opinion of each provider's health. That is a degradation in efficiency, not
in correctness: the worst case is the same wasted probe per replica.
"""

import logging
import time
from typing import Dict, Optional

from .errors import ProviderError

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """Tracks, per provider, the time until it may be tried again."""

    def __init__(self, cooldown_seconds: int = 120):
        self.cooldown_seconds = max(1, int(cooldown_seconds))
        self._open_until: Dict[str, float] = {}
        self._reason: Dict[str, str] = {}

    def is_open(self, provider: str) -> bool:
        until = self._open_until.get(provider)
        if until is None:
            return False
        if time.time() >= until:
            # Cooldown expired: forget it entirely rather than half-open. The
            # next request IS the probe, and it either succeeds or re-opens.
            self._open_until.pop(provider, None)
            self._reason.pop(provider, None)
            return False
        return True

    def seconds_remaining(self, provider: str) -> int:
        until = self._open_until.get(provider)
        if until is None:
            return 0
        return max(0, int(round(until - time.time())))

    def reason(self, provider: str) -> Optional[str]:
        return self._reason.get(provider)

    def record_failure(self, provider: str, error: ProviderError) -> None:
        """Open the breaker if this failure says the provider itself is unwell.

        Deliberately opens on the FIRST such failure rather than after N. The
        failures in `CIRCUIT_OPENING` are not flaky — a revoked key and an
        empty balance are stable states, and waiting for a threshold just buys
        more identical failures at full price.
        """
        if not error.opens_circuit:
            return
        self._open_until[provider] = time.time() + self.cooldown_seconds
        self._reason[provider] = error.category
        logger.warning(
            "llm circuit OPEN for %s (%s) — skipping for %ds",
            provider, error.category, self.cooldown_seconds,
        )

    def record_success(self, provider: str) -> None:
        if self._open_until.pop(provider, None) is not None:
            self._reason.pop(provider, None)
            logger.info("llm circuit CLOSED for %s", provider)

    def reset(self) -> None:
        """Clear all state. Exists for tests — nothing in the app calls it."""
        self._open_until.clear()
        self._reason.clear()
