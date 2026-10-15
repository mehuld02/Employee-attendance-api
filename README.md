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



