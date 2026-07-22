"""One-time helper: mint the Gmail refresh token for the reply scanner.

Prereq: a Google Cloud OAuth client of type "Desktop app" (see
docs/operations.md), with GMAIL_CLIENT_ID + GMAIL_CLIENT_SECRET in .env
(or entered at the prompt).

Usage:  python backend/gmail_auth.py

Opens your browser to a Google consent screen asking for READ-ONLY Gmail
access, catches the redirect on localhost, and prints the
GMAIL_REFRESH_TOKEN line to paste into .env and Railway variables.
"""
import http.server
import os
import secrets
import urllib.parse
import webbrowser

import httpx
from dotenv import load_dotenv

load_dotenv()

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
PORT = 8912  # loopback redirect; Desktop-app clients accept any localhost port


def main():
    client_id = os.environ.get("GMAIL_CLIENT_ID") or input("GMAIL_CLIENT_ID: ").strip()
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET") or input("GMAIL_CLIENT_SECRET: ").strip()

    redirect_uri = f"http://localhost:{PORT}/"
    state = secrets.token_urlsafe(16)
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",   # → refresh token
        "prompt": "consent",        # force a fresh refresh token every run
        "state": state,
    })

    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            ok = qs.get("state", [""])[0] == state and "code" in qs
            if ok:
                result["code"] = qs["code"][0]
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<h3>Done &mdash; return to the terminal.</h3>" if ok
                else b"<h3>Missing/invalid code &mdash; try again.</h3>"
            )

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("localhost", PORT), Handler)
    print("Opening your browser for Google consent (read-only Gmail)...")
    print("If it doesn't open, visit:\n" + auth_url + "\n")
    webbrowser.open(auth_url)
    while "code" not in result:
        server.handle_request()
    server.server_close()

    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": result["code"],
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }, timeout=30)
    r.raise_for_status()
    tokens = r.json()
    refresh = tokens.get("refresh_token")
    if not refresh:
        raise SystemExit(
            "Google returned no refresh_token (response: "
            f"{list(tokens)}). Re-run — 'prompt=consent' should force one."
        )

    print("\nAdd this to .env (local) and the Railway service variables:\n")
    print(f"GMAIL_REFRESH_TOKEN={refresh}")
    print("\n(The token grants READ-ONLY Gmail access and can be revoked any"
          " time at https://myaccount.google.com/permissions)")


if __name__ == "__main__":
    main()
