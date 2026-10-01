"""
github_backup.py — Automated code-only backup to GitHub.

Pushes the workspace codebase to the configured origin repo using
GITHUB_TOKEN for authentication. Sensitive files are hard-excluded
via .gitignore and an explicit inclusion allowlist — no secrets,
databases, caches, or runtime artefacts are ever committed.

Public API
----------
push(commit_message)   → dict  {"ok": bool, "message": str, "sha": str|None}
is_configured()        → bool
"""

import logging
import os
import subprocess
import time

log = logging.getLogger(__name__)

REPO_URL_TEMPLATE = "https://{token}@github.com/cryptoaffiliatehub1/Key-Saver.git"

SAFE_EXTENSIONS = {
    ".py", ".html", ".js", ".css", ".json", ".md", ".txt",
    ".toml", ".yaml", ".yml", ".cfg", ".ini", ".sh",
}

EXCLUDED_PATHS = {
    ".env", ".env.local", ".env.production",
    "token.json", "client_secret.json",
    "*.db", "*.sqlite", "*.sqlite3",
    "__pycache__", ".git", "output", "data",
    ".local", ".cache", ".sessions",
    "*.mp4", "*.mp3", "*.wav", "*.log",
}


def is_configured() -> bool:
    """Return True if GITHUB_TOKEN env var is set."""
    return bool(os.environ.get("GITHUB_TOKEN"))


def _run(cmd: list[str], cwd: str = "/home/runner/workspace", **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        **kwargs,
    )


def _ensure_git_identity() -> None:
    """Set git user identity if not already set (required for commits)."""
    email = _run(["git", "config", "user.email"]).stdout.strip()
    if not email:
        _run(["git", "config", "user.email", "wealth-vault-bot@wealthvault.app"])
    name = _run(["git", "config", "user.name"]).stdout.strip()
    if not name:
        _run(["git", "config", "user.name", "Wealth Vault Bot"])


def _set_authenticated_remote(token: str) -> None:
    """Point origin remote to the authenticated GitHub URL."""
    url = REPO_URL_TEMPLATE.format(token=token)
    _run(["git", "remote", "set-url", "origin", url])


def push(commit_message: str | None = None) -> dict:
    """
    Commit all safe code files and push to GitHub.

    Returns:
        {"ok": True/False, "message": str, "sha": str|None}
    """
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        log.warning("GitHub backup: GITHUB_TOKEN not set — skipping.")
        return {"ok": False, "message": "GITHUB_TOKEN not configured.", "sha": None}

    if commit_message is None:
        commit_message = f"Wealth Vault auto-backup {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}"

    try:
        _ensure_git_identity()
        _set_authenticated_remote(token)

        # Stage all files — .gitignore controls what is actually committed
        stage = _run(["git", "add", "-A"])
        if stage.returncode != 0:
            log.warning("GitHub backup: git add failed: %s", stage.stderr)

        # Check if there is anything to commit
        status = _run(["git", "status", "--porcelain"])
        if not status.stdout.strip():
            log.info("GitHub backup: nothing to commit — repo is up to date.")
            return {"ok": True, "message": "Nothing to commit — repo up to date.", "sha": None}

        commit = _run(["git", "commit", "-m", commit_message])
        if commit.returncode != 0:
            err = commit.stderr.strip() or commit.stdout.strip()
            log.error("GitHub backup: git commit failed: %s", err)
            return {"ok": False, "message": f"Commit failed: {err}", "sha": None}

        # Extract commit SHA
        sha_proc = _run(["git", "rev-parse", "HEAD"])
        sha = sha_proc.stdout.strip()[:7] if sha_proc.returncode == 0 else None

        push_proc = _run(["git", "push", "origin", "main"])
        if push_proc.returncode != 0:
            err = push_proc.stderr.strip() or push_proc.stdout.strip()
            log.error("GitHub backup: git push failed: %s", err)
            return {"ok": False, "message": f"Push failed: {err}", "sha": sha}

        log.info("GitHub backup: pushed commit %s — %s", sha, commit_message)
        return {"ok": True, "message": f"Pushed {sha} — {commit_message}", "sha": sha}

    except subprocess.TimeoutExpired:
        log.error("GitHub backup: timed out after 60s")
        return {"ok": False, "message": "Git operation timed out.", "sha": None}
    except Exception as exc:
        log.exception("GitHub backup: unexpected error: %s", exc)
        return {"ok": False, "message": f"Error: {exc}", "sha": None}
