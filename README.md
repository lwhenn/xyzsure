# XYZSure — Standalone AI Compliance

Flask app extracted from the **AI Compliance** module in `XYZ-LIMS-v2`
(`apps/Admin_Panel/ai_engine` + branded `/ai-compliance` shell).

## What it includes

- `/` — Google Workspace sign-in
- `/ai-compliance/` — home + help
- `/admin/ai-engine/...` — gap analysis, evidence search, reports, document list
- Google Sheets / Drive / Discovery Engine + LLM gap narratives (`ai_service.py`)

## What it does **not** include

Sample Control, Inventory, Document Control, Time Sheet, and other LIMS modules.

## External LIMS (“Include LIMS data”)

When `LIMS_API_BASE_URL` is set, the **Include LIMS data** checkbox calls an external HTTP API instead of querying XYZ LIMS ORM models.

Default request:

```http
GET {LIMS_API_BASE_URL}{LIMS_API_EVIDENCE_PATH}?q=...&keywords=a,b&req_code=GEN.123&limit=3&days=365
Authorization: Bearer {LIMS_API_TOKEN}
```

Expected JSON:

```json
{
  "records": [
    {
      "id": "42",
      "lims_type": "test_result",
      "sample_name": "S-100",
      "test_id": "CBC",
      "result": "Normal",
      "notes": "",
      "commit_timestamp": "2026-01-15T12:00:00Z",
      "link": "https://lims.example.com/results/42"
    },
    {
      "id": "99",
      "lims_type": "audit_action",
      "action_type": "corrective",
      "user_id": "jsmith",
      "description": "CAPA closed",
      "commit_timestamp": "2026-01-10T09:00:00Z"
    }
  ]
}
```

You may also return already-normalized rows (`snippet`, `document_name`, `title`, `from_lims`). Auth: `LIMS_API_TOKEN` (Bearer), `LIMS_API_KEY` (`X-API-Key`), or `LIMS_API_AUTH_HEADER`. Set `LIMS_API_FALLBACK_ORM=0` to disable shared-DB ORM fallback when the API is configured.

At startup the app probes the external API (if configured) and the local ORM. If **neither** is available, the **Include LIMS data** checkbox is disabled.

### XYZ-LIMS companion endpoint

Deploy XYZ-LIMS with `apps/compliance_api` and set on the LIMS server:

```env
LIMS_COMPLIANCE_API_TOKEN=generate-a-long-random-secret
```

Then on XYZSure:

```env
LIMS_API_BASE_URL=https://xyzlapp.com
LIMS_API_TOKEN=generate-a-long-random-secret
```

(Use your real LIMS public origin; include a `/lims` path prefix only if nginx exposes the app under that prefix.)

## Setup

```powershell
cd C:\XYZ\xyzsure
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Fill `.env` (easiest: copy the relevant keys from `C:\XYZ\XYZ-LIMS-v2\.env`).

Auth uses the same PostgreSQL `user_role` tables as LIMS. Users need the **admin** role.

For local Google OAuth, ensure `GOOGLE_CLIENT_SECRETS` redirect URIs include:

- `http://localhost:8090/google/authorize/callback`
- `http://localhost:8090/google/login/callback`

## Run

```powershell
python app.py
# or
flask --app app.py run --debug --host 0.0.0.0 --port 8090
```

Open http://localhost:8090/ → sign in → AI Compliance.

## Demo mode (for prospects)

Give prospects a link that opens the real AI Compliance screens filled with sample data for a fictional laboratory ("Riverbend Clinical Laboratory"). No sign-in is needed. Demo sessions make no Google, Discovery Engine, LLM, LIMS, or database calls, never read or write the saved report files, and any endpoint not explicitly allowed is refused.

```env
DEMO_ACCESS_CODES=acme-lab-7f3k2,trade-show-q4-9xw1
DEMO_SESSION_HOURS=8
```

Share `https://xyzsure.com/demo/<code>`, or have prospects type the code into the **Try the demo** box on the sign-in page (shown only when codes are configured). An unknown code in the link returns 404. **Exit demo** ends the session and returns to the sign-in page. Removing a code from `DEMO_ACCESS_CODES` (and restarting) ends every session that used it. Each visit is logged as `Demo session started (code=...)`.

Sample content lives in `apps/demo/sample_data.py`. Keep it fictional, and paraphrase checklist wording rather than copying licensed text.

## Production deploy (same VM as LIMS)

XYZSure is a **second Flask/Gunicorn process** next to LIMS. They do not share a socket or port.

| App | Domain (example) | Process | Socket |
|-----|------------------|---------|--------|
| LIMS | xyzlapp.com / xyzlaboratory.com | `xyz-lims.service` | `…/XYZ-LIMS.sock` |
| XYZSure | **xyzsure.com** | `xyzsure.service` | `…/xyzsure/xyzsure.sock` |

nginx routes by `server_name`; each upstream points at its own Unix socket. Templates: `deploy/xyzsure.service.example`, `deploy/xyzsure.nginx.conf.example`, `wsgi.py`.

### VM checklist

1. Copy app to `/usr/xyz_tools/xyzsure`, create venv, `pip install -r requirements.txt gunicorn`.
2. Copy `.env` (shared Postgres auth is fine). Set a **new** `FLASK_SECRET_KEY`, `TRUSTED_PROXY_HOPS=1`, and (if used) `LIMS_API_BASE_URL` to your LIMS public URL.
3. Google OAuth: add redirect URIs  
   `https://xyzsure.com/google/authorize/callback` and  
   `https://xyzsure.com/google/login/callback`.
4. Install systemd + nginx examples above; issue TLS for `xyzsure.com`; `systemctl enable --now xyzsure`; reload nginx. The nginx site file is `/etc/nginx/sites-available/xyzsure.conf` (symlinked into `sites-enabled/`). This VM's `nginx.conf` lists sites explicitly, so add `include /etc/nginx/sites-enabled/xyzsure.conf;` next to the other site includes.
5. DNS / Cloudflare: point `xyzsure.com` at the same VM.

Do **not** run production with `python app.py` / Flask debug. Use Gunicorn via systemd.

## Layout

| Path | Role |
|------|------|
| `app.py` | Slim Flask entry |
| `apps/Admin_Panel/ai_compliance.py` | `/ai-compliance` shell |
| `apps/Admin_Panel/ai_engine/` | Core routes + templates |
| `apps/Google_API/` | OAuth + Sheets/Drive helpers |
| `ai_service.py` | LLM providers |
| `data/` | Saved report JSON |
| `static/` | Bootstrap / DataTables assets |
