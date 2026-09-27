"""Slim Admin blueprint — hosts AI Compliance engine routes only."""

import logging
import os

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user

logger = logging.getLogger(__name__)

admin = Blueprint(
    "admin",
    __name__,
    template_folder="ai_engine/templates",
    url_prefix="/admin",
)


@admin.context_processor
def inject_bootstrap_icons_flag():
    try:
        static_folder = current_app.static_folder
        if not static_folder:
            return {"has_bootstrap_icons": False}
        css_path = os.path.join(
            static_folder, "bootstrap-icons", "font", "bootstrap-icons.css"
        )
        return {"has_bootstrap_icons": os.path.exists(css_path)}
    except Exception:
        return {"has_bootstrap_icons": False}


from .ai_engine import ai_engine  # noqa: E402

admin.register_blueprint(ai_engine)


@admin.before_request
def restrict_access():
    allowed_for_authenticated = (
        "admin.ai_engine.ai_search_reports",
        "admin.ai_engine.delete_ai_report",
        "admin.ai_engine.ai_gap_analysis_reports",
        "admin.ai_engine.delete_gap_analysis_report",
        "admin.ai_engine.export_all_reports_pdf",
    )

    if request.endpoint in allowed_for_authenticated:
        if not getattr(current_user, "is_authenticated", False):
            flash("Please log in to perform this action", "error")
            return redirect(url_for("index"))
        return

    if not getattr(current_user, "is_authenticated", False):
        flash("Please Login to access that page", "error")
        return redirect(url_for("index", next="/ai-compliance/"))

    if "admin" not in current_user.roles_list:
        flash("User not authorized to access AI Compliance", "error")
        return redirect(url_for("index"))


@admin.route("/", methods=["GET"])
def index():
    return redirect(url_for("ai_compliance.home"))


@admin.route("/ai-search-results", methods=["GET"])
def ai_search_results():
    return render_template("admin.ai_search_results.html")


@admin.route("/ai-search-result", methods=["GET"])
def ai_search_result_single():
    return render_template("admin.ai_search_result_single.html")
