# Employee Attendance & Analytics API

A REST API built with **FastAPI** and **MongoDB** for employee management, attendance tracking, manual attendance corrections, and analytics.

## Features

- Create and list employees with pagination.
- Record employee punch-in and punch-out events.
- Support overnight shifts and Indian Standard Time (IST) attendance dates.
- Correct attendance records with an audit history.
- Calculate monthly employee attendance statistics.
- Generate department summaries, late-arrival leaderboards, and daily department trends.
- Inspect MongoDB query plans through explain endpoints.
- Create database indexes automatically at application startup.

## Technology Stack

- Python 3.11+
- FastAPI
- PyMongo
- MongoDB 6.0+
- Pydantic
- Uvicorn

## Configuration

Set these environment variables before starting the application:

- `MONGO_URI`: MongoDB connection URI.
- `MONGO_DB`: Database name (defaults to `attendance_db`).

Keep credentials in a local `.env` file or environment variables. Never commit secrets to Git.

## Installation

Create and activate a virtual environment, then install dependencies:

```bash
python -m venv .venv
```

On Windows:

```powershell
.venv\Scripts\activate
```

Install the requirements:

```bash
pip install -r requirements.txt
```

Configure `MONGO_URI` and `MONGO_DB` in your environment or local `.env` file.

## Run the API

From the `candidate_kit` directory:

```bash
python -m uvicorn app.main:app --reload --port 8000
```

Open the interactive API documentation at:

`http://127.0.0.1:8000/docs`

Health check:

`http://127.0.0.1:8000/health`

## API Endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/health` | Check API and database connectivity |
| POST | `/employees` | Create an employee |
| GET | `/employees` | List employees |
| POST | `/attendance/punch-in` | Record punch-in |
| POST | `/attendance/punch-out` | Record punch-out |
| GET | `/attendance` | List attendance records |
| PATCH | `/attendance/{emp_code}/{attendance_day}` | Correct attendance and record history |
| GET | `/analytics/employees/{emp_code}/monthly` | Monthly employee statistics |
| GET | `/analytics/departments/summary` | Department analytics |
| GET | `/analytics/leaderboard/late` | Late-arrival leaderboard |
| GET | `/analytics/departments/{department}/trend` | Daily department attendance trends |
| GET | `/admin/explain/{endpoint}` | Inspect supported MongoDB query plans |

## Design Notes

- Employee codes and employee/date attendance combinations use unique indexes.
- Attendance timestamps are normalized to UTC internally and exposed as epoch milliseconds.
- Attendance dates and shift boundaries follow IST rules.
- Attendance analytics use stored attendance fields and MongoDB aggregation where appropriate.
- Manual corrections append audit history entries.
- Pagination is one-based, with a maximum page size of 100.

## Testing

Use Swagger UI at `/docs` to exercise the endpoints. Test normal operations, duplicate requests, invalid dates, missing employees, overnight shifts, and analytics with missing attendance records before deployment.