# Employee Attendance & Analytics API

A robust FastAPI & MongoDB backend service for employee attendance tracking, regularization audit logging, and analytical reporting.

## 1. Quick Start

### Prerequisites
- Python 3.11+
- MongoDB 6.0+ (Local instance or MongoDB Atlas)

### Setup & Run
```bash
# 1. Create and activate a virtual environment
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Linux/macOS:
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment variables (or create a local .env)
# Real environment variables take priority over .env
MONGO_URI="mongodb://localhost:27017"
MONGO_DB="attendance_db"

# 4. (Optional) Seed sample data
python sample_seed.py

# 5. Start the service
uvicorn app.main:app --port 8000
```
The API starts and becomes ready (`GET /health` returns 200) in under 20 seconds. Interactive documentation is available at `http://127.0.0.1:8000/docs`.

---

## 2. API Endpoints Overview

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Readiness probe (pings MongoDB; returns 200 if reachable, else 503). |
| `POST` | `/employees` | Create employee with unique `emp_code` (409 on duplicate). |
| `GET` | `/employees` | Paginated listing sorted by `emp_code` ascending. |
| `POST` | `/attendance/punch-in` | Creates the day's record (R1, R2); race-safe with unique `(emp_code, date)`. |
| `POST` | `/attendance/punch-out` | Atomically closes open punch, calculates work hours, overtime, and half-day. |
| `GET` | `/attendance` | Paginated attendance list sorted by `date` desc, `emp_code` asc. |
| `PATCH` | `/attendance/{emp_code}/{date}` | Regularize record, recompute derived metrics, append audit trail to `history`. |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly attendance summary for one employee (working days, presence, leave). |
| `GET` | `/analytics/departments/summary` | Department summary across collections; headcount includes zero-log employees (R9). |
| `GET` | `/analytics/leaderboard/late` | Competition ranking (1, 2, 2, 4) with ties and post-ranking limit. |
| `GET` | `/analytics/departments/{department}/trend` | Gap-filling daily trend with 7-day moving average (`$densify`, `$setWindowFields`). |
| `GET` | `/admin/explain/{endpoint}` | Raw MongoDB query execution plan proving index usage (`IXSCAN`). |

---

## 3. Core Business Rules Implemented

- **R1 (Timezone & Overnight Shifts):** Shift times and dates are in IST (UTC+05:30). A punch-in before `shift_end` on an overnight shift (`shift_end <= shift_start`) belongs to the previous day's shift. Instants are truncated to whole seconds.
- **R2 (Late Arrival):** 10-minute grace period. Late only if strictly $> 10:00$ after `shift_start`; then minutes are counted from `shift_start`.
- **R3 (Overtime):** Minutes past `shift_end` counted only if $\ge 30\text{ min}$, otherwise 0.
- **R4 & R8 (Half-Up Rounding):** Work hours and rates use arithmetic half-up rounding (`Decimal(ROUND_HALF_UP)`).
- **R5 - R7 (Presence & Mid-Month Joiners):** Working days count Mon–Fri from `joined_on`. Present days count Mon–Fri records with half-day weighting (0.5).
- **R9 (Headcount):** Employees with zero attendance logs are counted in headcount for their department.
- **R10 (Pagination):** 1-indexed pagination (`page >= 1`, max `page_size = 100`).

---

## 4. Key Architectural Decisions

- **Atomicity & Concurrency:** Database-level unique indexes on `employees.emp_code` and `attendance_logs(emp_code, date)` prevent duplicate creation races. Atomic `find_one_and_update` operations guarantee that concurrent punch-outs and regularizations never corrupt records.
- **Database-Level Aggregation:** All analytics run directly in MongoDB via aggregation pipelines rather than loading collections into Python memory.
- **Query Optimization:** Compound indexes are created idempotently on startup via FastAPI `lifespan`, eliminating collection scans (`COLLSCAN`).

---

## 5. Repository Deliverables

- [app/main.py](file:///C:/Users/91629/Desktop/HROne/app/main.py): Complete API implementation
- [REVIEW.md](file:///C:/Users/91629/Desktop/HROne/REVIEW.md): Starter code audit identifying 20 defects and their fixes
- [DECISIONS.md](file:///C:/Users/91629/Desktop/HROne/DECISIONS.md): Architectural decisions and index justifications
- [requirements.txt](file:///C:/Users/91629/Desktop/HROne/requirements.txt): Pinned dependencies
