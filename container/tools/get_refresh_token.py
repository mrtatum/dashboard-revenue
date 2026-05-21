"""
One-time setup CLI — works for OneDrive Personal and OneDrive for Business.

Run on your Mac, sign in as the Microsoft account that owns (or has been
shared with) the target folder, then paste the printed env block into
container/.env.

Prerequisites
-------------
1. Register an Entra (Azure AD) app at https://entra.microsoft.com →
   Identity → Applications → App registrations → New registration.
     * Name: anything (e.g. "Dashboard Revenue Reader")
     * Supported account types:
         - OneDrive Personal:   "Personal Microsoft accounts only"
         - OneDrive for Business (single tenant):
                                "Accounts in this organizational directory only"
         - Either:              "...any organizational directory and personal
                                 Microsoft accounts"
     * Redirect URI: leave blank.
     * After creation → Authentication → Advanced settings → "Allow public
       client flows" = YES (device-code flow needs this).
2. Note the Application (client) ID — that's MS_CLIENT_ID.
3. API permissions → Add a permission → Microsoft Graph → Delegated permissions
   → tick `Files.Read` and `offline_access`.
     * Personal: no admin consent needed.
     * Business: if your tenant requires admin consent for Files.Read, ask
       your admin to "Grant admin consent" on the API permissions page.
4. In OneDrive web, share the target folder with the account you'll sign in
   as, with **Can view** permission.

Then run:

    # OneDrive for Business (tenant GUID or domain):
    python3 container/tools/get_refresh_token.py \\
        --client-id <MS_CLIENT_ID> \\
        --tenant    contoso.onmicrosoft.com \\
        --share-url '<your OneDrive share URL>'

    # OneDrive Personal:
    python3 container/tools/get_refresh_token.py \\
        --client-id <MS_CLIENT_ID> \\
        --tenant    consumers \\
        --share-url 'https://1drv.ms/f/c/.../...?e=...'
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


SCOPE = "Files.Read offline_access"

DEVICE_CODE_URL_TMPL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/devicecode"
TOKEN_URL_TMPL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

GRAPH = "https://graph.microsoft.com/v1.0"


def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode("ascii")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:  # noqa: BLE001
            raise


def _get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def device_login(client_id: str, tenant: str) -> dict:
    print(f"\n→ Requesting device code (tenant={tenant})…")
    dc = _post_form(DEVICE_CODE_URL_TMPL.format(tenant=tenant),
                    {"client_id": client_id, "scope": SCOPE})
    if "user_code" not in dc:
        raise SystemExit(f"Device code request failed: {dc}")
    print("\n" + "=" * 60)
    print(dc.get("message") or
          f"Open {dc.get('verification_uri')} and enter code: {dc['user_code']}")
    print("=" * 60 + "\n")
    interval = int(dc.get("interval", 5))
    expires = time.time() + int(dc.get("expires_in", 900))
    while True:
        time.sleep(interval)
        if time.time() > expires:
            raise SystemExit("Device code expired before sign-in completed.")
        tok = _post_form(TOKEN_URL_TMPL.format(tenant=tenant), {
            "client_id": client_id,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": dc["device_code"],
        })
        if "access_token" in tok:
            return tok
        err = tok.get("error", "")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        if err in ("expired_token", "authorization_declined", "bad_verification_code"):
            raise SystemExit(f"Sign-in failed: {err} — {tok.get('error_description', '')}")
        raise SystemExit(f"Sign-in error: {tok}")


def resolve_share(token: str, share_url: str) -> dict:
    enc = "u!" + base64.urlsafe_b64encode(share_url.encode("utf-8")).decode("ascii").rstrip("=")
    return _get_json(f"{GRAPH}/shares/{enc}/driveItem", token)


def walk_folder(token: str, drive_id: str, item_id: str):
    """Yield (relative_path, driveItem) for every file under drive_id/item_id.

    Uses ONLY /drives/{drive_id}/items/{item_id}/... — exactly the same path
    surface the container is hard-coded to. Whatever this prints is exactly
    what the container will see.
    """
    stack = [("", item_id)]
    while stack:
        prefix, iid = stack.pop()
        url = f"{GRAPH}/drives/{drive_id}/items/{iid}/children"
        while url:
            data = _get_json(url, token)
            for child in data.get("value", []):
                rel = f"{prefix}/{child['name']}" if prefix else child["name"]
                if "folder" in child:
                    stack.append((rel, child["id"]))
                else:
                    yield rel, child
            url = data.get("@odata.nextLink")


def verify_access(token: str, drive_id: str, item_id: str, folder_name: str) -> bool:
    """Walk the folder and report what the container will see. Returns True if
    the layout looks bake-ready (a Pipeline xlsx and at least one SQ/YR* folder)."""
    print(f"\n→ Verifying container-eye-view of '{folder_name}'…")
    has_pipeline = False
    sq_years: set[str] = set()
    xlsx_paths: list[tuple[str, int]] = []
    total_files = 0
    try:
        for rel, item in walk_folder(token, drive_id, item_id):
            total_files += 1
            size = int(item.get("size", 0) or 0)
            low = rel.lower().replace("\\", "/")
            if low.endswith(".xlsx"):
                xlsx_paths.append((rel, size))
                if "pipeline" in low.rsplit("/", 1)[-1]:
                    has_pipeline = True
                for part in low.split("/"):
                    if part.startswith("yr") and part[2:].isdigit():
                        sq_years.add(part.upper())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:400]
        except Exception:  # noqa: BLE001
            pass
        print(f"WALK FAILED: HTTP {e.code} {e.reason}\n{body}", file=sys.stderr)
        return False

    print(f"  total files seen:     {total_files}")
    print(f"  xlsx files:           {len(xlsx_paths)}")
    print(f"  Pipeline xlsx found:  {'YES' if has_pipeline else 'no'}")
    print(f"  SQ year folders:      {sorted(sq_years) if sq_years else '(none)'}")
    if xlsx_paths:
        print("  first 10 xlsx paths:")
        for rel, size in xlsx_paths[:10]:
            print(f"    {rel}   ({size/1024:.0f} KB)")
        if len(xlsx_paths) > 10:
            print(f"    … and {len(xlsx_paths) - 10} more")

    ok = has_pipeline and bool(sq_years)
    print("\n  VERDICT: " + ("PASS — the container will be able to bake the dashboard from this folder."
                              if ok else
                              "PARTIAL — folder is reachable but the expected "
                              "Pipeline/<xlsx> + SQ/YR<YYYY>/<xlsx> layout is missing. "
                              "The container will run, /rebuild will succeed, but bake "
                              "will produce empty pipeline/orders until the layout matches."))
    return ok


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Get a refresh token + folder IDs for the dashboard "
                    "container (OneDrive Personal or for Business)."
    )
    ap.add_argument("--client-id", required=True,
                    help="MS_CLIENT_ID from your Entra app registration.")
    ap.add_argument("--tenant", default="common",
                    help="OAuth authority. Business: tenant GUID or domain "
                         "(e.g. contoso.onmicrosoft.com). Personal: 'consumers'. "
                         "Either: 'common' (default).")
    ap.add_argument("--share-url", required=True,
                    help="OneDrive share URL pointing at the target folder. "
                         "Used only to resolve driveId/itemId — the container "
                         "itself uses the IDs, not the share URL.")
    args = ap.parse_args(argv)

    tok = device_login(args.client_id, args.tenant)
    access_token = tok["access_token"]
    refresh_token = tok.get("refresh_token")
    if not refresh_token:
        raise SystemExit(
            "Sign-in succeeded but no refresh_token was returned. "
            "Make sure 'offline_access' is in your app's API permissions."
        )

    print("→ Resolving share URL to driveId/itemId…")
    try:
        item = resolve_share(access_token, args.share_url)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:400]
        except Exception:  # noqa: BLE001
            pass
        raise SystemExit(f"Share resolution failed: HTTP {e.code} {e.reason}\n{body}")

    drive_id = item.get("parentReference", {}).get("driveId") or item.get("driveId")
    item_id = item.get("id")
    name = item.get("name")
    is_folder = "folder" in item
    child_count = item.get("folder", {}).get("childCount") if is_folder else None
    if not drive_id or not item_id:
        raise SystemExit(f"Couldn't extract drive/item IDs from response: {item}")
    if not is_folder:
        print("WARNING: the share URL resolved to a FILE, not a folder. "
              "Share the parent folder instead, then re-run.", file=sys.stderr)

    print("\n" + "=" * 60)
    print(" SUCCESS — paste these into container/.env")
    print("=" * 60)
    print(f"MS_CLIENT_ID={args.client_id}")
    print(f"MS_TENANT={args.tenant}")
    print(f"MS_REFRESH_TOKEN={refresh_token}")
    print(f"ONEDRIVE_DRIVE_ID={drive_id}")
    print(f"ONEDRIVE_ITEM_ID={item_id}")
    print("=" * 60)
    print(f"\nFolder resolved: '{name}'"
          + (f"  ({child_count} children)" if child_count is not None else ""))

    # Container-eye-view check: walk the folder via the same Graph paths the
    # container will use, and report what it can see.
    if is_folder:
        verify_access(access_token, drive_id, item_id, name or "<folder>")

    print("\nKeep MS_REFRESH_TOKEN secret. Lifetime varies:"
          "\n  * OneDrive Personal: ~90 days of inactivity, rotates on each refresh."
          "\n  * OneDrive for Business: typically 90 days, but your tenant's "
          "Conditional Access policy may shorten this."
          "\nRe-run this script any time to issue a fresh token.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
