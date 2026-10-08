# Student Record Manager — Technical Documentation

**Document type:** Technical reference  
**Prepared:** 2026-10-08  
**Application:** Student Record Manager

## 1. System overview

The project is a single-page web application backed by a Flask JSON API. Angular renders the browser UI. Flask serves the static frontend bundle and handles API calls. The current persistence layer is a CSV file on local disk.

```text
Browser (Angular SPA)
        | same-origin HTTP / JSON
        v
Root Flask entry point: app.py (port 5000)
        | read / append / rewrite
        v
backend/data/students.csv
```

The root-level `app.py` is the entry point used by the supplied Dockerfile. `backend/app.py` is a second, similar Flask implementation with different static-asset and CSV paths; the Dockerfile does not use it. For the repository's root-level run and container workflow, use `app.py` from the project root.

## 2. Repository layout

| Path | Responsibility |
|---|---|
| `app.py` | Primary Flask app; API routes, CSV access, static frontend hosting, port 5000. |
| `backend/app.py` | Alternate/duplicate Flask entry point; uses `backend/data/students.csv` and `backend/frontend-dist`. |
| `backend/requirements.txt` | Pinned Python dependencies: Flask 3.1.0 and flask-cors 5.0.0. |
| `backend/data/students.csv` | CSV data store and sample student records. |
| `frontend/src/app/app.ts` | Angular standalone component, state, and HTTP calls. |
| `frontend/src/app/app.html` | Main UI template. |
| `frontend/src/app/app.css` | Component styling. |
| `frontend/src/main.ts` | Angular bootstrap. |
| `frontend/package.json` / `frontend/package-lock.json` | Frontend scripts and dependency manifest/lock. |
| `frontend/angular.json` | Angular build, serve, and test configuration. |
| `frontend/dist/frontend/browser` | Default Angular production build output in the current configuration. |
| `build/frontend` | Static directory expected by root `app.py` and the Dockerfile. |
| `Dockerfile` | Packages the root Flask app, CSV, and prebuilt static assets. |

There are no Kubernetes deployment/service manifests in the current project tree. `kubectl` alone does not create or configure a cluster.

## 3. Technology and runtime requirements

- **Backend:** Python; Flask 3.1.0; flask-cors 5.0.0.
- **Frontend:** Angular 22.2.x; TypeScript 6.0.x; RxJS 7.8.x.
- **Node.js:** use Node.js 22.22.3 or another version supported by the installed Angular 22 CLI. The validated local build used Node.js 22.22.3 and npm 10.9.8.
- **Container:** Docker Desktop/Engine. The Dockerfile base image is `python:3.12-slim`.
- **Kubernetes (optional deployment target):** a reachable cluster, image registry, and Kubernetes manifests are additional requirements; none are supplied here.

## 4. Local installation and run

From PowerShell, at the repository root:

```powershell
python -m pip install -r backend\requirements.txt
cd frontend
npm ci
npm run build
cd ..
New-Item -ItemType Directory -Force build\frontend | Out-Null
Copy-Item frontend\dist\frontend\browser\* build\frontend -Recurse -Force
python app.py
```

Then open `http://localhost:5000`. The root Flask app serves `build/frontend/index.html` and the API. The frontend build output is copied into the location expected by the primary app and Dockerfile.

For frontend development, `npm start` serves Angular separately (normally on port 4200). The project currently has no Angular proxy configuration, while API calls use relative `/api/...` URLs. Therefore, the development server does not automatically forward those requests to Flask; use the Flask-served built app for integrated local testing or add/configure a dev proxy if live reload against the API is required.

Frontend scripts declared in `package.json` include `npm start`, `npm run build`, `npm run watch`, and `npm test`. Backend requirements do not declare a test runner or backend test command.

## 5. HTTP API

All API paths are served by the root Flask app. JSON request/response examples below omit routine headers.

| Method and path | Purpose | Success response | Common errors |
|---|---|---|---|
| `GET /health` | Basic process health check. | `200`, plain text `OK`. | — |
| `GET /api/students` | List non-empty CSV rows. | `200`, JSON array of `{id,name,age,grade}` records. | `500` `{ "error": "Unable to read student records" }`. |
| `POST /api/students` | Validate and append a record. | `201` `{ "message": "Student added successfully", "id": 6 }`. | `400` for missing name, invalid age, or unsupported grade; `500` on unexpected failure. |
| `GET /api/students/{student_id}` | Retrieve one record by integer ID. | `200`, JSON student object. | `404` `{ "error": "Student not found", "student": null }`; `500` on read/lookup failure. |
| `DELETE /api/students/{student_id}` | Remove one record and rewrite the CSV. | `200` `{ "message": "Student deleted successfully" }`. | `404` if absent; `500` on failure. |
| `GET /api/summary` | Return record count and average age. | `200` `{ "count": 5, "average_age": 21.0 }`. | `500` for malformed data or an empty list (division by zero). |
| `POST /api/error/frontend` | Write a frontend error report to application logs. | `202` `{ "status": "frontend error logged" }`. | No explicit request validation is implemented. |

`POST /api/students` expects a JSON object with `name`, `age`, and `grade`. Age is passed through Python `int()`, then range-checked for 1–120. Fractional JSON numbers are truncated by this conversion; nonnumeric values raise an exception that the broad route handler returns as HTTP 500. Grade is trimmed, uppercased, and checked against `A/A+/C/D/F`. Responses for individual students preserve CSV values as strings.

The frontend error endpoint accepts `message`, `stack`, `context`, and `occurredAt`; the root app also accepts the aliases `endpoint` and `timestamp` for context and time. These values are written to the server log.

## 6. Data layer

The root Flask app resolves the data file relative to its source:

```text
<repository-root>/backend/data/students.csv
```

Required header fields are `id`, `name`, `age`, and `grade`. CSV reads validate that all four columns exist and ignore rows whose values are all blank. Adding computes `max(int(id)) + 1`; deletion reads all records then rewrites the file with the canonical header.

CSV storage is suitable only for small, single-process demonstrations. The application does not implement file locks, atomic transactions, concurrent-write protection, schema migrations, or database backups. Concurrent requests can race when assigning IDs or rewriting the file.

## 7. Logging, errors, and operational behavior

Logging is configured at INFO level with timestamp, severity, and message. CSV loading errors are logged with a `DATABASE_ERROR` prefix. Route failures are generally returned as JSON and logged where the route explicitly calls `logging.exception`. Frontend error reports are logged at ERROR level.

The app starts with Flask's built-in server at `0.0.0.0:5000`; no production WSGI server is configured. CORS is enabled globally with the default `flask-cors` configuration. There is no authentication or authorization layer. Review these defaults before exposing the app to untrusted networks.

The `/api/summary` empty-list division by zero is an intentional RCA demonstration defect in the current source, not an undocumented runtime guarantee.

## 8. Docker packaging

The root `Dockerfile`:

1. Starts from `python:3.12-slim`.
2. Installs `backend/requirements.txt`.
3. Copies root `app.py`, `backend/data`, and `build/frontend` into `/app`.
4. Exposes port 5000 and runs `python app.py`.

Build from the repository root after generating and copying the frontend bundle as shown above:

```powershell
docker build -t student-record-manager .
```

Run with a bind mount if CSV changes should persist outside the container:

```powershell
$dataPath = Join-Path (Get-Location) 'backend\data'
docker run --rm -p 5000:5000 --mount "type=bind,source=$dataPath,target=/app/backend/data" student-record-manager
```

Without a persistent mount, changes made to the container's CSV are lost when the container is removed. Docker Desktop on Windows requires its container backend to be operational; WSL 2 setup may be required. An installed Docker CLI or Docker Desktop application by itself does not prove the engine is running.

## 9. Kubernetes readiness

This repository does not currently define a Deployment, Service, ConfigMap, Secret, or persistent volume. A Kubernetes deployment would additionally need a built/published container image, cluster credentials/context, manifests, and a storage decision for the writable CSV. A local `kubectl` client is only a command-line interface to an existing cluster; it does not install a cluster or deploy this application by itself.

## 10. Known implementation constraints

- The static directory is hard-coded as `build/frontend` in root `app.py` and the Dockerfile; Angular's configured output is `frontend/dist/frontend/browser`, so copying/build integration is a separate step.
- `backend/app.py` has a different static directory and is not the Docker entry point.
- The Angular dev server has no API proxy.
- CSV persistence has no concurrency safety and ID values may be reused after deletion of the maximum ID.
- Empty class summaries return HTTP 500.
- There are no Kubernetes manifests, production WSGI server, authentication, or documented automated backend tests in the repository.

