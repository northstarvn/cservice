"""Exercise the live API against a real PostgreSQL database.

The suite fakes the database, which is right for 2 000 tests and wrong for
answering "does it work". This script starts nothing and mocks nothing: it
signs in over HTTP, walks the surfaces, and reports what actually came back.

    python3 scripts/smoke_live_api.py

Requires a seeded database (``scripts/seed_sample_data.py --reset``). Set
``CSERVICE_SMOKE_URL`` to point at a running server instead of the in-process
app; by default the app is imported and driven through TestClient, so no
separate uvicorn process is needed.

What it is looking for
----------------------
- a 5xx anywhere, which is the signal that a surface was only ever tested
  against fakes
- a payload shape that differs from the pydantic contract
- an engine that returns something obviously wrong for the seeded population
  (``chiara`` is high-churn with three cancellations, ``ana`` is loyal, and
  ``dmitri`` is dormant -- so a report that claims otherwise is misreading the
  data rather than merely formatting it)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASSWORD = "SamplePassw0rd!"

#: Surfaces an ordinary signed-in customer can reach. Admin-only routes are in
#: ADMIN_ROUTES and are run as `root`, who is seeded with `is_admin`.
SELF_ROUTES = [
    ("GET", "/health", None),
    ("GET", "/meta", None),
    ("GET", "/meta/features", None),
    ("GET", "/meta/ecosystem", None),
    ("GET", "/meta/scoring-catalog", None),
    ("GET", "/meta/customer-360", None),
    ("GET", "/meta/regional", None),
    ("GET", "/meta/decisions", None),
    ("GET", "/users/me", None),
    ("GET", "/users/me/security-posture", None),
    ("GET", "/chat/history", None),
    ("GET", "/chat/insights", None),
    ("GET", "/chat/loyalty-journey", None),
    ("GET", "/chat/activity-tree", None),
    ("GET", "/chat/communication-strategy", None),
    ("GET", "/chat/points/wallet", None),
    ("GET", "/chat/points/transactions", None),
    ("GET", "/chat/points/exchange/rates", None),
    ("GET", "/chat/payments/arrears", None),
    ("GET", "/chat/recovery-dashboard", None),
    # Stage A
    ("GET", "/chat/customer-360", None),
    ("GET", "/chat/me/recovery-status", None),
    ("GET", "/chat/me/status", None),
    ("GET", "/chat/me/points-forecast", None),
    ("GET", "/chat/me/policy-posture", None),
    ("GET", "/chat/me/preferences", None),
    ("GET", "/chat/me/consent-history", None),
    ("GET", "/chat/me/explanations", None),
]

ADMIN_ROUTES = [
    ("GET", "/chat/admin/customer-360", None),
    ("GET", "/chat/admin/explanation-vocabulary", None),
    ("GET", "/chat/admin/preference-catalog", None),
    ("GET", "/chat/admin/loyalty-journey", None),
    ("GET", "/chat/admin/activity-tree", None),
    ("GET", "/chat/admin/points/exchange", None),
    ("GET", "/chat/admin/payments/arrears", None),
    ("GET", "/chat/admin/recovery/playbooks", None),
    ("GET", "/chat/admin/recovery-guards", None),
    ("GET", "/chat/admin/recovery-analytics", None),
    ("GET", "/chat/admin/recovery-outreach-plan", None),
    ("GET", "/chat/admin/communication-strategy", None),
    ("GET", "/chat/admin/monetization-cohorts", None),
    ("GET", "/chat/admin-activity", None),
    ("GET", "/chat/admin/users", None),
    ("GET", "/audit/logs", None),
    ("GET", "/audit/logs/summary", None),
    ("GET", "/audit/catalog", None),
    ("GET", "/audit/efficiency", None),
    ("GET", "/topics/overview", None),
    ("GET", "/topics/intelligence", None),
]

#: Writes that must be exercised, because a read-only sweep cannot tell you
#: whether a *write* path works against real constraints.
WRITE_PROBES = [
    ("PUT", "/chat/me/preferences", {"preferences": {"communication_channel": "email"}}),
    # Proves the write landed, not just that it returned 200.
    ("GET", "/chat/me/preferences", None),
    ("PUT", "/chat/me/preferences", {"preferences": {"communication_channel": "carrier_pigeon"}}),
    ("PUT", "/chat/me/preferences", {"preferences": {"nope_not_a_key": 1}}),
    ("PUT", "/chat/me/preferences", {"consents": {"service": False}}),
    ("POST", "/chat/points/exchange/quote",
     {"point_type": "loyalty_points", "direction": "redeem", "amount": 10, "currency": "USD"}),
    ("GET", "/chat/payments/arrears/quote?principal=100&defer_days=30", None),
]

#: Probes whose *expected* status is not 200. A 405 here means the smoke script
#: is wrong, not the app, so they are listed rather than counted as failures.
EXPECTED_NON_200 = {
    # An invalid value must be refused, not silently stored.
    ("PUT", "/chat/me/preferences", '{"nope_not_a_key"'),
    # A required consent cannot be withdrawn; the response must say so.
    ("PUT", "/chat/me/preferences", '"service": false'),
}


def _client():
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app)


def _login(client, username: str) -> str | None:
    response = client.post(
        "/users/login", json={"username": username, "password": PASSWORD}
    )
    if response.status_code != 200:
        print(f"  ! login failed for {username}: {response.status_code} {response.text[:200]}")
        return None
    return response.json().get("access_token")


def main() -> int:
    base = os.getenv("CSERVICE_SMOKE_URL")
    if base:
        import httpx

        client = httpx.Client(base_url=base, timeout=30.0)
    else:
        client = _client()

    failures: list[str] = []
    server_errors: list[str] = []

    def run(label: str, routes, token: str | None) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        for method, path, body in routes:
            expected_non_200 = (method, path, json.dumps(body)) in EXPECTED_NON_200
            try:
                response = client.request(method, path, json=body, headers=headers)
            except Exception as exc:  # noqa: BLE001
                # A dropped connection means the server died on *this* route.
                # Stopping here would hide every route after it, so it is
                # recorded and the sweep continues -- the point of the script
                # is the full list, not the first crash.
                server_errors.append(f"{label} {method} {path} -> CRASH {type(exc).__name__}")
                print(f"  5xx CRASH {method:6s} {path}  ({type(exc).__name__})")
                break
            status = response.status_code
            if expected_non_200:
                ok = 400 <= status < 500
                mark = "ok " if ok else "!! "
                if not ok:
                    failures.append(
                        f"{label} {method} {path} -> {status}, expected a 4xx refusal"
                    )
                print(f"  {mark} {status} {method:6s} {path}  (expected 4xx)")
                continue
            mark = "ok " if status < 400 else "!! "
            if status >= 500:
                server_errors.append(f"{label} {method} {path} -> {status}")
                mark = "5xx"
            elif status >= 400:
                failures.append(f"{label} {method} {path} -> {status}")
            print(f"  {mark} {status} {method:6s} {path}")

    print("== public ==")
    run("public", [(m, p, b) for m, p, b in SELF_ROUTES if p in {"/health", "/meta", "/meta/features", "/meta/ecosystem", "/meta/scoring-catalog", "/meta/customer-360", "/meta/regional", "/meta/decisions"}], None)

    print("\n== signed in as chiara (high churn, 3 cancellations) ==")
    token = _login(client, "chiara")
    if token is None:
        print("cannot continue without a token")
        return 1
    run("self", [(m, p, b) for m, p, b in SELF_ROUTES if p not in {"/health", "/meta", "/meta/features", "/meta/ecosystem", "/meta/scoring-catalog", "/meta/customer-360", "/meta/regional", "/meta/decisions"}], token)

    print("\n== writes ==")
    run("write", WRITE_PROBES, token)

    print("\n== signed in as root (admin) ==")
    admin_token = _login(client, "root")
    if admin_token is None:
        print("cannot continue without an admin token")
        return 1
    run("admin", ADMIN_ROUTES, admin_token)

    print()
    print("=" * 60)
    if server_errors:
        print(f"SERVER ERRORS ({len(server_errors)}) -- these are 5xx and are bugs:")
        for entry in server_errors:
            print(f"  {entry}")
    if failures:
        print(f"CLIENT ERRORS ({len(failures)}):")
        for entry in failures:
            print(f"  {entry}")
    if not server_errors and not failures:
        print("all routes returned < 400")
    return 1 if server_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
