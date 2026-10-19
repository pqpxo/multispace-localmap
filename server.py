"""
server.py — Local Map Platform

A configurable community map server for any UK area.
On first run with no config.json, serves a setup wizard.
After setup, serves the map frontend driven by config.json.

Run:
    python3 server.py

Environment variables:
    ADMIN_TOKEN     — required for admin API access
    ADMIN_PATH      — URL path for admin page (default: random hex)
    CONFIG_PATH     — path to config.json (default: data/config.json)
    DB_PATH         — path to SQLite database (default: data/places.db)
    SMTP_HOST/PORT/USER/PASS/ADMIN_EMAIL — email notifications
    PUSHOVER_TOKEN/PUSHOVER_USER — push notifications
"""

import hashlib
import hmac
import json
import os
import secrets
import shutil
import smtplib
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from email.mime.text import MIMEText
from functools import wraps
from pathlib import Path

import requests as ext_requests
from flask import Flask, abort, jsonify, request, send_from_directory, redirect
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

# ---------------------------------------------------------------------------
# Paths & env
# ---------------------------------------------------------------------------
__version__ = "1.0.0"

DATA_DIR = os.environ.get("DATA_DIR", "data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
CONFIG_PATH = os.environ.get("CONFIG_PATH", os.path.join(DATA_DIR, "config.json"))
DB_PATH = os.environ.get("DB_PATH", os.path.join(DATA_DIR, "places.db"))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "changeme")
ADMIN_PATH = os.environ.get("ADMIN_PATH", "").strip() or secrets.token_hex(8)

# Email
SMTP_HOST = os.environ.get("SMTP_HOST", "mail.privateemail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")

# Pushover
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "")

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return None

def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

def is_setup_complete():
    cfg = load_config()
    return bool(cfg and cfg.get("setup_complete"))


# ---------------------------------------------------------------------------
# Secrets — API keys, kept out of config.json and never sent to the browser
# ---------------------------------------------------------------------------
SECRETS_PATH = os.path.join(DATA_DIR, "secrets.json")

# Keys that must never reach an unauthenticated client.
SECRET_FIELDS = ("google_api_key", "foursquare_api_key")

# The CARTO key is unavoidably public: the browser builds tile URLs with it.
# Restrict it by domain in the CARTO dashboard rather than relying on secrecy.
PUBLIC_KEY_FIELDS = ("carto_api_key",)


def load_secrets():
    if os.path.exists(SECRETS_PATH):
        try:
            with open(SECRETS_PATH) as f:
                return json.load(f)
        except Exception as e:
            print(f"Could not read secrets file: {e}")
    return {}


def save_secrets(secrets):
    os.makedirs(os.path.dirname(SECRETS_PATH), exist_ok=True)
    with open(SECRETS_PATH, "w") as f:
        json.dump(secrets, f, indent=2)
    try:
        os.chmod(SECRETS_PATH, 0o600)   # owner read/write only
    except OSError:
        pass


def mask_secret(value):
    """Render a key as e.g. 'AIza…9f3c' so it can be shown without exposing it."""
    if not value:
        return ""
    v = str(value)
    if len(v) <= 8:
        return "•" * len(v)
    return f"{v[:4]}{'•' * 8}{v[-4:]}"


def get_secret(name):
    return (SECRETS.get(name) or "").strip()


def _migrate_secrets_from_config():
    """Move any API keys previously stored in config.json into secrets.json."""
    if not SITE_CONFIG:
        return
    moved = []
    for field in SECRET_FIELDS + PUBLIC_KEY_FIELDS:
        val = SITE_CONFIG.pop(field, None)
        if val:
            SECRETS[field] = val
            moved.append(field)
    if moved:
        save_secrets(SECRETS)
        save_config(SITE_CONFIG)
        print(f"Moved {', '.join(moved)} out of config.json into secrets.json")


def public_config():
    """The config as served to unauthenticated clients — secrets removed."""
    if not SITE_CONFIG:
        return None
    cfg = {k: v for k, v in SITE_CONFIG.items() if k not in SECRET_FIELDS}
    cfg["version"] = __version__
    # The frontend needs the CARTO key to build tile URLs
    for field in PUBLIC_KEY_FIELDS:
        if get_secret(field):
            cfg[field] = get_secret(field)
    return cfg


SITE_CONFIG = load_config()
SECRETS = load_secrets()
_migrate_secrets_from_config()

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Create tables if they don't exist."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS places (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            osm_type TEXT,
            osm_id TEXT,
            name TEXT,
            group_name TEXT,
            subcategory TEXT,
            osm_key TEXT,
            address TEXT,
            postcode TEXT,
            phone TEXT,
            website TEXT,
            email TEXT,
            opening_hours TEXT,
            lat REAL,
            lon REAL,
            source TEXT DEFAULT 'manual',
            sources TEXT DEFAULT 'manual',
            last_verified TEXT,
            status TEXT DEFAULT 'active',
            hygiene_rating TEXT,
            hygiene_rating_date TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_places_group ON places(group_name);
        CREATE INDEX IF NOT EXISTS idx_places_status ON places(status);
        CREATE INDEX IF NOT EXISTS idx_places_latlon ON places(lat, lon);
        CREATE INDEX IF NOT EXISTS idx_places_name ON places(name);

        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            place_id INTEGER REFERENCES places(id),
            payload TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            admin_notes TEXT
        );
    """)
    conn.commit()
    conn.close()

def now_iso():
    return datetime.now(timezone.utc).isoformat()

init_db()

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder="public", static_url_path="")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2MB (for logo uploads)


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    """Never return an HTML error page to an API caller — the frontend parses JSON."""
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        if request.path.startswith("/api/"):
            return jsonify({"error": e.description, "status": e.code}), e.code
        return e
    app.logger.exception("Unhandled error on %s", request.path)
    if request.path.startswith("/api/"):
        return jsonify({"error": f"Server error: {type(e).__name__}"}), 500
    return "Internal server error", 500


@app.after_request
def add_security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"] = "geolocation=(self), camera=(), microphone=()"

    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdnjs.cloudflare.com https://static.cloudflareinsights.com https://cloud.umami.is; "
        "style-src 'self' 'unsafe-inline' https://unpkg.com https://fonts.googleapis.com https://cdnjs.cloudflare.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https:; "
        "connect-src 'self' https://api.postcodes.io https://nominatim.openstreetmap.org https://cloud.umami.is https://static.cloudflareinsights.com https://cloudflareinsights.com; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "object-src 'none';"
    )
    # Only force HTTPS upgrades when the request actually arrived over HTTPS.
    # Adding this on a plain-HTTP deployment breaks same-origin fetch() calls.
    if request.is_secure:
        csp += " upgrade-insecure-requests;"

    resp.headers["Content-Security-Policy"] = csp
    return resp


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
def notify_admin(sub_type, payload, place_name=None):
    site_name = (SITE_CONFIG or {}).get("site_name", "Local Map")
    subject = f"{site_name}: new {sub_type} submission"
    place = f" for {place_name}" if place_name else ""
    body = f"New {sub_type} submission{place}.\n\n"

    if sub_type == "review":
        body += f"Stars: {payload.get('stars', '?')}/5\n"
        body += f"Comment: {payload.get('comment', '')}\n"
    elif sub_type == "event":
        body += f"Title: {payload.get('title', '')}\n"
        body += f"Date: {payload.get('date', '')} {payload.get('time', '')}\n"
    elif sub_type == "new_business":
        body += f"Name: {payload.get('name', '')}\n"
        body += f"Address: {payload.get('address', '')}\n"
    else:
        body += json.dumps(payload, indent=2)

    body += f"\nReview at: /{ADMIN_PATH}"

    if SMTP_USER and SMTP_PASS and ADMIN_EMAIL:
        threading.Thread(target=_send_email, args=(subject, body), daemon=True).start()
    if PUSHOVER_TOKEN and PUSHOVER_USER:
        threading.Thread(target=_send_pushover, args=(subject, body), daemon=True).start()


def _send_email(subject, body):
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = SMTP_USER
        msg["To"] = ADMIN_EMAIL
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as s:
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
    except Exception as e:
        print(f"Email notification failed: {e}")


def _send_pushover(title, body):
    try:
        ext_requests.post(
            "https://api.pushover.net/1/messages.json",
            data={"token": PUSHOVER_TOKEN, "user": PUSHOVER_USER,
                  "title": title, "message": body[:1024], "priority": 0},
            timeout=10)
    except Exception as e:
        print(f"Pushover notification failed: {e}")


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
_rate_store = defaultdict(list)
RATE_LIMIT = 10
RATE_WINDOW = 3600

def check_rate_limit():
    ip = request.remote_addr or "unknown"
    now = time.time()
    _rate_store[ip] = [t for t in _rate_store[ip] if now - t < RATE_WINDOW]
    if len(_rate_store[ip]) >= RATE_LIMIT:
        return True
    _rate_store[ip].append(now)
    return False


# ---------------------------------------------------------------------------
# Admin auth with brute-force lockout
# ---------------------------------------------------------------------------
_failed_logins = defaultdict(list)

def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        ip = request.remote_addr or "unknown"
        now = time.time()
        _failed_logins[ip] = [t for t in _failed_logins[ip] if now - t < 900]
        if len(_failed_logins[ip]) >= 5:
            abort(429)
        auth = request.headers.get("Authorization", "")
        if not hmac.compare_digest(auth, f"Bearer {ADMIN_TOKEN}"):
            _failed_logins[ip].append(now)
            abort(401)
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Routes: Setup wizard (first-run only)
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    if not is_setup_complete():
        return send_from_directory("public", "setup.html")
    return send_from_directory("public", "index.html")


@app.route(f"/{ADMIN_PATH}")
def admin_page():
    return send_from_directory("public", "admin.html")


@app.route("/api/config")
def get_config():
    if not SITE_CONFIG:
        return jsonify({"setup_required": True}), 404
    return jsonify(public_config())


@app.route("/api/setup/save", methods=["POST"])
def setup_save():
    """Save the setup wizard config.

    The wizard may call this several times (e.g. before running the data
    import, then again at the end). Writes are permitted until the config is
    explicitly finalised with {"finalize": true}.
    """
    global SITE_CONFIG
    existing = load_config() or {}
    if existing.get("setup_complete"):
        return jsonify({"error": "Setup already complete"}), 403

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    required = ["site_name", "area_name", "center"]
    for field in required:
        if not data.get(field):
            return jsonify({"error": f"Missing required field: {field}"}), 400

    cfg = {
        "site_name": data["site_name"],
        "area_name": data["area_name"],
        "tagline": data.get("tagline", f"Explore shops, services, events & more across {data['area_name']}"),
        "center": data["center"],
        "default_zoom": data.get("default_zoom", 14),
        "min_zoom": data.get("min_zoom", 13),
        "radius_km": data.get("radius_km", 3),
        "boundary_polygon": data.get("boundary_polygon", []),
        "overpass_bbox": data.get("overpass_bbox", []),
        "postcode_prefix": data.get("postcode_prefix", ""),
        "theme_color": data.get("theme_color", "#35425c"),
        "theme": data.get("theme", {}),
        "tiles": data.get("tiles", {"default": "Light", "carto_api_key": ""}),
        "logo": data.get("logo", "logo.png"),
        "quick_links": data.get("quick_links", []),
        "social": data.get("social", {}),
        "setup_complete": bool(data.get("finalize")),
    }

    # Preserve any import history recorded during the wizard
    existing = load_config() or {}
    if existing.get("last_import"):
        cfg["last_import"] = existing["last_import"]

    # API keys entered in the wizard go to secrets.json, never config.json
    secrets_changed = False
    for field in SECRET_FIELDS + PUBLIC_KEY_FIELDS:
        val = (data.get(field) or "").strip()
        if val:
            SECRETS[field] = val
            secrets_changed = True
    if secrets_changed:
        save_secrets(SECRETS)

    save_config(cfg)
    SITE_CONFIG = cfg
    return jsonify({"ok": True, "finalized": cfg["setup_complete"]})


@app.route("/api/setup/upload-logo", methods=["POST"])
def upload_logo():
    """Upload a logo image during setup."""
    if is_setup_complete():
        return jsonify({"error": "Setup already complete"}), 403
    if "logo" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    f = request.files["logo"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    # Only allow image types
    allowed = {".png", ".jpg", ".jpeg", ".svg", ".webp"}
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in allowed:
        return jsonify({"error": f"File type {ext} not allowed"}), 400

    filename = "logo" + ext
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    f.save(os.path.join(UPLOAD_DIR, filename))
    return jsonify({"ok": True, "filename": "/uploads/" + filename})


@app.route("/uploads/<path:filename>")
def serve_upload(filename):
    """Serve user-uploaded images from the persistent data volume."""
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/api/setup/geocode", methods=["POST"])
def setup_geocode():
    """Geocode a postcode or place name for the setup wizard."""
    if is_setup_complete():
        return jsonify({"error": "Setup already complete"}), 403
    if check_rate_limit():
        return jsonify({"error": "Too many requests"}), 429

    data = request.get_json(silent=True)
    query = (data or {}).get("query", "").strip()
    if not query:
        return jsonify({"error": "Query required"}), 400
    if len(query) > 120:
        return jsonify({"error": "Query too long"}), 400

    # Try postcodes.io first (for UK postcodes)
    try:
        r = ext_requests.get(
            f"https://api.postcodes.io/postcodes/{query}",
            timeout=5)
        if r.status_code == 200:
            result = r.json().get("result", {})
            return jsonify({
                "lat": result.get("latitude"),
                "lon": result.get("longitude"),
                "name": result.get("admin_ward", query),
                "postcode": result.get("postcode"),
                "region": result.get("admin_district"),
            })
    except Exception:
        pass

    # Fallback: Nominatim (OpenStreetMap geocoding)
    try:
        r = ext_requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": f"{query}, UK", "format": "json", "limit": 1},
            headers={"User-Agent": "LocalMap/1.0"},
            timeout=5)
        results = r.json()
        if results:
            return jsonify({
                "lat": float(results[0]["lat"]),
                "lon": float(results[0]["lon"]),
                "name": results[0].get("display_name", query).split(",")[0],
            })
    except Exception:
        pass

    return jsonify({"error": "Could not geocode that location"}), 404


@app.route("/api/setup/boundary", methods=["POST"])
def setup_boundary():
    """Generate an approximate boundary polygon from a centre point and radius."""
    if is_setup_complete():
        return jsonify({"error": "Setup already complete"}), 403
    data = request.get_json(silent=True)
    lat = (data or {}).get("lat")
    lon = (data or {}).get("lon")
    radius_km = (data or {}).get("radius_km", 3)

    if not lat or not lon:
        return jsonify({"error": "lat and lon required"}), 400

    import math
    # Generate a rough circle polygon (16 points)
    points = []
    for i in range(16):
        angle = (2 * math.pi * i) / 16
        dlat = (radius_km / 111.32) * math.cos(angle)
        dlon = (radius_km / (111.32 * math.cos(math.radians(lat)))) * math.sin(angle)
        points.append([round(lat + dlat, 6), round(lon + dlon, 6)])
    points.append(points[0])  # close the polygon

    # Also calculate overpass bounding box
    lat_pad = radius_km / 111.32
    lon_pad = radius_km / (111.32 * math.cos(math.radians(lat)))
    bbox = [
        round(lat - lat_pad, 4),
        round(lon - lon_pad, 4),
        round(lat + lat_pad, 4),
        round(lon + lon_pad, 4),
    ]

    return jsonify({"polygon": points, "bbox": bbox})


IMPORTERS = {}   # populated below: name -> callable returning (count, error)


def _record_import(source, count):
    """Store the timestamp of a successful import so the admin can spot stale data."""
    global SITE_CONFIG
    if not SITE_CONFIG:
        return
    history = SITE_CONFIG.setdefault("last_import", {})
    history[source] = {"at": now_iso(), "count": count}
    save_config(SITE_CONFIG)


def _run_import(source):
    """Shared import runner used by both the wizard and the admin panel."""
    fn = IMPORTERS.get(source)
    if not fn:
        return None, jsonify({"error": f"Unknown source: {source}"}), 400

    count, error = fn()
    if error:
        return None, jsonify({"error": error}), 502

    _record_import(source, count)
    return count, None, None


# ---------------------------------------------------------------------------
# Background import jobs
#
# Some imports (Overture in particular) take minutes, which exceeds the default
# timeout on most reverse proxies. Running them in a thread and polling for
# status keeps the HTTP requests short.
# ---------------------------------------------------------------------------
_JOBS = {}
_JOBS_LOCK = threading.Lock()
JOB_RETENTION = 3600      # forget finished jobs after an hour


def _prune_jobs():
    now = time.time()
    for jid in [j for j, v in _JOBS.items()
                if v.get("finished_at") and now - v["finished_at"] > JOB_RETENTION]:
        _JOBS.pop(jid, None)


def _job_worker(job_id, source):
    fn = IMPORTERS.get(source)
    try:
        count, error = fn()
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is None:
                return
            job["finished_at"] = time.time()
            if error:
                job.update(status="error", error=error)
            else:
                job.update(status="done", imported=count)
        if not error:
            _record_import(source, count)
    except Exception as e:
        app.logger.exception("Import job %s (%s) failed", job_id, source)
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is not None:
                job.update(status="error",
                           error=f"{type(e).__name__}: {e}",
                           finished_at=time.time())


def start_import_job(source):
    """Kick off an import in the background. Returns (job_id, error_message)."""
    if source not in IMPORTERS:
        return None, f"Unknown source: {source}"

    with _JOBS_LOCK:
        _prune_jobs()
        # Don't let the same source run twice at once
        for jid, job in _JOBS.items():
            if job["source"] == source and job["status"] == "running":
                return jid, None

        job_id = secrets.token_hex(8)
        _JOBS[job_id] = {
            "id": job_id, "source": source, "status": "running",
            "imported": None, "error": None,
            "started_at": time.time(), "finished_at": None,
        }

    threading.Thread(target=_job_worker, args=(job_id, source), daemon=True).start()
    return job_id, None


def job_status(job_id):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return None
        out = dict(job)
    out["elapsed"] = int(time.time() - out["started_at"])
    out.pop("started_at", None)
    out.pop("finished_at", None)
    return out


@app.route("/api/setup/import", methods=["POST"])
def setup_import():
    """Run a data import during first-time setup.

    Locked once setup is finalised — otherwise anyone could trigger repeated
    imports and burn through metered API quotas (Google Places is billable).
    Use the authenticated /api/admin/import endpoint after setup.
    """
    if is_setup_complete():
        return jsonify({"error": "Setup already complete"}), 403
    if not SITE_CONFIG:
        return jsonify({"error": "Setup not complete"}), 400

    source = (request.get_json(silent=True) or {}).get("source", "osm")
    job_id, error = start_import_job(source)
    if error:
        return jsonify({"error": error}), 400
    return jsonify({"ok": True, "job_id": job_id, "source": source})


@app.route("/api/setup/import/status")
def setup_import_status():
    """Poll an import started during setup."""
    if is_setup_complete():
        return jsonify({"error": "Setup already complete"}), 403
    status = job_status(request.args.get("job_id", ""))
    if not status:
        return jsonify({"error": "Unknown job"}), 404
    return jsonify(status)


@app.route("/api/admin/import", methods=["POST"])
@require_admin
def admin_import():
    """Re-run an importer after setup (admin only). Runs in the background."""
    if not SITE_CONFIG:
        return jsonify({"error": "No config"}), 400

    source = (request.get_json(silent=True) or {}).get("source", "osm")
    job_id, error = start_import_job(source)
    if error:
        return jsonify({"error": error}), 400
    return jsonify({"ok": True, "job_id": job_id, "source": source})


@app.route("/api/admin/import/status")
@require_admin
def admin_import_status():
    """Poll a running import."""
    status = job_status(request.args.get("job_id", ""))
    if not status:
        return jsonify({"error": "Unknown job"}), 404
    return jsonify(status)


# Top-level OSM keys we care about. Values are classified in Python below,
# which keeps the Overpass query small enough to run reliably.
OSM_KEYS = ["amenity", "shop", "leisure", "tourism", "office", "craft", "healthcare"]

TAG_MAP = {
    "amenity": {
        "restaurant": ("food_drink", "restaurant"), "cafe": ("food_drink", "cafe"),
        "fast_food": ("food_drink", "fast_food"), "pub": ("food_drink", "pub"),
        "bar": ("food_drink", "bar"), "ice_cream": ("food_drink", "ice_cream"),
        "food_court": ("food_drink", "food_court"), "biergarten": ("food_drink", "bar"),
        "pharmacy": ("health", "pharmacy"), "dentist": ("health", "dentist"),
        "doctors": ("health", "doctors"), "clinic": ("health", "clinic"),
        "hospital": ("health", "hospital"), "veterinary": ("health", "veterinary"),
        "bank": ("services", "bank"), "post_office": ("services", "post_office"),
        "car_wash": ("services", "car_wash"), "fuel": ("services", "fuel"),
        "car_rental": ("services", "car_rental"), "driving_school": ("services", "driving_school"),
        "school": ("education", "school"), "kindergarten": ("education", "kindergarten"),
        "college": ("education", "college"), "university": ("education", "university"),
        "childcare": ("education", "childcare"),
        "library": ("community", "library"), "community_centre": ("community", "community_centre"),
        "place_of_worship": ("community", "place_of_worship"),
        "townhall": ("community", "government"), "social_facility": ("community", "social_facility"),
        "grave_yard": ("community", "grave_yard"), "recycling": ("community", "recycling"),
        "toilets": ("community", "toilets"),
        "theatre": ("leisure", "theatre"), "cinema": ("leisure", "cinema"),
        "nightclub": ("leisure", "night_club"), "arts_centre": ("leisure", "arts_centre"),
        "post_box": ("other", "post_box"), "bench": ("other", "bench"),
        "waste_basket": ("other", "waste_basket"), "waste_disposal": ("other", "waste_disposal"),
        "atm": ("services", "atm"), "telephone": ("other", "telephone"),
        "charging_station": ("transport", "charging_station"),
        "parcel_locker": ("services", "parcel_locker"),
        "drinking_water": ("other", "drinking_water"),
    },
    "shop": {
        "supermarket": ("shopping", "supermarket"), "convenience": ("shopping", "convenience"),
        "bakery": ("shopping", "bakery"), "butcher": ("shopping", "butcher"),
        "greengrocer": ("shopping", "greengrocer"), "clothes": ("shopping", "clothes"),
        "charity": ("shopping", "charity"), "newsagent": ("shopping", "newsagent"),
        "bookmaker": ("shopping", "bookmaker"), "florist": ("shopping", "florist"),
        "furniture": ("shopping", "furniture"), "hardware": ("shopping", "hardware"),
        "doityourself": ("shopping", "hardware"), "mobile_phone": ("shopping", "mobile_phone"),
        "electronics": ("shopping", "electronics"), "alcohol": ("shopping", "alcohol"),
        "pet": ("shopping", "pet"), "shoes": ("shopping", "shoes"),
        "toys": ("shopping", "toys"), "variety_store": ("shopping", "variety_store"),
        "department_store": ("shopping", "department_store"), "car": ("shopping", "car"),
        "car_parts": ("shopping", "car_parts"), "bicycle": ("shopping", "bicycle"),
        "jewelry": ("shopping", "jewelry"), "gift": ("shopping", "gift"),
        "stationery": ("shopping", "stationery"), "sports": ("shopping", "sports"),
        "hairdresser": ("services", "hairdresser"), "beauty": ("services", "beauty"),
        "optician": ("health", "optician"), "car_repair": ("services", "car_repair"),
        "tyres": ("services", "tyres"), "laundry": ("services", "laundry"),
        "dry_cleaning": ("services", "laundry"), "tattoo": ("services", "tattoo"),
        "travel_agency": ("services", "travel_agency"), "funeral_directors": ("services", "funeral_directors"),
        "estate_agent": ("services", "estate_agent"), "hifi": ("shopping", "electronics"),
    },
    "leisure": {
        "park": ("leisure", "park"), "playground": ("leisure", "playground"),
        "pitch": ("leisure", "pitch"), "garden": ("leisure", "garden"),
        "fitness_centre": ("leisure", "fitness_centre"), "sports_centre": ("leisure", "sports_centre"),
        "swimming_pool": ("leisure", "swimming_pool"), "fitness_station": ("leisure", "fitness_station"),
        "nature_reserve": ("leisure", "nature_reserve"), "golf_course": ("leisure", "golf_course"),
    },
    "tourism": {
        "hotel": ("services", "hotel"), "guest_house": ("services", "guest_house"),
        "museum": ("leisure", "museum"), "attraction": ("leisure", "attraction"),
        "information": ("leisure", "information"), "artwork": ("leisure", "artwork"),
    },
    "office": {
        "estate_agent": ("services", "estate_agent"), "insurance": ("services", "insurance"),
        "accountant": ("services", "accountant"), "lawyer": ("services", "lawyer"),
        "government": ("community", "government"), "company": ("services", "office"),
    },
    "craft": {
        "electrician": ("services", "electrician"), "plumber": ("services", "plumber"),
        "builder": ("services", "builder"), "carpenter": ("services", "carpenter"),
        "painter": ("services", "painter"), "roofer": ("services", "roofer"),
    },
    "healthcare": {
        "doctor": ("health", "doctors"), "dentist": ("health", "dentist"),
        "physiotherapist": ("health", "physiotherapist"), "pharmacy": ("health", "pharmacy"),
        "centre": ("health", "clinic"),
    },
}

# Subcategories we never want on the map
SKIP_SUBCATS = {"parking", "parking_space", "parking_entrance", "bicycle_parking"}

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]


def _classify(tags):
    for key, mapping in TAG_MAP.items():
        val = tags.get(key)
        if val and val in mapping:
            return mapping[val]
    return None


def _import_osm():
    """Import POIs from OpenStreetMap. Returns (count, error_message)."""
    import math

    bbox = SITE_CONFIG.get("overpass_bbox")
    if not bbox or len(bbox) != 4:
        center = SITE_CONFIG["center"]
        radius = SITE_CONFIG.get("radius_km", 3)
        lat_pad = radius / 111.32
        lon_pad = radius / (111.32 * math.cos(math.radians(center[0])))
        bbox = [
            round(center[0] - lat_pad, 4), round(center[1] - lon_pad, 4),
            round(center[0] + lat_pad, 4), round(center[1] + lon_pad, 4),
        ]

    bbox_str = f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"

    # One nwr statement per top-level key — small, fast, reliable.
    parts = "".join(f'nwr["{k}"]({bbox_str});' for k in OSM_KEYS)
    query = f"[out:json][timeout:90];({parts});out center tags;"

    elements, last_error = None, None
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            r = ext_requests.post(
                endpoint,
                data={"data": query},
                headers={"User-Agent": "LocalMap/1.0"},
                timeout=120,
            )
            if r.status_code != 200:
                last_error = f"Overpass returned HTTP {r.status_code}"
                continue
            elements = r.json().get("elements", [])
            break
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            continue

    if elements is None:
        print(f"Overpass import failed: {last_error}")
        return 0, f"Could not reach OpenStreetMap ({last_error})"

    print(f"Overpass returned {len(elements)} elements for bbox {bbox_str}")

    conn = sqlite3.connect(DB_PATH)
    inserted = 0

    for el in elements:
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        if lat is None or lon is None:
            continue

        tags = el.get("tags") or {}
        classified = _classify(tags)
        if not classified:
            continue
        group, subcat = classified
        if subcat in SKIP_SUBCATS:
            continue

        osm_id = str(el.get("id"))
        # Skip if we already have this OSM object
        existing = conn.execute(
            "SELECT 1 FROM places WHERE osm_id=? AND osm_type=?",
            (osm_id, el.get("type")),
        ).fetchone()
        if existing:
            continue

        addr_parts = [
            " ".join(p for p in [tags.get("addr:housenumber"), tags.get("addr:street")] if p),
            tags.get("addr:city"),
            tags.get("addr:postcode"),
        ]
        address = ", ".join(p for p in addr_parts if p) or None

        conn.execute("""
            INSERT INTO places (osm_type, osm_id, name, group_name, subcategory,
                osm_key, address, postcode, phone, website, opening_hours,
                lat, lon, source, sources, status)
            VALUES (?, ?, ?, ?, ?, 'osm', ?, ?, ?, ?, ?, ?, ?, 'osm', 'osm', 'active')
        """, (
            el.get("type"), osm_id,
            tags.get("name"), group, subcat,
            address, tags.get("addr:postcode"),
            tags.get("phone") or tags.get("contact:phone"),
            tags.get("website") or tags.get("contact:website"),
            tags.get("opening_hours"),
            lat, lon,
        ))
        inserted += 1

    conn.commit()
    conn.close()
    print(f"OSM import: inserted {inserted} places")
    return inserted, None


# ---------------------------------------------------------------------------
# Shared helpers for enrichment importers
# ---------------------------------------------------------------------------
def _haversine_m(lat1, lon1, lat2, lon2):
    import math
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _name_similarity(a, b):
    from difflib import SequenceMatcher
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _find_match(candidates, name, lat, lon, radius_m=300, threshold=0.85):
    """Find an existing place that looks like the same business."""
    best, best_score = None, 0.0
    for c in candidates:
        if c["lat"] is None or c["lon"] is None:
            continue
        if _haversine_m(lat, lon, c["lat"], c["lon"]) > radius_m:
            continue
        score = _name_similarity(name, c["name"])
        if score >= threshold and score > best_score:
            best, best_score = c, score
    return best


def _area_bounds():
    """Return (centre_lat, centre_lon, radius_km) for the configured area."""
    center = SITE_CONFIG["center"]
    return center[0], center[1], SITE_CONFIG.get("radius_km", 3)


# ---------------------------------------------------------------------------
# Food Standards Agency — hygiene ratings
# ---------------------------------------------------------------------------
FSA_BUSINESS_TYPE_MAP = {
    "Restaurant/Cafe/Canteen":              ("food_drink", "restaurant"),
    "Takeaway/sandwich shop":               ("food_drink", "fast_food"),
    "Pub/bar/nightclub":                    ("food_drink", "pub"),
    "Retailers - other":                    ("shopping", "shop"),
    "Retailers - supermarkets/hypermarkets": ("shopping", "supermarket"),
    "Hotel/bed & breakfast/guest house":    ("services", "hotel"),
    "Mobile caterer":                       ("food_drink", "fast_food"),
    "Manufacturers/packers":                ("services", "manufacturer"),
    "Distributors/Transporters":            ("services", "distributor"),
    "Hospitals/Childcare/Caring Premises":  ("health", "care"),
    "Farmers/growers":                      ("services", "farm"),
    "Importers/Exporters":                  ("services", "importer"),
    # Skipped entirely — these are catering contractors, not real venues
    "School/college/university":            None,
    "Other catering premises":              None,
}


def _import_fsa():
    """Import / enrich from the Food Standards Agency hygiene rating API."""
    lat, lon, radius_km = _area_bounds()
    # The FSA API expects MILES for maxDistanceLimit
    radius_miles = max(1, round(radius_km * 0.621371))

    establishments, last_error = None, None
    try:
        r = ext_requests.get(
            "https://api.ratings.food.gov.uk/Establishments",
            params={
                "longitude": lon,
                "latitude": lat,
                "maxDistanceLimit": radius_miles,
                "pageSize": 5000,
                "pageNumber": 1,
            },
            headers={"x-api-version": "2", "Accept": "application/json",
                     "User-Agent": "LocalMap/1.0"},
            timeout=90,
        )
        if r.status_code != 200:
            last_error = f"FSA API returned HTTP {r.status_code}"
        else:
            establishments = r.json().get("establishments", [])
    except Exception as e:
        last_error = f"{type(e).__name__}: {e}"

    if establishments is None:
        print(f"FSA import failed: {last_error}")
        return 0, f"Could not reach the Food Standards Agency ({last_error})"

    print(f"FSA returned {len(establishments)} establishments")

    conn = get_db()
    candidates = conn.execute(
        "SELECT id, name, lat, lon, sources FROM places WHERE status='active'"
    ).fetchall()

    inserted = enriched = 0

    for est in establishments:
        geo = est.get("geocode") or {}
        try:
            elat = float(geo.get("latitude"))
            elon = float(geo.get("longitude"))
        except (TypeError, ValueError):
            continue

        name = (est.get("BusinessName") or "").strip()
        if not name:
            continue

        classification = FSA_BUSINESS_TYPE_MAP.get(
            est.get("BusinessType") or "", ("food_drink", "other"))
        if classification is None:
            continue
        group, subcat = classification

        rating = est.get("RatingValue")
        rating_date = (est.get("RatingDate") or "")[:10] or None
        # Only keep numeric ratings; skip "AwaitingInspection", "Exempt" etc.
        if rating and not str(rating).isdigit():
            rating = None

        addr = ", ".join(p for p in [
            est.get("AddressLine1"), est.get("AddressLine2"),
            est.get("AddressLine3"), est.get("AddressLine4"),
            est.get("PostCode"),
        ] if p) or None

        match = _find_match(candidates, name, elat, elon)

        if match:
            sources = set((match["sources"] or "osm").split(","))
            sources.add("fsa")
            conn.execute(
                """UPDATE places SET hygiene_rating=?, hygiene_rating_date=?,
                   sources=?, address=COALESCE(address, ?), postcode=COALESCE(postcode, ?)
                   WHERE id=?""",
                (rating, rating_date, ",".join(sorted(sources)),
                 addr, est.get("PostCode"), match["id"]),
            )
            enriched += 1
        else:
            conn.execute(
                """INSERT INTO places (name, group_name, subcategory, address, postcode,
                       phone, lat, lon, source, sources, status,
                       hygiene_rating, hygiene_rating_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'fsa', 'fsa', 'active', ?, ?)""",
                (name, group, subcat, addr, est.get("PostCode"),
                 (est.get("Phone") or "").strip() or None,
                 elat, elon, rating, rating_date),
            )
            inserted += 1

    conn.commit()
    conn.close()
    print(f"FSA import: {inserted} new, {enriched} enriched")
    return inserted + enriched, None


# ---------------------------------------------------------------------------
# NaPTAN — public transport stops
# ---------------------------------------------------------------------------
# StopType codes worth mapping. Everything else is ignored.
NAPTAN_STOP_TYPES = {
    "BCT": ("transport", "bus_stop"),      # on-street bus stop
    "BCS": ("transport", "bus_stop"),      # marked bus bay
    "BCQ": ("transport", "bus_station"),   # bus station bay
    "BST": ("transport", "bus_station"),
    "RLY": ("transport", "train_station"),
    "RSE": ("transport", "train_station"),
    "MET": ("transport", "tram_stop"),     # tram / metro
    "PLT": ("transport", "tram_stop"),
    "FER": ("transport", "ferry_terminal"),
    "TXR": ("transport", "taxi_rank"),
}


def _import_naptan():
    """Import public transport stops from NaPTAN for the configured area."""
    import csv
    import io

    lat, lon, radius_km = _area_bounds()
    radius_m = radius_km * 1000

    # NaPTAN CSV export, filtered server-side by bounding box
    import math
    lat_pad = radius_km / 111.32
    lon_pad = radius_km / (111.32 * math.cos(math.radians(lat)))
    bbox = {
        "minLatitude": round(lat - lat_pad, 6),
        "maxLatitude": round(lat + lat_pad, 6),
        "minLongitude": round(lon - lon_pad, 6),
        "maxLongitude": round(lon + lon_pad, 6),
    }

    text, last_error = None, None
    try:
        r = ext_requests.get(
            "https://naptan.api.dft.gov.uk/v1/access-nodes",
            params={"dataFormat": "csv", **bbox},
            headers={"User-Agent": "LocalMap/1.0"},
            timeout=120,
        )
        if r.status_code != 200:
            last_error = f"NaPTAN API returned HTTP {r.status_code}"
        else:
            text = r.text
    except Exception as e:
        last_error = f"{type(e).__name__}: {e}"

    if text is None:
        print(f"NaPTAN import failed: {last_error}")
        return 0, f"Could not reach NaPTAN ({last_error})"

    reader = csv.DictReader(io.StringIO(text))
    conn = get_db()

    existing_ids = {
        row[0] for row in conn.execute(
            "SELECT osm_id FROM places WHERE source='naptan' AND osm_id IS NOT NULL"
        ).fetchall()
    }

    inserted = skipped = 0

    for row in reader:
        stop_type = (row.get("StopType") or "").strip().upper()
        classification = NAPTAN_STOP_TYPES.get(stop_type)
        if not classification:
            continue
        group, subcat = classification

        status = (row.get("Status") or "").strip().lower()
        if status and status != "active":
            continue

        try:
            slat = float(row.get("Latitude"))
            slon = float(row.get("Longitude"))
        except (TypeError, ValueError):
            continue

        # Enforce a circular radius rather than the bounding box
        if _haversine_m(lat, lon, slat, slon) > radius_m:
            continue

        atco = (row.get("ATCOCode") or "").strip()
        if not atco or atco in existing_ids:
            skipped += 1
            continue

        name = (row.get("CommonName") or "").strip()
        indicator = (row.get("Indicator") or "").strip()
        street = (row.get("Street") or "").strip()
        if indicator and name:
            name = f"{name} ({indicator})"
        if not name:
            name = subcat.replace("_", " ").title()

        conn.execute(
            """INSERT INTO places (osm_type, osm_id, name, group_name, subcategory,
                   address, lat, lon, source, sources, status)
               VALUES ('naptan', ?, ?, ?, ?, ?, ?, ?, 'naptan', 'naptan', 'active')""",
            (atco, name, group, subcat, street or None, slat, slon),
        )
        existing_ids.add(atco)
        inserted += 1

    conn.commit()
    conn.close()
    print(f"NaPTAN import: {inserted} stops inserted, {skipped} already present")
    return inserted, None


IMPORTERS.update({
    "osm": _import_osm,
    "fsa": _import_fsa,
    "naptan": _import_naptan,
})


# ---------------------------------------------------------------------------
# Foursquare Places
# ---------------------------------------------------------------------------
# Broad search terms — the API caps each response at 50 results, so several
# targeted queries give far better coverage than one generic sweep.
FOURSQUARE_QUERIES = [
    "restaurant", "cafe", "pub", "bar", "takeaway", "bakery",
    "supermarket", "convenience store", "clothing store", "barber",
    "hairdresser", "beauty salon", "pharmacy", "dentist", "doctor",
    "gym", "park", "school", "hotel", "bank", "car repair",
    "hardware store", "pet shop", "charity shop", "florist",
]

# Foursquare category names are free text; match on keywords.
FOURSQUARE_CATEGORY_RULES = [
    (("restaurant", "diner", "steakhouse", "pizzeria", "bistro"), ("food_drink", "restaurant")),
    (("café", "cafe", "coffee", "tea room"), ("food_drink", "cafe")),
    (("fast food", "takeaway", "take out", "fish and chips", "sandwich"), ("food_drink", "fast_food")),
    (("pub", "bar", "brewery", "nightclub", "cocktail"), ("food_drink", "pub")),
    (("bakery", "patisserie"), ("shopping", "bakery")),
    (("grocery", "supermarket"), ("shopping", "supermarket")),
    (("convenience",), ("shopping", "convenience")),
    (("pharmacy", "chemist"), ("health", "pharmacy")),
    (("dentist", "dental"), ("health", "dentist")),
    (("doctor", "physician", "medical", "clinic", "hospital"), ("health", "doctors")),
    (("optic", "eye care"), ("health", "optician")),
    (("veterinar",), ("health", "veterinary")),
    (("salon", "barber", "hair", "nail", "spa", "beauty"), ("services", "hairdresser")),
    (("bank", "atm", "credit union"), ("services", "bank")),
    (("automotive", "car repair", "mechanic", "tyre", "tire"), ("services", "car_repair")),
    (("gas station", "petrol"), ("services", "fuel")),
    (("hotel", "bed and breakfast", "hostel", "inn"), ("services", "hotel")),
    (("laundry", "dry clean"), ("services", "laundry")),
    (("school", "college", "university", "nursery", "childcare"), ("education", "school")),
    (("library",), ("community", "library")),
    (("church", "mosque", "synagogue", "temple", "spiritual"), ("community", "place_of_worship")),
    (("community cent", "town hall", "government"), ("community", "community_centre")),
    (("gym", "fitness", "yoga", "leisure cent"), ("leisure", "fitness_centre")),
    (("park", "playground", "garden", "recreation"), ("leisure", "park")),
    (("museum", "gallery", "theater", "theatre", "cinema"), ("leisure", "museum")),
    (("clothing", "boutique", "apparel", "shoe"), ("shopping", "clothes")),
    (("hardware", "diy", "home improvement"), ("shopping", "hardware")),
    (("pet",), ("shopping", "pet")),
    (("charity", "thrift"), ("shopping", "charity")),
    (("florist", "flower"), ("shopping", "florist")),
    (("book", "stationer", "newsagent"), ("shopping", "newsagent")),
    (("electronics", "mobile phone"), ("shopping", "electronics")),
    (("shop", "store", "retail", "market"), ("shopping", "shop")),
]


def _classify_foursquare(category_names):
    blob = " ".join(category_names).lower()
    for keywords, result in FOURSQUARE_CATEGORY_RULES:
        if any(k in blob for k in keywords):
            return result
    return ("other", "other")


def _import_foursquare():
    """Import places from the Foursquare Places API. Requires an API key."""
    api_key = get_secret("foursquare_api_key")
    if not api_key:
        return 0, ("No Foursquare API key set. Add one in Settings → Data sources. "
                   "Free keys: foursquare.com/developers")

    lat, lon, radius_km = _area_bounds()
    radius_m = min(int(radius_km * 1000), 100000)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Places-Api-Version": "2025-06-17",
        "Accept": "application/json",
        "User-Agent": "LocalMap/1.0",
    }

    seen, results, last_error = set(), [], None
    for query in FOURSQUARE_QUERIES:
        try:
            r = ext_requests.get(
                "https://places-api.foursquare.com/places/search",
                params={"ll": f"{lat},{lon}", "radius": radius_m,
                        "query": query, "limit": 50},
                headers=headers, timeout=30,
            )
            if r.status_code == 401:
                return 0, "Foursquare rejected the API key (401). Check it in Settings."
            if r.status_code == 429:
                last_error = "rate limited"
                break
            if r.status_code != 200:
                last_error = f"HTTP {r.status_code}"
                continue
            for place in r.json().get("results", []):
                pid = place.get("fsq_place_id") or place.get("fsq_id")
                if pid and pid not in seen:
                    seen.add(pid)
                    results.append(place)
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            continue

    if not results:
        msg = f" ({last_error})" if last_error else ""
        return 0, f"Foursquare returned no results{msg}"

    print(f"Foursquare returned {len(results)} unique places")

    conn = get_db()
    candidates = conn.execute(
        "SELECT id, name, lat, lon, sources FROM places WHERE status='active'"
    ).fetchall()

    existing_ids = {
        row[0] for row in conn.execute(
            "SELECT osm_id FROM places WHERE source='foursquare' AND osm_id IS NOT NULL"
        ).fetchall()
    }

    inserted = enriched = 0

    for place in results:
        pid = place.get("fsq_place_id") or place.get("fsq_id")
        if not pid or pid in existing_ids:
            continue

        name = (place.get("name") or "").strip()
        if not name:
            continue

        plat = place.get("latitude")
        plon = place.get("longitude")
        loc = place.get("location") or {}
        if plat is None or plon is None:
            geo = (place.get("geocodes") or {}).get("main") or {}
            plat, plon = geo.get("latitude"), geo.get("longitude")
        if plat is None or plon is None:
            continue

        cat_names = [c.get("name", "") for c in (place.get("categories") or [])]
        group, subcat = _classify_foursquare(cat_names)

        address = loc.get("formatted_address") or loc.get("address")
        postcode = loc.get("postcode")
        tel = place.get("tel")
        website = place.get("website")

        match = _find_match(candidates, name, plat, plon)
        if match:
            sources = set((match["sources"] or "osm").split(","))
            sources.add("foursquare")
            conn.execute(
                """UPDATE places SET
                       phone = COALESCE(phone, ?),
                       website = COALESCE(website, ?),
                       address = COALESCE(address, ?),
                       postcode = COALESCE(postcode, ?),
                       sources = ?
                   WHERE id = ?""",
                (tel, website, address, postcode, ",".join(sorted(sources)), match["id"]),
            )
            enriched += 1
        else:
            conn.execute(
                """INSERT INTO places (osm_type, osm_id, name, group_name, subcategory,
                       address, postcode, phone, website, lat, lon,
                       source, sources, status)
                   VALUES ('foursquare', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           'foursquare', 'foursquare', 'active')""",
                (pid, name, group, subcat, address, postcode, tel, website, plat, plon),
            )
            existing_ids.add(pid)
            inserted += 1

    conn.commit()
    conn.close()
    print(f"Foursquare import: {inserted} new, {enriched} enriched")
    return inserted + enriched, None


# ---------------------------------------------------------------------------
# Overture Maps Foundation
# ---------------------------------------------------------------------------
# Overture publishes open place data as Parquet on S3. DuckDB can query it
# remotely with a bounding-box filter, so we never download the whole release.
# Overture publishes monthly and keeps only the last two releases in the public
# buckets, so a hardcoded value goes stale fast. This is a fallback; the
# importer probes recent dates and uses the newest one that actually exists.
OVERTURE_RELEASE = "2026-08-19.0"

# Overture releases land on a Wednesday, roughly the third of each month.
# Probing a handful of recent candidates is cheaper than maintaining a pin.
def _overture_candidate_releases(limit=6):
    """Recent release identifiers, newest first."""
    from datetime import date, timedelta
    today = date.today()
    seen, out = set(), []
    # Walk back day by day and collect Wednesdays in the 15th-25th window
    for offset in range(0, 200):
        d = today - timedelta(days=offset)
        if d.weekday() == 2 and 15 <= d.day <= 25:
            tag = f"{d.isoformat()}.0"
            if tag not in seen:
                seen.add(tag)
                out.append(tag)
            if len(out) >= limit:
                break
    return out


def _overture_release_exists(con, release):
    """Cheap existence check — read a single row of metadata."""
    path = (f"s3://overturemaps-us-west-2/release/{release}"
            f"/theme=places/type=place/*.parquet")
    try:
        con.execute(f"SELECT 1 FROM read_parquet('{path}') LIMIT 1").fetchone()
        return True
    except Exception:
        return False

OVERTURE_CATEGORY_RULES = [
    (("restaurant", "diner", "steakhouse", "pizzeria"), ("food_drink", "restaurant")),
    (("cafe", "coffee", "tea_house"), ("food_drink", "cafe")),
    (("fast_food", "takeaway", "fish_and_chips", "sandwich"), ("food_drink", "fast_food")),
    (("bar", "pub", "brewery", "nightclub"), ("food_drink", "pub")),
    (("bakery", "patisserie"), ("shopping", "bakery")),
    (("supermarket", "grocery"), ("shopping", "supermarket")),
    (("convenience",), ("shopping", "convenience")),
    (("pharmacy", "drugstore"), ("health", "pharmacy")),
    (("dentist", "dental"), ("health", "dentist")),
    (("doctor", "physician", "medical", "clinic", "hospital"), ("health", "doctors")),
    (("optician", "eyewear"), ("health", "optician")),
    (("veterinar",), ("health", "veterinary")),
    (("hair", "barber", "salon", "beauty", "nail", "spa"), ("services", "hairdresser")),
    (("bank", "atm", "financial"), ("services", "bank")),
    (("automotive", "car_repair", "mechanic", "tyre"), ("services", "car_repair")),
    (("gas_station", "petrol", "fuel"), ("services", "fuel")),
    (("hotel", "lodging", "bed_and_breakfast"), ("services", "hotel")),
    (("laundry", "dry_clean"), ("services", "laundry")),
    (("school", "college", "university", "education", "childcare"), ("education", "school")),
    (("library",), ("community", "library")),
    (("church", "mosque", "synagogue", "temple", "religious"), ("community", "place_of_worship")),
    (("community", "government", "town_hall"), ("community", "community_centre")),
    (("gym", "fitness", "yoga", "sports_and_recreation"), ("leisure", "fitness_centre")),
    (("park", "playground", "garden"), ("leisure", "park")),
    (("museum", "gallery", "theatre", "cinema", "arts"), ("leisure", "museum")),
    (("clothing", "boutique", "apparel", "shoe"), ("shopping", "clothes")),
    (("hardware", "home_improvement"), ("shopping", "hardware")),
    (("pet",), ("shopping", "pet")),
    (("charity", "thrift"), ("shopping", "charity")),
    (("florist", "flower"), ("shopping", "florist")),
    (("book", "stationery", "newsagent"), ("shopping", "newsagent")),
    (("electronics", "mobile_phone"), ("shopping", "electronics")),
    (("shopping", "store", "retail", "market"), ("shopping", "shop")),
]


def _classify_overture(category):
    blob = (category or "").lower()
    for keywords, result in OVERTURE_CATEGORY_RULES:
        if any(k in blob for k in keywords):
            return result
    return ("other", "other")


def _import_overture():
    """Import places from Overture Maps open data via DuckDB."""
    try:
        import duckdb
    except ImportError:
        return 0, ("DuckDB is not installed in this container. Add 'duckdb' to "
                   "requirements.txt and rebuild to enable Overture imports.")

    import math
    lat, lon, radius_km = _area_bounds()
    lat_pad = radius_km / 111.32
    lon_pad = radius_km / (111.32 * math.cos(math.radians(lat)))
    xmin, xmax = lon - lon_pad, lon + lon_pad
    ymin, ymax = lat - lat_pad, lat + lat_pad

    configured = (SITE_CONFIG.get("overture_release") or "").strip()
    country = (SITE_CONFIG.get("overture_country") or "GB").upper()

    con = None
    try:
        con = duckdb.connect()
        # httpfs must be installed and loaded as separate statements before use
        con.execute("INSTALL httpfs")
        con.execute("LOAD httpfs")
        con.execute("SET s3_region='us-west-2'")
        con.execute("SET enable_progress_bar=false")
        # Overture files are large; these keep memory and connection count sane
        con.execute("SET memory_limit='512MB'")
        con.execute("SET threads=2")
        con.execute("SET http_keep_alive=true")
        con.execute("SET http_timeout=120000")

        # Overture keeps only the last couple of months in the public bucket,
        # so find a release that actually exists rather than trusting a pin.
        release = None
        if configured:
            if _overture_release_exists(con, configured):
                release = configured
            else:
                return 0, (f"The release '{configured}' set in Settings was not found. "
                           f"Overture only keeps the last two months available. "
                           f"Clear the field to auto-detect the newest release.")
        else:
            for candidate in _overture_candidate_releases():
                if _overture_release_exists(con, candidate):
                    release = candidate
                    break
            if not release:
                return 0, ("Could not find any recent Overture release. Check the "
                           "container has outbound internet access, or set a release "
                           "manually from docs.overturemaps.org.")

        print(f"Overture: using release {release}")
        path = (f"s3://overturemaps-us-west-2/release/{release}"
                f"/theme=places/type=place/*.parquet")

        # The `categories` column is deprecated and disappears in the September
        # 2026 release, replaced by `basic_category`. Try the new column first
        # and fall back, so both old and new releases work.
        rows = None
        last_err = None
        for category_expr in ("basic_category", "categories.primary"):
            query = f"""
                SELECT
                    names.primary          AS name,
                    {category_expr}        AS category,
                    bbox.xmin              AS lon,
                    bbox.ymin              AS lat,
                    addresses[1].freeform  AS address,
                    addresses[1].postcode  AS postcode,
                    websites[1]            AS website,
                    phones[1]              AS phone,
                    confidence             AS confidence
                FROM read_parquet('{path}', hive_partitioning=1)
                WHERE bbox.xmin BETWEEN {xmin} AND {xmax}
                  AND bbox.ymin BETWEEN {ymin} AND {ymax}
                  AND names.primary IS NOT NULL
                  AND addresses[1].country = '{country}'
            """
            try:
                rows = con.execute(query).fetchall()
                break
            except Exception as e:
                last_err = e
                if "basic_category" in str(e) or "Referenced column" in str(e):
                    continue      # older schema — try the deprecated column
                raise

        if rows is None:
            raise last_err

    except Exception as e:
        msg = str(e)
        print(f"Overture import failed: {type(e).__name__}: {msg}")
        if "Connection" in msg or "network" in msg.lower() or "resolve" in msg.lower():
            hint = "The container could not reach Overture's S3 bucket. Check outbound internet access."
        elif "HTTP" in msg or "404" in msg or "No files found" in msg:
            hint = ("The release was not found. Overture only keeps the last two "
                    "months available — clear the release field in Settings to auto-detect.")
        else:
            hint = f"{type(e).__name__}: {msg[:200]}"
        return 0, f"Overture query failed. {hint}"
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass

    print(f"Overture returned {len(rows)} places")

    conn = get_db()
    candidates = conn.execute(
        "SELECT id, name, lat, lon, sources FROM places WHERE status='active'"
    ).fetchall()

    inserted = enriched = 0

    for name, category, plon, plat, address, postcode, website, phone, confidence in rows:
        if not name or plat is None or plon is None:
            continue
        # Overture includes low-confidence records; skip the weakest
        if confidence is not None and confidence < 0.5:
            continue
        # Enforce a circular radius rather than the bounding box
        if _haversine_m(lat, lon, plat, plon) > radius_km * 1000:
            continue

        group, subcat = _classify_overture(category)

        match = _find_match(candidates, name, plat, plon)
        if match:
            sources = set((match["sources"] or "osm").split(","))
            sources.add("overture")
            conn.execute(
                """UPDATE places SET
                       phone = COALESCE(phone, ?),
                       website = COALESCE(website, ?),
                       address = COALESCE(address, ?),
                       postcode = COALESCE(postcode, ?),
                       sources = ?
                   WHERE id = ?""",
                (phone, website, address, postcode, ",".join(sorted(sources)), match["id"]),
            )
            enriched += 1
        else:
            conn.execute(
                """INSERT INTO places (name, group_name, subcategory, address, postcode,
                       phone, website, lat, lon, source, sources, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'overture', 'overture', 'active')""",
                (name, group, subcat, address, postcode, phone, website, plat, plon),
            )
            inserted += 1

    conn.commit()
    conn.close()
    print(f"Overture import: {inserted} new, {enriched} enriched")
    return inserted + enriched, None


IMPORTERS.update({
    "foursquare": _import_foursquare,
    "overture": _import_overture,
})


# ---------------------------------------------------------------------------
# Google Places API (New)
# ---------------------------------------------------------------------------
# Google bills each call at the highest SKU tier of ANY field requested.
# This mask deliberately stays inside the Pro tier: it excludes rating,
# userRatingCount, reviews and photos, which would push calls to Enterprise
# or Enterprise+Atmosphere and roughly double the per-call cost.
GOOGLE_FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.location",
    "places.primaryType",
    "places.types",
    "places.nationalPhoneNumber",
    "places.websiteUri",
    "places.regularOpeningHours.weekdayDescriptions",
    "places.businessStatus",
])

# Google place types → our groups. Checked against primaryType first, then
# the full types list, so the most specific match wins.
GOOGLE_TYPE_MAP = {
    "restaurant": ("food_drink", "restaurant"),
    "cafe": ("food_drink", "cafe"),
    "coffee_shop": ("food_drink", "cafe"),
    "meal_takeaway": ("food_drink", "fast_food"),
    "fast_food_restaurant": ("food_drink", "fast_food"),
    "bar": ("food_drink", "bar"),
    "pub": ("food_drink", "pub"),
    "bakery": ("shopping", "bakery"),
    "supermarket": ("shopping", "supermarket"),
    "grocery_store": ("shopping", "supermarket"),
    "convenience_store": ("shopping", "convenience"),
    "clothing_store": ("shopping", "clothes"),
    "shoe_store": ("shopping", "shoes"),
    "book_store": ("shopping", "newsagent"),
    "florist": ("shopping", "florist"),
    "pet_store": ("shopping", "pet"),
    "hardware_store": ("shopping", "hardware"),
    "home_improvement_store": ("shopping", "hardware"),
    "electronics_store": ("shopping", "electronics"),
    "furniture_store": ("shopping", "furniture"),
    "department_store": ("shopping", "department_store"),
    "liquor_store": ("shopping", "alcohol"),
    "store": ("shopping", "shop"),
    "pharmacy": ("health", "pharmacy"),
    "drugstore": ("health", "pharmacy"),
    "dentist": ("health", "dentist"),
    "doctor": ("health", "doctors"),
    "hospital": ("health", "hospital"),
    "physiotherapist": ("health", "physiotherapist"),
    "veterinary_care": ("health", "veterinary"),
    "optician": ("health", "optician"),
    "hair_salon": ("services", "hairdresser"),
    "barber_shop": ("services", "hairdresser"),
    "beauty_salon": ("services", "beauty"),
    "nail_salon": ("services", "beauty"),
    "spa": ("services", "beauty"),
    "bank": ("services", "bank"),
    "atm": ("services", "atm"),
    "car_repair": ("services", "car_repair"),
    "car_wash": ("services", "car_wash"),
    "car_dealer": ("shopping", "car"),
    "gas_station": ("services", "fuel"),
    "lodging": ("services", "hotel"),
    "hotel": ("services", "hotel"),
    "laundry": ("services", "laundry"),
    "post_office": ("services", "post_office"),
    "real_estate_agency": ("services", "estate_agent"),
    "insurance_agency": ("services", "insurance"),
    "lawyer": ("services", "lawyer"),
    "accounting": ("services", "accountant"),
    "travel_agency": ("services", "travel_agency"),
    "funeral_home": ("services", "funeral_directors"),
    "school": ("education", "school"),
    "primary_school": ("education", "school"),
    "secondary_school": ("education", "school"),
    "preschool": ("education", "kindergarten"),
    "university": ("education", "university"),
    "library": ("community", "library"),
    "church": ("community", "place_of_worship"),
    "mosque": ("community", "place_of_worship"),
    "synagogue": ("community", "place_of_worship"),
    "hindu_temple": ("community", "place_of_worship"),
    "place_of_worship": ("community", "place_of_worship"),
    "city_hall": ("community", "government"),
    "local_government_office": ("community", "government"),
    "community_center": ("community", "community_centre"),
    "gym": ("leisure", "fitness_centre"),
    "fitness_center": ("leisure", "fitness_centre"),
    "park": ("leisure", "park"),
    "playground": ("leisure", "playground"),
    "museum": ("leisure", "museum"),
    "art_gallery": ("leisure", "museum"),
    "movie_theater": ("leisure", "cinema"),
    "performing_arts_theater": ("leisure", "theatre"),
    "tourist_attraction": ("leisure", "attraction"),
}

# Nearby Search caps at 20 results per call, so we sweep by type group.
# Each entry is one API call. 12 calls per import run.
GOOGLE_TYPE_SWEEPS = [
    ["restaurant", "cafe", "bar", "meal_takeaway", "bakery"],
    ["supermarket", "convenience_store", "grocery_store", "liquor_store"],
    ["clothing_store", "shoe_store", "book_store", "florist", "pet_store"],
    ["hardware_store", "home_improvement_store", "electronics_store", "furniture_store"],
    ["pharmacy", "dentist", "doctor", "hospital", "veterinary_care"],
    ["hair_salon", "beauty_salon", "barber_shop", "spa"],
    ["bank", "atm", "post_office", "real_estate_agency", "insurance_agency"],
    ["car_repair", "car_wash", "gas_station", "car_dealer"],
    ["school", "primary_school", "secondary_school", "preschool", "university"],
    ["gym", "park", "playground", "museum", "movie_theater"],
    ["lodging", "travel_agency", "laundry", "funeral_home"],
    ["church", "library", "community_center", "local_government_office"],
]


def _classify_google(primary_type, types):
    if primary_type and primary_type in GOOGLE_TYPE_MAP:
        return GOOGLE_TYPE_MAP[primary_type]
    for t in (types or []):
        if t in GOOGLE_TYPE_MAP:
            return GOOGLE_TYPE_MAP[t]
    return ("other", "other")


def _format_google_hours(opening_hours):
    """Turn Google's weekday descriptions into an OSM-ish opening_hours string."""
    if not opening_hours:
        return None
    descriptions = opening_hours.get("weekdayDescriptions") or []
    if not descriptions:
        return None
    # "Monday: 9:00 AM – 5:30 PM" → "Monday: 9:00 AM – 5:30 PM"
    # Kept human-readable; the frontend already renders multi-line hours.
    return "; ".join(d.strip() for d in descriptions if d)


def _import_google():
    """Import places from the Google Places API (New). Requires an API key."""
    api_key = get_secret("google_api_key")
    if not api_key:
        return 0, ("No Google Places API key set. Add one in Settings → Data sources. "
                   "See console.cloud.google.com → Places API (New).")

    lat, lon, radius_km = _area_bounds()
    # Nearby Search caps radius at 50km
    radius_m = min(int(radius_km * 1000), 50000)

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": GOOGLE_FIELD_MASK,
    }

    seen, results, last_error, calls = set(), [], None, 0

    for type_group in GOOGLE_TYPE_SWEEPS:
        body = {
            "includedTypes": type_group,
            "maxResultCount": 20,
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lon},
                    "radius": radius_m,
                }
            },
        }
        try:
            r = ext_requests.post(
                "https://places.googleapis.com/v1/places:searchNearby",
                json=body, headers=headers, timeout=30,
            )
            calls += 1

            if r.status_code == 403:
                detail = ""
                try:
                    detail = r.json().get("error", {}).get("message", "")
                except Exception:
                    pass
                return 0, (f"Google rejected the request (403). {detail} "
                           "Check the key restrictions allow this server's IP, "
                           "and that Places API (New) is enabled.")
            if r.status_code == 429:
                last_error = "quota exceeded"
                break
            if r.status_code != 200:
                last_error = f"HTTP {r.status_code}"
                continue

            for place in r.json().get("places", []):
                pid = place.get("id")
                if pid and pid not in seen:
                    seen.add(pid)
                    results.append(place)
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            continue

    if not results:
        msg = f" ({last_error})" if last_error else ""
        return 0, f"Google Places returned no results{msg}"

    print(f"Google Places: {len(results)} unique places from {calls} API calls")

    conn = get_db()
    candidates = conn.execute(
        "SELECT id, name, lat, lon, sources FROM places WHERE status='active'"
    ).fetchall()
    existing_ids = {
        row[0] for row in conn.execute(
            "SELECT osm_id FROM places WHERE source='google' AND osm_id IS NOT NULL"
        ).fetchall()
    }

    inserted = enriched = 0

    for place in results:
        pid = place.get("id")
        if not pid or pid in existing_ids:
            continue

        # Skip permanently closed businesses
        if place.get("businessStatus") == "CLOSED_PERMANENTLY":
            continue

        name = ((place.get("displayName") or {}).get("text") or "").strip()
        if not name:
            continue

        loc = place.get("location") or {}
        plat, plon = loc.get("latitude"), loc.get("longitude")
        if plat is None or plon is None:
            continue

        group, subcat = _classify_google(place.get("primaryType"), place.get("types"))
        address = place.get("formattedAddress")
        phone = place.get("nationalPhoneNumber")
        website = place.get("websiteUri")
        hours = _format_google_hours(place.get("regularOpeningHours"))

        match = _find_match(candidates, name, plat, plon)
        if match:
            sources = set((match["sources"] or "osm").split(","))
            sources.add("google")
            conn.execute(
                """UPDATE places SET
                       phone = COALESCE(phone, ?),
                       website = COALESCE(website, ?),
                       address = COALESCE(address, ?),
                       opening_hours = COALESCE(opening_hours, ?),
                       sources = ?
                   WHERE id = ?""",
                (phone, website, address, hours,
                 ",".join(sorted(sources)), match["id"]),
            )
            enriched += 1
        else:
            conn.execute(
                """INSERT INTO places (osm_type, osm_id, name, group_name, subcategory,
                       address, phone, website, opening_hours, lat, lon,
                       source, sources, status)
                   VALUES ('google', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           'google', 'google', 'active')""",
                (pid, name, group, subcat, address, phone, website, hours, plat, plon),
            )
            existing_ids.add(pid)
            inserted += 1

    conn.commit()
    conn.close()
    print(f"Google Places import: {inserted} new, {enriched} enriched")
    return inserted + enriched, None


IMPORTERS["google"] = _import_google


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------
# Fields carried across when merging duplicates. Order matters only for
# deciding which record is "richest" and therefore kept.
MERGE_FIELDS = ["phone", "website", "email", "opening_hours", "address",
                "postcode", "hygiene_rating", "hygiene_rating_date"]

# Common suffixes stripped before comparing names, so "Greggs" and
# "Greggs Ltd" are recognised as the same business.
NAME_NOISE = [
    " ltd", " limited", " plc", " llp", " inc", " uk", " gb",
    " & co", " and co", " co", " the",
]


def _normalise_name(name):
    n = (name or "").strip().lower()
    for ch in ".,'’\"()-–—/":
        n = n.replace(ch, " ")
    n = " ".join(n.split())
    # Leading articles shift the first token and defeat bucketing
    for prefix in ("the ", "a "):
        if n.startswith(prefix):
            n = n[len(prefix):]
    for suffix in NAME_NOISE:
        if n.endswith(suffix):
            n = n[: -len(suffix)].strip()
    return n


def _richness(row):
    """How much useful data a record carries — used to pick which one to keep."""
    score = sum(1 for f in MERGE_FIELDS if row[f])
    # Prefer records confirmed by more than one source
    score += len((row["sources"] or "").split(",")) - 1
    return score


def find_duplicates(radius_m=250, threshold=0.88):
    """Find groups of active places that appear to be the same thing.

    Two records match when their normalised names are similar enough AND they
    sit within radius_m of each other. Returns a list of groups, each with the
    record to keep and the ones to merge into it.
    """
    conn = get_db()
    cols = "id, name, lat, lon, source, sources, group_name, subcategory, " + ", ".join(MERGE_FIELDS)
    rows = conn.execute(
        f"SELECT {cols} FROM places WHERE status='active' AND name IS NOT NULL AND name != ''"
    ).fetchall()
    conn.close()

    # Bucket by normalised name first so we only compare plausible pairs
    buckets = defaultdict(list)
    for r in rows:
        key = _normalise_name(r["name"])
        if key:
            buckets[key].append(r)

    # Also compare across buckets sharing a first token, to catch
    # "Tesco" vs "Tesco Express" style pairs
    groups, claimed = [], set()

    def cluster(members):
        used = set()
        for i, a in enumerate(members):
            if a["id"] in claimed or i in used:
                continue
            if a["lat"] is None or a["lon"] is None:
                continue
            cluster_rows = [a]
            for j, b in enumerate(members):
                if j <= i or j in used or b["id"] in claimed:
                    continue
                if b["lat"] is None or b["lon"] is None:
                    continue
                if _haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]) > radius_m:
                    continue
                if _name_similarity(_normalise_name(a["name"]),
                                    _normalise_name(b["name"])) < threshold:
                    continue
                cluster_rows.append(b)
                used.add(j)
            if len(cluster_rows) > 1:
                cluster_rows.sort(key=_richness, reverse=True)
                for r in cluster_rows:
                    claimed.add(r["id"])
                groups.append({
                    "keep": cluster_rows[0],
                    "merge": cluster_rows[1:],
                })

    # Exact normalised-name matches
    for members in buckets.values():
        if len(members) > 1:
            cluster(members)

    # Near matches sharing a first token
    by_token = defaultdict(list)
    for key, members in buckets.items():
        token = key.split(" ")[0]
        if len(token) >= 4:
            by_token[token].extend(members)
    for members in by_token.values():
        if len(members) > 1:
            cluster(members)

    return groups


def summarise_duplicates(groups):
    """Turn duplicate groups into something the admin UI can display."""
    out = []
    for g in groups:
        keep = g["keep"]
        out.append({
            "keep": {
                "id": keep["id"], "name": keep["name"],
                "source": keep["source"], "subcategory": keep["subcategory"],
                "address": keep["address"], "lat": keep["lat"], "lon": keep["lon"],
                "detail": sum(1 for f in MERGE_FIELDS if keep[f]),
            },
            "merge": [
                {"id": m["id"], "name": m["name"], "source": m["source"],
                 "address": m["address"], "lat": m["lat"], "lon": m["lon"],
                 "detail": sum(1 for f in MERGE_FIELDS if m[f]),
                 "distance_m": round(_haversine_m(keep["lat"], keep["lon"],
                                                  m["lat"], m["lon"]))}
                for m in g["merge"]
            ],
        })
    return out


def merge_duplicates(groups):
    """Merge each group into its keeper and mark the rest as merged."""
    conn = get_db()
    merged = 0

    for g in groups:
        keep = g["keep"]
        dupes = g["merge"]

        # Fill any gaps on the keeper from its duplicates
        for field in MERGE_FIELDS:
            if keep[field]:
                continue
            for d in dupes:
                if d[field]:
                    conn.execute(
                        f"UPDATE places SET {field} = ? WHERE id = ?",
                        (d[field], keep["id"]),
                    )
                    break

        # Combine source attribution
        sources = set()
        for r in [keep] + dupes:
            for src in (r["sources"] or r["source"] or "").split(","):
                if src.strip():
                    sources.add(src.strip())
        conn.execute("UPDATE places SET sources = ? WHERE id = ?",
                     (",".join(sorted(sources)), keep["id"]))

        # Re-point any submissions attached to the duplicates
        for d in dupes:
            conn.execute("UPDATE submissions SET place_id = ? WHERE place_id = ?",
                         (keep["id"], d["id"]))
            conn.execute("UPDATE places SET status = 'merged' WHERE id = ?", (d["id"],))
            merged += 1

    conn.commit()
    conn.close()
    return merged


@app.route("/api/admin/duplicates", methods=["GET"])
@require_admin
def admin_find_duplicates():
    """Report duplicate groups without changing anything."""
    try:
        radius = int(request.args.get("radius_m", 250))
        threshold = float(request.args.get("threshold", 0.88))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid parameters"}), 400

    radius = max(10, min(radius, 2000))
    threshold = max(0.5, min(threshold, 1.0))

    groups = find_duplicates(radius, threshold)
    total = sum(len(g["merge"]) for g in groups)
    return jsonify({
        "ok": True,
        "groups": len(groups),
        "duplicates": total,
        "detail": summarise_duplicates(groups),
    })


@app.route("/api/admin/duplicates", methods=["POST"])
@require_admin
def admin_merge_duplicates():
    """Merge duplicates.

    Detection always re-runs server-side, so the decision is based on current
    data. If the caller supplies a selection, only those keep/merge pairs are
    acted on — a stale browser tab can't cause an unintended merge, because any
    pair that no longer exists in the fresh scan is simply skipped.
    """
    data = request.get_json(silent=True) or {}
    try:
        radius = int(data.get("radius_m", 250))
        threshold = float(data.get("threshold", 0.88))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid parameters"}), 400

    radius = max(10, min(radius, 2000))
    threshold = max(0.5, min(threshold, 1.0))

    groups = find_duplicates(radius, threshold)

    # Optional selection: [{"keep_id": 12, "merge_ids": [34, 56]}, ...]
    selection = data.get("selection")
    skipped = 0
    if selection is not None:
        if not isinstance(selection, list):
            return jsonify({"error": "selection must be a list"}), 400

        wanted = {}
        for item in selection:
            if not isinstance(item, dict):
                continue
            try:
                keep_id = int(item.get("keep_id"))
                merge_ids = {int(m) for m in (item.get("merge_ids") or [])}
            except (TypeError, ValueError):
                return jsonify({"error": "Invalid selection"}), 400
            if merge_ids:
                wanted[keep_id] = merge_ids

        filtered = []
        for g in groups:
            keep_id = g["keep"]["id"]
            if keep_id not in wanted:
                skipped += len(g["merge"])
                continue
            chosen = [m for m in g["merge"] if m["id"] in wanted[keep_id]]
            skipped += len(g["merge"]) - len(chosen)
            if chosen:
                filtered.append({"keep": g["keep"], "merge": chosen})
        groups = filtered

    detail = summarise_duplicates(groups)
    merged = merge_duplicates(groups)

    print(f"Deduplication: merged {merged} duplicates across {len(groups)} groups"
          + (f", skipped {skipped} not selected" if skipped else ""))
    return jsonify({
        "ok": True,
        "groups": len(groups),
        "merged": merged,
        "skipped": skipped,
        "detail": detail[:100],
    })


# ---------------------------------------------------------------------------
# GeoJSON API
# ---------------------------------------------------------------------------
GEOJSON_FIELDS = [
    "id", "name", "group_name", "subcategory", "osm_key",
    "address", "postcode", "phone", "website", "email",
    "opening_hours", "status", "hygiene_rating", "hygiene_rating_date", "sources",
]

@app.route("/api/places.geojson")
def places_geojson():
    conn = get_db()
    cols = ", ".join(GEOJSON_FIELDS + ["lat", "lon"])
    rows = conn.execute(f"SELECT {cols} FROM places WHERE status='active'").fetchall()
    conn.close()

    features = []
    for r in rows:
        props = {f: r[f] for f in GEOJSON_FIELDS}
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
            "properties": props,
        })

    resp = jsonify({"type": "FeatureCollection", "features": features})
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


# ---------------------------------------------------------------------------
# Reviews & Events
# ---------------------------------------------------------------------------
@app.route("/api/reviews/<int:place_id>")
def get_reviews(place_id):
    conn = get_db()
    rows = conn.execute("""
        SELECT payload, created_at FROM submissions
        WHERE type='review' AND place_id=? AND status='approved'
        ORDER BY created_at DESC LIMIT 20
    """, (place_id,)).fetchall()
    conn.close()
    reviews = []
    for r in rows:
        p = json.loads(r["payload"])
        reviews.append({
            "stars": p.get("stars", 0), "comment": p.get("comment", ""),
            "name": p.get("name", "Anonymous"), "date": r["created_at"][:10],
        })
    return jsonify(reviews)


@app.route("/api/events")
def get_events():
    conn = get_db()
    rows = conn.execute("""
        SELECT payload, created_at FROM submissions
        WHERE type='event' AND status='approved'
        ORDER BY created_at DESC LIMIT 50
    """).fetchall()
    conn.close()
    TYPE_LABELS = {
        "carboot": "Carboot / Market", "charity": "Charity",
        "community": "Community", "music": "Music / Live",
        "sport": "Sport", "other": "Other",
    }
    events = []
    for r in rows:
        p = json.loads(r["payload"])
        events.append({
            "title": p.get("title", ""),
            "type_label": TYPE_LABELS.get(p.get("type", ""), p.get("type", "")),
            "date": p.get("date", ""), "time": p.get("time", ""),
            "location": p.get("location", ""),
            "lat": p.get("lat"), "lon": p.get("lon"),
            "desc": p.get("desc", ""),
        })
    return jsonify(events)


# ---------------------------------------------------------------------------
# Submissions
# ---------------------------------------------------------------------------
@app.route("/api/submit", methods=["POST"])
def submit():
    if check_rate_limit():
        return jsonify({"error": "Too many submissions. Try again later."}), 429

    data = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({"error": "JSON body required"}), 400
    if data.get("website2"):
        return jsonify({"ok": True}), 201  # honeypot

    sub_type = data.get("type")
    if sub_type not in ("edit", "review", "closed", "event", "new_business"):
        return jsonify({"error": "Invalid type"}), 400

    payload = data.get("payload")
    if not payload or not isinstance(payload, dict):
        return jsonify({"error": "Payload must be an object"}), 400

    # Cap individual field lengths so one submission can't bloat the database
    MAX_FIELD_LEN = 2000
    cleaned = {}
    for key, value in payload.items():
        if not isinstance(key, str) or len(key) > 64:
            continue
        if isinstance(value, str):
            cleaned[key] = value[:MAX_FIELD_LEN]
        elif isinstance(value, (int, float, bool)) or value is None:
            cleaned[key] = value
        # Anything else (nested objects, arrays) is dropped
    payload = cleaned

    if not payload:
        return jsonify({"error": "Payload required"}), 400

    # Star ratings must be 1-5
    if sub_type == "review":
        try:
            stars = int(payload.get("stars", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid rating"}), 400
        if not 1 <= stars <= 5:
            return jsonify({"error": "Rating must be between 1 and 5"}), 400
        payload["stars"] = stars

    # Coordinates, where supplied, must be plausible and inside the served area
    if sub_type == "event":
        try:
            plat = float(payload.get("lat"))
            plon = float(payload.get("lon"))
        except (TypeError, ValueError):
            return jsonify({"error": "A location is required"}), 400
        if not (-90 <= plat <= 90 and -180 <= plon <= 180):
            return jsonify({"error": "Invalid coordinates"}), 400
        if SITE_CONFIG and SITE_CONFIG.get("center"):
            clat, clon = SITE_CONFIG["center"]
            limit_m = (SITE_CONFIG.get("radius_km", 3) + 2) * 1000
            if _haversine_m(clat, clon, plat, plon) > limit_m:
                return jsonify({"error": "That location is outside this map's area"}), 400
        payload["lat"], payload["lon"] = plat, plon

    # place_id, if given, must reference a real place
    place_id = data.get("place_id")
    conn = get_db()
    place_name = None
    if place_id is not None:
        try:
            place_id = int(place_id)
        except (TypeError, ValueError):
            conn.close()
            return jsonify({"error": "Invalid place"}), 400
        row = conn.execute("SELECT name FROM places WHERE id=?", (place_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Unknown place"}), 404
        place_name = row["name"]

    conn.execute("""
        INSERT INTO submissions (type, place_id, payload, status, created_at)
        VALUES (?, ?, ?, 'pending', ?)
    """, (sub_type, place_id, json.dumps(payload), now_iso()))
    conn.commit()
    conn.close()

    notify_admin(sub_type, payload, place_name)
    return jsonify({"ok": True, "message": "Submission received"}), 201


# ---------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------
@app.route("/api/admin/stats")
@require_admin
def admin_stats():
    conn = get_db()
    counts = {}
    for status in ("pending", "approved", "rejected"):
        row = conn.execute("SELECT COUNT(*) FROM submissions WHERE status=?", (status,)).fetchone()
        counts[status] = row[0]
    conn.close()
    return jsonify(counts)


@app.route("/api/admin/pending")
@require_admin
def admin_pending():
    status = request.args.get("status", "pending")
    conn = get_db()
    rows = conn.execute("""
        SELECT s.*, p.name AS place_name
        FROM submissions s LEFT JOIN places p ON s.place_id = p.id
        WHERE s.status = ? ORDER BY s.created_at DESC LIMIT 100
    """, (status,)).fetchall()
    conn.close()
    items = []
    for r in rows:
        items.append({
            "id": r["id"], "type": r["type"], "place_id": r["place_id"],
            "place_name": r["place_name"], "payload": json.loads(r["payload"]),
            "status": r["status"], "created_at": r["created_at"],
            "reviewed_at": r["reviewed_at"],
        })
    return jsonify(items)


@app.route("/api/admin/<int:sub_id>/approve", methods=["POST"])
@require_admin
def approve(sub_id):
    conn = get_db()
    sub = conn.execute("SELECT * FROM submissions WHERE id=?", (sub_id,)).fetchone()
    if not sub:
        conn.close()
        abort(404)

    conn.execute("UPDATE submissions SET status='approved', reviewed_at=? WHERE id=?",
                 (now_iso(), sub_id))

    payload = json.loads(sub["payload"])
    sub_type = sub["type"]

    if sub_type == "edit" and sub["place_id"]:
        ALLOWED = {"name": "name", "subcategory": "subcategory", "opening_hours": "opening_hours",
                    "phone": "phone", "email": "email", "website": "website", "address": "address"}
        col = ALLOWED.get(payload.get("field"))
        if col and payload.get("value"):
            conn.execute(f"UPDATE places SET {col} = ? WHERE id = ?",
                         (payload["value"], sub["place_id"]))

    elif sub_type == "closed" and sub["place_id"]:
        conn.execute("UPDATE places SET status='closed' WHERE id=?", (sub["place_id"],))

    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/admin/<int:sub_id>/reject", methods=["POST"])
@require_admin
def reject(sub_id):
    conn = get_db()
    conn.execute("UPDATE submissions SET status='rejected', reviewed_at=? WHERE id=?",
                 (now_iso(), sub_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/admin/<int:sub_id>/delete", methods=["POST"])
@require_admin
def delete_submission(sub_id):
    conn = get_db()
    sub = conn.execute("SELECT * FROM submissions WHERE id=?", (sub_id,)).fetchone()
    if sub and sub["type"] == "closed" and sub["place_id"]:
        conn.execute("UPDATE places SET status='active' WHERE id=?", (sub["place_id"],))
    conn.execute("DELETE FROM submissions WHERE id=?", (sub_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/admin/config", methods=["GET"])
@require_admin
def admin_get_config():
    """Return current config for editing. API keys are masked, never sent in full."""
    cfg = dict(SITE_CONFIG or {})
    for field in SECRET_FIELDS + PUBLIC_KEY_FIELDS:
        cfg.pop(field, None)
        cfg[field + "_masked"] = mask_secret(get_secret(field))
        cfg[field + "_set"] = bool(get_secret(field))
    return jsonify(cfg)


@app.route("/api/admin/config", methods=["POST"])
@require_admin
def admin_update_config():
    """Update config fields. Merges with existing config."""
    global SITE_CONFIG
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    if not SITE_CONFIG:
        return jsonify({"error": "No config to update"}), 400

    allowed_fields = {"site_name", "area_name", "tagline", "center", "default_zoom",
                      "min_zoom", "radius_km", "boundary_polygon", "overpass_bbox",
                      "postcode_prefix", "theme_color", "accent_color", "font_family",
                      "sidebar_style", "default_layer", "overture_release", "theme",
                      "tiles", "logo", "quick_links", "social", "overture_country",
                      "theme_tokens"}
    for key in data:
        if key in allowed_fields:
            SITE_CONFIG[key] = data[key]

    # Regenerate the boundary if the centre or radius changed
    if "center" in data or "radius_km" in data:
        import math
        lat, lon = SITE_CONFIG["center"]
        radius = SITE_CONFIG.get("radius_km", 3)
        points = []
        for i in range(16):
            angle = (2 * math.pi * i) / 16
            dlat = (radius / 111.32) * math.cos(angle)
            dlon = (radius / (111.32 * math.cos(math.radians(lat)))) * math.sin(angle)
            points.append([round(lat + dlat, 6), round(lon + dlon, 6)])
        points.append(points[0])
        SITE_CONFIG["boundary_polygon"] = points
        lat_pad = radius / 111.32
        lon_pad = radius / (111.32 * math.cos(math.radians(lat)))
        SITE_CONFIG["overpass_bbox"] = [
            round(lat - lat_pad, 4), round(lon - lon_pad, 4),
            round(lat + lat_pad, 4), round(lon + lon_pad, 4),
        ]

    # API keys are stored separately and only overwritten when a real new value
    # is submitted. The UI sends back a masked placeholder for unchanged keys,
    # or an explicit empty string to clear one.
    secrets_changed = False
    for field in SECRET_FIELDS + PUBLIC_KEY_FIELDS:
        if field not in data:
            continue
        submitted = (data.get(field) or "").strip()
        current = get_secret(field)

        if submitted == "":
            if current:
                SECRETS.pop(field, None)
                secrets_changed = True
        elif "•" in submitted or submitted == mask_secret(current):
            continue           # unchanged placeholder — leave the stored key alone
        elif submitted != current:
            SECRETS[field] = submitted
            secrets_changed = True

    if secrets_changed:
        save_secrets(SECRETS)

    save_config(SITE_CONFIG)
    return jsonify({"ok": True, "message": "Config updated"})


ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif"}


@app.route("/api/admin/upload-image", methods=["POST"])
@require_admin
def admin_upload_image():
    """Upload a logo or quick-link icon from the admin panel."""
    if "image" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    f = request.files["image"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_IMAGE_EXT:
        return jsonify({"error": f"File type {ext} not allowed"}), 400

    if request.form.get("as_logo"):
        filename = "logo" + ext
    else:
        filename = f"icon-{secrets.token_hex(6)}{ext}"

    filename = secure_filename(filename)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    f.save(os.path.join(UPLOAD_DIR, filename))
    return jsonify({"ok": True, "filename": "/uploads/" + filename})


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    site_name = (SITE_CONFIG or {}).get("site_name", "Local Map")
    print(f"{site_name} — Local Map v{__version__}")
    print(f"  {'✓' if is_setup_complete() else '○'} Setup: {'complete' if is_setup_complete() else 'PENDING (wizard will run)'}")

    # A weak admin token is not a warning — it's an open door to the admin API.
    if ADMIN_TOKEN == "changeme" or len(ADMIN_TOKEN) < 20:
        print("\n  ✗  REFUSING TO START: ADMIN_TOKEN is missing or too short.")
        print("     The admin API protects submissions, settings and API keys.")
        print("     Generate one with:")
        print('       python3 -c "import secrets; print(secrets.token_urlsafe(32))"')
        print("     then set ADMIN_TOKEN in your .env file.\n")
        sys.exit(1)
    print(f"  ✓  ADMIN_TOKEN set ({len(ADMIN_TOKEN)} chars)")

    if SMTP_USER and ADMIN_EMAIL:
        print(f"  ✓  Email → {ADMIN_EMAIL}")
    else:
        print("  ○  Email disabled")

    if PUSHOVER_TOKEN and PUSHOVER_USER:
        print("  ✓  Pushover enabled")
    else:
        print("  ○  Pushover disabled")

    print(f"  ✓  Rate limit: {RATE_LIMIT}/hour/IP")
    print(f"  ✓  Admin path: /{ADMIN_PATH}")
    print(f"  ✓  DB: {DB_PATH}")
    print(f"  ✓  Secrets: {SECRETS_PATH} ({len(SECRETS)} stored)")
    print()

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8001

    # Prefer a production WSGI server. Flask's built-in server is single
    # threaded and explicitly not intended for production use.
    try:
        from waitress import serve
        print(f"Starting on http://0.0.0.0:{port} (waitress)")
        serve(app, host="0.0.0.0", port=port, threads=8, ident=None)
    except ImportError:
        print(f"Starting on http://0.0.0.0:{port} (Flask dev server — install "
              f"waitress for production)")
        app.run(host="0.0.0.0", port=port, debug=False)
