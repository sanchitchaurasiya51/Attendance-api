# REVIEW.md

## Review of the starter `app/main.py`

The starter handled the basic flow, but several edge cases and parts of the API contract were missing. I reviewed the original helpers and endpoints, then implemented the fixes in the submitted `app/main.py`. The helper functions for items 2–4 were rewritten rather than reused unchanged.

| # | Where | Problem | How it shows up | Fix in the submitted code |
|---|---|---|---|---|
| 1 | MongoDB client, L22–23 | PyMongo returned naive datetimes, which could be mixed with timezone-aware values. | Subtracting a naive datetime from an aware one can raise `TypeError` and return a 500. | The client now uses `tz_aware=True` with UTC. `ms_to_dt` and `dt_to_ms` handle timestamp conversion. |
| 2 | `compute_late_minutes`, L33–37 | The old calculation mishandled the 10-minute boundary, overnight shifts, server time zones, and sub-second values. | A punch at 09:40:01 for a 09:30 shift could return 0; an overnight punch could use the wrong shift date. | The helper now compares whole-second timestamps against the shift start on the attendance date in IST. Lateness is counted only when it is strictly over 10 minutes. |
| 3 | `compute_work_hours`, L40–41 | Python's `round()` uses half-even rounding and floating-point values, not the required half-up rule. | A duration of 8 hours, 7 minutes, and 30 seconds could return `8.12` instead of `8.13`. | Work hours are rounded to hundredths using integer arithmetic. The half-day check uses that rounded value. |
| 4 | `compute_overtime`, L44–47 | The old code did not enforce the 30-minute minimum, handled overnight shift ends incorrectly, and could mix naive and aware datetimes. | Leaving 10 minutes after shift end could count as overtime; an overnight shift could produce a wildly incorrect result. | `shift_bounds()` handles overnight shifts, and overtime is counted only when it reaches 30 minutes. |
| 5 | Request models, L53–66 | Inputs were not validated strictly. Invalid employee details, times, dates, timestamps, or attendance statuses could pass validation. | For example, a shift time such as `25:99` or a timestamp in seconds could be accepted. | Pydantic models now validate field formats, real calendar dates, shift times, allowed statuses, and epoch-millisecond ranges. |
| 6 | `/health`, L72–74 | The endpoint always returned `ok`, even if MongoDB was unavailable. | It could return HTTP 200 while the database was down. | The endpoint pings MongoDB and returns 503 if it cannot connect. It also retries index creation if indexes are not ready. |
| 7 | `create_employee`, L77–85 | Checking for an employee before inserting was vulnerable to concurrent requests. `created_at` was also stored as a naive local datetime. | Two requests could create the same employee, and the timestamp format was inconsistent. | The insert catches `DuplicateKeyError` and returns 409. `created_at` is stored in UTC and serialized as epoch milliseconds. |
| 8 | `list_employees`, L88–96 | Pagination skipped the wrong number of rows, totals ignored the department filter, results were not sorted, and page values were not bounded. | Page 1 skipped the first page, and a department filter still returned the overall employee count. | Pagination uses `(page - 1) * page_size`; counts respect filters; results sort by `emp_code`; page and page size are validated. |
| 9 | Punch-in, L101 | The starter did not handle an unknown employee before reading their shift details. | Punching in with an unknown `emp_code` could cause a 500 instead of a 404. | `_get_employee()` returns a 404 when the employee does not exist. |
| 10 | Punch-in timestamp, L102–103 | The timestamp used server-local time, retained milliseconds, and assigned the date using the server's calendar rather than the attendance-date rule. | A timestamp near midnight could be saved under the wrong date. | `ms_to_dt()` converts timestamps to UTC and truncates punch times to whole seconds. `attendance_date()` applies the IST and overnight-shift rules. |
| 11 | Punch-in insert, L104–118 | The check-then-insert pattern allowed two concurrent requests to create attendance records for the same employee and date. | Parallel punch-in requests could both return 201. | A unique `(emp_code, date)` index enforces one record per day; a duplicate-key error returns 409. |
| 12 | Punch-in response, L119–121 | The response exposed MongoDB's internal ID and returned raw datetimes. | Clients received an `id` field and datetime values instead of the required timestamp format. | `record_out()` omits `_id` and serializes punch times as epoch milliseconds. |
| 13 | `list_attendance`, L144–147 | The starter loaded all matching records into Python before sorting and paginating. | Large result sets could consume excessive memory and take longer to return. | Sorting, skipping, and limiting now happen in MongoDB, supported by indexes. |
| 14 | Attendance sorting, L145 | Sorting only by date left records on the same date in an unpredictable order. | Page results could appear in a different order between requests. | Results sort by date descending and `emp_code` ascending, matching the compound index. |
| 15 | Attendance filters, L130–141 | Dates and statuses were not properly validated, reversed date ranges were accepted, and pagination was unbounded. | Invalid dates, unknown statuses, or `page=0` could produce incorrect results. | Date formats and calendar dates are checked, reversed ranges return 422, status is restricted to allowed values, and pagination is bounded. |
| 16 | Attendance response, L149–150 | Responses exposed MongoDB IDs and raw documents could omit fields added by the API contract. | Older records might not include `history`, `half_day`, or default minute counts. | `record_out()` standardizes the response and supplies defaults for missing fields. |
| 17 | Indexes, whole file | The starter did not create indexes, so queries could scan the collection and duplicate records were not prevented by the database. | Queries could become slower as data grows, and uniqueness checks were not race-safe. | `ensure_indexes()` creates the employee, department, joining-date, and attendance indexes during startup. The operation can be retried. |

## Other checks

- `load_dotenv()` is used without overriding environment variables that are already set.
- The endpoints use regular `def` functions with synchronous PyMongo calls; FastAPI runs these endpoints in its thread pool.
- Removing `_id` from a document after `insert_one()` is intentional because PyMongo adds that field to the inserted document.
- Extra request fields are ignored by Pydantic, as required by the contract.
- List responses continue to include the requested `page` and `page_size`.

## Note

The index helper currently falls back to a non-unique index if creating a unique index fails. In that situation, the database will not enforce uniqueness for that index. For strict data integrity, it is safer to log the failure and keep the API from treating index creation as successful until the unique index has been created.
