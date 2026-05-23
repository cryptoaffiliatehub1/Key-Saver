"""
Google Sign-In OAuth helper.

Uses the same client_secret.json as YouTube but requests only profile/email
scopes so the user can log in with their Google account.

Flow:
  1. /auth/google         → redirect to Google consent screen
  2. /auth/google/callback → exchange code, fetch profile, return dict

PKCE fix: authorization_url() now returns (url, state, code_verifier).
The route stores all three in the server-side session. The callback
retrieves and passes code_verifier to exchange_code_and_get_profile()
so the token exchange never fails with "missing code verifier".
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests as _requests
from google_auth_oauthlib.flow import Flow

CLIENT_SECRET_FILE = Path("client_secret.json")

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


def _public_base_url() -> str:
    domain = (
        os.environ.get("REPLIT_DEV_DOMAIN")
        or os.environ.get("REPLIT_DOMAINS", "").split(",")[0]
    )
    return f"https://{domain}" if domain else "http://localhost:5000"


def get_redirect_uri() -> str:
    return f"{_public_base_url()}/auth/google/callback"


def has_client_secret() -> bool:
    return CLIENT_SECRET_FILE.exists() and CLIENT_SECRET_FILE.stat().st_size > 0


def _build_flow(state: str | None = None, code_verifier: str | None = None) -> Flow:
    flow = Flow.from_client_secrets_file(
        str(CLIENT_SECRET_FILE),
        scopes=SCOPES,
        state=state,
    )
    flow.redirect_uri = get_redirect_uri()
    if code_verifier:
        flow.code_verifier = code_verifier
    return flow


def authorization_url() -> tuple[str, str, str]:
    """
    Returns (auth_url, state, code_verifier).

    code_verifier may be an empty string if the underlying library does not
    generate one — callers must store it in the session regardless so the
    callback can pass it back correctly.
    """
    flow = _build_flow()
    url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="false",
        prompt="select_account",
    )
    code_verifier: str = getattr(flow, "code_verifier", "") or ""
    return url, state, code_verifier


def exchange_code_and_get_profile(
    authorization_response_url: str,
    state: str,
    code_verifier: str | None = None,
) -> dict[str, Any]:
    """
    Exchange the auth code and fetch the user's Google profile.
    Returns a dict with keys: sub, email, name, picture.

    Pass code_verifier (retrieved from the session) so that oauthlib can
    complete the PKCE handshake even when it generated the verifier
    automatically during the authorization step.
    """
    if authorization_response_url.startswith("http://"):
        authorization_response_url = "https://" + authorization_response_url[len("http://"):]

    flow = _build_flow(state=state, code_verifier=code_verifier or None)
    flow.fetch_token(authorization_response=authorization_response_url)
    creds = flow.credentials

    resp = _requests.get(
        USERINFO_URL,
        headers={"Authorization": f"Bearer {creds.token}"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()
