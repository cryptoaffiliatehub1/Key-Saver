"""
YouTube OAuth helper.

Uses the OAuth client in client_secret.json. The first time a user visits
/youtube/auth, we redirect them to Google. Google redirects back to
/youtube/callback on this same Flask app; we exchange the code and persist
token.json so future runs (and the scheduler) can post videos automatically.

Self-healing: if the stored token returns invalid_grant or is expired beyond
refresh, load_credentials() deletes the stale token.json and raises a clear
error — the dashboard will show a System Alert prompting re-authorisation.
"""
import json
import logging
import os
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

log = logging.getLogger("youtube_auth")

CLIENT_SECRET_FILE = Path("client_secret.json")
TOKEN_FILE = Path("token.json")
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


def _public_base_url() -> str:
    domain = os.environ.get("REPLIT_DEV_DOMAIN") or os.environ.get("REPLIT_DOMAINS", "").split(",")[0]
    if domain:
        return f"https://{domain}"
    return "http://localhost:5000"


def get_redirect_uri() -> str:
    return f"{_public_base_url()}/youtube/callback"


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


def has_client_secret() -> bool:
    return CLIENT_SECRET_FILE.exists() and CLIENT_SECRET_FILE.stat().st_size > 0


def has_token() -> bool:
    return TOKEN_FILE.exists() and TOKEN_FILE.stat().st_size > 0


def authorization_url() -> tuple[str, str, str]:
    flow = _build_flow()
    url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return url, state, getattr(flow, "code_verifier", "") or ""


def exchange_code(authorization_response_url: str, state: str, code_verifier: str | None = None) -> None:
    flow = _build_flow(state=state, code_verifier=code_verifier)
    flow.fetch_token(authorization_response=authorization_response_url)
    creds = flow.credentials
    TOKEN_FILE.write_text(creds.to_json())


def _is_grant_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in ("invalid_grant", "token has been expired", "token_expired",
                                   "token has been revoked", "invalid grant"))


def load_credentials() -> Credentials | None:
    """
    Load and auto-refresh stored credentials.

    Self-healing: if refresh fails with invalid_grant (revoked / expired
    beyond recovery), the stale token.json is deleted and a clear error is
    raised so the caller can surface a re-auth System Alert.
    """
    if not has_token():
        return None
    data = json.loads(TOKEN_FILE.read_text())
    creds = Credentials.from_authorized_user_info(data, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json())
        except (RefreshError, Exception) as exc:
            if _is_grant_error(exc):
                log.error("YouTube token is invalid/revoked — deleting token.json. Re-auth required. %s", exc)
                try:
                    TOKEN_FILE.unlink()
                except OSError:
                    pass
                raise RuntimeError(
                    "YouTube token expired and cannot be refreshed (invalid_grant). "
                    "Visit /youtube/auth to reconnect your channel."
                ) from exc
            raise
    return creds


def try_refresh_credentials() -> Credentials | None:
    """
    Attempt a silent credential refresh. Returns refreshed creds or None.
    Does NOT raise — logs the error and returns None on failure.
    """
    try:
        return load_credentials()
    except Exception as exc:
        log.warning("try_refresh_credentials failed: %s", exc)
        return None


def get_youtube_service():
    creds = load_credentials()
    if not creds:
        raise RuntimeError("YouTube is not authorized yet. Visit /youtube/auth to connect.")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def get_youtube_analytics_service():
    creds = load_credentials()
    if not creds:
        raise RuntimeError("YouTube is not authorized yet. Visit /youtube/auth to connect.")
    return build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)
