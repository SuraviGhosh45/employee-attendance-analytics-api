# DECISIONS.md

1. **Indexes.**
   We created a unique index on `employees.emp_code` for identity guarantees and `employees(department, joined_on)` for department filtering. On `attendance_logs`, we created a unique compound index on `(emp_code, date)` to enforce one record per day, `(date, emp_code)` for sorted pagination without memory spikes, and `(date, late_minutes, emp_code)` for leaderboard queries. We considered and rejected indexing `history` and `punch_out`, as they add write amplification without serving selective range queries.

2. **Punch-in race.**
   Both requests compute the attendance date and attempt `insert_one`. MongoDB's unique index on `(emp_code, date)` ensures atomicity at the storage engine level. The first request commits (201); the second triggers a `DuplicateKeyError`, which our exception handler intercepts and converts into a clean 409 Conflict.

3. **Ties.**
   The late leaderboard uses MongoDB's `$rank` window function (competition ranking: 1, 2, 2, 4). The `rank <= limit` filter executes *after* ranking. If two employees tie at the cutoff, both share that rank and are returned (ordered by `emp_code`), yielding more than `limit` rows as specified.

4. **Headcount.**
   The aggregation begins on `employees` (filtering `joined_on <= end_of_month`), grouping by department to compute headcount before performing a `$lookup` on `attendance_logs`. Employees with zero logs are thus counted in headcount.

5. **One thing you would change for 100x data.**
   Maintain daily and monthly pre-aggregated rollup collections updated asynchronously via Change Streams, avoiding heavy cross-collection aggregations over tens of millions of raw log documents.
