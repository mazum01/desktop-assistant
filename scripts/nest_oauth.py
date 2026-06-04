#!/usr/bin/env python3
"""Interactive OAuth2 setup helper for the Google Nest (SDM) integration.

Run this once to obtain a refresh_token and configure VERA:

    python3 scripts/nest_oauth.py

Prerequisites:
  1. Google Device Access project created (console.nest.google.com) — $5 one-time fee.
  2. OAuth2 Client ID + Secret created in Google Cloud Console (APIs & Services →
     Credentials → Create OAuth 2.0 Client ID; type = Web application;
     redirect URI = https://www.google.com).
  3. Smart Device Management API enabled for your Google Cloud project.

What this script does:
  - Builds the authorization URL and prompts you to open it in a browser.
  - Accepts the authorization code you paste back in.
  - Exchanges the code for a refresh_token.
  - Calls `vera iot config nest_thermostat …` to store the credentials in VERA.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.parse
import urllib.request

_TOKEN_URL  = "https://oauth2.googleapis.com/token"
_REDIRECT   = "https://www.google.com"
_SDM_SCOPE  = "https://www.googleapis.com/auth/sdm.service"

_BOLD  = "\033[1m"
_GREEN = "\033[32m"
_CYAN  = "\033[36m"
_RESET = "\033[0m"


def _banner(msg: str) -> None:
    print(f"\n{_BOLD}{_CYAN}{'─'*60}{_RESET}")
    print(f"{_BOLD}{_CYAN}{msg}{_RESET}")
    print(f"{_BOLD}{_CYAN}{'─'*60}{_RESET}\n")


def _ask(prompt: str, default: str = "") -> str:
    full_prompt = f"{_BOLD}{prompt}{_RESET}"
    if default:
        full_prompt += f" [{default}]"
    full_prompt += ": "
    val = input(full_prompt).strip()
    return val if val else default


def _exchange_code(client_id: str, client_secret: str, code: str) -> dict:
    payload = urllib.parse.urlencode({
        "client_id":     client_id,
        "client_secret": client_secret,
        "code":          code,
        "grant_type":    "authorization_code",
        "redirect_uri":  _REDIRECT,
    }).encode()
    req = urllib.request.Request(
        _TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    _banner("VERA — Google Nest OAuth2 Setup")

    print("You will need:")
    print("  • Your Google Device Access  Project ID")
    print("  • Your Google Cloud Console  OAuth2 Client ID")
    print("  • Your Google Cloud Console  OAuth2 Client Secret\n")

    # ── Gather credentials ────────────────────────────────────────────────────
    project_id    = _ask("Device Access Project ID",
                         default="a60fbc88-604f-412c-8a7d-21daa10183c7")
    client_id     = _ask("OAuth2 Client ID")
    client_secret = _ask("OAuth2 Client Secret")

    if not all([project_id, client_id, client_secret]):
        print("All three values are required.", file=sys.stderr)
        return 1

    # ── Build auth URL ────────────────────────────────────────────────────────
    auth_url = (
        f"https://nestservices.google.com/partnerconnections/{project_id}/auth?"
        + urllib.parse.urlencode({
            "redirect_uri":  _REDIRECT,
            "access_type":   "offline",
            "prompt":        "consent",
            "client_id":     client_id,
            "response_type": "code",
            "scope":         _SDM_SCOPE,
        })
    )

    _banner("Step 1 — Authorize in your browser")
    print("Open this URL in your browser:\n")
    print(f"  {_CYAN}{auth_url}{_RESET}\n")
    print("Click 'Allow', then copy the full URL you are redirected to.")
    print("It will look like:  https://www.google.com?code=4/0AX...&scope=...\n")

    # ── Accept redirected URL or bare code ───────────────────────────────────
    raw = _ask("Paste the redirect URL (or just the code= value)")
    if raw.startswith("http"):
        parsed = urllib.parse.urlparse(raw)
        code   = urllib.parse.parse_qs(parsed.query).get("code", [""])[0]
    else:
        code = raw.strip()

    if not code:
        print("Could not parse authorization code.", file=sys.stderr)
        return 1

    # ── Exchange for refresh token ────────────────────────────────────────────
    _banner("Step 2 — Exchanging code for refresh token…")
    try:
        tokens = _exchange_code(client_id, client_secret, code)
    except Exception as exc:
        print(f"Token exchange failed: {exc}", file=sys.stderr)
        return 1

    refresh_token = tokens.get("refresh_token", "")
    if not refresh_token:
        print(f"No refresh_token in response:\n{json.dumps(tokens, indent=2)}", file=sys.stderr)
        return 1

    print(f"{_GREEN}✓ Refresh token obtained.{_RESET}")

    # ── Configure VERA ────────────────────────────────────────────────────────
    _banner("Step 3 — Storing credentials in VERA…")
    cmd = [
        "python3", "scripts/desktop-assistant", "iot", "config", "nest_thermostat",
        f"project_id={project_id}",
        f"client_id={client_id}",
        f"client_secret={client_secret}",
        f"refresh_token={refresh_token}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"{_GREEN}✓ Credentials stored.{_RESET}")
    else:
        print(f"vera iot config failed:\n{result.stderr}", file=sys.stderr)
        print("\nRun this manually:")
        print(f"  vera iot config nest_thermostat \\")
        print(f"    project_id={project_id} \\")
        print(f"    client_id={client_id} \\")
        print(f"    client_secret={client_secret} \\")
        print(f"    refresh_token={refresh_token}")
        return 1

    # ── Restart daemon ────────────────────────────────────────────────────────
    _banner("Step 4 — Restarting VERA daemon…")
    r = subprocess.run(
        ["sudo", "systemctl", "restart", "desktop-assistant-core.service"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        print(f"{_GREEN}✓ Daemon restarted. Nest card will appear in the web GUI within ~60 s.{_RESET}\n")
    else:
        print("Could not restart daemon automatically. Run:")
        print("  sudo systemctl restart desktop-assistant-core.service\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
