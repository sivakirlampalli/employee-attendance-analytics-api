# REVIEW.md

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health()` | Always reported success without checking MongoDB. | Stop MongoDB; `/health` still returned 200. | Ping MongoDB and return 503 when the ping fails. |
| 2 | `create_employee()` | Duplicate detection was check-then-insert and could race; no database guarantee. | Send concurrent POSTs with the same `emp_code`. | Create a unique index and map `DuplicateKeyError` to 409. |
| 3 | `create_employee()` / `EmployeeIn` | No full contract validation for employee code, email, shift times, date, or equal shift boundaries. | Send malformed values or equal `shift_start`/`shift_end`. | Added Pydantic validation and a shift-difference check. |
| 4 | `list_employees()` | `skip = page * page_size` skipped the first page; total ignored the department filter; result was not sorted. | Request page 1 with a small page size or filter by department. | Use `(page - 1) * page_size`, filtered count, and `emp_code` ascending sort. |
| 5 | `punch_in()` | Missing-employee access could crash; duplicate check-then-insert was race-prone; invalid statuses/timestamps were accepted. | Punch in an unknown employee or send simultaneous requests / bad status. | Validate request, return 404 for unknown employees, and rely on a unique compound index for 409 conflicts. |
| 6 | `punch_in()` and time helpers | Used server-local time and calendar date, did not normalize/truncate instants, and mishandled overnight shifts. | Test UTC timestamps around midnight for a 22:00–06:00 shift. | Normalize instants to UTC, calculate the attendance date and shift boundaries in IST, and truncate fractional seconds. |
| 7 | `compute_late_minutes()` | Compared wall-clock timestamps without reliably anchoring the shift start to the attendance date; negative values were possible. | Test on-time, exactly 10-minute grace, and overnight cases. | Anchor shift start to the attendance date and apply the strict >600-second rule. |
| 8 | `compute_work_hours()` | Python `round()` uses banker's rounding and binary floats can yield incorrect half-way results. | Test durations at half-centesimal boundaries. | Use `Decimal` and `ROUND_HALF_UP`. |
| 9 | `compute_overtime()` | Did not account for overnight shift end on the next day. | Punch out after midnight for a 22:00–06:00 shift. | Calculate the expected end instant from the attendance date and shift definition. |
| 10 | `list_attendance()` | Loaded all matching documents into memory, counted only in Python, and omitted API pagination/date validation and required sort. | Test a large dataset, invalid date range, or page 2. | Use MongoDB count, indexed query, server-side sort/skip/limit, and validate filters. |
| 11 | Serialization | Mongo `_id`/BSON datetimes did not conform to the API's epoch-millisecond contract; legacy missing fields could be absent. | Inspect response types for punch and history timestamps. | Remove `_id`, convert timestamps to integer milliseconds, and supply legacy defaults. |
| 12 | Application initialization | No startup index creation. | Inspect indexes in a fresh database; concurrent duplicates could be stored. | Create required indexes idempotently at startup. |

### Additional Endpoints Reviewed

| # | Where (function / endpoint) | What was verified | How it was tested | Result |
|---|---|---|---|---|
| 13 | `punch_out()` | Rejects invalid punch-out times and prevents duplicate punch-outs. | Tested a punch-out that was not after punch-in and repeated a completed punch-out. | Invalid time returned 422; duplicate punch-out returned 409. |
| 14 | `correct_attendance()` | Validates corrections and records audit history with the actor, reason, timestamp, and changed values. | Corrected a test attendance record and inspected the returned `history` array. | Returned 200 with the change recorded in history. |
| 15 | `employee_monthly()` | Returns monthly working days, present days, leave days, late arrivals, overtime, and attendance percentage. | Queried `EMP1001` for October 2026. | Returned 200 with the expected metrics for the test data. |
| 16 | `department_summary()` | Includes department headcount and monthly attendance metrics. | Queried the October 2026 summary. | Returned 200; Engineering headcount was 1. |
| 17 | `late_leaderboard()` | Ranks employees by total late minutes and includes ranking information. | Queried the October 2026 leaderboard. | Returned 200; `EMP1001` ranked first in the test data. |
| 18 | `department_trend()` | Returns daily trend rows, including dates with no recorded attendance, and computes attendance rates and moving averages. | Queried Engineering for October 20–23, 2026. | Returned 200 with all four dates and zero attendance rates. |
| 19 | `/admin/explain/{endpoint}` | Exposes MongoDB execution statistics for supported query and aggregation paths. | Tested explain endpoints for attendance listing, monthly analytics, department summary, leaderboard, and department trend. | Successful requests returned 200; inspected plans used indexes where applicable. |


### Reviewed and retained

- The `employees` and `attendance_logs` collection names and the natural attendance key (`emp_code`, `date`) match `DATA_MODEL.md`.
- The default status `PRESENT` and default shift times are consistent with the API contract.
- The API continues to keep all implementation code in `app/main.py`, as required.
