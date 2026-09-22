# Blockages Log

## Resolved — No runnable Python test environment (2026-09-22)
- **Symptom:** `python`/`python3` (3.13) lacked fastapi/sqlalchemy/pydantic/pytest; no venv found, so changes could only be verified by static reasoning.
- **Resolution:** `/usr/bin/python3.13` has the full dependency stack installed. All changes were verified locally instead:
  - `pytest tests/` → **161 passed**.
  - `py_compile` on every modified module → OK.
  - `from app.main import app` (exercises the import graph / circular-import fix) → OK.
  - Route registration checks (single `/meta/capabilities`, chat retention routes intact) → OK.

## Implementation notes (2026-09-22)
- `app/services/bookings.py` `status_value` / `normalize_status_value` / `normalize_assignment_state` were reviewed as candidate redundant helpers; **kept** because they have distinct `None`-default contracts (`None` preserved / `"unknown"` / `"suggested"`) and are all referenced by routers and tests. Merging them would change behavior. The `build_booking_assignment_report_payload` / `from_record_compat` aliases are intentional thin wrappers imported by routers — kept to preserve the service API.