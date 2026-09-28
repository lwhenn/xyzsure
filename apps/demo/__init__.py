"""Gated, read-only demo mode.

Prospects open ``/demo/<access-code>`` (codes come from ``DEMO_ACCESS_CODES``) and
get a temporary session as a synthetic "Demo Visitor". While that session is
active every AI Compliance endpoint is answered from ``sample_data`` (a fictional
laboratory): no Google, Discovery Engine, LLM, LIMS, or database calls are made,
and nothing is written to disk. Endpoints not explicitly allowed are refused.
"""

import hmac
import logging
import os
import time

from flask import (
    Blueprint,
    abort,
    flash,
    has_request_context,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import UserMixin, current_user, login_user, logout_user

from . import sample_data

logger = logging.getLogger(__name__)

DEMO_USER_ID = "demo-visitor"
_SESSION_STARTED = "demo_started_at"
_SESSION_CODE = "demo_code"

demo = Blueprint("demo", __name__, template_folder="templates", url_prefix="/demo")


def _access_codes():
    raw = os.environ.get("DEMO_ACCESS_CODES", "")
    return [c.strip() for c in raw.split(",") if c.strip()]


def _session_seconds():
    try:
        return max(0.25, float(os.environ.get("DEMO_SESSION_HOURS", "8"))) * 3600
    except ValueError:
        return 8 * 3600


def _matching_code(candidate):
    candidate = (candidate or "").encode()
    for code in _access_codes():
        if hmac.compare_digest(candidate, code.encode()):
            return code
    return None


class DemoUser(UserMixin):
    id = DEMO_USER_ID
    name = "Demo Visitor"
    username = "Demo Visitor"
    display_name = "Demo Visitor"
    email = "demo@xyzsure.com"
    roles_list = ["admin"]
    google_credentials = None
    is_demo = True


def load_demo_user(user_id):
    """Flask-Login loader hook. Returns a DemoUser only for a live, still-valid demo session."""
    if user_id != DEMO_USER_ID:
        return None
    code = session.get(_SESSION_CODE)
    started = session.get(_SESSION_STARTED) or 0
    if not code or _matching_code(code) is None:
        return None
    if time.time() - float(started) > _session_seconds():
        return None
    return DemoUser()


def is_demo_session():
    if not has_request_context():
        return False
    try:
        return bool(getattr(current_user, "is_demo", False))
    except Exception:
        return False


def demo_enabled():
    return bool(_access_codes())


@demo.route("/<code>", methods=["GET"])
def enter(code):
    matched = _matching_code(code)
    if matched is None:
        abort(404)
    return _start_session(matched)


@demo.route("/", methods=["POST"])
def enter_with_form():
    matched = _matching_code((request.form.get("code") or "").strip())
    if matched is None:
        flash("That demo code isn't valid. Contact info@xyzlabc.com to request one.", "error")
        return redirect(url_for("index"))
    return _start_session(matched)


def _start_session(matched):
    logout_user()
    session.clear()
    session[_SESSION_CODE] = matched
    session[_SESSION_STARTED] = time.time()
    login_user(DemoUser(), remember=False)
    logger.info("Demo session started (code=%s, ip=%s)", matched, request.remote_addr)
    return redirect(url_for("ai_compliance.home"))


@demo.route("/exit", methods=["GET"])
def exit_demo():
    logout_user()
    session.pop(_SESSION_CODE, None)
    session.pop(_SESSION_STARTED, None)
    return redirect(url_for("how_it_works"))


@demo.route("/document/<slug>", methods=["GET"])
def document(slug):
    doc = sample_data.DOCUMENTS.get(slug)
    if not is_demo_session() or doc is None:
        abort(404)
    return render_template("demo_document.html", doc=doc, lab_name=sample_data.LAB_NAME)


# --- Request guard -----------------------------------------------------------

_DEMO_NOTICE = "This is a demo with sample data, so changes are not saved."


def _json_body():
    return request.get_json(silent=True) or {}


def _pretend_saved():
    return jsonify({"success": True, "demo": True, "updated": 0, "updates": 0, "message": _DEMO_NOTICE})


def _refuse(message="This action is turned off in the demo."):
    return jsonify({"success": False, "demo": True, "error": message}), 403


def _sheet_tabs():
    sheets = [{"sheetId": i, "title": t, "index": i} for i, t in enumerate(sample_data.DEMO_SHEETS)]
    return jsonify({"success": True, "sheets": sheets, "default": sample_data.DEMO_SHEET})


def _requirements():
    sheet = request.args.get("sheet") or sample_data.DEMO_SHEET
    return jsonify(sample_data.requirements_payload(sheet))


def _not_in_demo(code):
    return {
        "success": True,
        "results": [],
        "summary": f"**{code}**: This requirement is not part of the demo sample set.",
        "citations": [],
        "result_count": 0,
        "inferred_status": "NOT CHECKED",
        "session": "",
        "related_questions": [],
        "requirement_assessments": [],
        "missing_evidence_categories": [],
    }


def _ai_search():
    payload = _json_body()
    if payload.get("search_only"):
        return jsonify(sample_data.free_text_search_payload(payload.get("query")))

    codes = [str(c).strip() for c in (payload.get("requirements") or []) if str(c).strip()]
    code = codes[0] if codes else ""
    req = sample_data.requirement(code)
    if req is None:
        return jsonify(_not_in_demo(code))

    resp = sample_data.search_payload(req)
    if payload.get("session"):
        resp["summary"] = (
            f"**{code}**: (Demo follow-up) In the full product the AI continues the same "
            "conversation and searches your documents again to answer your follow-up question. "
            f"For this sample laboratory, the most relevant finding remains: {req['summary']}"
        )
    return jsonify(resp)


def _gap_assessment():
    payload = _json_body()
    code = str(payload.get("requirement_id") or payload.get("check") or payload.get("checklist_item") or "").strip()
    req = sample_data.requirement(code)
    if req is None:
        return jsonify({
            "success": True,
            "requirement_id": code,
            "assessment": {
                "requirement_id": code,
                "status": "Not Checked",
                "gap_analysis": "Not part of the demo sample set.",
                "corrective_actions": [],
                "cited_documents": [],
            },
        })
    return jsonify(sample_data.gap_assessment_payload(req))


def _reports(loader):
    def handler():
        if request.method != "GET":
            return _refuse("Saving reports is turned off in the demo. Use Export to download a copy.")
        return jsonify({"success": True, "reports": loader()})
    return handler


def _compliance_sheet():
    sheet = request.args.get("sheet") or sample_data.DEMO_SHEET
    return jsonify(sample_data.compliance_sheet_payload(sheet))


def _document_list():
    if request.method == "GET":
        return None
    return jsonify(sample_data.drive_files_payload())


# Keys are ai_engine endpoint names (without the "admin." / "ai_engine." prefix).
_AI_ENGINE_HANDLERS = {
    "cus_gen_requirements": _requirements,
    "cap_cus_sheet_tabs": _sheet_tabs,
    "cap_ai_search": _ai_search,
    "cap_gap_assessment": _gap_assessment,
    "cap_compliance_sheet": _compliance_sheet,
    "document_list": _document_list,
    "ai_gap_analysis_reports": _reports(sample_data.gap_analysis_reports),
    "ai_search_reports": _reports(sample_data.search_reports),
    "delete_gap_analysis_report": _refuse,
    "delete_ai_report": _refuse,
    "update_results": _pretend_saved,
    "save_cus_compliance_comments": _pretend_saved,
    "save_cus_action_completion": _pretend_saved,
    "save_cus_evidence_checks": _pretend_saved,
    "cap_compliance_sheet_update": _pretend_saved,
    "cap_compliance_update_comment": _pretend_saved,
    "save_inspection_sheet": _pretend_saved,
    "get_inspection_sheet": lambda: jsonify({"success": True, "data": [], "count": 0}),
    "get_inspection_links_api": lambda: jsonify({"success": True, "links": {}}),
    "get_google_service_account_email": lambda: jsonify(
        {"email": "xyzsure-reader@your-project.iam.gserviceaccount.com"}
    ),
}

# ai_engine endpoints that are safe to run for real in a demo session: they only
# render templates or read reports through the demo-aware loaders in ai_utils.
_AI_ENGINE_PASSTHROUGH = {
    "home",
    "ai_compliance_search",
    "ai_gap_analysis",
    "export_report",
    "export_all_reports_pdf",
}

_APP_PASSTHROUGH = {
    "index",
    "logout",
    "static",
    "how_it_works",
    "ai_compliance.home",
    "ai_compliance.help_page",
    "admin.index",
    "admin.ai_search_results",
    "admin.ai_search_result_single",
}


def _ai_engine_name(endpoint):
    for prefix in ("admin.ai_engine.", "ai_engine."):
        if endpoint.startswith(prefix):
            return endpoint[len(prefix):]
    return None


def demo_guard():
    if request.endpoint is None or not is_demo_session():
        return None

    endpoint = request.endpoint
    if endpoint in _APP_PASSTHROUGH or endpoint.startswith("demo."):
        return None

    name = _ai_engine_name(endpoint)
    if name in _AI_ENGINE_PASSTHROUGH:
        return None
    if name in _AI_ENGINE_HANDLERS:
        resp = _AI_ENGINE_HANDLERS[name]()
        if resp is not None:
            return resp
        return None

    logger.info("Demo session blocked endpoint %s", endpoint)
    if request.method == "GET" and not request.is_json:
        flash("That page isn't available in the demo.", "error")
        return redirect(url_for("ai_compliance.home"))
    return _refuse()


def init_demo(app):
    app.register_blueprint(demo)
    app.before_request(demo_guard)

    @app.context_processor
    def inject_demo_flag():
        return {
            "is_demo": is_demo_session(),
            "demo_enabled": demo_enabled(),
            "demo_lab_name": sample_data.LAB_NAME,
        }
