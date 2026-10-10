"""
Employee Attendance & Analytics API
Built with FastAPI & MongoDB (PyMongo)
"""
import calendar
import os
from contextlib import asynccontextmanager
from datetime import date as dt_date
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """Create all required indexes idempotently at startup."""
    try:
        # employees indexes
        db.employees.create_index([("emp_code", 1)], unique=True, name="idx_emp_code_unique")
        db.employees.create_index([("department", 1), ("emp_code", 1)], name="idx_emp_dept_code")
        db.employees.create_index([("joined_on", 1)], name="idx_emp_joined_on")
        db.employees.create_index([("department", 1), ("joined_on", 1)], name="idx_emp_dept_joined")

        # attendance_logs indexes
        db.attendance_logs.create_index(
            [("emp_code", 1), ("date", 1)], unique=True, name="idx_att_emp_date_unique"
        )
        db.attendance_logs.create_index(
            [("date", -1), ("emp_code", 1)], name="idx_att_date_desc_emp_asc"
        )
        db.attendance_logs.create_index(
            [("emp_code", 1), ("date", -1)], name="idx_att_emp_date_desc"
        )
        db.attendance_logs.create_index(
            [("date", 1), ("late_minutes", 1), ("emp_code", 1)], name="idx_att_date_late_emp"
        )
        db.attendance_logs.create_index(
            [("emp_code", 1), ("punch_in", -1)], name="idx_att_emp_punch_in_desc"
        )
    except Exception as e:
        # Log index creation warning if DB is not reachable yet at import time
        print(f"Warning initializing indexes: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Math & Conversion Helpers
# --------------------------------------------------------------------------- #
def round_half_up(val: float | Decimal, decimals: int = 2) -> float:
    """Standard arithmetic half-up rounding (R4, R8)."""
    d = Decimal(str(val))
    return float(d.quantize(Decimal(10) ** -decimals, rounding=ROUND_HALF_UP))


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert UTC or naive datetime to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def epoch_ms_to_dt(ms: Optional[int]) -> Optional[datetime]:
    """Convert epoch milliseconds to UTC datetime, truncated to whole seconds (R1)."""
    if ms is None:
        return None
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def parse_hh_mm(s: str) -> tuple[int, int]:
    h, m = map(int, s.split(":"))
    return h, m


def get_attendance_date(punch_dt_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date is IST calendar date of punch_in.
    Overnight shift exception (shift_end <= shift_start): punch-in earlier than
    shift_end (in IST time) belongs to previous day's shift.
    """
    punch_ist = punch_dt_utc.astimezone(IST)
    p_time = punch_ist.time()
    s_end_time = time(*parse_hh_mm(shift_end))
    s_start_time = time(*parse_hh_mm(shift_start))

    is_overnight = s_end_time <= s_start_time
    if is_overnight and p_time < s_end_time:
        att_date = punch_ist.date() - timedelta(days=1)
    else:
        att_date = punch_ist.date()
    return att_date.isoformat()


def compute_late_minutes(
    punch_in_utc: datetime, shift_start: str, shift_end: str, attendance_date_str: str
) -> int:
    """
    R2: Grace period 10 minutes.
    Late only if punch-in strictly > 10:00 after shift_start.
    Then whole minutes (floor) since shift_start, not grace period.
    """
    att_date = dt_date.fromisoformat(attendance_date_str)
    start_h, start_m = parse_hh_mm(shift_start)
    shift_start_ist = datetime.combine(att_date, time(start_h, start_m), tzinfo=IST)

    punch_ist = punch_in_utc.astimezone(IST)
    diff_sec = (punch_ist - shift_start_ist).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    hours = diff_sec / 3600.0
    return round_half_up(hours, 2)


def compute_overtime(
    punch_out_utc: datetime, shift_start: str, shift_end: str, attendance_date_str: str
) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if >= 30; else 0.
    Overnight shifts: shift_end falls on next calendar day.
    """
    att_date = dt_date.fromisoformat(attendance_date_str)
    end_h, end_m = parse_hh_mm(shift_end)
    start_h, start_m = parse_hh_mm(shift_start)

    is_overnight = time(end_h, end_m) <= time(start_h, start_m)
    end_date = att_date + timedelta(days=1) if is_overnight else att_date
    shift_end_ist = datetime.combine(end_date, time(end_h, end_m), tzinfo=IST)

    punch_out_ist = punch_out_utc.astimezone(IST)
    diff_sec = (punch_out_ist - shift_end_ist).total_seconds()
    if diff_sec >= 1800:  # 30 minutes
        return int(diff_sec // 60)
    return 0


def format_history_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    changes = {}
    for k, v in entry.get("changes", {}).items():
        from_val = v.get("from")
        to_val = v.get("to")
        if k in ("punch_in", "punch_out"):
            if isinstance(from_val, datetime):
                from_val = dt_to_epoch_ms(from_val)
            if isinstance(to_val, datetime):
                to_val = dt_to_epoch_ms(to_val)
        changes[k] = {"from": from_val, "to": to_val}

    at_val = entry.get("at")
    if isinstance(at_val, datetime):
        at_val = dt_to_epoch_ms(at_val)

    return {
        "at": at_val,
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [format_history_entry(h) for h in doc.get("history", [])],
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=120)
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """Readiness probe. Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database unreachable: {e}")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    if body.shift_start == body.shift_end:
        raise HTTPException(
            status_code=422, detail="shift_start and shift_end must differ"
        )
    now_utc = datetime.now(timezone.utc)
    truncated_now = datetime.fromtimestamp(int(now_utc.timestamp()), tz=timezone.utc)

    doc = body.model_dump()
    doc["created_at"] = truncated_now

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    doc.pop("_id", None)
    doc["created_at"] = dt_to_epoch_ms(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )
    for it in items:
        it["created_at"] = dt_to_epoch_ms(it.get("created_at"))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_dt = epoch_ms_to_dt(body.punched_at)
    else:
        now_sec = int(datetime.now(timezone.utc).timestamp())
        punch_dt = datetime.fromtimestamp(now_sec, tz=timezone.utc)

    att_date = get_attendance_date(punch_dt, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(
        punch_dt, emp["shift_start"], emp["shift_end"], att_date
    )

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409, detail="Already punched in for this date"
        )

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        out_dt = epoch_ms_to_dt(body.punched_at)
    else:
        now_sec = int(datetime.now(timezone.utc).timestamp())
        out_dt = datetime.fromtimestamp(now_sec, tz=timezone.utc)

    # Find most recent record with punch_in <= punched_at
    candidate = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": out_dt, "$ne": None}},
        sort=[("punch_in", -1)],
    )
    if not candidate:
        raise HTTPException(status_code=404, detail="No punch-in record found")

    if candidate.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    in_dt = candidate["punch_in"]
    if in_dt.tzinfo is None:
        in_dt = in_dt.replace(tzinfo=timezone.utc)

    diff_sec = (out_dt - in_dt).total_seconds()
    if diff_sec <= 0:
        raise HTTPException(
            status_code=422, detail="punched_at must be strictly after punch_in"
        )
    if diff_sec > 24 * 3600:
        raise HTTPException(
            status_code=422, detail="punched_at cannot be more than 24 hours after punch_in"
        )

    work_hours = compute_work_hours(in_dt, out_dt)
    overtime = compute_overtime(
        out_dt, emp["shift_start"], emp["shift_end"], candidate["date"]
    )
    half_day = work_hours < 4.50

    # Atomic update to prevent race conditions on simultaneous punch-outs
    updated = db.attendance_logs.find_one_and_update(
        {
            "emp_code": candidate["emp_code"],
            "date": candidate["date"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    return format_attendance_doc(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422, detail="date_from must not be greater than date_to"
        )

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    items = [format_attendance_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    old_status = rec["status"]
    new_status = body.status if body.status is not None else old_status

    if body.status in ("ABSENT", "LEAVE") and (
        body.punch_in is not None or body.punch_out is not None
    ):
        raise HTTPException(
            status_code=422,
            detail="Cannot supply punch_in or punch_out when setting status to ABSENT or LEAVE",
        )

    old_in = rec.get("punch_in")
    old_out = rec.get("punch_out")
    old_wh = rec.get("work_hours")
    old_late = rec.get("late_minutes", 0)
    old_ot = rec.get("overtime_minutes", 0)
    old_hd = rec.get("half_day", False)

    if new_status in ("ABSENT", "LEAVE"):
        new_in = None
        new_out = None
        new_wh = None
        new_late = 0
        new_ot = 0
        new_hd = False
    else:
        if body.punch_in is not None:
            new_in = epoch_ms_to_dt(body.punch_in)
        else:
            new_in = old_in

        if new_in is None:
            raise HTTPException(
                status_code=422, detail="Presence status requires punch_in"
            )

        calc_date = get_attendance_date(new_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in belongs to attendance date {calc_date}, must match {date}",
            )

        if body.punch_out is not None:
            new_out = epoch_ms_to_dt(body.punch_out)
        else:
            new_out = old_out

        if new_out is not None:
            diff_sec = (new_out - new_in).total_seconds()
            if diff_sec <= 0:
                raise HTTPException(
                    status_code=422, detail="punch_out must be strictly after punch_in"
                )
            if diff_sec > 24 * 3600:
                raise HTTPException(
                    status_code=422, detail="punch_out cannot be more than 24 hours after punch_in"
                )
            new_wh = compute_work_hours(new_in, new_out)
            new_ot = compute_overtime(new_out, emp["shift_start"], emp["shift_end"], date)
            new_hd = new_wh < 4.50
        else:
            new_wh = None
            new_ot = 0
            new_hd = False

        new_late = compute_late_minutes(
            new_in, emp["shift_start"], emp["shift_end"], date
        )

    # Detect field differences
    changes = {}
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if new_in != old_in:
        changes["punch_in"] = {"from": old_in, "to": new_in}
    if new_out != old_out:
        changes["punch_out"] = {"from": old_out, "to": new_out}
    if new_wh != old_wh:
        changes["work_hours"] = {"from": old_wh, "to": new_wh}
    if new_late != old_late:
        changes["late_minutes"] = {"from": old_late, "to": new_late}
    if new_ot != old_ot:
        changes["overtime_minutes"] = {"from": old_ot, "to": new_ot}
    if new_hd != old_hd:
        changes["half_day"] = {"from": old_hd, "to": new_hd}

    if not changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    now_sec = int(datetime.now(timezone.utc).timestamp())
    now_dt = datetime.fromtimestamp(now_sec, tz=timezone.utc)

    history_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Atomic update with history length match to prevent silent history loss
    existing_history = rec.get("history", [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "emp_code": emp_code,
            "date": date,
            "history": {"$size": len(existing_history)},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_in,
                "punch_out": new_out,
                "work_hours": new_wh,
                "late_minutes": new_late,
                "overtime_minutes": new_ot,
                "half_day": new_hd,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict")

    return format_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# Analytics Pipelines
# --------------------------------------------------------------------------- #
def count_working_days(joined_on: str, month: str) -> int:
    """R7: Mon-Fri working days in the month from joined_on for mid-month joiners."""
    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    month_end_date = dt_date(y, m, num_days)

    joined_date = dt_date.fromisoformat(joined_on)
    if joined_date > month_end_date:
        return 0

    month_start_date = dt_date(y, m, 1)
    effective_start = max(month_start_date, joined_date)

    count = 0
    curr = effective_start
    while curr <= month_end_date:
        if curr.weekday() < 5:  # Mon-Fri
            count += 1
        curr += timedelta(days=1)
    return count


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    working_days = count_working_days(emp["joined_on"], month)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start_date, "$lte": end_date}}},
        {
            "$addFields": {
                "day_of_week": {
                    "$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        stats = res[0]
        present_days = float(stats["present_days"])
        leave_days = int(stats["leave_days"])
        late_count = int(stats["late_count"])
        total_late = int(stats["total_late_minutes"])
        total_ot = int(stats["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        att_pct = round_half_up((present_days / working_days) * 100.0, 2)
    else:
        att_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": att_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$codes"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]},
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "day_of_week": {
                                "$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                            }
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.day_of_week", [2, 3, 4, 5, 6]]},
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        ]
                                    },
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "work_hours_array": {
                    "$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {
                            "$and": [
                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$$l.work_hours", None]},
                            ]
                        },
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": "$logs.late_minutes"
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {"$sort": {"department": 1}},
    ]

    items = []
    for doc in db.employees.aggregate(pipeline):
        wh_records = doc.get("work_hours_array", [])
        if wh_records:
            total_wh = sum(r["work_hours"] for r in wh_records)
            avg_wh = round_half_up(total_wh / len(wh_records), 2)
        else:
            avg_wh = None

        items.append(
            {
                "department": doc["department"],
                "headcount": int(doc["headcount"]),
                "present_days": round_half_up(float(doc["present_days"]), 2),
                "avg_work_hours": avg_wh,
                "late_count": int(doc["late_count"]),
                "total_late_minutes": int(doc["total_late_minutes"]),
                "leave_count": int(doc["leave_count"]),
                "on_duty_count": int(doc["on_duty_count"]),
            }
        )

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    # Standard competition ranking using window functions (1, 2, 2, 4)
    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    d_from = dt_date.fromisoformat(from_date)
    d_to = dt_date.fromisoformat(to_date)
    if d_to < d_from:
        raise HTTPException(status_code=422, detail="'to' must be >= 'from'")
    diff_days = (d_to - d_from).days + 1
    if diff_days > 92:
        raise HTTPException(
            status_code=422, detail="Date range cannot exceed 92 days"
        )

    dept_emps = list(
        db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1})
    )
    if not dept_emps:
        raise HTTPException(status_code=404, detail="Department not found")

    # Generate all calendar days in range for trend
    curr = d_from
    calendar_days = []
    while curr <= d_to:
        calendar_days.append(curr)
        curr += timedelta(days=1)

    emp_codes = [e["emp_code"] for e in dept_emps]

    # Pre-aggregate logs in the date range
    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
    ]

    day_stats = {d["_id"]: d for d in db.attendance_logs.aggregate(pipeline)}

    rates_history: List[Optional[float]] = []
    items = []

    for c_date in calendar_days:
        iso_str = c_date.isoformat()
        is_working_day = c_date.weekday() < 5
        headcount = sum(1 for e in dept_emps if e["joined_on"] <= iso_str)

        stat = day_stats.get(iso_str, {})
        present_count = float(stat.get("present_count", 0.0))
        late_count = int(stat.get("late_count", 0))

        if is_working_day and headcount > 0:
            rate = round_half_up(present_count / headcount, 4)
        else:
            rate = None

        rates_history.append(rate)

        # 7-day moving average of non-null rates inside range window (up to 6 preceding + current)
        window = rates_history[-7:]
        valid_rates = [r for r in window if r is not None]
        if valid_rates:
            moving_avg = round_half_up(sum(valid_rates) / len(valid_rates), 4)
        else:
            moving_avg = None

        items.append(
            {
                "date": iso_str,
                "is_working_day": is_working_day,
                "headcount": headcount,
                "present_count": present_count,
                "late_count": late_count,
                "attendance_rate": rate,
                "moving_avg_7d": moving_avg,
            }
        )

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint (Part C)
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = 1,
    page_size: int = 20,
):
    if endpoint == "attendance_list":
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        plan = (
            db.attendance_logs.find(q, {"_id": 0})
            .sort([("date", -1), ("emp_code", 1)])
            .skip(skip)
            .limit(page_size)
            .explain("executionStats")
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422, detail="emp_code and month are required"
            )
        y, m = map(int, month.split("-"))
        num_days = calendar.monthrange(y, m)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": start_date, "$lte": end_date}}},
            {"$group": {"_id": None, "total_late": {"$sum": "$late_minutes"}}},
        ]
        plan = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        y, m = map(int, month.split("-"))
        num_days = calendar.monthrange(y, m)[1]
        end_date = f"{month}-{num_days:02d}"
        match_stage: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
        if department:
            match_stage["department"] = department
        pipeline = [
            {"$match": match_stage},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
        ]
        plan = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": plan}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        y, m = map(int, month.split("-"))
        num_days = calendar.monthrange(y, m)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": start_date, "$lte": end_date},
                    "late_minutes": {"$gt": 0},
                }
            },
            {"$group": {"_id": "$emp_code", "total_late": {"$sum": "$late_minutes"}}},
            {"$sort": {"total_late": -1}},
            {"$limit": limit},
        ]
        plan = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "department_trend":
        if not from_date or not to_date:
            raise HTTPException(
                status_code=422, detail="'from' and 'to' parameters are required"
            )
        pipeline = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {"$group": {"_id": "$date", "count": {"$sum": 1}}},
        ]
        plan = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    else:
        raise HTTPException(status_code=422, detail=f"Unknown endpoint: {endpoint}")
