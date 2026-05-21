"""
Flask web service that serves Pipeline_Dashboard.html and exposes a
POST /rebuild endpoint which downloads fresh xlsx files from OneDrive
and re-bakes the dashboard.

RUNTIME DATA FLOW — OneDrive is the only external input.

    POST /rebuild
       │
       ▼
    onedrive.ScopedOneDrive ── HTTPS ──▶ graph.microsoft.com
       │ (scoped to a single drive_id + item_id)
       ▼
    /app/sources/        ← xlsx files written here, inside the container
       │
       ▼
    bake.py reads xlsx + /app/template/<embedded-marker>
       │
       ▼
    /app/static/Pipeline_Dashboard.html   ← served by GET /dashboard
    /app/static/last_bake.json            ← bake_id + watermark; cheap-to-check freshness

Every writable path the container touches (/app/sources, /app/static,
/app/secrets) lives inside the container's filesystem (backed by Docker
named volumes, not host bind mounts). The container does NOT read from
any path on the host.

SMART REFRESH

  POST /rebuild      Checks the OneDrive watermark first. If nothing has
                     changed since last_bake.json, returns updated=false
                     and skips the download/bake. Send ?force=1 to override.
  GET  /changes      Cheap "is anything newer upstream?" check. Hits Graph,
                     does NOT download.
  GET  /bake_id      Returns the currently served bake_id. No Graph hit.
                     The dashboard JS polls this for auto-reload.
  AUTO_REFRESH_INTERVAL_SECONDS
                     If > 0, a daemon thread calls /rebuild on that cadence,
                     so the dashboard updates without anyone clicking anything.

Environment variables (auth — OneDrive Personal or for Business):
  MS_CLIENT_ID           Entra (Azure AD) app client ID. REQUIRED.
  MS_REFRESH_TOKEN       Long-lived refresh token from tools/get_refresh_token.py. REQUIRED.
  MS_TENANT              OAuth authority. REQUIRED. One of:
                           * tenant GUID or domain (Business; e.g. contoso.onmicrosoft.com)
                           * 'organizations' (any Entra ID tenant)
                           * 'consumers'     (OneDrive Personal)
                           * 'common'        (either; auto-detected at sign-in)
  MS_SCOPE               OAuth scope. Default: "Files.Read offline_access".
  MS_SECRETS_PATH        Where rotated refresh tokens are written. Default /app/secrets/refresh_token.

Environment variables (folder scope):
  ONEDRIVE_DRIVE_ID      The driveId containing the target folder. REQUIRED.
  ONEDRIVE_ITEM_ID       The itemId of the target folder. REQUIRED.
                         (Both are printed by tools/get_refresh_token.py.)

Environment variables (web service):
  SOURCES_DIR                    xlsx cache. Default /app/sources.
  TEMPLATE_PATH                  Dashboard template. Default /app/template/...
  OUTPUT_PATH                    Baked dashboard. Default /app/static/Pipeline_Dashboard.html.
  REBUILD_TOKEN                  Optional: if set, /rebuild requires header X-Rebuild-Token.
  BAKE_ON_START                  "1" = run initial bake on boot if output missing.
  AUTO_REFRESH_INTERVAL_SECONDS  Background poller cadence. "0" = disabled (default).
                                 e.g. "900" for every 15 minutes.

Endpoints:
  GET  /                 Redirects to /dashboard
  GET  /dashboard        Serves Pipeline_Dashboard.html
  POST /rebuild          Smart rebuild (skip if watermark unchanged; ?force=1 overrides)
  GET  /changes          Cheap watermark check vs upstream
  GET  /bake_id          Currently served bake_id (no Graph hit)
  GET  /status           JSON: auth wiring + last build outcome
  GET  /healthz          200 OK
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import threading
import time
import traceback

from flask import Flask, jsonify, redirect, request, send_file

from . import bake as bake_mod
from . import auth as auth_mod
from . import onedrive as onedrive_mod


SOURCES_DIR = pathlib.Path(os.environ.get("SOURCES_DIR", "/app/sources"))
TEMPLATE_PATH = pathlib.Path(os.environ.get("TEMPLATE_PATH", "/app/template/Pipeline_Dashboard.template.html"))
OUTPUT_PATH = pathlib.Path(os.environ.get("OUTPUT_PATH", "/app/static/Pipeline_Dashboard.html"))
STATE_PATH = OUTPUT_PATH.with_name("last_bake.json")

REBUILD_TOKEN = os.environ.get("REBUILD_TOKEN", "")
BAKE_ON_START = os.environ.get("BAKE_ON_START", "0") == "1"
AUTO_REFRESH_INTERVAL = int(os.environ.get("AUTO_REFRESH_INTERVAL_SECONDS", "0") or "0")

log = logging.getLogger("server")

app = Flask(__name__)
_rebuild_lock = threading.Lock()
_last_build: dict = {"ok": None, "at": None, "summary": None, "error": None}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_client() -> "onedrive_mod.ScopedOneDrive":
    tokens = auth_mod.from_env()
    return onedrive_mod.from_env(tokens)


def _read_state() -> dict:
    """Return the contents of last_bake.json, or {} if missing/unreadable."""
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _local_signature() -> str:
    state = _read_state()
    return ((state.get("source_watermark") or {}).get("signature") or "")


def _do_rebuild(force: bool = False) -> dict:
    """Smart rebuild. Checks OneDrive watermark first unless force=True.

    Returns a summary dict with keys:
        ok (bool), updated (bool), reason (str), bake_id (int|None), ...
    """
    started = time.time()
    client = _build_client()
    log.info("rebuild: checking watermark (force=%s)", force)
    remote = client.get_watermark()
    local_sig = _local_signature()
    if not force and remote.signature() == local_sig and local_sig:
        log.info("rebuild: no upstream changes — skipping (sig=%s)", local_sig)
        state = _read_state()
        return {
            "ok": True,
            "updated": False,
            "reason": "no upstream changes",
            "bake_id": state.get("bake_id"),
            "remote_watermark": remote.to_dict(),
            "local_watermark": state.get("source_watermark") or {},
            "elapsed_sec": round(time.time() - started, 2),
        }

    log.info(
        "rebuild: pulling from drive=%s item=%s (scoped, authenticated)",
        client.drive_id[:8] + "...", client.item_id[:12] + "...",
    )
    dl = client.download_to(SOURCES_DIR, include_ext=(".xlsx",), skip_unchanged=True)
    log.info("rebuild: download done — %d new, %d cached, %.1f MB",
             dl.downloaded, dl.skipped, dl.total_bytes / 1_048_576)
    summary = bake_mod.bake(
        SOURCES_DIR, TEMPLATE_PATH, OUTPUT_PATH,
        watermark=remote.to_dict(),
    )
    summary.update({
        "ok": True,
        "updated": True,
        "reason": "forced" if force else "upstream changed",
        "download": {
            "downloaded": dl.downloaded,
            "skipped": dl.skipped,
            "total_bytes": dl.total_bytes,
            "file_count": len(dl.files),
        },
        "remote_watermark": remote.to_dict(),
        "elapsed_sec": round(time.time() - started, 2),
    })
    return summary


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.route("/")
def root():
    return redirect("/dashboard", code=302)


@app.route("/dashboard")
def dashboard():
    if not OUTPUT_PATH.exists():
        return (
            "<h1>Dashboard not built yet</h1>"
            "<p>POST /rebuild to fetch xlsx from OneDrive and bake the dashboard, "
            "or set BAKE_ON_START=1 to bake at container startup.</p>",
            503,
        )
    return send_file(str(OUTPUT_PATH), mimetype="text/html")


@app.route("/rebuild", methods=["POST"])
def rebuild():
    if REBUILD_TOKEN:
        if request.headers.get("X-Rebuild-Token", "") != REBUILD_TOKEN:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
    force = request.args.get("force", "").lower() in ("1", "true", "yes")
    if not _rebuild_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "rebuild already in progress"}), 409
    try:
        summary = _do_rebuild(force=force)
        _last_build.update({
            "ok": True,
            "at": summary.get("generated_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": summary,
            "error": None,
        })
        return jsonify(summary)
    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc()
        log.exception("rebuild failed")
        _last_build.update({
            "ok": False,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": None,
            "error": str(exc),
        })
        return jsonify({"ok": False, "updated": False, "error": str(exc), "traceback": tb}), 500
    finally:
        _rebuild_lock.release()


@app.route("/changes")
def changes():
    """Cheap 'is OneDrive newer than what I served last?' check.

    Hits Graph to read file metadata (no downloads). Useful for clients that
    want to decide whether to trigger a rebuild themselves.
    """
    try:
        client = _build_client()
        remote = client.get_watermark()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500
    local = (_read_state().get("source_watermark") or {})
    return jsonify({
        "ok": True,
        "changes_available": remote.signature() != local.get("signature", ""),
        "remote_watermark": remote.to_dict(),
        "local_watermark": local,
        "bake_id": _read_state().get("bake_id"),
    })


@app.route("/bake_id")
def bake_id():
    """Currently served bake_id. No Graph hit — safe to poll every few seconds."""
    state = _read_state()
    return jsonify({"bake_id": state.get("bake_id"), "generated_at": state.get("generated_at")})


@app.route("/status")
def status():
    state = _read_state()
    return jsonify({
        "auth": {
            "client_id_set": bool(os.environ.get("MS_CLIENT_ID", "").strip()),
            "refresh_token_set": bool(os.environ.get("MS_REFRESH_TOKEN", "").strip()),
            "tenant": os.environ.get("MS_TENANT", ""),
            "scope": os.environ.get("MS_SCOPE", auth_mod.DEFAULT_SCOPE),
        },
        "scope": {
            "drive_id_set": bool(os.environ.get("ONEDRIVE_DRIVE_ID", "").strip()),
            "item_id_set": bool(os.environ.get("ONEDRIVE_ITEM_ID", "").strip()),
        },
        "auto_refresh_interval_seconds": AUTO_REFRESH_INTERVAL,
        "rebuild_token_required": bool(REBUILD_TOKEN),
        "sources_dir": str(SOURCES_DIR),
        "output_path": str(OUTPUT_PATH),
        "output_exists": OUTPUT_PATH.exists(),
        "output_mtime": (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(OUTPUT_PATH.stat().st_mtime))
            if OUTPUT_PATH.exists() else None
        ),
        "last_bake_state": state,
        "last_build": _last_build,
    })


@app.route("/healthz")
def healthz():
    return "ok", 200


# ---------------------------------------------------------------------------
# Startup hooks
# ---------------------------------------------------------------------------

def _maybe_bake_on_start():
    if not BAKE_ON_START:
        return
    if OUTPUT_PATH.exists():
        log.info("startup: output exists, skipping initial bake")
        return
    try:
        log.info("startup: BAKE_ON_START=1 — running initial rebuild")
        summary = _do_rebuild(force=True)
        _last_build.update({
            "ok": True,
            "at": summary.get("generated_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": summary,
            "error": None,
        })
    except Exception as exc:  # noqa: BLE001
        log.exception("startup bake failed: %s", exc)
        _last_build.update({
            "ok": False,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "error": str(exc),
        })


def _background_poller():
    """Daemon thread: periodically smart-rebuild. Skips if nothing changed."""
    log.info("auto-refresh: starting background poller every %ds", AUTO_REFRESH_INTERVAL)
    # Stagger the first run a little so it doesn't race a startup bake.
    time.sleep(min(AUTO_REFRESH_INTERVAL, 60))
    while True:
        if not _rebuild_lock.acquire(blocking=False):
            log.info("auto-refresh: skip cycle — rebuild already running")
        else:
            try:
                summary = _do_rebuild(force=False)
                if summary.get("updated"):
                    log.info("auto-refresh: rebuilt (bake_id=%s)", summary.get("bake_id"))
                    _last_build.update({
                        "ok": True,
                        "at": summary.get("generated_at"),
                        "summary": summary,
                        "error": None,
                    })
                else:
                    log.info("auto-refresh: no upstream changes")
            except Exception as exc:  # noqa: BLE001
                log.exception("auto-refresh failed: %s", exc)
                _last_build.update({
                    "ok": False,
                    "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "error": str(exc),
                })
            finally:
                _rebuild_lock.release()
        time.sleep(AUTO_REFRESH_INTERVAL)


def _maybe_start_background_poller():
    if AUTO_REFRESH_INTERVAL <= 0:
        return
    t = threading.Thread(target=_background_poller, name="auto-refresh", daemon=True)
    t.start()


# Run startup hooks when imported by gunicorn / executed directly.
_maybe_bake_on_start()
_maybe_start_background_poller()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
