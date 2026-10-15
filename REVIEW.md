# Review of the starter implementation

Use the table below to record reproducible defects in the original starter. Verify each item against the contract and your tests before submitting.

| # | Where | What is wrong | How to reproduce / symptom | Fix |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Compares raw timestamps without consistently anchoring shift start to the attendance date/overnight shift and does not explicitly truncate instants first. | Punch around the grace boundary or punch in after midnight on an overnight shift. | Convert shift start to the UTC instant for the attendance date; truncate instants to whole seconds; apply strict > 600 seconds and floor minutes. |
| 2 | `compute_work_hours` | Python `round` does not implement required decimal half-up rounding. | Construct a duration whose hour value lies exactly on a half-cent boundary. | Use `Decimal` with `ROUND_HALF_UP` after truncating timestamps to seconds. |
| 3 | `compute_overtime` | Always places shift end on the attendance date, so overnight shifts calculate the wrong end instant. | Shift 22:00–06:00 with punch-out after 06:00 next day. | Move shift end to the next calendar day for overnight shifts; floor elapsed minutes and apply the 30-minute minimum. |
| 4 | `GET /health` | Returns success without pinging MongoDB. | Stop/restrict MongoDB and call `/health`. | Run MongoDB `ping`; return 503 if it fails. |
| 5 | `POST /employees` | Check-then-insert duplicate detection races and has no database-enforced uniqueness. | Send two concurrent requests with the same `emp_code`. | Unique index on `emp_code`; translate `DuplicateKeyError` to 409. |
| 6 | `GET /employees` | Uses `page * page_size` as skip, counts unfiltered employees, and does not sort. | Page 1 omits the first page; filter by department and compare `total`. | Skip `(page - 1) * page_size`, count with the same filter, sort by `emp_code`. |
| 7 | `POST /attendance/punch-in` | Does not handle unknown employees before using their shift; naive local timestamps and check-then-insert are unsafe. | Unknown employee or two simultaneous punch-ins. | Validate employee, convert epoch milliseconds to UTC, derive IST attendance date, and rely on a unique `(emp_code,date)` index. |
| 8 | `GET /attendance` | Fetches all matching documents into Python and sorts/slices in memory; it leaks `_id` and does not apply an index-backed query/pagination. | Large dataset or page/filter request. | Filter/sort/skip/limit/count in MongoDB and serialize fields without `_id`. |
| 9 | Date/time and API boundary handling | The starter does not consistently validate epoch milliseconds, truncate instants to seconds, or serialize BSON datetimes as epoch milliseconds. | Send seconds, floats, strings, or inspect response timestamp types. | Strict Pydantic validation and centralized UTC/epoch-ms conversion. |
| 10 | Input validation | Employee fields, shift-time equality, date ranges, page limits, and attendance statuses are insufficiently validated. | Invalid code, malformed date, equal shift times, invalid status, or page size 101. | Validate against `openapi.yaml`; return 422 for invalid inputs. |

## Reviewed and retained
- The natural attendance key is `(emp_code, date)`, not MongoDB `_id`.
- Calendar dates and shift times remain strings; instants are BSON UTC datetimes.
- `history` is an append-only audit array; it must not be replaced during correction.
- `work_hours`, `late_minutes`, `overtime_minutes` and `half_day` are server-derived fields, not client-controlled fields.


