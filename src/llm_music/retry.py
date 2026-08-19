"""Shared retry classification + backoff for LLM API calls."""

from __future__ import annotations

import random
import time

# Seeded so retry timing is reproducible across runs; jitter still spreads
# concurrent workers because each draw advances the shared stream.
_rng = random.Random(0)


def is_retryable(exc: Exception) -> bool:
    """Whether re-issuing the same request could plausibly succeed.

    An attached HTTP status decides: 408/409/429 and 5xx (incl. Anthropic's 529
    "overloaded") are transient; other 4xx — bad/unknown/unverified model, bad
    key, malformed request — are permanent, and retrying just burns attempts
    (e.g. an unverified org requesting `o3` 400s five times in a row). The
    status is checked *first* so a permanent error whose message happens to
    mention "connection" or "try again" isn't misread as transient.

    Without a status (SDKs surface stream drops and socket errors as plain
    exceptions), assume a network/transport hiccup and retry.
    """
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status is not None:
        return status in (408, 409, 429) or status >= 500
    return True


def is_overloaded(exc: Exception) -> bool:
    """Anthropic 529-class capacity backpressure — an infrastructure signal, not
    a property of the model. Callers may retry these without charging the model
    an attempt (attempts is a model-reliability covariate in analyses).

    Detected by status when present; streaming responses deliver the overload as
    an in-stream error event whose exception carries no status, so fall back to
    the error type in the message."""
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status == 529:
        return True
    return "overloaded_error" in str(exc)


def is_rate_limited(exc: Exception) -> bool:
    """HTTP 429 — the account or upstream provider is throttling. Like 529s,
    this is infrastructure backpressure, not a property of the model."""
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status == 429:
        return True
    return "Error code: 429" in str(exc)


# Transport-failure fingerprints: SDK exception class names (openai/anthropic
# raise APIConnectionError/APITimeoutError; httpx raises ConnectError,
# ReadTimeout, RemoteProtocolError) and the message fragments they surface.
_CONN_CLASS_MARKERS = ("connect", "timeout")
_CONN_MSG_MARKERS = ("connection error", "connection reset", "timed out",
                     "connection refused", "network is unreachable",
                     "server disconnected", "remote protocol error",
                     "ssl", "eof occurred", "name resolution")


def is_connection_error(exc: Exception) -> bool:
    """Network/transport failure before any HTTP response — the request never
    reached the model, so this is an infrastructure signal, not a property of
    the model, and callers must not charge it as a model attempt (a pod-side
    network flap once burned all 5 attempts of four arms at once).

    Only exceptions WITHOUT an HTTP status qualify: a real status (even one
    whose message mentions timeouts) is a provider verdict, handled elsewhere.
    """
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status is not None:
        return False
    cls = type(exc).__name__.lower()
    if any(m in cls for m in _CONN_CLASS_MARKERS):
        return True
    msg = str(exc).lower()
    return any(m in msg for m in _CONN_MSG_MARKERS)


def backoff_delay(attempt: int, cap: float = 20.0) -> float:
    """Exponential backoff duration with jitter: 2^attempt (capped) scaled by a
    random factor in [0.5, 1.0] so parallel workers don't retry in lockstep.
    Async callers pass this to asyncio.sleep; sync callers use backoff_sleep."""
    return min(2 ** attempt, cap) * _rng.uniform(0.5, 1.0)


def backoff_sleep(attempt: int, cap: float = 20.0) -> float:
    """Sleep for backoff_delay(attempt) and return the duration slept."""
    delay = backoff_delay(attempt, cap)
    time.sleep(delay)
    return delay
