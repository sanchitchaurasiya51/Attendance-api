import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from datetime import time as Time
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  

log = logging.getLogger("attendance")

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

client = MongoClient(MONGO_URI, tz_aware=True, tzinfo=UTC, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]


def employees_col():
    return db["employees"]


def logs_col():
    return db["attendance_logs"]


PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

MS_MIN, MS_MAX = 100_000_000_000, 4_102_444_800_000
EpochMs = Annotated[int, Field(strict=True, ge=MS_MIN, le=MS_MAX)]

HHMM_RE = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
DATE_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
DAY = timedelta(days=1)


def unprocessable(msg: str, loc: tuple = ("body",)):
    raise HTTPException(status_code=422, detail=[{"loc": list(loc), "msg": msg, "type": "value_error"}])


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def ms_to_dt(ms: int, truncate_seconds: bool = True) -> datetime:
    if truncate_seconds:
        ms -= ms % 1000
    return EPOCH + timedelta(milliseconds=ms)


def dt_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:  
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def ms_or_none(dt: Optional[datetime]) -> Optional[int]:
    return None if dt is None else dt_to_ms(dt)


def hhmm_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def shift_bounds(day: Date, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    s, e = hhmm_minutes(shift_start), hhmm_minutes(shift_end)
    start = datetime.combine(day, Time(s // 60, s % 60), tzinfo=IST)
    end_day = day + DAY if e <= s else day
    end = datetime.combine(end_day, Time(e // 60, e % 60), tzinfo=IST)
    return start, end


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> Date:
    local = punch_in.astimezone(IST)
    day = local.date()
    s, e = hhmm_minutes(shift_start), hhmm_minutes(shift_end)
    if e <= s and local.time() < Time(e // 60, e % 60):
        day -= DAY
    return day


def compute_late_minutes(punch_in: datetime, day: Date, shift_start: str, shift_end: str) -> int:
    start, _ = shift_bounds(day, shift_start, shift_end)
    seconds = int((punch_in - start).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_overtime(punch_out: datetime, day: Date, shift_start: str, shift_end: str) -> int:
    _, end = shift_bounds(day, shift_start, shift_end)
    minutes = int((punch_out - end).total_seconds()) // 60  
    return minutes if minutes >= 30 else 0


def work_hours_hundredths(punch_in: datetime, punch_out: datetime) -> int:
    seconds = int((punch_out - punch_in).total_seconds())
    return (seconds * 200 + 3600) // 7200


def derive_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime],
                  day: Date, shift_start: str, shift_end: str) -> dict:
    if status not in PRESENCE_STATUSES or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    
    out = {
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_in, day, shift_start, shift_end),
        "overtime_minutes": 0,
        "half_day": False,
    }

    if punch_out is not None:
        h100 = work_hours_hundredths(punch_in, punch_out)
        out["work_hours"] = h100 / 100
        out["overtime_minutes"] = compute_overtime(punch_out, day, shift_start, shift_end)
        out["half_day"] = h100 < 450  
    return out


def round_half_up(value: Decimal, places: int) -> float:
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def parse_day(value: str, name: str, loc: str = "query") -> Date:
    """YYYY-MM-DD and a real calendar date, else 422."""
    if not re.match(DATE_RE, value):
        unprocessable(f"{name} must be YYYY-MM-DD", (loc, name))
    try:
        return Date.fromisoformat(value)
    except ValueError:
        unprocessable(f"{name} is not a valid calendar date", (loc, name))


def month_bounds(month: str) -> tuple[Date, Date]:
    year, mon = int(month[:4]), int(month[5:])
    try:
        first = Date(year, mon, 1)
        next_first = Date(year + (mon == 12), mon % 12 + 1, 1)
    except ValueError:
        unprocessable("month is not valid", ("query", "month"))
    return first, next_first - DAY


def count_weekdays(first: Date, last: Date) -> int:
    if last < first:
        return 0
    days = (last - first).days + 1
    full_weeks, extra = divmod(days, 7)
    count = full_weeks * 5
    for i in range(extra):
        if (first + timedelta(days=full_weeks * 7 + i)).weekday() < 5:
            count += 1
    return count

class EmployeeCreate(BaseModel):
    emp_code: Annotated[str, Field(strict=True, pattern=r"^EMP[0-9]{4,6}$")]
    name: Annotated[str, Field(strict=True, min_length=1, max_length=100)]
    email: Annotated[str, Field(strict=True, max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(strict=True, min_length=1, max_length=50)]
    shift_start: Annotated[str, Field(strict=True, pattern=HHMM_RE)] = "09:30"
    shift_end: Annotated[str, Field(strict=True, pattern=HHMM_RE)] = "18:30"
    joined_on: Annotated[str, Field(strict=True, pattern=DATE_RE)]


    @model_validator(mode="after")
    def _check(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            Date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on is not a valid calendar date")
        return self


class PunchInRequest(BaseModel):
    emp_code: Annotated[str, Field(strict=True)]
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: Annotated[str, Field(strict=True)]
    punched_at: Optional[EpochMs] = None


class RegularizeRequest(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: Annotated[str, Field(strict=True, min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(strict=True, min_length=1, max_length=50)]
    

    @model_validator(mode="after")
    def _no_explicit_nulls(self):
        # the schema says these fields are not nullable: "omitted" is fine, an explicit null is not
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc.get("joined_on"),
        "created_at": ms_or_none(doc.get("created_at")),
    }


_INSTANT_FIELDS = ("punch_in", "punch_out")


def _history_value(field: str, value: Any) -> Any:
    return ms_or_none(value) if field in _INSTANT_FIELDS and isinstance(value, datetime) else value


def history_out(entry: dict) -> dict:
    return {
        "at": ms_or_none(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": {
            field: {"from": _history_value(field, ch.get("from")), "to": _history_value(field, ch.get("to"))}
            for field, ch in (entry.get("changes") or {}).items()
        } ,
    }


def record_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": ms_or_none(doc.get("punch_in")),
        "punch_out": ms_or_none(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


def page_window(page: int, page_size: int) -> int:
    return (page - 1) * page_size

_state = {"indexes_ready": False}


def _ensure_index(col, keys, **kwargs):
    try:
        col.create_index(keys, **kwargs)
    except PyMongoError as exc:
        if kwargs.get("unique"):
            log.error("unique index %s failed (%s); creating it as a plain index", kwargs.get("name"), exc)
            kwargs.pop("unique")
            col.create_index(keys, **kwargs)
        else:
            raise


def ensure_indexes() -> None:
    emp, logs = employees_col(), logs_col()
   
    _ensure_index(emp, [("emp_code", 1)], unique=True, name="emp_code_unique")
    _ensure_index(emp, [("department", 1), ("emp_code", 1)], name="department_emp_code")
    _ensure_index(emp, [("department", 1), ("joined_on", 1)], name="department_joined_on")
    _ensure_index(emp, [("joined_on", 1)], name="joined_on")
   
    _ensure_index(logs, [("emp_code", 1), ("date", 1)], unique=True, name="emp_code_date_unique")
    _ensure_index(logs, [("date", -1), ("emp_code", 1)], name="date_desc_emp_code")
    _state["indexes_ready"] = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except Exception as exc:  
        log.warning("index creation deferred: %s", exc)
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB is not reachable")
    if not _state["indexes_ready"]:
        try:
            ensure_indexes()
        except PyMongoError as exc:
            log.warning("index creation still failing: %s", exc)
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = ms_to_dt(now_ms(), truncate_seconds=False)
    try:
        employees_col().insert_one(doc) 
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    query = {} if department is None else {"department": department}
    col = employees_col()
    total = col.count_documents(query) if query else col.estimated_document_count()
    skip = page_window(page, page_size)
    items = []
    if skip < 2 ** 62:
        cursor = col.find(query, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
        items = [employee_out(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def _get_employee(emp_code: str) -> dict:
    emp = employees_col().find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")
    return emp


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = _get_employee(body.emp_code)
    shift_start, shift_end = emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")
    ts = ms_to_dt(body.punched_at if body.punched_at is not None else now_ms())
    day = attendance_date(ts, shift_start, shift_end)
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, day, shift_start, shift_end),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        logs_col().insert_one(doc)  # unique (emp_code, date): the race is decided by the database
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = _get_employee(body.emp_code)
    shift_start, shift_end = emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")
    out_dt = ms_to_dt(body.punched_at if body.punched_at is not None else now_ms())

    rec = logs_col().find_one(
        {"emp_code": body.emp_code,
         "date": {"$lte": out_dt.astimezone(IST).date().isoformat()},
         "punch_in": {"$lte": out_dt}},
        sort=[("date", -1)],
    )

    if rec is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")

    in_dt = rec["punch_in"]
    elapsed = (out_dt - in_dt).total_seconds()
    if elapsed <= 0:
        unprocessable("punched_at must be after punch_in")
    if elapsed > 24 * 3600:
        unprocessable("punched_at is more than 24 hours after punch_in")

    fields = derive_fields(rec["status"], in_dt, out_dt, Date.fromisoformat(rec["date"]), shift_start, shift_end)
    updated = logs_col().find_one_and_update(
        {"emp_code": rec["emp_code"], "date": rec["date"], "punch_in": in_dt, "punch_out": None},
        {"$set": {"punch_out": out_dt, **fields}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record was already punched out or changed concurrently")
    return record_out(updated)


def build_attendance_find(emp_code, date_from, date_to, status, page, page_size):
    df = parse_day(date_from, "date_from") if date_from else None
    dt = parse_day(date_to, "date_to") if date_to else None
    if df and dt and df > dt:
        unprocessable("date_from must not be after date_to", ("query", "date_from"))
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if df or dt:
        query["date"] = {}
        if df:
            query["date"]["$gte"] = df.isoformat()
        if dt:
            query["date"]["$lte"] = dt.isoformat()
    if status is not None:
        query["status"] = status
    sort = {"date": -1, "emp_code": 1}
    return query, sort, page_window(page, page_size), page_size


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    
    query, sort, skip, limit = build_attendance_find(emp_code, date_from, date_to, status, page, page_size)
    col = logs_col()
    total = col.count_documents(query) if query else col.estimated_document_count()
    items = []
    if skip < 2 ** 62:
        cursor = col.find(query, {"_id": 0}).sort(list(sort.items())).skip(skip).limit(limit)
        items = [record_out(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeRequest):
    day = parse_day(date, "date", loc="path")
    emp = _get_employee(emp_code)
    shift_start, shift_end = emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")
    rec = logs_col().find_one({"emp_code": emp_code, "date": day.isoformat()})
    if rec is None:
        raise HTTPException(status_code=404, detail="no attendance record for that date")

    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    new_status = body.status or old["status"]
    new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old["punch_in"]
    new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old["punch_out"]

    if new_status in PRESENCE_STATUSES:
        if new_in is None:
            unprocessable("a presence status requires a punch_in")
    else:  
        if body.punch_in is not None or body.punch_out is not None:
            unprocessable(f"punch times cannot be supplied for status {new_status}")
        new_in = new_out = None

    if new_in is not None:
        if attendance_date(new_in, shift_start, shift_end) != day:
            unprocessable("punch_in must stay on the record's attendance date")
        if new_out is not None:
            gap = (new_out - new_in).total_seconds()
            if gap <= 0:
                unprocessable("punch_out must be after punch_in")
            if gap > 24 * 3600:
                unprocessable("punch_out must be within 24 hours of punch_in")

    if (new_status, new_in, new_out) == (old["status"], old["punch_in"], old["punch_out"]):
        unprocessable("the request does not change anything")

    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
           **derive_fields(new_status, new_in, new_out, day, shift_start, shift_end)}
    changes = {f: {"from": old[f], "to": new[f]} for f in old if old[f] != new[f]}
    entry = {"at": ms_to_dt(now_ms(), truncate_seconds=False), "by": body.regularized_by,
             "reason": body.reason, "changes": changes}


    history = rec.get("history")
    guard = {"emp_code": emp_code, "date": day.isoformat(), "status": old["status"],
             "punch_in": old["punch_in"], "punch_out": old["punch_out"]}
    if isinstance(history, list):
        guard["history"] = {"$size": len(history)}
        update = {"$set": new, "$push": {"history": entry}}
    else:  
        guard["history"] = None
        update = {"$set": {**new, "history": [entry]}}
    updated = logs_col().find_one_and_update(guard, update, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(status_code=409, detail="the record was modified concurrently; retry")
    return record_out(updated)


_DATE_OBJ = {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}
_IS_PRESENT = {"$in": ["$status", PRESENCE_STATUSES]}
_IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": _DATE_OBJ}, 5]}
_IS_LATE = {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}

_PRESENT_HALVES = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 1, 2]}
_LATE_MINUTES = {"$ifNull": ["$late_minutes", 0]}
_HAS_HOURS = {"$and": [_IS_PRESENT, {"$isNumber": "$work_hours"}]}


def _count_if(cond: dict) -> dict:
    return {"$sum": {"$cond": [cond, 1, 0]}}


def monthly_pipeline(emp_code: str, first: Date, last: Date) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_halves": {"$sum": {"$cond": [{"$and": [_IS_PRESENT, _IS_WEEKDAY]}, _PRESENT_HALVES, 0]}},
            "leave_days": _count_if({"$eq": ["$status", "LEAVE"]}),
            "late_count": _count_if(_IS_LATE),
            "total_late_minutes": {"$sum": _LATE_MINUTES},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        } },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Annotated[str, Query(pattern=MONTH_RE)]):
    emp = _get_employee(emp_code)
    first, last = month_bounds(month)
    rows = list(logs_col().aggregate(monthly_pipeline(emp_code, first, last)))
    row = rows[0] if rows else {}
    present_days = Decimal(int(row.get("present_halves", 0))) / 2

    start = max(first, Date.fromisoformat(emp["joined_on"]))
    working_days = count_weekdays(start, last)
    pct = None if working_days == 0 else round_half_up(present_days / working_days * 100, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


def summary_pipeline(first: Date, last: Date, department: Optional[str]) -> list:
    """Employee-driven, so people without any log still count (R9) and the department comes
    from the employee. Each employee's logs are pre-aggregated inside $lookup (one index range
    scan per employee on (emp_code, date)), then the employees are rolled up per department."""
    match: dict = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        match["department"] = department
    per_employee = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_halves": {"$sum": {"$cond": [{"$and": [_IS_PRESENT, _IS_WEEKDAY]}, _PRESENT_HALVES, 0]}},
           
            "hours_sum": {"$sum": {"$cond": [_HAS_HOURS, {"$toDecimal": "$work_hours"}, 0]}},
            "hours_n": _count_if(_HAS_HOURS),
            "late_count": _count_if(_IS_LATE),
            "total_late_minutes": {"$sum": _LATE_MINUTES},
            "leave_count": _count_if({"$eq": ["$status", "LEAVE"]}),
            "on_duty_count": _count_if({"$eq": ["$status", "ON_DUTY"]}),
        } } ,
    ]

    def total(field: str) -> dict:
        return {"$sum": {"$ifNull": [f"$agg.{field}", 0]}}

    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": per_employee, "as": "agg"}},
        {"$addFields": {"agg": {"$ifNull": [{"$arrayElemAt": ["$agg", 0]}, {}]}}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_halves": total("present_halves"),
            "hours_sum": total("hours_sum"),
            "hours_n": total("hours_n"),
            "late_count": total("late_count"),
            "total_late_minutes": total("total_late_minutes"),
            "leave_count": total("leave_count"),
            "on_duty_count": total("on_duty_count"),
        }},
        {"$sort": {"_id": 1}},
    ]


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal128):
        return value.to_decimal()
    return Decimal(str(value))


@app.get("/analytics/departments/summary")
def department_summary(month: Annotated[str, Query(pattern=MONTH_RE)], department: Optional[str] = None):
    first, last = month_bounds(month)
    items = []
    for r in employees_col().aggregate(summary_pipeline(first, last, department)):
        n = int(r["hours_n"])
        avg = None if n == 0 else round_half_up(_to_decimal(r["hours_sum"]) / n, 2)
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": round_half_up(Decimal(int(r["present_halves"])) / 2, 2),
            "avg_work_hours": avg,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        } )
    return {"month": month, "items": items}


def leaderboard_pipeline(first: Date, last: Date, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": _LATE_MINUTES},
                    "late_count": _count_if(_IS_LATE)}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of an unknown employee have no match and drop out here
    ]
    if department is not None:  # ranking happens inside the department, so filter BEFORE ranking
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        # standard competition ranking: ties share a rank, the next rank is skipped (1, 2, 2, 4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit is applied AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Annotated[str, Query(pattern=MONTH_RE)],
                     limit: Annotated[int, Query(ge=1, le=50)] = 10,
                     department: Optional[str] = None):
    first, last = month_bounds(month)
    items = [
        {"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
         "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"])}
        for r in logs_col().aggregate(leaderboard_pipeline(first, last, limit, department))
    ]
    return {"month": month, "items": items}


def trend_inputs(department: str) -> tuple[list, list]:
   
    rows = list(employees_col().aggregate([
        {"$match": {"department": department}},
        {"$group": {"_id": "$joined_on", "n": {"$sum": 1}, "codes": {"$push": "$emp_code"}}},
        {"$sort": {"_id": 1}},
    ] ) )
    if not rows:
        raise HTTPException(status_code=404, detail="unknown department")
    return [c for r in rows for c in r["codes"]], [{"d": r["_id"], "n": r["n"]} for r in rows]


def check_trend_range(date_from: str, date_to: str) -> tuple[Date, Date]:
    f, t = parse_day(date_from, "from"), parse_day(date_to, "to")
    if t < f:
        unprocessable("'to' must not be before 'from'", ("query", "to"))
    if (t - f).days + 1 > 92:
        unprocessable("the range may not exceed 92 days", ("query", "to"))
    return f, t


def trend_pipeline(codes: list, joined: list, f: Date, t: Date) -> list:
    f_s, t_s = f.isoformat(), t.isoformat()
    lower = datetime(f.year, f.month, f.day, tzinfo=UTC)
    upper = datetime(t.year, t.month, t.day, tzinfo=UTC) + DAY  # $densify upper bound is exclusive
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": f_s, "$lte": t_s}}},
        {"$group": {"_id": "$date",
                    "present_halves": {"$sum": {"$cond": [_IS_PRESENT, _PRESENT_HALVES, 0]}},
                    "late_count": _count_if(_IS_LATE)}},
       
        {"$unionWith": {"pipeline": [{"$documents": [{"_id": f_s, "present_halves": 0, "late_count": 0}]}]}},
        {"$group": {"_id": "$_id", "present_halves": {"$sum": "$present_halves"},
                    "late_count": {"$sum": "$late_count"}}},
        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lower, upper]}}},
        {"$addFields": {
            "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "present_halves": {"$ifNull": ["$present_halves", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
        }},
        {"$addFields": {"headcount": {"$reduce": {
            "input": {"$literal": joined}, "initialValue": 0,
            "in": {"$add": ["$$value", {"$cond": [{"$lte": ["$$this.d", "$date"]}, "$$this.n", 0]}]}}}}},
        {"$addFields": {"q": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [{"$add": [{"$multiply": ["$present_halves", 10000]}, "$headcount"]},
                                    {"$multiply": [2, "$headcount"]}]}},
            None]}}},
        {"$addFields": {"qn": {"$cond": [{"$eq": ["$q", None]}, 0, 1]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "q_sum": {"$sum": "$q", "window": {"documents": [-6, 0]}},
            "q_n": {"$sum": "$qn", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"day": 1}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "late_count": 1,
            "present_count": {"$divide": ["$present_halves", 2]},
            "attendance_rate": {"$cond": [{"$eq": ["$q", None]}, None, {"$divide": ["$q", 10000]}]},
            "moving_avg_7d": {"$cond": [{"$eq": ["$q_n", 0]}, None, {"$divide": [
                {"$floor": {"$divide": [{"$add": [{"$multiply": [2, "$q_sum"]}, "$q_n"]},
                                        {"$multiply": [2, "$q_n"]}]}}, 10000]}]},
        } } ,
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str,
                     from_: Annotated[str, Query(alias="from", pattern=DATE_RE)],
                     to: Annotated[str, Query(pattern=DATE_RE)]):
    f, t = check_trend_range(from_, to)
    codes, joined = trend_inputs(department)
    items = [
        {"date": r["date"], "is_working_day": bool(r["is_working_day"]), "headcount": int(r["headcount"]),
         "present_count": float(r["present_count"]), "late_count": int(r["late_count"]),
         "attendance_rate": r["attendance_rate"], "moving_avg_7d": r["moving_avg_7d"]}
        for r in logs_col().aggregate(trend_pipeline(codes, joined, f, t))
    ]
    return {"department": department, "items": items}



def _explain_json(command: dict) -> dict:
    result = db.command({"explain": command, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))


def _require(**params):
    missing = [k for k, v in params.items() if v is None]
    if missing:
        unprocessable("missing query parameter(s): " + ", ".join(missing), ("query", missing[0]))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from", pattern=DATE_RE),
    to: Optional[str] = Query(None, pattern=DATE_RE),
    page: Page = 1,
    page_size: PageSize = 20,
):
    
    if endpoint == "attendance_list":
        query, sort, skip, lim = build_attendance_find(emp_code, date_from, date_to, status, page, page_size)
        command = {"find": "attendance_logs", "filter": query, "sort": sort, "skip": skip, "limit": lim}
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        _require(emp_code=emp_code, month=month)
        first, last = month_bounds(month)
        command = {"aggregate": "attendance_logs", "pipeline": monthly_pipeline(emp_code, first, last), "cursor": {}}
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        _require(month=month)
        first, last = month_bounds(month)
        command = {"aggregate": "employees", "pipeline": summary_pipeline(first, last, department), "cursor": {}}
        collection = "employees"
    elif endpoint == "late_leaderboard":
        _require(month=month)
        first, last = month_bounds(month)
        command = {"aggregate": "attendance_logs",
                   "pipeline": leaderboard_pipeline(first, last, limit, department), "cursor": {}}
        collection = "attendance_logs"
    else:  # department_trend
        _require(department=department, **{"from": from_}, to=to)
        f, t = check_trend_range(from_, to)
        codes, joined = trend_inputs(department)
        command = {"aggregate": "attendance_logs", "pipeline": trend_pipeline(codes, joined, f, t), "cursor": {}}
        collection = "attendance_logs"
    return {"endpoint": endpoint, "collection": collection, "explain": _explain_json(command)}
