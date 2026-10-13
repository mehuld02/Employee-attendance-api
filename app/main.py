"""Employee Attendance & Analytics API.

Run: uvicorn app.main:app --port 8000
Required environment variables: MONGO_URI, MONGO_DB
"""
import os
import re
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
EPOCH_MIN_MS = 100_000_000_000
EPOCH_MAX_MS = 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ------------------------------- startup/indexes ---------------------------
@app.on_event("startup")
def ensure_indexes():
    # Unique indexes are the concurrency guarantees, not just performance hints.
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="ix_employee_joined_department")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_status_employee")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_attendance_employee_punchin")


# -------------------------------- utilities --------------------------------
def parse_date(value: str, field: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, f"{field} must be YYYY-MM-DD")
    if parsed.isoformat() != value:
        raise HTTPException(422, f"{field} must be YYYY-MM-DD")
    return parsed


def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    first = date(year, month, 1)
    return first, date(year, month, monthrange(year, month)[1])


def validate_epoch_ms(value: Optional[int], field: str = "punched_at") -> Optional[int]:
    if value is None:
        return None
    # bool is an int subclass; reject it. Pydantic also rejects floats/strings below.
    if isinstance(value, bool) or not isinstance(value, int) or not EPOCH_MIN_MS <= value <= EPOCH_MAX_MS:
        raise HTTPException(422, f"{field} must be epoch milliseconds")
    return value


def from_ms(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    validate_epoch_ms(value)
    return datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0)


def to_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)  # PyMongo naive datetimes represent UTC.
    return int(value.timestamp() * 1000)


def truncate_second(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def local_datetime(day: date, shift_time: str) -> datetime:
    hour, minute = map(int, shift_time.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=IST).astimezone(UTC)


def is_overnight(emp: dict) -> bool:
    return emp["shift_end"] <= emp["shift_start"]


def attendance_day(punch_in: datetime, emp: dict) -> date:
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(emp) and local.time().replace(tzinfo=None) < time(*map(int, emp["shift_end"].split(":"))):
        day -= timedelta(days=1)
    return day


def shift_start_instant(attendance_date: str, emp: dict) -> datetime:
    return local_datetime(parse_date(attendance_date), emp["shift_start"])


def shift_end_instant(attendance_date: str, emp: dict) -> datetime:
    day = parse_date(attendance_date)
    if is_overnight(emp):
        day += timedelta(days=1)
    return local_datetime(day, emp["shift_end"])


def compute_late_minutes(punch_in: Optional[datetime], shift_start: str, attendance_date_value: str, emp: Optional[dict] = None) -> int:
    if punch_in is None:
        return 0
    if emp is None:
        emp = {"shift_start": shift_start, "shift_end": "18:30"}
    start = shift_start_instant(attendance_date_value, emp)
    minutes_seconds = int((truncate_second(punch_in) - start).total_seconds())
    if minutes_seconds <= 600:
        return 0
    return max(0, minutes_seconds // 60)


def round_half_up(value: Optional[float], places: int = 2) -> Optional[float]:
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def compute_work_hours(punch_in: Optional[datetime], punch_out: Optional[datetime]) -> Optional[float]:
    if punch_in is None or punch_out is None:
        return None
    seconds = int((truncate_second(punch_out) - truncate_second(punch_in)).total_seconds())
    return round_half_up(seconds / 3600, 2)


def compute_overtime(punch_out: Optional[datetime], attendance_date_value: str, emp: dict) -> int:
    if punch_out is None:
        return 0
    end = shift_end_instant(attendance_date_value, emp)
    seconds = int((truncate_second(punch_out) - end).total_seconds())
    minutes = seconds // 60
    return int(minutes) if minutes >= 30 else 0


def working_days(start: date, end: date) -> int:
    if end < start:
        return 0
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def output_employee(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    if "created_at" in result:
        result["created_at"] = to_ms(result["created_at"])
    return result


def output_attendance(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)
    result.setdefault("history", [])
    result.setdefault("punch_in", None)
    result.setdefault("punch_out", None)
    result.setdefault("work_hours", None)
    result["punch_in"] = to_ms(result.get("punch_in"))
    result["punch_out"] = to_ms(result.get("punch_out"))
    for item in result["history"]:
        item["at"] = to_ms(item.get("at"))
        for field in ("punch_in", "punch_out"):
            if field in item.get("changes", {}):
                change = item["changes"][field]
                change["from"] = to_ms(change.get("from")) if isinstance(change.get("from"), datetime) else change.get("from")
                change["to"] = to_ms(change.get("to")) if isinstance(change.get("to"), datetime) else change.get("to")
    return result


def page_validate(page: int, page_size: int) -> None:
    if page < 1 or not 1 <= page_size <= 100:
        raise HTTPException(422, "page must be >= 1 and page_size must be between 1 and 100")


def month_bounds_strings(month: str) -> tuple[str, str, date, date]:
    start, end = parse_month(month)
    return start.isoformat(), end.isoformat(), start, end


def present_weight(doc: dict) -> float:
    if doc.get("status") not in PRESENCE:
        return 0.0
    day = parse_date(doc["date"])
    if day.weekday() >= 5:
        return 0.0
    return 0.5 if doc.get("half_day", False) else 1.0


def aggregation_result(collection, pipeline):
    return list(collection.aggregate(pipeline, allowDiskUse=True))


# ---------------------------------- models ---------------------------------
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EmployeeIn(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_join_date(cls, value):
        parse_date(value, "joined_on")
        return value

    @model_validator(mode="after")
    def different_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=EPOCH_MIN_MS, le=EPOCH_MAX_MS)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def exact_epoch_ms(cls, value):
        if value is not None and (type(value) is not int or not EPOCH_MIN_MS <= value <= EPOCH_MAX_MS):
            raise ValueError("punched_at must be integer epoch milliseconds")
        return value


class PunchOutIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=EPOCH_MIN_MS, le=EPOCH_MAX_MS)

    @field_validator("punched_at", mode="before")
    @classmethod
    def exact_epoch_ms(cls, value):
        if value is not None and (type(value) is not int or not EPOCH_MIN_MS <= value <= EPOCH_MAX_MS):
            raise ValueError("punched_at must be integer epoch milliseconds")
        return value


class RegularizeIn(StrictModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def exact_epoch_ms(cls, value):
        if value is not None and (type(value) is not int or not EPOCH_MIN_MS <= value <= EPOCH_MAX_MS):
            raise ValueError("punch times must be integer epoch milliseconds or null")
        return value


# -------------------------------- endpoints -------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return output_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    page_validate(page, page_size)
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    docs = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [output_employee(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = truncate_second(from_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC))
    day = attendance_day(ts, emp).isoformat()
    doc = {
        "emp_code": body.emp_code, "date": day, "status": body.status,
        "punch_in": ts, "punch_out": None, "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], day, emp),
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return output_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = truncate_second(from_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC))
    doc = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if not doc:
        raise HTTPException(404, "no punch-in found")
    if doc.get("punch_out") is not None:
        raise HTTPException(409, "record already punched out")
    punch_in_dt = doc.get("punch_in")
    if ts <= punch_in_dt or ts > punch_in_dt + timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    hours = compute_work_hours(punch_in_dt, ts)
    overtime = compute_overtime(ts, doc["date"], emp)
    half_day = hours is not None and hours < 4.50
    updated = db.attendance_logs.find_one_and_update(
        {"_id": doc["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": hours, "overtime_minutes": overtime, "half_day": half_day}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record already punched out")
    return output_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
):
    page_validate(page, page_size)
    if date_from: parse_date(date_from, "date_from")
    if date_to: parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    query = {}
    if emp_code is not None: query["emp_code"] = emp_code
    if status is not None: query["status"] = status
    if date_from or date_to:
        query["date"] = {}
        if date_from: query["date"]["$gte"] = date_from
        if date_to: query["date"]["$lte"] = date_to
    total = db.attendance_logs.count_documents(query)
    docs = db.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [output_attendance(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not old:
        raise HTTPException(404, "attendance record not found")
    current_status = old.get("status")
    current_in = old.get("punch_in")
    current_out = old.get("punch_out")
    new_status = body.status if body.status is not None else current_status
    new_in = from_ms(body.punch_in) if "punch_in" in body.model_fields_set else current_in
    new_out = from_ms(body.punch_out) if "punch_out" in body.model_fields_set else current_out
    if new_status in {"ABSENT", "LEAVE"}:
        if ("punch_in" in body.model_fields_set and body.punch_in is not None) or ("punch_out" in body.model_fields_set and body.punch_out is not None):
            raise HTTPException(422, "ABSENT and LEAVE cannot have punch times")
        new_in, new_out = None, None
    elif new_in is None:
        raise HTTPException(422, "presence status requires punch_in")
    if new_in is not None:
        new_in = truncate_second(new_in)
        if attendance_day(new_in, emp).isoformat() != date:
            raise HTTPException(422, "punch_in must belong to this attendance date")
    if new_out is not None:
        new_out = truncate_second(new_out)
        if new_in is None or new_out <= new_in or new_out > new_in + timedelta(hours=24):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    late = compute_late_minutes(new_in, emp["shift_start"], date, emp) if new_status in PRESENCE else 0
    hours = compute_work_hours(new_in, new_out) if new_status in PRESENCE and new_out is not None else None
    overtime = compute_overtime(new_out, date, emp) if new_status in PRESENCE and new_out is not None else 0
    half = bool(hours is not None and hours < 4.50)
    new_values = {"status": new_status, "punch_in": new_in, "punch_out": new_out, "work_hours": hours, "late_minutes": late, "overtime_minutes": overtime, "half_day": half}
    old_values = {"status": current_status, "punch_in": old.get("punch_in"), "punch_out": old.get("punch_out"), "work_hours": old.get("work_hours"), "late_minutes": old.get("late_minutes", 0), "overtime_minutes": old.get("overtime_minutes", 0), "half_day": old.get("half_day", False)}
    changes = {}
    for key, new_value in new_values.items():
        old_value = old_values.get(key)
        if isinstance(old_value, datetime): old_value = truncate_second(old_value)
        if isinstance(new_value, datetime): new_value = truncate_second(new_value)
        if old_value != new_value:
            changes[key] = {"from": old_value, "to": new_value}
    if not changes:
        raise HTTPException(422, "request does not change the record")
    history_entry = {"at": datetime.now(UTC).replace(microsecond=0), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    history_filter = {"_id": old["_id"], "status": current_status, "punch_in": old.get("punch_in"), "punch_out": old.get("punch_out")}
    # Optimistic compare-and-set: a concurrent edit loses with 409 rather than overwriting another edit.
    if "history" in old:
        history_filter["history"] = old["history"]
    else:
        history_filter["history"] = {"$exists": False}
    result = db.attendance_logs.update_one(history_filter, {"$set": new_values, "$push": {"history": history_entry}})
    if result.modified_count != 1:
        raise HTTPException(409, "record changed concurrently; reload and retry")
    updated = db.attendance_logs.find_one({"_id": old["_id"]})
    return output_attendance(updated)


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
