from flask import Flask, jsonify, request
from flask_cors import CORS
import csv
import logging
from pathlib import Path

app = Flask(__name__, static_folder="frontend-dist", static_url_path="")
CORS(app)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

CSV_FILE = Path(__file__).parent / "data" / "students.csv"


def read_students():
    try:
        if not CSV_FILE.exists():
            raise FileNotFoundError(f"CSV file not found: {CSV_FILE}")

        with open(CSV_FILE, newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)

            required = {"id", "name", "age", "grade"}
            actual = set(reader.fieldnames or [])
            missing = required - actual

            if missing:
                raise ValueError(
                    f"Missing required CSV columns: {sorted(missing)}"
                )

            return [
                row for row in reader
                if any((value or "").strip() for value in row.values())
            ]

    except Exception:
        logging.exception(
            "DATABASE_ERROR: Unable to read students.csv"
        )
        raise


@app.route("/")
def frontend():
    logging.info("Student Record Manager frontend accessed")
    return app.send_static_file("index.html")


@app.route("/health")
def health():
    return "OK"


@app.route("/api/students", methods=["GET"])
def get_students():
    try:
        students = read_students()
        logging.info(
            "Students loaded successfully: %d records",
            len(students)
        )
        return jsonify(students)

    except Exception:
        return jsonify({
            "error": "Unable to read student records"
        }), 500


@app.route("/api/students", methods=["POST"])
def add_student():
    try:
        data = request.get_json(silent=True) or {}

        name = str(data.get("name", "")).strip()
        age = int(data.get("age", 0))
        grade = str(data.get("grade", "")).strip().upper()

        if not name:
            return jsonify({"error": "Name is required"}), 400

        if age < 1 or age > 125:
            return jsonify({"error": "Age must be between 1 and 125"}), 400

        if grade not in {"A", "B", "C", "D", "F"}:
            return jsonify({
                "error": "Grade must be A, B, C, D or F"
            }), 400

        students = read_students()

        next_id = max(
            [int(student["id"]) for student in students],
            default=0
        ) + 1

        with open(
            CSV_FILE,
            "a",
            newline="",
            encoding="utf-8"
        ) as file:
            writer = csv.writer(file)
            writer.writerow([next_id, name, age, grade])

        logging.info(
            "Student added successfully: id=%s name=%s",
            next_id,
            name
        )

        return jsonify({
            "message": "Student added successfully",
            "id": next_id
        }), 201

    except Exception:
        logging.exception(
            "BACKEND_ERROR: Failed while adding student"
        )
        return jsonify({
            "error": "Backend failed while adding student"
        }), 500


@app.route("/api/students/<int:student_id>", methods=["GET"])
def get_student(student_id):
    try:
        students = read_students()

        for student in students:
            if int(student["id"]) == student_id:
                return jsonify(student)

        return jsonify({
            "error": "Student not found",
            "student": None
        }), 404

    except Exception:
        logging.exception(
            "BACKEND_ERROR: Student lookup failed"
        )
        return jsonify({
            "error": "Backend failed during student lookup"
        }), 500


@app.route("/api/students/<int:student_id>", methods=["DELETE"])
def delete_student(student_id):
    try:
        students = read_students()

        remaining = [
            student for student in students
            if int(student["id"]) != student_id
        ]

        if len(remaining) == len(students):
            return jsonify({
                "error": "Student not found"
            }), 404

        with open(
            CSV_FILE,
            "w",
            newline="",
            encoding="utf-8"
        ) as file:
            writer = csv.DictWriter(
                file,
                fieldnames=["id", "name", "age", "grade"]
            )
            writer.writeheader()
            writer.writerows(remaining)

        logging.info(
            "Student deleted successfully: id=%s",
            student_id
        )

        return jsonify({
            "message": "Student deleted successfully"
        })

    except Exception:
        logging.exception(
            "BACKEND_ERROR: Failed while deleting student"
        )
        return jsonify({
            "error": "Backend failed while deleting student"
        }), 500


@app.route("/api/summary")
def summary():
    try:
        students = read_students()

        total_age = sum(int(student["age"]) for student in students)

        # Intentional application defect for RCA demonstration:
        # if there are zero students, this causes ZeroDivisionError.
        average_age = total_age / len(students)

        return jsonify({
            "count": len(students),
            "average_age": round(average_age, 2)
        })

    except Exception:
        logging.exception(
            "BACKEND_ERROR: Student summary calculation failed"
        )
        return jsonify({
            "error": "Backend failed while generating summary"
        }), 500


@app.route("/api/error/frontend", methods=["POST"])
def frontend_error():
    data = request.get_json(silent=True) or {}

    message = data.get(
        "message",
        "Unknown frontend error"
    )
    stack = data.get(
        "stack",
        "No frontend stack trace"
    )
    context = data.get(
        "context",
        "No frontend context"
    )
    occurred_at = data.get(
        "occurredAt",
        "Unknown timestamp"
    )

    logging.error(
        "FRONTEND_ERROR: %s | CONTEXT: %s | OCCURRED_AT: %s | STACK: %s",
        message,
        context,
        occurred_at,
        stack
    )

    return jsonify({
        "status": "frontend error logged"
    }), 202


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
