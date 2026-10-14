# Employee Attendance & Analytics API

A REST API built with **FastAPI and MongoDB** to manage employee attendance, handle manual corrections, and generate attendance analytics.

All application code is in `app/main.py`.

## Tech Stack

- **Python 3.11+** – Application logic
- **FastAPI** – REST API development
- **MongoDB** – Employee and attendance data storage
- **PyMongo** – Database operations

## Getting Started

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure MongoDB

Make sure MongoDB 6.0 or later is running locally, then set the connection details:

```bash
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
```

You can also configure these variables in a local `.env` file. Actual environment variables take precedence.

### 3. Start the API

```bash
uvicorn app.main:app --port 8000
```

Open `http://localhost:8000/docs` to explore and test the API using Swagger UI.

## Key Implementation Details

- **Attendance management:** Supports punch-in, punch-out, and manual attendance corrections.
- **Data integrity:** Unique indexes prevent duplicate employee codes and multiple attendance records for the same employee on a single date.
- **Concurrent requests:** Database constraints handle simultaneous punch-ins, while optimistic concurrency control prevents conflicting PATCH updates.
- **Audit trail:** Manual corrections retain a history of changes.
- **Analytics:** MongoDB aggregation pipelines power attendance reports, department summaries, and leaderboard calculations.
- **Consistent calculations:** Analytics use stored derived values, with integer-based calculations for half-up rounding. `moving_avg_7d` is the half-up mean of the already rounded (4-decimal) `attendance_rate` values in the window.
- **Query analysis:** Explain endpoints help inspect the database queries used by the API.

## Assumptions and Edge Cases

- Punch times are stored as UTC datetimes truncated to whole seconds and exposed as epoch milliseconds.
- Employees who joined after the end of a reporting month are excluded from that month's headcount. Employees with no attendance records are still counted.
- Presence counts include every record with a presence status, even if its date is before the employee's joining date.
- A PATCH that sends `punch_in`/`punch_out` together with status `ABSENT` or `LEAVE` is rejected with `422`. Setting `ABSENT` or `LEAVE` on its own clears both punch times.
- PATCH requests that make no actual changes are rejected with `422`. Conflicting concurrent updates return `409 Conflict`.

## Requirements

- Python 3.11 or later
- MongoDB 6.0 or later, for the aggregation operators used by the analytics pipelines

Database indexes are created automatically when the application starts, and index creation is safe to repeat.

