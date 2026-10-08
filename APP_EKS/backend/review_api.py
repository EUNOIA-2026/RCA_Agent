import logging

from flask import Blueprint, current_app, jsonify, request

if __package__:
    from .review import (
        analyze_review,
        load_sample_review,
        validate_review_payload,
    )
else:
    from review import (
        analyze_review,
        load_sample_review,
        validate_review_payload,
    )


review_api = Blueprint("review_api", __name__)


@review_api.get("/api/reviews/sample")
def get_sample_review():
    try:
        return jsonify(load_sample_review())
    except OSError:
        logging.exception("REVIEW_ERROR: Unable to read sample Jira ticket")
        return jsonify({"error": "Unable to load the sample Jira review"}), 500


@review_api.post("/api/reviews")
def review_changes():
    if (
        request.content_length is not None
        and request.content_length > 2 * 1024 * 1024
    ):
        return jsonify({"error": "Review request must not exceed 2 MiB"}), 413
    try:
        ticket, diff_text, changed_files = validate_review_payload(
            request.get_json(silent=True)
        )
        return jsonify(analyze_review(ticket, diff_text, changed_files))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except Exception:
        current_app.logger.exception("REVIEW_ERROR: Jira change review failed")
        return jsonify({"error": "Unable to review the supplied changes"}), 500
