import os
import logging
import time
import re
from datetime import datetime, timedelta, UTC
import json
import requests
from urllib.parse import parse_qs, urlsplit, urlunsplit

from flask import Blueprint, current_app, request, redirect, url_for, session
from flask_login import login_user, current_user
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
import google_auth_oauthlib.flow
from googleapiclient.discovery import build
import httplib2
from google_auth_httplib2 import AuthorizedHttp
from google.oauth2 import service_account
from email.message import EmailMessage
import google.oauth2.credentials
import google.auth.exceptions
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload
import io

import pandas as pd
import numpy as np
import base64

from database import db_session
from models.user_role import User

logger = logging.getLogger(__name__)

SHEETS_API_TIMEOUT_SEC = int(os.getenv("GOOGLE_SHEETS_API_TIMEOUT_SEC", "90"))
SHEETS_API_MAX_RETRIES = int(os.getenv("GOOGLE_SHEETS_API_MAX_RETRIES", "3"))


def _load_service_account_sheets_creds():
    creds = service_account.Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    if GOOGLE_SERVICE_ACCOUNT_SUBJECT:
        creds = creds.with_subject(GOOGLE_SERVICE_ACCOUNT_SUBJECT)
    return creds


def _build_sheets_service(creds, timeout_sec=None):
    timeout = timeout_sec or SHEETS_API_TIMEOUT_SEC
    http = AuthorizedHttp(creds, http=httplib2.Http(timeout=timeout))
    return build("sheets", "v4", http=http)


def _sheet_title_matches(existing_title, target_title):
    return str(existing_title or "").strip().lower() == str(target_title or "").strip().lower()


def _sheet_already_exists_error(exc):
    message = str(exc).lower()
    return "already exists" in message and "addsheet" in message


def _list_sheet_titles(spreadsheet_id):
    meta = get_sheets(spreadsheet_id, [], includeGridData=False)
    return [
        sheet.get("properties", {}).get("title")
        for sheet in meta.get("sheets", [])
    ]


def _find_sheet_title(existing_titles, target_title):
    for title in existing_titles or []:
        if _sheet_title_matches(title, target_title):
            return title
    return None


def _is_retryable_sheets_error(exc):
    """Return True for transient Sheets/API transport failures worth retrying."""
    if isinstance(exc, HttpError):
        status = getattr(getattr(exc, "resp", None), "status", None)
        return status in {429, 500, 502, 503, 504}
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    name = exc.__class__.__name__.lower()
    return any(token in name for token in ("timeout", "connection", "ssl", "socket"))


def _execute_sheets_api(request_fn, max_attempts=None, label="Sheets API"):
    attempts = max_attempts or SHEETS_API_MAX_RETRIES
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return request_fn()
        except Exception as exc:
            last_exc = exc
            if _sheet_already_exists_error(exc):
                break
            if attempt >= attempts or not _is_retryable_sheets_error(exc):
                break
            logger.warning(
                "%s failed (attempt %s/%s): %s",
                label,
                attempt,
                attempts,
                exc,
            )
            time.sleep(min(2 * attempt, 10))
    raise last_exc


"""
TO UPDATE SERVICE ACCOUNT PERMISSIONS:
https://admin.google.com/ > Security > Access and data control > API controls > Domain wide delegation
"""

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", None)
GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"
SCOPES = [
    "email",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "openid",
    "profile",
]

# Service account impersonation email for domain-wide delegation
# Used when accessing Google APIs on behalf of the organization
GOOGLE_SERVICE_ACCOUNT_SUBJECT = os.environ.get("GOOGLE_SERVICE_ACCOUNT_SUBJECT", None)
if not GOOGLE_SERVICE_ACCOUNT_SUBJECT:
    logger.warning("GOOGLE_SERVICE_ACCOUNT_SUBJECT environment variable not set - domain-wide delegation will not work")

# Email address for service account email sending
# Used specifically for Gmail API operations (can be different from GOOGLE_SERVICE_ACCOUNT_SUBJECT)
GOOGLE_SERVICE_ACCOUNT_EMAIL = os.environ.get("GOOGLE_SERVICE_ACCOUNT_EMAIL", None)
if not GOOGLE_SERVICE_ACCOUNT_EMAIL:
    logger.warning("GOOGLE_SERVICE_ACCOUNT_EMAIL environment variable not set - email sending will not work")

google_api = Blueprint(
    "google_api", __name__, template_folder="templates", url_prefix="/google"
)

_LOCAL_OAUTH_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _request_host_name():
    return (request.host or "").split(":", 1)[0].strip().lower()


def _should_force_https_oauth_url():
    forwarded_proto = (request.headers.get("X-Forwarded-Proto") or "").split(",", 1)[0].strip().lower()
    if forwarded_proto == "https":
        return True

    if request.is_secure:
        return True

    if _request_host_name() in _LOCAL_OAUTH_HOSTS:
        return False

    force_https = os.environ.get("GOOGLE_OAUTH_FORCE_HTTPS")
    if force_https is not None:
        return force_https.lower() in ("1", "true", "yes")

    return not current_app.debug


def _normalize_oauth_url(url):
    if not url:
        return url

    parsed = urlsplit(url)
    if parsed.scheme == "https" or not _should_force_https_oauth_url():
        return url

    return urlunsplit(parsed._replace(scheme="https"))


def _prepare_oauthlib_transport():
    if _request_host_name() in _LOCAL_OAUTH_HOSTS and current_app.debug:
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")


@google_api.route("/login/callback", methods=["POST"])
def login_callback():
    parsed_url = parse_qs(request.get_data())

    # Verify the Cross-Site Request Forgery (CSRF) token
    csrf_token_cookie = request.cookies.get("g_csrf_token")
    if not csrf_token_cookie:
        return "No CSRF token in Cookie.", 400

    csrf_token_body = parsed_url[b"g_csrf_token"][0]
    if not csrf_token_body:
        return "No CSRF token in post body.", 400

    if csrf_token_cookie != csrf_token_body.decode("utf-8"):
        return "Failed to verify double submit cookie.", 400

    # decode jwt
    idinfo = id_token.verify_oauth2_token(
        parsed_url[b"credential"][0], google_requests.Request(), GOOGLE_CLIENT_ID
    )

    if GOOGLE_CLIENT_ID != idinfo["aud"]:
        return "aud not equal to client ID", 400

    if idinfo["iss"] not in ["accounts.google.com", "https://accounts.google.com"]:
        return "iss not accounts.google.com or https://accounts.google.com", 400

    if datetime.fromtimestamp(idinfo["exp"]) < datetime.now():
        return "jwt expired", 400

    if idinfo["hd"] not in ["xyzlabc.com"]:
        return "incorrect organization", 400

    if not idinfo["email_verified"]:
        return "User email not available or not verified by Google.", 400

    if not db_session.get(User, idinfo["sub"]):
        user = User(id=idinfo["sub"], name=idinfo["name"], email=idinfo["email"])
        db_session.add(user)
        db_session.commit()
    else:
        user = db_session.get(User, idinfo["sub"])

    # Begin user session by logging the user in
    login_user(user)
    session["logged_in_by"] = "Google"

    # does not have refresh token and has creds                         user has a refresh token and is >60days out of use
    if (not user.last_token_refresh and user.google_credentials) or (
        user.last_token_refresh
        and user.last_token_refresh + timedelta(days=90) < datetime.now(UTC)
    ):
        revoke(user.id)
        user.last_token_refresh = None
        user.google_credentials = None

    next_url = session.get("post_login_next")
    if (
        isinstance(next_url, str)
        and next_url.startswith("/")
        and not next_url.startswith("//")
        and not any(ch in next_url for ch in ("\r", "\n", "\\"))
    ):
        # Keep post_login_next when Google API authorize is still required.
        if user.google_credentials:
            session.pop("post_login_next", None)
        return redirect(next_url)
    session.pop("post_login_next", None)
    return redirect(url_for("index"))


@google_api.route("/authorize", methods=["GET"])
def authorize():
    _prepare_oauthlib_transport()
    flow = google_auth_oauthlib.flow.Flow.from_client_config(
        json.loads(os.environ["GOOGLE_CLIENT_SECRETS"]), scopes=SCOPES
    )
    flow.redirect_uri = _normalize_oauth_url(
        url_for("google_api.authorize_callback", _external=True)
    )
    authorization_url, state = flow.authorization_url(
        access_type="offline"
    )  # , include_granted_scopes='true'
    session["google_state"] = state
    return redirect(authorization_url)


@google_api.route("/authorize/callback")
def authorize_callback():
    _prepare_oauthlib_transport()
    state = session["google_state"]
    flow = google_auth_oauthlib.flow.Flow.from_client_config(
        json.loads(os.environ["GOOGLE_CLIENT_SECRETS"]), scopes=SCOPES, state=state
    )
    flow.redirect_uri = _normalize_oauth_url(
        url_for("google_api.authorize_callback", _external=True)
    )

    authorization_response = _normalize_oauth_url(request.url)
    flow.fetch_token(authorization_response=authorization_response)

    # Store the credentials in the session.
    # ACTION ITEM for developers:
    #     Store user's access and refresh tokens in your data store if
    #     incorporating this code into your real app.
    credentials = flow.credentials

    user = db_session.get(User, current_user.id)
    user.google_credentials = credentials_to_dict(
        credentials, old_credentials=user.google_credentials
    )
    user.last_token_refresh = datetime.now(UTC)
    db_session.commit()
    next_url = session.pop("post_login_next", None)
    if (
        isinstance(next_url, str)
        and next_url.startswith("/")
        and not next_url.startswith("//")
        and not any(ch in next_url for ch in ("\r", "\n", "\\"))
    ):
        return redirect(next_url)
    return redirect(url_for("index"))


######################################################################################################################################
def revoke(user_id):
    user_obj = db_session.get(User, user_id)

    if user_obj.google_credentials is None:
        user_obj.google_credentials = None
        user_obj.last_token_refresh = None
        logger.warning("user had no credentials")
        return "user had no credentials"

    try:
        if not user_obj.google_credentials["refresh_token"]:
            user_obj.google_credentials = None
            user_obj.last_token_refresh = None
            logger.warning("user had no refresh token")
            return "user had no refresh token"
    except Exception as e:
        logger.error(e)

    credentials = google.oauth2.credentials.Credentials(**user_obj.google_credentials)

    try:
        revoke = requests.post(
            "https://oauth2.googleapis.com/revoke",
            params={"token": credentials.token},
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

        status_code = getattr(revoke, "status_code")
        if status_code == 200:
            user_obj.google_credentials = None
            user_obj.last_token_refresh = None
            db_session.commit()
            return "Credentials successfully revoked."
        else:
            logger.warning(f"status code: {status_code} ({revoke.text})")
            if "Token expired or revoked" in revoke.text or "invalid_token" in revoke.text:
                user_obj.google_credentials = None
                user_obj.last_token_refresh = None
                db_session.commit()
                return "Credentials (invalid/expired) removed from user."
            else:
                raise Exception("unable to revoke: Status code")
    except Exception as e:
        logger.error(f"{e}: {str(user_obj.google_credentials)}")
        # Fallback if we really can't revoke but want to clear the state
        user_obj.google_credentials = None
        user_obj.last_token_refresh = None
        db_session.commit()
        return f"Revocation error handled, local credentials cleared. Error: {e}"


#####################################################################################################################################
def credentials_to_dict(credentials, old_credentials=None):
    # Check if this is a service account credential (doesn't have refresh_token)
    if not hasattr(credentials, 'refresh_token'):
        return None  # Service account credentials shouldn't be converted
    
    refreshToken = credentials.refresh_token
    try:
        if (
            refreshToken in ["", None, "null", "NULL", "Null", "None"]
            and old_credentials is not None
        ):
            logger.warning(f"USER CREDS REFRESH TOKEN WAS {refreshToken}")
            refreshToken = old_credentials["refresh_token"]
    except Exception as e:
        logger.exception(e)

    return {
        "token": credentials.token,
        "refresh_token": refreshToken,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": credentials.scopes,
    }


def refreshToken(user_id):
    user_obj = db_session.get(User, user_id)
    if user_obj is None:
        raise Exception("User not found")

    if user_obj.google_credentials["refresh_token"] is None:
        user_obj.google_credentials = None
        user_obj.last_token_refresh = None
        db_session.commit()
        return True

    google_creds = user_obj.google_credentials
    if not google_creds["refresh_token"]:
        raise Exception("refresh token is None")
    if google_creds["refresh_token"] == "null":
        raise Exception("refresh token is null")
    if "refresh_token" not in google_creds:
        raise Exception("no refresh token found")

    params = {
        "grant_type": "refresh_token",
        "client_id": google_creds["client_id"],
        "client_secret": google_creds["client_secret"],
        "refresh_token": google_creds["refresh_token"],
    }

    authorization_url = "https://oauth2.googleapis.com/token"

    r = requests.post(authorization_url, data=params)
    if r.ok:
        google_creds["token"] = r.json()
        user_obj.google_credentials = google_creds
        user_obj.last_token_refresh = datetime.now(UTC)
        db_session.commit()
        return True
    else:
        error_msg = r.text
        logger.warning(f"Token refresh failed: {error_msg}")

        # If token is invalid/revoked, clear credentials so user can re-authorize
        if "invalid_grant" in error_msg.lower():
            logger.info(f"Clearing invalid credentials for user {user_id}")
            user_obj.google_credentials = None
            user_obj.last_token_refresh = None
            db_session.commit()
            raise Exception(
                "Google credentials expired or revoked. User needs to re-authorize."
            )

        raise Exception(f"Unable to refresh token: {error_msg}")


def update_creds(new_creds):
    new_creds_dict = credentials_to_dict(new_creds)
    
    # Skip updating if service account credentials (no refresh_token)
    if new_creds_dict is None:
        logger.debug("Skipping credential update for service account")
        return
    
    old_creds_dict = current_user.google_credentials

    if old_creds_dict != new_creds_dict:
        changed = []
        for key in new_creds_dict.keys():
            if new_creds_dict[key] != old_creds_dict[key]:
                changed.append(key)
        if changed:
            logger.info(f'Updating google creds: {", ".join(changed)}')

        new_scopes = new_creds_dict["scopes"]
        old_scopes = SCOPES
        new_scopes.sort()
        old_scopes.sort()
        if old_scopes != new_scopes:
            logger.warning(
                f"scopes do not match: {old_scopes} > {new_scopes} ||| CHANGED {list(set(old_scopes) - set(new_scopes))}"
            )

        user = db_session.get(User, current_user.id)
        user.google_credentials = credentials_to_dict(
            new_creds, old_credentials=user.google_credentials
        )
        user.last_token_refresh = datetime.now(UTC)
        db_session.commit()


def get_values_sheets(
    spreadsheetId: str,
    range,
    dateTimeRenderOption="SERIAL_NUMBER",
    valueRenderOption="FORMATTED_VALUE",
    majorDimension="DIMENSION_UNSPECIFIED",
    spoof=False,
):
    try:
        creds = _load_service_account_sheets_creds()
    except Exception as e:
        logger.error(f"Failed to load service account credentials: {e}")
        raise Exception("Service account not configured. Unable to access Google Sheets.")

    service = _build_sheets_service(creds)
    return_obj = _execute_sheets_api(
        lambda: service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheetId,
            range=range,
            dateTimeRenderOption=dateTimeRenderOption,
            valueRenderOption=valueRenderOption,
            majorDimension=majorDimension,
        )
        .execute(),
        label=f"get_values_sheets {spreadsheetId} {range}",
    )

    if not spoof:
        update_creds(creds)
    return return_obj


def get_sheets(spreadsheetId: str, ranges: list, includeGridData=False):
    try:
        creds = _load_service_account_sheets_creds()
    except Exception as e:
        logger.error(f"Failed to load service account credentials: {e}")
        raise Exception("Service account not configured. Unable to access Google Sheets.")

    service = _build_sheets_service(creds)
    return_obj = _execute_sheets_api(
        lambda: service.spreadsheets()
        .get(
            spreadsheetId=spreadsheetId, ranges=ranges, includeGridData=includeGridData
        )
        .execute(),
        label=f"get_sheets {spreadsheetId}",
    )
    update_creds(creds)
    return return_obj


def batchupdate_sheets(spreadsheetId: str, body: dict):
    try:
        creds = _load_service_account_sheets_creds()
    except Exception as e:
        logger.error(f"Failed to load service account credentials: {e}")
        raise Exception("Service account not configured. Unable to access Google Sheets.")
    service = _build_sheets_service(creds)
    return_obj = _execute_sheets_api(
        lambda: service.spreadsheets().batchUpdate(spreadsheetId=spreadsheetId, body=body).execute(),
        label=f"batchupdate_sheets {spreadsheetId}",
    )
    update_creds(creds)
    return return_obj


def ensure_sheet_tab(spreadsheet_id: str, title: str) -> tuple[str, bool]:
    """Return the sheet tab title, creating it only when missing."""
    target_title = str(title or "").strip()
    if not target_title:
        raise ValueError("Sheet tab title is required")

    existing_titles = []
    try:
        existing_titles = _list_sheet_titles(spreadsheet_id)
    except Exception as exc:
        logger.warning(
            "Could not list sheets for %s before creating tab %s: %s",
            spreadsheet_id,
            target_title,
            exc,
        )

    found_tab = _find_sheet_title(existing_titles, target_title)
    if found_tab:
        return found_tab, False

    try:
        batchupdate_sheets(
            spreadsheet_id,
            {"requests": [{"addSheet": {"properties": {"title": target_title}}}]},
        )
        return target_title, True
    except Exception as exc:
        if not _sheet_already_exists_error(exc):
            raise

        try:
            existing_titles = _list_sheet_titles(spreadsheet_id)
        except Exception as list_exc:
            logger.warning(
                "Sheet tab %s already exists on %s but could not re-list tabs: %s",
                target_title,
                spreadsheet_id,
                list_exc,
            )
            return target_title, False

        found_tab = _find_sheet_title(existing_titles, target_title)
        return found_tab or target_title, False


def batchupdate_values_sheets(spreadsheetId: str, body: dict, spoof=False):
    if spoof:
        creds = _load_service_account_sheets_creds()
    else:
        try:
            creds = _load_service_account_sheets_creds()
        except Exception as e:
            logger.error(f"Failed to load service account credentials: {e}")
            raise Exception("Service account not configured. Unable to access Google Sheets.")
    service = _build_sheets_service(creds)
    return_obj = _execute_sheets_api(
        lambda: service.spreadsheets().values().batchUpdate(spreadsheetId=spreadsheetId, body=body).execute(),
        label=f"batchupdate_values_sheets {spreadsheetId}",
    )
    if not spoof:
        update_creds(creds)
    return return_obj


def append_sheets(
    spreadsheetId,
    range,
    body,
    valueInputOption="RAW",
    insertDataOption="INSERT_ROWS",
    spoof=False,
):
    if spoof:
        creds = _load_service_account_sheets_creds()
    else:
        creds = google.oauth2.credentials.Credentials(**current_user.google_credentials)
    service = _build_sheets_service(creds)
    return_obj = _execute_sheets_api(
        lambda: service.spreadsheets()
        .values()
        .append(
            spreadsheetId=spreadsheetId,
            range=range,
            body=body,
            valueInputOption=valueInputOption,
            insertDataOption=insertDataOption,
        )
        .execute(),
        label=f"append_sheets {spreadsheetId}",
    )
    if not spoof:
        update_creds(creds)
    logger.debug(f"Append sheet from {spreadsheetId} {range}")
    return return_obj


def send_email(
    to,
    subject,
    content,
    cc=None,
    attachment=None,
    attachment_mime=None,
    attachment_filename=None,
    spoof=False,
):
    if spoof:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT"]),
            scopes=["https://www.googleapis.com/auth/gmail.send"],
        )
        # Gmail API requires a real mailbox context when using a service account.
        # Use delegated subject first; otherwise fall back to configured sender email.
        delegated_sender = GOOGLE_SERVICE_ACCOUNT_EMAIL
        if delegated_sender:
            creds = creds.with_subject(delegated_sender)
        else:
            raise ValueError(
                "Spoof email send requires GOOGLE_SERVICE_ACCOUNT_EMAIL for delegated Gmail mailbox access"
            )
    else:
        creds = google.oauth2.credentials.Credentials(**current_user.google_credentials)

    service = build("gmail", "v1", credentials=creds)
    message = EmailMessage()
    message.set_content(content)

    message["To"] = to
    if cc:
        message["cc"] = cc

    if spoof:
        sender_email = GOOGLE_SERVICE_ACCOUNT_EMAIL
        if sender_email:
            message["From"] = sender_email
        else:
            logger.warning("GOOGLE_SERVICE_ACCOUNT_EMAIL not set; email From field may be invalid")
    else:
        message["From"] = current_user.email

    message["Subject"] = subject

    if attachment and attachment_mime and attachment_filename:
        maintype, subtype = attachment_mime.split("/")
        message.add_attachment(
            attachment, maintype, subtype, filename=attachment_filename
        )

    # encoded message
    encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()

    create_message = {"raw": encoded_message}

    try:
        send_message = (
            service.users().messages().send(userId="me", body=create_message).execute()
        )
    except HttpError as error:
        logger.exception(
            "Gmail send failed (spoof=%s, from=%s, to=%s, subject=%s): %s",
            spoof,
            message.get("From"),
            to,
            subject,
            error,
        )
        raise

    if not spoof:
        update_creds(creds)
    logger.info(f"Sent email {subject} to {to}")


def pull_gsheet_data(workbook_id, range, spoof=False):
    rows = get_values_sheets(
        workbook_id, range, dateTimeRenderOption="FORMATTED_STRING", spoof=spoof
    )
    data = rows.get("values") or []

    # If sheet is empty or only has headers, return empty DataFrame with headers if present
    if not data or len(data) == 0:
        logger.info(f"Pulling sheet from {workbook_id} {range}: empty")
        return pd.DataFrame()

    header = list(data[0])
    body = [list(r) for r in data[1:]]

    # Determine required number of columns (max of header length and longest row)
    max_cols = len(header)
    for r in body:
        if len(r) > max_cols:
            max_cols = len(r)

    # Normalize header to max_cols (pad with generic column names if necessary)
    norm_header = header[:] if header else []
    col_idx = 1
    while len(norm_header) < max_cols:
        norm_header.append(f"Column_{len(norm_header)+1}")
        col_idx += 1

    # Pad/truncate body rows to match normalized header length
    norm_rows = []
    for r in body:
        if len(r) < max_cols:
            r = r + [""] * (max_cols - len(r))
        elif len(r) > max_cols:
            r = r[:max_cols]
        norm_rows.append(r)

    try:
        df = pd.DataFrame(norm_rows, columns=norm_header)
    except Exception as e:
        logger.exception(
            f"Failed to construct DataFrame for sheet {workbook_id} {range}: {e}"
        )
        # Fallback: create DataFrame without columns
        df = pd.DataFrame(norm_rows)

    # Replace NaN/None with empty string for consistency
    df = df.replace({np.nan: "", None: ""})
    logger.info(
        f"Pulling sheet from {workbook_id} {range}: {len(df)} rows, {len(df.columns)} columns"
    )
    return df


def upload_gdrive(
    local_filename, drive_filename, drive_id, folder_id, mimetype="*/*", spoof=False
):
    """Insert new file.
    Returns : Id's of the file uploaded

    Load pre-authorized user credentials from the environment.
    TODO(developer) - See https://developers.google.com/identity
    for guides on implementing OAuth2 for the application.
    """
    if spoof:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT"]),
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        if GOOGLE_SERVICE_ACCOUNT_SUBJECT:
            creds = creds.with_subject(GOOGLE_SERVICE_ACCOUNT_SUBJECT)
    else:
        creds = google.oauth2.credentials.Credentials(**current_user.google_credentials)

    try:
        # create drive api client
        service = build("drive", "v3", credentials=creds)

        file_metadata = {
            "name": drive_filename,
            "mimeType": mimetype,
            "driveId": drive_id,
            "parents": [folder_id],
        }

        media = MediaFileUpload(local_filename, mimetype=mimetype, resumable=True)
        # pylint: disable=maybe-no-member
        file = (
            service.files()
            .create(
                body=file_metadata,
                media_body=media,
                fields="id",
                supportsAllDrives=True,
            )
            .execute()
        )

        logger.info(f'File ID: {file.get("id")}')

        if not spoof:
            update_creds(creds)

    except HttpError as error:
        logger.warning(f"An error occurred: {error}")
        file = None

    return file.get("id")


def upload_gdrive_bytes_to_folder(
    data: bytes,
    drive_filename: str,
    folder_id: str,
    mimetype: str = "application/pdf",
    spoof: bool = False,
    media_mimetype: str = None,
    description: str = None,
):
    """
    Upload bytes into a Drive folder. Resolves Shared Drive driveId from the parent folder
    (omits driveId when the folder is in My Drive).
    media_mimetype: bytes content type; defaults to mimetype. Use when converting on upload
    (e.g. DOCX bytes -> Google Doc via mimetype application/vnd.google-apps.document).
    Returns metadata dict (id, name, webViewLink) or {} on failure.
    """
    upload_mimetype = media_mimetype or mimetype
    if spoof:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT"]),
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        if GOOGLE_SERVICE_ACCOUNT_SUBJECT:
            creds = creds.with_subject(GOOGLE_SERVICE_ACCOUNT_SUBJECT)
    else:
        creds = google.oauth2.credentials.Credentials(**current_user.google_credentials)

    try:
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        folder_meta = (
            service.files()
            .get(fileId=folder_id, fields="id,driveId", supportsAllDrives=True)
            .execute()
        )
        drive_id = folder_meta.get("driveId")

        file_metadata = {
            "name": drive_filename,
            "mimeType": mimetype,
            "parents": [folder_id],
        }
        if description:
            file_metadata["description"] = description
        if drive_id:
            file_metadata["driveId"] = drive_id

        resumable = len(data) > 5 * 1024 * 1024
        media = MediaIoBaseUpload(
            io.BytesIO(data), mimetype=upload_mimetype, resumable=resumable
        )
        file = (
            service.files()
            .create(
                body=file_metadata,
                media_body=media,
                fields="id,name,webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )

        logger.info(f'Uploaded bytes to Drive folder {folder_id}, file ID: {file.get("id")}')

        if not spoof:
            update_creds(creds)

        return file or {}
    except HttpError as error:
        logger.warning(f"upload_gdrive_bytes_to_folder failed: {error}")
        return {}


def list_drive_folder_files(folder_url_or_id, recursive=True, max_files=50, spoof=False, path_prefix=""):
    """
    List files from a Google Drive folder.
    
    Args:
        folder_url_or_id: Either a folder ID or a full Google Drive folder URL
        recursive: If True, search subfolders as well
        max_files: Maximum number of files to return
        spoof: Whether to use service account credentials
    
    Returns:
        List of dicts with file info: [{"name": "...", "id": "...", "webViewLink": "...", "mimeType": "...", "folder_path": "..."}]
    """
    # Extract folder ID from URL if needed
    folder_id = folder_url_or_id
    if "drive.google.com" in str(folder_url_or_id):
        # Extract folder ID from URL patterns like:
        # https://drive.google.com/drive/folders/FOLDER_ID
        # https://drive.google.com/drive/u/0/folders/FOLDER_ID
        import re
        match = re.search(r'/folders/([a-zA-Z0-9_-]+)', str(folder_url_or_id))
        if match:
            folder_id = match.group(1)
        else:
            logger.warning(f"Could not extract folder ID from URL: {folder_url_or_id}")
            return []
    
    creds = None
    
    if spoof:
        try:
            creds = service_account.Credentials.from_service_account_info(
                json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT"]),
                scopes=["https://www.googleapis.com/auth/drive.readonly"],
            )
            if GOOGLE_SERVICE_ACCOUNT_SUBJECT:
                creds = creds.with_subject(GOOGLE_SERVICE_ACCOUNT_SUBJECT)
        except Exception as e:
            logger.warning(f"Service account initialization failed: {e}. Will attempt user credentials.")
            spoof = False
    
    if not spoof:
        if not current_user or not hasattr(current_user, 'google_credentials') or not current_user.google_credentials:
            logger.warning("No user credentials available for Drive API")
            return []
        creds = google.oauth2.credentials.Credentials(**current_user.google_credentials)

    try:
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        
        files_list = []
        
        # Query to get files in folder
        query = f"'{folder_id}' in parents and trashed=false"
        if not recursive:
            query += " and mimeType!='application/vnd.google-apps.folder'"
        
        page_token = None
        while len(files_list) < max_files:
            results = service.files().list(
                q=query,
                pageSize=min(100, max_files - len(files_list)),
                pageToken=page_token,
                fields="nextPageToken, files(id, name, mimeType, webViewLink, size, modifiedTime)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True
            ).execute()
            
            items = results.get('files', [])
            if not items:
                break
            
            for item in items:
                # If it's a folder and recursive is True, list its contents too
                if recursive and item['mimeType'] == 'application/vnd.google-apps.folder':
                    folder_name = item.get('name', '')
                    next_prefix = f"{path_prefix}/{folder_name}" if path_prefix else folder_name
                    subfolder_files = list_drive_folder_files(
                        item['id'],
                        recursive=True,
                        max_files=max_files - len(files_list),
                        spoof=spoof,
                        path_prefix=next_prefix,
                    )
                    files_list.extend(subfolder_files)
                else:
                    # Only include non-folder files
                    if item['mimeType'] != 'application/vnd.google-apps.folder':
                        files_list.append({
                            'name': item.get('name', ''),
                            'id': item.get('id', ''),
                            'webViewLink': item.get('webViewLink', f"https://drive.google.com/file/d/{item.get('id')}/view"),
                            'mimeType': item.get('mimeType', ''),
                            'size': item.get('size', ''),
                            'modifiedTime': item.get('modifiedTime', ''),
                            'folder_path': path_prefix,
                        })
                
                if len(files_list) >= max_files:
                    break
            
            page_token = results.get('nextPageToken')
            if not page_token:
                break
        
        if not spoof:
            update_creds(creds)
        
        logger.debug(f"Listed {len(files_list)} files from Drive folder {folder_id}")
        return files_list[:max_files]
        
    except HttpError as error:
        error_content = str(error)
        logger.warning(f"An error occurred listing Drive folder {folder_id}: {error}")
        
        # If it's an authorization error and we were using service account, try user credentials
        if spoof and ("unauthorized" in error_content.lower() or "forbidden" in error_content.lower()):
            logger.info(f"Service account authorization failed for folder {folder_id}. Attempting with user credentials...")
            if current_user and hasattr(current_user, 'google_credentials') and current_user.google_credentials:
                try:
                    user_creds = google.oauth2.credentials.Credentials(**current_user.google_credentials)
                    service = build("drive", "v3", credentials=user_creds, cache_discovery=False)
                    files_list = []
                    query = f"'{folder_id}' in parents and trashed=false"
                    if not recursive:
                        query += " and mimeType!='application/vnd.google-apps.folder'"
                    
                    page_token = None
                    while len(files_list) < max_files:
                        results = service.files().list(
                            q=query,
                            pageSize=min(100, max_files - len(files_list)),
                            pageToken=page_token,
                            fields="nextPageToken, files(id, name, mimeType, webViewLink, size, modifiedTime)",
                            supportsAllDrives=True,
                            includeItemsFromAllDrives=True
                        ).execute()
                        
                        items = results.get('files', [])
                        if not items:
                            break
                        
                        for item in items:
                            if recursive and item['mimeType'] == 'application/vnd.google-apps.folder':
                                folder_name = item.get('name', '')
                                next_prefix = f"{path_prefix}/{folder_name}" if path_prefix else folder_name
                                subfolder_files = list_drive_folder_files(
                                    item['id'],
                                    recursive=True,
                                    max_files=max_files - len(files_list),
                                    spoof=False,
                                    path_prefix=next_prefix,
                                )
                                files_list.extend(subfolder_files)
                            else:
                                if item['mimeType'] != 'application/vnd.google-apps.folder':
                                    files_list.append({
                                        'name': item.get('name', ''),
                                        'id': item.get('id', ''),
                                        'webViewLink': item.get('webViewLink', f"https://drive.google.com/file/d/{item.get('id')}/view"),
                                        'mimeType': item.get('mimeType', ''),
                                        'size': item.get('size', ''),
                                        'modifiedTime': item.get('modifiedTime', ''),
                                        'folder_path': path_prefix,
                                    })
                            
                            if len(files_list) >= max_files:
                                break
                        
                        page_token = results.get('nextPageToken')
                        if not page_token:
                            break
                    
                    update_creds(user_creds)
                    logger.debug(f"Successfully listed {len(files_list)} files from Drive folder {folder_id} using user credentials")
                    return files_list[:max_files]
                except Exception as fallback_error:
                    logger.warning(f"Fallback with user credentials also failed: {fallback_error}")
                    return []
            else:
                logger.warning("No user credentials available for fallback")
                return []
        
        return []
    except google.auth.exceptions.RefreshError as auth_error:
        logger.error(f"Google authentication error for Drive folder {folder_id}: {auth_error}. Service account may lack required scopes.")
        # Try fallback with user credentials
        if spoof and current_user and hasattr(current_user, 'google_credentials') and current_user.google_credentials:
            try:
                logger.info(f"Attempting fallback with user credentials for folder {folder_id}")
                return list_drive_folder_files(folder_url_or_id, recursive=recursive, max_files=max_files, spoof=False)
            except Exception as fallback_error:
                logger.warning(f"Fallback with user credentials also failed: {fallback_error}")
                return []
        return []
    except Exception as e:
        logger.exception(f"Unexpected error listing Drive folder {folder_id}: {e}")
        # Try fallback with user credentials if there was an unexpected auth error
        error_str = str(e).lower()
        if spoof and ("unauthorized" in error_str or "auth" in error_str) and current_user and hasattr(current_user, 'google_credentials') and current_user.google_credentials:
            try:
                logger.info(f"Attempting fallback with user credentials after unexpected error for folder {folder_id}")
                return list_drive_folder_files(folder_url_or_id, recursive=recursive, max_files=max_files, spoof=False)
            except Exception as fallback_error:
                logger.warning(f"Fallback with user credentials also failed: {fallback_error}")
                return []
        return []


def list_drive_folder_contents(folder_url_or_id, max_items=200, spoof=False):
    """
    List immediate child folders and files in a Google Drive folder.

    Returns:
        {"folders": [{"id", "name"}, ...], "files": [{"id", "name", "mimeType", "webViewLink"}, ...]}
    """
    folder_id = folder_url_or_id
    if "drive.google.com" in str(folder_url_or_id):
        match = re.search(r"/folders/([a-zA-Z0-9_-]+)", str(folder_url_or_id))
        if match:
            folder_id = match.group(1)
        else:
            logger.warning(f"Could not extract folder ID from URL: {folder_url_or_id}")
            return {"folders": [], "files": []}

    creds = None
    if spoof:
        try:
            creds = service_account.Credentials.from_service_account_info(
                json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT"]),
                scopes=["https://www.googleapis.com/auth/drive.readonly"],
            )
            if GOOGLE_SERVICE_ACCOUNT_SUBJECT:
                creds = creds.with_subject(GOOGLE_SERVICE_ACCOUNT_SUBJECT)
        except Exception as e:
            logger.warning(f"Service account initialization failed: {e}. Will attempt user credentials.")
            spoof = False

    if not spoof:
        if not current_user or not hasattr(current_user, "google_credentials") or not current_user.google_credentials:
            logger.warning("No user credentials available for Drive API")
            return {"folders": [], "files": []}
        creds = google.oauth2.credentials.Credentials(**current_user.google_credentials)

    try:
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        query = f"'{folder_id}' in parents and trashed=false"
        folders = []
        files = []
        page_token = None
        while len(folders) + len(files) < max_items:
            results = (
                service.files()
                .list(
                    q=query,
                    pageSize=min(100, max_items - len(folders) - len(files)),
                    pageToken=page_token,
                    fields="nextPageToken, files(id, name, mimeType, webViewLink, modifiedTime)",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            items = results.get("files", [])
            if not items:
                break
            for item in items:
                if item.get("mimeType") == "application/vnd.google-apps.folder":
                    folders.append({"id": item.get("id", ""), "name": item.get("name", "")})
                else:
                    files.append(
                        {
                            "id": item.get("id", ""),
                            "name": item.get("name", ""),
                            "mimeType": item.get("mimeType", ""),
                            "webViewLink": item.get(
                                "webViewLink",
                                f"https://drive.google.com/file/d/{item.get('id')}/view",
                            ),
                        }
                    )
                if len(folders) + len(files) >= max_items:
                    break
            page_token = results.get("nextPageToken")
            if not page_token:
                break

        folders.sort(key=lambda x: (x.get("name") or "").lower())
        files.sort(key=lambda x: (x.get("name") or "").lower())
        if not spoof:
            update_creds(creds)
        return {"folders": folders, "files": files}
    except HttpError as error:
        logger.warning(f"An error occurred listing Drive folder contents for {folder_id}: {error}")
        return {"folders": [], "files": []}
    except Exception as e:
        logger.exception(f"Unexpected error listing Drive folder contents for {folder_id}: {e}")
        return {"folders": [], "files": []}


def list_drive_child_folders(
    folder_url_or_id, recursive=False, max_folders=100, spoof=False, path_prefix=""
):
    """
    List subfolders inside a Google Drive folder.

    Returns:
        List of dicts: [{"name": "...", "id": "...", "folder_path": "..."}]
    """
    folder_id = folder_url_or_id
    if "drive.google.com" in str(folder_url_or_id):
        match = re.search(r"/folders/([a-zA-Z0-9_-]+)", str(folder_url_or_id))
        if match:
            folder_id = match.group(1)
        else:
            logger.warning(f"Could not extract folder ID from URL: {folder_url_or_id}")
            return []

    creds = None
    if spoof:
        try:
            creds = service_account.Credentials.from_service_account_info(
                json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT"]),
                scopes=["https://www.googleapis.com/auth/drive.readonly"],
            )
            if GOOGLE_SERVICE_ACCOUNT_SUBJECT:
                creds = creds.with_subject(GOOGLE_SERVICE_ACCOUNT_SUBJECT)
        except Exception as e:
            logger.warning(f"Service account initialization failed: {e}. Will attempt user credentials.")
            spoof = False

    if not spoof:
        if not current_user or not hasattr(current_user, "google_credentials") or not current_user.google_credentials:
            logger.warning("No user credentials available for Drive API")
            return []
        creds = google.oauth2.credentials.Credentials(**current_user.google_credentials)

    try:
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        folders_list = []
        query = (
            f"'{folder_id}' in parents and trashed=false "
            "and mimeType='application/vnd.google-apps.folder'"
        )
        page_token = None
        while len(folders_list) < max_folders:
            results = (
                service.files()
                .list(
                    q=query,
                    pageSize=min(100, max_folders - len(folders_list)),
                    pageToken=page_token,
                    fields="nextPageToken, files(id, name)",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            items = results.get("files", [])
            if not items:
                break
            for item in items:
                folder_name = item.get("name", "")
                next_prefix = f"{path_prefix}/{folder_name}" if path_prefix else folder_name
                folders_list.append(
                    {
                        "name": folder_name,
                        "id": item.get("id", ""),
                        "folder_path": next_prefix,
                    }
                )
                if recursive and len(folders_list) < max_folders:
                    subfolders = list_drive_child_folders(
                        item["id"],
                        recursive=True,
                        max_folders=max_folders - len(folders_list),
                        spoof=spoof,
                        path_prefix=next_prefix,
                    )
                    folders_list.extend(subfolders)
                if len(folders_list) >= max_folders:
                    break
            page_token = results.get("nextPageToken")
            if not page_token:
                break

        if not spoof:
            update_creds(creds)
        return folders_list[:max_folders]
    except HttpError as error:
        logger.warning(f"An error occurred listing Drive subfolders for {folder_id}: {error}")
        return []
    except Exception as e:
        logger.exception(f"Unexpected error listing Drive subfolders for {folder_id}: {e}")
        return []


def search_files_in_parent_folders(
    parent_folder_ids,
    name_contains: str = None,
    mime_types=None,
    max_files: int = 200,
    spoof: bool = False,
):
    """
    Search non-folder files under one or more parent folders (batched queries).
    Returns list of dicts with id, name, mimeType, webViewLink, modifiedTime, parents.
    """
    parent_folder_ids = [str(p).strip() for p in (parent_folder_ids or []) if str(p).strip()]
    if not parent_folder_ids or max_files <= 0:
        return []

    mime_types = [m for m in (mime_types or []) if m]
    results = []
    batch_size = 40

    try:
        service, creds, using_spoof = _get_drive_service(scope_readonly=True, spoof=spoof)
        for offset in range(0, len(parent_folder_ids), batch_size):
            if len(results) >= max_files:
                break
            batch = parent_folder_ids[offset : offset + batch_size]
            parents_q = " or ".join(f"'{fid}' in parents" for fid in batch)
            q = f"trashed=false and ({parents_q})"
            if mime_types:
                if len(mime_types) == 1:
                    q += f" and mimeType='{mime_types[0]}'"
                else:
                    q += " and (" + " or ".join(f"mimeType='{m}'" for m in mime_types) + ")"
            if name_contains:
                safe_name = (name_contains or "").replace("'", "\\'")
                q += f" and name contains '{safe_name}'"

            page_token = None
            while len(results) < max_files:
                resp = (
                    service.files()
                    .list(
                        q=q,
                        pageSize=min(100, max_files - len(results)),
                        pageToken=page_token,
                        fields="nextPageToken, files(id, name, mimeType, webViewLink, modifiedTime, parents)",
                        supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                    )
                    .execute()
                )
                for item in resp.get("files", []):
                    if item.get("mimeType") == "application/vnd.google-apps.folder":
                        continue
                    parents = item.get("parents") or []
                    results.append(
                        {
                            "id": item.get("id", ""),
                            "name": item.get("name", ""),
                            "mimeType": item.get("mimeType", ""),
                            "webViewLink": item.get(
                                "webViewLink",
                                f"https://drive.google.com/file/d/{item.get('id')}/view",
                            ),
                            "modifiedTime": item.get("modifiedTime", ""),
                            "parents": parents,
                        }
                    )
                    if len(results) >= max_files:
                        break
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

        if not using_spoof:
            update_creds(creds)
    except Exception as e:
        logger.warning(f"search_files_in_parent_folders failed: {e}")
        return []

    return results[:max_files]


# --------------------- Drive Content Helpers ---------------------
def _get_drive_service(scope_readonly=True, spoof=False):
    """Internal: build Drive service with either service account (spoof) or user creds."""
    scopes = [
        "https://www.googleapis.com/auth/drive.readonly"
        if scope_readonly
        else "https://www.googleapis.com/auth/drive"
    ]
    creds = None
    if spoof:
        try:
            creds = service_account.Credentials.from_service_account_info(
                json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT"]), scopes=scopes
            )
            if GOOGLE_SERVICE_ACCOUNT_SUBJECT:
                creds = creds.with_subject(GOOGLE_SERVICE_ACCOUNT_SUBJECT)
        except Exception as e:
            logger.warning(
                f"Service account init failed for Drive access: {e}. Falling back to user credentials."
            )
            spoof = False
    if not spoof:
        if not current_user or not getattr(current_user, "google_credentials", None):
            raise RuntimeError("No user Google credentials available for Drive API")
        creds = google.oauth2.credentials.Credentials(**current_user.google_credentials)
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return service, creds, spoof


def get_file_metadata(file_id: str, fields: str = "id,name,mimeType,size,modifiedTime", spoof=False):
    """Retrieve Drive file metadata fields.

    Returns a dict or {} on error.
    """
    try:
        service, creds, using_spoof = _get_drive_service(scope_readonly=True, spoof=spoof)
        meta = (
            service.files()
            .get(fileId=file_id, fields=fields, supportsAllDrives=True)
            .execute()
        )
        if not using_spoof:
            update_creds(creds)
        return meta or {}
    except Exception as e:
        logger.warning(f"get_file_metadata failed for {file_id}: {e}")
        return {}


def copy_drive_file_to_folder(
    source_file_id: str, target_folder_id: str, new_name: str = None, spoof=False
):
    """
    Copy a Drive file into a target folder.
    Returns metadata dict (id, name, mimeType, webViewLink) or {} on failure.
    """
    try:
        service, creds, using_spoof = _get_drive_service(scope_readonly=False, spoof=spoof)
        body = {"parents": [target_folder_id]}
        if new_name:
            body["name"] = new_name

        copied = (
            service.files()
            .copy(
                fileId=source_file_id,
                body=body,
                fields="id,name,mimeType,webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )
        if not using_spoof:
            update_creds(creds)
        return copied or {}
    except Exception as e:
        logger.warning(
            f"copy_drive_file_to_folder failed for {source_file_id} -> {target_folder_id}: {e}"
        )
        return {}


def create_drive_subfolder(parent_folder_id: str, folder_name: str, spoof=False):
    """
    Create a subfolder inside a Drive folder.
    Returns metadata dict (id, name, webViewLink) or {} on failure.
    """
    parent_folder_id = (parent_folder_id or "").strip()
    folder_name = (folder_name or "").strip()
    if not parent_folder_id or not folder_name:
        return {}
    try:
        service, creds, using_spoof = _get_drive_service(scope_readonly=False, spoof=spoof)
        body = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_folder_id],
        }
        folder = (
            service.files()
            .create(
                body=body,
                fields="id,name,webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )
        if not using_spoof:
            update_creds(creds)
        return folder or {}
    except Exception as e:
        logger.warning(
            f"create_drive_subfolder failed for {parent_folder_id}/{folder_name}: {e}"
        )
        return {}


def export_google_doc_text(file_id: str, spoof=False) -> str:
    """Export Google Doc to plain text.
    Works for mimeType 'application/vnd.google-apps.document'. Returns text or ''.
    """
    try:
        service, creds, using_spoof = _get_drive_service(scope_readonly=True, spoof=spoof)
        data = (
            service.files()
            .export(fileId=file_id, mimeType="text/plain")
            .execute()
        )
        # data is bytes
        text = data.decode("utf-8", errors="ignore") if isinstance(data, (bytes, bytearray)) else str(data)
        if not using_spoof:
            update_creds(creds)
        return text or ""
    except Exception as e:
        logger.debug(f"export_google_doc_text failed for {file_id}: {e}")
        return ""


def export_google_doc_pdf_bytes(file_id: str, spoof=False) -> bytes:
    """Export Google Doc as PDF bytes. Returns b'' on failure."""
    try:
        service, creds, using_spoof = _get_drive_service(scope_readonly=True, spoof=spoof)
        data = (
            service.files()
            .export(fileId=file_id, mimeType="application/pdf")
            .execute()
        )
        if not using_spoof:
            update_creds(creds)
        if data is None:
            return b""
        return bytes(data) if not isinstance(data, bytes) else data
    except Exception as e:
        logger.debug(f"export_google_doc_pdf_bytes failed for {file_id}: {e}")
        return b""


def download_drive_file_bytes(file_id: str, spoof=False) -> bytes:
    """Download arbitrary Drive file bytes via media API. Returns bytes or b''."""
    try:
        service, creds, using_spoof = _get_drive_service(scope_readonly=True, spoof=spoof)
        request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            # No log spam on status
        if not using_spoof:
            update_creds(creds)
        return fh.getvalue()
    except Exception as e:
        logger.debug(f"download_drive_file_bytes failed for {file_id}: {e}")
        return b""


def rename_drive_file(
    file_id: str,
    new_name: str,
    description: str = None,
    spoof: bool = False,
):
    """
    Rename a Drive file (and optionally update description).
    Returns metadata dict (id, name, mimeType, webViewLink) or {} on failure.
    """
    file_id = (file_id or "").strip()
    new_name = (new_name or "").strip()
    if not file_id or not new_name:
        return {}
    try:
        service, creds, using_spoof = _get_drive_service(scope_readonly=False, spoof=spoof)
        body = {"name": new_name}
        if description is not None:
            body["description"] = description
        updated = (
            service.files()
            .update(
                fileId=file_id,
                body=body,
                fields="id,name,mimeType,webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )
        if not using_spoof:
            update_creds(creds)
        return updated or {}
    except Exception as e:
        logger.warning(f"rename_drive_file failed for {file_id}: {e}")
        return {}


def move_drive_file_to_folder(
    file_id: str,
    target_folder_id: str,
    new_name: str = None,
    description: str = None,
    spoof: bool = False,
):
    """
    Move a Drive file into target_folder_id (optionally rename / update description).
    Returns metadata dict (id, name, mimeType, webViewLink) or {} on failure.
    """
    file_id = (file_id or "").strip()
    target_folder_id = (target_folder_id or "").strip()
    if not file_id or not target_folder_id:
        return {}
    try:
        service, creds, using_spoof = _get_drive_service(scope_readonly=False, spoof=spoof)
        current = (
            service.files()
            .get(fileId=file_id, fields="parents", supportsAllDrives=True)
            .execute()
        )
        previous_parents = ",".join(current.get("parents") or [])
        body = {}
        if new_name:
            body["name"] = new_name.strip()
        if description is not None:
            body["description"] = description

        update_kwargs = {
            "fileId": file_id,
            "addParents": target_folder_id,
            "removeParents": previous_parents,
            "fields": "id,name,mimeType,webViewLink",
            "supportsAllDrives": True,
        }
        if body:
            update_kwargs["body"] = body
        updated = service.files().update(**update_kwargs).execute()
        if not using_spoof:
            update_creds(creds)
        return updated or {}
    except Exception as e:
        logger.warning(
            f"move_drive_file_to_folder failed for {file_id} -> {target_folder_id}: {e}"
        )
        return {}


def trash_drive_file(file_id: str, spoof=False) -> bool:
    """Move a Drive file to trash. Returns True on success."""
    try:
        service, creds, using_spoof = _get_drive_service(scope_readonly=False, spoof=spoof)
        service.files().update(
            fileId=file_id,
            body={"trashed": True},
            supportsAllDrives=True,
        ).execute()
        if not using_spoof:
            update_creds(creds)
        return True
    except Exception as e:
        logger.warning(f"trash_drive_file failed for {file_id}: {e}")
        return False


def get_drive_text_snippet(file_id: str, mime_type: str = None, max_chars: int = 8000, spoof=False, timeout_sec: int = 5, max_size_mb: float = 2.0) -> str:
    """Attempt to extract a text snippet from a Drive file.

    - Google Docs: export to text/plain
    - PDF: basic text extraction if pdfminer.six available, else ''
    - DOCX: basic text extraction if python-docx available, else ''
    - Other: ''
    
    Args:
        timeout_sec: Max seconds per file (default 5)
        max_size_mb: Skip files larger than this (default 2MB)
    """
    import signal
    import time as _time
    start = _time.time()
    try:
        mt = mime_type
        if not mt:
            meta = get_file_metadata(file_id, fields="mimeType,name,size", spoof=spoof)
            mt = meta.get("mimeType")
            # Check file size early - skip large files
            size_bytes = meta.get("size")
            if size_bytes:
                try:
                    size_mb = int(size_bytes) / (1024 * 1024)
                    if size_mb > max_size_mb:
                        logger.debug(f"Skipping large file {file_id}: {size_mb:.1f}MB > {max_size_mb}MB")
                        return ""
                except Exception:
                    pass
        mt = str(mt or "").lower()

        text = ""
        if mt == "application/vnd.google-apps.document":
            text = export_google_doc_text(file_id, spoof=spoof)
            # Early timeout check
            if (_time.time() - start) > timeout_sec:
                logger.debug(f"Timeout extracting {file_id} after {timeout_sec}s")
                return text[:max_chars] if text else ""
        elif mt == "application/pdf":
            data = download_drive_file_bytes(file_id, spoof=spoof)
            if data:
                try:
                    from pdfminer.high_level import extract_text
                    # pdfminer expects path or file-like
                    text = extract_text(io.BytesIO(data)) or ""
                except Exception as e:
                    logger.debug(f"PDF text extraction failed for {file_id}: {e}")
                    # Fallback to pypdf if available
                    try:
                        from pypdf import PdfReader
                        reader = PdfReader(io.BytesIO(data))
                        parts = []
                        for page in reader.pages:
                            try:
                                parts.append(page.extract_text() or "")
                            except Exception:
                                parts.append("")
                        text = "\n".join([p for p in parts if p])
                    except Exception as e2:
                        logger.debug(f"pypdf text extraction failed for {file_id}: {e2}")
                        text = ""
        elif mt in (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/msword",
        ):
            data = download_drive_file_bytes(file_id, spoof=spoof)
            if data:
                try:
                    import docx
                    with io.BytesIO(data) as bio:
                        document = docx.Document(bio)
                        parts = [p.text for p in document.paragraphs if p.text]
                        text = "\n".join(parts)
                except Exception as e:
                    logger.debug(f"DOCX text extraction failed for {file_id}: {e}")
                    text = ""
        else:
            # Unsupported types: skip
            text = ""

        if not text:
            return ""
        text = text.strip()
        if max_chars and len(text) > max_chars:
            return text[:max_chars]
        return text
    except Exception as e:
        logger.debug(f"get_drive_text_snippet failed for {file_id}: {e}")
        return ""

def truncate_for_sheets(text, max_chars=49500):
    """
    Truncate text for Google Sheets cells.
    
    Google Sheets has a hard limit of 50,000 characters per cell.
    This function truncates with a safety margin to prevent truncation by Google.
    
    Args:
        text: The text to truncate
        max_chars: Maximum characters allowed (default 49500 to leave 500 char margin)
    
    Returns:
        Truncated text if it exceeds max_chars, otherwise original text
    """
    if not text:
        return text
    
    text_str = str(text).strip()
    
    if len(text_str) > max_chars:
        # Truncate and add indicator
        return text_str[:max_chars] + "\n\n[... truncated due to length ...]"
    
    return text_str