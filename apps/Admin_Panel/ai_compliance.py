"""Standalone AI Compliance shell: branded entry at /ai-compliance."""

import os

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    url_for,
)
from flask_login import current_user

ai_compliance = Blueprint(
    "ai_compliance",
    __name__,
    template_folder="ai_engine/templates",
    url_prefix="/ai-compliance",
)


@ai_compliance.context_processor
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


@ai_compliance.before_request
def restrict_access():
    if not getattr(current_user, "is_authenticated", False):
        flash("Please Login to access that page", "error")
        return redirect(url_for("index", next="/ai-compliance/"))
    if "admin" not in current_user.roles_list:
        flash("User not authorized to access AI Compliance", "error")
        return redirect(url_for("index"))


@ai_compliance.route("/", methods=["GET"])
def home():
    return render_template("ai_compliance.home.html")


@ai_compliance.route("/help", methods=["GET"])
def help_page():
    return render_template("ai_compliance.help.html")
