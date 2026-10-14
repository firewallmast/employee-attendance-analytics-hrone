# Employee Attendance & Analytics API

This project is built using Python, FastAPI and MongoDB to manage employee attendance and generate some useful attendance reports.

It handles employee records, punch-in and punch-out, attendance corrections and keeps a history of the changes made. It also provides monthly reports, department summaries, late employee rankings and department attendance trends.

## Features

- Add and view employee details
- Record employee punch-in and punch-out
- View attendance records with filters and pagination
- Correct attendance details and keep track of changes
- Generate monthly attendance reports
- Get department-wise attendance summaries
- Find employees with the highest late minutes
- View department attendance trends
- Check MongoDB query execution plans

## Requirements

- Python 3.11 or above
- MongoDB 6.0 or above
- Packages mentioned in `requirements.txt`

## Setup

First, install the required packages from the project folder:

```bash
pip install -r requirements.txt
```

Make sure MongoDB is running before starting the application.

The application uses `MONGO_URI` and `MONGO_DB` for database configuration. If they are not set, it uses the local MongoDB server and the database name `attendance_db`.

To load the sample data, run:

```powershell
py sample_seed.py
```

This script resets the sample collections before loading the data, so use it only when you want to reset them.

## Running the Application

From the project root folder, run:

```powershell
py -m uvicorn app.main:app --port 8000
```

After the server starts, open this link in your browser to test the APIs:

http://localhost:8000/docs

## Project Files

- `app/main.py` - contains the API code
- `requirements.txt` - required Python packages
- `openapi.yaml` - API contract and rules
- `DATA_MODEL.md` - database structure
- `REVIEW.md` - review of the starter code
- `DECISIONS.md` - engineering decisions
- `sample_data/` - sample attendance and employee data
- `sample_seed.py` - script to load sample data

## Security

Environment files, database passwords and other secrets should not be uploaded to the public repository.