"""Suite-wide fixtures.

Added because two external providers now keep **process-global** state -- a
circuit breaker and a verdict cache -- so that a slow or unreachable third party
cannot put a timeout in front of a customer. That is the right shape for the
application and the wrong shape for a test suite: the state outlives the test
that created it.

The concrete symptom this fixes: `test_consolidation_regressions.py`'s two
`analyze_sentiment` tests pass in isolation and fail in the full run. Earlier
tests call `analyze_sentiment` for real, the network is unavailable, three
consecutive failures open the breaker, and every later assertion about the
provider gets `None` from a breaker it never knew was open.

A per-module reset inside the affected test files would fix today's symptom and
leave the next file to rediscover it. An autouse fixture is the honest owner of
"reset the globals the modules own", because the globals are the thing that
changed.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_external_provider_state():
    """Return every external provider's breaker and cache to a clean state.

    Autouse and unconditional. The providers are process-global by design, so a
    test that leaves one open is a test that has changed the behaviour of every
    test after it -- which is the definition of order-dependent.

    Resets by calling each module's own reset function rather than reaching into
    its attributes: the reset entry point is the contract, and poking the
    internals here would let the two drift apart silently.

    Failures are swallowed deliberately. A module that does not exist, or a
    provider that has been removed, must not fail every other test in the suite
    because the reset could not find it.
    """
    yield
    for module_path, reset_name in (
        ("app.services.chat_analytics", "reset_sentiment_provider"),
        ("app.services.brain_router", "reset_external_circuit"),
        ("app.services.ai_providers", "reset_ai_providers"),
    ):
        try:
            module = pytest.importorskip(module_path)
            getattr(module, reset_name)()
        except Exception:  # noqa: BLE001 -- a reset must never fail the suite
            continue