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

CSV_FILE = Path(__file__).parent / "data" / "users.csv"


@app.route("/")
def frontend():
    logging.info("Angular frontend accessed")
    return app.send_static_file("index.html")


@app.route("/api/users")
def users():
    logging.info("Reading users from CSV")

    with open(CSV_FILE, newline="", encoding="utf-8") as file:
        data = list(csv.DictReader(file))

    return jsonify(data)


@app.route("/api/error/frontend", methods=["POST"])
def frontend_error():
    data = request.get_json(silent=True) or {}

    message = data.get("message", "Unknown frontend error")
    stack = data.get("stack", "No frontend stack trace")

    logging.error(
        "FRONTEND_ERROR: %s | STACK: %s",
        message,
        stack
    )

    return jsonify({"status": "frontend error logged"}), 500


@app.route("/api/error/backend")
def backend_error():
    try:
        number = 10
        zero = 0
        result = number / zero
        return str(result)

    except Exception:
        logging.exception(
            "BACKEND_ERROR: Backend calculation failed"
        )
        return jsonify({"error": "Backend error generated"}), 500


@app.route("/api/error/database")
def database_error():
    try:
        with open(CSV_FILE, newline="", encoding="utf-8") as file:
            data = list(csv.DictReader(file))

        phone = data[0]["phone"]

        return jsonify({"phone": phone})

    except Exception:
        logging.exception(
            "DATABASE_ERROR: CSV database schema error in users.csv"
        )
        return jsonify({"error": "Database/CSV error generated"}), 500


@app.route("/health")
def health():
    return "OK"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
