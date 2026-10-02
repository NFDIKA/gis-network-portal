from collections import deque
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import List, Optional
import base64
import contextvars
import csv
import hashlib
import hmac
import io
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
import xml.etree.ElementTree as ET
import zipfile

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
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
JUNCTION_TYPES = {"CLOSURE", "SLACK"}   # penghubung kabel: port = joint (1 masuk dari hulu + 1 keluar ke hilir)
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

    # 7. Pengaturan aplikasi (mis. aturan perencanaan) dan rencana pasang baru
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT, updated_at DATETIME, updated_by TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, status TEXT DEFAULT 'Draft',          -- Draft / Realized
            origin_type TEXT, origin_id INTEGER, origin_name TEXT,
            dest_lat REAL, dest_lng REAL, dest_name TEXT, installation TEXT,
            summary TEXT,           -- JSON ringkasan (panjang, jenis kabel, jumlah aset)
            result TEXT,            -- JSON hasil perhitungan lengkap (rute, aset, BOQ)
            realized_info TEXT,     -- JSON id aset yang dibuat saat diwujudkan
            created_by TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, realized_at DATETIME
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS khs_items (
            key TEXT PRIMARY KEY,
            code TEXT, jenis TEXT, description TEXT, unit TEXT,
            prices TEXT,             -- JSON {"EKO": [material, jasa], ...}
            sort INTEGER DEFAULT 0, edited INTEGER DEFAULT 0, updated_at DATETIME, updated_by TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS otdr_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cable_id INTEGER, core TEXT, wavelength_nm INTEGER, direction TEXT, measured_at TEXT,
            length_m REAL, total_loss_db REAL, avg_db_km REAL, orl_db REAL,
            expected_db REAL, delta_db REAL, status TEXT, reasons TEXT, events TEXT, notes TEXT,
            source_file TEXT, uploaded_by TEXT, uploaded_at DATETIME
        )
    ''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_otdr_cable ON otdr_results(cable_id, core, measured_at)")
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS optical_power_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_type TEXT, asset_id INTEGER, port_core TEXT,
            tx_dbm REAL, rx_dbm REAL, wavelength_nm INTEGER, note TEXT, device TEXT,
            measured_by TEXT, measured_at DATETIME
        )
    ''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_power_log ON optical_power_log(asset_type, asset_id, port_core, id)")
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS optical_power (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_type TEXT, asset_id INTEGER, port_core TEXT,
            tx_dbm REAL, rx_dbm REAL, wavelength_nm INTEGER, note TEXT,
            updated_by TEXT, updated_at DATETIME,
            UNIQUE(asset_type, asset_id, port_core)
        )
    ''')

    # Perangkat/interface yang dipatch ke port OTB sebuah POP (OLT/router/switch; VLAN, service, pelanggan PTP)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS otb_port_devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER, port_core TEXT,
            purpose TEXT, device_type TEXT, vendor TEXT, device_name TEXT, interface TEXT,
            vlan TEXT, service TEXT, customer_name TEXT, customer_node_id INTEGER, notes TEXT,
            updated_by TEXT, updated_at DATETIME,
            UNIQUE(asset_id, port_core)
        )
    ''')

    # --- AUTO MIGRATION UNTUK DB EXISTING ---
    def add_column_if_missing(table, column, col_type):
        cursor.execute(f"PRAGMA table_info({table})")
        cols = [r[1] for r in cursor.fetchall()]
        if column not in cols:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            print(f"[MIGRATION] Added {column} to {table}")

    add_column_if_missing("plans", "boq_adjust", "TEXT")      # JSON penyesuaian BOQ (region, qty/item per baris, tambahan)
    add_column_if_missing("cables", "fiber_mode", "TEXT")      # SM (single-mode, bawaan) | MM (multimode)
    add_column_if_missing("otdr_results", "from_node_id", "INTEGER")
    add_column_if_missing("otdr_results", "to_node_id", "INTEGER")
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
    add_column_if_missing("incidents", "map_hidden", "INTEGER DEFAULT 0")   # 1 = penanda disembunyikan dari peta
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
    # Sambungan lewat kabel yang sudah dihapus = tidak sah (aset hanya terhubung karena kabel): hapus
    cursor.execute("DELETE FROM core_connections WHERE via_cable_id IS NOT NULL AND via_cable_id NOT IN (SELECT id FROM cables)")
    if cursor.rowcount:
        print(f"[CLEANUP] {cursor.rowcount} sambungan core dengan kabel yang sudah dihapus dibersihkan")
    # Sambungan antar dua node tanpa kabel (sisa data lama dari penghapusan kabel) = tidak sah: hapus
    cursor.execute("DELETE FROM core_connections WHERE from_asset_type = 'NODE' AND to_asset_type = 'NODE' AND via_cable_id IS NULL")
    if cursor.rowcount:
        print(f"[CLEANUP] {cursor.rowcount} sambungan antar aset tanpa kabel dibersihkan")

    # POP memakai port OTB ('OTB-1 / P07'), bukan Tube/Core: ubah label lama (idempotent)
    migrated = 0
    for pop in cursor.execute("SELECT id, capacity FROM nodes WHERE UPPER(type) = 'POP'").fetchall():
        for tbl, t_col, i_col, p_col in (("core_connections", "from_asset_type", "from_asset_id", "from_port_core"),
                                        ("core_connections", "to_asset_type", "to_asset_id", "to_port_core"),
                                        ("optical_power_log", "asset_type", "asset_id", "port_core"),
                                        ("optical_power", "asset_type", "asset_id", "port_core")):
            for r in cursor.execute(f"SELECT id, {p_col} AS p FROM {tbl} WHERE {t_col} = 'NODE' AND {i_col} = ? AND {p_col} LIKE 'Tube%'", (pop[0],)).fetchall():
                new = _legacy_pop_port(pop[1], r[1])
                if new != r[1]:
                    cursor.execute(f"UPDATE {tbl} SET {p_col} = ? WHERE id = ?", (new, r[0]))
                    migrated += 1
    if migrated:
        print(f"[MIGRASI] {migrated} label port POP diubah ke format OTB (OTB-n / Pxx)")

    # Insiden lama selalu tersimpan EKO/BANJARMASIN (nilai tetap dari form). Samakan dengan aset terkaitnya
    # agar filter Cluster/Area akurat. Hanya sekali.
    if not cursor.execute("SELECT 1 FROM settings WHERE key = 'incident_scope_backfill_v1'").fetchone():
        fixed = 0
        for inc_id, cab_id, node_id in cursor.execute(
                "SELECT id, linked_cable_id, linked_node_id FROM incidents "
                "WHERE linked_cable_id IS NOT NULL OR linked_node_id IS NOT NULL").fetchall():
            src = None
            if cab_id is not None:
                src = cursor.execute("SELECT cluster, area, city FROM cables WHERE id = ?", (cab_id,)).fetchone()
            if src is None and node_id is not None:
                src = cursor.execute("SELECT cluster, area, city FROM nodes WHERE id = ?", (node_id,)).fetchone()
            if src and (src[0] or src[1]):
                cursor.execute(
                    "UPDATE incidents SET cluster = COALESCE(NULLIF(?, ''), cluster), area = COALESCE(NULLIF(?, ''), area), "
                    "city = COALESCE(NULLIF(?, ''), city) WHERE id = ?", (src[0], src[1], src[2], inc_id))
                fixed += cursor.rowcount
        cursor.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES "
                       "('incident_scope_backfill_v1', ?, CURRENT_TIMESTAMP, 'system')", (str(fixed),))
        if fixed:
            print(f"[MIGRASI] Cluster/Area {fixed} insiden disamakan dengan aset terkait")

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
    "teknisi": {"view", "incident.write", "status.write", "otdr.upload", "power.write"},
    "noc": {"view", "incident.write", "status.write", "asset.write", "connection.delete",
            "incident.delete", "repair.undo", "audit.view", "audit.restore",
            "data.import", "plan.write", "plan.realize", "otdr.upload", "core.remap", "power.write"},
}
ROLE_PERMS["admin"] = set().union(*ROLE_PERMS.values()) | {"asset.delete", "user.manage", "plan.rules", "khs.edit", "loss.edit"}

# Urutan penting: yang pertama cocok dipakai. perm None = publik, "auth" = cukup sudah login.
ROUTE_PERMS = [
    ("POST", r"^/api/auth/(login|logout)$", None),
    ("GET", r"^/api/auth/me$", None),
    ("POST", r"^/api/auth/password$", "auth"),
    (None, r"^/api/users(/.*)?$", "user.manage"),
    ("GET", r"^/api/audit$", "audit.view"),
    ("POST", r"^/api/audit/\d+/restore$", "audit.restore"),
    ("POST", r"^/api/incidents/\d+/repairs/\d+/undo$", "repair.undo"),
    ("POST", r"^/api/incidents/(locate|analyze-impact|map-visibility)$", "incident.write"),
    ("PUT", r"^/api/incidents/\d+/map-visibility$", "incident.write"),
    ("POST", r"^/api/import/(preview|commit)$", "data.import"),
    ("PUT", r"^/api/plan/rules$", "plan.rules"),
    ("PUT", r"^/api/loss/params$", "loss.edit"),
    ("POST", r"^/api/otdr/(preview|commit|manual)$", "otdr.upload"),
    ("PUT", r"^/api/power$", "power.write"),
    ("PUT", r"^/api/nodes/\d+/port-devices$", "asset.write"),
    ("DELETE", r"^/api/nodes/\d+/port-devices$", "asset.write"),
    ("DELETE", r"^/api/otdr/\d+$", "otdr.upload"),
    ("POST", r"^/api/cables/\d+/core-remap$", "core.remap"),
    ("POST", r"^/api/(plans/bulk|plan/alternatives|boq/recap)$", "plan.write"),
    ("POST", r"^/api/khs/import$", "khs.edit"),
    ("PUT", r"^/api/khs/[^/]+$", "khs.edit"),
    ("PUT", r"^/api/boq/map$", "khs.edit"),
    ("POST", r"^/api/boq/(calc|export)$", "plan.write"),
    ("PUT", r"^/api/plans/\d+/boq$", "plan.write"),
    ("POST", r"^/api/plan/preview$", "plan.write"),
    ("POST", r"^/api/plans$", "plan.write"),
    ("DELETE", r"^/api/plans/\d+$", "plan.write"),
    ("POST", r"^/api/plans/\d+/realize$", "plan.realize"),
    ("POST", r"^/api/incidents(/\d+/(events|repairs))?$", "incident.write"),
    ("PUT", r"^/api/incidents/\d+/status$", "incident.write"),
    ("DELETE", r"^/api/incidents/\d+$", "incident.delete"),
    ("PUT", r"^/api/(nodes|cables)/\d+/status$", "status.write"),
    ("POST", r"^/api/(connect/options|route)$", "asset.write"),
    ("POST", r"^/api/cables/\d+/auto-connect$", "asset.write"),
    ("DELETE", r"^/api/cables/\d+/auto-connect$", "connection.delete"),
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
    fiber_mode: Optional[str] = "SM"
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
    fiber_mode: Optional[str] = None
    parent_cable_id: Optional[int] = None
    from_node_id: Optional[int] = None
    to_node_id: Optional[int] = None


FULLSPLICE_TAG = "[FULLSPLICE] Splice penuh closure hasil perbaikan"
REQUIRE_CABLE_FOR_NODE_LINK = True   # sambungan antar dua aset wajib melalui kabel (aset hanya terhubung karena kabel)


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
    cluster: Optional[str] = None   # kosong = ikut aset terkait (lalu nilai bawaan)
    area: Optional[str] = None
    city: Optional[str] = None
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
    splice_all: bool = False          # sambung penuh: SEMUA core kabel disambung di closure (bukan hanya yang sudah terpakai)


class EventCreate(BaseModel):
    message: str
    actor: Optional[str] = None


# --- HELPER ---
def _fields_set(model: BaseModel) -> dict:
    """Hanya field yang benar-benar dikirim klien (membedakan 'tidak dikirim' vs 'null')."""
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_unset=True)
    return model.dict(exclude_unset=True)


def _check_fiber_mode(value: Optional[str]):
    if value is not None and str(value).upper() not in ("SM", "MM"):
        raise HTTPException(status_code=400, detail="Jenis serat harus SM (single-mode) atau MM (multimode)")


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


OTB_MAX_UNITS = 16
OTB_LEGACY_RE = re.compile(r"^\s*Tube\s*\d+\s*-\s*Core\s*(\d+)\s*$", re.I)


def _otb_sizes(capacity) -> list:
    """Jumlah port tiap OTB pada POP dari teks kapasitas: '48C' -> [48] (satu OTB), '12+24' -> [12, 24] (dua OTB)."""
    s = str(capacity or "")
    if re.match(r"^\s*\d+(\s*\+\s*\d+)*\s*(c|core|port|p)?\s*$", s, re.I):
        nums = [int(x) for x in re.findall(r"\d+", s) if 0 < int(x) <= 576]
        if nums:
            return nums[:OTB_MAX_UNITS]
    return [_core_total(capacity)]


def _otb_label(idx: int, port: int) -> str:
    return f"OTB-{idx} / P{port:02d}"


def _otb_port_labels(capacity) -> list:
    return [_otb_label(i, p) for i, sz in enumerate(_otb_sizes(capacity), 1) for p in range(1, sz + 1)]


def _legacy_pop_port(capacity, port):
    """Label lama POP ('Tube 1 - Core 7', penomoran berlanjut) -> label OTB ('OTB-1 / P07'); lainnya tidak diubah."""
    m = OTB_LEGACY_RE.match(str(port or ""))
    if not m:
        return port
    n = int(m.group(1))
    for i, sz in enumerate(_otb_sizes(capacity), 1):
        if n <= sz:
            return _otb_label(i, n)
        n -= sz
    return port


def _norm_pop_port(cursor, a_type, a_id, port):
    """Terima label lama pada POP (klien/impor lama) dan ubah ke label OTB."""
    if a_type != "NODE" or not OTB_LEGACY_RE.match(str(port or "")):
        return port
    row = cursor.execute("SELECT type, capacity FROM nodes WHERE id = ?", (a_id,)).fetchone()
    if row and (row["type"] or "").upper() == "POP":
        return _legacy_pop_port(row["capacity"], port)
    return port


def _port_labels(category: str, a_type, capacity) -> list:
    """Daftar port/core sah sebuah aset. Tiang tidak punya port; ODP memakai IN-n / OUT-n; POP memakai port OTB."""
    if category == "NODE":
        t = (a_type or "").upper()
        if t in NODE_TYPES_NO_PORT:
            return []
        if t == "POP":
            return _otb_port_labels(capacity)
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
    if category == "NODE":
        n = cursor.execute("SELECT type FROM nodes WHERE id = ?", (asset_id,)).fetchone()
        if n and (n["type"] or "").upper() in JUNCTION_TYPES:
            ins, outs = _joint_dirs(cursor, asset_id)
            return ins & outs
    return used


def _joint_dirs(cursor, node_id):
    """Joint closure/slack: (port yang sudah menerima dari hulu, port yang sudah meneruskan ke hilir)."""
    ins, outs = set(), set()
    for r in cursor.execute(
            "SELECT from_asset_type, from_asset_id, from_port_core, to_asset_type, to_asset_id, to_port_core FROM core_connections "
            "WHERE (from_asset_type = 'NODE' AND from_asset_id = ?) OR (to_asset_type = 'NODE' AND to_asset_id = ?)",
            (node_id, node_id)).fetchall():
        if r["to_asset_type"] == "NODE" and r["to_asset_id"] == node_id and r["to_port_core"]:
            ins.add(r["to_port_core"])
        if r["from_asset_type"] == "NODE" and r["from_asset_id"] == node_id and r["from_port_core"]:
            outs.add(r["from_port_core"])
    return ins, outs


def _check_capacity_fits(cursor, category: str, asset_id: int, new_type, new_capacity):
    """Kapasitas/tipe baru tidak boleh membuang port yang sedang terpakai sambungan."""
    labels = set(_port_labels(category, new_type, new_capacity))
    orphan = sorted(_used_ports(cursor, category, asset_id) - labels)
    if orphan:
        raise HTTPException(
            status_code=409,
            detail=f"Kapasitas/tipe baru tidak muat: port {', '.join(orphan)} masih dipakai sambungan. "
                   f"Putus sambungannya dulu.")
    if category == "NODE":
        dev = sorted({r[0] for r in cursor.execute(
            "SELECT port_core FROM otb_port_devices WHERE asset_id = ?", (asset_id,)).fetchall()} - labels)
        if dev:
            raise HTTPException(
                status_code=409,
                detail=f"Kapasitas/tipe baru tidak muat: port {', '.join(dev)} masih terhubung ke perangkat. "
                       f"Hapus catatan perangkatnya dulu.")


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
def _split_cable_at(cursor, cable_id, lat, lng, node_name, spec, existing_node_id=None, splice_all=False) -> dict:
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

    if existing_node_id:
        node_id = existing_node_id
    else:
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
    spliced, skipped, full_added = 0, 0, 0
    done_cores = set()
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
        done_cores.add(core)

    # Sambung penuh: core yang belum dipakai ikut disambung di closure (kabel A core c -> joint c -> kabel B core c)
    if splice_all:
        used_a = _used_ports(cursor, "CABLE", cable_id)
        for core in _core_labels(_core_total(cab["capacity"])):
            if core in done_cores or core in used_a:
                continue
            cursor.execute(
                """INSERT INTO core_connections (from_asset_type, from_asset_id, from_port_core,
                       to_asset_type, to_asset_id, to_port_core, via_cable_id, via_core, status, notes)
                   VALUES ('CABLE', ?, ?, 'NODE', ?, ?, NULL, NULL, 'Connected', ?)""",
                (cable_id, core, node_id, core, FULLSPLICE_TAG))
            cursor.execute(
                """INSERT INTO core_connections (from_asset_type, from_asset_id, from_port_core,
                       to_asset_type, to_asset_id, to_port_core, via_cable_id, via_core, status, notes)
                   VALUES ('NODE', ?, ?, 'CABLE', ?, ?, NULL, NULL, 'Connected', ?)""",
                (node_id, core, cable_b, core, FULLSPLICE_TAG))
            full_added += 1

    # Dampak insiden: segmen B mewarisi status & catatan pemulihan kabel asli
    for im in cursor.execute(
            "SELECT incident_id, prev_status FROM incident_impacts WHERE asset_type = 'CABLE' AND asset_id = ?",
            (cable_id,)).fetchall():
        cursor.execute("INSERT INTO incident_impacts (incident_id, asset_type, asset_id, prev_status) "
                       "VALUES (?, 'CABLE', ?, ?)", (im["incident_id"], cable_b, im["prev_status"]))

    return {"node_id": node_id, "cable_a_id": cable_id, "cable_b_id": cable_b,
            "position_m": round(sn["along_m"], 1), "length_m": round(sn["total_m"], 1),
            "latitude": pt[1], "longitude": pt[0], "cores_spliced": spliced, "cores_skipped": skipped,
            "cores_full_added": full_added, "cores_total": _core_total(cab["capacity"])}


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
        names_, conns_ = _trace_graph(cursor)
        idx_ = _hop_index(conns_)
        for r in cursor.execute(
                f"SELECT id FROM core_connections WHERE via_cable_id = ? AND via_core IN ({marks})",
                (inc["linked_cable_id"], *cores)).fetchall():
            start_h = next((c for c in conns_ if c["id"] == r["id"]), None)
            seen_h, queue_h = set(), deque([start_h] if start_h else [])
            while queue_h:          # ikuti SIRKUIT core tsb (melewati closure lewat joint yang sama)
                h = queue_h.popleft()
                if h["id"] in seen_h:
                    continue
                seen_h.add(h["id"])
                if h["to_asset_type"] == "NODE":
                    nodes.add(h["to_asset_id"])
                if h.get("via_cable_id") is not None and h["via_cable_id"] != inc["linked_cable_id"]:
                    cables.add(h["via_cable_id"])
                queue_h.extend(_next_down(names_, idx_, h))
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
SCOPE_DEFAULTS = {"cluster": "EKO", "area": "BANJARMASIN"}


def _scope_conds(cluster="ALL", area="ALL", alias=""):
    """Kondisi SQL filter Cluster/Area (tanpa beda huruf besar-kecil). Kolom kosong dianggap nilai bawaan,
    sama seperti yang ditampilkan di peta. 'ALL'/kosong = tanpa filter."""
    p = f"{alias}." if alias else ""
    conds, params = [], []
    for col, val in (("cluster", cluster), ("area", area)):
        v = (val or "").strip()
        if v and v.upper() != "ALL":
            conds.append(f"COALESCE(NULLIF({p}{col}, ''), '{SCOPE_DEFAULTS[col]}') = ? COLLATE NOCASE")
            params.append(v)
    return conds, params


def _where(conds):
    return (" WHERE " + " AND ".join(conds)) if conds else ""


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
def get_inventory(q: str = "", cluster: str = "ALL", area: str = "ALL", type: str = "ALL", status: str = "ALL",
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
    sc, sp = _scope_conds(cluster, area)
    where += sc
    params += sp
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
            "NULL AS installation, spec_data FROM nodes "
            "UNION ALL "
            "SELECT 'CABLE' AS category, id, name, type, status, cluster, area, city, capacity, "
            "installation, NULL AS spec_data FROM cables")
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
def get_nodes(cluster: str = "ALL", area: str = "ALL"):
    sc, sp = _scope_conds(cluster, area)
    with db() as conn:
        nodes = conn.execute("SELECT * FROM nodes" + _where(sc), sp).fetchall()
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
def get_cables(cluster: str = "ALL", area: str = "ALL"):
    sc, sp = _scope_conds(cluster, area)
    with db() as conn:
        cables = conn.execute("SELECT * FROM cables" + _where(sc), sp).fetchall()

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
                "fiber_mode": c.get("fiber_mode") or "SM",
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
    _check_fiber_mode(cable.fiber_mode)
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
                                core_data, installation, parent_cable_id, from_node_id, to_node_id, fiber_mode)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            cable.name, cable.type, cable.status, json.dumps(geojson_geom),
            cable.cluster, cable.area, cable.city, cable.capacity, cable.core_data, cable.installation or None,
            cable.parent_cable_id, cable.from_node_id, cable.to_node_id, (cable.fiber_mode or "SM").upper(),
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
def get_summary(cluster: str = "ALL", area: str = "ALL"):
    sc, sp = _scope_conds(cluster, area)
    scn = " AND ".join(sc)  # kondisi tanpa alias; dipakai untuk nodes / cables / incidents (kolom sama)
    A = (" AND " + scn) if scn else ""   # tambahan setelah WHERE yang sudah ada
    W = (" WHERE " + scn) if scn else ""  # WHERE tunggal
    with db() as conn:
        q = lambda sql, extra=(): conn.execute(sql, tuple(extra)).fetchone()[0]

        total_nodes = q("SELECT COUNT(*) FROM nodes" + W, sp)
        total_odp = q("SELECT COUNT(*) FROM nodes WHERE type = 'ODP'" + A, sp)
        total_cables = q("SELECT COUNT(*) FROM cables" + W, sp)

        tickets_open = q("SELECT COUNT(*) FROM incidents WHERE status != 'Resolved'" + A, sp)

        # Aset Cut/Broken yang BUKAN akibat tiket aktif (dilaporkan manual dari popup) tetap dihitung
        covered = """SELECT im.asset_id FROM incident_impacts im
                     JOIN incidents i ON i.id = im.incident_id
                     WHERE i.status != 'Resolved' AND im.asset_type = '{t}'"""
        manual_nodes = q(
            f"SELECT COUNT(*) FROM nodes WHERE status = 'Cut/Broken' AND id NOT IN ({covered.format(t='NODE')})" + A, sp)
        manual_cables = q(
            f"SELECT COUNT(*) FROM cables WHERE status = 'Cut/Broken' AND id NOT IN ({covered.format(t='CABLE')})" + A, sp)
        broken_nodes = q("SELECT COUNT(*) FROM nodes WHERE status = 'Cut/Broken'" + A, sp)
        broken_cables = q("SELECT COUNT(*) FROM cables WHERE status = 'Cut/Broken'" + A, sp)

        # --- rincian per jenis node: jumlah + sebaran status ---
        by_type = {}
        for r in conn.execute("SELECT UPPER(COALESCE(type, '')) AS t, status, COUNT(*) AS n FROM nodes" + W + " GROUP BY 1, 2", sp):
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
        for r in conn.execute("SELECT id, type, installation FROM cables" + W, sp):
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
        breakdown = _scope_rows(conn)   # selalu seluruh data: dasar untuk memilih Cluster/Area
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
        "breakdown": breakdown,
    }


def _scope_key(c, a):
    return ((c or "").strip().upper() or SCOPE_DEFAULTS["cluster"], (a or "").strip().upper() or SCOPE_DEFAULTS["area"])


def _scope_rows(conn):
    """Rekap per (cluster, area) di seluruh data (tanpa filter): node, ODP, rusak, kabel, panjang, insiden aktif."""
    rows, label = {}, {}

    def row(c, a):
        k = _scope_key(c, a)
        label.setdefault(k, ((c or "").strip() or SCOPE_DEFAULTS["cluster"], (a or "").strip() or SCOPE_DEFAULTS["area"]))
        return rows.setdefault(k, {"nodes": 0, "odp": 0, "broken": 0, "cables": 0, "length_m": 0.0, "incidents": 0,
                                   "incidents_open": 0})
    for r in conn.execute("SELECT cluster, area, COUNT(*) n, SUM(type = 'ODP') odp, "
                          "SUM(status = 'Cut/Broken') br FROM nodes GROUP BY 1, 2"):
        d = row(r["cluster"], r["area"]); d["nodes"] += r["n"]; d["odp"] += r["odp"] or 0; d["broken"] += r["br"] or 0
    lengths = _cable_length_map(conn)
    for r in conn.execute("SELECT id, cluster, area, status FROM cables"):
        d = row(r["cluster"], r["area"]); d["cables"] += 1; d["length_m"] += lengths.get(r["id"], 0.0)
        d["broken"] += 1 if r["status"] == "Cut/Broken" else 0
    for r in conn.execute("SELECT cluster, area, COUNT(*) n, SUM(status != 'Resolved') op FROM incidents GROUP BY 1, 2"):
        d = row(r["cluster"], r["area"]); d["incidents"] += r["n"]; d["incidents_open"] += r["op"] or 0
    out = []
    for k in sorted(rows):
        d = rows[k]; d["length_m"] = round(d["length_m"], 1)
        out.append({"cluster": label[k][0], "area": label[k][1], **d})
    return out


@app.get("/api/filters/options")
def get_filter_options():
    """Daftar Cluster & Area yang ada di data (untuk filter), lengkap dengan jumlahnya."""
    with db() as conn:
        rows = _scope_rows(conn)
    clusters, areas = {}, {}
    for r in rows:
        c = clusters.setdefault(r["cluster"].upper(), {"value": r["cluster"], "assets": 0, "incidents": 0})
        c["assets"] += r["nodes"] + r["cables"]; c["incidents"] += r["incidents"]
        a = areas.setdefault(r["area"].upper(), {"value": r["area"], "clusters": [], "assets": 0, "incidents": 0})
        a["assets"] += r["nodes"] + r["cables"]; a["incidents"] += r["incidents"]
        if r["cluster"] not in a["clusters"]:
            a["clusters"].append(r["cluster"])
    return {"clusters": [clusters[k] for k in sorted(clusters)], "areas": [areas[k] for k in sorted(areas)],
            "pairs": [{"cluster": r["cluster"], "area": r["area"], "assets": r["nodes"] + r["cables"],
                       "incidents": r["incidents"]} for r in rows]}


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
        cursor.execute("DELETE FROM otb_port_devices WHERE asset_id = ?", (node_id,))
        cursor.execute("UPDATE otb_port_devices SET customer_node_id = NULL WHERE customer_node_id = ?", (node_id,))
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
    _check_fiber_mode(data.get("fiber_mode"))
    if data.get("fiber_mode"):
        data["fiber_mode"] = str(data["fiber_mode"]).upper()
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
            ["name", "type", "status", "cluster", "area", "city", "capacity", "core_data", "fiber_mode"],
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
                              "installation", "parent_cable_id", "from_node_id", "to_node_id", "fiber_mode"])
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
               f"({len(snap['refs']['connections']) + len(snap['refs']['via'])} sambungan core ikut terhapus)", snapshot=snap)

        cursor.execute(
            "DELETE FROM core_connections WHERE (from_asset_type = 'CABLE' AND from_asset_id = ?) "
            "OR (to_asset_type = 'CABLE' AND to_asset_id = ?)", (cable_id, cable_id))
        # aset hanya terhubung karena kabel: sambungan yang melewati kabel ini ikut dihapus (bukan dibiarkan tanpa kabel)
        cursor.execute("DELETE FROM core_connections WHERE via_cable_id = ?", (cable_id,))
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
        cab = conn.execute("SELECT * FROM cables WHERE id = ?", (cable_id,)).fetchone()
        if not cab:
            raise HTTPException(status_code=404, detail="Kabel tidak ditemukan")
        _p = _loss_params(conn.cursor())
        _lat = _latest_otdr(conn.cursor(), cable_id)
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
    km = _cable_len_km(cab)
    n_conn = {}
    for r in rows:
        for lbl in {r["via_core"] if r["via_cable_id"] == cable_id else None,
                    r["from_port_core"] if r["from_asset_type"] == "CABLE" and r["from_asset_id"] == cable_id else None,
                    r["to_port_core"] if r["to_asset_type"] == "CABLE" and r["to_asset_id"] == cable_id else None} - {None}:
            n_conn[lbl] = n_conn.get(lbl, 0) + 1
    cores = [{"core": lbl, "used": lbl in used, "connection_id": used.get(lbl),
              "calc_db": round(km * _fiber_coef(_p, cab["fiber_mode"] if "fiber_mode" in cab.keys() else None) + _p["splice_db"] * n_conn.get(lbl, 0), 2),
              "measured": _lat.get(lbl)}
             for lbl in _core_labels(total)]
    used_count = sum(1 for c in cores if c["used"])
    return {"cable_id": cable_id, "name": cab["name"], "total": total, "length_km": round(km, 3), "fiber_mode": ((cab["fiber_mode"] if "fiber_mode" in cab.keys() else None) or "SM").upper(),
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
        if REQUIRE_CABLE_FOR_NODE_LINK and from_type == "NODE" and to_type == "NODE":
            if payload.via_cable_id is None:
                raise HTTPException(status_code=400, detail="Aset hanya bisa terhubung lewat kabel: pilih kabel penghubung")
            cabs = cursor.execute("SELECT * FROM cables").fetchall()
            touch = set()
            for nid in (payload.from_asset_id, payload.to_asset_id):
                nrow = cursor.execute("SELECT * FROM nodes WHERE id = ?", (nid,)).fetchone()
                touch |= {c["row"]["id"] for c in _cables_at_node(cursor, nrow, cabs)}
            if payload.via_cable_id not in touch:
                raise HTTPException(status_code=400, detail="Kabel penghubung tidak menyentuh kedua aset (ujung/lintasannya jauh dari aset)")
        payload.from_port_core = _norm_pop_port(cursor, from_type, payload.from_asset_id, payload.from_port_core)
        payload.to_port_core = _norm_pop_port(cursor, to_type, payload.to_asset_id, payload.to_port_core)
        for side, a_type, a_id, port in (
                ("asal", from_type, payload.from_asset_id, payload.from_port_core),
                ("tujuan", to_type, payload.to_asset_id, payload.to_port_core)):
            row = cursor.execute(
                f"SELECT name, {'type' if a_type == 'NODE' else 'NULL AS type'}, capacity "
                f"FROM {_table_of(a_type)} WHERE id = ?", (a_id,)).fetchone()
            joint = a_type == "NODE" and (row["type"] or "").upper() in JUNCTION_TYPES
            if joint:
                # closure/slack = penghubung kabel: port adalah joint; kapasitas hanya informasi (tray)
                if not re.match(r"^Tube \d+ - Core \d+$", str(port or "")):
                    raise HTTPException(status_code=400, detail=f"Joint '{port}' tidak valid pada {row['name']} (format: Tube 1 - Core 1)")
                ins, outs = _joint_dirs(cursor, a_id)
                if side == "asal" and port in outs:
                    raise HTTPException(status_code=409,
                                        detail=f"Joint '{port}' pada {row['name']} sudah meneruskan ke kabel hilir lain")
                if side == "tujuan" and port in ins:
                    raise HTTPException(status_code=409,
                                        detail=f"Joint '{port}' pada {row['name']} sudah menerima kabel hulu lain")
                continue
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
@app.get("/api/nodes/{node_id}/junction")
def node_junction(node_id: int):
    """Kabel yang berujung/melintas di closure/slack + status joint (masuk/keluar)."""
    with db() as conn:
        cur = conn.cursor()
        node = cur.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if not node:
            raise HTTPException(status_code=404, detail="Node tidak ditemukan")
        cables = cur.execute("SELECT * FROM cables").fetchall()
        out = []
        for a_ in _cables_at_node(cur, node, cables):
            c = a_["row"]
            used = len(_used_ports(cur, "CABLE", c["id"]))
            out.append({"id": c["id"], "name": c["name"], "type": c["type"], "capacity": c["capacity"],
                        "at_end": a_["at_end"], "at_start": a_["at_start"],
                        "through": not (a_["at_end"] or a_["at_start"]), "used": used, "total": _core_total(c["capacity"])})
        ins, outs = _joint_dirs(cur, node_id)
    return {"node_id": node_id, "cables": out, "joints_in": sorted(ins), "joints_out": sorted(outs),
            "joints_complete": sorted(ins & outs)}


# --- TAHAP B: sambung otomatis saat kabel digambar di peta ---
AC_RANK = {"POP": 0, "CLOSURE": 1, "SLACK": 1, "ODP": 2, "PELANGGAN": 3}


class AutoConnectReq(BaseModel):
    cores: int = 1
    reverse: bool = False                 # balik arah hulu <-> hilir
    preview: bool = False                 # hanya simulasi (tidak disimpan)
    upstream_cable_id: Optional[int] = None   # pilihan kabel hulu bila closure punya beberapa
    respect_order: bool = False           # True: hulu = ujung awal kabel (urutan klik pengguna), tanpa menebak dari jenis aset
    from_ports: Optional[List[str]] = None    # pilihan port/joint di aset hulu ('AUTO' = umpan otomatis dari hulu)
    to_ports: Optional[List[str]] = None      # pilihan port di aset hilir (ODP/pelanggan)
    via_cores: Optional[List[str]] = None     # pilihan core pada kabel ini
    full: bool = False                    # sambung penuh: semua core kabel (sesuai kapasitas & ketersediaan hulu/hilir)


def _full_core_count(cursor, up, down, capacity, cable_id=None, exclude=None):
    """Jumlah core untuk 'sambung penuh': semua core kabel yang masih bebas, dibatasi port hilir/hulu yang tersedia.
    Return (n, catatan)."""
    tu, td = (up["type"] or "").upper(), (down["type"] or "").upper()
    if tu == "ODP" or td == "ODP":
        return 1, "ODP hanya 1 core: sambung penuh dibatasi 1 core"
    total = _core_total(capacity)
    used0 = _used_ports(cursor, "CABLE", cable_id) if cable_id else set()
    ins_dn0 = _joint_dirs(cursor, down["id"])[0] if td in JUNCTION_TYPES else set()
    n = len([x for x in _core_labels(total) if x not in used0 and x not in ins_dn0])
    if td == "PELANGGAN":
        usedd = _used_ports(cursor, "NODE", down["id"])
        n = min(n, len([x for x in _port_labels("NODE", "PELANGGAN", down["capacity"]) if x not in usedd]))
    if tu in JUNCTION_TYPES:
        def can_feed(k):
            cursor.execute("SAVEPOINT fullfeed")
            try:
                s, _ = _acquire_feed(cursor, up, k, set(exclude or ()), 0, [])
            finally:
                cursor.execute("ROLLBACK TO fullfeed")
                cursor.execute("RELEASE fullfeed")
            return bool(s)
        lo, hi = 0, n
        while lo < hi:                      # cari k terbesar yang masih bisa disuplai hulu
            mid = (lo + hi + 1) // 2
            if can_feed(mid):
                lo = mid
            else:
                hi = mid - 1
        n = lo
    else:
        lab = _port_labels("NODE", tu, up["capacity"])
        lab = [x for x in lab if x.startswith("OUT-")] if tu == "ODP" else lab
        usedu = _used_ports(cursor, "NODE", up["id"])
        n = min(n, len([x for x in lab if x not in usedu]))
    note = None
    if n < total:
        note = f"Sambung penuh: {n} dari {total} core (sisanya terpakai atau port hulu/hilir tidak cukup)"
    return n, note


class _PreviewRollback(Exception):
    pass


def _ac_plan_and_apply(cursor, cable_id, req):
    cab = cursor.execute("SELECT * FROM cables WHERE id = ?", (cable_id,)).fetchone()
    if not cab:
        raise HTTPException(status_code=404, detail="Kabel tidak ditemukan")
    res = {"ok": False, "cable": {"id": cab["id"], "name": cab["name"]}, "reason": None, "warnings": [],
           "needs_choice": None, "links": [], "from": None, "to": None, "cores": req.cores}
    n = int(req.cores or 1)
    if n < 1 or (n > 12 and not req.full):
        res["reason"] = "Jumlah core harus 1-12"
        return res
    a_ = _cable_end_node(cursor, cab, True)
    b_ = _cable_end_node(cursor, cab, False)
    if not a_ or not b_:
        res["reason"] = "Kedua ujung kabel harus berada di sebuah node (jarak <= %d m) agar bisa disambung otomatis" % NODE_SNAP_M
        return res
    ta, tb = (a_["type"] or "").upper(), (b_["type"] or "").upper()
    if ta not in AC_RANK or tb not in AC_RANK:
        bad = a_ if ta not in AC_RANK else b_
        res["reason"] = f"Ujung kabel di {bad['name']} ({bad['type']}): bukan perangkat yang bisa disambung otomatis"
        return res
    if req.respect_order:
        up, down = a_, b_
    else:
        up, down = (a_, b_) if AC_RANK[ta] <= AC_RANK[tb] else (b_, a_)
    if req.reverse:
        up, down = down, up
    res["from"] = {"id": up["id"], "name": up["name"], "type": up["type"]}
    res["to"] = {"id": down["id"], "name": down["name"], "type": down["type"]}
    tu, td = (up["type"] or "").upper(), (down["type"] or "").upper()
    if td == "POP" or tu == "PELANGGAN" or up["id"] == down["id"]:
        res["reason"] = "Arah tidak valid (hulu tidak boleh PELANGGAN, hilir tidak boleh POP); coba 'Balik arah'"
        return res
    if req.full:
        n, fnote = _full_core_count(cursor, up, down, cab["capacity"], cable_id, {cable_id})
        res["cores"] = n
        if fnote:
            res["warnings"].append(fnote)
        if n < 1:
            res["reason"] = "Tidak ada core yang bisa disambung penuh (core kabel habis atau port hulu/hilir tidak tersedia)"
            return res
        req = req.model_copy(update={"via_cores": None, "from_ports": None, "to_ports": None}) if hasattr(req, "model_copy") else req
    if td == "ODP" and n > 1:
        res["reason"] = "ODP hanya 1 core; layanan 2 core dedicated hanya dari closure/POP ke pelanggan"
        return res
    if tu == "ODP" and n > 1:
        res["reason"] = "ODP hanya 1 core; tidak bisa menyuplai lebih dari 1 core"
        return res
    # core kabel yang bebas (dan, untuk closure hilir, label joint belum menerima hulu lain)
    used = _used_ports(cursor, "CABLE", cable_id)
    ins_dn = _joint_dirs(cursor, down["id"])[0] if td in JUNCTION_TYPES else set()
    free = [x for x in _core_labels(_core_total(cab["capacity"])) if x not in used and x not in ins_dn]
    if len(free) < n:
        res["reason"] = f"Kabel {cab['name']} hanya punya {len(free)} core kosong; butuh {n}"
        return res
    if req.via_cores:
        if len(req.via_cores) != n or len(set(req.via_cores)) != n or any(c not in free for c in req.via_cores):
            res["reason"] = "Core kabel yang dipilih tidak valid / sudah terpakai / jumlah tidak sama dengan jumlah core"
            return res
        cores = list(req.via_cores)
    else:
        cores = free[:n]
    # port hilir
    if td == "ODP":
        ins = [x for x in _port_labels("NODE", "ODP", down["capacity"]) if x.startswith("IN-")]
        usedp = _used_ports(cursor, "NODE", down["id"])
        fr = [x for x in ins if x not in usedp]
        if not fr:
            res["reason"] = f"Port IN {down['name']} sudah terpakai"
            return res
        to_ports = [fr[0]]
        if req.to_ports:
            if len(req.to_ports) != 1 or req.to_ports[0] not in fr:
                res["reason"] = f"Port IN '{req.to_ports[0]}' tidak tersedia pada {down['name']}"
                return res
            to_ports = list(req.to_ports)
    elif td == "PELANGGAN":
        usedp = _used_ports(cursor, "NODE", down["id"])
        fr = [x for x in _port_labels("NODE", "PELANGGAN", down["capacity"]) if x not in usedp]
        if len(fr) < n:
            res["reason"] = f"Titik pelanggan {down['name']} hanya {len(fr)} port kosong; butuh {n}"
            return res
        to_ports = fr[:n]
        if req.to_ports:
            if len(req.to_ports) != n or len(set(req.to_ports)) != n or any(p not in fr for p in req.to_ports):
                res["reason"] = "Port pelanggan yang dipilih tidak valid / sudah terpakai"
                return res
            to_ports = list(req.to_ports)
    else:
        to_ports = list(cores)
    # pilihan kabel hulu bila hulu = closure dan perlu umpan
    exclude = {cable_id}
    if tu in JUNCTION_TYPES:
        ins_u, outs_u = _joint_dirs(cursor, up["id"])
        ready = [p for p in ins_u if p not in outs_u]
        if len(ready) < n:
            cands = []
            for a2 in _cables_at_node(cursor, up, cursor.execute("SELECT * FROM cables").fetchall()):
                c = a2["row"]
                if c["id"] == cable_id or not a2["at_end"]:
                    continue
                fr2 = [x for x in _core_labels(_core_total(c["capacity"])) if x not in _used_ports(cursor, "CABLE", c["id"]) and x not in ins_u]
                if len(fr2) >= n - len(ready):
                    cands.append({"id": c["id"], "name": c["name"], "type": c["type"], "free": len(fr2)})
            if len(cands) > 1 and not req.upstream_cable_id:
                res["needs_choice"] = cands
                res["reason"] = f"{up['name']} punya {len(cands)} kabel hulu; pilih salah satu"
                return res
            if req.upstream_cable_id:
                exclude |= {c["id"] for c in cands if c["id"] != req.upstream_cable_id}
    before = cursor.execute("SELECT COALESCE(MAX(id), 0) AS m FROM core_connections").fetchone()["m"]
    feed_log = []
    chosen = [p for p in (req.from_ports or []) if p != "AUTO"]
    if req.from_ports and chosen and len(chosen) == len(req.from_ports):
        # pengguna memilih sendiri port/joint hulu
        if len(chosen) != n or len(set(chosen)) != n:
            res["reason"] = "Jumlah port hulu harus sama dengan jumlah core"
            return res
        if tu in JUNCTION_TYPES:
            ins_u2, outs_u2 = _joint_dirs(cursor, up["id"])
            okp = {p for p in ins_u2 if p not in outs_u2}
        else:
            lab = _port_labels("NODE", tu, up["capacity"])
            lab = [x for x in lab if x.startswith("OUT-")] if tu == "ODP" else lab
            usedu = _used_ports(cursor, "NODE", up["id"])
            okp = {x for x in lab if x not in usedu}
        if any(p not in okp for p in chosen):
            res["reason"] = f"Port/joint hulu pilihan tidak tersedia pada {up['name']}"
            return res
        srcs = [("NODE", up["id"], p) for p in chosen]
    else:
        srcs, why = _acquire_feed(cursor, up, n, exclude, 0, feed_log)
        if not srcs:
            res["reason"] = why
            return res
    links = list(feed_log)
    for i in range(n):
        links.append(_link(cursor, srcs[i], "NODE", down["id"], to_ports[i], cable_id, cores[i],
                           "Auto-sambung dari peta"))
    cursor.execute("UPDATE core_connections SET notes = TRIM(COALESCE(notes, '') || ' [AC#' || ? || ']') WHERE id > ?",
                   (cable_id, before))
    res.update(ok=True, links=[x["label"] for x in links], cores_used=cores)
    if len(srcs) and td in JUNCTION_TYPES:
        res["warnings"].append(f"Joint di {down['name']} memakai label core kabel ini ({', '.join(cores)})")
    return res


class ConnectOptionsReq(BaseModel):
    from_node_id: int
    to_node_id: int
    cores: int = 1
    capacity: Optional[str] = "12C"
    reverse: bool = False
    full: bool = False


@app.post("/api/connect/options")
def connect_options(req: ConnectOptionsReq):
    """Mode 'Hubungkan aset': kandidat port/core untuk dua aset yang dipilih (sebelum kabel disimpan).
    Default dipilih sistem (terendah yang kosong); pengguna boleh mengganti."""
    n = int(req.cores or 1)
    out = {"ok": False, "reason": None, "from": None, "to": None, "from_options": [], "to_options": [], "cable_cores": [],
           "defaults": {"from_ports": [], "to_ports": [], "via_cores": []}, "warnings": [], "feed": None}
    with db() as conn:
        cur = conn.cursor()
        a_ = cur.execute("SELECT * FROM nodes WHERE id = ?", (req.from_node_id,)).fetchone()
        b_ = cur.execute("SELECT * FROM nodes WHERE id = ?", (req.to_node_id,)).fetchone()
        if not a_ or not b_:
            raise HTTPException(status_code=404, detail="Aset tidak ditemukan")
        up, down = (b_, a_) if req.reverse else (a_, b_)
        tu, td = (up["type"] or "").upper(), (down["type"] or "").upper()
        out["from"] = {"id": up["id"], "name": up["name"], "type": up["type"]}
        out["to"] = {"id": down["id"], "name": down["name"], "type": down["type"]}
        if n < 1 or (n > 12 and not req.full):
            out["reason"] = "Jumlah core harus 1-12"; return out
        if up["id"] == down["id"]:
            out["reason"] = "Aset awal dan akhir harus berbeda"; return out
        if tu not in AC_RANK or td not in AC_RANK:
            out["reason"] = "Hanya POP, Closure, Slack, ODP, dan Pelanggan yang bisa dihubungkan (bukan tiang/handhole)"; return out
        if td == "POP" or tu == "PELANGGAN":
            out["reason"] = "Arah tidak valid (hulu tidak boleh PELANGGAN, hilir tidak boleh POP); coba 'Balik arah'"; return out
        if req.full:
            n, fnote = _full_core_count(cur, up, down, req.capacity or "12C", None, set())
            out["cores"] = n
            if fnote:
                out["warnings"].append(fnote)
            if n < 1:
                out["reason"] = "Tidak ada core yang bisa disambung penuh (port hulu/hilir tidak tersedia)"; return out
        if (td == "ODP" or tu == "ODP") and n > 1:
            out["reason"] = "ODP hanya 1 core; 2 core dedicated hanya dari closure/POP ke pelanggan"; return out
        # hulu
        if tu in JUNCTION_TYPES:
            ins_u, outs_u = _joint_dirs(cur, up["id"])
            ready = sorted(p for p in ins_u if p not in outs_u)
            out["from_options"] = [{"port": p, "label": f"{p} (sudah tiba dari hulu)", "state": "arrived"} for p in ready]
            # simulasi umpan otomatis (dibatalkan setelah dihitung)
            try:
                sp = cur.execute("SAVEPOINT feedtest")
                srcs, why = _acquire_feed(cur, up, n, set(), 0, [])
                cur.execute("ROLLBACK TO feedtest")
                cur.execute("RELEASE feedtest")
            except Exception as exc:
                srcs, why = None, str(exc)
            out["feed"] = {"ok": bool(srcs), "reason": why}
            if srcs:
                out["from_options"].append({"port": "AUTO", "label": "Otomatis (dibuatkan dari kabel hulu)", "state": "feed"})
            out["defaults"]["from_ports"] = ready[:n] if len(ready) >= n else (["AUTO"] * n if srcs else [])
            if not out["from_options"]:
                out["reason"] = why or f"{up['name']} belum punya jalur hulu"; return out
        else:
            lab = _port_labels("NODE", tu, up["capacity"])
            lab = [x for x in lab if x.startswith("OUT-")] if tu == "ODP" else lab
            usedu = _used_ports(cur, "NODE", up["id"])
            free_u = [x for x in lab if x not in usedu]
            out["from_options"] = [{"port": p, "label": p, "state": "free"} for p in free_u]
            if len(free_u) < n:
                out["reason"] = f"{up['name']} hanya {len(free_u)} port kosong; butuh {n}"; return out
            out["defaults"]["from_ports"] = free_u[:n]
        # hilir
        used_c = set()
        if td in JUNCTION_TYPES:
            ins_d = _joint_dirs(cur, down["id"])[0]
        else:
            ins_d = set()
            usedd = _used_ports(cur, "NODE", down["id"])
            if td == "ODP":
                fr = [x for x in _port_labels("NODE", "ODP", down["capacity"]) if x.startswith("IN-") and x not in usedd]
            else:
                fr = [x for x in _port_labels("NODE", "PELANGGAN", down["capacity"]) if x not in usedd]
            out["to_options"] = [{"port": p, "label": p, "state": "free"} for p in fr]
            need = 1 if td == "ODP" else n
            if len(fr) < need:
                out["reason"] = f"{down['name']} hanya {len(fr)} port kosong; butuh {need}"; return out
            out["defaults"]["to_ports"] = fr[:need]
        cores = [x for x in _core_labels(_core_total(req.capacity or "12C")) if x not in ins_d]
        out["cable_cores"] = cores
        if len(cores) < n:
            out["reason"] = f"Kapasitas kabel {req.capacity} tidak cukup untuk {n} core"; return out
        out["defaults"]["via_cores"] = cores[:n]
        if td in JUNCTION_TYPES:
            out["warnings"].append(f"Joint di {down['name']} memakai label core kabel (sambungan menerus)")
        out["ok"] = True
    return out


class RouteReq(BaseModel):
    points: List[List[float]]      # [[lat, lng], ...] minimal 2


@app.post("/api/route")
def route_between(req: RouteReq):
    """Rute mengikuti jalan antar titik (OSRM; gagal -> garis lurus). Untuk mode 'Hubungkan aset'."""
    if len(req.points) < 2 or len(req.points) > 25:
        raise HTTPException(status_code=400, detail="Butuh 2-25 titik")
    for p in req.points:
        if len(p) != 2 or not (-90 <= p[0] <= 90) or not (-180 <= p[1] <= 180):
            raise HTTPException(status_code=400, detail="Koordinat tidak valid")
    r = _route_between([(p[0], p[1]) for p in req.points])
    length = sum(_haversine_m(r["coords"][i][1], r["coords"][i][0], r["coords"][i + 1][1], r["coords"][i + 1][0]) for i in range(len(r["coords"]) - 1))
    return {**r, "length_m": round(length, 1)}


@app.post("/api/cables/{cable_id}/auto-connect")
def auto_connect_cable(cable_id: int, req: AutoConnectReq):
    """Sambungkan kabel (yang baru digambar) otomatis berdasar core yang tersedia: hulu -> kabel -> hilir.
    preview=true hanya menghitung (tidak menyimpan)."""
    out = None
    try:
        with db() as conn:
            cur = conn.cursor()
            out = _ac_plan_and_apply(cur, cable_id, req)
            if out["ok"] and not req.preview:
                _audit(cur, "CONNECT", "CABLE", cable_id, out["cable"]["name"],
                       f"Auto-sambung kabel {out['cable']['name']}: {out['from']['name']} -> {out['to']['name']} ({len(out['links'])} sambungan)")
            elif req.preview or not out["ok"]:
                raise _PreviewRollback()
    except _PreviewRollback:
        pass
    out["preview"] = bool(req.preview)
    return out


@app.delete("/api/cables/{cable_id}/auto-connect")
def undo_auto_connect(cable_id: int):
    """Batalkan seluruh sambungan hasil auto-sambung kabel ini (paket)."""
    with db() as conn:
        cur = conn.cursor()
        rows = cur.execute("SELECT * FROM core_connections WHERE notes LIKE ?", (f"%[AC#{cable_id}]%",)).fetchall()
        if not rows:
            raise HTTPException(status_code=404, detail="Tidak ada sambungan auto untuk kabel ini")
        for r in rows:
            _audit(cur, "DISCONNECT", "CONNECTION", r["id"], _conn_label(cur, r), "Batal auto-sambung", snapshot=_row_dict(r))
            cur.execute("DELETE FROM core_connections WHERE id = ?", (r["id"],))
    return {"message": f"{len(rows)} sambungan dibatalkan", "removed": len(rows)}


class SplitAtNode(BaseModel):
    node_id: int
    splice_all: bool = False


@app.post("/api/cables/{cable_id}/split-at-node")
def split_cable_at_node(cable_id: int, payload: SplitAtNode):
    """Pecah kabel yang MELINTAS di closure/slack menjadi dua segmen (A | B) agar closure menjadi titik sambung (joint)."""
    with db() as conn:
        cur = conn.cursor()
        node = cur.execute("SELECT * FROM nodes WHERE id = ?", (payload.node_id,)).fetchone()
        cab = cur.execute("SELECT * FROM cables WHERE id = ?", (cable_id,)).fetchone()
        if not node or not cab:
            raise HTTPException(status_code=404, detail="Closure atau kabel tidak ditemukan")
        if (node["type"] or "").upper() not in JUNCTION_TYPES:
            raise HTTPException(status_code=400, detail="Hanya closure/slack yang bisa menjadi titik pecah kabel")
        if cab["from_node_id"] == node["id"] or cab["to_node_id"] == node["id"]:
            raise HTTPException(status_code=400, detail="Kabel sudah berujung di closure ini; tidak perlu dipecah")
        res = _split_cable_at(cur, cable_id, node["latitude"], node["longitude"], node["name"], {}, existing_node_id=node["id"], splice_all=payload.splice_all)
        _audit(cur, "UPDATE", "CABLE", cable_id, cab["name"],
               f"Kabel dipecah di {node['name']}: {cab['name']} | {cab['name']}-seg2 ({res['cores_spliced'] + res.get('cores_full_added', 0)} core disambung)")
    return res


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


def _is_junction(names, key):
    return key[0] == "NODE" and (names.get(key, (None, None))[1] or "").upper() in JUNCTION_TYPES


def _hop_index(conns):
    by_from, by_to = {}, {}
    for c in conns:
        by_from.setdefault((c["from_asset_type"], c["from_asset_id"]), []).append(c)
        by_to.setdefault((c["to_asset_type"], c["to_asset_id"]), []).append(c)
    return by_from, by_to


def _joint_convention(idx, n):
    """True bila closure memakai konvensi joint (ada port masuk = port keluar). Data lama (label beda) -> perilaku lama."""
    ins = {x.get("to_port_core") for x in idx[1].get(n, [])}
    outs = {x.get("from_port_core") for x in idx[0].get(n, [])}
    return bool(ins & outs)


def _next_down(names, idx, c):
    """Hop berikutnya ke arah pelanggan. Di closure/slack hanya yang meneruskan JOINT yang sama (sirkuit tidak tercampur)."""
    n = (c["to_asset_type"], c["to_asset_id"])
    out = idx[0].get(n, [])
    if _is_junction(names, n) and c.get("to_port_core") and _joint_convention(idx, n):
        out = [x for x in out if x.get("from_port_core") == c["to_port_core"]]
    return out


def _next_up(names, idx, c):
    n = (c["from_asset_type"], c["from_asset_id"])
    ins = idx[1].get(n, [])
    if _is_junction(names, n) and c.get("from_port_core") and _joint_convention(idx, n):
        ins = [x for x in ins if x.get("to_port_core") == c["from_port_core"]]
    return ins


def _customers_after(names, idx, c):
    """Pelanggan yang dapat dicapai ke arah hilir dari hop c (termasuk ujung c sendiri)."""
    seen, out, queue = set(), set(), deque([c])
    while queue:
        h = queue.popleft()
        if h["id"] in seen:
            continue
        seen.add(h["id"])
        k = (h["to_asset_type"], h["to_asset_id"])
        if names.get(k, (None, None))[1] == "PELANGGAN":
            out.add(k)
        queue.extend(_next_down(names, idx, h))
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
    idx = _hop_index(conns)

    def walk(direction):
        hops, seen = [], set()
        first = (idx[0] if direction == "down" else idx[1]).get(start, [])
        queue = deque((c, 1) for c in first)
        while queue:
            c, level = queue.popleft()
            if c["id"] in seen:
                continue
            seen.add(c["id"])
            hop = _trace_hop(names, c, level)
            if direction == "down":
                hop["customers_below"] = len(_customers_after(names, idx, c))
            hops.append(hop)
            queue.extend((x, level + 1) for x in (_next_down(names, idx, c) if direction == "down" else _next_up(names, idx, c)))
        return hops

    upstream, downstream = walk("up"), walk("down")
    by_id = {c["id"]: c for c in conns}

    through = []
    if asset_type == "CABLE":
        for c in conns:
            if c.get("via_cable_id") == asset_id:
                hop = _trace_hop(names, c, 1)
                cust = _customers_after(names, idx, c)
                hop["customers_below"] = len(cust)
                hop["customers"] = sorted(names[k][0] for k in cust)
                through.append(hop)

    reach = set()
    for h in downstream + through:
        reach |= _customers_after(names, idx, by_id[h["connection_id"]])
    customers = [{"type": k[0], "id": k[1], "name": names[k][0]} for k in sorted(reach, key=lambda k: names[k][0])]

    name, a_type = names[start]
    # info kabel & status node untuk diagram (kapasitas, panjang, status, core terpakai)
    cable_ids, node_ids = set(), set()
    for h in upstream + downstream + through:
        for e in (h["from"], h["to"]):
            (cable_ids if e["type"] == "CABLE" else node_ids).add(e["id"])
        if h.get("via"):
            cable_ids.add(h["via"]["cable_id"])
    if asset_type == "CABLE":
        cable_ids.add(asset_id)
    else:
        node_ids.add(asset_id)
    cinfo, ninfo = {}, {}
    with db() as conn2:
        lens = _cable_length_map(conn2, list(cable_ids)) if cable_ids else {}
        for cid in cable_ids:
            r = conn2.execute("SELECT id, name, type, status, capacity FROM cables WHERE id = ?", (cid,)).fetchone()
            if not r:
                continue
            used = {x["via_core"] for x in conn2.execute(
                "SELECT DISTINCT via_core FROM core_connections WHERE via_cable_id = ? AND via_core IS NOT NULL", (cid,)).fetchall()}
            cinfo[cid] = {"name": r["name"], "type": r["type"], "status": r["status"], "capacity": r["capacity"],
                          "total": _core_total(r["capacity"]), "length_m": round(float(lens.get(cid) or 0)),
                          "used_cores": sorted(used)}
        for nid in node_ids:
            r = conn2.execute("SELECT id, status, type, capacity FROM nodes WHERE id = ?", (nid,)).fetchone()
            if r:
                ninfo[nid] = {"status": r["status"], "type": r["type"], "capacity": r["capacity"]}
                if (r["type"] or "").upper() == "POP":
                    ninfo[nid]["devices"] = {
                        d["port_core"]: {k: d[k] for k in ("purpose", "device_type", "vendor", "device_name", "interface",
                                                           "vlan", "service", "customer_name")}
                        for d in conn2.execute("SELECT * FROM otb_port_devices WHERE asset_id = ?", (nid,)).fetchall()}
    return {
        "asset": {"type": asset_type, "id": asset_id, "name": name, "asset_type": a_type},
        "upstream": upstream, "downstream": downstream, "through": through,
        "cables": cinfo, "nodes": ninfo,
        "customers": customers,
        "summary": {"upstream_hops": len(upstream), "downstream_hops": len(downstream),
                    "through_cores": len(through), "customers_affected": len(customers)},
    }


# 14b. PERANGKAT / INTERFACE PADA PORT OTB (POP)
OTB_PURPOSES = {"PON": "PON (ke OLT)", "PTP": "Pelanggan PTP (langsung ke perangkat)",
                "UPLINK": "Uplink / backbone", "LAINNYA": "Lainnya"}
OTB_DEVICE_TYPES = ["OLT", "Router", "Switch", "Lainnya"]
OTB_VENDORS = ["Huawei", "Raisecom", "Cisco", "ZTE", "Nokia", "MikroTik", "Lainnya"]


class PortDevicePayload(BaseModel):
    port_core: str
    purpose: str
    device_type: Optional[str] = "Router"
    vendor: Optional[str] = None
    device_name: str
    interface: str
    vlan: Optional[str] = None
    service: Optional[str] = None
    customer_name: Optional[str] = None
    customer_node_id: Optional[int] = None
    notes: Optional[str] = None


def _norm_vlan(text) -> Optional[str]:
    """'100', '100,200', '100-110' (1..4094) -> teks ternormalisasi; kosong -> None."""
    t = str(text or "").strip()
    if not t:
        return None
    out = []
    for part in re.split(r"[,\s;]+", t):
        if not part:
            continue
        m = re.match(r"^(\d{1,4})(?:\s*-\s*(\d{1,4}))?$", part)
        if not m:
            raise HTTPException(status_code=400, detail=f"VLAN '{part}' tidak valid (contoh: 100 atau 100,200 atau 100-110)")
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if not (1 <= a <= 4094 and 1 <= b <= 4094) or a > b:
            raise HTTPException(status_code=400, detail=f"VLAN '{part}' di luar rentang 1-4094")
        out.append(str(a) if a == b else f"{a}-{b}")
    return ",".join(out) or None


def _pop_ports_of(cursor, node_id: int):
    n = cursor.execute("SELECT id, name, type, capacity FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if not n:
        raise HTTPException(status_code=404, detail="Node tidak ditemukan")
    if (n["type"] or "").upper() != "POP":
        raise HTTPException(status_code=400, detail="Perangkat pada port OTB hanya dicatat untuk aset bertipe POP")
    return n, _otb_port_labels(n["capacity"])


def _port_devices_of(cursor, node_id: int, labels: list) -> list:
    order = {p: i for i, p in enumerate(labels)}
    rows = [dict(r) for r in cursor.execute("SELECT * FROM otb_port_devices WHERE asset_id = ?", (node_id,)).fetchall()]
    rows.sort(key=lambda r: order.get(r["port_core"], 10 ** 6))
    return rows


@app.get("/api/nodes/{node_id}/port-devices")
def get_port_devices(node_id: int):
    with db() as conn:
        cursor = conn.cursor()
        n, labels = _pop_ports_of(cursor, node_id)
        return {"node_id": node_id, "node_name": n["name"], "devices": _port_devices_of(cursor, node_id, labels),
                "catalog": {"purposes": OTB_PURPOSES, "device_types": OTB_DEVICE_TYPES, "vendors": OTB_VENDORS}}


@app.put("/api/nodes/{node_id}/port-devices")
def put_port_device(node_id: int, payload: PortDevicePayload):
    """Catat/ubah perangkat yang dipatch ke satu port OTB (satu port = satu interface perangkat)."""
    purpose = (payload.purpose or "").strip().upper()
    if purpose not in OTB_PURPOSES:
        raise HTTPException(status_code=400, detail=f"Peruntukan harus salah satu: {', '.join(OTB_PURPOSES)}")
    dtype = (payload.device_type or "").strip() or "Lainnya"
    if dtype not in OTB_DEVICE_TYPES:
        raise HTTPException(status_code=400, detail=f"Jenis perangkat harus salah satu: {', '.join(OTB_DEVICE_TYPES)}")
    name = (payload.device_name or "").strip()
    iface = (payload.interface or "").strip()
    if not name or not iface:
        raise HTTPException(status_code=400, detail="Nama perangkat dan interface/port wajib diisi")
    if len(name) > 80 or len(iface) > 60:
        raise HTTPException(status_code=400, detail="Nama perangkat maks 80 karakter, interface maks 60 karakter")
    vendor = (payload.vendor or "").strip()[:40] or None
    vlan = _norm_vlan(payload.vlan)
    service = (payload.service or "").strip()[:120] or None
    notes = (payload.notes or "").strip()[:300] or None
    cust_name = (payload.customer_name or "").strip()[:120] or None
    cust_id = payload.customer_node_id
    with db() as conn:
        cursor = conn.cursor()
        n, labels = _pop_ports_of(cursor, node_id)
        port = _legacy_pop_port(n["capacity"], payload.port_core)
        if port not in labels:
            raise HTTPException(status_code=400, detail=f"Port '{payload.port_core}' tidak ada pada {n['name']}")
        if purpose == "PTP":
            if cust_id is not None:
                c = cursor.execute("SELECT name, type FROM nodes WHERE id = ?", (cust_id,)).fetchone()
                if not c or (c["type"] or "").upper() != "PELANGGAN":
                    raise HTTPException(status_code=400, detail="Pelanggan yang dipilih tidak ditemukan (harus aset bertipe PELANGGAN)")
                cust_name = cust_name or c["name"]
            if not cust_name:
                raise HTTPException(status_code=400, detail="Peruntukan PTP: nama pelanggan wajib diisi")
        else:
            cust_name, cust_id = None, None
        dup = cursor.execute(
            "SELECT port_core FROM otb_port_devices WHERE asset_id = ? AND port_core != ? "
            "AND LOWER(device_name) = LOWER(?) AND LOWER(interface) = LOWER(?)", (node_id, port, name, iface)).fetchone()
        if dup:
            raise HTTPException(status_code=409, detail=f"Interface {iface} pada {name} sudah dipakai port {dup['port_core']}")
        before = cursor.execute("SELECT * FROM otb_port_devices WHERE asset_id = ? AND port_core = ?", (node_id, port)).fetchone()
        u = CURRENT_USER.get()
        cursor.execute("DELETE FROM otb_port_devices WHERE asset_id = ? AND port_core = ?", (node_id, port))
        cursor.execute(
            """INSERT INTO otb_port_devices (asset_id, port_core, purpose, device_type, vendor, device_name, interface,
                   vlan, service, customer_name, customer_node_id, notes, updated_by, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (node_id, port, purpose, dtype, vendor, name, iface, vlan, service, cust_name, cust_id, notes,
             (u["username"] if u else None) or "system", _now_str()))
        _audit(cursor, "UPDATE" if before else "CREATE", "NODE", node_id, n["name"],
               f"{'Ubah' if before else 'Catat'} perangkat {name} [{iface}] pada {n['name']} {port} ({purpose}"
               f"{', VLAN ' + vlan if vlan else ''}{', pelanggan ' + cust_name if cust_name else ''})")
        return {"message": "Perangkat port OTB tersimpan", "devices": _port_devices_of(cursor, node_id, labels)}


@app.delete("/api/nodes/{node_id}/port-devices")
def delete_port_device(node_id: int, port: str):
    with db() as conn:
        cursor = conn.cursor()
        n, labels = _pop_ports_of(cursor, node_id)
        port = _legacy_pop_port(n["capacity"], port)
        r = cursor.execute("SELECT * FROM otb_port_devices WHERE asset_id = ? AND port_core = ?", (node_id, port)).fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Tidak ada perangkat tercatat pada port ini")
        cursor.execute("DELETE FROM otb_port_devices WHERE id = ?", (r["id"],))
        _audit(cursor, "DELETE", "NODE", node_id, n["name"],
               f"Hapus catatan perangkat {r['device_name']} [{r['interface']}] dari {n['name']} {port}")
        return {"message": "Catatan perangkat dihapus", "devices": _port_devices_of(cursor, node_id, labels)}


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
def get_incidents(cluster: str = "ALL", area: str = "ALL"):
    sc, sp = _scope_conds(cluster, area, "i")
    with db() as conn:
        incidents = conn.execute(
            "SELECT i.*, (SELECT COUNT(*) FROM incident_impacts im WHERE im.incident_id = i.id) AS impact_count "
            "FROM incidents i" + _where(sc), sp).fetchall()
        repair_node = {}
        for r in conn.execute(
                "SELECT incident_id, node_id FROM incident_repairs WHERE node_id IS NOT NULL AND undone_at IS NULL ORDER BY id").fetchall():
            repair_node[r["incident_id"]] = r["node_id"]

    features = []
    for inc in incidents:
        item = _stamp(dict(inc), "reported_at", "resolved_at")
        item["repair_node_id"] = repair_node.get(item["id"])
        # 'tanpa dampak' = tidak mengubah status aset apa pun; hanya yang begini boleh disembunyikan dari peta
        item["no_effect"] = (item.get("impact_count") or 0) == 0
        item["map_hidden"] = bool(item.get("map_hidden")) and item["no_effect"]
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

        # Cluster/Area/Kota insiden: isian eksplisit > ikut aset terkait > nilai bawaan
        src_loc = None
        if cable_id is not None:
            src_loc = cursor.execute("SELECT cluster, area, city FROM cables WHERE id = ?", (cable_id,)).fetchone()
        elif node_id is not None:
            src_loc = cursor.execute("SELECT cluster, area, city FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if src_loc is None:
            # tanpa aset terkait: ikut node terdekat (maks. ~20 km) agar tetap masuk filter wilayah yang benar
            box = 0.2
            best = None
            for r in cursor.execute(
                    "SELECT cluster, area, city, latitude, longitude FROM nodes WHERE latitude BETWEEN ? AND ? "
                    "AND longitude BETWEEN ? AND ?", (lat - box, lat + box, lng - box, lng + box)).fetchall():
                d = _haversine_m(lat, lng, r["latitude"], r["longitude"])
                if d <= 20000 and (best is None or d < best[0]):
                    best = (d, r)
            if best:
                src_loc = best[1]
        inc_cluster = (inc.cluster or "").strip() or (src_loc and src_loc["cluster"]) or DEFAULT_CLUSTER
        inc_area = (inc.area or "").strip() or (src_loc and src_loc["area"]) or DEFAULT_AREA
        inc_city = (inc.city or "").strip() or (src_loc and src_loc["city"]) or DEFAULT_CITY

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
             inc_cluster, inc_area, inc_city,
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
                     "ticket": inc["ticket_number"], "repair": kind}, splice_all=payload.splice_all)

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
        "port_devices": [dict(r) for r in cursor.execute(
            "SELECT * FROM otb_port_devices WHERE asset_id = ?", (node_id,)).fetchall()],
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
        "via": [dict(r) for r in cursor.execute(
            "SELECT * FROM core_connections WHERE via_cable_id = ?", (cable_id,)).fetchall()],
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
        if t == "NODE" and (row["type"] or "").upper() == "POP":
            port = r[f"{side}_port_core"] = _legacy_pop_port(row["capacity"], port)
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
    for d in refs.get("port_devices") or []:
        d = dict(d)
        d.pop("id", None)
        _insert_row(cursor, "otb_port_devices", d)
    if refs.get("port_devices"):
        notes.append(f"{len(refs['port_devices'])} catatan perangkat OTB dipulihkan")
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
    full_via = [v for v in refs["via"] if "from_asset_type" in v]
    if full_via:                       # snapshot baru: sambungan utuh dipasang kembali
        r2, s2, n2 = _restore_connections(cursor, full_via)
        restored += r2; skipped += s2; cn = cn + n2
    for v in refs["via"]:              # snapshot lama: hanya id + core
        if "from_asset_type" in v:
            continue
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

    allc = cursor.execute(
        "SELECT * FROM core_connections WHERE (from_asset_type = 'NODE' AND from_asset_id = ?) "
        "OR (to_asset_type = 'NODE' AND to_asset_id = ?)", (N["id"], N["id"])).fetchall()
    full_ids = [c["id"] for c in allc if (c["notes"] or "") == FULLSPLICE_TAG]   # splice penuh bagian dari perbaikan
    conns = [c for c in allc if c["id"] not in full_ids]
    out["full_ids"] = full_ids
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
    out.update(ok=True, split=True, A=A, B=B, N=N, pairs=pairs, full_ids=full_ids)
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
    for fid in plan.get("full_ids", []):
        cursor.execute("DELETE FROM core_connections WHERE id = ?", (fid,))
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
        if lat is not None and lng is not None:
            # 1) cari DI DALAM area peta (kotak ~ +-0.5 derajat) supaya alamat generik tidak nyasar ke kota lain
            box = f"&viewbox={lng - 0.5},{lat + 0.5},{lng + 0.5},{lat - 0.5}"
            res = _normalize_nominatim(_geocode_http_get(url + box + "&bounded=1"))
            if res:
                return res
            # 2) tidak ada di area itu: cari ke seluruh Indonesia (UI menandai hasil yang jauh)
            return _normalize_nominatim(_geocode_http_get(url + box))
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


# =====================================================================================
# ROUND 9 - A. SEMBUNYIKAN INSIDEN YANG TIDAK MENGUBAH ASET DARI PETA
# =====================================================================================
# Insiden yang tidak mengubah status aset apa pun (tidak ada baris di incident_impacts) hanyalah
# penanda di peta. Penanda ini boleh disembunyikan/ditampilkan kembali. Insiden yang masih
# berdampak pada aset TIDAK boleh disembunyikan (selalu tampil); bila sebuah insiden yang
# disembunyikan kelak berdampak pada aset, ia otomatis tampil lagi (lihat get_incidents).
class MapVisibility(BaseModel):
    hidden: bool


class MapVisibilityBulk(BaseModel):
    hidden: bool
    ids: Optional[List[int]] = None   # kosong = semua insiden yang memenuhi syarat


def _incident_impact_count(cursor, incident_id: int) -> int:
    return cursor.execute("SELECT COUNT(*) FROM incident_impacts WHERE incident_id = ?",
                          (incident_id,)).fetchone()[0]


@app.put("/api/incidents/{incident_id}/map-visibility")
def set_incident_map_visibility(incident_id: int, payload: MapVisibility):
    with db() as conn:
        cursor = conn.cursor()
        inc = cursor.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        if not inc:
            raise HTTPException(status_code=404, detail="Incident tidak ditemukan")
        n = _incident_impact_count(cursor, incident_id)
        if payload.hidden and n > 0:
            raise HTTPException(
                status_code=409,
                detail=f"Insiden ini masih mengubah status {n} aset, jadi tidak bisa disembunyikan dari peta")
        cursor.execute("UPDATE incidents SET map_hidden = ? WHERE id = ?", (1 if payload.hidden else 0, incident_id))
        msg = "Penanda disembunyikan dari peta" if payload.hidden else "Penanda ditampilkan kembali di peta"
        _log_event(cursor, incident_id, "MAP_VISIBILITY", msg, actor=_current_username())
        _audit(cursor, "UPDATE", "INCIDENT", incident_id, inc["ticket_number"],
               f"{msg}: {inc['ticket_number']}", {"map_hidden": [bool(inc["map_hidden"]), payload.hidden]})
    return {"message": msg, "id": incident_id, "map_hidden": payload.hidden}


@app.post("/api/incidents/map-visibility")
def set_incidents_map_visibility(payload: MapVisibilityBulk):
    """Massal: sembunyikan semua insiden tanpa dampak (atau tampilkan semua yang disembunyikan)."""
    with db() as conn:
        cursor = conn.cursor()
        if payload.ids is not None:
            rows = []
            for iid in payload.ids[:1000]:
                r = cursor.execute("SELECT id, ticket_number, map_hidden FROM incidents WHERE id = ?", (iid,)).fetchone()
                if r:
                    rows.append(r)
        else:
            rows = cursor.execute("SELECT id, ticket_number, map_hidden FROM incidents").fetchall()
        changed = skipped = 0
        for r in rows:
            if payload.hidden:
                if _incident_impact_count(cursor, r["id"]) > 0:
                    skipped += 1
                    continue
                if r["map_hidden"]:
                    continue
            elif not r["map_hidden"]:
                continue
            cursor.execute("UPDATE incidents SET map_hidden = ? WHERE id = ?", (1 if payload.hidden else 0, r["id"]))
            changed += 1
        if changed:
            word = "Menyembunyikan" if payload.hidden else "Menampilkan kembali"
            _audit(cursor, "UPDATE", "INCIDENT", None, None, f"{word} {changed} penanda insiden di peta")
    return {"changed": changed, "skipped_with_impact": skipped, "hidden": payload.hidden}


# =====================================================================================
# ROUND 9 - B. ATURAN PERENCANAAN, ROUTING JALAN (OSRM), DAN PERENCANAAN PASANG BARU
# =====================================================================================
# Rute kabel mengikuti jalan lewat layanan OSRM (lewat server ini: ada cache + pembatasan laju).
# Bila layanan tidak terjangkau, rute otomatis jatuh ke garis lurus dan itu dicatat sebagai peringatan.
#   NETGIS_ROUTER      = osrm (default) | off  (off = selalu garis lurus)
#   NETGIS_ROUTER_URL  = alamat layanan routing sendiri, mis. http://osrm.lokal:5000/route/v1/driving
ROUTER = os.environ.get("NETGIS_ROUTER", "osrm").strip().lower()
ROUTER_URL = os.environ.get("NETGIS_ROUTER_URL", "").strip()
ROUTE_CACHE_TTL = 3600
ROUTE_CACHE_MAX = 300
ROUTE_MIN_INTERVAL = 1.0           # detik antar-panggilan keluar
ROUTE_USER_PER_MIN = 40
MAX_ROUTE_KM = 100.0
_route_cache = {}
_route_last_call = [0.0]
_route_user_hits = {}
_route_lock = threading.Lock()

DEFAULT_PLAN_RULES = {
    "pole_spacing_m": 50,        # tiang baru setiap N meter (kabel udara)
    "hh_spacing_m": 100,         # handhole setiap N meter (kabel tanah)
    "slack_length_m": 20,        # panjang cadangan per slack
    "slack_count": 2,            # jumlah slack per jalur (ujung awal & ujung akhir, sisanya merata)
    "pole_type": "Tiang 7m",
    "hh_type": "HH Standar",
    "customer_capacity": "2 Core",
    "reuse_radius_m": 8,         # tiang/HH eksisting dalam radius ini dipakai ulang, tidak dibuat baru
    "drop_max_m": 1000,          # panjang maksimum dropcore; lebih dari ini perlu kabel distribusi + closure (+ ODP) baru
    "hub_distance_m": 200,       # jarak closure/ODP baru dari pelanggan (mode "sedekat mungkin")
    "otb_cable_capacity": "12C", # kapasitas kabel udara minimum bila diterminasi OTB di lokasi pelanggan
    # jenis kabel menurut panjang rute: dipakai tingkat pertama yang panjangnya < max_m
    "cable_tiers": [
        {"max_m": 1000, "type": "Drop", "label": "Kabel Dropcore", "capacity": "2C"},
        {"max_m": 5000, "type": "Distribution", "label": "Kabel Distribusi", "capacity": "24C"},
        {"max_m": 20000, "type": "Feeder", "label": "Kabel Feeder", "capacity": "48C"},
        {"max_m": None, "type": "Backbone", "label": "Kabel Backbone", "capacity": "96C"},
    ],
}


def _validate_plan_rules(rules: dict) -> dict:
    """Gabungkan dengan bawaan + validasi. Kunci tak dikenal dibuang."""
    out = json.loads(json.dumps(DEFAULT_PLAN_RULES))
    src = rules if isinstance(rules, dict) else {}

    def num(key, lo, hi, integer=False):
        if key in src and src[key] is not None:
            try:
                v = float(src[key])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"Aturan '{key}' harus berupa angka")
            if not (lo <= v <= hi):
                raise HTTPException(status_code=400, detail=f"Aturan '{key}' harus antara {lo} dan {hi}")
            out[key] = int(v) if integer or v == int(v) else v

    num("pole_spacing_m", 10, 500)
    num("hh_spacing_m", 20, 1000)
    num("slack_length_m", 0, 200)
    num("slack_count", 0, 10, True)
    num("reuse_radius_m", 0, 50)
    num("drop_max_m", 100, 5000)
    num("hub_distance_m", 20, 1000)
    if src.get("otb_cable_capacity"):
        oc = str(src["otb_cable_capacity"]).strip().upper()
        if not re.match(r"^\d{1,3}C$", oc):
            raise HTTPException(status_code=400, detail="Aturan 'otb_cable_capacity' harus berformat seperti 12C")
        out["otb_cable_capacity"] = oc
    for key in ("pole_type", "hh_type", "customer_capacity"):
        if src.get(key):
            out[key] = str(src[key]).strip()[:40]
    tiers = src.get("cable_tiers")
    if tiers is not None:
        if not isinstance(tiers, list) or not (1 <= len(tiers) <= 8):
            raise HTTPException(status_code=400, detail="Aturan jenis kabel butuh 1-8 tingkat")
        clean, last = [], 0.0
        for i, t in enumerate(tiers):
            if not isinstance(t, dict) or t.get("type") not in CABLE_TYPES:
                raise HTTPException(status_code=400, detail=f"Tingkat kabel #{i + 1}: jenis harus salah satu dari {', '.join(sorted(CABLE_TYPES))}")
            cap = str(t.get("capacity") or "").strip().upper()
            if not re.match(r"^\d{1,3}C$", cap):
                raise HTTPException(status_code=400, detail=f"Tingkat kabel #{i + 1}: kapasitas harus berformat seperti 12C")
            mx = t.get("max_m")
            if i == len(tiers) - 1:
                mx = None
            else:
                try:
                    mx = float(mx)
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail=f"Tingkat kabel #{i + 1}: batas panjang (m) wajib diisi")
                if mx <= last:
                    raise HTTPException(status_code=400, detail="Batas panjang tiap tingkat harus makin besar")
                last = mx
            clean.append({"max_m": mx, "type": t["type"], "capacity": cap,
                          "label": str(t.get("label") or f"Kabel {t['type']}")[:40]})
        out["cable_tiers"] = clean
    return out


def _get_setting(cursor, key):
    r = cursor.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    try:
        return json.loads(r["value"]) if r else None
    except (TypeError, ValueError):
        return None


def _plan_rules(cursor, override=None) -> dict:
    base = _get_setting(cursor, "plan_rules") or {}
    merged = dict(base)
    if isinstance(override, dict):
        merged.update({k: v for k, v in override.items() if v is not None})
    return _validate_plan_rules(merged)


class PlanRulesPayload(BaseModel):
    rules: dict


@app.get("/api/plan/rules")
def get_plan_rules():
    with db() as conn:
        cursor = conn.cursor()
        saved = _get_setting(cursor, "plan_rules")
        return {"rules": _plan_rules(cursor), "defaults": DEFAULT_PLAN_RULES, "customized": saved is not None}


@app.put("/api/plan/rules")
def put_plan_rules(payload: PlanRulesPayload):
    clean = _validate_plan_rules(payload.rules)
    with db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO settings (key, value, updated_at, updated_by) VALUES ('plan_rules', ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at, "
            "updated_by = excluded.updated_by",
            (json.dumps(clean), _now_str(), _current_username()))
        _audit(cursor, "UPDATE", "SETTING", None, "plan_rules", "Mengubah aturan bawaan perencanaan pasang baru")
    return {"message": "Aturan perencanaan disimpan", "rules": clean}


def _router_http_get(url: str):
    """Dipisah agar mudah ditiru saat pengujian."""
    ua = "NETGIS-Enterprise/1.0" + (f" ({GEOCODER_CONTACT})" if GEOCODER_CONTACT else "")
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _straight_route(points):
    return [[p[1], p[0]] for p in points]


def _route_between(points) -> dict:
    """
    points: [(lat, lng), ...] minimal 2 titik (asal, titik singgah..., tujuan).
    Hasil: {coords [[lng,lat],...], source 'osrm'|'lurus', note}.
    Tidak pernah melempar error jaringan: gagal -> garis lurus + catatan.
    """
    pts = [(round(float(a), 6), round(float(b), 6)) for a, b in points]
    if ROUTER in ("off", "", "none"):
        return {"coords": _straight_route(pts), "source": "lurus", "note": "Routing jalan dinonaktifkan admin; memakai garis lurus"}
    key = (ROUTER_URL, tuple(pts))
    now = time.time()
    u = CURRENT_USER.get()
    with _route_lock:
        hit = _route_cache.get(key)
        if hit and now - hit[0] < ROUTE_CACHE_TTL:
            return dict(hit[1])
        uid = u["id"] if u else 0
        dq = _route_user_hits.setdefault(uid, deque())
        while dq and now - dq[0] > 60:
            dq.popleft()
        if len(dq) >= ROUTE_USER_PER_MIN:
            return {"coords": _straight_route(pts), "source": "lurus",
                    "note": "Terlalu banyak permintaan rute, sementara memakai garis lurus"}
        dq.append(now)
        wait = ROUTE_MIN_INTERVAL - (now - _route_last_call[0])
        _route_last_call[0] = now + max(wait, 0)
    if wait > 0:
        time.sleep(min(wait, ROUTE_MIN_INTERVAL))
    base = ROUTER_URL or "https://router.project-osrm.org/route/v1/driving"
    path = ";".join(f"{lng},{lat}" for lat, lng in pts)
    try:
        data = _router_http_get(f"{base.rstrip('/')}/{path}?overview=full&geometries=geojson&steps=false")
        if data.get("code") != "Ok" or not data.get("routes"):
            raise ValueError(data.get("code") or "tidak ada rute")
        geom = data["routes"][0]["geometry"]["coordinates"]
        coords = [[float(c[0]), float(c[1])] for c in geom if len(c) >= 2]
        if len(coords) < 2:
            raise ValueError("geometri kosong")
        # OSRM menempelkan titik ke jalan: sambungkan ujungnya ke titik asli supaya jalur tetap tepat di aset
        if _haversine_m(pts[0][0], pts[0][1], coords[0][1], coords[0][0]) > 1.5:
            coords.insert(0, [pts[0][1], pts[0][0]])
        if _haversine_m(pts[-1][0], pts[-1][1], coords[-1][1], coords[-1][0]) > 1.5:
            coords.append([pts[-1][1], pts[-1][0]])
        res = {"coords": coords, "source": "osrm", "note": None}
    except Exception as exc:   # jaringan/timeout/format
        print(f"[WARN] Routing gagal: {exc}")
        res = {"coords": _straight_route(pts), "source": "lurus",
               "note": "Layanan rute jalan tidak dapat dihubungi; jalur memakai garis lurus"}
        return res            # kegagalan tidak di-cache
    with _route_lock:
        while len(_route_cache) >= ROUTE_CACHE_MAX:
            _route_cache.pop(next(iter(_route_cache)))
        _route_cache[key] = (now, dict(res))
    return res


def _points_along(coords, dists):
    """Titik (lat, lng) pada polyline [[lng,lat],...] untuk tiap jarak (m) dari awal; dists terurut naik."""
    seg = [_haversine_m(a[1], a[0], b[1], b[0]) for a, b in zip(coords, coords[1:])]
    total = sum(seg)
    out, i, acc = [], 0, 0.0
    for d in dists:
        d = max(0.0, min(d, total))
        while i < len(seg) - 1 and acc + seg[i] < d:
            acc += seg[i]
            i += 1
        t = 0.0 if seg[i] == 0 else max(0.0, min(1.0, (d - acc) / seg[i]))
        a, b = coords[i], coords[i + 1]
        out.append((a[1] + (b[1] - a[1]) * t, a[0] + (b[0] - a[0]) * t))
    return out


def _pick_tier(rules: dict, length_m: float) -> dict:
    for t in rules["cable_tiers"]:
        if t["max_m"] is None or length_m < t["max_m"]:
            return t
    return rules["cable_tiers"][-1]


# --- Ketersediaan kapasitas pada sebuah node (dipakai Cek Coverage dan perencanaan) ---
def _cables_at_node(cursor, node, cables) -> list:
    """Kabel yang berujung/melewati node: lewat from/to_node_id, ujung kabel <= NODE_SNAP_M, atau (slack/closure/HH)
    kabel yang melintas <= 15 m. cables: baris kabel (sekali muat)."""
    out = []
    for c in cables:
        coords = _cable_coords(c)
        if not coords:
            continue
        end_is_to = c["to_node_id"] == node["id"] or _haversine_m(
            node["latitude"], node["longitude"], coords[-1][1], coords[-1][0]) <= NODE_SNAP_M
        start_is_from = c["from_node_id"] == node["id"] or _haversine_m(
            node["latitude"], node["longitude"], coords[0][1], coords[0][0]) <= NODE_SNAP_M
        passes = False
        if not (end_is_to or start_is_from) and (node["type"] or "").upper() in ("SLACK", "CLOSURE", "HH"):
            passes = _snap_to_polyline(coords, node["latitude"], node["longitude"])["offset_m"] <= 15.0
        if end_is_to or start_is_from or passes:
            out.append({"row": c, "at_end": end_is_to, "at_start": start_is_from})
    return out


def _node_availability(cursor, node, cables) -> dict:
    """
    Ketersediaan untuk sambungan baru.
      ODP     : port OUT yang masih kosong
      CLOSURE : core kosong pada kabel yang berujung/melewati closure (diambil yang terbanyak)
      SLACK   : idem; ditandai 'ujung kabel' bila slack berada di ujung hilir sebuah kabel
    """
    t = (node["type"] or "").upper()
    res = {"kind": t, "free": 0, "total": 0, "eligible": False, "reason": None, "detail": "", "cables": [],
           "at_cable_end": False}
    if node["status"] == "Cut/Broken":
        res["reason"] = "Aset sedang putus (Cut/Broken)"
    if t in ("ODP", "POP"):
        labels = _port_labels("NODE", t, node["capacity"])
        outs = [p for p in labels if p.startswith("OUT-")] if t == "ODP" else labels
        used = _used_ports(cursor, "NODE", node["id"])
        free = [p for p in outs if p not in used]
        res.update(free=len(free), total=len(outs),
                   detail=f"{len(free)} dari {len(outs)} {'port OUT' if t == 'ODP' else 'port/core'} kosong")
        if not res["reason"] and not free:
            res["reason"] = "Semua port sudah terpakai"
    elif t in ("CLOSURE", "SLACK"):
        best = 0
        for a in _cables_at_node(cursor, node, cables):
            c = a["row"]
            total = _core_total(c["capacity"])
            used = len(_used_ports(cursor, "CABLE", c["id"]))
            free = max(total - used, 0)
            res["cables"].append({"id": c["id"], "name": c["name"], "type": c["type"], "free": free, "total": total,
                                  "at_end": a["at_end"]})
            if a["at_end"]:
                res["at_cable_end"] = True
            best = max(best, free)
        res["cables"].sort(key=lambda x: -x["free"])
        res.update(free=best, total=max([c["total"] for c in res["cables"]] or [0]))
        if not res["cables"]:
            res["detail"] = "Tidak ada kabel terhubung"
            if not res["reason"]:
                res["reason"] = "Tidak ada kabel yang berujung/melewati aset ini"
        else:
            top = res["cables"][0]
            res["detail"] = f"{best} core kosong (kabel {top['name']})"
            if not res["reason"] and best == 0:
                res["reason"] = "Semua core kabel sudah terpakai"
    else:
        res["reason"] = "Jenis aset ini bukan titik sambung"
    res["eligible"] = res["reason"] is None
    return res


# --- Perencanaan Pasang Baru ---
class PlanRequest(BaseModel):
    name: Optional[str] = None
    origin_type: Optional[str] = "NODE"       # NODE | POINT
    origin_id: Optional[int] = None
    origin_lat: Optional[float] = Field(None, ge=-90, le=90)
    origin_lng: Optional[float] = Field(None, ge=-180, le=180)
    origin_name: Optional[str] = None
    dest_lat: float = Field(..., ge=-90, le=90)
    dest_lng: float = Field(..., ge=-180, le=180)
    dest_name: Optional[str] = None
    installation: Optional[str] = "Udara"
    via: Optional[List[List[float]]] = None   # titik singgah [[lat, lng], ...]
    rules: Optional[dict] = None
    create_customer: Optional[bool] = True
    use_poles: Optional[bool] = True          # False: tanpa tiang/handhole baru (mis. numpang infrastruktur eksisting)
    use_slack: Optional[bool] = True          # False: tanpa slack
    customer_cores: Optional[int] = 1         # layanan pelanggan: 1 core, 2 core (Tx-Rx dedicated) atau N core
    termination: Optional[str] = "DROPCORE_ROSET"   # DROPCORE_ROSET | UDARA_OTB (kabel udara + OTB di pelanggan)
    scenario: Optional[str] = "AUTO"          # AUTO (menurut batas dropcore) | DIRECT | HUB (distribusi baru + closure)
    hub_mode: Optional[str] = "NEAREST"       # NEAREST (sedekat mungkin ke pelanggan) | MAP (dipilih di peta)
    hub_lat: Optional[float] = Field(None, ge=-90, le=90)
    hub_lng: Optional[float] = Field(None, ge=-180, le=180)
    splitter: Optional[str] = None            # rasio splitter ODP baru: 1:4, 1:8, 2:8 (hanya layanan 1 core)


TERMINATIONS = ("DROPCORE_ROSET", "UDARA_OTB")
SCENARIOS = ("AUTO", "DIRECT", "HUB")
OTB_SIZES = (12, 24, 48, 96)


def _otb_size(n: int) -> int:
    return next((s for s in OTB_SIZES if s >= n), OTB_SIZES[-1])


def _split_polyline(coords, d):
    """Pecah polyline [[lng,lat],...] pada jarak d (m) dari awal -> (bagian1, bagian2, titik[lng,lat])."""
    seg = [_haversine_m(a[1], a[0], b[1], b[0]) for a, b in zip(coords, coords[1:])]
    d = max(0.0, min(d, sum(seg)))
    acc = 0.0
    for i, s in enumerate(seg):
        if acc + s >= d or i == len(seg) - 1:
            t = 0.0 if s == 0 else max(0.0, min(1.0, (d - acc) / s))
            a, b = coords[i], coords[i + 1]
            p = [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]
            return coords[:i + 1] + [p], [p] + coords[i + 1:], p
        acc += s
    return coords, coords[-1:], coords[-1]


def _segment_assets(rules, inst, coords, use_poles, use_slack, existing, tag):
    """Tiang/HH + slack sepanjang satu segmen kabel; tiang/HH eksisting dalam radius dipakai ulang."""
    length = _polyline_length_m(coords)
    slack_n, slack_len = (int(rules["slack_count"]) if use_slack else 0), float(rules["slack_length_m"])
    inset = min(5.0, length / 4)
    if slack_n <= 0:
        slack_d = []
    elif slack_n == 1:
        slack_d = [inset]
    else:
        slack_d = [inset + (length - 2 * inset) * i / (slack_n - 1) for i in range(slack_n)]
    passive_kind = "TIANG" if inst == "Udara" else "HH"
    spacing = float(rules["pole_spacing_m"] if inst == "Udara" else rules["hh_spacing_m"])
    pass_d, k = [], 1
    while use_poles and k * spacing < length - 2.0:
        pass_d.append(k * spacing)
        k += 1
    rr = float(rules["reuse_radius_m"])
    assets = []
    for d, (la, ln) in zip(pass_d, _points_along(coords, pass_d)):
        hit = None
        for e in existing.get(passive_kind, []):
            dd = _haversine_m(la, ln, e["latitude"], e["longitude"])
            if dd <= rr and (hit is None or dd < hit[0]):
                hit = (dd, e)
        if hit:
            la, ln = hit[1]["latitude"], hit[1]["longitude"]
        assets.append({"kind": passive_kind, "distance_m": round(d, 1), "latitude": la, "longitude": ln, "segment": tag,
                       "existing_id": hit[1]["id"] if hit else None, "existing_name": hit[1]["name"] if hit else None})
    for d, (la, ln) in zip(slack_d, _points_along(coords, slack_d)):
        assets.append({"kind": "SLACK", "distance_m": round(d, 1), "latitude": la, "longitude": ln, "segment": tag,
                       "existing_id": None, "existing_name": None})
    assets.sort(key=lambda a: (a["distance_m"], a["kind"]))
    new_p = sum(1 for a in assets if a["kind"] == passive_kind and not a["existing_id"])
    re_p = sum(1 for a in assets if a["kind"] == passive_kind and a["existing_id"])
    return {"length": length, "assets": assets, "slack_n": slack_n, "slack_len": slack_len, "slack_total": slack_n * slack_len,
            "total_cable": round(length + slack_n * slack_len, 1), "new_passive": new_p, "reuse_passive": re_p, "kind": passive_kind}


def _drop_capacity(cores: int) -> str:
    return "1C" if cores <= 1 else "2C" if cores == 2 else "4C" if cores <= 4 else "12C"


def _compute_plan(cursor, req: PlanRequest) -> dict:
    inst = req.installation or "Udara"
    if inst not in INSTALLATIONS:
        raise HTTPException(status_code=400, detail=f"Pemasangan harus salah satu dari: {', '.join(sorted(INSTALLATIONS))}")
    rules = _plan_rules(cursor, req.rules)
    warnings = []
    cores = int(req.customer_cores or 1)
    if not (1 <= cores <= 12):
        raise HTTPException(status_code=400, detail="Layanan pelanggan harus 1 sampai 12 core (2 core = Tx-Rx dedicated)")
    term = (req.termination or "DROPCORE_ROSET").upper()
    if term not in TERMINATIONS:
        raise HTTPException(status_code=400, detail="Terminasi harus DROPCORE_ROSET atau UDARA_OTB")
    scen_req = (req.scenario or "AUTO").upper()
    if scen_req not in SCENARIOS:
        raise HTTPException(status_code=400, detail="Skenario harus AUTO, DIRECT atau HUB")
    hub_mode = (req.hub_mode or "NEAREST").upper()
    if hub_mode not in ("NEAREST", "MAP"):
        raise HTTPException(status_code=400, detail="Mode penempatan hub harus NEAREST atau MAP")
    ratio = (req.splitter or "1:8").strip()
    if not re.match(r"^[12]:(4|8|16)$", ratio):
        raise HTTPException(status_code=400, detail="Splitter ODP baru harus 1:4, 1:8, 2:8 (atau 1:16)")
    rules["customer_capacity"] = f"{cores} Core"

    # --- asal ---
    origin = {"type": "POINT", "id": None, "name": (req.origin_name or "Titik asal"), "kind": None, "status": None}
    if (req.origin_type or "NODE").upper() == "NODE":
        if req.origin_id is None:
            raise HTTPException(status_code=400, detail="Aset asal wajib dipilih")
        n = cursor.execute("SELECT * FROM nodes WHERE id = ? AND type != 'INCIDENT'", (req.origin_id,)).fetchone()
        if not n:
            raise HTTPException(status_code=404, detail="Aset asal tidak ditemukan")
        if (n["type"] or "").upper() in ("TIANG", "HH", "PELANGGAN"):
            raise HTTPException(status_code=400, detail=f"Aset asal tidak boleh berjenis {n['type']}")
        origin.update(type="NODE", id=n["id"], name=n["name"], kind=n["type"], status=n["status"],
                      lat=n["latitude"], lng=n["longitude"], cluster=n["cluster"], area=n["area"], city=n["city"])
        cables = cursor.execute("SELECT * FROM cables").fetchall()
        av = _node_availability(cursor, n, cables)
        origin["availability"] = {k: av[k] for k in ("free", "total", "eligible", "reason", "detail", "at_cable_end")}
        if cores >= 2 and (n["type"] or "").upper() == "ODP":
            raise HTTPException(status_code=400, detail=f"Layanan {cores} core (dedicated) tidak bisa diambil dari ODP karena ODP hanya 1 core; pilih Closure/POP sebagai asal")
        if av["reason"]:
            warnings.append(f"Aset asal {n['name']}: {av['reason']}")
        elif n["status"] == "Maintenance":
            warnings.append(f"Aset asal {n['name']} sedang Maintenance")
    else:
        if req.origin_lat is None or req.origin_lng is None:
            raise HTTPException(status_code=400, detail="Koordinat asal wajib diisi")
        origin.update(lat=req.origin_lat, lng=req.origin_lng)
    dest = {"lat": req.dest_lat, "lng": req.dest_lng, "name": (req.dest_name or "").strip() or "Pelanggan baru"}
    suggest_odp = False
    if origin["type"] == "NODE":
        if origin["availability"]["eligible"] and origin["availability"]["free"] < cores and (origin.get("kind") or "").upper() != "ODP":
            warnings.append(f"Aset asal {origin['name']} hanya punya {origin['availability']['free']} port/core kosong; layanan butuh {cores} core")
        if (origin.get("kind") or "").upper() == "ODP" and origin["availability"]["free"] == 0:
            suggest_odp = True
            warnings.append("Port ODP asal penuh: BOQ menyertakan saran ODP baru + splitter; pindahkan titik asal atau pasang ODP baru")
        elif origin["availability"]["eligible"] and origin["availability"]["free"] == 1:
            warnings.append(f"Aset asal {origin['name']} tinggal 1 port/core kosong; penuh setelah rencana ini diwujudkan")

    via = []
    for v in (req.via or [])[:8]:
        if len(v) < 2 or not (-90 <= v[0] <= 90 and -180 <= v[1] <= 180):
            raise HTTPException(status_code=400, detail="Titik singgah tidak valid")
        via.append((v[0], v[1]))
    straight = _haversine_m(origin["lat"], origin["lng"], dest["lat"], dest["lng"])
    if straight < 3:
        raise HTTPException(status_code=400, detail="Titik asal dan tujuan hampir sama (< 3 m)")
    if straight > MAX_ROUTE_KM * 1000:
        raise HTTPException(status_code=400, detail=f"Jarak terlalu jauh (maks {MAX_ROUTE_KM:.0f} km)")

    # --- rute & skenario ---
    drop_max = float(rules["drop_max_m"])
    hub_pt = None
    if hub_mode == "MAP" and scen_req != "DIRECT":
        if req.hub_lat is None or req.hub_lng is None:
            if scen_req == "HUB":
                raise HTTPException(status_code=400, detail="Titik hub belum dipilih di peta")
        else:
            hub_pt = (req.hub_lat, req.hub_lng)
    rt_notes = []
    if hub_pt:
        rt1 = _route_between([(origin["lat"], origin["lng"])] + via + [hub_pt])
        rt2 = _route_between([hub_pt, (dest["lat"], dest["lng"])])
        c1, c2 = rt1["coords"], rt2["coords"]
        coords = c1 + c2[1:]
        rt = {"coords": coords, "source": rt1["source"] if rt1["source"] == rt2["source"] else "mixed", "note": rt1["note"] or rt2["note"]}
        scen = "HUB"
    else:
        rt = _route_between([(origin["lat"], origin["lng"])] + via + [(dest["lat"], dest["lng"])])
        coords = rt["coords"]
    length = _polyline_length_m(coords)
    if rt["note"]:
        warnings.append(rt["note"])
    if length > straight * 2.5 + 300 and rt["source"] == "osrm":
        warnings.append(f"Rute jalan {length / max(straight, 1):.1f}x lebih panjang dari garis lurus; pertimbangkan titik singgah")
    if not hub_pt:
        scen = "HUB" if scen_req == "HUB" or (scen_req == "AUTO" and length >= drop_max) else "DIRECT"
        if scen == "HUB":
            hd = min(float(rules["hub_distance_m"]), length * 0.5)
            c1, c2, hp = _split_polyline(coords, length - hd)
            hub_pt = (hp[1], hp[0])
    if scen == "HUB" and origin["type"] == "NODE" and length < 2 * 20:
        scen = "DIRECT"
    use_poles, use_slack = req.use_poles is not False, req.use_slack is not False
    existing = {k: [dict(r) for r in cursor.execute("SELECT id, name, latitude, longitude FROM nodes WHERE type = ?", (k,)).fetchall()]
                for k in ("TIANG", "HH")}
    notes = []
    seg_defs = []     # {tag, label, coords, type, cap, inst, tier_label}

    def tier_for_distribution(l):
        t = _pick_tier(rules, l)
        if t["type"] == "Drop":
            t = next((x for x in rules["cable_tiers"] if x["type"] != "Drop"), t)
        return t

    def last_mile(l, tag, label, c):
        if term == "UDARA_OTB":
            n_cap = _otb_size(max(cores, int(_cap_cores(rules["otb_cable_capacity"]) or 12)))
            return {"tag": tag, "label": label, "coords": c, "type": "Distribution", "cap": f"{n_cap}C", "inst": "Udara",
                    "tier_label": "Kabel Udara"}
        return {"tag": tag, "label": label, "coords": c, "type": "Drop", "cap": _drop_capacity(cores), "inst": inst,
                "tier_label": "Kabel Dropcore"}
    hub = None
    if scen == "DIRECT":
        seg_defs.append(last_mile(length, "S1", "Origin → pelanggan", coords))
        if term == "DROPCORE_ROSET" and length >= drop_max:
            warnings.append(f"Panjang {length:.0f} m melebihi batas dropcore {drop_max:.0f} m; pertimbangkan skenario distribusi baru + closure")
        notes.append("Skenario langsung: " + ("dropcore dari aset asal ke pelanggan dan roset di lokasi pelanggan" if term == "DROPCORE_ROSET"
                                              else f"kabel udara {seg_defs[0]['cap']} dari aset asal dan OTB {_otb_size(cores)} core di lokasi pelanggan (OTB sesuai kapasitas kabel)"))
    else:
        c1, c2 = (c1, c2)
        l1, l2 = _polyline_length_m(c1), _polyline_length_m(c2)
        t1 = tier_for_distribution(l1)
        seg_defs.append({"tag": "S1", "label": "Origin → hub (distribusi baru)", "coords": c1, "type": t1["type"], "cap": t1["capacity"],
                         "inst": inst, "tier_label": t1["label"]})
        seg_defs.append(last_mile(l2, "S2", "Hub → pelanggan", c2))
        if term == "DROPCORE_ROSET" and l2 >= drop_max:
            warnings.append(f"Hub → pelanggan {l2:.0f} m melebihi batas dropcore {drop_max:.0f} m; geser hub lebih dekat ke pelanggan")
        has_odp = cores == 1 and term == "DROPCORE_ROSET"
        hub = {"latitude": hub_pt[0], "longitude": hub_pt[1], "mode": hub_mode, "to_customer_m": round(l2, 1),
               "closure_capacity": "12 Core" if 2 * cores <= 12 else "24 Core", "closure_size": 12 if 2 * cores <= 12 else 24,
               "odp": ({"ratio": ratio, "capacity": f"{ratio.split(':')[0]} In - {ratio.split(':')[1]} Out"} if has_odp else None)}
        notes.append(f"Skenario >1 km: kabel distribusi baru {t1['capacity']} + closure di hub, "
                     + (f"ODP baru splitter {ratio}, lalu dropcore + roset ke pelanggan" if has_odp else
                        ("lalu dropcore dedicated + roset dari closure ke pelanggan" if term == "DROPCORE_ROSET"
                         else f"lalu kabel udara {seg_defs[1]['cap']} dari closure dengan OTB di pelanggan")))
        if cores >= 2:
            notes.append(f"Layanan {cores} core dedicated (Tx-Rx) diambil langsung dari closure, bukan dari ODP (ODP hanya 1 core)")
    notes.append(f"Layanan pelanggan: {cores} core" + (" (Tx-Rx dedicated end-to-end)" if cores >= 2 else " (1 core)"))
    segments, assets = [], []
    tot_len = tot_cab = tot_slack = tot_slack_n = 0.0
    sums = {"poles_new": 0, "poles_existing": 0, "hh_new": 0, "hh_existing": 0}
    for sd in seg_defs:
        sp = _segment_assets(rules, sd["inst"], sd["coords"], use_poles, use_slack, existing, sd["tag"])
        assets += sp["assets"]
        for a_ in sp["assets"]:
            a_["distance_m"] = a_["distance_m"] + (0 if sd["tag"] == "S1" else _polyline_length_m(seg_defs[0]["coords"]))
        tag = "poles" if sd["inst"] == "Udara" else "hh"
        sums[tag + "_new"] += sp["new_passive"]
        sums[tag + "_existing"] += sp["reuse_passive"]
        tot_len += sp["length"]
        tot_cab += sp["total_cable"]
        tot_slack += sp["slack_total"]
        tot_slack_n += sp["slack_n"]
        segments.append({"tag": sd["tag"], "label": sd["label"], "coords": sd["coords"], "cable_type": sd["type"], "cable_label": sd["tier_label"],
                         "cable_capacity": sd["cap"], "installation": sd["inst"], "route_length_m": round(sp["length"], 1),
                         "cable_total_m": sp["total_cable"], "slack_count": sp["slack_n"], "slack_length_m": sp["slack_len"],
                         "poles_new": sp["new_passive"] if sd["inst"] == "Udara" else 0, "poles_existing": sp["reuse_passive"] if sd["inst"] == "Udara" else 0,
                         "hh_new": sp["new_passive"] if sd["inst"] == "Tanah" else 0, "hh_existing": sp["reuse_passive"] if sd["inst"] == "Tanah" else 0})
    assets.sort(key=lambda a: (a["distance_m"], a["kind"]))
    first = segments[0]
    slack_len = float(rules["slack_length_m"])
    summary = {
        "route_length_m": round(length, 1), "straight_m": round(straight, 1),
        "slack_count": int(tot_slack_n), "slack_length_m": slack_len, "slack_total_m": tot_slack,
        "cable_total_m": round(tot_cab, 1), "cable_type": first["cable_type"], "cable_label": first["cable_label"],
        "cable_capacity": first["cable_capacity"], "installation": first["installation"],
        "poles_new": sums["poles_new"], "poles_existing": sums["poles_existing"],
        "hh_new": sums["hh_new"], "hh_existing": sums["hh_existing"],
        "customer": 1 if req.create_customer else 0, "route_source": rt["source"], "suggest_new_odp": suggest_odp,
        "use_poles": use_poles, "use_slack": use_slack,
        "scenario": scen, "termination": term, "customer_cores": cores, "segments": [{k: v for k, v in s_.items() if k != "coords"} for s_ in segments],
        "hub": hub, "new_odp": bool(hub and hub["odp"]), "splitter": ratio if hub and hub["odp"] else None,
        "closure_size": hub["closure_size"] if hub else None,
    }
    _bmap, _bset = _boq_cfg(cursor)
    extra_spl = [hub["odp"]["ratio"]] if hub and hub["odp"] else []
    extra_sp = (2 * cores) if hub else 0
    loss = _plan_loss(cursor, origin, tot_cab, bool(req.create_customer), int(_bset["splice_per_customer"]), extra_splitters=extra_spl, extra_splices=extra_sp)
    summary.update(loss_db=loss["total_db"], rx_dbm=loss["rx_dbm"], loss_status=loss["status"])
    if loss["status"] == "BAD":
        warnings.append(f"Redaman estimasi {loss['total_db']:.1f} dB (daya terima {loss['rx_dbm']:.1f} dBm) melebihi batas ONT {loss['params']['rx_min_dbm']:.0f} dBm; rute/penempatan tidak layak")
    elif loss["status"] == "WARN":
        warnings.append(f"Redaman estimasi {loss['total_db']:.1f} dB: sisa margin hanya {loss['margin_db']:.1f} dB (batas {loss['params']['margin_db']:.0f} dB)")
    passive_kind = "TIANG" if first["installation"] == "Udara" else "HH"
    boq = []
    for sg in segments:
        boq.append({"code": "CABLE", "label": f"{sg['cable_label']} {sg['cable_capacity']} ({sg['installation']}) - {sg['label']}",
                    "qty": sg["cable_total_m"], "unit": "m", "cable_type": sg["cable_type"]})
    if use_poles:
        if summary["poles_new"] or not summary["hh_new"]:
            boq.append({"code": "TIANG", "label": rules["pole_type"], "qty": summary["poles_new"], "unit": "unit"})
        if summary["hh_new"]:
            boq.append({"code": "HH", "label": rules["hh_type"], "qty": summary["hh_new"], "unit": "unit"})
    if use_slack:
        boq.append({"code": "SLACK", "label": f"Slack {slack_len:g} m", "qty": int(tot_slack_n), "unit": "unit"})
    if hub:
        boq.append({"code": "CLOSURE", "label": f"Closure {hub['closure_size']} core (hub)", "qty": 1, "unit": "unit"})
        if hub["odp"]:
            boq.append({"code": "ODP_BARU", "label": f"ODP baru, splitter {hub['odp']['ratio']}", "qty": 1, "unit": "unit"})
    if not use_poles:
        warnings.append("Tanpa tiang/handhole baru: pastikan kabel numpang pada infrastruktur eksisting atau jalur sudah tersedia")
    if req.create_customer:
        boq.append({"code": "PELANGGAN", "label": f"Titik pelanggan ({cores} core, " + (f"roset {cores} port" if term == 'DROPCORE_ROSET' else f"OTB {_otb_size(cores)} core") + ")",
                    "qty": 1, "unit": "unit"})
    summary["notes"] = notes
    return {"origin": origin, "dest": dest, "via": [list(v) for v in via],
            "route": {"coords": coords, "length_m": round(length, 1), "source": rt["source"]},
            "cable": {"type": first["cable_type"], "label": first["cable_label"], "capacity": first["cable_capacity"],
                      "installation": first["installation"], "length_m": round(first["route_length_m"], 1), "total_length_m": first["cable_total_m"]},
            "segments": segments, "hub": hub, "scenario": scen, "termination": term, "customer_cores": cores, "notes": notes,
            "assets": assets, "summary": summary, "boq_items": boq, "rules": rules, "loss": loss,
            "create_customer": bool(req.create_customer), "use_poles": use_poles, "use_slack": use_slack, "warnings": warnings}


@app.post("/api/plan/preview")
def preview_plan(req: PlanRequest):
    with db() as conn:
        return _compute_plan(conn.cursor(), req)


@app.post("/api/plans")
def save_plan(req: PlanRequest):
    with db() as conn:
        cursor = conn.cursor()
        result = _compute_plan(cursor, req)
        o, d, s = result["origin"], result["dest"], result["summary"]
        name = (req.name or "").strip() or f"Pasang baru {d['name']}"
        cursor.execute(
            """INSERT INTO plans (name, status, origin_type, origin_id, origin_name, dest_lat, dest_lng, dest_name,
                                  installation, summary, result, created_by)
               VALUES (?, 'Draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, o["type"], o["id"], o["name"], d["lat"], d["lng"], d["name"], s["installation"],
             json.dumps(s), json.dumps(result), _current_username()))
        pid = cursor.lastrowid
        _audit(cursor, "CREATE", "PLAN", pid, name,
               f"Rencana pasang baru '{name}': {o['name']} -> {d['name']}, {s['route_length_m']:.0f} m "
               f"kabel {s['cable_type']} ({s['installation']})")
    return {"message": "Rencana disimpan", "id": pid, "name": name, **result}


@app.get("/api/plans")
def list_plans():
    with db() as conn:
        rows = conn.execute("SELECT * FROM plans ORDER BY id DESC LIMIT 200").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["summary"] = json.loads(d["summary"]) if d.get("summary") else {}
        d.pop("result", None)
        out.append(_stamp(d, "created_at", "realized_at"))
    return out


def _load_plan(cursor, plan_id: int):
    r = cursor.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="Rencana tidak ditemukan")
    return r


@app.get("/api/plans/{plan_id}")
def get_plan(plan_id: int):
    with db() as conn:
        r = _load_plan(conn.cursor(), plan_id)
    d = dict(r)
    res = json.loads(d.pop("result"))
    d["summary"] = json.loads(d["summary"]) if d.get("summary") else {}
    d["realized"] = json.loads(d["realized_info"]) if d.get("realized_info") else None
    d.pop("realized_info", None)
    try:
        d["boq_adjust"] = json.loads(d["boq_adjust"]) if d.get("boq_adjust") else None
    except (TypeError, ValueError):
        d["boq_adjust"] = None
    return {**_stamp(d, "created_at", "realized_at"), "plan": res}


@app.delete("/api/plans/{plan_id}")
def delete_plan(plan_id: int):
    with db() as conn:
        cursor = conn.cursor()
        r = _load_plan(cursor, plan_id)
        if r["status"] != "Draft":
            raise HTTPException(status_code=409, detail="Hanya rencana berstatus Draft yang bisa dihapus")
        cursor.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
        _audit(cursor, "DELETE", "PLAN", plan_id, r["name"], f"Menghapus rencana pasang baru '{r['name']}'")
    return {"message": "Rencana dihapus"}


@app.post("/api/plans/{plan_id}/realize")
def realize_plan(plan_id: int):
    """Wujudkan rencana: buat tiang/HH/slack baru, titik pelanggan, dan kabel dari aset asal ke pelanggan.
    Alokasi core/port dilakukan sesudahnya di Detail Core."""
    with db() as conn:
        cursor = conn.cursor()
        r = _load_plan(cursor, plan_id)
        if r["status"] != "Draft":
            raise HTTPException(status_code=409, detail="Rencana ini sudah diwujudkan")
        plan = json.loads(r["result"])
        o, d, cab, rules = plan["origin"], plan["dest"], plan["cable"], plan["rules"]
        if o["type"] == "NODE":
            row = cursor.execute("SELECT * FROM nodes WHERE id = ?", (o["id"],)).fetchone()
            if not row:
                raise HTTPException(status_code=409, detail="Aset asal sudah tidak ada; buat rencana baru")
            cluster, area, city = row["cluster"], row["area"], row["city"]
        else:
            cluster, area, city = "EKO", "BANJARMASIN", "Kota Banjarmasin"
        prefix = f"PSB-{plan_id:04d}"

        def make_node(name, ntype, lat, lng, capacity):
            cursor.execute(
                """INSERT INTO nodes (name, type, status, latitude, longitude, cluster, area, city, capacity, spec_data)
                   VALUES (?, ?, 'Active', ?, ?, ?, ?, ?, ?, '{}')""",
                (name, ntype, lat, lng, cluster, area, city, capacity))
            nid = cursor.lastrowid
            _audit(cursor, "CREATE", "NODE", nid, name, f"Tambah {ntype} {name} (dari rencana {prefix})",
                   snapshot=_row_dict(cursor.execute("SELECT * FROM nodes WHERE id = ?", (nid,)).fetchone()))
            return nid

        created = {"nodes": [], "reused": []}
        counters = {}
        for a in plan["assets"]:
            if a.get("existing_id") and _exists(cursor, "nodes", a["existing_id"]):
                created["reused"].append(a["existing_id"])
                continue
            counters[a["kind"]] = counters.get(a["kind"], 0) + 1
            cap = {"TIANG": rules["pole_type"], "HH": rules["hh_type"],
                   "SLACK": f"Slack {rules['slack_length_m']:g}m"}[a["kind"]]
            created["nodes"].append(make_node(f"{prefix}-{a['kind']}-{counters[a['kind']]:02d}", a["kind"],
                                              a["latitude"], a["longitude"], cap))
        cust_id = None
        cores = int(plan.get("customer_cores") or 1)
        hub = plan.get("hub")
        if plan.get("create_customer"):
            cust_id = make_node(d["name"] if d["name"] != "Pelanggan baru" else f"{prefix}-PLG", "PELANGGAN",
                                d["lat"], d["lng"], f"{cores} Core")
            created["nodes"].append(cust_id)
        segs = plan.get("segments") or [{"tag": "S1", "coords": plan["route"]["coords"], "cable_type": cab["type"], "cable_capacity": cab["capacity"],
                                         "installation": cab["installation"]}]
        closure_id = odp_id = None
        if hub:
            closure_id = make_node(f"{prefix}-CLS", "CLOSURE", hub["latitude"], hub["longitude"], hub["closure_capacity"])
            created["nodes"].append(closure_id)
            if hub.get("odp"):
                odp_id = make_node(f"{prefix}-ODP", "ODP", hub["latitude"], hub["longitude"], hub["odp"]["capacity"])
                created["nodes"].append(odp_id)

        def make_cable(name, sg, from_id, to_id):
            cursor.execute(
                """INSERT INTO cables (name, type, status, geojson_geometry, cluster, area, city, capacity, core_data,
                                       installation, from_node_id, to_node_id)
                   VALUES (?, ?, 'Active', ?, ?, ?, ?, ?, '{}', ?, ?, ?)""",
                (name, sg["cable_type"], json.dumps({"type": "LineString", "coordinates": sg["coords"]}),
                 cluster, area, city, sg["cable_capacity"], sg["installation"], from_id, to_id))
            cid = cursor.lastrowid
            _audit(cursor, "CREATE", "CABLE", cid, name,
                   f"Tambah kabel {sg['cable_type']} {name} ({sg['cable_capacity']}, {sg['installation']}) dari rencana {prefix}",
                   snapshot=_row_dict(cursor.execute("SELECT * FROM cables WHERE id = ?", (cid,)).fetchone()))
            return cid
        if hub:
            cable_id = make_cable(f"{prefix}-S1-{segs[0]['cable_type'][:4].upper()}", segs[0], o["id"], closure_id)
            cable2_id = make_cable(f"{prefix}-S2-{segs[1]['cable_type'][:4].upper()}", segs[1], odp_id or closure_id, cust_id)
            cname = f"{prefix}-S1-{segs[0]['cable_type'][:4].upper()}"
            alloc = _allocate_hub(cursor, o["id"], cable_id, closure_id, odp_id, cable2_id, cust_id, cores) if o["type"] == "NODE" else \
                {"allocated": False, "reason": "Titik asal bebas (bukan aset); alokasi core manual"}
        else:
            cname = f"{prefix}-{segs[0]['cable_type'][:4].upper()}"
            cable_id = make_cable(cname, segs[0], o["id"], cust_id)
            cable2_id = None
            alloc = _allocate_new_customer(cursor, o["id"], cable_id, cust_id, cores) if o["type"] == "NODE" else \
                {"allocated": False, "reason": "Titik asal bebas (bukan aset); alokasi core manual"}
        info = {"cable_id": cable_id, "cable2_id": cable2_id, "closure_id": closure_id, "odp_id": odp_id, "cable_name": cname, "customer_id": cust_id,
                "node_ids": created["nodes"], "reused_ids": created["reused"], "allocation": alloc}
        cursor.execute("UPDATE plans SET status = 'Realized', realized_at = ?, realized_info = ? WHERE id = ?",
                       (_now_str(), json.dumps(info), plan_id))
        _audit(cursor, "UPDATE", "PLAN", plan_id, r["name"],
               f"Rencana '{r['name']}' diwujudkan: kabel {cname}, {len(created['nodes'])} aset baru")
    tail = (f"Core dialokasikan otomatis ({alloc.get('cores', 1)} core): {alloc['from_port']} -> {alloc['to_port']} lewat {alloc['via_core']}."
            if alloc.get("allocated") else f"Alokasi core manual di Detail Core ({alloc.get('reason')}).")
    return {"message": f"Rencana diwujudkan: kabel {cname} dan {len(created['nodes'])} aset baru dibuat. {tail}", **info}


# =====================================================================================
# ROUND 9 - C. CEK COVERAGE
# =====================================================================================
# Titik sambung yang dinilai: ODP (port OUT kosong), Closure (core kosong pada kabel yang berujung/melewatinya),
# dan Slack (idem; ditandai bila berada di ujung kabel). Aset yang putus/penuh tetap ditampilkan dengan alasannya.
COVERAGE_MAX_SEARCH_M = 10000.0
COVERAGE_ROUTE_TOP = 3


@app.get("/api/coverage")
def check_coverage(lat: float, lng: float, radius: float = 500, limit: int = 8, route: int = 1):
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise HTTPException(status_code=400, detail="Koordinat tidak valid")
    radius = max(50.0, min(float(radius), 3000.0))
    limit = max(1, min(int(limit), 15))
    with db() as conn:
        cursor = conn.cursor()
        cables = cursor.execute("SELECT * FROM cables").fetchall()
        rules = _plan_rules(cursor)
        near = []
        for n in cursor.execute(
                "SELECT * FROM nodes WHERE type IN ('ODP', 'CLOSURE', 'SLACK') "
                "AND latitude IS NOT NULL AND longitude IS NOT NULL").fetchall():
            d = _haversine_m(lat, lng, n["latitude"], n["longitude"])
            if d <= COVERAGE_MAX_SEARCH_M:
                near.append((d, n))
        near.sort(key=lambda x: x[0])
        cands = []
        for d, n in near[:60]:
            av = _node_availability(cursor, n, cables)
            cands.append({
                "node_id": n["id"], "name": n["name"], "type": n["type"], "status": n["status"],
                "latitude": n["latitude"], "longitude": n["longitude"],
                "cluster": n["cluster"], "city": n["city"],
                "distance_m": round(d, 1), "route_m": None, "route_source": None, "route_coords": None,
                "free": av["free"], "total": av["total"], "eligible": av["eligible"], "reason": av["reason"],
                "detail": av["detail"], "cables": av["cables"][:3], "at_cable_end": av["at_cable_end"],
            })
        # jarak rute jalan untuk beberapa kandidat layak terdekat
        if route:
            done = 0
            for c in cands:
                if not c["eligible"]:
                    continue
                if done >= COVERAGE_ROUTE_TOP:
                    break
                rt = _route_between([(c["latitude"], c["longitude"]), (lat, lng)])
                c["route_m"] = round(_polyline_length_m(rt["coords"]), 1)
                c["route_source"] = rt["source"]
                c["route_coords"] = rt["coords"]
                done += 1
        for c in cands:
            eff = c["route_m"] if c["route_m"] is not None else c["distance_m"]
            c["effective_m"] = eff
            c["in_range"] = eff <= radius
            tier = _pick_tier(rules, eff)
            c["suggested_cable"] = tier["label"]
            c["suggested_type"] = tier["type"]
        # layak dulu; yang berstatus Active didahulukan dari Maintenance; lalu menurut jarak
        cands.sort(key=lambda c: (not c["eligible"], c["status"] != "Active", c["effective_m"]))
        cands = cands[:limit]
    best = next((c for c in cands if c["eligible"] and c["in_range"]), None)
    nearest_ok = next((c for c in cands if c["eligible"]), None)
    if best:
        msg = f"Tercover: {best['name']} ({best['type']}) berjarak ±{best['effective_m']:.0f} m, {best['detail']}."
    elif nearest_ok:
        msg = (f"Di luar jangkauan {radius:.0f} m. Titik sambung layak terdekat: {nearest_ok['name']} "
               f"±{nearest_ok['effective_m']:.0f} m. Lanjutkan ke perencanaan Pasang Baru.")
    elif cands:
        msg = "Ada aset di sekitar, tetapi tidak ada yang bisa dipakai (penuh/putus). Pertimbangkan perencanaan Pasang Baru."
    else:
        msg = f"Tidak ada ODP/Closure/Slack dalam {COVERAGE_MAX_SEARCH_M / 1000:.0f} km."
    return {"point": {"latitude": lat, "longitude": lng}, "radius_m": radius, "covered": best is not None,
            "best_node_id": best["node_id"] if best else None,
            "nearest_eligible_node_id": nearest_ok["node_id"] if nearest_ok else None,
            "message": msg, "candidates": cands}


# =====================================================================================
# ROUND 9 - D. EXPORT
# =====================================================================================
EXPORT_FORMATS = {"geojson", "kml", "csv", "xlsx"}
EXPORT_SCOPES = {"all", "nodes", "cables", "connections", "incidents"}
_KML_COLORS = {   # aabbggrr
    "POP": "ff f6 5c 8b", "CLOSURE": "ff 0b 9e f5", "ODP": "ff 81 b9 10", "TIANG": "ff 5b 55 47",
    "HH": "ff 6e 76 0f", "SLACK": "ff 8b 72 6b", "PELANGGAN": "ff f1 a3 3b",
    "Backbone": "ff 8b 5c f6", "Feeder": "ff 0b 9e f5", "Distribution": "ff f6 82 3b", "Drop": "ff 81 b9 10",
}


def _kml_color(key, default="ff888888"):
    return (_KML_COLORS.get(key) or default).replace(" ", "")


def _csv_safe(v):
    """Cegah 'formula injection' di Excel: sel teks berawalan = + - @ diberi tanda kutip."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def _scope_ok(row, cluster, area):
    """Cocokkan Cluster/Area satu baris (tanpa beda huruf besar-kecil; kosong = nilai bawaan)."""
    rc, ra = _scope_key(row["cluster"], row["area"])
    if cluster and cluster.upper() != "ALL" and rc != cluster.strip().upper():
        return False
    return not (area and area.upper() != "ALL" and ra != area.strip().upper())


def _export_rows(cursor, scope, type_, status, cluster, installation, q, area="ALL"):
    ql = (q or "").strip().lower()
    node_names = {r["id"]: r["name"] for r in cursor.execute("SELECT id, name FROM nodes").fetchall()}
    nodes, cables = [], []
    want_nodes = scope in ("all", "nodes") and not (type_ in CABLE_TYPES or type_ == "CABLE")
    want_cables = scope in ("all", "cables") and (type_ == "ALL" or type_ == "CABLE" or type_ in CABLE_TYPES)
    if want_nodes:
        for r in cursor.execute("SELECT * FROM nodes WHERE type != 'INCIDENT' ORDER BY type, name").fetchall():
            if type_ != "ALL" and r["type"] != type_:
                continue
            if status != "ALL" and r["status"] != status:
                continue
            if not _scope_ok(r, cluster, area):
                continue
            if ql and ql not in f"{r['name']} {r['city']} {r['area']}".lower():
                continue
            nodes.append(dict(r))
    if want_cables:
        for r in cursor.execute("SELECT * FROM cables ORDER BY type, name").fetchall():
            d = dict(r)
            if type_ in CABLE_TYPES and d["type"] != type_:
                continue
            if status != "ALL" and d["status"] != status:
                continue
            if not _scope_ok(d, cluster, area):
                continue
            if installation == "NONE" and d.get("installation"):
                continue
            if installation in INSTALLATIONS and d.get("installation") != installation:
                continue
            if ql and ql not in f"{d['name']} {d['city']} {d['area']}".lower():
                continue
            coords = _cable_coords(r)
            if not coords:
                continue
            d["coords"] = coords
            d["length_m"] = round(_polyline_length_m(coords), 1)
            d["from_node"] = node_names.get(d.get("from_node_id"))
            d["to_node"] = node_names.get(d.get("to_node_id"))
            cables.append(d)
    return nodes, cables


INCIDENT_CSV_HEADER = ["nomor_tiket", "judul", "tingkat", "jenis", "status", "dilaporkan_utc", "selesai_utc",
                       "cluster", "area", "kota", "latitude", "longitude", "kabel_terkait", "node_terkait",
                       "posisi_dari_hulu_m", "core_terdampak", "pelapor", "sumber", "aset_terdampak",
                       "tampil_di_peta", "deskripsi", "resolusi"]


def _export_incidents(cursor, status, cluster, q, area="ALL"):
    """Insiden (sesuai filter) sebagai dict siap tulis. status hanya dipakai bila berupa status insiden."""
    ql = (q or "").strip().lower()
    out = []
    rows = cursor.execute(
        "SELECT i.*, c.name AS cable_name, n.name AS node_name, "
        "(SELECT COUNT(*) FROM incident_impacts im WHERE im.incident_id = i.id) AS impact_count "
        "FROM incidents i LEFT JOIN cables c ON c.id = i.linked_cable_id LEFT JOIN nodes n ON n.id = i.linked_node_id "
        "ORDER BY i.id DESC").fetchall()
    for r in rows:
        if status in INCIDENT_STATUSES and r["status"] != status:
            continue
        if not _scope_ok(r, cluster, area):
            continue
        if ql and ql not in f"{r['ticket_number']} {r['title']} {r['city']} {r['area']} {r['cable_name']} {r['node_name']}".lower():
            continue
        d = dict(r)
        try:
            cores = json.loads(d.get("affected_cores") or "null")
        except ValueError:
            cores = None
        d["cores_text"] = ", ".join(map(str, cores)) if cores else ""
        d["no_effect"] = (d.get("impact_count") or 0) == 0
        d["visible_text"] = "Disembunyikan" if d["no_effect"] and d.get("map_hidden") else "Tampil"
        out.append(d)
    return out


def _incident_cells(i):
    return [i["ticket_number"], i["title"], i["severity"], i["incident_type"], i["status"],
            _utc_iso(i.get("reported_at")), _utc_iso(i.get("resolved_at")),
            i["cluster"] or SCOPE_DEFAULTS["cluster"], i["area"] or SCOPE_DEFAULTS["area"], i["city"],
            i["latitude"], i["longitude"], i["cable_name"], i["node_name"],
            round(i["cable_position_m"], 1) if i.get("cable_position_m") is not None else None,
            i["cores_text"], i["reporter"], i["source"], i["impact_count"], i["visible_text"],
            i["description"], i.get("resolution")]


# ---------- XLSX tanpa dependensi (zipfile + XML) ----------
_XL_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_XL_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xl_col(i: int) -> str:
    out, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        out = chr(65 + r) + out
    return out


class XlFormula:
    """Sel berumus: hanya dibuat oleh kode ini (bukan dari input pengguna). `cached` = nilai terhitung agar tampil tanpa kalkulasi ulang."""
    def __init__(self, formula, cached):
        self.formula, self.cached = formula, cached


def _xl_cell(ref, v, style=0):
    if v is None or v == "":
        return ""
    st = f' s="{style}"' if style else ""
    if isinstance(v, XlFormula):
        c = v.cached if isinstance(v.cached, (int, float)) and not isinstance(v.cached, bool) else 0
        return f'<c r="{ref}"{st}><f>{_xml(v.formula)}</f><v>{c!r}</v></c>'
    if isinstance(v, bool):
        return f'<c r="{ref}"{st} t="b"><v>{int(v)}</v></c>'
    if isinstance(v, (int, float)):
        if isinstance(v, float) and not math.isfinite(v):
            return ""
        return f'<c r="{ref}"{st}><v>{v!r}</v></c>'
    txt = _XL_ILLEGAL.sub("", str(v))[:32000]
    return f'<c r="{ref}"{st} t="inlineStr"><is><t xml:space="preserve">{_xml(txt)}</t></is></c>'


def _xl_sheet_xml(header, rows) -> str:
    widths = [min(60, max(8, len(str(h)) + 2)) for h in header]
    for r in rows[:300]:
        for j, v in enumerate(r[:len(header)]):
            if v is not None and not isinstance(v, (int, float, XlFormula)):
                widths[j] = min(60, max(widths[j], len(str(v)) + 2))
    cols = "".join(f'<col min="{j + 1}" max="{j + 1}" width="{w}" customWidth="1"/>' for j, w in enumerate(widths))
    body = ['<row r="1">' + "".join(_xl_cell(f"{_xl_col(j)}1", h, 1) for j, h in enumerate(header)) + "</row>"]
    for i, r in enumerate(rows, start=2):
        body.append(f'<row r="{i}">' + "".join(_xl_cell(f"{_xl_col(j)}{i}", v) for j, v in enumerate(r)) + "</row>")
    last = f"{_xl_col(max(0, len(header) - 1))}{len(rows) + 1}"
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<worksheet xmlns="{_XL_NS}"><sheetViews><sheetView workbookViewId="0">'
            '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
            f'<sheetFormatPr defaultRowHeight="15"/><cols>{cols}</cols><sheetData>{"".join(body)}</sheetData>'
            f'<autoFilter ref="A1:{last}"/></worksheet>')


def _xlsx_bytes(sheets) -> bytes:
    """sheets: [(nama, header, rows)] -> berkas .xlsx (header tebal, baris pertama dibekukan, filter otomatis)."""
    names, used = [], set()
    for nm, _h, _r in sheets:
        base = re.sub(r"[\[\]:*?/\\]", " ", nm).strip()[:31] or "Sheet"
        cand, k = base, 2
        while cand.lower() in used:
            cand = f"{base[:28]} {k}"; k += 1
        used.add(cand.lower()); names.append(cand)
    n = len(sheets)
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
          + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, n + 1))
          + "</Types>")
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          f'<workbook xmlns="{_XL_NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
          + "".join(f'<sheet name="{_xml(nm)}" sheetId="{i}" r:id="rId{i}"/>' for i, nm in enumerate(names, start=1))
          + "</sheets></workbook>")
    wbrels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
              + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, n + 1))
              + f'<Relationship Id="rId{n + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              f'<styleSheet xmlns="{_XL_NS}"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
              '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font></fonts>'
              '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
              '<fill><patternFill patternType="solid"><fgColor rgb="FF0F766E"/><bgColor indexed="64"/></patternFill></fill></fills>'
              '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
              '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
              '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs>'
              '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", wbrels)
        z.writestr("xl/styles.xml", styles)
        for i, (_nm, h, r) in enumerate(sheets, start=1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _xl_sheet_xml(h, r))
    return buf.getvalue()


_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _xlsx_read(data: bytes):
    """.xlsx -> [(nama_sheet, [[sel,...],...])]. Sel berupa teks (angka dipertahankan sebagai teks aslinya)."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Berkas Excel rusak atau bukan .xlsx")
    with z:
        if sum(i.file_size for i in z.infolist()) > IMPORT_MAX_BYTES * 8:
            raise HTTPException(status_code=413, detail="Isi berkas Excel terlalu besar setelah diekstrak")

        def rd(name):
            try:
                raw = z.read(name)
            except KeyError:
                return None
            if re.search(rb"<!\s*(DOCTYPE|ENTITY)", raw[:4096], re.I):
                raise HTTPException(status_code=400, detail="Berkas Excel dengan DTD/ENTITY tidak diperbolehkan")
            try:
                return ET.fromstring(raw)
            except ET.ParseError as exc:
                raise HTTPException(status_code=400, detail=f"Isi berkas Excel tidak valid: {exc}")
        wb = rd("xl/workbook.xml")
        if wb is None:
            raise HTTPException(status_code=400, detail="Bukan berkas .xlsx yang valid (xl/workbook.xml tidak ada)")
        rel_root = rd("xl/_rels/workbook.xml.rels")
        targets = {}
        if rel_root is not None:
            for r in rel_root:
                t = r.get("Target") or ""
                targets[r.get("Id")] = t.lstrip("/") if t.startswith("/") else "xl/" + t
        shared = []
        sst = rd("xl/sharedStrings.xml")
        if sst is not None:
            for si in sst.iter(f"{{{_XL_NS}}}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{_XL_NS}}}t")))
        out = []
        for sh in wb.iter(f"{{{_XL_NS}}}sheet"):
            path = targets.get(sh.get(f"{{{_REL_NS}}}id"))
            root = rd(path) if path else None
            if root is None:
                continue
            rows = []
            for row in root.iter(f"{{{_XL_NS}}}row"):
                cells, maxc = {}, -1
                for c in row.findall(f"{{{_XL_NS}}}c"):
                    ref = c.get("r") or ""
                    m = re.match(r"([A-Z]+)", ref)
                    if m:
                        idx = 0
                        for ch in m.group(1):
                            idx = idx * 26 + (ord(ch) - 64)
                        idx -= 1
                    else:
                        idx = maxc + 1
                    t = c.get("t")
                    v = c.find(f"{{{_XL_NS}}}v")
                    if t == "inlineStr":
                        val = "".join(x.text or "" for x in c.iter(f"{{{_XL_NS}}}t"))
                    elif v is None or v.text is None:
                        val = ""
                    elif t == "s":
                        try:
                            val = shared[int(v.text)]
                        except (ValueError, IndexError):
                            val = ""
                    else:
                        val = v.text
                        if t not in ("str", "b", "e") and val.endswith(".0"):
                            val = val[:-2]
                    cells[idx] = val
                    maxc = max(maxc, idx)
                rows.append([cells.get(j, "") for j in range(maxc + 1)] if maxc >= 0 else [])
                if len(rows) > IMPORT_MAX_RECORDS + 50:
                    raise HTTPException(status_code=413, detail=f"Sheet '{sh.get('name')}' terlalu panjang (maks {IMPORT_MAX_RECORDS} baris)")
            out.append((sh.get("name") or "Sheet", rows))
        return out


def _decimate_coords(coords, max_chars=30000):
    """Excel membatasi isi sel 32.767 karakter: kurangi titik (ujung tetap) bila jalur terlalu rapat."""
    txt = _fmt_coord_list(coords)
    if len(txt) <= max_chars:
        return txt, False
    k = 2
    while True:
        sub = coords[::k]
        if sub[-1] != coords[-1]:
            sub = sub + [coords[-1]]
        txt = _fmt_coord_list(sub)
        if len(txt) <= max_chars or k > 64:
            return txt, True
        k += 1


def _scope_summary_rows(nodes, cables, incidents):
    """Rekap per Cluster/Area dari data yang diekspor."""
    agg = {}

    def row(c, a):
        k = _scope_key(c, a)
        return agg.setdefault(k, {"label": ((c or "").strip() or SCOPE_DEFAULTS["cluster"], (a or "").strip() or SCOPE_DEFAULTS["area"]),
                                  "nodes": 0, "odp": 0, "cables": 0, "len": 0.0, "inc": 0, "open": 0, "broken": 0})
    for n in nodes:
        d = row(n["cluster"], n["area"]); d["nodes"] += 1; d["odp"] += n["type"] == "ODP"; d["broken"] += n["status"] == "Cut/Broken"
    for c in cables:
        d = row(c["cluster"], c["area"]); d["cables"] += 1; d["len"] += c["length_m"]; d["broken"] += c["status"] == "Cut/Broken"
    for i in incidents:
        d = row(i["cluster"], i["area"]); d["inc"] += 1; d["open"] += i["status"] != "Resolved"
    rows = []
    for k in sorted(agg):
        d = agg[k]
        rows.append([d["label"][0], d["label"][1], d["nodes"], d["odp"], d["cables"], round(d["len"] / 1000, 3),
                     d["broken"], d["inc"], d["open"]])
    if rows:
        rows.append(["TOTAL", "", *[round(sum(r[j] for r in rows), 3) for j in range(2, 9)]])
    return ["Cluster", "Area", "Node", "ODP", "Kabel", "Panjang kabel (km)", "Aset Cut/Broken", "Insiden", "Insiden belum selesai"], rows


def _export_xlsx_sheets(cursor, nodes, cables, incidents, scope, filt_text):
    sheets, notes = [], []
    if scope in ("all", "nodes", "cables", "incidents"):
        h, rows = _scope_summary_rows(nodes, cables, incidents)
        sheets.append(("Rekap Cluster-Area", h, rows))
    if scope in ("all", "nodes"):
        sheets.append(("Node", ["id"] + NODE_CSV_HEADER,
                       [[n["id"], n["name"], n["type"], n["status"], n["latitude"], n["longitude"], n["cluster"],
                         n["area"], n["city"], n["capacity"]] for n in nodes]))
    if scope in ("all", "cables"):
        crow, cut = [], 0
        for c in cables:
            txt, dec = _decimate_coords(c["coords"])
            cut += dec
            crow.append([c["id"], c["name"], c["type"], c["status"], c.get("installation"), c["capacity"], c["cluster"],
                         c["area"], c["city"], c["length_m"], c["from_node"], c["to_node"], txt])
        if cut:
            notes.append(f"{cut} jalur kabel terlalu rapat untuk satu sel Excel sehingga titiknya dikurangi (ujung tetap)")
        sheets.append(("Kabel", ["id"] + CABLE_CSV_HEADER, crow))
    if scope in ("all", "incidents"):
        sheets.append(("Insiden", INCIDENT_CSV_HEADER, [_incident_cells(i) for i in incidents]))
    if scope in ("all", "connections"):
        parts = _connection_rows(cursor)
        sheets.append(("Sambungan Core", parts[0], parts[1]))
    info = [["Waktu ekspor (UTC)", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")],
            ["Pengguna", (CURRENT_USER.get() or {}).get("username")],
            ["Filter", filt_text or "Tanpa filter"],
            ["Node", len(nodes)], ["Kabel", len(cables)], ["Insiden", len(incidents)]] + [["Catatan", n] for n in notes]
    sheets.append(("Info", ["Keterangan", "Nilai"], info))
    return sheets


def _export_geojson(nodes, cables, incidents=()) -> bytes:
    feats = []
    for i in incidents:
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [i["longitude"], i["latitude"]]},
                      "properties": {"kind": "INCIDENT", "ticket": i["ticket_number"], "title": i["title"],
                                     "severity": i["severity"], "incident_type": i["incident_type"],
                                     "status": i["status"], "cluster": i["cluster"], "area": i["area"],
                                     "city": i["city"], "cable": i["cable_name"], "node": i["node_name"]}})
    for n in nodes:
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [n["longitude"], n["latitude"]]},
                      "properties": {"kind": "NODE", "id": n["id"], "name": n["name"], "type": n["type"],
                                     "status": n["status"], "cluster": n["cluster"], "area": n["area"],
                                     "city": n["city"], "capacity": n["capacity"]}})
    for c in cables:
        feats.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": c["coords"]},
                      "properties": {"kind": "CABLE", "id": c["id"], "name": c["name"], "type": c["type"],
                                     "status": c["status"], "installation": c.get("installation"),
                                     "cluster": c["cluster"], "area": c["area"], "city": c["city"],
                                     "capacity": c["capacity"], "length_m": c["length_m"],
                                     "from_node": c["from_node"], "to_node": c["to_node"]}})
    return json.dumps({"type": "FeatureCollection", "features": feats}, ensure_ascii=False).encode("utf-8")


def _xml(s) -> str:
    from xml.sax.saxutils import escape
    return escape("" if s is None else str(s))


def _export_kml(nodes, cables) -> bytes:
    def desc(pairs):
        rows = "".join(f"<tr><td><b>{_xml(k)}</b></td><td>{_xml(v)}</td></tr>" for k, v in pairs if v not in (None, ""))
        return f"<![CDATA[<table>{rows}</table>]]>"

    def ext(pairs):
        return "<ExtendedData>" + "".join(
            f'<Data name="{_xml(k)}"><value>{_xml(v)}</value></Data>' for k, v in pairs if v is not None) + "</ExtendedData>"

    styles = []
    for key in sorted({n["type"] for n in nodes} | {c["type"] for c in cables}):
        col = _kml_color(key)
        styles.append(f'<Style id="s-{_xml(key)}"><IconStyle><color>{col}</color><scale>0.9</scale></IconStyle>'
                      f'<LineStyle><color>{col}</color><width>3</width></LineStyle></Style>')
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>NETGIS Export</name>', *styles]
    by_type = {}
    for n in nodes:
        by_type.setdefault(("NODE", n["type"]), []).append(n)
    for c in cables:
        by_type.setdefault(("CABLE", c["type"]), []).append(c)
    for (kind, typ), items in sorted(by_type.items()):
        out.append(f"<Folder><name>{_xml(typ if kind == 'NODE' else 'Kabel ' + typ)}</name>")
        for it in items:
            pairs = [("kind", kind), ("type", it["type"]), ("status", it["status"]), ("cluster", it["cluster"]),
                     ("area", it["area"]), ("city", it["city"]), ("capacity", it["capacity"])]
            if kind == "CABLE":
                pairs += [("installation", it.get("installation")), ("length_m", it["length_m"])]
                geom = "<LineString><tessellate>1</tessellate><coordinates>" + " ".join(
                    f"{p[0]},{p[1]},0" for p in it["coords"]) + "</coordinates></LineString>"
            else:
                geom = f"<Point><coordinates>{it['longitude']},{it['latitude']},0</coordinates></Point>"
            out.append(f'<Placemark><name>{_xml(it["name"])}</name><styleUrl>#s-{_xml(typ)}</styleUrl>'
                       f'<description>{desc(pairs)}</description>{ext(pairs)}{geom}</Placemark>')
        out.append("</Folder>")
    out.append("</Document></kml>")
    return "\n".join(out).encode("utf-8")


def _csv_bytes(header, rows, delimiter) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delimiter, lineterminator="\r\n")
    w.writerow(header)
    for r in rows:
        w.writerow(["" if v is None else v for v in r])
    return ("﻿" + buf.getvalue()).encode("utf-8")


NODE_CSV_HEADER = ["name", "type", "status", "latitude", "longitude", "cluster", "area", "city", "capacity"]
CABLE_CSV_HEADER = ["name", "type", "status", "installation", "capacity", "cluster", "area", "city",
                    "length_m", "from_node", "to_node", "coordinates"]


def _fmt_coord_list(coords) -> str:
    return ";".join(f"{p[0]:.7f} {p[1]:.7f}" for p in coords)


def _connection_rows(cursor, safe=False):
    f = _csv_safe if safe else (lambda v: v)
    names = {("NODE", r["id"]): r["name"] for r in cursor.execute("SELECT id, name FROM nodes").fetchall()}
    names.update({("CABLE", r["id"]): r["name"] for r in cursor.execute("SELECT id, name FROM cables").fetchall()})
    rows = []
    for r in cursor.execute("SELECT * FROM core_connections ORDER BY id").fetchall():
        rows.append([r["id"], r["from_asset_type"], f(names.get((r["from_asset_type"], r["from_asset_id"]))),
                     r["from_port_core"], r["to_asset_type"],
                     f(names.get((r["to_asset_type"], r["to_asset_id"]))), r["to_port_core"],
                     f(names.get(("CABLE", r["via_cable_id"]))), r["via_core"], r["status"], f(r["notes"])])
    return ["id", "from_type", "from_name", "from_port_core", "to_type", "to_name", "to_port_core",
            "via_cable", "via_core", "status", "notes"], rows


def _export_csv_parts(cursor, nodes, cables, scope, delimiter, incidents=None) -> dict:
    parts = {}
    if scope in ("all", "nodes"):
        parts["nodes.csv"] = _csv_bytes(
            ["id"] + NODE_CSV_HEADER,
            [[n["id"], _csv_safe(n["name"]), n["type"], n["status"], n["latitude"], n["longitude"],
              _csv_safe(n["cluster"]), _csv_safe(n["area"]), _csv_safe(n["city"]), _csv_safe(n["capacity"])]
             for n in nodes], delimiter)
    if scope in ("all", "cables"):
        parts["cables.csv"] = _csv_bytes(
            ["id"] + CABLE_CSV_HEADER,
            [[c["id"], _csv_safe(c["name"]), c["type"], c["status"], c.get("installation"), _csv_safe(c["capacity"]),
              _csv_safe(c["cluster"]), _csv_safe(c["area"]), _csv_safe(c["city"]), c["length_m"],
              _csv_safe(c["from_node"]), _csv_safe(c["to_node"]), _fmt_coord_list(c["coords"])]
             for c in cables], delimiter)
    if scope in ("all", "connections"):
        head, rows = _connection_rows(cursor, safe=True)
        parts["connections.csv"] = _csv_bytes(head, rows, delimiter)
    if scope in ("all", "incidents"):
        parts["incidents.csv"] = _csv_bytes(
            INCIDENT_CSV_HEADER, [[_csv_safe(v) for v in _incident_cells(i)] for i in (incidents or [])], delimiter)
    return parts


@app.get("/api/export")
def export_data(format: str = "geojson", scope: str = "all", type: str = "ALL", status: str = "ALL",
                cluster: str = "ALL", installation: str = "ALL", q: str = "", delimiter: str = ",",
                area: str = "ALL"):
    """Unduh data jaringan. format: geojson | kml | csv | xlsx. scope: all | nodes | cables | connections | incidents.
    Filter (type/status/cluster/area/installation/q) sama dengan Asset Inventory. CSV 'all' = ZIP berisi file per jenis;
    XLSX = satu berkas dengan beberapa sheet (+ Rekap Cluster-Area). 'incidents' tersedia untuk CSV/XLSX/GeoJSON."""
    fmt, scope = (format or "").lower(), (scope or "").lower()
    if fmt not in EXPORT_FORMATS:
        raise HTTPException(status_code=400, detail=f"Format tidak dikenal. Pilihan: {', '.join(sorted(EXPORT_FORMATS))}")
    if scope not in EXPORT_SCOPES:
        raise HTTPException(status_code=400, detail=f"Cakupan tidak dikenal. Pilihan: {', '.join(sorted(EXPORT_SCOPES))}")
    if fmt in ("geojson", "kml") and scope == "connections":
        raise HTTPException(status_code=400, detail="Sambungan core hanya bisa diekspor sebagai CSV atau Excel")
    if fmt == "kml" and scope == "incidents":
        raise HTTPException(status_code=400, detail="Insiden bisa diekspor sebagai CSV, Excel, atau GeoJSON")
    if scope == "incidents" and status not in ("ALL", *INCIDENT_STATUSES):
        raise HTTPException(status_code=400, detail=f"Status insiden tidak valid. Pilihan: {', '.join(sorted(INCIDENT_STATUSES))}")
    delim = ";" if delimiter in (";", "semicolon") else "\t" if delimiter in ("\t", "tab") else ","
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    with db() as conn:
        cursor = conn.cursor()
        # status aset (Active/...) dan status insiden (Open/...) berbeda: masing-masing hanya memfilter jenisnya
        nodes, cables = _export_rows(cursor, scope, type, status if status in ASSET_STATUSES else "ALL",
                                     cluster, installation, q, area)
        incidents = []
        if scope in ("all", "incidents") and fmt in ("csv", "xlsx") or (scope == "incidents" and fmt == "geojson"):
            incidents = _export_incidents(cursor, status, cluster, q, area)
        filt = [f"{k}={v}" for k, v in (("cluster", cluster), ("area", area), ("jenis", type), ("status", status),
                                        ("pemasangan", installation), ("cari", (q or "").strip())) if v and v != "ALL"]
        if fmt == "geojson":
            body = _export_geojson(nodes, cables, incidents)
            media, fname = "application/geo+json", f"netgis_{scope}_{stamp}.geojson"
        elif fmt == "kml":
            body, media, fname = _export_kml(nodes, cables), "application/vnd.google-earth.kml+xml", f"netgis_{scope}_{stamp}.kml"
        elif fmt == "xlsx":
            body = _xlsx_bytes(_export_xlsx_sheets(cursor, nodes, cables, incidents, scope, ", ".join(filt)))
            media, fname = XLSX_MEDIA, f"netgis_{'semua' if scope == 'all' else scope}_{stamp}.xlsx"
        else:
            parts = _export_csv_parts(cursor, nodes, cables, scope, delim, incidents)
            if len(parts) == 1:
                (pname, body), = parts.items()
                media, fname = "text/csv; charset=utf-8", f"netgis_{pname[:-4]}_{stamp}.csv"
            else:
                zb = io.BytesIO()
                with zipfile.ZipFile(zb, "w", zipfile.ZIP_DEFLATED) as z:
                    for pname, pdata in parts.items():
                        z.writestr(pname, pdata)
                body, media, fname = zb.getvalue(), "application/zip", f"netgis_semua_{stamp}.zip"
        _audit(cursor, "EXPORT", "DATA", None, fname,
               f"Ekspor {fmt.upper()} ({scope}): {len(nodes)} node, {len(cables)} kabel, {len(incidents)} insiden"
               + (f" [{', '.join(filt)}]" if filt else ""))
    return Response(content=body, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{fname}"',
                             "X-Export-Nodes": str(len(nodes)), "X-Export-Cables": str(len(cables)),
                             "X-Export-Incidents": str(len(incidents))})


@app.get("/api/import/template")
def import_template(kind: str = "nodes", delimiter: str = ",", format: str = "csv"):
    delim = ";" if delimiter in (";", "semicolon") else ","
    if (format or "").lower() == "xlsx":
        ex_node = [["Contoh-ODP-01", "ODP", "Active", -3.323, 114.593, "EKO", "BANJARMASIN", "Kota Banjarmasin", "1 In - 8 Out"],
                   ["Contoh-Tiang-01", "TIANG", "Active", -3.3231, 114.5931, "EKO", "BANJARMASIN", "Kota Banjarmasin", "Tiang 7m"]]
        ex_cable = [["Contoh-Kabel-01", "Distribution", "Active", "Udara", "24C", "EKO", "BANJARMASIN", "Kota Banjarmasin",
                     None, None, None, "114.5930000 -3.3230000;114.5950000 -3.3240000;114.5970000 -3.3250000"]]
        guide = [["Sheet 'Node'", "Satu baris = satu titik (POP, CLOSURE, ODP, TIANG, HH, SLACK, PELANGGAN). Kolom wajib: name, type, latitude, longitude"],
                 ["Sheet 'Kabel'", "Satu baris = satu jalur. Kolom wajib: name, type (Backbone/Feeder/Distribution/Drop), coordinates"],
                 ["coordinates", "Pasangan 'bujur lintang' dipisah titik-koma: 114.593 -3.323;114.595 -3.324"],
                 ["status", "Active / Maintenance / Cut/Broken (kosong = Active)"],
                 ["cluster, area", "Dipakai untuk filter. Kosong = EKO / BANJARMASIN"],
                 ["Baris contoh", "Hapus baris contoh sebelum mengunggah"]]
        body = _xlsx_bytes([("Node", NODE_CSV_HEADER, ex_node), ("Kabel", CABLE_CSV_HEADER, ex_cable),
                            ("Petunjuk", ["Bagian", "Keterangan"], guide)])
        return Response(content=body, media_type=XLSX_MEDIA,
                        headers={"Content-Disposition": 'attachment; filename="template_import_netgis.xlsx"'})
    if kind == "cables":
        body = _csv_bytes(CABLE_CSV_HEADER, [
            ["Contoh-Kabel-01", "Distribution", "Active", "Udara", "24C", "EKO", "BANJARMASIN", "Kota Banjarmasin",
             "", "", "", "114.5930000 -3.3230000;114.5950000 -3.3240000;114.5970000 -3.3250000"]], delim)
        fname = "template_import_kabel.csv"
    elif kind == "nodes":
        body = _csv_bytes(NODE_CSV_HEADER, [
            ["Contoh-ODP-01", "ODP", "Active", "-3.3230000", "114.5930000", "EKO", "BANJARMASIN", "Kota Banjarmasin",
             "1 In - 8 Out"],
            ["Contoh-Tiang-01", "TIANG", "Active", "-3.3231000", "114.5931000", "EKO", "BANJARMASIN",
             "Kota Banjarmasin", "Tiang 7m"]], delim)
        fname = "template_import_node.csv"
    else:
        raise HTTPException(status_code=400, detail="kind harus 'nodes' atau 'cables'")
    return Response(content=body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# =====================================================================================
# ROUND 9 - E. IMPORT (GeoJSON, KML/KMZ, CSV) DENGAN PRATINJAU
# =====================================================================================
IMPORT_MAX_BYTES = 8_000_000
IMPORT_MAX_RECORDS = 5000
IMPORT_PREVIEW_ROWS = 300
DEFAULT_CLUSTER, DEFAULT_AREA, DEFAULT_CITY = "EKO", "BANJARMASIN", "Kota Banjarmasin"

NODE_TYPE_ALIASES = {
    "POP": "POP", "HEADEND": "POP", "HEAD END": "POP", "OLT": "POP",
    "CLOSURE": "CLOSURE", "JC": "CLOSURE", "JOINT CLOSURE": "CLOSURE", "JOINTCLOSURE": "CLOSURE", "JOINT": "CLOSURE",
    "ODP": "ODP", "TIANG": "TIANG", "TP": "TIANG", "POLE": "TIANG", "HH": "HH", "HANDHOLE": "HH", "HAND HOLE": "HH",
    "SLACK": "SLACK", "PELANGGAN": "PELANGGAN", "CUSTOMER": "PELANGGAN", "PLG": "PELANGGAN", "CUST": "PELANGGAN",
}
CABLE_TYPE_ALIASES = {
    "BACKBONE": "Backbone", "BB": "Backbone", "BKB": "Backbone",
    "FEEDER": "Feeder", "FDR": "Feeder",
    "DISTRIBUTION": "Distribution", "DISTRIBUSI": "Distribution", "DIST": "Distribution",
    "DROP": "Drop", "DROPCORE": "Drop", "DROP CORE": "Drop", "DRP": "Drop", "DC": "Drop",
}
STATUS_ALIASES = {
    "ACTIVE": "Active", "AKTIF": "Active", "NORMAL": "Active", "UP": "Active",
    "MAINTENANCE": "Maintenance", "PERAWATAN": "Maintenance",
    "CUT/BROKEN": "Cut/Broken", "CUT": "Cut/Broken", "BROKEN": "Cut/Broken", "PUTUS": "Cut/Broken", "RUSAK": "Cut/Broken",
}
INSTALL_ALIASES = {"UDARA": "Udara", "AERIAL": "Udara", "AU": "Udara", "TANAH": "Tanah", "UNDERGROUND": "Tanah",
                   "DUCT": "Tanah", "TT": "Tanah"}
NODE_DEFAULT_CAPACITY = {"ODP": "1 In - 8 Out", "TIANG": "Tiang 7m", "HH": "HH Standar", "SLACK": "Slack 20m",
                         "PELANGGAN": "2 Core", "POP": "48C", "CLOSURE": "24C"}
CABLE_DEFAULT_CAPACITY = {"Drop": "2C", "Distribution": "24C", "Feeder": "48C", "Backbone": "96C"}

# nama kolom CSV / properti (setelah dinormalkan: huruf kecil, spasi/tanda minus -> garis bawah)
_COL_ALIASES = {
    "name": {"name", "nama", "nama_aset", "asset_name", "label", "title"},
    "type": {"type", "tipe", "jenis", "jenis_aset", "kategori", "category"},
    "status": {"status", "kondisi"},
    "lat": {"lat", "latitude", "y"},
    "lng": {"lng", "lon", "long", "longitude", "x"},
    "cluster": {"cluster"},
    "area": {"area", "wilayah"},
    "city": {"city", "kota", "kabupaten"},
    "capacity": {"capacity", "kapasitas", "core", "jumlah_core"},
    "installation": {"installation", "pemasangan", "instalasi", "jenis_pemasangan"},
    "coordinates": {"coordinates", "koordinat", "geometry", "wkt", "path", "jalur", "geom"},
}


def _canon_key(k) -> str:
    s = re.sub(r"[\s\-]+", "_", str(k or "").strip().lower())
    for canon, names in _COL_ALIASES.items():
        if s in names:
            return canon
    return s


def _canon_props(props: dict) -> dict:
    out = {}
    for k, v in (props or {}).items():
        ck = _canon_key(k)
        if ck not in out and v not in (None, ""):
            out[ck] = v
    return out


def _to_float(v, decimal_comma=False):
    if v is None or v == "":
        return None
    s = str(v).strip()
    if decimal_comma and "," in s and "." not in s:
        s = s.replace(",", ".")
    try:
        f = float(s)
    except ValueError:
        return None
    return f if math.isfinite(f) else None


def _norm_node_type(v):
    return NODE_TYPE_ALIASES.get(re.sub(r"\s+", " ", str(v or "").strip().upper()))


def _norm_cable_type(v):
    return CABLE_TYPE_ALIASES.get(re.sub(r"\s+", " ", str(v or "").strip().upper()))


def _infer_from_name(name, aliases):
    for tok in re.split(r"[^A-Za-z0-9]+", str(name or "").upper()):
        if tok and tok in aliases:
            return aliases[tok]
    return None


def _norm_capacity_node(typ, v):
    s = str(v or "").strip()
    if not s:
        return NODE_DEFAULT_CAPACITY[typ], False
    if typ == "ODP":
        m = re.search(r"(\d+)\s*in\s*-\s*(\d+)\s*out", s, re.I)
        if m:
            return f"{int(m.group(1))} In - {int(m.group(2))} Out", False
        return NODE_DEFAULT_CAPACITY[typ], True
    if typ in ("POP", "CLOSURE"):
        m = re.match(r"^(\d+)\s*(c|core)?$", s, re.I)
        if m and int(m.group(1)) > 0:
            return f"{int(m.group(1))}C", False
        return NODE_DEFAULT_CAPACITY[typ], True
    if typ == "PELANGGAN":
        m = re.match(r"^(\d+)\s*(c|core)?$", s, re.I)
        if m and int(m.group(1)) > 0:
            return f"{int(m.group(1))} Core", False
        return NODE_DEFAULT_CAPACITY[typ], True
    return s[:40], False


def _norm_capacity_cable(typ, v):
    s = str(v or "").strip()
    if not s:
        return CABLE_DEFAULT_CAPACITY[typ], False
    m = re.match(r"^(\d+)\s*(c|core)?$", s, re.I)
    if m and 0 < int(m.group(1)) <= 1000:
        return f"{int(m.group(1))}C", False
    return CABLE_DEFAULT_CAPACITY[typ], True


def _valid_ll(lat, lng) -> bool:
    return lat is not None and lng is not None and -90 <= lat <= 90 and -180 <= lng <= 180


def _clean_coords(raw):
    """[[lng,lat,(alt)],...] -> [[lng,lat],...] atau None bila ada titik rusak."""
    out = []
    for p in raw or []:
        try:
            lng, lat = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError):
            return None
        if not _valid_ll(lat, lng):
            return None
        if not out or [lng, lat] != out[-1]:
            out.append([lng, lat])
    return out


def _parse_wkt_or_list(text, decimal_comma=False):
    """'LINESTRING(lng lat, ...)' atau 'lng lat;lng lat' atau 'lng,lat;lng,lat' -> [[lng,lat],...] / None."""
    s = str(text or "").strip()
    m = re.match(r"^\s*(?:SRID=\d+;)?LINESTRING\s*(?:Z\s*)?\((.*)\)\s*$", s, re.I | re.S)
    pairs = []
    if m:
        chunks = [c for c in m.group(1).split(",") if c.strip()]
    else:
        chunks = [c for c in re.split(r"[;|\n]", s) if c.strip()]
    for c in chunks:
        nums = [x for x in re.split(r"[\s,]+", c.strip()) if x]
        if len(nums) < 2:
            return None
        a, b = _to_float(nums[0]), _to_float(nums[1])
        if a is None or b is None:
            return None
        pairs.append([a, b])
    return _clean_coords(pairs)


def _decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _raw_from_geojson(text):
    try:
        gj = json.loads(text)
    except ValueError:
        raise HTTPException(status_code=400, detail="Berkas GeoJSON bukan JSON yang valid")
    feats = []
    if isinstance(gj, dict) and gj.get("type") == "FeatureCollection":
        feats = gj.get("features") or []
    elif isinstance(gj, dict) and gj.get("type") == "Feature":
        feats = [gj]
    elif isinstance(gj, dict) and gj.get("type") in ("Point", "LineString", "MultiLineString"):
        feats = [{"type": "Feature", "geometry": gj, "properties": {}}]
    else:
        raise HTTPException(status_code=400, detail="GeoJSON harus berisi FeatureCollection/Feature")
    raws = []
    for i, f in enumerate(feats):
        if not isinstance(f, dict):
            continue
        g, props = f.get("geometry") or {}, f.get("properties") or {}
        gt, co = g.get("type"), g.get("coordinates")
        base = {"src": f"fitur #{i + 1}", "props": props, "folder": None}
        if gt == "Point":
            raws.append({**base, "geom": "point", "coords": co})
        elif gt == "LineString":
            raws.append({**base, "geom": "line", "coords": co})
        elif gt == "MultiLineString":
            for j, part in enumerate(co or []):
                raws.append({**base, "src": f"fitur #{i + 1}.{j + 1}", "geom": "line", "coords": part,
                             "part": (j + 1) if len(co) > 1 else None})
        else:
            raws.append({**base, "geom": "unsupported", "coords": None, "gt": gt})
    return raws


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _raw_from_kml(text):
    if re.search(r"<!\s*(DOCTYPE|ENTITY)", text, re.I):
        raise HTTPException(status_code=400, detail="KML dengan DTD/ENTITY tidak diperbolehkan")
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError as exc:
        raise HTTPException(status_code=400, detail=f"Berkas KML tidak valid: {exc}")
    raws = []

    def kml_coords(el):
        txt = (el.findtext("{*}coordinates") or "").strip()
        pts = []
        for tok in txt.split():
            nums = tok.split(",")
            if len(nums) >= 2:
                pts.append(nums[:2])
        return pts

    def walk(el, folder):
        for ch in el:
            t = _local(ch.tag)
            if t in ("Folder", "Document"):
                nm = (ch.findtext("{*}name") or "").strip() or folder
                walk(ch, nm)
            elif t == "Placemark":
                props = {}
                nm = (ch.findtext("{*}name") or "").strip()
                if nm:
                    props["name"] = nm
                for d in ch.iter():
                    lt = _local(d.tag)
                    if lt == "Data" and d.get("name"):
                        props[d.get("name")] = (d.findtext("{*}value") or "").strip()
                    elif lt == "SimpleData" and d.get("name"):
                        props[d.get("name")] = (d.text or "").strip()
                geoms = [g for g in ch.iter() if _local(g.tag) in ("Point", "LineString")]
                if not geoms:
                    raws.append({"src": f"placemark '{nm or '?'}'", "props": props, "folder": folder,
                                 "geom": "unsupported", "coords": None, "gt": "Polygon/lainnya"})
                for j, g in enumerate(geoms):
                    pts = kml_coords(g)
                    gt = _local(g.tag)
                    raws.append({"src": f"placemark '{nm or '?'}'", "props": props, "folder": folder,
                                 "geom": "point" if gt == "Point" else "line",
                                 "coords": pts[0] if gt == "Point" and pts else pts,
                                 "part": (j + 1) if len(geoms) > 1 else None})
    walk(root, None)
    return raws


def _raw_from_csv(text):
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        raise HTTPException(status_code=400, detail="CSV kosong atau tanpa baris data")
    head = lines[0]
    delim = max([",", ";", "\t", "|"], key=lambda d: head.count(d))
    if head.count(delim) == 0:
        raise HTTPException(status_code=400, detail="Pemisah kolom CSV tidak terdeteksi (gunakan , atau ;)")
    rdr = csv.reader(io.StringIO("\n".join(lines)), delimiter=delim)
    header = [_canon_key(h) for h in next(rdr)]
    if "name" not in header:
        raise HTTPException(status_code=400, detail="CSV butuh kolom 'name' (atau 'nama')")
    return _raw_from_rows(header, list(rdr), 2, "baris", delim != ",")


def _raw_from_rows(header, rows, first_no, label, dc=False):
    """Baris tabel (CSV / sheet Excel) -> raw. header sudah dinormalkan (_canon_key)."""
    raws = []
    for i, row in enumerate(rows, start=first_no):
        props = {header[j]: str(v).strip() for j, v in enumerate(row) if j < len(header) and str(v).strip() != ""}
        if not props:
            continue
        co = props.get("coordinates")
        if co:
            raws.append({"src": f"{label} {i}", "props": props, "folder": None, "geom": "line_text",
                         "coords": co, "decimal_comma": dc})
        else:
            lat, lng = _to_float(props.get("lat"), dc), _to_float(props.get("lng"), dc)
            raws.append({"src": f"{label} {i}", "props": props, "folder": None, "geom": "point",
                         "coords": [lng, lat] if lat is not None and lng is not None else None})
    return raws


_XLSX_SKIP_SHEETS = re.compile(r"^\s*(insiden|incident|incidents|sambungan core|connections?|rekap.*|ringkasan|info|petunjuk)\s*$", re.I)


def _raw_from_xlsx(data: bytes):
    """Setiap sheet yang punya kolom 'name' dan (lat+lng atau coordinates) dibaca; sheet rekap/insiden dilewati."""
    raws, used = [], []
    for sname, rows in _xlsx_read(data):
        if _XLSX_SKIP_SHEETS.search(sname) or not rows:
            continue
        hi = next((i for i, r in enumerate(rows) if any(str(c).strip() for c in r)), None)
        if hi is None:
            continue
        header = [_canon_key(h) for h in rows[hi]]
        if "name" not in header or not ({"lat", "lng"} <= set(header) or "coordinates" in header):
            continue
        used.append(sname)
        raws += _raw_from_rows(header, rows[hi + 1:], hi + 2, f"sheet '{sname}' baris")
    if not used:
        raise HTTPException(status_code=400, detail="Tidak ada sheet yang cocok: butuh kolom 'name' dan "
                                                    "'latitude' + 'longitude' (node) atau 'coordinates' (kabel)")
    return raws


def _zip_has(data: bytes, member: str) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return member in z.namelist()
    except zipfile.BadZipFile:
        return False


def _extract_import(filename: str, content: str, content_base64: str):
    name = (filename or "").lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    data = None
    if content_base64:
        try:
            data = base64.b64decode(content_base64, validate=False)
        except Exception:
            raise HTTPException(status_code=400, detail="Isi berkas (base64) tidak valid")
        if len(data) > IMPORT_MAX_BYTES:
            raise HTTPException(status_code=413, detail=f"Berkas terlalu besar (maks {IMPORT_MAX_BYTES // 1_000_000} MB)")
    elif content:
        if len(content.encode("utf-8")) > IMPORT_MAX_BYTES:
            raise HTTPException(status_code=413, detail=f"Berkas terlalu besar (maks {IMPORT_MAX_BYTES // 1_000_000} MB)")
    else:
        raise HTTPException(status_code=400, detail="Berkas kosong")
    if ext in ("xls", "ods"):
        raise HTTPException(status_code=400, detail="Format .xls/.ods lama tidak didukung. Simpan sebagai .xlsx atau CSV")
    is_zip = bool(data and data[:2] == b"PK")
    if ext in ("xlsx", "xlsm") or (is_zip and ext not in ("kmz",) and _zip_has(data, "xl/workbook.xml")):
        if data is None:
            raise HTTPException(status_code=400, detail="Excel harus dikirim sebagai data biner (base64)")
        return "xlsx", _raw_from_xlsx(data)
    if ext == "kmz" or is_zip:
        if data is None:
            raise HTTPException(status_code=400, detail="KMZ harus dikirim sebagai data biner (base64)")
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                kmls = [i for i in z.infolist() if i.filename.lower().endswith(".kml") and not i.is_dir()]
                if not kmls:
                    raise HTTPException(status_code=400, detail="KMZ tidak berisi berkas .kml")
                info = sorted(kmls, key=lambda i: (i.filename.lower() != "doc.kml", i.filename))[0]
                if info.file_size > IMPORT_MAX_BYTES * 3:
                    raise HTTPException(status_code=413, detail="Isi KMZ terlalu besar setelah diekstrak")
                text = _decode_text(z.read(info))
        except zipfile.BadZipFile:
            raise HTTPException(status_code=400, detail="Berkas KMZ rusak")
        return "kml", _raw_from_kml(text)
    text = _decode_text(data) if data is not None else content
    if text.startswith("﻿"):
        text = text[1:]
    stripped = text.lstrip()
    if ext == "kml" or stripped.startswith("<"):
        return "kml", _raw_from_kml(text)
    if ext in ("geojson", "json") or stripped.startswith("{"):
        return "geojson", _raw_from_geojson(text)
    if ext in ("csv", "txt", "tsv") or "," in stripped.split("\n", 1)[0] or ";" in stripped.split("\n", 1)[0]:
        return "csv", _raw_from_csv(text)
    raise HTTPException(status_code=400, detail="Format tidak dikenali. Gunakan GeoJSON, KML/KMZ, CSV, atau Excel (.xlsx)")


def _build_records(raws):
    """raw -> catatan terstandar (node/kabel) lengkap dengan peringatan & galat per baris."""
    recs = []
    for rw in raws:
        p = _canon_props(rw.get("props"))
        rec = {"src": rw["src"], "kind": None, "name": None, "type": None, "status": "Active",
               "warnings": [], "errors": []}
        if rw["geom"] == "unsupported":
            rec["errors"].append(f"Jenis geometri {rw.get('gt') or 'lain'} tidak didukung (hanya titik & garis)")
            rec["kind"] = "?"
            rec["name"] = str(p.get("name") or rw["src"])
            recs.append(rec)
            continue
        is_line = rw["geom"] in ("line", "line_text")
        rec["kind"] = "CABLE" if is_line else "NODE"
        raw_name = str(p.get("name") or "").strip()
        if rw.get("part"):
            raw_name = f"{raw_name} ({rw['part']})" if raw_name else ""
        rec["name"] = raw_name
        if not raw_name:
            rec["errors"].append("Nama kosong")
        rec["name"] = raw_name[:120]
        # status
        if p.get("status"):
            st = STATUS_ALIASES.get(str(p["status"]).strip().upper())
            if st is None:
                rec["warnings"].append(f"Status '{p['status']}' tidak dikenal; dipakai Active")
            elif st == "Cut/Broken":
                rec["warnings"].append("Status putus tidak diimpor (gangguan harus lewat tiket); dipakai Active")
            else:
                rec["status"] = st
        rec["cluster"] = str(p.get("cluster") or DEFAULT_CLUSTER)[:60]
        rec["area"] = str(p.get("area") or DEFAULT_AREA)[:60]
        rec["city"] = str(p.get("city") or DEFAULT_CITY)[:60]
        if not is_line:
            co = rw.get("coords")
            lat = lng = None
            if co and len(co) >= 2:
                lng, lat = _to_float(co[0], True), _to_float(co[1], True)
            if not _valid_ll(lat, lng):
                rec["errors"].append("Koordinat tidak valid / di luar rentang (lat/lng tertukar?)")
            rec["lat"], rec["lng"] = lat, lng
            typ = _norm_node_type(p.get("type")) or _norm_node_type(rw.get("folder"))
            if typ is None and p.get("type"):
                rec["warnings"].append(f"Jenis '{p['type']}' tidak dikenal")
            if typ is None:
                typ = _infer_from_name(rec["name"], NODE_TYPE_ALIASES)
                if typ:
                    rec["warnings"].append(f"Jenis ditebak dari nama: {typ}")
            if typ is None:
                rec["errors"].append("Jenis aset tidak diketahui (isi kolom type: POP/CLOSURE/ODP/TIANG/HH/SLACK/PELANGGAN)")
            else:
                rec["type"] = typ
                rec["capacity"], bad = _norm_capacity_node(typ, p.get("capacity"))
                if bad:
                    rec["warnings"].append(f"Kapasitas '{p.get('capacity')}' tidak sesuai; dipakai {rec['capacity']}")
        else:
            if rw["geom"] == "line_text":
                coords = _parse_wkt_or_list(rw["coords"], rw.get("decimal_comma"))
            else:
                coords = _clean_coords(rw.get("coords"))
            if not coords or len(coords) < 2:
                rec["errors"].append("Geometri kabel butuh minimal 2 titik koordinat yang valid")
            else:
                rec["coords"] = coords
                rec["length_m"] = round(_polyline_length_m(coords), 1)
            typ = _norm_cable_type(p.get("type")) or _norm_cable_type(str(rw.get("folder") or "").replace("Kabel", "").strip())
            if typ is None and p.get("type"):
                rec["warnings"].append(f"Jenis kabel '{p['type']}' tidak dikenal")
            if typ is None:
                typ = _infer_from_name(rec["name"], CABLE_TYPE_ALIASES)
                if typ:
                    rec["warnings"].append(f"Jenis ditebak dari nama: {typ}")
            if typ is None:
                typ = "Distribution"
                rec["warnings"].append("Jenis kabel tidak diketahui; dipakai Distribution")
            rec["type"] = typ
            rec["capacity"], bad = _norm_capacity_cable(typ, p.get("capacity"))
            if bad:
                rec["warnings"].append(f"Kapasitas '{p.get('capacity')}' tidak sesuai; dipakai {rec['capacity']}")
            inst = None
            if p.get("installation"):
                inst = INSTALL_ALIASES.get(str(p["installation"]).strip().upper())
                if inst is None:
                    rec["warnings"].append(f"Pemasangan '{p['installation']}' tidak dikenal; dikosongkan")
            rec["installation"] = inst
        recs.append(rec)
    return recs


class ImportRequest(BaseModel):
    filename: str = ""
    content: Optional[str] = ""
    content_base64: Optional[str] = ""
    on_duplicate: Optional[str] = "skip"    # skip | update | create


def _plan_import(cursor, req: ImportRequest):
    mode = (req.on_duplicate or "skip").lower()
    if mode not in ("skip", "update", "create"):
        raise HTTPException(status_code=400, detail="on_duplicate harus skip, update, atau create")
    fmt, raws = _extract_import(req.filename, req.content or "", req.content_base64 or "")
    if not raws:
        raise HTTPException(status_code=400, detail="Tidak ada data yang bisa dibaca dari berkas")
    if len(raws) > IMPORT_MAX_RECORDS:
        raise HTTPException(status_code=413, detail=f"Terlalu banyak data ({len(raws)}); maksimal {IMPORT_MAX_RECORDS} per impor")
    recs = _build_records(raws)

    ex_nodes = [dict(r) for r in cursor.execute(
        "SELECT * FROM nodes WHERE type != 'INCIDENT'").fetchall()]
    node_by_key = {((r["name"] or "").strip().lower(), r["type"]): r for r in ex_nodes}
    ex_cables = [dict(r) for r in cursor.execute("SELECT * FROM cables").fetchall()]
    cable_by_key = {((r["name"] or "").strip().lower(), r["type"]): r for r in ex_cables}
    seen = set()
    for rec in recs:
        rec["action"] = "error" if rec["errors"] else "create"
        if rec["errors"]:
            continue
        key = (rec["name"].strip().lower(), rec["type"])
        if (rec["kind"], key) in seen:
            rec["action"] = "skip"
            rec["warnings"].append("Duplikat di dalam berkas ini; baris ini dilewati")
            continue
        seen.add((rec["kind"], key))
        existing = node_by_key.get(key) if rec["kind"] == "NODE" else cable_by_key.get(key)
        if existing is None and rec["kind"] == "NODE":
            for e in ex_nodes:       # titik yang sama persis (<1.5 m) dan jenis sama
                if e["type"] == rec["type"] and _haversine_m(rec["lat"], rec["lng"], e["latitude"], e["longitude"]) < 1.5:
                    existing = e
                    break
        if existing is not None:
            rec["existing_id"] = existing["id"]
            rec["existing_name"] = existing["name"]
            if mode == "skip":
                rec["action"] = "skip"
                rec["warnings"].append(f"Sudah ada ({existing['name']}); dilewati")
            elif mode == "update":
                rec["action"] = "update"
            else:
                rec["warnings"].append(f"Nama sama dengan aset yang sudah ada ({existing['name']}); tetap dibuat baru")
    return fmt, recs, mode


def _import_summary(fmt, recs, mode, filename):
    counts = {"create": 0, "update": 0, "skip": 0, "error": 0}
    by_type = {}
    for r in recs:
        counts[r["action"]] += 1
        k = r["type"] or "?"
        by_type[k] = by_type.get(k, 0) + (1 if r["action"] in ("create", "update") else 0)
    rows = []
    for r in recs[:IMPORT_PREVIEW_ROWS]:
        rows.append({"src": r["src"], "kind": r["kind"], "name": r["name"], "type": r["type"], "status": r["status"],
                     "action": r["action"], "warnings": r["warnings"], "errors": r["errors"],
                     "length_m": r.get("length_m"), "capacity": r.get("capacity"),
                     "installation": r.get("installation"), "latitude": r.get("lat"), "longitude": r.get("lng")})
    return {"filename": filename, "format": fmt, "on_duplicate": mode, "total": len(recs),
            "nodes": sum(1 for r in recs if r["kind"] == "NODE"), "cables": sum(1 for r in recs if r["kind"] == "CABLE"),
            "counts": counts, "by_type": {k: v for k, v in by_type.items() if v},
            "warnings": sum(len(r["warnings"]) for r in recs), "rows": rows,
            "truncated": len(recs) > IMPORT_PREVIEW_ROWS}


@app.post("/api/import/preview")
def import_preview(req: ImportRequest):
    """Baca berkas, validasi, dan laporkan apa yang AKAN terjadi. Tidak mengubah data."""
    with db() as conn:
        fmt, recs, mode = _plan_import(conn.cursor(), req)
    return _import_summary(fmt, recs, mode, req.filename)


@app.post("/api/import/commit")
def import_commit(req: ImportRequest):
    """Terapkan impor: baris valid dibuat/diperbarui dalam SATU transaksi; baris bergalat dilewati."""
    with db() as conn:
        cursor = conn.cursor()
        fmt, recs, mode = _plan_import(cursor, req)
        made = {"nodes": 0, "cables": 0, "updated": 0, "linked_ends": 0, "unlinked_cables": 0}
        # 1) node dulu supaya ujung kabel bisa terhubung ke node yang baru dibuat
        for rec in [r for r in recs if r["kind"] == "NODE"]:
            if rec["action"] == "create":
                cursor.execute(
                    """INSERT INTO nodes (name, type, status, latitude, longitude, cluster, area, city, capacity, spec_data)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')""",
                    (rec["name"], rec["type"], rec["status"], rec["lat"], rec["lng"], rec["cluster"], rec["area"],
                     rec["city"], rec["capacity"]))
                nid = cursor.lastrowid
                rec["new_id"] = nid
                made["nodes"] += 1
                _audit(cursor, "CREATE", "NODE", nid, rec["name"], f"Tambah {rec['type']} {rec['name']} (impor {req.filename})",
                       snapshot=_row_dict(cursor.execute("SELECT * FROM nodes WHERE id = ?", (nid,)).fetchone()))
            elif rec["action"] == "update":
                _import_update(cursor, "NODE", rec, made, req.filename)
        nodes_for_link = [dict(r) for r in cursor.execute(
            "SELECT id, type, latitude, longitude FROM nodes WHERE type NOT IN ('INCIDENT', 'TIANG', 'HH')").fetchall()]

        def nearest_node(lng, lat):
            best = None
            for n in nodes_for_link:
                d = _haversine_m(lat, lng, n["latitude"], n["longitude"])
                if d <= NODE_SNAP_M and (best is None or d < best[0]):
                    best = (d, n["id"])
            return best[1] if best else None

        for rec in [r for r in recs if r["kind"] == "CABLE"]:
            if rec["action"] == "create":
                a = nearest_node(*rec["coords"][0])
                b = nearest_node(*rec["coords"][-1])
                if a is not None and a == b:
                    b = None
                cursor.execute(
                    """INSERT INTO cables (name, type, status, geojson_geometry, cluster, area, city, capacity, core_data,
                                           installation, from_node_id, to_node_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, ?)""",
                    (rec["name"], rec["type"], rec["status"],
                     json.dumps({"type": "LineString", "coordinates": rec["coords"]}), rec["cluster"], rec["area"],
                     rec["city"], rec["capacity"], rec.get("installation"), a, b))
                cid = cursor.lastrowid
                made["cables"] += 1
                made["linked_ends"] += (a is not None) + (b is not None)
                if a is None and b is None:
                    made["unlinked_cables"] += 1
                _audit(cursor, "CREATE", "CABLE", cid, rec["name"],
                       f"Tambah kabel {rec['type']} {rec['name']} ({rec['capacity']}) (impor {req.filename})",
                       snapshot=_row_dict(cursor.execute("SELECT * FROM cables WHERE id = ?", (cid,)).fetchone()))
            elif rec["action"] == "update":
                _import_update(cursor, "CABLE", rec, made, req.filename)
        counts = {"create": made["nodes"] + made["cables"], "update": made["updated"],
                  "skip": sum(1 for r in recs if r["action"] == "skip"),
                  "error": sum(1 for r in recs if r["action"] == "error")}
        _audit(cursor, "IMPORT", "DATA", None, req.filename,
               f"Impor {fmt.upper()} '{req.filename}': {made['nodes']} node & {made['cables']} kabel dibuat, "
               f"{made['updated']} diperbarui, {counts['skip']} dilewati, {counts['error']} bergalat")
    return {"message": f"Impor selesai: {made['nodes']} node, {made['cables']} kabel dibuat; {made['updated']} diperbarui; "
                       f"{counts['skip']} dilewati; {counts['error']} bergalat.",
            "format": fmt, "counts": counts, **made}


def _import_update(cursor, kind, rec, made, filename):
    """Perbarui atribut aset yang sudah ada (bukan posisi/geometri/jenis)."""
    table = "nodes" if kind == "NODE" else "cables"
    old = cursor.execute(f"SELECT * FROM {table} WHERE id = ?", (rec["existing_id"],)).fetchone()
    if not old:
        rec["action"] = "skip"
        return
    new = {"cluster": rec["cluster"], "area": rec["area"], "city": rec["city"], "capacity": rec["capacity"]}
    if kind == "CABLE" and rec.get("installation"):
        new["installation"] = rec["installation"]
    if old["status"] != "Cut/Broken" and rec["status"] in ("Active", "Maintenance"):
        new["status"] = rec["status"]       # status gangguan dikelola tiket, tidak ditimpa impor
    try:
        if new["capacity"] != old["capacity"]:
            _check_capacity_fits(cursor, "NODE" if kind == "NODE" else "CABLE", old["id"], old["type"], new["capacity"])
    except HTTPException:
        new["capacity"] = old["capacity"]
        rec["warnings"].append("Kapasitas tidak diubah: port/core masih dipakai sambungan")
    changes = _diff_rows(cursor, dict(old), {**dict(old), **new}, list(new.keys()))
    if not changes:
        rec["action"] = "skip"
        rec["warnings"].append("Tidak ada perubahan")
        return
    sets = ", ".join(f"{k} = ?" for k in changes)
    cursor.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*[new[k] for k in changes], old["id"]))
    made["updated"] += 1
    _audit(cursor, "UPDATE", kind, old["id"], old["name"],
           _summarize_changes("node" if kind == "NODE" else "kabel", old["name"], changes) + f" (impor {filename})", changes)


# =====================================================================================
# ROUND 11 - BOQ DARI KHS
# =====================================================================================
# Katalog harga KHS (material + jasa per regional) disimpan di tabel khs_items dan bisa diedit/diimpor ulang.
# BOQ rencana Pasang Baru dihitung dari ringkasan rencana + pemetaan komponen -> item KHS (bisa diganti per baris).
KHS_REGIONS = ["NSO", "CSO", "SSO", "WJO", "CJDO", "WKO", "EKO", "EJO", "BNO", "SMUO", "MPO"]
KHS_MAX_PRICE = 100_000_000_000
KHS_SEED_FILE = Path(__file__).with_name("khs_seed.json")
DEFAULT_BOQ_MAP = {
    # kabel menurut "<pemasangan>:<jumlah core>"; kabel tanah >= 12 core = ADSS pada duct/tanam
    "cable": {"Udara:1": "007", "Udara:2": "005", "Udara:4": "006", "Udara:12": "008", "Udara:24": "009",
              "Udara:48": "010", "Udara:96": "011",
              "Tanah:1": "007", "Tanah:2": "005", "Tanah:4": "006", "Tanah:12": "001", "Tanah:24": "002",
              "Tanah:48": "003", "Tanah:96": "004"},
    "pole": {"Tiang 7m": "052", "Tiang 9m": "053", "Tiang Beton 7m": "056", "Tiang Beton 9m": "057"},
    "pole_default": "052",
    "pole_reuse": "055",       # aksesoris kabel pada tiang eksisting yang dipakai ulang
    "hh": "108", "slack_udara": "109", "slack_tanah": "126",
    "duct": "068",             # pipa duct PVC 100 mm per meter (kabel tanah)
    "trench": "087",           # galian + pengurugan kedalaman 1 m per meter (kabel tanah)
    "customer": {"1": "048", "2": "049", "4": "050", "8": "051"},
    "splice": "015",           # jasa penyambungan fusion splice per sambungan (core)
    "odp_new": "037",          # ODP tiang 8 core (disarankan saat port ODP asal penuh)
    "splitter_odp": "034",     # passive splitter 1:8 untuk ODP
    "splitter": {"1:8": "034", "1:16": "036"},   # per rasio; 1:4 & 2:8 belum ada di KHS -> dipakai splitter_odp + catatan
    "closure": {"12": "017.1", "24": "017.2", "48": "017.3", "96": "017.4"},
    "otb": {"12": "029", "24": "030", "48": "031", "96": "032"},     # OTB di lokasi pelanggan sesuai kapasitas kabel
    "pigtail": "025",          # adapter + pigtail untuk terminasi OTB
}
# splice_per_customer: jumlah sambungan fusion per pelanggan; minimal 2 (di ODP + di roset)
DEFAULT_BOQ_SETTINGS = {"tax_pct": 11.0, "default_region": "EKO", "splice_per_customer": 2}
BOQ_PARTS = ("both", "material", "jasa")


def _num_or_none(v):
    if v is None or v == "":
        return None
    try:
        f = float(str(v).replace(",", "") if isinstance(v, str) and "," in v and "." not in v else v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _parse_khs(data: bytes) -> list:
    """.xlsx KHS -> [{key, code, jenis, description, unit, prices{REG:[material, jasa]}}].
    Mendukung format KHS asli (dua baris judul: baris wilayah NSO..MPO untuk MATERIAL lalu JASA) dan
    format ekspor aplikasi ('EKO Material', 'EKO Jasa')."""
    sheets = _xlsx_read(data)
    for pref in ("KHS FO REGIONAL", "KHS"):
        sheets.sort(key=lambda sh: 0 if pref in sh[0].upper() else 1)
        if pref in sheets[0][0].upper():
            break
    for sname, rows in sheets:
        hi = next((i for i, r in enumerate(rows[:15])
                   if any(str(c).strip().lower().startswith("deskripsi") for c in r)), None)
        if hi is None:
            continue
        head = [str(c).strip() for c in rows[hi]]
        col = lambda *names: next((j for j, h in enumerate(head) if h.lower() in names or any(h.lower().startswith(n) for n in names)), None)
        c_desc, c_unit, c_jenis = col("deskripsi"), col("satuan"), col("jenis")
        mat, jasa, start = {}, {}, hi + 1
        for j, h in enumerate(head):
            m = re.match(r"^([A-Za-z]+)\s+(material|jasa)$", h, re.I)
            if m and m.group(1).upper() in KHS_REGIONS:
                (mat if m.group(2).lower() == "material" else jasa)[m.group(1).upper()] = j
        if not mat and not jasa and hi + 1 < len(rows):
            seen = set()
            for j, c in enumerate(rows[hi + 1]):
                reg = str(c).strip().upper()
                if reg in KHS_REGIONS:
                    (jasa if reg in seen else mat)[reg] = j
                    seen.add(reg)
            start = hi + 2
        if not mat and not jasa:
            continue
        items, counts = [], {}
        for r in rows[start:]:
            desc = str(r[c_desc]).strip() if c_desc is not None and c_desc < len(r) else ""
            if not desc:
                continue
            m = re.match(r"^(\d{1,4})_\[([^\]]*)\]_(.*)$", desc, re.S)
            code, jenis, text = (m.group(1), m.group(2).strip(), m.group(3).strip()) if m else (None, "", desc)
            if code is None:
                code = str(r[0]).strip() if r and r[0] not in ("", None) else f"X{len(items) + 1}"
            if c_jenis is not None and c_jenis < len(r) and str(r[c_jenis]).strip().startswith("["):
                jenis = str(r[c_jenis]).strip().strip("[] ")
            unit = str(r[c_unit]).strip() if c_unit is not None and c_unit < len(r) else ""
            prices = {}
            for reg in set(mat) | set(jasa):
                pm = _num_or_none(r[mat[reg]]) if reg in mat and mat[reg] < len(r) else None
                pj = _num_or_none(r[jasa[reg]]) if reg in jasa and jasa[reg] < len(r) else None
                prices[reg] = [pm, pj]
            counts[code] = counts.get(code, 0) + 1
            items.append({"code": code, "jenis": jenis, "description": re.sub(r"\s+", " ", text), "unit": unit, "prices": prices})
        dup = {c for c, n in counts.items() if n > 1}
        seq = {}
        for it in items:
            if it["code"] in dup:
                seq[it["code"]] = seq.get(it["code"], 0) + 1
                it["key"] = f"{it['code']}.{seq[it['code']]}"
            else:
                it["key"] = it["code"]
        if items:
            return items
    raise HTTPException(status_code=400, detail="Format KHS tidak dikenali: butuh kolom 'DESKRIPSI ITEM PEKERJAAN' dan kode wilayah (NSO, EKO, ...)")


def _khs_store(cursor, items, mode="replace", who=None):
    now = _now_str()
    if mode == "replace":
        cursor.execute("DELETE FROM khs_items")
    existing = {r["key"] for r in cursor.execute("SELECT key FROM khs_items").fetchall()}
    added = updated = 0
    for i, it in enumerate(items):
        pr = json.dumps(it["prices"])
        if it["key"] in existing:
            cursor.execute("UPDATE khs_items SET prices = ?, description = ?, unit = ?, jenis = ?, sort = ?, edited = 0, "
                           "updated_at = ?, updated_by = ? WHERE key = ?",
                           (pr, it["description"], it["unit"], it["jenis"], i, now, who, it["key"]))
            updated += 1
        else:
            cursor.execute("INSERT INTO khs_items (key, code, jenis, description, unit, prices, sort, updated_at, updated_by) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (it["key"], it["code"], it["jenis"], it["description"], it["unit"], pr, i, now, who))
            added += 1
    return added, updated


def _khs_ready(cursor):
    """Isi katalog dari khs_seed.json (KHS FO Regional 2025) bila masih kosong."""
    if cursor.execute("SELECT COUNT(*) FROM khs_items").fetchone()[0]:
        return
    try:
        seed = json.loads(KHS_SEED_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    _khs_store(cursor, seed["items"], "replace", "seed")
    cursor.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES ('khs_meta', ?, ?, 'seed') "
                   "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                   (json.dumps({"source": seed.get("source", "KHS"), "imported_at": _now_str()}), _now_str()))


def _khs_row(r) -> dict:
    d = dict(r)
    try:
        d["prices"] = json.loads(d["prices"]) if d.get("prices") else {}
    except (TypeError, ValueError):
        d["prices"] = {}
    return d


def _boq_cfg(cursor):
    m = json.loads(json.dumps(DEFAULT_BOQ_MAP))
    saved = _get_setting(cursor, "boq_map") or {}
    for k, v in saved.items():
        if k in ("cable", "pole", "customer", "splitter", "closure", "otb") and isinstance(v, dict):
            m[k].update({str(a): str(b) for a, b in v.items() if b})
        elif k in m and isinstance(v, str) and v:
            m[k] = v
    st = dict(DEFAULT_BOQ_SETTINGS)
    st.update({k: v for k, v in (_get_setting(cursor, "boq_settings") or {}).items() if k in st})
    return m, st


@app.get("/api/khs")
def list_khs(q: str = "", jenis: str = "ALL"):
    with db() as conn:
        cursor = conn.cursor()
        _khs_ready(cursor)
        rows = [_khs_row(r) for r in cursor.execute("SELECT * FROM khs_items ORDER BY sort, key").fetchall()]
        meta = _get_setting(cursor, "khs_meta") or {}
        bmap, bset = _boq_cfg(cursor)
    regions = [g for g in KHS_REGIONS if any(g in r["prices"] for r in rows)] or KHS_REGIONS
    jenis_all = sorted({r["jenis"] for r in rows if r["jenis"]})
    qq = (q or "").strip().lower()
    out = [r for r in rows if (jenis in ("ALL", "") or r["jenis"] == jenis)
           and (not qq or qq in r["description"].lower() or qq in r["code"].lower() or qq in r["key"].lower())]
    for r in out:
        r.pop("sort", None)
    return {"items": out, "total": len(rows), "regions": regions, "jenis": jenis_all, "meta": meta,
            "map": bmap, "settings": bset}


class KhsItemPayload(BaseModel):
    prices: Optional[dict] = None
    description: Optional[str] = None
    unit: Optional[str] = None


@app.put("/api/khs/{key}")
def update_khs(key: str, payload: KhsItemPayload):
    with db() as conn:
        cursor = conn.cursor()
        r = cursor.execute("SELECT * FROM khs_items WHERE key = ?", (key,)).fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Item KHS tidak ditemukan")
        old = _khs_row(r)
        prices = {k: list(v) for k, v in old["prices"].items()}
        changed = []
        for reg, val in (payload.prices or {}).items():
            reg = str(reg).upper()
            if reg not in KHS_REGIONS or not isinstance(val, dict):
                raise HTTPException(status_code=400, detail=f"Wilayah tidak valid: {reg}")
            cur = prices.get(reg, [None, None])
            new = list(cur)
            for i, f in enumerate(("material", "jasa")):
                if f in val:
                    v = _num_or_none(val[f])
                    if val[f] not in (None, "") and v is None:
                        raise HTTPException(status_code=400, detail=f"Harga {f} {reg} harus berupa angka")
                    if v is not None and not (0 <= v <= KHS_MAX_PRICE):
                        raise HTTPException(status_code=400, detail=f"Harga {f} {reg} harus antara 0 dan {KHS_MAX_PRICE:,}")
                    new[i] = v
            if new != cur:
                prices[reg] = new
                changed.append(f"{reg}: {cur[0] or 0:g}/{cur[1] or 0:g} -> {new[0] or 0:g}/{new[1] or 0:g}")
        desc = (payload.description if payload.description is not None else old["description"]).strip()[:600] or old["description"]
        unit = (payload.unit if payload.unit is not None else old["unit"]).strip()[:20]
        if desc != old["description"] or unit != old["unit"]:
            changed.append("deskripsi/satuan")
        if not changed:
            return {"message": "Tidak ada perubahan", "key": key}
        cursor.execute("UPDATE khs_items SET prices = ?, description = ?, unit = ?, edited = 1, updated_at = ?, updated_by = ? "
                       "WHERE key = ?", (json.dumps(prices), desc, unit, _now_str(), _current_username(), key))
        _audit(cursor, "UPDATE", "KHS", None, key, f"Ubah harga KHS {key}: " + "; ".join(changed)[:400])
    return {"message": "Item KHS diperbarui", "key": key}


class KhsImportPayload(BaseModel):
    filename: str = ""
    content_base64: str = ""
    mode: Optional[str] = "merge"      # merge: perbarui yang ada + tambah baru | replace: ganti seluruh katalog


@app.post("/api/khs/import")
def import_khs(payload: KhsImportPayload):
    mode = (payload.mode or "merge").lower()
    if mode not in ("merge", "replace"):
        raise HTTPException(status_code=400, detail="mode harus merge atau replace")
    if not (payload.filename or "").lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail="Gunakan berkas Excel .xlsx")
    try:
        raw = base64.b64decode(payload.content_base64 or "", validate=False)
    except Exception:
        raise HTTPException(status_code=400, detail="Isi berkas tidak valid")
    if not raw or len(raw) > IMPORT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Berkas kosong atau terlalu besar")
    items = _parse_khs(raw)
    with db() as conn:
        cursor = conn.cursor()
        added, updated = _khs_store(cursor, items, mode, _current_username())
        cursor.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES ('khs_meta', ?, ?, ?) "
                       "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at, "
                       "updated_by = excluded.updated_by",
                       (json.dumps({"source": payload.filename[:120], "imported_at": _now_str()}), _now_str(), _current_username()))
        keys = {r["key"] for r in cursor.execute("SELECT key FROM khs_items").fetchall()}
        bmap, _ = _boq_cfg(cursor)
        used = set(bmap["cable"].values()) | set(bmap["pole"].values()) | set(bmap["customer"].values()) | {
            bmap[k] for k in ("pole_default", "pole_reuse", "hh", "slack_udara", "slack_tanah", "duct", "trench", "splice", "odp_new", "splitter_odp")}
        missing = sorted(used - keys)
        _audit(cursor, "IMPORT", "KHS", None, payload.filename, f"Impor KHS ({mode}): {added} baru, {updated} diperbarui")
    return {"message": f"KHS diimpor: {added} item baru, {updated} diperbarui", "added": added, "updated": updated,
            "missing_map": missing}


@app.get("/api/khs/export")
def export_khs():
    with db() as conn:
        cursor = conn.cursor()
        _khs_ready(cursor)
        rows = [_khs_row(r) for r in cursor.execute("SELECT * FROM khs_items ORDER BY sort, key").fetchall()]
    regs = [g for g in KHS_REGIONS if any(g in r["prices"] for r in rows)] or KHS_REGIONS
    head = ["NO", "DESKRIPSI ITEM PEKERJAAN", "SATUAN", "JENIS"] + [f"{g} {k}" for k in ("Material", "Jasa") for g in regs]
    body = []
    for r in rows:
        desc = f"{r['code']}_[{r['jenis']}]_{r['description']}"
        body.append([r["key"], desc, r["unit"], f"[{r['jenis']}]"]
                    + [(r["prices"].get(g) or [None, None])[i] for i in (0, 1) for g in regs])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    return Response(content=_xlsx_bytes([("KHS", head, body)]), media_type=XLSX_MEDIA,
                    headers={"Content-Disposition": f'attachment; filename="khs_netgis_{stamp}.xlsx"'})


class BoqMapPayload(BaseModel):
    map: Optional[dict] = None
    settings: Optional[dict] = None


@app.put("/api/boq/map")
def put_boq_map(payload: BoqMapPayload):
    with db() as conn:
        cursor = conn.cursor()
        _khs_ready(cursor)
        keys = {r["key"] for r in cursor.execute("SELECT key FROM khs_items").fetchall()}
        cur, st = _boq_cfg(cursor)
        if payload.map is not None:
            new = json.loads(json.dumps(cur))
            for k, v in payload.map.items():
                if k not in DEFAULT_BOQ_MAP:
                    continue
                if isinstance(DEFAULT_BOQ_MAP[k], dict):
                    if not isinstance(v, dict):
                        raise HTTPException(status_code=400, detail=f"Pemetaan '{k}' harus berupa objek")
                    new[k].update({str(a): str(b) for a, b in v.items() if b})
                else:
                    new[k] = str(v)
            bad = sorted({x for k, v in new.items() for x in (v.values() if isinstance(v, dict) else [v]) if x not in keys})
            if bad:
                raise HTTPException(status_code=400, detail=f"Item KHS tidak ditemukan: {', '.join(bad)}")
            cursor.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES ('boq_map', ?, ?, ?) "
                           "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at, "
                           "updated_by = excluded.updated_by", (json.dumps(new), _now_str(), _current_username()))
            _audit(cursor, "UPDATE", "SETTING", None, "boq_map", "Mengubah pemetaan komponen BOQ -> item KHS")
        if payload.settings is not None:
            ns = dict(st)
            if "tax_pct" in payload.settings:
                t = _num_or_none(payload.settings["tax_pct"])
                if t is None or not (0 <= t <= 100):
                    raise HTTPException(status_code=400, detail="PPN harus antara 0 dan 100")
                ns["tax_pct"] = t
            if "splice_per_customer" in payload.settings:
                sp = _num_or_none(payload.settings["splice_per_customer"])
                if sp is None or sp != int(sp) or not (2 <= sp <= 48):
                    raise HTTPException(status_code=400, detail="Jumlah splicing per pelanggan harus bilangan bulat 2-48 (minimal ODP + roset)")
                ns["splice_per_customer"] = int(sp)
            if "default_region" in payload.settings:
                g = str(payload.settings["default_region"]).upper()
                if g not in KHS_REGIONS:
                    raise HTTPException(status_code=400, detail="Wilayah bawaan tidak valid")
                ns["default_region"] = g
            cursor.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES ('boq_settings', ?, ?, ?) "
                           "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at, "
                           "updated_by = excluded.updated_by", (json.dumps(ns), _now_str(), _current_username()))
            _audit(cursor, "UPDATE", "SETTING", None, "boq_settings", f"Mengubah pengaturan BOQ: PPN {ns['tax_pct']:g}%, wilayah {ns['default_region']}, splicing/pelanggan {ns['splice_per_customer']}")
        m2, s2 = _boq_cfg(cursor)
    return {"message": "Pengaturan BOQ disimpan", "map": m2, "settings": s2}


class BoqRequest(BaseModel):
    summary: dict
    rules: Optional[dict] = None
    region: Optional[str] = None
    origin_cluster: Optional[str] = None
    adjust: Optional[dict] = None
    plan_name: Optional[str] = None


def _cap_cores(cap) -> str:
    m = re.search(r"\d+", str(cap or ""))
    return m.group(0) if m else ""


def _boq_compute(cursor, req: BoqRequest) -> dict:
    _khs_ready(cursor)
    s = req.summary or {}
    inst = s.get("installation") or "Udara"
    if inst not in INSTALLATIONS:
        raise HTTPException(status_code=400, detail="Ringkasan rencana tidak valid (pemasangan)")
    rules = _plan_rules(cursor, req.rules)
    bmap, bset = _boq_cfg(cursor)
    adj = req.adjust if isinstance(req.adjust, dict) else {}
    region = (req.region or adj.get("region") or "").upper() or None
    if not region:
        oc = (req.origin_cluster or "").strip().upper()
        region = oc if oc in KHS_REGIONS else bset["default_region"]
    if region not in KHS_REGIONS:
        raise HTTPException(status_code=400, detail=f"Wilayah harus salah satu dari: {', '.join(KHS_REGIONS)}")
    items = {r["key"]: _khs_row(r) for r in cursor.execute("SELECT * FROM khs_items").fetchall()}
    warnings = []

    def f(key, default=0.0):
        v = _num_or_none(s.get(key))
        return default if v is None else v
    use_poles, use_slack = s.get("use_poles", True) is not False, s.get("use_slack", True) is not False
    segs = s.get("segments") if isinstance(s.get("segments"), list) and s.get("segments") else None
    legacy = segs is None
    if legacy:
        segs = [{"tag": "", "label": "", "cable_type": s.get("cable_type"), "cable_label": s.get("cable_label"), "cable_capacity": s.get("cable_capacity"),
                 "installation": inst, "route_length_m": f("route_length_m"), "cable_total_m": f("cable_total_m"),
                 "slack_count": f("slack_count"), "poles_new": f("poles_new"), "poles_existing": f("poles_existing"), "hh_new": f("hh_new")}]
    term = (s.get("termination") or "DROPCORE_ROSET").upper()
    has_cores = "customer_cores" in s
    ncores = int(f("customer_cores", 0) or 0) or (int(_cap_cores(rules["customer_capacity"]) or 1) if not has_cores else 1)
    hub = s.get("hub") if isinstance(s.get("hub"), dict) else None
    notes = []
    auto = []
    for sg in segs:
        sfx = "" if (legacy or len(segs) == 1) else f"_{sg['tag']}"
        tag = f" [{sg['label']}]" if sg.get("label") and len(segs) > 1 else ""
        sinst = sg.get("installation") or inst
        cores_ = _cap_cores(sg.get("cable_capacity"))
        cable_key = bmap["cable"].get(f"{sinst}:{cores_}")
        if not cable_key:
            warnings.append(f"Tidak ada item KHS untuk kabel {sg.get('cable_capacity')} ({sinst}); pilih item secara manual")
        auto.append({"id": "CABLE" + sfx, "component": f"{sg.get('cable_label') or 'Kabel'} {sg.get('cable_capacity') or ''} ({sinst}){tag}".replace("  ", " "),
                     "key": cable_key, "qty": round(float(sg.get("cable_total_m") or 0), 1)})
        if sinst == "Udara":
            if use_poles:
                auto.append({"id": "TIANG" + sfx, "component": f"Tiang baru ({rules['pole_type']}){tag}",
                             "key": bmap["pole"].get(rules["pole_type"], bmap["pole_default"]), "qty": float(sg.get("poles_new") or 0)})
                if float(sg.get("poles_existing") or 0) > 0:
                    auto.append({"id": "TIANG_REUSE" + sfx, "component": f"Aksesoris tiang eksisting (dipakai ulang){tag}",
                                 "key": bmap["pole_reuse"], "qty": float(sg["poles_existing"])})
            if use_slack:
                auto.append({"id": "SLACK" + sfx, "component": f"Slack pada tiang{tag}", "key": bmap["slack_udara"], "qty": float(sg.get("slack_count") or 0)})
        else:
            auto.append({"id": "DUCT" + sfx, "component": f"Pipa duct PVC 100 mm{tag}", "key": bmap["duct"], "qty": round(float(sg.get("route_length_m") or 0), 1)})
            auto.append({"id": "GALIAN" + sfx, "component": f"Galian & pengurugan{tag}", "key": bmap["trench"], "qty": round(float(sg.get("route_length_m") or 0), 1)})
            if use_poles:
                auto.append({"id": "HH" + sfx, "component": f"Handhole baru ({rules['hh_type']}){tag}", "key": bmap["hh"], "qty": float(sg.get("hh_new") or 0)})
            if use_slack:
                auto.append({"id": "SLACK" + sfx, "component": f"Slack dalam handhole{tag}", "key": bmap["slack_tanah"], "qty": float(sg.get("slack_count") or 0)})
    splices = 0
    if hub:
        csz = str(int(hub.get("closure_size") or 12))
        auto.append({"id": "CLOSURE", "component": f"Closure {csz} core di hub", "key": bmap["closure"].get(csz), "qty": 1.0})
        splices += 2 * ncores
        notes.append(f"Skenario >1 km: jaringan distribusi baru (kabel, tiang, slack) ke hub, closure {csz} core di hub; hub-pelanggan {float(hub.get('to_customer_m') or 0):.0f} m")
        if hub.get("odp"):
            r_ = hub["odp"]["ratio"]
            sk = bmap["splitter"].get(r_)
            auto.append({"id": "ODP_BARU", "component": "ODP baru di hub (1 core dari closure)", "key": bmap["odp_new"], "qty": 1.0})
            auto.append({"id": "SPLITTER", "component": f"Splitter {r_} untuk ODP baru", "key": sk or bmap["splitter_odp"], "qty": 1.0})
            if not sk:
                notes.append(f"KHS belum punya item splitter {r_}; dipakai splitter 1:8 sebagai pendekatan harga, ganti manual bila perlu")
            notes.append("Dari ODP baru ke pelanggan memakai dropcore + roset (ODP hanya 1 core)")
        elif ncores >= 2:
            notes.append(f"Layanan {ncores} core dedicated (Tx-Rx) diambil langsung dari closure, tanpa ODP")
    elif s.get("suggest_new_odp"):
        auto.append({"id": "ODP_BARU", "component": "ODP baru (port ODP asal penuh)", "key": bmap["odp_new"], "qty": 1.0})
        auto.append({"id": "SPLITTER", "component": "Splitter 1:8 untuk ODP baru", "key": bmap["splitter_odp"], "qty": 1.0})
    if f("customer") > 0:
        if term == "UDARA_OTB":
            osz = str(_otb_size(max(ncores, int(_cap_cores(segs[-1].get("cable_capacity")) or ncores))))
            auto.append({"id": "OTB", "component": f"OTB {osz} core di lokasi pelanggan (sesuai kapasitas kabel)", "key": bmap["otb"].get(osz), "qty": 1.0})
            auto.append({"id": "PIGTAIL", "component": "Adapter + pigtail terminasi OTB", "key": bmap["pigtail"], "qty": float(ncores)})
            notes.append(f"Terminasi kabel udara + OTB {osz} core di pelanggan; OTB dipilih sesuai kapasitas kabel ({segs[-1].get('cable_capacity')})")
        else:
            port = next((p_ for p_ in (1, 2, 4, 8) if p_ >= ncores), 8)
            auto.append({"id": "PELANGGAN", "component": f"Roset di pelanggan ({port} port, {ncores} core)",
                         "key": bmap["customer"].get(str(port)), "qty": 1.0})
            notes.append(f"Dropcore + roset {port} port di lokasi pelanggan")
        auto.append({"id": "SPLICE", "component": f"Penyambungan fusion splice ({ncores} core x {int(bset['splice_per_customer'])} + closure)",
                     "key": bmap["splice"], "qty": float(max(2, int(bset["splice_per_customer"])) * (ncores if has_cores else 1) + splices)})
    ov = adj.get("lines") if isinstance(adj.get("lines"), dict) else {}
    lines = []

    def price_line(lid, comp, key, qty, auto_qty, manual, parts="both"):
        it = items.get(key) if key else None
        pm, pj = ((it["prices"].get(region) or [None, None]) if it else [None, None])
        parts = parts if parts in BOQ_PARTS else "both"
        # harga yang dipakai: material saja / jasa saja / keduanya (item KHS bisa dipakai sebagian)
        pm_f = (pm or 0.0) if parts in ("both", "material") else 0.0
        pj_f = (pj or 0.0) if parts in ("both", "jasa") else 0.0
        note = None
        if not it:
            note = "Item KHS belum dipilih" if not key else f"Item KHS {key} tidak ada"
        elif pm is None and pj is None:
            note = f"Harga {region} kosong pada KHS"
        elif pm_f + pj_f == 0:
            note = f"Harga {region} bernilai 0 pada KHS" if parts == "both" else f"Harga {parts} {region} bernilai 0 pada KHS"
        if note:
            warnings.append(f"{comp}: {note}")
        qty = round(max(0.0, qty), 2)
        return {"id": lid, "component": comp, "key": key, "code": it["code"] if it else None,
                "description": it["description"] if it else "(belum dipilih)", "unit": it["unit"] if it else "",
                "qty": qty, "auto_qty": auto_qty, "manual": manual, "parts": parts,
                "price_material": pm_f, "price_jasa": pj_f, "unit_price": pm_f + pj_f,
                "total_material": round(qty * pm_f), "total_jasa": round(qty * pj_f),
                "total": round(qty * (pm_f + pj_f)), "note": note}
    for a in auto:
        o = ov.get(a["id"]) if isinstance(ov.get(a["id"]), dict) else {}
        if o.get("removed"):
            continue
        qty = _num_or_none(o.get("qty"))
        key = o.get("key") or a["key"]
        parts = o.get("parts") if o.get("parts") in BOQ_PARTS else "both"
        lines.append(price_line(a["id"], a["component"], key, a["qty"] if qty is None else qty, a["qty"],
                                qty is not None or bool(o.get("key")) or parts != "both", parts))
    for i, ex in enumerate((adj.get("extra") or [])[:60]):
        if not isinstance(ex, dict):
            continue
        q = _num_or_none(ex.get("qty"))
        if not ex.get("key") or q is None or q <= 0:
            continue
        if ex["key"] not in items:
            raise HTTPException(status_code=400, detail=f"Item KHS tambahan tidak ditemukan: {ex['key']}")
        lines.append(price_line(f"X{i + 1}", "Item tambahan", ex["key"], q, None, True,
                                ex.get("parts") if ex.get("parts") in BOQ_PARTS else "both"))
    tax_pct = _num_or_none(adj.get("tax_pct"))
    tax_pct = bset["tax_pct"] if tax_pct is None else tax_pct
    if not (0 <= tax_pct <= 100):
        raise HTTPException(status_code=400, detail="PPN harus antara 0 dan 100")
    mat = sum(l["total_material"] for l in lines)
    jasa = sum(l["total_jasa"] for l in lines)
    sub = mat + jasa
    tax = round(sub * tax_pct / 100)
    return {"region": region, "lines": lines, "adjust": {"region": region, "tax_pct": tax_pct,
                                                          "lines": ov, "extra": adj.get("extra") or []},
            "totals": {"material": mat, "jasa": jasa, "subtotal": sub, "tax_pct": tax_pct, "tax": tax, "total": sub + tax},
            "warnings": warnings, "installation": inst, "notes": notes}


@app.post("/api/boq/calc")
def boq_calc(req: BoqRequest):
    with db() as conn:
        return _boq_compute(conn.cursor(), req)


@app.post("/api/boq/export")
def boq_export(req: BoqRequest):
    with db() as conn:
        cursor = conn.cursor()
        res = _boq_compute(cursor, req)
        s, t = req.summary, res["totals"]
        name = (req.plan_name or "").strip() or "Rencana Pasang Baru"
        head = ["No", "Komponen", "Kode KHS", "Uraian Pekerjaan (KHS)", "Satuan", "Volume",
                "Harga Material", "Harga Jasa", "Harga Satuan", "Total Material", "Total Jasa", "Jumlah", "Catatan"]
        n = len(res["lines"])
        rows = []
        for i, l in enumerate(res["lines"], start=1):
            r = i + 1      # nomor baris Excel (baris 1 = judul kolom); volume & harga bisa diubah langsung di Excel
            rows.append([i, l["component"], l["code"], l["description"], l["unit"], l["qty"], l["price_material"], l["price_jasa"],
                         XlFormula(f"G{r}+H{r}", l["unit_price"]), XlFormula(f"F{r}*G{r}", l["total_material"]),
                         XlFormula(f"F{r}*H{r}", l["total_jasa"]), XlFormula(f"J{r}+K{r}", l["total"]),
                         l["note"] or "; ".join(x for x in (
                             "Volume diubah manual" if l["manual"] and l["auto_qty"] is not None and l["qty"] != l["auto_qty"] else "",
                             {"material": "Hanya material", "jasa": "Hanya jasa"}.get(l["parts"], "")) if x)])
        a, z = 2, n + 1
        sr, tr, gr = n + 2, n + 3, n + 4
        if n:
            rows.append([None, "Subtotal", None, None, None, None, None, None, None, XlFormula(f"SUM(J{a}:J{z})", t["material"]),
                         XlFormula(f"SUM(K{a}:K{z})", t["jasa"]), XlFormula(f"SUM(L{a}:L{z})", t["subtotal"]), None])
        else:
            rows.append([None, "Subtotal", None, None, None, None, None, None, None, 0, 0, 0, None])
        rows += [[None, "PPN (%)", None, None, "%", t["tax_pct"], None, None, None, None, None, XlFormula(f"ROUND(L{sr}*F{tr}/100,0)", t["tax"]), None],
                 [None, "TOTAL", None, None, None, None, None, None, None, None, None, XlFormula(f"L{sr}+L{tr}", t["total"]), None]]
        info = [["Rencana", name], ["Wilayah KHS", res["region"]], ["Pemasangan", res["installation"]],
                ["Panjang rute (m)", s.get("route_length_m")], ["Total kabel + slack (m)", s.get("cable_total_m")],
                ["Jenis kabel", f"{s.get('cable_label') or ''} {s.get('cable_capacity') or ''}".strip()],
                ["Sumber harga", (_get_setting(cursor, "khs_meta") or {}).get("source", "KHS")],
                ["Dibuat", _now_str()]]
        if res["warnings"]:
            info.append(["Peringatan", "; ".join(res["warnings"])[:1500]])
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        safe = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")[:40] or "rencana"
        fname = f"boq_{safe}_{res['region']}_{stamp}.xlsx"
        _audit(cursor, "EXPORT", "BOQ", None, fname, f"Ekspor BOQ '{name}' ({res['region']}): total Rp {t['total']:,}")
    body = _xlsx_bytes([("BOQ", head, rows), ("Info", ["Keterangan", "Nilai"], info)])
    return Response(content=body, media_type=XLSX_MEDIA,
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


class PlanBoqPayload(BaseModel):
    adjust: dict


@app.put("/api/plans/{plan_id}/boq")
def save_plan_boq(plan_id: int, payload: PlanBoqPayload):
    with db() as conn:
        cursor = conn.cursor()
        r = _load_plan(cursor, plan_id)
        adj = payload.adjust if isinstance(payload.adjust, dict) else {}
        res = _boq_compute(cursor, BoqRequest(summary=json.loads(r["summary"] or "{}"), adjust=adj))
        cursor.execute("UPDATE plans SET boq_adjust = ? WHERE id = ?", (json.dumps(res["adjust"]), plan_id))
        _audit(cursor, "UPDATE", "PLAN", plan_id, r["name"],
               f"Simpan BOQ rencana '{r['name']}' ({res['region']}): Rp {res['totals']['total']:,}")
    return {"message": "BOQ rencana disimpan", **res}



# =====================================================================================
# ROUND 12 - A. REDAMAN (KALKULASI) & ALOKASI CORE OTOMATIS
# =====================================================================================
DEFAULT_LOSS = {
    "fiber_db_km": 0.35,       # redaman serat per km (G.652D @1310 nm)
    "splice_db": 0.10,         # per sambungan fusion
    "connector_db": 0.30,      # per pasang konektor
    "tx_dbm": 3.0,             # daya kirim OLT
    "rx_min_dbm": -27.0,       # sensitivitas minimum ONT
    "margin_db": 3.0,          # cadangan keselamatan
    "splitter_db": {"1:2": 3.7, "1:4": 7.2, "1:8": 10.5, "1:16": 13.7, "1:32": 17.1, "1:64": 21.0, "2:4": 7.5, "2:8": 11.0},
    "rx_max_dbm": -8.0,        # batas atas daya terima ONT (overload)
    "mm_fiber_db_km_850": 3.0,  # multimode OM3/OM4 @850 nm (dB/km)
    "mm_fiber_db_km_1300": 1.0, # multimode @1300 nm
    "mm_tx_dbm": -3.0,         # daya kirim transceiver multimode (SX)
    "mm_rx_min_dbm": -17.0,    # sensitivitas penerima multimode
    "otdr_warn_db": 1.0,       # selisih ukur - hitung yang dianggap perlu dicek
    "otdr_bad_db": 3.0,        # selisih yang dianggap bermasalah
    "event_warn_db": 0.5,      # redaman satu event (splice/konektor) yang mencurigakan
    "event_bad_db": 1.0,
}


def _loss_params(cursor, override=None) -> dict:
    p = json.loads(json.dumps(DEFAULT_LOSS))
    saved = _get_setting(cursor, "loss_params") or {}
    for src in (saved, override or {}):
        for k, v in src.items():
            if k == "splitter_db" and isinstance(v, dict):
                p["splitter_db"].update({str(a): float(b) for a, b in v.items() if _num_or_none(b) is not None})
            elif k in p and k != "splitter_db" and _num_or_none(v) is not None:
                p[k] = float(v)
    return p


def _validate_loss_params(src: dict) -> dict:
    out = {}
    lim = {"fiber_db_km": (0, 5), "splice_db": (0, 2), "connector_db": (0, 3), "tx_dbm": (-10, 15), "rx_min_dbm": (-50, 0),
           "margin_db": (0, 15), "mm_fiber_db_km_850": (0, 8), "mm_fiber_db_km_1300": (0, 4), "rx_max_dbm": (-30, 10), "mm_tx_dbm": (-20, 10), "mm_rx_min_dbm": (-40, 0), "otdr_warn_db": (0, 20), "otdr_bad_db": (0, 40), "event_warn_db": (0, 10), "event_bad_db": (0, 20)}
    for k, (lo, hi) in lim.items():
        if k in src and src[k] not in (None, ""):
            v = _num_or_none(src[k])
            if v is None or not (lo <= v <= hi):
                raise HTTPException(status_code=400, detail=f"Parameter '{k}' harus berupa angka {lo} s.d. {hi}")
            out[k] = v
    if isinstance(src.get("splitter_db"), dict):
        sp = {}
        for a, b in src["splitter_db"].items():
            if not re.match(r"^[12]:\d{1,3}$", str(a)):
                raise HTTPException(status_code=400, detail=f"Rasio splitter '{a}' harus berformat 1:8 atau 2:8")
            v = _num_or_none(b)
            if v is None or not (0 <= v <= 40):
                raise HTTPException(status_code=400, detail=f"Redaman splitter {a} harus 0-40 dB")
            sp[str(a)] = v
        out["splitter_db"] = sp
    return out


@app.get("/api/loss/params")
def get_loss_params():
    with db() as conn:
        cursor = conn.cursor()
        return {"params": _loss_params(cursor), "defaults": DEFAULT_LOSS, "customized": _get_setting(cursor, "loss_params") is not None}


class LossParamsPayload(BaseModel):
    params: dict


@app.put("/api/loss/params")
def put_loss_params(payload: LossParamsPayload):
    clean = _validate_loss_params(payload.params or {})
    with db() as conn:
        cursor = conn.cursor()
        cur = _get_setting(cursor, "loss_params") or {}
        merged = {**cur, **{k: v for k, v in clean.items() if k != "splitter_db"}}
        if "splitter_db" in clean:
            merged["splitter_db"] = {**(cur.get("splitter_db") or {}), **clean["splitter_db"]}
        cursor.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES ('loss_params', ?, ?, ?) "
                       "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at, "
                       "updated_by = excluded.updated_by", (json.dumps(merged), _now_str(), _current_username()))
        _audit(cursor, "UPDATE", "SETTING", None, "loss_params", "Mengubah parameter redaman (kalkulasi loss budget)")
        return {"message": "Parameter redaman disimpan", "params": _loss_params(cursor)}


def _splitter_ratio(capacity) -> Optional[str]:
    m = re.search(r"(\d+)\s*In\s*-\s*(\d+)\s*Out", str(capacity or ""), re.I)
    return f"{int(m.group(1))}:{int(m.group(2))}" if m else None


def _cable_len_km(row) -> float:
    c = _cable_coords(row)
    return _polyline_length_m(c) / 1000.0 if c else 0.0


def _fiber_coef(p: dict, mode=None, wl=None) -> float:
    """Redaman serat per km menurut jenis serat (SM/MM) dan panjang gelombang (MM: 850 vs 1300 nm)."""
    if (mode or "SM").upper() == "MM":
        return p["mm_fiber_db_km_1300"] if (wl or 850) >= 1200 else p["mm_fiber_db_km_850"]
    return p["fiber_db_km"]


def _loss_status(total_db: float, p: dict, mode: str = "SM") -> dict:
    mm = (mode or "SM").upper() == "MM"
    rx = (p["mm_tx_dbm"] if mm else p["tx_dbm"]) - total_db
    margin = rx - (p["mm_rx_min_dbm"] if mm else p["rx_min_dbm"])
    st = "OK" if margin >= p["margin_db"] else "WARN" if margin >= 0 else "BAD"
    return {"rx_dbm": round(rx, 2), "margin_db": round(margin, 2), "status": st}


def _meas_status(rx, p, mode="SM"):
    """Status sinyal berdasarkan Rx TERUKUR (tiap perangkat memberi daya berbeda, jadi tanpa estimasi):
    BAD bila di bawah sensitivitas, WARN bila margin kurang, HIGH bila melebihi batas atas (overload)."""
    if rx is None:
        return None
    mm = (mode or "SM").upper() == "MM"
    lo = p["mm_rx_min_dbm"] if mm else p["rx_min_dbm"]
    if rx > p.get("rx_max_dbm", -8.0):
        return "HIGH"
    margin = rx - lo
    return "BAD" if margin < 0 else "WARN" if margin < p["margin_db"] else "OK"


def _loss_total(bd: dict, p: dict) -> float:
    fiber = bd["fiber_db"] if bd.get("fiber_db") is not None else bd["fiber_km"] * p["fiber_db_km"]
    return (fiber + bd["splices"] * p["splice_db"] + bd["connectors"] * p["connector_db"]
            + sum(s["db"] for s in bd["splitters"]))


def _calc_loss(cursor, start_key, p=None, splice_per_customer=2) -> dict:
    """Redaman estimasi dari POP/OLT sampai aset start_key (jalur hulu menurut sambungan core).
    Dihitung dari: panjang kabel dilalui, jumlah splice, konektor (sisi OLT & roset), dan splitter ODP yang dilewati."""
    p = p or _loss_params(cursor)
    names, conns = _trace_graph(cursor)
    cab_rows = cursor.execute("SELECT * FROM cables").fetchall()
    cab_len = {r["id"]: _cable_len_km(r) for r in cab_rows}
    cab_mode = {r["id"]: ((r["fiber_mode"] if "fiber_mode" in r.keys() else None) or "SM").upper() for r in cab_rows}
    nodes = {r["id"]: r for r in cursor.execute("SELECT id, name, type, capacity FROM nodes").fetchall()}
    bd = {"fiber_km": 0.0, "fiber_db": 0.0, "splices": 0, "connectors": 0, "splitters": [], "hops": 0}
    modes_seen = set()
    path, notes, seen_c, seen_n = [], [], set(), set()
    key, seen = start_key, {start_key}
    prev_hop = None
    while True:
        up = [c for c in conns if (c["to_asset_type"], c["to_asset_id"]) == key and (c["from_asset_type"], c["from_asset_id"]) not in seen]
        if prev_hop is not None and _is_junction(names, key) and prev_hop.get("from_port_core"):
            up = [c for c in up if c.get("to_port_core") == prev_hop["from_port_core"]]   # joint yang sama
        if not up:
            break
        c = up[0]
        prev_hop = c
        frm = (c["from_asset_type"], c["from_asset_id"])
        bd["hops"] += 1
        for cid in {c.get("via_cable_id"), c["from_asset_id"] if c["from_asset_type"] == "CABLE" else None,
                    c["to_asset_id"] if c["to_asset_type"] == "CABLE" else None} - {None}:
            if cid not in seen_c:
                seen_c.add(cid)
                km = cab_len.get(cid, 0.0)
                bd["fiber_km"] += km
                bd["fiber_db"] += km * _fiber_coef(p, cab_mode.get(cid))
                modes_seen.add(cab_mode.get(cid, "SM"))
                if km == 0:
                    notes.append(f"Panjang kabel #{cid} tidak diketahui")
        f_node = nodes.get(c["from_asset_id"]) if c["from_asset_type"] == "NODE" else None
        f_type = (f_node["type"] or "").upper() if f_node else None
        t_node = nodes.get(c["to_asset_id"]) if c["to_asset_type"] == "NODE" else None
        to_cust = bool(t_node and (t_node["type"] or "").upper() == "PELANGGAN")
        if f_type in ("POP", "OLT"):
            bd["connectors"] += 1
        if to_cust:
            bd["splices"] += max(1, int(splice_per_customer or 2))      # minimal 2: sisi ODP + sisi roset
            bd["connectors"] += 1
        elif f_type not in ("POP", "OLT"):
            bd["splices"] += 1
        if f_type == "ODP" and str(c.get("from_port_core") or "").startswith("OUT") and c["from_asset_id"] not in seen_n:
            seen_n.add(c["from_asset_id"])
            ratio = _splitter_ratio(f_node["capacity"]) or "1:8"
            bd["splitters"].append({"node_id": c["from_asset_id"], "name": f_node["name"], "ratio": ratio,
                                    "db": p["splitter_db"].get(ratio, 0.0)})
        path.append({"connection_id": c["id"], "from": names.get(frm, (f"{frm[0]} #{frm[1]}", None))[0],
                     "to": names.get(key, (f"{key[0]} #{key[1]}", None))[0],
                     "via": c.get("via_cable_name")})
        seen.add(frm)
        key = frm
        if bd["hops"] > 60:
            notes.append("Jalur terlalu panjang; dipotong di 60 lompatan")
            break
    total = _loss_total(bd, p)
    mode = "MM" if modes_seen == {"MM"} else "SM"
    if len(modes_seen) > 1:
        notes.append("Jalur campur single-mode & multimode; status memakai batas single-mode")
    return {"breakdown": {**bd, "fiber_km": round(bd["fiber_km"], 3), "fiber_db": round(bd["fiber_db"], 3)}, "total_db": round(total, 2),
            "mode": mode, **_loss_status(total, p, mode), "path": list(reversed(path)), "notes": sorted(set(notes)),
            "root": names.get(key, ("?", None))[0] if bd["hops"] else None}


def _plan_loss(cursor, origin: dict, total_cable_m: float, customer: bool, splice_count: int, p=None,
               extra_splitters=None, extra_splices=0) -> dict:
    """Redaman rencana pasang baru = redaman dari POP ke aset asal + kabel baru + splice/konektor baru."""
    p = p or _loss_params(cursor)
    bd = {"fiber_km": 0.0, "fiber_db": 0.0, "splices": 0, "connectors": 0, "splitters": [], "hops": 0}
    notes = []
    if origin.get("type") == "NODE" and origin.get("id") is not None:
        up = _calc_loss(cursor, ("NODE", origin["id"]), p, splice_count)
        bd = {**up["breakdown"]}
        bd["splitters"] = list(bd["splitters"])
        notes += up["notes"]
        if not up["breakdown"]["hops"] and (origin.get("kind") or "").upper() not in ("POP", "OLT"):
            notes.append("Jalur aset asal ke POP belum tersambung di data core; redaman hulu tidak terhitung")
        row = cursor.execute("SELECT name, type, capacity FROM nodes WHERE id = ?", (origin["id"],)).fetchone()
        if row and (row["type"] or "").upper() == "ODP":
            ratio = _splitter_ratio(row["capacity"]) or "1:8"
            bd["splitters"].append({"node_id": origin["id"], "name": row["name"], "ratio": ratio, "db": p["splitter_db"].get(ratio, 0.0)})
        elif row and (row["type"] or "").upper() in ("POP", "OLT"):
            bd["connectors"] += 1
    bd["fiber_km"] += total_cable_m / 1000.0
    bd["fiber_db"] = (bd.get("fiber_db") or 0.0) + total_cable_m / 1000.0 * p["fiber_db_km"]
    bd["splices"] += max(1, int(splice_count)) if customer else 1
    bd["splices"] += int(extra_splices or 0)
    for rt_ in (extra_splitters or []):
        bd["splitters"].append({"node_id": None, "name": "ODP baru", "ratio": rt_, "db": p["splitter_db"].get(rt_, 0.0)})
    if customer:
        bd["connectors"] += 1
    bd["hops"] += 1
    total = _loss_total(bd, p)
    return {"breakdown": {**bd, "fiber_km": round(bd["fiber_km"], 3), "fiber_db": round(bd["fiber_db"], 3)}, "total_db": round(total, 2),
            **_loss_status(total, p), "notes": sorted(set(notes)), "params": {k: p[k] for k in ("tx_dbm", "rx_min_dbm", "margin_db")}}


@app.get("/api/loss/path")
def loss_path(asset_type: str, asset_id: int):
    """Redaman estimasi dari POP sampai aset ini + pembanding hasil ukur OTDR terbaru pada kabel di jalur tsb."""
    asset_type = asset_type.upper()
    if asset_type not in ("NODE", "CABLE"):
        raise HTTPException(status_code=400, detail="Tipe aset harus NODE atau CABLE")
    with db() as conn:
        cursor = conn.cursor()
        if not _exists(cursor, _table_of(asset_type), asset_id):
            raise HTTPException(status_code=404, detail="Aset tidak ditemukan")
        p = _loss_params(cursor)
        _, bset = _boq_cfg(cursor)
        res = _calc_loss(cursor, (asset_type, asset_id), p, bset["splice_per_customer"])
        cab_ids = {h["via"] for h in res["path"] if h["via"]}
        res["otdr"] = _otdr_for_cable_names(cursor, cab_ids, p)
        res["params"] = {k: p[k] for k in ("fiber_db_km", "splice_db", "connector_db", "tx_dbm", "rx_min_dbm", "margin_db")}
    return res


def _otdr_for_cable_names(cursor, names, p):
    out = []
    for nm in sorted(names):
        r = cursor.execute("SELECT id FROM cables WHERE name = ?", (nm,)).fetchone()
        if r:
            for core, rec in _latest_otdr(cursor, r["id"], p).items():
                out.append({"cable": nm, "core": core, **rec})
    return out


# --- Alokasi core otomatis setelah rencana diwujudkan ---
def _cable_end_node(cursor, cable, at_start):
    """Node di ujung kabel (from/to_node_id, atau node terdekat <= NODE_SNAP_M dari ujung geometri)."""
    nid = cable["from_node_id"] if at_start else cable["to_node_id"]
    if nid:
        return cursor.execute("SELECT * FROM nodes WHERE id = ?", (nid,)).fetchone()
    coords = _cable_coords(cable)
    if not coords:
        return None
    lng, lat = coords[0] if at_start else coords[-1]
    best = None
    for n in cursor.execute("SELECT * FROM nodes").fetchall():
        d = _haversine_m(n["latitude"], n["longitude"], lat, lng)
        if d <= NODE_SNAP_M and (best is None or d < best[0]):
            best = (d, n)
    return best[1] if best else None


def _acquire_feed(cursor, node, n, exclude_cable_ids, depth=0, log=None):
    """
    n sumber sirkuit (("NODE", node_id, port)) yang bisa disambung ke hilir dari `node`.
      ODP     : 1 port OUT kosong
      POP     : port kosong
      CLOSURE : joint yang sudah tiba tapi belum diteruskan; bila kurang, ambil core kosong pada kabel yang berujung di
                closure lalu minta umpan ke node hulu kabel itu (rekursif) dan buat hop hulu -> joint.
    Mengembalikan (sources, alasan_gagal). log: daftar hop yang dibuat (untuk laporan).
    """
    t = (node["type"] or "").upper()
    if depth > 8:
        return None, "Rantai hulu terlalu dalam / berputar"
    if t == "ODP":
        if n > 1:
            return None, "ODP hanya 1 core; layanan dedicated 2 core tidak bisa diambil dari ODP"
        labels = [x for x in _port_labels("NODE", t, node["capacity"]) if x.startswith("OUT-")]
        used = _used_ports(cursor, "NODE", node["id"])
        free = [x for x in labels if x not in used]
        if not free:
            return None, f"Port {node['name']} sudah penuh; alokasi manual atau tambah ODP"
        return [("NODE", node["id"], free[0])], None
    if t == "POP":
        used = _used_ports(cursor, "NODE", node["id"])
        free = [x for x in _port_labels("NODE", t, node["capacity"]) if x not in used]
        if len(free) < n:
            return None, f"Port {node['name']} hanya {len(free)} kosong; butuh {n}"
        return [("NODE", node["id"], x) for x in free[:n]], None
    if t in JUNCTION_TYPES:
        ins, outs = _joint_dirs(cursor, node["id"])
        ready = sorted(p for p in ins if p not in outs)
        srcs = [("NODE", node["id"], p) for p in ready[:n]]
        need = n - len(srcs)
        if need <= 0:
            return srcs, None
        cables = cursor.execute("SELECT * FROM cables").fetchall()
        why = f"Tidak ada kabel hulu yang berujung di {node['name']}"
        for a_ in _cables_at_node(cursor, node, cables):
            c = a_["row"]
            if c["id"] in exclude_cable_ids:
                continue
            if not a_["at_end"]:
                why = f"Kabel {c['name']} hanya melintas di {node['name']}; pecah kabel di closure dulu"
                continue
            up = _cable_end_node(cursor, c, True)
            if not up or up["id"] == node["id"]:
                why = f"Kabel {c['name']} tidak punya node hulu"
                continue
            used = _used_ports(cursor, "CABLE", c["id"])
            free = [x for x in _core_labels(_core_total(c["capacity"])) if x not in used and x not in ins]
            if len(free) < need:
                why = f"Kabel {c['name']} hanya {len(free)} core kosong; butuh {need}"
                continue
            feeds, why2 = _acquire_feed(cursor, up, need, exclude_cable_ids | {c["id"]}, depth + 1, log)
            if not feeds:
                why = why2
                continue
            for i, f in enumerate(feeds):
                lk = _link(cursor, f, "NODE", node["id"], free[i], c["id"], free[i],
                           f"Alokasi otomatis: umpan hulu -> {node['name']}")
                if log is not None:
                    log.append(lk)
                srcs.append(("NODE", node["id"], free[i]))
            return srcs, None
        return None, why
    return None, f"Jenis aset asal {t} tidak mendukung alokasi otomatis"


def _pick_sources(cursor, origin, exclude_cable_ids, n, log=None):
    return _acquire_feed(cursor, origin, n, set(exclude_cable_ids), 0, log)


def _link(cursor, src, dst_type, dst_id, dst_port, cable_id, core, note):
    cursor.execute("""INSERT INTO core_connections (from_asset_type, from_asset_id, from_port_core, to_asset_type, to_asset_id,
                      to_port_core, via_cable_id, via_core, status, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Connected', ?)""",
                   (src[0], src[1], src[2], dst_type, dst_id, dst_port, cable_id, core, note))
    cid = cursor.lastrowid
    crow = cursor.execute("SELECT * FROM core_connections WHERE id = ?", (cid,)).fetchone()
    lbl = _conn_label(cursor, crow)
    _audit(cursor, "CONNECT", "CONNECTION", cid, lbl, f"Alokasi core otomatis: {lbl}", snapshot=_row_dict(crow))
    return {"id": cid, "label": lbl}


def _allocate_new_customer(cursor, origin_id, cable_id, cust_id, n=1) -> dict:
    """Sambungkan pelanggan baru (n core): n port/core kosong pada aset asal -> titik pelanggan, lewat n core pertama kabel baru."""
    if not cust_id:
        return {"allocated": False, "reason": "Rencana tanpa titik pelanggan; core dialokasikan manual"}
    origin = cursor.execute("SELECT * FROM nodes WHERE id = ?", (origin_id,)).fetchone()
    cab = cursor.execute("SELECT * FROM cables WHERE id = ?", (cable_id,)).fetchone()
    cust = cursor.execute("SELECT * FROM nodes WHERE id = ?", (cust_id,)).fetchone()
    if not (origin and cab and cust):
        return {"allocated": False, "reason": "Aset asal/kabel/pelanggan tidak ditemukan"}
    cab_cores = _core_labels(_core_total(cab["capacity"]))
    cust_ports = _port_labels("NODE", "PELANGGAN", cust["capacity"])
    if len(cust_ports) < n or len(cab_cores) < n:
        return {"allocated": False, "reason": "Titik pelanggan/kabel tidak punya cukup port/core"}
    feed_log = []
    srcs, why = _pick_sources(cursor, origin, {cable_id}, n, feed_log)
    if not srcs:
        return {"allocated": False, "reason": why}
    links = feed_log + [_link(cursor, srcs[i], "NODE", cust_id, cust_ports[i], cable_id, cab_cores[i], "Alokasi otomatis dari rencana pasang baru") for i in range(n)]
    return {"allocated": True, "connection_id": links[0]["id"], "from_port": srcs[0][2], "from_type": srcs[0][0], "from_id": srcs[0][1],
            "to_port": cust_ports[0], "via_core": cab_cores[0], "label": links[0]["label"], "cores": n, "links": links}


def _allocate_hub(cursor, origin_id, cable1, closure_id, odp_id, cable2, cust_id, n) -> dict:
    """Rantai alokasi skenario hub: asal -> closure (kabel1), lalu closure -> pelanggan (kabel2) atau closure -> ODP -> pelanggan."""
    origin = cursor.execute("SELECT * FROM nodes WHERE id = ?", (origin_id,)).fetchone()
    cl = cursor.execute("SELECT * FROM nodes WHERE id = ?", (closure_id,)).fetchone()
    cust = cursor.execute("SELECT * FROM nodes WHERE id = ?", (cust_id,)).fetchone() if cust_id else None
    c1 = cursor.execute("SELECT * FROM cables WHERE id = ?", (cable1,)).fetchone()
    c2 = cursor.execute("SELECT * FROM cables WHERE id = ?", (cable2,)).fetchone()
    if not (origin and cl and c1 and c2):
        return {"allocated": False, "reason": "Aset rantai tidak lengkap"}
    feed_log = []
    srcs, why = _pick_sources(cursor, origin, {cable1}, n, feed_log)
    if not srcs:
        return {"allocated": False, "reason": why}
    k1, k2 = _core_labels(_core_total(c1["capacity"])), _core_labels(_core_total(c2["capacity"]))
    cports = _port_labels("NODE", "CLOSURE", cl["capacity"])
    if len(k1) < n or len(k2) < n:
        return {"allocated": False, "reason": "Kapasitas closure/kabel baru tidak cukup"}
    links = list(feed_log)
    for i in range(n):
        links.append(_link(cursor, srcs[i], "NODE", closure_id, k1[i], cable1, k1[i], "Alokasi otomatis: asal -> closure hub"))
    if odp_id:
        odp = cursor.execute("SELECT * FROM nodes WHERE id = ?", (odp_id,)).fetchone()
        links.append(_link(cursor, ("NODE", closure_id, k1[0]), "NODE", odp_id, "IN-1", None, None, "Alokasi otomatis: closure -> ODP baru"))
        if cust:
            cp = _port_labels("NODE", "PELANGGAN", cust["capacity"])
            links.append(_link(cursor, ("NODE", odp_id, "OUT-1"), "NODE", cust_id, cp[0], cable2, k2[0], "Alokasi otomatis: ODP baru -> pelanggan"))
    elif cust:
        cp = _port_labels("NODE", "PELANGGAN", cust["capacity"])
        for i in range(n):
            links.append(_link(cursor, ("NODE", closure_id, k1[i]), "NODE", cust_id, cp[i], cable2, k2[i], "Alokasi otomatis: closure -> pelanggan"))
    return {"allocated": True, "connection_id": links[0]["id"], "from_port": srcs[0][2], "from_type": srcs[0][0], "from_id": srcs[0][1],
            "to_port": cports[0], "via_core": k1[0], "label": links[0]["label"], "cores": n, "links": links}


# =====================================================================================
# ROUND 12 - B. UNGGAH OTDR (hasil ukur) + LAYER PETA
# =====================================================================================
OTDR_STATUS_RANK = {"OK": 0, "WARN": 1, "BAD": 2}
_OTDR_ALIASES = {
    "cable": {"kabel", "cable", "namakabel", "cablename", "cableid", "idkabel", "kabelid", "kodekabel"},
    "core": {"core", "serat", "fiber", "fibre", "coreno", "nocore", "nomorcore"},
    "wavelength": {"panjanggelombangnm", "panjanggelombang", "wavelength", "wavelengthnm", "wl", "nm", "lambda"},
    "date": {"tanggalukur", "tanggal", "date", "measuredat", "tglukur", "tgl", "tanggalpengukuran"},
    "length": {"panjangm", "panjang", "length", "lengthm", "panjangkabelm", "fiberlength"},
    "loss": {"totalredamandb", "totalredaman", "totalloss", "totallossdb", "redamantotal", "redaman", "redamandb",
             "loss", "lossdb", "attenuation", "totalattenuation"},
    "avg": {"redamandbkm", "dbkm", "avgdbkm", "attenuationdbkm", "averageloss", "avgloss"},
    "orl": {"orldb", "orl"},
    "ev_dist": {"jarakeventm", "jarakevent", "eventdistance", "eventdistancem", "distance", "distancem", "jarak", "posisi", "posisim"},
    "ev_loss": {"redamaneventdb", "redamanevent", "eventloss", "eventlossdb", "lossevent", "spliceloss"},
    "ev_type": {"jenisevent", "eventtype", "tipeevent"},
    "ev_refl": {"reflektansidb", "reflektansi", "reflectance", "refl"},
    "direction": {"arah", "direction", "arahukur"},
    "from": {"dari", "from", "asetawal", "titikawal", "titikasal", "dariaset", "start", "asal"},
    "to": {"ke", "to", "asettujuan", "titikakhir", "tujuan", "end", "keaset"},
    "notes": {"catatan", "notes", "keterangan", "remark", "remarks"},
}
OTDR_TEMPLATE_HEADER = ["Kabel", "Dari", "Ke", "Core", "Panjang Gelombang (nm)", "Tanggal Ukur", "Panjang (m)", "Total Redaman (dB)",
                        "Redaman (dB/km)", "ORL (dB)", "Arah", "Jarak Event (m)", "Redaman Event (dB)", "Jenis Event",
                        "Reflektansi (dB)", "Catatan"]


def _norm_header(h) -> str:
    return re.sub(r"[^a-z0-9]", "", str(h or "").lower())


def _upload_rows(filename: str, b64: str, text: str = "") -> list:
    """CSV/XLSX -> [dict kolom-baku -> teks, '_row': nomor baris]. Kolom dikenali lewat nama (Indonesia/Inggris)."""
    name = (filename or "").lower()
    raw = base64.b64decode(b64 or "", validate=False) if b64 else (text or "").encode("utf-8")
    if not raw or len(raw) > IMPORT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Berkas kosong atau terlalu besar")
    if name.endswith((".xlsx", ".xlsm")) or raw[:2] == b"PK":
        sheets = _xlsx_read(raw)
        grid = next((rows for _n, rows in sheets if rows), [])
    elif name.endswith((".xls", ".ods", ".sor")):
        raise HTTPException(status_code=400, detail="Format ini belum didukung. Simpan sebagai .xlsx atau CSV (ekspor tabel event dari perangkat lunak OTDR)")
    else:
        try:
            txt = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            txt = raw.decode("latin-1")
        first = txt.splitlines()[0] if txt.splitlines() else ""
        delim = max((",", ";", "\t"), key=lambda d: first.count(d))
        grid = [row for row in csv.reader(io.StringIO(txt), delimiter=delim)]
    hi = next((i for i, r in enumerate(grid[:20]) if sum(1 for c in r if any(_norm_header(c) in a for a in _OTDR_ALIASES.values())) >= 2), None)
    if hi is None:
        raise HTTPException(status_code=400, detail="Judul kolom tidak dikenali. Pakai template OTDR (kolom Kabel, Core, Total Redaman (dB), ...)")
    cols = {}
    for j, h in enumerate(grid[hi]):
        k = _norm_header(h)
        for field, al in _OTDR_ALIASES.items():
            if k in al and field not in cols:
                cols[field] = j
    out = []
    for i, r in enumerate(grid[hi + 1:], start=hi + 2):
        if not any(str(c).strip() for c in r):
            continue
        d = {f: (str(r[j]).strip() if j < len(r) else "") for f, j in cols.items()}
        d["_row"] = i
        out.append(d)
    if len(out) > IMPORT_MAX_RECORDS:
        raise HTTPException(status_code=413, detail=f"Terlalu banyak baris (maks {IMPORT_MAX_RECORDS})")
    return out


def _fnum(v):
    if v is None or str(v).strip() == "":
        return None
    t = str(v).strip().replace(" ", "")
    t = re.sub(r"[^0-9,.\-+eE]", "", t)
    if "," in t and "." not in t:
        t = t.replace(",", ".")
    elif "," in t and "." in t:
        t = t.replace(",", "") if t.rfind(".") > t.rfind(",") else t.replace(".", "").replace(",", ".")
    try:
        f = float(t)
    except ValueError:
        return None
    return f if math.isfinite(f) else None


def _fdate(v) -> Optional[str]:
    t = str(v or "").strip()
    if not t:
        return None
    t = t.split(" ")[0].split("T")[0]
    for pat, order in ((r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", "ymd"), (r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$", "dmy")):
        m = re.match(pat, t)
        if m:
            a, b, c = (int(x) for x in m.groups())
            y, mo, d = (a, b, c) if order == "ymd" else (c, b, a)
            try:
                return datetime(y, mo, d).strftime("%Y-%m-%d")
            except ValueError:
                return None
    if re.match(r"^\d{5}$", t):      # nomor seri tanggal Excel
        try:
            return (datetime(1899, 12, 30) + timedelta(days=int(t))).strftime("%Y-%m-%d")
        except (ValueError, OverflowError):
            return None
    return None


def _core_label_from(v, total) -> Optional[str]:
    t = str(v or "").strip()
    labels = _core_labels(total)
    if t in labels:
        return t
    m = re.search(r"(\d+)\s*$", t)
    if m and 1 <= int(m.group(1)) <= total:
        return labels[int(m.group(1)) - 1]
    return None


def _otdr_eval(total_db, length_m, events, p, cable_len_m, coef=None) -> dict:
    """Bandingkan hasil ukur dengan estimasi (serat + splice). Status terburuk dari selisih total & event tunggal."""
    L = length_m if length_m else cable_len_m
    n_ev = sum(1 for e in events if (e.get("type") or "").lower() in ("", "splice", "sambungan", "fusion", "connector", "konektor"))
    expected = (L / 1000.0) * (p["fiber_db_km"] if coef is None else coef) + n_ev * p["splice_db"] if L else None
    st, why = "OK", []
    delta = None
    if total_db is not None and expected is not None:
        delta = round(total_db - expected, 2)
        if delta >= p["otdr_bad_db"]:
            st, why = "BAD", [f"Redaman ukur {delta:+.2f} dB di atas estimasi"]
        elif delta >= p["otdr_warn_db"]:
            st, why = "WARN", [f"Redaman ukur {delta:+.2f} dB di atas estimasi"]
    for e in events:
        el = e.get("loss_db")
        es = "OK"
        if el is not None:
            es = "BAD" if el >= p["event_bad_db"] else "WARN" if el >= p["event_warn_db"] else "OK"
        e["status"] = es
        if OTDR_STATUS_RANK[es] > OTDR_STATUS_RANK[st]:
            st = es
        if es != "OK":
            why.append(f"Event {e.get('distance_m') or '?'} m: {el:.2f} dB")
    return {"expected_db": None if expected is None else round(expected, 2), "delta_db": delta, "status": st, "reasons": why}


def _cable_ends(cable, nodes) -> dict:
    """Aset di ujung A (titik pertama) dan B (titik terakhir) kabel: dari relasi from/to node, atau node terdekat (<= 30 m)."""
    coords = _cable_coords(cable) or []
    out = {}
    for side, fid, pt in (("A", cable["from_node_id"], coords[0] if coords else None), ("B", cable["to_node_id"], coords[-1] if coords else None)):
        nd = nodes.get(fid) if fid else None
        if nd is None and pt is not None:
            best = None
            for n in nodes.values():
                if (n["type"] or "").upper() in ("INCIDENT",):
                    continue
                d = _haversine_m(pt[1], pt[0], n["latitude"], n["longitude"])
                if d <= 30 and (best is None or d < best[0]):
                    best = (d, n)
            nd = best[1] if best else None
        out[side] = {"id": nd["id"], "name": nd["name"], "type": nd["type"]} if nd else None
    return out


def _resolve_node_ref(ref, nodes, by_name):
    ref = str(ref or "").strip()
    if not ref:
        return None, None
    if re.fullmatch(r"\d+", ref) and int(ref) in nodes and ref.lower() not in by_name:
        return int(ref), None
    cand = by_name.get(ref.lower(), [])
    if len(cand) == 1:
        return cand[0]["id"], None
    return None, (f"Aset '{ref}' ganda ({len(cand)}); pakai ID aset" if cand else f"Aset '{ref}' tidak ditemukan")


def _otdr_direction(r_from, r_to, r_dir, ends, nodes, by_name):
    """Arah ukur dari kolom Dari/Ke (aset di ujung kabel) atau kolom Arah. Mengembalikan (arah, from_id, to_id, galat)."""
    def side_of(ref):
        t = str(ref or "").strip().upper()
        if t in ("A", "B"):
            return t, (ends[t]["id"] if ends.get(t) else None), None
        nid, err = _resolve_node_ref(ref, nodes, by_name)
        if err:
            return None, None, err
        for s in ("A", "B"):
            if ends.get(s) and ends[s]["id"] == nid:
                return s, nid, None
        nm = nodes[nid]["name"] if nid in nodes else ref
        ea = ends["A"]["name"] if ends.get("A") else "?"
        eb = ends["B"]["name"] if ends.get("B") else "?"
        return None, nid, f"'{nm}' bukan ujung kabel ini (ujung A: {ea}, ujung B: {eb})"
    fs = ts = None
    if r_from:
        fs, _fid, err = side_of(r_from)
        if err:
            return None, None, None, err
    if r_to:
        ts, _tid, err = side_of(r_to)
        if err:
            return None, None, None, err
    if fs and ts and fs == ts:
        return None, None, None, "Titik Dari dan Ke berada di ujung yang sama"
    if fs is None and ts is not None:
        fs = "B" if ts == "A" else "A"
    if fs:
        direction = "A-B" if fs == "A" else "B-A"
    else:
        d = re.sub(r"[^AB]", "", (r_dir or "").upper())
        direction = {"AB": "A-B", "BA": "B-A"}.get(d)
    if direction is None:
        return None, None, None, None
    a, b = direction.split("-")
    return direction, (ends[a]["id"] if ends.get(a) else None), (ends[b]["id"] if ends.get(b) else None), None


def _otdr_parse(cursor, filename, b64, text="", default_cable=None, default_from=None):
    return _otdr_parse_rows(cursor, _upload_rows(filename, b64, text), default_cable, default_from)


def _otdr_parse_rows(cursor, rows, default_cable=None, default_from=None):
    p = _loss_params(cursor)
    nodes = {r["id"]: r for r in cursor.execute("SELECT id, name, type, latitude, longitude FROM nodes").fetchall()}
    node_by_name = {}
    for n in nodes.values():
        node_by_name.setdefault((n["name"] or "").strip().lower(), []).append(n)
    cables = {r["id"]: r for r in cursor.execute("SELECT * FROM cables").fetchall()}
    ends_cache = {}
    by_name = {}
    for r in cables.values():
        by_name.setdefault((r["name"] or "").strip().lower(), []).append(r)
    groups, order, errors = {}, [], []
    last_key = None
    for r in rows:
        cname = r.get("cable", "")
        has_default = default_cable not in (None, "")
        if cname == "" and has_default and (r.get("core", "") != "" or last_key is None):
            cname = str(default_cable)
        if cname == "" and last_key is not None:
            key = last_key           # baris event lanjutan: kabel/core kosong = sama dengan baris sebelumnya
            g = groups[key]
        else:
            cab = None
            if re.fullmatch(r"\d+", cname) and int(cname) in cables and (cname.lower() not in by_name):
                cab = cables[int(cname)]
            else:
                cand = by_name.get(cname.strip().lower(), [])
                cab = cand[0] if len(cand) == 1 else None
                if len(cand) > 1:
                    errors.append({"row": r["_row"], "message": f"Nama kabel '{cname}' ganda ({len(cand)} kabel); pakai ID kabel"})
                    last_key = None
                    continue
            if cab is None:
                errors.append({"row": r["_row"], "message": f"Kabel '{cname}' tidak ditemukan" if cname else "Kolom Kabel kosong"})
                last_key = None
                continue
            total = _core_total(cab["capacity"])
            core = _core_label_from(r.get("core", ""), total)
            if core is None:
                errors.append({"row": r["_row"], "message": f"Core '{r.get('core', '')}' tidak ada pada kabel {cab['name']} ({total} core)"})
                last_key = None
                continue
            wl = int(_fnum(r.get("wavelength")) or 0) or None
            if wl is not None and not (800 <= wl <= 1700):
                errors.append({"row": r["_row"], "message": f"Panjang gelombang {wl} nm tidak wajar (800-1700)"})
                last_key = None
                continue
            date = _fdate(r.get("date")) or datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if cab["id"] not in ends_cache:
                ends_cache[cab["id"]] = _cable_ends(cab, nodes)
            direction, from_id, to_id, derr = _otdr_direction(r.get("from") or default_from, r.get("to"), r.get("direction"),
                                                               ends_cache[cab["id"]], nodes, node_by_name)
            if derr:
                errors.append({"row": r["_row"], "message": f"{cab['name']}: {derr}"})
                last_key = None
                continue
            key = (cab["id"], core, wl, date, direction)
            if key not in groups:
                groups[key] = {"cable_id": cab["id"], "cable_name": cab["name"], "core": core, "wavelength_nm": wl, "measured_at": date,
                               "direction": direction, "from_node_id": from_id, "to_node_id": to_id,
                               "from_name": nodes[from_id]["name"] if from_id in nodes else None,
                               "to_name": nodes[to_id]["name"] if to_id in nodes else None,
                               "fiber_mode": ((cab["fiber_mode"] if "fiber_mode" in cab.keys() else None) or "SM").upper(), "length_m": None, "total_loss_db": None, "avg_db_km": None, "orl_db": None,
                               "notes": "", "events": [], "rows": [], "issues": []}
                order.append(key)
            g = groups[key]
            last_key = key
        g["rows"].append(r["_row"])
        for fld, src in (("length_m", "length"), ("total_loss_db", "loss"), ("avg_db_km", "avg"), ("orl_db", "orl")):
            v = _fnum(r.get(src))
            if v is not None and g[fld] is None:
                if v < 0 and fld != "orl_db":
                    g["issues"].append(f"Baris {r['_row']}: nilai {src} negatif diabaikan")
                else:
                    g[fld] = v
        if r.get("notes") and not g["notes"]:
            g["notes"] = r["notes"][:300]
        d, el = _fnum(r.get("ev_dist")), _fnum(r.get("ev_loss"))
        if d is not None or el is not None:
            g["events"].append({"distance_m": d, "loss_db": el, "type": (r.get("ev_type") or "").strip().lower()[:20],
                                "reflectance_db": _fnum(r.get("ev_refl"))})
    out = []
    for key in order:
        g = groups[key]
        g["events"] = [e for e in g["events"] if e["distance_m"] is not None or e["loss_db"] is not None]
        g["events"].sort(key=lambda e: (e["distance_m"] is None, e["distance_m"] or 0))
        if g["total_loss_db"] is None and g["events"]:
            sm = sum((e["loss_db"] or 0) for e in g["events"])
            if g["avg_db_km"] is not None and g["length_m"]:
                sm += g["avg_db_km"] * g["length_m"] / 1000.0
            g["total_loss_db"] = round(sm, 2)
            g["issues"].append("Total redaman dihitung dari event + redaman serat")
        if g["total_loss_db"] is None:
            errors.append({"row": g["rows"][0], "message": f"{g['cable_name']} {g['core']}: tanpa Total Redaman maupun event"})
            continue
        if g["avg_db_km"] is None and g["length_m"]:
            g["avg_db_km"] = round(g["total_loss_db"] / (g["length_m"] / 1000.0), 3) if g["length_m"] > 0 else None
        cab_len = _polyline_length_m(_cable_coords(cables[g["cable_id"]]) or []) if _cable_coords(cables[g["cable_id"]]) else 0
        ev = _otdr_eval(g["total_loss_db"], g["length_m"], g["events"], p, cab_len, _fiber_coef(p, g["fiber_mode"], g["wavelength_nm"]))
        g.update(expected_db=ev["expected_db"], delta_db=ev["delta_db"], status=ev["status"], reasons=ev["reasons"])
        if g["length_m"] and cab_len and abs(g["length_m"] - cab_len) / cab_len > 0.25:
            g["issues"].append(f"Panjang ukur {g['length_m']:.0f} m berbeda >25% dari panjang kabel di peta ({cab_len:.0f} m)")
        dup = cursor.execute("SELECT id FROM otdr_results WHERE cable_id = ? AND core = ? AND IFNULL(wavelength_nm, 0) = ? "
                             "AND measured_at = ? AND IFNULL(direction, '') = ?",
                             (g["cable_id"], g["core"], g["wavelength_nm"] or 0, g["measured_at"], g["direction"] or "")).fetchone()
        g["duplicate_id"] = dup["id"] if dup else None
        out.append(g)
    return out, errors


class OtdrUpload(BaseModel):
    filename: str = ""
    content_base64: Optional[str] = ""
    content: Optional[str] = ""
    on_duplicate: Optional[str] = "skip"      # skip | replace
    default_cable_id: Optional[int] = None    # segmen bawaan untuk baris tanpa kolom Kabel
    default_from: Optional[str] = None        # 'A' | 'B' | id/nama aset ujung awal ukur


@app.post("/api/otdr/preview")
def otdr_preview(req: OtdrUpload):
    with db() as conn:
        groups, errors = _otdr_parse(conn.cursor(), req.filename, req.content_base64 or "", req.content or "", req.default_cable_id, req.default_from)
    cnt = {"OK": 0, "WARN": 0, "BAD": 0}
    for g in groups:
        cnt[g["status"]] += 1
    return {"count": len(groups), "errors": errors[:200], "error_count": len(errors), "status_counts": cnt,
            "duplicates": sum(1 for g in groups if g["duplicate_id"]),
            "items": [{k: g[k] for k in ("cable_id", "cable_name", "core", "wavelength_nm", "measured_at", "direction", "from_name", "to_name", "fiber_mode", "length_m",
                                         "total_loss_db", "expected_db", "delta_db", "status", "reasons", "issues", "duplicate_id")}
                      | {"events": len(g["events"])} for g in groups[:300]]}


def _otdr_store(cursor, groups, errors, mode, source):
    made = replaced = skipped = 0
    for g in groups:
        if g["duplicate_id"]:
            if mode == "skip":
                skipped += 1
                continue
            cursor.execute("DELETE FROM otdr_results WHERE id = ?", (g["duplicate_id"],))
            replaced += 1
        else:
            made += 1
        cursor.execute(
            """INSERT INTO otdr_results (cable_id, core, wavelength_nm, direction, from_node_id, to_node_id, measured_at, length_m,
                   total_loss_db, avg_db_km, orl_db, expected_db, delta_db, status, reasons, events, notes, source_file, uploaded_by, uploaded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (g["cable_id"], g["core"], g["wavelength_nm"], g["direction"], g.get("from_node_id"), g.get("to_node_id"), g["measured_at"],
             g["length_m"], g["total_loss_db"], g["avg_db_km"], g["orl_db"], g["expected_db"], g["delta_db"], g["status"],
             json.dumps(g["reasons"]), json.dumps(g["events"]), g["notes"], (source or "")[:120], _current_username(), _now_str()))
    _audit(cursor, "IMPORT", "OTDR", None, source or "otdr",
           f"Simpan OTDR: {made} baru, {replaced} diganti, {skipped} dilewati, {len(errors)} bermasalah")
    return made, replaced, skipped


def _otdr_result(made, replaced, skipped, errors):
    return {"message": f"OTDR disimpan: {made} baru, {replaced} diganti, {skipped} dilewati" + (f", {len(errors)} baris bermasalah" if errors else ""),
            "created": made, "replaced": replaced, "skipped": skipped, "errors": errors[:50], "error_count": len(errors)}


@app.post("/api/otdr/commit")
def otdr_commit(req: OtdrUpload):
    mode = (req.on_duplicate or "skip").lower()
    if mode not in ("skip", "replace"):
        raise HTTPException(status_code=400, detail="on_duplicate harus skip atau replace")
    with db() as conn:
        cursor = conn.cursor()
        groups, errors = _otdr_parse(cursor, req.filename, req.content_base64 or "", req.content or "", req.default_cable_id, req.default_from)
        if not groups:
            raise HTTPException(status_code=400, detail="Tidak ada hasil ukur yang valid untuk disimpan" + (f": {errors[0]['message']}" if errors else ""))
        made, replaced, skipped = _otdr_store(cursor, groups, errors, mode, req.filename or "otdr")
    return _otdr_result(made, replaced, skipped, errors)


class OtdrEvent(BaseModel):
    distance_m: Optional[float] = None
    loss_db: Optional[float] = None
    type: Optional[str] = ""


class OtdrManual(BaseModel):
    cable_id: int
    from_node: Optional[str] = None          # 'A' | 'B' | id/nama aset ujung awal ukur
    to_node: Optional[str] = None
    core: str
    wavelength_nm: Optional[int] = None
    measured_at: Optional[str] = None
    length_m: Optional[float] = None
    total_loss_db: Optional[float] = None
    events: Optional[List[OtdrEvent]] = None
    notes: Optional[str] = ""
    on_duplicate: Optional[str] = "skip"


@app.post("/api/otdr/manual")
def otdr_manual(req: OtdrManual):
    """Input satu hasil ukur lewat form (segmen kabel + dari/ke), tanpa berkas."""
    mode = (req.on_duplicate or "skip").lower()
    if mode not in ("skip", "replace"):
        raise HTTPException(status_code=400, detail="on_duplicate harus skip atau replace")
    rows = [{"cable": str(req.cable_id), "from": req.from_node or "", "to": req.to_node or "", "core": req.core,
             "wavelength": str(req.wavelength_nm or ""), "date": req.measured_at or "", "length": "" if req.length_m is None else str(req.length_m),
             "loss": "" if req.total_loss_db is None else str(req.total_loss_db), "notes": req.notes or "", "_row": 1}]
    for k, e in enumerate(req.events or []):
        rows.append({"cable": "", "core": "", "ev_dist": "" if e.distance_m is None else str(e.distance_m),
                     "ev_loss": "" if e.loss_db is None else str(e.loss_db), "ev_type": e.type or "", "_row": 2 + k})
    with db() as conn:
        cursor = conn.cursor()
        groups, errors = _otdr_parse_rows(cursor, rows)
        if not groups:
            raise HTTPException(status_code=400, detail=errors[0]["message"] if errors else "Data ukur tidak valid")
        made, replaced, skipped = _otdr_store(cursor, groups, errors, mode, "input manual")
        g = groups[0]
    return {**_otdr_result(made, replaced, skipped, errors), "item": {k: g[k] for k in ("cable_name", "core", "direction", "from_name", "to_name",
            "total_loss_db", "expected_db", "delta_db", "status", "reasons")}}


def _otdr_row(r) -> dict:
    d = dict(r)
    for k in ("reasons", "events"):
        try:
            d[k] = json.loads(d[k]) if d.get(k) else []
        except (TypeError, ValueError):
            d[k] = []
    return d


def _latest_otdr(cursor, cable_id, p=None) -> dict:
    out = {}
    for r in cursor.execute("SELECT * FROM otdr_results WHERE cable_id = ? ORDER BY measured_at DESC, id DESC", (cable_id,)).fetchall():
        if r["core"] not in out:
            out[r["core"]] = {"id": r["id"], "loss_db": r["total_loss_db"], "date": r["measured_at"], "wavelength_nm": r["wavelength_nm"],
                              "expected_db": r["expected_db"], "delta_db": r["delta_db"], "status": r["status"],
                              "length_m": r["length_m"], "avg_db_km": r["avg_db_km"], "direction": r["direction"]}
    return out


@app.get("/api/otdr")
def list_otdr(cable_id: Optional[int] = None, limit: int = 100):
    limit = max(1, min(int(limit), 500))
    with db() as conn:
        q = ("SELECT o.*, c.name AS cable_name, fn.name AS from_name, tn.name AS to_name FROM otdr_results o "
             "LEFT JOIN cables c ON c.id = o.cable_id LEFT JOIN nodes fn ON fn.id = o.from_node_id LEFT JOIN nodes tn ON tn.id = o.to_node_id")
        rows = conn.execute(q + (" WHERE o.cable_id = ?" if cable_id is not None else "") +
                            " ORDER BY o.measured_at DESC, o.id DESC LIMIT ?",
                            ((cable_id, limit) if cable_id is not None else (limit,))).fetchall()
    return [_stamp(_otdr_row(r), "uploaded_at") for r in rows]


@app.delete("/api/otdr/{otdr_id}")
def delete_otdr(otdr_id: int):
    with db() as conn:
        cursor = conn.cursor()
        r = cursor.execute("SELECT o.*, c.name AS cable_name FROM otdr_results o LEFT JOIN cables c ON c.id = o.cable_id WHERE o.id = ?",
                           (otdr_id,)).fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Hasil OTDR tidak ditemukan")
        cursor.execute("DELETE FROM otdr_results WHERE id = ?", (otdr_id,))
        _audit(cursor, "DELETE", "OTDR", otdr_id, f"{r['cable_name']} {r['core']}", f"Hapus hasil OTDR {r['cable_name']} {r['core']} ({r['measured_at']})")
    return {"message": "Hasil OTDR dihapus"}


@app.get("/api/otdr/template")
def otdr_template(format: str = "xlsx"):
    ex = [["KAB-FEEDER-01", "POP-1", "JC-1", 1, 1310, "2026-10-01", 2450, 1.52, 0.35, 38.2, None, 800, 0.12, "splice", None, "Dari/Ke = aset di ujung kabel; menentukan arah ukur & posisi titik putus"],
          [None, None, None, None, None, None, None, None, None, None, None, 1650, 0.62, "splice", None, "Baris lanjutan: kabel & core kosong = sama dengan baris di atasnya"],
          ["KAB-FEEDER-01", "JC-1", "POP-1", 2, 1310, "2026-10-01", 2450, 1.10, 0.35, 38.5, None, None, None, None, None, "Diukur dari ujung seberang (arah B-A) juga boleh"]]
    if (format or "").lower() == "csv":
        sio = io.StringIO()
        w = csv.writer(sio)
        w.writerow(OTDR_TEMPLATE_HEADER)
        w.writerows([["" if c is None else c for c in r] for r in ex])
        return Response(content=sio.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="template_otdr_netgis.csv"'})
    return Response(content=_xlsx_bytes([("OTDR", OTDR_TEMPLATE_HEADER, ex)]), media_type=XLSX_MEDIA,
                    headers={"Content-Disposition": 'attachment; filename="template_otdr_netgis.xlsx"'})


@app.get("/api/otdr/map")
def otdr_map(cluster: str = "ALL", area: str = "ALL", events: str = "issues"):
    """GeoJSON layer OTDR: kabel yang punya hasil ukur (warna = status terburuk) + titik event (default hanya yang bermasalah)."""
    sc, sp = _scope_conds(cluster, area, "c")
    feats = []
    with db() as conn:
        cursor = conn.cursor()
        p = _loss_params(cursor)
        cabs = cursor.execute("SELECT c.* FROM cables c" + _where(sc + ["c.id IN (SELECT DISTINCT cable_id FROM otdr_results)"]), sp).fetchall()
        for c in cabs:
            coords = _cable_coords(c)
            if not coords:
                continue
            latest = cursor.execute("SELECT * FROM otdr_results WHERE cable_id = ? ORDER BY measured_at DESC, id DESC", (c["id"],)).fetchall()
            per_core = {}
            for r in latest:
                per_core.setdefault(r["core"], r)
            worst = max(per_core.values(), key=lambda r: (OTDR_STATUS_RANK.get(r["status"], 0), r["total_loss_db"] or 0))
            statuses = [r["status"] for r in per_core.values()]
            geom_len = _polyline_length_m(coords)
            feats.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords},
                          "properties": {"kind": "OTDR_CABLE", "id": c["id"], "name": c["name"], "cable_type": c["type"],
                                         "status": worst["status"], "cores_measured": len(per_core), "cores_total": _core_total(c["capacity"]),
                                         "ok": statuses.count("OK"), "warn": statuses.count("WARN"), "bad": statuses.count("BAD"),
                                         "worst_core": worst["core"], "max_loss_db": max(r["total_loss_db"] or 0 for r in per_core.values()),
                                         "worst_delta_db": worst["delta_db"], "last_date": max(r["measured_at"] for r in per_core.values()),
                                         "length_m": round(geom_len, 1)}})
            evs = []
            for core, r in per_core.items():
                for e in _otdr_row(r)["events"]:
                    if e.get("distance_m") is None:
                        continue
                    if events != "all" and e.get("status", "OK") == "OK":
                        continue
                    d = e["distance_m"]
                    if (r["direction"] or "").upper() in ("B-A", "B->A", "BA"):
                        d = (r["length_m"] or geom_len) - d
                    if r["length_m"] and geom_len:
                        d = d * geom_len / r["length_m"]          # skala ke panjang kabel di peta
                    evs.append((d, core, e, r))
            if evs:
                for (la, ln), (d, core, e, r) in zip(_points_along(coords, [x[0] for x in evs]), evs):
                    feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [ln, la]},
                                  "properties": {"kind": "OTDR_EVENT", "cable_id": c["id"], "name": c["name"], "core": core,
                                                 "distance_m": e["distance_m"], "loss_db": e.get("loss_db"), "event_type": e.get("type") or "event",
                                                 "status": e.get("status", "OK"), "date": r["measured_at"]}})
    return {"type": "FeatureCollection", "features": feats,
            "summary": {"cables": sum(1 for f in feats if f["properties"]["kind"] == "OTDR_CABLE"),
                        "events": sum(1 for f in feats if f["properties"]["kind"] == "OTDR_EVENT"),
                        "bad": sum(1 for f in feats if f["properties"]["kind"] == "OTDR_CABLE" and f["properties"]["status"] == "BAD"),
                        "warn": sum(1 for f in feats if f["properties"]["kind"] == "OTDR_CABLE" and f["properties"]["status"] == "WARN")}}


# =====================================================================================
# Round 12b - Redaman & daya optik di setiap aset, segmen OTDR (dari-ke), locator titik putus
# =====================================================================================
class PowerPayload(BaseModel):
    asset_type: str = "NODE"
    asset_id: int
    port_core: str
    tx_dbm: Optional[float] = None
    rx_dbm: Optional[float] = None
    wavelength_nm: Optional[int] = None
    note: Optional[str] = ""
    device: Optional[str] = ""


def _power_row(cursor, atype, aid, port):
    r = cursor.execute("SELECT * FROM optical_power WHERE asset_type = ? AND asset_id = ? AND port_core = ?", (atype, aid, port)).fetchone()
    if not r:
        return None
    prev = cursor.execute("SELECT tx_dbm, rx_dbm, measured_at FROM optical_power_log WHERE asset_type = ? AND asset_id = ? AND port_core = ? "
                          "ORDER BY id DESC LIMIT 1 OFFSET 1", (atype, aid, port)).fetchone()
    return _stamp({"tx_dbm": r["tx_dbm"], "rx_dbm": r["rx_dbm"], "wavelength_nm": r["wavelength_nm"], "note": r["note"] or "",
                   "updated_by": r["updated_by"], "updated_at": r["updated_at"],
                   "prev_tx_dbm": prev["tx_dbm"] if prev else None, "prev_rx_dbm": prev["rx_dbm"] if prev else None,
                   "prev_at": prev["measured_at"] if prev else None}, "updated_at", "prev_at")


@app.put("/api/power")
def put_power(payload: PowerPayload):
    """Catat daya optik terukur (power meter / DDM) pada sebuah port/core aset: Tx (keluar) dan/atau Rx (masuk), dBm."""
    atype = (payload.asset_type or "NODE").upper()
    if atype != "NODE":
        raise HTTPException(status_code=400, detail="Daya optik dicatat pada port aset (POP/Closure/ODP/Pelanggan); redaman kabel diturunkan dari kedua ujungnya")
    for k, v in (("Tx", payload.tx_dbm), ("Rx", payload.rx_dbm)):
        if v is not None and not (-60 <= v <= 30):
            raise HTTPException(status_code=400, detail=f"Daya {k} harus -60 s.d. +30 dBm")
    with db() as conn:
        cursor = conn.cursor()
        n = cursor.execute("SELECT * FROM nodes WHERE id = ?", (payload.asset_id,)).fetchone()
        if not n:
            raise HTTPException(status_code=404, detail="Aset tidak ditemukan")
        payload.port_core = _norm_pop_port(cursor, "NODE", payload.asset_id, payload.port_core)
        if payload.port_core not in _port_labels("NODE", n["type"], n["capacity"]):
            raise HTTPException(status_code=400, detail=f"Port '{payload.port_core}' tidak ada pada {n['name']}")
        key = (atype, payload.asset_id, payload.port_core)
        if payload.tx_dbm is None and payload.rx_dbm is None:
            cursor.execute("DELETE FROM optical_power WHERE asset_type = ? AND asset_id = ? AND port_core = ?", key)
            msg = "Catatan daya dihapus"
        else:
            cursor.execute(
                "INSERT INTO optical_power (asset_type, asset_id, port_core, tx_dbm, rx_dbm, wavelength_nm, note, updated_by, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(asset_type, asset_id, port_core) DO UPDATE SET tx_dbm = excluded.tx_dbm, "
                "rx_dbm = excluded.rx_dbm, wavelength_nm = excluded.wavelength_nm, note = excluded.note, updated_by = excluded.updated_by, "
                "updated_at = excluded.updated_at",
                (*key, payload.tx_dbm, payload.rx_dbm, payload.wavelength_nm, (payload.note or "")[:200], _current_username(), _now_str()))
            msg = "Daya optik disimpan"
            cursor.execute("INSERT INTO optical_power_log (asset_type, asset_id, port_core, tx_dbm, rx_dbm, wavelength_nm, note, device, measured_by, measured_at) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (*key, payload.tx_dbm, payload.rx_dbm, payload.wavelength_nm, (payload.note or "")[:200], (payload.device or "")[:60],
                            _current_username(), _now_str()))
            _audit(cursor, "UPDATE", "POWER", payload.asset_id, n["name"],
                   f"Daya optik {n['name']} {payload.port_core}: Tx {payload.tx_dbm} / Rx {payload.rx_dbm} dBm")
        return {"message": msg, "power": _power_row(cursor, *key)}


@app.get("/api/power/history")
def power_history(asset_id: int, port_core: str, limit: int = 20):
    """Riwayat pengukuran daya optik sebuah port (terbaru dulu) untuk melihat tren."""
    limit = max(1, min(int(limit), 100))
    with db() as conn:
        cursor = conn.cursor()
        rows = cursor.execute("SELECT * FROM optical_power_log WHERE asset_type = 'NODE' AND asset_id = ? AND port_core = ? ORDER BY id DESC LIMIT ?",
                              (asset_id, port_core, limit)).fetchall()
        return [_stamp({"tx_dbm": r["tx_dbm"], "rx_dbm": r["rx_dbm"], "wavelength_nm": r["wavelength_nm"], "device": r["device"] or "",
                        "note": r["note"] or "", "by": r["measured_by"], "at": r["measured_at"]}, "at") for r in rows]


def _status_from_delta(delta, p):
    if delta is None:
        return None
    a = abs(delta)
    return "BAD" if a >= p["otdr_bad_db"] else "WARN" if a >= p["otdr_warn_db"] else "OK"


@app.get("/api/loss/asset")
def loss_asset(asset_type: str, asset_id: int):
    """Kolom redaman sebuah aset: hitung (dari POP) + daya optik terukur (Tx/Rx) per port/core.
    Kabel: tiap core memakai Tx di aset hulu dan Rx di aset hilir sambungannya; mode serat SM/MM menentukan koefisien."""
    asset_type = asset_type.upper()
    if asset_type not in ("NODE", "CABLE"):
        raise HTTPException(status_code=400, detail="Tipe aset harus NODE atau CABLE")
    with db() as conn:
        cursor = conn.cursor()
        if not _exists(cursor, _table_of(asset_type), asset_id):
            raise HTTPException(status_code=404, detail="Aset tidak ditemukan")
        p = _loss_params(cursor)
        _, bset = _boq_cfg(cursor)
        spl = int(bset["splice_per_customer"])
        nodes = {r["id"]: r for r in cursor.execute("SELECT id, name, type, capacity, latitude, longitude FROM nodes").fetchall()}
        if asset_type == "NODE":
            n = nodes[asset_id]
            t = (n["type"] or "").upper()
            ports = _port_labels("NODE", t, n["capacity"])
            res = _calc_loss(cursor, ("NODE", asset_id), p, spl)
            mode = res["mode"] if res["breakdown"]["hops"] else "SM"
            mm = mode == "MM"
            tx = p["mm_tx_dbm"] if mm else p["tx_dbm"]
            rxmin = p["mm_rx_min_dbm"] if mm else p["rx_min_dbm"]
            l_in = res["total_db"]
            conns = cursor.execute(
                "SELECT * FROM core_connections WHERE (from_asset_type = 'NODE' AND from_asset_id = ?) OR (to_asset_type = 'NODE' AND to_asset_id = ?)",
                (asset_id, asset_id)).fetchall()
            names, _c = _trace_graph(cursor)
            peer = {}
            for c in conns:
                if c["from_asset_type"] == "NODE" and c["from_asset_id"] == asset_id and c["from_port_core"]:
                    o = names.get((c["to_asset_type"], c["to_asset_id"]), ("?", None))[0]
                    peer[c["from_port_core"]] = f"{o} [{c['to_port_core'] or '-'}]"
                if c["to_asset_type"] == "NODE" and c["to_asset_id"] == asset_id and c["to_port_core"]:
                    o = names.get((c["from_asset_type"], c["from_asset_id"]), ("?", None))[0]
                    peer[c["to_port_core"]] = f"{o} [{c['from_port_core'] or '-'}]"
            ratio = _splitter_ratio(n["capacity"]) or "1:8" if t == "ODP" else None
            rows = []
            for port in ports:
                used = port in peer
                if t in ("POP", "OLT"):
                    local = p["connector_db"]
                elif t == "ODP":
                    local = l_in + (p["splitter_db"].get(ratio, 0.0) if port.startswith("OUT") else 0.0)
                elif t == "CLOSURE":
                    local = l_in + (p["splice_db"] if used else 0.0)
                else:
                    local = l_in
                calc_db = round(local, 2)
                st = _loss_status(calc_db, p, mode)
                pw = _power_row(cursor, "NODE", asset_id, port)
                rxm = pw["rx_dbm"] if pw else None
                trend = round(rxm - pw["prev_rx_dbm"], 2) if pw and rxm is not None and pw.get("prev_rx_dbm") is not None else None
                rows.append({"port": port, "used": used, "peer": peer.get(port), "power": pw,
                             "meas_status": _meas_status(rxm, p, mode) if t not in ("POP", "OLT") else None,
                             "rx_margin_db": round(rxm - rxmin, 2) if rxm is not None else None,
                             "rx_trend_db": trend,
                             "trend_status": _status_from_delta(trend, p) if trend is not None else None})
            return {"kind": "NODE", "asset": {"id": asset_id, "name": n["name"], "type": t}, "rx_min_dbm": rxmin, "rx_max_dbm": p["rx_max_dbm"],
                    "ports": rows, "editable_power": True, "splitter": ratio}
        cab = cursor.execute("SELECT * FROM cables WHERE id = ?", (asset_id,)).fetchone()
        mode = ((cab["fiber_mode"] if "fiber_mode" in cab.keys() else None) or "SM").upper()
        km = _cable_len_km(cab)
        ends = _cable_ends(cab, nodes)
        coef = _fiber_coef(p, mode)
        lat = _latest_otdr(cursor, asset_id, p)
        conns = cursor.execute("SELECT * FROM core_connections WHERE via_cable_id = ?", (asset_id,)).fetchall()
        names, _c = _trace_graph(cursor)
        by_core = {}
        for c in conns:
            if c["via_core"]:
                by_core.setdefault(c["via_core"], []).append(c)
        total = _core_total(cab["capacity"])
        rows = []

        def side(atype, aid, port, key):
            nm = names.get((atype, aid), ("?", None))[0]
            pw = _power_row(cursor, atype, aid, port) if atype == "NODE" and port else None
            return {"type": atype, "id": aid, "name": nm, "port": port, "power": pw, key: (pw or {}).get(key)}
        for lbl in _core_labels(total):
            cs = by_core.get(lbl, [])
            up = down = None
            if cs:
                c = cs[0]
                up = side(c["from_asset_type"], c["from_asset_id"], c["from_port_core"], "tx_dbm")
                down = side(c["to_asset_type"], c["to_asset_id"], c["to_port_core"], "rx_dbm")
            tx_v = up["tx_dbm"] if up else None
            rx_v = down["rx_dbm"] if down else None
            pw_loss = round(tx_v - rx_v, 2) if tx_v is not None and rx_v is not None else None
            rows.append({"core": lbl, "used": bool(cs), "up": up, "down": down, "pw_loss_db": pw_loss,
                         "pw_status": _meas_status(rx_v, p, mode), "measured": lat.get(lbl)})
        return {"kind": "CABLE", "asset": {"id": asset_id, "name": cab["name"], "type": cab["type"]}, "mode": mode, "length_km": round(km, 3),
                "ends": ends, "cores": rows, "editable_power": False,
                "rx_min_dbm": p["mm_rx_min_dbm"] if mode == "MM" else p["rx_min_dbm"], "rx_max_dbm": p["rx_max_dbm"]}


@app.get("/api/otdr/segments")
def otdr_segments(q: str = "", cluster: str = "ALL", area: str = "ALL", limit: int = 300):
    """Daftar segmen (kabel) yang bisa diukur: kabel + aset di ujung A dan B."""
    limit = max(1, min(int(limit), 1000))
    sc, sp = _scope_conds(cluster, area, "c")
    if q.strip():
        sc.append("c.name LIKE ?")
        sp = list(sp) + [f"%{q.strip()}%"]
    with db() as conn:
        cursor = conn.cursor()
        nodes = {r["id"]: r for r in cursor.execute("SELECT id, name, type, latitude, longitude FROM nodes").fetchall()}
        rows = cursor.execute("SELECT c.* FROM cables c" + _where(sc) + " ORDER BY c.name LIMIT ?", (*sp, limit)).fetchall()
        out = []
        for c in rows:
            ends = _cable_ends(c, nodes)
            out.append({"id": c["id"], "name": c["name"], "type": c["type"], "capacity": c["capacity"],
                        "fiber_mode": ((c["fiber_mode"] if "fiber_mode" in c.keys() else None) or "SM").upper(),
                        "length_m": round(_cable_len_km(c) * 1000, 1), "a": ends["A"], "b": ends["B"]})
    return out


@app.get("/api/otdr/locate")
def otdr_locate(cable_id: int, distance_m: float, from_end: str = "A", otdr_length_m: Optional[float] = None):
    """Ubah jarak hasil OTDR (dari ujung ukur) menjadi titik di peta: koordinat + aset di kiri/kanan titik. Untuk menentukan titik putus."""
    if distance_m < 0:
        raise HTTPException(status_code=400, detail="Jarak tidak boleh negatif")
    if otdr_length_m is not None and otdr_length_m <= 0:
        raise HTTPException(status_code=400, detail="Panjang terukur harus lebih dari 0")
    with db() as conn:
        cursor = conn.cursor()
        cab = cursor.execute("SELECT * FROM cables WHERE id = ?", (cable_id,)).fetchone()
        if not cab:
            raise HTTPException(status_code=404, detail="Kabel tidak ditemukan")
        coords = _cable_coords(cab)
        if not coords:
            raise HTTPException(status_code=400, detail="Kabel tidak punya geometri")
        nodes = {r["id"]: r for r in cursor.execute("SELECT id, name, type, latitude, longitude FROM nodes").fetchall()}
        by_name = {}
        for n in nodes.values():
            by_name.setdefault((n["name"] or "").strip().lower(), []).append(n)
        ends = _cable_ends(cab, nodes)
        fe = (from_end or "A").strip()
        if fe.upper() in ("A", "B"):
            sd = fe.upper()
        else:
            nid, err = _resolve_node_ref(fe, nodes, by_name)
            if err:
                raise HTTPException(status_code=400, detail=err)
            sd = next((s for s in ("A", "B") if ends.get(s) and ends[s]["id"] == nid), None)
            if sd is None:
                raise HTTPException(status_code=400, detail=f"'{nodes[nid]['name']}' bukan ujung kabel ini")
        geom = _polyline_length_m(coords)
        scale = geom / otdr_length_m if otdr_length_m else 1.0
        d_geo = distance_m * scale
        warnings = []
        beyond = d_geo > geom * 1.02
        if beyond:
            warnings.append(f"Jarak ({d_geo:.0f} m pada skala peta) melebihi panjang kabel {geom:.0f} m; periksa ujung ukur atau isi panjang terukur")
        d_geo = min(d_geo, geom)
        if not otdr_length_m and geom and distance_m > 0:
            warnings.append("Panjang optik ukur tidak diisi: jarak dianggap sama dengan panjang di peta (slack/serat longgar tidak dikoreksi)")
        along_a = d_geo if sd == "A" else geom - d_geo
        la, ln = _points_along(coords, [along_a])[0]
        near = []
        for n in nodes.values():
            if (n["type"] or "").upper() == "INCIDENT":
                continue
            sn = _snap_to_polyline(coords, n["latitude"], n["longitude"])
            if sn["offset_m"] > 25:
                continue
            from_start = sn["along_m"] if sd == "A" else geom - sn["along_m"]
            near.append({"id": n["id"], "name": n["name"], "type": n["type"], "from_start_m": round(from_start, 1),
                         "gap_m": round(from_start - d_geo, 1), "offset_m": round(sn["offset_m"], 1)})
        near.sort(key=lambda a: a["from_start_m"])
        before = next((a for a in reversed(near) if a["gap_m"] <= 0), None)
        after = next((a for a in near if a["gap_m"] > 0), None)
        other = "B" if sd == "A" else "A"
        return {"cable_id": cable_id, "cable_name": cab["name"], "lat": la, "lng": ln, "distance_m": round(d_geo, 1),
                "from_end": sd, "from_name": (ends[sd] or {}).get("name"), "to_name": (ends[other] or {}).get("name"),
                "length_m": round(geom, 1), "scale": round(scale, 4), "beyond": beyond, "before": before, "after": after,
                "nearest": sorted(near, key=lambda a: abs(a["gap_m"]))[:3], "warnings": warnings}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")