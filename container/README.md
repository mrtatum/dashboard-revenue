# Dashboard Revenue — Container

A Docker image that serves `Pipeline_Dashboard.html` as a web service and rebuilds it on demand from xlsx files stored in **OneDrive** — either OneDrive Personal or OneDrive for Business — using a scoped, identity-based credential (no anonymous share links).

---

## Prerequisites

- [Docker Desktop](https://docs.docker.com/desktop/) installed and running
- Python 3 (for the one-time setup script — runs on your laptop, not inside the container)
- A Microsoft account that has access to the OneDrive folder you want to use as a data source

---

## How it works

```
   user browser
       │
       ▼
   GET /dashboard ──▶ serves baked Pipeline_Dashboard.html
   POST /rebuild  ──▶ downloads xlsx from OneDrive ──▶ re-bakes HTML
                              │
                    Microsoft Graph API (HTTPS)
                              │
                    /drives/{ONEDRIVE_DRIVE_ID}/items/{ONEDRIVE_ITEM_ID}/
```

The container reads **only** the OneDrive folder you specify. All data stays inside Docker-managed named volumes — nothing is written to the host filesystem.

---

## First-time setup

### Step 1 — Register an Azure app (once per deployment)

1. Go to [entra.microsoft.com](https://entra.microsoft.com) → **Identity → Applications → App registrations → New registration**
2. Fill in:
   - **Name:** anything, e.g. `Dashboard Revenue Reader`
   - **Supported account types** — choose based on your OneDrive type:

     | OneDrive type | Supported account types | `MS_TENANT` value |
     |---|---|---|
     | OneDrive for Business | "Accounts in this organizational directory only" | your tenant GUID or domain, e.g. `contoso.onmicrosoft.com` |
     | OneDrive Personal | "Personal Microsoft accounts only" | `consumers` |
     | Either | "Accounts in any organizational directory and personal Microsoft accounts" | `common` |

   - **Redirect URI:** leave blank
3. After creation → **Authentication → Advanced settings → Allow public client flows = Yes**
4. Copy the **Application (client) ID** — this is your `MS_CLIENT_ID`
5. Go to **API permissions → Add a permission → Microsoft Graph → Delegated permissions** → tick **`Files.Read`** and **`offline_access`**
   - OneDrive Personal: no admin consent needed
   - OneDrive for Business: if your tenant requires admin consent, ask your admin to click **Grant admin consent** on the API permissions page

> **Tip for stronger isolation:** sign in as a dedicated "reader" account that only has the target folder shared with it. The container's token then cannot access anything else on your drive.

---

### Step 2 — Share the OneDrive folder

1. Open [OneDrive web](https://onedrive.live.com) (or your org's SharePoint)
2. Right-click the folder that contains your xlsx source files
3. Click **Share → Copy link**
4. Keep this URL for Step 3 — it looks like: `https://1drv.ms/f/c/abc123.../...?e=xxxxx`

---

### Step 3 — Run the setup script

Run this once on your laptop (not inside the container). It signs you in via browser, resolves the folder, and prints all the environment values you need:

```bash
# OneDrive for Business:
python3 container/tools/get_refresh_token.py \
    --client-id 'YOUR_MS_CLIENT_ID' \
    --tenant    'contoso.onmicrosoft.com' \
    --share-url '<your OneDrive share URL>'

# OneDrive Personal:
python3 container/tools/get_refresh_token.py \
    --client-id 'YOUR_MS_CLIENT_ID' \
    --tenant    consumers \
    --share-url 'https://1drv.ms/f/c/.../...?e=...'
```

The script will:
1. Display a short code and a URL — open the URL in your browser, enter the code, and sign in
2. Resolve the share URL to get the folder's Drive ID and Item ID
3. Print a ready-to-paste block like this:

```
============================================================
 SUCCESS — paste these into container/.env
============================================================
MS_CLIENT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
MS_TENANT=consumers
MS_REFRESH_TOKEN=0.AXXX...very-long-string...
ONEDRIVE_DRIVE_ID=b!xxxxxxxxxxxxxxxxxxxx
ONEDRIVE_ITEM_ID=01XXXXXXXXXXXXXXXXXXXXXXXX
============================================================
```

---

### Step 4 — Create the `.env` file

```bash
cd container/
cp .env.example .env
```

Open `.env` and paste in the values from Step 3:

```env
MS_CLIENT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
MS_TENANT=consumers
MS_REFRESH_TOKEN=0.AXXX...
ONEDRIVE_DRIVE_ID=b!xxxx...
ONEDRIVE_ITEM_ID=01XXXX...

# Optional but recommended for internet-facing deployments:
REBUILD_TOKEN=some-long-random-secret

# Auto-refresh: re-bake every 15 minutes if data changed (0 = disabled)
AUTO_REFRESH_INTERVAL_SECONDS=0
```

> **Keep `.env` secret.** It contains your refresh token. Never commit it to Git — it is already listed in `.gitignore`.

---

### Step 5 — Build and start

```bash
cd container/
docker compose up -d --build
```

Wait about 15 seconds for the first bake, then open:

```
http://<your-server-ip>:8080/dashboard
```

Or on the same machine:

```
http://localhost:8080/dashboard
```

---

## Running on a new machine

1. Clone / copy the repo onto the new machine
2. Install Docker Desktop and make sure it is running
3. **Re-run Step 3** (the setup script) to get a fresh refresh token — or reuse the same `.env` if the token has not expired
4. Copy your `.env` file into `container/`
5. Run:

```bash
cd container/
docker compose up -d --build
```

That's it — no other state to transfer. The container downloads source files from OneDrive fresh on first `/rebuild`.

---

## Changing the OneDrive source folder

When you want the dashboard to read from a different OneDrive folder:

**1. Get a share URL for the new folder** (same as Step 2 above)

**2. Re-run the setup script** with the new share URL:

```bash
python3 container/tools/get_refresh_token.py \
    --client-id 'YOUR_MS_CLIENT_ID' \
    --tenant    consumers \
    --share-url '<new folder share URL>'
```

**3. Update `.env`** — replace only the two folder ID lines (keep `MS_CLIENT_ID`, `MS_TENANT`, and `MS_REFRESH_TOKEN` the same if using the same account):

```env
ONEDRIVE_DRIVE_ID=b!new-drive-id...
ONEDRIVE_ITEM_ID=01NEW-ITEM-ID...
```

**4. Clear the source cache** so stale files from the old folder are removed:

```bash
docker compose down -v          # removes all named volumes
docker compose up -d            # starts fresh, downloads from new folder
```

---

## Useful commands

```bash
# Start
docker compose up -d

# Start and rebuild the image
docker compose up -d --build

# View live logs
docker logs -f dashboard-revenue

# Stop
docker compose down

# Stop and wipe all cached data (sources, baked HTML, secrets)
docker compose down -v

# Check status and auth wiring
curl -s http://localhost:8080/status | python3 -m json.tool

# Force a dashboard rebuild from OneDrive
curl -X POST http://localhost:8080/rebuild

# Force rebuild even if OneDrive data has not changed
curl -X POST "http://localhost:8080/rebuild?force=1"
```

---

## Available endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/` | Redirects to `/dashboard` |
| GET | `/dashboard` | Serves the baked dashboard HTML |
| POST | `/rebuild` | Downloads latest xlsx from OneDrive and re-bakes (skips if unchanged) |
| GET | `/changes` | Checks OneDrive metadata only — no downloads |
| GET | `/bake_id` | Returns current bake ID (used by dashboard JS for auto-reload) |
| GET | `/status` | JSON: auth status, last bake result, env var wiring |
| GET | `/healthz` | Returns `200 OK` — used by Docker healthcheck |

---

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `MS_CLIENT_ID` | Yes | — | App (client) ID from Entra app registration |
| `MS_REFRESH_TOKEN` | Yes | — | From `get_refresh_token.py`. Treat as a password |
| `MS_TENANT` | Yes | — | Tenant GUID/domain (Business) · `consumers` (Personal) · `common` (either) |
| `ONEDRIVE_DRIVE_ID` | Yes | — | Target folder's driveId, from `get_refresh_token.py` |
| `ONEDRIVE_ITEM_ID` | Yes | — | Target folder's itemId, from `get_refresh_token.py` |
| `REBUILD_TOKEN` | No | `""` (off) | If set, `/rebuild` requires header `X-Rebuild-Token: <value>` |
| `BAKE_ON_START` | No | `1` | Bake on container startup if cached HTML is missing |
| `AUTO_REFRESH_INTERVAL_SECONDS` | No | `0` (off) | Background poll interval in seconds. `900` = every 15 min |
| `PORT` | No | `8080` | HTTP port inside the container |

---

## Token lifecycle and renewal

The refresh token **rotates on every use** — the container automatically saves the latest token to the `dashboard-secrets` volume so restarts pick it up.

- **OneDrive Personal:** ~90 days of inactivity before expiry
- **OneDrive for Business:** usually ~90 days, but your tenant's Conditional Access policy may shorten this significantly

**If the container loses access to OneDrive** (`/status` shows a token error):

```bash
# 1. Re-run the setup script to get a fresh token
python3 container/tools/get_refresh_token.py \
    --client-id 'YOUR_MS_CLIENT_ID' \
    --tenant    consumers \
    --share-url '<your OneDrive share URL>'

# 2. Update MS_REFRESH_TOKEN in .env with the new value

# 3. Restart the container
docker compose up -d --force-recreate
```

---

## Project structure

```
container/
├── Dockerfile                  # python:3.12-slim base image
├── docker-compose.yml          # service definition with named volumes
├── .env.example                # template — copy to .env and fill in
├── .env                        # your secrets — never commit this
├── requirements.txt            # flask, gunicorn, openpyxl, requests
├── README.md                   # this file
├── run-local.sh                # convenience script for local dev
├── app/
│   ├── server.py               # Flask: /dashboard, /rebuild, /status, /healthz
│   ├── bake.py                 # xlsx → dashboard HTML
│   ├── auth.py                 # refresh-token + rotation logic
│   └── onedrive.py             # Graph API calls, scoped to one folder
├── template/
│   └── Pipeline_Dashboard.template.html
└── tools/
    └── get_refresh_token.py    # one-time setup script (run on your laptop)
```

---

## Security notes

- The container's token is scoped to **one Microsoft account** and **one Entra app**. It can read only what that account can read on OneDrive.
- All Graph calls are hard-coded to `/drives/{ONEDRIVE_DRIVE_ID}/items/{ONEDRIVE_ITEM_ID}/...` — the code refuses to read outside that folder.
- For internet-facing deployments, set `REBUILD_TOKEN` to prevent unauthorized rebuild triggers.
- For strongest isolation, use a dedicated "reader" Microsoft account that only has the target folder shared with it.
