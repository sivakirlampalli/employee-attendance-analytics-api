"""Employee Attendance & Analytics API - candidate implementation.

Run from the repository root with: uvicorn app.main:app --port 8000
Configuration: MONGO_URI and MONGO_DB environment variables.
"""
import os
import re
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional
from bson.json_util import dumps, loads

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # Existing environment variables take precedence over values in .env.

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# MongoDB indexes. Creating an existing index again is safe (idempotent).
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="ix_attendance_employee_date_status")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", ASCENDING)], name="ix_attendance_status_date")


@app.on_event("startup")
def startup() -> None:
    # Index creation is deliberately performed at startup, not as a manual step.
    ensure_indexes()


# --------------------------------------------------------------------------- #
# Time / serialization helpers
# --------------------------------------------------------------------------- #
def truncate_seconds(value: datetime) -> datetime:
    """Normalize an instant to UTC and truncate fractional seconds (R1)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def epoch_ms_to_datetime(value: int) -> datetime:
    if type(value) is not int or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise HTTPException(422, "timestamp must be an integer epoch-millisecond value in range")
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        raise HTTPException(422, "timestamp is out of range")


def datetime_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return int(truncate_seconds(value).timestamp() * 1000)


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """Return the shift's start date in IST, including the overnight early-morning rule."""
    local = truncate_seconds(punch_in).astimezone(IST)
    start_t = time.fromisoformat(shift_start)
    end_t = time.fromisoformat(shift_end)
    shift_day = local.date()
    if end_t <= start_t and local.time().replace(tzinfo=None) < end_t:
        shift_day -= timedelta(days=1)
    return shift_day.isoformat()


def shift_start_datetime(attendance_day: str, shift_start: str) -> datetime:
    day = date.fromisoformat(attendance_day)
    t = time.fromisoformat(shift_start)
    return datetime.combine(day, t, tzinfo=IST).astimezone(UTC)


def shift_end_datetime(attendance_day: str, shift_start: str, shift_end: str) -> datetime:
    day = date.fromisoformat(attendance_day)
    start_t = time.fromisoformat(shift_start)
    end_t = time.fromisoformat(shift_end)
    if end_t <= start_t:
        day += timedelta(days=1)
    return datetime.combine(day, end_t, tzinfo=IST).astimezone(UTC)


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_day: Optional[str] = None) -> int:
    punch_in = truncate_seconds(punch_in)
    local = punch_in.astimezone(IST)
    if attendance_day is None:
        attendance_day = local.date().isoformat()
    shift_start_at = shift_start_datetime(attendance_day, shift_start)
    elapsed = int((punch_in - shift_start_at).total_seconds() // 60)
    seconds_late = (punch_in - shift_start_at).total_seconds()
    return max(0, elapsed) if seconds_late > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(str((truncate_seconds(punch_out) - truncate_seconds(punch_in)).total_seconds()))
    hours = (seconds / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, attendance_day: str) -> int:
    punch_out = truncate_seconds(punch_out)
    expected_end = shift_end_datetime(attendance_day, shift_start, shift_end)
    elapsed_seconds = (punch_out - expected_end).total_seconds()
    whole_minutes = int(elapsed_seconds // 60)
    return whole_minutes if whole_minutes >= 30 else 0


def serialize_employee(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    if "created_at" in result:
        result["created_at"] = datetime_to_epoch_ms(result["created_at"])
    return result


def serialize_attendance(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    result.setdefault("history", [])
    result.setdefault("half_day", False)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("work_hours", None)
    result.setdefault("punch_in", None)
    result.setdefault("punch_out", None)
    result["punch_in"] = datetime_to_epoch_ms(result["punch_in"])
    result["punch_out"] = datetime_to_epoch_ms(result["punch_out"])
    normalized_history = []
    for entry in result["history"] or []:
        item = dict(entry)
        item["at"] = datetime_to_epoch_ms(item.get("at"))
        changes = {}
        for field_name, change in (item.get("changes") or {}).items():
            pair = dict(change)
            if field_name in {"punch_in", "punch_out"}:
                pair["from"] = datetime_to_epoch_ms(pair.get("from"))
                pair["to"] = datetime_to_epoch_ms(pair.get("to"))
            changes[field_name] = pair
        item["changes"] = changes
        normalized_history.append(item)
    result["history"] = normalized_history
    return result


def validate_date_string(value: str, field_name: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, f"{field_name} must be YYYY-MM-DD")
    if parsed.isoformat() != value:
        raise HTTPException(422, f"{field_name} must be YYYY-MM-DD")
    return parsed


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def joined_on_is_date(cls, value: str) -> str:
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)


# --------------------------------------------------------------------------- #
# Existing endpoints - reviewed and corrected (Part A)
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    skip = (page - 1) * page_size
    total = db.employees.count_documents(query)
    cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    return {"items": [serialize_employee(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")
    ts = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else truncate_seconds(datetime.now(UTC))
    local = ts.astimezone(IST)
    attendance_day = attendance_date_for(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], attendance_day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return serialize_attendance(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from:
        validate_date_string(date_from, "date_from")
    if date_to:
        validate_date_string(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
    if status is not None and status not in ALL_STATUSES:
        raise HTTPException(status_code=422, detail="invalid status")
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    total = db.attendance_logs.count_documents(query)
    cursor = (db.attendance_logs.find(query, {"_id": 0})
              .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
              .skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize_attendance(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# TODO: implement the remaining contract endpoints in Part B and Part C.
# POST /attendance/punch-out
# PATCH /attendance/{emp_code}/{date}
# GET /analytics/employees/{emp_code}/monthly
# GET /analytics/departments/summary
# GET /analytics/leaderboard/late
# GET /analytics/departments/{department}/trend
# GET /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Remaining contract endpoints
# --------------------------------------------------------------------------- #
class PunchCorrectionIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    punch_out: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    regularized_by: str = Field(min_length=1, max_length=50)
    reason: str = Field(min_length=5, max_length=200)


def is_weekday(day: date) -> bool:
    return day.weekday() < 5


def round_half_up(value, places=2):
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def _attendance_query_for_range(emp_code: str, start: str, end: str):
    return {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")
    ts = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else truncate_seconds(datetime.now(UTC))
    # Most recent open shift whose punch-in is not after the requested punch-out.
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}, "punch_out": None},
        sort=[("punch_in", DESCENDING), ("date", DESCENDING)],
    )
    if record is None:
        closed = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}, "punch_out": {"$ne": None}},
            sort=[("punch_in", DESCENDING)],
        )
        if closed is not None:
            raise HTTPException(status_code=409, detail="attendance already punched out")
        raise HTTPException(status_code=404, detail="no open punch-in found")
    pin = truncate_seconds(record["punch_in"])
    if ts <= pin or ts > pin + timedelta(hours=24):
        raise HTTPException(status_code=422, detail="punch-out must be after punch-in and within 24 hours")
    hours = compute_work_hours(pin, ts)
    overtime = compute_overtime(ts, emp["shift_start"], emp["shift_end"], record["date"])
    half_day = hours < 4.50
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": hours, "overtime_minutes": overtime, "half_day": half_day}},
    )
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="attendance was punched out concurrently")
    record.update({"punch_out": ts, "work_hours": hours, "overtime_minutes": overtime, "half_day": half_day})
    return serialize_attendance(record)


@app.patch("/attendance/{emp_code}/{attendance_day}")
def correct_attendance(emp_code: str, attendance_day: str, body: PunchCorrectionIn):
    validate_date_string(attendance_day)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": attendance_day})
    if record is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    patch = body.model_dump(exclude_unset=True)
    by = patch.pop("regularized_by")
    reason = patch.pop("reason")
    old = dict(record)
    new_status = patch.get("status", record.get("status"))
    if new_status in {"ABSENT", "LEAVE"}:
        if "punch_in" in patch and patch["punch_in"] is not None:
            raise HTTPException(status_code=422, detail="ABSENT/LEAVE cannot have punch times")
        if "punch_out" in patch and patch["punch_out"] is not None:
            raise HTTPException(status_code=422, detail="ABSENT/LEAVE cannot have punch times")
        proposed_pin = proposed_pout = None
    else:
        proposed_pin = epoch_ms_to_datetime(patch["punch_in"]) if patch.get("punch_in") is not None else record.get("punch_in")
        proposed_pout = epoch_ms_to_datetime(patch["punch_out"]) if patch.get("punch_out") is not None else record.get("punch_out")
        if proposed_pin is None:
            raise HTTPException(status_code=422, detail="presence status requires punch_in")
        if attendance_date_for(proposed_pin, emp["shift_start"], emp["shift_end"]) != attendance_day:
            raise HTTPException(status_code=422, detail="punch_in must belong to the attendance date")
        if proposed_pout is not None and (proposed_pout <= proposed_pin or proposed_pout > proposed_pin + timedelta(hours=24)):
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    proposed = dict(record)
    proposed["status"] = new_status
    proposed["punch_in"] = proposed_pin
    proposed["punch_out"] = proposed_pout
    if new_status in {"ABSENT", "LEAVE"}:
        proposed.update({"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False})
    else:
        proposed["late_minutes"] = compute_late_minutes(proposed_pin, emp["shift_start"], attendance_day)
        if proposed_pout is not None:
            proposed["work_hours"] = compute_work_hours(proposed_pin, proposed_pout)
            proposed["overtime_minutes"] = compute_overtime(proposed_pout, emp["shift_start"], emp["shift_end"], attendance_day)
            proposed["half_day"] = proposed["work_hours"] < 4.50
        else:
            proposed.update({"work_hours": None, "overtime_minutes": 0, "half_day": False})

    allowed = {"status", "punch_in", "punch_out"}
    changes = {}
    set_values = {}
    for field in allowed:
        before, after = old.get(field), proposed.get(field)
        if before != after:
            changes[field] = {"from": before, "to": after}
            set_values[field] = after
    for field in ("work_hours", "late_minutes", "overtime_minutes", "half_day"):
        before, after = old.get(field, {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}[field]), proposed.get(field)
        if before != after:
            changes[field] = {"from": before, "to": after}
            set_values[field] = after
    if not changes:
        raise HTTPException(status_code=422, detail="correction does not change any fields")

    history_entry = {"at": truncate_seconds(datetime.now(UTC)), "by": by, "reason": reason, "changes": changes}
    # Optimistic concurrency: update only if the stored values have not changed since read.
    old_history = record.get("history", [])
    try:
        result = db.attendance_logs.update_one(
            {"_id": record["_id"], "history": old_history},
            {"$set": set_values, "$push": {"history": history_entry}},
        )
    except PyMongoError:
        raise HTTPException(status_code=409, detail="could not save correction")
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="attendance changed concurrently; retry correction")
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-\d{2}$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")
    try:
        year, mon = map(int, month.split("-"))
        month_start = date(year, mon, 1)
        month_end = date(year + (mon == 12), 1 if mon == 12 else mon + 1, 1) - timedelta(days=1)
    except (ValueError, OverflowError):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    start = max(month_start, date.fromisoformat(emp["joined_on"]))
    working_days = sum(1 for n in range((month_end - start).days + 1) if is_weekday(start + timedelta(days=n))) if start <= month_end else 0
    pipeline = [
        {"$match": _attendance_query_for_range(emp_code, month_start.isoformat(), month_end.isoformat())},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE_STATUSES)]}, {"$lt": [{"$dateFromString": {"dateString": "$date"}}, {"$dateFromString": {"dateString": (month_end + timedelta(days=1)).isoformat()}}]}]}, {"$cond": [{"$and": [{"$eq": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]}, False]}, 0, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
        }},
    ]
    # Filter records to weekdays in Python only for the calendar rule; all attendance metrics
    # themselves are sourced from stored fields and are aggregated by MongoDB.
    docs = list(db.attendance_logs.aggregate([
        {"$match": _attendance_query_for_range(emp_code, month_start.isoformat(), month_end.isoformat())},
        {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": {"$ifNull": ["$half_day", False]},
                      "late_minutes": {"$ifNull": ["$late_minutes", 0]}, "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]}, "work_hours": 1}}
    ]))
    present_days = sum((0.5 if d.get("half_day", False) else 1.0) for d in docs
                       if d.get("status") in PRESENCE_STATUSES and is_weekday(date.fromisoformat(d["date"])))
    leave_days = sum(1 for d in docs if d.get("status") == "LEAVE")
    late_count = sum(1 for d in docs if d.get("late_minutes", 0) > 0)
    total_late_minutes = sum(d.get("late_minutes", 0) for d in docs)
    attendance_pct = round_half_up(present_days / working_days * 100, 2) if working_days else None
    return {"emp_code": emp_code, "month": month, "working_days": working_days, "present_days": round_half_up(present_days, 2),
            "leave_days": leave_days, "late_count": late_count, "total_late_minutes": total_late_minutes,
            "total_overtime_minutes": sum(d.get("overtime_minutes", 0) for d in docs),
            "attendance_pct": attendance_pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    try:
        year, mon = map(int, month.split("-"))
        start = date(year, mon, 1)
        end = date(year + (mon == 12), 1 if mon == 12 else mon + 1, 1) - timedelta(days=1)
    except (ValueError, OverflowError):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    # Aggregation joins employees to their month records; employees without logs remain in headcount.
    pipeline = [
        {"$match": {"joined_on": {"$lte": end.isoformat()}, **({"department": department} if department is not None else {})}},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start.isoformat()]}, {"$lte": ["$date", end.isoformat()]}]}}},
        ], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "headcount": {"$addToSet": "$emp_code"},
            "work_hours": {"$push": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE_STATUSES)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", None]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            "present_days": {"$sum": {"$cond": [{"$and": [
                {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
                {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, [2, 3, 4, 5, 6]]}
            ]}, {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}},
        }},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount"},
            "avg_work_hours": {"$avg": {"$filter": {"input": "$work_hours", "as": "h", "cond": {"$ne": ["$$h", None]}}}},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1, "present_days": 1}},
        {"$sort": {"department": 1}},
    ]
    rows = list(db.employees.aggregate(pipeline))
    for row in rows:
        if row.get("avg_work_hours") is not None:
            row["avg_work_hours"] = round_half_up(row["avg_work_hours"], 2)
    return {"month": month, "items": rows}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
                     limit: int = Query(default=10, ge=1, le=50),
                     department: Optional[str] = None):
    try:
        year, mon = map(int, month.split("-"))
        start = date(year, mon, 1)
        end = date(year + (mon == 12), 1 if mon == 12 else mon + 1, 1) - timedelta(days=1)
    except (ValueError, OverflowError):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    pipeline = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1, "rank": 1}},
    ]
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    date_from, date_to = from_, to
    from_day = validate_date_string(date_from, "from")
    to_day = validate_date_string(date_to, "to")
    if to_day < from_day:
        raise HTTPException(status_code=422, detail="date_to must be on or after date_from")
    if (to_day - from_day).days + 1 > 92:
        raise HTTPException(status_code=422, detail="date range cannot exceed 92 days")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="department not found")
    # MongoDB densifies the date series; no Python-side loop creates missing calendar days.
    pipeline = [
        {"$match": {"date": {"$gte": date_from, "$lte": date_to}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {"$group": {"_id": "$date",
            "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE_STATUSES)]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$project": {"_id": 0, "date": "$_id", "present_count": 1, "late_count": 1}},
        {"$set": {"dateValue": {"$dateFromString": {"dateString": "$date"}}}},
        {"$densify": {"field": "dateValue", "range": {"step": 1, "unit": "day", "bounds": [
            datetime.combine(from_day, time.min, tzinfo=UTC),
            datetime.combine(to_day + timedelta(days=1), time.min, tzinfo=UTC)
        ]}}},
        {"$set": {"date": {"$dateToString": {"date": "$dateValue", "format": "%Y-%m-%d"}}}},
        {"$set": {"present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]}}},
        {"$lookup": {"from": "employees", "let": {"day": "$date"}, "pipeline": [
            {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
            {"$count": "count"},
        ], "as": "headcountData"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$headcountData.count", 0]}, 0]},
                  "is_working_day": {"$in": [{"$dayOfWeek": "$dateValue"}, [2, 3, 4, 5, 6]]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]}, None]}}},
        {"$setWindowFields": {"sortBy": {"dateValue": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}
        }}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}},
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    # $densify output values may be BSON datetimes; make rates null on non-working/no-headcount days.
    for row in rows:
        if row.get("attendance_rate") is not None:
            row["attendance_rate"] = round_half_up(row["attendance_rate"], 4)
        if row.get("moving_avg_7d") is not None:
            row["moving_avg_7d"] = round_half_up(row["moving_avg_7d"], 4)
    return {"department": department, "date_from": date_from, "date_to": date_to, "items": rows}


def make_json_safe(data):
    """Convert MongoDB explain output into JSON-safe values."""
    return json.loads(dumps(data))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    month: Optional[str] = None,
    department: Optional[str] = None,
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
):
    try:
        if endpoint == "attendance_list":
            q = {}
            if emp_code: q["emp_code"] = emp_code
            if date_from: q.setdefault("date", {})["$gte"] = validate_date_string(date_from, "date_from").isoformat()
            if date_to: q.setdefault("date", {})["$lte"] = validate_date_string(date_to, "date_to").isoformat()
            if status:
                if status not in ALL_STATUSES: raise HTTPException(status_code=422, detail="invalid status")
                q["status"] = status
            return make_json_safe({"endpoint": endpoint, "collection": "attendance_logs", "explain": db.command({"explain": {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page-1)*page_size, "limit": page_size}, "verbosity": "executionStats"})})
        if endpoint == "employee_monthly":
            if not emp_code or not month: raise HTTPException(status_code=422, detail="emp_code and month are required")
            if not re.fullmatch(r"\d{4}-\d{2}", month): raise HTTPException(status_code=422, detail="month must be YYYY-MM")
            y, m = map(int, month.split("-")); s = date(y,m,1); e = date(y + (m==12), 1 if m==12 else m+1, 1)-timedelta(days=1)
            pipeline = [{"$match": _attendance_query_for_range(emp_code,s.isoformat(),e.isoformat())}, {"$sort":{"date":1}}]
            return make_json_safe({"endpoint": endpoint, "collection": "attendance_logs", "explain": db.command({"explain": {"aggregate":"attendance_logs","pipeline":pipeline,"cursor":{}}, "verbosity":"executionStats"})})
        if endpoint == "department_summary":
            if not month: raise HTTPException(status_code=422, detail="month is required")
            if not re.fullmatch(r"\d{4}-\d{2}", month): raise HTTPException(status_code=422, detail="month must be YYYY-MM")
            y,m=map(int,month.split("-")); s=date(y,m,1); e=date(y+(m==12),1 if m==12 else m+1,1)-timedelta(days=1)
            pipeline=[{"$match":{"joined_on":{"$lte":e.isoformat()}}},{"$lookup":{"from":"attendance_logs","let":{"code":"$emp_code"},"pipeline":[{"$match":{"$expr":{"$and":[{"$eq":["$emp_code","$$code"]},{"$gte":["$date",s.isoformat()]},{"$lte":["$date",e.isoformat()]}]}}}],"as":"logs"}}]
            return make_json_safe({"endpoint": endpoint, "collection": "employees", "explain": db.command({"explain":{"aggregate":"employees","pipeline":pipeline,"cursor":{}},"verbosity":"executionStats"})})
        if endpoint == "late_leaderboard":
            if not month: raise HTTPException(status_code=422, detail="month is required")
            if not re.fullmatch(r"\d{4}-\d{2}", month): raise HTTPException(status_code=422, detail="month must be YYYY-MM")
            y,m=map(int,month.split("-")); s=date(y,m,1); e=date(y+(m==12),1 if m==12 else m+1,1)-timedelta(days=1)
            pipeline=[{"$match":{"date":{"$gte":s.isoformat(),"$lte":e.isoformat()},"late_minutes":{"$gt":0}}},{"$group":{"_id":"$emp_code","total_late_minutes":{"$sum":"$late_minutes"}}}]
            if department is not None:
                pipeline += [{"$lookup":{"from":"employees","localField":"_id","foreignField":"emp_code","as":"employee"}},{"$unwind":"$employee"},{"$match":{"employee.department":department}}]
            return make_json_safe(db.command({"explain":{"aggregate":"attendance_logs","pipeline":pipeline,"cursor":{}},"verbosity":"executionStats"}))
        if endpoint == "department_trend":
            if not department or not from_ or not to: raise HTTPException(status_code=422, detail="department, from and to are required")
            date_from, date_to = validate_date_string(from_, "from").isoformat(), validate_date_string(to, "to").isoformat()
            if date_to < date_from: raise HTTPException(status_code=422, detail="to must be on or after from")
            pipeline=[{"$match":{"date":{"$gte":date_from,"$lte":date_to}}},{"$lookup":{"from":"employees","localField":"emp_code","foreignField":"emp_code","as":"employee"}},{"$unwind":"$employee"},{"$match":{"employee.department":department}}]
            return make_json_safe(db.command({"explain":{"aggregate":"attendance_logs","pipeline":pipeline,"cursor":{}},"verbosity":"executionStats"}))
    except HTTPException:
        raise
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail=f"explain failed: {exc.__class__.__name__}")
