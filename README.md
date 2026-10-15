# Employee Attendance & Analytics API

FastAPI + PyMongo + MongoDB API for employee records, attendance punch-in/out, manual corrections with audit history, and monthly/departments analytics.

## Requirements
- Python 3.11+
- MongoDB 6.0+ 

## Local setup (Windows CMD)
```bat
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
create .env
```
Edit `.env` with your own `MONGO_URI` and `MONGO_DB`.

Load the sample dataset into a development database:
```bat
python sample_seed.py
```

Run the API:
```bat
uvicorn app.main:app --port 8000 --reload
```
Open `http://127.0.0.1:8000/docs` for interactive API docs. `GET /health` checks MongoDB connectivity. Required indexes are created on application startup.

## Important notes
- All instants in request/response bodies are integer epoch milliseconds. MongoDB stores them as UTC BSON datetimes.
- Employee identity is `emp_code`; MongoDB `_id` is internal and not returned.
- Analytics are implemented as MongoDB aggregation pipelines. The trend endpoint builds a calendar series in the pipeline.
- Run against a dedicated development database before using `sample_seed.py`, because it deletes existing `employees` and `attendance_logs` documents.


