# Dashboard Revenue — container

A Docker image that serves `Pipeline_Dashboard.html` as a web service and
rebuilds it on demand from xlsx files in **OneDrive** — either OneDrive
Personal or OneDrive for Business — read with a scoped, identity-based
credential (no anonymous share links).

## Security model in one paragraph

The container holds a refresh token tied to one Microsoft account and one
Entra app you registered. That token can read **only what that account can
read** in OneDrive — so if you sign in as a dedicated "dashboard reader"
account that only has the target folder shared with it, the container
literally cannot see the rest of your drive. On top of that, every Microsoft
Graph call is hard-coded to
`/drives/{ONEDRIVE_DRIVE_ID}/items/{ONEDRIVE_ITEM_ID}/...` — the code
*refuses* to construct any URL outside that subtree (see `app/onedrive.py`).
Optional `REBUILD_TOKEN` adds an HTTP-level secret so `/rebuild` can't be
poked from outside.

## Runtime data flow — OneDrive is the only external input

```
                ┌──────────────────────────────────────────────────┐
                │                                                  │
   user click   │   GET /dashboard ───▶ /app/static/*.html         │
   ───────────▶ │                                                  │
                │   POST /rebuild ───▶ ScopedOneDrive ──HTTPS──▶ Microsoft Graph
                │                            │                    │
                │                            ▼                    │
                │                    /app/sources/*.xlsx  ◀── ONE source of data
                │                            │                    │
                │                            ▼                    │
                │                    bake.py + /app/template/  ──▶ /app/static/*.html
                │                                                  │
                │      (everything inside the dotted box is        │
                │       inside the container; no host paths)       │
                └──────────────────────────────────────────────────┘
```

## What's in this folder

```
container/
├── Dockerfile                # python:3.12-slim, gunicorn, linux/amd64
├── docker-compose.yml        # one-shot run with named volumes + env
├── .env.example              # copy → .env, fill from get_refresh_token.py
├── requirements.txt          # flask, gunicorn, openpyxl, requests
├── README.md                 # this file
├── app/                      # baked into the image at build time
│   ├── server.py             # Flask: /dashboard, /rebuild, /status, /healthz
│   ├── bake.py               # xlsx → dashboard HTML (reads /app/sources only)
│   ├── auth.py               # GraphTokenProvider (refresh-token, rotation)
│   └── onedrive.py           # ScopedOneDrive — auth'd, folder-locked Graph calls
├── template/                 # baked into the image at build time
│   └── Pipeline_Dashboard.template.html   # has __EMBEDDED_JSON__ marker
└── tools/
    └── get_refresh_token.py  # one-time device-code login (runs on your Mac,
                              # NOT inside the container)
```

Files the container **creates** at runtime, all inside its own filesystem
(Docker-managed named volumes, never on the host):

| Path inside container         | What lives there                           | Volume               |
|-------------------------------|--------------------------------------------|----------------------|
| `/app/sources/`               | xlsx files downloaded from OneDrive        | `dashboard-sources`  |
| `/app/static/`                | baked `Pipeline_Dashboard.html`            | `dashboard-static`   |
| `/app/secrets/refresh_token`  | rotated MS refresh token (chmod 700)       | `dashboard-secrets`  |

## Backup of the local-only setup

Before the container work, `Dashboard_Revenue/` (HTML + every xlsx) was zipped
to `Dashboard_Revenue/Dashboard_Revenue_backup_2026-05-18.zip` (~17 MB). Unzip
to roll back at any time.

## One-time setup (~5 minutes)

### 1. Register an Entra (Azure AD) app

Go to https://entra.microsoft.com → **Identity → Applications → App
registrations → New registration**.

* **Name:** anything (e.g. `Dashboard Revenue Reader`)
* **Supported account types** — pick what matches your OneDrive flavor:

    | OneDrive flavor              | Supported account types                                              | `MS_TENANT` value                                |
    |------------------------------|----------------------------------------------------------------------|--------------------------------------------------|
    | OneDrive for Business        | "Accounts in this organizational directory only"                     | your tenant GUID or domain (e.g. `contoso.onmicrosoft.com`) |
    | OneDrive Personal            | "Personal Microsoft accounts only"                                   | `consumers`                                      |
    | Either                       | "Accounts in any organizational directory and personal Microsoft accounts" | `common`                                  |

* **Redirect URI:** leave blank
* After creation → **Authentication → Advanced settings → Allow public
  client flows = Yes** (device-code flow needs this)
* Copy the **Application (client) ID** — that becomes `MS_CLIENT_ID`

Then → **API permissions → Add a permission → Microsoft Graph → Delegated
permissions** → tick **`Files.Read`** and **`offline_access`**.
* OneDrive Personal: no admin consent needed.
* OneDrive for Business: if your tenant requires admin consent for
  `Files.Read`, ask your admin to click **Grant admin consent for <tenant>**
  on the API permissions page. (Most orgs allow user consent for read-only
  Files scopes by default.)

### 2. Share the target folder with the account you'll sign in as

In OneDrive web, share the folder containing
`Pipeline/Pipeline system team.xlsx` + `SQ/YR<YYYY>/SQ_*.xlsx` with the
account you plan to authenticate as. **Permission: Can view.**

> For strongest isolation, sign in as a dedicated "dashboard reader" account
> that has *only* the target folder shared with it. The container's token
> then has zero ability to see anything else, regardless of bugs in our code.
> For Business, ask your admin for a service account; for Personal, a free
> outlook.com account works.

### 3. Run the setup script on your Mac

```bash
cd /Users/mtatum/Claude_Cowork/Dashboard_Revenue

# OneDrive for Business — replace contoso.onmicrosoft.com with your tenant:
python3 container/tools/get_refresh_token.py \
    --client-id 'YOUR_CLIENT_ID_FROM_STEP_1' \
    --tenant    'contoso.onmicrosoft.com' \
    --share-url '<your OneDrive share URL>'

# OneDrive Personal:
python3 container/tools/get_refresh_token.py \
    --client-id 'YOUR_CLIENT_ID_FROM_STEP_1' \
    --tenant    consumers \
    --share-url 'https://1drv.ms/f/c/.../...?e=...'
```

It will:
* print a short code and a URL — open the URL in your browser, type the code,
  and sign in as the personal account from step 2
* exchange the code for an access token + refresh token
* resolve the share URL to `driveId` + `itemId`
* print a block of `MS_*` and `ONEDRIVE_*` values ready to paste into `.env`

### 4. Drop the values into `.env`

```bash
cd container
cp .env.example .env
# edit .env, paste the block from step 3
# Optional: also set REBUILD_TOKEN=<long random string> if internet-facing.
```

### 5. Build and run

```bash
docker compose up -d --build
# wait ~15 s for the first bake
open http://<server-ip>:8080/dashboard
```

## Endpoints

| Method | Path         | What                                                                                            |
|--------|--------------|-------------------------------------------------------------------------------------------------|
| GET    | `/`          | Redirects to `/dashboard`                                                                       |
| GET    | `/dashboard` | Serves the baked HTML                                                                           |
| POST   | `/rebuild`   | **Smart**: checks OneDrive watermark first; skips if unchanged. `?force=1` overrides.           |
| GET    | `/changes`   | Cheap "anything newer upstream?" check. Hits Graph metadata only, no downloads.                 |
| GET    | `/bake_id`   | Currently served bake_id. No Graph hit. Used by dashboard JS for auto-reload.                   |
| GET    | `/status`    | JSON: auth wiring + last_bake state + last build outcome                                        |
| GET    | `/healthz`   | 200 OK                                                                                          |

## Smart refresh — only re-bake when there's new data

* Every bake writes `last_bake.json` next to the HTML with a `bake_id`
  (monotonic) and a **watermark** = `max(lastModifiedDateTime)` + file count
  + total size across the folder's xlsx files.
* `POST /rebuild` first calls `get_watermark()` (which only reads file
  metadata via Graph — no downloads) and compares against the stored
  watermark. If the signatures match, the endpoint returns
  `{"ok": true, "updated": false, "reason": "no upstream changes"}` and
  does nothing else. If they differ, it downloads + re-bakes and increments
  `bake_id`.
* The dashboard's **Refresh** button calls `/rebuild` and:
    * `updated: true` → reloads the page
    * `updated: false` → shows "✓ Already up to date" toast, no reload
* The dashboard tab also polls `/bake_id` every 60 s. If the server has
  re-baked since the page loaded, the tab auto-reloads.
* Set `AUTO_REFRESH_INTERVAL_SECONDS=900` (or similar) in `.env` to enable
  the background poller — the server then checks OneDrive every 15 min and
  re-bakes only when something has actually changed. Combined with the JS
  poll, the dashboard updates itself with zero clicks.

`/rebuild` honors `REBUILD_TOKEN` if set: callers must send header
`X-Rebuild-Token: <value>`. The dashboard's Refresh button calls `/rebuild`
in-page; if you set `REBUILD_TOKEN`, put the service behind a reverse proxy
that injects the header for browser sessions (or leave the token blank for
LAN-only deployments and rely on network ACLs).

## Environment variables

| Variable             | Default                                          | Notes                                                            |
|----------------------|--------------------------------------------------|------------------------------------------------------------------|
| `MS_CLIENT_ID`       | _(required)_                                     | App (client) ID from Entra app registration.                     |
| `MS_REFRESH_TOKEN`   | _(required)_                                     | From `get_refresh_token.py`. Treat as a password.                |
| `MS_TENANT`          | _(required)_                                     | Tenant GUID/domain (Business) · `consumers` (Personal) · `common` (either). |
| `MS_SCOPE`           | `Files.Read offline_access`                      | Don't widen unless you know why.                                 |
| `MS_SECRETS_PATH`    | `/app/secrets/refresh_token`                     | Rotated refresh tokens are written here (mount as a volume).     |
| `ONEDRIVE_DRIVE_ID`  | _(required)_                                     | Target folder's driveId, from `get_refresh_token.py`.            |
| `ONEDRIVE_ITEM_ID`   | _(required)_                                     | Target folder's itemId, from `get_refresh_token.py`.             |
| `REBUILD_TOKEN`      | `""` (off)                                       | If set, `/rebuild` requires header `X-Rebuild-Token: <value>`.   |
| `BAKE_ON_START`      | `1` (compose) / `0` (Dockerfile)                 | Bake at boot if cached HTML is missing.                          |
| `AUTO_REFRESH_INTERVAL_SECONDS` | `0` (off)                             | Background poller cadence. e.g. `900` = check every 15 min, rebuild only when changed. |
| `SOURCES_DIR`        | `/app/sources`                                   | Where xlsx files are cached (mount as a volume).                 |
| `OUTPUT_PATH`        | `/app/static/Pipeline_Dashboard.html`            | Baked HTML.                                                      |
| `PORT`               | `8080`                                           | HTTP port inside the container.                                  |

## Token lifecycle

Refresh tokens **rotate on every refresh**. The container persists each new
rotated token to `/app/secrets/refresh_token` (the `dashboard-secrets` named
volume) so a restart picks up the freshest token automatically.

* **OneDrive Personal** — typically ~90 days of inactivity before expiry.
* **OneDrive for Business** — usually ~90 days, but your tenant's
  Conditional Access policy can shorten this (sometimes drastically — e.g.
  a `signInFrequency` policy can force daily reauth). If `/rebuild` starts
  returning 401 from Graph and `/status` shows a token-refresh error, your
  tenant has expired the token; re-run `get_refresh_token.py` and update
  `MS_REFRESH_TOKEN` in `.env`.

## Restoring the local-only workflow

```bash
cd /Users/mtatum/Claude_Cowork
unzip Dashboard_Revenue/Dashboard_Revenue_backup_2026-05-18.zip -d Dashboard_Revenue_restored
open Dashboard_Revenue_restored/Dashboard_Revenue/Pipeline_Dashboard.html
```

That gives you the exact pre-container state.

## Verifying scope at runtime

```bash
curl -s http://localhost:8080/status | python3 -m json.tool
```

You'll see whether each env var is set, plus the last build's result/error.
The actual driveId/itemId are not returned (only a boolean) so the scope
itself isn't leaked.

## Known caveats

* **Bake script is best-effort.** It parses the `Pipeline` sheet of
  `Pipeline system team.xlsx` and `SQ_Used_V103` in each SQ file. On the
  local sample it reproduces the existing dashboard exactly (665 pipeline
  rows, 131 orders). If templates change, see `app/bake.py`.
* **Scope is enforced in our code, not by Graph.** Delegated `Files.Read`
  doesn't have per-folder granularity — the token can read anything the
  signed-in account can read. The dedicated service-account pattern (share
  only the target folder with the signing-in account) is what enforces it
  at the Microsoft side. For OneDrive for Business shops that want true
  Microsoft-side per-resource isolation, the right pattern is **app-only
  authentication with `Sites.Selected`** — ask if you want to move to that;
  it's a bigger change but eliminates the user/refresh-token dimension.
* **Conditional Access (Business only).** Some tenants block device-code
  flow entirely, or require a compliant device. If `get_refresh_token.py`
  fails with AADSTS50059/53000/53003, your tenant's CA policy is the cause —
  your admin needs to grant an exception for this app, or you'll need to
  switch to a redirect-based auth flow.
