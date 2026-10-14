"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""

import os
import calendar
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, StrictInt, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError


# Read .env for local development. Real environment variables take priority.
load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENCE_STATUSES | {"ABSENT", "LEAVE"}
EMP_CODE_PATTERN = re.compile(r"^EMP\d{4,6}$")
MIN_EPOCH_MS = 100_000_000_000


client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=IST,
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def now_ist() -> datetime:
    """Return the current IST time, truncated to whole seconds."""
    return datetime.now(IST).replace(microsecond=0)


def timestamp_to_ist(punched_at: Optional[int]) -> datetime:
    """Convert epoch milliseconds to IST and truncate to whole seconds."""
    if punched_at is None:
        return now_ist()

    # StrictInt in request models rejects strings, floats and booleans.
    if punched_at < MIN_EPOCH_MS:
        raise HTTPException(
            status_code=422,
            detail="Timestamp must be epoch milliseconds, not seconds",
        )

    try:
        return datetime.fromtimestamp(punched_at / 1000, tz=IST).replace(
            microsecond=0
        )
    except (OverflowError, OSError, ValueError):
        raise HTTPException(status_code=422, detail="Timestamp is out of range")


def datetime_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    """Convert a stored datetime to epoch milliseconds."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=IST)
    return int(value.timestamp() * 1000)


def api_value(value):
    """Convert MongoDB datetimes to epoch milliseconds without exposing _id."""
    if isinstance(value, datetime):
        return datetime_to_epoch_ms(value)
    if isinstance(value, list):
        return [api_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: api_value(item)
            for key, item in value.items()
            if key != "_id"
        }
    return value


def serialize_record(doc: dict) -> dict:
    """Serialize a MongoDB document using the public API field conventions."""
    clean_doc = {key: value for key, value in doc.items() if key != "_id"}
    return api_value(clean_doc)


def validate_time(value: str) -> None:
    try:
        parsed = datetime.strptime(value, "%H:%M")
        if parsed.strftime("%H:%M") != value:
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="Time must use HH:MM format")


def validate_date(value: str, field_name: str = "date") -> None:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
        if parsed.strftime("%Y-%m-%d") != value:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must use YYYY-MM-DD format",
        )


def shift_start_datetime(date_str: str, shift_start: str) -> datetime:
    hour, minute = map(int, shift_start.split(":"))
    day = datetime.strptime(date_str, "%Y-%m-%d").date()
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


def compute_late_minutes(
    punch_in: datetime, shift_start: str, attendance_date: Optional[str] = None
) -> int:
    """Apply the strict 10-minute grace rule; count full minutes from shift start."""
    if attendance_date is None:
        attendance_date = punch_in.date().isoformat()

    start = shift_start_datetime(attendance_date, shift_start)
    elapsed_seconds = (punch_in - start).total_seconds()

    if elapsed_seconds <= 10 * 60:
        return 0
    return int(elapsed_seconds // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(str((punch_out - punch_in).total_seconds()))
    hours = (seconds / Decimal("3600")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return float(hours)


def get_shift_end(date_str: str, shift_start: str, shift_end: str) -> datetime:
    start = shift_start_datetime(date_str, shift_start)
    end_hour, end_minute = map(int, shift_end.split(":"))
    shift_end_dt = datetime(
        start.year, start.month, start.day, end_hour, end_minute, tzinfo=IST
    )

    # End time at or before start time means an overnight shift.
    if shift_end_dt <= start:
        shift_end_dt += timedelta(days=1)

    return shift_end_dt


def compute_overtime(
    punch_out: datetime, shift_start: str, shift_end: str, date_str: str
) -> int:
    end = get_shift_end(date_str, shift_start, shift_end)
    minutes = int((punch_out - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def attendance_date_for_punch(
    punched_at: datetime, shift_start: str, shift_end: str
) -> str:
    """For overnight shifts, early-morning punches belong to the prior shift date."""
    start_hour, start_minute = map(int, shift_start.split(":"))
    end_hour, end_minute = map(int, shift_end.split(":"))

    is_overnight = (end_hour, end_minute) <= (start_hour, start_minute)
    if is_overnight and (punched_at.hour, punched_at.minute) < (
        end_hour,
        end_minute,
    ):
        return (punched_at.date() - timedelta(days=1)).isoformat()

    return punched_at.date().isoformat()


def duplicate_employee_code(value: str) -> bool:
    return EMP_CODE_PATTERN.fullmatch(value) is not None


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def check_emp_code(cls, value: str) -> str:
        if not duplicate_employee_code(value):
            raise ValueError("emp_code must match EMP followed by 4 to 6 digits")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def check_shift_time(cls, value: str) -> str:
        validate_time(value)
        return value

    @field_validator("joined_on")
    @classmethod
    def check_joined_on(cls, value: str) -> str:
        validate_date(value, "joined_on")
        return value

    @field_validator("shift_end")
    @classmethod
    def shifts_must_differ(cls, value: str, info):
        shift_start = info.data.get("shift_start")
        if shift_start is not None and value == shift_start:
            raise ValueError("shift_start must differ from shift_end")
        return value


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def check_punched_at(cls, value):
        if value is not None and value < MIN_EPOCH_MS:
            raise ValueError("punched_at must be epoch milliseconds")
        return value

    @field_validator("status")
    @classmethod
    def check_status(cls, value: str) -> str:
        if value not in PRESENCE_STATUSES:
            raise ValueError("status must be PRESENT, WFH or ON_DUTY")
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def check_punched_at(cls, value):
        if value is not None and value < MIN_EPOCH_MS:
            raise ValueError("punched_at must be epoch milliseconds")
        return value


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[str] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str
    regularized_by: str

    @field_validator("punch_in", "punch_out")
    @classmethod
    def check_timestamps(cls, value):
        if value is not None and value < MIN_EPOCH_MS:
            raise ValueError("Punch times must be epoch milliseconds")
        return value

    @field_validator("status")
    @classmethod
    def check_status(cls, value):
        if value is not None and value not in ALL_STATUSES:
            raise ValueError("Invalid attendance status")
        return value

    @field_validator("reason", "regularized_by")
    @classmethod
    def check_required_text(cls, value):
        if not value.strip():
            raise ValueError("This field cannot be empty")
        return value


# ---------------------------------------------------------------------------
# Startup: indexes
# ---------------------------------------------------------------------------
@app.on_event("startup")
def create_indexes():
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="employee_code_unique",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="employee_department_code",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="attendance_employee_date_unique",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_employee",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="attendance_employee_punch_in",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING)],
        name="attendance_date_status",
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    try:
        db.command("ping")
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB is unavailable")
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(
            status_code=422,
            detail="shift_start must differ from shift_end",
        )

    doc = body.model_dump()
    doc["created_at"] = now_ist()

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    created = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    return serialize_record(created)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}
    if department is not None:
        query["department"] = department

    total = db.employees.count_documents(query)
    documents = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [serialize_record(doc) for doc in documents],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Punch in
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    punched_at = timestamp_to_ist(body.punched_at)
    attendance_date = attendance_date_for_punch(
        punched_at, employee["shift_start"], employee["shift_end"]
    )
    late_minutes = compute_late_minutes(
        punched_at, employee["shift_start"], attendance_date
    )

    record = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(record)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Already punched in for this attendance date",
        )

    return serialize_record(
        db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "date": attendance_date}
        )
    )


# ---------------------------------------------------------------------------
# Punch out
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    punched_at = timestamp_to_ist(body.punched_at)

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punched_at},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        raise HTTPException(status_code=404, detail="No punch-in found")

    punch_in_time = record.get("punch_in")
    if punch_in_time is None:
        raise HTTPException(status_code=404, detail="No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Already punched out")

    if punched_at <= punch_in_time:
        raise HTTPException(
            status_code=422, detail="punch-out must be after punch-in"
        )
    if punched_at > punch_in_time + timedelta(hours=24):
        raise HTTPException(
            status_code=422,
            detail="punch-out cannot be more than 24 hours after punch-in",
        )

    work_hours = compute_work_hours(punch_in_time, punched_at)
    overtime_minutes = compute_overtime(
        punched_at,
        employee["shift_start"],
        employee["shift_end"],
        record["date"],
    )

    # The conditional update means only one simultaneous punch-out can succeed.
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punched_at,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": work_hours < 4.50,
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="Already punched out")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_record(updated)


# ---------------------------------------------------------------------------
# Attendance listing
# ---------------------------------------------------------------------------
@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}

    if emp_code is not None:
        query["emp_code"] = emp_code

    if date_from is not None:
        validate_date(date_from, "date_from")
    if date_to is not None:
        validate_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must be on or before date_to",
        )

    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to

    if status is not None:
        if status not in ALL_STATUSES:
            raise HTTPException(status_code=422, detail="Invalid status")
        query["status"] = status

    total = db.attendance_logs.count_documents(query)
    documents = list(
        db.attendance_logs.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [serialize_record(doc) for doc in documents],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Attendance regularization
# ---------------------------------------------------------------------------
@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    validate_date(date)

    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    if body.status in {"ABSENT", "LEAVE"} and (
        body.punch_in is not None or body.punch_out is not None
    ):
        raise HTTPException(
            status_code=422,
            detail="ABSENT/LEAVE cannot include punch times",
        )

    new_status = body.status if body.status is not None else record["status"]
    new_in = (
        timestamp_to_ist(body.punch_in)
        if body.punch_in is not None
        else record.get("punch_in")
    )
    new_out = (
        timestamp_to_ist(body.punch_out)
        if body.punch_out is not None
        else record.get("punch_out")
    )

    if new_status in {"ABSENT", "LEAVE"}:
        new_in = None
        new_out = None
    else:
        if new_in is None:
            raise HTTPException(
                status_code=422,
                detail="A presence status requires punch_in",
            )

        # Overnight records still use the record's attendance date as their shift date.
        expected_date = attendance_date_for_punch(
            new_in, employee["shift_start"], employee["shift_end"]
        )
        if expected_date != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must belong to the attendance date",
            )

        if new_out is not None:
            if new_out <= new_in:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in",
                )
            if new_out - new_in > timedelta(hours=24):
                raise HTTPException(
                    status_code=422,
                    detail="Punch duration cannot exceed 24 hours",
                )

    if new_in is not None:
        new_in = new_in.replace(microsecond=0)
    if new_out is not None:
        new_out = new_out.replace(microsecond=0)

    late_minutes = 0
    overtime_minutes = 0
    work_hours = None
    half_day = False

    if new_in is not None:
        late_minutes = compute_late_minutes(
            new_in, employee["shift_start"], date
        )
        if new_out is not None:
            work_hours = compute_work_hours(new_in, new_out)
            half_day = work_hours < 4.50
            overtime_minutes = compute_overtime(
                new_out,
                employee["shift_start"],
                employee["shift_end"],
                date,
            )

    proposed = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "work_hours": work_hours,
        "half_day": half_day,
    }

    # The history records only fields whose values actually changed.
    changes = {}
    for field, new_value in proposed.items():
        old_value = record.get(field)
        if old_value != new_value:
            if field in {"punch_in", "punch_out"}:
                changes[field] = {
                    "from": datetime_to_epoch_ms(old_value),
                    "to": datetime_to_epoch_ms(new_value),
                }
            else:
                changes[field] = {"from": old_value, "to": new_value}

    if not changes:
        raise HTTPException(status_code=422, detail="No changes requested")

    history_entry = {
        "at": datetime_to_epoch_ms(now_ist()),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Compare the old state so concurrent corrections cannot overwrite one another.
    update_filter = {
        "_id": record["_id"],
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
    }
    if "history" in record:
        update_filter["history"] = record["history"]
    else:
        update_filter["history"] = {"$exists": False}

    result = db.attendance_logs.update_one(
        update_filter,
        {
            "$set": proposed,
            "$push": {"history": history_entry},
        },
    )

    if result.matched_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance record changed concurrently; retry",
        )

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_record(updated)




# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #



# ---------------------------------------------------------------------------
# Employee monthly analytics
# ---------------------------------------------------------------------------

from calendar import monthrange
from datetime import date
from decimal import Decimal, ROUND_HALF_UP


def round_half_up(value, places=2):
    """Round a number using the assignment's ROUND_HALF_UP rule."""
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(
        quantum, rounding=ROUND_HALF_UP
    ))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    # 1. Check that the employee exists.
    employee = db.employees.find_one({"emp_code": emp_code})

    if not employee:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    # 2. Calculate the month's date range.
    year, month_number = map(int, month.split("-"))

    first_day = date(year, month_number, 1)
    last_day = date(
        year,
        month_number,
        monthrange(year, month_number)[1],
    )

    # 3. Count weekdays from joining date through month end.
    joined_on = date.fromisoformat(employee["joined_on"])
    start_day = max(first_day, joined_on)

    working_days = 0

    if start_day <= last_day:
        for day_number in range(start_day.day, last_day.day + 1):
            current_day = date(year, month_number, day_number)

            # Monday-Friday only; holidays are not excluded.
            if current_day.weekday() < 5:
                working_days += 1

    # 4. Aggregate attendance records in MongoDB.
    # MongoDB $dayOfWeek: Sunday=1, Saturday=7.
    weekday_expression = {
        "$not": [
            {
                "$in": [
                    {
                        "$dayOfWeek": {
                            "$dateFromString": {
                                "dateString": "$date"
                            }
                        }
                    },
                    [1, 7],
                ]
            }
        ]
    }

    presence_status_expression = {
        "$in": [
            "$status",
            ["PRESENT", "WFH", "ON_DUTY"],
        ]
    }

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": first_day.isoformat(),
                    "$lte": last_day.isoformat(),
                },
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
                                    presence_status_expression,
                                    weekday_expression,
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },

                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },

                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },

                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },

                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    result = list(db.attendance_logs.aggregate(pipeline))
    totals = result[0] if result else {}

    # 5. Calculate the final response.
    present_days = round_half_up(
        totals.get("present_days", 0), 2
    )

    attendance_pct = (
        round_half_up(
            (present_days / working_days) * 100,
            2,
        )
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": totals.get("leave_days", 0),
        "late_count": totals.get("late_count", 0),
        "total_late_minutes": totals.get("total_late_minutes", 0),
        "total_overtime_minutes": totals.get(
            "total_overtime_minutes", 0
        ),
        "attendance_pct": attendance_pct,
    }


# ---------------------------------------------------------------------------
# Department summary analytics
# ---------------------------------------------------------------------------

@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    from calendar import monthrange
    from datetime import date

    year, month_number = map(int, month.split("-"))
    first_day = date(year, month_number, 1).isoformat()
    last_day = date(
        year, month_number, monthrange(year, month_number)[1]
    ).isoformat()

    pipeline = [
        {
            "$match": {
                "joined_on": {"$lte": last_day}
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", first_day]},
                                    {"$lte": ["$date", last_day]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$not": [{
                                            "$in": [
                                                {"$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$logs.date"
                                                    }
                                                }},
                                                [1, 7],
                                            ]
                                        }]
                                    },
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "work_hours_values": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            None,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$headcount"},
                "present_days": 1,
                "avg_work_hours": {
                    "$let": {
                        "vars": {
                            "hours": {
                                "$filter": {
                                    "input": "$work_hours_values",
                                    "as": "h",
                                    "cond": {"$ne": ["$$h", None]},
                                }
                            }
                        },
                        "in": {"$avg": "$$hours"},
                    }
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]

    items = list(db.employees.aggregate(pipeline))

    for item in items:
        if item["avg_work_hours"] is not None:
            item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)
        item["present_days"] = round_half_up(item["present_days"], 2)

    return {"month": month, "items": items}


# ---------------------------------------------------------------------------
# Late-comers leaderboard
# ---------------------------------------------------------------------------

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    from calendar import monthrange
    from datetime import date

    year, month_number = map(int, month.split("-"))
    first_day = date(year, month_number, 1).isoformat()
    last_day = date(
        year, month_number, monthrange(year, month_number)[1]
    ).isoformat()

    pipeline = [
        {"$match": {
            "date": {"$gte": first_day, "$lte": last_day},
            "late_minutes": {"$gt": 0},
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "employee",
        }},
        {"$unwind": "$employee"},
    ]

    if department:
        pipeline.append({
            "$match": {"employee.department": department}
        })

    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {"$rank": {}}
            },
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$employee.name",
            "department": "$employee.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


from datetime import date, datetime, timedelta, timezone
from fastapi import HTTPException, Query


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
):
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="'to' must be on or after 'from'")

    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="Unknown department")

    start = datetime.combine(from_date, datetime.min.time(), tzinfo=timezone.utc)
    end_exclusive = datetime.combine(
        to_date + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
    )

    pipeline = [
        {"$documents": [{"day": start}]},
        {
            "$densify": {
                "field": "day",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start, end_exclusive],
                },
            }
        },
        {
            "$set": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"current_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "department": department,
                            "$expr": {"$lte": ["$joined_on", "$$current_date"]},
                        }
                    },
                    {"$project": {"_id": 0, "emp_code": 1}},
                ],
                "as": "employees_today",
            }
        },
        {
            "$set": {
                "headcount": {"$size": "$employees_today"},
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$day"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$day"}, 6]},
                    ]
                },
                "emp_codes": "$employees_today.emp_code",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"d": "$date", "codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$d"]},
                                    {"$in": ["$emp_code", "$$codes"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$set": {
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "log",
                                    "cond": {
                                        "$in": [
                                            "$$log.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                }
                            },
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$eq": ["$$log.half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$gt": ["$$log.late_minutes", 0]},
                        }
                    }
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]},
            }
        },
        {"$sort": {"date": 1}},
    ]

    items = list(db.aggregate(pipeline))
    return {"department": department, "from": from_date.isoformat(),
            "to": to_date.isoformat(), "items": items}









from bson import json_util
import json


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
):
    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }
    if endpoint not in allowed:
        raise HTTPException(status_code=422, detail="Unsupported endpoint")

    if endpoint == "attendance_list":
        filters = {}
        if emp_code:
            filters["emp_code"] = emp_code
        if status:
            filters["status"] = status
        if date_from or date_to:
            filters["date"] = {}
            if date_from:
                filters["date"]["$gte"] = date_from.isoformat()
            if date_to:
                filters["date"]["$lte"] = date_to.isoformat()

        command = {
            "find": "attendance_logs",
            "filter": filters,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        collection = "attendance_logs"

    else:
        if not month and endpoint in {
            "employee_monthly", "department_summary", "late_leaderboard"
        }:
            raise HTTPException(status_code=422, detail="month is required")

        if endpoint == "employee_monthly" and not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required")

        if endpoint == "department_trend":
            if not department or not from_date or not to_date:
                raise HTTPException(
                    status_code=422,
                    detail="department, from and to are required",
                )
            if to_date < from_date or (to_date - from_date).days + 1 > 92:
                raise HTTPException(status_code=422, detail="Invalid date range")

            if not db.employees.find_one({"department": department}):
                raise HTTPException(status_code=404, detail="Unknown department")

            start = datetime.combine(
                from_date, datetime.min.time(), tzinfo=timezone.utc
            )
            end = datetime.combine(
                to_date + timedelta(days=1),
                datetime.min.time(),
                tzinfo=timezone.utc,
            )
            pipeline = [
                {"$match": {"date": {
                    "$gte": from_date.isoformat(),
                    "$lte": to_date.isoformat(),
                }}},
                {"$sort": {"date": 1}},
            ]

        else:
            year, month_num = map(int, month.split("-"))
            first = date(year, month_num, 1).isoformat()
            last = date(year, month_num, calendar.monthrange(year, month_num)[1]).isoformat()

            filters = {"date": {"$gte": first, "$lte": last}}

            if endpoint == "employee_monthly":
                filters["emp_code"] = emp_code
            elif endpoint == "late_leaderboard":
                filters["late_minutes"] = {"$gt": 0}

            pipeline = [{"$match": filters}]

            if endpoint == "late_leaderboard":
                pipeline += [
                    {"$group": {
                        "_id": "$emp_code",
                        "total_late_minutes": {"$sum": "$late_minutes"},
                    }},
                    {"$sort": {"total_late_minutes": -1}},
                    {"$limit": limit},
                ]

        collection = "attendance_logs"
        command = {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {},
        }

    try:
        result = db.command(
            "explain",
            command,
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": collection,
            "explain": json.loads(json_util.dumps(result)),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))