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
        value = value.replace(tzinfo=UTC)  
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


def monthly_employee_pipeline(emp_code: str, month: str):
    start_s, end_s, start, end = month_bounds_strings(month)
    pipeline = [
        {"$match": {"emp_code": emp_code}},
        {"$set": {
            "_month_start": {"$dateFromString": {"dateString": start_s}},
            "_month_end": {"$dateFromString": {"dateString": end_s}},
            "_joined": {"$dateFromString": {"dateString": "$joined_on"}},
        }},
        {"$set": {"_effective_start": {"$cond": [{"$gt": ["$_joined", "$_month_start"]}, "$_joined", "$_month_start"]}}},
        {"$set": {"_working_dates": {"$filter": {"input": {"$map": {
            "input": {"$range": [0, {"$max": [0, {"$add": [{"$dateDiff": {"startDate": "$_effective_start", "endDate": "$_month_end", "unit": "day"}}, 1]}]}]},
            "as": "offset", "in": {"$dateAdd": {"startDate": "$_effective_start", "unit": "day", "amount": "$$offset"}}}},
            "as": "day", "cond": {"$in": [{"$dayOfWeek": "$$day"}, [2,3,4,5,6]]}}}}},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start_s]}, {"$lte": ["$date", end_s]}]}}},
            {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1, "overtime_minutes": 1}},
        ], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$set": {"_weekday": {"$cond": [{"$ne": [{"$ifNull": ["$logs.date", None]}, None]}, {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 0]}}},
        {"$group": {"_id": None, "working_days": {"$first": {"$size": "$_working_dates"}},
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$in": ["$_weekday", [2,3,4,5,6]]}]}, {"$cond": ["$logs.half_day", 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$logs.overtime_minutes", 0]}},
        }},
    ]
    return pipeline, start, end


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if not emp:
        raise HTTPException(404, "employee not found")
    pipeline, start, end = monthly_employee_pipeline(emp_code, month)
    rows = aggregation_result(db.employees, pipeline)
    values = rows[0] if rows else {}
    wd = int(values.get("working_days", 0))
    present = round_half_up(values.get("present_days", 0), 2)
    pct = round_half_up(present / wd * 100, 4) if wd else None
    return {"emp_code": emp_code, "month": month, "working_days": wd, "present_days": present,
            "leave_days": int(values.get("leave_days", 0)), "late_count": int(values.get("late_count", 0)),
            "total_late_minutes": int(values.get("total_late_minutes", 0)),
            "total_overtime_minutes": int(values.get("total_overtime_minutes", 0)), "attendance_pct": pct}


def department_summary_pipeline(month: str, department: Optional[str] = None):
    start_s, end_s, _, end = month_bounds_strings(month)
    match = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None: match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start_s]}, {"$lte": ["$date", end_s]}]}}},
            {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "work_hours": 1, "late_minutes": 1, "overtime_minutes": 1}},
        ], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$set": {"_has_log": {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
                   "_weekday": {"$cond": [{"$ne": [{"$ifNull": ["$logs.date", None]}, None]}, {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 0]},
                   "_present": {"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", "2000-01-01"]}}}}, [2,3,4,5,6]]}]}}},
        {"$group": {"_id": {"emp_code": "$emp_code", "department": "$department"},
            "headcount": {"$sum": {"$cond": ["$_has_log", 0, 1]}},
            "present_days": {"$sum": {"$cond": ["$_present", {"$cond": ["$logs.half_day", 0.5, 1]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            "hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", 0]}},
            "hours_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, 1, 0]}},
        }},
        {"$group": {"_id": "$_id.department", "headcount": {"$sum": 1}, "present_days": {"$sum": "$present_days"},
            "late_count": {"$sum": "$late_count"}, "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"}, "on_duty_count": {"$sum": "$on_duty_count"},
            "hours_sum": {"$sum": "$hours_sum"}, "hours_count": {"$sum": "$hours_count"}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1, "late_count": 1,
            "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$hours_count", 0]}, {"$divide": ["$hours_sum", "$hours_count"]}, None]}}},
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    pipeline = department_summary_pipeline(month, department)
    rows = aggregation_result(db.employees, pipeline)
    for row in rows:
        row["present_days"] = round_half_up(row.get("present_days", 0), 2)
        row["avg_work_hours"] = round_half_up(row.get("avg_work_hours"), 2)
    return {"month": month, "items": rows}


def leaderboard_pipeline(month: str, limit: int, department: Optional[str] = None):
    start_s, end_s, _, _ = month_bounds_strings(month)
    pipeline = [
        {"$match": {"date": {"$gte": start_s, "$lte": end_s}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    return {"month": month, "items": aggregation_result(db.attendance_logs, leaderboard_pipeline(month, limit, department))}


def trend_pipeline(department: str, from_date: str, to_date: str):
    start = parse_date(from_date, "from")
    end = parse_date(to_date, "to")
    count = (end - start).days + 1
    return [
        {"$match": {"department": department}},
        {"$set": {"_days": {"$map": {"input": {"$range": [0, count]}, "as": "offset", "in": {"$dateToString": {"date": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": from_date}}, "unit": "day", "amount": "$$offset"}}, "format": "%Y-%m-%d"}}}}}},
        {"$unwind": "$_days"},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code", "day": "$_days"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$eq": ["$date", "$$day"]}]}}},
            {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
        ], "as": "_logs"}},
        {"$unwind": {"path": "$_logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$_days", "headcount": {"$sum": {"$cond": [{"$lte": ["$joined_on", "$_days"]}, 1, 0]}},
            "present_count": {"$sum": {"$cond": [{"$and": [{"$lte": ["$joined_on", "$_days"]}, {"$in": ["$_logs.status", list(PRESENCE)]}]}, {"$cond": ["$_logs.half_day", 0.5, 1]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$and": [{"$lte": ["$joined_on", "$_days"]}, {"$gt": [{"$ifNull": ["$_logs.late_minutes", 0]}, 0]}]}, 1, 0]}}}},
        {"$set": {"_weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$_id"}}}, "date": "$_id"}},
        {"$set": {"is_working_day": {"$in": ["$_weekday", [2,3,4,5,6]]}, "attendance_rate": {"$cond": [{"$and": [{"$in": ["$_weekday", [2,3,4,5,6]]}, {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    start = parse_date(from_date, "from")
    end = parse_date(to_date, "to")
    if end < start or (end - start).days + 1 > 92:
        raise HTTPException(422, "to must be >= from and range must not exceed 92 days")
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "department not found")
    rows = aggregation_result(db.employees, trend_pipeline(department, from_date, to_date))
    for row in rows:
        row["attendance_rate"] = round_half_up(row.get("attendance_rate"), 4)
        row["moving_avg_7d"] = round_half_up(row.get("moving_avg_7d"), 4)
        row["present_count"] = round_half_up(row.get("present_count", 0), 2)
    return {"department": department, "items": rows}


def find_query_for_explain(emp_code=None, date_from=None, date_to=None, status=None):
    query = {}
    if emp_code is not None: query["emp_code"] = emp_code
    if status is not None: query["status"] = status
    if date_from or date_to:
        query["date"] = {}
        if date_from: query["date"]["$gte"] = date_from
        if date_to: query["date"]["$lte"] = date_to
    return query


def explain_find(collection_name: str, query: dict, sort: list, page: int, page_size: int):
    cursor = db[collection_name].find(query).sort(sort).skip((page - 1) * page_size).limit(page_size)
    return cursor.explain("executionStats")


def explain_aggregate(collection_name: str, pipeline: list):
    return db.command({"explain": {"aggregate": collection_name, "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"})


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from: parse_date(date_from, "date_from")
        if date_to: parse_date(date_to, "date_to")
        if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be <= date_to")
        query = find_query_for_explain(emp_code, date_from, date_to, status)
        return explain_find("attendance_logs", query, [("date", DESCENDING), ("emp_code", ASCENDING)], page, page_size)
    if endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422, "emp_code and month are required")
        pipeline, _, _ = monthly_employee_pipeline(emp_code, month)
        return explain_aggregate("employees", pipeline)
    if endpoint == "department_summary":
        if not month: raise HTTPException(422, "month is required")
        return explain_aggregate("employees", department_summary_pipeline(month, department))
    if endpoint == "late_leaderboard":
        if not month: raise HTTPException(422, "month is required")
        return explain_aggregate("attendance_logs", leaderboard_pipeline(month, limit, department))
    if endpoint == "department_trend":
        if not department or not from_date or not to_date: raise HTTPException(422, "department, from and to are required")
        start, end = parse_date(from_date, "from"), parse_date(to_date, "to")
        if end < start or (end - start).days + 1 > 92: raise HTTPException(422, "invalid date range")
        return explain_aggregate("employees", trend_pipeline(department, from_date, to_date))
    raise HTTPException(422, "unsupported endpoint")
