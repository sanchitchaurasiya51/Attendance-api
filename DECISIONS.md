# DECISIONS.md

1. **Indexes:** I added a unique index on `emp_code` in `employees` and indexes for department and joining-date queries. In `attendance_logs`, I used a unique `(emp_code, date)` index to prevent duplicate daily records and `(date desc, emp_code)` for attendance lists and leaderboard month scans. I skipped the `status` index because it would add write overhead without much benefit.

2. **Punch-in race:** Two requests can arrive at the same time for one employee. The unique index allows only one insert, while the other returns `409 Conflict`. This avoids a separate check before inserting.

3. **Leaderboard ties:** I used `$rank` before applying the limit so employees with equal scores get the same rank. This can return more rows than the requested limit.

4. **Monthly headcount:** I start from `employees` (only those who joined on or before the month end) and use `$lookup` to find attendance records. Employees with no attendance are still counted.

5. **Handling 100x data:** If the dataset grows significantly, I would maintain monthly summary records per employee. This would reduce the amount of raw attendance data processed by analytics queries.