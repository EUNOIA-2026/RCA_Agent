# Student Record Manager — Functional Documentation

**Document type:** Functional guide  
**Prepared:** 2026-10-08  
**Application:** Student Record Manager

## 1. Purpose and scope

Student Record Manager is a small web application for maintaining a class list. A user can view records, add a student, look up a student by ID, delete a record, and view a class count and average age. The interface is built with Angular; a Flask service exposes the API and stores records in a CSV file.

This document describes the functionality currently implemented in the repository. There is no edit/update-student feature, authentication, or role-based access control.

## 2. User interface

The single-page interface contains:

- **Add Student** — name, age, and grade inputs with an Add Student action.
- **Student Records** — table of IDs, names, ages, and grades; Refresh reloads the list; Delete removes a row.
- **Search Student** — accepts an ID and displays a success or not-found message in the status area.
- **Class Summary** — View Summary displays the record count and average age.
- **Jira Change Review** — compares ticket acceptance criteria with a pasted unified diff or selected changed files, then reports provisional met, partially met, or missing statuses with changed-line evidence.
- **Status** — reports the latest successful operation or a user-facing error message.

The records are loaded automatically when the page starts. The UI uses same-origin paths such as `/api/students`; the Angular development server does not have an API proxy configured.

## 3. Typical user workflows

### View and refresh records

Open the application page. The current student list is loaded automatically. Select **Refresh** to fetch it again. If there are no rows, the interface shows “No student records available.”

### Add a student

Enter a name, age, and grade, then select **Add Student**. The server trims the name, normalizes the grade to uppercase, validates the values, assigns an ID, and appends the record to the CSV file. On success, the form is reset and the list is reloaded.

Validation rules intended by the API:

- Name must not be blank.
- Age must be an integer from 1 through 120, inclusive.
- Grade must be one of `A`, `A+`, `C`, `D`, or `F` (case-insensitive on input).

The API intends to accept integer ages from 1 through 120. Its current `int()` conversion truncates fractional JSON numbers and nonnumeric age values fall through to a generic HTTP 500 instead of a validation-specific 400. Supply an integer age. For other rejected values, the API returns HTTP 400 with a JSON `error` message. The form performs basic required-field checks, while the server remains authoritative for validation.

### Find a student

Enter a student ID and select **Search**. A matching record produces a status message containing the student's name and grade. An empty ID prompts the user to enter an ID; an unknown ID produces a not-found message.

### Delete a student

Select **Delete** on a row. The server removes that ID from the CSV and the interface reloads the list. If the ID no longer exists, the API responds with a not-found error.

### View the class summary

Select **View Summary**. The response contains the number of students and the arithmetic mean of their ages, rounded to two decimal places. The current implementation returns an error when the list is empty; see Known behavior below.

### Review implementation changes against a Jira ticket

In **Jira Change Review**, paste the Jira ticket (including an **Acceptance Criteria** section) and either paste a unified diff or select the changed source files from the local machine. **Load sample ticket and diff** loads the demonstration fixture in `tests/jira/`. Select **Review changes** to see each parsed criterion labeled met, partially met, or missing, along with keyword coverage and matching file/line evidence from added diff lines or selected source files. The result also includes a documentation-update draft listing the provisional assessment and evidence. Copy or adapt it only after verifying the implementation and tests; the app does not modify maintained documentation files.

This is a local keyword-coverage heuristic, not an AI or semantic code reviewer. A keyword match does not establish that the behavior works, and a missing keyword does not prove the requirement is absent. It does not execute tests or update documents automatically; findings must be manually verified, and documentation updates remain a developer task.

Notes on coverage vs. repository behavior:

- The review feature is purely a keyword-coverage heuristic implemented in `backend/review.py` and exposed via the blueprint in `backend/review_api.py` (GET `/api/reviews/sample` and POST `/api/reviews`). It returns provisional statuses (`met`, `partially_met`, `missing`), a numeric keyword coverage, up to three file/line evidence entries, and a generated `documentation_draft` string. The frontend UI exposes the same functionality in the "Jira Change Review" section of the main page.

- Important implementation gap discovered: the bundled sample Jira ticket `tests/jira/STU-104.md` includes an Acceptance Criterion that reads "Every result includes a verified email address." The Student Record Manager's persisted student record model and the search API do not include email fields or email-verification behavior. Therefore, criterion is not implemented by the application and cannot be satisfied by the repository changes currently present. The review heuristic can only match keywords like "email" in changed files; it cannot implement or verify email delivery/verification or add the required data model.

- Action items for maintainers:
  - If the project intends to satisfy acceptance criteria that require additional data (for example, storing and verifying student email addresses), implement the data model, API fields, UI inputs, and verification workflow and update the functional and technical documentation accordingly.
  - Use the review results as triage hints only. For any criterion that is marked `met` or `partially_met`, confirm the behavior by inspecting the code paths and running tests or manual checks.


## 4. Data and persistence

Student records are stored in `backend/data/students.csv` with the header:

```csv
id,name,age,grade
```

The repository includes five sample records. Reads return CSV fields as strings. New IDs are calculated as one greater than the highest ID currently in the file (or 1 when the file has no records). As a result, deleting the highest-numbered record can allow that ID to be reused later.

In a local run, changes are written to the working CSV file. In a container run, the CSV is part of the image unless the data directory is mounted as persistent storage.

## 5. User-visible errors and known behavior

- The API returns JSON error messages for invalid input, missing students, and backend failures. The interface displays these messages for ordinary API responses.
- Network failures are not handled uniformly by every UI action. In particular, the search action reports caught failures to the frontend-error logging endpoint; other actions may surface an unhandled browser error.
- **Empty class summary:** the current backend divides by the record count without a zero-record check. With an empty CSV, `/api/summary` raises a division-by-zero error and returns HTTP 500. This is an intentional defect retained for root-cause-analysis demonstration.
- There is no confirmation dialog before deletion and no edit operation.

## 6. Availability

When run from the repository root, the Flask app listens on `http://localhost:5000`. `/health` returns plain-text `OK`. The root page is served from the prebuilt frontend files in `build/frontend`; the frontend build must be copied there before packaging or serving updated UI assets.

See the [Technical Documentation](technical-documentation.md) for API contracts, setup, architecture, and deployment details.
