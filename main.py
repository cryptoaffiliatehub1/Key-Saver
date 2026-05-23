import functools
import json
import os
import re
import secrets
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

import google.generativeai as genai
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template_string,
    request,
    send_from_directory,
    session,
    url_for,
)
from gtts import gTTS
from werkzeug.security import check_password_hash, generate_password_hash

import ab_tester
import affiliate_comments
import audio_engine
import dashboard
import google_auth
import retention_engine
import trend_hunter
import uploader
import viral_engine
import youtube_auth

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET")
if not app.secret_key:
    raise RuntimeError("SESSION_SECRET must be set before starting the app.")

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)
PREFERRED_GEMINI_MODELS = ["gemini-1.5-flash-latest", "gemini-flash-latest", "gemini-2.0-flash"]

VAULT_QUOTES = [
    "Wealth is a silent master. Welcome to the Vault.",
    "The elite don't chase money. Money chases the elite.",
    "Information is the new oil. You are refining it.",
    "While they sleep, the Vault compounds.",
    "Power is not given. It is engineered.",
    "The algorithm bows to those who understand it.",
    "Your audience is infinite. Your excuses are not.",
    "Silence is the strategy of the wealthy.",
    "Every upload is a brick in your empire.",
    "The world rewards those who show up daily.",
]

# ─────────────────────────────────────────────
#  AUTH — user store + helpers
# ─────────────────────────────────────────────
USERS_FILE = Path("data/users.json")
USERS_FILE.parent.mkdir(exist_ok=True)


def _load_users() -> list[dict]:
    if not USERS_FILE.exists() or USERS_FILE.stat().st_size == 0:
        return []
    try:
        return json.loads(USERS_FILE.read_text())
    except json.JSONDecodeError:
        return []


def _save_users(users: list[dict]) -> None:
    USERS_FILE.write_text(json.dumps(users, indent=2))


def _find_user(username: str) -> dict | None:
    for u in _load_users():
        if u["username"].lower() == username.lower():
            return u
    return None


def _auto_role() -> str:
    """First user ever → admin. Everyone else → viewer."""
    return "admin" if not _load_users() else "viewer"


def _create_user(username: str, password: str, role: str | None = None) -> bool:
    """Returns False if username already taken."""
    users = _load_users()
    if any(u["username"].lower() == username.lower() for u in users):
        return False
    users.append({
        "username": username,
        "password_hash": generate_password_hash(password),
        "role": role or _auto_role(),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    _save_users(users)
    return True


def _find_user_by_google_id(google_id: str) -> dict | None:
    for u in _load_users():
        if u.get("google_id") == google_id:
            return u
    return None


def _upsert_google_user(google_id: str, email: str, name: str, picture: str) -> dict:
    """Find or create a user linked to a Google account. Returns the user dict."""
    users = _load_users()
    # Match by google_id first, then by email
    for u in users:
        if u.get("google_id") == google_id:
            u["name"] = name
            u["picture"] = picture
            _save_users(users)
            return u
    for u in users:
        if u.get("email", "").lower() == email.lower():
            u["google_id"] = google_id
            u["name"] = name
            u["picture"] = picture
            _save_users(users)
            return u
    # New user — derive username from email local part
    base = email.split("@")[0].replace(".", "_").lower()
    username = base
    taken = {u["username"].lower() for u in users}
    counter = 2
    while username in taken:
        username = f"{base}{counter}"
        counter += 1
    role = "admin" if not users else "viewer"
    new_user = {
        "username": username,
        "email": email,
        "name": name,
        "picture": picture,
        "google_id": google_id,
        "password_hash": None,
        "role": role,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    users.append(new_user)
    _save_users(users)
    return new_user


def _promote_to_admin(username: str) -> bool:
    users = _load_users()
    for u in users:
        if u["username"].lower() == username.lower():
            u["role"] = "admin"
            _save_users(users)
            return True
    return False


# ─── Invite store ───────────────────────────────────────────────────────────
INVITES_FILE = Path("data/invites.json")
SETTINGS_FILE = Path("data/vault_settings.json")


def _load_invites() -> list[dict]:
    if not INVITES_FILE.exists() or INVITES_FILE.stat().st_size == 0:
        return []
    try:
        return json.loads(INVITES_FILE.read_text())
    except json.JSONDecodeError:
        return []


def _save_invites(invites: list[dict]) -> None:
    INVITES_FILE.write_text(json.dumps(invites, indent=2))


def _load_settings() -> dict:
    if not SETTINGS_FILE.exists() or SETTINGS_FILE.stat().st_size == 0:
        return {}
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except json.JSONDecodeError:
        return {}


def _save_settings(settings: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2))


_API_KEY_NAMES = [
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "ELEVENLABS_API_KEY",
    "DEEPGRAM_API_KEY",
    "FISH_AUDIO_API_KEY",
    "PEXELS_API_KEY",
]


def _load_api_keys_into_env() -> None:
    """Inject API keys saved via the Settings UI into os.environ (env var wins if already set)."""
    settings = _load_settings()
    for key, val in settings.get("api_keys", {}).items():
        if val and not os.environ.get(key):
            os.environ[key] = val


def _mask_key(val: str) -> str:
    if not val:
        return ""
    return val[:4] + "••••" + val[-3:] if len(val) > 8 else "••••"


def _is_invite_only() -> bool:
    return bool(_load_settings().get("invite_only", False))


def _create_invite(created_by: str, expires_hours: int = 48) -> dict:
    token = secrets.token_urlsafe(24)
    now = time.time()
    invite = {
        "token": token,
        "created_by": created_by,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + expires_hours * 3600)),
        "expires_ts": now + expires_hours * 3600,
        "used": False,
        "used_by": None,
        "used_at": None,
    }
    invites = _load_invites()
    invites.append(invite)
    _save_invites(invites)
    return invite


def _validate_and_consume_invite(token: str, used_by: str) -> bool:
    """Returns True and marks invite used if valid, unexpired, unused. False otherwise."""
    invites = _load_invites()
    for inv in invites:
        if inv["token"] == token:
            if inv["used"]:
                return False
            if time.time() > inv.get("expires_ts", 0):
                return False
            inv["used"] = True
            inv["used_by"] = used_by
            inv["used_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _save_invites(invites)
            return True
    return False


def _active_invites() -> list[dict]:
    """Return invites that are unused and not expired."""
    now = time.time()
    return [i for i in _load_invites() if not i["used"] and now < i.get("expires_ts", 0)]


def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login_page", next=request.path))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login_page", next=request.path))
        if session.get("role") != "admin":
            flash("Admin access required for this action.")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated


# ─────────────────────────────────────────────
#  AUTH PAGE TEMPLATE
# ─────────────────────────────────────────────
AUTH_PAGE = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wealth Vault — {{ 'Sign Up' if mode == 'signup' else 'Login' }}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800;900&display=swap');
  :root{--gold:#D4AF37;--gold-border:rgba(212,175,55,.35);--gold-dim:rgba(212,175,55,.12);--bg:#000;--surface:#0a0a0a;--text:#f0ead6;--muted:#6b6350;color-scheme:dark}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);min-height:100vh;display:flex;align-items:center;justify-content:center;font-family:Inter,system-ui,sans-serif;overflow:hidden}

  /* particle canvas */
  #particles{position:fixed;inset:0;pointer-events:none;z-index:0}

  .card{position:relative;z-index:10;width:100%;max-width:420px;margin:0 16px;background:var(--surface);border:1px solid #1c1c1c;border-radius:22px;padding:40px 36px 36px;box-shadow:0 0 80px rgba(0,0,0,.8),0 0 40px rgba(212,175,55,.04)}

  .vault-icon{width:56px;height:56px;border-radius:50%;background:linear-gradient(135deg,var(--gold) 0%,#5c3e00 100%);display:flex;align-items:center;justify-content:center;font-size:26px;font-weight:900;color:#000;margin:0 auto 18px;box-shadow:0 0 28px rgba(212,175,55,.35)}
  .vault-title{text-align:center;font-size:1.65rem;font-weight:950;letter-spacing:-.04em;background:linear-gradient(135deg,#fff 30%,var(--gold));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;margin-bottom:4px}
  .vault-sub{text-align:center;color:var(--muted);font-size:.82rem;margin-bottom:28px;line-height:1.5}

  /* tabs */
  .tabs{display:grid;grid-template-columns:1fr 1fr;gap:4px;background:#0d0d0d;border:1px solid #1c1c1c;border-radius:12px;padding:4px;margin-bottom:28px}
  .tab{padding:9px;border-radius:9px;border:none;background:transparent;color:var(--muted);font-family:inherit;font-size:.83rem;font-weight:800;cursor:pointer;transition:all .2s;letter-spacing:.04em;text-transform:uppercase}
  .tab.active{background:var(--gold-dim);border:1px solid var(--gold-border);color:var(--gold)}

  .field{margin-bottom:16px}
  label{display:block;font-size:.68rem;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);font-weight:900;margin-bottom:7px}
  input[type=text],input[type=password]{width:100%;background:#0d0d0d;border:1px solid #222;border-radius:10px;color:var(--text);padding:11px 14px;font:inherit;font-size:.9rem;transition:border-color .2s,box-shadow .2s;outline:none}
  input[type=text]:focus,input[type=password]:focus{border-color:var(--gold-border);box-shadow:0 0 0 3px rgba(212,175,55,.08)}

  .btn{width:100%;padding:13px;border-radius:12px;border:1px solid var(--gold-border);background:var(--gold-dim);color:var(--gold);font:inherit;font-size:.88rem;font-weight:900;letter-spacing:.07em;text-transform:uppercase;cursor:pointer;transition:all .2s;margin-top:4px}
  .btn:hover{background:rgba(212,175,55,.22);box-shadow:0 0 18px rgba(212,175,55,.15)}
  .btn:active{transform:scale(.98)}

  .btn-google{width:100%;padding:13px;border-radius:12px;border:1px solid #2a2a2a;background:#111;color:#e8eaed;font:inherit;font-size:.88rem;font-weight:700;cursor:pointer;transition:all .2s;display:flex;align-items:center;justify-content:center;gap:10px;text-decoration:none;margin-bottom:16px}
  .btn-google:hover{background:#1a1a1a;border-color:#3a3a3a;box-shadow:0 0 14px rgba(255,255,255,.05)}
  .btn-google svg{flex-shrink:0}

  .flash{padding:10px 14px;border-radius:9px;font-size:.82rem;font-weight:700;margin-bottom:18px;line-height:1.4}
  .flash.error{background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);color:#f87171}
  .flash.success{background:rgba(34,197,94,.1);border:1px solid rgba(34,197,94,.3);color:#86efac}

  .divider{text-align:center;color:var(--muted);font-size:.75rem;margin:20px 0;position:relative}
  .divider::before,.divider::after{content:'';position:absolute;top:50%;width:42%;height:1px;background:#1c1c1c}
  .divider::before{left:0}.divider::after{right:0}

  .quote{text-align:center;color:var(--muted);font-size:.78rem;font-style:italic;margin-top:22px;line-height:1.55;padding-top:20px;border-top:1px solid #111}
  .quote em{color:rgba(212,175,55,.6);font-style:normal}

  .first-admin-notice{padding:10px 14px;border-radius:9px;background:rgba(212,175,55,.07);border:1px solid var(--gold-border);font-size:.78rem;color:var(--muted);margin-bottom:18px;line-height:1.5}
  .first-admin-notice strong{color:var(--gold)}
</style></head>
<body>
<canvas id="particles"></canvas>

<div class="card">
  <div class="vault-icon">W</div>
  <div class="vault-title">Wealth Vault</div>
  <div class="vault-sub">Autonomous YouTube Shorts Empire<br>Dark Psychology &amp; Wealth Niche</div>

  {% with msgs = get_flashed_messages(with_categories=true) %}
    {% for cat, msg in msgs %}
      <div class="flash {{ cat }}">{{ msg }}</div>
    {% endfor %}
  {% endwith %}

  {% if is_first_user %}
  <div class="first-admin-notice">&#9733; <strong>No accounts yet.</strong> The first account created will automatically become <strong>Admin</strong> with full access.</div>
  {% endif %}

  <!-- GOOGLE SIGN-IN -->
  {% if google_available %}
  <a href="{{ url_for('google_auth_start') }}" class="btn-google">
    <svg width="18" height="18" viewBox="0 0 48 48"><path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.08 17.74 9.5 24 9.5z"/><path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/><path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/><path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.18 1.48-4.97 2.31-8.16 2.31-6.26 0-11.57-3.59-13.46-8.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/><path fill="none" d="M0 0h48v48H0z"/></svg>
    Continue with Google
  </a>
  <div class="divider">or use your vault account</div>
  {% endif %}

  <div class="tabs">
    <button class="tab {{ 'active' if mode == 'login' }}" onclick="switchTab('login')">Login</button>
    <button class="tab {{ 'active' if mode == 'signup' }}" onclick="switchTab('signup')">Sign Up</button>
  </div>

  <!-- LOGIN FORM -->
  <form id="form-login" method="post" action="{{ url_for('login_post') }}"
        style="{{ 'display:none' if mode == 'signup' else '' }}">
    <input type="hidden" name="next" value="{{ next_url }}">
    <div class="field">
      <label>Username</label>
      <input type="text" name="username" autocomplete="username" required placeholder="your_username">
    </div>
    <div class="field">
      <label>Password</label>
      <input type="password" name="password" autocomplete="current-password" required placeholder="••••••••">
    </div>
    <button class="btn" type="submit">Enter the Vault &rarr;</button>
  </form>

  <!-- SIGNUP FORM -->
  <form id="form-signup" method="post" action="{{ url_for('signup_post') }}"
        style="{{ '' if mode == 'signup' else 'display:none' }}">
    {% if invite_only and not is_first_user %}
    <div class="first-admin-notice" style="margin-bottom:18px;">&#128274; <strong>Invite-only.</strong> You need a valid invite link to create an account.</div>
    {% endif %}
    <div class="field">
      <label>Username</label>
      <input type="text" name="username" autocomplete="username" required placeholder="your_username" minlength="3" maxlength="30">
    </div>
    <div class="field">
      <label>Password</label>
      <input type="password" name="password" autocomplete="new-password" required placeholder="••••••••" minlength="6">
    </div>
    <div class="field">
      <label>Confirm Password</label>
      <input type="password" name="confirm" autocomplete="new-password" required placeholder="••••••••">
    </div>
    {% if invite_only and not is_first_user %}
    <div class="field">
      <label>Invite Code <span style="color:var(--gold)">*</span></label>
      <input type="text" name="invite_token" placeholder="paste your invite code" value="{{ invite_token }}" {% if invite_only %}required{% endif %} style="font-family:monospace;font-size:.82rem;letter-spacing:.04em;">
    </div>
    {% elif invite_token %}
    <input type="hidden" name="invite_token" value="{{ invite_token }}">
    {% endif %}
    <button class="btn" type="submit">Create Account &rarr;</button>
  </form>

  <div class="quote"><em>"{{ quote }}"</em></div>
</div>

<script>
function switchTab(t){
  document.getElementById('form-login').style.display = t==='login'?'':'none';
  document.getElementById('form-signup').style.display = t==='signup'?'':'none';
  document.querySelectorAll('.tab').forEach((el,i)=>el.classList.toggle('active',i===(t==='login'?0:1)));
  history.replaceState(null,'',t==='login'?'{{ url_for("login_page") }}':'{{ url_for("login_page", mode="signup") }}');
}

// Particle background
(function(){
  const c=document.getElementById('particles'),ctx=c.getContext('2d');
  let W,H,pts=[];
  function resize(){W=c.width=window.innerWidth;H=c.height=window.innerHeight;pts=Array.from({length:55},()=>({x:Math.random()*W,y:Math.random()*H,vx:(Math.random()-.5)*.22,vy:(Math.random()-.5)*.22,r:Math.random()*1.8+.4,o:Math.random()*.35+.08}))}
  window.addEventListener('resize',resize);resize();
  function draw(){
    ctx.clearRect(0,0,W,H);
    pts.forEach(p=>{
      p.x+=p.vx;p.y+=p.vy;
      if(p.x<0||p.x>W)p.vx*=-1;
      if(p.y<0||p.y>H)p.vy*=-1;
      ctx.beginPath();ctx.arc(p.x,p.y,p.r,0,Math.PI*2);
      ctx.fillStyle=`rgba(212,175,55,${p.o})`;ctx.fill();
    });
    requestAnimationFrame(draw);
  }draw();
})();
</script>
</body></html>
"""

# ─────────────────────────────────────────────
#  MAIN VAULT DASHBOARD
# ─────────────────────────────────────────────
VAULT_PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The Wealth Vault — Command Center</title>
<style>
  :root {
    --gold: #D4AF37;
    --gold-dim: rgba(212,175,55,0.15);
    --gold-border: rgba(212,175,55,0.35);
    --bg: #000;
    --surface: #0a0a0a;
    --text: #f0ead6;
    --muted: #6b6350;
    color-scheme: dark;
    font-family: 'Inter', 'Segoe UI', system-ui, sans-serif;
  }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); min-height: 100vh; }

  /* ── TOP NAV ── */
  nav {
    position: fixed; top: 0; left: 0; right: 0; z-index: 100;
    height: 56px; background: rgba(0,0,0,0.96);
    border-bottom: 1px solid var(--gold-border);
    display: flex; align-items: center; justify-content: space-between;
    padding: 0 20px; backdrop-filter: blur(14px);
  }
  .nav-brand {
    font-size: 0.78rem; font-weight: 900; letter-spacing: 0.22em;
    text-transform: uppercase; color: var(--gold);
  }
  .nav-right { display: flex; align-items: center; gap: 10px; }

  /* ── HAMBURGER ── */
  #menu-toggle { display: none; }
  .burger {
    width: 36px; height: 36px; display: flex; flex-direction: column;
    justify-content: center; gap: 5px; cursor: pointer; padding: 4px;
    border-radius: 6px; transition: background .2s; flex-shrink: 0;
  }
  .burger:hover { background: var(--gold-dim); }
  .burger span {
    display: block; height: 2px; background: var(--gold);
    border-radius: 2px; transition: transform .3s, opacity .3s;
  }
  #menu-toggle:checked + .overlay { display: block; }
  #menu-toggle:checked ~ .side-panel { transform: translateX(0); }
  /* animate burger → X when open */
  #menu-toggle:checked ~ nav .burger span:nth-child(1) { transform: translateY(7px) rotate(45deg); }
  #menu-toggle:checked ~ nav .burger span:nth-child(2) { opacity: 0; }
  #menu-toggle:checked ~ nav .burger span:nth-child(3) { transform: translateY(-7px) rotate(-45deg); }

  /* ── OVERLAY ── */
  .overlay {
    display: none; position: fixed; inset: 0; z-index: 190;
    background: rgba(0,0,0,0.6); backdrop-filter: blur(3px);
  }

  /* ── GLASSMORPHIC SIDE PANEL ── */
  .side-panel {
    position: fixed; top: 0; right: 0; bottom: 0; width: 340px; z-index: 200;
    background: rgba(0,0,0,0.88);
    backdrop-filter: blur(28px); -webkit-backdrop-filter: blur(28px);
    border-left: 1px solid var(--gold-border);
    box-shadow: -24px 0 80px rgba(0,0,0,0.85), inset 0 0 0 1px rgba(212,175,55,0.06);
    transform: translateX(100%); transition: transform .32s cubic-bezier(.4,0,.2,1);
    overflow-y: auto; padding: 20px 20px 48px;
    scrollbar-width: thin; scrollbar-color: #222 transparent;
  }
  .panel-header {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 24px; padding-bottom: 14px;
    border-bottom: 1px solid rgba(212,175,55,0.2);
  }
  .panel-title {
    font-size: 0.68rem; font-weight: 900; letter-spacing: 0.22em;
    text-transform: uppercase; color: var(--gold);
  }
  .panel-close-btn {
    cursor: pointer; font-size: 1.1rem; color: var(--muted); line-height: 1;
    width: 28px; height: 28px; display: grid; place-items: center;
    border-radius: 6px; border: 1px solid #222; background: transparent;
    transition: color .2s, border-color .2s, background .2s;
  }
  .panel-close-btn:hover { color: var(--gold); border-color: var(--gold-border); background: var(--gold-dim); }

  .panel-section { margin-bottom: 26px; }
  .panel-section h3 {
    font-size: 0.62rem; letter-spacing: 0.2em; text-transform: uppercase;
    color: var(--muted); font-weight: 900; margin-bottom: 10px;
    padding-bottom: 7px; border-bottom: 1px solid #141414;
  }

  /* ── STATUS PILLS ── */
  .pill-row { display: flex; flex-wrap: wrap; gap: 7px; }
  .pill {
    display: inline-flex; align-items: center; gap: 5px; padding: 4px 10px;
    border-radius: 999px; font-size: 0.68rem; font-weight: 800;
    letter-spacing: 0.06em; text-transform: uppercase;
  }
  .pill.ok   { background: rgba(34,197,94,0.1);  color: #86efac; border: 1px solid rgba(74,222,128,0.28); }
  .pill.warn { background: rgba(212,175,55,0.1); color: var(--gold); border: 1px solid var(--gold-border); }
  .pill.bad  { background: rgba(192,57,43,0.1);  color: #f87171; border: 1px solid rgba(239,68,68,0.28); }
  .pill-dot { width: 5px; height: 5px; border-radius: 50%; flex-shrink: 0; }
  .pill.ok .pill-dot   { background: #4ade80; box-shadow: 0 0 5px #4ade80; }
  .pill.warn .pill-dot { background: var(--gold); box-shadow: 0 0 5px var(--gold); }
  .pill.bad .pill-dot  { background: #f87171; }

  /* ── TRIAD CARDS IN SIDEBAR ── */
  .triad-item {
    display: flex; gap: 12px; align-items: flex-start;
    padding: 12px; border-radius: 10px; background: rgba(255,255,255,0.02);
    border: 1px solid #181818; margin-bottom: 8px;
    transition: border-color .2s, background .2s;
  }
  .triad-item:hover { border-color: var(--gold-border); background: rgba(212,175,55,0.04); }
  .triad-item:last-child { margin-bottom: 0; }
  .triad-icon { font-size: 1.3rem; flex-shrink: 0; margin-top: 1px; }
  .triad-info h4 {
    font-size: 0.72rem; letter-spacing: 0.1em; text-transform: uppercase;
    color: var(--gold); font-weight: 900; margin-bottom: 4px;
  }
  .triad-info p { color: var(--muted); font-size: 0.76rem; line-height: 1.5; }
  .sdot {
    display: inline-block; width: 6px; height: 6px; border-radius: 50%;
    margin-right: 4px; vertical-align: middle;
  }
  .sdot.green { background: #22c55e; box-shadow: 0 0 5px #22c55e; }
  .sdot.grey  { background: #333; }

  /* ── TREND LIST IN SIDEBAR ── */
  .trend-item {
    display: flex; gap: 10px; align-items: flex-start;
    padding: 8px 10px; border-radius: 8px; background: rgba(255,255,255,0.02);
    border: 1px solid #161616; margin-bottom: 6px;
  }
  .trend-rank { color: var(--gold); font-size: 0.68rem; font-weight: 900; min-width: 18px; }
  .trend-title { font-size: 0.78rem; color: var(--text); line-height: 1.4; }
  .trend-ch { font-size: 0.68rem; color: var(--muted); margin-top: 2px; }

  /* ── SIDEBAR NAV LINKS ── */
  .panel-nav-link {
    display: flex; align-items: center; justify-content: space-between;
    color: var(--text); font-size: 0.82rem; font-weight: 700;
    text-decoration: none; padding: 10px 14px;
    border: 1px solid #1a1a1a; border-radius: 9px; margin-bottom: 6px;
    background: rgba(255,255,255,0.02);
    transition: color .2s, border-color .2s, background .2s;
  }
  .panel-nav-link:hover { color: var(--gold); border-color: var(--gold-border); background: var(--gold-dim); }
  .panel-nav-link .arrow { color: var(--muted); font-size: 0.75rem; }
  .panel-nav-link:hover .arrow { color: var(--gold); }

  /* ── MAIN LAYOUT ── */
  main {
    min-height: 100vh; display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    padding: 80px 24px 60px; max-width: 680px; margin: 0 auto;
  }

  /* ── HERO ── */
  .hero-eyebrow {
    display: inline-flex; align-items: center; gap: 8px;
    border: 1px solid var(--gold-border); background: var(--gold-dim);
    color: var(--gold); padding: 5px 14px; border-radius: 999px;
    font-size: 0.68rem; font-weight: 900; letter-spacing: 0.16em;
    text-transform: uppercase; margin-bottom: 20px;
  }
  .hero-eyebrow .live-dot {
    width: 6px; height: 6px; border-radius: 50%; background: var(--gold);
    animation: blink 2s ease-in-out infinite;
  }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:.25} }

  h1 {
    font-size: clamp(1.8rem, 5vw, 3rem); font-weight: 900;
    letter-spacing: -0.02em; line-height: 1.2; margin-bottom: 6px;
    color: var(--text); text-align: center;
  }
  h1 em {
    font-style: normal;
    background: linear-gradient(135deg, #fff 20%, var(--gold) 100%);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    background-clip: text;
  }
  .hero-tagline {
    font-size: 0.82rem; color: var(--muted); letter-spacing: 0.06em;
    text-align: center; margin-bottom: 36px; font-style: italic;
  }

  /* ── FLASH MESSAGES ── */
  .flash-list { list-style: none; display: grid; gap: 8px; margin-bottom: 20px; width: 100%; }
  .flash-list li {
    padding: 11px 16px; border-radius: 10px;
    background: rgba(212,175,55,0.07); border: 1px solid var(--gold-border);
    color: var(--gold); font-size: 0.88rem;
  }

  /* ── FORCE UPLOAD CARD ── */
  .upload-card {
    width: 100%; border: 1px solid var(--gold-border); border-radius: 18px;
    background: var(--surface); padding: 28px 28px 24px;
    box-shadow: 0 0 60px rgba(212,175,55,0.06), 0 24px 80px rgba(0,0,0,0.5);
    margin-bottom: 16px;
  }
  .upload-card-label {
    font-size: 0.62rem; font-weight: 900; letter-spacing: 0.22em;
    text-transform: uppercase; color: var(--muted); margin-bottom: 16px;
    display: block;
  }
  .force-grid { display: grid; gap: 12px; }
  .input-field {
    width: 100%; background: #080808; border: 1px solid #1e1e1e;
    border-radius: 10px; color: var(--text); padding: 14px 16px;
    font: inherit; font-size: 0.95rem; transition: border-color .2s, box-shadow .2s;
  }
  .input-field:focus { outline: none; border-color: var(--gold); box-shadow: 0 0 0 3px rgba(212,175,55,0.08); }
  .input-field::placeholder { color: var(--muted); }
  .ab-toggle {
    display: flex; align-items: center; gap: 10px;
    color: #666; font-size: 0.82rem; cursor: pointer; user-select: none;
  }
  .ab-toggle input { accent-color: var(--gold); width: 15px; height: 15px; }
  .btn-gold {
    width: 100%; border: 0; border-radius: 12px; cursor: pointer;
    font: inherit; font-weight: 900; padding: 17px 20px;
    font-size: 1rem; letter-spacing: 0.06em; text-transform: uppercase;
    background: linear-gradient(135deg, #B8962E 0%, var(--gold) 55%, #e8c84a 100%);
    color: #000; box-shadow: 0 8px 36px rgba(212,175,55,0.28);
    transition: filter .2s, transform .15s, box-shadow .2s;
  }
  .btn-gold:hover { filter: brightness(1.08); transform: translateY(-2px); box-shadow: 0 14px 48px rgba(212,175,55,0.38); }
  .btn-gold:active { transform: translateY(0); }

  /* ── PIPELINE TRACKER ── */
  .pipeline-block {
    margin-top: 16px; padding: 18px 20px; border-radius: 12px;
    background: #060606; border: 1px solid #1a1a1a;
  }
  .pipeline-header {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 14px; gap: 10px; flex-wrap: wrap;
  }
  .pipeline-state {
    font-size: 0.65rem; font-weight: 900; letter-spacing: 0.16em;
    text-transform: uppercase; color: var(--gold);
  }
  .pipeline-elapsed {
    font-size: 0.65rem; font-weight: 700; letter-spacing: 0.08em;
    color: var(--muted); font-variant-numeric: tabular-nums;
  }
  .pipeline-msg { font-size: 0.85rem; color: #666; margin-bottom: 14px; min-height: 1.2em; }
  .pipeline-stages {
    display: flex; align-items: center; gap: 0; margin-bottom: 12px;
    overflow-x: auto; padding-bottom: 2px;
  }
  .stage {
    display: flex; flex-direction: column; align-items: center; gap: 4px;
    flex: 1; min-width: 52px; position: relative;
  }
  .stage:not(:last-child)::after {
    content: ''; position: absolute; top: 13px; left: calc(50% + 13px);
    width: calc(100% - 26px); height: 1px;
    background: #1e1e1e; transition: background .4s;
  }
  .stage.done::after { background: var(--gold); }
  .stage-dot {
    width: 26px; height: 26px; border-radius: 50%; display: flex;
    align-items: center; justify-content: center;
    font-size: 0.7rem; font-weight: 900; position: relative; z-index: 1;
    transition: all .4s; border: 1.5px solid #222; background: #0a0a0a; color: #333;
  }
  .stage.done .stage-dot { background: var(--gold); color: #000; border-color: var(--gold); }
  .stage.active .stage-dot {
    background: rgba(212,175,55,0.12); color: var(--gold);
    border-color: var(--gold); box-shadow: 0 0 12px rgba(212,175,55,0.3);
    animation: pulse-stage 1.4s ease-in-out infinite;
  }
  .stage.error .stage-dot { background: rgba(248,113,113,0.12); color: #f87171; border-color: #f87171; }
  @keyframes pulse-stage {
    0%, 100% { box-shadow: 0 0 8px rgba(212,175,55,0.3); }
    50% { box-shadow: 0 0 18px rgba(212,175,55,0.6); }
  }
  .stage-label {
    font-size: 0.58rem; font-weight: 900; letter-spacing: 0.1em;
    text-transform: uppercase; color: #333; transition: color .4s; white-space: nowrap;
  }
  .stage.done .stage-label { color: #8a7840; }
  .stage.active .stage-label { color: var(--gold); }
  .stage.error .stage-label { color: #f87171; }
  .progress-wrap { background: #111; border-radius: 999px; height: 2px; overflow: hidden; margin-bottom: 10px; }
  .progress-bar { height: 100%; background: linear-gradient(90deg, #B8962E, var(--gold)); border-radius: 999px; transition: width .6s ease; }
  .pipeline-meta { font-size: 0.75rem; color: var(--muted); display: flex; flex-wrap: wrap; gap: 10px; }
  .pipeline-meta a { color: var(--gold); font-weight: 800; text-decoration: none; }
  .pipeline-meta a:hover { text-decoration: underline; }
  .pipeline-idle { text-align: center; padding: 10px 0 2px; color: #333; font-size: 0.78rem; }
  /* legacy compat */
  .job-block { margin-top: 16px; padding: 15px 16px; border-radius: 10px; background: #080808; border: 1px solid #1c1c1c; }
  .job-state { font-size: 0.65rem; font-weight: 900; letter-spacing: 0.16em; text-transform: uppercase; color: var(--gold); margin-bottom: 5px; }
  .job-msg { font-size: 0.88rem; color: #777; }
  .job-meta { margin-top: 8px; font-size: 0.76rem; color: var(--muted); display: flex; flex-wrap: wrap; gap: 10px; }
  .job-meta a { color: var(--gold); font-weight: 800; text-decoration: none; }
  .job-meta a:hover { text-decoration: underline; }

  /* ── NEXT STRIKE INDICATOR ── */
  .strike-bar {
    width: 100%; display: flex; align-items: center; justify-content: center; gap: 14px;
    padding: 10px 18px; border-radius: 10px;
    background: rgba(212,175,55,0.04); border: 1px solid #1a1a1a;
  }
  .strike-label {
    font-size: 0.62rem; font-weight: 900; letter-spacing: 0.18em;
    text-transform: uppercase; color: var(--muted);
  }
  .strike-times {
    display: flex; gap: 10px; align-items: center;
  }
  .strike-time {
    font-size: 0.82rem; font-weight: 800; color: var(--gold);
    letter-spacing: 0.06em; font-variant-numeric: tabular-nums;
  }
  .strike-sep { color: #2a2a2a; font-size: 0.72rem; }
  .strike-tz { font-size: 0.65rem; color: var(--muted); }

  /* ── SEO ORACLE VARIANTS ── */
  .variant-row {
    display: flex; justify-content: space-between; font-size: 0.78rem;
    padding: 7px 10px; background: #0a0a0a; border-radius: 7px;
    border: 1px solid #1a1a1a; gap: 10px;
  }
  .variant-row.best { border-color: var(--gold-border); }

  @media (max-width: 600px) {
    main { padding: 72px 16px 50px; }
    .upload-card { padding: 20px; }
  }
</style>
</head>
<body>

<input type="checkbox" id="menu-toggle" style="display:none">
<label class="overlay" for="menu-toggle"></label>

<!-- GLASSMORPHIC SIDE PANEL -->
<aside class="side-panel">
  <div class="panel-header">
    <span class="panel-title">&#9679; Vault Command</span>
    <label for="menu-toggle" class="panel-close-btn">&#x2715;</label>
  </div>

  <!-- NAVIGATION LINKS -->
  <div class="panel-section">
    <h3>Navigate</h3>
    <a href="{{ url_for('index') }}" class="panel-nav-link">
      <span>&#9646; Command Center</span><span class="arrow">&#8599;</span>
    </a>
    <a href="{{ url_for('view_dashboard') }}" class="panel-nav-link">
      <span>&#9646; Performance Dashboard</span><span class="arrow">&#8599;</span>
    </a>
    <a href="{{ url_for('view_ab_tests') }}" class="panel-nav-link">
      <span>&#9646; A/B Hook Tests</span><span class="arrow">&#8599;</span>
    </a>
    <a href="{{ url_for('sample_reel') }}" class="panel-nav-link">
      <span>&#9646; Sample Reel</span><span class="arrow">&#8599;</span>
    </a>
    <a href="{{ url_for('view_affiliate_comments') }}" class="panel-nav-link">
      <span>&#9646; Affiliate Comments</span><span class="arrow">&#8599;</span>
    </a>
    <a href="{{ url_for('view_trend_hunter') }}" class="panel-nav-link">
      <span>&#9646; Trend Hunter</span><span class="arrow">&#8599;</span>
    </a>
    <a href="{{ url_for('view_spike_log') }}" class="panel-nav-link">
      <span>&#9646; Auto-Seeder Log</span><span class="arrow">&#8599;</span>
    </a>
    {% if session.get('role') == 'admin' %}
    <a href="{{ url_for('view_users') }}" class="panel-nav-link">
      <span>&#9646; User Management</span><span class="arrow">&#8599;</span>
    </a>
    <a href="{{ url_for('admin_settings') }}" class="panel-nav-link">
      <span>&#9646; API Settings</span><span class="arrow">&#8599;</span>
    </a>
    {% endif %}
  </div>

  <!-- OMNI ENGINE STATS -->
  <div class="panel-section">
    <h3>Omni Empire Engine v35</h3>
    <div class="pill-row">
      <span class="pill {{ 'ok' if youtube_ready else 'bad' }}"><span class="pill-dot"></span>YouTube {{ 'Connected' if youtube_ready else 'Offline' }}</span>
      <span class="pill {{ 'ok' if pexels_ready else 'bad' }}"><span class="pill-dot"></span>Pexels {{ 'OK' if pexels_ready else 'Missing' }}</span>
      <span class="pill {{ 'ok' if gemini_ready else 'bad' }}"><span class="pill-dot"></span>Gemini {{ 'OK' if gemini_ready else 'Missing' }}</span>
      <span class="pill {{ 'ok' if elevenlabs_ready else 'warn' }}"><span class="pill-dot"></span>ElevenLabs {{ 'OK' if elevenlabs_ready else 'gTTS' }}</span>
      <span class="pill {{ 'ok' if openrouter_ready else 'warn' }}"><span class="pill-dot"></span>OpenRouter {{ 'OK' if openrouter_ready else 'Optional' }}</span>
      <span class="pill {{ 'ok' if scheduler_running else 'warn' }}"><span class="pill-dot"></span>Scheduler {{ 'Live' if scheduler_running else 'Off' }}</span>
    </div>
  </div>

  <!-- AI TRIAD -->
  <div class="panel-section">
    <h3>The AI Triad</h3>
    <div class="triad-item">
      <div class="triad-icon">&#9998;</div>
      <div class="triad-info">
        <h4>The Architect</h4>
        <p><span class="sdot {{ 'green' if gemini_ready else 'grey' }}"></span>Gemini script generation · SEO Oracle selects best of 5 title variants · daily pipeline.</p>
      </div>
    </div>
    <div class="triad-item">
      <div class="triad-icon">&#9654;</div>
      <div class="triad-info">
        <h4>The Cinematic Oracle</h4>
        <p><span class="sdot green"></span>6–8 Pexels clips per Short · 3 s swaps · word-by-word captions · ghost watermark.</p>
      </div>
    </div>
    <div class="triad-item">
      <div class="triad-icon">&#128202;</div>
      <div class="triad-info">
        <h4>The Trend Hunter</h4>
        <p><span class="sdot {{ 'green' if trend_updated else 'grey' }}"></span>Scrapes top Wealth/Luxury Shorts every 12h.
          {% if trend_updated %}Last sync: {{ trend_updated }}.{% else %}Syncing on first upload.{% endif %}</p>
      </div>
    </div>
  </div>

  <!-- TRENDING VIDEOS (if available) -->
  {% if trending_videos %}
  <div class="panel-section">
    <h3>Trend Hunter — Top Picks</h3>
    {% for v in trending_videos[:5] %}
    <div class="trend-item">
      <span class="trend-rank">#{{ loop.index }}</span>
      <div>
        <div class="trend-title"><a href="{{ v.url }}" target="_blank" style="color:var(--text);text-decoration:none;">{{ v.title }}</a></div>
        <div class="trend-ch">{{ v.channel }}</div>
      </div>
    </div>
    {% endfor %}
  </div>
  {% endif %}

  <!-- OAUTH -->
  <div class="panel-section">
    <h3>YouTube OAuth</h3>
    {% if not youtube_ready %}
      <a href="{{ url_for('youtube_auth_start') }}" style="color:var(--gold);font-size:0.82rem;font-weight:800;text-decoration:none;">Authorize YouTube &rarr;</a>
      <p style="color:var(--muted);font-size:0.72rem;margin-top:7px;line-height:1.55;">
        Redirect URI for Google Cloud Console:<br>
        <code style="color:#666;word-break:break-all;font-size:0.68rem;">{{ redirect_uri }}</code>
      </p>
    {% else %}
      <p style="color:#86efac;font-size:0.8rem;font-weight:800;">Token active — auto-upload enabled.</p>
      <a href="{{ url_for('youtube_auth_start') }}" style="color:var(--muted);font-size:0.72rem;text-decoration:none;display:block;margin-top:7px;">Re-authorize &rarr;</a>
    {% endif %}
  </div>

  <!-- AFFILIATE -->
  <div class="panel-section">
    <h3>Affiliate Comment</h3>
    <form method="post" action="{{ url_for('save_affiliate_settings') }}" style="display:grid;gap:9px;">
      <div>
        <label style="display:block;font-size:0.62rem;letter-spacing:0.12em;text-transform:uppercase;color:var(--muted);font-weight:900;margin-bottom:4px;">Referral URL</label>
        <input name="affiliate_url" type="url" value="{{ affiliate_url }}" placeholder="https://your-link.com/ref"
          style="width:100%;background:#080808;border:1px solid #1e1e1e;border-radius:8px;color:var(--text);padding:8px 11px;font:inherit;font-size:0.8rem;">
      </div>
      <div>
        <label style="display:block;font-size:0.62rem;letter-spacing:0.12em;text-transform:uppercase;color:var(--muted);font-weight:900;margin-bottom:4px;">CTA Line</label>
        <input name="affiliate_cta" type="text" value="{{ affiliate_cta }}" placeholder="Crypto Affiliate Hub — Start today."
          style="width:100%;background:#080808;border:1px solid #1e1e1e;border-radius:8px;color:var(--text);padding:8px 11px;font:inherit;font-size:0.8rem;">
      </div>
      <button type="submit" style="border:0;border-radius:8px;background:var(--gold-dim);border:1px solid var(--gold-border);color:var(--gold);font-weight:900;font-size:0.72rem;padding:8px;cursor:pointer;letter-spacing:0.08em;text-transform:uppercase;">Save Settings</button>
    </form>
    <a href="{{ url_for('view_affiliate_comments') }}" style="display:block;margin-top:9px;color:var(--muted);font-size:0.75rem;font-weight:800;text-decoration:none;">View Comment Log &rarr;</a>
  </div>

  <!-- QUICK PLANNER -->
  <div class="panel-section">
    <h3>Quick Planner</h3>
    <form method="post" action="{{ url_for('generate') }}" style="display:grid;gap:10px;">
      <textarea name="description" required maxlength="1200"
        style="width:100%;min-height:90px;resize:vertical;background:#080808;border:1px solid #1e1e1e;border-radius:8px;color:var(--text);padding:10px 12px;font:inherit;font-size:0.8rem;"
        placeholder="Describe your video idea — script + voice + links only…">{{ description }}</textarea>
      <button type="submit" style="border:1px solid #222;border-radius:8px;background:transparent;color:var(--muted);font-weight:900;font-size:0.72rem;padding:8px;cursor:pointer;letter-spacing:0.06em;text-transform:uppercase;transition:color .2s,border-color .2s;">Generate Plan Only (no upload)</button>
    </form>
  </div>

  <!-- LOGOUT -->
  <div class="panel-section" style="margin-bottom:0;">
    <a href="{{ url_for('logout') }}" style="display:block;text-align:center;padding:10px;border:1px solid #1a1a1a;border-radius:9px;color:var(--muted);font-size:0.72rem;font-weight:900;letter-spacing:0.1em;text-transform:uppercase;text-decoration:none;transition:color .2s,border-color .2s;">Sign Out</a>
  </div>
</aside>

<!-- TOP NAV -->
<nav>
  <span class="nav-brand">&#9679; Wealth Vault</span>
  <div class="nav-right">
    {% if session.get('picture') %}
      <img src="{{ session.get('picture') }}" width="26" height="26" style="border-radius:50%;border:1.5px solid var(--gold-border);object-fit:cover;flex-shrink:0;" alt="">
    {% else %}
      <div style="width:26px;height:26px;border-radius:50%;background:linear-gradient(135deg,var(--gold),#5c3e00);display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:900;color:#000;flex-shrink:0;">{{ (session.get('username','?')[0])|upper }}</div>
    {% endif %}
    <span style="font-size:.73rem;color:var(--muted);font-weight:700;max-width:80px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{{ session.get('username','') }}</span>
    {% if session.get('role') == 'admin' %}
      <span style="font-size:.6rem;font-weight:900;letter-spacing:.1em;text-transform:uppercase;color:var(--gold);background:var(--gold-dim);border:1px solid var(--gold-border);padding:2px 7px;border-radius:5px;">Admin</span>
    {% endif %}
    <label for="menu-toggle" style="cursor:pointer;">
      <div class="burger"><span></span><span></span><span></span></div>
    </label>
  </div>
</nav>

<main>
  <!-- FLASH MESSAGES -->
  {% with messages = get_flashed_messages() %}
    {% if messages %}
      <ul class="flash-list">{% for m in messages %}<li>{{ m }}</li>{% endfor %}</ul>
    {% endif %}
  {% endwith %}

  <!-- YOUTUBE OFFLINE BANNER -->
  {% if not youtube_ready %}
  <div style="width:100%;background:rgba(192,57,43,0.08);border:1px solid rgba(239,68,68,0.3);border-radius:12px;padding:14px 18px;margin-bottom:20px;display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;">
    <div>
      <span style="font-size:0.68rem;font-weight:900;letter-spacing:0.14em;text-transform:uppercase;color:#f87171;">YouTube Offline</span>
      <p style="font-size:0.8rem;color:#888;margin-top:3px;line-height:1.5;">Token missing or expired. Auto-upload is disabled until you re-authorize.</p>
    </div>
    <a href="{{ url_for('youtube_auth_start') }}" style="flex-shrink:0;display:inline-block;background:rgba(239,68,68,0.12);border:1px solid rgba(239,68,68,0.35);color:#f87171;font-size:0.76rem;font-weight:900;letter-spacing:0.1em;text-transform:uppercase;padding:9px 16px;border-radius:8px;text-decoration:none;transition:background .2s;">Authorize Now &rarr;</a>
  </div>
  {% endif %}

  <!-- HERO -->
  <div class="hero-eyebrow"><span class="live-dot"></span>Omni Empire Engine v35 — Active</div>
  <h1>Wealth is a <em>silent master</em>.</h1>
  <p class="hero-tagline" id="vault-quote">{{ quote }}</p>

  <!-- FORCE UPLOAD CARD -->
  <div class="upload-card">
    <span class="upload-card-label">Force Viral Upload</span>
    <form method="post" action="{{ url_for('force_upload') }}" class="force-grid">
      <input
        class="input-field"
        type="text" name="seed" maxlength="200"
        placeholder="Topic seed — leave blank for auto-pick from the Seed Pool"
      >
      <label class="ab-toggle">
        <input type="checkbox" name="ab_mode" value="1" {{ 'checked' if ab_mode_default }}>
        Run A/B Hook Test (2 variants · loser archived after 48 h)
      </label>
      <button class="btn-gold" type="submit">&#9654;&ensp;FIRE THE VAULT NOW</button>
    </form>

    <!-- LIVE PIPELINE TRACKER — updated every 2.5s via JS -->
    <div id="pipeline-tracker" class="pipeline-block">
      <div class="pipeline-header">
        <span class="pipeline-state" id="pt-state">IDLE</span>
        <span class="pipeline-elapsed" id="pt-elapsed"></span>
      </div>
      <div class="pipeline-msg" id="pt-msg">Waiting for next scheduled run — 08:00 London / 20:00 New York.</div>
      <div class="pipeline-stages" id="pt-stages">
        <div class="stage" id="ps-script"><div class="stage-dot">&#9998;</div><div class="stage-label">Script</div></div>
        <div class="stage" id="ps-voice"><div class="stage-dot">&#9654;</div><div class="stage-label">Voice</div></div>
        <div class="stage" id="ps-broll"><div class="stage-dot">&#9634;</div><div class="stage-label">B-Roll</div></div>
        <div class="stage" id="ps-render"><div class="stage-dot">&#9881;</div><div class="stage-label">Render</div></div>
        <div class="stage" id="ps-upload"><div class="stage-dot">&#8679;</div><div class="stage-label">Upload</div></div>
        <div class="stage" id="ps-done"><div class="stage-dot">&#10003;</div><div class="stage-label">Done</div></div>
      </div>
      <div class="progress-wrap"><div class="progress-bar" id="pt-bar" style="width:0%"></div></div>
      <div class="pipeline-meta" id="pt-meta"></div>
      {% if latest_job and latest_job.title_variants %}
      <details id="pt-variants" style="margin-top:12px;">
        <summary style="cursor:pointer;color:var(--muted);font-size:0.75rem;letter-spacing:0.06em;">SEO Oracle — Title Variants</summary>
        <div style="margin-top:10px;display:grid;gap:5px;" id="pt-variants-list">
        {% for v in latest_job.title_variants %}
          <div class="variant-row {{ 'best' if loop.first }}">
            <span style="color:{{ 'var(--gold)' if loop.first else '#777' }};">{{ v.title }}</span>
            <span style="color:var(--muted);font-weight:800;flex-shrink:0;">{{ v.score }}</span>
          </div>
        {% endfor %}
        </div>
      </details>
      {% endif %}
    </div>

    <!-- FIDELITY SUMMARY / CHECKLIST OF DOMINATION -->
    {% if latest_job and latest_job.state == 'done' %}
    <div style="margin-top:16px;padding:16px 18px;border-radius:12px;background:rgba(212,175,55,0.03);border:1px solid rgba(212,175,55,0.2);">
      <div style="font-size:0.62rem;font-weight:900;letter-spacing:0.2em;text-transform:uppercase;color:var(--muted);margin-bottom:12px;">Fidelity Summary — Checklist of Domination</div>
      <div style="display:grid;gap:7px;">
        <div style="display:flex;align-items:center;gap:10px;font-size:0.8rem;">
          <span style="color:#22c55e;font-size:0.9rem;font-weight:900;flex-shrink:0;">&#10003;</span>
          <span style="color:#888;"><strong style="color:var(--text);">4K Forge Active</strong> — Real-ESRGAN smoothing &amp; 1080p portrait at 30fps</span>
        </div>
        <div style="display:flex;align-items:center;gap:10px;font-size:0.8rem;">
          <span style="color:#22c55e;font-size:0.9rem;font-weight:900;flex-shrink:0;">&#10003;</span>
          <span style="color:#888;"><strong style="color:var(--text);">Sage Mentor Tone</strong> — ElevenLabs 'Brian' is the confirmed audio source</span>
        </div>
        <div style="display:flex;align-items:center;gap:10px;font-size:0.8rem;">
          <span style="color:{{ '#22c55e' if latest_job.get('duration_seconds', 0) >= 60 else '#f87171' }};font-size:0.9rem;font-weight:900;flex-shrink:0;">&#10003;</span>
          <span style="color:#888;"><strong style="color:var(--text);">Optimal Duration (60–75 s)</strong> — Script enforced at 150+ words before render</span>
        </div>
        <div style="display:flex;align-items:center;gap:10px;font-size:0.8rem;">
          <span style="color:#22c55e;font-size:0.9rem;font-weight:900;flex-shrink:0;">&#10003;</span>
          <span style="color:#888;"><strong style="color:var(--text);">Global Audio Bridge</strong> — Spanish/Hindi audience layer tagged in upload metadata</span>
        </div>
        <div style="display:flex;align-items:center;gap:10px;font-size:0.8rem;">
          <span style="color:#22c55e;font-size:0.9rem;font-weight:900;flex-shrink:0;">&#10003;</span>
          <span style="color:#888;"><strong style="color:var(--text);">Ghost Watermark</strong> — "Crypto Affiliate Hub" composited at 10% opacity</span>
        </div>
        <div style="display:flex;align-items:center;gap:10px;font-size:0.8rem;margin-top:4px;padding-top:8px;border-top:1px solid #141414;">
          <span style="width:8px;height:8px;border-radius:50%;flex-shrink:0;background:{{ '#22c55e' if openrouter_ready else '#f87171' }};box-shadow:0 0 6px {{ '#22c55e' if openrouter_ready else '#f87171' }};"></span>
          <span style="color:var(--muted);font-size:0.74rem;">OpenRouter 429-fallback — {{ 'online · bridge active' if openrouter_ready else 'key missing — Gemini-only mode' }}</span>
        </div>
      </div>
    </div>
    {% endif %}
  </div>

  <!-- NEXT SCHEDULED STRIKE -->
  <div class="strike-bar">
    <span class="strike-label">Next Strike</span>
    <div class="strike-times">
      <span class="strike-time">08:00</span>
      <span class="strike-sep">·</span>
      <span class="strike-time">20:00</span>
    </div>
    <span class="strike-sep" style="color:#1e1e1e">|</span>
    <span class="strike-tz">London&thinsp;/&thinsp;New&thinsp;York</span>
    <span class="strike-sep" style="color:#1e1e1e">|</span>
    <span class="strike-label">IN</span>
    <span class="strike-time" id="strike-countdown" style="font-size:0.9rem;color:var(--gold);">--:--:--</span>
  </div>
</main>

<script>
  const QUOTES = {{ quotes_json | safe }};
  let qi = 0;
  const qel = document.getElementById('vault-quote');
  qel.style.transition = 'opacity .45s';
  setInterval(() => {
    qi = (qi + 1) % QUOTES.length;
    qel.style.opacity = '0';
    setTimeout(() => { qel.textContent = QUOTES[qi]; qel.style.opacity = '1'; }, 460);
  }, 7000);

  // ── Live countdown to next 08:00 / 20:00 UTC ──
  // ── LIVE PIPELINE TRACKER ──────────────────────────────────────────────
  const STAGE_MAP = {
    queued:     { step: -1, label: 'QUEUED' },
    pending:    { step: -1, label: 'QUEUED' },
    scripting:  { step: 0,  label: 'SCRIPTING' },
    voiceover:  { step: 1,  label: 'VOICE' },
    downloading:{ step: 2,  label: 'B-ROLL' },
    rendering:  { step: 3,  label: 'RENDERING' },
    cooldown:   { step: 4,  label: 'UPLOAD' },
    uploading:  { step: 4,  label: 'UPLOADING' },
    cleanup:    { step: 5,  label: 'DONE' },
    done:       { step: 5,  label: 'DONE' },
    error:      { step: -2, label: 'ERROR' },
  };
  const STAGE_IDS = ['ps-script','ps-voice','ps-broll','ps-render','ps-upload','ps-done'];

  function fmtElapsed(startedAt) {
    if (!startedAt) return '';
    const s = Math.floor(Date.now()/1000 - startedAt);
    if (s < 60) return s + 's';
    if (s < 3600) return Math.floor(s/60) + 'm ' + (s%60) + 's';
    return Math.floor(s/3600) + 'h ' + Math.floor((s%3600)/60) + 'm';
  }

  function applyStages(step, isError) {
    STAGE_IDS.forEach((id, i) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.classList.remove('done','active','error');
      if (isError) { if (i === step) el.classList.add('error'); return; }
      if (i < step) el.classList.add('done');
      else if (i === step) el.classList.add('active');
    });
  }

  function renderPipeline(job) {
    const stateEl   = document.getElementById('pt-state');
    const msgEl     = document.getElementById('pt-msg');
    const barEl     = document.getElementById('pt-bar');
    const metaEl    = document.getElementById('pt-meta');
    const elapsedEl = document.getElementById('pt-elapsed');
    if (!stateEl) return;

    if (!job || job.state === 'idle') {
      stateEl.textContent = 'IDLE';
      stateEl.style.color = 'var(--muted)';
      msgEl.textContent   = 'Waiting for next scheduled run — 08:00 London / 20:00 New York.';
      barEl.style.width   = '0%';
      metaEl.innerHTML    = '';
      if (elapsedEl) elapsedEl.textContent = '';
      applyStages(-1, false);
      return;
    }

    const sm      = STAGE_MAP[job.state] || { step: 0, label: job.state.toUpperCase() };
    const isError = job.state === 'error';
    const isDone  = job.state === 'done' || job.state === 'cleanup';

    stateEl.textContent = sm.label + (job.id ? ' — ' + job.id : '');
    stateEl.style.color = isError ? '#f87171' : isDone ? '#86efac' : 'var(--gold)';
    msgEl.textContent   = job.message || '';
    barEl.style.width   = (job.progress || 0) + '%';
    if (elapsedEl) elapsedEl.textContent = job.started_at ? fmtElapsed(job.started_at) : '';

    applyStages(sm.step, isError);

    let meta = '';
    if (job.seed)      meta += '<span>Seed: <em>' + job.seed.slice(0,60) + '</em></span>';
    if (job.title)     meta += '<span>Title: <strong>' + job.title + '</strong></span>';
    if (job.video_id)  meta += '<a href="https://youtu.be/' + job.video_id + '" target="_blank">View A on YouTube</a>';
    if (job.video_id_b) meta += '<a href="https://youtu.be/' + job.video_id_b + '" target="_blank">View B on YouTube</a>';
    metaEl.innerHTML = meta;
  }

  let _ptTimer = null;
  function pollPipeline() {
    fetch('/api/job-status')
      .then(r => r.json())
      .then(job => {
        renderPipeline(job);
        const active = job && job.state && !['idle','done','error','cleanup'].includes(job.state);
        clearTimeout(_ptTimer);
        _ptTimer = setTimeout(pollPipeline, active ? 2500 : 8000);
      })
      .catch(() => { clearTimeout(_ptTimer); _ptTimer = setTimeout(pollPipeline, 10000); });
  }

  // seed the tracker with server-rendered data immediately, then start polling
  (function() {
    const initState = {{ '"' + (latest_job.state if latest_job else 'idle') + '"' }};
    const initJob   = initState === 'idle' ? null : {
      state:      initState,
      id:         {{ '"' + (latest_job.id if latest_job else '') + '"' }},
      message:    {{ (latest_job.message if latest_job else '') | tojson }},
      title:      {{ (latest_job.title if latest_job else '') | tojson }},
      video_id:   {{ (latest_job.video_id if latest_job else '') | tojson }},
      video_id_b: {{ (latest_job.video_id_b if latest_job else '') | tojson }},
      progress:   {{ job_progress }},
      started_at: {{ latest_job.started_at | int if latest_job else 0 }},
    };
    renderPipeline(initJob);
    setTimeout(pollPipeline, 2500);
  })();

  // ── STRIKE COUNTDOWN ───────────────────────────────────────────────────
  function updateStrikeCountdown() {
    const now = new Date();
    const nowSec = now.getUTCHours() * 3600 + now.getUTCMinutes() * 60 + now.getUTCSeconds();
    const strikes = [8 * 3600, 20 * 3600];
    let diff = strikes.find(s => s > nowSec);
    if (diff === undefined) diff = strikes[0] + 86400;
    diff -= nowSec;
    const h = Math.floor(diff / 3600);
    const m = Math.floor((diff % 3600) / 60);
    const s = diff % 60;
    const pad = n => String(n).padStart(2, '0');
    const el = document.getElementById('strike-countdown');
    if (el) el.textContent = pad(h) + 'h ' + pad(m) + 'm ' + pad(s) + 's';
  }
  updateStrikeCountdown();
  setInterval(updateStrikeCountdown, 1000);
</script>
</body>
</html>
"""

# ─────────────────────────────────────────────
#  A/B TEST PAGE
# ─────────────────────────────────────────────
AB_PAGE = """
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wealth Vault — A/B Hook Tests</title>
<style>
  :root { --gold:#D4AF37; --gold-border:rgba(212,175,55,.35); --gold-dim:rgba(212,175,55,.12); --bg:#000; --surface:#0a0a0a; --text:#f0ead6; --muted:#6b6350; color-scheme:dark; font-family:Inter,system-ui,sans-serif; }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);min-height:100vh}
  main{max-width:1100px;margin:0 auto;padding:40px 24px 80px}
  a{color:var(--gold);text-decoration:none;font-weight:800}
  a:hover{text-decoration:underline}
  h1{font-size:clamp(1.8rem,5vw,3rem);letter-spacing:-.03em;margin:12px 0 6px;background:linear-gradient(135deg,#fff 30%,var(--gold));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
  .lead{color:var(--muted);font-size:.9rem;margin-bottom:28px;line-height:1.6}
  .card{border:1px solid #1c1c1c;border-radius:14px;background:var(--surface);padding:22px;margin-bottom:14px}
  table{width:100%;border-collapse:collapse;font-size:.82rem}
  th{text-align:left;padding:9px 12px;color:var(--muted);font-size:.68rem;letter-spacing:.1em;text-transform:uppercase;border-bottom:1px solid #1a1a1a}
  td{padding:10px 12px;border-bottom:1px solid #111;vertical-align:top}
  .badge-win{display:inline-block;padding:2px 9px;border-radius:999px;font-size:.68rem;font-weight:900;background:rgba(34,197,94,.12);color:#86efac;border:1px solid rgba(74,222,128,.3)}
  .badge-pending{display:inline-block;padding:2px 9px;border-radius:999px;font-size:.68rem;font-weight:900;background:var(--gold-dim);color:var(--gold);border:1px solid var(--gold-border)}
  .empty{padding:36px;text-align:center;color:var(--muted);border:1px dashed #1a1a1a;border-radius:12px}
  .hook-a{color:#93c5fd} .hook-b{color:#f9a8d4}
</style></head>
<body><main>
<a href="{{ url_for('index') }}">&larr; Command Center</a>
<h1>A/B Hook Tests</h1>
<p class="lead">Two variant hooks per scheduled run. After 48 hours the engine auto-archives the loser and feeds the winning hook style into future scripts.</p>
{% with messages = get_flashed_messages() %}{% if messages %}
  <div style="padding:12px 16px;border-radius:10px;background:var(--gold-dim);border:1px solid var(--gold-border);color:var(--gold);margin-bottom:16px;font-size:.88rem;">
    {% for m in messages %}{{ m }}<br>{% endfor %}
  </div>
{% endif %}{% endwith %}
<div class="card">
{% if tests %}
<table>
  <thead><tr>
    <th>Test ID</th><th>Hook A</th><th>Hook B</th>
    <th class="num">Views A</th><th class="num">Views B</th>
    <th>Winner</th><th>Age</th>
  </tr></thead>
  <tbody>
  {% for t in tests %}
  <tr>
    <td style="font-family:monospace;color:var(--muted);font-size:.72rem;">{{ t.test_id }}</td>
    <td><span class="hook-a">{{ t.variant_a.get('hook','—')[:60] }}</span><br>
      {% if t.variant_a.get('video_id') %}<a href="https://youtu.be/{{ t.variant_a.video_id }}" target="_blank" style="font-size:.72rem;">watch</a>{% endif %}
    </td>
    <td><span class="hook-b">{{ t.variant_b.get('hook','—')[:60] }}</span><br>
      {% if t.variant_b.get('video_id') %}<a href="https://youtu.be/{{ t.variant_b.video_id }}" target="_blank" style="font-size:.72rem;">watch</a>{% endif %}
    </td>
    <td style="text-align:right;font-weight:800;">{{ '{:,}'.format(t.variant_a.get('views',0)) }}</td>
    <td style="text-align:right;font-weight:800;">{{ '{:,}'.format(t.variant_b.get('views',0)) }}</td>
    <td>
      {% if t.winner %}<span class="badge-win">{{ t.winner }}</span>
      {% else %}<span class="badge-pending">Pending</span>{% endif %}
    </td>
    <td style="color:var(--muted);font-size:.72rem;">{{ t.get('created_at','') | int | age_fmt }}</td>
  </tr>
  {% endfor %}
  </tbody>
</table>
{% else %}
<div class="empty">No A/B tests recorded yet. Enable the A/B toggle on the Force Upload and run your first test.</div>
{% endif %}
</div>
</main></body></html>
"""

# ─────────────────────────────────────────────
#  RESULT PAGE (legacy quick planner)
# ─────────────────────────────────────────────
RESULT_PAGE = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wealth Vault — Script Assets</title>
<style>
  :root{--gold:#D4AF37;--gold-border:rgba(212,175,55,.35);--bg:#000;--surface:#0a0a0a;--text:#f0ead6;--muted:#6b6350;color-scheme:dark;font-family:Inter,system-ui,sans-serif}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);min-height:100vh}
  main{max-width:900px;margin:0 auto;padding:36px 22px 80px}
  a{color:var(--gold);font-weight:800;text-decoration:none}
  h1{font-size:clamp(2rem,6vw,3.8rem);letter-spacing:-.04em;margin:14px 0 8px;background:linear-gradient(135deg,#fff 30%,var(--gold));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
  .lead{color:var(--muted);margin-bottom:24px;line-height:1.6}
  .card{border:1px solid #1c1c1c;border-radius:14px;background:var(--surface);padding:22px;margin-bottom:14px}
  .card h2{font-size:.68rem;letter-spacing:.14em;text-transform:uppercase;color:var(--gold);font-weight:900;margin-bottom:14px}
  .script{white-space:pre-wrap;color:var(--text);line-height:1.7;font-size:.98rem}
  ul.plain{list-style:none;display:grid;gap:8px}
  ul.plain li{padding:11px 14px;border-radius:9px;background:#0d0d0d;border:1px solid #1c1c1c;font-size:.88rem;color:var(--text)}
  .actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}
  button{border:0;border-radius:999px;background:linear-gradient(135deg,#B8962E,var(--gold));color:#000;font-weight:900;padding:11px 18px;cursor:pointer;font:inherit}
  .copied{color:#86efac;font-weight:800}
</style></head>
<body><main>
<a href="{{ url_for('index') }}">&larr; Command Center</a>
<h1>Script Assets</h1>
<p class="lead">{{ description }}</p>
<div class="grid" style="display:grid;gap:14px">
  {% if voiceover_filename %}
  <div class="card">
    <h2>Voiceover</h2>
    <a href="{{ url_for('download_output', filename=voiceover_filename) }}">Download MP3</a>
  </div>
  {% endif %}
  <div class="card">
    <h2>Narrator Script</h2>
    <div id="script" class="script">{{ script }}</div>
    <div class="actions">
      <button onclick="copyScript()">Copy Script</button>
      <span id="cs" class="copied"></span>
    </div>
  </div>
  <div class="card">
    <h2>Pexels Links</h2>
    {% if pexels_links %}
      <ul class="plain">{% for i in pexels_links %}<li><a href="{{ i.url }}" target="_blank">{{ i.keyword }}</a></li>{% endfor %}</ul>
    {% else %}<p style="color:var(--muted)">PEXELS_API_KEY not set.</p>{% endif %}
  </div>
  <div class="card">
    <h2>Keywords</h2>
    <ul class="plain">{% for k in keywords %}<li>{{ k }}</li>{% endfor %}</ul>
  </div>
  <div class="card">
    <h2>Music Mood</h2>
    <p style="color:var(--gold);font-size:1.1rem;font-weight:800">{{ music_mood }}</p>
    <p style="color:var(--muted);font-size:.82rem;margin-top:6px">{{ model_used }}</p>
  </div>
</div>
</main>
<script>
async function copyScript(){
  const t=document.getElementById('script').innerText;
  const s=document.getElementById('cs');
  try{await navigator.clipboard.writeText(t);s.textContent='Copied';}
  catch{s.textContent='Select and copy manually';}
}
</script></body></html>
"""

# ─────────────────────────────────────────────
#  USER MANAGEMENT PAGE
# ─────────────────────────────────────────────
USERS_PAGE = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wealth Vault — User Management</title>
<style>
  :root{--gold:#D4AF37;--gold-dim:rgba(212,175,55,.08);--gold-border:rgba(212,175,55,.28);--muted:#7a7a7a;--card:#0c0c0c;--radius:12px;}
  *{box-sizing:border-box;margin:0;padding:0;}
  body{background:#000;color:#e8e0d0;font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh;}
  nav{display:flex;align-items:center;justify-content:space-between;padding:0 24px;height:52px;border-bottom:1px solid #111;background:#000;position:sticky;top:0;z-index:100;}
  .nav-brand{font-size:.82rem;font-weight:900;letter-spacing:.18em;text-transform:uppercase;color:var(--gold);}
  .nav-links{display:flex;gap:6px;}
  .nav-links a{font-size:.75rem;font-weight:800;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);text-decoration:none;padding:6px 13px;border-radius:7px;transition:color .15s,background .15s;}
  .nav-links a:hover,.nav-links a.active{color:var(--gold);background:var(--gold-dim);}
  .page{max-width:860px;margin:40px auto;padding:0 20px 60px;}
  h1{font-size:1.35rem;font-weight:900;letter-spacing:.08em;text-transform:uppercase;color:var(--gold);margin-bottom:6px;}
  .subtitle{font-size:.8rem;color:var(--muted);margin-bottom:28px;}
  .flash{padding:10px 16px;border-radius:8px;font-size:.82rem;font-weight:700;margin-bottom:18px;}
  .flash.success{background:rgba(212,175,55,.12);border:1px solid var(--gold-border);color:var(--gold);}
  .flash.error{background:rgba(220,50,50,.1);border:1px solid rgba(220,50,50,.3);color:#f87171;}
  table{width:100%;border-collapse:collapse;background:var(--card);border-radius:var(--radius);overflow:hidden;border:1px solid #1a1a1a;}
  th{font-size:.68rem;font-weight:900;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);padding:12px 16px;text-align:left;border-bottom:1px solid #181818;background:#080808;}
  td{padding:14px 16px;border-bottom:1px solid #111;font-size:.82rem;vertical-align:middle;}
  tr:last-child td{border-bottom:none;}
  tr:hover td{background:#0d0d0d;}
  .avatar{width:30px;height:30px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:11px;font-weight:900;color:#000;flex-shrink:0;vertical-align:middle;}
  .badge-admin{display:inline-block;font-size:.62rem;font-weight:900;letter-spacing:.1em;text-transform:uppercase;color:var(--gold);background:var(--gold-dim);border:1px solid var(--gold-border);padding:2px 8px;border-radius:5px;}
  .badge-viewer{display:inline-block;font-size:.62rem;font-weight:900;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);background:#0d0d0d;border:1px solid #222;padding:2px 8px;border-radius:5px;}
  .badge-google{display:inline-block;font-size:.6rem;font-weight:800;letter-spacing:.06em;text-transform:uppercase;color:#4285f4;background:rgba(66,133,244,.08);border:1px solid rgba(66,133,244,.25);padding:2px 7px;border-radius:5px;}
  .badge-local{display:inline-block;font-size:.6rem;font-weight:800;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);background:#0a0a0a;border:1px solid #1e1e1e;padding:2px 7px;border-radius:5px;}
  .btn{display:inline-block;font-size:.7rem;font-weight:900;letter-spacing:.07em;text-transform:uppercase;padding:5px 13px;border-radius:7px;border:none;cursor:pointer;text-decoration:none;transition:opacity .15s;}
  .btn:hover{opacity:.8;}
  .btn:disabled{opacity:.3;cursor:not-allowed;}
  .btn-gold{background:var(--gold);color:#000;}
  .btn-dim{background:#141414;color:var(--muted);border:1px solid #222;}
  .btn-danger{background:rgba(220,50,50,.15);color:#f87171;border:1px solid rgba(220,50,50,.3);}
  .actions{display:flex;gap:8px;flex-wrap:wrap;}
  .you-tag{font-size:.6rem;font-weight:900;color:var(--muted);background:#111;border:1px solid #1e1e1e;padding:2px 6px;border-radius:4px;margin-left:6px;vertical-align:middle;}
  .count-bar{display:flex;align-items:center;gap:10px;margin-bottom:20px;}
  .count-pill{font-size:.72rem;font-weight:800;padding:4px 12px;border-radius:20px;}
  .count-admin{background:var(--gold-dim);border:1px solid var(--gold-border);color:var(--gold);}
  .count-viewer{background:#0d0d0d;border:1px solid #222;color:var(--muted);}
</style>
</head><body>
<nav>
  <span class="nav-brand">&#9679; Wealth Vault</span>
  <div class="nav-links">
    <a href="{{ url_for('index') }}">Command Center</a>
    <a href="{{ url_for('view_dashboard') }}">Dashboard</a>
    <a href="{{ url_for('view_ab_tests') }}">A/B Tests</a>
    <a href="{{ url_for('view_users') }}" class="active">Users</a>
    <a href="{{ url_for('admin_settings') }}">Settings</a>
  </div>
  <div style="display:flex;align-items:center;gap:8px;margin-right:8px;">
    {% if session.get('picture') %}
      <img src="{{ session.get('picture') }}" width="26" height="26" style="border-radius:50%;border:1.5px solid var(--gold-border);object-fit:cover;" alt="">
    {% else %}
      <div class="avatar" style="width:26px;height:26px;background:linear-gradient(135deg,var(--gold),#5c3e00);">{{ (session.get('username','?')[0])|upper }}</div>
    {% endif %}
    <span style="font-size:.75rem;color:var(--muted);font-weight:700;max-width:90px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{{ session.get('username','') }}</span>
    <span class="badge-admin">Admin</span>
    <a href="{{ url_for('logout') }}" style="font-size:.72rem;font-weight:900;letter-spacing:.08em;text-transform:uppercase;color:var(--gold);border:1px solid var(--gold-border);background:var(--gold-dim);padding:5px 12px;border-radius:7px;text-decoration:none;">Logout</a>
  </div>
</nav>

<div class="page">
  <h1>User Management</h1>
  <p class="subtitle">Manage who can access the Vault and what they can do. You cannot remove your own account.</p>

  {% with messages = get_flashed_messages(with_categories=true) %}
    {% for cat, msg in messages %}
      <div class="flash {{ cat }}">{{ msg }}</div>
    {% endfor %}
  {% endwith %}

  <!-- INVITE-ONLY TOGGLE -->
  <div style="background:var(--card);border:1px solid #1a1a1a;border-radius:var(--radius);padding:18px 22px;margin-bottom:24px;display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;">
    <div>
      <div style="font-size:.82rem;font-weight:900;letter-spacing:.05em;text-transform:uppercase;color:{% if invite_only %}var(--gold){% else %}var(--muted){% endif %};">
        {% if invite_only %}&#128274; Invite-only mode ON{% else %}&#128275; Open signup{% endif %}
      </div>
      <div style="font-size:.74rem;color:var(--muted);margin-top:4px;">
        {% if invite_only %}Only people with a valid invite link can create accounts.{% else %}Anyone who finds the signup page can create a Viewer account.{% endif %}
      </div>
    </div>
    <form method="POST" action="{{ url_for('toggle_invite_only') }}" style="flex-shrink:0;">
      {% if invite_only %}
        <button type="submit" class="btn btn-dim" style="white-space:nowrap;">Disable Invite-only</button>
      {% else %}
        <button type="submit" class="btn btn-gold" style="white-space:nowrap;">Enable Invite-only</button>
      {% endif %}
    </form>
  </div>

  <!-- API KEY STATUS PANEL -->
  {% set key_rows = [
    ("GEMINI_API_KEY",       "Gemini Flash",       "Script generation (primary LLM)"),
    ("OPENROUTER_API_KEY",   "OpenRouter",         "429 fallback bridge + auto-patch"),
    ("ELEVENLABS_API_KEY",   "ElevenLabs",         "Voice tier 1 — Sage Mentor (highest quality)"),
    ("DEEPGRAM_API_KEY",     "Deepgram Aura",      "Voice tier 2 — fallback if ElevenLabs fails"),
    ("FISH_AUDIO_API_KEY",   "Fish Audio",         "Voice tier 3 — fallback if Deepgram fails"),
    ("PEXELS_API_KEY",       "Pexels",             "B-roll video clips"),
    ("SESSION_SECRET",       "Session Secret",     "Flask session security — required"),
  ] %}
  <div style="margin-bottom:24px;">
    <div style="font-size:.82rem;font-weight:900;letter-spacing:.08em;text-transform:uppercase;color:var(--gold);margin-bottom:10px;">API Key Status</div>
    <div style="background:var(--card);border:1px solid #1a1a1a;border-radius:var(--radius);overflow:hidden;">
      {% for key, label, note in key_rows %}
      <div style="display:flex;align-items:center;gap:12px;padding:11px 18px;border-bottom:1px solid #111;{% if loop.last %}border-bottom:none;{% endif %}">
        {% if api_keys[key] %}
          <span style="width:8px;height:8px;border-radius:50%;background:#22c55e;flex-shrink:0;"></span>
          <span style="font-size:.8rem;font-weight:800;color:#e8e0d0;min-width:130px;">{{ label }}</span>
          <span style="font-family:monospace;font-size:.74rem;color:var(--muted);">{{ api_keys[key] }}</span>
        {% else %}
          <span style="width:8px;height:8px;border-radius:50%;background:#f87171;flex-shrink:0;"></span>
          <span style="font-size:.8rem;font-weight:800;color:#f87171;min-width:130px;">{{ label }}</span>
          <span style="font-size:.74rem;color:var(--muted);">NOT SET — {{ note }}</span>
        {% endif %}
      </div>
      {% endfor %}
    </div>
  </div>

  <!-- SYSTEM ALERTS -->
  {% if alerts %}
  <div style="margin-bottom:24px;">
    <div style="font-size:.82rem;font-weight:900;letter-spacing:.08em;text-transform:uppercase;color:#f87171;margin-bottom:10px;">System Alerts <span style="color:var(--muted);font-weight:400;font-size:.72rem;text-transform:none;letter-spacing:0;">({{ alerts|length }} unacknowledged)</span></div>
    {% for alert in alerts %}
    <div style="background:#0d0505;border:1px solid rgba(220,50,50,.25);border-radius:var(--radius);padding:14px 18px;margin-bottom:8px;">
      <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:10px;flex-wrap:wrap;">
        <div style="flex:1;">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
            <span style="font-size:.62rem;font-weight:900;text-transform:uppercase;letter-spacing:.08em;padding:2px 7px;border-radius:4px;
              {% if alert.category == 'crash' %}background:rgba(239,68,68,.15);color:#f87171;border:1px solid rgba(239,68,68,.3);
              {% elif alert.category == 'token_error' %}background:rgba(251,191,36,.1);color:#fbbf24;border:1px solid rgba(251,191,36,.25);
              {% elif alert.category == 'rate_limit' %}background:rgba(99,102,241,.1);color:#818cf8;border:1px solid rgba(99,102,241,.25);
              {% else %}background:var(--gold-dim);color:var(--gold);border:1px solid var(--gold-border);{% endif %}
              ">{{ alert.category }}</span>
            <span style="font-size:.7rem;color:var(--muted);">{{ alert.ts }}</span>
          </div>
          <div style="font-size:.83rem;font-weight:700;color:#e8e0d0;margin-bottom:{% if alert.patch_suggestion %}8px{% else %}0{% endif %};">{{ alert.message }}</div>
          {% if alert.patch_suggestion %}
          <details style="margin-top:6px;">
            <summary style="font-size:.72rem;color:var(--gold);cursor:pointer;font-weight:800;letter-spacing:.04em;text-transform:uppercase;">Auto-Patch Suggestion</summary>
            <pre style="margin-top:8px;font-size:.72rem;color:#a0e0a0;background:#050f05;border:1px solid #1a2e1a;border-radius:7px;padding:10px 14px;overflow-x:auto;line-height:1.55;white-space:pre-wrap;">{{ alert.patch_suggestion }}</pre>
          </details>
          {% endif %}
        </div>
        <form method="POST" action="/admin/alerts/{{ loop.index0 }}/ack" style="flex-shrink:0;">
          <button type="submit" class="btn btn-dim" style="padding:4px 10px;font-size:.65rem;">Dismiss</button>
        </form>
      </div>
    </div>
    {% endfor %}
  </div>
  {% endif %}

  <div class="count-bar">
    <span class="count-pill count-admin">{{ admins }} Admin{% if admins != 1 %}s{% endif %}</span>
    <span class="count-pill count-viewer">{{ viewers }} Viewer{% if viewers != 1 %}s{% endif %}</span>
    <span style="font-size:.72rem;color:var(--muted);margin-left:4px;">{{ total }} account{% if total != 1 %}s{% endif %} total</span>
  </div>

  <table>
    <thead>
      <tr>
        <th>User</th>
        <th>Role</th>
        <th>Auth</th>
        <th>Joined</th>
        <th>Actions</th>
      </tr>
    </thead>
    <tbody>
      {% for u in users %}
      <tr>
        <td>
          {% if u.picture %}
            <img src="{{ u.picture }}" width="30" height="30" style="border-radius:50%;border:1.5px solid var(--gold-border);object-fit:cover;vertical-align:middle;margin-right:8px;" alt="">
          {% else %}
            <span class="avatar" style="background:linear-gradient(135deg,var(--gold),#5c3e00);margin-right:8px;">{{ (u.username[0])|upper }}</span>
          {% endif %}
          <strong style="font-size:.85rem;">{{ u.username }}</strong>
          {% if u.username == session.get('username') %}<span class="you-tag">you</span>{% endif %}
          {% if u.email %}<br><span style="font-size:.7rem;color:var(--muted);margin-left:38px;">{{ u.email }}</span>{% endif %}
        </td>
        <td>
          {% if u.role == 'admin' %}
            <span class="badge-admin">Admin</span>
          {% else %}
            <span class="badge-viewer">Viewer</span>
          {% endif %}
        </td>
        <td>
          {% if u.google_id %}
            <span class="badge-google">Google</span>
          {% else %}
            <span class="badge-local">Local</span>
          {% endif %}
        </td>
        <td style="color:var(--muted);font-size:.76rem;white-space:nowrap;">{{ u.created_at or '—' }}</td>
        <td>
          <div class="actions">
            {% if u.username != session.get('username') %}
              <form method="POST" action="{{ url_for('change_user_role', username=u.username) }}" style="display:inline;">
                {% if u.role == 'admin' %}
                  <button type="submit" class="btn btn-dim" title="Demote to Viewer">Demote</button>
                {% else %}
                  <button type="submit" class="btn btn-gold" title="Promote to Admin">Promote</button>
                {% endif %}
              </form>
              <form method="POST" action="{{ url_for('delete_user', username=u.username) }}" style="display:inline;"
                    onsubmit="return confirm('Delete {{ u.username }}? This cannot be undone.')">
                <button type="submit" class="btn btn-danger">Delete</button>
              </form>
            {% else %}
              <span style="font-size:.72rem;color:var(--muted);">—</span>
            {% endif %}
          </div>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

  <!-- INVITE LINKS SECTION -->
  <div style="margin-top:36px;">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:16px;">
      <div>
        <div style="font-size:1rem;font-weight:900;letter-spacing:.06em;text-transform:uppercase;color:var(--gold);">Invite Links</div>
        <div style="font-size:.74rem;color:var(--muted);margin-top:3px;">Each link is single-use and expires in 48 hours. Share directly — no password needed.</div>
      </div>
      <form method="POST" action="{{ url_for('create_invite') }}" style="flex-shrink:0;">
        <button type="submit" class="btn btn-gold" style="white-space:nowrap;">+ Generate Invite Link</button>
      </form>
    </div>

    {% if active_invites %}
    <table>
      <thead>
        <tr>
          <th>Invite Link</th>
          <th>Created By</th>
          <th>Expires</th>
          <th>Action</th>
        </tr>
      </thead>
      <tbody>
        {% for inv in active_invites %}
        <tr>
          <td style="max-width:340px;">
            <div style="display:flex;align-items:center;gap:8px;">
              <code id="inv-{{ loop.index }}" style="font-size:.72rem;color:var(--gold);background:#0a0a0a;border:1px solid #1e1e1e;padding:5px 10px;border-radius:7px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:220px;display:block;">{{ base_url }}/login?invite={{ inv.token }}</code>
              <button onclick="copyInvite('inv-{{ loop.index }}')" class="btn btn-dim" style="padding:5px 10px;white-space:nowrap;flex-shrink:0;">Copy</button>
            </div>
          </td>
          <td style="color:var(--muted);font-size:.8rem;">{{ inv.created_by }}</td>
          <td style="color:var(--muted);font-size:.76rem;white-space:nowrap;">{{ inv.expires_at }}</td>
          <td>
            <form method="POST" action="{{ url_for('revoke_invite', token=inv.token) }}"
                  onsubmit="return confirm('Revoke this invite? The link will stop working immediately.')">
              <button type="submit" class="btn btn-danger">Revoke</button>
            </form>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    {% else %}
    <div style="padding:28px;text-align:center;color:var(--muted);border:1px dashed #1a1a1a;border-radius:var(--radius);font-size:.82rem;">
      No active invites. Generate one above to share access.
    </div>
    {% endif %}
  </div>
</div>

<script>
function copyInvite(id) {
  const el = document.getElementById(id);
  navigator.clipboard.writeText(el.textContent.trim()).then(() => {
    const btn = el.nextElementSibling;
    const orig = btn.textContent;
    btn.textContent = 'Copied!';
    btn.style.color = 'var(--gold)';
    setTimeout(() => { btn.textContent = orig; btn.style.color = ''; }, 2000);
  });
}
</script>
</body></html>
"""

# ─────────────────────────────────────────────
#  SETTINGS PAGE
# ─────────────────────────────────────────────
SETTINGS_PAGE = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wealth Vault — API Settings</title>
<style>
  :root{--gold:#D4AF37;--gold-dim:rgba(212,175,55,.08);--gold-border:rgba(212,175,55,.28);--muted:#7a7a7a;--card:#0c0c0c;--radius:12px;}
  *{box-sizing:border-box;margin:0;padding:0;}
  body{background:#000;color:#e8e0d0;font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh;}
  nav{display:flex;align-items:center;justify-content:space-between;padding:0 24px;height:52px;border-bottom:1px solid #111;background:#000;position:sticky;top:0;z-index:100;}
  .nav-brand{font-size:.82rem;font-weight:900;letter-spacing:.18em;text-transform:uppercase;color:var(--gold);}
  .nav-links{display:flex;gap:6px;}
  .nav-links a{font-size:.75rem;font-weight:800;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);text-decoration:none;padding:6px 13px;border-radius:7px;transition:color .15s,background .15s;}
  .nav-links a:hover,.nav-links a.active{color:var(--gold);background:var(--gold-dim);}
  .page{max-width:720px;margin:40px auto;padding:0 20px 80px;}
  h1{font-size:1.35rem;font-weight:900;letter-spacing:.08em;text-transform:uppercase;color:var(--gold);margin-bottom:6px;}
  .subtitle{font-size:.8rem;color:var(--muted);margin-bottom:32px;line-height:1.6;}
  .flash{padding:10px 16px;border-radius:8px;font-size:.82rem;font-weight:700;margin-bottom:18px;}
  .flash.success{background:rgba(212,175,55,.12);border:1px solid var(--gold-border);color:var(--gold);}
  .flash.error{background:rgba(220,50,50,.1);border:1px solid rgba(220,50,50,.3);color:#f87171;}
  .flash.info{background:rgba(99,102,241,.08);border:1px solid rgba(99,102,241,.25);color:#818cf8;}
  .section-label{font-size:.68rem;font-weight:900;letter-spacing:.14em;text-transform:uppercase;color:var(--gold);margin-bottom:14px;padding-bottom:8px;border-bottom:1px solid #1a1a1a;}
  .card{background:var(--card);border:1px solid #1a1a1a;border-radius:var(--radius);padding:24px;margin-bottom:20px;}
  .key-row{margin-bottom:22px;border-bottom:1px solid #111;padding-bottom:20px;}
  .key-row:last-child{margin-bottom:0;border-bottom:none;padding-bottom:0;}
  .key-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;gap:8px;}
  .key-label{font-size:.78rem;font-weight:900;letter-spacing:.05em;color:#e8e0d0;}
  .key-desc{font-size:.72rem;color:var(--muted);line-height:1.5;margin-bottom:8px;}
  .status-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0;}
  .dot-ok{background:#22c55e;box-shadow:0 0 6px #22c55e;}
  .dot-missing{background:#f87171;}
  .dot-warn{background:var(--gold);box-shadow:0 0 6px var(--gold);}
  .current-val{font-family:monospace;font-size:.74rem;color:var(--muted);background:#080808;border:1px solid #1a1a1a;padding:3px 9px;border-radius:5px;}
  input[type=text],input[type=password]{width:100%;background:#080808;border:1px solid #1e1e1e;border-radius:9px;color:#e8e0d0;padding:10px 14px;font:inherit;font-size:.88rem;transition:border-color .2s,box-shadow .2s;outline:none;font-family:monospace;}
  input[type=text]:focus,input[type=password]:focus{border-color:var(--gold-border);box-shadow:0 0 0 3px rgba(212,175,55,.08);}
  input::placeholder{color:#333;font-family:monospace;}
  .btn-save{display:inline-flex;align-items:center;gap:8px;background:var(--gold);color:#000;font-size:.8rem;font-weight:900;letter-spacing:.07em;text-transform:uppercase;padding:11px 28px;border-radius:10px;border:none;cursor:pointer;transition:opacity .15s;margin-top:4px;}
  .btn-save:hover{opacity:.85;}
  .btn-remove{display:inline-flex;align-items:center;gap:5px;background:transparent;color:#f87171;border:1px solid rgba(248,113,113,.3);font-size:.66rem;font-weight:900;letter-spacing:.07em;text-transform:uppercase;padding:4px 11px;border-radius:6px;cursor:pointer;transition:background .15s,border-color .15s;}
  .btn-remove:hover{background:rgba(248,113,113,.1);border-color:rgba(248,113,113,.5);}
  .btn-action{display:inline-flex;align-items:center;gap:6px;background:#0c0c0c;color:var(--gold);border:1px solid var(--gold-border);font-size:.76rem;font-weight:900;letter-spacing:.06em;text-transform:uppercase;padding:9px 20px;border-radius:9px;cursor:pointer;transition:background .15s;}
  .btn-action:hover{background:var(--gold-dim);}
  .hint{font-size:.72rem;color:var(--muted);margin-top:6px;line-height:1.5;}
  .warn-box{background:rgba(212,175,55,.06);border:1px solid var(--gold-border);border-radius:10px;padding:14px 18px;margin-bottom:24px;font-size:.8rem;color:var(--muted);line-height:1.6;}
  .warn-box strong{color:var(--gold);}
  .key-env-name{font-family:monospace;font-size:.7rem;color:#555;background:#0a0a0a;border:1px solid #1a1a1a;padding:2px 7px;border-radius:4px;}
  .source-badge{font-size:.62rem;font-weight:900;letter-spacing:.07em;text-transform:uppercase;padding:2px 7px;border-radius:4px;}
  .src-env{background:rgba(99,102,241,.1);border:1px solid rgba(99,102,241,.25);color:#818cf8;}
  .src-saved{background:rgba(34,197,94,.1);border:1px solid rgba(34,197,94,.25);color:#86efac;}
  .src-none{background:#0d0d0d;border:1px solid #1e1e1e;color:var(--muted);}
  .key-input-row{display:flex;align-items:center;gap:8px;}
  .key-input-row input{flex:1;}
  .retention-stat{text-align:center;padding:14px 18px;background:#080808;border:1px solid #1a1a1a;border-radius:10px;}
  .retention-stat .val{font-size:1.5rem;font-weight:900;color:var(--gold);line-height:1;}
  .retention-stat .lbl{font-size:.65rem;font-weight:900;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-top:5px;}
</style></head>
<body>
<nav>
  <span class="nav-brand">&#9679; Wealth Vault</span>
  <div class="nav-links">
    <a href="{{ url_for('index') }}">Command Center</a>
    <a href="{{ url_for('view_dashboard') }}">Dashboard</a>
    <a href="{{ url_for('view_users') }}">Users</a>
    <a href="{{ url_for('admin_settings') }}" class="active">Settings</a>
  </div>
  <div style="display:flex;align-items:center;gap:8px;margin-right:8px;">
    {% if session.get('picture') %}
      <img src="{{ session.get('picture') }}" width="26" height="26" style="border-radius:50%;border:1.5px solid var(--gold-border);object-fit:cover;" alt="">
    {% else %}
      <div style="width:26px;height:26px;border-radius:50%;background:linear-gradient(135deg,var(--gold),#5c3e00);display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:900;color:#000;">{{ (session.get('username','?')[0])|upper }}</div>
    {% endif %}
    <span style="font-size:.75rem;color:var(--muted);font-weight:700;">{{ session.get('username','') }}</span>
    <a href="{{ url_for('logout') }}" style="font-size:.72rem;font-weight:900;letter-spacing:.08em;text-transform:uppercase;color:var(--gold);border:1px solid var(--gold-border);background:var(--gold-dim);padding:5px 12px;border-radius:7px;text-decoration:none;">Logout</a>
  </div>
</nav>

<div class="page">
  <h1>API Settings</h1>
  <p class="subtitle">Enter your API keys to power the engine. Keys saved here are stored securely in the Vault data folder and loaded at startup. Leave a field blank to keep the existing value.</p>

  {% with messages = get_flashed_messages(with_categories=true) %}
    {% for cat, msg in messages %}
      <div class="flash {{ cat }}">{{ msg }}</div>
    {% endfor %}
  {% endwith %}

  <div class="warn-box">
    <strong>&#9888; Heads up:</strong> API keys saved here are stored in <code>data/vault_settings.json</code>. If you have already set a key as a Replit Secret (environment variable), that value takes priority and you do not need to re-enter it here — it will show as <span class="source-badge src-env">ENV</span>.
  </div>

  <form method="POST" action="{{ url_for('admin_settings_post') }}">
    <div class="card">
      <div class="section-label">AI & Script Generation</div>

      <div class="key-row">
        <div class="key-header">
          <span class="key-label">Gemini API Key</span>
          <div style="display:flex;align-items:center;gap:8px;">
            <span class="key-env-name">GEMINI_API_KEY</span>
            {% if keys.GEMINI_API_KEY.source == 'env' %}
              <span class="source-badge src-env">ENV</span>
              <span class="status-dot dot-ok"></span>
            {% elif keys.GEMINI_API_KEY.source == 'saved' %}
              <span class="source-badge src-saved">Saved</span>
              <span class="status-dot dot-ok"></span>
            {% else %}
              <span class="source-badge src-none">Not set</span>
              <span class="status-dot dot-missing"></span>
            {% endif %}
          </div>
        </div>
        <p class="key-desc">Powers the script generation pipeline (primary LLM). Required for the Omni Engine to run.</p>
        {% if keys.GEMINI_API_KEY.masked %}
          <div style="margin-bottom:8px;"><span class="current-val">Current: {{ keys.GEMINI_API_KEY.masked }}</span></div>
        {% endif %}
        <div class="key-input-row">
          <input type="password" name="GEMINI_API_KEY" placeholder="Enter new key to update, or leave blank to keep existing" autocomplete="off">
          {% if keys.GEMINI_API_KEY.source == 'saved' %}
          <form method="POST" action="{{ url_for('admin_settings_clear_key') }}" onsubmit="return confirm('Remove GEMINI_API_KEY from the vault?')">
            <input type="hidden" name="key_name" value="GEMINI_API_KEY">
            <button type="submit" class="btn-remove">&#215; Remove</button>
          </form>
          {% endif %}
        </div>
      </div>

      <div class="key-row">
        <div class="key-header">
          <span class="key-label">OpenRouter API Key</span>
          <div style="display:flex;align-items:center;gap:8px;">
            <span class="key-env-name">OPENROUTER_API_KEY</span>
            {% if keys.OPENROUTER_API_KEY.source == 'env' %}
              <span class="source-badge src-env">ENV</span>
              <span class="status-dot dot-ok"></span>
            {% elif keys.OPENROUTER_API_KEY.source == 'saved' %}
              <span class="source-badge src-saved">Saved</span>
              <span class="status-dot dot-ok"></span>
            {% else %}
              <span class="source-badge src-none">Not set</span>
              <span class="status-dot dot-warn"></span>
            {% endif %}
          </div>
        </div>
        <p class="key-desc">Optional fallback bridge used when Gemini hits rate limits. Also powers the auto-patch crash recovery.</p>
        {% if keys.OPENROUTER_API_KEY.masked %}
          <div style="margin-bottom:8px;"><span class="current-val">Current: {{ keys.OPENROUTER_API_KEY.masked }}</span></div>
        {% endif %}
        <div class="key-input-row">
          <input type="password" name="OPENROUTER_API_KEY" placeholder="Enter new key to update, or leave blank to keep existing" autocomplete="off">
          {% if keys.OPENROUTER_API_KEY.source == 'saved' %}
          <form method="POST" action="{{ url_for('admin_settings_clear_key') }}" onsubmit="return confirm('Remove OPENROUTER_API_KEY from the vault?')">
            <input type="hidden" name="key_name" value="OPENROUTER_API_KEY">
            <button type="submit" class="btn-remove">&#215; Remove</button>
          </form>
          {% endif %}
        </div>
      </div>
    </div>

    <div class="card">
      <div class="section-label">Voice & Media</div>

      <div class="key-row">
        <div class="key-header">
          <span class="key-label">ElevenLabs API Key</span>
          <div style="display:flex;align-items:center;gap:8px;">
            <span class="key-env-name">ELEVENLABS_API_KEY</span>
            {% if keys.ELEVENLABS_API_KEY.source == 'env' %}
              <span class="source-badge src-env">ENV</span>
              <span class="status-dot dot-ok"></span>
            {% elif keys.ELEVENLABS_API_KEY.source == 'saved' %}
              <span class="source-badge src-saved">Saved</span>
              <span class="status-dot dot-ok"></span>
            {% else %}
              <span class="source-badge src-none">Not set</span>
              <span class="status-dot dot-warn"></span>
            {% endif %}
          </div>
        </div>
        <p class="key-desc">Voice Tier 1 — Sage Mentor (Brian). Highest quality. Falls through to Deepgram → Fish Audio → gTTS if unavailable.</p>
        {% if keys.ELEVENLABS_API_KEY.masked %}
          <div style="margin-bottom:8px;"><span class="current-val">Current: {{ keys.ELEVENLABS_API_KEY.masked }}</span></div>
        {% endif %}
        <div class="key-input-row">
          <input type="password" name="ELEVENLABS_API_KEY" placeholder="Enter new key to update, or leave blank to keep existing" autocomplete="off">
          {% if keys.ELEVENLABS_API_KEY.source == 'saved' %}
          <form method="POST" action="{{ url_for('admin_settings_clear_key') }}" onsubmit="return confirm('Remove ELEVENLABS_API_KEY from the vault?')">
            <input type="hidden" name="key_name" value="ELEVENLABS_API_KEY">
            <button type="submit" class="btn-remove">&#215; Remove</button>
          </form>
          {% endif %}
        </div>
      </div>

      <div class="key-row">
        <div class="key-header">
          <span class="key-label">Deepgram API Key</span>
          <div style="display:flex;align-items:center;gap:8px;">
            <span class="key-env-name">DEEPGRAM_API_KEY</span>
            {% if keys.DEEPGRAM_API_KEY.source == 'env' %}
              <span class="source-badge src-env">ENV</span>
              <span class="status-dot dot-ok"></span>
            {% elif keys.DEEPGRAM_API_KEY.source == 'saved' %}
              <span class="source-badge src-saved">Saved</span>
              <span class="status-dot dot-ok"></span>
            {% else %}
              <span class="source-badge src-none">Not set</span>
              <span class="status-dot dot-warn"></span>
            {% endif %}
          </div>
        </div>
        <p class="key-desc">Voice Tier 2 — Deepgram Aura (aura-asteria-en). Near-real-time, reliable. Activates automatically if ElevenLabs fails.</p>
        {% if keys.DEEPGRAM_API_KEY.masked %}
          <div style="margin-bottom:8px;"><span class="current-val">Current: {{ keys.DEEPGRAM_API_KEY.masked }}</span></div>
        {% endif %}
        <div class="key-input-row">
          <input type="password" name="DEEPGRAM_API_KEY" placeholder="Enter new key to update, or leave blank to keep existing" autocomplete="off">
          {% if keys.DEEPGRAM_API_KEY.source == 'saved' %}
          <form method="POST" action="{{ url_for('admin_settings_clear_key') }}" onsubmit="return confirm('Remove DEEPGRAM_API_KEY from the vault?')">
            <input type="hidden" name="key_name" value="DEEPGRAM_API_KEY">
            <button type="submit" class="btn-remove">&#215; Remove</button>
          </form>
          {% endif %}
        </div>
      </div>

      <div class="key-row">
        <div class="key-header">
          <span class="key-label">Fish Audio API Key</span>
          <div style="display:flex;align-items:center;gap:8px;">
            <span class="key-env-name">FISH_AUDIO_API_KEY</span>
            {% if keys.FISH_AUDIO_API_KEY.source == 'env' %}
              <span class="source-badge src-env">ENV</span>
              <span class="status-dot dot-ok"></span>
            {% elif keys.FISH_AUDIO_API_KEY.source == 'saved' %}
              <span class="source-badge src-saved">Saved</span>
              <span class="status-dot dot-ok"></span>
            {% else %}
              <span class="source-badge src-none">Not set</span>
              <span class="status-dot dot-warn"></span>
            {% endif %}
          </div>
        </div>
        <p class="key-desc">Voice Tier 3 — Fish Audio streaming TTS. Final paid fallback before free gTTS safety net.</p>
        {% if keys.FISH_AUDIO_API_KEY.masked %}
          <div style="margin-bottom:8px;"><span class="current-val">Current: {{ keys.FISH_AUDIO_API_KEY.masked }}</span></div>
        {% endif %}
        <div class="key-input-row">
          <input type="password" name="FISH_AUDIO_API_KEY" placeholder="Enter new key to update, or leave blank to keep existing" autocomplete="off">
          {% if keys.FISH_AUDIO_API_KEY.source == 'saved' %}
          <form method="POST" action="{{ url_for('admin_settings_clear_key') }}" onsubmit="return confirm('Remove FISH_AUDIO_API_KEY from the vault?')">
            <input type="hidden" name="key_name" value="FISH_AUDIO_API_KEY">
            <button type="submit" class="btn-remove">&#215; Remove</button>
          </form>
          {% endif %}
        </div>
      </div>

      <div class="key-row">
        <div class="key-header">
          <span class="key-label">Pexels API Key</span>
          <div style="display:flex;align-items:center;gap:8px;">
            <span class="key-env-name">PEXELS_API_KEY</span>
            {% if keys.PEXELS_API_KEY.source == 'env' %}
              <span class="source-badge src-env">ENV</span>
              <span class="status-dot dot-ok"></span>
            {% elif keys.PEXELS_API_KEY.source == 'saved' %}
              <span class="source-badge src-saved">Saved</span>
              <span class="status-dot dot-ok"></span>
            {% else %}
              <span class="source-badge src-none">Not set</span>
              <span class="status-dot dot-missing"></span>
            {% endif %}
          </div>
        </div>
        <p class="key-desc">Fetches 6-8 vertical B-roll video clips per Short from Pexels. Required for full video rendering.</p>
        {% if keys.PEXELS_API_KEY.masked %}
          <div style="margin-bottom:8px;"><span class="current-val">Current: {{ keys.PEXELS_API_KEY.masked }}</span></div>
        {% endif %}
        <div class="key-input-row">
          <input type="password" name="PEXELS_API_KEY" placeholder="Enter new key to update, or leave blank to keep existing" autocomplete="off">
          {% if keys.PEXELS_API_KEY.source == 'saved' %}
          <form method="POST" action="{{ url_for('admin_settings_clear_key') }}" onsubmit="return confirm('Remove PEXELS_API_KEY from the vault?')">
            <input type="hidden" name="key_name" value="PEXELS_API_KEY">
            <button type="submit" class="btn-remove">&#215; Remove</button>
          </form>
          {% endif %}
        </div>
      </div>
    </div>

    <button type="submit" class="btn-save">&#9654; Save Keys</button>
    <p class="hint" style="margin-top:12px;">Saved keys are applied immediately — no restart needed. Keys set as Replit Secrets always take priority over keys saved here.</p>
  </form>

  <!-- RETENTION ANALYTICS LOOP SECTION -->
  <div style="margin-top:32px;">
    <div class="section-label" style="margin-bottom:14px;">Retention Analytics Loop</div>
    <div class="card">
      <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:18px;">
        <div style="flex:1;min-width:200px;">
          <div style="font-size:.82rem;font-weight:900;color:#e8e0d0;margin-bottom:6px;">48-Hour Performance Feedback</div>
          <p class="key-desc" style="margin-bottom:0;">
            After each video turns 48 hours old, this loop fetches its real views, watch time, and average retention % from YouTube Analytics. It scores every hook pattern and automatically updates the script prompt to lean harder into whatever structure is keeping viewers watching. Runs daily at 06:30 UTC — or trigger it manually below.
          </p>
        </div>
        <form method="POST" action="{{ url_for('admin_run_retention') }}" style="flex-shrink:0;">
          <button type="submit" class="btn-action">&#9654; Run Analysis Now</button>
        </form>
      </div>
      {% if retention_scores %}
      <div style="margin-bottom:16px;">
        <div style="font-size:.65rem;font-weight:900;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);margin-bottom:10px;">Latest Hook Scores</div>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px;">
          {% for s in retention_scores[:4] %}
          <div class="retention-stat">
            <div class="val">{{ s.retention_pct }}%</div>
            <div style="font-size:.7rem;color:#a0916c;margin:5px 0 3px;line-height:1.3;font-weight:700;">{{ s.hook[:48] }}{% if s.hook|length > 48 %}…{% endif %}</div>
            <div class="lbl">{{ s.views|int }} views &bull; score {{ "%.1f"|format(s.score) }}</div>
          </div>
          {% endfor %}
        </div>
      </div>
      {% if retention_avg %}
      <div style="background:#080808;border:1px solid #1a1a1a;border-radius:9px;padding:12px 16px;font-size:.78rem;color:var(--muted);line-height:1.6;">
        <strong style="color:var(--gold);">Channel Average Retention:</strong> {{ retention_avg }}%
        &nbsp;&bull;&nbsp;
        <strong style="color:#86efac;">Target:</strong> beat {{ retention_target }}% on the next video
      </div>
      {% endif %}
      {% else %}
      <div style="background:#080808;border:1px solid #111;border-radius:9px;padding:16px 18px;font-size:.78rem;color:var(--muted);text-align:center;">
        No retention data yet — scores appear after your first videos have been live for 48+ hours. Click <strong style="color:#e8e0d0;">Run Analysis Now</strong> to check.
      </div>
      {% endif %}
    </div>
  </div>

  <!-- YOUTUBE OAUTH SECTION -->
  <div style="margin-top:32px;">
    <div class="section-label" style="margin-bottom:14px;">YouTube Channel Authorization</div>
    <div class="card">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:18px;">
        <div>
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px;">
            <span style="font-size:.82rem;font-weight:900;color:#e8e0d0;">YouTube OAuth</span>
            {% if yt_ready %}
              <span style="display:inline-flex;align-items:center;gap:5px;font-size:.68rem;font-weight:900;text-transform:uppercase;letter-spacing:.07em;background:rgba(34,197,94,.1);border:1px solid rgba(34,197,94,.28);color:#86efac;padding:2px 9px;border-radius:999px;"><span style="width:6px;height:6px;border-radius:50%;background:#4ade80;box-shadow:0 0 5px #4ade80;display:inline-block;"></span>Connected</span>
            {% else %}
              <span style="display:inline-flex;align-items:center;gap:5px;font-size:.68rem;font-weight:900;text-transform:uppercase;letter-spacing:.07em;background:rgba(248,113,113,.1);border:1px solid rgba(248,113,113,.28);color:#f87171;padding:2px 9px;border-radius:999px;"><span style="width:6px;height:6px;border-radius:50%;background:#f87171;display:inline-block;"></span>Not connected</span>
            {% endif %}
          </div>
          <p class="key-desc" style="margin-bottom:0;">Authorizes the engine to upload Shorts, read analytics, and post comments on your YouTube channel.</p>
        </div>
        <a href="{{ url_for('youtube_auth_start') }}"
           style="display:inline-flex;align-items:center;gap:8px;flex-shrink:0;background:{% if yt_ready %}#0d0d0d{% else %}var(--gold){% endif %};color:{% if yt_ready %}var(--muted){% else %}#000{% endif %};border:1px solid {% if yt_ready %}#222{% else %}var(--gold-border){% endif %};font-size:.78rem;font-weight:900;letter-spacing:.07em;text-transform:uppercase;padding:10px 22px;border-radius:9px;text-decoration:none;transition:opacity .15s;">
          {% if yt_ready %}&#8635; Re-authorize{% else %}&#9654; Connect YouTube{% endif %}
        </a>
      </div>

      <!-- REDIRECT URI SETUP -->
      <div style="background:#050505;border:1px solid #1a1a1a;border-radius:9px;padding:16px 18px;">
        <div style="font-size:.68rem;font-weight:900;letter-spacing:.12em;text-transform:uppercase;color:var(--gold);margin-bottom:10px;">Google Cloud Console Setup</div>
        <p style="font-size:.78rem;color:var(--muted);line-height:1.6;margin-bottom:14px;">
          Both URIs below must be added to your OAuth 2.0 Client's <strong style="color:#e8e0d0;">Authorized redirect URIs</strong> in
          <a href="https://console.cloud.google.com/apis/credentials" target="_blank" style="color:var(--gold);font-weight:800;">Google Cloud Console</a>.
        </p>
        <div style="margin-bottom:10px;">
          <div style="font-size:.65rem;font-weight:900;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:5px;">Google Sign-In callback</div>
          <div style="display:flex;align-items:center;gap:8px;">
            <code id="uri-google" style="flex:1;font-size:.75rem;color:var(--gold);background:#0a0a0a;border:1px solid #1e1e1e;padding:7px 12px;border-radius:7px;word-break:break-all;display:block;">{{ google_redirect_uri }}</code>
            <button onclick="copyURI('uri-google',this)" style="flex-shrink:0;background:#111;border:1px solid #222;color:var(--muted);font-size:.68rem;font-weight:900;letter-spacing:.06em;text-transform:uppercase;padding:6px 12px;border-radius:6px;cursor:pointer;transition:color .15s,border-color .15s;white-space:nowrap;">Copy</button>
          </div>
        </div>
        <div>
          <div style="font-size:.65rem;font-weight:900;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:5px;">YouTube OAuth callback</div>
          <div style="display:flex;align-items:center;gap:8px;">
            <code id="uri-youtube" style="flex:1;font-size:.75rem;color:var(--gold);background:#0a0a0a;border:1px solid #1e1e1e;padding:7px 12px;border-radius:7px;word-break:break-all;display:block;">{{ yt_redirect_uri }}</code>
            <button onclick="copyURI('uri-youtube',this)" style="flex-shrink:0;background:#111;border:1px solid #222;color:var(--muted);font-size:.68rem;font-weight:900;letter-spacing:.06em;text-transform:uppercase;padding:6px 12px;border-radius:6px;cursor:pointer;transition:color .15s,border-color .15s;white-space:nowrap;">Copy</button>
          </div>
        </div>
        <p style="font-size:.72rem;color:#444;margin-top:12px;line-height:1.55;">
          After adding both URIs in Google Cloud Console, click Save there, then use the Connect YouTube button above.
        </p>
      </div>
    </div>
  </div>
</div>

<script>
function copyURI(id, btn) {
  const text = document.getElementById(id).textContent.trim();
  navigator.clipboard.writeText(text).then(() => {
    const orig = btn.textContent;
    btn.textContent = 'Copied!';
    btn.style.color = 'var(--gold)';
    btn.style.borderColor = 'var(--gold-border)';
    setTimeout(() => { btn.textContent = orig; btn.style.color = ''; btn.style.borderColor = ''; }, 2000);
  });
}
</script>
</body></html>
"""

# ─────────────────────────────────────────────
#  TREND HUNTER PAGE
# ─────────────────────────────────────────────
TREND_HUNTER_PAGE = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wealth Vault — Trend Hunter</title>
<style>
  :root{--gold:#D4AF37;--gold-border:rgba(212,175,55,.35);--gold-dim:rgba(212,175,55,.12);--bg:#000;--surface:#080808;--card:#0c0c0c;--text:#f0ead6;--muted:#6b6350;--green:#22c55e;--green-dim:rgba(34,197,94,.1);--red:#f87171;--red-dim:rgba(248,113,113,.1);--orange:#fb923c;color-scheme:dark;font-family:Inter,system-ui,sans-serif}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);min-height:100vh}
  nav{display:flex;align-items:center;justify-content:space-between;padding:0 24px;height:52px;border-bottom:1px solid #111;background:#000;position:sticky;top:0;z-index:100}
  .nav-brand{font-size:.82rem;font-weight:900;letter-spacing:.18em;text-transform:uppercase;color:var(--gold)}
  .nav-links{display:flex;gap:6px}
  .nav-links a{font-size:.75rem;font-weight:800;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);text-decoration:none;padding:6px 13px;border-radius:7px;transition:color .15s,background .15s}
  .nav-links a:hover,.nav-links a.active{color:var(--gold);background:var(--gold-dim)}
  main{max-width:1100px;margin:0 auto;padding:38px 24px 80px}
  a{color:var(--gold);text-decoration:none;font-weight:800}a:hover{opacity:.8}
  h1{font-size:2rem;font-weight:950;letter-spacing:-.02em;color:#fff;margin-bottom:4px}
  .lead{color:var(--muted);font-size:.84rem;margin-bottom:28px;line-height:1.6}

  /* stat grid */
  .stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:10px;margin-bottom:28px}
  .stat-card{border:1px solid #181818;border-radius:14px;background:var(--card);padding:18px 20px;position:relative;overflow:hidden;transition:border-color .2s}
  .stat-card:hover{border-color:var(--gold-border)}
  .stat-card::before{content:'';position:absolute;inset:0;background:radial-gradient(ellipse at top left,rgba(212,175,55,.05),transparent 70%);pointer-events:none}
  .stat-card .lbl{font-size:.62rem;font-weight:900;letter-spacing:.13em;text-transform:uppercase;color:var(--muted);margin-bottom:8px}
  .stat-card .val{font-size:1.8rem;font-weight:950;letter-spacing:-.03em;color:var(--gold);line-height:1}
  .stat-card .sub{font-size:.68rem;color:var(--muted);margin-top:5px}

  /* pinned keyword banner */
  .pinned-banner{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:16px 22px;border-radius:12px;border:1px solid rgba(212,175,55,.4);background:linear-gradient(135deg,rgba(212,175,55,.08),transparent);margin-bottom:22px;flex-wrap:wrap}
  .pinned-kw{font-size:1rem;font-weight:900;color:var(--gold)}
  .pinned-meta{font-size:.72rem;color:var(--muted);margin-top:3px}

  /* buttons */
  .btn{display:inline-flex;align-items:center;gap:6px;font-size:.72rem;font-weight:900;letter-spacing:.07em;text-transform:uppercase;padding:7px 16px;border-radius:8px;border:none;cursor:pointer;text-decoration:none;transition:opacity .15s}
  .btn:hover{opacity:.8}
  .btn-gold{background:var(--gold);color:#000}
  .btn-dim{background:#111;color:var(--muted);border:1px solid #1e1e1e}
  .btn-red{background:rgba(248,113,113,.12);color:var(--red);border:1px solid rgba(248,113,113,.25)}
  .btn-sm{font-size:.64rem;padding:5px 11px;border-radius:6px}

  /* toolbar */
  .toolbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;gap:10px;flex-wrap:wrap}
  .toolbar-title{font-size:.76rem;font-weight:900;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}

  /* keyword table */
  .kw-table{width:100%;border-collapse:collapse;border-radius:14px;overflow:hidden;border:1px solid #161616;background:var(--card);margin-bottom:24px}
  .kw-table th{text-align:left;padding:10px 16px;font-size:.63rem;font-weight:900;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);background:#0a0a0a;border-bottom:1px solid #111}
  .kw-table td{padding:11px 16px;border-bottom:1px solid #0e0e0e;vertical-align:middle;font-size:.85rem}
  .kw-table tr:last-child td{border-bottom:0}
  .kw-table tr:hover td{background:rgba(255,255,255,.015)}
  .rank{font-size:.72rem;font-weight:900;color:var(--muted);width:32px}
  .kw-name{font-weight:800;color:var(--text)}
  .freq-bar-wrap{width:140px}
  .freq-bar-track{height:5px;background:#151515;border-radius:99px;overflow:hidden;margin-top:5px}
  .freq-bar-fill{height:100%;border-radius:99px;background:linear-gradient(90deg,#5c3e00,var(--gold));transition:width .4s}
  .freq-count{font-size:.72rem;color:var(--muted);margin-top:3px}
  .spike-pill{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:999px;font-size:.64rem;font-weight:900;white-space:nowrap}
  .spike-hot{background:rgba(251,146,60,.12);border:1px solid rgba(251,146,60,.3);color:var(--orange)}
  .spike-new{background:var(--green-dim);border:1px solid rgba(34,197,94,.25);color:var(--green)}
  .spike-none{background:#111;border:1px solid #1e1e1e;color:var(--muted)}
  .actions-col{text-align:right}

  /* videos grid */
  .section-title{font-size:.7rem;font-weight:900;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin-bottom:12px}
  .vid-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px;margin-bottom:24px}
  .vid-card{border:1px solid #161616;border-radius:12px;background:var(--card);padding:14px;transition:border-color .2s}
  .vid-card:hover{border-color:#2a2a2a}
  .vid-title{font-size:.8rem;font-weight:800;color:var(--text);line-height:1.4;margin-bottom:6px}
  .vid-ch{font-size:.68rem;color:var(--muted);margin-bottom:8px}
  .vid-query{font-size:.62rem;color:var(--muted);font-style:italic}

  /* info box */
  .info-box{border:1px solid #181818;border-radius:12px;background:var(--card);padding:20px;margin-top:4px}
  .info-box p{font-size:.83rem;color:var(--muted);line-height:1.7;margin-bottom:10px}
  .info-box p:last-child{margin-bottom:0}

  /* flash */
  .flash-box{padding:12px 16px;border-radius:10px;background:var(--gold-dim);border:1px solid var(--gold-border);color:var(--gold);margin-bottom:20px;font-size:.85rem}
  .empty{padding:36px;text-align:center;color:var(--muted);border:1px dashed #1a1a1a;border-radius:12px;font-size:.85rem}

  @media(max-width:640px){
    .kw-table .freq-bar-wrap{display:none}
    .vid-grid{grid-template-columns:1fr 1fr}
  }
</style></head>
<body>

<nav>
  <span class="nav-brand">&#9679; Wealth Vault</span>
  <div class="nav-links">
    <a href="{{ url_for('index') }}">Command Center</a>
    <a href="{{ url_for('view_dashboard') }}">Dashboard</a>
    <a href="{{ url_for('view_trend_hunter') }}" class="active">Trend Hunter</a>
    <a href="{{ url_for('view_spike_log') }}">Auto-Seeder</a>
    {% if session.get('role') == 'admin' %}<a href="{{ url_for('admin_settings') }}">Settings</a>{% endif %}
  </div>
  <div style="display:flex;align-items:center;gap:8px;margin-right:8px;">
    {% if session.get('picture') %}
      <img src="{{ session.get('picture') }}" width="26" height="26" style="border-radius:50%;border:1.5px solid var(--gold-border);object-fit:cover;" alt="">
    {% else %}
      <div style="width:26px;height:26px;border-radius:50%;background:linear-gradient(135deg,var(--gold),#5c3e00);display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:900;color:#000;">{{ (session.get('username','?')[0])|upper }}</div>
    {% endif %}
    <a href="{{ url_for('logout') }}" style="font-size:.72rem;font-weight:900;letter-spacing:.08em;text-transform:uppercase;color:var(--gold);border:1px solid var(--gold-border);background:var(--gold-dim);padding:5px 12px;border-radius:7px;">Logout</a>
  </div>
</nav>

<main>
  <h1>Trend Hunter</h1>
  <p class="lead">Real-time keyword intelligence from the top Wealth &amp; Luxury Shorts on YouTube. Pin the best keyword and the next scheduled run will use it as its script seed automatically.</p>

  {% with messages = get_flashed_messages() %}{% if messages %}
    <div class="flash-box">{% for m in messages %}{{ m }}<br>{% endfor %}</div>
  {% endif %}{% endwith %}

  <!-- HERO STATS -->
  <div class="stat-grid">
    <div class="stat-card" style="border-color:rgba(212,175,55,.25)">
      <div class="lbl">Keywords Tracked</div>
      <div class="val">{{ keywords|length }}</div>
      <div class="sub">extracted from top Shorts</div>
    </div>
    <div class="stat-card">
      <div class="lbl">Videos Analyzed</div>
      <div class="val">{{ videos|length }}</div>
      <div class="sub">Wealth &amp; Luxury Shorts</div>
    </div>
    <div class="stat-card">
      <div class="lbl">Spiking Now</div>
      <div class="val" style="color:{% if spikes %}var(--orange){% else %}var(--muted){% endif %}">{{ spikes|length }}</div>
      <div class="sub">vs previous 12h cycle</div>
    </div>
    <div class="stat-card">
      <div class="lbl">Last Refresh</div>
      <div class="val" style="font-size:1rem;padding-top:4px;">{{ updated_at[:16].replace('T',' ') if updated_at else '—' }}</div>
      <div class="sub">UTC &mdash; refreshes every 12h</div>
    </div>
  </div>

  <!-- PINNED KEYWORD BANNER -->
  {% if pinned %}
  <div class="pinned-banner">
    <div>
      <div style="font-size:.65rem;font-weight:900;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);margin-bottom:4px;">&#127358; Pinned for Next Run</div>
      <div class="pinned-kw">{{ pinned.keyword }}</div>
      <div class="pinned-meta">Set {{ pinned.pinned_at[:16].replace('T',' ') }} UTC &mdash; the next scheduled upload will use this as its seed and clear the pin automatically.</div>
    </div>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <form method="POST" action="{{ url_for('unpin_trend_keyword') }}" style="display:inline">
        <button class="btn btn-red btn-sm" type="submit">&#10005; Clear Pin</button>
      </form>
      <a href="{{ url_for('view_spike_log') }}" class="btn btn-dim btn-sm">Auto-Seeder Log</a>
    </div>
  </div>
  {% else %}
  <div style="padding:14px 18px;border-radius:10px;border:1px dashed #1e1e1e;margin-bottom:22px;display:flex;align-items:center;gap:10px;color:var(--muted);font-size:.82rem;">
    <span style="font-size:1.2rem">&#128204;</span>
    <span>No keyword pinned — click <strong style="color:var(--text)">Set as Seed</strong> on any keyword below to auto-feed it into the next scheduled script.</span>
  </div>
  {% endif %}

  <!-- TOOLBAR -->
  <div class="toolbar">
    <span class="toolbar-title">Keyword Rankings &mdash; {{ keywords|length }} total</span>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <form method="POST" action="{{ url_for('refresh_trends') }}" style="display:inline">
        <button class="btn btn-dim btn-sm" type="submit">&#8635; Force Refresh</button>
      </form>
      {% if not pinned and keywords %}
      <form method="POST" action="{{ url_for('pin_trend_keyword') }}" style="display:inline">
        <input type="hidden" name="keyword" value="{{ keywords[0].word }}">
        <button class="btn btn-gold btn-sm" type="submit">&#9889; Auto-Pin Top Keyword</button>
      </form>
      {% endif %}
    </div>
  </div>

  <!-- KEYWORD TABLE -->
  {% if keywords %}
  <table class="kw-table">
    <thead>
      <tr>
        <th style="width:36px">#</th>
        <th>Keyword</th>
        <th>Frequency</th>
        <th>Spike</th>
        <th style="text-align:right">Action</th>
      </tr>
    </thead>
    <tbody>
    {% set max_freq = keywords[0].freq if keywords else 1 %}
    {% for kw in keywords %}
    <tr>
      <td class="rank">{{ loop.index }}</td>
      <td>
        <span class="kw-name">{{ kw.word }}</span>
        {% if loop.index == 1 and not pinned %}<span style="font-size:.6rem;font-weight:900;color:var(--gold);background:var(--gold-dim);border:1px solid var(--gold-border);padding:1px 7px;border-radius:99px;margin-left:6px;">TOP</span>{% endif %}
      </td>
      <td class="freq-bar-wrap">
        <div style="display:flex;align-items:center;gap:8px">
          <div style="flex:1">
            <div class="freq-bar-track">
              <div class="freq-bar-fill" style="width:{{ [(kw.freq / max_freq * 100)|int, 100]|min }}%"></div>
            </div>
            <div class="freq-count">{{ kw.freq }} mention{{ 's' if kw.freq != 1 else '' }}</div>
          </div>
        </div>
      </td>
      <td>
        {% if kw.ratio is not none %}
          {% if kw.ratio >= 3 %}
            <span class="spike-pill spike-hot">&#9650; {{ kw.ratio }}x spike</span>
          {% elif kw.ratio >= 1.5 %}
            <span class="spike-pill spike-new">&#8593; rising {{ kw.ratio }}x</span>
          {% else %}
            <span class="spike-pill spike-none">stable</span>
          {% endif %}
        {% elif kw.is_new %}
          <span class="spike-pill spike-new">&#9733; new</span>
        {% else %}
          <span class="spike-pill spike-none">&mdash;</span>
        {% endif %}
      </td>
      <td class="actions-col">
        {% if pinned and pinned.keyword == kw.word %}
          <span style="font-size:.66rem;font-weight:900;color:var(--gold);padding:4px 10px;border-radius:6px;border:1px solid var(--gold-border);background:var(--gold-dim);">&#10003; Pinned</span>
        {% else %}
          <form method="POST" action="{{ url_for('pin_trend_keyword') }}" style="display:inline">
            <input type="hidden" name="keyword" value="{{ kw.word }}">
            <button class="btn btn-dim btn-sm" type="submit">&#128204; Set as Seed</button>
          </form>
        {% endif %}
      </td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
  <div class="empty">No keyword data yet — click Force Refresh or wait for the next scheduled run.</div>
  {% endif %}

  <!-- TRENDING VIDEOS GRID -->
  {% if videos %}
  <div class="section-title">Trending Videos Analyzed</div>
  <div class="vid-grid">
    {% for v in videos %}
    <div class="vid-card">
      <div class="vid-title"><a href="{{ v.url }}" target="_blank" style="color:var(--text)">{{ v.title }}</a></div>
      <div class="vid-ch">{{ v.channel }}</div>
      <div class="vid-query">query: {{ v.query }}</div>
    </div>
    {% endfor %}
  </div>
  {% endif %}

  <!-- HOW IT WORKS -->
  <div class="info-box">
    <div class="section-title" style="margin-bottom:14px">How Trend Hunter Works</div>
    <p>Every 12 hours the engine searches YouTube for the top-performing Shorts across four Wealth &amp; Luxury queries. It extracts every significant word from their titles, ranks them by frequency, and saves the top 20 as trending keywords.</p>
    <p>When you pin a keyword, the next scheduled upload (08:00 London / 20:00 New York) builds its Gemini script seed around that keyword instead of a random one, then clears the pin automatically. This lets you ride a trending topic at the exact right moment.</p>
    <p>The Auto-Seeder runs every 30 minutes separately. If any keyword's frequency jumps 3× compared to the previous 12h snapshot, it fires an emergency upload immediately — even outside the regular schedule.</p>
  </div>
</main></body></html>
"""

# ─────────────────────────────────────────────
#  SPIKE LOG PAGE
# ─────────────────────────────────────────────
SPIKE_LOG_PAGE = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wealth Vault — Auto-Seeder Spike Log</title>
<style>
  :root{--gold:#D4AF37;--gold-border:rgba(212,175,55,.35);--gold-dim:rgba(212,175,55,.12);--bg:#000;--surface:#0a0a0a;--text:#f0ead6;--muted:#6b6350;color-scheme:dark;font-family:Inter,system-ui,sans-serif}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);min-height:100vh}
  main{max-width:960px;margin:0 auto;padding:38px 22px 80px}
  a{color:var(--gold);text-decoration:none;font-weight:800}a:hover{text-decoration:underline}
  h1{font-size:clamp(1.8rem,5vw,3rem);letter-spacing:-.03em;margin:12px 0 6px;background:linear-gradient(135deg,#fff 30%,var(--gold));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
  .lead{color:var(--muted);font-size:.9rem;margin-bottom:28px;line-height:1.6}
  .card{border:1px solid #1c1c1c;border-radius:14px;background:var(--surface);padding:22px;margin-bottom:14px}
  .card h2{font-size:.68rem;letter-spacing:.14em;text-transform:uppercase;color:var(--gold);font-weight:900;margin-bottom:16px}
  .stat-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:22px}
  .stat{border:1px solid #1c1c1c;border-radius:12px;background:var(--surface);padding:16px}
  .stat .label{color:var(--muted);font-size:.68rem;text-transform:uppercase;letter-spacing:.1em;font-weight:900}
  .stat .value{font-size:1.6rem;font-weight:950;color:var(--gold);margin-top:4px}
  .stat .sub{font-size:.72rem;color:var(--muted);margin-top:3px}
  .entry{padding:18px;border-radius:10px;background:#0d0d0d;border:1px solid #1c1c1c;margin-bottom:10px}
  .entry-meta{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:10px}
  .kw-pill{display:inline-block;padding:3px 10px;border-radius:999px;background:var(--gold-dim);border:1px solid var(--gold-border);color:var(--gold);font-size:.72rem;font-weight:900;letter-spacing:.05em}
  .seed-text{font-size:.85rem;color:#c8c0a8;line-height:1.55;border-left:2px solid var(--gold-border);padding-left:12px;font-style:italic}
  .job-id{font-family:monospace;font-size:.75rem;color:var(--muted)}
  .ts{color:var(--muted);font-size:.76rem}
  .empty{padding:36px;text-align:center;color:var(--muted);border:1px dashed #1a1a1a;border-radius:12px}
  .cooldown{display:flex;align-items:center;gap:10px;padding:14px 18px;border-radius:10px;background:rgba(212,175,55,.06);border:1px solid var(--gold-border);margin-bottom:22px;font-size:.85rem}
  .cooldown-icon{font-size:1.3rem}
</style></head>
<body><main>
<a href="{{ url_for('index') }}">&larr; Command Center</a>
<h1>Auto-Seeder Spike Log</h1>
<p class="lead">Every 30 minutes the engine checks YouTube trend frequencies. When any keyword jumps 3× its previous cycle count, an emergency Short is generated and uploaded automatically — no schedule, no wait.</p>

{% if cooldown_remaining > 0 %}
<div class="cooldown">
  <span class="cooldown-icon">⏳</span>
  <span>Cooldown active — next spike upload allowed in <strong style="color:var(--gold);">{{ '%dh %02dm' % (cooldown_remaining // 3600, (cooldown_remaining % 3600) // 60) }}</strong>. This prevents spam when multiple keywords spike simultaneously.</span>
</div>
{% else %}
<div class="cooldown" style="border-color:rgba(34,197,94,.3);background:rgba(34,197,94,.06);">
  <span class="cooldown-icon">⚡</span>
  <span style="color:#86efac;">Auto-Seeder is armed — will fire on next detected 3× keyword spike.</span>
</div>
{% endif %}

<div class="stat-row">
  <div class="stat">
    <div class="label">Emergency Uploads</div>
    <div class="value">{{ entries | length }}</div>
    <div class="sub">spike-triggered total</div>
  </div>
  <div class="stat">
    <div class="label">Spike Threshold</div>
    <div class="value">3×</div>
    <div class="sub">vs previous 12h cycle</div>
  </div>
  <div class="stat">
    <div class="label">Check Interval</div>
    <div class="value">30m</div>
    <div class="sub">APScheduler cron</div>
  </div>
  <div class="stat">
    <div class="label">Cooldown</div>
    <div class="value">4h</div>
    <div class="sub">between spike uploads</div>
  </div>
</div>

{% if entries %}
<div class="card">
  <h2>Upload Log — newest first</h2>
  {% for e in entries %}
  <div class="entry">
    <div class="entry-meta">
      <span class="ts">{{ e.triggered_at }}</span>
      {% for kw in e.keywords %}
        <span class="kw-pill">⚡ {{ kw }}</span>
      {% endfor %}
      <span class="job-id">job {{ e.job_id }}</span>
    </div>
    <div class="seed-text">"{{ e.seed }}"</div>
  </div>
  {% endfor %}
</div>
{% else %}
<div class="empty">No spike-triggered uploads yet. The engine checks every 30 minutes — as soon as a keyword jumps 3× it will fire here automatically.</div>
{% endif %}

<div class="card" style="margin-top:8px;">
  <h2>How the Auto-Seeder Works</h2>
  <p style="color:var(--muted);font-size:.85rem;line-height:1.65;">
    Each scheduled run (08:00 London / 20:00 New York) saves a snapshot of current keyword frequencies,
    then fetches fresh trending data. Every 30 minutes the spike-check job compares the live counts
    against the snapshot. If any keyword has grown 3× or more it constructs a seed combining
    the top spiking keywords, calls the full viral pipeline, and uploads immediately.
    A 4-hour cooldown prevents multiple emergency uploads from overlapping.
  </p>
</div>
</main></body></html>
"""

# ─────────────────────────────────────────────
#  AFFILIATE COMMENTS PAGE
# ─────────────────────────────────────────────
AFFILIATE_PAGE = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wealth Vault — Affiliate Comments</title>
<style>
  :root{--gold:#D4AF37;--gold-border:rgba(212,175,55,.35);--gold-dim:rgba(212,175,55,.12);--bg:#000;--surface:#0a0a0a;--text:#f0ead6;--muted:#6b6350;color-scheme:dark;font-family:Inter,system-ui,sans-serif}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);min-height:100vh}
  main{max-width:960px;margin:0 auto;padding:38px 22px 80px}
  a{color:var(--gold);text-decoration:none;font-weight:800}a:hover{text-decoration:underline}
  h1{font-size:clamp(1.8rem,5vw,3rem);letter-spacing:-.03em;margin:12px 0 6px;background:linear-gradient(135deg,#fff 30%,var(--gold));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
  .lead{color:var(--muted);font-size:.9rem;margin-bottom:28px;line-height:1.6}
  .card{border:1px solid #1c1c1c;border-radius:14px;background:var(--surface);padding:22px;margin-bottom:14px}
  .card h2{font-size:.68rem;letter-spacing:.14em;text-transform:uppercase;color:var(--gold);font-weight:900;margin-bottom:16px}
  .comment-row{padding:16px;border-radius:10px;background:#0d0d0d;border:1px solid #1c1c1c;margin-bottom:10px}
  .comment-meta{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin-bottom:8px}
  .comment-meta a{font-size:.8rem}
  .comment-text{white-space:pre-wrap;font-size:.85rem;color:#c8c0a8;line-height:1.6;border-left:2px solid var(--gold-border);padding-left:12px;margin-top:8px}
  .badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:.68rem;font-weight:900}
  .badge-ok{background:rgba(34,197,94,.12);color:#86efac;border:1px solid rgba(74,222,128,.3)}
  .badge-skip{background:var(--gold-dim);color:var(--gold);border:1px solid var(--gold-border)}
  .badge-err{background:rgba(192,57,43,.12);color:#f87171;border:1px solid rgba(239,68,68,.3)}
  .pin-btn{display:inline-flex;align-items:center;gap:5px;padding:5px 12px;border-radius:7px;background:var(--gold-dim);border:1px solid var(--gold-border);color:var(--gold);font-size:.76rem;font-weight:900;text-decoration:none;letter-spacing:.06em;text-transform:uppercase}
  .pin-btn:hover{background:rgba(212,175,55,.25)}
  .empty{padding:36px;text-align:center;color:var(--muted);border:1px dashed #1a1a1a;border-radius:12px}
  .stat-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:22px}
  .stat{border:1px solid #1c1c1c;border-radius:12px;background:var(--surface);padding:16px}
  .stat .label{color:var(--muted);font-size:.68rem;text-transform:uppercase;letter-spacing:.1em;font-weight:900}
  .stat .value{font-size:1.6rem;font-weight:950;color:var(--gold);margin-top:4px}
</style></head>
<body><main>
<a href="{{ url_for('index') }}">&larr; Command Center</a>
<h1>Affiliate Comments</h1>
<p class="lead">Every successful upload triggers an automatic affiliate comment. Copy the Studio link to pin it in one click.</p>

{% set posted = comments | selectattr('status','equalto','posted') | list %}
{% set skipped = comments | selectattr('status','equalto','skipped') | list %}
{% set errored = comments | selectattr('status','equalto','error') | list %}
<div class="stat-row">
  <div class="stat"><div class="label">Posted</div><div class="value">{{ posted | length }}</div></div>
  <div class="stat"><div class="label">Skipped</div><div class="value">{{ skipped | length }}</div></div>
  <div class="stat"><div class="label">Errors</div><div class="value">{{ errored | length }}</div></div>
</div>

{% if comments %}
  <div class="card">
    <h2>Comment Log — newest first</h2>
    {% for c in comments %}
      <div class="comment-row">
        <div class="comment-meta">
          {% if c.status == 'posted' %}
            <span class="badge badge-ok">Posted</span>
          {% elif c.status == 'skipped' %}
            <span class="badge badge-skip">Skipped — no URL set</span>
          {% else %}
            <span class="badge badge-err">Error</span>
          {% endif %}
          <a href="https://youtu.be/{{ c.video_id }}" target="_blank">{{ c.video_id }}</a>
          <span style="color:var(--muted);font-size:.76rem;">{{ c.posted_at }}</span>
          {% if c.studio_pin_url %}
            <a class="pin-btn" href="{{ c.studio_pin_url }}" target="_blank">&#128204; Pin in Studio</a>
          {% endif %}
        </div>
        {% if c.comment_text %}
          <div class="comment-text">{{ c.comment_text }}</div>
        {% elif c.error %}
          <div style="color:#f87171;font-size:.82rem;margin-top:6px;">{{ c.error }}</div>
        {% endif %}
      </div>
    {% endfor %}
  </div>
{% else %}
  <div class="empty">No comments logged yet. After your first upload the comment will appear here automatically.</div>
{% endif %}

<div class="card" style="margin-top:8px;">
  <h2>How Pinning Works</h2>
  <p style="color:var(--muted);font-size:.85rem;line-height:1.65;">
    YouTube's public API does not expose a "pin comment" endpoint.
    After the engine posts the comment, click <strong style="color:var(--gold);">Pin in Studio</strong>
    next to any row — it opens YouTube Studio for that exact video.
    In the Comments tab, find the top comment (posted by your channel),
    click the three-dot menu &rarr; <em>Pin comment</em>. Done in ~3 seconds.
  </p>
</div>
</main></body></html>
"""

# ─────────────────────────────────────────────
#  DASHBOARD PAGE
# ─────────────────────────────────────────────
DASHBOARD_PAGE = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wealth Vault — Performance Dashboard</title>
<style>
  :root{--gold:#D4AF37;--gold-border:rgba(212,175,55,.35);--gold-dim:rgba(212,175,55,.12);--bg:#000;--surface:#080808;--card:#0c0c0c;--text:#f0ead6;--muted:#6b6350;--green:#22c55e;--green-dim:rgba(34,197,94,.1);--red:#f87171;--red-dim:rgba(248,113,113,.1);color-scheme:dark;font-family:Inter,system-ui,sans-serif}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);min-height:100vh}
  nav{display:flex;align-items:center;justify-content:space-between;padding:0 24px;height:52px;border-bottom:1px solid #111;background:#000;position:sticky;top:0;z-index:100;}
  .nav-brand{font-size:.82rem;font-weight:900;letter-spacing:.18em;text-transform:uppercase;color:var(--gold);}
  .nav-links{display:flex;gap:6px;}
  .nav-links a{font-size:.75rem;font-weight:800;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);text-decoration:none;padding:6px 13px;border-radius:7px;transition:color .15s,background .15s;}
  .nav-links a:hover,.nav-links a.active{color:var(--gold);background:var(--gold-dim);}
  main{max-width:1160px;margin:0 auto;padding:38px 24px 80px}
  a{color:var(--gold);text-decoration:none;font-weight:800}
  a:hover{opacity:.8}
  h1{font-size:2rem;font-weight:950;letter-spacing:-.02em;color:#fff;margin-bottom:4px}
  .lead{color:var(--muted);font-size:.84rem;margin-bottom:30px;line-height:1.6}

  /* ── HERO STAT GRID ── */
  .stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:32px}
  .stat-card{border:1px solid #181818;border-radius:14px;background:var(--card);padding:18px 20px;position:relative;overflow:hidden;transition:border-color .2s}
  .stat-card:hover{border-color:var(--gold-border)}
  .stat-card::before{content:'';position:absolute;inset:0;background:radial-gradient(ellipse at top left,rgba(212,175,55,.05),transparent 70%);pointer-events:none}
  .stat-card .lbl{font-size:.63rem;font-weight:900;letter-spacing:.13em;text-transform:uppercase;color:var(--muted);margin-bottom:8px}
  .stat-card .val{font-size:1.9rem;font-weight:950;letter-spacing:-.03em;color:var(--gold);line-height:1}
  .stat-card .sub{font-size:.68rem;color:var(--muted);margin-top:5px}
  .stat-card.highlight{border-color:rgba(212,175,55,.3);background:linear-gradient(135deg,rgba(212,175,55,.06),transparent)}

  /* ── TOOLBAR ── */
  .toolbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;gap:12px;flex-wrap:wrap}
  .toolbar-title{font-size:.78rem;font-weight:900;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
  .btn{display:inline-flex;align-items:center;gap:6px;font-size:.72rem;font-weight:900;letter-spacing:.07em;text-transform:uppercase;padding:7px 16px;border-radius:8px;border:none;cursor:pointer;text-decoration:none;transition:opacity .15s}
  .btn:hover{opacity:.8}
  .btn-gold{background:var(--gold);color:#000}
  .btn-dim{background:#111;color:var(--muted);border:1px solid #1e1e1e}
  .btn-studio{background:#282828;color:#fff;border:1px solid #333;font-size:.66rem;padding:4px 10px;border-radius:6px}

  /* ── VIDEO CARDS ── */
  .video-grid{display:flex;flex-direction:column;gap:12px}
  .vcard{border:1px solid #161616;border-radius:14px;background:var(--card);padding:18px 20px;display:grid;grid-template-columns:100px 1fr auto;gap:16px;align-items:start;transition:border-color .2s}
  .vcard:hover{border-color:#2a2a2a}
  .vcard.top-card{border-color:rgba(212,175,55,.35);background:linear-gradient(135deg,rgba(212,175,55,.04),transparent)}
  .thumb{width:100px;height:56px;border-radius:8px;object-fit:cover;background:#111;flex-shrink:0}
  .thumb-placeholder{width:100px;height:56px;border-radius:8px;background:#0d0d0d;border:1px solid #1a1a1a;display:flex;align-items:center;justify-content:center;font-size:1.4rem;flex-shrink:0}
  .vcard-title{font-size:.9rem;font-weight:800;color:var(--text);line-height:1.4;margin-bottom:6px}
  .vcard-meta{display:flex;flex-wrap:wrap;align-items:center;gap:7px;margin-bottom:10px}
  .pill{display:inline-flex;align-items:center;gap:4px;font-size:.65rem;font-weight:900;letter-spacing:.06em;padding:3px 9px;border-radius:999px;white-space:nowrap}
  .pill-gold{background:var(--gold-dim);border:1px solid var(--gold-border);color:var(--gold)}
  .pill-green{background:var(--green-dim);border:1px solid rgba(34,197,94,.25);color:var(--green)}
  .pill-red{background:var(--red-dim);border:1px solid rgba(248,113,113,.25);color:var(--red)}
  .pill-muted{background:#111;border:1px solid #1e1e1e;color:var(--muted)}
  .pill-top{background:linear-gradient(135deg,#b8862a,#D4AF37);color:#000;border:none;font-weight:950}
  .hook-line{font-size:.72rem;color:var(--gold);font-weight:800;margin-bottom:8px;opacity:.85}
  /* Views bar */
  .views-bar-wrap{margin-top:8px}
  .views-bar-label{display:flex;justify-content:space-between;font-size:.66rem;color:var(--muted);margin-bottom:4px}
  .views-bar-track{height:5px;background:#151515;border-radius:99px;overflow:hidden}
  .views-bar-fill{height:100%;border-radius:99px;background:linear-gradient(90deg,#5c3e00,var(--gold));transition:width .4s ease}
  /* Metrics mini-grid */
  .metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(90px,1fr));gap:8px;margin-top:10px}
  .metric{background:#0a0a0a;border:1px solid #151515;border-radius:8px;padding:8px 10px;text-align:center}
  .metric .m-lbl{font-size:.58rem;font-weight:900;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:3px}
  .metric .m-val{font-size:1rem;font-weight:900;color:var(--text)}
  .metric.m-green .m-val{color:var(--green)}
  .metric.m-gold .m-val{color:var(--gold)}
  /* Right-side actions col */
  .vcard-actions{display:flex;flex-direction:column;gap:6px;align-items:flex-end;min-width:80px}
  .time-label{font-size:.64rem;color:var(--muted);text-align:right;line-height:1.5;margin-top:4px}

  /* ── EMPTY & FLASH ── */
  .empty{padding:48px 32px;text-align:center;color:var(--muted);border:1px dashed #1a1a1a;border-radius:14px}
  .empty .empty-icon{font-size:2.4rem;margin-bottom:12px}
  .flash-box{padding:12px 16px;border-radius:10px;background:var(--gold-dim);border:1px solid var(--gold-border);color:var(--gold);margin-bottom:20px;font-size:.85rem}
  .warn-inline{color:#fde68a;font-size:.7rem}

  @media(max-width:640px){
    .vcard{grid-template-columns:1fr;gap:10px}
    .vcard-actions{flex-direction:row;align-items:center}
    .stat-grid{grid-template-columns:repeat(2,1fr)}
  }
</style></head>
<body>

<nav>
  <span class="nav-brand">&#9679; Wealth Vault</span>
  <div class="nav-links">
    <a href="{{ url_for('index') }}">Command Center</a>
    <a href="{{ url_for('view_dashboard') }}" class="active">Dashboard</a>
    <a href="{{ url_for('view_ab_tests') }}">A/B Tests</a>
    <a href="{{ url_for('view_affiliate_comments') }}">Comments</a>
    {% if session.get('role') == 'admin' %}<a href="{{ url_for('view_users') }}">Users</a>{% endif %}
  </div>
  <div style="display:flex;align-items:center;gap:8px;margin-right:8px;">
    {% if session.get('picture') %}
      <img src="{{ session.get('picture') }}" width="26" height="26" style="border-radius:50%;border:1.5px solid var(--gold-border);object-fit:cover;" alt="">
    {% else %}
      <div style="width:26px;height:26px;border-radius:50%;background:linear-gradient(135deg,var(--gold),#5c3e00);display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:900;color:#000;">{{ (session.get('username','?')[0])|upper }}</div>
    {% endif %}
    <a href="{{ url_for('logout') }}" style="font-size:.72rem;font-weight:900;letter-spacing:.08em;text-transform:uppercase;color:var(--gold);border:1px solid var(--gold-border);background:var(--gold-dim);padding:5px 12px;border-radius:7px;">Logout</a>
  </div>
</nav>

<main>
  <h1>Performance Dashboard</h1>
  <p class="lead">Live stats from YouTube Data + Analytics APIs &mdash; every Short the engine published. Last 28&nbsp;days.</p>

  {% with messages = get_flashed_messages() %}{% if messages %}
    <div class="flash-box">{% for m in messages %}{{ m }}<br>{% endfor %}</div>
  {% endif %}{% endwith %}

  <!-- HERO STATS -->
  <div class="stat-grid">
    <div class="stat-card highlight">
      <div class="lbl">Videos Published</div>
      <div class="val">{{ totals.videos }}</div>
      <div class="sub">Total Shorts uploaded</div>
    </div>
    <div class="stat-card">
      <div class="lbl">Total Views</div>
      <div class="val">{% if totals.views >= 1000000 %}{{ '%.1f'|format(totals.views/1000000) }}M{% elif totals.views >= 1000 %}{{ '%.1f'|format(totals.views/1000) }}K{% else %}{{ totals.views }}{% endif %}</div>
      <div class="sub">{{ '{:,}'.format(totals.views) }} exact</div>
    </div>
    <div class="stat-card">
      <div class="lbl">Watch Time</div>
      <div class="val">{% if totals.watch_minutes >= 60 %}{{ '{:,}'.format((totals.watch_minutes // 60)|int) }}h{% else %}{{ totals.watch_minutes }}m{% endif %}</div>
      <div class="sub">{{ '{:,}'.format(totals.watch_minutes) }} minutes</div>
    </div>
    <div class="stat-card">
      <div class="lbl">Total Likes</div>
      <div class="val">{% if totals.likes >= 1000 %}{{ '%.1f'|format(totals.likes/1000) }}K{% else %}{{ '{:,}'.format(totals.likes) }}{% endif %}</div>
      <div class="sub">{{ '{:,}'.format(totals.comments) }} comments</div>
    </div>
    <div class="stat-card">
      <div class="lbl">Avg Retention</div>
      <div class="val">{% if totals.avg_retention is not none %}{{ '%.1f'|format(totals.avg_retention) }}<span style="font-size:1rem">%</span>{% else %}&mdash;{% endif %}</div>
      <div class="sub">of full video watched</div>
    </div>
    <div class="stat-card">
      <div class="lbl">Avg CTR</div>
      <div class="val">{% if totals.avg_ctr is not none %}{{ '%.2f'|format(totals.avg_ctr) }}<span style="font-size:1rem">%</span>{% else %}&mdash;{% endif %}</div>
      <div class="sub">card click-through rate</div>
    </div>
  </div>

  <!-- TOOLBAR -->
  <div class="toolbar">
    <span class="toolbar-title">{{ rows|length }} Short{% if rows|length != 1 %}s{% endif %} &mdash; sorted by views</span>
    <div style="display:flex;gap:8px;">
      <a href="{{ url_for('view_dashboard') }}" class="btn btn-dim">&#8635; Refresh Data</a>
    </div>
  </div>

  <!-- VIDEO CARDS -->
  {% if rows %}
  <div class="video-grid">
    {% for r in rows %}
    <div class="vcard {% if loop.first %}top-card{% endif %}">
      <!-- Thumbnail -->
      {% if r.thumbnail %}
        <img class="thumb" src="{{ r.thumbnail }}" alt="{{ r.title }}">
      {% else %}
        <div class="thumb-placeholder">&#127916;</div>
      {% endif %}

      <!-- Main content -->
      <div>
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px;">
          {% if loop.first %}<span class="pill pill-top">&#127942; Top Performer</span>{% endif %}
          {% if r.retention_pct is not none %}
            {% if r.retention_pct >= 60 %}<span class="pill pill-green">{{ '%.0f'|format(r.retention_pct) }}% Retained</span>
            {% elif r.retention_pct >= 40 %}<span class="pill pill-gold">{{ '%.0f'|format(r.retention_pct) }}% Retained</span>
            {% else %}<span class="pill pill-red">{{ '%.0f'|format(r.retention_pct) }}% Retained</span>{% endif %}
          {% endif %}
          {% if r.ctr is not none %}
            {% if r.ctr >= 5 %}<span class="pill pill-green">{{ '%.2f'|format(r.ctr) }}% CTR</span>
            {% elif r.ctr >= 2 %}<span class="pill pill-gold">{{ '%.2f'|format(r.ctr) }}% CTR</span>
            {% else %}<span class="pill pill-muted">{{ '%.2f'|format(r.ctr) }}% CTR</span>{% endif %}
          {% endif %}
          {% if r.analytics_error %}<span class="pill pill-muted warn-inline" title="{{ r.analytics_error }}">&#9888; Analytics limited</span>{% endif %}
        </div>

        <div class="vcard-title">
          <a href="{{ r.url }}" target="_blank" style="color:var(--text);">{{ r.title }}</a>
        </div>
        {% if r.hook %}<div class="hook-line">HOOK &mdash; {{ r.hook }}</div>{% endif %}

        <!-- Views bar -->
        <div class="views-bar-wrap">
          <div class="views-bar-label">
            <span>{{ '{:,}'.format(r.views or 0) }} views</span>
            <span>{{ r.views_pct }}% of top</span>
          </div>
          <div class="views-bar-track">
            <div class="views-bar-fill" style="width:{{ r.views_pct }}%"></div>
          </div>
        </div>

        <!-- Metric tiles -->
        <div class="metrics">
          <div class="metric m-gold">
            <div class="m-lbl">Views</div>
            <div class="m-val">{% if r.views >= 1000 %}{{ '%.1f'|format(r.views/1000) }}K{% else %}{{ r.views or 0 }}{% endif %}</div>
          </div>
          <div class="metric">
            <div class="m-lbl">Likes</div>
            <div class="m-val">{{ '{:,}'.format(r.likes or 0) }}</div>
          </div>
          <div class="metric">
            <div class="m-lbl">Comments</div>
            <div class="m-val">{{ '{:,}'.format(r.comments or 0) }}</div>
          </div>
          <div class="metric {% if r.avg_view_seconds and r.avg_view_seconds >= 45 %}m-green{% endif %}">
            <div class="m-lbl">Avg View</div>
            <div class="m-val">{% if r.avg_view_seconds is not none %}{{ '%.0f'|format(r.avg_view_seconds) }}s{% else %}&mdash;{% endif %}</div>
          </div>
          <div class="metric">
            <div class="m-lbl">Watch Min</div>
            <div class="m-val">{% if r.watch_minutes %}{{ '{:,}'.format(r.watch_minutes|int) }}{% else %}&mdash;{% endif %}</div>
          </div>
          <div class="metric">
            <div class="m-lbl">Impressions</div>
            <div class="m-val">{% if r.impressions %}{{ '{:,}'.format(r.impressions|int) }}{% else %}&mdash;{% endif %}</div>
          </div>
        </div>
      </div>

      <!-- Actions column -->
      <div class="vcard-actions">
        <a href="{{ r.url }}" target="_blank" class="btn btn-gold" style="font-size:.66rem;padding:5px 12px;">&#9654; Watch</a>
        {% if r.studio_url %}
          <a href="{{ r.studio_url }}" target="_blank" class="btn-studio btn">&#9998; Studio</a>
        {% endif %}
        <div class="time-label">
          {{ (r.published_at or r.uploaded_at or '')[:10] }}
        </div>
      </div>
    </div>
    {% endfor %}
  </div>

  {% else %}
  <div class="empty">
    <div class="empty-icon">&#128202;</div>
    <div style="font-size:.9rem;font-weight:700;color:var(--text);margin-bottom:8px;">No uploads tracked yet</div>
    <div style="font-size:.82rem;">Run the Omni Engine to publish your first Short &mdash; it will appear here automatically.</div>
    <div style="margin-top:16px;"><a href="{{ url_for('index') }}" class="btn btn-gold">&#9654; Go to Command Center</a></div>
  </div>
  {% endif %}
</main></body></html>
"""


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def build_prompt(description: str) -> str:
    return f"""
You are a short-form faceless video strategist. Create a complete plan for a viral 30-60 second faceless video:
{description}
Return only valid JSON with exactly these keys:
- script, keywords (5 phrases), music_mood
Do not include markdown, commentary, or extra keys.
""".strip()


def extract_json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    return json.loads(m.group(0) if m else cleaned)


def safe_slug(value: str) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "video")[:48]


def call_gemini(description: str) -> dict:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set.")
    prompt = build_prompt(description)
    genai.configure(api_key=api_key)

    def _primary() -> dict:
        last_error = "unknown"
        for model_name in PREFERRED_GEMINI_MODELS:
            try:
                model = genai.GenerativeModel(
                    model_name,
                    generation_config={"temperature": 0.85, "top_p": 0.9,
                                       "max_output_tokens": 1200,
                                       "response_mime_type": "application/json"},
                )
                result = extract_json(model.generate_content(prompt).text)
                script = str(result.get("script", "")).strip()
                kws = result.get("keywords", [])
                mood = str(result.get("music_mood", "")).strip()
                if script and isinstance(kws, list) and len(kws) == 5 and mood:
                    return {"script": script,
                            "keywords": [str(k).strip() for k in kws[:5]],
                            "music_mood": mood,
                            "model_used": model_name}
            except Exception as err:
                if openrouter_fallback.is_rate_limit_error(err):
                    raise   # bubble up immediately to trigger the fallback bridge
                last_error = str(err)
        raise RuntimeError(f"Gemini failed on all models: {last_error}")

    return openrouter_fallback.call_with_fallback(prompt, _primary, temperature=0.85, max_tokens=1200)


def get_pexels_links(keywords: list[str]) -> list[dict]:
    api_key = os.environ.get("PEXELS_API_KEY")
    if not api_key:
        flash("PEXELS_API_KEY not set.")
        return []
    links = []
    for kw in keywords:
        try:
            r = requests.get("https://api.pexels.com/videos/search", headers={"Authorization": api_key},
                             params={"query": kw, "orientation": "portrait", "per_page": 1}, timeout=20)
            r.raise_for_status()
            videos = r.json().get("videos", [])
            if videos and videos[0].get("url"):
                links.append({"keyword": kw, "url": videos[0]["url"]})
        except requests.RequestException:
            pass
    return links


def _job_progress(state: str) -> int:
    return {"queued": 2, "pending": 2, "scripting": 10, "voiceover": 22, "downloading": 38,
            "rendering": 60, "cooldown": 78, "uploading": 90, "cleanup": 97, "done": 100, "error": 0}.get(state, 5)


@app.template_filter("age_fmt")
def age_fmt(ts: int) -> str:
    if not ts:
        return "—"
    secs = int(time.time()) - ts
    if secs < 3600:
        return f"{secs//60}m ago"
    if secs < 86400:
        return f"{secs//3600}h ago"
    return f"{secs//86400}d ago"


# ─────────────────────────────────────────────
#  ROUTES
# ─────────────────────────────────────────────
def _vault_ctx() -> dict:
    trending = trend_hunter.get_trending()
    import random as _r
    return dict(
        quote=_r.choice(VAULT_QUOTES),
        quotes_json=json.dumps(VAULT_QUOTES),
        youtube_ready=youtube_auth.has_token(),
        pexels_ready=bool(os.environ.get("PEXELS_API_KEY")),
        gemini_ready=bool(os.environ.get("GEMINI_API_KEY")),
        elevenlabs_ready=bool(os.environ.get("ELEVENLABS_API_KEY")),
        openrouter_ready=bool(os.environ.get("OPENROUTER_API_KEY")),
        scheduler_running=_scheduler_state["running"],
        redirect_uri=youtube_auth.get_redirect_uri(),
        latest_job=viral_engine.latest_job(),
        job_progress=_job_progress(viral_engine.latest_job().state if viral_engine.latest_job() else "done"),
        trending_videos=trending.get("videos", []),
        trend_updated=trending.get("updated_at"),
        ab_mode_default=youtube_auth.has_token(),
        description="",
        affiliate_url=affiliate_comments.get_affiliate_url(),
        affiliate_cta=affiliate_comments.get_affiliate_cta(),
    )


# ─────────────────────────────────────────────
#  AUTH ROUTES
# ─────────────────────────────────────────────
def _auth_ctx(mode: str = "login", next_url: str = "/",
              invite_token: str = "") -> dict:
    import random as _r
    return dict(
        mode=mode,
        next_url=next_url,
        quote=_r.choice(VAULT_QUOTES),
        google_available=google_auth.has_client_secret(),
        is_first_user=not _load_users(),
        invite_only=_is_invite_only(),
        invite_token=invite_token,
    )


@app.get("/login")
def login_page():
    if session.get("logged_in"):
        return redirect(url_for("index"))
    mode = request.args.get("mode", "login")
    next_url = request.args.get("next", "/")
    invite_token = request.args.get("invite", "")
    if invite_token:
        mode = "signup"
    return render_template_string(AUTH_PAGE, **_auth_ctx(mode, next_url, invite_token))


@app.post("/login")
def login_post():
    import random as _r
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    next_url = request.form.get("next", "/")
    user = _find_user(username)
    if not user or not check_password_hash(user["password_hash"], password):
        flash("Invalid username or password.", "error")
        return render_template_string(AUTH_PAGE, mode="login", next_url=next_url,
                                      quote=_r.choice(VAULT_QUOTES))
    session["logged_in"] = True
    session["username"] = user["username"]
    session["role"] = user.get("role", "viewer")
    session["picture"] = user.get("picture", "")
    return redirect(next_url if next_url.startswith("/") else "/")


@app.post("/signup")
def signup_post():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    confirm = request.form.get("confirm") or ""
    invite_token = (request.form.get("invite_token") or "").strip()
    is_first = not _load_users()

    def _fail(msg: str):
        flash(msg, "error")
        return render_template_string(AUTH_PAGE,
                                      **_auth_ctx("signup", "/", invite_token))

    if len(username) < 3:
        return _fail("Username must be at least 3 characters.")
    if len(password) < 6:
        return _fail("Password must be at least 6 characters.")
    if password != confirm:
        return _fail("Passwords do not match.")

    # Invite-only gate (skip for very first account — that's the founding admin)
    if _is_invite_only() and not is_first:
        if not invite_token:
            return _fail("An invite code is required to sign up.")
        # We'll consume the token after successful account creation
        invites = _load_invites()
        now = time.time()
        valid = next(
            (i for i in invites
             if i["token"] == invite_token and not i["used"] and now < i.get("expires_ts", 0)),
            None,
        )
        if not valid:
            return _fail("That invite code is invalid or has already been used.")

    if not _create_user(username, password):
        return _fail("Username already taken — choose another.")

    # Consume the invite after account is confirmed created
    if _is_invite_only() and not is_first and invite_token:
        _validate_and_consume_invite(invite_token, username)

    user = _find_user(username)
    session["logged_in"] = True
    session["username"] = username
    session["role"] = user.get("role", "viewer") if user else "viewer"
    session["picture"] = ""
    flash(f"Welcome to the Vault, {username}. You are {'Admin' if session['role'] == 'admin' else 'Viewer'}.", "success")
    return redirect(url_for("index"))


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


# ─────────────────────────────────────────────
#  GOOGLE OAUTH ROUTES
# ─────────────────────────────────────────────
@app.get("/auth/google")
def google_auth_start():
    if not google_auth.has_client_secret():
        flash("client_secret.json is missing — Google login unavailable.", "error")
        return redirect(url_for("login_page"))
    for key in ("google_oauth_state", "google_oauth_cv"):
        session.pop(key, None)
    url, state, code_verifier = google_auth.authorization_url()
    session["google_oauth_state"] = state
    session["google_oauth_cv"] = code_verifier   # PKCE verifier stored server-side
    session.modified = True
    return redirect(url)


@app.get("/auth/google/callback")
def google_auth_callback():
    state = session.pop("google_oauth_state", None)
    code_verifier = session.pop("google_oauth_cv", None)

    # Stale/missing session — silent redirect clears any bad state
    if not state:
        return redirect(url_for("login_page"))

    # Error returned by Google (e.g. user cancelled)
    if request.args.get("error"):
        flash(f"Google sign-in cancelled: {request.args['error']}", "error")
        return redirect(url_for("login_page"))

    try:
        profile = google_auth.exchange_code_and_get_profile(
            request.url, state=state, code_verifier=code_verifier or None
        )
    except Exception as err:
        flash(f"Google login failed: {err}", "error")
        return redirect(url_for("login_page"))
    google_id = profile.get("sub", "")
    email = profile.get("email", "")
    name = profile.get("name", email)
    picture = profile.get("picture", "")
    if not google_id or not email:
        flash("Could not retrieve Google profile — try again.", "error")
        return redirect(url_for("login_page"))
    user = _upsert_google_user(google_id, email, name, picture)
    session["logged_in"] = True
    session["username"] = user["username"]
    session["role"] = user.get("role", "viewer")
    session["picture"] = picture
    flash(f"Welcome, {name}! Signed in with Google as {user['role'].capitalize()}.", "success")
    return redirect(url_for("index"))


@app.post("/admin/alerts/<int:index>/ack")
@admin_required
def acknowledge_alert(index: int):
    dashboard.acknowledge_alert(index)
    return redirect(url_for("view_users"))


# ─────────────────────────────────────────────
#  PROTECTED ROUTES
# ─────────────────────────────────────────────
@app.get("/")
@login_required
def index():
    ctx = _vault_ctx()
    ctx["description"] = session.pop("description", "")
    return render_template_string(VAULT_PAGE, **ctx)


@app.post("/force-upload")
@admin_required
def force_upload():
    if not os.environ.get("PEXELS_API_KEY"):
        flash("Set PEXELS_API_KEY before uploading.")
        return redirect(url_for("index"))
    if not os.environ.get("GEMINI_API_KEY"):
        flash("Set GEMINI_API_KEY before uploading.")
        return redirect(url_for("index"))
    # Block if 2 pipelines are already running (thread pool limit)
    if viral_engine.active_job_count() >= 2:
        flash("Pipeline is already running — check the tracker above. Please wait for it to finish.")
        return redirect(url_for("index"))
    seed = (request.form.get("seed") or "").strip() or viral_engine.random_seed()
    ab = bool(request.form.get("ab_mode"))
    do_upload = youtube_auth.has_token()
    try:
        viral_engine.run_in_background(seed, do_upload=do_upload, ab_mode=ab)
    except Exception as exc:
        app.logger.error("force_upload failed to start pipeline: %s", exc)
        flash(f"Could not start pipeline: {exc}")
        return redirect(url_for("index"))
    if not do_upload:
        flash("Pipeline started — YouTube not connected yet so rendering only. Authorize YouTube to enable auto-upload.")
    else:
        msg = "A/B test pipeline started." if ab else "Pipeline started — generating and uploading your Short now."
        flash(msg + " Watch the tracker for live progress.")
    return redirect(url_for("index"))


@app.get("/youtube/auth")
@admin_required
def youtube_auth_start():
    if not youtube_auth.has_client_secret():
        flash("client_secret.json is missing.")
        return redirect(url_for("index"))
    for key in ("yt_oauth_state", "yt_code_verifier"):
        session.pop(key, None)
    url, state, code_verifier = youtube_auth.authorization_url()
    session["yt_oauth_state"] = state
    session["yt_code_verifier"] = code_verifier
    session.modified = True
    return redirect(url)


@app.get("/youtube/callback")
@admin_required
def youtube_auth_callback():
    state = session.pop("yt_oauth_state", None)
    code_verifier = session.pop("yt_code_verifier", None)
    if not state:
        flash("OAuth state missing — click Authorize YouTube again.")
        return redirect(url_for("index"))
    callback_url = request.url
    if callback_url.startswith("http://"):
        callback_url = "https://" + callback_url[len("http://"):]
    try:
        youtube_auth.exchange_code(callback_url, state=state, code_verifier=code_verifier)
        flash("YouTube authorized. Token saved — auto-upload is now live.")
    except Exception as err:
        flash(f"OAuth failed: {err}")
    return redirect(url_for("index"))


@app.post("/affiliate/settings")
@admin_required
def save_affiliate_settings():
    url = (request.form.get("affiliate_url") or "").strip()
    cta = (request.form.get("affiliate_cta") or "").strip()
    affiliate_comments.save_settings({
        k: v for k, v in {"affiliate_url": url, "affiliate_cta": cta}.items() if v
    })
    flash("Affiliate settings saved. Every future upload will have a comment posted automatically.")
    return redirect(url_for("index"))


@app.get("/affiliate/comments")
@login_required
def view_affiliate_comments():
    comments = affiliate_comments.list_comments()
    return render_template_string(AFFILIATE_PAGE, comments=comments)


@app.get("/trends")
@login_required
def view_trend_hunter():
    data = trend_hunter.get_trending()
    raw_kws = data.get("keywords", [])
    freq_map: dict = data.get("keyword_freq", {})
    prev_freq: dict = data.get("prev_keyword_freq", {})

    keywords = []
    for kw in raw_kws:
        freq = freq_map.get(kw, 1)
        prev = prev_freq.get(kw, 0)
        if prev == 0:
            ratio = None
            is_new = True
        else:
            ratio = round(freq / prev, 2) if prev else None
            is_new = False
        keywords.append({"word": kw, "freq": freq, "ratio": ratio, "is_new": is_new})

    keywords.sort(key=lambda x: x["freq"], reverse=True)

    current_freq = freq_map
    prev_f = prev_freq
    spikes = [k for k in keywords
              if (k["ratio"] is not None and k["ratio"] >= 3)
              or (k["is_new"] and k["freq"] >= 3)]

    return render_template_string(
        TREND_HUNTER_PAGE,
        keywords=keywords,
        videos=data.get("videos", []),
        updated_at=data.get("updated_at"),
        pinned=trend_hunter.get_pinned_keyword_record(),
        spikes=spikes,
    )


@app.post("/trends/pin")
@login_required
def pin_trend_keyword():
    kw = (request.form.get("keyword") or "").strip()
    if not kw:
        flash("No keyword provided.")
        return redirect(url_for("view_trend_hunter"))
    trend_hunter.pin_keyword(kw)
    flash(f'"{kw}" pinned — the next scheduled upload will use it as its seed.')
    return redirect(url_for("view_trend_hunter"))


@app.post("/trends/unpin")
@login_required
def unpin_trend_keyword():
    trend_hunter.clear_pinned_keyword()
    flash("Pinned keyword cleared.")
    return redirect(url_for("view_trend_hunter"))


@app.post("/trends/refresh")
@login_required
def refresh_trends():
    import threading as _threading
    _threading.Thread(target=trend_hunter.fetch_trending_with_freq,
                      daemon=True, name="trend-hunter-manual").start()
    flash("Trend refresh triggered — data will update in a few seconds. Reload to see the latest.")
    return redirect(url_for("view_trend_hunter"))


@app.get("/spike-log")
@login_required
def view_spike_log():
    entries = trend_hunter._read_spike_log()
    cooldown_remaining = max(0, int(_SPIKE_COOLDOWN - (time.time() - _last_spike_upload)))
    return render_template_string(SPIKE_LOG_PAGE, entries=entries,
                                  cooldown_remaining=cooldown_remaining)


@app.get("/ab-tests")
@login_required
def view_ab_tests():
    try:
        ab_tester.settle_pending_tests()
    except Exception:
        pass
    tests = ab_tester.list_tests()
    return render_template_string(AB_PAGE, tests=tests)


@app.get("/dashboard")
@login_required
def view_dashboard():
    if not youtube_auth.has_token():
        flash("Authorize YouTube first to load performance data.")
        return render_template_string(
            DASHBOARD_PAGE, rows=[],
            totals={"videos": 0, "views": 0, "likes": 0, "comments": 0,
                    "watch_minutes": 0, "avg_retention": None, "avg_ctr": None},
        )
    try:
        data = dashboard.build_dashboard()
    except Exception as err:
        flash(f"Dashboard error: {err}")
        data = {"rows": [], "totals": {"videos": 0, "views": 0, "likes": 0, "avg_retention": None}}
    return render_template_string(DASHBOARD_PAGE, **data)


@app.post("/generate")
@admin_required
def generate():
    description = request.form.get("description", "").strip()
    if len(description) < 10:
        flash("Add more detail.")
        session["description"] = description
        return redirect(url_for("index"))
    try:
        result = call_gemini(description)
        fn = f"{int(time.time())}-{safe_slug(description)}-voiceover.mp3"
        gTTS(text=result["script"], lang="en", slow=False).save(str(OUTPUT_DIR / fn))
        pexels_links = get_pexels_links(result["keywords"])
    except Exception as err:
        flash(str(err))
        session["description"] = description
        return redirect(url_for("index"))
    session["video_result"] = {"description": description, "voiceover_filename": fn,
                               "pexels_links": pexels_links, **result}
    return redirect(url_for("results"))


@app.get("/results")
@login_required
def results():
    result = session.get("video_result")
    if not result:
        return redirect(url_for("index"))
    return render_template_string(RESULT_PAGE, **result)


@app.get("/output/<path:filename>")
@admin_required
def download_output(filename: str):
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=True)


@app.get("/ping")
def ping():
    return jsonify({"ok": True, "ts": int(time.time())})


@app.get("/api/job-status")
@login_required
def job_status_api():
    job = viral_engine.latest_job()
    if not job:
        return jsonify({"state": "idle"})
    return jsonify({
        "state":      job.state,
        "message":    job.message,
        "title":      job.title,
        "seed":       job.seed,
        "video_id":   job.video_id,
        "video_id_b": job.video_id_b,
        "progress":   _job_progress(job.state),
        "started_at": job.started_at,
        "id":         job.id,
    })


def _base_url() -> str:
    """Best-effort public base URL for invite links."""
    domains = os.environ.get("REPLIT_DOMAINS", "")
    if domains:
        return "https://" + domains.split(",")[0].strip()
    dev = os.environ.get("REPLIT_DEV_DOMAIN", "")
    if dev:
        return "https://" + dev
    return request.host_url.rstrip("/")


@app.get("/admin/users")
@admin_required
def view_users():
    users = _load_users()
    admins = sum(1 for u in users if u.get("role") == "admin")
    viewers = len(users) - admins
    def _mask(key: str) -> str:
        v = os.environ.get(key, "")
        if not v:
            return ""
        return v[:4] + "••••" + v[-3:] if len(v) > 8 else "••••"

    api_keys = {k: _mask(k) for k in
                ["GEMINI_API_KEY", "OPENROUTER_API_KEY", "ELEVENLABS_API_KEY",
                 "PEXELS_API_KEY", "SESSION_SECRET"]}
    alerts = dashboard.list_alerts(unacknowledged_only=True)
    return render_template_string(USERS_PAGE, users=users,
                                  admins=admins, viewers=viewers, total=len(users),
                                  invite_only=_is_invite_only(),
                                  active_invites=_active_invites(),
                                  base_url=_base_url(),
                                  api_keys=api_keys,
                                  alerts=alerts)


@app.post("/admin/users/<username>/role")
@admin_required
def change_user_role(username: str):
    users = _load_users()
    target = next((u for u in users if u["username"] == username), None)
    if not target:
        flash(f"User '{username}' not found.", "error")
        return redirect(url_for("view_users"))
    if username == session.get("username"):
        flash("You cannot change your own role.", "error")
        return redirect(url_for("view_users"))
    old_role = target.get("role", "viewer")
    if old_role == "admin":
        admin_count = sum(1 for u in users if u.get("role") == "admin")
        if admin_count <= 1:
            flash("Cannot demote — at least one Admin must remain.", "error")
            return redirect(url_for("view_users"))
        target["role"] = "viewer"
        flash(f"{username} has been demoted to Viewer.", "success")
    else:
        target["role"] = "admin"
        flash(f"{username} has been promoted to Admin.", "success")
    _save_users(users)
    return redirect(url_for("view_users"))


@app.post("/admin/users/<username>/delete")
@admin_required
def delete_user(username: str):
    if username == session.get("username"):
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("view_users"))
    users = _load_users()
    target = next((u for u in users if u["username"] == username), None)
    if not target:
        flash(f"User '{username}' not found.", "error")
        return redirect(url_for("view_users"))
    if target.get("role") == "admin":
        admin_count = sum(1 for u in users if u.get("role") == "admin")
        if admin_count <= 1:
            flash("Cannot delete the last Admin account.", "error")
            return redirect(url_for("view_users"))
    users = [u for u in users if u["username"] != username]
    _save_users(users)
    flash(f"Account '{username}' has been removed from the Vault.", "success")
    return redirect(url_for("view_users"))


@app.post("/admin/invites")
@admin_required
def create_invite():
    invite = _create_invite(created_by=session["username"])
    link = f"{_base_url()}/login?invite={invite['token']}"
    flash(f"Invite created — expires in 48 h. Link: {link}", "success")
    return redirect(url_for("view_users"))


@app.post("/admin/invites/<token>/revoke")
@admin_required
def revoke_invite(token: str):
    invites = _load_invites()
    found = False
    for inv in invites:
        if inv["token"] == token and not inv["used"]:
            inv["used"] = True
            inv["used_by"] = "__revoked__"
            inv["used_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            found = True
            break
    if found:
        _save_invites(invites)
        flash("Invite revoked successfully.", "success")
    else:
        flash("Invite not found or already used.", "error")
    return redirect(url_for("view_users"))


@app.get("/admin/settings")
@admin_required
def admin_settings():
    saved = _load_settings().get("api_keys", {})
    keys = {}
    for name in _API_KEY_NAMES:
        env_val = os.environ.get(name, "")
        saved_val = saved.get(name, "")
        if env_val:
            source = "env"
            masked = _mask_key(env_val)
        elif saved_val:
            source = "saved"
            masked = _mask_key(saved_val)
        else:
            source = "none"
            masked = ""
        keys[name] = {"source": source, "masked": masked}
    scores = retention_engine.latest_scores(4)
    enrichment = retention_engine.get_prompt_enrichment()
    return render_template_string(
        SETTINGS_PAGE,
        keys=keys,
        yt_ready=youtube_auth.has_token(),
        yt_redirect_uri=youtube_auth.get_redirect_uri(),
        google_redirect_uri=google_auth.get_redirect_uri(),
        retention_scores=scores,
        retention_avg=enrichment.get("avg_retention_pct"),
        retention_target=enrichment.get("target_retention", 45.0),
    )


@app.post("/admin/settings")
@admin_required
def admin_settings_post():
    settings = _load_settings()
    api_keys = settings.get("api_keys", {})
    updated = []
    for name in _API_KEY_NAMES:
        val = (request.form.get(name) or "").strip()
        if val:
            api_keys[name] = val
            os.environ[name] = val
            updated.append(name)
    settings["api_keys"] = api_keys
    _save_settings(settings)
    if updated:
        flash(f"Saved and applied: {', '.join(updated)}", "success")
    else:
        flash("No changes — all fields were left blank.", "success")
    return redirect(url_for("admin_settings"))


@app.post("/admin/settings/clear-key")
@admin_required
def admin_settings_clear_key():
    key_name = (request.form.get("key_name") or "").strip()
    if key_name not in _API_KEY_NAMES:
        flash("Unknown key name — nothing removed.", "error")
        return redirect(url_for("admin_settings"))
    settings = _load_settings()
    api_keys = settings.get("api_keys", {})
    if key_name in api_keys:
        del api_keys[key_name]
        settings["api_keys"] = api_keys
        _save_settings(settings)
        os.environ.pop(key_name, None)
        flash(f"{key_name} removed from the vault.", "success")
    else:
        flash(f"{key_name} is not stored in the vault (Replit Secrets cannot be removed from here).", "info")
    return redirect(url_for("admin_settings"))


@app.post("/admin/settings/run-retention")
@admin_required
def admin_run_retention():
    try:
        result = retention_engine.run_retention_analysis()
        analysed = result.get("analysed", 0)
        if analysed:
            flash(f"Retention analysis complete — {analysed} video(s) scored. Hook memory updated.", "success")
        else:
            flash("Retention analysis ran — no eligible videos yet (videos must be at least 48 hours old).", "info")
    except Exception as exc:
        flash(f"Retention analysis error: {exc}", "error")
    return redirect(url_for("admin_settings"))


@app.post("/admin/settings/invite-only")
@admin_required
def toggle_invite_only():
    settings = _load_settings()
    current = bool(settings.get("invite_only", False))
    settings["invite_only"] = not current
    _save_settings(settings)
    state = "enabled" if settings["invite_only"] else "disabled"
    flash(f"Invite-only mode {state}.", "success")
    return redirect(url_for("view_users"))


SAMPLE_REEL_PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sample Reel — Wealth Vault</title>
<style>
:root{--gold:#D4AF37;--gold-dim:rgba(212,175,55,.08);--gold-border:rgba(212,175,55,.22);--surface:#0a0a0a;--card:#0e0e0e;--muted:#666;--radius:12px}
*{box-sizing:border-box;margin:0;padding:0}
body{background:#000;color:#e8e0d0;font-family:'Inter',system-ui,sans-serif;min-height:100vh}
nav{display:flex;align-items:center;justify-content:space-between;padding:0 28px;height:54px;border-bottom:1px solid #111;background:#000;position:sticky;top:0;z-index:100}
.nav-brand{font-size:.82rem;font-weight:900;letter-spacing:.12em;text-transform:uppercase;color:var(--gold)}
.nav-links{display:flex;gap:6px}
.nav-links a{font-size:.75rem;font-weight:800;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);text-decoration:none;padding:6px 13px;border-radius:7px;transition:color .15s,background .15s}
.nav-links a:hover,.nav-links a.active{color:var(--gold);background:var(--gold-dim)}
.page{max-width:1080px;margin:0 auto;padding:40px 24px 80px}
h1{font-size:1.6rem;font-weight:950;letter-spacing:-.02em;color:var(--gold);margin-bottom:4px}
.subtitle{color:var(--muted);font-size:.86rem;margin-bottom:36px}
.grid{display:grid;grid-template-columns:340px 1fr;gap:32px;align-items:start}
@media(max-width:760px){.grid{grid-template-columns:1fr}}

/* ─── Phone frame ─────────────────────────────── */
.phone-wrap{display:flex;flex-direction:column;align-items:center;gap:16px}
.phone{width:280px;height:498px;background:#050505;border-radius:36px;border:2.5px solid #222;position:relative;overflow:hidden;box-shadow:0 0 0 6px #0d0d0d,0 0 60px rgba(212,175,55,.07)}
.phone-notch{position:absolute;top:14px;left:50%;transform:translateX(-50%);width:72px;height:10px;background:#111;border-radius:6px;z-index:10}
.phone-bg{position:absolute;inset:0;background:linear-gradient(160deg,#0a0806 0%,#050505 40%,#080508 100%);overflow:hidden}
.phone-bg::before{content:'';position:absolute;inset:0;background:
  radial-gradient(ellipse at 30% 20%,rgba(212,175,55,.04) 0%,transparent 60%),
  radial-gradient(ellipse at 80% 70%,rgba(180,100,20,.03) 0%,transparent 50%);
  animation:bgshift 8s ease-in-out infinite alternate}
@keyframes bgshift{0%{opacity:.6}100%{opacity:1}}
.phone-bars{position:absolute;bottom:0;left:0;right:0;height:220px;background:linear-gradient(to top,rgba(212,175,55,.06),transparent);pointer-events:none}
.wm{position:absolute;bottom:28px;right:16px;font-size:9px;font-weight:700;color:rgba(255,255,255,.10);letter-spacing:.06em;text-transform:uppercase}
.caption-stage{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;padding:0 18px}
.caption-word{font-size:36px;font-weight:950;color:#fff;text-align:center;text-shadow:0 0 18px rgba(0,0,0,.9),0 3px 0 rgba(0,0,0,.95);letter-spacing:-.01em;line-height:1.1;text-transform:uppercase;animation:wordpop .22s cubic-bezier(.34,1.56,.64,1);max-width:260px;word-break:break-word}
@keyframes wordpop{0%{transform:scale(.7);opacity:0}100%{transform:scale(1);opacity:1}}
.phone-tag{position:absolute;top:32px;left:18px;font-size:9px;font-weight:900;letter-spacing:.1em;text-transform:uppercase;color:rgba(212,175,55,.7);background:rgba(212,175,55,.07);border:1px solid rgba(212,175,55,.15);padding:2px 8px;border-radius:4px}
.timer-bar{width:280px;height:3px;background:#111;border-radius:2px;overflow:hidden}
.timer-fill{height:100%;background:var(--gold);border-radius:2px;width:0%;transition:width .05s linear}
.play-btn{display:flex;align-items:center;gap:8px;background:var(--gold-dim);border:1px solid var(--gold-border);color:var(--gold);font-size:.76rem;font-weight:900;letter-spacing:.08em;text-transform:uppercase;padding:8px 20px;border-radius:8px;cursor:pointer;transition:background .15s}
.play-btn:hover{background:rgba(212,175,55,.18)}
.play-btn svg{width:14px;height:14px;fill:var(--gold);flex-shrink:0}

/* ─── Info panel ──────────────────────────────── */
.card{background:var(--card);border:1px solid #1a1a1a;border-radius:var(--radius);padding:22px 24px;margin-bottom:16px}
.card-title{font-size:.68rem;font-weight:900;letter-spacing:.1em;text-transform:uppercase;color:var(--gold);margin-bottom:14px}
.script-text{font-size:.88rem;line-height:1.75;color:#c8c0b0;white-space:pre-wrap}
.pause-mark{color:rgba(212,175,55,.5);font-style:italic;font-size:.78rem}
.title-row{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid #111}
.title-row:last-child{border-bottom:none}
.score-bar-wrap{width:54px;flex-shrink:0}
.score-bar{height:4px;background:#1a1a1a;border-radius:2px;overflow:hidden}
.score-fill{height:100%;border-radius:2px;background:linear-gradient(90deg,var(--gold),#b8960c)}
.score-num{font-size:.62rem;font-weight:900;color:var(--gold);text-align:right;margin-bottom:2px}
.title-text{font-size:.82rem;font-weight:700;color:#e8e0d0;flex:1;line-height:1.4}
.best-badge{font-size:.58rem;font-weight:900;letter-spacing:.08em;text-transform:uppercase;background:rgba(212,175,55,.12);border:1px solid var(--gold-border);color:var(--gold);padding:2px 7px;border-radius:4px;flex-shrink:0}
.tags-wrap{display:flex;flex-wrap:wrap;gap:6px}
.tag{font-size:.7rem;font-weight:700;color:var(--muted);background:#111;border:1px solid #1c1c1c;padding:3px 9px;border-radius:5px;letter-spacing:.04em}
.kw-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:6px}
.kw-item{display:flex;align-items:center;gap:8px;font-size:.78rem;color:#c8c0b0;background:#0a0a0a;border:1px solid #161616;border-radius:7px;padding:7px 10px}
.kw-icon{color:var(--gold);font-size:.78rem;flex-shrink:0}
.meta-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px}
.meta-cell{background:#0a0a0a;border:1px solid #161616;border-radius:8px;padding:10px 12px;text-align:center}
.meta-label{font-size:.6rem;font-weight:900;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-bottom:3px}
.meta-value{font-size:1.1rem;font-weight:950;color:var(--gold)}
.desc-box{font-size:.78rem;line-height:1.7;color:var(--muted);background:#050505;border:1px solid #111;border-radius:8px;padding:12px 14px;white-space:pre-wrap}
.pipeline-badge{display:inline-flex;align-items:center;gap:6px;font-size:.66rem;font-weight:900;letter-spacing:.07em;text-transform:uppercase;padding:4px 12px;border-radius:6px;margin-bottom:8px}
.pb-ok{background:rgba(34,197,94,.08);border:1px solid rgba(34,197,94,.2);color:#22c55e}
.pb-model{background:rgba(212,175,55,.08);border:1px solid var(--gold-border);color:var(--gold)}
</style>
</head><body>
<nav>
  <span class="nav-brand">&#9679; Wealth Vault</span>
  <div class="nav-links">
    <a href="{{ url_for('index') }}">Command Center</a>
    <a href="{{ url_for('view_dashboard') }}">Dashboard</a>
    <a href="{{ url_for('view_ab_tests') }}">A/B Tests</a>
    <a href="{{ url_for('sample_reel') }}" class="active">Sample Reel</a>
    {% if session.get('role') == 'admin' %}<a href="{{ url_for('view_users') }}">Users</a>{% endif %}
    {% if session.get('role') == 'admin' %}<a href="{{ url_for('admin_settings') }}">Settings</a>{% endif %}
  </div>
  <div style="display:flex;align-items:center;gap:8px;margin-right:8px;">
    {% if session.get('picture') %}
      <img src="{{ session.get('picture') }}" width="26" height="26" style="border-radius:50%;border:1.5px solid var(--gold-border);object-fit:cover;" alt="">
    {% else %}
      <div style="width:26px;height:26px;border-radius:50%;background:linear-gradient(135deg,var(--gold),#5c3e00);display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:900;color:#000;">{{ (session.get('username','?')[0])|upper }}</div>
    {% endif %}
    <a href="{{ url_for('logout') }}" style="font-size:.72rem;font-weight:900;letter-spacing:.08em;text-transform:uppercase;color:var(--gold);border:1px solid var(--gold-border);background:var(--gold-dim);padding:5px 12px;border-radius:7px;text-decoration:none;">Logout</a>
  </div>
</nav>
<div class="page">
  <h1>Sample Reel Output</h1>
  <p class="subtitle">A realistic preview of one engine run — script, captions, SEO Oracle titles, and upload metadata.</p>

  <div class="grid">
    <!-- ── Phone preview ────────────────────────────── -->
    <div class="phone-wrap">
      <div class="phone">
        <div class="phone-notch"></div>
        <div class="phone-bg"></div>
        <div class="phone-bars"></div>
        <div class="phone-tag">#WealthVault #Shorts</div>
        <div class="caption-stage">
          <div class="caption-word" id="cap">TAP PLAY</div>
        </div>
        <div class="wm">Crypto Affiliate Hub</div>
      </div>
      <div class="timer-bar"><div class="timer-fill" id="tbar"></div></div>
      <button class="play-btn" id="playBtn" onclick="startReel()">
        <svg viewBox="0 0 16 16"><polygon points="3,1 13,8 3,15"/></svg>
        <span id="playLabel">Play Caption Preview</span>
      </button>
      <div style="font-size:.68rem;color:var(--muted);text-align:center;max-width:260px;line-height:1.5;">
        Word-by-word captions · 30 fps rendering · 1080×1920 portrait<br>
        gTTS or ElevenLabs narration · Pexels B-roll every 3 s
      </div>
    </div>

    <!-- ── Info panel ─────────────────────────────── -->
    <div>
      <!-- Pipeline badges -->
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:18px;">
        <span class="pipeline-badge pb-ok">&#10003; Script — Gemini 1.5 Flash</span>
        <span class="pipeline-badge pb-ok">&#10003; Voice — ElevenLabs Brian</span>
        <span class="pipeline-badge pb-ok">&#10003; B-roll — 8 Pexels clips</span>
        <span class="pipeline-badge pb-ok">&#10003; MoviePy render — 30 fps</span>
        <span class="pipeline-badge pb-model">SEO Oracle — 5 title variants</span>
      </div>

      <!-- Stats -->
      <div class="meta-grid">
        <div class="meta-cell"><div class="meta-label">Duration</div><div class="meta-value">38 s</div></div>
        <div class="meta-cell"><div class="meta-label">Word count</div><div class="meta-value">114</div></div>
        <div class="meta-cell"><div class="meta-label">Clip swaps</div><div class="meta-value">12×</div></div>
      </div>

      <!-- Script -->
      <div class="card">
        <div class="card-title">Generated Script</div>
        <div class="script-text">They don't want you to know this.

The elite don't work harder — they work on things that <em>compound</em>. <span class="pause-mark">[pause]</span>

While you trade time for money, they build systems that earn while they sleep. The silent weapon isn't discipline. It's strategic invisibility. <span class="pause-mark">[pause]</span>

The wealthiest minds never reveal their next move. They let the crowd react — then position themselves ahead of the chaos. <span class="pause-mark">[pause]</span>

Information asymmetry is the real currency. The moment you understand what others don't — you win before the game even starts. <span class="pause-mark">[pause]</span>

And that's exactly why they spend millions keeping financial education out of schools.

They don't want you to know this.</div>
      </div>

      <!-- SEO Titles -->
      <div class="card">
        <div class="card-title">SEO Oracle — Title Variants</div>
        {% for t in titles %}
        <div class="title-row">
          <div class="score-bar-wrap">
            <div class="score-num">{{ t.score }}</div>
            <div class="score-bar"><div class="score-fill" style="width:{{ t.score }}%;"></div></div>
          </div>
          <div class="title-text">{{ t.title }}</div>
          {% if loop.first %}<span class="best-badge">Best Pick</span>{% endif %}
        </div>
        {% endfor %}
      </div>

      <!-- Keywords -->
      <div class="card">
        <div class="card-title">SEO Keywords (8)</div>
        <div class="kw-grid">
          {% for kw in keywords %}
          <div class="kw-item"><span class="kw-icon">&#9670;</span>{{ kw }}</div>
          {% endfor %}
        </div>
      </div>

      <!-- Tags -->
      <div class="card">
        <div class="card-title">Upload Tags (12)</div>
        <div class="tags-wrap">
          {% for tag in tags %}
          <span class="tag">#{{ tag }}</span>
          {% endfor %}
        </div>
      </div>

      <!-- Description -->
      <div class="card">
        <div class="card-title">YouTube Description</div>
        <div class="desc-box">{{ description }}</div>
      </div>
    </div>
  </div>
</div>

<script>
const WORDS = `They don't want you to know this. The elite don't work harder they work on things that compound. While you trade time for money they build systems that earn while they sleep. The silent weapon isn't discipline. It's strategic invisibility. The wealthiest minds never reveal their next move. They let the crowd react then position themselves ahead of the chaos. Information asymmetry is the real currency. The moment you understand what others don't you win before the game even starts. And that's exactly why they spend millions keeping financial education out of schools. They don't want you to know this.`.split(/\s+/).filter(Boolean);

let timer = null;
let idx = 0;

function startReel() {
  if (timer) { clearInterval(timer); timer = null; idx = 0; }
  const cap = document.getElementById('cap');
  const bar = document.getElementById('tbar');
  const btn = document.getElementById('playBtn');
  const lbl = document.getElementById('playLabel');
  lbl.textContent = 'Restart';
  idx = 0;
  bar.style.width = '0%';

  function tick() {
    if (idx >= WORDS.length) {
      clearInterval(timer); timer = null;
      lbl.textContent = 'Play Caption Preview';
      cap.textContent = '&#9679;';
      bar.style.width = '100%';
      setTimeout(() => { bar.style.width = '0%'; cap.innerHTML = 'TAP PLAY'; }, 1200);
      return;
    }
    cap.style.animation = 'none';
    cap.offsetHeight; /* reflow */
    cap.style.animation = '';
    cap.textContent = WORDS[idx].toUpperCase();
    bar.style.width = ((idx / WORDS.length) * 100) + '%';
    idx++;
  }
  tick();
  timer = setInterval(tick, 310);
}
</script>
</body></html>
"""


def _render_sample_reel():
    titles = [
        {"title": "The DARK SECRET The Rich Will Never Tell You", "score": 96},
        {"title": "Why You Stay Broke — And How They Keep It That Way", "score": 91},
        {"title": "The Silent Weapon of the Elite (Exposed)", "score": 88},
        {"title": "Financial Education They BANNED From Schools", "score": 85},
        {"title": "The 1% Blueprint: How Power Compounds in Silence", "score": 82},
    ]
    keywords = [
        "dark psychology wealth", "elite money secrets", "financial manipulation",
        "compound wealth systems", "information asymmetry", "broke mindset trap",
        "billionaire tactics", "strategic invisibility",
    ]
    tags = [
        "wealth", "darkpsychology", "shorts", "mindset", "money",
        "elitesecrets", "financialfreedom", "psychology", "viral",
        "richvspoor", "WealthVault", "WealthVaultEntry",
    ]
    description = (
        "The elite don't work harder — they work on systems that compound while you sleep.\n\n"
        "This isn't motivation. This is the blueprint they don't teach in schools.\n\n"
        "If this hit, follow for the dark psychology of wealth they hope you never find.\n\n"
        "#Shorts #Wealth #DarkPsychology #MoneyMindset #EliteSecrets #WealthVault"
    )
    return render_template_string(
        SAMPLE_REEL_PAGE,
        titles=titles,
        keywords=keywords,
        tags=tags,
        description=description,
    )


@app.get("/sample-reel-public")
def sample_reel_public():
    return _render_sample_reel()


@app.get("/sample-reel-preview")
def sample_reel_preview():
    return send_from_directory("static", "sample-reel-preview.html")


@app.get("/sample-reel")
@login_required
def sample_reel():
    return _render_sample_reel()


@app.get("/healthz")
def healthz():
    return {"status": "ok", "youtube_authorized": youtube_auth.has_token(),
            "scheduler": _scheduler_state["running"]}


# ─────────────────────────────────────────────
#  SCHEDULER  (08:00 London / 20:00 New York)
# ─────────────────────────────────────────────
_scheduler_state: dict = {"running": False, "scheduler": None}


def _scheduled_job() -> None:
    if not (youtube_auth.has_token() and os.environ.get("PEXELS_API_KEY") and os.environ.get("GEMINI_API_KEY")):
        return
    # Settle any 48h-old A/B tests before starting the new run
    try:
        ab_tester.settle_pending_tests()
    except Exception:
        pass
    # Snapshot keyword frequencies before refresh so the next spike-check has a baseline
    try:
        trend_hunter.snapshot_keyword_frequencies()
    except Exception:
        pass
    # Refresh trending data with frequency tracking
    try:
        trend_hunter.fetch_trending_with_freq()
    except Exception:
        pass
    pinned_kw = trend_hunter.get_pinned_keyword()
    if pinned_kw:
        seed = (f"the wealth secret behind {pinned_kw} — what the elite already know "
                f"and why they keep it hidden from the masses")
        try:
            trend_hunter.clear_pinned_keyword()
        except Exception:
            pass
        app.logger.info("Scheduler using pinned keyword seed: %r", pinned_kw)
    else:
        seed = viral_engine.random_seed()
    viral_engine.run_in_background(seed, do_upload=True, ab_mode=True)


# Minimum gap between auto-seeder emergency uploads (seconds)
_SPIKE_COOLDOWN = 4 * 3600
_last_spike_upload: float = 0.0


def _spike_check_job() -> None:
    """Runs every 30 minutes. Fires an emergency upload if any keyword has spiked 3x."""
    global _last_spike_upload
    if not (youtube_auth.has_token() and os.environ.get("PEXELS_API_KEY") and os.environ.get("GEMINI_API_KEY")):
        return
    if (time.time() - _last_spike_upload) < _SPIKE_COOLDOWN:
        return
    try:
        trend_hunter.fetch_trending_with_freq()
        spikes = trend_hunter.detect_spike(threshold=3.0)
    except Exception:
        return
    if not spikes:
        return
    top = spikes[:3]
    kws = [s["keyword"] for s in top]
    seed = f"the sudden rise of {kws[0]} — what the elite already know about {' and '.join(kws[:2])}"
    job = viral_engine.run_in_background(seed, do_upload=True, ab_mode=False)
    _last_spike_upload = time.time()
    try:
        trend_hunter.log_spike_upload(kws, seed, job.id)
    except Exception:
        pass
    app.logger.info("Auto-Seeder fired emergency upload — spike keywords: %s | job %s", kws, job.id)


def start_scheduler() -> None:
    if _scheduler_state["running"]:
        return
    sched = BackgroundScheduler()
    # 08:00 London (UTC / BST — we use UTC+0; BST auto-shifts ±1h which is acceptable)
    sched.add_job(_scheduled_job, CronTrigger(hour=8, minute=0, timezone="Europe/London"),
                  id="morning_london", replace_existing=True)
    # 20:00 New York
    sched.add_job(_scheduled_job, CronTrigger(hour=20, minute=0, timezone="America/New_York"),
                  id="evening_ny", replace_existing=True)
    # Spike check every 30 minutes — fires emergency upload when any keyword jumps 3x
    sched.add_job(_spike_check_job, CronTrigger(minute="*/30"),
                  id="spike_check", replace_existing=True)
    # Retention analytics loop — runs every 48 hours, scores hook performance,
    # updates hook_memory.json so the next script generation gets smarter prompts
    sched.add_job(
        lambda: retention_engine.run_retention_analysis(),
        CronTrigger(hour=6, minute=30),
        id="retention_loop",
        replace_existing=True,
    )
    sched.start()
    _scheduler_state["running"] = True
    _scheduler_state["scheduler"] = sched


def _start_keepalive() -> None:
    """
    Self-pinger that prevents Replit from sleeping.

    Every 4 minutes this thread hits /ping on the server's own public URL.
    When the app is deployed (always-on), this is a no-op safety net.
    When running on Replit free tier, it prevents the 10-minute inactivity
    sleep that would stop scheduled uploads from firing.
    """
    import threading
    import urllib.request

    def _loop() -> None:
        time.sleep(60)          # let Flask fully start first
        while True:
            try:
                domain = (
                    os.environ.get("REPLIT_DOMAINS", "").split(",")[0].strip()
                    or os.environ.get("REPLIT_DEV_DOMAIN", "")
                )
                if domain:
                    url = f"https://{domain}/ping"
                    urllib.request.urlopen(url, timeout=10)    # noqa: S310
                    app.logger.debug("keep-alive ping OK → %s", url)
            except Exception as exc:
                app.logger.debug("keep-alive ping failed (non-fatal): %s", exc)
            time.sleep(240)     # ping every 4 minutes

    t = threading.Thread(target=_loop, daemon=True, name="keepalive")
    t.start()
    app.logger.info("keep-alive: self-pinger started (240s interval) — server will not sleep.")


_load_api_keys_into_env()
start_scheduler()
uploader.start()
_start_keepalive()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
