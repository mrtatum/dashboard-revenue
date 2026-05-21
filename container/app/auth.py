"""
Microsoft Graph token provider — refresh-token flow.

The container is configured with:
  MS_CLIENT_ID       App (client) ID of the Entra/Azure-AD app you registered.
  MS_TENANT          OAuth authority. One of:
                       * a tenant GUID (e.g. 12345678-aaaa-...)
                       * a verified tenant domain (e.g. contoso.onmicrosoft.com)
                       * 'organizations'  — any Entra ID tenant
                       * 'consumers'      — OneDrive Personal (Microsoft accounts)
                       * 'common'         — either (auto-detected at sign-in)
                     For OneDrive for Business, pass your tenant GUID or domain.
  MS_REFRESH_TOKEN   Long-lived refresh token obtained once via the
                     `tools/get_refresh_token.py` device-code flow.

This module:
  * Exchanges the refresh token for a short-lived access token (~1 hour)
  * Caches the access token in memory until it's near expiry
  * Writes any rotated refresh token to /app/secrets/refresh_token so a
    container restart picks up the freshest token automatically
  * Restricts the OAuth scope to `Files.Read offline_access` — the minimum
    that lets us read files plus get a refreshable token

The scope is a *Graph* scope, not a folder scope: the token's *identity*
defines what it can see. Folder isolation is enforced separately, by signing
in with an account that only has the target folder shared with it.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import threading
import time
from typing import Optional

import requests

log = logging.getLogger("auth")

# OAuth endpoint. The {tenant} segment selects which AAD authority handles the
# request: a tenant GUID/domain for OneDrive for Business; 'consumers' for
# OneDrive Personal; 'common' or 'organizations' for either.
_TOKEN_URL_TMPL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

# Minimum scope: read files + obtain a refresh token.
DEFAULT_SCOPE = "Files.Read offline_access"

# Where rotated refresh tokens are persisted (mount this as a volume in prod).
DEFAULT_SECRETS_PATH = pathlib.Path(os.environ.get("MS_SECRETS_PATH", "/app/secrets/refresh_token"))


class GraphAuthError(RuntimeError):
    pass


class GraphTokenProvider:
    """Owns the access-token lifecycle for one identity."""

    def __init__(
        self,
        client_id: str,
        refresh_token: str,
        tenant: str,
        scope: str = DEFAULT_SCOPE,
        secrets_path: Optional[pathlib.Path] = DEFAULT_SECRETS_PATH,
        session: Optional[requests.Session] = None,
    ):
        if not client_id:
            raise GraphAuthError("MS_CLIENT_ID is required")
        if not refresh_token:
            raise GraphAuthError("MS_REFRESH_TOKEN is required")
        if not tenant:
            raise GraphAuthError(
                "MS_TENANT is required. Use your tenant GUID/domain for OneDrive "
                "for Business, 'consumers' for OneDrive Personal, or 'common' "
                "to support either."
            )
        self.client_id = client_id
        self.tenant = tenant
        self.scope = scope
        self._refresh_token = refresh_token
        self._secrets_path = secrets_path
        self._session = session or requests.Session()
        self._lock = threading.Lock()
        self._access_token: Optional[str] = None
        self._access_expiry: float = 0.0
        # Prefer the persisted refresh token if it's newer than the env value.
        self._load_persisted_refresh_token()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def access_token(self) -> str:
        """Return a valid access token, refreshing if needed."""
        with self._lock:
            now = time.time()
            # Refresh 60s before actual expiry to avoid races.
            if self._access_token and now < self._access_expiry - 60:
                return self._access_token
            self._refresh()
            assert self._access_token is not None
            return self._access_token

    def authorized_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token()}"}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        url = _TOKEN_URL_TMPL.format(tenant=self.tenant)
        data = {
            "client_id": self.client_id,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "scope": self.scope,
        }
        log.info("auth: refreshing access token (tenant=%s)", self.tenant)
        r = self._session.post(url, data=data, timeout=30)
        if r.status_code != 200:
            raise GraphAuthError(
                f"token refresh failed ({r.status_code}): {r.text[:400]}"
            )
        body = r.json()
        self._access_token = body["access_token"]
        self._access_expiry = time.time() + int(body.get("expires_in", 3600))
        # Refresh tokens may rotate; persist the new one if so.
        new_rt = body.get("refresh_token")
        if new_rt and new_rt != self._refresh_token:
            self._refresh_token = new_rt
            self._persist_refresh_token(new_rt)
        log.info("auth: token refreshed, expires in ~%ds", int(body.get("expires_in", 3600)))

    def _load_persisted_refresh_token(self) -> None:
        if not self._secrets_path:
            return
        try:
            if self._secrets_path.exists():
                persisted = self._secrets_path.read_text(encoding="utf-8").strip()
                if persisted:
                    log.info("auth: loaded persisted refresh token from %s", self._secrets_path)
                    self._refresh_token = persisted
        except OSError as exc:
            log.warning("auth: cannot read persisted refresh token: %s", exc)

    def _persist_refresh_token(self, token: str) -> None:
        if not self._secrets_path:
            return
        try:
            self._secrets_path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write — temp file then rename.
            tmp = self._secrets_path.with_suffix(self._secrets_path.suffix + ".tmp")
            tmp.write_text(token, encoding="utf-8")
            os.replace(tmp, self._secrets_path)
            try:
                os.chmod(self._secrets_path, 0o600)
            except OSError:
                pass
            log.info("auth: rotated refresh token persisted to %s", self._secrets_path)
        except OSError as exc:
            log.warning(
                "auth: cannot persist rotated refresh token to %s (%s) — "
                "will keep using it in memory until container restart",
                self._secrets_path, exc,
            )


def from_env() -> GraphTokenProvider:
    """Build a provider from MS_* environment variables."""
    return GraphTokenProvider(
        client_id=os.environ.get("MS_CLIENT_ID", "").strip(),
        refresh_token=os.environ.get("MS_REFRESH_TOKEN", "").strip(),
        tenant=os.environ.get("MS_TENANT", "").strip(),
        scope=os.environ.get("MS_SCOPE", DEFAULT_SCOPE).strip() or DEFAULT_SCOPE,
    )
