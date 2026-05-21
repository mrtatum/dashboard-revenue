"""
OneDrive client — *authenticated*, folder-scoped reads via Microsoft Graph.

This module never references `/me/drive`, `/drive/root`, or the user's
folder path. Every call is constructed as

    /drives/{DRIVE_ID}/items/{ITEM_ID}/...

where DRIVE_ID and ITEM_ID are the env-configured target folder. That keeps
the container blast radius limited to one folder subtree at the *code* level.

True isolation comes from the *identity* the refresh token belongs to:
sign in during the one-time setup with an account that has only the target
folder shared with it, and the token literally cannot see anything else.
"""

from __future__ import annotations

import logging
import os
import pathlib
import time
from dataclasses import dataclass
from typing import Iterable, Optional

import requests

from .auth import GraphTokenProvider

log = logging.getLogger("onedrive")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


@dataclass
class DownloadResult:
    downloaded: int
    skipped: int
    total_bytes: int
    files: list[str]


@dataclass
class Watermark:
    """A snapshot of "how fresh is the upstream folder?" — no downloads."""
    max_modified: str          # ISO timestamp string, "" if folder is empty
    file_count: int
    total_bytes: int

    def signature(self) -> str:
        """A short string that changes iff the watermark would trigger a rebuild."""
        return f"{self.max_modified}|{self.file_count}|{self.total_bytes}"

    def to_dict(self) -> dict:
        return {
            "max_modified": self.max_modified,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "signature": self.signature(),
        }


class ScopedOneDrive:
    """All operations are confined to a single (drive_id, item_id) subtree."""

    def __init__(
        self,
        token_provider: GraphTokenProvider,
        drive_id: str,
        item_id: str,
        session: Optional[requests.Session] = None,
    ):
        if not drive_id or not item_id:
            raise ValueError("drive_id and item_id are required")
        self.tokens = token_provider
        self.drive_id = drive_id
        self.item_id = item_id
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "DashboardRevenue/2.0"})

    # ------------------------------------------------------------------
    # HTTP helpers — every URL is built under /drives/{drive_id}/items/{item_id}
    # ------------------------------------------------------------------

    def _item_path(self, item_id: str) -> str:
        return f"/drives/{self.drive_id}/items/{item_id}"

    def _get(self, path_or_url: str) -> dict:
        # Accept both absolute Graph URLs (for @odata.nextLink) and relative
        # paths. Relative paths MUST begin with /drives/{self.drive_id}/items/
        # — anything else is a bug and we refuse it.
        if path_or_url.startswith("http"):
            url = path_or_url
            if not url.startswith(f"{GRAPH_BASE}/drives/{self.drive_id}/items/"):
                raise ValueError(f"refusing out-of-scope URL: {url}")
        else:
            if not path_or_url.startswith(f"/drives/{self.drive_id}/items/"):
                raise ValueError(f"refusing out-of-scope path: {path_or_url}")
            url = f"{GRAPH_BASE}{path_or_url}"
        r = self.session.get(url, headers=self.tokens.authorized_headers(), timeout=60)
        if r.status_code == 401:
            # Token may have just expired; one retry after forcing refresh.
            r = self.session.get(url, headers=self.tokens.authorized_headers(), timeout=60)
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------
    # Walk + download
    # ------------------------------------------------------------------

    def root_item(self) -> dict:
        return self._get(self._item_path(self.item_id))

    def list_children(self, item_id: str) -> Iterable[dict]:
        url = f"{self._item_path(item_id)}/children"
        while url:
            data = self._get(url) if url.startswith("/") else self._get(url)
            for child in data.get("value", []):
                yield child
            url = data.get("@odata.nextLink")  # absolute URL — _get checks scope

    def walk(self) -> Iterable[tuple[str, dict]]:
        """Yield (relative_path, driveItem) for every file under the scoped folder."""
        stack: list[tuple[str, str]] = [("", self.item_id)]
        while stack:
            prefix, item_id = stack.pop()
            for child in self.list_children(item_id):
                rel = f"{prefix}/{child['name']}" if prefix else child["name"]
                if "folder" in child:
                    stack.append((rel, child["id"]))
                else:
                    yield (rel, child)

    def get_watermark(self, include_ext: tuple[str, ...] = (".xlsx",)) -> Watermark:
        """Walk the folder via Graph and summarize 'is the source folder newer?'.

        Returns max(lastModifiedDateTime) across files matching include_ext,
        plus the count and total size. No file contents are downloaded —
        only the listing is fetched, so this is cheap.
        """
        max_mod = ""
        count = 0
        total = 0
        for rel_path, item in self.walk():
            if include_ext and not rel_path.lower().endswith(include_ext):
                continue
            count += 1
            total += int(item.get("size", 0) or 0)
            mod = item.get("lastModifiedDateTime") or ""
            if mod > max_mod:
                max_mod = mod
        return Watermark(max_modified=max_mod, file_count=count, total_bytes=total)

    def download_to(
        self,
        target_dir: str | os.PathLike,
        include_ext: tuple[str, ...] = (".xlsx",),
        skip_unchanged: bool = True,
    ) -> DownloadResult:
        target = pathlib.Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        downloaded = skipped = 0
        total_bytes = 0
        files: list[str] = []
        for rel_path, item in self.walk():
            if include_ext and not rel_path.lower().endswith(include_ext):
                continue
            dest = target / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            size = item.get("size", 0)
            if skip_unchanged and dest.exists() and dest.stat().st_size == size and size > 0:
                skipped += 1
                files.append(rel_path)
                continue
            dl_url = item.get("@microsoft.graph.downloadUrl")
            if dl_url:
                # Pre-authed redirect — safe and quota-friendly.
                self._stream_to_file_unauth(dl_url, dest)
            else:
                # Fallback: /content on the scoped path
                content_path = f"{self._item_path(item['id'])}/content"
                self._stream_to_file_auth(content_path, dest)
            downloaded += 1
            total_bytes += dest.stat().st_size
            files.append(rel_path)
        return DownloadResult(
            downloaded=downloaded,
            skipped=skipped,
            total_bytes=total_bytes,
            files=sorted(files),
        )

    def _stream_to_file_unauth(self, url: str, dest: pathlib.Path, retries: int = 3) -> None:
        # @microsoft.graph.downloadUrl is pre-signed; no Authorization needed.
        self._stream(url, dest, headers=None, retries=retries)

    def _stream_to_file_auth(self, scoped_path: str, dest: pathlib.Path, retries: int = 3) -> None:
        if not scoped_path.startswith(f"/drives/{self.drive_id}/items/"):
            raise ValueError(f"refusing out-of-scope path: {scoped_path}")
        url = f"{GRAPH_BASE}{scoped_path}"
        self._stream(url, dest, headers=self.tokens.authorized_headers(), retries=retries)

    def _stream(self, url: str, dest: pathlib.Path, headers, retries: int) -> None:
        backoff = 1.0
        for attempt in range(retries):
            try:
                with self.session.get(url, stream=True, timeout=120, headers=headers) as r:
                    r.raise_for_status()
                    tmp = dest.with_suffix(dest.suffix + ".part")
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(chunk_size=64 * 1024):
                            if chunk:
                                f.write(chunk)
                    os.replace(tmp, dest)
                return
            except (requests.RequestException, OSError):
                if attempt == retries - 1:
                    raise
                time.sleep(backoff)
                backoff *= 2


def from_env(token_provider: GraphTokenProvider) -> ScopedOneDrive:
    drive_id = os.environ.get("ONEDRIVE_DRIVE_ID", "").strip()
    item_id = os.environ.get("ONEDRIVE_ITEM_ID", "").strip()
    if not drive_id or not item_id:
        raise RuntimeError(
            "ONEDRIVE_DRIVE_ID and ONEDRIVE_ITEM_ID are required. "
            "Run tools/get_refresh_token.py to obtain them for your folder."
        )
    return ScopedOneDrive(token_provider, drive_id=drive_id, item_id=item_id)
