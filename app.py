"""
XYZSure — standalone AI Compliance application.

Extracted from XYZ-LIMS-v2 AI Compliance / AI Engine module.
"""

import logging
import os
from urllib.parse import urlsplit

from dotenv import load_dotenv

load_dotenv()

from flask import (  # noqa: E402
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import LoginManager, current_user, login_required, logout_user
from flask_session import Session  # noqa: E402

from database import db_session  # noqa: E402
from models.user_role import User  # noqa: E402
import models.general  # noqa: E402,F401 — register Client mapper for User.client

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-change-me")
app.config["PERMANENT_SESSION_LIFETIME"] = int(
    os.environ.get("FLASK_PERMANENT_SESSION_LIFETIME", "86400")
)
app.config["SESSION_PERMANENT"] = (
    str(os.environ.get("FLASK_SESSION_PERMANENT", "True")).lower()
    in ("1", "true", "yes")
)

# Behind reverse proxies (optional)
_proxy_hops = int(os.environ.get("TRUSTED_PROXY_HOPS", "0") or "0")
if _proxy_hops > 0:
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(
        app.wsgi_app, x_for=_proxy_hops, x_proto=_proxy_hops, x_host=_proxy_hops
    )

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
)
logger = logging.getLogger(__name__)
logging.getLogger("apps.Google_API").setLevel(logging.WARNING)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "index"

app.config["SESSION_TYPE"] = os.environ.get("SESSION_TYPE", "filesystem")
app.config["SESSION_FILE_DIR"] = os.environ.get(
    "SESSION_FILE_DIR", os.path.join(app.root_path, "flask_session")
)
app.config["SESSION_SERIALIZATION_FORMAT"] = "json"
os.makedirs(app.config["SESSION_FILE_DIR"], exist_ok=True)
Session(app)

# Blueprints — AI Compliance only
from apps.Admin_Panel import admin  # noqa: E402
from apps.Admin_Panel.ai_compliance import ai_compliance  # noqa: E402
from apps import Google_API  # noqa: E402
from apps.demo import DEMO_USER_ID, init_demo, load_demo_user  # noqa: E402

app.register_blueprint(admin)
app.register_blueprint(ai_compliance)
app.register_blueprint(Google_API.google_api)
init_demo(app)

# Probe external LIMS API + local ORM once at startup (disables UI checkbox if neither works).
try:
    from apps.lims_adapter import refresh_lims_availability

    _lims_status = refresh_lims_availability()
    logger.info(
        "Startup LIMS check: available=%s (api=%s, orm=%s)",
        _lims_status.get("available"),
        (_lims_status.get("api") or {}).get("available"),
        (_lims_status.get("orm") or {}).get("available"),
    )
except Exception as _lims_exc:
    logger.warning("Startup LIMS availability check failed: %s", _lims_exc)


@login_manager.user_loader
def load_user(user_id):
    if user_id == DEMO_USER_ID:
        return load_demo_user(user_id)
    for _ in range(2):
        try:
            return db_session.get(User, user_id)
        except Exception as exc:
            exc_text = str(exc).lower()
            if "operationalerror" not in exc.__class__.__name__.lower() and all(
                token not in exc_text
                for token in (
                    "ssl syscall error",
                    "bad record mac",
                    "server closed the connection",
                    "connection not open",
                )
            ):
                raise
            logger.warning("Transient DB error loading user; resetting session")
            db_session.remove()
    return None


@app.context_processor
def inject_helpers():
    def endpoint_exists(name: str) -> bool:
        try:
            return name in app.view_functions or any(
                rule.endpoint == name for rule in app.url_map.iter_rules()
            )
        except Exception:
            return False

    has_icons = False
    try:
        css = os.path.join(
            app.static_folder or "", "bootstrap-icons", "font", "bootstrap-icons.css"
        )
        has_icons = os.path.exists(css)
    except Exception:
        pass

    lims_available = False
    lims_hint = "LIMS source status unknown."
    try:
        from apps.lims_adapter import get_lims_availability

        status = get_lims_availability()
        lims_available = bool(status.get("available"))
        lims_hint = status.get("hint") or lims_hint
    except Exception:
        pass

    return {
        "endpoint_exists": endpoint_exists,
        "has_bootstrap_icons": has_icons,
        "lims_data_available": lims_available,
        "lims_data_source_hint": lims_hint,
    }


def _safe_next_url(candidate):
    if not candidate or not isinstance(candidate, str):
        return None
    next_url = candidate.strip()
    if not next_url.startswith("/") or next_url.startswith("//"):
        return None
    if any(ch in next_url for ch in ("\r", "\n", "\\")):
        return None
    return next_url


@app.route("/", methods=["GET", "HEAD"])
def index():
    next_url = _safe_next_url(request.args.get("next"))
    if next_url and current_user.is_authenticated:
        return redirect(next_url)
    if next_url and not current_user.is_authenticated:
        session["post_login_next"] = next_url

    if current_user.is_authenticated:
        dest = next_url or "/ai-compliance/"
        return redirect(dest)

    return render_template(
        "login.html",
        google_client_id=os.environ.get("GOOGLE_CLIENT_ID"),
    )


@app.route("/how-it-works", methods=["GET", "HEAD"])
def how_it_works():
    """Public marketing page — CTA destination for 'See how it works'."""
    return render_template("marketing.html")


@app.route("/logout", methods=["GET"])
@login_required
def logout():
    logout_user()
    next_url = _safe_next_url(request.args.get("next"))
    if next_url:
        return redirect(url_for("index", next=next_url))
    return redirect(url_for("index"))


@app.teardown_appcontext
def shutdown_session(exception=None):
    db_session.remove()


@app.before_request
def check_load_creds():
    if request.endpoint is None:
        return

    try:
        user_is_authenticated = current_user.is_authenticated
    except Exception as exc:
        exc_text = str(exc).lower()
        transient = "operationalerror" in exc.__class__.__name__.lower() or any(
            t in exc_text
            for t in (
                "ssl syscall error",
                "bad record mac",
                "server closed the connection",
                "connection not open",
                "psycopg2",
            )
        )
        if not transient:
            raise
        logger.exception("Database connection lost while checking auth")
        db_session.remove()
        if request.method == "GET":
            flash("Database connection was reset. Please try again.", "error")
            return redirect(url_for("index"))
        abort(503, description="Temporary database connection issue. Please retry.")

    public = {
        "index",
        "how_it_works",
        "static",
        "google_api.login_callback",
        "demo.enter",
        "demo.enter_with_form",
        "demo.exit_demo",
    }

    if user_is_authenticated:
        if getattr(current_user, "is_demo", False):
            return
        if request.endpoint not in (
            "google_api.authorize",
            "google_api.authorize_callback",
            "logout",
        ):
            if "client" not in getattr(current_user, "roles_list", []):
                if not current_user.google_credentials:
                    if request.method == "GET":
                        return redirect(url_for("google_api.authorize"))
                    abort(403, description="User has no Google credentials")
        return

    if request.endpoint in public:
        return

    if request.method == "GET":
        flash("Please Login to access that page", "error")
        path = urlsplit(request.full_path).path or request.path
        next_url = _safe_next_url(path.rstrip("?") or request.path)
        if next_url in ("/",):
            return redirect(url_for("index"))
        return redirect(url_for("index", next=next_url))

    flash("Please Login to access that page", "error")
    abort(401)


@app.errorhandler(404)
def not_found(e):
    return render_template("login.html", google_client_id=os.environ.get("GOOGLE_CLIENT_ID")), 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8090"))
    app.run(host="0.0.0.0", port=port, debug=True)
