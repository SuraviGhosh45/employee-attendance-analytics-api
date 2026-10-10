# Employee Attendance & Analytics API

A FastAPI and MongoDB backend service for employee attendance tracking, manual regularization audits, and organizational analytics.

## Prerequisites & Running

- **Python:** 3.11+
- **Database:** MongoDB 6.0+ (Local instance or MongoDB Atlas)

### Setup & Run
```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure Environment (optional .env; system environment variables take precedence)
# MONGO_URI="mongodb://localhost:27017"
# MONGO_DB="attendance_db"

# 3. Seed sample data (optional)
python sample_seed.py

# 4. Start the server (ready within 20 seconds)
uvicorn app.main:app --port 8000
```

## Implementation Highlights

- **Contract & Schemas:** Strictly aligned with `openapi.yaml` and `DATA_MODEL.md`.
- **Business Rules (R1 - R10):**
  - **R1:** Handled overnight shifts crossing midnight and truncated all instants to whole seconds.
  - **R2:** Enforced strictly > 10:00 grace threshold for late arrival calculations.
  - **R3:** Overtime counted only when $\ge 30\text{ min}$, with cross-midnight end time calculation.
  - **R4 & R8:** Mathematical Half-Up rounding (`Decimal` with `ROUND_HALF_UP`) for work hours and rates.
  - **R5 - R7:** Weekday presence calculation with half-day weighting (0.5), mid-month joiner handling.
  - **R9:** Headcount includes employees with zero attendance records.
- **Concurrency & Races:** Handled via MongoDB unique indexes (`emp_code`, `(emp_code, date)`) and atomic updates (`find_one_and_update`), preventing race conditions.
- **Indexes:** Created automatically and idempotently on application startup.
- **Explain Probe:** Available at `GET /admin/explain/{endpoint}` with `executionStats`.

## Submission Deliverables
- [app/main.py](file:///C:/Users/91629/Desktop/HROne/app/main.py): Complete API implementation
- [REVIEW.md](file:///C:/Users/91629/Desktop/HROne/REVIEW.md): Code review and 20 identified defects with fixes
- [DECISIONS.md](file:///C:/Users/91629/Desktop/HROne/DECISIONS.md): Architectural decisions and index strategy
- [requirements.txt](file:///C:/Users/91629/Desktop/HROne/requirements.txt): Runtime dependencies
