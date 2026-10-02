from collections import deque
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import List, Optional
import contextvars
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
import urllib.request

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Path dihitung dari lokasi file ini, bukan dari folder tempat uvicorn dijalankan
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "gis_network.db"
STATIC_DIR = BASE_DIR / "static"

ASSET_STATUSES = {"Active", "Maintenance", "Cut/Broken"}
INCIDENT_STATUSES = {"Open", "In Progress", "Temporary Fix", "Resolved"}
# Hanya status ini yang menandai aset terdampak sebagai Cut/Broken.
# 'Temporary Fix' = layanan sudah pulih sementara (aset dipulihkan) tetapi tiket TETAP terbuka
# sampai ada perbaikan permanen.
ACTIVE_IMPACT_STATUSES = {"Open", "In Progress"}
CABLE_INCIDENT_TYPES = {"FO Cut", "Cable Sagging", "Fiber Degradation"}
CABLE_SNAP_M = 100.0   # titik insiden dianggap berada di kabel bila jaraknya <= ini
NODE_SNAP_M = 30.0     # idem untuk node/device
SAME_POINT_M = 3.0     # closure yang sudah ada dalam radius ini dipakai ulang, tidak dobel
CABLE_TYPES = {"Backbone", "Feeder", "Distribution", "Drop"}
INSTALLATIONS = {"Udara", "Tanah"}   # cara pemasangan kabel (aerial / underground)
NODE_TYPES_NO_PORT = {"TIANG", "HH"}  # aset pasif tanpa port: tiang & handhole


# --- DATABASE ---
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db():
    """Satu koneksi per request: commit jika sukses, rollback jika error, selalu ditutup."""
    conn = get_db_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 1. Tabel Nodes
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, type TEXT, status TEXT,
            latitude REAL, longitude REAL,
            cluster TEXT, area TEXT, city TEXT, capacity TEXT, spec_data TEXT,
            parent_node_id INTEGER,       -- Upstream Node (misal OLT / ODC)
            upstream_cable_id INTEGER     -- Kabel pemasok sinyal utama
        )
    ''')

    # 2. Tabel Cables
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS cables (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, type TEXT, status TEXT,
            geojson_geometry TEXT, cluster TEXT, area TEXT, city TEXT, capacity TEXT, core_data TEXT,
            parent_cable_id INTEGER,     -- ID Kabel Utama / Parent
            from_node_id INTEGER,        -- Node Asal (Upstream)
            to_node_id INTEGER           -- Node Tujuan (Downstream)
        )
    ''')

    # 3. Tabel Core Connections
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS core_connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_asset_type TEXT, from_asset_id INTEGER, from_port_core TEXT,
            to_asset_type TEXT, to_asset_id INTEGER, to_port_core TEXT,
            via_cable_id INTEGER,   -- kabel media yang dilewati sambungan ini
            via_core TEXT,          -- core MILIK kabel media tsb yang terpakai (mis. 'Tube 1 - Core 3')
            status TEXT, notes TEXT
        )
    ''')

    # 4. Tabel Incidents
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_number TEXT UNIQUE, title TEXT, severity TEXT, incident_type TEXT,
            status TEXT DEFAULT 'Open', description TEXT,
            latitude REAL, longitude REAL, cluster TEXT, area TEXT, city TEXT,
            linked_cable_id INTEGER, linked_node_id INTEGER,
            reported_at DATETIME DEFAULT CURRENT_TIMESTAMP, resolved_at DATETIME
        )
    ''')

    # 4b. Riwayat kejadian per tiket (audit trail): dibuat, dampak, perbaikan, catatan lapangan, dst.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS incident_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            message TEXT,
            actor TEXT,
            data TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_events_incident ON incident_events(incident_id)")

    # 4c. Perbaikan lapangan (sementara / permanen) yang mengubah topologi peta
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS incident_repairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id INTEGER NOT NULL,
            kind TEXT,            -- TEMPORARY / PERMANENT
            action TEXT,          -- ADD_CLOSURE / EXTRA_JOINT / OTHER
            node_id INTEGER,      -- closure/joint yang dibuat atau dipakai
            cable_a_id INTEGER, cable_b_id INTEGER,
            position_m REAL,
            technician TEXT, notes TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_repairs_incident ON incident_repairs(incident_id)")

    # 5. Tabel Incident Impacts: mencatat aset mana yang di-set 'Cut/Broken' oleh incident mana,
    #    beserta status sebelumnya, supaya pemulihan hanya menyentuh aset milik incident tersebut.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS incident_impacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id INTEGER NOT NULL,
            asset_type TEXT NOT NULL,     -- 'NODE' / 'CABLE'
            asset_id INTEGER NOT NULL,
            prev_status TEXT
        )
    ''')
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_impacts_incident ON incident_impacts(incident_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_impacts_asset ON incident_impacts(asset_type, asset_id)"
    )

    # 6. Pengguna, sesi login, dan riwayat perubahan (audit)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            full_name TEXT,
            role TEXT NOT NULL DEFAULT 'viewer',
            password_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            must_change_password INTEGER NOT NULL DEFAULT 0,
            failed_attempts INTEGER NOT NULL DEFAULT 0,
            locked_until DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_login_at DATETIME
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            expires_at DATETIME NOT NULL,
            last_seen_at DATETIME,
            ip TEXT
        )
    ''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts DATETIME DEFAULT CURRENT_TIMESTAMP,
            user_id INTEGER, username TEXT, role TEXT,
            action TEXT NOT NULL,          -- CREATE / UPDATE / STATUS / DELETE / RESTORE / CONNECT / DISCONNECT / ...
            entity_type TEXT,              -- NODE / CABLE / CONNECTION / INCIDENT / USER
            entity_id INTEGER, entity_name TEXT,
            summary TEXT,
            changes TEXT,                  -- JSON {field: [lama, baru]}
            snapshot TEXT,                 -- JSON cuplikan (dipakai untuk pemulihan)
            ip TEXT,
            restored_at DATETIME
        )
    ''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_type, entity_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts)")

    # --- AUTO MIGRATION UNTUK DB EXISTING ---
    def add_column_if_missing(table, column, col_type):
        cursor.execute(f"PRAGMA table_info({table})")
        cols = [r[1] for r in cursor.fetchall()]
        if column not in cols:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            print(f"[MIGRATION] Added {column} to {table}")

    add_column_if_missing("cables", "installation", "TEXT")   # Udara / Tanah; NULL = belum diisi (data lama)
    add_column_if_missing("cables", "parent_cable_id", "INTEGER")
    add_column_if_missing("cables", "from_node_id", "INTEGER")
    add_column_if_missing("cables", "to_node_id", "INTEGER")
    add_column_if_missing("nodes", "parent_node_id", "INTEGER")
    add_column_if_missing("nodes", "upstream_cable_id", "INTEGER")
    add_column_if_missing("incidents", "linked_cable_id", "INTEGER")
    add_column_if_missing("incidents", "linked_node_id", "INTEGER")
    add_column_if_missing("incidents", "resolved_at", "DATETIME")
    add_column_if_missing("core_connections", "via_core", "TEXT")
    add_column_if_missing("incidents", "cable_position_m", "REAL")   # jarak titik cut dari ujung asal kabel
    add_column_if_missing("incidents", "cable_length_m", "REAL")
    add_column_if_missing("incidents", "affected_cores", "TEXT")     # JSON; kosong = semua core
    add_column_if_missing("incidents", "source", "TEXT DEFAULT 'MANUAL'")  # MANUAL / AUTO
    add_column_if_missing("incidents", "resolution", "TEXT")
    add_column_if_missing("incidents", "reporter", "TEXT")
    # Ujung hulu/hilir kabel SAAT gangguan terjadi (kabel bisa dipecah oleh perbaikan setelahnya)
    add_column_if_missing("incidents", "cable_from_node_id", "INTEGER")
    add_column_if_missing("incidents", "cable_to_node_id", "INTEGER")
    add_column_if_missing("incident_repairs", "prev_status", "TEXT")   # status tiket sebelum perbaikan (untuk batal)
    add_column_if_missing("incident_repairs", "undone_at", "DATETIME")
    add_column_if_missing("incident_repairs", "undone_by", "TEXT")
    add_column_if_missing("incident_repairs", "undo_reason", "TEXT")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_conn_via_cable ON core_connections(via_cable_id)"
    )

    # Data lama: form dulu menyimpan tipe "Dropcore", sedangkan peta mengenali "Drop"
    cursor.execute("UPDATE cables SET type = 'Drop' WHERE type = 'Dropcore'")

    # Bersihkan sambungan core yatim (aset/kabelnya sudah tidak ada)
    cursor.execute('''
        DELETE FROM core_connections
        WHERE (from_asset_type = 'NODE'  AND from_asset_id NOT IN (SELECT id FROM nodes))
           OR (from_asset_type = 'CABLE' AND from_asset_id NOT IN (SELECT id FROM cables))
           OR (to_asset_type   = 'NODE'  AND to_asset_id   NOT IN (SELECT id FROM nodes))
           OR (to_asset_type   = 'CABLE' AND to_asset_id   NOT IN (SELECT id FROM cables))
    ''')
    if cursor.rowcount:
        print(f"[CLEANUP] {cursor.rowcount} sambungan core yatim dihapus")
    cursor.execute(
        "UPDATE core_connections SET via_cable_id = NULL, via_core = NULL "
        "WHERE via_cable_id IS NOT NULL AND via_cable_id NOT IN (SELECT id FROM cables)"
    )

    _bootstrap_admin(cursor)

    conn.commit()
    conn.close()
    print("[INFO] Database topologi & migrasi berhasil diperbarui.")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="ISP WebGIS Prototype API", lifespan=lifespan)

# Allow CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(sqlite3.IntegrityError)
async def integrity_error_handler(_request: Request, exc: sqlite3.IntegrityError):
    return JSONResponse(status_code=409, content={"detail": f"Data bentrok: {exc}"})


@app.exception_handler(Exception)
async def unhandled_error_handler(_request: Request, exc: Exception):
    print(f"[ERROR] {type(exc).__name__}: {exc}")
    return JSONResponse(status_code=500, content={"detail": str(exc)})


# =====================================================================================
# AUTENTIKASI, PERAN, DAN RIWAYAT PERUBAHAN (AUDIT)
# =====================================================================================
SESSION_COOKIE = "netgis_session"
SESSION_HOURS = 12
SESSION_TOUCH_MINUTES = 5
MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 10
PBKDF2_ITERATIONS = 240_000
MIN_PASSWORD_LEN = 8

ROLES = {"admin": "Admin", "noc": "NOC", "teknisi": "Teknisi Lapangan", "viewer": "Viewer / Sales"}
ROLE_PERMS = {
    "viewer": {"view"},
    "teknisi": {"view", "incident.write", "status.write"},
    "noc": {"view", "incident.write", "status.write", "asset.write", "connection.delete",
            "incident.delete", "repair.undo", "audit.view", "audit.restore"},
}
ROLE_PERMS["admin"] = set().union(*ROLE_PERMS.values()) | {"asset.delete", "user.manage"}

# Urutan penting: yang pertama cocok dipakai. perm None = publik, "auth" = cukup sudah login.
ROUTE_PERMS = [
    ("POST", r"^/api/auth/(login|logout)$", None),
    ("GET", r"^/api/auth/me$", None),
    ("POST", r"^/api/auth/password$", "auth"),
    (None, r"^/api/users(/.*)?$", "user.manage"),
    ("GET", r"^/api/audit$", "audit.view"),
    ("POST", r"^/api/audit/\d+/restore$", "audit.restore"),
    ("POST", r"^/api/incidents/\d+/repairs/\d+/undo$", "repair.undo"),
    ("POST", r"^/api/incidents/(locate|analyze-impact)$", "incident.write"),
    ("POST", r"^/api/incidents(/\d+/(events|repairs))?$", "incident.write"),
    ("PUT", r"^/api/incidents/\d+/status$", "incident.write"),
    ("DELETE", r"^/api/incidents/\d+$", "incident.delete"),
    ("PUT", r"^/api/(nodes|cables)/\d+/status$", "status.write"),
    ("POST", r"^/api/(nodes|cables|connections)$", "asset.write"),
    ("PUT", r"^/api/(nodes|cables)/\d+$", "asset.write"),
    ("DELETE", r"^/api/connections/\d+$", "connection.delete"),
    ("DELETE", r"^/api/(nodes|cables)/\d+$", "asset.delete"),
    ("GET", r"^/api/", "view"),
]

CURRENT_USER = contextvars.ContextVar("netgis_current_user", default=None)
CURRENT_TOKEN = contextvars.ContextVar("netgis_current_token", default=None)
CURRENT_IP = contextvars.ContextVar("netgis_current_ip", default=None)


def _now_str(minutes: float = 0) -> str:
    """Waktu UTC format SQLite ('YYYY-MM-DD HH:MM:SS'), sama dengan CURRENT_TIMESTAMP."""
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def _hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def _verify_password(password: str, stored: Optional[str]) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


_DUMMY_HASH = None


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _public_user(row) -> dict:
    return {
        "id": row["id"], "username": row["username"], "full_name": row["full_name"] or row["username"],
        "role": row["role"], "role_label": ROLES.get(row["role"], row["role"]),
        "must_change_password": bool(row["must_change_password"]),
        "permissions": sorted(ROLE_PERMS.get(row["role"], set())),
    }


def _current_username(default=None):
    u = CURRENT_USER.get()
    return u["username"] if u else default


def _user_from_token(token):
    """Sesi valid -> data user (dengan perpanjangan sesi berkala); selain itu None."""
    if not token:
        return None
    with db() as conn:
        row = conn.execute(
            """SELECT u.*, s.expires_at AS s_expires, s.last_seen_at AS s_seen
               FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?""",
            (_hash_token(token),)).fetchone()
        if not row or not row["active"]:
            return None
        if row["s_expires"] <= _now_str():
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))
            return None
        if (row["s_seen"] or "") <= _now_str(-SESSION_TOUCH_MINUTES):
            conn.execute("UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE token_hash = ?",
                         (_now_str(), _now_str(SESSION_HOURS * 60), _hash_token(token)))
        return _public_user(row)


def required_permission(method: str, path: str):
    """Izin yang dibutuhkan sebuah request. Aksi tulis yang tidak dikenal hanya untuk admin (tolak-secara-default)."""
    for m, pattern, perm in ROUTE_PERMS:
        if (m is None or m == method.upper()) and re.match(pattern, path):
            return perm
    return "view" if method.upper() == "GET" else "user.manage"


def authorize(method: str, path: str, user):
    """None bila boleh; selain itu (status_http, detail, code)."""
    if not path.startswith("/api/"):
        return None
    perm = required_permission(method, path)
    if perm is None:
        return None
    if user is None:
        return (401, "Silakan login terlebih dahulu", "login_required")
    if user["must_change_password"] and perm != "auth":
        return (403, "Anda harus mengganti password terlebih dahulu", "password_change_required")
    if perm == "auth":
        return None
    if perm not in ROLE_PERMS.get(user["role"], set()):
        return (403, f"Peran {ROLES.get(user['role'], user['role'])} tidak diizinkan untuk aksi ini ({perm})",
                "forbidden")
    return None


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/"):
        return await call_next(request)          # halaman & aset statis tetap publik (login ada di halaman)
    token = request.cookies.get(SESSION_COOKIE)
    user = _user_from_token(token)
    denied = authorize(request.method, path, user)
    if denied:
        return JSONResponse(status_code=denied[0], content={"detail": denied[1], "code": denied[2]})
    t1, t2, t3 = CURRENT_USER.set(user), CURRENT_TOKEN.set(token), CURRENT_IP.set(
        request.client.host if request.client else None)
    try:
        return await call_next(request)
    finally:
        CURRENT_USER.reset(t1)
        CURRENT_TOKEN.reset(t2)
        CURRENT_IP.reset(t3)


# --- AUDIT LOG ---
FIELD_LABELS = {
    "name": "nama", "type": "tipe", "status": "status", "latitude": "latitude", "longitude": "longitude",
    "cluster": "cluster", "area": "area", "city": "kota", "capacity": "kapasitas", "spec_data": "spesifikasi",
    "parent_node_id": "parent node", "upstream_cable_id": "kabel pemasok", "parent_cable_id": "kabel induk",
    "from_node_id": "node asal", "to_node_id": "node tujuan", "core_data": "data core",
    "installation": "pemasangan",
}
_REF_TABLE = {"parent_node_id": "nodes", "from_node_id": "nodes", "to_node_id": "nodes",
              "upstream_cable_id": "cables", "parent_cable_id": "cables"}


def _audit(cursor, action, entity_type, entity_id=None, entity_name=None, summary="", changes=None,
           snapshot=None, username=None):
    """Satu baris riwayat perubahan, ditulis dalam transaksi yang sama dengan perubahannya."""
    u = CURRENT_USER.get()
    cursor.execute(
        """INSERT INTO audit_log (user_id, username, role, action, entity_type, entity_id, entity_name,
                                  summary, changes, snapshot, ip)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (u["id"] if u else None, (u["username"] if u else None) or username or "system",
         u["role"] if u else None, action, entity_type, entity_id, entity_name, summary,
         json.dumps(changes, ensure_ascii=False) if changes else None,
         json.dumps(snapshot, ensure_ascii=False, default=str) if snapshot is not None else None,
         CURRENT_IP.get()))
    return cursor.lastrowid


def _ref_name(cursor, field, value):
    table = _REF_TABLE.get(field)
    if table and value is not None:
        r = cursor.execute(f"SELECT name FROM {table} WHERE id = ?", (value,)).fetchone()
        return r["name"] if r else f"#{value}"
    return value


def _diff_rows(cursor, old: dict, new: dict, fields) -> dict:
    out = {}
    for f in fields:
        if old.get(f) != new.get(f):
            out[f] = [_ref_name(cursor, f, old.get(f)), _ref_name(cursor, f, new.get(f))]
    return out


def _summarize_changes(label, name, changes: dict, moved_m=None, extra="") -> str:
    parts = []
    for f, (a, b) in changes.items():
        if f in ("latitude", "longitude", "spec_data", "core_data"):
            continue
        parts.append(f"{FIELD_LABELS.get(f, f)}: {a if a not in (None, '') else '-'} -> {b if b not in (None, '') else '-'}")
    if moved_m is not None:
        parts.append(f"posisi dipindah ±{moved_m:.0f} m")
    if "core_data" in changes or "spec_data" in changes:
        parts.append("data tambahan diubah")
    return f"Ubah {label} {name}: " + "; ".join(parts) + extra if parts else f"Ubah {label} {name}" + extra


def _row_dict(row):
    return dict(row) if row is not None else None


def _conn_label(cursor, r) -> str:
    def nm(t, i):
        row = cursor.execute(f"SELECT name FROM {_table_of(t)} WHERE id = ?", (i,)).fetchone()
        return row["name"] if row else f"#{i}"
    s = f"{nm(r['from_asset_type'], r['from_asset_id'])} [{r['from_port_core']}] -> " \
        f"{nm(r['to_asset_type'], r['to_asset_id'])} [{r['to_port_core']}]"
    if r["via_cable_id"] is not None:
        s += f" via {nm('CABLE', r['via_cable_id'])} ({r['via_core'] or '-'})"
    return s


# --- MODEL & ENDPOINT AUTENTIKASI ---
class LoginRequest(BaseModel):
    username: str
    password: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class UserCreate(BaseModel):
    username: str
    full_name: Optional[str] = None
    role: str
    password: str


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    role: Optional[str] = None
    active: Optional[bool] = None


class PasswordReset(BaseModel):
    new_password: Optional[str] = None


def _check_password_policy(pw: str):
    if len(pw or "") < MIN_PASSWORD_LEN:
        raise HTTPException(status_code=400, detail=f"Password minimal {MIN_PASSWORD_LEN} karakter")
    if pw.isdigit() or pw.isalpha():
        raise HTTPException(status_code=400, detail="Password harus mengandung huruf dan angka/simbol")


def _cookie_secure() -> bool:
    return os.environ.get("NETGIS_COOKIE_SECURE", "").lower() in ("1", "true", "yes")


@app.post("/api/auth/login")
def login(payload: LoginRequest):
    global _DUMMY_HASH
    username = (payload.username or "").strip()
    failure = None           # (status, detail) -> dilempar SETELAH transaksi di-commit (agar hitungan gagal tersimpan)
    token = None
    user_public = None
    with db() as conn:
        cursor = conn.cursor()
        row = cursor.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
        if row is None:
            if _DUMMY_HASH is None:
                _DUMMY_HASH = _hash_password("dummy-password-1")
            _verify_password(payload.password, _DUMMY_HASH)   # samakan waktu respons
            _audit(cursor, "LOGIN_FAILED", "USER", None, username, f"Login gagal: user '{username}' tidak dikenal",
                   username=username)
            failure = (401, "Username atau password salah")
        elif row["locked_until"] and row["locked_until"] > _now_str():
            failure = (429, f"Akun dikunci sementara karena terlalu banyak percobaan gagal. "
                            f"Coba lagi setelah {LOCK_MINUTES} menit.")
            _audit(cursor, "LOGIN_FAILED", "USER", row["id"], row["username"], "Login ditolak: akun terkunci",
                   username=row["username"])
        elif not row["active"] or not _verify_password(payload.password, row["password_hash"]):
            n = (row["failed_attempts"] or 0) + 1
            if n >= MAX_FAILED_LOGINS:
                cursor.execute("UPDATE users SET failed_attempts = 0, locked_until = ? WHERE id = ?",
                               (_now_str(LOCK_MINUTES), row["id"]))
                msg = f"Login gagal ({MAX_FAILED_LOGINS}x): akun dikunci {LOCK_MINUTES} menit"
            else:
                cursor.execute("UPDATE users SET failed_attempts = ? WHERE id = ?", (n, row["id"]))
                msg = f"Login gagal ({n}/{MAX_FAILED_LOGINS})"
            _audit(cursor, "LOGIN_FAILED", "USER", row["id"], row["username"], msg, username=row["username"])
            failure = (401, "Username atau password salah")
        else:
            cursor.execute("UPDATE users SET failed_attempts = 0, locked_until = NULL, last_login_at = ? WHERE id = ?",
                           (_now_str(), row["id"]))
            cursor.execute("DELETE FROM sessions WHERE expires_at <= ?", (_now_str(),))
            token = secrets.token_urlsafe(32)
            cursor.execute(
                "INSERT INTO sessions (token_hash, user_id, expires_at, last_seen_at, ip) VALUES (?, ?, ?, ?, ?)",
                (_hash_token(token), row["id"], _now_str(SESSION_HOURS * 60), _now_str(), CURRENT_IP.get()))
            user_public = _public_user(row)
            ctok = CURRENT_USER.set(user_public)       # supaya baris audit mencatat user ini
            try:
                _audit(cursor, "LOGIN", "USER", row["id"], row["username"], "Login berhasil")
            finally:
                CURRENT_USER.reset(ctok)
    if failure:
        raise HTTPException(status_code=failure[0], detail=failure[1])
    resp = JSONResponse({"user": user_public})
    resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_HOURS * 3600, httponly=True, samesite="lax",
                    secure=_cookie_secure(), path="/")
    return resp


@app.post("/api/auth/logout")
def logout():
    token = CURRENT_TOKEN.get()
    if token:
        with db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))
            u = CURRENT_USER.get()
            if u:
                _audit(cursor, "LOGOUT", "USER", u["id"], u["username"], "Logout")
    resp = JSONResponse({"message": "Logout berhasil"})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/api/auth/me")
def whoami():
    u = CURRENT_USER.get()
    if not u:
        raise HTTPException(status_code=401, detail="Belum login")
    return {"user": u}


@app.post("/api/auth/password")
def change_password(payload: PasswordChange):
    u = CURRENT_USER.get()
    if not u:
        raise HTTPException(status_code=401, detail="Belum login")
    _check_password_policy(payload.new_password)
    with db() as conn:
        cursor = conn.cursor()
        row = cursor.execute("SELECT * FROM users WHERE id = ?", (u["id"],)).fetchone()
        if not row or not _verify_password(payload.current_password, row["password_hash"]):
            raise HTTPException(status_code=400, detail="Password saat ini salah")
        if payload.current_password == payload.new_password:
            raise HTTPException(status_code=400, detail="Password baru harus berbeda dari yang lama")
        cursor.execute("UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
                       (_hash_password(payload.new_password), u["id"]))
        cur_tok = CURRENT_TOKEN.get()
        cursor.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                       (u["id"], _hash_token(cur_tok) if cur_tok else ""))
        _audit(cursor, "PASSWORD_CHANGED", "USER", u["id"], u["username"], "Password diganti oleh pemilik akun")
    return {"message": "Password berhasil diganti"}


# --- KELOLA USER (admin) ---
def _user_row_out(r) -> dict:
    return {"id": r["id"], "username": r["username"], "full_name": r["full_name"] or r["username"],
            "role": r["role"], "role_label": ROLES.get(r["role"], r["role"]), "active": bool(r["active"]),
            "must_change_password": bool(r["must_change_password"]),
            "last_login_at": _utc_iso(r["last_login_at"]), "created_at": _utc_iso(r["created_at"]),
            "locked": bool(r["locked_until"] and r["locked_until"] > _now_str())}


def _active_admin_count(cursor, excluding=None) -> int:
    sql = "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1"
    args = ()
    if excluding is not None:
        sql += " AND id != ?"
        args = (excluding,)
    return cursor.execute(sql, args).fetchone()[0]


@app.get("/api/users")
def list_users():
    with db() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY username COLLATE NOCASE").fetchall()
    return {"users": [_user_row_out(r) for r in rows], "roles": [{"value": k, "label": v} for k, v in ROLES.items()]}


@app.post("/api/users")
def create_user(payload: UserCreate):
    username = (payload.username or "").strip()
    if not re.match(r"^[A-Za-z0-9_.-]{3,32}$", username):
        raise HTTPException(status_code=400, detail="Username 3-32 karakter: huruf, angka, titik, garis bawah, strip")
    if payload.role not in ROLES:
        raise HTTPException(status_code=400, detail=f"Peran tidak valid. Pilihan: {', '.join(ROLES)}")
    _check_password_policy(payload.password)
    with db() as conn:
        cursor = conn.cursor()
        if cursor.execute("SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone():
            raise HTTPException(status_code=409, detail=f"Username '{username}' sudah dipakai")
        cursor.execute(
            "INSERT INTO users (username, full_name, role, password_hash, must_change_password) VALUES (?, ?, ?, ?, 1)",
            (username, (payload.full_name or "").strip() or username, payload.role, _hash_password(payload.password)))
        uid = cursor.lastrowid
        _audit(cursor, "USER_CREATED", "USER", uid, username, f"User {username} dibuat dengan peran {ROLES[payload.role]}")
    return {"message": "User dibuat. Wajib ganti password saat login pertama.", "id": uid}


@app.put("/api/users/{user_id}")
def update_user(user_id: int, payload: UserUpdate):
    data = _fields_set(payload)
    me = CURRENT_USER.get()
    with db() as conn:
        cursor = conn.cursor()
        row = cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User tidak ditemukan")
        if "role" in data and data["role"] not in ROLES:
            raise HTTPException(status_code=400, detail=f"Peran tidak valid. Pilihan: {', '.join(ROLES)}")
        new_role = data.get("role", row["role"])
        new_active = int(bool(data["active"])) if "active" in data else row["active"]
        if me and me["id"] == user_id and (not new_active or new_role != row["role"]):
            raise HTTPException(status_code=400, detail="Anda tidak dapat menonaktifkan atau mengubah peran akun sendiri")
        if row["role"] == "admin" and row["active"] and (new_role != "admin" or not new_active) \
                and _active_admin_count(cursor, excluding=user_id) == 0:
            raise HTTPException(status_code=409, detail="Harus tersisa minimal satu admin aktif")
        changes = {}
        if "full_name" in data and (data["full_name"] or "").strip() != (row["full_name"] or ""):
            changes["full_name"] = [row["full_name"], data["full_name"].strip()]
        if new_role != row["role"]:
            changes["role"] = [row["role"], new_role]
        if new_active != row["active"]:
            changes["active"] = [bool(row["active"]), bool(new_active)]
        if changes:
            cursor.execute("UPDATE users SET full_name = ?, role = ?, active = ? WHERE id = ?",
                           (changes.get("full_name", [None, row["full_name"]])[1], new_role, new_active, user_id))
            if not new_active or new_role != row["role"]:
                cursor.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))   # paksa login ulang
            _audit(cursor, "USER_UPDATED", "USER", user_id, row["username"],
                   f"Ubah user {row['username']}: " + "; ".join(f"{k}: {a} -> {b}" for k, (a, b) in changes.items()),
                   changes)
    return {"message": "User diperbarui"}


@app.post("/api/users/{user_id}/reset-password")
def reset_user_password(user_id: int, payload: PasswordReset):
    new_pw = payload.new_password or (secrets.token_urlsafe(9) + "7")
    _check_password_policy(new_pw)
    with db() as conn:
        cursor = conn.cursor()
        row = cursor.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User tidak ditemukan")
        cursor.execute("UPDATE users SET password_hash = ?, must_change_password = 1, failed_attempts = 0, "
                       "locked_until = NULL WHERE id = ?", (_hash_password(new_pw), user_id))
        cursor.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        _audit(cursor, "PASSWORD_RESET", "USER", user_id, row["username"], f"Password {row['username']} direset oleh admin")
    return {"message": "Password direset. User wajib menggantinya saat login.", "new_password": new_pw}


def _bootstrap_admin(cursor):
    """Database tanpa user -> buat admin awal. Password dari NETGIS_ADMIN_PASSWORD, atau acak dan dicetak SEKALI."""
    if cursor.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        return
    pw = os.environ.get("NETGIS_ADMIN_PASSWORD")
    generated = not pw
    if generated:
        pw = secrets.token_urlsafe(9) + "7"
    cursor.execute("INSERT INTO users (username, full_name, role, password_hash, must_change_password) "
                   "VALUES ('admin', 'Administrator', 'admin', ?, 1)", (_hash_password(pw),))
    print("=" * 62)
    print("[AUTH] Akun admin awal dibuat.")
    print("       username : admin")
    print(f"       password : {pw}" if generated else "       password : (dari NETGIS_ADMIN_PASSWORD)")
    print("       Anda akan diminta menggantinya saat login pertama.")
    print("=" * 62)


# --- MODEL DATA (PYDANTIC) ---
class NodeCreate(BaseModel):
    name: str
    type: str
    status: str
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    cluster: Optional[str] = "EKO"
    area: Optional[str] = "BANJARMASIN"
    city: Optional[str] = "Kota Banjarmasin"
    capacity: Optional[str] = "8 Port"
    spec_data: Optional[str] = "{}"
    parent_node_id: Optional[int] = None
    upstream_cable_id: Optional[int] = None


class CableCreate(BaseModel):
    name: str
    type: str
    status: str
    coordinates: List[List[float]]
    cluster: Optional[str] = "EKO"
    area: Optional[str] = "BANJARMASIN"
    city: Optional[str] = "Kota Banjarmasin"
    capacity: Optional[str] = "2C"
    core_data: Optional[str] = "{}"
    installation: Optional[str] = "Udara"
    parent_cable_id: Optional[int] = None
    from_node_id: Optional[int] = None
    to_node_id: Optional[int] = None


class StatusUpdate(BaseModel):
    status: str  # 'Active', 'Maintenance', 'Cut/Broken'


class NodeUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    status: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    cluster: Optional[str] = None
    area: Optional[str] = None
    city: Optional[str] = None
    capacity: Optional[str] = None
    spec_data: Optional[str] = None
    parent_node_id: Optional[int] = None
    upstream_cable_id: Optional[int] = None


class CableUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    status: Optional[str] = None
    coordinates: Optional[List[List[float]]] = None
    cluster: Optional[str] = None
    area: Optional[str] = None
    city: Optional[str] = None
    capacity: Optional[str] = None
    core_data: Optional[str] = None
    installation: Optional[str] = None
    parent_cable_id: Optional[int] = None
    from_node_id: Optional[int] = None
    to_node_id: Optional[int] = None


class CoreConnectionSchema(BaseModel):
    from_asset_type: str
    from_asset_id: int
    from_port_core: str
    to_asset_type: str
    to_asset_id: int
    to_port_core: str
    via_cable_id: Optional[int] = None
    via_core: Optional[str] = None   # wajib bila via_cable_id diisi
    status: Optional[str] = "Connected"
    notes: Optional[str] = ""


class IncidentCreate(BaseModel):
    ticket_number: str
    title: str
    severity: str
    incident_type: str
    status: Optional[str] = "Open"
    description: Optional[str] = ""
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    cluster: Optional[str] = "EKO"
    area: Optional[str] = "BANJARMASIN"
    city: Optional[str] = "Kota Banjarmasin"
    linked_cable_id: Optional[int] = None
    linked_node_id: Optional[int] = None
    affected_cores: Optional[List[int]] = None   # nomor core terdampak; kosong = semua core kabel
    reporter: Optional[str] = None


class IncidentStatusUpdate(BaseModel):
    status: str
    actor: Optional[str] = None
    note: Optional[str] = None


class LocateRequest(BaseModel):
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)


class RepairCreate(BaseModel):
    kind: str                         # TEMPORARY | PERMANENT
    action: str                       # ADD_CLOSURE | EXTRA_JOINT | OTHER
    name: Optional[str] = None
    technician: Optional[str] = None
    notes: Optional[str] = ""
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)


class EventCreate(BaseModel):
    message: str
    actor: Optional[str] = None


# --- HELPER ---
def _fields_set(model: BaseModel) -> dict:
    """Hanya field yang benar-benar dikirim klien (membedakan 'tidak dikirim' vs 'null')."""
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_unset=True)
    return model.dict(exclude_unset=True)


def _check_installation(value: Optional[str]):
    if value not in (None, "") and value not in INSTALLATIONS:
        raise HTTPException(status_code=400, detail=f"Jenis pemasangan tidak valid. Pilihan: {', '.join(sorted(INSTALLATIONS))}")


def _check_status(status: Optional[str], allowed=ASSET_STATUSES):
    if status is not None and status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Status tidak valid: '{status}'. Pilihan: {', '.join(sorted(allowed))}",
        )


def _check_coordinates(coords):
    if coords is None or len(coords) < 2 or any(len(p) < 2 for p in coords):
        raise HTTPException(status_code=400, detail="Kabel butuh minimal 2 titik koordinat [lng, lat]")


def _exists(cursor, table: str, row_id: Optional[int]) -> bool:
    if row_id is None:
        return True
    return cursor.execute(f"SELECT 1 FROM {table} WHERE id = ?", (row_id,)).fetchone() is not None


def _check_refs(cursor, **refs):
    """refs: kolom -> (tabel, id). Menolak referensi ke aset yang tidak ada."""
    for label, (table, row_id) in refs.items():
        if not _exists(cursor, table, row_id):
            raise HTTPException(status_code=400, detail=f"{label} #{row_id} tidak ditemukan")


def _table_of(asset_type: str) -> str:
    return "nodes" if asset_type == "NODE" else "cables"


def _build_update(table, row_id, data, plain_fields, nullable_fields):
    """
    plain_fields    : field biasa, nilai None dianggap 'tidak diubah'
    nullable_fields : field topologi, boleh dikirim null untuk mengosongkan
    """
    sets, values = [], []
    for key in plain_fields:
        if data.get(key) is not None:
            sets.append(f"{key} = ?")
            values.append(data[key])
    for key in nullable_fields:
        if key in data:
            sets.append(f"{key} = ?")
            values.append(data[key])
    return sets, values


# --- PORT / CORE: aturan yang sama dengan frontend (buildPortOptionsHtml) ---
def _core_total(capacity) -> int:
    """Jumlah core dari teks kapasitas ('12C', '24 Core', ...). Default 12 (sama dengan frontend)."""
    m = re.match(r"\s*(\d+)", str(capacity or ""))
    return int(m.group(1)) if m and int(m.group(1)) > 0 else 12


def _core_labels(total: int) -> list:
    """Label core standar: 12 core per tube, penomoran core berlanjut."""
    return [f"Tube {(i - 1) // 12 + 1} - Core {i}" for i in range(1, total + 1)]


def _port_labels(category: str, a_type, capacity) -> list:
    """Daftar port/core sah sebuah aset. Tiang tidak punya port; ODP memakai IN-n / OUT-n."""
    if category == "NODE":
        t = (a_type or "").upper()
        if t in NODE_TYPES_NO_PORT:
            return []
        if t == "ODP":
            m = re.search(r"(\d+)\s*In\s*-\s*(\d+)\s*Out", str(capacity or ""), re.I)
            n_in, n_out = (int(m.group(1)), int(m.group(2))) if m else (1, 8)
            return [f"IN-{i}" for i in range(1, n_in + 1)] + [f"OUT-{i}" for i in range(1, n_out + 1)]
    return _core_labels(_core_total(capacity))


def _used_ports(cursor, category: str, asset_id: int) -> set:
    """Port/core milik aset ini yang sudah dipakai sambungan (sebagai ujung, atau dilewati bila kabel)."""
    used = set()
    rows = cursor.execute(
        """SELECT from_asset_type, from_asset_id, from_port_core,
                  to_asset_type, to_asset_id, to_port_core, via_cable_id, via_core
           FROM core_connections
           WHERE (from_asset_type = ? AND from_asset_id = ?)
              OR (to_asset_type = ? AND to_asset_id = ?)
              OR (? = 'CABLE' AND via_cable_id = ?)""",
        (category, asset_id, category, asset_id, category, asset_id)).fetchall()
    for r in rows:
        if r["from_asset_type"] == category and r["from_asset_id"] == asset_id and r["from_port_core"]:
            used.add(r["from_port_core"])
        if r["to_asset_type"] == category and r["to_asset_id"] == asset_id and r["to_port_core"]:
            used.add(r["to_port_core"])
        if category == "CABLE" and r["via_cable_id"] == asset_id and r["via_core"]:
            used.add(r["via_core"])
    return used


def _check_capacity_fits(cursor, category: str, asset_id: int, new_type, new_capacity):
    """Kapasitas/tipe baru tidak boleh membuang port yang sedang terpakai sambungan."""
    labels = set(_port_labels(category, new_type, new_capacity))
    orphan = sorted(_used_ports(cursor, category, asset_id) - labels)
    if orphan:
        raise HTTPException(
            status_code=409,
            detail=f"Kapasitas/tipe baru tidak muat: port {', '.join(orphan)} masih dipakai sambungan. "
                   f"Putus sambungannya dulu.")


def _would_cycle(cursor, table: str, column: str, row_id: int, new_parent: Optional[int]) -> bool:
    """True bila menjadikan new_parent sebagai parent row_id akan membentuk lingkaran topologi."""
    seen, cur = set(), new_parent
    while cur is not None and cur not in seen:
        if cur == row_id:
            return True
        seen.add(cur)
        r = cursor.execute(f"SELECT {column} FROM {table} WHERE id = ?", (cur,)).fetchone()
        cur = r[0] if r else None
    return False


# --- GEOMETRI: titik insiden -> posisi pada jalur kabel ---
def _haversine_m(lat1, lng1, lat2, lng2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _cable_coords(row):
    try:
        coords = json.loads(row["geojson_geometry"])["coordinates"]
        return coords if len(coords) >= 2 else None
    except (TypeError, ValueError, KeyError):
        return None


def _snap_to_polyline(coords, lat, lng):
    """
    Titik terdekat pada polyline [[lng,lat],...] terhadap (lat,lng).
    Mengembalikan: index segmen, t (0..1), titik hasil snap, offset_m (jarak titik asli ke kabel),
    along_m (jarak dari ujung awal kabel sampai titik snap), total_m (panjang kabel).
    """
    ky = 110540.0
    kx = 111320.0 * math.cos(math.radians(lat))
    seglens = [_haversine_m(coords[i][1], coords[i][0], coords[i + 1][1], coords[i + 1][0])
               for i in range(len(coords) - 1)]
    best = None
    for i in range(len(coords) - 1):
        ax, ay = (coords[i][0] - lng) * kx, (coords[i][1] - lat) * ky
        bx, by = (coords[i + 1][0] - lng) * kx, (coords[i + 1][1] - lat) * ky
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        t = 0.0 if l2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / l2))
        d = math.hypot(ax + t * dx, ay + t * dy)
        if best is None or d < best[0]:
            best = (d, i, t)
    d, i, t = best
    point = [coords[i][0] + t * (coords[i + 1][0] - coords[i][0]),
             coords[i][1] + t * (coords[i + 1][1] - coords[i][1])]
    return {"index": i, "t": t, "point": point, "offset_m": d,
            "along_m": sum(seglens[:i]) + seglens[i] * t, "total_m": sum(seglens)}


def _locate(cursor, lat, lng) -> dict:
    """Kabel & node terdekat dari sebuah titik (dalam batas toleransi), untuk auto-link insiden."""
    cable = None
    for row in cursor.execute("SELECT id, name, type, geojson_geometry FROM cables").fetchall():
        coords = _cable_coords(row)
        if not coords:
            continue
        sn = _snap_to_polyline(coords, lat, lng)
        if sn["offset_m"] <= CABLE_SNAP_M and (cable is None or sn["offset_m"] < cable["offset_m"]):
            cable = {"id": row["id"], "name": row["name"], "type": row["type"],
                     "offset_m": round(sn["offset_m"], 1), "position_m": round(sn["along_m"], 1),
                     "length_m": round(sn["total_m"], 1),
                     "snapped": {"latitude": sn["point"][1], "longitude": sn["point"][0]}}
    node = None
    for row in cursor.execute(
            "SELECT id, name, type, latitude, longitude FROM nodes WHERE type != 'INCIDENT'").fetchall():
        if row["latitude"] is None or row["longitude"] is None:
            continue
        d = _haversine_m(lat, lng, row["latitude"], row["longitude"])
        if d <= NODE_SNAP_M and (node is None or d < node["distance_m"]):
            node = {"id": row["id"], "name": row["name"], "type": row["type"], "distance_m": round(d, 1)}
    return {"cable": cable, "node": node}


# --- RIWAYAT INSIDEN (audit trail) ---
def _log_event(cursor, incident_id, event_type, message, data=None, actor=None):
    cursor.execute(
        "INSERT INTO incident_events (incident_id, event_type, message, actor, data) VALUES (?, ?, ?, ?, ?)",
        (incident_id, event_type, message, _current_username() or actor or "system",
         json.dumps(data, ensure_ascii=False) if data is not None else None))


def _utc_iso(value):
    """SQLite CURRENT_TIMESTAMP menyimpan UTC tanpa penanda zona ('2026-10-02 07:18:00').
    Dikirim sebagai ISO-8601 dengan 'Z' agar browser menampilkannya di zona waktu perangkat."""
    if not value or not isinstance(value, str):
        return value
    v = value.strip()
    if v.endswith("Z") or "+" in v[10:] or v[10:].count("-"):
        return v
    return v.replace(" ", "T") + "Z"


def _stamp(row: dict, *fields) -> dict:
    for f in fields:
        if f in row:
            row[f] = _utc_iso(row[f])
    return row


def _describe_assets(cursor, nodes, cables) -> dict:
    """Ringkasan aset terdampak (nama, tipe) + daftar pelanggan, untuk riwayat & layar detail."""
    out_nodes, out_cables = [], []
    for nid in sorted(nodes):
        r = cursor.execute("SELECT id, name, type FROM nodes WHERE id = ?", (nid,)).fetchone()
        if r:
            out_nodes.append({"id": r["id"], "name": r["name"], "type": r["type"]})
    for cid in sorted(cables):
        r = cursor.execute("SELECT id, name, type FROM cables WHERE id = ?", (cid,)).fetchone()
        if r:
            out_cables.append({"id": r["id"], "name": r["name"], "type": r["type"]})
    customers = [n["name"] for n in out_nodes if (n["type"] or "").upper() == "PELANGGAN"]
    return {"nodes": out_nodes, "cables": out_cables, "customers": customers,
            "counts": {"nodes": len(out_nodes), "cables": len(out_cables),
                       "customers": len(customers),
                       "odp": sum(1 for n in out_nodes if (n["type"] or "").upper() == "ODP")}}


# --- PERBAIKAN LAPANGAN: sisipkan closure/joint di jalur kabel (split kabel) ---
def _split_cable_at(cursor, cable_id, lat, lng, node_name, spec) -> dict:
    """
    Memecah kabel di titik (lat,lng) menjadi A (ujung awal -> titik) dan B (titik -> ujung akhir),
    menyisipkan node CLOSURE di titik itu, merapikan topologi dan menyambung ulang core:
        X.p --(kabel A, core c)--> CLOSURE.c   dan   CLOSURE.c --(kabel B, core c)--> Y.q
    Aset yang terdampak insiden ikut diwarisi kabel B supaya pemulihannya konsisten.
    """
    cab = cursor.execute("SELECT * FROM cables WHERE id = ?", (cable_id,)).fetchone()
    coords = _cable_coords(cab) if cab else None
    if not coords:
        raise HTTPException(status_code=400, detail="Geometri kabel tidak valid untuk dipecah")
    sn = _snap_to_polyline(coords, lat, lng)
    if sn["offset_m"] > CABLE_SNAP_M:
        raise HTTPException(
            status_code=400,
            detail=f"Titik perbaikan berjarak {sn['offset_m']:.0f} m dari kabel (maks {CABLE_SNAP_M:.0f} m)")
    if sn["along_m"] < 1 or sn["total_m"] - sn["along_m"] < 1:
        raise HTTPException(status_code=400, detail="Titik perbaikan berada di ujung kabel")

    i, pt = sn["index"], sn["point"]
    coords_a = coords[: i + 1] + [pt]
    coords_b = [pt] + coords[i + 1:]
    geom = lambda c: json.dumps({"type": "LineString", "coordinates": c})

    cursor.execute(
        """INSERT INTO nodes (name, type, status, latitude, longitude, cluster, area, city, capacity,
                              spec_data, parent_node_id, upstream_cable_id)
           VALUES (?, 'CLOSURE', 'Active', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (node_name, pt[1], pt[0], cab["cluster"], cab["area"], cab["city"], cab["capacity"],
         json.dumps(spec, ensure_ascii=False), cab["from_node_id"], cable_id))
    node_id = cursor.lastrowid

    cursor.execute(
        """INSERT INTO cables (name, type, status, geojson_geometry, cluster, area, city, capacity,
                               core_data, installation, parent_cable_id, from_node_id, to_node_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (f"{cab['name']}-seg2", cab["type"], cab["status"], geom(coords_b), cab["cluster"], cab["area"],
         cab["city"], cab["capacity"], cab["core_data"], cab["installation"] if "installation" in cab.keys() else None,
         cable_id, node_id, cab["to_node_id"]))
    cable_b = cursor.lastrowid
    cursor.execute("UPDATE cables SET geojson_geometry = ?, to_node_id = ? WHERE id = ?",
                   (geom(coords_a), node_id, cable_id))

    # Topologi: yang tadinya disuplai kabel asli di sisi ujung kini disuplai segmen B lewat closure
    if cab["to_node_id"] is not None:
        cursor.execute("UPDATE nodes SET upstream_cable_id = ? WHERE upstream_cable_id = ? AND id != ?",
                       (cable_b, cable_id, node_id))
        if cab["from_node_id"] is not None:
            cursor.execute("UPDATE nodes SET parent_node_id = ? WHERE id = ? AND parent_node_id = ?",
                           (node_id, cab["to_node_id"], cab["from_node_id"]))
            cursor.execute("UPDATE nodes SET parent_node_id = ? WHERE upstream_cable_id = ? AND parent_node_id = ?",
                           (node_id, cable_b, cab["from_node_id"]))
        cursor.execute("UPDATE cables SET parent_cable_id = ? WHERE parent_cable_id = ? AND from_node_id = ? AND id != ?",
                       (cable_b, cable_id, cab["to_node_id"], cable_b))

    # Core: pecah tiap sambungan yang melewati kabel ini menjadi dua lewat closure (core yang sama)
    spliced, skipped = 0, 0
    for r in cursor.execute("SELECT * FROM core_connections WHERE via_cable_id = ?", (cable_id,)).fetchall():
        core = r["via_core"]
        if not core:
            skipped += 1   # data lama tanpa info core: tidak bisa dipecah dengan aman
            continue
        cursor.execute("UPDATE core_connections SET to_asset_type = 'NODE', to_asset_id = ?, to_port_core = ? "
                       "WHERE id = ?", (node_id, core, r["id"]))
        cursor.execute(
            """INSERT INTO core_connections (from_asset_type, from_asset_id, from_port_core,
                   to_asset_type, to_asset_id, to_port_core, via_cable_id, via_core, status, notes)
               VALUES ('NODE', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (node_id, core, r["to_asset_type"], r["to_asset_id"], r["to_port_core"],
             cable_b, core, r["status"], "Splice closure hasil perbaikan"))
        spliced += 1

    # Dampak insiden: segmen B mewarisi status & catatan pemulihan kabel asli
    for im in cursor.execute(
            "SELECT incident_id, prev_status FROM incident_impacts WHERE asset_type = 'CABLE' AND asset_id = ?",
            (cable_id,)).fetchall():
        cursor.execute("INSERT INTO incident_impacts (incident_id, asset_type, asset_id, prev_status) "
                       "VALUES (?, 'CABLE', ?, ?)", (im["incident_id"], cable_b, im["prev_status"]))

    return {"node_id": node_id, "cable_a_id": cable_id, "cable_b_id": cable_b,
            "position_m": round(sn["along_m"], 1), "length_m": round(sn["total_m"], 1),
            "latitude": pt[1], "longitude": pt[0], "cores_spliced": spliced, "cores_skipped": skipped}


# --- IMPACT ANALYSIS ---
def get_downstream_assets(cursor, start_cable_id=None, start_node_id=None):
    """Menelusuri seluruh aset downstream (Node & Cable) dari titik kerusakan/insiden."""
    affected_cables, affected_nodes = set(), set()
    queue = deque()

    def add_cable(cid):
        if cid is not None and cid not in affected_cables:
            affected_cables.add(cid)
            queue.append(("CABLE", cid))

    def add_node(nid):
        if nid is not None and nid not in affected_nodes:
            affected_nodes.add(nid)
            queue.append(("NODE", nid))

    def add_asset(asset_type, asset_id):
        if asset_type == "NODE":
            add_node(asset_id)
        elif asset_type == "CABLE":
            add_cable(asset_id)

    add_cable(start_cable_id)
    add_node(start_node_id)

    while queue:
        kind, current = queue.popleft()

        if kind == "CABLE":
            for r in cursor.execute("SELECT id FROM cables WHERE parent_cable_id = ?", (current,)):
                add_cable(r["id"])
            for r in cursor.execute("SELECT id FROM nodes WHERE upstream_cable_id = ?", (current,)):
                add_node(r["id"])
            row = cursor.execute("SELECT to_node_id FROM cables WHERE id = ?", (current,)).fetchone()
            if row:
                add_node(row["to_node_id"])
            for r in cursor.execute(
                """SELECT to_asset_type, to_asset_id FROM core_connections
                   WHERE via_cable_id = ? OR (from_asset_type = 'CABLE' AND from_asset_id = ?)""",
                (current, current),
            ):
                add_asset(r["to_asset_type"], r["to_asset_id"])
        else:
            for r in cursor.execute("SELECT id FROM nodes WHERE parent_node_id = ?", (current,)):
                add_node(r["id"])
            for r in cursor.execute("SELECT id FROM cables WHERE from_node_id = ?", (current,)):
                add_cable(r["id"])
            for r in cursor.execute(
                """SELECT to_asset_type, to_asset_id FROM core_connections
                   WHERE from_asset_type = 'NODE' AND from_asset_id = ?""",
                (current,),
            ):
                add_asset(r["to_asset_type"], r["to_asset_id"])

    return affected_cables, affected_nodes


def release_incident_impact(cursor, incident_id):
    """
    Pulihkan aset yang di-set 'Cut/Broken' oleh incident ini, kecuali masih terdampak
    incident aktif lain. Status manual (mis. Maintenance) dikembalikan seperti semula.
    """
    rows = cursor.execute(
        "SELECT asset_type, asset_id, prev_status FROM incident_impacts WHERE incident_id = ?",
        (incident_id,),
    ).fetchall()
    cursor.execute("DELETE FROM incident_impacts WHERE incident_id = ?", (incident_id,))

    for r in rows:
        still_hit = cursor.execute(
            """SELECT 1 FROM incident_impacts im
               JOIN incidents i ON i.id = im.incident_id
               WHERE im.asset_type = ? AND im.asset_id = ? AND i.status != 'Resolved'
               LIMIT 1""",
            (r["asset_type"], r["asset_id"]),
        ).fetchone()
        if still_hit:
            continue
        restore = r["prev_status"] or "Active"
        # Hanya sentuh aset yang memang masih Cut/Broken (jika sudah diubah manual, biarkan)
        cursor.execute(
            f"UPDATE {_table_of(r['asset_type'])} SET status = ? WHERE id = ? AND status = 'Cut/Broken'",
            (restore, r["asset_id"]),
        )


def apply_incident_impact(cursor, incident_id):
    """Hitung aset terdampak dari incident ini, tandai 'Cut/Broken', dan catat status sebelumnya."""
    inc = cursor.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
    if not inc:
        return None

    release_incident_impact(cursor, incident_id)  # idempoten: pulihkan dulu, hitung ulang
    if inc["status"] not in ACTIVE_IMPACT_STATUSES:
        return set(), set()  # Resolved / Temporary Fix: layanan sudah pulih

    cores = None
    try:
        cores = json.loads(inc["affected_cores"]) if inc["affected_cores"] else None
    except ValueError:
        cores = None

    cables, nodes = set(), set()
    if inc["linked_cable_id"] is not None and cores:
        # Gangguan level core (mis. redaman tinggi): hanya yang disuplai core tsb yang terdampak
        marks = ",".join("?" * len(cores))
        for r in cursor.execute(
                f"SELECT to_asset_type, to_asset_id FROM core_connections "
                f"WHERE via_cable_id = ? AND via_core IN ({marks})", (inc["linked_cable_id"], *cores)).fetchall():
            c2, n2 = get_downstream_assets(
                cursor,
                start_cable_id=r["to_asset_id"] if r["to_asset_type"] == "CABLE" else None,
                start_node_id=r["to_asset_id"] if r["to_asset_type"] == "NODE" else None)
            cables |= c2
            nodes |= n2
    else:
        cables, nodes = get_downstream_assets(
            cursor, start_cable_id=inc["linked_cable_id"], start_node_id=inc["linked_node_id"]
        )

    for asset_type, ids in (("NODE", nodes), ("CABLE", cables)):
        table = _table_of(asset_type)
        for asset_id in ids:
            row = cursor.execute(f"SELECT status FROM {table} WHERE id = ?", (asset_id,)).fetchone()
            if not row:
                continue
            other = cursor.execute(
                """SELECT im.prev_status FROM incident_impacts im
                   JOIN incidents i ON i.id = im.incident_id
                   WHERE im.asset_type = ? AND im.asset_id = ? AND i.status != 'Resolved'
                   LIMIT 1""",
                (asset_type, asset_id),
            ).fetchone()
            prev = other["prev_status"] if other else row["status"]
            cursor.execute(
                "INSERT INTO incident_impacts (incident_id, asset_type, asset_id, prev_status) VALUES (?, ?, ?, ?)",
                (incident_id, asset_type, asset_id, prev),
            )
            cursor.execute(f"UPDATE {table} SET status = 'Cut/Broken' WHERE id = ?", (asset_id,))

    return cables, nodes


def _apply_and_log(cursor, incident_id, reason, actor=None):
    """apply_incident_impact + catat hasilnya di riwayat tiket. Mengembalikan (cables, nodes, ringkasan)."""
    before = {(r["asset_type"], r["asset_id"]) for r in cursor.execute(
        "SELECT asset_type, asset_id FROM incident_impacts WHERE incident_id = ?", (incident_id,))}
    result = apply_incident_impact(cursor, incident_id)
    if result is None:
        return None
    cables, nodes = result
    summary = _describe_assets(cursor, nodes, cables)
    if cables or nodes:
        c = summary["counts"]
        def _names(items, limit=8):
            names = [x["name"] for x in items]
            return ", ".join(names[:limit]) + (f" +{len(names) - limit} lainnya" if len(names) > limit else "")
        msg = (f"{reason}: {c['nodes']} node ({c['customers']} pelanggan), {c['cables']} kabel ditandai Cut/Broken."
               + (f" Node: {_names(summary['nodes'])}." if summary["nodes"] else "")
               + (f" Kabel: {_names(summary['cables'])}." if summary["cables"] else ""))
        _log_event(cursor, incident_id, "IMPACT_APPLIED", msg, summary, actor)
    elif before:
        _log_event(cursor, incident_id, "IMPACT_RELEASED",
                   f"{reason}: {len(before)} aset dipulihkan", None, actor)
    return cables, nodes, summary


# --- ROUTE API ---

# 1. GET ALL NODES
# --- INVENTORY: filter + sort + pagination di server ---
INVENTORY_SORT = {"name": "name", "type": "type", "cluster": "cluster", "area": "area",
                  "city": "city", "capacity": "capacity", "status": "status", "category": "category",
                  "installation": "installation", "length": None}   # length: dihitung dari geometri, diurutkan di Python


def _cable_length_map(conn, ids=None) -> dict:
    """{id_kabel: panjang_m} dari geometri."""
    sql = "SELECT id, geojson_geometry FROM cables"
    args = []
    if ids is not None:
        if not ids:
            return {}
        sql += f" WHERE id IN ({','.join('?' * len(ids))})"
        args = list(ids)
    out = {}
    for r in conn.execute(sql, args).fetchall():
        try:
            g = json.loads(r["geojson_geometry"])
            coords = g["coordinates"] if isinstance(g, dict) else g
            out[r["id"]] = round(_polyline_length_m(coords), 1)
        except (TypeError, ValueError, KeyError):
            out[r["id"]] = 0.0
    return out


@app.get("/api/inventory")
def get_inventory(q: str = "", cluster: str = "ALL", type: str = "ALL", status: str = "ALL",
                  installation: str = "ALL", sort: str = "name", order: str = "asc", page: int = 1,
                  page_size: int = 25):
    """Daftar aset (node + kabel) terpaginasi. type='CABLE' = semua kabel; selain itu mencocokkan tipe node/kabel.
    installation: Udara | Tanah | NONE (belum diisi) -> hanya berlaku untuk kabel."""
    if sort not in INVENTORY_SORT:
        raise HTTPException(status_code=400, detail=f"Kolom sort tidak valid: {sort}")
    if installation not in ("ALL", "NONE") and installation not in INSTALLATIONS:
        raise HTTPException(status_code=400, detail=f"Filter pemasangan tidak valid: {installation}")
    order_sql = "DESC" if order.lower() == "desc" else "ASC"
    page_size = max(1, min(int(page_size), 200))

    where, params = [], []
    if q.strip():
        like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(name LIKE ? ESCAPE '\\' OR city LIKE ? ESCAPE '\\' OR area LIKE ? ESCAPE '\\')")
        params += [like, like, like]
    if cluster != "ALL":
        where.append("cluster = ?")
        params.append(cluster)
    if type == "CABLE":
        where.append("category = 'CABLE'")
    elif type != "ALL":
        where.append("type = ?")
        params.append(type)
    if status != "ALL":
        where.append("status = ?")
        params.append(status)
    if installation == "NONE":
        where.append("category = 'CABLE' AND (installation IS NULL OR installation = '')")
    elif installation != "ALL":
        where.append("installation = ?")
        params.append(installation)

    base = ("SELECT 'NODE' AS category, id, name, type, status, cluster, area, city, capacity, "
            "NULL AS installation FROM nodes "
            "UNION ALL "
            "SELECT 'CABLE' AS category, id, name, type, status, cluster, area, city, capacity, "
            "installation FROM cables")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    with db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM ({base}){where_sql}", params).fetchone()[0]
        pages = max(1, -(-total // page_size))
        page = max(1, min(int(page), pages))  # halaman di luar jangkauan (mis. setelah hapus) -> dirapikan
        if sort == "length":
            # urut berdasarkan panjang: hitung dari geometri lalu potong halaman di Python (node dianggap 0 m)
            rows = [dict(r) for r in conn.execute(f"SELECT * FROM ({base}){where_sql}", params).fetchall()]
            lengths = _cable_length_map(conn)
            for r in rows:
                r["length_m"] = lengths.get(r["id"]) if r["category"] == "CABLE" else None
            rows.sort(key=lambda r: ((r["name"] or "").lower(), r["id"]))
            rows.sort(key=lambda r: r["length_m"] or 0.0, reverse=(order_sql == "DESC"))
            items = rows[(page - 1) * page_size: page * page_size]
        else:
            rows = conn.execute(
                f"SELECT * FROM ({base}){where_sql} "
                f"ORDER BY {INVENTORY_SORT[sort]} COLLATE NOCASE {order_sql}, category, id "
                f"LIMIT ? OFFSET ?", (*params, page_size, (page - 1) * page_size)).fetchall()
            items = [dict(r) for r in rows]
            lengths = _cable_length_map(conn, [r["id"] for r in items if r["category"] == "CABLE"])
            for r in items:
                r["length_m"] = lengths.get(r["id"]) if r["category"] == "CABLE" else None
        # ringkasan kabel pada hasil filter (seluruh halaman, bukan hanya halaman ini)
        cab_sql = f"SELECT id FROM ({base}){where_sql}" + (" AND" if where else " WHERE") + " category = 'CABLE'"
        cab_ids = [r["id"] for r in conn.execute(cab_sql, params).fetchall()]
        cab_len = round(sum(_cable_length_map(conn, cab_ids).values()), 1) if cab_ids else 0.0
    return {"items": items, "total": total, "page": page,
            "page_size": page_size, "pages": pages,
            "summary": {"cables": len(cab_ids), "length_m": cab_len}}


@app.get("/api/nodes")
def get_nodes():
    with db() as conn:
        nodes = conn.execute("SELECT * FROM nodes").fetchall()
        # Closure/joint hasil perbaikan lapangan: jenis perbaikan terakhir + tiketnya
        repair_by_node = {}
        for r in conn.execute(
                """SELECT r.node_id, r.kind, r.incident_id, i.ticket_number
                   FROM incident_repairs r JOIN incidents i ON i.id = r.incident_id
                   WHERE r.node_id IS NOT NULL AND r.undone_at IS NULL ORDER BY r.id""").fetchall():
            repair_by_node[r["node_id"]] = r

    features = []
    for node in nodes:
        n = dict(node)
        rp = repair_by_node.get(n["id"])
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [n["longitude"], n["latitude"]]},
            "properties": {
                "id": n["id"],
                "name": n["name"],
                "type": n["type"],
                "status": n["status"],
                "cluster": n.get("cluster") or "EKO",
                "area": n.get("area") or "BANJARMASIN",
                "city": n.get("city") or "Kota Banjarmasin",
                "capacity": n.get("capacity") or "8 Port",
                "spec_data": n.get("spec_data") or "{}",
                "parent_node_id": n.get("parent_node_id"),
                "upstream_cable_id": n.get("upstream_cable_id"),
                "repair_kind": rp["kind"] if rp else None,
                "repair_ticket": rp["ticket_number"] if rp else None,
                "repair_incident_id": rp["incident_id"] if rp else None,
            },
        })
    return {"type": "FeatureCollection", "features": features}


# 2. CREATE NODE (POST)
@app.post("/api/nodes")
def create_node(node: NodeCreate):
    _check_status(node.status)
    with db() as conn:
        cursor = conn.cursor()
        _check_refs(
            cursor,
            **{"Parent node": ("nodes", node.parent_node_id),
               "Kabel upstream": ("cables", node.upstream_cable_id)},
        )
        cursor.execute('''
            INSERT INTO nodes (name, type, status, latitude, longitude, cluster, area, city,
                               capacity, spec_data, parent_node_id, upstream_cable_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            node.name, node.type, node.status, node.latitude, node.longitude,
            node.cluster, node.area, node.city, node.capacity, node.spec_data,
            node.parent_node_id, node.upstream_cable_id,
        ))
        node_id = cursor.lastrowid
        _audit(cursor, "CREATE", "NODE", node_id, node.name, f"Tambah {node.type} {node.name}",
               snapshot=_row_dict(cursor.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()))
    print(f"[SUCCESS] Node '{node.name}' berhasil disimpan dengan ID: {node_id}")
    return {"message": "Node berhasil ditambahkan", "id": node_id}


# 3. GET ALL CABLES
@app.get("/api/cables")
def get_cables():
    with db() as conn:
        cables = conn.execute("SELECT * FROM cables").fetchall()

    features = []
    for cable in cables:
        c = dict(cable)
        geom_raw = c.get("geojson_geometry") or c.get("coordinates")
        try:
            geom = json.loads(geom_raw) if isinstance(geom_raw, str) else geom_raw
        except (TypeError, ValueError):
            geom = None
        if isinstance(geom, list):
            geom = {"type": "LineString", "coordinates": geom}
        if not isinstance(geom, dict) or len(geom.get("coordinates") or []) < 2:
            print(f"[WARN] Kabel ID {c['id']} dilewati: geometri tidak valid")
            continue

        features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "id": c["id"],
                "name": c["name"],
                "type": "Drop" if c["type"] == "Dropcore" else c["type"],
                "status": c["status"],
                "cluster": c.get("cluster") or "EKO",
                "area": c.get("area") or "BANJARMASIN",
                "city": c.get("city") or "Kota Banjarmasin",
                "capacity": c.get("capacity") or "24C",
                "core_data": c.get("core_data") or "{}",
                "installation": c.get("installation"),
                "length_m": round(_polyline_length_m(geom["coordinates"]), 1),
                "parent_cable_id": c.get("parent_cable_id"),
                "from_node_id": c.get("from_node_id"),
                "to_node_id": c.get("to_node_id"),
            },
        })
    return {"type": "FeatureCollection", "features": features}


# 4. CREATE CABLE (POST)
@app.post("/api/cables")
def create_cable(cable: CableCreate):
    _check_status(cable.status)
    _check_installation(cable.installation)
    _check_coordinates(cable.coordinates)
    geojson_geom = {"type": "LineString", "coordinates": cable.coordinates}

    with db() as conn:
        cursor = conn.cursor()
        _check_refs(
            cursor,
            **{"Parent kabel": ("cables", cable.parent_cable_id),
               "Node asal": ("nodes", cable.from_node_id),
               "Node tujuan": ("nodes", cable.to_node_id)},
        )
        cursor.execute('''
            INSERT INTO cables (name, type, status, geojson_geometry, cluster, area, city, capacity,
                                core_data, installation, parent_cable_id, from_node_id, to_node_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            cable.name, cable.type, cable.status, json.dumps(geojson_geom),
            cable.cluster, cable.area, cable.city, cable.capacity, cable.core_data, cable.installation or None,
            cable.parent_cable_id, cable.from_node_id, cable.to_node_id,
        ))
        cable_id = cursor.lastrowid
        _audit(cursor, "CREATE", "CABLE", cable_id, cable.name, f"Tambah kabel {cable.type} {cable.name} ({cable.capacity})",
               snapshot=_row_dict(cursor.execute("SELECT * FROM cables WHERE id = ?", (cable_id,)).fetchone()))
    print(f"[SUCCESS] Cable '{cable.name}' ({cable.capacity}) berhasil disimpan dengan ID: {cable_id}")
    return {"message": "Kabel berhasil ditambahkan", "id": cable_id}


def _asset_covered(cursor, asset_type, asset_id) -> bool:
    """True bila aset sudah tercatat pada tiket aktif (terdampak atau menjadi titik gangguannya)."""
    link_col = "linked_cable_id" if asset_type == "CABLE" else "linked_node_id"
    hit = cursor.execute(
        f"""SELECT 1 FROM incidents i
            WHERE i.status IN ('Open', 'In Progress') AND (
                i.{link_col} = ? OR EXISTS (
                    SELECT 1 FROM incident_impacts im
                    WHERE im.incident_id = i.id AND im.asset_type = ? AND im.asset_id = ?))
            LIMIT 1""", (asset_id, asset_type, asset_id)).fetchone()
    return hit is not None


def _auto_incident(cursor, asset_type, asset_id) -> int:
    """Status manual 'Cut/Broken' tanpa tiket -> otomatis dibuatkan tiket supaya setiap gangguan tercatat."""
    row = cursor.execute(f"SELECT * FROM {_table_of(asset_type)} WHERE id = ?", (asset_id,)).fetchone()
    pos = length = None
    if asset_type == "NODE":
        lat, lng = row["latitude"], row["longitude"]
    else:
        coords = _cable_coords(row)
        if coords:
            lng, lat = coords[len(coords) // 2]
            sn = _snap_to_polyline(coords, lat, lng)
            pos, length = sn["along_m"], sn["total_m"]
        else:
            lat = lng = 0
    n = cursor.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM incidents").fetchone()[0]
    ticket = f"AUTO-{n:05d}"
    while cursor.execute("SELECT 1 FROM incidents WHERE ticket_number = ?", (ticket,)).fetchone():
        n += 1
        ticket = f"AUTO-{n:05d}"
    cursor.execute(
        """INSERT INTO incidents (ticket_number, title, severity, incident_type, status, description,
               latitude, longitude, cluster, area, city, linked_cable_id, linked_node_id,
               cable_position_m, cable_length_m, source, cable_from_node_id, cable_to_node_id)
           VALUES (?, ?, 'Major', 'Manual Status', 'Open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'AUTO', ?, ?)""",
        (ticket, f"Status manual Cut/Broken: {row['name']}",
         "Tiket dibuat otomatis karena status aset diubah manual menjadi Cut/Broken.",
         lat or 0, lng or 0, row["cluster"], row["area"], row["city"],
         asset_id if asset_type == "CABLE" else None, asset_id if asset_type == "NODE" else None,
         pos, length,
         row["from_node_id"] if asset_type == "CABLE" else None,
         row["to_node_id"] if asset_type == "CABLE" else None))
    inc_id = cursor.lastrowid
    _log_event(cursor, inc_id, "CREATED", f"Tiket otomatis dibuat: {row['name']} diubah manual ke Cut/Broken")
    _apply_and_log(cursor, inc_id, "Dampak dihitung")
    return inc_id


def _auto_resolve(cursor, asset_type, asset_id):
    """Aset dipulihkan manual -> tutup tiket AUTO miliknya (tiket manual tidak disentuh)."""
    link_col = "linked_cable_id" if asset_type == "CABLE" else "linked_node_id"
    for inc in cursor.execute(
            f"SELECT id FROM incidents WHERE source = 'AUTO' AND {link_col} = ? "
            f"AND status IN ('Open', 'In Progress', 'Temporary Fix')", (asset_id,)).fetchall():
        cursor.execute("UPDATE incidents SET status = 'Resolved', resolved_at = CURRENT_TIMESTAMP, "
                       "resolution = 'Status aset dipulihkan manual' WHERE id = ?", (inc["id"],))
        _log_event(cursor, inc["id"], "STATUS_CHANGED", "Tiket otomatis ditutup: status aset dipulihkan manual")
        _apply_and_log(cursor, inc["id"], "Aset dipulihkan")


def _set_asset_status(asset_type, asset_id, new_status):
    _check_status(new_status)
    table = _table_of(asset_type)
    label = "Node" if asset_type == "NODE" else "Kabel"
    with db() as conn:
        cursor = conn.cursor()
        row = cursor.execute(f"SELECT status FROM {table} WHERE id = ?", (asset_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"{label} tidak ditemukan")
        old = row["status"]
        if new_status == "Cut/Broken" and old != "Cut/Broken" and not _asset_covered(cursor, asset_type, asset_id):
            _auto_incident(cursor, asset_type, asset_id)   # aset ikut ditandai Cut/Broken lewat dampak tiket
        else:
            cursor.execute(f"UPDATE {table} SET status = ? WHERE id = ?", (new_status, asset_id))
            if old == "Cut/Broken" and new_status != "Cut/Broken":
                _auto_resolve(cursor, asset_type, asset_id)
        if old != new_status:
            nm = cursor.execute(f"SELECT name FROM {table} WHERE id = ?", (asset_id,)).fetchone()["name"]
            _audit(cursor, "STATUS", asset_type, asset_id, nm, f"Status {label.lower()} {nm}: {old} -> {new_status} (manual)",
                   {"status": [old, new_status]})
    print(f"[SUCCESS] Status {label} ID {asset_id} diperbarui menjadi '{new_status}'")


# 5. UPDATE STATUS NODE
@app.put("/api/nodes/{node_id}/status")
def update_node_status(node_id: int, payload: StatusUpdate):
    _set_asset_status("NODE", node_id, payload.status)
    return {"message": "Status node berhasil diperbarui"}


# 6. UPDATE STATUS KABEL
@app.put("/api/cables/{cable_id}/status")
def update_cable_status(cable_id: int, payload: StatusUpdate):
    _set_asset_status("CABLE", cable_id, payload.status)
    return {"message": "Status kabel berhasil diperbarui"}


# 7. SUMMARY STATISTIK DASHBOARD
NODE_SUMMARY_ORDER = ["POP", "CLOSURE", "ODP", "HH", "TIANG", "SLACK", "PELANGGAN", "INCIDENT"]


@app.get("/api/dashboard/summary")
def get_summary():
    with db() as conn:
        q = lambda sql: conn.execute(sql).fetchone()[0]

        total_nodes = q("SELECT COUNT(*) FROM nodes")
        total_odp = q("SELECT COUNT(*) FROM nodes WHERE type = 'ODP'")
        total_cables = q("SELECT COUNT(*) FROM cables")

        tickets_open = q("SELECT COUNT(*) FROM incidents WHERE status != 'Resolved'")

        # Aset Cut/Broken yang BUKAN akibat tiket aktif (dilaporkan manual dari popup) tetap dihitung
        covered = """SELECT im.asset_id FROM incident_impacts im
                     JOIN incidents i ON i.id = im.incident_id
                     WHERE i.status != 'Resolved' AND im.asset_type = '{t}'"""
        manual_nodes = q(
            f"SELECT COUNT(*) FROM nodes WHERE status = 'Cut/Broken' AND id NOT IN ({covered.format(t='NODE')})"
        )
        manual_cables = q(
            f"SELECT COUNT(*) FROM cables WHERE status = 'Cut/Broken' AND id NOT IN ({covered.format(t='CABLE')})"
        )
        broken_nodes = q("SELECT COUNT(*) FROM nodes WHERE status = 'Cut/Broken'")
        broken_cables = q("SELECT COUNT(*) FROM cables WHERE status = 'Cut/Broken'")

        # --- rincian per jenis node: jumlah + sebaran status ---
        by_type = {}
        for r in conn.execute("SELECT UPPER(COALESCE(type, '')) AS t, status, COUNT(*) AS n FROM nodes GROUP BY 1, 2"):
            d = by_type.setdefault(r["t"], {"total": 0, "Active": 0, "Maintenance": 0, "Cut/Broken": 0})
            d["total"] += r["n"]
            d[r["status"] if r["status"] in ("Active", "Maintenance", "Cut/Broken") else "Active"] += r["n"]
        for t in NODE_SUMMARY_ORDER:
            by_type.setdefault(t, {"total": 0, "Active": 0, "Maintenance": 0, "Cut/Broken": 0})

        # --- rincian kabel: panjang total, per jenis, per cara pemasangan ---
        cab = {"total": total_cables, "length_m": 0.0, "broken": broken_cables,
               "by_type": {t: {"count": 0, "length_m": 0.0} for t in ("Backbone", "Feeder", "Distribution", "Drop")},
               "by_installation": {k: {"count": 0, "length_m": 0.0} for k in ("Udara", "Tanah", "Belum diisi")}}
        lengths = _cable_length_map(conn)
        for r in conn.execute("SELECT id, type, installation FROM cables"):
            ln = lengths.get(r["id"], 0.0)
            t = "Drop" if r["type"] == "Dropcore" else (r["type"] or "Distribution")
            bt = cab["by_type"].setdefault(t, {"count": 0, "length_m": 0.0})
            bt["count"] += 1
            bt["length_m"] += ln
            bi = cab["by_installation"][r["installation"] if r["installation"] in INSTALLATIONS else "Belum diisi"]
            bi["count"] += 1
            bi["length_m"] += ln
            cab["length_m"] += ln
        cab["length_m"] = round(cab["length_m"], 1)
        for grp in (cab["by_type"], cab["by_installation"]):
            for v in grp.values():
                v["length_m"] = round(v["length_m"], 1)

    return {
        "total_nodes": total_nodes,
        "total_odp": total_odp,
        # Tiket aktif + laporan manual; aset terdampak satu tiket tidak dihitung ganda
        "total_incidents": tickets_open + manual_nodes + manual_cables,
        "affected_assets": broken_nodes + broken_cables,
        "total_cables": total_cables,
        "tickets_open": tickets_open,
        "nodes_by_type": by_type,
        "cables": cab,
    }


# 8. UPDATE NODE (PUT)
@app.put("/api/nodes/{node_id}")
def update_node(node_id: int, payload: NodeUpdate):
    data = _fields_set(payload)
    _check_status(data.get("status"))
    if data.get("parent_node_id") == node_id:
        raise HTTPException(status_code=400, detail="Node tidak boleh menjadi parent dirinya sendiri")

    with db() as conn:
        cursor = conn.cursor()
        if not _exists(cursor, "nodes", node_id):
            raise HTTPException(status_code=404, detail="Node tidak ditemukan")
        _check_refs(
            cursor,
            **{"Parent node": ("nodes", data.get("parent_node_id")),
               "Kabel upstream": ("cables", data.get("upstream_cable_id"))},
        )
        if data.get("parent_node_id") is not None and _would_cycle(
                cursor, "nodes", "parent_node_id", node_id, data["parent_node_id"]):
            raise HTTPException(status_code=400, detail="Parent node membentuk lingkaran topologi")
        if "capacity" in data or "type" in data:
            cur = cursor.execute("SELECT type, capacity FROM nodes WHERE id = ?", (node_id,)).fetchone()
            _check_capacity_fits(cursor, "NODE", node_id,
                                 data.get("type") or cur["type"], data.get("capacity") or cur["capacity"])

        old_node = _row_dict(cursor.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone())
        sets, values = _build_update(
            "nodes", node_id, data,
            ["name", "type", "status", "latitude", "longitude", "cluster", "area", "city", "capacity", "spec_data"],
            ["parent_node_id", "upstream_cable_id"],
        )
        if sets:
            cursor.execute(f"UPDATE nodes SET {', '.join(sets)} WHERE id = ?", (*values, node_id))
        snapped = 0
        if "latitude" in data or "longitude" in data:
            snapped = _snap_cables_to_node(cursor, node_id)
        new_node = _row_dict(cursor.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone())
        changes = _diff_rows(cursor, old_node, new_node,
                             ["name", "type", "status", "latitude", "longitude", "cluster", "area", "city",
                              "capacity", "spec_data", "parent_node_id", "upstream_cable_id"])
        if changes:
            moved = None
            if "latitude" in changes or "longitude" in changes:
                moved = _haversine_m(old_node["latitude"], old_node["longitude"], new_node["latitude"], new_node["longitude"])
            _audit(cursor, "UPDATE", "NODE", node_id, new_node["name"],
                   _summarize_changes(old_node["type"].lower(), new_node["name"], changes, moved,
                                      f" ({snapped} kabel ikut disesuaikan)" if snapped else ""), changes)
    print(f"[SUCCESS] Node ID {node_id} berhasil di-update")
    return {"message": "Node berhasil diperbarui"}


def _snap_cables_to_node(cursor, node_id: int):
    """Node dipindah -> ujung kabel yang terhubung (from/to) ikut pindah supaya jalur tetap tersambung."""
    n = cursor.execute("SELECT latitude, longitude FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if not n or n["latitude"] is None or n["longitude"] is None:
        return 0
    point = [n["longitude"], n["latitude"]]
    snapped = 0
    for cab in cursor.execute(
            "SELECT id, geojson_geometry, from_node_id, to_node_id FROM cables "
            "WHERE from_node_id = ? OR to_node_id = ?", (node_id, node_id)).fetchall():
        try:
            geom = json.loads(cab["geojson_geometry"])
            coords = geom["coordinates"]
        except (TypeError, ValueError, KeyError):
            continue
        if len(coords) < 2:
            continue
        if cab["from_node_id"] == node_id:
            coords[0] = point
        if cab["to_node_id"] == node_id:
            coords[-1] = point
        geom["coordinates"] = coords
        cursor.execute("UPDATE cables SET geojson_geometry = ? WHERE id = ?", (json.dumps(geom), cab["id"]))
        snapped += 1
    return snapped


# 9. DELETE NODE
@app.delete("/api/nodes/{node_id}")
def delete_node(node_id: int):
    with db() as conn:
        cursor = conn.cursor()
        if not _exists(cursor, "nodes", node_id):
            raise HTTPException(status_code=404, detail="Node tidak ditemukan")

        snap = _snapshot_node_for_delete(cursor, node_id)
        _audit(cursor, "DELETE", "NODE", node_id, snap["row"]["name"],
               f"Hapus {snap['row']['type'].lower()} {snap['row']['name']} "
               f"({len(snap['refs']['connections'])} sambungan ikut terhapus)", snapshot=snap)

        # Bersihkan semua referensi ke node ini
        cursor.execute(
            "DELETE FROM core_connections WHERE (from_asset_type = 'NODE' AND from_asset_id = ?) "
            "OR (to_asset_type = 'NODE' AND to_asset_id = ?)", (node_id, node_id))
        cursor.execute("UPDATE nodes SET parent_node_id = NULL WHERE parent_node_id = ?", (node_id,))
        cursor.execute("UPDATE cables SET from_node_id = NULL WHERE from_node_id = ?", (node_id,))
        cursor.execute("UPDATE cables SET to_node_id = NULL WHERE to_node_id = ?", (node_id,))
        cursor.execute("UPDATE incidents SET linked_node_id = NULL WHERE linked_node_id = ?", (node_id,))
        cursor.execute("DELETE FROM incident_impacts WHERE asset_type = 'NODE' AND asset_id = ?", (node_id,))
        cursor.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
    print(f"[SUCCESS] Node ID {node_id} berhasil dihapus")
    return {"message": "Node berhasil dihapus"}


# 10. UPDATE CABLE (PUT)
@app.put("/api/cables/{cable_id}")
def update_cable(cable_id: int, payload: CableUpdate):
    data = _fields_set(payload)
    _check_status(data.get("status"))
    _check_installation(data.get("installation"))
    if "installation" in data and not data["installation"]:
        data["installation"] = None
    if data.get("parent_cable_id") == cable_id:
        raise HTTPException(status_code=400, detail="Kabel tidak boleh menjadi parent dirinya sendiri")

    with db() as conn:
        cursor = conn.cursor()
        if not _exists(cursor, "cables", cable_id):
            raise HTTPException(status_code=404, detail="Kabel tidak ditemukan")
        _check_refs(
            cursor,
            **{"Parent kabel": ("cables", data.get("parent_cable_id")),
               "Node asal": ("nodes", data.get("from_node_id")),
               "Node tujuan": ("nodes", data.get("to_node_id"))},
        )
        if data.get("parent_cable_id") is not None and _would_cycle(
                cursor, "cables", "parent_cable_id", cable_id, data["parent_cable_id"]):
            raise HTTPException(status_code=400, detail="Parent kabel membentuk lingkaran topologi")
        if data.get("capacity"):
            _check_capacity_fits(cursor, "CABLE", cable_id, None, data["capacity"])

        old_cab = _row_dict(cursor.execute("SELECT * FROM cables WHERE id = ?", (cable_id,)).fetchone())
        sets, values = _build_update(
            "cables", cable_id, data,
            ["name", "type", "status", "cluster", "area", "city", "capacity", "core_data"],
            ["installation", "parent_cable_id", "from_node_id", "to_node_id"],
        )
        if data.get("coordinates") is not None:
            _check_coordinates(data["coordinates"])
            sets.append("geojson_geometry = ?")
            values.append(json.dumps({"type": "LineString", "coordinates": data["coordinates"]}))
        if sets:
            cursor.execute(f"UPDATE cables SET {', '.join(sets)} WHERE id = ?", (*values, cable_id))
        new_cab = _row_dict(cursor.execute("SELECT * FROM cables WHERE id = ?", (cable_id,)).fetchone())
        changes = _diff_rows(cursor, old_cab, new_cab,
                             ["name", "type", "status", "cluster", "area", "city", "capacity", "core_data",
                              "installation", "parent_cable_id", "from_node_id", "to_node_id"])
        extra = ""
        if old_cab["geojson_geometry"] != new_cab["geojson_geometry"]:
            n_old = len(_cable_coords(old_cab) or [])
            n_new = len(_cable_coords(new_cab) or [])
            changes["geometry"] = [f"{n_old} titik", f"{n_new} titik"]
            extra = f" (geometri jalur diubah: {n_old} -> {n_new} titik)"
        if changes:
            _audit(cursor, "UPDATE", "CABLE", cable_id, new_cab["name"],
                   _summarize_changes("kabel", new_cab["name"], {k: v for k, v in changes.items() if k != "geometry"},
                                      None, extra), changes)
    print(f"[SUCCESS] Cable ID {cable_id} berhasil di-update")
    return {"message": "Kabel berhasil diperbarui"}


# 11. DELETE CABLE
@app.delete("/api/cables/{cable_id}")
def delete_cable(cable_id: int):
    with db() as conn:
        cursor = conn.cursor()
        if not _exists(cursor, "cables", cable_id):
            raise HTTPException(status_code=404, detail="Kabel tidak ditemukan")

        snap = _snapshot_cable_for_delete(cursor, cable_id)
        _audit(cursor, "DELETE", "CABLE", cable_id, snap["row"]["name"],
               f"Hapus kabel {snap['row']['type']} {snap['row']['name']} "
               f"({len(snap['refs']['connections']) + len(snap['refs']['via'])} sambungan terdampak)", snapshot=snap)

        cursor.execute(
            "DELETE FROM core_connections WHERE (from_asset_type = 'CABLE' AND from_asset_id = ?) "
            "OR (to_asset_type = 'CABLE' AND to_asset_id = ?)", (cable_id, cable_id))
        cursor.execute(
            "UPDATE core_connections SET via_cable_id = NULL, via_core = NULL WHERE via_cable_id = ?",
            (cable_id,))
        cursor.execute("UPDATE cables SET parent_cable_id = NULL WHERE parent_cable_id = ?", (cable_id,))
        cursor.execute("UPDATE nodes SET upstream_cable_id = NULL WHERE upstream_cable_id = ?", (cable_id,))
        cursor.execute("UPDATE incidents SET linked_cable_id = NULL WHERE linked_cable_id = ?", (cable_id,))
        cursor.execute("DELETE FROM incident_impacts WHERE asset_type = 'CABLE' AND asset_id = ?", (cable_id,))
        cursor.execute("DELETE FROM cables WHERE id = ?", (cable_id,))
    print(f"[SUCCESS] Cable ID {cable_id} berhasil dihapus")
    return {"message": "Kabel berhasil dihapus"}


# 11b. PEMAKAIAN CORE SEBUAH KABEL
@app.get("/api/cables/{cable_id}/core-usage")
def get_cable_core_usage(cable_id: int):
    """Status tiap core milik kabel: terpakai oleh sambungan mana (via kabel ini atau sambungan langsung)."""
    with db() as conn:
        cab = conn.execute("SELECT id, name, capacity FROM cables WHERE id = ?", (cable_id,)).fetchone()
        if not cab:
            raise HTTPException(status_code=404, detail="Kabel tidak ditemukan")
        rows = conn.execute(
            """SELECT id, from_asset_type, from_asset_id, from_port_core,
                      to_asset_type, to_asset_id, to_port_core, via_cable_id, via_core
               FROM core_connections
               WHERE via_cable_id = ?
                  OR (from_asset_type = 'CABLE' AND from_asset_id = ?)
                  OR (to_asset_type = 'CABLE' AND to_asset_id = ?)""",
            (cable_id, cable_id, cable_id)).fetchall()

    used = {}
    for r in rows:
        if r["via_cable_id"] == cable_id and r["via_core"]:
            used[r["via_core"]] = r["id"]
        if r["from_asset_type"] == "CABLE" and r["from_asset_id"] == cable_id and r["from_port_core"]:
            used[r["from_port_core"]] = r["id"]
        if r["to_asset_type"] == "CABLE" and r["to_asset_id"] == cable_id and r["to_port_core"]:
            used[r["to_port_core"]] = r["id"]

    total = _core_total(cab["capacity"])
    cores = [{"core": lbl, "used": lbl in used, "connection_id": used.get(lbl)}
             for lbl in _core_labels(total)]
    used_count = sum(1 for c in cores if c["used"])
    return {"cable_id": cable_id, "name": cab["name"], "total": total,
            "used": used_count, "available": total - used_count, "cores": cores}


# 12. GET CORE CONNECTIONS
@app.get("/api/connections")
def get_connections(asset_type: Optional[str] = None, asset_id: Optional[int] = None):
    query = """
        SELECT c.*, cb.name AS via_cable_name
        FROM core_connections c
        LEFT JOIN cables cb ON c.via_cable_id = cb.id
    """
    with db() as conn:
        if asset_type and asset_id is not None:
            asset_type = asset_type.upper()
            query += """
                WHERE (c.from_asset_type = ? AND c.from_asset_id = ?)
                   OR (c.to_asset_type = ? AND c.to_asset_id = ?)
            """
            params = [asset_type, asset_id, asset_type, asset_id]
            if asset_type == "CABLE":
                # sambungan yang MELEWATI kabel ini juga milik kabel ini
                query += " OR c.via_cable_id = ?"
                params.append(asset_id)
            rows = conn.execute(query, params).fetchall()
        else:
            rows = conn.execute(query).fetchall()
    return [dict(r) for r in rows]


# 13. CREATE CORE CONNECTION
@app.post("/api/connections")
def create_connection(payload: CoreConnectionSchema):
    from_type = payload.from_asset_type.upper()
    to_type = payload.to_asset_type.upper()
    if from_type not in ("NODE", "CABLE") or to_type not in ("NODE", "CABLE"):
        raise HTTPException(status_code=400, detail="Tipe aset harus NODE atau CABLE")
    if from_type == to_type and payload.from_asset_id == payload.to_asset_id:
        raise HTTPException(status_code=400, detail="Aset asal dan tujuan tidak boleh sama")

    with db() as conn:
        cursor = conn.cursor()
        _check_refs(
            cursor,
            **{"Aset asal": (_table_of(from_type), payload.from_asset_id),
               "Aset tujuan": (_table_of(to_type), payload.to_asset_id),
               "Kabel penghubung": ("cables", payload.via_cable_id)},
        )
        for side, a_type, a_id, port in (
                ("asal", from_type, payload.from_asset_id, payload.from_port_core),
                ("tujuan", to_type, payload.to_asset_id, payload.to_port_core)):
            row = cursor.execute(
                f"SELECT name, {'type' if a_type == 'NODE' else 'NULL AS type'}, capacity "
                f"FROM {_table_of(a_type)} WHERE id = ?", (a_id,)).fetchone()
            if port not in _port_labels(a_type, row["type"], row["capacity"]):
                raise HTTPException(
                    status_code=400,
                    detail=f"Port/core '{port}' tidak ada pada aset {side} {row['name']}")
            if port in _used_ports(cursor, a_type, a_id):
                raise HTTPException(
                    status_code=409,
                    detail=f"Port/core '{port}' pada {row['name']} sudah dipakai sambungan lain")
        dup = cursor.execute(
            """SELECT 1 FROM core_connections
               WHERE from_asset_type = ? AND from_asset_id = ? AND from_port_core = ?
                 AND to_asset_type = ? AND to_asset_id = ? AND to_port_core = ?""",
            (from_type, payload.from_asset_id, payload.from_port_core,
             to_type, payload.to_asset_id, payload.to_port_core),
        ).fetchone()
        if dup:
            raise HTTPException(status_code=409, detail="Sambungan yang sama sudah ada")

        via_core = (payload.via_core or "").strip() or None
        if payload.via_cable_id is not None:
            if not via_core:
                raise HTTPException(status_code=400, detail="Core kabel media wajib dipilih")
            cab = cursor.execute("SELECT name, capacity FROM cables WHERE id = ?",
                                 (payload.via_cable_id,)).fetchone()
            total = _core_total(cab["capacity"])
            if via_core not in _core_labels(total):
                raise HTTPException(
                    status_code=400,
                    detail=f"Core '{via_core}' tidak ada pada kabel {cab['name']} ({total} core)")
            used = cursor.execute(
                "SELECT 1 FROM core_connections WHERE via_cable_id = ? AND via_core = ?",
                (payload.via_cable_id, via_core)).fetchone()
            if used:
                raise HTTPException(
                    status_code=409,
                    detail=f"Core '{via_core}' pada kabel {cab['name']} sudah dipakai sambungan lain")
        else:
            via_core = None

        cursor.execute("""
            INSERT INTO core_connections
            (from_asset_type, from_asset_id, from_port_core, to_asset_type, to_asset_id, to_port_core,
             via_cable_id, via_core, status, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            from_type, payload.from_asset_id, payload.from_port_core,
            to_type, payload.to_asset_id, payload.to_port_core,
            payload.via_cable_id, via_core, payload.status, payload.notes,
        ))
        new_id = cursor.lastrowid
        crow = cursor.execute("SELECT * FROM core_connections WHERE id = ?", (new_id,)).fetchone()
        lbl = _conn_label(cursor, crow)
        _audit(cursor, "CONNECT", "CONNECTION", new_id, lbl, f"Sambung core: {lbl}", snapshot=_row_dict(crow))
    return {"message": "Sambungan core berhasil disimpan", "id": new_id}


# 14. DELETE CORE CONNECTION
@app.delete("/api/connections/{connection_id}")
def delete_connection(connection_id: int):
    """Putus/hapus sambungan core berdasarkan ID."""
    with db() as conn:
        crow = conn.execute("SELECT * FROM core_connections WHERE id = ?", (connection_id,)).fetchone()
        if not crow:
            raise HTTPException(status_code=404, detail="Sambungan tidak ditemukan")
        cur = conn.cursor()
        lbl = _conn_label(cur, crow)
        _audit(cur, "DISCONNECT", "CONNECTION", connection_id, lbl, f"Putus sambungan core: {lbl}",
               snapshot=_row_dict(crow))
        cur.execute("DELETE FROM core_connections WHERE id = ?", (connection_id,))
    return {"message": "Sambungan core berhasil dihapus"}


# 14b. TRACE JALUR (hulu / hilir) BERDASARKAN SAMBUNGAN CORE
def _trace_graph(cursor):
    names = {}
    for r in cursor.execute("SELECT id, name, type FROM nodes").fetchall():
        names[("NODE", r["id"])] = (r["name"], r["type"])
    for r in cursor.execute("SELECT id, name, type FROM cables").fetchall():
        names[("CABLE", r["id"])] = (r["name"], r["type"])
    conns = [dict(r) for r in cursor.execute(
        "SELECT c.*, cb.name AS via_cable_name FROM core_connections c "
        "LEFT JOIN cables cb ON cb.id = c.via_cable_id ORDER BY c.id").fetchall()]
    return names, conns


def _trace_hop(names, c, level):
    def end(t, i, port):
        n = names.get((t, i), (f"{t} #{i}", None))
        return {"type": t, "id": i, "name": n[0], "asset_type": n[1], "port": port}
    via = None
    if c.get("via_cable_id") is not None:
        via = {"cable_id": c["via_cable_id"], "name": c.get("via_cable_name"), "core": c.get("via_core")}
    return {"level": level, "connection_id": c["id"], "status": c.get("status"),
            "from": end(c["from_asset_type"], c["from_asset_id"], c["from_port_core"]),
            "to": end(c["to_asset_type"], c["to_asset_id"], c["to_port_core"]),
            "via": via}


def _customers_below(names, conns, start_key):
    """Himpunan PELANGGAN yang dapat dicapai ke arah hilir dari start_key (termasuk start itu sendiri)."""
    seen, out, queue = {start_key}, set(), deque([start_key])
    while queue:
        key = queue.popleft()
        if names.get(key, (None, None))[1] == "PELANGGAN":
            out.add(key)
        for c in conns:
            if (c["from_asset_type"], c["from_asset_id"]) == key:
                nxt = (c["to_asset_type"], c["to_asset_id"])
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
    return out


@app.get("/api/trace")
def trace_asset(asset_type: str, asset_id: int):
    """
    Jalur sebuah aset berdasarkan sambungan core:
      upstream   = rantai ke arah POP (level 1 = tetangga terdekat)
      downstream = rantai ke arah pelanggan, beserta jumlah pelanggan di bawah tiap hop
      through    = (khusus kabel) sambungan yang MELEWATI kabel ini, per core, beserta dampaknya
    """
    asset_type = asset_type.upper()
    if asset_type not in ("NODE", "CABLE"):
        raise HTTPException(status_code=400, detail="Tipe aset harus NODE atau CABLE")
    with db() as conn:
        cursor = conn.cursor()
        if not _exists(cursor, _table_of(asset_type), asset_id):
            raise HTTPException(status_code=404, detail="Aset tidak ditemukan")
        names, conns = _trace_graph(cursor)

    start = (asset_type, asset_id)

    def walk(direction):
        hops, seen, queue = [], {start}, deque([(start, 1)])
        used_conn = set()
        while queue:
            key, level = queue.popleft()
            for c in conns:
                if c["id"] in used_conn:
                    continue
                here = (c["to_asset_type"], c["to_asset_id"]) if direction == "up" \
                    else (c["from_asset_type"], c["from_asset_id"])
                if here != key:
                    continue
                used_conn.add(c["id"])
                hop = _trace_hop(names, c, level)
                nxt = (c["from_asset_type"], c["from_asset_id"]) if direction == "up" \
                    else (c["to_asset_type"], c["to_asset_id"])
                if direction == "down":
                    cust = _customers_below(names, conns, nxt)
                    hop["customers_below"] = len(cust)
                hops.append(hop)
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append((nxt, level + 1))
        return hops

    upstream, downstream = walk("up"), walk("down")

    through = []
    if asset_type == "CABLE":
        for c in conns:
            if c.get("via_cable_id") == asset_id:
                hop = _trace_hop(names, c, 1)
                cust = _customers_below(names, conns, (c["to_asset_type"], c["to_asset_id"]))
                hop["customers_below"] = len(cust)
                hop["customers"] = sorted(names[k][0] for k in cust)
                through.append(hop)

    reach = set()
    for h in downstream:
        reach |= _customers_below(names, conns, (h["to"]["type"], h["to"]["id"]))
    for h in through:
        reach |= _customers_below(names, conns, (h["to"]["type"], h["to"]["id"]))
    customers = [{"type": k[0], "id": k[1], "name": names[k][0]} for k in sorted(reach, key=lambda k: names[k][0])]

    name, a_type = names[start]
    return {
        "asset": {"type": asset_type, "id": asset_id, "name": name, "asset_type": a_type},
        "upstream": upstream, "downstream": downstream, "through": through,
        "customers": customers,
        "summary": {"upstream_hops": len(upstream), "downstream_hops": len(downstream),
                    "through_cores": len(through), "customers_affected": len(customers)},
    }


# 15. GET NODE PORT SUMMARY
@app.get("/api/nodes/{node_id}/port-summary")
def get_node_port_summary(node_id: int):
    with db() as conn:
        connected_count = conn.execute("""
            SELECT COUNT(*) FROM core_connections
            WHERE (from_asset_type = 'NODE' AND from_asset_id = ?)
               OR (to_asset_type = 'NODE' AND to_asset_id = ?)
        """, (node_id, node_id)).fetchone()[0]
    return {"node_id": node_id, "used_ports": connected_count}


# 16. INCIDENTS MANAGEMENT
@app.get("/api/incidents")
def get_incidents():
    with db() as conn:
        incidents = conn.execute("SELECT * FROM incidents").fetchall()
        repair_node = {}
        for r in conn.execute(
                "SELECT incident_id, node_id FROM incident_repairs WHERE node_id IS NOT NULL AND undone_at IS NULL ORDER BY id").fetchall():
            repair_node[r["incident_id"]] = r["node_id"]

    features = []
    for inc in incidents:
        item = _stamp(dict(inc), "reported_at", "resolved_at")
        item["repair_node_id"] = repair_node.get(item["id"])
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [item["longitude"], item["latitude"]]},
            "properties": item,
        })
    return {"type": "FeatureCollection", "features": features}


def _incident_location(cursor, inc) -> dict:
    """Posisi gangguan pada kabel + jarak ke ujung hulu/hilir (acuan OTDR bagi tim lapangan)."""
    out = {"cable": None, "node": None}

    def node_name(nid):
        if nid is None:
            return None
        r = cursor.execute("SELECT name FROM nodes WHERE id = ?", (nid,)).fetchone()
        return r["name"] if r else None

    if inc["linked_cable_id"] is not None:
        cab = cursor.execute("SELECT id, name, type, from_node_id, to_node_id FROM cables WHERE id = ?",
                             (inc["linked_cable_id"],)).fetchone()
        if cab:
            pos, length = inc["cable_position_m"], inc["cable_length_m"]
            up_id = inc["cable_from_node_id"] if inc["cable_from_node_id"] is not None else cab["from_node_id"]
            down_id = inc["cable_to_node_id"] if inc["cable_to_node_id"] is not None else cab["to_node_id"]
            out["cable"] = {
                "id": cab["id"], "name": cab["name"], "type": cab["type"],
                "position_m": pos, "length_m": length,
                "upstream": {"node_id": up_id, "name": node_name(up_id),
                             "distance_m": round(pos, 1) if pos is not None else None},
                "downstream": {"node_id": down_id, "name": node_name(down_id),
                               "distance_m": round(length - pos, 1) if pos is not None and length else None},
            }
    if inc["linked_node_id"] is not None:
        n = cursor.execute("SELECT id, name, type FROM nodes WHERE id = ?", (inc["linked_node_id"],)).fetchone()
        if n:
            out["node"] = {"id": n["id"], "name": n["name"], "type": n["type"]}
    return out


@app.post("/api/incidents/locate")
def locate_incident_point(payload: LocateRequest):
    """Kabel/node terdekat dari sebuah titik -> dipakai form insiden untuk auto-link & menempel ke jalur."""
    with db() as conn:
        return _locate(conn.cursor(), payload.latitude, payload.longitude)


@app.post("/api/incidents")
def create_incident(inc: IncidentCreate):
    status = inc.status or "Open"
    _check_status(status, INCIDENT_STATUSES)

    with db() as conn:
        cursor = conn.cursor()
        _check_refs(
            cursor,
            **{"Kabel terdampak": ("cables", inc.linked_cable_id),
               "Node terdampak": ("nodes", inc.linked_node_id)},
        )
        ticket = (inc.ticket_number or "").strip()
        if not ticket:
            raise HTTPException(status_code=400, detail="Nomor tiket wajib diisi")
        if cursor.execute("SELECT 1 FROM incidents WHERE ticket_number = ?", (ticket,)).fetchone():
            raise HTTPException(status_code=409, detail=f"Nomor tiket '{ticket}' sudah dipakai")

        cable_id, node_id = inc.linked_cable_id, inc.linked_node_id
        lat, lng = inc.latitude, inc.longitude
        notes = []

        # Tidak memilih aset -> cari otomatis berdasarkan lokasi titik gangguan
        if cable_id is None and node_id is None:
            loc = _locate(cursor, lat, lng)
            if inc.incident_type in CABLE_INCIDENT_TYPES and loc["cable"]:
                cable_id = loc["cable"]["id"]
                notes.append(f"Auto-link ke kabel {loc['cable']['name']} (jarak {loc['cable']['offset_m']} m)")
            elif inc.incident_type not in CABLE_INCIDENT_TYPES and loc["node"]:
                node_id = loc["node"]["id"]
                notes.append(f"Auto-link ke node {loc['node']['name']} (jarak {loc['node']['distance_m']} m)")

        # Titik gangguan ditempelkan ke jalur kabel supaya akurat & bisa dihitung jaraknya
        pos = length = None
        cab_from = cab_to = None
        if cable_id is not None:
            cab = cursor.execute("SELECT * FROM cables WHERE id = ?", (cable_id,)).fetchone()
            cab_from, cab_to = cab["from_node_id"], cab["to_node_id"]
            coords = _cable_coords(cab)
            if coords:
                sn = _snap_to_polyline(coords, lat, lng)
                if sn["offset_m"] > 0.5:
                    notes.append(f"Titik ditempelkan ke jalur kabel (bergeser {sn['offset_m']:.1f} m)")
                lat, lng = sn["point"][1], sn["point"][0]
                pos, length = sn["along_m"], sn["total_m"]

        cores_json = None
        if inc.affected_cores:
            if cable_id is None:
                raise HTTPException(status_code=400, detail="Core terdampak hanya berlaku untuk insiden pada kabel")
            cab = cursor.execute("SELECT capacity FROM cables WHERE id = ?", (cable_id,)).fetchone()
            valid = _core_labels(_core_total(cab["capacity"]))
            labels = []
            for n in inc.affected_cores:
                if n < 1 or n > len(valid):
                    raise HTTPException(status_code=400, detail=f"Core {n} tidak ada pada kabel (1-{len(valid)})")
                labels.append(valid[n - 1])
            cores_json = json.dumps(labels)

        cursor.execute(
            """INSERT INTO incidents (
                   ticket_number, title, severity, incident_type, status,
                   description, latitude, longitude, cluster, area, city,
                   linked_cable_id, linked_node_id, cable_position_m, cable_length_m,
                   affected_cores, source, reporter, cable_from_node_id, cable_to_node_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'MANUAL', ?, ?, ?)""",
            (ticket, inc.title, inc.severity, inc.incident_type, status,
             inc.description or "", lat, lng,
             inc.cluster or "EKO", inc.area or "BANJARMASIN", inc.city or "Kota Banjarmasin",
             cable_id, node_id, pos, length, cores_json, inc.reporter, cab_from, cab_to))
        last_id = cursor.lastrowid
        _log_event(cursor, last_id, "CREATED",
                   f"Tiket {ticket} dibuat ({inc.incident_type}, {inc.severity})",
                   {"notes": notes, "affected_cores": json.loads(cores_json) if cores_json else None},
                   inc.reporter)
        impact = None
        if status in ACTIVE_IMPACT_STATUSES:
            res = _apply_and_log(cursor, last_id, "Dampak dihitung", inc.reporter)
            impact = res[2] if res else None
        _audit(cursor, "CREATE", "INCIDENT", last_id, ticket,
               f"Tiket {ticket} dibuat: {inc.title} ({inc.incident_type}, {inc.severity})")
        row = cursor.execute("SELECT * FROM incidents WHERE id = ?", (last_id,)).fetchone()
        location = _incident_location(cursor, row)
    return {"message": "Incident berhasil dicatat", "id": last_id, "ticket_number": ticket,
            "linked_cable_id": cable_id, "linked_node_id": node_id, "notes": notes,
            "location": location, "impact": impact}


@app.get("/api/incidents/{incident_id}")
def get_incident_detail(incident_id: int):
    """Detail tiket: lokasi pada kabel, dampak (hulu/hilir/pelanggan), perbaikan, dan riwayat lengkap."""
    with db() as conn:
        cursor = conn.cursor()
        inc = cursor.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        if not inc:
            raise HTTPException(status_code=404, detail="Incident tidak ditemukan")

        rows = cursor.execute(
            "SELECT asset_type, asset_id FROM incident_impacts WHERE incident_id = ?", (incident_id,)).fetchall()
        events = [dict(e) for e in cursor.execute(
            "SELECT * FROM incident_events WHERE incident_id = ? ORDER BY id", (incident_id,)).fetchall()]
        if rows:
            impact = _describe_assets(cursor, {r["asset_id"] for r in rows if r["asset_type"] == "NODE"},
                                      {r["asset_id"] for r in rows if r["asset_type"] == "CABLE"})
            impact["live"] = True
        else:
            impact = {"nodes": [], "cables": [], "customers": [],
                      "counts": {"nodes": 0, "cables": 0, "customers": 0, "odp": 0}, "live": False}
            for e in reversed(events):   # tiket sudah pulih: tampilkan dampak terakhir dari riwayat
                if e["event_type"] == "IMPACT_APPLIED" and e["data"]:
                    impact = json.loads(e["data"])
                    impact["live"] = False
                    break
        for e in events:
            e["data"] = json.loads(e["data"]) if e["data"] and e["event_type"] != "IMPACT_APPLIED" else None
            _stamp(e, "created_at")
        repairs = []
        for r in cursor.execute("SELECT * FROM incident_repairs WHERE incident_id = ? ORDER BY id",
                                (incident_id,)).fetchall():
            item = _stamp(dict(r), "created_at", "undone_at")
            chk = _undo_check(cursor, incident_id, r["id"])
            item["can_undo"], item["undo_reason"] = chk["ok"], chk["reason"]
            repairs.append(item)
        location = _incident_location(cursor, inc)
        data = _stamp(dict(inc), "reported_at", "resolved_at")
    return {"incident": data, "location": location, "impact": impact, "repairs": repairs, "events": events}


@app.post("/api/incidents/analyze-impact")
def analyze_incident_impact(incident_id: int):
    with db() as conn:
        res = _apply_and_log(conn.cursor(), incident_id, "Dampak dihitung ulang")
    if res is None:
        raise HTTPException(status_code=404, detail="Incident tidak ditemukan")
    aff_cables, aff_nodes, summary = res
    return {"affected_nodes": sorted(aff_nodes), "affected_cables": sorted(aff_cables), "impact": summary}


@app.post("/api/incidents/{incident_id}/events")
def add_incident_note(incident_id: int, payload: EventCreate):
    """Catatan lapangan / update progres pada tiket."""
    msg = (payload.message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="Catatan tidak boleh kosong")
    with db() as conn:
        cursor = conn.cursor()
        if not _exists(cursor, "incidents", incident_id):
            raise HTTPException(status_code=404, detail="Incident tidak ditemukan")
        _log_event(cursor, incident_id, "NOTE", msg, None, payload.actor)
    return {"message": "Catatan ditambahkan"}


@app.post("/api/incidents/{incident_id}/repairs")
def add_incident_repair(incident_id: int, payload: RepairCreate):
    """
    Catat perbaikan lapangan. ADD_CLOSURE / EXTRA_JOINT menyisipkan closure/joint di titik gangguan:
    kabel dipecah, topologi & sambungan core ikut diperbarui. TEMPORARY -> status 'Temporary Fix'
    (layanan pulih, tiket tetap terbuka); PERMANENT -> 'Resolved'.
    """
    kind, action = payload.kind.upper(), payload.action.upper()
    if kind not in ("TEMPORARY", "PERMANENT"):
        raise HTTPException(status_code=400, detail="kind harus TEMPORARY atau PERMANENT")
    if action not in ("ADD_CLOSURE", "EXTRA_JOINT", "OTHER"):
        raise HTTPException(status_code=400, detail="action harus ADD_CLOSURE, EXTRA_JOINT, atau OTHER")

    with db() as conn:
        cursor = conn.cursor()
        inc = cursor.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        if not inc:
            raise HTTPException(status_code=404, detail="Incident tidak ditemukan")
        if inc["status"] == "Resolved":
            raise HTTPException(status_code=409, detail="Tiket sudah Resolved; buka kembali sebelum menambah perbaikan")

        result = {"node_id": None, "cable_a_id": None, "cable_b_id": None, "position_m": None}
        reused = False
        if action in ("ADD_CLOSURE", "EXTRA_JOINT"):
            if inc["linked_cable_id"] is None:
                raise HTTPException(
                    status_code=400,
                    detail="Tiket belum terkait kabel; closure/joint hanya untuk gangguan di jalur kabel")
            lat = payload.latitude if payload.latitude is not None else inc["latitude"]
            lng = payload.longitude if payload.longitude is not None else inc["longitude"]
            kind_label = "joint" if action == "EXTRA_JOINT" else "closure"

            # Sudah ada closure di titik yang sama (mis. permanen setelah sementara) -> pakai ulang
            near = None
            for n in cursor.execute(
                    "SELECT id, name, latitude, longitude FROM nodes WHERE type = 'CLOSURE'").fetchall():
                if n["latitude"] is None:
                    continue
                d = _haversine_m(lat, lng, n["latitude"], n["longitude"])
                if d <= SAME_POINT_M and (near is None or d < near[0]):
                    near = (d, n)
            if near:
                reused = True
                result["node_id"] = near[1]["id"]
                cursor.execute(
                    "UPDATE nodes SET spec_data = ? WHERE id = ?",
                    (json.dumps({"kind": kind_label, "incident_id": incident_id,
                                 "repair": kind, "upgraded": True}, ensure_ascii=False), near[1]["id"]))
            else:
                prefix = "JT" if action == "EXTRA_JOINT" else "JC"
                name = (payload.name or "").strip() or f"{prefix}-{inc['ticket_number']}"
                result = _split_cable_at(
                    cursor, inc["linked_cable_id"], lat, lng, name,
                    {"kind": kind_label, "incident_id": incident_id,
                     "ticket": inc["ticket_number"], "repair": kind})

        cursor.execute(
            """INSERT INTO incident_repairs (incident_id, kind, action, node_id, cable_a_id, cable_b_id,
                                             position_m, technician, notes, prev_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (incident_id, kind, action, result.get("node_id"), result.get("cable_a_id"),
             result.get("cable_b_id"), result.get("position_m"), payload.technician, payload.notes or "",
             inc["status"]))
        repair_id = cursor.lastrowid

        label = {"ADD_CLOSURE": "Closure baru", "EXTRA_JOINT": "Joint tambahan", "OTHER": "Perbaikan lain"}[action]
        _log_event(cursor, incident_id, "REPAIR",
                   f"Perbaikan {'sementara' if kind == 'TEMPORARY' else 'permanen'}: {label}"
                   + (" (memakai closure yang sudah ada)" if reused else "")
                   + (f". {payload.notes}" if payload.notes else ""),
                   {"repair_id": repair_id, "kind": kind, "action": action, "reused": reused, **result},
                   payload.technician)

        new_status = "Temporary Fix" if kind == "TEMPORARY" else "Resolved"
        if new_status == "Resolved":
            cursor.execute("UPDATE incidents SET status = 'Resolved', resolved_at = CURRENT_TIMESTAMP, "
                           "resolution = ? WHERE id = ?", (payload.notes or label, incident_id))
        else:
            cursor.execute("UPDATE incidents SET status = 'Temporary Fix', resolved_at = NULL WHERE id = ?",
                           (incident_id,))
        _log_event(cursor, incident_id, "STATUS_CHANGED", f"Status tiket: {inc['status']} -> {new_status}",
                   None, payload.technician)
        _apply_and_log(cursor, incident_id, "Layanan dipulihkan", payload.technician)
        _audit(cursor, "REPAIR", "INCIDENT", incident_id, inc["ticket_number"],
               f"Perbaikan {'sementara' if kind == 'TEMPORARY' else 'permanen'} {label}"
               + (" (closure dipakai ulang)" if reused else "")
               + (f", kabel dipecah di {result['position_m']:.0f} m" if result.get("cable_b_id") else "")
               + f" - {payload.technician or 'tanpa nama teknisi'}",
               snapshot={"repair_id": repair_id, **result})
    return {"message": "Perbaikan dicatat", "repair_id": repair_id, "status": new_status,
            "reused_existing_node": reused, **result}


@app.put("/api/incidents/{incident_id}/status")
def update_incident_status(incident_id: int, payload: IncidentStatusUpdate):
    _check_status(payload.status, INCIDENT_STATUSES)
    with db() as conn:
        cursor = conn.cursor()
        old = cursor.execute("SELECT status FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        if not old:
            raise HTTPException(status_code=404, detail="Incident tidak ditemukan")
        if payload.status == "Resolved":
            cursor.execute("UPDATE incidents SET status = ?, resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
                           (payload.status, incident_id))
        else:
            # Dibuka kembali / diubah: resolved_at dikosongkan
            cursor.execute("UPDATE incidents SET status = ?, resolved_at = NULL WHERE id = ?",
                           (payload.status, incident_id))
        _log_event(cursor, incident_id, "STATUS_CHANGED",
                   f"Status tiket: {old['status']} -> {payload.status}"
                   + (f". {payload.note}" if payload.note else ""), None, payload.actor)
        # Resolved/Temporary Fix -> pulihkan aset tiket ini saja; Open/In Progress -> terapkan (ulang) dampaknya
        _apply_and_log(cursor, incident_id, "Dampak disesuaikan", payload.actor)
        tk = cursor.execute("SELECT ticket_number FROM incidents WHERE id = ?", (incident_id,)).fetchone()["ticket_number"]
        _audit(cursor, "STATUS", "INCIDENT", incident_id, tk, f"Status tiket {tk}: {old['status']} -> {payload.status}",
               {"status": [old["status"], payload.status]})
    return {"message": "Status incident diperbarui dan status aset dipulihkan/disesuaikan"}


@app.delete("/api/incidents/{incident_id}")
def delete_incident(incident_id: int):
    with db() as conn:
        cursor = conn.cursor()
        inc = cursor.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        if not inc:
            raise HTTPException(status_code=404, detail="Incident tidak ditemukan")
        release_incident_impact(cursor, incident_id)
        # Riwayat TIDAK ikut dihapus: jejak audit tetap ada beserta cuplikan tiketnya
        _log_event(cursor, incident_id, "DELETED", f"Tiket {inc['ticket_number']} dihapus",
                   {k: inc[k] for k in ("ticket_number", "title", "incident_type", "severity", "status")})
        _audit(cursor, "DELETE", "INCIDENT", incident_id, inc["ticket_number"],
               f"Hapus tiket {inc['ticket_number']}: {inc['title']}", snapshot=_row_dict(inc))
        cursor.execute("DELETE FROM incidents WHERE id = ?", (incident_id,))
    return {"message": "Incident berhasil dihapus dan status aset dipulihkan"}


# --- RIWAYAT PERUBAHAN: LIHAT & PULIHKAN ---
def _pragma_cols(cursor, table):
    return [r[1] for r in cursor.execute(f"PRAGMA table_info({table})").fetchall()]


def _insert_row(cursor, table, row: dict):
    cols = [c for c in _pragma_cols(cursor, table) if c in row]
    cursor.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                   [row[c] for c in cols])


def _snapshot_node_for_delete(cursor, node_id: int) -> dict:
    """Cuplikan node + semua referensi yang akan dibersihkan saat dihapus (untuk pemulihan)."""
    row = _row_dict(cursor.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone())
    ids = lambda sql: [r[0] for r in cursor.execute(sql, (node_id,)).fetchall()]
    return {"row": row, "refs": {
        "children": ids("SELECT id FROM nodes WHERE parent_node_id = ?"),
        "cables_from": ids("SELECT id FROM cables WHERE from_node_id = ?"),
        "cables_to": ids("SELECT id FROM cables WHERE to_node_id = ?"),
        "incidents": ids("SELECT id FROM incidents WHERE linked_node_id = ?"),
        "connections": [dict(r) for r in cursor.execute(
            "SELECT * FROM core_connections WHERE (from_asset_type = 'NODE' AND from_asset_id = ?) "
            "OR (to_asset_type = 'NODE' AND to_asset_id = ?)", (node_id, node_id)).fetchall()],
        "impacts": [dict(r) for r in cursor.execute(
            "SELECT incident_id, prev_status FROM incident_impacts WHERE asset_type = 'NODE' AND asset_id = ?",
            (node_id,)).fetchall()],
    }}


def _snapshot_cable_for_delete(cursor, cable_id: int) -> dict:
    row = _row_dict(cursor.execute("SELECT * FROM cables WHERE id = ?", (cable_id,)).fetchone())
    ids = lambda sql: [r[0] for r in cursor.execute(sql, (cable_id,)).fetchall()]
    return {"row": row, "refs": {
        "child_cables": ids("SELECT id FROM cables WHERE parent_cable_id = ?"),
        "upstream_nodes": ids("SELECT id FROM nodes WHERE upstream_cable_id = ?"),
        "incidents": ids("SELECT id FROM incidents WHERE linked_cable_id = ?"),
        "connections": [dict(r) for r in cursor.execute(
            "SELECT * FROM core_connections WHERE (from_asset_type = 'CABLE' AND from_asset_id = ?) "
            "OR (to_asset_type = 'CABLE' AND to_asset_id = ?)", (cable_id, cable_id)).fetchall()],
        "via": [{"id": r["id"], "via_core": r["via_core"]} for r in cursor.execute(
            "SELECT id, via_core FROM core_connections WHERE via_cable_id = ?", (cable_id,)).fetchall()],
        "impacts": [dict(r) for r in cursor.execute(
            "SELECT incident_id, prev_status FROM incident_impacts WHERE asset_type = 'CABLE' AND asset_id = ?",
            (cable_id,)).fetchall()],
    }}


def _connection_conflict(cursor, r: dict):
    """Alasan sebuah sambungan tidak bisa dipasang kembali (aset hilang / port atau core sudah dipakai); None = aman."""
    for side in ("from", "to"):
        t, i, port = r[f"{side}_asset_type"], r[f"{side}_asset_id"], r[f"{side}_port_core"]
        row = cursor.execute(f"SELECT name, type, capacity FROM {_table_of(t)} WHERE id = ?", (i,)).fetchone() \
            if t in ("NODE", "CABLE") else None
        if not row:
            return f"aset {side} (#{i}) sudah tidak ada"
        if port not in _port_labels(t, row["type"], row["capacity"]):
            return f"port {port} tidak ada pada {row['name']}"
        if port in _used_ports(cursor, t, i):
            return f"port {port} pada {row['name']} sudah dipakai sambungan lain"
    if r.get("via_cable_id") is not None:
        cab = cursor.execute("SELECT name FROM cables WHERE id = ?", (r["via_cable_id"],)).fetchone()
        if not cab:
            return "kabel media sudah tidak ada"
        if cursor.execute("SELECT 1 FROM core_connections WHERE via_cable_id = ? AND via_core = ?",
                          (r["via_cable_id"], r["via_core"])).fetchone():
            return f"core {r['via_core']} pada {cab['name']} sudah dipakai sambungan lain"
    return None


def _restore_connections(cursor, rows):
    restored = skipped = 0
    notes = []
    for r in rows:
        why = _connection_conflict(cursor, r)
        if why:
            skipped += 1
            notes.append(f"sambungan #{r['id']} tidak dipulihkan: {why}")
            continue
        if cursor.execute("SELECT 1 FROM core_connections WHERE id = ?", (r["id"],)).fetchone():
            r = {k: v for k, v in r.items() if k != "id"}
        _insert_row(cursor, "core_connections", r)
        restored += 1
    return restored, skipped, notes


def _reapply_impacts(cursor, impacts):
    for incident_id in sorted({x["incident_id"] for x in impacts}):
        inc = cursor.execute("SELECT status FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        if inc and inc["status"] in ACTIVE_IMPACT_STATUSES:
            _apply_and_log(cursor, incident_id, "Aset dipulihkan dari riwayat")


def _restore_node(cursor, snap) -> dict:
    row, refs = dict(snap["row"]), snap["refs"]
    if _exists(cursor, "nodes", row["id"]):
        raise HTTPException(status_code=409, detail="Node ini sudah ada kembali")
    notes = []
    if row.get("parent_node_id") is not None and not _exists(cursor, "nodes", row["parent_node_id"]):
        row["parent_node_id"] = None
        notes.append("parent node sudah tidak ada, dikosongkan")
    if row.get("upstream_cable_id") is not None and not _exists(cursor, "cables", row["upstream_cable_id"]):
        row["upstream_cable_id"] = None
        notes.append("kabel pemasok sudah tidak ada, dikosongkan")
    if refs["impacts"]:
        row["status"] = refs["impacts"][0]["prev_status"] or "Active"   # status sebelum tertimpa dampak tiket
    _insert_row(cursor, "nodes", row)
    nid = row["id"]
    for cid in refs["children"]:
        cursor.execute("UPDATE nodes SET parent_node_id = ? WHERE id = ? AND parent_node_id IS NULL", (nid, cid))
    for cid in refs["cables_from"]:
        cursor.execute("UPDATE cables SET from_node_id = ? WHERE id = ? AND from_node_id IS NULL", (nid, cid))
    for cid in refs["cables_to"]:
        cursor.execute("UPDATE cables SET to_node_id = ? WHERE id = ? AND to_node_id IS NULL", (nid, cid))
    for iid in refs["incidents"]:
        cursor.execute("UPDATE incidents SET linked_node_id = ? WHERE id = ? AND linked_node_id IS NULL", (nid, iid))
    restored, skipped, cn = _restore_connections(cursor, refs["connections"])
    _reapply_impacts(cursor, refs["impacts"])
    return {"name": row["name"], "connections_restored": restored, "connections_skipped": skipped,
            "notes": notes + cn}


def _restore_cable(cursor, snap) -> dict:
    row, refs = dict(snap["row"]), snap["refs"]
    if _exists(cursor, "cables", row["id"]):
        raise HTTPException(status_code=409, detail="Kabel ini sudah ada kembali")
    notes = []
    for col, table, label in (("parent_cable_id", "cables", "kabel induk"), ("from_node_id", "nodes", "node asal"),
                              ("to_node_id", "nodes", "node tujuan")):
        if row.get(col) is not None and not _exists(cursor, table, row[col]):
            row[col] = None
            notes.append(f"{label} sudah tidak ada, dikosongkan")
    if refs["impacts"]:
        row["status"] = refs["impacts"][0]["prev_status"] or "Active"
    _insert_row(cursor, "cables", row)
    cid = row["id"]
    for x in refs["child_cables"]:
        cursor.execute("UPDATE cables SET parent_cable_id = ? WHERE id = ? AND parent_cable_id IS NULL", (cid, x))
    for x in refs["upstream_nodes"]:
        cursor.execute("UPDATE nodes SET upstream_cable_id = ? WHERE id = ? AND upstream_cable_id IS NULL", (cid, x))
    for x in refs["incidents"]:
        cursor.execute("UPDATE incidents SET linked_cable_id = ? WHERE id = ? AND linked_cable_id IS NULL", (cid, x))
    restored, skipped, cn = _restore_connections(cursor, refs["connections"])
    for v in refs["via"]:
        if v["via_core"] and not cursor.execute(
                "SELECT 1 FROM core_connections WHERE via_cable_id = ? AND via_core = ?", (cid, v["via_core"])).fetchone():
            cursor.execute("UPDATE core_connections SET via_cable_id = ?, via_core = ? "
                           "WHERE id = ? AND via_cable_id IS NULL", (cid, v["via_core"], v["id"]))
            restored += 1
    _reapply_impacts(cursor, refs["impacts"])
    return {"name": row["name"], "connections_restored": restored, "connections_skipped": skipped,
            "notes": notes + cn}


@app.get("/api/audit")
def list_audit(entity_type: str = "ALL", action: str = "ALL", q: str = "", username: str = "",
               entity_id: Optional[int] = None, limit: int = 50, offset: int = 0):
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    where, args = [], []
    if entity_type != "ALL":
        where.append("entity_type = ?")
        args.append(entity_type.upper())
        if entity_id is not None:
            where.append("entity_id = ?")
            args.append(entity_id)
    if action != "ALL":
        where.append("action = ?")
        args.append(action.upper())
    if username:
        where.append("username = ? COLLATE NOCASE")
        args.append(username)
    if q:
        where.append("(entity_name LIKE ? OR summary LIKE ? OR username LIKE ?)")
        args += [f"%{q}%"] * 3
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM audit_log {clause}", args).fetchone()[0]
        rows = conn.execute(f"SELECT * FROM audit_log {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
                            (*args, limit, offset)).fetchall()
        items = []
        for r in rows:
            restorable = False
            if r["restored_at"] is None and r["snapshot"]:
                if r["action"] == "DELETE" and r["entity_type"] in ("NODE", "CABLE"):
                    restorable = not conn.execute(f"SELECT 1 FROM {_table_of(r['entity_type'])} WHERE id = ?",
                                                  (r["entity_id"],)).fetchone()
                elif r["action"] == "DISCONNECT" and r["entity_type"] == "CONNECTION":
                    restorable = True
            items.append({
                "id": r["id"], "ts": _utc_iso(r["ts"]), "username": r["username"], "role": r["role"],
                "action": r["action"], "entity_type": r["entity_type"], "entity_id": r["entity_id"],
                "entity_name": r["entity_name"], "summary": r["summary"],
                "changes": json.loads(r["changes"]) if r["changes"] else None,
                "restorable": restorable, "restored_at": _utc_iso(r["restored_at"]), "ip": r["ip"]})
    return {"total": total, "limit": limit, "offset": offset, "items": items}


@app.post("/api/audit/{audit_id}/restore")
def restore_from_audit(audit_id: int):
    """Pulihkan aset/sambungan yang dihapus berdasarkan cuplikan di riwayat (id asli dipakai kembali)."""
    with db() as conn:
        cursor = conn.cursor()
        a = cursor.execute("SELECT * FROM audit_log WHERE id = ?", (audit_id,)).fetchone()
        if not a:
            raise HTTPException(status_code=404, detail="Entri riwayat tidak ditemukan")
        if a["restored_at"]:
            raise HTTPException(status_code=409, detail="Entri ini sudah pernah dipulihkan")
        snap = json.loads(a["snapshot"]) if a["snapshot"] else None
        et = a["entity_type"]
        if a["action"] == "DELETE" and et == "NODE" and snap:
            res = _restore_node(cursor, snap)
        elif a["action"] == "DELETE" and et == "CABLE" and snap:
            res = _restore_cable(cursor, snap)
        elif a["action"] == "DISCONNECT" and et == "CONNECTION" and snap:
            why = _connection_conflict(cursor, snap)
            if why:
                raise HTTPException(status_code=409, detail=f"Sambungan tidak bisa dipasang kembali: {why}")
            if cursor.execute("SELECT 1 FROM core_connections WHERE id = ?", (snap["id"],)).fetchone():
                snap = {k: v for k, v in snap.items() if k != "id"}
            _insert_row(cursor, "core_connections", snap)
            res = {"name": a["entity_name"], "connections_restored": 1, "connections_skipped": 0, "notes": []}
        else:
            raise HTTPException(status_code=400, detail="Entri ini tidak bisa dipulihkan")
        cursor.execute("UPDATE audit_log SET restored_at = ? WHERE id = ?", (_now_str(), audit_id))
        extra = f" ({res['connections_restored']} sambungan dipasang kembali" + \
                (f", {res['connections_skipped']} dilewati" if res["connections_skipped"] else "") + ")" \
            if et != "CONNECTION" else ""
        _audit(cursor, "RESTORE", et, a["entity_id"], a["entity_name"],
               f"Memulihkan {et.lower()} {a['entity_name']} dari riwayat #{audit_id}{extra}"
               + (". " + "; ".join(res["notes"]) if res["notes"] else ""))
    return {"message": f"{a['entity_name']} berhasil dipulihkan", **res}


# --- BATALKAN PERBAIKAN LAPANGAN (gabungkan kembali kabel yang dipecah) ---
def _polyline_length_m(coords) -> float:
    return sum(_haversine_m(a[1], a[0], b[1], b[0]) for a, b in zip(coords, coords[1:]))


def _undo_check(cursor, incident_id: int, repair_id: int) -> dict:
    """Periksa apakah perbaikan bisa dibatalkan; bila ya, kembalikan rencana penggabungan (split=True)."""
    rep = cursor.execute("SELECT * FROM incident_repairs WHERE id = ? AND incident_id = ?",
                         (repair_id, incident_id)).fetchone()
    if not rep:
        raise HTTPException(status_code=404, detail="Perbaikan tidak ditemukan pada tiket ini")
    out = {"ok": False, "reason": None, "rep": rep, "split": False}
    if rep["undone_at"]:
        out["reason"] = "Perbaikan ini sudah dibatalkan"
        return out
    later = cursor.execute(
        """SELECT r.id, i.ticket_number FROM incident_repairs r JOIN incidents i ON i.id = r.incident_id
           WHERE r.id > ? AND r.undone_at IS NULL
             AND ((r.node_id IS NOT NULL AND r.node_id = ?) OR r.cable_a_id IN (?, ?) OR r.cable_b_id IN (?, ?))
           ORDER BY r.id LIMIT 1""",
        (repair_id, rep["node_id"], rep["cable_a_id"], rep["cable_b_id"], rep["cable_a_id"], rep["cable_b_id"])
    ).fetchone()
    if later:
        out["reason"] = f"Batalkan dulu perbaikan yang lebih baru pada titik/kabel yang sama (tiket {later['ticket_number']})"
        return out
    if not (rep["node_id"] and rep["cable_a_id"] and rep["cable_b_id"]):
        out["ok"] = True              # perbaikan tanpa perubahan peta (lainnya / closure dipakai ulang)
        return out

    A = cursor.execute("SELECT * FROM cables WHERE id = ?", (rep["cable_a_id"],)).fetchone()
    B = cursor.execute("SELECT * FROM cables WHERE id = ?", (rep["cable_b_id"],)).fetchone()
    N = cursor.execute("SELECT * FROM nodes WHERE id = ?", (rep["node_id"],)).fetchone()
    if not (A and B and N):
        out["reason"] = "Closure atau salah satu segmen kabel sudah tidak ada (dihapus manual)"
        return out
    if A["to_node_id"] != N["id"] or B["from_node_id"] != N["id"]:
        out["reason"] = "Susunan kabel di closure sudah diubah; gabungkan manual lewat Edit"
        return out
    other = cursor.execute(
        "SELECT name FROM cables WHERE id NOT IN (?, ?) AND (from_node_id = ? OR to_node_id = ?) LIMIT 1",
        (A["id"], B["id"], N["id"], N["id"])).fetchone()
    if other:
        out["reason"] = f"Kabel lain ({other['name']}) tersambung ke closure ini; pindahkan/hapus dulu"
        return out

    conns = cursor.execute(
        "SELECT * FROM core_connections WHERE (from_asset_type = 'NODE' AND from_asset_id = ?) "
        "OR (to_asset_type = 'NODE' AND to_asset_id = ?)", (N["id"], N["id"])).fetchall()
    r1 = [c for c in conns if c["to_asset_type"] == "NODE" and c["to_asset_id"] == N["id"] and c["via_cable_id"] == A["id"]]
    r2 = [c for c in conns if c["from_asset_type"] == "NODE" and c["from_asset_id"] == N["id"] and c["via_cable_id"] == B["id"]]
    pairs, used2 = [], set()
    for a in r1:
        m = [b for b in r2 if b["from_port_core"] == a["to_port_core"] and b["id"] not in used2]
        if not m:
            out["reason"] = f"Sambungan di port closure {a['to_port_core']} tidak berpasangan; cek sambungannya"
            return out
        if m[0]["via_core"] != a["via_core"]:
            out["reason"] = (f"Core dipindah di closure ({a['via_core']} -> {m[0]['via_core']}); "
                             f"kembalikan sambungan ke core yang sama sebelum membatalkan")
            return out
        used2.add(m[0]["id"])
        pairs.append((a, m[0]))
    if len(conns) != len(r1) + len(r2) or len(used2) != len(r2):
        out["reason"] = "Closure punya sambungan lain di luar hasil perbaikan; putuskan dulu"
        return out
    stray = cursor.execute(
        """SELECT 1 FROM core_connections WHERE via_cable_id = ? AND NOT (from_asset_type = 'NODE' AND from_asset_id = ?)
           LIMIT 1""", (B["id"], N["id"])).fetchone()
    if stray:
        out["reason"] = "Ada sambungan lain yang melewati segmen kabel kedua; pindahkan dulu"
        return out
    out.update(ok=True, split=True, A=A, B=B, N=N, pairs=pairs)
    return out


def _undo_apply(cursor, plan) -> dict:
    A, B, N = plan["A"], plan["B"], plan["N"]
    ca, cb = _cable_coords(A), _cable_coords(B)
    if not ca or not cb:
        raise HTTPException(status_code=409, detail="Geometri kabel tidak valid untuk digabung")
    # Tiket yang dampaknya menyentuh B/closure dipulihkan dulu, lalu dihitung ulang setelah penggabungan
    affected = {r["incident_id"] for r in cursor.execute(
        """SELECT incident_id FROM incident_impacts WHERE (asset_type = 'CABLE' AND asset_id IN (?, ?))
           OR (asset_type = 'NODE' AND asset_id = ?)""", (A["id"], B["id"], N["id"])).fetchall()}
    for iid in affected:
        release_incident_impact(cursor, iid)

    len_a = _polyline_length_m(ca)
    merged = ca[:-1] + cb[1:]
    parent_of_closure = N["parent_node_id"]
    cursor.execute("UPDATE cables SET geojson_geometry = ?, to_node_id = ? WHERE id = ?",
                   (json.dumps({"type": "LineString", "coordinates": merged}), B["to_node_id"], A["id"]))
    cursor.execute("UPDATE nodes SET upstream_cable_id = ? WHERE upstream_cable_id = ?", (A["id"], B["id"]))
    cursor.execute("UPDATE nodes SET parent_node_id = ? WHERE parent_node_id = ?", (parent_of_closure, N["id"]))
    cursor.execute("UPDATE cables SET parent_cable_id = ? WHERE parent_cable_id = ?", (A["id"], B["id"]))
    # Tiket lain yang menempel pada segmen B pindah ke kabel gabungan (jarak dihitung dari ujung asal)
    merged_len = _polyline_length_m(merged)
    cursor.execute(
        """UPDATE incidents SET linked_cable_id = ?, cable_position_m = CASE WHEN cable_position_m IS NULL THEN NULL
                                                                           ELSE cable_position_m + ? END,
                                cable_length_m = ?
           WHERE linked_cable_id = ?""", (A["id"], len_a, merged_len, B["id"]))
    cursor.execute("UPDATE incidents SET linked_node_id = NULL WHERE linked_node_id = ?", (N["id"],))
    for r1, r2 in plan["pairs"]:
        cursor.execute("UPDATE core_connections SET to_asset_type = ?, to_asset_id = ?, to_port_core = ? WHERE id = ?",
                       (r2["to_asset_type"], r2["to_asset_id"], r2["to_port_core"], r1["id"]))
        cursor.execute("DELETE FROM core_connections WHERE id = ?", (r2["id"],))
    cursor.execute("DELETE FROM incident_impacts WHERE (asset_type = 'CABLE' AND asset_id = ?) "
                   "OR (asset_type = 'NODE' AND asset_id = ?)", (B["id"], N["id"]))
    cursor.execute("DELETE FROM cables WHERE id = ?", (B["id"],))
    cursor.execute("DELETE FROM nodes WHERE id = ?", (N["id"],))
    return {"affected": affected, "pairs": len(plan["pairs"]), "removed_node": N["name"], "removed_cable": B["name"],
            "merged_cable": A["name"], "length_m": round(merged_len, 1)}


class UndoRepairRequest(BaseModel):
    reason: Optional[str] = None


@app.post("/api/incidents/{incident_id}/repairs/{repair_id}/undo")
def undo_incident_repair(incident_id: int, repair_id: int, payload: UndoRepairRequest = None):
    """
    Batalkan perbaikan (mis. salah titik): kabel A+B digabung kembali, closure hasil perbaikan dihapus,
    sambungan core dikembalikan seperti sebelumnya, dan tiket kembali ke status sebelum perbaikan.
    """
    reason = ((payload.reason if payload else None) or "").strip()
    with db() as conn:
        cursor = conn.cursor()
        inc = cursor.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        if not inc:
            raise HTTPException(status_code=404, detail="Incident tidak ditemukan")
        plan = _undo_check(cursor, incident_id, repair_id)
        if not plan["ok"]:
            raise HTTPException(status_code=409, detail=plan["reason"])
        rep = plan["rep"]

        merged = None
        snapshot = {"repair": dict(rep)}
        if plan["split"]:
            snapshot.update(node=dict(plan["N"]), cable_b=dict(plan["B"]))
            merged = _undo_apply(cursor, plan)

        cursor.execute("UPDATE incident_repairs SET undone_at = ?, undone_by = ?, undo_reason = ? WHERE id = ?",
                       (_now_str(), _current_username(), reason or None, repair_id))
        prev = rep["prev_status"] if rep["prev_status"] in INCIDENT_STATUSES and rep["prev_status"] != "Resolved" else "Open"
        cursor.execute("UPDATE incidents SET status = ?, resolved_at = NULL, resolution = NULL WHERE id = ?",
                       (prev, incident_id))
        desc = "Perbaikan dibatalkan"
        if merged:
            desc += f": {merged['removed_node']} dihapus, kabel {merged['removed_cable']} digabung kembali ke {merged['merged_cable']}"
        _log_event(cursor, incident_id, "REPAIR_UNDONE", desc + (f". Alasan: {reason}" if reason else ""),
                   {"repair_id": repair_id, **(merged and {k: v for k, v in merged.items() if k != "affected"} or {})})
        _log_event(cursor, incident_id, "STATUS_CHANGED", f"Status tiket: {inc['status']} -> {prev}")
        _audit(cursor, "UNDO_REPAIR", "INCIDENT", incident_id, inc["ticket_number"], desc, snapshot=snapshot)
        for iid in sorted((merged["affected"] if merged else set()) | {incident_id}):
            _apply_and_log(cursor, iid, "Perbaikan dibatalkan")
    return {"message": desc, "status": prev, "merged": bool(merged),
            **({k: v for k, v in merged.items() if k != "affected"} if merged else {})}


# --- MOUNT STATIC FILES (HARUS PALING BAWAH) ---

# =====================================================================
# PENCARIAN TEMPAT (GEOCODING) - seperti kolom cari di peta online
# =====================================================================
# Server meneruskan kueri ke layanan geocoding berbasis OpenStreetMap (Photon, cadangan Nominatim).
# Lewat server agar: tidak terkena batasan CORS, ada cache + pembatasan laju (sesuai kebijakan layanan
# gratis), dan hanya user yang sudah login yang bisa memakainya.
#   NETGIS_GEOCODER      = photon (default) | nominatim | off
#   NETGIS_GEOCODER_URL  = alamat layanan sendiri (mis. Photon/Nominatim self-hosted), opsional
#   NETGIS_GEOCODER_CONTACT = email kontak untuk User-Agent (diminta kebijakan Nominatim), opsional
GEOCODER = os.environ.get("NETGIS_GEOCODER", "photon").strip().lower()
GEOCODER_URL = os.environ.get("NETGIS_GEOCODER_URL", "").strip()
GEOCODER_CONTACT = os.environ.get("NETGIS_GEOCODER_CONTACT", "").strip()
GEOCODE_CACHE_TTL = 3600
GEOCODE_CACHE_MAX = 500
GEOCODE_MIN_INTERVAL = 1.0           # detik antar-panggilan keluar (batas wajar layanan gratis)
GEOCODE_USER_PER_MIN = 40            # batas per user per menit
_geocode_cache = {}                  # kunci -> (waktu, hasil)
_geocode_last_call = [0.0]
_geocode_user_hits = {}              # user_id -> deque[waktu]
_geocode_lock = threading.Lock()


def _geocode_http_get(url: str):
    """Dipisah agar mudah ditiru saat pengujian."""
    ua = "NETGIS-Enterprise/1.0" + (f" ({GEOCODER_CONTACT})" if GEOCODER_CONTACT else "")
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=6) as r:
        return json.loads(r.read().decode("utf-8"))


def _fmt_place_label(p: dict) -> str:
    parts = []
    street = p.get("street")
    if street:
        parts.append(f"{street} {p.get('housenumber')}" if p.get("housenumber") else street)
    for k in ("district", "locality", "city", "county", "state", "country"):
        v = p.get(k)
        if v and v not in parts and v != p.get("name"):
            parts.append(v)
    return ", ".join(parts)


def _normalize_photon(data: dict) -> list:
    out = []
    for f in (data or {}).get("features", []):
        try:
            lng, lat = f["geometry"]["coordinates"][:2]
            p = f.get("properties", {})
            name = p.get("name") or p.get("street") or p.get("city") or p.get("state")
            if not name:
                continue
            ext = p.get("extent")  # [minLon, maxLat, maxLon, minLat]
            bbox = [ext[3], ext[0], ext[1], ext[2]] if ext and len(ext) == 4 else None  # [s, w, n, e]
            out.append({"name": name, "label": _fmt_place_label(p), "kind": p.get("osm_value") or p.get("osm_key") or "place",
                        "lat": float(lat), "lng": float(lng), "bbox": bbox})
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    return out


def _normalize_nominatim(data) -> list:
    out = []
    for r in data or []:
        try:
            disp = r.get("display_name") or ""
            name = r.get("name") or disp.split(",")[0].strip()
            label = ", ".join(x.strip() for x in disp.split(",")[1:4]) if "," in disp else ""
            bb = r.get("boundingbox")  # [s, n, w, e] sebagai string
            bbox = [float(bb[0]), float(bb[2]), float(bb[1]), float(bb[3])] if bb and len(bb) == 4 else None
            out.append({"name": name, "label": label, "kind": r.get("type") or r.get("category") or "place",
                        "lat": float(r["lat"]), "lng": float(r["lon"]), "bbox": bbox})
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    return out


def _geocode_query(q: str, lat, lng, limit: int) -> list:
    qs = urllib.parse.quote(q)
    if GEOCODER == "nominatim":
        base = GEOCODER_URL or "https://nominatim.openstreetmap.org/search"
        url = f"{base}?format=jsonv2&q={qs}&limit={limit}&countrycodes=id&accept-language=id"
        if lat is not None and lng is not None:   # condong ke area peta: kotak ~ +-0.5 derajat sebagai preferensi
            url += f"&viewbox={lng - 0.5},{lat + 0.5},{lng + 0.5},{lat - 0.5}"
        return _normalize_nominatim(_geocode_http_get(url))
    base = GEOCODER_URL or "https://photon.komoot.io/api/"
    url = f"{base}?q={qs}&limit={limit}"
    if lat is not None and lng is not None:
        url += f"&lat={lat}&lon={lng}"
    return _normalize_photon(_geocode_http_get(url))


@app.get("/api/geocode")
def geocode(q: str = "", lat: Optional[float] = None, lng: Optional[float] = None, limit: int = 6):
    """Cari nama tempat/alamat. Selalu 200: bila layanan gagal, 'results' kosong dan 'error' berisi alasannya
    (pencarian aset NETGIS di sisi klien tetap berjalan)."""
    q = (q or "").strip()
    limit = max(1, min(int(limit), 10))
    if len(q) < 3 or len(q) > 120:
        return {"results": [], "provider": GEOCODER, "error": None}
    if GEOCODER in ("off", "", "none"):
        return {"results": [], "provider": "off", "error": "Pencarian tempat dinonaktifkan oleh admin"}
    if lat is not None and not (-90 <= lat <= 90) or lng is not None and not (-180 <= lng <= 180):
        lat = lng = None
    # bulatkan titik bias agar cache efektif (geser peta kecil tidak mengubah kunci)
    blat = round(lat, 1) if lat is not None else None
    blng = round(lng, 1) if lng is not None else None
    key = (GEOCODER, q.lower(), blat, blng, limit)
    now = time.time()
    u = CURRENT_USER.get()
    with _geocode_lock:
        hit = _geocode_cache.get(key)
        if hit and now - hit[0] < GEOCODE_CACHE_TTL:
            return {"results": hit[1], "provider": GEOCODER, "cached": True, "error": None}
        uid = u["id"] if u else 0
        dq = _geocode_user_hits.setdefault(uid, deque())
        while dq and now - dq[0] > 60:
            dq.popleft()
        if len(dq) >= GEOCODE_USER_PER_MIN:
            return {"results": [], "provider": GEOCODER, "error": "Terlalu banyak pencarian, tunggu sebentar"}
        dq.append(now)
        wait = GEOCODE_MIN_INTERVAL - (now - _geocode_last_call[0])
        _geocode_last_call[0] = now + max(wait, 0)
    if wait > 0:
        time.sleep(min(wait, GEOCODE_MIN_INTERVAL))
    try:
        results = _geocode_query(q, blat if blat is not None else lat, blng if blng is not None else lng, limit)
    except Exception as exc:   # jaringan/timeout/format: jangan menjatuhkan pencarian aset
        print(f"[WARN] Geocoding gagal: {exc}")
        return {"results": [], "provider": GEOCODER, "error": "Layanan pencarian tempat tidak dapat dihubungi"}
    with _geocode_lock:
        while len(_geocode_cache) >= GEOCODE_CACHE_MAX:
            _geocode_cache.pop(next(iter(_geocode_cache)))
        _geocode_cache[key] = (now, results)
    return {"results": results, "provider": GEOCODER, "error": None}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")