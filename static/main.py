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
from html import unescape
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
        CREATE TABLE IF NOT EXISTS import_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT, fmt TEXT, owner TEXT, status TEXT DEFAULT 'open',
            on_duplicate TEXT DEFAULT 'skip', file_size INTEGER, total INTEGER, overrides TEXT DEFAULT '{}', ver INTEGER DEFAULT 0,
            counts TEXT, created_at TEXT, updated_at TEXT, validated_at TEXT, committed_at TEXT, result TEXT
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
        CREATE TABLE IF NOT EXISTS sor_traces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT, sha256 TEXT UNIQUE, size INTEGER, meta TEXT, trace TEXT, file_blob BLOB,
            note TEXT, analysis TEXT, uploaded_by TEXT, uploaded_at TEXT
        )
    ''')
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

    # parameter aset (improvement K): kode registrasi (semua node kecuali HH/SLACK/INCIDENT), data layanan pelanggan, trunk POP
    for _c, _t in (("reg_code", "TEXT"), ("service", "TEXT"), ("bandwidth_mbps", "REAL"), ("device_sn", "TEXT"),
                   ("link_type", "TEXT"), ("trunk_mbps", "REAL"), ("trunk_overbook", "REAL")):
        add_column_if_missing("nodes", _c, _t)
    add_column_if_missing("otb_port_devices", "slot", "TEXT")   # slot/PON perangkat (mis. 1/1, 1/1/3) diisi manual
    for _ix, _sql in (
            ("ux_nodes_reg_code", "CREATE UNIQUE INDEX IF NOT EXISTS ux_nodes_reg_code ON nodes(reg_code) WHERE reg_code IS NOT NULL"),
            ("ux_nodes_device_sn", "CREATE UNIQUE INDEX IF NOT EXISTS ux_nodes_device_sn ON nodes(device_sn) WHERE device_sn IS NOT NULL"),
            ("ux_nodes_name", "CREATE UNIQUE INDEX IF NOT EXISTS ux_nodes_name ON nodes(lower(trim(name))) WHERE type != 'INCIDENT'")):
        try:
            cursor.execute(_sql)
        except sqlite3.DatabaseError as _e:   # data lama sudah memuat duplikat -> pemeriksaan tetap di level aplikasi
            print(f"[MIGRATION] Indeks {_ix} tidak dibuat ({_e}); duplikat lama perlu dirapikan")
    # --- FOLDER ASET (gaya Google Earth) + INDEKS PERFORMA ---
    add_column_if_missing("nodes", "folder_path", "TEXT")      # mis. "EKO/ODP"; pemisah "/" = subfolder
    add_column_if_missing("cables", "folder_path", "TEXT")
    cursor.execute("CREATE TABLE IF NOT EXISTS asset_folders (path TEXT PRIMARY KEY)")   # folder kosong yang dibuat pengguna
    cursor.execute("""CREATE TRIGGER IF NOT EXISTS trg_nodes_folder AFTER INSERT ON nodes WHEN NEW.folder_path IS NULL
        BEGIN UPDATE nodes SET folder_path = COALESCE(NULLIF(TRIM(REPLACE(NEW.cluster, '/', '-')), ''), 'Tanpa Cluster')
            || '/' || COALESCE(NULLIF(TRIM(REPLACE(NEW.type, '/', '-')), ''), 'Lain') WHERE id = NEW.id; END""")
    cursor.execute("""CREATE TRIGGER IF NOT EXISTS trg_cables_folder AFTER INSERT ON cables WHEN NEW.folder_path IS NULL
        BEGIN UPDATE cables SET folder_path = COALESCE(NULLIF(TRIM(REPLACE(NEW.cluster, '/', '-')), ''), 'Tanpa Cluster')
            || '/Kabel ' || COALESCE(NULLIF(TRIM(REPLACE(NEW.type, '/', '-')), ''), 'Lain') WHERE id = NEW.id; END""")
    cursor.execute("""UPDATE nodes SET folder_path = COALESCE(NULLIF(TRIM(REPLACE(cluster, '/', '-')), ''), 'Tanpa Cluster')
        || '/' || COALESCE(NULLIF(TRIM(REPLACE(type, '/', '-')), ''), 'Lain') WHERE folder_path IS NULL""")
    cursor.execute("""UPDATE cables SET folder_path = COALESCE(NULLIF(TRIM(REPLACE(cluster, '/', '-')), ''), 'Tanpa Cluster')
        || '/Kabel ' || COALESCE(NULLIF(TRIM(REPLACE(type, '/', '-')), ''), 'Lain') WHERE folder_path IS NULL""")
    for _sql in ("CREATE INDEX IF NOT EXISTS idx_nodes_folder ON nodes(folder_path)",
                 "CREATE INDEX IF NOT EXISTS idx_cables_folder ON cables(folder_path)",
                 "CREATE INDEX IF NOT EXISTS idx_nodes_type_status ON nodes(type, status)",
                 "CREATE INDEX IF NOT EXISTS idx_nodes_cluster ON nodes(cluster, area)",
                 "CREATE INDEX IF NOT EXISTS idx_nodes_pos ON nodes(latitude, longitude)",
                 "CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_node_id)",
                 "CREATE INDEX IF NOT EXISTS idx_cables_type_status ON cables(type, status)",
                 "CREATE INDEX IF NOT EXISTS idx_cables_cluster ON cables(cluster, area)",
                 "CREATE INDEX IF NOT EXISTS idx_cables_from ON cables(from_node_id)",
                 "CREATE INDEX IF NOT EXISTS idx_cables_to ON cables(to_node_id)",
                 "CREATE INDEX IF NOT EXISTS idx_conn_from ON core_connections(from_asset_type, from_asset_id)",
                 "CREATE INDEX IF NOT EXISTS idx_conn_to ON core_connections(to_asset_type, to_asset_id)"):
        cursor.execute(_sql)
    add_column_if_missing("plans", "boq_adjust", "TEXT")      # JSON penyesuaian BOQ (region, qty/item per baris, tambahan)
    add_column_if_missing("cables", "fiber_mode", "TEXT")      # SM (single-mode, bawaan) | MM (multimode)
    add_column_if_missing("otdr_results", "from_node_id", "INTEGER")
    add_column_if_missing("otdr_results", "to_node_id", "INTEGER")
    add_column_if_missing("sor_traces", "incident_id", "INTEGER")
    add_column_if_missing("sor_traces", "is_baseline", "INTEGER DEFAULT 0")
    add_column_if_missing("sor_traces", "baseline_by", "TEXT")
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

    # --- WILAYAH: Regional -> Cluster -> Area (daftar resmi; aset menyimpan nama cluster/area sebagai teks) ---
    cursor.execute("CREATE TABLE IF NOT EXISTS wil_regions (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE)")
    cursor.execute("""CREATE TABLE IF NOT EXISTS wil_clusters (id INTEGER PRIMARY KEY AUTOINCREMENT,
        region_id INTEGER NOT NULL REFERENCES wil_regions(id), name TEXT NOT NULL UNIQUE COLLATE NOCASE)""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS wil_areas (id INTEGER PRIMARY KEY AUTOINCREMENT,
        cluster_id INTEGER NOT NULL REFERENCES wil_clusters(id), name TEXT NOT NULL UNIQUE COLLATE NOCASE)""")
    _wil_seed(cursor)

    _bootstrap_admin(cursor)

    conn.commit()
    conn.close()
    print("[INFO] Database topologi & migrasi berhasil diperbarui.")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    _refresh_import_limits(True)
    yield


app = FastAPI(title="ISP WebGIS Prototype API", lifespan=lifespan)

# Allow CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
try:   # data peta besar (puluhan MB JSON) jadi ~10x lebih kecil di jaringan; berkas < 1 KB tidak dimampatkan
    from fastapi.middleware.gzip import GZipMiddleware
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
except ImportError:
    pass


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
ROLE_PERMS["admin"] = set().union(*ROLE_PERMS.values()) | {"asset.delete", "user.manage", "plan.rules", "khs.edit", "loss.edit", "import.config", "wilayah.edit"}

# Urutan penting: yang pertama cocok dipakai. perm None = publik, "auth" = cukup sudah login.
ROUTE_PERMS = [
    ("POST", r"^/api/auth/(login|logout)$", None),
    ("GET", r"^/api/auth/me$", None),
    ("POST", r"^/api/auth/password$", "auth"),
    (None, r"^/api/users(/.*)?$", "user.manage"),
    ("GET", r"^/api/audit$", "audit.view"),
    ("POST", r"^/api/audit/\d+/restore$", "audit.restore"),
    ("POST", r"^/api/incidents/\d+/repairs/\d+/undo$", "repair.undo"),
    ("POST", r"^/api/incidents/(locate|analyze-impact|map-visibility|preview-impact)$", "incident.write"),
    ("PUT", r"^/api/incidents/\d+/map-visibility$", "incident.write"),
    ("POST", r"^/api/import/(preview|commit|commit-async)$", "data.import"),
    ("PUT", r"^/api/import/limits$", "import.config"),
    (None, r"^/api/import/(sessions|assets)(/.*)?$", "data.import"),
    ("PUT", r"^/api/plan/rules$", "plan.rules"),
    ("PUT", r"^/api/loss/params$", "loss.edit"),
    ("POST", r"^/api/otdr/(preview|commit|manual|sor|sor/parse|sor/analyze|sor/analyze2)$", "otdr.upload"),
    ("POST", r"^/api/otdr/sor/\d+/baseline$", "otdr.upload"),
    ("DELETE", r"^/api/otdr/sor/\d+/baseline$", "otdr.upload"),
    ("POST", r"^/api/otdr/sor/\d+/incident$", "incident.write"),
    ("PUT", r"^/api/power$", "power.write"),
    ("PUT", r"^/api/nodes/\d+/port-devices$", "asset.write"),
    ("DELETE", r"^/api/nodes/\d+/port-devices$", "asset.write"),
    ("DELETE", r"^/api/otdr/(sor/)?\d+$", "otdr.upload"),
    ("POST", r"^/api/cables/\d+/core-remap$", "core.remap"),
    ("POST", r"^/api/(plans/bulk|plan/alternatives|boq/recap)$", "plan.write"),
    ("POST", r"^/api/khs/import$", "khs.edit"),
    ("PUT", r"^/api/khs/[^/]+$", "khs.edit"),
    ("PUT", r"^/api/boq/map$", "khs.edit"),
    ("POST", r"^/api/boq/(calc|export)$", "plan.write"),
    ("POST", r"^/api/coverage/bulk/(parse|run|export)$", "plan.write"),
    ("PUT", r"^/api/plans/\d+/boq$", "plan.write"),
    ("POST", r"^/api/plan/preview$", "plan.write"),
    ("POST", r"^/api/plan/pdf$", "plan.write"),
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
    ("POST", r"^/api/assets/bulk-delete$", "asset.delete"),
    ("POST", r"^/api/assets/move$", "asset.write"),
    ("POST", r"^/api/assets/bulk-update$", "asset.write"),
    ("POST", r"^/api/assets/autoname$", "asset.write"),
    ("PUT", r"^/api/naming$", "wilayah.edit"),
    ("POST", r"^/api/naming/preview$", "wilayah.edit"),
    ("POST", r"^/api/topofix/(capacity|split|ends|connect|customers|components)/(preview|apply)$", "asset.write"),
    ("POST", r"^/api/topofix/ends/link$", "asset.write"),
    ("POST", r"^/api/topofix/puzzle/(preview|apply)$", "asset.write"),
    ("GET", r"^/api/topofix/", "asset.write"),
    ("DELETE", r"^/api/topofix/connect/batch/[A-Za-z0-9_-]+$", "connection.delete"),
    ("POST", r"^/api/wilayah(/.*)?$", "wilayah.edit"),
    ("PUT", r"^/api/wilayah(/.*)?$", "wilayah.edit"),
    ("DELETE", r"^/api/wilayah(/.*)?$", "wilayah.edit"),
    ("POST", r"^/api/folders(/rename)?$", "asset.write"),
    ("DELETE", r"^/api/folders$", "asset.write"),
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
    "reg_code": "kode registrasi", "service": "layanan", "bandwidth_mbps": "bandwidth (Mbps)", "device_sn": "SN perangkat",
    "link_type": "jenis layanan", "trunk_mbps": "kapasitas trunk (Mbps)", "trunk_overbook": "rasio overbooking",
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
    name: str = ""                     # kosong = nama otomatis (selain POP dan PELANGGAN)
    type: str
    status: str
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    cluster: Optional[str] = None      # wajib terdaftar di menu Wilayah (kosong = bawaan lama bila terdaftar)
    area: Optional[str] = None
    city: Optional[str] = "Kota Banjarmasin"
    capacity: Optional[str] = "8 Port"
    spec_data: Optional[str] = "{}"
    parent_node_id: Optional[int] = None
    upstream_cable_id: Optional[int] = None
    reg_code: Optional[str] = None          # kode registrasi (opsional)
    service: Optional[str] = None           # khusus PELANGGAN
    bandwidth_mbps: Optional[float] = None  # khusus PELANGGAN
    device_sn: Optional[str] = None         # khusus PELANGGAN (SN ONT/CPE)
    link_type: Optional[str] = None         # khusus PELANGGAN: GPON | PTP
    trunk_mbps: Optional[float] = None      # khusus POP: kapasitas trunk
    trunk_overbook: Optional[float] = None  # khusus POP: rasio overbooking (>=1)


class CableCreate(BaseModel):
    name: str = ""                     # kosong = nama otomatis
    type: str
    status: str
    coordinates: List[List[float]]
    cluster: Optional[str] = None
    area: Optional[str] = None
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
    reg_code: Optional[str] = None
    service: Optional[str] = None
    bandwidth_mbps: Optional[float] = None
    device_sn: Optional[str] = None
    link_type: Optional[str] = None
    trunk_mbps: Optional[float] = None
    trunk_overbook: Optional[float] = None


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


# --- PARAMETER ASET, KODE REGISTRASI & ANTI-DUPLIKAT (tanpa membedakan huruf besar/kecil) ---
NO_REG_TYPES = {"HH", "SLACK", "INCIDENT"}            # kabel juga tidak punya kode registrasi
LINK_TYPES = ["GPON", "PTP"]
SERVICE_SUGGEST = ["Internet", "Dedicated Internet", "IPTV", "VPN L2", "VPN L3", "Lainnya"]
CUSTOMER_FIELDS = ("service", "bandwidth_mbps", "device_sn", "link_type")
TRUNK_FIELDS = ("trunk_mbps", "trunk_overbook")
BW_MAX_MBPS = 1_000_000
REG_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9._/\-]{1,39}$")
SN_RE = re.compile(r"^[A-Z0-9][A-Z0-9._:\-]{3,39}$")
NODE_ASSET_FIELDS = ("reg_code",) + CUSTOMER_FIELDS + TRUNK_FIELDS


def _norm_name(v) -> str:
    """Rapikan nama: buang spasi di tepi & ganda. Huruf dibiarkan sesuai input (perbandingan memakai _name_key)."""
    return " ".join(str(v or "").split())


def _name_key(v) -> str:
    return _norm_name(v).lower()


def _norm_code(v):
    """Kode registrasi / SN: huruf besar, tanpa spasi. Kosong -> None."""
    t = re.sub(r"\s+", "", str(v or "")).upper()
    return t or None


def _find_node_dup(cursor, column, value, exclude_id=None):
    if value is None:
        return None
    if column == "name":
        sql, arg = "SELECT id, name, type FROM nodes WHERE type != 'INCIDENT' AND lower(trim(name)) = ?", _name_key(value)
    else:
        sql, arg = f"SELECT id, name, type FROM nodes WHERE {column} = ?", value
    args = [arg]
    if exclude_id is not None:
        sql += " AND id != ?"
        args.append(exclude_id)
    return cursor.execute(sql + " LIMIT 1", args).fetchone()


def _find_cable_dup(cursor, name, exclude_id=None):
    sql, args = "SELECT id, name, type FROM cables WHERE lower(trim(name)) = ?", [_name_key(name)]
    if exclude_id is not None:
        sql += " AND id != ?"
        args.append(exclude_id)
    return cursor.execute(sql + " LIMIT 1", args).fetchone()


def _check_name_unique_node(cursor, name, ntype, exclude_id=None):
    if (ntype or "").upper() == "INCIDENT":
        return
    d = _find_node_dup(cursor, "name", name, exclude_id)
    if d:
        raise HTTPException(status_code=409, detail=(
            f"Nama '{_norm_name(name)}' sudah dipakai aset '{d['name']}' ({d['type']}, ID {d['id']}). "
            "Nama harus unik, tanpa membedakan huruf besar/kecil."))


def _check_name_unique_cable(cursor, name, exclude_id=None):
    d = _find_cable_dup(cursor, name, exclude_id)
    if d:
        raise HTTPException(status_code=409, detail=(
            f"Nama kabel '{_norm_name(name)}' sudah dipakai kabel '{d['name']}' ({d['type']}, ID {d['id']}). "
            "Nama harus unik, tanpa membedakan huruf besar/kecil."))


def _clean_asset_fields(cursor, ntype, data: dict, exclude_id=None, creating=False) -> dict:
    """Normalisasi + validasi parameter aset pada dict `data` (hanya kunci yang dikirim). Mengembalikan data baru."""
    t = (ntype or "").upper()
    out = dict(data)
    if "reg_code" in out:
        code = _norm_code(out["reg_code"])
        if code is not None:
            if t in NO_REG_TYPES:
                raise HTTPException(status_code=400, detail=f"Aset bertipe {t} tidak memakai kode registrasi")
            if not REG_CODE_RE.match(code):
                raise HTTPException(status_code=400, detail="Kode registrasi hanya huruf/angka/. _ / - (2-40 karakter)")
            d = _find_node_dup(cursor, "reg_code", code, exclude_id)
            if d:
                raise HTTPException(status_code=409, detail=f"Kode registrasi {code} sudah dipakai aset '{d['name']}' ({d['type']}, ID {d['id']})")
        out["reg_code"] = code
    cust_given = [k for k in CUSTOMER_FIELDS if out.get(k) not in (None, "")]
    if cust_given and t != "PELANGGAN":
        raise HTTPException(status_code=400, detail="Layanan, bandwidth, SN perangkat, dan jenis hanya untuk aset PELANGGAN")
    trunk_given = [k for k in TRUNK_FIELDS if out.get(k) not in (None, "")]
    if trunk_given and t != "POP":
        raise HTTPException(status_code=400, detail="Kapasitas trunk hanya untuk aset POP")
    if "service" in out:
        sv = _norm_name(out["service"])
        if len(sv) > 80:
            raise HTTPException(status_code=400, detail="Layanan maksimal 80 karakter")
        out["service"] = sv or None
    if "bandwidth_mbps" in out:
        bw = out["bandwidth_mbps"]
        if bw in (None, ""):
            out["bandwidth_mbps"] = None
        else:
            try:
                bw = float(bw)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="Bandwidth harus berupa angka (Mbps)")
            if not (0 < bw <= BW_MAX_MBPS):
                raise HTTPException(status_code=400, detail=f"Bandwidth harus > 0 dan maksimal {BW_MAX_MBPS:,} Mbps")
            out["bandwidth_mbps"] = round(bw, 3)
    if "device_sn" in out:
        sn = _norm_code(out["device_sn"])
        if sn is not None:
            if not SN_RE.match(sn):
                raise HTTPException(status_code=400, detail="SN perangkat hanya huruf/angka/. _ : - (4-40 karakter)")
            d = _find_node_dup(cursor, "device_sn", sn, exclude_id)
            if d:
                raise HTTPException(status_code=409, detail=f"SN perangkat {sn} sudah dipakai pelanggan '{d['name']}' (ID {d['id']})")
        out["device_sn"] = sn
    if "link_type" in out:
        lt = str(out["link_type"] or "").strip().upper()
        if lt and lt not in LINK_TYPES:
            raise HTTPException(status_code=400, detail=f"Jenis layanan harus salah satu dari: {', '.join(LINK_TYPES)}")
        out["link_type"] = lt or None
    if "trunk_mbps" in out:
        tr = out["trunk_mbps"]
        if tr in (None, ""):
            out["trunk_mbps"] = None
        else:
            try:
                tr = float(tr)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="Kapasitas trunk harus berupa angka (Mbps)")
            if not (0 < tr <= BW_MAX_MBPS * 10):
                raise HTTPException(status_code=400, detail="Kapasitas trunk harus > 0 Mbps")
            out["trunk_mbps"] = round(tr, 3)
    if "trunk_overbook" in out:
        ob = out["trunk_overbook"]
        if ob in (None, ""):
            out["trunk_overbook"] = None
        else:
            try:
                ob = float(ob)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="Rasio overbooking harus berupa angka")
            if not (1 <= ob <= 100):
                raise HTTPException(status_code=400, detail="Rasio overbooking harus 1 s.d. 100 (1 = tanpa overbooking)")
            out["trunk_overbook"] = round(ob, 2)
    return out


def _unique_node_name(cursor, base) -> str:
    """Nama otomatis yang pasti belum dipakai: tambahkan ' (2)', ' (3)' ... bila bentrok."""
    cand, k = _norm_name(base), 2
    while _find_node_dup(cursor, "name", cand):
        cand = f"{_norm_name(base)} ({k})"
        k += 1
    return cand


def _unique_cable_name(cursor, base) -> str:
    cand, k = _norm_name(base), 2
    while _find_cable_dup(cursor, cand):
        cand = f"{_norm_name(base)} ({k})"
        k += 1
    return cand


def _gen_reg_code(cursor, ntype) -> str:
    """Kode registrasi otomatis: <TIPE>-<nomor 5 digit>, nomor berikutnya yang belum terpakai."""
    pre = {"POP": "POP", "CLOSURE": "CLS", "ODP": "ODP", "TIANG": "TNG", "PELANGGAN": "PLG"}.get((ntype or "").upper(), (ntype or "AST").upper()[:3])
    n = cursor.execute("SELECT COUNT(*) FROM nodes WHERE reg_code LIKE ?", (pre + "-%",)).fetchone()[0] + 1
    while cursor.execute("SELECT 1 FROM nodes WHERE reg_code = ?", (f"{pre}-{n:05d}",)).fetchone():
        n += 1
    return f"{pre}-{n:05d}"


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
        _check_name_unique_node(cursor, node_name, "CLOSURE")
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
        (_unique_cable_name(cursor, f"{cab['name']}-seg2"), cab["type"], cab["status"], geom(coords_b), cab["cluster"], cab["area"],
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


def _core_circuit(cursor, names_, idx_, conns_, cable_id, label):
    """Ikuti sirkuit satu core kabel sampai hilir: (node_ids, cable_ids) yang disuplai core tsb."""
    nodes, cables = set(), set()
    for r in conns_:
        if r.get("via_cable_id") != cable_id or r.get("via_core") != label:
            continue
        seen_h, queue_h = set(), deque([r])
        while queue_h:          # ikuti SIRKUIT core tsb (melewati closure lewat joint yang sama)
            h = queue_h.popleft()
            if h["id"] in seen_h:
                continue
            seen_h.add(h["id"])
            if h["to_asset_type"] == "NODE":
                nodes.add(h["to_asset_id"])
            if h.get("via_cable_id") is not None and h["via_cable_id"] != cable_id:
                cables.add(h["via_cable_id"])
            queue_h.extend(_next_down(names_, idx_, h))
    return nodes, cables


def _compute_impact(cursor, linked_cable_id, linked_node_id, cores):
    """Himpunan (kabel, node) yang terdampak. cores = daftar label core (None = seluruh kabel/aset)."""
    cables, nodes = set(), set()
    if linked_cable_id is not None and cores:
        # Gangguan level core (mis. redaman tinggi): hanya yang disuplai core tsb yang terdampak
        names_, conns_ = _trace_graph(cursor)
        idx_ = _hop_index(conns_)
        for lbl in cores:
            n_, c_ = _core_circuit(cursor, names_, idx_, conns_, linked_cable_id, lbl)
            nodes |= n_
            cables |= c_
    else:
        cables, nodes = get_downstream_assets(cursor, start_cable_id=linked_cable_id, start_node_id=linked_node_id)
    return cables, nodes


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

    cables, nodes = _compute_impact(cursor, inc["linked_cable_id"], inc["linked_node_id"], cores)

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


def _scope_conds(cluster="ALL", area="ALL", alias="", region="ALL"):
    """Kondisi SQL filter Regional/Cluster/Area (tanpa beda huruf besar-kecil). Kolom kosong dianggap nilai bawaan,
    sama seperti yang ditampilkan di peta. 'ALL'/kosong = tanpa filter. Regional '__NONE__' = aset di luar daftar Wilayah."""
    p = f"{alias}." if alias else ""
    conds, params = [], []
    rg = (region or "").strip()
    if rg and rg.upper() != "ALL":
        cexpr = f"UPPER(COALESCE(NULLIF(TRIM({p}cluster), ''), '{SCOPE_DEFAULTS['cluster']}'))"
        if rg == WIL_NONE:
            conds.append(f"{cexpr} NOT IN (SELECT UPPER(name) FROM wil_clusters)")
        else:
            conds.append(f"{cexpr} IN (SELECT UPPER(c.name) FROM wil_clusters c JOIN wil_regions g ON g.id = c.region_id "
                         f"WHERE g.name = ? COLLATE NOCASE)")
            params.append(rg)
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
                  "installation": "installation", "folder": "folder_path", "length": None}   # length: dihitung dari geometri, diurutkan di Python


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


def _inv_where(q, cluster, area, type, status, installation, folder, region="ALL"):
    """Kondisi WHERE inventaris (dipakai daftar inventaris & jumlah per folder)."""
    where, params = [], []
    if q.strip():
        like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(name LIKE ? ESCAPE '\\' OR city LIKE ? ESCAPE '\\' OR area LIKE ? ESCAPE '\\' "
                     "OR reg_code LIKE ? ESCAPE '\\' OR device_sn LIKE ? ESCAPE '\\')")
        params += [like, like, like, like, like]
    sc, sp = _scope_conds(cluster, area, "", region)
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
    fp = _norm_folder(folder, allow_empty=True)
    if fp:   # folder + seluruh subfoldernya
        where.append("(folder_path = ? OR folder_path LIKE ? ESCAPE '\\')")
        params += [fp, _like_escape(fp) + "/%"]
    if installation == "NONE":
        where.append("category = 'CABLE' AND (installation IS NULL OR installation = '')")
    elif installation != "ALL":
        where.append("installation = ?")
        params.append(installation)

    return where, params


_INV_BASE = ("SELECT 'NODE' AS category, id, name, type, status, cluster, area, city, capacity, "
        "NULL AS installation, spec_data, reg_code, service, bandwidth_mbps, device_sn, link_type, trunk_mbps, trunk_overbook, folder_path FROM nodes "
        "UNION ALL "
        "SELECT 'CABLE' AS category, id, name, type, status, cluster, area, city, capacity, "
        "installation, NULL AS spec_data, NULL AS reg_code, NULL AS service, NULL AS bandwidth_mbps, NULL AS device_sn, "
        "NULL AS link_type, NULL AS trunk_mbps, NULL AS trunk_overbook, folder_path FROM cables")


@app.get("/api/folders/counts")
def folder_counts(q: str = "", cluster: str = "ALL", area: str = "ALL", type: str = "ALL", status: str = "ALL",
                  installation: str = "ALL", region: str = "ALL"):
    """Jumlah aset per folder yang LOLOS filter inventaris aktif (jenis, status, cari, wilayah, pemasangan)."""
    if installation not in ("ALL", "NONE") and installation not in INSTALLATIONS:
        raise HTTPException(status_code=400, detail=f"Filter pemasangan tidak valid: {installation}")
    where, params = _inv_where(q, cluster, area, type, status, installation, "", region)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    with db() as conn:
        rows = conn.execute(f"SELECT folder_path AS p, COUNT(*) AS n FROM ({_INV_BASE}){where_sql} "
                            f"GROUP BY folder_path", params).fetchall()
    return {"folders": [{"path": r["p"], "count": r["n"]} for r in rows if r["p"]]}


@app.get("/api/inventory")
def get_inventory(q: str = "", cluster: str = "ALL", area: str = "ALL", type: str = "ALL", status: str = "ALL",
                  installation: str = "ALL", sort: str = "name", order: str = "asc", page: int = 1,
                  page_size: int = 25, folder: str = "", region: str = "ALL"):
    """Daftar aset (node + kabel) terpaginasi. type='CABLE' = semua kabel; selain itu mencocokkan tipe node/kabel.
    installation: Udara | Tanah | NONE (belum diisi) -> hanya berlaku untuk kabel."""
    if sort not in INVENTORY_SORT:
        raise HTTPException(status_code=400, detail=f"Kolom sort tidak valid: {sort}")
    if installation not in ("ALL", "NONE") and installation not in INSTALLATIONS:
        raise HTTPException(status_code=400, detail=f"Filter pemasangan tidak valid: {installation}")
    order_sql = "DESC" if order.lower() == "desc" else "ASC"
    page_size = max(1, min(int(page_size), 200))

    where, params = _inv_where(q, cluster, area, type, status, installation, folder, region)
    base = _INV_BASE
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
def get_nodes(cluster: str = "ALL", area: str = "ALL", region: str = "ALL"):
    sc, sp = _scope_conds(cluster, area, "", region)
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
                "folder_path": n.get("folder_path"),
                "reg_code": n.get("reg_code"), "service": n.get("service"), "bandwidth_mbps": n.get("bandwidth_mbps"),
                "device_sn": n.get("device_sn"), "link_type": n.get("link_type"),
                "trunk_mbps": n.get("trunk_mbps"), "trunk_overbook": n.get("trunk_overbook"),
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
    node.name = _norm_name(node.name)
    auto_name = not node.name and (node.type or "").upper() in NAME_AUTO_NODE
    if not node.name and not auto_name:
        raise HTTPException(status_code=400, detail="Nama aset wajib diisi (POP dan Pelanggan diberi nama manual)")
    if len(node.name) > 120:
        raise HTTPException(status_code=400, detail="Nama aset maksimal 120 karakter")
    with db() as conn:
        cursor = conn.cursor()
        _check_refs(
            cursor,
            **{"Parent node": ("nodes", node.parent_node_id),
               "Kabel upstream": ("cables", node.upstream_cable_id)},
        )
        node.cluster, node.area = _wil_resolve(cursor, node.cluster, node.area)
        if auto_name:
            node.name = _gen_name(cursor, "NODE", node.type, node.cluster, node.area)
        _check_name_unique_node(cursor, node.name, node.type)
        extra = _clean_asset_fields(cursor, node.type, {k: getattr(node, k) for k in NODE_ASSET_FIELDS})
        cursor.execute('''
            INSERT INTO nodes (name, type, status, latitude, longitude, cluster, area, city,
                               capacity, spec_data, parent_node_id, upstream_cable_id,
                               reg_code, service, bandwidth_mbps, device_sn, link_type, trunk_mbps, trunk_overbook)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            node.name, node.type, node.status, node.latitude, node.longitude,
            node.cluster, node.area, node.city, node.capacity, node.spec_data,
            node.parent_node_id, node.upstream_cable_id,
            extra["reg_code"], extra["service"], extra["bandwidth_mbps"], extra["device_sn"], extra["link_type"],
            extra["trunk_mbps"], extra["trunk_overbook"],
        ))
        node_id = cursor.lastrowid
        _audit(cursor, "CREATE", "NODE", node_id, node.name, f"Tambah {node.type} {node.name}",
               snapshot=_row_dict(cursor.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()))
    print(f"[SUCCESS] Node '{node.name}' berhasil disimpan dengan ID: {node_id}")
    return {"message": "Node berhasil ditambahkan", "id": node_id, "name": node.name}


# 3. GET ALL CABLES
def _core_fault_info(c, faults, used):
    """Gangguan sebagian core pada kabel (tiket aktif dengan core tertentu); None bila tidak ada / seluruh kabel."""
    f = faults.get(c["id"])
    if not f or f["all"] or not f["cores"]:
        return None
    return {"down": len(f["cores"]), "labels": sorted(f["cores"]), "used": used.get(c["id"], 0),
            "total": _core_total(c.get("capacity") or "24C"),
            "tickets": sorted({t for v in f["cores"].values() for t in v})}


@app.get("/api/cables")
def get_cables(cluster: str = "ALL", area: str = "ALL", region: str = "ALL"):
    sc, sp = _scope_conds(cluster, area, "", region)
    with db() as conn:
        cables = conn.execute("SELECT * FROM cables" + _where(sc), sp).fetchall()

    with db() as conn:
        _faults = _active_core_faults(conn.cursor())
        _used = {}
        for r in conn.execute("SELECT via_cable_id AS c, COUNT(DISTINCT via_core) AS n FROM core_connections "
                              "WHERE via_cable_id IS NOT NULL GROUP BY via_cable_id").fetchall():
            _used[r["c"]] = r["n"]

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
                "folder_path": c.get("folder_path"),
                "fiber_mode": c.get("fiber_mode") or "SM",
                "length_m": round(_polyline_length_m(geom["coordinates"]), 1),
                "parent_cable_id": c.get("parent_cable_id"),
                "from_node_id": c.get("from_node_id"),
                "to_node_id": c.get("to_node_id"),
                "core_fault": _core_fault_info(c, _faults, _used),
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
    cable.name = _norm_name(cable.name)
    auto_name = not cable.name and cable.type in NAME_AUTO_CABLE
    if not cable.name and not auto_name:
        raise HTTPException(status_code=400, detail="Nama kabel wajib diisi")

    with db() as conn:
        cursor = conn.cursor()
        cable.cluster, cable.area = _wil_resolve(cursor, cable.cluster, cable.area)
        if auto_name:
            cable.name = _gen_name(cursor, "CABLE", cable.type, cable.cluster, cable.area, None, None, cable.capacity, cable.installation)
        _check_name_unique_cable(cursor, cable.name)
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
    return {"message": "Kabel berhasil ditambahkan", "id": cable_id, "name": cable.name}


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
def get_summary(cluster: str = "ALL", area: str = "ALL", region: str = "ALL"):
    sc, sp = _scope_conds(cluster, area, "", region)
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


# =====================================================================================
# WILAYAH: Regional -> Cluster -> Area (daftar resmi, dikelola admin)
# =====================================================================================
REGION_UNSET = "Belum diatur"      # regional penampung data lama sampai admin memindahkan clustermya
WIL_NONE = "__NONE__"             # nilai filter Regional: aset yang cluster-nya tidak ada di daftar
WIL_UNREG_LABEL = "Belum terdaftar"
_WIL_BAD = re.compile(r"[/\\<>|;]")


def _wil_name(v, label):
    s_ = re.sub(r"\s+", " ", str(v or "")).strip()
    if not s_:
        raise HTTPException(status_code=400, detail=f"Nama {label} wajib diisi")
    if len(s_) > 60:
        raise HTTPException(status_code=400, detail=f"Nama {label} maksimal 60 karakter")
    if _WIL_BAD.search(s_):
        raise HTTPException(status_code=400, detail=f"Nama {label} tidak boleh memuat karakter / \\ < > | ;")
    if s_.upper() in ("ALL", WIL_NONE, WIL_UNREG_LABEL.upper()):
        raise HTTPException(status_code=400, detail=f"Nama '{s_}' dicadangkan sistem")
    return s_


def _wil_seed(cursor):
    """Isi awal daftar dari cluster/area yang sudah ada di data (sekali saja, saat daftar masih kosong)."""
    if cursor.execute("SELECT 1 FROM wil_regions LIMIT 1").fetchone() or cursor.execute("SELECT 1 FROM wil_clusters LIMIT 1").fetchone():
        return
    pairs = []
    for t in ("nodes", "cables", "incidents"):
        try:
            for r in cursor.execute(f"SELECT DISTINCT TRIM(cluster) c, TRIM(area) a FROM {t} "
                                    f"WHERE TRIM(COALESCE(cluster, '')) != '' ORDER BY 1, 2"):
                pairs.append((r[0], r[1] or ""))
        except sqlite3.DatabaseError:
            pass
    pairs.append((SCOPE_DEFAULTS["cluster"], SCOPE_DEFAULTS["area"]))
    cursor.execute("INSERT INTO wil_regions (name) VALUES (?)", (REGION_UNSET,))
    rid = cursor.lastrowid
    cids, used_areas = {}, set()
    for c, a in pairs:
        if c.upper() not in cids:
            cursor.execute("INSERT INTO wil_clusters (region_id, name) VALUES (?, ?)", (rid, c))
            cids[c.upper()] = cursor.lastrowid
        if a and a.upper() not in used_areas:
            cursor.execute("INSERT INTO wil_areas (cluster_id, name) VALUES (?, ?)", (cids[c.upper()], a))
            used_areas.add(a.upper())
    print(f"[MIGRASI] Daftar Wilayah dibuat dari data: {len(cids)} cluster, {len(used_areas)} area (regional '{REGION_UNSET}')")


def _wil_maps(cursor):
    cl = {r["name"].upper(): dict(r) for r in cursor.execute(
        "SELECT c.id, c.name, c.region_id, g.name AS rname FROM wil_clusters c JOIN wil_regions g ON g.id = c.region_id")}
    ar = {r["name"].upper(): dict(r) for r in cursor.execute(
        "SELECT a.id, a.name, a.cluster_id, c.name AS cname FROM wil_areas a JOIN wil_clusters c ON c.id = a.cluster_id")}
    return cl, ar


def _wil_resolve(cursor, cluster, area, maps=None):
    """Cluster+Area harus terdaftar dan area harus milik cluster itu. Mengembalikan nama resmi (cluster, area).
    Keduanya kosong = nilai bawaan lama (EKO/BANJARMASIN) bila terdaftar; area saja = cluster diturunkan dari area."""
    cl, ar = maps or _wil_maps(cursor)
    c, a = (cluster or "").strip(), (area or "").strip()
    if not c and not a:
        c, a = SCOPE_DEFAULTS["cluster"], SCOPE_DEFAULTS["area"]
        if c.upper() not in cl or a.upper() not in ar:
            raise HTTPException(status_code=400, detail="Cluster dan Area wajib dipilih")
    if a and not c:
        hit = ar.get(a.upper())
        if not hit:
            raise HTTPException(status_code=400, detail=f"Area '{a}' belum terdaftar. Minta admin menambahkannya di menu Wilayah")
        c = hit["cname"]
    ch = cl.get(c.upper())
    if not ch:
        raise HTTPException(status_code=400, detail=f"Cluster '{c}' belum terdaftar. Minta admin menambahkannya di menu Wilayah")
    if not a:
        raise HTTPException(status_code=400, detail=f"Area wajib dipilih untuk cluster '{ch['name']}'")
    ah = ar.get(a.upper())
    if not ah:
        raise HTTPException(status_code=400, detail=f"Area '{a}' belum terdaftar. Minta admin menambahkannya di menu Wilayah")
    if ah["cluster_id"] != ch["id"]:
        raise HTTPException(status_code=400, detail=f"Area '{ah['name']}' bukan bagian dari cluster '{ch['name']}' (milik '{ah['cname']}')")
    return ch["name"], ah["name"]


def _wil_check_update(cursor, table, rid, data):
    """Ubah aset: cluster/area hanya divalidasi bila benar-benar berubah (aset lama di luar daftar tetap bisa diedit)."""
    if "cluster" not in data and "area" not in data:
        return
    cur = cursor.execute(f"SELECT cluster, area FROM {table} WHERE id = ?", (rid,)).fetchone()
    nc = data["cluster"] if "cluster" in data else cur["cluster"]
    na = data["area"] if "area" in data else cur["area"]
    if (str(nc or "").strip().upper(), str(na or "").strip().upper()) == (str(cur["cluster"] or "").strip().upper(), str(cur["area"] or "").strip().upper()):
        data.pop("cluster", None); data.pop("area", None)
        return
    data["cluster"], data["area"] = _wil_resolve(cursor, nc, na)


def _region_pred(cursor, region):
    """Fungsi(cluster_UPPER) -> bool untuk filter Regional di ekspor; None bila tanpa filter."""
    rg = (region or "").strip()
    if not rg or rg.upper() == "ALL":
        return None
    cl, _ar = _wil_maps(cursor)
    if rg == WIL_NONE:
        return lambda c: c not in cl
    return lambda c: c in cl and cl[c]["rname"].upper() == rg.upper()


def _wil_counts(cursor):
    """{(CLUSTER_UPPER, AREA_UPPER): [aset, tiket]} dari seluruh data."""
    out = {}
    d = SCOPE_DEFAULTS
    for t, idx in (("nodes", 0), ("cables", 0), ("incidents", 1)):
        extra = " WHERE type != 'INCIDENT'" if t == "nodes" else ""
        for r in cursor.execute(
                f"SELECT UPPER(COALESCE(NULLIF(TRIM(cluster), ''), '{d['cluster']}')) c, "
                f"UPPER(COALESCE(NULLIF(TRIM(area), ''), '{d['area']}')) a, COUNT(*) n FROM {t}{extra} GROUP BY 1, 2"):
            out.setdefault((r["c"], r["a"]), [0, 0])[idx] += r["n"]
    return out


def _wil_tree(cursor):
    cnt = _wil_counts(cursor)
    regions = []
    reg_rows = cursor.execute("SELECT id, name FROM wil_regions ORDER BY name COLLATE NOCASE").fetchall()
    clu_rows = cursor.execute("SELECT id, region_id, name FROM wil_clusters ORDER BY name COLLATE NOCASE").fetchall()
    are_rows = cursor.execute("SELECT id, cluster_id, name FROM wil_areas ORDER BY name COLLATE NOCASE").fetchall()
    reg_names, clu_names, area_keys = set(), set(), set()
    areas_by_c = {}
    for a in are_rows:
        areas_by_c.setdefault(a["cluster_id"], []).append(a)
    clus_by_r = {}
    for c in clu_rows:
        clus_by_r.setdefault(c["region_id"], []).append(c)
    for g in reg_rows:
        cl_out, g_assets, g_inc = [], 0, 0
        for c in clus_by_r.get(g["id"], []):
            ar_out, c_assets, c_inc = [], 0, 0
            clu_names.add(c["name"].upper())
            for a in areas_by_c.get(c["id"], []):
                k = (c["name"].upper(), a["name"].upper())
                area_keys.add(k)
                n, inc = cnt.get(k, [0, 0])
                ar_out.append({"id": a["id"], "name": a["name"], "assets": n, "incidents": inc})
                c_assets += n; c_inc += inc
            # aset di cluster ini yang areanya belum terdaftar ikut dihitung ke cluster
            for (cc, aa), (n, inc) in cnt.items():
                if cc == c["name"].upper() and (cc, aa) not in {(c["name"].upper(), x["name"].upper()) for x in areas_by_c.get(c["id"], [])}:
                    c_assets += n; c_inc += inc
            cl_out.append({"id": c["id"], "name": c["name"], "assets": c_assets, "incidents": c_inc, "areas": ar_out})
            g_assets += c_assets; g_inc += c_inc
        regions.append({"id": g["id"], "name": g["name"], "assets": g_assets, "incidents": g_inc, "clusters": cl_out})
    unreg = []
    for (cc, aa), (n, inc) in sorted(cnt.items()):
        if (cc, aa) not in area_keys:
            unreg.append({"cluster": cc, "area": aa, "assets": n, "incidents": inc,
                          "cluster_registered": cc in clu_names})
    return {"regions": regions, "unregistered": unreg, "unregistered_assets": sum(u["assets"] for u in unreg)}


@app.get("/api/filters/options")
def get_filter_options():
    """Daftar Regional/Cluster/Area untuk semua pilihan (filter, form, ekspor): seluruh daftar resmi Wilayah
    (juga yang belum punya aset) ditambah cluster/area di data yang belum terdaftar (regional 'Belum terdaftar')."""
    with db() as conn:
        cur = conn.cursor()
        tree = _wil_tree(cur)
    clusters, areas, pairs, regions = {}, {}, [], []
    for g in tree["regions"]:
        regions.append({"value": g["name"], "clusters": [c["name"] for c in g["clusters"]], "assets": g["assets"], "id": g["name"]})
        for c in g["clusters"]:
            clusters[c["name"].upper()] = {"value": c["name"], "region": g["name"], "assets": c["assets"],
                                           "incidents": c["incidents"], "registered": True}
            for a in c["areas"]:
                areas[a["name"].upper()] = {"value": a["name"], "clusters": [c["name"]], "assets": a["assets"],
                                            "incidents": a["incidents"], "registered": True}
                pairs.append({"cluster": c["name"], "area": a["name"], "assets": a["assets"], "incidents": a["incidents"],
                              "region": g["name"], "registered": True})
    for u in tree["unregistered"]:
        cu = clusters.get(u["cluster"])
        if cu is None:
            cu = clusters.setdefault(u["cluster"], {"value": u["cluster"], "region": WIL_UNREG_LABEL, "assets": 0,
                                                    "incidents": 0, "registered": False})
        cu["assets"] += u["assets"]; cu["incidents"] += u["incidents"]
        ao = areas.setdefault(u["area"], {"value": u["area"], "clusters": [], "assets": 0, "incidents": 0, "registered": False})
        ao["assets"] += u["assets"]; ao["incidents"] += u["incidents"]
        if cu["value"] not in ao["clusters"]:
            ao["clusters"].append(cu["value"])
        pairs.append({"cluster": cu["value"], "area": u["area"], "assets": u["assets"], "incidents": u["incidents"],
                      "region": cu["region"], "registered": False})
    if tree["unregistered"]:
        regions.append({"value": WIL_UNREG_LABEL, "id": WIL_NONE, "clusters": sorted({u["cluster"] for u in tree["unregistered"]}),
                        "assets": tree["unregistered_assets"]})
    for c in clusters.values():
        c.setdefault("region", WIL_UNREG_LABEL)
    return {"regions": regions, "clusters": [clusters[k] for k in sorted(clusters)], "areas": [areas[k] for k in sorted(areas)],
            "pairs": pairs, "unregistered_assets": tree["unregistered_assets"]}


class WilCreate(BaseModel):
    level: str                         # region | cluster | area
    name: str
    parent_id: Optional[int] = None    # cluster -> id regional; area -> id cluster


class WilUpdate(BaseModel):
    name: Optional[str] = None
    parent_id: Optional[int] = None    # pindah cluster ke regional lain / area ke cluster lain


class WilBulk(BaseModel):
    text: str
    dry_run: Optional[bool] = False


_WIL_LEVELS = {"region": ("wil_regions", "Regional"), "cluster": ("wil_clusters", "Cluster"), "area": ("wil_areas", "Area")}


def _wil_level(level):
    if level not in _WIL_LEVELS:
        raise HTTPException(status_code=400, detail="Tingkat harus region, cluster, atau area")
    return _WIL_LEVELS[level]


def _wil_dup(cursor, table, name, label, exclude_id=None):
    r = cursor.execute(f"SELECT id FROM {table} WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if r and r["id"] != exclude_id:
        raise HTTPException(status_code=409, detail=f"{label} '{name}' sudah ada (nama harus unik)")


def _wil_cascade_cluster(cursor, old, new):
    """Ganti nama cluster di seluruh aset/tiket + folder bawaan 'Cluster/...' yang mengikutinya."""
    n = 0
    for t in ("nodes", "cables", "incidents"):
        n += cursor.execute(f"UPDATE {t} SET cluster = ? WHERE UPPER(TRIM(cluster)) = ?", (new, old.upper())).rowcount
    n_ = new.replace("/", "-"); o_ = old.replace("/", "-")
    for t in ("nodes", "cables"):
        cursor.execute(f"UPDATE {t} SET folder_path = ? || substr(folder_path, ?) WHERE folder_path = ? OR folder_path LIKE ? ESCAPE '\\'",
                       (n_, len(o_) + 1, o_, _like_escape(o_) + "/%"))
    cursor.execute("UPDATE asset_folders SET path = ? || substr(path, ?) WHERE path = ? OR path LIKE ? ESCAPE '\\'",
                   (n_, len(o_) + 1, o_, _like_escape(o_) + "/%"))
    return n


def _wil_cascade_area(cursor, cluster, old_area, new_cluster, new_area):
    n = 0
    for t in ("nodes", "cables", "incidents"):
        n += cursor.execute(f"UPDATE {t} SET cluster = ?, area = ? WHERE UPPER(TRIM(area)) = ? AND UPPER(TRIM(cluster)) = ?",
                            (new_cluster, new_area, old_area.upper(), cluster.upper())).rowcount
    return n


@app.get("/api/wilayah")
def get_wilayah():
    with db() as conn:
        return _wil_tree(conn.cursor())


@app.post("/api/wilayah")
def create_wilayah(req: WilCreate):
    table, label = _wil_level(req.level)
    name = _wil_name(req.name, label)
    with db() as conn:
        cur = conn.cursor()
        _wil_dup(cur, table, name, label)
        if req.level == "region":
            cur.execute("INSERT INTO wil_regions (name) VALUES (?)", (name,))
        elif req.level == "cluster":
            if not cur.execute("SELECT 1 FROM wil_regions WHERE id = ?", (req.parent_id,)).fetchone():
                raise HTTPException(status_code=400, detail="Pilih Regional untuk cluster ini")
            cur.execute("INSERT INTO wil_clusters (region_id, name) VALUES (?, ?)", (req.parent_id, name))
        else:
            if not cur.execute("SELECT 1 FROM wil_clusters WHERE id = ?", (req.parent_id,)).fetchone():
                raise HTTPException(status_code=400, detail="Pilih Cluster untuk area ini")
            cur.execute("INSERT INTO wil_areas (cluster_id, name) VALUES (?, ?)", (req.parent_id, name))
        nid = cur.lastrowid
        _audit(cur, "CREATE", "WILAYAH", nid, name, f"Tambah {label.lower()} {name}")
    return {"message": f"{label} '{name}' ditambahkan", "id": nid}


@app.put("/api/wilayah/{level}/{wid}")
def update_wilayah(level: str, wid: int, req: WilUpdate):
    table, label = _wil_level(level)
    with db() as conn:
        cur = conn.cursor()
        row = cur.execute(f"SELECT * FROM {table} WHERE id = ?", (wid,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"{label} tidak ditemukan")
        old = row["name"]
        new = _wil_name(req.name, label) if req.name is not None else old
        if new.lower() != old.lower():
            _wil_dup(cur, table, new, label, wid)
        moved, msgs = 0, []
        if level == "cluster":
            if req.parent_id is not None and req.parent_id != row["region_id"]:
                if not cur.execute("SELECT 1 FROM wil_regions WHERE id = ?", (req.parent_id,)).fetchone():
                    raise HTTPException(status_code=400, detail="Regional tujuan tidak ditemukan")
                cur.execute("UPDATE wil_clusters SET region_id = ? WHERE id = ?", (req.parent_id, wid))
                msgs.append("dipindah ke regional lain")
            if new != old:
                cur.execute("UPDATE wil_clusters SET name = ? WHERE id = ?", (new, wid))
                moved = _wil_cascade_cluster(cur, old, new)
        elif level == "area":
            cl = cur.execute("SELECT id, name FROM wil_clusters WHERE id = ?", (row["cluster_id"],)).fetchone()
            tgt = cl
            if req.parent_id is not None and req.parent_id != row["cluster_id"]:
                tgt = cur.execute("SELECT id, name FROM wil_clusters WHERE id = ?", (req.parent_id,)).fetchone()
                if not tgt:
                    raise HTTPException(status_code=400, detail="Cluster tujuan tidak ditemukan")
                cur.execute("UPDATE wil_areas SET cluster_id = ? WHERE id = ?", (tgt["id"], wid))
                msgs.append(f"dipindah ke cluster {tgt['name']}")
            if new != old:
                cur.execute("UPDATE wil_areas SET name = ? WHERE id = ?", (new, wid))
            if new != old or tgt["id"] != cl["id"]:
                moved = _wil_cascade_area(cur, cl["name"], old, tgt["name"], new)
        else:
            if new != old:
                cur.execute("UPDATE wil_regions SET name = ? WHERE id = ?", (new, wid))
        parts = ([f"nama {old} -> {new}"] if new != old else []) + msgs
        if not parts:
            return {"message": "Tidak ada perubahan", "updated_assets": 0}
        _audit(cur, "UPDATE", "WILAYAH", wid, new, f"Ubah {label.lower()} {old}: " + ", ".join(parts)
               + (f" ({moved} aset/tiket ikut diperbarui)" if moved else ""))
    return {"message": f"{label} diperbarui" + (f"; {moved} aset/tiket ikut disesuaikan" if moved else ""), "updated_assets": moved}


@app.delete("/api/wilayah/{level}/{wid}")
def delete_wilayah(level: str, wid: int, move_to: Optional[int] = None):
    table, label = _wil_level(level)
    with db() as conn:
        cur = conn.cursor()
        row = cur.execute(f"SELECT * FROM {table} WHERE id = ?", (wid,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"{label} tidak ditemukan")
        name = row["name"]
        if level == "region":
            n = cur.execute("SELECT COUNT(*) FROM wil_clusters WHERE region_id = ?", (wid,)).fetchone()[0]
            if n:
                raise HTTPException(status_code=409, detail=f"Regional '{name}' masih memiliki {n} cluster. Pindahkan atau hapus cluster-nya dulu")
        elif level == "cluster":
            n = cur.execute("SELECT COUNT(*) FROM wil_areas WHERE cluster_id = ?", (wid,)).fetchone()[0]
            if n:
                raise HTTPException(status_code=409, detail=f"Cluster '{name}' masih memiliki {n} area. Pindahkan atau hapus area-nya dulu")
            cnt = sum(v[0] + v[1] for (c, _a), v in _wil_counts(cur).items() if c == name.upper())
            if cnt:
                raise HTTPException(status_code=409, detail=f"Cluster '{name}' masih dipakai {cnt} aset/tiket. Pindahkan asetnya ke cluster lain dulu")
        else:
            cl = cur.execute("SELECT name FROM wil_clusters WHERE id = ?", (row["cluster_id"],)).fetchone()
            nn, ni = _wil_counts(cur).get((cl["name"].upper(), name.upper()), [0, 0])
            if nn + ni:
                if move_to is None:
                    raise HTTPException(status_code=409, detail=f"Area '{name}' masih dipakai {nn} aset dan {ni} tiket. "
                                                                 f"Pilih area tujuan untuk memindahkannya sebelum menghapus")
                if move_to == wid:
                    raise HTTPException(status_code=400, detail="Area tujuan tidak boleh sama dengan area yang dihapus")
                t = cur.execute("SELECT a.name an, c.name cn FROM wil_areas a JOIN wil_clusters c ON c.id = a.cluster_id WHERE a.id = ?", (move_to,)).fetchone()
                if not t:
                    raise HTTPException(status_code=400, detail="Area tujuan tidak ditemukan")
                _wil_cascade_area(cur, cl["name"], name, t["cn"], t["an"])
                _audit(cur, "UPDATE", "WILAYAH", wid, name, f"Pindahkan {nn} aset & {ni} tiket dari area {name} ke {t['cn']} / {t['an']}")
        cur.execute(f"DELETE FROM {table} WHERE id = ?", (wid,))
        _audit(cur, "DELETE", "WILAYAH", wid, name, f"Hapus {label.lower()} {name}")
    return {"message": f"{label} '{name}' dihapus"}


@app.post("/api/wilayah/bulk")
def bulk_wilayah(req: WilBulk):
    """Tempel daftar 'Regional > Cluster > Area' (satu baris per area; 2 kolom = Regional > Cluster). Yang sudah ada dipakai ulang;
    cluster/area yang ada tetapi di induk lain DIPINDAH (aset ikut). dry_run = hanya hitung, tidak menyimpan."""
    lines = [ln for ln in (req.text or "").replace("\r", "").split("\n") if ln.strip()]
    if not lines:
        raise HTTPException(status_code=400, detail="Daftar kosong")
    if len(lines) > 5000:
        raise HTTPException(status_code=400, detail="Maksimal 5000 baris sekali tempel")
    res = {"regions_created": 0, "clusters_created": 0, "clusters_moved": 0, "areas_created": 0, "areas_moved": 0,
           "unchanged": 0, "errors": [], "assets_updated": 0}
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SAVEPOINT wil_bulk")
        for i, ln in enumerate(lines, 1):
            parts = [x.strip() for x in re.split(r"\s*(?:>|\||;|\t)\s*", ln.strip()) if x.strip()]
            if len(parts) < 2 or len(parts) > 3:
                res["errors"].append({"line": i, "text": ln.strip()[:80], "message": "Format harus Regional > Cluster > Area"})
                continue
            try:
                g = _wil_name(parts[0], "Regional"); c = _wil_name(parts[1], "Cluster")
                a = _wil_name(parts[2], "Area") if len(parts) == 3 else None
                changed = False
                gr = cur.execute("SELECT id FROM wil_regions WHERE name = ? COLLATE NOCASE", (g,)).fetchone()
                if not gr:
                    cur.execute("INSERT INTO wil_regions (name) VALUES (?)", (g,)); gid = cur.lastrowid
                    res["regions_created"] += 1; changed = True
                else:
                    gid = gr["id"]
                cr = cur.execute("SELECT id, region_id FROM wil_clusters WHERE name = ? COLLATE NOCASE", (c,)).fetchone()
                if not cr:
                    cur.execute("INSERT INTO wil_clusters (region_id, name) VALUES (?, ?)", (gid, c)); cid = cur.lastrowid
                    res["clusters_created"] += 1; changed = True
                else:
                    cid = cr["id"]
                    if cr["region_id"] != gid:
                        cur.execute("UPDATE wil_clusters SET region_id = ? WHERE id = ?", (gid, cid))
                        res["clusters_moved"] += 1; changed = True
                if a:
                    ar = cur.execute("SELECT a.id, a.cluster_id, a.name, c.name cn FROM wil_areas a JOIN wil_clusters c ON c.id = a.cluster_id "
                                     "WHERE a.name = ? COLLATE NOCASE", (a,)).fetchone()
                    cname = cur.execute("SELECT name FROM wil_clusters WHERE id = ?", (cid,)).fetchone()["name"]
                    if not ar:
                        cur.execute("INSERT INTO wil_areas (cluster_id, name) VALUES (?, ?)", (cid, a))
                        res["areas_created"] += 1; changed = True
                    elif ar["cluster_id"] != cid:
                        cur.execute("UPDATE wil_areas SET cluster_id = ? WHERE id = ?", (cid, ar["id"]))
                        res["assets_updated"] += _wil_cascade_area(cur, ar["cn"], ar["name"], cname, ar["name"])
                        res["areas_moved"] += 1; changed = True
                if not changed:
                    res["unchanged"] += 1
            except HTTPException as exc:
                res["errors"].append({"line": i, "text": ln.strip()[:80], "message": exc.detail})
        if req.dry_run:
            cur.execute("ROLLBACK TO wil_bulk")
        else:
            _audit(cur, "IMPORT", "WILAYAH", None, "daftar wilayah",
                   f"Tempel daftar Wilayah: {res['regions_created']} regional, {res['clusters_created']} cluster, "
                   f"{res['areas_created']} area baru; {res['clusters_moved']} cluster & {res['areas_moved']} area dipindah")
        cur.execute("RELEASE wil_bulk")
    res["dry_run"] = bool(req.dry_run)
    res["message"] = ("Pratinjau: " if req.dry_run else "Tersimpan: ") + (
        f"{res['regions_created']} regional, {res['clusters_created']} cluster, {res['areas_created']} area baru; "
        f"{res['clusters_moved']} cluster & {res['areas_moved']} area dipindah; {len(res['errors'])} baris bermasalah")
    return res


# 8. UPDATE NODE (PUT)
@app.put("/api/nodes/{node_id}")
def update_node(node_id: int, payload: NodeUpdate):
    data = _fields_set(payload)
    _check_status(data.get("status"))
    if data.get("parent_node_id") == node_id:
        raise HTTPException(status_code=400, detail="Node tidak boleh menjadi parent dirinya sendiri")
    if "name" in data and data["name"] is not None:
        data["name"] = _norm_name(data["name"])
        if not data["name"]:
            raise HTTPException(status_code=400, detail="Nama aset wajib diisi")
        if len(data["name"]) > 120:
            raise HTTPException(status_code=400, detail="Nama aset maksimal 120 karakter")

    with db() as conn:
        cursor = conn.cursor()
        if not _exists(cursor, "nodes", node_id):
            raise HTTPException(status_code=404, detail="Node tidak ditemukan")
        cur0 = cursor.execute("SELECT name, type FROM nodes WHERE id = ?", (node_id,)).fetchone()
        _wil_check_update(cursor, "nodes", node_id, data)
        eff_type = data.get("type") or cur0["type"]
        if data.get("name") is not None:
            _check_name_unique_node(cursor, data["name"], eff_type, node_id)
        asset_keys = [k for k in NODE_ASSET_FIELDS if k in data]
        if asset_keys:
            data.update(_clean_asset_fields(cursor, eff_type, {k: data[k] for k in asset_keys}, node_id))
        if data.get("type") and data["type"].upper() != (cur0["type"] or "").upper():
            # tipe berubah: kosongkan parameter yang tidak berlaku lagi untuk tipe baru
            nt = data["type"].upper()
            for k in NODE_ASSET_FIELDS:
                if k in data:
                    continue
                if (k == "reg_code" and nt in NO_REG_TYPES) or (k in CUSTOMER_FIELDS and nt != "PELANGGAN") or (k in TRUNK_FIELDS and nt != "POP"):
                    data[k] = None
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
            ["parent_node_id", "upstream_cable_id", *NODE_ASSET_FIELDS],
        )
        if sets:
            cursor.execute(f"UPDATE nodes SET {', '.join(sets)} WHERE id = ?", (*values, node_id))
        snapped = 0
        if "latitude" in data or "longitude" in data:
            snapped = _snap_cables_to_node(cursor, node_id)
        new_node = _row_dict(cursor.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone())
        changes = _diff_rows(cursor, old_node, new_node,
                             ["name", "type", "status", "latitude", "longitude", "cluster", "area", "city",
                              "capacity", "spec_data", "parent_node_id", "upstream_cable_id", *NODE_ASSET_FIELDS])
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
        _wil_check_update(cursor, "cables", cable_id, data)
        if data.get("name") is not None:
            data["name"] = _norm_name(data["name"])
            if not data["name"]:
                raise HTTPException(status_code=400, detail="Nama kabel wajib diisi")
            _check_name_unique_cable(cursor, data["name"], cable_id)
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


# 11a. HAPUS MASSAL (Inventory: pilih banyak baris). Tiap aset dihapus lewat jalur yang sama dengan hapus satuan
# (audit + snapshot pemulihan); yang gagal dilaporkan satu per satu, yang lain tetap diproses.
BULK_DELETE_MAX = 200


class BulkDelete(BaseModel):
    nodes: List[int] = Field(default_factory=list)
    cables: List[int] = Field(default_factory=list)


@app.post("/api/assets/bulk-delete")
def bulk_delete_assets(payload: BulkDelete):
    nodes = list(dict.fromkeys(payload.nodes or []))
    cables = list(dict.fromkeys(payload.cables or []))
    if not nodes and not cables:
        raise HTTPException(status_code=400, detail="Tidak ada aset yang dipilih")
    if len(nodes) + len(cables) > BULK_DELETE_MAX:
        raise HTTPException(status_code=400, detail=f"Maksimal {BULK_DELETE_MAX} aset per permintaan")
    deleted, failed = {"nodes": 0, "cables": 0}, []
    for kind, ids, fn in (("cables", cables, delete_cable), ("nodes", nodes, delete_node)):
        for i in ids:
            try:
                fn(i)
                deleted[kind] += 1
            except HTTPException as e:
                failed.append({"kind": "CABLE" if kind == "cables" else "NODE", "id": i, "detail": str(e.detail)})
            except Exception as e:  # satu aset bermasalah tidak boleh membatalkan sisanya
                failed.append({"kind": "CABLE" if kind == "cables" else "NODE", "id": i, "detail": str(e)})
    return {"deleted": deleted, "deleted_total": deleted["nodes"] + deleted["cables"], "failed": failed}


# 11a. FOLDER ASET (pohon bersubfolder seperti Google Earth)
FOLDER_MAX_DEPTH = 8
FOLDER_NAME_MAX = 60
MOVE_MAX = 5000


def _like_escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _norm_folder(path, allow_empty=False) -> str:
    """Rapikan jalur folder: 'A / B//C' -> 'A/B/C'. Kosong hanya boleh bila allow_empty."""
    parts = []
    for part in str(path or "").replace("\\", "/").split("/"):
        part = re.sub(r"[\x00-\x1f]", "", part).strip()
        if part:
            parts.append(part[:FOLDER_NAME_MAX])
    if not parts:
        if allow_empty:
            return ""
        raise HTTPException(status_code=400, detail="Nama folder wajib diisi")
    if len(parts) > FOLDER_MAX_DEPTH:
        raise HTTPException(status_code=400, detail=f"Subfolder maksimal {FOLDER_MAX_DEPTH} tingkat")
    return "/".join(parts)


class FolderPath(BaseModel):
    path: str


class FolderRename(BaseModel):
    path: str
    new_path: str


class AssetMove(BaseModel):
    nodes: List[int] = Field(default_factory=list)
    cables: List[int] = Field(default_factory=list)
    folder: str


def _folder_rows(conn):
    cnt = {}
    for tbl, key in (("nodes", "nodes"), ("cables", "cables")):
        for r in conn.execute(f"SELECT folder_path AS p, COUNT(*) AS n FROM {tbl} "
                              f"WHERE folder_path IS NOT NULL AND folder_path != '' GROUP BY folder_path").fetchall():
            cnt.setdefault(r["p"], {"nodes": 0, "cables": 0})[key] = r["n"]
    explicit = {r["path"] for r in conn.execute("SELECT path FROM asset_folders").fetchall()}
    for p in explicit:
        cnt.setdefault(p, {"nodes": 0, "cables": 0})
    return [{"path": p, "nodes": v["nodes"], "cables": v["cables"], "explicit": p in explicit}
            for p, v in sorted(cnt.items(), key=lambda kv: kv[0].lower())]


@app.get("/api/folders")
def list_folders():
    """Semua jalur folder yang dipakai aset (+ folder kosong buatan pengguna) beserta jumlah isinya."""
    with db() as conn:
        return {"folders": _folder_rows(conn)}


@app.post("/api/folders")
def create_folder(payload: FolderPath):
    path = _norm_folder(payload.path)
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO asset_folders (path) VALUES (?)", (path,))
        _audit(conn.cursor(), "CREATE", "FOLDER", None, path, f"Buat folder {path}")
    return {"message": "Folder dibuat", "path": path}


@app.post("/api/folders/rename")
def rename_folder(payload: FolderRename):
    """Ganti nama / pindahkan folder beserta seluruh subfolder dan asetnya."""
    old, new = _norm_folder(payload.path), _norm_folder(payload.new_path)
    if old == new:
        return {"message": "Tidak ada perubahan", "moved": 0}
    if new == old or new.startswith(old + "/"):
        raise HTTPException(status_code=400, detail="Folder tidak bisa dipindahkan ke dalam dirinya sendiri")
    like = _like_escape(old) + "/%"
    moved = 0
    with db() as conn:
        cur = conn.cursor()
        for tbl in ("nodes", "cables"):
            for r in cur.execute(f"SELECT id, folder_path FROM {tbl} WHERE folder_path = ? OR folder_path LIKE ? ESCAPE '\\'",
                                 (old, like)).fetchall():
                np_ = new + r["folder_path"][len(old):]
                if len([x for x in np_.split("/") if x]) > FOLDER_MAX_DEPTH:
                    raise HTTPException(status_code=400, detail=f"Subfolder maksimal {FOLDER_MAX_DEPTH} tingkat")
                cur.execute(f"UPDATE {tbl} SET folder_path = ? WHERE id = ?", (np_, r["id"]))
                moved += 1
        for r in cur.execute("SELECT path FROM asset_folders WHERE path = ? OR path LIKE ? ESCAPE '\\'", (old, like)).fetchall():
            cur.execute("DELETE FROM asset_folders WHERE path = ?", (r["path"],))
            cur.execute("INSERT OR IGNORE INTO asset_folders (path) VALUES (?)", (new + r["path"][len(old):],))
        _audit(cur, "UPDATE", "FOLDER", None, old, f"Folder '{old}' -> '{new}' ({moved} aset)")
    return {"message": "Folder diperbarui", "moved": moved, "path": new}


@app.delete("/api/folders")
def delete_folder(path: str):
    """Hapus folder KOSONG (tanpa aset di folder maupun subfoldernya). Aset tidak pernah ikut terhapus."""
    p = _norm_folder(path)
    like = _like_escape(p) + "/%"
    with db() as conn:
        n = sum(conn.execute(f"SELECT COUNT(*) FROM {t} WHERE folder_path = ? OR folder_path LIKE ? ESCAPE '\\'",
                             (p, like)).fetchone()[0] for t in ("nodes", "cables"))
        if n:
            raise HTTPException(status_code=400, detail=f"Folder masih berisi {n} aset; pindahkan dulu asetnya")
        conn.execute("DELETE FROM asset_folders WHERE path = ? OR path LIKE ? ESCAPE '\\'", (p, like))
        _audit(conn.cursor(), "DELETE", "FOLDER", None, p, f"Hapus folder kosong {p}")
    return {"message": "Folder dihapus"}


@app.post("/api/assets/move")
def move_assets(payload: AssetMove):
    folder = _norm_folder(payload.folder)
    nodes = list(dict.fromkeys(payload.nodes or []))
    cables = list(dict.fromkeys(payload.cables or []))
    if not nodes and not cables:
        raise HTTPException(status_code=400, detail="Tidak ada aset yang dipilih")
    if len(nodes) + len(cables) > MOVE_MAX:
        raise HTTPException(status_code=400, detail=f"Maksimal {MOVE_MAX} aset per permintaan")
    moved = {"nodes": 0, "cables": 0}
    with db() as conn:
        cur = conn.cursor()
        for tbl, ids in (("nodes", nodes), ("cables", cables)):
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                q = ",".join("?" * len(chunk))
                cur.execute(f"UPDATE {tbl} SET folder_path = ? WHERE id IN ({q})", (folder, *chunk))
                moved[tbl] += cur.rowcount
        cur.execute("INSERT OR IGNORE INTO asset_folders (path) VALUES (?)", (folder,))
        _audit(cur, "UPDATE", "FOLDER", None, folder,
               f"Pindahkan {moved['nodes']} node & {moved['cables']} kabel ke folder '{folder}'")
    return {"message": "Aset dipindahkan", "moved": moved, "moved_total": moved["nodes"] + moved["cables"], "folder": folder}


# 11a. UBAH MASSAL ASET (status, cluster/area, kota, pemasangan kabel, folder)
BULK_UPDATE_MAX = 200


class AssetBulkUpdate(BaseModel):
    nodes: List[int] = Field(default_factory=list)
    cables: List[int] = Field(default_factory=list)
    status: Optional[str] = None
    cluster: Optional[str] = None
    area: Optional[str] = None
    city: Optional[str] = None
    installation: Optional[str] = None
    folder: Optional[str] = None
    cable_type: Optional[str] = None        # jenis kabel: Backbone | Feeder | Distribution | Drop (hanya kabel)
    cable_capacity: Optional[str] = None    # kapasitas kabel, mis. 12C (hanya kabel)
    fiber_mode: Optional[str] = None        # SM | MM (hanya kabel)
    node_type: Optional[str] = None         # jenis titik: POP | CLOSURE | ODP | HH | TIANG | SLACK | PELANGGAN (hanya titik)
    node_capacity: Optional[str] = None     # kapasitas titik, mis. "1 In - 8 Out" atau "24 Core" (hanya titik)


BULK_NODE_TYPES = ("POP", "CLOSURE", "ODP", "HH", "TIANG", "SLACK", "PELANGGAN")


@app.post("/api/assets/bulk-update")
def bulk_update_assets(payload: AssetBulkUpdate):
    """Ubah beberapa aset sekaligus. Hanya kolom yang diisi yang diubah; tiap aset memakai aturan yang sama dengan ubah satuan
    (daftar Wilayah, status, riwayat). Aset yang gagal dilewati dan dilaporkan; yang lain tetap berubah. Maks 200 aset/permintaan."""
    nodes = list(dict.fromkeys(payload.nodes or []))
    cables = list(dict.fromkeys(payload.cables or []))
    if not nodes and not cables:
        raise HTTPException(status_code=400, detail="Tidak ada aset yang dipilih")
    if len(nodes) + len(cables) > BULK_UPDATE_MAX:
        raise HTTPException(status_code=400, detail=f"Maksimal {BULK_UPDATE_MAX} aset per permintaan")
    status = (payload.status or "").strip() or None
    cluster = (payload.cluster or "").strip() or None
    area = (payload.area or "").strip() or None
    city = (payload.city or "").strip() or None
    inst = (payload.installation or "").strip() or None
    folder_raw = (payload.folder or "").strip()
    cable_type = (payload.cable_type or "").strip() or None
    cable_cap = (payload.cable_capacity or "").strip().upper() or None
    fmode = (payload.fiber_mode or "").strip().upper() or None
    node_type = (payload.node_type or "").strip().upper() or None
    node_cap = " ".join((payload.node_capacity or "").split()) or None
    if cable_type is not None and cable_type not in CABLE_TYPES:
        raise HTTPException(status_code=400, detail=f"Jenis kabel tidak valid. Pilihan: {', '.join(sorted(CABLE_TYPES))}")
    if cable_cap is not None and not re.match(r"^\d{1,3}C$", cable_cap):
        raise HTTPException(status_code=400, detail="Kapasitas kabel harus berformat seperti 12C")
    _check_fiber_mode(fmode)
    if node_type is not None and node_type not in BULK_NODE_TYPES:
        raise HTTPException(status_code=400, detail=f"Jenis titik tidak valid. Pilihan: {', '.join(BULK_NODE_TYPES)}")
    if node_cap is not None and len(node_cap) > 60:
        raise HTTPException(status_code=400, detail="Kapasitas titik maksimal 60 karakter")
    _check_status(status)
    if inst is not None and inst not in INSTALLATIONS:
        raise HTTPException(status_code=400, detail=f"Pemasangan tidak valid. Pilihan: {', '.join(sorted(INSTALLATIONS))}")
    if cluster and not area:
        raise HTTPException(status_code=400, detail="Pilih Area untuk cluster yang dipilih")
    if city and len(city) > 120:
        raise HTTPException(status_code=400, detail="Kota maksimal 120 karakter")
    folder = _norm_folder(folder_raw, allow_empty=True) if folder_raw else ""
    if not any((status, area, city, inst, folder, cable_type, cable_cap, fmode, node_type, node_cap)):
        raise HTTPException(status_code=400, detail="Tidak ada perubahan yang diisi")
    if area:
        with db() as conn:
            cluster, area = _wil_resolve(conn.cursor(), cluster, area)   # cek sekali di depan; hanya area = cluster diturunkan
    ok_ids = {"NODE": [], "CABLE": []}
    failed, ignored_inst, not_applicable = [], 0, 0
    for kind, ids in (("NODE", nodes), ("CABLE", cables)):
        table = _table_of(kind)
        for aid in ids:
            with db() as conn:
                row = conn.execute(f"SELECT name FROM {table} WHERE id = ?", (aid,)).fetchone()
            if not row:
                failed.append({"kind": kind, "id": aid, "name": f"#{aid}", "reason": "Aset tidak ditemukan"})
                continue
            try:
                data = {}
                if area:
                    data["cluster"], data["area"] = cluster, area
                if city:
                    data["city"] = city
                if kind == "CABLE" and inst:
                    data["installation"] = inst
                if kind == "CABLE":
                    if cable_type:
                        data["type"] = cable_type
                    if cable_cap:
                        data["capacity"] = cable_cap
                    if fmode:
                        data["fiber_mode"] = fmode
                else:
                    if node_type:
                        data["type"] = node_type
                    if node_cap:
                        data["capacity"] = node_cap
                if inst and kind == "NODE":
                    ignored_inst += 1
                if not data and not status and not folder and not (inst and kind == "NODE"):
                    not_applicable += 1          # kolom yang diisi tidak berlaku untuk jenis aset ini (mis. jenis kabel pada titik)
                    continue
                if data:
                    if kind == "NODE":
                        update_node(aid, NodeUpdate(**data))
                    else:
                        update_cable(aid, CableUpdate(**data))
                if status:
                    _set_asset_status(kind, aid, status)
                ok_ids[kind].append(aid)
            except HTTPException as e:
                failed.append({"kind": kind, "id": aid, "name": row["name"], "reason": str(e.detail)})
    moved = None
    if folder and (ok_ids["NODE"] or ok_ids["CABLE"]):
        moved = move_assets(AssetMove(nodes=ok_ids["NODE"], cables=ok_ids["CABLE"], folder=folder))["moved_total"]
    updated = len(ok_ids["NODE"]) + len(ok_ids["CABLE"])
    parts = [f"{k}={v}" for k, v in (("status", status), ("cluster", cluster), ("area", area), ("kota", city),
                                     ("pemasangan", inst), ("folder", folder), ("jenis kabel", cable_type), ("kapasitas kabel", cable_cap),
                                     ("serat", fmode), ("jenis titik", node_type), ("kapasitas titik", node_cap)) if v]
    with db() as conn:
        _audit(conn.cursor(), "UPDATE", "BULK", None, "ubah massal",
               f"Ubah massal {updated} aset ({len(ok_ids['NODE'])} node, {len(ok_ids['CABLE'])} kabel): {', '.join(parts)}"
               + (f"; {len(failed)} gagal" if failed else ""))
    return {"message": f"{updated} aset diubah" + (f", {len(failed)} dilewati" if failed else ""), "updated": updated,
            "nodes": len(ok_ids["NODE"]), "cables": len(ok_ids["CABLE"]), "failed": failed[:50], "failed_total": len(failed),
            "installation_ignored_nodes": ignored_inst, "not_applicable": not_applicable, "folder_moved": moved}


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
            if a_type == "NODE" and (row["type"] or "").upper() == "POP":
                # OTB diperlakukan seperti closure: tiap port menerima 1 kabel MASUK (hulu/feeder) dan 1 kabel KELUAR
                # (hilir/distribusi); sisi depan (perangkat/OLT) dicatat terpisah dan tidak memblokir sambungan kabel
                ins, outs = _joint_dirs(cursor, a_id)
                if side == "asal" and port in outs:
                    raise HTTPException(status_code=409,
                                        detail=f"Port '{port}' pada {row['name']} sudah meneruskan ke kabel keluar lain")
                if side == "tujuan" and port in ins:
                    raise HTTPException(status_code=409,
                                        detail=f"Port '{port}' pada {row['name']} sudah menerima kabel masuk lain")
                continue
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
    if n < 1 or (n > _core_total(cab["capacity"]) and not req.full):
        res["reason"] = f"Jumlah core harus 1-{_core_total(cab['capacity'])} (sesuai kapasitas kabel)"
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
    fmap = {}
    if req.from_ports and chosen and len(chosen) == len(req.from_ports):
        # pengguna memilih sendiri port/joint hulu
        if len(chosen) != n or len(set(chosen)) != n:
            res["reason"] = "Jumlah port hulu harus sama dengan jumlah core"
            return res
        if tu in JUNCTION_TYPES:
            ins_u2, outs_u2 = _joint_dirs(cursor, up["id"])
            okp = {p for p in ins_u2 if p not in outs_u2}
            need_feed = [p for p in chosen if p not in okp]
            if need_feed:
                # core hulu yang dipilih belum tiba di joint: ambil dari kabel hulu (core persis yang dipilih)
                fsrcs, fwhy = _acquire_feed(cursor, up, len(need_feed), exclude, 0, feed_log, prefer=need_feed)
                if not fsrcs:
                    res["reason"] = fwhy or f"Core hulu pilihan tidak tersedia pada {up['name']}"
                    return res
                fmap = {x[2]: x for x in fsrcs}
                okp |= set(need_feed)
        else:
            lab = _port_labels("NODE", tu, up["capacity"])
            lab = [x for x in lab if x.startswith("OUT-")] if tu == "ODP" else lab
            usedu = _used_ports(cursor, "NODE", up["id"])
            okp = {x for x in lab if x not in usedu}
        if any(p not in okp for p in chosen):
            res["reason"] = f"Port/joint hulu pilihan tidak tersedia pada {up['name']}"
            return res
        srcs = [fmap[p] if (tu in JUNCTION_TYPES and p in fmap) else ("NODE", up["id"], p) for p in chosen]
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
        if n < 1 or (n > _core_total(req.capacity or "12C") and not req.full):
            out["reason"] = f"Jumlah core harus 1-{_core_total(req.capacity or '12C')} (sesuai kapasitas kabel)"; return out
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
            # kabel hulu yang bisa memasok core ke joint ini (pengguna boleh memilih core-nya sendiri)
            feed_cables = []
            all_cables = cur.execute("SELECT * FROM cables").fetchall()
            at_up = [a2 for a2 in _cables_at_node(cur, up, all_cables) if a2["at_end"]]
            for a2 in at_up:
                c = a2["row"]
                freec = [x for x in _core_labels(_core_total(c["capacity"]))
                         if x not in _used_ports(cur, "CABLE", c["id"]) and x not in ins_u]
                if not freec:
                    continue
                others = {b2["row"]["id"] for b2 in at_up if b2["row"]["id"] != c["id"]}
                try:
                    cur.execute("SAVEPOINT feedcab")
                    fs, _w = _acquire_feed(cur, up, 1, others, 0, [], prefer=[freec[0]])
                    cur.execute("ROLLBACK TO feedcab")
                    cur.execute("RELEASE feedcab")
                except Exception:
                    fs = None
                if fs:
                    feed_cables.append({"id": c["id"], "name": c["name"], "capacity": c["capacity"], "cores": freec})
            out["feed_cables"] = feed_cables
            if len(ready) >= n:
                out["defaults"]["from_ports"] = ready[:n]
            elif srcs and feed_cables:
                fill = feed_cables[0]["cores"][: n - len(ready)]
                out["defaults"]["from_ports"] = (ready + fill) if len(ready) + len(fill) >= n else ["AUTO"] * n
            else:
                out["defaults"]["from_ports"] = ["AUTO"] * n if srcs else []
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
    return key[0] == "NODE" and (names.get(key, (None, None))[1] or "").upper() in (JUNCTION_TYPES | {"POP"})


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
    slot: Optional[str] = None


SLOT_RE = re.compile(r"^[A-Za-z0-9/:@._-]{1,40}$")


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
    slot = re.sub(r"\s+", "", str(payload.slot or "")) or None
    if slot and not SLOT_RE.match(slot):
        raise HTTPException(status_code=400, detail="Slot/PON hanya huruf, angka dan / : @ . _ - (maks 40 karakter), contoh 1/1 atau 1/1/3")
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
                   vlan, service, customer_name, customer_node_id, notes, updated_by, updated_at, slot)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (node_id, port, purpose, dtype, vendor, name, iface, vlan, service, cust_name, cust_id, notes,
             (u["username"] if u else None) or "system", _now_str(), slot))
        _audit(cursor, "UPDATE" if before else "CREATE", "NODE", node_id, n["name"],
               f"{'Ubah' if before else 'Catat'} perangkat {name} [{(slot + ' ') if slot else ''}{iface}] pada {n['name']} {port} ({purpose}"
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


# --- PEMERIKSAAN KEUNIKAN (dipakai form untuk umpan balik langsung) & KODE REGISTRASI OTOMATIS ---
@app.get("/api/nodes/check-unique")
def check_unique(name: str = "", reg_code: str = "", device_sn: str = "", exclude_id: Optional[int] = None,
                 kind: str = "NODE", type: str = ""):
    """Cek nama / kode registrasi / SN. Hasil: {field: {value (ternormalisasi), ok, conflict}}. Huruf besar/kecil diabaikan."""
    out = {}
    with db() as conn:
        c = conn.cursor()
        if name.strip():
            d = _find_cable_dup(c, name, exclude_id) if kind.upper() == "CABLE" else (
                None if type.upper() == "INCIDENT" else _find_node_dup(c, "name", name, exclude_id))
            out["name"] = {"value": _norm_name(name), "ok": d is None,
                           "conflict": None if d is None else {"id": d["id"], "name": d["name"], "type": d["type"]}}
        for col, val in (("reg_code", reg_code), ("device_sn", device_sn)):
            code = _norm_code(val)
            if code:
                d = _find_node_dup(c, col, code, exclude_id)
                rx = REG_CODE_RE if col == "reg_code" else SN_RE
                out[col] = {"value": code, "ok": d is None and bool(rx.match(code)), "valid_format": bool(rx.match(code)),
                            "conflict": None if d is None else {"id": d["id"], "name": d["name"], "type": d["type"]}}
    return out


@app.get("/api/nodes/next-reg-code")
def next_reg_code(type: str = "ODP"):
    t = type.upper()
    if t in NO_REG_TYPES:
        raise HTTPException(status_code=400, detail=f"Aset bertipe {t} tidak memakai kode registrasi")
    with db() as conn:
        return {"reg_code": _gen_reg_code(conn.cursor(), t)}


# --- KAPASITAS TRUNK POP: beban = jumlah BW pelanggan aktif yang terhubung ke bawah POP ---
def _trunk_status(pct, trunk):
    if not trunk:
        return "unset"
    if pct > 100:
        return "over"
    return "warn" if pct >= 80 else "ok"


def _upstream_pops(names, idx, start) -> list:
    """Id POP yang menjadi hulu sebuah aset (menelusuri sambungan ke arah POP)."""
    if names.get(start, (None, None))[1] == "POP":
        return [start[1]]
    seen, pops, queue = set(), [], deque(idx[1].get(start, []))
    while queue:
        c = queue.popleft()
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        for t, i in ((c["from_asset_type"], c["from_asset_id"]), (c["to_asset_type"], c["to_asset_id"])):
            if t == "NODE" and names.get((t, i), (None, None))[1] == "POP" and i not in pops:
                pops.append(i)
        queue.extend(_next_up(names, idx, c))
    return pops


@app.get("/api/nodes/{node_id}/trunk")
def pop_trunk(node_id: int):
    with db() as conn:
        cursor = conn.cursor()
        n = cursor.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if not n:
            raise HTTPException(status_code=404, detail="Node tidak ditemukan")
        if (n["type"] or "").upper() != "POP":
            raise HTTPException(status_code=400, detail="Kapasitas trunk hanya untuk aset POP")
        names, conns = _trace_graph(cursor)
        idx = _hop_index(conns)
        return _trunk_info(cursor, n, names, idx)


def _trunk_info(cursor, n, names, idx) -> dict:
    node_id = n["id"]
    reach = set()
    for c in idx[0].get(("NODE", node_id), []):
        reach |= _customers_after(names, idx, c)
    ids = [k[1] for k in reach]
    rows = []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        rows += cursor.execute(
            f"SELECT id, name, status, service, bandwidth_mbps, link_type FROM nodes WHERE id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
    trunk = n["trunk_mbps"]
    ob = n["trunk_overbook"] or 1.0
    delivered = [r for r in rows if r["status"] == "Active"]
    inactive = [r for r in rows if r["status"] != "Active"]
    total_bw = round(sum(r["bandwidth_mbps"] or 0 for r in delivered), 3)
    effective = round(total_bw / ob, 3)
    pct = round(effective / trunk * 100, 1) if trunk else 0.0
    by_type, by_service = {}, {}
    for r in delivered:
        lt = r["link_type"] or "-"
        a = by_type.setdefault(lt, {"count": 0, "mbps": 0.0}); a["count"] += 1; a["mbps"] = round(a["mbps"] + (r["bandwidth_mbps"] or 0), 3)
        sv = r["service"] or "-"
        b = by_service.setdefault(sv, {"count": 0, "mbps": 0.0}); b["count"] += 1; b["mbps"] = round(b["mbps"] + (r["bandwidth_mbps"] or 0), 3)
    top = sorted(delivered, key=lambda r: -(r["bandwidth_mbps"] or 0))[:5]
    return {
        "node_id": node_id, "node_name": n["name"], "trunk_mbps": trunk, "overbook": ob,
        "customers_total": len(rows), "delivered_count": len(delivered), "inactive_count": len(inactive),
        "missing_bw_count": sum(1 for r in delivered if not r["bandwidth_mbps"]),
        "delivered_mbps": total_bw, "effective_mbps": effective,
        "free_mbps": round(trunk - effective, 3) if trunk else None, "util_pct": pct, "status": _trunk_status(pct, trunk),
        "by_type": by_type, "by_service": by_service,
        "top": [{"id": r["id"], "name": r["name"], "mbps": r["bandwidth_mbps"], "link_type": r["link_type"], "service": r["service"]} for r in top],
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
def get_incidents(cluster: str = "ALL", area: str = "ALL", region: str = "ALL"):
    sc, sp = _scope_conds(cluster, area, "i", region)
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


class ImpactPreview(BaseModel):
    linked_cable_id: Optional[int] = None
    linked_node_id: Optional[int] = None
    affected_cores: Optional[List[int]] = None   # nomor core (1..N); kosong = seluruh kabel


def _active_core_faults(cursor, cable_ids=None):
    """Peta kabel -> {'all': [tiket gangguan seluruh kabel], 'cores': {label: [tiket]}} dari tiket aktif."""
    out = {}
    q = ("SELECT ticket_number, linked_cable_id, affected_cores FROM incidents "
         "WHERE linked_cable_id IS NOT NULL AND status IN ('Open','In Progress')")
    for r in cursor.execute(q).fetchall():
        cid = r["linked_cable_id"]
        if cable_ids is not None and cid not in cable_ids:
            continue
        e = out.setdefault(cid, {"all": [], "cores": {}})
        labels = None
        try:
            labels = json.loads(r["affected_cores"]) if r["affected_cores"] else None
        except ValueError:
            labels = None
        if labels:
            for lbl in labels:
                e["cores"].setdefault(lbl, []).append(r["ticket_number"])
        else:
            e["all"].append(r["ticket_number"])
    return out


@app.get("/api/cables/{cable_id}/core-routes")
def get_cable_core_routes(cable_id: int):
    """Tiap core kabel: terpakai atau tidak, menuju ODP/pelanggan mana (ikuti sirkuit), dan tiket aktif yang menimpanya."""
    with db() as conn:
        cur = conn.cursor()
        cab = cur.execute("SELECT * FROM cables WHERE id = ?", (cable_id,)).fetchone()
        if not cab:
            raise HTTPException(status_code=404, detail="Kabel tidak ditemukan")
        names, conns = _trace_graph(cur)
        idx = _hop_index(conns)
        faults = _active_core_faults(cur, {cable_id}).get(cable_id, {"all": [], "cores": {}})
        res = []
        for i, lbl in enumerate(_core_labels(_core_total(cab["capacity"])), start=1):
            starts = [c for c in conns if c.get("via_cable_id") == cable_id and c.get("via_core") == lbl]
            odps, ends, cust, seen = [], [], 0, set()
            queue = deque(starts)
            while queue:
                h = queue.popleft()
                if h["id"] in seen:
                    continue
                seen.add(h["id"])
                k = (h["to_asset_type"], h["to_asset_id"])
                nm, tp = names.get(k, (None, None))
                tpu = (tp or "").upper()
                if k[0] == "NODE":
                    if tpu == "ODP" and nm not in odps:
                        odps.append(nm)
                    elif tpu == "PELANGGAN":
                        cust += 1
                nxt = _next_down(names, idx, h)
                if not nxt and k[0] == "NODE" and tpu not in ("PELANGGAN",) and nm not in ends:
                    ends.append(nm)
                queue.extend(nxt)
            tickets = list(faults["all"]) + list(faults["cores"].get(lbl, []))
            res.append({"number": i, "core": lbl, "used": bool(starts),
                        "odps": odps, "ends": ends[:4], "customers": cust,
                        "down": bool(tickets), "tickets": sorted(set(tickets))})
    return {"cable_id": cable_id, "name": cab["name"], "total": len(res),
            "used": sum(1 for r in res if r["used"]), "cores": res}


@app.post("/api/incidents/preview-impact")
def preview_incident_impact(payload: ImpactPreview):
    """Pratinjau dampak SEBELUM tiket disimpan (tanpa menulis apa pun)."""
    with db() as conn:
        cur = conn.cursor()
        labels = None
        if payload.affected_cores:
            if payload.linked_cable_id is None:
                raise HTTPException(status_code=400, detail="Core terdampak hanya berlaku untuk insiden pada kabel")
            cab = cur.execute("SELECT capacity FROM cables WHERE id = ?", (payload.linked_cable_id,)).fetchone()
            if not cab:
                raise HTTPException(status_code=404, detail="Kabel tidak ditemukan")
            valid = _core_labels(_core_total(cab["capacity"]))
            labels = []
            for n in payload.affected_cores:
                if n < 1 or n > len(valid):
                    raise HTTPException(status_code=400, detail=f"Core {n} tidak ada pada kabel (1-{len(valid)})")
                labels.append(valid[n - 1])
        elif payload.linked_cable_id is not None and not cur.execute(
                "SELECT 1 FROM cables WHERE id = ?", (payload.linked_cable_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Kabel tidak ditemukan")
        if payload.linked_node_id is not None and not cur.execute(
                "SELECT 1 FROM nodes WHERE id = ?", (payload.linked_node_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Node tidak ditemukan")
        cables, nodes = _compute_impact(cur, payload.linked_cable_id, payload.linked_node_id, labels)
        out = _describe_assets(cur, nodes, cables)
        out["scope"] = "core" if labels else ("kabel" if payload.linked_cable_id is not None
                                               else ("node" if payload.linked_node_id is not None else "titik"))
        out["cores"] = labels or []
        out["odp_names"] = [n["name"] for n in out["nodes"] if (n["type"] or "").upper() == "ODP"]
    return out


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
                name = _norm_name(payload.name) or _unique_node_name(cursor, f"{prefix}-{inc['ticket_number']}")
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
        if t == "NODE" and (row["type"] or "").upper() == "POP":
            ins_, outs_ = _joint_dirs(cursor, i)
            if (side == "from" and port in outs_) or (side == "to" and port in ins_):
                return f"port {port} pada {row['name']} sudah dipakai sambungan lain"
            continue
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
    if (row.get("type") or "").upper() != "INCIDENT":
        d = _find_node_dup(cursor, "name", row.get("name"))
        if d:
            raise HTTPException(status_code=409, detail=f"Tidak bisa dipulihkan: nama '{row.get('name')}' kini dipakai aset lain ('{d['name']}', ID {d['id']}). Ubah nama aset itu dulu.")
    for col, lab in (("reg_code", "Kode registrasi"), ("device_sn", "SN perangkat")):
        if row.get(col) and _find_node_dup(cursor, col, row[col]):
            raise HTTPException(status_code=409, detail=f"Tidak bisa dipulihkan: {lab} {row[col]} kini dipakai aset lain")
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
BULK_ROUTE_CAP = contextvars.ContextVar("netgis_bulk_route_cap", default=None)   # proses massal boleh lebih banyak rute per menit (jeda antar-panggilan global tetap berlaku)
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
    "drop_new_poles": False,     # False: kabel dropcore (< drop_max_m) tanpa tiang/HH baru (hanya kabel besar yang memakai tiang/HH baru); tiang eksisting tetap dipakai ulang
    "drop_max_m": 1000,          # panjang maksimum dropcore; lebih dari ini perlu kabel distribusi + closure (+ ODP) baru
    "hub_distance_m": 200,       # jarak closure/ODP baru dari pelanggan (mode "sedekat mungkin")
    "otb_cable_capacity": "12C", # kapasitas kabel udara minimum bila diterminasi OTB di lokasi pelanggan
    # jenis kabel menurut panjang rute: dipakai tingkat pertama yang panjangnya < max_m
    "cable_tiers": [
        {"max_m": 1000, "type": "Drop", "label": "Kabel Dropcore", "capacity": "2C"},
        {"max_m": 5000, "type": "Distribution", "label": "Kabel Distribusi", "capacity": "12C"},
        {"max_m": 10000, "type": "Distribution", "label": "Kabel Distribusi", "capacity": "24C"},
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
    if src.get("drop_new_poles") is not None:
        out["drop_new_poles"] = bool(src["drop_new_poles"])
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
        if len(dq) >= (BULK_ROUTE_CAP.get() or ROUTE_USER_PER_MIN):
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


def _detour_coords(coords, factor):
    """Jalur zig-zag halus dari titik awal ke akhir dengan panjang total = garis lurus x factor (perkiraan jalur menurut jalan)."""
    a, b = coords[0], coords[-1]
    d = _haversine_m(a[1], a[0], b[1], b[0])
    if d < 20 or factor <= 1.0005:
        return [list(a), list(b)]
    k = max(1, int(round(d / 200.0)))
    s_ = d / (2 * k)
    amp = s_ * math.sqrt(factor * factor - 1.0)
    mlat = 111320.0
    mlng = 111320.0 * max(0.05, math.cos(math.radians((a[1] + b[1]) / 2)))
    dx, dy = (b[0] - a[0]) * mlng, (b[1] - a[1]) * mlat
    ln = math.hypot(dx, dy) or 1.0
    px, py = -dy / ln, dx / ln
    pts = [[a[0], a[1]]]
    for i in range(1, 2 * k):
        t = i / (2 * k)
        off = 0.0 if i % 2 == 0 else (amp if (i // 2) % 2 == 0 else -amp)
        pts.append([a[0] + (dx * t + px * off) / mlng, a[1] + (dy * t + py * off) / mlat])
    pts.append([b[0], b[1]])
    return pts


PLAN_MAX_BENDS = 30


def _plan_route(points, mode, bends=None):
    """Jalur untuk rencana pasang baru. mode: road (OSRM, jatuh ke garis lurus bila gagal) | straight | custom (titik belokan buatan pengguna)."""
    pts = [(round(float(a), 6), round(float(b), 6)) for a, b in points]
    if mode == "straight":
        return {"coords": _straight_route(pts), "source": "pilihan_lurus", "note": "Mode garis lurus dipilih: jalur tidak mengikuti jalan"}
    if mode == "custom":
        mid = [(round(float(a), 6), round(float(b), 6)) for a, b in (bends or [])]
        full = [pts[0]] + mid + [pts[-1]]
        return {"coords": _straight_route(full), "source": "manual",
                "note": f"Jalur disunting manual ({len(mid)} titik belokan); tidak ditempel ke jalan"}
    return _route_between(pts)


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
    route_mode: Optional[str] = "road"        # road (ikuti jalan) | straight (garis lurus) | custom (jalur disunting manual)
    custom_path: Optional[List[List[float]]] = None   # mode custom: titik belokan di antara asal & tujuan [[lat, lng], ...] (maks 30)
    rules: Optional[dict] = None
    create_customer: Optional[bool] = True
    use_poles: Optional[bool] = True          # False: tanpa tiang/handhole baru (mis. numpang infrastruktur eksisting)
    use_slack: Optional[bool] = True          # False: tanpa slack
    new_odp_origin: Optional[bool] = False    # catuan = Closure, tetapi tarikan < batas dropcore: pasang ODP baru di closure (ODP terdekat tidak layak/penuh)
    customer_cores: Optional[int] = 1         # layanan pelanggan: 1 core, 2 core (Tx-Rx dedicated) atau N core
    termination: Optional[str] = "DROPCORE_ROSET"   # DROPCORE_ROSET | UDARA_OTB (kabel udara + OTB di pelanggan)
    scenario: Optional[str] = "AUTO"          # AUTO (menurut batas dropcore) | DIRECT | HUB (distribusi baru + closure)
    hub_mode: Optional[str] = "NEAREST"       # NEAREST (sedekat mungkin ke pelanggan) | MAP (dipilih di peta)
    hub_lat: Optional[float] = Field(None, ge=-90, le=90)
    hub_lng: Optional[float] = Field(None, ge=-180, le=180)
    splitter: Optional[str] = None            # rasio splitter ODP baru: 1:4, 1:8, 2:8 (hanya layanan 1 core)
    # data layanan pelanggan yang akan dibuat saat rencana diwujudkan (semua opsional)
    cust_service: Optional[str] = None
    cust_bw_mbps: Optional[float] = None
    cust_sn: Optional[str] = None
    cust_link_type: Optional[str] = None
    cust_reg_code: Optional[str] = None
    # port OTB POP asal: kosong = otomatis; berisi = pilihan manual (jumlah = jumlah core layanan)
    pop_ports: Optional[List[str]] = None
    remarks: Optional[str] = Field(None, max_length=1000)   # keterangan dari perencana (tercetak di PDF)
    detour_factor: Optional[float] = Field(None, ge=1.0, le=3.0)   # hanya mode road: bila rute jalan tak tersedia/tak wajar, panjang jalur = garis lurus x faktor


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


def _project_on_polyline(coords, lat, lng, segs=None):
    """Proyeksi titik ke polyline [[lng,lat],...] -> (jarak_sepanjang_jalur_m, jarak_tegak_lurus_m).
    Pendekatan bidang lokal (equirectangular) per ruas; panjang ruas memakai haversine agar konsisten dengan _points_along."""
    if segs is None:
        segs = [_haversine_m(a[1], a[0], b[1], b[0]) for a, b in zip(coords, coords[1:])]
    k = 111320.0
    cl = math.cos(math.radians(lat))
    best, acc = (0.0, float("inf")), 0.0
    for i, (a, b) in enumerate(zip(coords, coords[1:])):
        ax, ay = (a[0] - lng) * k * cl, (a[1] - lat) * k
        bx, by = (b[0] - lng) * k * cl, (b[1] - lat) * k
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / L2))
        px, py = ax + dx * t, ay + dy * t
        off = math.hypot(px, py)
        if off < best[1]:
            best = (acc + t * segs[i], off)
        acc += segs[i]
    return best


def _corridor_assets(coords, cands, radius_m, min_sep_m=5.0):
    """Aset (dict berisi latitude/longitude) yang berada di koridor +-radius_m dari polyline, terurut sepanjang jalur.
    Aset yang berhimpitan (< min_sep_m sepanjang jalur, mis. tiang di dua sisi jalan) dipilih yang terdekat ke jalur."""
    if not coords or len(coords) < 2 or not cands:
        return []
    segs = [_haversine_m(a[1], a[0], b[1], b[0]) for a, b in zip(coords, coords[1:])]
    lats, lngs = [c[1] for c in coords], [c[0] for c in coords]
    mlat = radius_m / 111320.0 + 1e-5
    mlng = mlat / max(0.1, math.cos(math.radians(sum(lats) / len(lats))))
    lo_a, hi_a, lo_o, hi_o = min(lats) - mlat, max(lats) + mlat, min(lngs) - mlng, max(lngs) + mlng
    hits = []
    for e in cands:
        la, ln = e["latitude"], e["longitude"]
        if la is None or ln is None or not (lo_a <= la <= hi_a and lo_o <= ln <= hi_o):
            continue
        along, off = _project_on_polyline(coords, la, ln, segs)
        if off <= radius_m:
            hits.append({"along": along, "offset": off, "node": e})
    hits.sort(key=lambda h: h["along"])
    out = []
    for h in hits:
        if out and h["along"] - out[-1]["along"] < min_sep_m:
            if h["offset"] < out[-1]["offset"]:
                out[-1] = h
            continue
        out.append(h)
    return out


def _segment_assets(rules, inst, coords, use_poles, use_slack, existing, tag, allow_new=True):
    """Tiang/HH + slack sepanjang satu segmen kabel.
    Tiang/HH eksisting dibaca di koridor rute (+- reuse_radius_m, jarak antar tiang apa adanya); tiang/HH baru hanya
    mengisi bentang yang lebih panjang dari jarak maksimal (pole_spacing_m / hh_spacing_m)."""
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
    rr = float(rules["reuse_radius_m"])
    assets = []
    reuse = _corridor_assets(coords, existing.get(passive_kind, []), rr) if (use_poles and rr > 0) else []
    pass_d, prev = [], 0.0       # tiang/HH baru: isi bentang antar penyangga (awal, eksisting..., akhir) bila > jarak maksimal
    for end in [h["along"] for h in reuse] + [length]:
        if not allow_new:                    # dropcore: tanpa tiang/HH baru
            pass
        elif use_poles and not reuse:          # tanpa tiang eksisting: kelipatan jarak tetap dari awal (perilaku lama)
            k = 1
            while prev + k * spacing < end - 2.0:
                pass_d.append(prev + k * spacing)
                k += 1
        elif use_poles:                      # bentang berbatasan dengan tiang eksisting: bagi rata agar tidak ada tiang baru yang menempel
            gap = end - prev
            n_new = max(0, math.ceil((gap - 2.0) / spacing) - 1)
            pass_d += [prev + gap * j / (n_new + 1) for j in range(1, n_new + 1)]
        prev = max(prev, end)
    for h in reuse:
        e = h["node"]
        assets.append({"kind": passive_kind, "distance_m": round(h["along"], 1), "latitude": e["latitude"], "longitude": e["longitude"],
                       "segment": tag, "existing_id": e["id"], "existing_name": e["name"], "offset_m": round(h["offset"], 1)})
    for d, (la, ln) in zip(pass_d, _points_along(coords, pass_d)):
        assets.append({"kind": passive_kind, "distance_m": round(d, 1), "latitude": la, "longitude": ln, "segment": tag,
                       "existing_id": None, "existing_name": None})
    for d, (la, ln) in zip(slack_d, _points_along(coords, slack_d)):
        assets.append({"kind": "SLACK", "distance_m": round(d, 1), "latitude": la, "longitude": ln, "segment": tag,
                       "existing_id": None, "existing_name": None})
    assets.sort(key=lambda a: (a["distance_m"], a["kind"]))
    new_p = sum(1 for a in assets if a["kind"] == passive_kind and not a["existing_id"])
    re_p = sum(1 for a in assets if a["kind"] == passive_kind and a["existing_id"])
    return {"length": length, "assets": assets, "slack_n": slack_n, "slack_len": slack_len, "slack_total": slack_n * slack_len,
            "total_cable": round(length + slack_n * slack_len, 1), "new_passive": new_p, "reuse_passive": re_p, "kind": passive_kind}


def _cable_line(geom_text):
    """Koordinat [[lng,lat],...] dari geojson_geometry kabel (LineString / MultiLineString disambung berurutan)."""
    try:
        g = json.loads(geom_text) if isinstance(geom_text, str) else (geom_text or {})
    except (TypeError, ValueError):
        return []
    c = g.get("coordinates") or []
    if g.get("type") == "MultiLineString":
        c = [p for ln in c for p in ln]
    return [p for p in c if isinstance(p, (list, tuple)) and len(p) >= 2]


def _support_kinds(installation):
    return ("TIANG",) if installation == "Udara" else ("HH",) if installation == "Tanah" else ("TIANG", "HH")


def _support_radius(radius):
    try:
        r = float(radius)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="radius harus berupa angka")
    if not (1 <= r <= 50):
        raise HTTPException(status_code=400, detail="radius harus antara 1 dan 50 meter")
    return r


@app.get("/api/cables/{cable_id}/supports")
def get_cable_supports(cable_id: int, radius: Optional[float] = None):
    """Tiang/HH yang dilewati kabel. Relasi DIHITUNG dari posisi (koridor +-radius dari jalur kabel), bukan disimpan:
    tiang/HH hasil impor, input manual, maupun hasil rencana sama-sama terbaca."""
    with db() as conn:
        cur = conn.cursor()
        cab = cur.execute("SELECT id, name, installation, geojson_geometry FROM cables WHERE id = ?", (cable_id,)).fetchone()
        if not cab:
            raise HTTPException(status_code=404, detail="Kabel tidak ditemukan")
        rules = _plan_rules(cur, None)
        r = _support_radius(radius if radius is not None else rules["reuse_radius_m"])
        coords = _cable_line(cab["geojson_geometry"])
        kinds = _support_kinds(cab["installation"])
        length = _polyline_length_m(coords) if len(coords) > 1 else 0.0
        items = []
        for k in kinds:
            cands = [dict(x) for x in cur.execute(
                "SELECT id, name, type, status, latitude, longitude, capacity FROM nodes WHERE type = ?", (k,)).fetchall()]
            for h in _corridor_assets(coords, cands, r):
                n = h["node"]
                items.append({"id": n["id"], "name": n["name"], "type": n["type"], "status": n["status"], "capacity": n["capacity"],
                              "latitude": n["latitude"], "longitude": n["longitude"],
                              "along_m": round(h["along"], 1), "offset_m": round(h["offset"], 1)})
        items.sort(key=lambda x: x["along_m"])
        spacing = float(rules["pole_spacing_m"] if "TIANG" in kinds and len(kinds) == 1 else rules["hh_spacing_m"] if "HH" in kinds and len(kinds) == 1
                        else rules["pole_spacing_m"])
        pts = [0.0] + [i["along_m"] for i in items] + [round(length, 1)]
        spans = [round(b - a, 1) for a, b in zip(pts, pts[1:])]
        return {"cable_id": cab["id"], "cable_name": cab["name"], "installation": cab["installation"], "kinds": list(kinds),
                "radius_m": r, "length_m": round(length, 1), "count": len(items), "items": items,
                "spacing_rule_m": spacing, "max_span_m": max(spans) if spans else 0.0,
                "spans_over_rule": sum(1 for s in spans if s > spacing + 2.0)}


@app.get("/api/nodes/{node_id}/cables-through")
def get_node_cables_through(node_id: int, radius: Optional[float] = None):
    """Kabel yang menumpang pada sebuah tiang/HH (dihitung dari posisi, koridor +-radius dari jalur kabel)."""
    with db() as conn:
        cur = conn.cursor()
        n = cur.execute("SELECT id, name, type, latitude, longitude FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if not n:
            raise HTTPException(status_code=404, detail="Aset tidak ditemukan")
        if n["type"] not in ("TIANG", "HH"):
            raise HTTPException(status_code=400, detail="Hanya untuk aset bertipe TIANG atau HH")
        r = _support_radius(radius if radius is not None else _plan_rules(cur, None)["reuse_radius_m"])
        items = []
        for c in cur.execute("SELECT id, name, type, status, capacity, installation, geojson_geometry FROM cables").fetchall():
            if n["type"] not in _support_kinds(c["installation"]):
                continue
            coords = _cable_line(c["geojson_geometry"])
            hit = _corridor_assets(coords, [dict(n)], r)
            if hit:
                items.append({"id": c["id"], "name": c["name"], "type": c["type"], "status": c["status"], "capacity": c["capacity"],
                              "installation": c["installation"], "along_m": round(hit[0]["along"], 1),
                              "offset_m": round(hit[0]["offset"], 1), "length_m": round(_polyline_length_m(coords), 1)})
        items.sort(key=lambda x: (x["offset_m"], x["name"]))
        return {"node_id": n["id"], "node_name": n["name"], "type": n["type"], "radius_m": r, "count": len(items), "items": items}


def _plan_trunk_check(cursor, origin, bw_mbps, warnings) -> list:
    """Dampak bandwidth pelanggan baru terhadap trunk POP hulu aset asal (hanya bila BW diisi & asal berupa aset)."""
    if not bw_mbps or origin.get("type") != "NODE" or origin.get("id") is None:
        return []
    names, conns = _trace_graph(cursor)
    idx = _hop_index(conns)
    out = []
    for pid in _upstream_pops(names, idx, ("NODE", origin["id"])):
        n = cursor.execute("SELECT * FROM nodes WHERE id = ?", (pid,)).fetchone()
        if not n:
            continue
        t = _trunk_info(cursor, n, names, idx)
        ob = t["overbook"] or 1.0
        after = round(t["effective_mbps"] + bw_mbps / ob, 3)
        pct_after = round(after / t["trunk_mbps"] * 100, 1) if t["trunk_mbps"] else 0.0
        item = {"pop_id": pid, "pop_name": n["name"], "trunk_mbps": t["trunk_mbps"], "before_mbps": t["effective_mbps"],
                "after_mbps": after, "before_pct": t["util_pct"], "after_pct": pct_after,
                "status_after": _trunk_status(pct_after, t["trunk_mbps"]), "overbook": ob}
        out.append(item)
        if t["trunk_mbps"] and item["status_after"] in ("warn", "over"):
            warnings.append(f"Trunk {n['name']}: {t['util_pct']}% -> {pct_after}% setelah pelanggan ini"
                            + (" (MELEBIHI kapasitas)" if item["status_after"] == "over" else " (hampir penuh)"))
    return out


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
    cust_info = None
    if req.create_customer:
        raw = {k: v for k, v in (("service", req.cust_service), ("bandwidth_mbps", req.cust_bw_mbps), ("device_sn", req.cust_sn),
                                 ("link_type", req.cust_link_type), ("reg_code", req.cust_reg_code)) if v not in (None, "")}
        if raw:
            cust_info = _clean_asset_fields(cursor, "PELANGGAN", raw)    # 400 (format) / 409 (SN, kode sudah dipakai)
        dn = _norm_name(req.dest_name)
        if dn and dn != "Pelanggan baru":
            d0 = _find_node_dup(cursor, "name", dn)
            if d0:
                warnings.append(f"Nama pelanggan '{dn}' sudah dipakai aset '{d0['name']}' ({d0['type']}); ubah nama agar rencana bisa diwujudkan")

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
        if av["reason"] and (n["type"] or "").upper() != "POP":
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
        if origin["availability"]["eligible"] and origin["availability"]["free"] < cores and (origin.get("kind") or "").upper() not in ("ODP", "POP"):
            warnings.append(f"Aset asal {origin['name']} hanya punya {origin['availability']['free']} port/core kosong; layanan butuh {cores} core")
        if (origin.get("kind") or "").upper() == "ODP" and origin["availability"]["free"] == 0:
            suggest_odp = True
            warnings.append("Port ODP asal penuh: BOQ menyertakan saran ODP baru + splitter; pindahkan titik asal atau pasang ODP baru")
        elif origin["availability"]["eligible"] and origin["availability"]["free"] == 1 and (origin.get("kind") or "").upper() != "POP":
            warnings.append(f"Aset asal {origin['name']} tinggal 1 port/core kosong; penuh setelah rencana ini diwujudkan")
        if (origin.get("kind") or "").upper() == "POP":
            # port OTB: otomatis (usulan) atau manual (dikunci pada rencana, divalidasi ulang saat diwujudkan)
            nrow = cursor.execute("SELECT id, name, type, capacity FROM nodes WHERE id = ?", (origin["id"],)).fetchone()
            ch = _pop_choose_ports(cursor, nrow, cores, req.pop_ports, True)
            if ch["error"] and ch["mode"] == "MANUAL":
                raise HTTPException(status_code=400, detail=ch["error"])
            if ch["error"]:
                warnings.append(f"Aset asal {origin['name']}: {ch['error']}")
            warnings.extend(ch["warnings"])
            origin["pop_ports"] = ch["ports"] if ch["mode"] == "MANUAL" else []
            origin["port_plan"] = {"mode": ch["mode"], "ports": ch["ports"], "warnings": ch["warnings"], "error": ch["error"]}

    slack_origin = origin["type"] == "NODE" and (origin.get("kind") or "").upper() == "SLACK"
    via = []
    for v in (req.via or [])[:8]:
        if len(v) < 2 or not (-90 <= v[0] <= 90 and -180 <= v[1] <= 180):
            raise HTTPException(status_code=400, detail="Titik singgah tidak valid")
        via.append((v[0], v[1]))
    route_mode = (req.route_mode or "road").lower()
    if route_mode not in ("road", "straight", "custom"):
        raise HTTPException(status_code=400, detail="route_mode harus road, straight, atau custom")
    bends = []
    if route_mode == "custom":
        for v in (req.custom_path or []):
            if len(v) < 2 or not (-90 <= v[0] <= 90 and -180 <= v[1] <= 180):
                raise HTTPException(status_code=400, detail="Titik belokan jalur tidak valid")
            bends.append((v[0], v[1]))
        if len(bends) > PLAN_MAX_BENDS:
            raise HTTPException(status_code=400, detail=f"Titik belokan maksimal {PLAN_MAX_BENDS}")
    straight = _haversine_m(origin["lat"], origin["lng"], dest["lat"], dest["lng"])
    if straight < 3:
        raise HTTPException(status_code=400, detail="Titik asal dan tujuan hampir sama (< 3 m)")
    if straight > MAX_ROUTE_KM * 1000:
        raise HTTPException(status_code=400, detail=f"Jarak terlalu jauh (maks {MAX_ROUTE_KM:.0f} km)")

    # --- rute & skenario ---
    drop_max = float(rules["drop_max_m"])
    hub_pt = None
    if hub_mode == "MAP" and scen_req != "DIRECT" and route_mode != "custom":
        if req.hub_lat is None or req.hub_lng is None:
            if scen_req == "HUB":
                raise HTTPException(status_code=400, detail="Titik hub belum dipilih di peta")
        else:
            hub_pt = (req.hub_lat, req.hub_lng)
    rt_notes = []
    if hub_pt:
        rt1 = _plan_route([(origin["lat"], origin["lng"])] + via + [hub_pt], route_mode)
        rt2 = _plan_route([hub_pt, (dest["lat"], dest["lng"])], route_mode)
        c1, c2 = rt1["coords"], rt2["coords"]
        coords = c1 + c2[1:]
        rt = {"coords": coords, "source": rt1["source"] if rt1["source"] == rt2["source"] else "mixed", "note": rt1["note"] or rt2["note"]}
        scen = "HUB"
    else:
        rt = _plan_route([(origin["lat"], origin["lng"])] + via + [(dest["lat"], dest["lng"])], route_mode, bends)
        coords = rt["coords"]
        if req.detour_factor and route_mode == "road" and (rt["source"] != "osrm" or _polyline_length_m(coords) > straight * 2.5 + 300):
            coords = _detour_coords(coords, float(req.detour_factor))
            rt = {"coords": coords, "source": "perkiraan",
                  "note": f"Rute jalan tidak tersedia/tidak wajar: panjang jalur diperkirakan garis lurus x{float(req.detour_factor):g}"}
    length = _polyline_length_m(coords)
    if length > MAX_ROUTE_KM * 1000 * 2:
        raise HTTPException(status_code=400, detail=f"Jalur terlalu panjang (maks {MAX_ROUTE_KM * 2:.0f} km)")
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
                                              else f"kabel udara {seg_defs[0]['cap']} dari aset asal dan OTB {_otb_size(max(cores, int(_cap_cores(seg_defs[0]['cap']) or cores)))} core di lokasi pelanggan (OTB sesuai kapasitas kabel)"
                                              + (f"; layanan {cores} core, sisa {int(_cap_cores(seg_defs[0]['cap'])) - cores} core cadangan (spare)" if int(_cap_cores(seg_defs[0]['cap']) or 0) > cores else "")))
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
        drop_seg = sd["type"] == "Drop" and not rules.get("drop_new_poles")
        sp = _segment_assets(rules, sd["inst"], sd["coords"], use_poles, use_slack and not drop_seg, existing, sd["tag"], allow_new=not drop_seg)
        if drop_seg and use_poles:
            notes.append(f"Segmen dropcore ({sd['label']}) tanpa " + ("tiang" if sd["inst"] == "Udara" else "handhole") + " baru: kabel menumpang tiang eksisting/bangunan (tiang baru hanya untuk kabel di atas batas dropcore)")
        assets += sp["assets"]
        used_ids = {a_["existing_id"] for a_ in sp["assets"] if a_.get("existing_id")}
        if used_ids:     # satu tiang/HH eksisting tidak dipakai dua segmen (mis. di sekitar titik hub)
            existing = {k_: [e_ for e_ in v_ if e_["id"] not in used_ids] for k_, v_ in existing.items()}
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
                         "new_passive_allowed": not drop_seg,
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
        "customer": 1 if req.create_customer else 0, "terminate": True, "route_source": rt["source"], "suggest_new_odp": suggest_odp or bool(req.new_odp_origin and scen == "DIRECT"),
        "use_poles": use_poles, "use_slack": use_slack,
        "scenario": scen, "termination": term, "customer_cores": cores, "segments": [{k: v for k, v in s_.items() if k != "coords"} for s_ in segments],
        "hub": hub, "new_odp": bool(hub and hub["odp"]), "splitter": ratio if hub and hub["odp"] else None,
        "closure_size": hub["closure_size"] if hub else None,
    }
    onc = None
    if slack_origin:
        # Slack hanya titik lewat kabel: untuk mengambil layanan perlu closure baru di titik slack (+ ODP baru bila langsung dropcore 1 core)
        onc = {"closure_size": 12 if 2 * cores <= 12 else 24, "odp": bool(cores == 1 and term == "DROPCORE_ROSET" and scen == "DIRECT"), "ratio": ratio}
        summary["origin_new_closure"] = onc
        notes.append(f"Aset asal berupa Slack: perlu pemasangan closure {onc['closure_size']} core baru di titik slack" + (f" + ODP baru (splitter {ratio})" if onc["odp"] else ""))
        warnings.append("Asal berupa Slack: perlu pemasangan closure" + (" + ODP" if onc["odp"] else "") + " baru di titik slack sebelum layanan bisa ditarik")
    _bmap, _bset = _boq_cfg(cursor)
    extra_spl = [hub["odp"]["ratio"]] if hub and hub["odp"] else []
    extra_sp = (2 * cores) if hub else 0
    if onc:
        extra_sp += 2 * cores
        if onc["odp"]:
            extra_spl = extra_spl + [ratio]
    loss = _plan_loss(cursor, origin, tot_cab, True, int(_bset["splice_per_customer"]), extra_splitters=extra_spl, extra_splices=extra_sp)
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
        if summary["poles_new"] or (not summary["hh_new"] and any(sg_.get("new_passive_allowed", True) for sg_ in segments)):
            boq.append({"code": "TIANG", "label": rules["pole_type"], "qty": summary["poles_new"], "unit": "unit"})
        if summary["hh_new"]:
            boq.append({"code": "HH", "label": rules["hh_type"], "qty": summary["hh_new"], "unit": "unit"})
    if use_slack:
        boq.append({"code": "SLACK", "label": f"Slack {slack_len:g} m", "qty": int(tot_slack_n), "unit": "unit"})
    if hub:
        boq.append({"code": "CLOSURE", "label": f"Closure {hub['closure_size']} core (hub)", "qty": 1, "unit": "unit"})
        if hub["odp"]:
            boq.append({"code": "ODP_BARU", "label": f"ODP baru, splitter {hub['odp']['ratio']}", "qty": 1, "unit": "unit"})
    if onc:
        boq.append({"code": "CLOSURE", "label": f"Closure {onc['closure_size']} core baru (di slack asal)", "qty": 1, "unit": "unit"})
        if onc["odp"]:
            boq.append({"code": "ODP_BARU", "label": f"ODP baru, splitter {ratio} (di slack asal)", "qty": 1, "unit": "unit"})
    if not use_poles:
        warnings.append("Tanpa tiang/handhole baru: pastikan kabel numpang pada infrastruktur eksisting atau jalur sudah tersedia")
    boq.append({"code": "PELANGGAN" if req.create_customer else "TERMINASI",
                "label": ("Titik pelanggan" if req.create_customer else "Terminasi pelanggan") + f" ({cores} core, " + (f"roset {cores} port" if term == 'DROPCORE_ROSET' else f"OTB {_otb_size(max(cores, int(_cap_cores(segments[-1].get('cable_capacity')) or cores)))} core") + ")",
                "qty": 1, "unit": "unit"})
    summary["notes"] = notes
    trunk_check = _plan_trunk_check(cursor, origin, (cust_info or {}).get("bandwidth_mbps"), warnings)
    return {"origin": origin, "dest": dest, "via": [list(v) for v in via],
            "route": {"coords": coords, "length_m": round(length, 1), "source": rt["source"], "mode": route_mode,
                      "bends": [list(b) for b in bends] if route_mode == "custom" else None},
            "cable": {"type": first["cable_type"], "label": first["cable_label"], "capacity": first["cable_capacity"],
                      "installation": first["installation"], "length_m": round(first["route_length_m"], 1), "total_length_m": first["cable_total_m"]},
            "segments": segments, "hub": hub, "scenario": scen, "termination": term, "customer_cores": cores, "notes": notes,
            "assets": assets, "summary": summary, "boq_items": boq, "rules": rules, "loss": loss,
            "create_customer": bool(req.create_customer), "use_poles": use_poles, "use_slack": use_slack, "warnings": warnings,
            "customer_info": cust_info, "trunk_check": trunk_check,
            "remarks": (req.remarks or "").strip()[:1000] or None}


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
            if o.get("pop_ports") and (row["type"] or "").upper() == "POP":
                # pilihan port manual diperiksa ulang SEBELUM aset apa pun dibuat; tidak diganti diam-diam
                chk = _pop_choose_ports(cursor, row, len(o["pop_ports"]), o["pop_ports"], True)
                if chk["error"]:
                    raise HTTPException(status_code=409, detail=f"Port OTB pilihan rencana tidak lagi valid: {chk['error']}. Ubah pilihan port lalu simpan ulang rencana")
        else:
            cluster, area, city = "EKO", "BANJARMASIN", "Kota Banjarmasin"
        prefix = f"PSB-{plan_id:04d}"

        def make_node(name, ntype, lat, lng, capacity, user_name=False, extra=None):
            if user_name:
                _check_name_unique_node(cursor, name, ntype)
            else:
                name = _unique_node_name(cursor, name)
            cursor.execute(
                """INSERT INTO nodes (name, type, status, latitude, longitude, cluster, area, city, capacity, spec_data)
                   VALUES (?, ?, 'Active', ?, ?, ?, ?, ?, ?, '{}')""",
                (name, ntype, lat, lng, cluster, area, city, capacity))
            nid = cursor.lastrowid
            if extra:    # data layanan pelanggan dari rencana; dicek ulang karena SN/kode bisa dipakai pihak lain sejak rencana dibuat
                ex = _clean_asset_fields(cursor, ntype, extra, nid)
                cursor.execute(f"UPDATE nodes SET {', '.join(k + ' = ?' for k in ex)} WHERE id = ?", (*ex.values(), nid))
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
            cust_user = d["name"] != "Pelanggan baru"
            cust_id = make_node(_norm_name(d["name"]) if cust_user else f"{prefix}-PLG", "PELANGGAN",
                                d["lat"], d["lng"], f"{cores} Core", user_name=cust_user, extra=plan.get("customer_info"))
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
            alloc = _allocate_hub(cursor, o["id"], cable_id, closure_id, odp_id, cable2_id, cust_id, cores, o.get("pop_ports") or None) if o["type"] == "NODE" else \
                {"allocated": False, "reason": "Titik asal bebas (bukan aset); alokasi core manual"}
        else:
            cname = f"{prefix}-{segs[0]['cable_type'][:4].upper()}"
            cable_id = make_cable(cname, segs[0], o["id"], cust_id)
            cable2_id = None
            alloc = _allocate_new_customer(cursor, o["id"], cable_id, cust_id, cores, o.get("pop_ports") or None) if o["type"] == "NODE" else \
                {"allocated": False, "reason": "Titik asal bebas (bukan aset); alokasi core manual"}
        info = {"cable_id": cable_id, "cable2_id": cable2_id, "closure_id": closure_id, "odp_id": odp_id, "cable_name": cname, "customer_id": cust_id,
                "node_ids": created["nodes"], "reused_ids": created["reused"], "allocation": alloc}
        cursor.execute("UPDATE plans SET status = 'Realized', realized_at = ?, realized_info = ? WHERE id = ?",
                       (_now_str(), json.dumps(info), plan_id))
        _audit(cursor, "UPDATE", "PLAN", plan_id, r["name"],
               f"Rencana '{r['name']}' diwujudkan: kabel {cname}, {len(created['nodes'])} aset baru")
    tail = (f"Core dialokasikan otomatis ({alloc.get('cores', 1)} core): {alloc['from_port']} -> {alloc['to_port']} lewat {alloc['via_core']}."
            if alloc.get("allocated") else f"Alokasi core manual di Detail Core ({alloc.get('reason')}).")
    if alloc.get("allocated") and alloc.get("ports") and (o.get("kind") or "").upper() == "POP":
        tail += f" Port OTB: {', '.join(alloc['ports'])}." + (" Peringatan: " + "; ".join(alloc["port_notes"]) + "." if alloc.get("port_notes") else "")
    return {"message": f"Rencana diwujudkan: kabel {cname} dan {len(created['nodes'])} aset baru dibuat. {tail}", **info}


# =====================================================================================
# ROUND 9 - C. CEK COVERAGE
# =====================================================================================
# Titik sambung yang dinilai: ODP (port OUT kosong), Closure (core kosong pada kabel yang berujung/melewatinya),
# dan Slack (idem; ditandai bila berada di ujung kabel). Aset yang putus/penuh tetap ditampilkan dengan alasannya.
COVERAGE_MAX_SEARCH_M = 10000.0
COVERAGE_ROUTE_TOP = 6
COVERAGE_SLACK_PREF_M = 1000.0   # ODP/Closure layak dalam jarak ini didahulukan; Slack (perlu closure + ODP baru) hanya bila tidak ada
COVERAGE_UNUSABLE_MAX = 6      # maks. aset terdekat yang tidak bisa dipakai tetap ditampilkan (beserta alasannya)


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
                # Router OSRM memakai profil mobil (patuh jalan satu arah / pembatas jalan). Kabel tidak: bila rute jauh lebih
                # panjang dari garis lurus, itu hampir pasti memutar karena aturan lalu lintas, bukan jarak kabel sebenarnya.
                if rt["source"] == "osrm" and c["route_m"] > c["distance_m"] * 2.5 + 300:
                    c["route_raw_m"] = c["route_m"]
                    c["route_m"] = round(c["distance_m"] * 1.3, 1)
                    c["route_source"] = "perkiraan"
                    c["route_coords"] = None
                done += 1
        # Perbandingan yang adil: aset yang sudah dihitung rute jalannya biasanya lebih panjang dari garis lurus, sehingga
        # aset DEKAT bisa tersalip aset JAUH yang belum dihitung rutenya (masih garis lurus). Untuk mengurutkan, aset tanpa
        # rute diperkirakan dengan rasio rute/garis-lurus (median) dari aset yang sudah berute.
        ratios = sorted(c["route_m"] / c["distance_m"] for c in cands if c["route_m"] and c["distance_m"] >= 30)
        detour = min(3.0, max(1.0, ratios[len(ratios) // 2])) if ratios else 1.0
        for c in cands:
            eff = c["route_m"] if c["route_m"] is not None else c["distance_m"]
            c["effective_m"] = eff
            c["rank_m"] = c["route_m"] if c["route_m"] is not None else c["distance_m"] * detour
            c["in_range"] = eff <= radius
            tier = _pick_tier(rules, eff)
            c["suggested_cable"] = tier["label"]
            c["suggested_type"] = tier["type"]
        # layak dulu; yang berstatus Active didahulukan dari Maintenance; lalu menurut jarak
        main_ok = any(c["eligible"] and c["type"] != "SLACK" and c["distance_m"] <= COVERAGE_SLACK_PREF_M for c in cands)
        for c in cands:
            c["needs_closure"] = c["type"] == "SLACK"
            c["slack_deferred"] = bool(main_ok and c["needs_closure"])
        cands.sort(key=lambda c: (not c["eligible"], c["slack_deferred"], c["status"] != "Active", c["rank_m"]))
        # Yang layak dibatasi 'limit'. Aset terdekat yang TIDAK bisa dipakai (penuh/putus/tanpa kabel) tetap ditampilkan
        # beserta alasannya bila berada dalam radius atau lebih dekat daripada titik layak terdekat, agar tidak "hilang" dari daftar.
        elig_all = [c for c in cands if c["eligible"]]
        elig = elig_all[:limit]
        # Aset layak yang PALING DEKAT selalu ikut tampil, walau berstatus Maintenance (urutan tampil mendahulukan Active,
        # sehingga tanpa ini ia bisa terpotong oleh 8 aset Active yang lebih jauh).
        # Tiga aset layak TERDEKAT (garis lurus) juga selalu ikut tampil.
        for ne in sorted(elig_all, key=lambda c: c["distance_m"])[:3] + [min(elig_all, key=lambda c: c["rank_m"])] if elig_all else []:
            if ne not in elig:
                if len(elig) >= limit:
                    drop = next((x for x in reversed(elig) if x not in sorted(elig_all, key=lambda c: c["distance_m"])[:3]), None)
                    if drop is not None: elig.remove(drop)
                elig.append(ne)
        elig.sort(key=lambda c: (c["slack_deferred"], c["status"] != "Active", c["rank_m"]))
        ref_m = max(radius, min([c["effective_m"] for c in elig_all] or [0.0]))
        not_ok = sorted((c for c in cands if not c["eligible"] and c["distance_m"] <= ref_m), key=lambda c: c["distance_m"])[:COVERAGE_UNUSABLE_MAX]
        cands = elig + not_ok
    best = next((c for c in cands if c["eligible"] and c["in_range"] and not c["slack_deferred"]), None)
    nearest_ok = next((c for c in cands if c["eligible"]), None)
    closest_bad = min((c for c in cands if not c["eligible"]), key=lambda c: c["distance_m"], default=None)
    if best:
        msg = f"Tercover: {best['name']} ({best['type']}) berjarak ±{best['effective_m']:.0f} m, {best['detail']}."
        if best["needs_closure"]:
            msg += " Catatan: Slack bukan titik sambung; perlu pemasangan closure + ODP baru di titik slack."
    elif nearest_ok:
        msg = (f"Di luar jangkauan {radius:.0f} m. Titik sambung layak terdekat: {nearest_ok['name']} "
               f"±{nearest_ok['effective_m']:.0f} m. Lanjutkan ke perencanaan Pasang Baru.")
    elif cands:
        msg = "Ada aset di sekitar, tetapi tidak ada yang bisa dipakai (penuh/putus). Pertimbangkan perencanaan Pasang Baru."
    else:
        msg = f"Tidak ada ODP/Closure/Slack dalam {COVERAGE_MAX_SEARCH_M / 1000:.0f} km."
    ref_d = (best or nearest_ok or {}).get("effective_m")
    if closest_bad and (ref_d is None or closest_bad["distance_m"] < ref_d):
        msg += f" Catatan: {closest_bad['name']} ({closest_bad['type']}) lebih dekat (±{closest_bad['distance_m']:.0f} m) tetapi tidak bisa dipakai: {closest_bad['reason']}."
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


def _scope_ok(row, cluster, area, rp=None):
    """Cocokkan Regional/Cluster/Area satu baris (tanpa beda huruf besar-kecil; kosong = nilai bawaan).
    rp = fungsi(nama_cluster_UPPER) -> bool dari _region_pred (None = tanpa filter Regional)."""
    rc, ra = _scope_key(row["cluster"], row["area"])
    if rp is not None and not rp(rc):
        return False
    if cluster and cluster.upper() != "ALL" and rc != cluster.strip().upper():
        return False
    return not (area and area.upper() != "ALL" and ra != area.strip().upper())


def _folder_ok(fp_row, fp):
    """Folder + seluruh subfoldernya; fp kosong = semua."""
    if not fp:
        return True
    v = fp_row or ""
    return v == fp or v.startswith(fp + "/")


def _export_rows(cursor, scope, type_, status, cluster, installation, q, area="ALL", region="ALL", folder=""):
    ql = (q or "").strip().lower()
    fpn = _norm_folder(folder, allow_empty=True)
    rp = _region_pred(cursor, region)
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
            if not _scope_ok(r, cluster, area, rp) or not _folder_ok(r["folder_path"], fpn):
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
            if not _scope_ok(d, cluster, area, rp) or not _folder_ok(d.get("folder_path"), fpn):
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


def _export_incidents(cursor, status, cluster, q, area="ALL", region="ALL"):
    rp = _region_pred(cursor, region)
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
        if not _scope_ok(r, cluster, area, rp):
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
                         n["area"], n["city"], n["capacity"], *_asset_cells(n)] for n in nodes]))
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
                                     "city": n["city"], "capacity": n["capacity"],
                                     **{k: n.get(k) for k in ASSET_EXPORT_COLS if n.get(k) not in (None, "")}}})
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
    tree = {}   # jalur folder -> [(kind, item)]
    for n in nodes:
        tree.setdefault(n.get("folder_path") or n["type"], []).append(("NODE", n))
    for c in cables:
        tree.setdefault(c.get("folder_path") or ("Kabel " + c["type"]), []).append(("CABLE", c))

    def placemark(kind, it):
        typ = it["type"]
        pairs = [("kind", kind), ("type", typ), ("status", it["status"]), ("cluster", it["cluster"]),
                 ("area", it["area"]), ("city", it["city"]), ("capacity", it["capacity"]),
                 ("folder", it.get("folder_path"))]
        if kind == "NODE":
            pairs += [(k, it.get(k)) for k in ASSET_EXPORT_COLS if it.get(k) not in (None, "")]
            geom = f"<Point><coordinates>{it['longitude']},{it['latitude']},0</coordinates></Point>"
        else:
            pairs += [("installation", it.get("installation")), ("length_m", it["length_m"])]
            geom = "<LineString><tessellate>1</tessellate><coordinates>" + " ".join(
                f"{p[0]},{p[1]},0" for p in it["coords"]) + "</coordinates></LineString>"
        return (f'<Placemark><name>{_xml(it["name"])}</name><styleUrl>#s-{_xml(typ)}</styleUrl>'
                f'<description>{desc(pairs)}</description>{ext(pairs)}{geom}</Placemark>')

    # susun pohon: {nama: {"_items": [...], "_kids": {...}}}
    root = {"_items": [], "_kids": {}}
    for path, items in tree.items():
        node = root
        for part in [x for x in path.split("/") if x]:
            node = node["_kids"].setdefault(part, {"_items": [], "_kids": {}})
        node["_items"].extend(items)

    def emit(node):
        for name in sorted(node["_kids"], key=str.lower):
            kid = node["_kids"][name]
            out.append(f"<Folder><name>{_xml(name)}</name>")
            emit(kid)
            out.append("</Folder>")
        for kind, it in sorted(node["_items"], key=lambda x: (x[0], (x[1]["name"] or "").lower())):
            out.append(placemark(kind, it))
    emit(root)
    out.append("</Document></kml>")
    return "\n".join(out).encode("utf-8")


def _csv_bytes(header, rows, delimiter) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delimiter, lineterminator="\r\n")
    w.writerow(header)
    for r in rows:
        w.writerow(["" if v is None else v for v in r])
    return ("﻿" + buf.getvalue()).encode("utf-8")


ASSET_EXPORT_COLS = ["reg_code", "service", "bandwidth_mbps", "device_sn", "link_type", "trunk_mbps", "trunk_overbook"]
NODE_CSV_HEADER = ["name", "type", "status", "latitude", "longitude", "cluster", "area", "city", "capacity"] + ASSET_EXPORT_COLS


def _asset_cells(n, safe=False):
    f = _csv_safe if safe else (lambda v: v)
    return [f(n.get(c)) for c in ASSET_EXPORT_COLS]
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
              _csv_safe(n["cluster"]), _csv_safe(n["area"]), _csv_safe(n["city"]), _csv_safe(n["capacity"]),
              *_asset_cells(n, safe=True)]
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
                area: str = "ALL", region: str = "ALL", folder: str = ""):
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
                                     cluster, installation, q, area, region, folder)
        incidents = []
        if not _norm_folder(folder, allow_empty=True) and (scope in ("all", "incidents") and fmt in ("csv", "xlsx") or (scope == "incidents" and fmt == "geojson")):
            incidents = _export_incidents(cursor, status, cluster, q, area, region)
        filt = [f"{k}={v}" for k, v in (("regional", region), ("cluster", cluster), ("area", area), ("folder", _norm_folder(folder, allow_empty=True)), ("jenis", type), ("status", status),
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
        ex_node = [["Contoh-ODP-01", "ODP", "Active", -3.323, 114.593, "EKO", "BANJARMASIN", "Kota Banjarmasin", "1 In - 8 Out", "ODP-00001", None, None, None, None, None, None],
                   ["Contoh-Tiang-01", "TIANG", "Active", -3.3231, 114.5931, "EKO", "BANJARMASIN", "Kota Banjarmasin", "Tiang 7m", None, None, None, None, None, None, None],
                   ["Contoh-Pelanggan-01", "PELANGGAN", "Active", -3.3235, 114.5935, "EKO", "BANJARMASIN", "Kota Banjarmasin", "2 Core", "PLG-00001", "Dedicated Internet", 100, "HWTC1A2B3C4D", "GPON", None, None],
                   ["Contoh-POP-01", "POP", "Active", -3.3200, 114.5900, "EKO", "BANJARMASIN", "Kota Banjarmasin", "12+24", "POP-00001", None, None, None, None, 10000, 2]]
        ex_cable = [["Contoh-Kabel-01", "Distribution", "Active", "Udara", "24C", "EKO", "BANJARMASIN", "Kota Banjarmasin",
                     None, None, None, "114.5930000 -3.3230000;114.5950000 -3.3240000;114.5970000 -3.3250000"]]
        guide = [["Sheet 'Node'", "Satu baris = satu titik (POP, CLOSURE, ODP, TIANG, HH, SLACK, PELANGGAN). Kolom wajib: name, type, latitude, longitude"],
                 ["Sheet 'Kabel'", "Satu baris = satu jalur. Kolom wajib: name, type (Backbone/Feeder/Distribution/Drop), coordinates"],
                 ["coordinates", "Pasangan 'bujur lintang' dipisah titik-koma: 114.593 -3.323;114.595 -3.324"],
                 ["status", "Active / Maintenance / Cut/Broken (kosong = Active)"],
                 ["reg_code", "Kode registrasi (opsional, unik, tanpa membedakan huruf besar/kecil). Tidak berlaku untuk HH, SLACK, dan kabel"],
                 ["service, bandwidth_mbps, device_sn, link_type", "Khusus PELANGGAN. bandwidth_mbps boleh '100', '100 Mbps', atau '1 Gbps'. link_type = GPON atau PTP. device_sn = SN ONT/CPE (unik)"],
                 ["trunk_mbps, trunk_overbook", "Khusus POP. Kapasitas trunk (Mbps, boleh '10 Gbps') dan rasio overbooking 1 s.d. 100"],
                 ["Nama aset", "Harus unik (huruf besar/kecil dianggap sama); nama yang sudah ada dilewati / diperbarui sesuai pilihan"],
                 ["cluster, area", "Harus sesuai daftar menu Wilayah. Kosong = diambil dari nama folder rute (KML) atau bawaan EKO / BANJARMASIN bila terdaftar"],
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
             "1 In - 8 Out", "ODP-00001", "", "", "", "", "", ""],
            ["Contoh-Tiang-01", "TIANG", "Active", "-3.3231000", "114.5931000", "EKO", "BANJARMASIN",
             "Kota Banjarmasin", "Tiang 7m", "", "", "", "", "", "", ""],
            ["Contoh-Pelanggan-01", "PELANGGAN", "Active", "-3.3235000", "114.5935000", "EKO", "BANJARMASIN",
             "Kota Banjarmasin", "2 Core", "PLG-00001", "Dedicated Internet", "100", "HWTC1A2B3C4D", "GPON", "", ""],
            ["Contoh-POP-01", "POP", "Active", "-3.3200000", "114.5900000", "EKO", "BANJARMASIN",
             "Kota Banjarmasin", "12+24", "POP-00001", "", "", "", "", "10000", "2"]], delim)
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
NEAR_DUP_M = 50.0     # nama sama + jarak <= ini = aset yang sama (duplikat); lebih jauh = aset berbeda bernama sama
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
    "reg_code": {"reg_code", "kode_registrasi", "kode_reg", "registrasi", "registration_code", "reg"},
    "service": {"service", "layanan"},
    "bandwidth_mbps": {"bandwidth_mbps", "bandwidth", "bw", "bw_mbps", "kecepatan"},
    "device_sn": {"device_sn", "sn", "sn_perangkat", "serial", "serial_number", "sn_ont", "sn_cpe"},
    "link_type": {"link_type", "jenis_layanan", "jenis_link", "tipe_layanan"},
    "trunk_mbps": {"trunk_mbps", "trunk", "kapasitas_trunk"},
    "trunk_overbook": {"trunk_overbook", "overbooking", "overbook", "rasio_overbooking"},
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


# kata tambahan yang dikenali sebagai petunjuk jenis (nama aset / nama folder), selain alias baku di atas
NODE_KEYWORDS = {**NODE_TYPE_ALIASES, "SPLITER": "ODP", "SPLITTER": "ODP", "MANHOLE": "HH", "SAMBUNGAN": "CLOSURE",
                 "CADANGAN": "SLACK", "PELANGGANS": "PELANGGAN", "TIANGS": "TIANG"}
CABLE_KEYWORDS = {**CABLE_TYPE_ALIASES, "UTAMA": "Backbone", "DISTRIBUTION": "Distribution"}


def _kw_lookup(tok, aliases):
    tok = tok.upper()
    if tok in aliases:
        return aliases[tok]
    base = re.sub(r"\d+$", "", tok)          # ODP01 -> ODP, HH12 -> HH
    return aliases.get(base) if base and base != tok else None


def _infer_from_name(name, aliases):
    for tok in re.split(r"[^A-Za-z0-9]+", str(name or "").upper()):
        if tok:
            t = _kw_lookup(tok, aliases)
            if t:
                return t
    return None


def _infer_from_path(rw, aliases):
    """Jenis dari nama folder: dari folder terdalam ke induk. Satu folder harus menunjuk SATU jenis (kalau ganda, dilewati).
    Mengembalikan (jenis, nama folder yang dipakai) atau (None, None)."""
    parts = [x.strip() for x in str(rw.get("folder_path") or "").split("/") if x.strip()]
    if not parts and rw.get("folder"):
        parts = [str(rw["folder"])]
    for part in reversed(parts):
        hits = set()
        for tok in re.split(r"[^A-Za-z0-9]+", part.upper()):
            t = _kw_lookup(tok, aliases) if tok else None
            if t:
                hits.add(t)
        if len(hits) == 1:
            return hits.pop(), part
    return None, None


def _norm_capacity_node(typ, v):
    s = str(v or "").strip()
    if not s:
        return NODE_DEFAULT_CAPACITY[typ], False
    if typ == "ODP":
        m = re.search(r"(\d+)\s*in\s*-\s*(\d+)\s*out", s, re.I)
        if m:
            return f"{int(m.group(1))} In - {int(m.group(2))} Out", False
        return NODE_DEFAULT_CAPACITY[typ], True
    if typ == "POP" and "+" in s:      # beberapa OTB: '12+24' (jumlah port tiap OTB)
        if re.match(r"^\s*\d+(\s*\+\s*\d+)+\s*(c|core|port|p)?\s*$", s, re.I):
            nums = [int(x) for x in re.findall(r"\d+", s)]
            if all(0 < n <= 576 for n in nums) and len(nums) <= OTB_MAX_UNITS:
                return "+".join(str(n) for n in nums), False
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

    def desc_props(ch):
        """Isi <description> (HTML / teks 'kunci: nilai') -> properti, hanya kolom yang dikenal."""
        html_ = ch.findtext("{*}description") or ""
        if not html_.strip():
            return {}
        pairs = []
        for m in re.finditer(r"<tr[^>]*>\s*<t[dh][^>]*>(.*?)</t[dh]>\s*<t[dh][^>]*>(.*?)</t[dh]>", html_, re.I | re.S):
            pairs.append((m.group(1), m.group(2)))
        txt = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", html_, flags=re.I)
        for line in re.sub(r"<[^>]+>", "", txt).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                pairs.append((k, v))
        known = {n for names in _COL_ALIASES.values() for n in names} - {"name", "title", "label", "x", "y", "geometry", "path"}
        out = {}
        for k, v in pairs:
            k = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", k)).strip()
            v = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", v))).strip()
            ck = re.sub(r"[\s\-]+", "_", k.lower())
            if ck in known and v and ck not in out:
                out[ck] = v
        return out

    def kml_coords(el):
        txt = (el.findtext("{*}coordinates") or "").strip()
        pts = []
        for tok in txt.split():
            nums = tok.split(",")
            if len(nums) >= 2:
                pts.append(nums[:2])
        return pts

    def walk(el, folder, fpath=()):
        for ch in el:
            t = _local(ch.tag)
            if t in ("Folder", "Document"):
                nm = (ch.findtext("{*}name") or "").strip() or folder
                # nama <Document> (biasanya nama berkas) tidak ikut jalur; hanya <Folder>
                walk(ch, nm, fpath + (((ch.findtext("{*}name") or "").strip(),) if t == "Folder" and (ch.findtext("{*}name") or "").strip() else ()))
            elif t == "Placemark":
                props = {}
                nm = (ch.findtext("{*}name") or "").strip()
                if nm:
                    props["name"] = nm
                for dk, dv in desc_props(ch).items():
                    props.setdefault(dk, dv)
                for d in ch.iter():
                    lt = _local(d.tag)
                    if lt == "Data" and d.get("name"):
                        props[d.get("name")] = (d.findtext("{*}value") or "").strip()
                    elif lt == "SimpleData" and d.get("name"):
                        props[d.get("name")] = (d.text or "").strip()
                geoms = [g for g in ch.iter() if _local(g.tag) in ("Point", "LineString")]
                if not geoms:
                    raws.append({"src": f"placemark '{nm or '?'}'", "props": props, "folder": folder,
                                 "folder_path": "/".join(fpath), "geom": "unsupported", "coords": None, "gt": "Polygon/lainnya"})
                for j, g in enumerate(geoms):
                    pts = kml_coords(g)
                    gt = _local(g.tag)
                    raws.append({"src": f"placemark '{nm or '?'}'", "props": props, "folder": folder,
                                 "folder_path": "/".join(fpath), "geom": "point" if gt == "Point" else "line",
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


def _extract_import(filename: str, content: str, content_base64: str, raw: Optional[bytes] = None):
    name = (filename or "").lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    data = None
    if raw is not None:
        data = bytes(raw)
        if len(data) > IMPORT_MAX_BYTES:
            raise HTTPException(status_code=413, detail=f"Berkas terlalu besar (maks {IMPORT_MAX_BYTES / 1_000_000:g} MB)")
    elif content_base64:
        try:
            data = base64.b64decode(content_base64, validate=False)
        except Exception:
            raise HTTPException(status_code=400, detail="Isi berkas (base64) tidak valid")
        if len(data) > IMPORT_MAX_BYTES:
            raise HTTPException(status_code=413, detail=f"Berkas terlalu besar (maks {IMPORT_MAX_BYTES / 1_000_000:g} MB)")
    elif content:
        if len(content.encode("utf-8")) > IMPORT_MAX_BYTES:
            raise HTTPException(status_code=413, detail=f"Berkas terlalu besar (maks {IMPORT_MAX_BYTES / 1_000_000:g} MB)")
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


def _parse_bw_mbps(v):
    """'100', 100, '100 Mbps', '1,5 Gbps', '500M', '1G' -> Mbps (float) atau None bila tidak terbaca."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.fullmatch(r"\s*([0-9]+(?:[.,][0-9]+)?)\s*([a-zA-Z]*)\s*(?:bps|/s)?\s*", str(v))
    if not m:
        return None
    n = float(m.group(1).replace(",", "."))
    u = m.group(2).lower()
    if u in ("", "m", "mb", "mbps", "mbit"):
        return n
    if u in ("g", "gb", "gbps", "gbit"):
        return n * 1000
    if u in ("k", "kb", "kbps"):
        return n / 1000
    return None


def _import_asset_fields(typ, p, rec):
    """Parameter aset dari satu baris impor -> rec['asset'] (hanya yang terisi & valid). Yang tak valid dilewati + peringatan."""
    t = (typ or "").upper()
    out = {}
    w = rec["warnings"].append

    def given(k):
        return p.get(k) not in (None, "")

    if given("reg_code"):
        if t in NO_REG_TYPES:
            w(f"Kode registrasi tidak berlaku untuk {t}; diabaikan")
        else:
            code = _norm_code(p["reg_code"])
            if code and REG_CODE_RE.match(code):
                out["reg_code"] = code
            else:
                w(f"Kode registrasi '{p['reg_code']}' tidak valid; diabaikan")
    cust = [k for k in CUSTOMER_FIELDS if given(k)]
    if cust and t != "PELANGGAN":
        w("Layanan, bandwidth, SN, dan jenis hanya untuk PELANGGAN; diabaikan")
    elif t == "PELANGGAN":
        if given("service"):
            sv = _norm_name(p["service"])
            if len(sv) <= 80:
                out["service"] = sv
            else:
                w("Layanan > 80 karakter; diabaikan")
        if given("bandwidth_mbps"):
            bw = _parse_bw_mbps(p["bandwidth_mbps"])
            if bw is not None and 0 < bw <= BW_MAX_MBPS:
                out["bandwidth_mbps"] = round(bw, 3)
            else:
                w(f"Bandwidth '{p['bandwidth_mbps']}' tidak valid; diabaikan")
        if given("device_sn"):
            sn = _norm_code(p["device_sn"])
            if sn and SN_RE.match(sn):
                out["device_sn"] = sn
            else:
                w(f"SN perangkat '{p['device_sn']}' tidak valid; diabaikan")
        if given("link_type"):
            lt = str(p["link_type"]).strip().upper()
            if lt in LINK_TYPES:
                out["link_type"] = lt
            else:
                w(f"Jenis layanan '{p['link_type']}' bukan GPON/PTP; diabaikan")
    trunk = [k for k in TRUNK_FIELDS if given(k)]
    if trunk and t != "POP":
        w("Kapasitas trunk hanya untuk POP; diabaikan")
    elif t == "POP":
        if given("trunk_mbps"):
            tr = _parse_bw_mbps(p["trunk_mbps"])
            if tr is not None and 0 < tr <= BW_MAX_MBPS * 10:
                out["trunk_mbps"] = round(tr, 3)
            else:
                w(f"Kapasitas trunk '{p['trunk_mbps']}' tidak valid; diabaikan")
        if given("trunk_overbook"):
            ob = _to_float(p["trunk_overbook"], True)
            if ob is not None and 1 <= ob <= 100:
                out["trunk_overbook"] = round(ob, 2)
            else:
                w(f"Rasio overbooking '{p['trunk_overbook']}' harus 1-100; diabaikan")
    rec["asset"] = out


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
            rec["noname"] = True            # diberi nama otomatis di _plan_import (jenis_nomor)
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
        _fp = rw.get("folder_path") or str(p.get("folder") or p.get("folder_path") or "")
        if _fp:
            try:
                rec["folder_path"] = _norm_folder(_fp, allow_empty=True) or None
            except HTTPException:
                rec["folder_path"] = None
        rec["cluster"] = str(p.get("cluster") or "")[:60]     # kosong -> ditentukan _wil_assign_imports (folder rute / bawaan)
        rec["area"] = str(p.get("area") or "")[:60]
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
                typ = _infer_from_name(rec["name"], NODE_KEYWORDS)
                if typ:
                    rec["warnings"].append(f"Jenis ditebak dari nama: {typ}")
            if typ is None:
                typ, fnm = _infer_from_path(rw, NODE_KEYWORDS)
                if typ:
                    rec["warnings"].append(f"Jenis ditebak dari nama folder '{fnm}': {typ}")
            if typ is None:
                rec["errors"].append("Jenis aset tidak diketahui (isi kolom type: POP/CLOSURE/ODP/TIANG/HH/SLACK/PELANGGAN)")
            else:
                rec["type"] = typ
                rec["capacity"], bad = _norm_capacity_node(typ, p.get("capacity"))
                if bad:
                    rec["warnings"].append(f"Kapasitas '{p.get('capacity')}' tidak sesuai; dipakai {rec['capacity']}")
                _import_asset_fields(typ, p, rec)
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
                typ = _infer_from_name(rec["name"], CABLE_KEYWORDS)
                if typ:
                    rec["warnings"].append(f"Jenis ditebak dari nama: {typ}")
            if typ is None:
                typ, fnm = _infer_from_path(rw, CABLE_KEYWORDS)
                if typ:
                    rec["warnings"].append(f"Jenis ditebak dari nama folder '{fnm}': {typ}")
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


def _numbered_name(base, taken_keys, limit=120):
    """Nama unik dengan akhiran -2, -3, ... (taken_keys = kunci nama yang sudah dipakai)."""
    base = (base or "").strip()
    for n in range(2, 100000):
        suf = f"-{n}"
        cand = base[: limit - len(suf)] + suf
        if _name_key(cand) not in taken_keys:
            return cand
    return base


def _wil_assign_imports(cursor, recs):
    """Cluster/Area tiap baris impor harus ada di daftar Wilayah. Bila berkas tak mengisinya, diambil dari nama folder rute
    (area/cluster yang namanya cocok), terakhir dari nilai bawaan lama bila terdaftar. Tidak cocok = galat baris."""
    maps = _wil_maps(cursor)
    cl, ar = maps
    for rec in recs:
        if rec["errors"]:
            continue
        c, a = (rec.get("cluster") or "").strip(), (rec.get("area") or "").strip()
        note = None
        if not (c and a):
            segs = [x.strip() for x in str(rec.get("folder_path") or "").split("/") if x.strip()]
            if not a:
                for sg in reversed(segs):
                    h = ar.get(sg.upper())
                    if h and (not c or h["cname"].upper() == c.upper()):
                        a, c, note = h["name"], (c or h["cname"]), f"Cluster/Area diambil dari nama folder '{sg}'"
                        break
            if not c:
                for sg in reversed(segs):
                    if sg.upper() in cl:
                        c, note = cl[sg.upper()]["name"], f"Cluster diambil dari nama folder '{sg}'"
                        break
        fallback = not c and not a
        try:
            rec["cluster"], rec["area"] = _wil_resolve(cursor, c, a, maps)
        except HTTPException as exc:
            rec["errors"].append(exc.detail)
            continue
        if note:
            rec["warnings"].append(note)
        elif fallback:
            rec["warnings"].append(f"Cluster/Area tidak ada di berkas maupun nama folder; dipakai bawaan {rec['cluster']} / {rec['area']}")


def _far_from_existing(rec, existing):
    """Jarak (m) antara baris impor dan aset lama bernama sama bila > NEAR_DUP_M, selain itu None (dianggap aset yang sama)."""
    try:
        if rec["kind"] == "NODE":
            if existing.get("latitude") is None:
                return None
            d = _haversine_m(rec["lat"], rec["lng"], existing["latitude"], existing["longitude"])
        else:
            g = json.loads(existing.get("geojson_geometry") or "{}").get("coordinates") or []
            if len(g) < 2 or not rec.get("coords"):
                return None
            a0, a1, b0, b1 = rec["coords"][0], rec["coords"][-1], g[0], g[-1]
            fw = max(_haversine_m(a0[1], a0[0], b0[1], b0[0]), _haversine_m(a1[1], a1[0], b1[1], b1[0]))
            bw = max(_haversine_m(a0[1], a0[0], b1[1], b1[0]), _haversine_m(a1[1], a1[0], b0[1], b0[0]))
            d = min(fw, bw)
    except (ValueError, TypeError, KeyError, IndexError):
        return None
    return d if d > NEAR_DUP_M else None


def _plan_import(cursor, req: ImportRequest, progress=None, overrides=None, raws=None, fmt=None, apply_names=False):
    """Baca + validasi + rencanakan aksi tiap baris. overrides = koreksi pengguna per baris (kunci = nomor baris 'rid')."""
    mode = (req.on_duplicate or "skip").lower()
    if mode not in ("skip", "update", "create"):
        raise HTTPException(status_code=400, detail="on_duplicate harus skip, update, atau create")
    if raws is None:
        fmt, raws = _extract_import(req.filename, req.content or "", req.content_base64 or "")
    if not raws:
        raise HTTPException(status_code=400, detail="Tidak ada data yang bisa dibaca dari berkas")
    if len(raws) > IMPORT_MAX_RECORDS:
        raise HTTPException(status_code=413, detail=f"Terlalu banyak data ({len(raws)}); maksimal {IMPORT_MAX_RECORDS} per impor")
    ov_all = overrides or {}
    recs = _build_records(_apply_raw_overrides(cursor, raws, ov_all))
    for i, r_ in enumerate(recs):
        r_["rid"] = i
    _wil_assign_imports(cursor, recs)

    ex_nodes = [dict(r) for r in cursor.execute(
        "SELECT * FROM nodes WHERE type != 'INCIDENT'").fetchall()]
    node_by_key = {_name_key(r["name"]): r for r in ex_nodes}
    ex_cables = [dict(r) for r in cursor.execute("SELECT * FROM cables").fetchall()]
    cable_by_key = {_name_key(r["name"]): r for r in ex_cables}
    node_by_id = {r["id"]: r for r in ex_nodes}
    cable_by_id = {r["id"]: r for r in ex_cables}
    node_grid = {}
    for e_ in ex_nodes:
        if e_.get("latitude") is not None and e_.get("longitude") is not None:
            node_grid.setdefault((e_["type"], int(math.floor(e_["latitude"] * 1000)), int(math.floor(e_["longitude"] * 1000))), []).append(e_)
    reg_db = {r["reg_code"]: r for r in ex_nodes if r.get("reg_code")}
    sn_db = {r["device_sn"]: r for r in ex_nodes if r.get("device_sn")}
    reg_file, sn_file = {}, {}
    seen = set()
    seen_pos = {}
    for idx, rec in enumerate(recs):
        if progress and idx % 50 == 0:
            progress(idx, len(recs))
        ov = ov_all.get(str(rec["rid"])) or {}
        oact = ov.get("action")
        if oact == "skip":
            was = "; ".join(rec["errors"])
            rec["errors"] = []
            rec["action"] = "skip"
            rec["warnings"].append("Dilewati atas keputusan pengguna" + (f" (galat awal: {was})" if was else ""))
            continue
        merge_to = None
        if oact == "merge" and rec["kind"] in ("NODE", "CABLE"):
            merge_to = (node_by_id if rec["kind"] == "NODE" else cable_by_id).get(int(ov.get("merge_id") or 0))
            if merge_to is None:
                rec["errors"].append("Aset tujuan gabung tidak ditemukan (mungkin sudah dihapus); pilih ulang")
            else:
                rec["errors"] = []          # gabung hanya memperbarui atribut; geometri/jenis dari berkas tak dipakai
                rec["type"] = merge_to["type"]
                rec["name"] = merge_to["name"]
                rec["cluster"], rec["area"], rec["city"] = merge_to["cluster"], merge_to["area"], merge_to["city"]
                if not ov.get("status"):
                    rec["status"] = merge_to["status"]
        rec["action"] = "error" if rec["errors"] else "create"
        if rec["errors"]:
            continue
        if rec.get("noname"):
            pref = rec["type"] or ("KABEL" if rec["kind"] == "CABLE" else "ASET")
            n_ = 1
            while True:
                cand = f"{pref}-{n_:03d}"
                ck = _name_key(cand)
                if (rec["kind"], ck) not in seen and ck not in (node_by_key if rec["kind"] == "NODE" else cable_by_key):
                    break
                n_ += 1
            rec["name"] = cand
            rec["warnings"].append(f"Nama kosong; dibuat otomatis: {cand}")
        key = _name_key(rec["name"])
        if (rec["kind"], key) in seen_pos:
            same_spot = rec["kind"] == "NODE" and any(
                t_ == rec["type"] and _haversine_m(rec["lat"], rec["lng"], la_, ln_) < 1.5 for t_, la_, ln_ in seen_pos[(rec["kind"], key)])
            if same_spot:
                rec["action"] = "skip"
                rec["warnings"].append("Duplikat persis di dalam berkas (nama, jenis, dan titik sama); baris ini dilewati")
                continue
            taken_k = {k_ for (kd_, k_) in seen_pos if kd_ == rec["kind"]} | set(node_by_key if rec["kind"] == "NODE" else cable_by_key)
            old_nm = rec["name"]
            rec["name"] = _numbered_name(old_nm, taken_k)
            key = _name_key(rec["name"])
            rec["warnings"].append(f"Nama ganda di dalam berkas; diberi nomor: {old_nm} -> {rec['name']}")
        seen_pos.setdefault((rec["kind"], key), []).append((rec["type"], rec.get("lat"), rec.get("lng")))
        seen.add((rec["kind"], key))
        existing = merge_to if merge_to is not None else (node_by_key.get(key) if rec["kind"] == "NODE" else cable_by_key.get(key))
        if existing is None and rec["kind"] == "NODE" and oact != "create":
            ci, cj = int(math.floor(rec["lat"] * 1000)), int(math.floor(rec["lng"] * 1000))
            for di in (-1, 0, 1):               # titik yang sama persis (<1.5 m) dan jenis sama (indeks sel ~110 m)
                for dj in (-1, 0, 1):
                    for e in node_grid.get((rec["type"], ci + di, cj + dj), ()):
                        if _haversine_m(rec["lat"], rec["lng"], e["latitude"], e["longitude"]) < 1.5:
                            existing = e
                            break
                    if existing is not None:
                        break
                if existing is not None:
                    break
        if existing is not None and merge_to is None and oact != "create" and mode != "create" and _name_key(existing["name"]) == key:
            far = _far_from_existing(rec, existing)
            if far is not None:       # nama sama tetapi tempatnya berbeda: bukan duplikat -> aset baru bernomor
                taken_k = {k_ for (kd_, k_) in seen_pos if kd_ == rec["kind"]} | set(node_by_key if rec["kind"] == "NODE" else cable_by_key)
                old_nm = rec["name"]
                rec["name"] = _numbered_name(old_nm, taken_k)
                seen_pos.setdefault((rec["kind"], _name_key(rec["name"])), []).append((rec["type"], rec.get("lat"), rec.get("lng")))
                seen.add((rec["kind"], _name_key(rec["name"])))
                rec["warnings"].append(f"Nama sama dengan '{existing['name']}' tetapi berjarak {far:,.0f} m (di luar {NEAR_DUP_M:.0f} m): dianggap aset berbeda, dibuat baru dengan nomor: {old_nm} -> {rec['name']}")
                existing = None
        if existing is not None:
            rec["existing_id"] = existing["id"]
            rec["existing_name"] = existing["name"]
            if merge_to is not None:
                rec["action"] = "update"
                rec["capacity"] = existing["capacity"]
                rec["warnings"].append(f"Digabung ke aset yang sudah ada: {existing['name']} (nama, posisi, jenis, dan kapasitas tetap; hanya atribut lain yang diperbarui)")
            elif (_name_key(existing["name"]) == key and existing["type"] != rec["type"]) or mode == "create" or oact == "create":
                if _name_key(existing["name"]) == key:
                    taken_k = {k_ for (kd_, k_) in seen_pos if kd_ == rec["kind"]} | set(node_by_key if rec["kind"] == "NODE" else cable_by_key)
                    old_nm = rec["name"]
                    rec["name"] = _numbered_name(old_nm, taken_k)
                    seen_pos.setdefault((rec["kind"], _name_key(rec["name"])), []).append((rec["type"], rec.get("lat"), rec.get("lng")))
                    seen.add((rec["kind"], _name_key(rec["name"])))
                    rec["warnings"].append(f"Nama sudah dipakai aset yang ada ({existing['name']}); diberi nomor: {old_nm} -> {rec['name']}")
                    rec.pop("existing_id", None); rec.pop("existing_name", None)
                else:
                    rec["warnings"].append(f"Titik sama dengan aset yang ada ({existing['name']}) tetapi dibuat sebagai aset baru")
                    rec.pop("existing_id", None); rec.pop("existing_name", None)
            elif mode == "skip":
                rec["action"] = "skip"
                rec["warnings"].append(f"Sudah ada ({existing['name']}); dilewati")
            elif mode == "update":
                rec["action"] = "update"
        if rec["action"] in ("create", "update") and rec["kind"] == "NODE":
            own = rec.get("existing_id")
            for col, db_map, file_map, lab in (("reg_code", reg_db, reg_file, "Kode registrasi"), ("device_sn", sn_db, sn_file, "SN perangkat")):
                val = (rec.get("asset") or {}).get(col)
                if not val:
                    continue
                other = db_map.get(val)
                if other is not None and other["id"] != own:
                    rec["action"] = "error"
                    rec["errors"].append(f"{lab} {val} sudah dipakai aset '{other['name']}'")
                elif val in file_map:
                    rec["action"] = "error"
                    rec["errors"].append(f"{lab} {val} dipakai juga oleh baris lain dalam berkas ({file_map[val]})")
                else:
                    file_map[val] = rec["name"]
    _import_system_names(cursor, recs, apply_names)
    return fmt, recs, mode


def _import_system_names(cursor, recs, apply=False):
    """Samakan nama aset baru hasil impor dengan format nama sistem (kecuali POP dan Pelanggan, dan baris yang hanya memperbarui/gabung)."""
    cfg = _naming_cfg(cursor)
    if not cfg.get("import_rename"):
        return
    counter, done = {}, 0
    for rec in recs:
        if rec["action"] != "create":
            continue
        if rec["kind"] == "NODE":
            if (rec["type"] or "").upper() not in NAME_AUTO_NODE:
                continue
            typ, cap, inst = rec["type"], None, None
        elif rec["kind"] == "CABLE":
            if rec["type"] not in NAME_AUTO_CABLE:
                continue
            typ, cap, inst = rec["type"], rec.get("capacity"), rec.get("installation")
        else:
            continue
        try:
            new = _gen_name(cursor, rec["kind"], typ, rec.get("cluster"), rec.get("area"), cfg, None, cap, inst, counter)
        except HTTPException:
            continue
        if not apply:
            rec["sys_name"] = new            # pratinjau: nama asal tetap tampil, nama sistem baru diberikan saat disimpan
            done += 1
            continue
        old = rec["name"]
        rec["orig_name"] = old
        rec["name"] = new
        rec["warnings"].append(f"Nama disamakan dengan format sistem: {old} -> {new}")
        done += 1
    return done


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
                     "installation": r.get("installation"), "latitude": r.get("lat"), "longitude": r.get("lng"),
                     "sys_name": r.get("sys_name")})
    return {"filename": filename, "format": fmt, "on_duplicate": mode, "total": len(recs),
            "nodes": sum(1 for r in recs if r["kind"] == "NODE"), "cables": sum(1 for r in recs if r["kind"] == "CABLE"),
            "counts": counts, "by_type": {k: v for k, v in by_type.items() if v},
            "warnings": sum(len(r["warnings"]) for r in recs), "rows": rows,
            "truncated": len(recs) > IMPORT_PREVIEW_ROWS}


@app.post("/api/import/preview")
def import_preview(req: ImportRequest):
    """Baca berkas, validasi, dan laporkan apa yang AKAN terjadi. Tidak mengubah data."""
    _refresh_import_limits()
    with db() as conn:
        fmt, recs, mode = _plan_import(conn.cursor(), req)
    return _import_summary(fmt, recs, mode, req.filename)


@app.post("/api/import/commit")
def import_commit(req: ImportRequest):
    """Terapkan impor: baris valid dibuat/diperbarui dalam SATU transaksi; baris bergalat dilewati."""
    _refresh_import_limits()
    return _do_import_commit(req)


# --- Impor dengan progres nyata: berjalan di thread latar, klien memantau persentase per batch ---
IMPORT_JOBS = {}
IMPORT_JOBS_LOCK = threading.Lock()
IMPORT_PROGRESS_STEP = 25   # perbarui progres setiap N baris


def _job_set(job_id, **kw):
    with IMPORT_JOBS_LOCK:
        j = IMPORT_JOBS.get(job_id)
        if j is not None:
            j.update(kw)


@app.post("/api/import/commit-async")
def import_commit_async(req: ImportRequest):
    """Mulai impor di latar belakang; kembalikan job_id untuk dipantau lewat GET /api/import/jobs/{job_id}."""
    _refresh_import_limits()
    return _start_import_job(req, None, "", None)


def _start_import_job(req, plan_kwargs, note: str, session_id):
    job_id = secrets.token_hex(8)
    owner = _current_username()
    with IMPORT_JOBS_LOCK:
        for k in [k for k, v in IMPORT_JOBS.items() if v["status"] != "running" and time.time() - v["updated"] > 600][:50]:
            IMPORT_JOBS.pop(k, None)
        if any(v["status"] == "running" and v.get("owner") == owner for v in IMPORT_JOBS.values()):
            raise HTTPException(409, "Masih ada impor yang berjalan. Tunggu sampai selesai.")
        IMPORT_JOBS[job_id] = {"job_id": job_id, "status": "running", "stage": "Memeriksa berkas", "done": 0, "total": 0,
                               "percent": 0, "started": time.time(), "updated": time.time(), "owner": owner,
                               "result": None, "error": None, "session_id": session_id}

    def progress(stage, done, total, pct=None):
        if pct is None:
            pct = int(done * 100 / total) if total else 0
        _job_set(job_id, stage=stage, done=done, total=total, percent=max(0, min(int(pct), 99)), updated=time.time())

    def run():
        try:
            res = _do_import_commit(req, progress, plan_kwargs, note)
            if session_id:
                with db() as conn:
                    conn.cursor().execute("UPDATE import_sessions SET status = 'committed', committed_at = ?, updated_at = ?, result = ? WHERE id = ?",
                                          (_now_str(), _now_str(), json.dumps(res), session_id))
                try:
                    _sess_path(session_id).unlink()
                except OSError:
                    pass
                _sess_forget(session_id)
            _job_set(job_id, status="done", stage="Selesai", percent=100, result=res, updated=time.time())
        except HTTPException as e:
            _job_set(job_id, status="error", error=str(e.detail), updated=time.time())
        except Exception as e:     # noqa: BLE001 - jangan biarkan thread mati tanpa kabar
            _job_set(job_id, status="error", error=f"Impor gagal: {e}", updated=time.time())

    ctx = contextvars.copy_context()
    threading.Thread(target=lambda: ctx.run(run), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/import/jobs/{job_id}")
def import_job(job_id: str):
    with IMPORT_JOBS_LOCK:
        j = IMPORT_JOBS.get(job_id)
        if not j or j.get("owner") != _current_username():
            raise HTTPException(404, "Proses impor tidak ditemukan")
        out = {k: v for k, v in j.items() if k not in ("owner", "started")}
        out["elapsed_s"] = round(time.time() - j["started"], 1)
    return out


def _do_import_commit(req, progress=None, plan_kwargs=None, audit_note=""):
    rep = progress or (lambda *a, **k: None)
    with db() as conn:
        cursor = conn.cursor()
        # Bobot: membaca & memeriksa data 0-40%, menyimpan 40-99%, sisanya riwayat
        rep("Membaca berkas", 0, 0, 0)
        fmt, recs, mode = _plan_import(
            cursor, req, lambda i, n: rep("Validasi akhir", i, n, 40 * i / max(n, 1)), apply_names=True, **(plan_kwargs or {}))
        total_work = sum(1 for r in recs if r["action"] in ("create", "update")) or 1
        done_work = [0]

        def tick(stage, force=False):
            done_work[0] += 0 if force else 1
            if force or done_work[0] % IMPORT_PROGRESS_STEP == 0:
                rep(stage, done_work[0], total_work, 40 + 58 * done_work[0] / total_work)
        made = {"nodes": 0, "cables": 0, "updated": 0, "linked_ends": 0, "unlinked_cables": 0}
        # 1) node dulu supaya ujung kabel bisa terhubung ke node yang baru dibuat
        for rec in [r for r in recs if r["kind"] == "NODE"]:
            if rec["action"] in ("create", "update"):
                tick("Menyimpan titik (node)")
            if rec["action"] == "create":
                ax = rec.get("asset") or {}
                cursor.execute(
                    """INSERT INTO nodes (name, type, status, latitude, longitude, cluster, area, city, capacity, spec_data,
                                          reg_code, service, bandwidth_mbps, device_sn, link_type, trunk_mbps, trunk_overbook)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, ?, ?, ?, ?, ?)""",
                    (_norm_name(rec["name"]), rec["type"], rec["status"], rec["lat"], rec["lng"], rec["cluster"], rec["area"],
                     rec["city"], rec["capacity"], *[ax.get(c) for c in ASSET_EXPORT_COLS]))
                nid = cursor.lastrowid
                rec["new_id"] = nid
                if rec.get("folder_path"):
                    cursor.execute("UPDATE nodes SET folder_path = ? WHERE id = ?", (rec["folder_path"], nid))
                made["nodes"] += 1
                _audit(cursor, "CREATE", "NODE", nid, rec["name"], f"Tambah {rec['type']} {rec['name']} (impor {req.filename})" + (f"; nama asal: {rec['orig_name']}" if rec.get("orig_name") else ""),
                       snapshot=_row_dict(cursor.execute("SELECT * FROM nodes WHERE id = ?", (nid,)).fetchone()))
            elif rec["action"] == "update":
                _import_update(cursor, "NODE", rec, made, req.filename)
        nodes_for_link = [dict(r) for r in cursor.execute(
            "SELECT id, type, latitude, longitude FROM nodes WHERE type NOT IN ('INCIDENT', 'TIANG', 'HH')").fetchall()]

        link_grid = {}      # indeks sel ~110 m: pencarian tetangga terdekat tidak lagi memindai semua node (O(n) -> O(1))
        for n in nodes_for_link:
            link_grid.setdefault((int(math.floor(n["latitude"] * 1000)), int(math.floor(n["longitude"] * 1000))), []).append(n)

        def nearest_node(lng, lat):
            best = None
            ci, cj = int(math.floor(lat * 1000)), int(math.floor(lng * 1000))
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    for n in link_grid.get((ci + di, cj + dj), ()):
                        d = _haversine_m(lat, lng, n["latitude"], n["longitude"])
                        if d <= NODE_SNAP_M and (best is None or d < best[0]):
                            best = (d, n["id"])
            return best[1] if best else None

        tick("Menyimpan kabel", force=True)
        for rec in [r for r in recs if r["kind"] == "CABLE"]:
            if rec["action"] in ("create", "update"):
                tick("Menyimpan kabel")
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
                if rec.get("folder_path"):
                    cursor.execute("UPDATE cables SET folder_path = ? WHERE id = ?", (rec["folder_path"], cid))
                made["cables"] += 1
                made["linked_ends"] += (a is not None) + (b is not None)
                if a is None and b is None:
                    made["unlinked_cables"] += 1
                _audit(cursor, "CREATE", "CABLE", cid, rec["name"],
                       f"Tambah kabel {rec['type']} {rec['name']} ({rec['capacity']}) (impor {req.filename})" + (f"; nama asal: {rec['orig_name']}" if rec.get("orig_name") else ""),
                       snapshot=_row_dict(cursor.execute("SELECT * FROM cables WHERE id = ?", (cid,)).fetchone()))
            elif rec["action"] == "update":
                _import_update(cursor, "CABLE", rec, made, req.filename)
        rep("Mencatat riwayat perubahan", total_work, total_work, 99)
        counts = {"create": made["nodes"] + made["cables"], "update": made["updated"],
                  "skip": sum(1 for r in recs if r["action"] == "skip"),
                  "error": sum(1 for r in recs if r["action"] == "error")}
        _audit(cursor, "IMPORT", "DATA", None, req.filename,
               f"Impor {fmt.upper()} '{req.filename}': {made['nodes']} node & {made['cables']} kabel dibuat, "
               f"{made['updated']} diperbarui, {counts['skip']} dilewati, {counts['error']} bergalat" + (f" ({audit_note})" if audit_note else ""))
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
    if kind == "NODE":
        new.update({k: v for k, v in (rec.get("asset") or {}).items() if v is not None})   # kolom kosong tidak menghapus data lama
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
# ROUND 14 - BATAS IMPOR (ATUR ADMIN) & SESI IMPOR TERSIMPAN DENGAN KOREKSI PER BARIS
# =====================================================================================
# Berkas yang diunggah disimpan di server sebagai "sesi impor". Koreksi pengguna (ganti jenis, nama, koordinat,
# gabung ke aset yang sudah ada, atau lewati) disimpan sebagai OVERRIDE per baris, bukan mengubah berkas aslinya.
# Setiap perubahan dan penerapan akhir menjalankan validasi yang SAMA lagi dari awal terhadap data terbaru.
IMPORT_DEFAULT_MB = 8
IMPORT_DEFAULT_RECORDS = 5000
IMPORT_HARD_MAX_MB = 500
IMPORT_HARD_MAX_RECORDS = 200_000
IMPORT_DIR = BASE_DIR / "import_sessions"
IMPORT_SESSION_DAYS = 30                 # sesi terbuka tanpa aktivitas selama ini dibuang otomatis
IMPORT_PAGE_MAX = 200
_LIMITS_CHECKED = [0.0]


def _import_limit_values(s) -> tuple:
    mb, rec = IMPORT_DEFAULT_MB, IMPORT_DEFAULT_RECORDS
    if isinstance(s, dict):
        v = _num_or_none(s.get("max_mb"))
        if v is not None and 0.5 <= v <= IMPORT_HARD_MAX_MB:
            mb = v
        v = _num_or_none(s.get("max_records"))
        if v is not None and 100 <= v <= IMPORT_HARD_MAX_RECORDS:
            rec = int(v)
    return mb, rec


def _refresh_import_limits(force: bool = False):
    """Muat batas impor dari pengaturan ke variabel modul (dipakai seluruh jalur impor). Dicek paling sering tiap 2 detik."""
    global IMPORT_MAX_BYTES, IMPORT_MAX_RECORDS
    if not force and time.time() - _LIMITS_CHECKED[0] < 2.0:
        return
    s = None
    try:
        with db() as conn:
            s = _get_setting(conn.cursor(), "import_limits")
    except Exception:       # noqa: BLE001 - tabel pengaturan belum ada saat start pertama
        s = None
    mb, rec = _import_limit_values(s)
    IMPORT_MAX_BYTES, IMPORT_MAX_RECORDS = int(mb * 1_000_000), int(rec)
    _LIMITS_CHECKED[0] = time.time()


def _import_limits_payload(cursor=None) -> dict:
    saved = None
    if cursor is not None:
        saved = _get_setting(cursor, "import_limits")
    mb, rec = _import_limit_values(saved)
    return {"max_mb": mb, "max_records": rec, "max_bytes": int(mb * 1_000_000),
            "hard_max_mb": IMPORT_HARD_MAX_MB, "hard_max_records": IMPORT_HARD_MAX_RECORDS,
            "default_mb": IMPORT_DEFAULT_MB, "default_records": IMPORT_DEFAULT_RECORDS, "customized": saved is not None}


class ImportLimitsPayload(BaseModel):
    max_mb: float
    max_records: int


@app.get("/api/import/limits")
def get_import_limits():
    with db() as conn:
        return _import_limits_payload(conn.cursor())


@app.put("/api/import/limits")
def put_import_limits(payload: ImportLimitsPayload):
    if not (0.5 <= payload.max_mb <= IMPORT_HARD_MAX_MB):
        raise HTTPException(status_code=400, detail=f"Ukuran berkas maksimal harus 0,5 sampai {IMPORT_HARD_MAX_MB} MB")
    if not (100 <= payload.max_records <= IMPORT_HARD_MAX_RECORDS):
        raise HTTPException(status_code=400, detail=f"Jumlah baris maksimal harus 100 sampai {IMPORT_HARD_MAX_RECORDS:,} baris".replace(",", "."))
    with db() as conn:
        cursor = conn.cursor()
        old = _import_limits_payload(cursor)
        cursor.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES ('import_limits', ?, ?, ?) "
                       "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                       (json.dumps({"max_mb": payload.max_mb, "max_records": payload.max_records}), _now_str(), _current_username()))
        _audit(cursor, "UPDATE", "SETTING", None, "import_limits",
               f"Batas impor diubah: {old['max_mb']:g} MB / {old['max_records']} baris menjadi {payload.max_mb:g} MB / {payload.max_records} baris",
               {"max_mb": [old["max_mb"], payload.max_mb], "max_records": [old["max_records"], payload.max_records]})
        out = _import_limits_payload(cursor)
    _refresh_import_limits(True)
    return {"message": f"Batas impor disimpan: {out['max_mb']:g} MB dan {out['max_records']} baris", **out}


# ---------------------------------------------------------------- sesi impor
_SESS_RAWS: dict = {}           # {sid: (ver_file, fmt, raws)} - hasil baca berkas (tanpa override)
_SESS_PLAN: dict = {}           # {sid: {"ver", "t", "recs", "fmt", "mode"}}
_SESS_LOCK = threading.Lock()
SESS_CACHE_MAX = 3
OVR_STR = ("name", "type", "status", "capacity", "installation", "cluster", "area")


def _sess_path(sid: int) -> Path:
    return IMPORT_DIR / f"{int(sid)}.bin"


def _sess_forget(sid: int):
    with _SESS_LOCK:
        _SESS_RAWS.pop(sid, None)
        _SESS_PLAN.pop(sid, None)


def _sess_cache_put(store: dict, sid: int, val):
    with _SESS_LOCK:
        store[sid] = val
        while len(store) > SESS_CACHE_MAX:
            store.pop(next(iter(store)))


def _sess_get(cursor, sid: int, need_open: bool = True):
    r = cursor.execute("SELECT * FROM import_sessions WHERE id = ?", (sid,)).fetchone()
    u = CURRENT_USER.get()
    if not r or not u or (r["owner"] != u["username"] and "user.manage" not in ROLE_PERMS.get(u["role"], set())):
        raise HTTPException(status_code=404, detail="Sesi impor tidak ditemukan")
    if need_open and r["status"] != "open":
        raise HTTPException(status_code=409, detail="Sesi impor ini sudah ditutup (" + {"committed": "sudah diterapkan", "discarded": "dibuang"}.get(r["status"], r["status"]) + ")")
    return r


def _sess_cleanup(cursor):
    """Buang sesi terbuka yang lama tak disentuh (dan berkasnya)."""
    old = (datetime.now(timezone.utc) - timedelta(days=IMPORT_SESSION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    for r in cursor.execute("SELECT id FROM import_sessions WHERE status = 'open' AND updated_at < ?", (old,)).fetchall():
        cursor.execute("UPDATE import_sessions SET status = 'discarded', updated_at = ? WHERE id = ?", (_now_str(), r["id"]))
        try:
            _sess_path(r["id"]).unlink()
        except OSError:
            pass
        _sess_forget(r["id"])


def _sess_raws(srow):
    """(fmt, raws) dari berkas sesi; di-cache. Tiap baris diberi 'rid' (urutan) sebagai kunci koreksi yang stabil."""
    sid = srow["id"]
    with _SESS_LOCK:
        c = _SESS_RAWS.get(sid)
    if c:
        return c[0], c[1]
    p = _sess_path(sid)
    try:
        data = p.read_bytes()
    except OSError:
        raise HTTPException(status_code=410, detail="Berkas sesi impor sudah tidak ada di server; unggah ulang berkasnya")
    fmt, raws = _extract_import(srow["filename"], "", "", raw=data)
    for i, rw in enumerate(raws):
        rw["rid"] = i
    _sess_cache_put(_SESS_RAWS, sid, (fmt, raws))
    return fmt, raws


def _apply_raw_overrides(cursor, raws, overrides):
    """Terapkan koreksi pengguna ke data mentah sebelum divalidasi (salinan; berkas & cache asli tidak berubah)."""
    if not overrides:
        return raws
    out = list(raws)
    for k, ov in overrides.items():
        try:
            i = int(k)
        except (TypeError, ValueError):
            continue
        if not (0 <= i < len(out)) or not isinstance(ov, dict):
            continue
        rw = dict(out[i])
        p = _canon_props(rw.get("props"))
        for f in OVR_STR:
            if ov.get(f) not in (None, ""):
                p[f] = ov[f]
        if ov.get("name"):
            rw["part"] = None
        has_ll = ov.get("lat") is not None and ov.get("lng") is not None
        if has_ll and rw.get("geom") in ("point", "unsupported"):
            rw["geom"], rw["coords"] = "point", [ov["lng"], ov["lat"]]
        if ov.get("action") == "merge" and ov.get("merge_id"):
            line = rw.get("geom") in ("line", "line_text")
            tgt = cursor.execute("SELECT name, type FROM " + ("cables" if line else "nodes") + " WHERE id = ?", (int(ov["merge_id"]),)).fetchone()
            if tgt:
                p["name"], p["type"] = tgt["name"], tgt["type"]
                rw["part"] = None
        rw["props"] = p
        out[i] = rw
    return out


def _sess_plan(cursor, srow, fresh: bool = False):
    sid = srow["id"]
    ver = (srow["ver"], srow["on_duplicate"])
    with _SESS_LOCK:
        c = _SESS_PLAN.get(sid)
    if c and not fresh and c["ver"] == ver and time.time() - c["t"] < 120:
        return c
    fmt, raws = _sess_raws(srow)
    ov = json.loads(srow["overrides"] or "{}")
    fmt, recs, mode = _plan_import(cursor, ImportRequest(filename=srow["filename"], on_duplicate=srow["on_duplicate"]),
                                   overrides=ov, raws=raws, fmt=fmt)
    c = {"ver": ver, "t": time.time(), "recs": recs, "fmt": fmt, "mode": mode}
    _sess_cache_put(_SESS_PLAN, sid, c)
    return c


def _sess_counts(recs) -> dict:
    counts = {"create": 0, "update": 0, "skip": 0, "error": 0}
    by_type = {}
    for r in recs:
        counts[r["action"]] += 1
        if r["action"] in ("create", "update"):
            k = r["type"] or "?"
            by_type[k] = by_type.get(k, 0) + 1
    return {"counts": counts, "by_type": by_type, "warnings": sum(1 for r in recs if r["warnings"] and r["action"] != "error"),
            "nodes": sum(1 for r in recs if r["kind"] == "NODE"), "cables": sum(1 for r in recs if r["kind"] == "CABLE")}


def _sess_meta(srow, plan=None) -> dict:
    ov = json.loads(srow["overrides"] or "{}")
    m = {"id": srow["id"], "filename": srow["filename"], "format": srow["fmt"], "owner": srow["owner"], "status": srow["status"],
         "on_duplicate": srow["on_duplicate"], "size": srow["file_size"], "total": srow["total"], "edited": len(ov),
         "created_at": srow["created_at"], "updated_at": srow["updated_at"], "validated_at": srow["validated_at"],
         "committed_at": srow["committed_at"]}
    if srow["result"]:
        try:
            m["result"] = json.loads(srow["result"])
        except (TypeError, ValueError):
            pass
    if plan is not None:
        m.update(_sess_counts(plan["recs"]))
    elif srow["counts"]:
        try:
            m.update(json.loads(srow["counts"]))
        except (TypeError, ValueError):
            pass
    return m


def _sess_store_counts(cursor, sid, plan):
    cursor.execute("UPDATE import_sessions SET counts = ? WHERE id = ?", (json.dumps(_sess_counts(plan["recs"])), sid))


def _sess_create(filename: str, data: bytes, mode: str) -> dict:
    mode = (mode or "skip").lower()
    if mode not in ("skip", "update", "create"):
        raise HTTPException(status_code=400, detail="on_duplicate harus skip, update, atau create")
    _refresh_import_limits()
    if not data:
        raise HTTPException(status_code=400, detail="Berkas kosong")
    if len(data) > IMPORT_MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"Berkas terlalu besar (maks {IMPORT_MAX_BYTES / 1_000_000:g} MB). Admin dapat menaikkan batas ini di tab Impor.")
    fmt, raws = _extract_import(filename, "", "", raw=data)
    if not raws:
        raise HTTPException(status_code=400, detail="Tidak ada data yang bisa dibaca dari berkas")
    if len(raws) > IMPORT_MAX_RECORDS:
        raise HTTPException(status_code=413, detail=f"Terlalu banyak data ({len(raws)}); maksimal {IMPORT_MAX_RECORDS} per impor. Admin dapat menaikkan batas ini di tab Impor.")
    for i, rw in enumerate(raws):
        rw["rid"] = i
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        cursor = conn.cursor()
        _sess_cleanup(cursor)
        cursor.execute("INSERT INTO import_sessions (filename, fmt, owner, status, on_duplicate, file_size, total, overrides, ver, created_at, updated_at) "
                       "VALUES (?, ?, ?, 'open', ?, ?, ?, '{}', 0, ?, ?)",
                       (filename or "berkas", fmt, _current_username(), mode, len(data), len(raws), _now_str(), _now_str()))
        sid = cursor.lastrowid
        try:
            _sess_path(sid).write_bytes(data)
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"Berkas sesi tidak dapat disimpan di server: {e}")
        _sess_cache_put(_SESS_RAWS, sid, (fmt, raws))
        srow = cursor.execute("SELECT * FROM import_sessions WHERE id = ?", (sid,)).fetchone()
        plan = _sess_plan(cursor, srow, True)
        _sess_store_counts(cursor, sid, plan)
        _audit(cursor, "IMPORT", "DATA", sid, filename, f"Membuka sesi impor #{sid} '{filename}' ({len(raws)} baris, {len(data) // 1024} KB)")
        return _sess_meta(srow, plan)


@app.post("/api/import/sessions")
def import_session_create(req: ImportRequest):
    """Buat sesi impor dari berkas (JSON base64 / teks). Untuk berkas besar gunakan /api/import/sessions/upload."""
    _refresh_import_limits()
    if req.content_base64:
        try:
            data = base64.b64decode(req.content_base64, validate=False)
        except Exception:
            raise HTTPException(status_code=400, detail="Isi berkas (base64) tidak valid")
    else:
        data = (req.content or "").encode("utf-8")
    return _sess_create(req.filename, data, req.on_duplicate)


@app.post("/api/import/sessions/upload")
async def import_session_upload(request: Request, filename: str = "", on_duplicate: str = "skip"):
    """Unggah berkas mentah (tanpa base64): badan permintaan = isi berkas. Ukuran dicek sebelum dan selama menerima."""
    import asyncio
    _refresh_import_limits()
    limit = IMPORT_MAX_BYTES
    msg = f"Berkas terlalu besar (maks {limit / 1_000_000:g} MB). Admin dapat menaikkan batas ini di tab Impor."
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > limit:
        raise HTTPException(status_code=413, detail=msg)
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            raise HTTPException(status_code=413, detail=msg)
    ctx = contextvars.copy_context()
    loop = asyncio.get_running_loop()
    data = bytes(buf)
    return await loop.run_in_executor(None, lambda: ctx.run(_sess_create, filename, data, on_duplicate))


@app.get("/api/import/sessions")
def import_session_list(status: str = "open"):
    with db() as conn:
        cursor = conn.cursor()
        _sess_cleanup(cursor)
        u = CURRENT_USER.get()
        admin = "user.manage" in ROLE_PERMS.get(u["role"], set())
        st = status if status in ("open", "committed", "discarded", "all") else "open"
        q = "SELECT * FROM import_sessions WHERE 1=1"
        args = []
        if st != "all":
            q += " AND status = ?"; args.append(st)
        if not admin:
            q += " AND owner = ?"; args.append(u["username"])
        q += " ORDER BY id DESC LIMIT 50"
        return {"sessions": [_sess_meta(r) for r in cursor.execute(q, args).fetchall()]}


@app.get("/api/import/sessions/{sid}")
def import_session_get(sid: int):
    with db() as conn:
        cursor = conn.cursor()
        srow = _sess_get(cursor, sid, False)
        if srow["status"] != "open":
            return _sess_meta(srow)
        plan = _sess_plan(cursor, srow)
        return _sess_meta(srow, plan)


class ImportSessionPatch(BaseModel):
    on_duplicate: Optional[str] = None


@app.put("/api/import/sessions/{sid}")
def import_session_patch(sid: int, payload: ImportSessionPatch):
    mode = (payload.on_duplicate or "").lower()
    if mode not in ("skip", "update", "create"):
        raise HTTPException(status_code=400, detail="on_duplicate harus skip, update, atau create")
    with db() as conn:
        cursor = conn.cursor()
        srow = _sess_get(cursor, sid)
        cursor.execute("UPDATE import_sessions SET on_duplicate = ?, ver = ver + 1, updated_at = ? WHERE id = ?", (mode, _now_str(), sid))
        srow = _sess_get(cursor, sid)
        plan = _sess_plan(cursor, srow)
        _sess_store_counts(cursor, sid, plan)
        return _sess_meta(srow, plan)


@app.delete("/api/import/sessions/{sid}")
def import_session_discard(sid: int):
    with db() as conn:
        cursor = conn.cursor()
        srow = _sess_get(cursor, sid)
        cursor.execute("UPDATE import_sessions SET status = 'discarded', updated_at = ? WHERE id = ?", (_now_str(), sid))
        _audit(cursor, "IMPORT", "DATA", sid, srow["filename"], f"Membuang sesi impor #{sid} '{srow['filename']}' tanpa diterapkan")
    try:
        _sess_path(sid).unlink()
    except OSError:
        pass
    _sess_forget(sid)
    return {"message": "Sesi impor dibuang"}


# ---- tampilan baris
def _norm_row_orig(rw) -> dict:
    p = _canon_props(rw.get("props"))
    co = rw.get("coords") if rw.get("geom") == "point" else None
    return {"name": str(p.get("name") or ""), "type": str(p.get("type") or rw.get("folder") or ""), "status": str(p.get("status") or ""),
            "capacity": str(p.get("capacity") or ""), "installation": str(p.get("installation") or ""),
            "lat": co[1] if co and len(co) >= 2 else None, "lng": co[0] if co and len(co) >= 2 else None,
            "folder": rw.get("folder")}


def _suggest_name(name: str, taken: set):
    base = (name or "").strip()[:110]
    if not base:
        return None
    for n in range(2, 200):
        cand = f"{base}-{n}"
        if _name_key(cand) not in taken:
            return cand
    return None


def _sess_taken_names(cursor, recs) -> set:
    taken = {_name_key(r["name"]) for r in cursor.execute("SELECT name FROM nodes WHERE type != 'INCIDENT'").fetchall()}
    taken |= {_name_key(r["name"]) for r in cursor.execute("SELECT name FROM cables").fetchall()}
    taken |= {_name_key(r["name"]) for r in recs if r["action"] in ("create", "update") and r["name"]}
    return taken


def _row_view(rec, raws, ov, taken) -> dict:
    rid = rec["rid"]
    rw = raws[rid]
    v = {"rid": rid, "src": rec["src"], "kind": rec["kind"], "name": rec["name"], "type": rec["type"], "status": rec["status"],
         "capacity": rec.get("capacity"), "installation": rec.get("installation"), "latitude": rec.get("lat"), "longitude": rec.get("lng"),
         "length_m": rec.get("length_m"), "action": rec["action"], "errors": rec["errors"], "warnings": rec["warnings"],
         "existing_id": rec.get("existing_id"), "existing_name": rec.get("existing_name"), "sys_name": rec.get("sys_name"),
         "edited": bool(ov), "override": ov or {}, "orig": _norm_row_orig(rw)}
    if rec["action"] == "error" and any(("sudah dipakai" in e or "duplikat" in e.lower()) for e in rec["errors"]):
        v["suggest_name"] = _suggest_name(rec["name"], taken)
    return v


@app.get("/api/import/sessions/{sid}/rows")
def import_session_rows(sid: int, filter: str = "error", q: str = "", page: int = 1, size: int = 50, kind: str = "", reason: str = "", ids_only: int = 0):
    size = max(1, min(int(size), IMPORT_PAGE_MAX))
    page = max(1, int(page))
    with db() as conn:
        cursor = conn.cursor()
        srow = _sess_get(cursor, sid)
        plan = _sess_plan(cursor, srow)
        recs = plan["recs"]
        ov = json.loads(srow["overrides"] or "{}")
        f = (filter or "error").lower()
        qq = (q or "").strip().lower()

        def keep(r):
            if f == "error" and r["action"] != "error":
                return False
            if f == "warning" and not (r["warnings"] and r["action"] != "error"):
                return False
            if f == "edited" and str(r["rid"]) not in ov:
                return False
            if f in ("create", "update", "skip") and r["action"] != f:
                return False
            if kind and r["kind"] != kind.upper():
                return False
            if reason and reason not in r["errors"] and reason not in r["warnings"]:
                return False
            if qq and qq not in (r["name"] or "").lower() and qq not in r["src"].lower():
                return False
            return True
        hit = [r for r in recs if keep(r)]
        total = len(hit)
        if ids_only:
            return {"total": total, "rids": [r["rid"] for r in hit], "kinds": {k: sum(1 for r in hit if r["kind"] == k) for k in ("NODE", "CABLE", "?") if any(r["kind"] == k for r in hit)}}
        part = hit[(page - 1) * size: page * size]
        _, raws = _sess_raws(srow)
        taken = _sess_taken_names(cursor, recs) if any(r["action"] == "error" for r in part) else set()
        return {"filter": f, "page": page, "size": size, "total": total, "pages": max(1, -(-total // size)),
                "rows": [_row_view(r, raws, ov.get(str(r["rid"])), taken) for r in part],
                **_sess_counts(recs), "edited": len(ov)}


@app.get("/api/import/sessions/{sid}/reasons")
def import_session_reasons(sid: int, kind: str = "all"):
    """Pesan galat/peringatan yang paling sering (untuk 'terapkan ke semua baris serupa')."""
    with db() as conn:
        cursor = conn.cursor()
        srow = _sess_get(cursor, sid)
        recs = _sess_plan(cursor, srow)["recs"]
        bag = {}
        for r in recs:
            for lvl, msgs in (("error", r["errors"]), ("warning", r["warnings"])):
                for m in msgs:
                    e = bag.setdefault((lvl, m), {"level": lvl, "message": m, "count": 0, "kinds": {}})
                    e["count"] += 1
                    e["kinds"][r["kind"]] = e["kinds"].get(r["kind"], 0) + 1
        items = sorted(bag.values(), key=lambda e: (e["level"] != "error", -e["count"]))
        if kind in ("error", "warning"):
            items = [e for e in items if e["level"] == kind]
        return {"reasons": items[:100], "more": max(0, len(items) - 100)}


class ImportRowPatch(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    status: Optional[str] = None
    capacity: Optional[str] = None
    installation: Optional[str] = None
    cluster: Optional[str] = None
    area: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    action: Optional[str] = None          # create | merge | skip | "" (hapus)
    merge_id: Optional[int] = None
    reset: Optional[bool] = False


def _sent_fields(m) -> set:
    return set(getattr(m, "model_fields_set", None) or getattr(m, "__fields_set__", set()))


def _clean_override(cursor, kind: str, payload: ImportRowPatch, cur: dict) -> dict:
    """Gabungkan koreksi baru ke koreksi lama dengan validasi dasar. Nilai '' / null menghapus bidang itu."""
    out = dict(cur)
    sent = _sent_fields(payload)
    if payload.reset:
        return {}
    for f in OVR_STR + ("lat", "lng", "action", "merge_id"):
        if f not in sent:
            continue
        v = getattr(payload, f)
        if v is None or v == "":
            out.pop(f, None)
            if f == "action":
                out.pop("merge_id", None)
            continue
        if f in OVR_STR:
            v = _norm_name(v) if f == "name" else str(v).strip()
            if len(v) > 120:
                raise HTTPException(status_code=400, detail=f"Isi '{f}' terlalu panjang")
        out[f] = v
    if "type" in out:
        t = _norm_node_type(out["type"]) if kind in ("NODE", "?") else _norm_cable_type(out["type"])
        if t is None:
            raise HTTPException(status_code=400, detail=("Jenis aset tidak dikenal. Pilih: POP, CLOSURE, ODP, TIANG, HH, SLACK, PELANGGAN" if kind != "CABLE"
                                                         else "Jenis kabel tidak dikenal. Pilih: Backbone, Feeder, Distribution, Drop"))
        out["type"] = t
    if "status" in out and STATUS_ALIASES.get(str(out["status"]).upper()) not in ("Active", "Maintenance"):
        raise HTTPException(status_code=400, detail="Status impor hanya Active atau Maintenance (status gangguan lewat tiket)")
    if "status" in out:
        out["status"] = STATUS_ALIASES[str(out["status"]).upper()]
    if "installation" in out:
        i = INSTALL_ALIASES.get(str(out["installation"]).upper())
        if i is None:
            raise HTTPException(status_code=400, detail="Pemasangan harus Udara atau Tanah")
        out["installation"] = i
    if ("lat" in out) != ("lng" in out) or ("lat" in out and not _valid_ll(out["lat"], out["lng"])):
        raise HTTPException(status_code=400, detail="Koordinat harus lengkap (lat dan lng) dan berada dalam rentang yang sah")
    if "lat" in out and kind == "CABLE":
        raise HTTPException(status_code=400, detail="Koordinat jalur kabel tidak dapat diedit per titik; gambar ulang di peta bila jalurnya salah")
    if "installation" in out and kind == "NODE":
        raise HTTPException(status_code=400, detail="Pemasangan hanya untuk kabel")
    act = out.get("action")
    if act is not None and act not in ("create", "merge", "skip"):
        raise HTTPException(status_code=400, detail="Aksi harus create, merge, atau skip")
    if act == "merge":
        if kind == "?" and "lat" not in out:
            raise HTTPException(status_code=400, detail="Baris ini tidak punya geometri yang didukung; isi koordinat agar dibaca sebagai titik")
        tbl = "cables" if kind == "CABLE" else "nodes"
        mid = out.get("merge_id")
        if not mid or not cursor.execute(f"SELECT 1 FROM {tbl} WHERE id = ?" + (" AND type != 'INCIDENT'" if tbl == "nodes" else ""), (int(mid),)).fetchone():
            raise HTTPException(status_code=400, detail="Pilih aset tujuan gabung yang sudah ada (" + ("kabel" if kind == "CABLE" else "titik") + ")")
    else:
        out.pop("merge_id", None)
    return out


@app.put("/api/import/sessions/{sid}/rows/{rid}")
def import_session_row(sid: int, rid: int, payload: ImportRowPatch):
    with db() as conn:
        cursor = conn.cursor()
        srow = _sess_get(cursor, sid)
        plan = _sess_plan(cursor, srow)
        if not (0 <= rid < len(plan["recs"])):
            raise HTTPException(status_code=404, detail="Baris tidak ditemukan")
        kind = plan["recs"][rid]["kind"]
        # 'kind' bisa berubah oleh koreksi sebelumnya; gunakan jenis geometri asli untuk '?' tanpa koordinat
        ov = json.loads(srow["overrides"] or "{}")
        new = _clean_override(cursor, kind, payload, ov.get(str(rid)) or {})
        if new:
            ov[str(rid)] = new
        else:
            ov.pop(str(rid), None)
        cursor.execute("UPDATE import_sessions SET overrides = ?, ver = ver + 1, updated_at = ?, validated_at = NULL WHERE id = ?",
                       (json.dumps(ov), _now_str(), sid))
        srow = _sess_get(cursor, sid)
        plan = _sess_plan(cursor, srow)          # validasi ulang seluruh data terhadap data terbaru
        _sess_store_counts(cursor, sid, plan)
        _, raws = _sess_raws(srow)
        taken = _sess_taken_names(cursor, plan["recs"]) if plan["recs"][rid]["action"] == "error" else set()
        return {"row": _row_view(plan["recs"][rid], raws, new, taken), "session": _sess_meta(srow, plan)}


class ImportBulk(BaseModel):
    where: dict = {}
    set: dict = {}
    dry: Optional[bool] = False
    restore: Optional[dict] = None      # {rid: koreksi_sebelumnya} untuk membatalkan (undo) penerapan terakhir


BULK_SET_FIELDS = ("type", "status", "capacity", "installation", "cluster", "area", "action")


@app.post("/api/import/sessions/{sid}/bulk")
def import_session_bulk(sid: int, payload: ImportBulk):
    """Terapkan koreksi yang sama ke semua baris yang cocok. where: status(error|warning|any), reason, type, kind, folder, q."""
    w = payload.where or {}
    st = payload.set or {}
    bad = [k for k in st if k not in BULK_SET_FIELDS]
    if (bad or not st) and payload.restore is None:
        raise HTTPException(status_code=400, detail="Koreksi massal hanya untuk: " + ", ".join(BULK_SET_FIELDS))
    if st.get("action") not in (None, "skip", "create"):
        raise HTTPException(status_code=400, detail="Aksi massal hanya 'skip' atau 'create' (gabung butuh pilihan aset per baris)")
    with db() as conn:
        cursor = conn.cursor()
        srow = _sess_get(cursor, sid)
        if payload.restore is not None:
            ov = json.loads(srow["overrides"] or "{}")
            n_ = 0
            for k_, val in payload.restore.items():
                if not str(k_).isdigit():
                    continue
                if val:
                    ov[str(k_)] = {a: b for a, b in dict(val).items() if a in ("name", "type", "status", "capacity", "installation", "cluster", "area", "lat", "lng", "action", "merge_id")}
                else:
                    ov.pop(str(k_), None)
                n_ += 1
            cursor.execute("UPDATE import_sessions SET overrides = ?, ver = ver + 1, updated_at = ?, validated_at = NULL WHERE id = ?",
                           (json.dumps(ov), _now_str(), sid))
            srow = _sess_get(cursor, sid)
            plan = _sess_plan(cursor, srow)
            _sess_store_counts(cursor, sid, plan)
            return {"restored": n_, "session": _sess_meta(srow, plan)}
        plan = _sess_plan(cursor, srow)
        _, raws = _sess_raws(srow)
        recs = plan["recs"]
        rid_set = None
        if w.get("rids") is not None:
            rid_set = {int(x) for x in w["rids"] if str(x).lstrip("-").isdigit()}
        level = str(w.get("status") or ("any" if rid_set is not None else "error"))
        qq = str(w.get("q") or "").strip().lower()

        def match(r):
            if rid_set is not None and r["rid"] not in rid_set:
                return False
            if level == "error" and r["action"] != "error":
                return False
            if level == "warning" and not (r["warnings"] and r["action"] != "error"):
                return False
            if w.get("kind") and r["kind"] != str(w["kind"]).upper():
                return False
            if w.get("reason") and w["reason"] not in r["errors"] and w["reason"] not in r["warnings"]:
                return False
            if w.get("type") is not None and (r["type"] or "") != str(w["type"]):
                return False
            if w.get("folder") is not None and str(raws[r["rid"]].get("folder") or "") != str(w["folder"]):
                return False
            if qq and qq not in (r["name"] or "").lower():
                return False
            return True
        hit = [r for r in recs if match(r)]
        ov = json.loads(srow["overrides"] or "{}")
        apply, ignored = [], []
        for r in hit:
            cand = {}
            try:
                cand = _clean_override(cursor, r["kind"], ImportRowPatch(**{k: v for k, v in st.items()}), ov.get(str(r["rid"])) or {})
            except HTTPException:
                ignored.append(r)           # mis. jenis titik diterapkan ke kabel
                continue
            if r["kind"] == "?" and st.get("action") != "skip":
                ignored.append(r)
                continue
            apply.append((r, cand))
        res = {"matched": len(hit), "applied": len(apply), "ignored": len(ignored),
               "sample": [r["name"] or r["src"] for r, _ in apply[:5]],
               "before": {str(r["rid"]): (ov.get(str(r["rid"])) or {}) for r, _ in apply} if not payload.dry and len(apply) <= 5000 else {}}
        if payload.dry:
            return res
        for r, cand in apply:
            ov[str(r["rid"])] = cand
        cursor.execute("UPDATE import_sessions SET overrides = ?, ver = ver + 1, updated_at = ?, validated_at = NULL WHERE id = ?",
                       (json.dumps(ov), _now_str(), sid))
        srow = _sess_get(cursor, sid)
        plan = _sess_plan(cursor, srow)
        _sess_store_counts(cursor, sid, plan)
        return {**res, "session": _sess_meta(srow, plan)}


@app.post("/api/import/sessions/{sid}/validate")
def import_session_validate(sid: int):
    """Validasi ulang penuh dari awal terhadap data terbaru (sebelum diterapkan)."""
    _refresh_import_limits()
    with db() as conn:
        cursor = conn.cursor()
        srow = _sess_get(cursor, sid)
        plan = _sess_plan(cursor, srow, True)
        cursor.execute("UPDATE import_sessions SET validated_at = ? WHERE id = ?", (_now_str(), sid))
        _sess_store_counts(cursor, sid, plan)
        srow = _sess_get(cursor, sid)
        return _sess_meta(srow, plan)


@app.post("/api/import/sessions/{sid}/commit")
def import_session_commit(sid: int):
    """Terapkan sesi di latar belakang (validasi akhir ke-2 dijalankan di awal); pantau lewat /api/import/jobs/{job_id}."""
    _refresh_import_limits()
    with db() as conn:
        cursor = conn.cursor()
        srow = _sess_get(cursor, sid)
        fmt, raws = _sess_raws(srow)
        ov = json.loads(srow["overrides"] or "{}")
        req = ImportRequest(filename=srow["filename"], on_duplicate=srow["on_duplicate"])
    note = f"sesi #{sid}" + (f", {len(ov)} baris dikoreksi" if ov else "")
    return _start_import_job(req, {"overrides": ov, "raws": raws, "fmt": fmt}, note, sid)


@app.get("/api/import/sessions/{sid}/report.csv")
def import_session_report(sid: int):
    with db() as conn:
        cursor = conn.cursor()
        srow = _sess_get(cursor, sid, False)
        plan = _sess_plan(cursor, srow) if srow["status"] == "open" else None
        if plan is None:
            raise HTTPException(status_code=409, detail="Laporan hanya tersedia untuk sesi yang masih terbuka")
        ov = json.loads(srow["overrides"] or "{}")
        _, raws = _sess_raws(srow)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["baris", "sumber", "jenis_data", "nama", "jenis", "aksi", "galat", "peringatan", "koreksi_pengguna", "nilai_asal"])
        for r in plan["recs"]:
            if r["action"] == "error" or r["warnings"] or str(r["rid"]) in ov:
                o = raws[r["rid"]]
                w.writerow([r["rid"] + 1, r["src"], r["kind"], r["name"], r["type"] or "", r["action"], " | ".join(r["errors"]),
                            " | ".join(r["warnings"]), json.dumps(ov.get(str(r["rid"])) or {}, ensure_ascii=False),
                            json.dumps(_norm_row_orig(o), ensure_ascii=False)])
    safe = re.sub(r"[^A-Za-z0-9]+", "_", srow["filename"]).strip("_")[:40] or "impor"
    return Response(content=("﻿" + buf.getvalue()).encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="laporan_impor_{safe}_{sid}.csv"'})


@app.get("/api/import/assets")
def import_assets(kind: str = "NODE", q: str = "", lat: Optional[float] = None, lng: Optional[float] = None, type: str = "", limit: int = 15):
    """Cari aset yang sudah ada sebagai tujuan 'gabung' (nama mengandung q; titik diurut dari yang terdekat bila lat/lng diberikan)."""
    limit = max(1, min(int(limit), 50))
    like = f"%{(q or '').strip()}%"
    with db() as conn:
        cursor = conn.cursor()
        if (kind or "").upper() == "CABLE":
            rows = [dict(r) for r in cursor.execute("SELECT id, name, type, status, capacity FROM cables WHERE name LIKE ? ORDER BY name LIMIT 500", (like,)).fetchall()]
            return {"assets": rows[:limit]}
        sql = "SELECT id, name, type, status, capacity, latitude, longitude FROM nodes WHERE type != 'INCIDENT' AND name LIKE ?"
        args = [like]
        if type:
            sql += " AND type = ?"; args.append(type.upper())
        rows = [dict(r) for r in cursor.execute(sql + " ORDER BY name LIMIT 5000", args).fetchall()]
        if lat is not None and lng is not None:
            for r in rows:
                r["distance_m"] = round(_haversine_m(lat, lng, r["latitude"], r["longitude"]), 1)
            rows.sort(key=lambda r: r["distance_m"])
        return {"assets": rows[:limit]}


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
    "survey": {"500": "121", "1000": "122", "max": "123"},   # biaya survei per paket menurut panjang tarikan (<=500 m, <=1000 m, lebih)
}
# splice_per_customer: jumlah sambungan fusion per pelanggan; minimal 2 (di ODP + di roset)
# splice_direct / splice_hub: jumlah core sambungan (fusion) per core layanan untuk skenario langsung (< batas dropcore) / hub (> batas dropcore)
# parts_default: komponen BOQ yang secara bawaan hanya dihitung jasa (material disediakan sendiri); bisa diubah per baris di form BOQ
DEFAULT_BOQ_SETTINGS = {"tax_pct": 11.0, "default_region": "EKO", "splice_per_customer": 2, "splice_direct": 2, "splice_hub": 5,
                        "parts_default": {"CABLE": "jasa", "TIANG": "jasa", "CLOSURE": "jasa", "CLOSURE_ASAL": "jasa",
                                          "ODP_BARU": "jasa", "ODP_BARU_ASAL": "jasa", "SPLITTER": "jasa", "SPLITTER_ASAL": "jasa"}}
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
        if k in ("cable", "pole", "customer", "splitter", "closure", "otb", "survey") and isinstance(v, dict):
            m[k].update({str(a): str(b) for a, b in v.items() if b})
        elif k in m and isinstance(v, str) and v:
            m[k] = v
    st = dict(DEFAULT_BOQ_SETTINGS)
    st["parts_default"] = dict(st["parts_default"])
    for k, v in (_get_setting(cursor, "boq_settings") or {}).items():
        if k == "parts_default" and isinstance(v, dict):
            st["parts_default"].update({str(a): b for a, b in v.items() if b in BOQ_PARTS})
        elif k in st:
            st[k] = v
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
            for sk_, lo_, hi_ in (("splice_direct", 1, 48), ("splice_hub", 1, 48)):
                if sk_ in payload.settings:
                    v_ = _num_or_none(payload.settings[sk_])
                    if v_ is None or v_ != int(v_) or not (lo_ <= v_ <= hi_):
                        raise HTTPException(status_code=400, detail=f"'{sk_}' harus bilangan bulat {lo_}-{hi_}")
                    ns[sk_] = int(v_)
            if isinstance(payload.settings.get("parts_default"), dict):
                pdz = dict(ns.get("parts_default") or {})
                for a_, b_ in payload.settings["parts_default"].items():
                    if b_ not in BOQ_PARTS:
                        raise HTTPException(status_code=400, detail="Mode harga komponen harus both, material atau jasa")
                    pdz[str(a_)] = b_
                ns["parts_default"] = pdz
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
            if use_poles and (float(sg.get("poles_new") or 0) > 0 or sg.get("new_passive_allowed", True)):
                auto.append({"id": "TIANG" + sfx, "component": f"Tiang baru ({rules['pole_type']}){tag}",
                             "key": bmap["pole"].get(rules["pole_type"], bmap["pole_default"]), "qty": float(sg.get("poles_new") or 0)})
            if use_poles and float(sg.get("poles_existing") or 0) > 0:
                auto.append({"id": "TIANG_REUSE" + sfx, "component": f"Aksesoris tiang eksisting (dipakai ulang){tag}",
                             "key": bmap["pole_reuse"], "qty": float(sg["poles_existing"])})
            if use_slack and (float(sg.get("slack_count") or 0) > 0 or sg.get("new_passive_allowed", True)):
                auto.append({"id": "SLACK" + sfx, "component": f"Slack pada tiang{tag}", "key": bmap["slack_udara"], "qty": float(sg.get("slack_count") or 0)})
        else:
            auto.append({"id": "DUCT" + sfx, "component": f"Pipa duct PVC 100 mm{tag}", "key": bmap["duct"], "qty": round(float(sg.get("route_length_m") or 0), 1)})
            auto.append({"id": "GALIAN" + sfx, "component": f"Galian & pengurugan{tag}", "key": bmap["trench"], "qty": round(float(sg.get("route_length_m") or 0), 1)})
            if use_poles and (float(sg.get("hh_new") or 0) > 0 or sg.get("new_passive_allowed", True)):
                auto.append({"id": "HH" + sfx, "component": f"Handhole baru ({rules['hh_type']}){tag}", "key": bmap["hh"], "qty": float(sg.get("hh_new") or 0)})
            if use_slack and (float(sg.get("slack_count") or 0) > 0 or sg.get("new_passive_allowed", True)):
                auto.append({"id": "SLACK" + sfx, "component": f"Slack dalam handhole{tag}", "key": bmap["slack_tanah"], "qty": float(sg.get("slack_count") or 0)})
    splices = 0
    onc_sp = 0
    onc = s.get("origin_new_closure") if isinstance(s.get("origin_new_closure"), dict) else None
    if onc:
        csz0 = str(int(onc.get("closure_size") or 12))
        auto.append({"id": "CLOSURE_ASAL", "component": f"Closure {csz0} core baru di slack asal", "key": bmap["closure"].get(csz0), "qty": 1.0})
        splices += 2 * ncores
        onc_sp = 2 * ncores
        notes.append("Catuan berupa Slack: perlu pemasangan closure baru di titik slack" + (" + ODP baru" if onc.get("odp") else ""))
        if onc.get("odp"):
            r0 = onc.get("ratio") or "1:8"
            sk0 = bmap["splitter"].get(r0)
            auto.append({"id": "ODP_BARU_ASAL", "component": "ODP baru di slack asal (1 core dari closure)", "key": bmap["odp_new"], "qty": 1.0})
            auto.append({"id": "SPLITTER_ASAL", "component": f"Splitter {r0} untuk ODP baru di slack asal", "key": sk0 or bmap["splitter_odp"], "qty": 1.0})
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
    if f("customer") > 0 or s.get("terminate"):    # terminasi di lokasi pelanggan selalu ada, walau titik pelanggan tidak dibuat di data
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
        per_core = max(int(bset["splice_hub"] if hub else bset["splice_direct"]), int(bset["splice_per_customer"]))
        auto.append({"id": "SPLICE", "component": f"Penyambungan fusion splice ({per_core} core" + (f" x {ncores} layanan" if ncores > 1 else "") + (", skenario hub" if hub else "") + ")",
                     "key": bmap["splice"], "qty": float(per_core * (ncores if has_cores else 1) + onc_sp)})
    rl_ = f("route_length_m")
    if rl_ > 0 and bmap.get("survey"):
        sv = bmap["survey"]
        auto.append({"id": "SURVEY", "component": f"Biaya survei lokasi (tarikan {rl_:.0f} m)",
                     "key": sv["500"] if rl_ <= 500 else sv["1000"] if rl_ <= 1000 else sv["max"], "qty": 1.0})
    ov = adj.get("lines") if isinstance(adj.get("lines"), dict) else {}
    lines = []

    def price_line(lid, comp, key, qty, auto_qty, manual, parts="both", dflt="both"):
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
                "qty": qty, "auto_qty": auto_qty, "manual": manual, "parts": parts, "default_parts": dflt,
                "price_material": pm_f, "price_jasa": pj_f, "unit_price": pm_f + pj_f,
                "total_material": round(qty * pm_f), "total_jasa": round(qty * pj_f),
                "total": round(qty * (pm_f + pj_f)), "note": note}
    removed_lines = []
    for a in auto:
        o = ov.get(a["id"]) if isinstance(ov.get(a["id"]), dict) else {}
        if o.get("removed"):
            removed_lines.append({"id": a["id"], "component": a["component"], "auto_qty": a["qty"]})
            continue
        qty = _num_or_none(o.get("qty"))
        key = o.get("key") or a["key"]
        dflt = bset["parts_default"].get(re.sub(r"_S\d+$", "", a["id"]), "both")
        parts = o.get("parts") if o.get("parts") in BOQ_PARTS else dflt
        lines.append(price_line(a["id"], a["component"], key, a["qty"] if qty is None else qty, a["qty"],
                                qty is not None or bool(o.get("key")) or parts != dflt, parts, dflt))
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
            "warnings": warnings, "installation": inst, "notes": notes, "removed": removed_lines,
            "estimate_note": "Estimasi di luar transportasi dan biaya lainnya."}


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
                ["Dibuat", _now_str()], ["Catatan", res.get("estimate_note") or ""]]
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
# LAPORAN PDF RENCANA PASANG BARU (ASPLAN)  -  Pratinjau / Draft / Terwujud
# Versi "lapangan" (tanpa harga, untuk tim lapangan) dan "lengkap" (memuat BOQ KHS + PPN)
# =====================================================================================
PDF_VARIANTS = ("lapangan", "lengkap")


def _pdf_txt(v) -> str:
    """Teks aman untuk font bawaan PDF (Latin-1): ganti tanda panah dsb."""
    s = "" if v is None else str(v)
    for a, b in (("→", "->"), ("←", "<-"), ("−", "-"), ("≤", "<="), ("≥", ">="), ("×", "x"),
                 ("–", "-"), ("—", "-"), ("•", "-"), ("≈", "~"), ("✕", "x")):
        s = s.replace(a, b)
    return s.encode("cp1252", "replace").decode("cp1252")


def _pdf_local(value, tz_min: int = 0) -> str:
    """'YYYY-MM-DD HH:MM:SS' (UTC) -> 'DD-MM-YYYY HH:MM' pada zona perangkat (menit dari UTC)."""
    try:
        d = datetime.strptime(str(value)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S") + timedelta(minutes=tz_min)
    except (ValueError, TypeError):
        return str(value or "-")
    sg = "+" if tz_min >= 0 else "-"
    return d.strftime("%d-%m-%Y %H:%M") + f" (UTC{sg}{abs(tz_min) // 60:g})"


def _pdf_rp(n) -> str:
    return "Rp " + f"{int(round(n or 0)):,}".replace(",", ".")


def _pdf_fm(m) -> str:
    try:
        m = float(m)
    except (TypeError, ValueError):
        return "-"
    return f"{m / 1000:.2f} km" if m >= 1000 else f"{m:.0f} m"


def _pdf_map_drawing(plan: dict, width: float, height: float):
    """Peta skematik rute (bukan peta dasar): jalur per segmen, titik asal/tujuan/singgah/hub, tiang/HH/slack."""
    from reportlab.graphics.shapes import Drawing, Line, Circle, Rect, String, PolyLine, Polygon
    from reportlab.lib import colors
    o, d = plan.get("origin") or {}, plan.get("dest") or {}
    pts = []
    for sg in plan.get("segments") or []:
        pts += [(c[1], c[0]) for c in (sg.get("coords") or [])]
    if not pts:
        pts = [(c[1], c[0]) for c in ((plan.get("route") or {}).get("coords") or [])]
    for p in (o, d):
        if p.get("lat") is not None:
            pts.append((p["lat"], p["lng"]))
    hub = plan.get("hub")
    if hub:
        pts.append((hub["latitude"], hub["longitude"]))
    dr = Drawing(width, height)
    dr.add(Rect(0, 0, width, height, strokeColor=colors.HexColor("#cbd5e1"), fillColor=colors.HexColor("#f8fafc"), strokeWidth=0.8))
    if not pts:
        dr.add(String(width / 2, height / 2, "Rute tidak tersedia", textAnchor="middle", fontName="Helvetica", fontSize=9))
        return dr
    lat0 = sum(p[0] for p in pts) / len(pts)
    kx = 111320.0 * math.cos(math.radians(lat0))
    ky = 110540.0
    xs = [p[1] * kx for p in pts]
    ys = [p[0] * ky for p in pts]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    spanx, spany = max(maxx - minx, 30.0), max(maxy - miny, 30.0)
    pad = 38
    sc = min((width - 2 * pad) / spanx, (height - 2 * pad) / spany)
    offx = (width - spanx * sc) / 2
    offy = (height - spany * sc) / 2

    def P(lat, lng):
        return (offx + (lng * kx - minx) * sc, offy + (lat * ky - miny) * sc)
    palette = ["#2563eb", "#16a34a", "#9333ea", "#ea580c"]
    for i, sg in enumerate(plan.get("segments") or []):
        flat = []
        for c in sg.get("coords") or []:
            x, y = P(c[1], c[0])
            flat += [x, y]
        if len(flat) >= 4:
            dr.add(PolyLine(flat, strokeColor=colors.HexColor(palette[i % 4]), strokeWidth=2.2))
    for a in plan.get("assets") or []:
        x, y = P(a["latitude"], a["longitude"])
        k = a.get("kind")
        if k == "TIANG":
            dr.add(Circle(x, y, 2.4, fillColor=colors.HexColor("#475569"), strokeColor=colors.white, strokeWidth=0.4))
        elif k == "HH":
            dr.add(Rect(x - 2.6, y - 2.6, 5.2, 5.2, fillColor=colors.HexColor("#0f766e"), strokeColor=colors.white, strokeWidth=0.4))
        else:
            dr.add(Polygon([x, y + 3.6, x + 3.2, y, x, y - 3.6, x - 3.2, y], fillColor=colors.HexColor("#f59e0b"), strokeColor=colors.white, strokeWidth=0.4))
    for v in plan.get("via") or []:
        x, y = P(v[0], v[1])
        dr.add(Circle(x, y, 3.6, fillColor=colors.HexColor("#0ea5e9"), strokeColor=colors.white, strokeWidth=0.8))
    if hub:
        x, y = P(hub["latitude"], hub["longitude"])
        dr.add(Rect(x - 5, y - 5, 10, 10, fillColor=colors.HexColor("#f97316"), strokeColor=colors.white, strokeWidth=1))
        dr.add(String(x + 8, y - 3, "Hub / Closure", fontName="Helvetica", fontSize=7.5, fillColor=colors.HexColor("#9a3412")))
    if o.get("lat") is not None:
        x, y = P(o["lat"], o["lng"])
        dr.add(Circle(x, y, 5.5, fillColor=colors.HexColor("#16a34a"), strokeColor=colors.white, strokeWidth=1.2))
        dr.add(String(x + 8, y + 4, _pdf_txt("Asal: " + str(o.get("name") or "")), fontName="Helvetica", fontSize=8, fillColor=colors.HexColor("#14532d")))
    if d.get("lat") is not None:
        x, y = P(d["lat"], d["lng"])
        dr.add(Circle(x, y, 5.5, fillColor=colors.HexColor("#dc2626"), strokeColor=colors.white, strokeWidth=1.2))
        dr.add(String(x + 8, y - 11, _pdf_txt("Tujuan: " + str(d.get("name") or "")), fontName="Helvetica", fontSize=8, fillColor=colors.HexColor("#7f1d1d")))
    # skala
    target = (width - 2 * pad) * 0.28 / sc
    mag = 10 ** math.floor(math.log10(max(target, 1)))
    nice = next((m * mag for m in (1, 2, 5, 10) if m * mag >= target * 0.6), mag)
    L = nice * sc
    dr.add(Line(12, 12, 12 + L, 12, strokeWidth=1.6, strokeColor=colors.black))
    dr.add(Line(12, 9, 12, 15, strokeWidth=1, strokeColor=colors.black))
    dr.add(Line(12 + L, 9, 12 + L, 15, strokeWidth=1, strokeColor=colors.black))
    dr.add(String(12 + L / 2, 17, f"{nice:g} m" if nice < 1000 else f"{nice / 1000:g} km", textAnchor="middle", fontName="Helvetica", fontSize=7.5))
    # panah utara
    nx, ny = width - 18, height - 36
    dr.add(Polygon([nx, ny + 18, nx - 5, ny, nx + 5, ny], fillColor=colors.HexColor("#334155"), strokeColor=colors.HexColor("#334155")))
    dr.add(String(nx, ny + 21, "U", textAnchor="middle", fontSize=8, fontName="Helvetica-Bold"))
    # legenda
    ly = height - 12
    for i, (lab, col) in enumerate((("Tiang", "#475569"), ("Handhole", "#0f766e"), ("Slack", "#f59e0b"), ("Titik singgah", "#0ea5e9"))):
        lx = 10 + i * 84
        dr.add(Circle(lx, ly + 2.5, 3, fillColor=colors.HexColor(col), strokeColor=colors.white))
        dr.add(String(lx + 6, ly, lab, fontName="Helvetica", fontSize=7.5, fillColor=colors.HexColor("#334155")))
    return dr


# ---------------------------------------------------------------------------------
# PETA RUTE PADA PDF ASPLAN: peta dasar (tile) + titik bernomor yang sama dengan tabel poin
# ---------------------------------------------------------------------------------
PDF_BASEMAPS = ("osm", "satelit", "off")
_MERC_R = 6378137.0
_TILE_CACHE: "dict" = {}
_TILE_CACHE_MAX = 600
_TILE_DOWN_UNTIL = 0.0
_TILE_LOCK = threading.Lock()


def _tile_cfg(kind: str):
    """(url_template, atribusi). Dapat diganti lewat variabel lingkungan (tile server internal / penyedia lain)."""
    if kind == "satelit":
        return (os.environ.get("NETGIS_TILE_URL_SAT", "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"),
                os.environ.get("NETGIS_TILE_ATTR_SAT", "Imagery (c) Esri, Maxar, Earthstar Geographics"))
    return (os.environ.get("NETGIS_TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png"),
            os.environ.get("NETGIS_TILE_ATTR", "(c) OpenStreetMap contributors"))


def _pdf_basemap_mode(v: Optional[str]) -> str:
    v = (v or "osm").lower()
    if os.environ.get("NETGIS_PDF_BASEMAP", "").lower() == "off":
        return "off"
    if v not in PDF_BASEMAPS:
        raise HTTPException(status_code=400, detail="Peta dasar PDF tidak dikenal (osm | satelit | off)")
    return v


def _merc(lat: float, lng: float):
    lat = max(min(lat, 85.0511), -85.0511)
    return (_MERC_R * math.radians(lng), _MERC_R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)))


def _merc_lat(y: float) -> float:
    return math.degrees(2 * math.atan(math.exp(y / _MERC_R)) - math.pi / 2)


def _fetch_tile(url_tpl: str, z: int, x: int, y: int, deadline: float):
    n = 1 << z
    if y < 0 or y >= n:
        return None
    x %= n
    key = (url_tpl, z, x, y)
    with _TILE_LOCK:
        hit = _TILE_CACHE.get(key)
    if hit is not None:
        return hit
    left = deadline - time.time()
    if left <= 0.3:
        return None
    try:
        req = urllib.request.Request(url_tpl.format(z=z, x=x, y=y), headers={"User-Agent": "NETGIS-Enterprise/1.0 (laporan asplan PDF)"})
        with urllib.request.urlopen(req, timeout=min(5.0, left)) as r:
            data = r.read(600_000)
    except Exception:
        return None
    if not data:
        return None
    with _TILE_LOCK:
        if len(_TILE_CACHE) >= _TILE_CACHE_MAX:
            for k in list(_TILE_CACHE)[:_TILE_CACHE_MAX // 4]:
                _TILE_CACHE.pop(k, None)
        _TILE_CACHE[key] = data
    return data


def _basemap_jpeg(kind: str, xmin: float, ymin: float, xmax: float, ymax: float):
    """Gambar peta dasar yang tepat menutupi kotak pandang (koordinat Mercator, meter).
    Hasil: {'jpeg': bytes, 'attr': str, 'missing': n, 'zoom': z} atau None bila tidak ada tile sama sekali / pustaka gambar tidak ada."""
    global _TILE_DOWN_UNTIL
    if kind == "off" or time.time() < _TILE_DOWN_UNTIL:
        return None
    try:
        from PIL import Image
    except ImportError:
        return None
    url_tpl, attr = _tile_cfg(kind)
    full = 2 * math.pi * _MERC_R
    z = 1
    for zz in range(18, 0, -1):
        ppm = 256 * (1 << zz) / full
        if (xmax - xmin) * ppm <= 1500 and (ymax - ymin) * ppm <= 1100:
            z = zz
            break
    ppm = 256 * (1 << z) / full
    half = math.pi * _MERC_R
    pxl, pxr = (xmin + half) * ppm, (xmax + half) * ppm
    pyt, pyb = (half - ymax) * ppm, (half - ymin) * ppm
    tx0, tx1 = int(pxl // 256), int(pxr // 256)
    ty0, ty1 = int(pyt // 256), int(pyb // 256)
    if (tx1 - tx0 + 1) * (ty1 - ty0 + 1) > 60:
        return None
    deadline = time.time() + 14.0
    jobs = [(tx, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]
    got = {}
    from concurrent.futures import ThreadPoolExecutor
    ex = ThreadPoolExecutor(max_workers=6)
    try:
        futs = {ex.submit(_fetch_tile, url_tpl, z, tx, ty, deadline): (tx, ty) for tx, ty in jobs}
        for f, k in futs.items():
            try:
                d = f.result(timeout=max(0.5, deadline - time.time() + 1))
            except Exception:
                d = None
            if d:
                got[k] = d
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    if not got:
        _TILE_DOWN_UNTIL = time.time() + 45          # jaringan/penyedia tidak tersedia: jangan menunggu lagi untuk peta berikutnya
        return None
    canvas_im = Image.new("RGB", ((tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256), (226, 232, 240))
    missing = 0
    for (tx, ty) in jobs:
        d = got.get((tx, ty))
        if not d:
            missing += 1
            continue
        try:
            im = Image.open(io.BytesIO(d)).convert("RGB")
            canvas_im.paste(im, ((tx - tx0) * 256, (ty - ty0) * 256))
        except Exception:
            missing += 1
    box = (pxl - tx0 * 256, pyt - ty0 * 256, pxr - tx0 * 256, pyb - ty0 * 256)
    crop = canvas_im.crop(tuple(int(round(v)) for v in box))
    out = io.BytesIO()
    crop.save(out, "JPEG", quality=82)
    return {"jpeg": out.getvalue(), "attr": attr, "missing": missing, "zoom": z, "tiles": len(jobs)}


def _along_on(coords_ll, lat, lng) -> float:
    """Jarak sepanjang polyline (m) dari titik awal ke proyeksi titik (lat,lng); coords_ll = [(lat,lng), ...]."""
    if len(coords_ll) < 2:
        return 0.0
    kx = 111320.0 * math.cos(math.radians(lat))
    ky = 110540.0
    best, best_along, cum = None, 0.0, 0.0
    for (a_lat, a_lng), (b_lat, b_lng) in zip(coords_ll, coords_ll[1:]):
        ax, ay = (a_lng - lng) * kx, (a_lat - lat) * ky
        bx, by = (b_lng - lng) * kx, (b_lat - lat) * ky
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / L2))
        px, py = ax + t * dx, ay + t * dy
        d2 = px * px + py * py
        seg = math.sqrt(L2)
        if best is None or d2 < best:
            best, best_along = d2, cum + t * seg
        cum += seg
    return best_along


def _plan_points(plan: dict) -> list:
    """Daftar titik rute BERNOMOR (dipakai peta dan tabel poin agar nomornya sama), urut dari asal ke tujuan."""
    o, d = plan.get("origin") or {}, plan.get("dest") or {}
    route = []
    for sg in plan.get("segments") or []:
        route += [(c[1], c[0]) for c in (sg.get("coords") or [])]
    if len(route) < 2:
        route = [(c[1], c[0]) for c in ((plan.get("route") or {}).get("coords") or [])]
    total = 0.0
    for (a, b) in zip(route, route[1:]):
        total += math.hypot((b[1] - a[1]) * 111320.0 * math.cos(math.radians(a[0])), (b[0] - a[0]) * 110540.0)
    mid = []
    for i, v in enumerate(plan.get("via") or [], 1):
        mid.append({"kind": "SINGGAH", "lat": v[0], "lng": v[1], "info": f"Titik singgah {i}", "along": _along_on(route, v[0], v[1]), "rank": 1})
    hub = plan.get("hub")
    if hub:
        mid.append({"kind": "HUB", "lat": hub["latitude"], "lng": hub["longitude"],
                    "info": f"Closure {hub.get('closure_size', '')} core" + (f" + ODP baru {hub['odp']['ratio']}" if hub.get("odp") else ""),
                    "along": _along_on(route, hub["latitude"], hub["longitude"]), "rank": 1})
    for i, bd in enumerate(((plan.get("route") or {}).get("bends")) or [], 1):
        mid.append({"kind": "BELOKAN", "lat": bd[0], "lng": bd[1], "info": f"Titik belokan {i} (jalur disunting manual)", "along": _along_on(route, bd[0], bd[1]), "rank": 1})
    nseg = len(plan.get("segments") or [])
    for a in plan.get("assets") or []:
        kind = a.get("kind") or "TIANG"
        reuse = bool(a.get("existing_id"))
        info = ("Pakai ulang (survei): " + str(a.get("existing_name") or a.get("existing_id"))) if reuse else "Baru"
        if a.get("segment") and nseg > 1:
            info += f" ({a['segment']})"
        mid.append({"kind": kind, "lat": a["latitude"], "lng": a["longitude"], "info": info, "reuse": reuse,
                    "along": float(a.get("distance_m") or 0.0), "rank": 2})
    mid.sort(key=lambda m: (m["along"], m["rank"]))
    items = []
    if o.get("lat") is not None:
        items.append({"kind": "ASAL", "lat": o["lat"], "lng": o["lng"], "info": str(o.get("name") or "-"), "along": 0.0})
    items += mid
    if d.get("lat") is not None:
        items.append({"kind": "TUJUAN", "lat": d["lat"], "lng": d["lng"], "info": str(d.get("name") or "-"),
                      "along": float((plan.get("summary") or {}).get("route_length_m") or total)})
    for i, it in enumerate(items, 1):
        it["no"] = i
        it.setdefault("reuse", False)
    return items


_PDF_KIND_STYLE = {   # warna isi, bentuk
    "ASAL": ("#16a34a", "c"), "TUJUAN": ("#dc2626", "c"), "SINGGAH": ("#0284c7", "c"), "HUB": ("#ea580c", "s"),
    "TIANG": ("#475569", "c"), "HH": ("#0f766e", "s"), "SLACK": ("#d97706", "d"), "BELOKAN": ("#2563eb", "c"),
}


def _pdf_route_map(plan: dict, items: list, width: float, map_h: float, *, basemap: str = "osm",
                   subset=None, number_all: bool = True, title_note: str = ""):
    """Flowable peta rute. subset = daftar titik yang jadi fokus (peta detail); number_all=False -> hanya titik kunci yang bernomor.
    Mengembalikan (flowable, info)."""
    from reportlab.graphics.shapes import Drawing, Line, Circle, Rect, String, Polygon, PolyLine
    from reportlab.graphics import renderPDF
    from reportlab.lib import colors
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import Flowable

    focus = subset if subset else items
    legend_h = 20.0
    H = map_h + legend_h
    route_ll = []                        # per segmen: [(lat,lng)]
    for sg in plan.get("segments") or []:
        pts = [(c[1], c[0]) for c in (sg.get("coords") or [])]
        if len(pts) >= 2:
            route_ll.append(pts)
    if not route_ll:
        pts = [(c[1], c[0]) for c in ((plan.get("route") or {}).get("coords") or [])]
        if len(pts) >= 2:
            route_ll.append(pts)

    # kotak pandang (Mercator): peta penuh = semua titik + rute; peta detail = titik fokus saja
    pm = [_merc(it["lat"], it["lng"]) for it in focus]
    if not subset:
        for seg in route_ll:
            pm += [_merc(a, b) for a, b in seg]
    drawing = Drawing(width, H)
    if not pm:
        drawing.add(String(width / 2, H / 2, "Rute tidak tersedia", textAnchor="middle", fontName="Helvetica", fontSize=9))
        return _MapFlow(drawing, None, map_h), {"basemap": False}
    xs, ys = [p[0] for p in pm], [p[1] for p in pm]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    lat_mid = _merc_lat(cy)
    k_real = math.cos(math.radians(lat_mid))            # meter nyata = meter Mercator * k_real
    spx = max((max(xs) - min(xs)) * 1.16, 70.0 / k_real)
    spy = max((max(ys) - min(ys)) * 1.16, 70.0 / k_real)
    aspect = width / map_h
    if spx / spy < aspect:
        spx = spy * aspect
    else:
        spy = spx / aspect
    xmin, xmax, ymin, ymax = cx - spx / 2, cx + spx / 2, cy - spy / 2, cy + spy / 2
    sc = width / spx                                      # titik PDF per meter Mercator

    def XY(lat, lng):
        mx, my = _merc(lat, lng)
        return ((mx - xmin) * sc, (my - ymin) * sc)

    bm = _basemap_jpeg(basemap, xmin, ymin, xmax, ymax) if basemap != "off" else None
    if bm is None:
        drawing.add(Rect(0, 0, width, map_h, fillColor=colors.HexColor("#f8fafc"), strokeColor=colors.HexColor("#cbd5e1"), strokeWidth=0.8))
        # kisi tipis agar skematik tetap punya acuan arah
    else:
        drawing.add(Rect(0, 0, width, map_h, fillColor=None, strokeColor=colors.HexColor("#94a3b8"), strokeWidth=0.8))

    def clip_seg(x0, y0, x1, y1):
        """Liang-Barsky terhadap [0,width]x[0,map_h]."""
        dx, dy = x1 - x0, y1 - y0
        t0, t1 = 0.0, 1.0
        for p, q in ((-dx, x0), (dx, width - x0), (-dy, y0), (dy, map_h - y0)):
            if p == 0:
                if q < 0:
                    return None
            else:
                r = q / p
                if p < 0:
                    if r > t1:
                        return None
                    t0 = max(t0, r)
                else:
                    if r < t0:
                        return None
                    t1 = min(t1, r)
        return (x0 + t0 * dx, y0 + t0 * dy, x0 + t1 * dx, y0 + t1 * dy)

    palette = ["#2563eb", "#9333ea", "#16a34a", "#ea580c"]
    for casing in (True, False):
        for si, seg in enumerate(route_ll):
            col = colors.white if casing else colors.HexColor(palette[si % 4])
            sw = 5.0 if casing else 2.6
            prev = None
            for (la, ln) in seg:
                cur = XY(la, ln)
                if prev is not None:
                    c = clip_seg(prev[0], prev[1], cur[0], cur[1])
                    if c:
                        drawing.add(Line(c[0], c[1], c[2], c[3], strokeColor=col, strokeWidth=sw, strokeLineCap=1))
                prev = cur

    focus_ids = {id(it) for it in focus}
    key_kinds = ("ASAL", "TUJUAN", "SINGGAH", "HUB")
    placed = []                                          # pusat label yang sudah dipakai: (x, y, r)
    overlay = []
    namebox = []

    def marker(kind, x, y, r, reuse, num, dim=False):
        fill, shape = _PDF_KIND_STYLE.get(kind, ("#475569", "c"))
        out = colors.HexColor("#111827") if reuse else colors.white
        ow = 1.9 if reuse else 1.1
        shapes = []
        if shape == "c":
            shapes.append(Circle(x, y, r, fillColor=colors.HexColor(fill), strokeColor=out, strokeWidth=ow))
        elif shape == "s":
            shapes.append(Rect(x - r, y - r, 2 * r, 2 * r, fillColor=colors.HexColor(fill), strokeColor=out, strokeWidth=ow))
        else:
            shapes.append(Polygon([x, y + r * 1.25, x + r * 1.25, y, x, y - r * 1.25, x - r * 1.25, y], fillColor=colors.HexColor(fill), strokeColor=out, strokeWidth=ow))
        if num is not None:
            fs = 7.2 if len(str(num)) <= 2 else 6.2
            shapes.append(String(x, y - fs * 0.34, str(num), textAnchor="middle", fontName="Helvetica-Bold", fontSize=fs, fillColor=colors.white))
        return shapes

    offsets = []
    for rad in (1.0, 1.9, 2.8):
        for ang in range(0, 360, 45):
            offsets.append((rad, math.radians(ang)))
    for it in items:
        x, y = XY(it["lat"], it["lng"])
        if not (-8 <= x <= width + 8 and -8 <= y <= map_h + 8):
            continue
        infocus = id(it) in focus_ids
        numbered = infocus and (number_all or it["kind"] in key_kinds)
        if not numbered:
            if not infocus or not number_all:
                col, _ = _PDF_KIND_STYLE.get(it["kind"], ("#475569", "c"))
                overlay.append(Circle(x, y, 2.0, fillColor=colors.HexColor(col), strokeColor=colors.white, strokeWidth=0.5))
            continue
        digits = len(str(it["no"]))
        r = 7.6 if it["kind"] in key_kinds else (6.4 if digits <= 2 else 7.4)
        lx, ly = x, y
        for rad, ang in [(0.0, 0.0)] + offsets:
            tx, ty = x + math.cos(ang) * rad * (r * 2.3), y + math.sin(ang) * rad * (r * 2.3)
            if all((tx - px) ** 2 + (ty - py) ** 2 >= (r + pr + 1.2) ** 2 for px, py, pr in placed):
                lx, ly = tx, ty
                break
        else:
            lx, ly = x, y
        placed.append((lx, ly, r))
        if (lx, ly) != (x, y):
            overlay.append(Line(x, y, lx, ly, strokeColor=colors.HexColor("#111827"), strokeWidth=0.8))
            overlay.append(Circle(x, y, 1.6, fillColor=colors.HexColor("#111827"), strokeColor=colors.white, strokeWidth=0.4))
        overlay += marker(it["kind"], lx, ly, r, it.get("reuse"), it["no"])
        if it["kind"] in ("ASAL", "TUJUAN") and (number_all or not subset):
            nm = _pdf_txt(("Asal: " if it["kind"] == "ASAL" else "Tujuan: ") + str(it.get("info") or ""))[:34]
            tw = len(nm) * 4.3 + 6
            bx = min(max(lx - tw / 2, 4), width - tw - 4)
            by = ly + r + 3 if ly + r + 18 < map_h else ly - r - 15
            namebox.append(Rect(bx, by, tw, 12, fillColor=colors.Color(1, 1, 1, alpha=0.9), strokeColor=colors.HexColor("#94a3b8"), strokeWidth=0.4))
            namebox.append(String(bx + 3, by + 3.4, nm, fontName="Helvetica-Bold", fontSize=7.6, fillColor=colors.HexColor("#14532d" if it["kind"] == "ASAL" else "#7f1d1d")))
    for sh in overlay:
        drawing.add(sh)
    for sh in namebox:
        drawing.add(sh)

    # skala (meter nyata), panah utara, atribusi
    target = width * 0.2 * k_real / sc
    mag = 10 ** math.floor(math.log10(max(target, 1)))
    nice = next((m * mag for m in (1, 2, 5, 10) if m * mag >= target * 0.6), mag)
    L = nice / k_real * sc                                # panjang di PDF untuk 'nice' meter nyata
    drawing.add(Rect(6, 6, L + 18, 22, fillColor=colors.Color(1, 1, 1, alpha=0.85), strokeColor=None))
    drawing.add(Line(12, 12, 12 + L, 12, strokeWidth=1.8, strokeColor=colors.black))
    drawing.add(Line(12, 9, 12, 15, strokeWidth=1, strokeColor=colors.black))
    drawing.add(Line(12 + L, 9, 12 + L, 15, strokeWidth=1, strokeColor=colors.black))
    drawing.add(String(12 + L / 2, 18, f"{nice:g} m" if nice < 1000 else f"{nice / 1000:g} km", textAnchor="middle", fontName="Helvetica-Bold", fontSize=7.5))
    nx, ny = width - 20, map_h - 44
    drawing.add(Rect(nx - 13, ny - 6, 26, 44, fillColor=colors.Color(1, 1, 1, alpha=0.85), strokeColor=None))
    drawing.add(Polygon([nx, ny + 26, nx - 6, ny, nx + 6, ny], fillColor=colors.HexColor("#334155"), strokeColor=colors.HexColor("#334155")))
    drawing.add(String(nx, ny + 29, "U", textAnchor="middle", fontSize=8.5, fontName="Helvetica-Bold"))
    if bm:
        at = bm["attr"]
        tw = len(at) * 3.9 + 8
        drawing.add(Rect(width - tw - 2, 2, tw, 11, fillColor=colors.Color(1, 1, 1, alpha=0.85), strokeColor=None))
        drawing.add(String(width - tw + 2, 5, at, fontName="Helvetica", fontSize=6.8, fillColor=colors.HexColor("#334155")))

    # legenda (pita di atas peta)
    drawing.add(Rect(0, map_h, width, legend_h, fillColor=colors.HexColor("#f1f5f9"), strokeColor=colors.HexColor("#cbd5e1"), strokeWidth=0.6))
    lx = 8
    ly = map_h + 7
    leg = [("ASAL", "Asal", False), ("TUJUAN", "Tujuan", False), ("SINGGAH", "Singgah", False), ("BELOKAN", "Belokan", False), ("HUB", "Hub", False),
           ("TIANG", "Tiang", False), ("TIANG", "Pakai ulang", True), ("HH", "Handhole", False), ("SLACK", "Slack", False)]
    kinds_present = {it["kind"] for it in items}
    for kind, lab, reuse in leg:
        if kind in ("TIANG", "HH", "SLACK", "SINGGAH", "HUB", "BELOKAN") and kind not in kinds_present:
            continue
        if reuse and not any(it.get("reuse") for it in items):
            continue
        for sh in marker(kind, lx + 5, ly + 3, 4.6, reuse, None):
            drawing.add(sh)
        tw = len(lab) * 4.1 + 6
        drawing.add(String(lx + 12, ly, lab, fontName="Helvetica", fontSize=7.6, fillColor=colors.HexColor("#334155")))
        lx += 12 + tw + 6
    if len(route_ll) > 1:
        for si, sg in enumerate(plan.get("segments") or []):
            col = palette[si % 4]
            drawing.add(Line(lx, ly + 3, lx + 14, ly + 3, strokeColor=colors.HexColor(col), strokeWidth=2.6))
            lab = _pdf_txt(str(sg.get("label") or sg.get("tag") or f"S{si + 1}"))[:34]
            drawing.add(String(lx + 18, ly, lab, fontName="Helvetica", fontSize=7.6, fillColor=colors.HexColor("#334155")))
            lx += 18 + len(lab) * 4.0 + 10
    return _MapFlow(drawing, bm["jpeg"] if bm else None, map_h), {"basemap": bool(bm), "missing": (bm or {}).get("missing", 0), "zoom": (bm or {}).get("zoom")}


def _make_map_flow_class():
    from reportlab.platypus import Flowable

    class _MapFlow(Flowable):
        def __init__(self, drawing, jpeg, map_h):
            super().__init__()
            self.drawing, self.jpeg, self.map_h = drawing, jpeg, map_h
            self.width, self.height = drawing.width, drawing.height

        def wrap(self, aw, ah):
            return self.width, self.height

        def draw(self):
            from reportlab.graphics import renderPDF
            from reportlab.lib.utils import ImageReader
            if self.jpeg:
                self.canv.drawImage(ImageReader(io.BytesIO(self.jpeg)), 0, 0, self.width, self.map_h)
            renderPDF.draw(self.drawing, self.canv, 0, 0)
    return _MapFlow


class _MapFlowLazy:
    """Menunda impor reportlab (opsional) sampai benar-benar dipakai."""
    def __call__(self, drawing, jpeg, map_h):
        global _MapFlowCls
        if _MapFlowCls is None:
            _MapFlowCls = _make_map_flow_class()
        return _MapFlowCls(drawing, jpeg, map_h)


_MapFlowCls = None
_MapFlow = _MapFlowLazy()


def _plan_pdf_bytes(plan: dict, meta: dict, boq: Optional[dict], variant: str, basemap: str = "off") -> bytes:
    try:
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.units import mm
        from reportlab.lib import colors
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.platypus import BaseDocTemplate, PageTemplate, Frame, NextPageTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether, PageBreak
        from reportlab.pdfgen import canvas as rl_canvas
    except ImportError:
        raise HTTPException(status_code=501, detail="Pustaka PDF belum terpasang di server. Jalankan: pip install reportlab")
    from xml.sax.saxutils import escape as _esc

    status = meta["status"]                 # PRATINJAU | DRAFT | TEREALISASI
    stat_col = {"PRATINJAU": "#64748b", "DRAFT": "#d97706", "TEREALISASI": "#15803d"}[status]
    full = variant == "lengkap"
    s = plan.get("summary") or {}
    o, dst = plan.get("origin") or {}, plan.get("dest") or {}
    tz = int(meta.get("tz_min") or 0)

    ink, quiet, line = colors.HexColor("#0f172a"), colors.HexColor("#475569"), colors.HexColor("#cbd5e1")
    st = {
        "h1": ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=16, leading=20, textColor=ink, spaceAfter=2),
        "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=11.5, leading=14, textColor=colors.HexColor("#1d4ed8"), spaceBefore=11, spaceAfter=4, keepWithNext=1),
        "p": ParagraphStyle("p", fontName="Helvetica", fontSize=9, leading=12, textColor=ink),
        "sm": ParagraphStyle("sm", fontName="Helvetica", fontSize=8, leading=10.5, textColor=quiet),
        "c": ParagraphStyle("c", fontName="Helvetica", fontSize=8.2, leading=10.2, textColor=ink),
        "cb": ParagraphStyle("cb", fontName="Helvetica-Bold", fontSize=8.2, leading=10.2, textColor=ink),
        "ch": ParagraphStyle("ch", fontName="Helvetica-Bold", fontSize=8.2, leading=10.2, textColor=colors.white),
        "cr": ParagraphStyle("cr", fontName="Helvetica", fontSize=8.2, leading=10.2, textColor=ink, alignment=2),
        "warn": ParagraphStyle("warn", fontName="Helvetica", fontSize=8.8, leading=11.5, textColor=colors.HexColor("#92400e"), leftIndent=9, bulletIndent=0),
    }
    P = lambda t, k="c": Paragraph(_esc(_pdf_txt(t)), st[k])

    def table(rows, widths, head=True, zebra=True, align_right=()):
        data = []
        for ri, r in enumerate(rows):
            cells = []
            for ci, c in enumerate(r):
                if hasattr(c, "wrap"):
                    cells.append(c)
                else:
                    k = "ch" if head and ri == 0 else ("cr" if ci in align_right and not (head and ri == 0) else "c")
                    cells.append(P(c, k))
            data.append(cells)
        t = Table(data, colWidths=widths, repeatRows=1 if head else 0)
        cmds = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("GRID", (0, 0), (-1, -1), 0.4, line),
                ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5)]
        if head:
            cmds.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e293b")))
        if zebra:
            for i in range(1 if head else 0, len(rows)):
                if (i % 2) == 0:
                    cmds.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#f1f5f9")))
        t.setStyle(TableStyle(cmds))
        return t

    def kv(rows, w1=48 * mm, w2=130 * mm):
        data = [[P(a, "cb"), P(b)] for a, b in rows]
        t = Table(data, colWidths=[w1, w2])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.3, line),
                               ("LEFTPADDING", (0, 0), (-1, -1), 3), ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5)]))
        return t

    W = A4[0] - 30 * mm
    el = []
    name = meta.get("name") or "Rencana Pasang Baru"
    el.append(Paragraph(_esc(_pdf_txt("Asplan Pasang Baru: " + name)), st["h1"]))
    vlabel = "Versi lapangan (tanpa harga)" if not full else "Versi lengkap (dengan BOQ KHS)"
    el.append(Paragraph(f'<font color="{stat_col}"><b>{status}</b></font> &nbsp;|&nbsp; {_esc(vlabel)}' +
                        (f' &nbsp;|&nbsp; Rencana #{int(meta["plan_id"])}' if meta.get("plan_id") else ""), st["p"]))
    el.append(Spacer(1, 6))
    scen = {"DIRECT": ("Langsung (dropcore dari aset asal)" if plan.get("termination") == "DROPCORE_ROSET" else "Langsung (kabel udara dari aset asal + OTB di pelanggan)"),
            "HUB": "Hub (distribusi baru + closure)"}.get(plan.get("scenario"), plan.get("scenario") or "-")
    cab = plan.get("cable") or {}
    rows = [("Asal", f"{o.get('name', '-')} ({o.get('kind') or o.get('type') or '-'})" + (f", {o.get('cluster')}/{o.get('area')}" if o.get("cluster") else "")),
            ("Koordinat asal", f"{o['lat']:.6f}, {o['lng']:.6f}" if o.get("lat") is not None else "-"),
            ("Tujuan", f"{dst.get('name', '-')}"),
            ("Koordinat tujuan", f"{dst.get('lat', 0):.6f}, {dst.get('lng', 0):.6f}"),
            ("Skenario", scen),
            ("Layanan", f"{plan.get('customer_cores') or 1} core; terminasi: " + ("roset di pelanggan" if plan.get("termination") == "DROPCORE_ROSET" else "kabel udara + OTB di pelanggan")),
            ("Kabel", f"{cab.get('label', '-')} {cab.get('capacity', '')} ({cab.get('installation', '-')})"),
            ("Panjang rute", f"{_pdf_fm(s.get('route_length_m'))} ({'mengikuti jalan' if s.get('route_source') == 'osrm' else 'disunting manual' if s.get('route_source') == 'manual' else 'garis lurus'}); total kabel + slack {_pdf_fm(s.get('cable_total_m'))}"),
            ("Dibuat", f"{_pdf_local(meta.get('created_at'), tz)} oleh {meta.get('created_by') or '-'}")]
    if status == "TEREALISASI" and meta.get("realized"):
        rz = meta["realized"]
        rows.append(("Terwujud", f"{_pdf_local(meta.get('realized_at'), tz)}; kabel {rz.get('cable_name', '-')}"))
    el.append(kv(rows))
    if (plan.get("remarks") or "").strip():
        el.append(Paragraph("Keterangan perencana", st["h2"]))
        el.append(Paragraph(_esc(_pdf_txt(plan["remarks"].strip())).replace("\n", "<br/>"), st["p"]))
    # ---- spesifikasi (halaman 1 bersama identitas rencana)
    el.append(Paragraph("Spesifikasi teknis", st["h2"]))
    segs = plan.get("segments") or []
    sg = [["Segmen", "Kabel", "Pemasangan", "Rute", "Kabel + slack", "Tiang/HH baru", "Slack"]]
    for g in segs:
        newp = g.get("poles_new") if g.get("installation") == "Udara" else g.get("hh_new")
        sg.append([g.get("label", g.get("tag", "")), f"{g.get('cable_label', '')} {g.get('cable_capacity', '')}", g.get("installation", ""),
                   _pdf_fm(g.get("route_length_m")), _pdf_fm(g.get("cable_total_m")), str(newp if newp is not None else "-"),
                   f"{g.get('slack_count', 0)} x {s.get('slack_length_m', '-')} m"])
    el.append(table(sg, [34 * mm, 36 * mm, 21 * mm, 19 * mm, 24 * mm, 20 * mm, W - 154 * mm]))
    ex = []
    av = o.get("availability") or {}
    if av.get("detail"):
        ex.append(("Ketersediaan asal", av["detail"]))
    if o.get("pop_ports"):
        ex.append(("Port POP dipilih", ", ".join(o["pop_ports"])))
    if plan.get("hub"):
        h = plan["hub"]
        ex.append(("Hub", f"Closure {h.get('closure_size', '-')} core; jarak hub ke pelanggan {_pdf_fm(h.get('to_customer_m'))}" + (f"; ODP baru splitter {h['odp']['ratio']}" if h.get("odp") else "")))
    ci = plan.get("customer_info")
    if ci:
        ex.append(("Pelanggan", "; ".join(f"{k}: {v}" for k, v in (("layanan", ci.get("service")), ("bandwidth", ci.get("bandwidth_mbps") and f"{ci['bandwidth_mbps']} Mbps"), ("jenis", ci.get("link_type")), ("SN", ci.get("device_sn"))) if v)))
    if ex:
        el.append(Spacer(1, 4)); el.append(kv(ex))


    # ---- peta rute + tabel poin bernomor
    pts = _plan_points(plan)
    WL = A4[1] - 30 * mm
    many = len(pts) > 40
    base = basemap if basemap in PDF_BASEMAPS else "off"
    el.append(NextPageTemplate("land"))
    el.append(PageBreak())
    el.append(Paragraph("Peta rute dan nomor titik", st["h2"]))
    mf, minfo = _pdf_route_map(plan, pts, WL, 392, basemap=base, number_all=not many)
    el.append(mf)
    cap = ("Nomor pada peta = kolom No pada tabel titik rute (halaman berikutnya). " +
           ("Peta dasar: " + {"osm": "OpenStreetMap", "satelit": "citra satelit"}.get(base, base) + ". " if minfo.get("basemap") else
            ("Peta dasar tidak tersedia saat PDF dibuat (server tidak dapat mengambil tile); gambar tetap lengkap tanpa peta dasar. " if base != "off" else "")) +
           (f"Sebagian tile peta dasar gagal diambil ({minfo['missing']}); area itu tampil kosong. " if minfo.get("missing") else "") +
           ("Tiang/slack/handhole bernomor pada peta detail. " if many else "") +
           "Posisi titik adalah rencana; verifikasi dengan survei lapangan.")
    el.append(Paragraph(_esc(_pdf_txt(cap)), st["sm"]))
    el.append(NextPageTemplate("port"))
    el.append(PageBreak())
    el.append(Paragraph("Tabel titik rute dan aset yang dibutuhkan", st["h2"]))
    wp = [["No", "Titik", "Keterangan", "Jarak dari asal", "Latitude", "Longitude", "Catatan lapangan"]]
    for it in pts:
        kind = {"HH": "HANDHOLE"}.get(it["kind"], it["kind"])
        wp.append([str(it["no"]), kind, it["info"] if it["kind"] not in ("ASAL", "TUJUAN") else it["info"], _pdf_fm(it["along"]) if it["kind"] != "ASAL" else "0 m",
                   f"{it['lat']:.6f}", f"{it['lng']:.6f}", ""])
    el.append(table(wp, [10 * mm, 18 * mm, W - 10 * mm - 18 * mm - 22 * mm - 21 * mm - 21 * mm - 36 * mm, 22 * mm, 21 * mm, 21 * mm, 36 * mm], align_right=(3,)))
    if many:
        el.append(NextPageTemplate("land"))
        el.append(PageBreak())
        chunk = 20
        parts = [pts[i:i + chunk] for i in range(0, len(pts), chunk)]
        for pi, part in enumerate(parts, 1):
            el.append(Paragraph(f"Peta detail {pi} dari {len(parts)}: titik {part[0]['no']} sampai {part[-1]['no']}", st["h2"]))
            df, _ = _pdf_route_map(plan, pts, WL, 392, basemap=base, subset=part, number_all=True)
            el.append(df)
            el.append(Paragraph("Lingkaran bernomor = titik pada bagian ini; titik kecil tanpa nomor = titik di luar bagian ini (lihat peta detail lain).", st["sm"]))
            if pi < len(parts):
                el.append(PageBreak())
        el.append(NextPageTemplate("port"))
        el.append(PageBreak())

    # ---- material
    el.append(Paragraph("Kebutuhan material", st["h2"]))
    mt = [["No", "Komponen", "Jumlah", "Satuan"]]
    for i, b in enumerate(plan.get("boq_items") or [], 1):
        mt.append([str(i), b.get("label", ""), (f"{b['qty']:.1f}" if b.get("unit") == "m" else str(b.get("qty"))), b.get("unit", "")])
    el.append(table(mt, [9 * mm, W - 9 * mm - 24 * mm - 20 * mm, 24 * mm, 20 * mm], align_right=(2,)))

    # ---- instruksi lapangan
    el.append(Paragraph("Rencana pelaksanaan (tim lapangan)", st["h2"]))
    steps = ["Survei jalur: cocokkan titik pada tabel dengan kondisi lapangan, catat hambatan (izin tiang, persilangan jalan/sungai, kepemilikan lahan)."]
    if s.get("use_poles") is not False:
        if s.get("installation") == "Udara":
            steps.append(f"Pasang {s.get('poles_new', 0)} tiang baru" + (f" (pakai ulang {s.get('poles_existing')} tiang eksisting)" if s.get("poles_existing") else "") + " pada titik TIANG di tabel.")
        else:
            steps.append(f"Siapkan {s.get('hh_new', 0)} handhole baru" + (f" (pakai ulang {s.get('hh_existing')})" if s.get("hh_existing") else "") + " dan jalur ducting pada titik HANDHOLE di tabel.")
        if s.get("poles_existing") or s.get("hh_existing"):
            steps.append("Survei tiang/handhole eksisting yang dipakai ulang (ditandai \"Pakai ulang\" di tabel): periksa kelayakan, kapasitas, dan izin tumpang sebelum kabel ditarik. Bila tidak layak, ganti dengan yang baru dan laporkan ke perencana.")
    for g in segs:
        steps.append(f"Tarik {g.get('cable_label', '')} {g.get('cable_capacity', '')} ({g.get('installation', '')}) segmen \"{g.get('label', '')}\" sepanjang {_pdf_fm(g.get('route_length_m'))}; kebutuhan kabel termasuk slack {_pdf_fm(g.get('cable_total_m'))}.")
    if s.get("use_slack") is not False and s.get("slack_count"):
        steps.append(f"Sisakan slack {s.get('slack_count')} titik x {s.get('slack_length_m')} m pada titik SLACK.")
    if plan.get("hub"):
        h = plan["hub"]
        steps.append(f"Pasang closure {h.get('closure_size', '-')} core di titik HUB" + (f" dan ODP baru splitter {h['odp']['ratio']}" if h.get("odp") else "") + ".")
    steps.append("Terminasi di lokasi pelanggan: " + (f"roset {plan.get('customer_cores') or 1} port." if plan.get("termination") == "DROPCORE_ROSET" else "OTB (kabel udara)."))
    steps.append(f"Sambung (splicing) pada {o.get('name', 'aset asal')}: " + ("gunakan port yang dipilih (" + ", ".join(o["pop_ports"]) + ")." if o.get("pop_ports") else "alokasi core/port kosong pertama; konfirmasi label core pada Detail Core setelah diwujudkan."))
    lo = plan.get("loss")
    if lo:
        steps.append(f"Ukur daya optik dan OTDR; bandingkan dengan estimasi redaman {lo.get('total_db', 0):.2f} dB (Rx estimasi {lo.get('rx_dbm', 0):.2f} dBm, batas ONT {((lo.get('params') or {}).get('rx_min_dbm', -27)):.0f} dBm).")
    steps.append("Dokumentasikan hasil (foto titik, as-built, hasil ukur) dan laporkan ke NOC untuk pembaruan data.")
    ck = [[P("[   ]", "c"), P(f"{i}. {t}")] for i, t in enumerate(steps, 1)]
    t = Table(ck, colWidths=[11 * mm, W - 11 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.3, line), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    el.append(t)

    # ---- redaman
    if lo:
        el.append(Paragraph("Anggaran redaman (estimasi)", st["h2"]))
        b = lo.get("breakdown") or {}
        pr = lo.get("params") or {}
        rr = [["Komponen", "Nilai"],
              ["Serat optik", f"{b.get('fiber_km', 0):.3f} km = {b.get('fiber_db', 0):.2f} dB"],
              ["Sambungan fusion", f"{b.get('splices', 0)} titik"],
              ["Konektor", f"{b.get('connectors', 0)} pasang"]]
        for sp in b.get("splitters") or []:
            rr.append([f"Splitter {sp.get('ratio')}", f"{sp.get('name')}: {sp.get('db')} dB"])
        rr += [["Total redaman", f"{lo.get('total_db', 0):.2f} dB"], ["Daya kirim (Tx)", f"{pr.get('tx_dbm', '-')} dBm"],
               ["Daya terima estimasi (Rx)", f"{lo.get('rx_dbm', 0):.2f} dBm (batas ONT {pr.get('rx_min_dbm', '-')} dBm)"],
               ["Margin tersisa", f"{lo.get('margin_db', 0):.2f} dB"], ["Status", {"OK": "Layak", "WARN": "Layak, margin tipis", "BAD": "TIDAK LAYAK"}.get(lo.get("status"), lo.get("status", "-"))]]
        el.append(table(rr, [60 * mm, W - 60 * mm]))

    # ---- peringatan
    notes = list(plan.get("notes") or [])
    warns = list(plan.get("warnings") or [])
    if warns or notes:
        el.append(Paragraph("Peringatan dan catatan", st["h2"]))
        for w in warns:
            el.append(Paragraph(_esc(_pdf_txt(w)), st["warn"], bulletText="!"))
        for nn in notes:
            el.append(Paragraph(_esc(_pdf_txt(nn)), st["p"], bulletText="-"))

    # ---- lampiran: skematik (tanpa peta dasar), nomor sama dengan tabel
    el.append(Paragraph("Lampiran: skematik rute (tanpa peta dasar)", st["h2"]))
    sf, _ = _pdf_route_map(plan, pts, W, 250, basemap="off", number_all=not many)
    el.append(sf)
    el.append(Paragraph("Skematik dari koordinat rute pada skala yang sama ke semua arah; nomor sama dengan tabel titik rute.", st["sm"]))

    # ---- BOQ lengkap
    if full:
        el.append(PageBreak())
        el.append(Paragraph("Rencana Anggaran Biaya (BOQ KHS)", st["h1"]))
        if not boq:
            el.append(Paragraph("BOQ tidak tersedia.", st["p"]))
        else:
            tt = boq["totals"]
            el.append(Paragraph(_esc(f"Wilayah harga KHS: {boq.get('region')}; pemasangan {boq.get('installation')}."), st["sm"]))
            el.append(Spacer(1, 4))
            bl = [["No", "Komponen", "Kode KHS", "Uraian", "Sat", "Vol", "Harga satuan", "Jumlah"]]
            for i, l in enumerate(boq["lines"], 1):
                _chg = l.get("manual") and l.get("auto_qty") is not None and l["qty"] != l["auto_qty"]
                bl.append([str(i), l.get("component", "") + (" *" if (l.get("manual") and l.get("auto_qty") is None) or _chg else ""), l.get("code") or "-", l.get("description", ""), l.get("unit", ""),
                           f"{l['qty']:g}" + (" *" if _chg else ""), _pdf_rp(l.get("unit_price")), _pdf_rp(l.get("total"))])
            wb = [8 * mm, 25 * mm, 18 * mm, W - 8 * mm - 25 * mm - 18 * mm - 13 * mm - 14 * mm - 26 * mm - 28 * mm, 13 * mm, 14 * mm, 26 * mm, 28 * mm]
            el.append(table(bl, wb, align_right=(5, 6, 7)))
            el.append(Spacer(1, 6))
            sm = [["Subtotal material", _pdf_rp(tt["material"])], ["Subtotal jasa", _pdf_rp(tt["jasa"])], ["Subtotal", _pdf_rp(tt["subtotal"])],
                  [f"PPN {tt['tax_pct']:g}%", _pdf_rp(tt["tax"])], ["TOTAL", _pdf_rp(tt["total"])]]
            tb = Table([[P(a, "cb" if a == "TOTAL" else "c"), P(b, "cb" if a == "TOTAL" else "cr")] for a, b in sm], colWidths=[45 * mm, 40 * mm], hAlign="RIGHT")
            tb.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, line), ("BACKGROUND", (0, 4), (-1, 4), colors.HexColor("#e2e8f0"))]))
            el.append(tb)
            for w in boq.get("warnings") or []:
                el.append(Paragraph(_esc(_pdf_txt(w)), st["warn"], bulletText="!"))
            # catatan penyesuaian manual: agar pembaca tahu BOQ berbeda dari hitungan otomatis / tabel material
            adj_notes = []
            for i, l in enumerate(boq["lines"], 1):
                if l.get("manual") and l.get("auto_qty") is not None and l["qty"] != l["auto_qty"]:
                    adj_notes.append(f"Baris {i} ({l.get('component', '')}): volume diubah manual menjadi {l['qty']:g} {l.get('unit', '')} (hitungan otomatis {l['auto_qty']:g}).")
                elif l.get("auto_qty") is None:
                    adj_notes.append(f"Baris {i}: item tambahan manual (KHS {l.get('code') or '-'}), tidak ada pada kebutuhan material otomatis.")
            for rm in boq.get("removed") or []:
                adj_notes.append(f"Baris otomatis dihapus dari BOQ: {rm.get('component', '')} (volume otomatis {rm.get('auto_qty', 0):g}); pada tabel kebutuhan material baris ini masih tercantum.")
            if adj_notes:
                el.append(Spacer(1, 4))
                el.append(Paragraph("Catatan penyesuaian BOQ (tanda * = diubah/ditambah manual)", st["h2"]))
                for an in adj_notes:
                    el.append(Paragraph(_esc(_pdf_txt(an)), st["sm"], bulletText="-"))

    # ---- persetujuan
    sig = Table([[P("Dibuat oleh", "cb"), P("Diperiksa oleh", "cb"), P("Disetujui oleh", "cb")],
                 [P(meta.get("created_by") or ""), P(""), P("")],
                 [P(""), P(""), P("")],
                 [P("Tanggal:"), P("Tanggal:"), P("Tanggal:")]], colWidths=[W / 3] * 3, rowHeights=[14, 14, 38, 14])
    sig.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, line), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    el.append(KeepTogether([Paragraph("Persetujuan", st["h2"]), sig]))

    gen = f"Dibuat {_pdf_local(_now_str(), tz)} oleh {meta.get('generated_by') or '-'}"

    class _Canvas(rl_canvas.Canvas):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._saved = []

        def showPage(self):
            self._saved.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved)
            for stt in self._saved:
                self.__dict__.update(stt)
                self._decor(total)
                super().showPage()
            super().save()

        def _decor(self, total):
            w, h = self._pagesize
            self.saveState()
            if status == "PRATINJAU":
                self.setFont("Helvetica-Bold", 64)
                self.setFillColor(colors.Color(0.55, 0.6, 0.68, alpha=0.13))
                self.translate(w / 2, h / 2); self.rotate(45)
                self.drawCentredString(0, 0, "PRATINJAU")
                self.rotate(-45); self.translate(-w / 2, -h / 2)
            self.setFillColor(colors.HexColor("#0f172a"))
            self.setFont("Helvetica-Bold", 10)
            self.drawString(15 * mm, h - 11 * mm, "NETGIS Enterprise")
            self.setFont("Helvetica", 8.5)
            self.setFillColor(colors.HexColor("#475569"))
            self.drawString(15 * mm + 100, h - 11 * mm, "Asplan Pasang Baru")
            self.setFillColor(colors.HexColor(stat_col))
            self.setFont("Helvetica-Bold", 9)
            self.drawRightString(w - 15 * mm, h - 11 * mm, status)
            self.setStrokeColor(colors.HexColor("#cbd5e1")); self.setLineWidth(0.6)
            self.line(15 * mm, h - 13.5 * mm, w - 15 * mm, h - 13.5 * mm)
            self.line(15 * mm, 12.5 * mm, w - 15 * mm, 12.5 * mm)
            self.setFont("Helvetica", 7.5); self.setFillColor(colors.HexColor("#64748b"))
            self.drawString(15 * mm, 8 * mm, _pdf_txt(gen + (f" | Rencana #{meta['plan_id']}" if meta.get("plan_id") else "")))
            self.drawRightString(w - 15 * mm, 8 * mm, f"Halaman {self._pageNumber} dari {total}")
            self.restoreState()

    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=18 * mm, bottomMargin=17 * mm,
                          title=_pdf_txt(f"Asplan {name}"), author="NETGIS Enterprise")
    pw, ph = A4
    doc.addPageTemplates([
        PageTemplate(id="port", pagesize=A4, frames=[Frame(15 * mm, 17 * mm, pw - 30 * mm, ph - 35 * mm, leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, id="fp")]),
        PageTemplate(id="land", pagesize=landscape(A4), frames=[Frame(15 * mm, 17 * mm, ph - 30 * mm, pw - 35 * mm, leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, id="fl")]),
    ])
    doc.build(el, canvasmaker=_Canvas)
    return buf.getvalue()


class PlanPdfRequest(PlanRequest):
    variant: Optional[str] = "lapangan"
    basemap: Optional[str] = "osm"
    boq_adjust: Optional[dict] = None
    tz_min: Optional[int] = 0


def _pdf_variant(v: Optional[str]) -> str:
    v = (v or "lapangan").lower()
    if v not in PDF_VARIANTS:
        raise HTTPException(status_code=400, detail="Versi PDF tidak dikenal (lapangan | lengkap)")
    if v == "lengkap":
        u = CURRENT_USER.get()
        if not u or "plan.write" not in ROLE_PERMS.get(u["role"], set()):
            raise HTTPException(status_code=403, detail="Versi lengkap (memuat harga) hanya untuk peran yang boleh mengelola rencana")
    return v


def _pdf_response(body: bytes, name: str, status: str, variant: str) -> Response:
    safe = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")[:40] or "rencana"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    fname = f"asplan_{safe}_{status.lower()}_{variant}_{stamp}.pdf"
    return Response(content=body, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.post("/api/plan/pdf")
def plan_pdf_preview(req: PlanPdfRequest):
    """PDF dari hasil perhitungan yang BELUM disimpan (label PRATINJAU)."""
    variant = _pdf_variant(req.variant)
    with db() as conn:
        cursor = conn.cursor()
        res = _compute_plan(cursor, req)
        name = (req.name or "").strip() or f"Pasang baru {res['dest']['name']}"
        boq = _boq_compute(cursor, BoqRequest(summary=res["summary"], adjust=req.boq_adjust, origin_cluster=(res["origin"] or {}).get("cluster"))) if variant == "lengkap" else None
        meta = {"name": name, "status": "PRATINJAU", "plan_id": None, "created_at": _now_str(), "created_by": _current_username(),
                "generated_by": _current_username(), "tz_min": req.tz_min}
        body = _plan_pdf_bytes(res, meta, boq, variant, _pdf_basemap_mode(req.basemap))
        _audit(cursor, "EXPORT", "PLAN", None, name, f"Unduh PDF asplan (PRATINJAU, versi {variant}): '{name}'")
    return _pdf_response(body, name, "PRATINJAU", variant)


@app.get("/api/plans/{plan_id}/pdf")
def plan_pdf_saved(plan_id: int, variant: str = "lapangan", tz: int = 0, basemap: str = "osm"):
    """PDF rencana tersimpan: Draft -> 'DRAFT' (asplan awal ke tim lapangan), Realized -> 'TEREALISASI'."""
    variant = _pdf_variant(variant)
    with db() as conn:
        cursor = conn.cursor()
        r = _load_plan(cursor, plan_id)
        plan = json.loads(r["result"])
        summary = json.loads(r["summary"] or "{}")
        status = "TEREALISASI" if r["status"] == "Realized" else "DRAFT"
        boq = None
        if variant == "lengkap":
            try:
                adj = json.loads(r["boq_adjust"]) if r["boq_adjust"] else None
            except (TypeError, ValueError):
                adj = None
            boq = _boq_compute(cursor, BoqRequest(summary=summary, adjust=adj, origin_cluster=((plan.get("origin") or {}).get("cluster"))))
        try:
            realized = json.loads(r["realized_info"]) if r["realized_info"] else None
        except (TypeError, ValueError):
            realized = None
        meta = {"name": r["name"], "status": status, "plan_id": plan_id, "created_at": r["created_at"], "created_by": r["created_by"],
                "realized_at": r["realized_at"], "realized": realized, "generated_by": _current_username(), "tz_min": tz}
        body = _plan_pdf_bytes(plan, meta, boq, variant, _pdf_basemap_mode(basemap))
        _audit(cursor, "EXPORT", "PLAN", plan_id, r["name"], f"Unduh PDF asplan #{plan_id} ({status}, versi {variant}): '{r['name']}'")
    return _pdf_response(body, r["name"], status, variant)


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
    "otdr_slack_m": 20.0,       # cadangan kabel (slack/closure) per titik sambung, meter
    "otdr_normal_pct": 2.0,     # toleransi panjang jalur vs event akhir OTDR dianggap normal (%)
    "otdr_helix_pct": 0.5,      # serat lebih panjang dari kabel (helix/loose tube), %
    "otdr_route_err_pct": 2.0,  # kekeliruan rute di peta vs lapangan, %
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
           "margin_db": (0, 15), "mm_fiber_db_km_850": (0, 8), "mm_fiber_db_km_1300": (0, 4), "rx_max_dbm": (-30, 10), "mm_tx_dbm": (-20, 10), "mm_rx_min_dbm": (-40, 0), "otdr_warn_db": (0, 20), "otdr_bad_db": (0, 40), "event_warn_db": (0, 10), "event_bad_db": (0, 20),
           "otdr_slack_m": (0, 200), "otdr_normal_pct": (0, 20), "otdr_helix_pct": (0, 5), "otdr_route_err_pct": (0, 20)}
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


# --- Pemilihan port OTB POP: otomatis / manual -------------------------------------------------
def _pop_port_states(cursor, node) -> list:
    """Status tiap port OTB: free (kosong) | in (sudah menerima kabel masuk) | out (sudah meneruskan kabel keluar) | full (keduanya),
    beserta catatan perangkat di sisi depan."""
    labels = _port_labels("NODE", "POP", node["capacity"])
    ins, outs = _joint_dirs(cursor, node["id"])
    devs = {r["port_core"]: dict(r) for r in cursor.execute(
        "SELECT * FROM otb_port_devices WHERE asset_id = ?", (node["id"],)).fetchall()}
    res = []
    for lab in labels:
        st = "full" if (lab in ins and lab in outs) else "out" if lab in outs else "in" if lab in ins else "free"
        d = devs.get(lab)
        res.append({"port": lab, "state": st,
                    "device": ({k: d.get(k) for k in ("purpose", "device_type", "device_name", "slot", "interface", "customer_name")} if d else None)})
    return res


def _pop_choose_ports(cursor, node, n: int, manual=None, allow_in=False):
    """Pilih n port OTB untuk sirkuit baru. Mengembalikan {"mode","ports","warnings","error"}.
    Manual : port yang disebut pengguna divalidasi apa adanya (tidak pernah diganti diam-diam).
    Otomatis: (1) port kosong yang depannya perangkat PON/OLT lebih dulu, lalu port kosong tanpa perangkat; port yang depannya
              dicadangkan (PTP/uplink/lainnya) dilewati; (2) bila allow_in: port yang baru menerima kabel masuk, dengan peringatan.
              Port yang sudah punya kabel keluar tidak pernah dipilih."""
    states = _pop_port_states(cursor, node)
    by = {x["port"]: x for x in states}
    out = {"mode": "MANUAL" if manual else "AUTO", "ports": [], "warnings": [], "error": None}

    def reserved(x):
        d = x["device"]
        return bool(d) and (d.get("purpose") or "").upper() != "PON"

    if manual:
        seen = []
        for raw in manual:
            lab = _legacy_pop_port(node["capacity"], str(raw or "").strip())
            if lab in seen:
                out["error"] = f"Port {lab} dipilih dua kali"
                return out
            seen.append(lab)
        if len(seen) != n:
            out["error"] = f"Layanan {n} core membutuhkan tepat {n} port OTB; dipilih {len(seen)}"
            return out
        for lab in seen:
            x = by.get(lab)
            if not x:
                out["error"] = f"Port {lab} tidak ada pada {node['name']}"
                return out
            if x["state"] in ("out", "full"):
                out["error"] = f"Port {lab} pada {node['name']} sudah meneruskan kabel keluar lain"
                return out
            if x["state"] == "in":
                out["warnings"].append(f"Port {lab} sudah menerima kabel masuk: kabel keluar baru akan menyambung sirkuit masuk itu ke pelanggan ini")
            if reserved(x):
                d = x["device"]
                out["warnings"].append(f"Port {lab} dicatat untuk {d.get('purpose')} {d.get('device_name') or ''}"
                                       f"{' (' + d['customer_name'] + ')' if d.get('customer_name') else ''}; pastikan memang dipakai untuk pelanggan ini")
        out["ports"] = seen
        return out

    free = [x for x in states if x["state"] == "free"]
    pon = [x for x in free if x["device"] and not reserved(x)]
    plain = [x for x in free if not x["device"]]
    skipped = [x for x in free if reserved(x)]
    picked = [x["port"] for x in pon + plain][:n]
    if len(picked) < n and allow_in:
        for x in states:
            if x["state"] == "in" and not reserved(x) and len(picked) < n:
                picked.append(x["port"])
                out["warnings"].append(f"Port {x['port']} sudah menerima kabel masuk (feeder): meneruskannya akan menyambung sirkuit itu ke pelanggan ini")
    if len(picked) < n:
        out["error"] = (f"Port {node['name']} hanya {len(picked)} yang bisa dipilih otomatis; butuh {n}"
                        + (f" ({len(skipped)} port kosong dicadangkan untuk perangkat lain, pilih manual bila perlu)" if skipped else ""))
        return out
    out["ports"] = picked
    return out


@app.get("/api/nodes/{node_id}/otb-ports")
def get_otb_ports(node_id: int, n: int = 1):
    """Status tiap port OTB POP untuk pemilihan manual + usulan otomatis (n port)."""
    with db() as conn:
        cursor = conn.cursor()
        node = cursor.execute("SELECT id, name, type, capacity FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if not node:
            raise HTTPException(status_code=404, detail="Node tidak ditemukan")
        if (node["type"] or "").upper() != "POP":
            raise HTTPException(status_code=400, detail="Port OTB hanya untuk aset bertipe POP")
        auto = _pop_choose_ports(cursor, node, max(1, min(int(n), 12)), None, True)
        return {"node_id": node_id, "node_name": node["name"], "ports": _pop_port_states(cursor, node),
                "suggest": auto["ports"], "suggest_warnings": auto["warnings"], "suggest_error": auto["error"]}


def _acquire_feed(cursor, node, n, exclude_cable_ids, depth=0, log=None, pop_ports=None, notes=None, allow_in=False, prefer=None):
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
        ch = _pop_choose_ports(cursor, node, n, pop_ports if depth == 0 else None, allow_in)
        if ch["error"]:
            return None, ch["error"]
        if notes is not None:
            notes.extend(ch["warnings"])
        return [("NODE", node["id"], x) for x in ch["ports"]], None
    if t in JUNCTION_TYPES:
        ins, outs = _joint_dirs(cursor, node["id"])
        ready = sorted(p for p in ins if p not in outs)
        # prefer: pengguna memilih core tertentu pada kabel hulu (bukan yang terendah)
        srcs = [] if prefer else [("NODE", node["id"], p) for p in ready[:n]]
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
            if prefer:
                bad = [x for x in prefer if x not in free]
                if bad:
                    why = f"Core hulu {', '.join(bad[:3])} tidak tersedia pada kabel {c['name']} (sudah terpakai / bukan core kabel ini)"
                    continue
                free = list(prefer)
            if len(free) < need:
                why = f"Kabel {c['name']} hanya {len(free)} core kosong; butuh {need}"
                continue
            feeds, why2 = _acquire_feed(cursor, up, need, exclude_cable_ids | {c["id"]}, depth + 1, log, None, notes, allow_in)
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


def _pick_sources(cursor, origin, exclude_cable_ids, n, log=None, pop_ports=None, notes=None, allow_in=False):
    return _acquire_feed(cursor, origin, n, set(exclude_cable_ids), 0, log, pop_ports, notes, allow_in)


def _link(cursor, src, dst_type, dst_id, dst_port, cable_id, core, note):
    cursor.execute("""INSERT INTO core_connections (from_asset_type, from_asset_id, from_port_core, to_asset_type, to_asset_id,
                      to_port_core, via_cable_id, via_core, status, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Connected', ?)""",
                   (src[0], src[1], src[2], dst_type, dst_id, dst_port, cable_id, core, note))
    cid = cursor.lastrowid
    crow = cursor.execute("SELECT * FROM core_connections WHERE id = ?", (cid,)).fetchone()
    lbl = _conn_label(cursor, crow)
    _audit(cursor, "CONNECT", "CONNECTION", cid, lbl, f"Alokasi core otomatis: {lbl}", snapshot=_row_dict(crow))
    return {"id": cid, "label": lbl}


def _allocate_new_customer(cursor, origin_id, cable_id, cust_id, n=1, pop_ports=None) -> dict:
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
    feed_log, port_notes = [], []
    srcs, why = _pick_sources(cursor, origin, {cable_id}, n, feed_log, pop_ports, port_notes, True)
    if not srcs:
        return {"allocated": False, "reason": why}
    links = feed_log + [_link(cursor, srcs[i], "NODE", cust_id, cust_ports[i], cable_id, cab_cores[i], "Alokasi otomatis dari rencana pasang baru") for i in range(n)]
    return {"allocated": True, "port_notes": port_notes, "ports": [x[2] for x in srcs], "connection_id": links[0]["id"], "from_port": srcs[0][2], "from_type": srcs[0][0], "from_id": srcs[0][1],
            "to_port": cust_ports[0], "via_core": cab_cores[0], "label": links[0]["label"], "cores": n, "links": links}


def _allocate_hub(cursor, origin_id, cable1, closure_id, odp_id, cable2, cust_id, n, pop_ports=None) -> dict:
    """Rantai alokasi skenario hub: asal -> closure (kabel1), lalu closure -> pelanggan (kabel2) atau closure -> ODP -> pelanggan."""
    origin = cursor.execute("SELECT * FROM nodes WHERE id = ?", (origin_id,)).fetchone()
    cl = cursor.execute("SELECT * FROM nodes WHERE id = ?", (closure_id,)).fetchone()
    cust = cursor.execute("SELECT * FROM nodes WHERE id = ?", (cust_id,)).fetchone() if cust_id else None
    c1 = cursor.execute("SELECT * FROM cables WHERE id = ?", (cable1,)).fetchone()
    c2 = cursor.execute("SELECT * FROM cables WHERE id = ?", (cable2,)).fetchone()
    if not (origin and cl and c1 and c2):
        return {"allocated": False, "reason": "Aset rantai tidak lengkap"}
    feed_log, port_notes = [], []
    srcs, why = _pick_sources(cursor, origin, {cable1}, n, feed_log, pop_ports, port_notes, True)
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
    return {"allocated": True, "port_notes": port_notes, "ports": [x[2] for x in srcs], "connection_id": links[0]["id"], "from_port": srcs[0][2], "from_type": srcs[0][0], "from_id": srcs[0][1],
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



# ======================= OTDR: file SOR (SR-4731 v1/v2) =======================
import struct
C_VAC = 299792458.0


class SorError(ValueError):
    pass


class _SorR:
    def __init__(self, b, pos=0, end=None):
        self.b, self.p, self.end = b, pos, len(b) if end is None else end

    def need(self, n):
        if self.p + n > self.end:
            raise SorError("Blok SOR terpotong / tidak lengkap")

    def u(self, n):
        self.need(n)
        v = int.from_bytes(self.b[self.p:self.p + n], "little"); self.p += n; return v

    def i(self, n):
        self.need(n)
        v = int.from_bytes(self.b[self.p:self.p + n], "little", signed=True); self.p += n; return v

    def s(self):
        e = self.b.find(b"\0", self.p, self.end)
        if e < 0:
            raise SorError("String SOR tidak berakhir")
        v = self.b[self.p:e].decode("latin-1").strip(); self.p = e + 1; return v

    def fixed(self, n):
        self.need(n)
        v = self.b[self.p:self.p + n].decode("latin-1").strip("\0 "); self.p += n; return v


def _sor_blocks(d):
    if d[:4] != b"Map\0":
        raise SorError("Bukan file SOR (blok Map tidak ditemukan)")
    r = _SorR(d, 4)
    ver = r.u(2); size = r.u(4); nb = r.u(2)
    out = {}; pos = size
    for _ in range(nb - 1):
        name = r.s(); v = r.u(2); sz = r.u(4)
        if pos + sz > len(d):
            raise SorError(f"Blok {name} melebihi ukuran file")
        out[name] = (pos, sz); pos += sz
    return ver / 100.0, out


def _sor_seek(d, blocks, name):
    if name not in blocks:
        return None
    pos, sz = blocks[name]
    r = _SorR(d, pos, pos + sz)
    r.s()                                # nama blok
    return r


def parse_sor(data: bytes, max_points: int = 3000) -> dict:
    d = bytes(data)
    if len(d) < 60:
        raise SorError("File terlalu kecil untuk SOR")
    ver, blocks = _sor_blocks(d)
    v2 = ver >= 2.0
    res = {"format": f"SR-4731 v{ver:.2f}", "sha256": hashlib.sha256(d).hexdigest(), "size": len(d), "blocks": sorted(blocks)}
    for need in ("GenParams", "FxdParams", "DataPts"):
        if need not in blocks:
            raise SorError(f"Blok wajib {need} tidak ada")
    # --- GenParams
    r = _sor_seek(d, blocks, "GenParams")
    gen = {"language": r.fixed(2)}
    gen["cable_id"] = r.s(); gen["fiber_id"] = r.s()
    if v2:
        gen["fiber_type"] = r.u(2)
    gen["wavelength_nm"] = r.u(2)
    gen["location_a"] = r.s(); gen["location_b"] = r.s(); gen["cable_code"] = r.s()
    gen["build_condition"] = r.fixed(2)
    gen["user_offset"] = r.i(4)
    if v2:
        gen["user_offset_distance"] = r.i(4)
    gen["operator"] = r.s(); gen["comment"] = r.s()
    res["general"] = gen
    # --- SupParams
    sup = {}
    r = _sor_seek(d, blocks, "SupParams")
    if r:
        try:
            for k in ("supplier", "mainframe", "mainframe_sn", "module", "module_sn", "software", "other"):
                sup[k] = r.s()
        except SorError:
            pass
    res["supplier"] = sup
    # --- FxdParams
    r = _sor_seek(d, blocks, "FxdParams")
    fx = {}
    ts = r.u(4); fx["timestamp"] = ts
    fx["units"] = r.fixed(2)
    fx["wavelength_nm"] = r.u(2) / 10.0
    fx["acq_offset"] = r.i(4)
    if v2:
        fx["acq_offset_distance"] = r.i(4)
    npw = r.u(2)
    pws = [r.u(2) for _ in range(npw)]
    spac = [r.u(4) for _ in range(npw)]
    npts = [r.u(4) for _ in range(npw)]
    n_grp = r.u(4)
    ior = n_grp / 1e5
    if not (1.3 <= ior <= 1.7):
        raise SorError(f"Indeks bias grup tidak wajar ({ior}); file mungkin rusak")
    fx.update(pulse_width_ns=pws[0] if pws else None, sample_spacing_100ps=spac[0] if spac else None, n_points=npts[0] if npts else None,
              group_index=ior, backscatter=r.u(2), averages=r.u(4), avg_time_s=r.u(2) / 10.0,
              range_100ps=r.u(4), range_distance=r.i(4), front_panel_offset=r.i(4),
              noise_floor=r.u(2), noise_scale=r.u(2), power_offset=r.u(2),
              loss_threshold_db=r.u(2) / 1000.0, refl_threshold_db=-r.u(2) / 1000.0, eot_threshold_db=r.u(2) / 1000.0,
              trace_type=r.fixed(2))
    res["fixed"] = fx
    # --- DataPts
    r = _sor_seek(d, blocks, "DataPts")
    total_n = r.u(4); ntr = r.u(2); n2 = r.u(4); scale = r.u(2)
    if ntr < 1 or n2 <= 0 or n2 > 5_000_000:
        raise SorError("Blok DataPts tidak valid")
    raw = struct.unpack_from(f"<{n2}H", d, r.p) if r.p + 2 * n2 <= r.end else None
    if raw is None:
        raise SorError("Data titik trace terpotong")
    spacing_m = fx["sample_spacing_100ps"] * 1e-14 * C_VAC / ior / 2.0 if fx["sample_spacing_100ps"] else None
    # SR-4731: nilai = -dB*1000/skala*(scale/1000) dari referensi; level relatif: titik terkuat = 0 dB
    vmin = min(raw)
    k = 0.001 * (scale / 1000.0 if scale else 1.0)
    db = [-(v - vmin) * k for v in raw]
    res["trace"] = {"n": n2, "scale": scale, "spacing_m": spacing_m, "length_m": (n2 - 1) * spacing_m if spacing_m else None}
    # desimasi min/maks agar ringan di peramban
    if n2 <= max_points:
        idx = list(range(n2)); pts = [[round(i * spacing_m, 2) if spacing_m else i, round(db[i], 3)] for i in idx]
    else:
        step = n2 / (max_points / 2.0); pts = []
        i0 = 0.0
        while i0 < n2:
            a = int(i0); b = min(n2, int(i0 + step)) or a + 1
            seg = range(a, max(b, a + 1))
            lo = min(seg, key=lambda i: db[i]); hi = max(seg, key=lambda i: db[i])
            for i in sorted({lo, hi}):
                pts.append([round(i * spacing_m, 2), round(db[i], 3)])
            i0 += step
    res["trace"]["points"] = pts
    # --- KeyEvents
    events = []
    summ = {}
    r = _sor_seek(d, blocks, "KeyEvents")
    if r:
        ne = r.u(2)
        for _ in range(ne):
            e = {"no": r.u(2)}
            tt = r.u(4)
            e["tt_100ps"] = tt
            e["distance_m"] = tt * 1e-10 * C_VAC / ior / 2.0   # waktu tempuh pulang-pergi (dibagi 2)
            e["slope_db_km"] = r.i(2) / 1000.0
            e["splice_loss_db"] = r.i(2) / 1000.0
            e["reflectance_db"] = r.i(4) / 1000.0
            e["type_code"] = r.fixed(8)
            if v2:
                for kk in ("end_prev", "start", "end", "start_next", "peak"):
                    e[kk + "_pt"] = r.u(4)
            e["comment"] = r.s()
            events.append(e)
        try:
            summ = {"total_loss_db": r.i(4) / 1000.0, "loss_start": r.i(4), "loss_end": r.i(4), "orl_db": r.u(2) / 1000.0}
        except SorError:
            summ = {}
    res["events"] = events
    res["summary"] = summ
    res["raw_db"] = db       # untuk analisis internal (tidak diserialisasi)
    res["_spacing_m"] = spacing_m
    return res


SOR_MAX_BYTES = 8 * 1024 * 1024


def _sor_public(res: dict) -> dict:
    out = {k: v for k, v in res.items() if k not in ("raw_db", "_spacing_m")}
    return out


def _sor_decode(req) -> bytes:
    try:
        raw = base64.b64decode(req.content_base64 or "", validate=False)
    except Exception:
        raise HTTPException(status_code=400, detail="Isi file tidak valid (base64)")
    if not raw:
        raise HTTPException(status_code=400, detail="File SOR kosong")
    if len(raw) > SOR_MAX_BYTES:
        raise HTTPException(status_code=413, detail="File SOR terlalu besar (maks 8 MB)")
    return raw


class SorUpload(BaseModel):
    filename: str = ""
    content_base64: Optional[str] = ""
    note: Optional[str] = ""


@app.post("/api/otdr/sor/parse")
def otdr_sor_parse(req: SorUpload):
    raw = _sor_decode(req)
    try:
        return _sor_public(parse_sor(raw))
    except SorError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except (struct.error, IndexError, ValueError):
        raise HTTPException(status_code=422, detail="File SOR tidak dapat dibaca (struktur rusak)")


@app.post("/api/otdr/sor")
def otdr_sor_save(req: SorUpload):
    raw = _sor_decode(req)
    try:
        res = _sor_public(parse_sor(raw))
    except SorError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except (struct.error, IndexError, ValueError):
        raise HTTPException(status_code=422, detail="File SOR tidak dapat dibaca (struktur rusak)")
    with db() as conn:
        cursor = conn.cursor()
        dup = cursor.execute("SELECT id FROM sor_traces WHERE sha256 = ?", (res["sha256"],)).fetchone()
        if dup:
            raise HTTPException(status_code=409, detail=f"File yang sama sudah tersimpan (ID {dup['id']})")
        trace = res.pop("trace")
        cursor.execute(
            """INSERT INTO sor_traces (filename, sha256, size, meta, trace, file_blob, note, uploaded_by, uploaded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ((req.filename or "")[:200], res["sha256"], len(raw), json.dumps(res), json.dumps(trace), raw,
             (req.note or "")[:500], _current_username(), datetime.now(timezone.utc).isoformat()))
        new_id = cursor.lastrowid
        g = res["general"]
        _audit(cursor, "IMPORT", "OTDR_SOR", new_id, req.filename or "sor",
               f"Simpan file SOR {req.filename} (kabel {g.get('cable_id')}, serat {g.get('fiber_id')}, {len(res['events'])} event)")
    return {"id": new_id, "message": "File SOR tersimpan"}


def _sor_row(r, full=False) -> dict:
    meta = json.loads(r["meta"] or "{}")
    d = {"id": r["id"], "filename": r["filename"], "size": r["size"], "note": r["note"], "uploaded_by": r["uploaded_by"],
         "uploaded_at": r["uploaded_at"], "general": meta.get("general"), "fixed": meta.get("fixed"),
         "events": meta.get("events"), "supplier": meta.get("supplier"), "summary": meta.get("summary"),
         "format": meta.get("format"), "analysis": json.loads(r["analysis"]) if r["analysis"] else None}
    if full:
        d["trace"] = json.loads(r["trace"] or "{}")
    return d



# ---- jalur OTDR berbasis aset: awal + port + arah -> kabel berurutan -> posisi event di peta ----
SOR_SLACK_TYPES = {"SLACK", "CLOSURE"}


def _node_pt(n):
    return {"id": n["id"], "name": n["name"], "type": n["type"], "lat": n["latitude"], "lng": n["longitude"]}


def _orient_cable(cab, coords, start_node, nodes):
    """True bila perjalanan dari start_node searah titik pertama->terakhir (A->B)."""
    if cab["from_node_id"] == start_node["id"]:
        return True
    if cab["to_node_id"] == start_node["id"]:
        return False
    da = _haversine_m(coords[0][1], coords[0][0], start_node["latitude"], start_node["longitude"])
    db_ = _haversine_m(coords[-1][1], coords[-1][0], start_node["latitude"], start_node["longitude"])
    return da <= db_


def _sor_hops_auto(cursor, start_id, port, direction):
    names, conns = _trace_graph(cursor)
    idx = _hop_index(conns)
    key = ("NODE", start_id)
    pool = (idx[0] if direction == "down" else idx[1]).get(key, [])
    pf = "from_port_core" if direction == "down" else "to_port_core"
    first = [c for c in pool if (not port) or c.get(pf) == port]
    if not first:
        raise HTTPException(status_code=404, detail="Tidak ada sambungan " + ("keluar" if direction == "down" else "masuk") + (f" pada port '{port}'" if port else "") + " di aset awal")
    if len(first) > 1:
        raise HTTPException(status_code=409, detail="Aset awal punya lebih dari satu sambungan; pilih port tertentu")
    hops, notes, seen = [], [], set()
    c = first[0]
    while c and c["id"] not in seen and len(hops) < 300:
        seen.add(c["id"])
        hops.append((c, direction))
        nxt = _next_down(names, idx, c) if direction == "down" else _next_up(names, idx, c)
        nk = (c["to_asset_type"], c["to_asset_id"]) if direction == "down" else (c["from_asset_type"], c["from_asset_id"])
        if len(nxt) == 1:
            c = nxt[0]
        else:
            if len(nxt) > 1:
                notes.append(f"Jalur berhenti di {names.get(nk, ('?',))[0]}: bercabang ({len(nxt)} sambungan)")
            c = None
    return hops, notes


def _sor_build_path(cursor, start_id, port, direction, cable_ids=None):
    nodes = {r["id"]: r for r in cursor.execute("SELECT * FROM nodes").fetchall()}
    st = nodes.get(start_id)
    if not st:
        raise HTTPException(status_code=404, detail="Aset awal tidak ditemukan")
    segs, notes = [], []
    cur = st
    if cable_ids:
        for cid in cable_ids:
            cab = cursor.execute("SELECT * FROM cables WHERE id = ?", (cid,)).fetchone()
            if not cab:
                raise HTTPException(status_code=404, detail=f"Kabel #{cid} tidak ditemukan")
            coords = _cable_coords(cab)
            if not coords:
                raise HTTPException(status_code=400, detail=f"Kabel {cab['name']} tidak punya geometri")
            fwd = _orient_cable(cab, coords, cur, nodes)
            far_id = cab["to_node_id"] if fwd else cab["from_node_id"]
            far = nodes.get(far_id) if far_id else None
            if far is None:
                far = (_cable_ends(cab, nodes).get("B" if fwd else "A") or None)
                far = nodes.get(far["id"]) if far else None
            segs.append({"cable": cab, "coords": coords if fwd else coords[::-1], "from": cur, "to": far, "core": None, "port_in": None, "port_out": None})
            if far is None:
                notes.append(f"Ujung kabel {cab['name']} tidak terhubung ke aset; jalur manual berhenti di sini")
                break
            cur = far
        return segs, notes, nodes
    hops, notes = _sor_hops_auto(cursor, start_id, port, direction)
    for c, d in hops:
        a = nodes.get(c["from_asset_id"] if d == "down" else c["to_asset_id"])
        b = nodes.get(c["to_asset_id"] if d == "down" else c["from_asset_id"])
        if c["from_asset_type"] != "NODE" or c["to_asset_type"] != "NODE" or not a or not b:
            notes.append("Ada sambungan non-aset pada jalur; jalur dipotong di sini")
            break
        cab = cursor.execute("SELECT * FROM cables WHERE id = ?", (c.get("via_cable_id"),)).fetchone() if c.get("via_cable_id") else None
        coords = _cable_coords(cab) if cab else None
        if coords:
            fwd = _orient_cable(cab, coords, a, nodes)
            oc = coords if fwd else coords[::-1]
        else:
            oc = [[a["longitude"], a["latitude"]], [b["longitude"], b["latitude"]]]
            if cab is None:
                notes.append(f"Sambungan {a['name']} → {b['name']} tanpa kabel: jarak dihitung garis lurus")
        segs.append({"cable": cab, "coords": oc, "from": a, "to": b, "core": c.get("via_core"),
                     "port_in": c["from_port_core"] if d == "down" else c["to_port_core"],
                     "port_out": c["to_port_core"] if d == "down" else c["from_port_core"]})
    return segs, notes, nodes


def _sor_timeline(segs, slack_m, helix_pct):
    """Garis waktu optik: [{kind:'seg'|'slack', start, end, ...}], posisi tiap node sepanjang jalur."""
    tl, pos = [], 0.0
    nodes_on = []
    if segs:
        nodes_on.append({**_node_pt(segs[0]["from"]), "optical_m": 0.0, "slack": False})
    for i, sg in enumerate(segs):
        g = _polyline_length_m(sg["coords"])
        o = g * (1.0 + helix_pct / 100.0)
        tl.append({"kind": "seg", "i": i, "start": pos, "end": pos + o, "geo_m": g, "cable": sg["cable"]["name"] if sg["cable"] else None})
        pos += o
        to = sg["to"]
        if to is not None:
            has = (to["type"] or "").upper() in SOR_SLACK_TYPES and i < len(segs) - 1
            nd = {**_node_pt(to), "optical_m": pos, "slack": has}
            if has and slack_m > 0:
                tl.append({"kind": "slack", "i": i, "start": pos, "end": pos + slack_m, "node": to})
                pos += slack_m
                nd["optical_end_m"] = pos
            nodes_on.append(nd)
    return tl, nodes_on, pos


def _sor_locate(segs, tl, d, helix_pct):
    for t in tl:
        if d <= t["end"] + 1e-9 or t is tl[-1]:
            if t["kind"] == "slack":
                n = t["node"]
                return {"lat": n["latitude"], "lng": n["longitude"], "in": "slack", "node": n["name"], "segment": t["i"]}
            g = max(0.0, min((d - t["start"]) / (1.0 + helix_pct / 100.0), t["geo_m"]))
            lat, lng = _points_along(segs[t["i"]]["coords"], [g])[0]
            return {"lat": lat, "lng": lng, "in": "cable", "geo_from_seg_start_m": round(g, 1), "segment": t["i"]}
    return None


def _sor_sigma(d, n_slack, slack_m, p, spacing_m):
    comp = {"slack": 0.4 * slack_m * math.sqrt(n_slack) if n_slack else 0.0,
            "rute": d * p["otdr_route_err_pct"] / 100.0 / 2.0,
            "helix": d * 0.005,
            "alat": math.sqrt((spacing_m or 1.3) ** 2 + (d * 0.0005) ** 2)}
    tot = math.sqrt(sum(v * v for v in comp.values()))
    return tot, {k: round(v, 1) for k, v in comp.items()}


def _sor_reflective(e):
    c = str(e.get("type_code") or "")
    return c[:1] in ("1", "2")


class SorAnalyze(BaseModel):
    sor_id: Optional[int] = None
    content_base64: Optional[str] = ""
    filename: str = ""
    start_node_id: int
    start_port: Optional[str] = None
    direction: Optional[str] = "down"          # down | up
    cable_ids: Optional[List[int]] = None       # jalur manual (urut dari aset awal)
    slack_m: Optional[float] = None
    launch_offset_m: Optional[float] = 0.0      # panjang kabel launch / patchcord sebelum aset awal
    save: Optional[bool] = False


@app.get("/api/otdr/sor/start-nodes")
def otdr_sor_start_nodes(q: str = "", limit: int = 30):
    like = "%" + (q or "").strip().lower() + "%"
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT n.id, n.name, n.type FROM nodes n JOIN core_connections c ON "
            "((c.from_asset_type='NODE' AND c.from_asset_id=n.id) OR (c.to_asset_type='NODE' AND c.to_asset_id=n.id)) "
            "WHERE LOWER(n.name) LIKE ? ORDER BY n.name LIMIT ?", (like, max(1, min(limit, 100)))).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/nodes/{node_id}/otdr-starts")
def otdr_sor_starts(node_id: int):
    """Pilihan titik awal ukur pada sebuah aset: tiap sambungan (port, arah, tujuan berikutnya)."""
    with db() as conn:
        cursor = conn.cursor()
        n = cursor.execute("SELECT id, name, type FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if not n:
            raise HTTPException(status_code=404, detail="Aset tidak ditemukan")
        names, conns = _trace_graph(cursor)
    out = []
    for c in conns:
        if c["from_asset_type"] == "NODE" and c["from_asset_id"] == node_id:
            other = names.get((c["to_asset_type"], c["to_asset_id"]), ("?",))[0]
            out.append({"port": c["from_port_core"], "direction": "down", "to": other, "to_port": c["to_port_core"],
                        "cable": c.get("via_cable_name"), "core": c.get("via_core")})
        if c["to_asset_type"] == "NODE" and c["to_asset_id"] == node_id:
            other = names.get((c["from_asset_type"], c["from_asset_id"]), ("?",))[0]
            out.append({"port": c["to_port_core"], "direction": "up", "to": other, "to_port": c["from_port_core"],
                        "cable": c.get("via_cable_name"), "core": c.get("via_core")})
    out.sort(key=lambda x: (x["port"] or "", x["direction"]))
    return {"node": dict(n), "starts": out}


def _sor_run(cursor, req, res, p):
    slack_m = p["otdr_slack_m"] if req.slack_m is None else float(req.slack_m)
    if not (0 <= slack_m <= 200):
        raise HTTPException(status_code=400, detail="Slack harus 0-200 m")
    launch = float(req.launch_offset_m or 0.0)
    if not (0 <= launch <= 5000):
        raise HTTPException(status_code=400, detail="Offset launch harus 0-5000 m")
    direction = (req.direction or "down").lower()
    if direction not in ("down", "up"):
        raise HTTPException(status_code=400, detail="Arah harus down atau up")
    segs, notes, _nodes = _sor_build_path(cursor, req.start_node_id, (req.start_port or "").strip() or None, direction, req.cable_ids or None)
    if not segs:
        raise HTTPException(status_code=400, detail="Jalur kosong")
    tl, nodes_on, total = _sor_timeline(segs, slack_m, p["otdr_helix_pct"])
    spacing = (res.get("trace") or {}).get("spacing_m")
    n_slack_all = sum(1 for t in tl if t["kind"] == "slack")
    evs = []
    for e in res.get("events") or []:
        d = e["distance_m"] - launch
        if d < -1e-6:
            evs.append({"no": e["no"], "optical_m": e["distance_m"], "before_start": True})
            continue
        n_sl = sum(1 for t in tl if t["kind"] == "slack" and t["end"] <= d + 1e-9)
        loc = _sor_locate(segs, tl, min(d, total), p["otdr_helix_pct"])
        sig, comp = _sor_sigma(d, n_sl, slack_m, p, spacing)
        near = sorted(nodes_on, key=lambda n: abs(n["optical_m"] - d))[0]
        evs.append({"no": e["no"], "optical_m": round(e["distance_m"], 1), "path_m": round(d, 1), "beyond_path": d > total + 1e-6,
                    "slack_passed": n_sl, "sigma_m": round(sig, 1), "uncertainty_m": round(2 * sig, 1), "components": comp,
                    "lat": loc["lat"] if loc else None, "lng": loc["lng"] if loc else None, "in": loc["in"] if loc else None,
                    "segment": loc["segment"] if loc else None,
                    "segment_cable": (segs[loc["segment"]]["cable"]["name"] if loc and segs[loc["segment"]]["cable"] else None),
                    "segment_cable_id": (segs[loc["segment"]]["cable"]["id"] if loc and segs[loc["segment"]]["cable"] else None),
                    "segment_core": (segs[loc["segment"]]["core"] if loc else None),
                    "near_node": near["name"], "near_gap_m": round(d - near["optical_m"], 1)})
    # klasifikasi event terakhir
    end = None
    if res.get("events"):
        le = res["events"][-1]
        d = le["distance_m"] - launch
        tol = p["otdr_normal_pct"] / 100.0
        diff = d - total
        pct = (diff / total * 100.0) if total else 0.0
        refl = _sor_reflective(le)
        if abs(diff) <= tol * total:
            kind, msg = "NORMAL", "Event akhir sesuai panjang jalur (selisih %.1f%%): ujung serat normal." % pct
        elif diff < 0 and refl:
            kind, msg = "BREAK", "Event akhir lebih pendek %.0f m dari jalur dan reflektif: indikasi serat patah/putus." % (-diff)
        elif diff < 0:
            kind, msg = "BEND", "Event akhir lebih pendek %.0f m dari jalur dan tidak reflektif: indikasi tekukan tajam (macrobend) atau sambungan buruk." % (-diff)
        else:
            kind, msg = "LONGER", "Event akhir lebih panjang %.0f m dari jalur: data jalur belum lengkap (kabel/slack belum tercatat) atau titik awal/arah keliru." % diff
        end = {"kind": kind, "message": msg, "event_no": le["no"], "reflective": refl, "end_m": round(d, 1), "path_m": round(total, 1),
               "diff_m": round(diff, 1), "diff_pct": round(pct, 2), "tolerance_pct": p["otdr_normal_pct"]}
        ev_last = next((x for x in evs if x["no"] == le["no"]), None)
        if ev_last and kind in ("BREAK", "BEND"):
            end["lat"], end["lng"] = ev_last.get("lat"), ev_last.get("lng")
            end["uncertainty_m"] = ev_last.get("uncertainty_m")
            end["slack_passed"] = ev_last.get("slack_passed")
            end["near_node"], end["near_gap_m"] = ev_last.get("near_node"), ev_last.get("near_gap_m")
            end["segment_cable"], end["segment_cable_id"], end["segment_core"] = ev_last.get("segment_cable"), ev_last.get("segment_cable_id"), ev_last.get("segment_core")
    path = {"start": _node_pt(segs[0]["from"]), "start_port": segs[0].get("port_in"), "direction": direction,
            "slack_m": slack_m, "launch_offset_m": launch, "helix_pct": p["otdr_helix_pct"], "optical_total_m": round(total, 1),
            "geo_total_m": round(sum(t["geo_m"] for t in tl if t["kind"] == "seg"), 1), "slack_count": n_slack_all,
            "manual": bool(req.cable_ids), "nodes": [{k: v for k, v in n.items()} for n in nodes_on],
            "segments": [{"cable_id": s["cable"]["id"] if s["cable"] else None, "cable": s["cable"]["name"] if s["cable"] else None,
                          "core": s["core"], "from": s["from"]["name"], "to": s["to"]["name"] if s["to"] else None,
                          "geo_m": round(_polyline_length_m(s["coords"]), 1),
                          "line": [[c[1], c[0]] for c in s["coords"]]} for s in segs]}
    return {"path": path, "events": evs, "end": end, "notes": notes}


@app.post("/api/otdr/sor/analyze")
def otdr_sor_analyze(req: SorAnalyze):
    with db() as conn:
        cursor = conn.cursor()
        if req.sor_id:
            r = cursor.execute("SELECT id, filename, meta, trace FROM sor_traces WHERE id = ?", (req.sor_id,)).fetchone()
            if not r:
                raise HTTPException(status_code=404, detail="File SOR tidak ditemukan")
            res = json.loads(r["meta"]); res["trace"] = json.loads(r["trace"])
        else:
            try:
                res = _sor_public(parse_sor(_sor_decode(req)))
            except SorError as e:
                raise HTTPException(status_code=422, detail=str(e))
        p = _loss_params(cursor)
        out = _sor_run(cursor, req, res, p)
        if req.save:
            if not req.sor_id:
                raise HTTPException(status_code=400, detail="Simpan berkas SOR dahulu sebelum menyimpan analisis")
            cursor.execute("UPDATE sor_traces SET analysis = ? WHERE id = ?", (json.dumps({**out, "request": {
                "start_node_id": req.start_node_id, "start_port": req.start_port, "direction": req.direction,
                "cable_ids": req.cable_ids, "slack_m": req.slack_m, "launch_offset_m": req.launch_offset_m}}), req.sor_id))
            _audit(cursor, "UPDATE", "OTDR_SOR", req.sor_id, r["filename"], f"Simpan analisis jalur SOR {r['filename']} ({(out['end'] or {}).get('kind', '-')})")
    return out



# ---------------- SOR lanjutan: tiket, baseline, dua arah ----------------
SOR_WL_BUCKETS = [850, 1310, 1383, 1490, 1550, 1625, 1650]


def _sor_bkey(meta: dict) -> str:
    g, fx = meta.get("general") or {}, meta.get("fixed") or {}
    wl = fx.get("wavelength_nm") or g.get("wavelength_nm") or 0
    b = min(SOR_WL_BUCKETS, key=lambda x: abs(x - wl)) if wl else 0
    return f"{(g.get('cable_id') or '').strip().lower()}|{(g.get('fiber_id') or '').strip().lower()}|{b}"


def _sor_load(cursor, sor_id):
    r = cursor.execute("SELECT * FROM sor_traces WHERE id = ?", (sor_id,)).fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="File SOR tidak ditemukan")
    meta = json.loads(r["meta"] or "{}")
    meta["trace"] = json.loads(r["trace"] or "{}")
    return r, meta


@app.post("/api/otdr/sor/{sor_id}/baseline")
def otdr_sor_set_baseline(sor_id: int):
    with db() as conn:
        cursor = conn.cursor()
        r, meta = _sor_load(cursor, sor_id)
        key = _sor_bkey(meta)
        if key.startswith("|"):
            raise HTTPException(status_code=400, detail="Berkas tidak punya ID kabel/serat; baseline tidak bisa dikelompokkan")
        for o in cursor.execute("SELECT id, meta FROM sor_traces WHERE is_baseline = 1 AND id != ?", (sor_id,)).fetchall():
            if _sor_bkey(json.loads(o["meta"] or "{}")) == key:
                cursor.execute("UPDATE sor_traces SET is_baseline = 0, baseline_by = NULL WHERE id = ?", (o["id"],))
        cursor.execute("UPDATE sor_traces SET is_baseline = 1, baseline_by = ? WHERE id = ?", (_current_username(), sor_id))
        _audit(cursor, "UPDATE", "OTDR_SOR", sor_id, r["filename"], f"Jadikan baseline {key}")
    return {"message": "Dijadikan baseline", "key": key}


@app.delete("/api/otdr/sor/{sor_id}/baseline")
def otdr_sor_unset_baseline(sor_id: int):
    with db() as conn:
        cursor = conn.cursor()
        r, _m = _sor_load(cursor, sor_id)
        cursor.execute("UPDATE sor_traces SET is_baseline = 0, baseline_by = NULL WHERE id = ?", (sor_id,))
        _audit(cursor, "UPDATE", "OTDR_SOR", sor_id, r["filename"], "Lepas status baseline")
    return {"message": "Baseline dilepas"}


def _sor_match_events(base, new, spacing):
    used, rows = set(), []
    for ne in new:
        tol = max(3 * (spacing or 1.3), 0.005 * ne["distance_m"], 5.0)
        best = None
        for be in base:
            if be["no"] in used:
                continue
            dd = abs(be["distance_m"] - ne["distance_m"])
            if dd <= tol and (best is None or dd < best[0]):
                best = (dd, be)
        if best:
            used.add(best[1]["no"])
        rows.append((best[1] if best else None, ne))
    lost = [be for be in base if be["no"] not in used]
    return rows, lost


def _sor_compare(bm, nm, p):
    sp = (nm.get("trace") or {}).get("spacing_m")
    rows, lost = _sor_match_events(bm.get("events") or [], nm.get("events") or [], sp)
    wb, wn = p["event_warn_db"], p["event_bad_db"]
    out = []

    def lvl(delta):
        return "BAD" if delta >= p["event_bad_db"] else "WARN" if delta >= p["event_warn_db"] else "OK"

    def refl(e):
        r = e.get("reflectance_db")
        return r if r is not None and r > -1000 else None
    for be, ne in rows:
        if be is None:
            st = "BAD" if ne.get("splice_loss_db", 0) >= wn else "WARN"
            out.append({"kind": "NEW", "new_no": ne["no"], "distance_m": round(ne["distance_m"], 1), "new_loss": ne.get("splice_loss_db"),
                        "new_refl": refl(ne), "status": st})
            continue
        dl = (ne.get("splice_loss_db") or 0) - (be.get("splice_loss_db") or 0)
        rb, rn = refl(be), refl(ne)
        drf = (rn - rb) if (rb is not None and rn is not None) else None
        st = lvl(dl)
        if st == "OK" and drf is not None and abs(drf) >= 3:
            st = "WARN"
        out.append({"kind": "CHANGED" if st != "OK" else "SAME", "base_no": be["no"], "new_no": ne["no"], "distance_m": round(ne["distance_m"], 1),
                    "shift_m": round(ne["distance_m"] - be["distance_m"], 1), "base_loss": be.get("splice_loss_db"), "new_loss": ne.get("splice_loss_db"),
                    "delta_loss": round(dl, 3), "base_refl": rb, "new_refl": rn, "delta_refl": round(drf, 2) if drf is not None else None, "status": st})
    for be in lost:
        out.append({"kind": "LOST", "base_no": be["no"], "distance_m": round(be["distance_m"], 1), "base_loss": be.get("splice_loss_db"), "status": "WARN"})
    out.sort(key=lambda r: r["distance_m"])
    tb, tn = (bm.get("summary") or {}).get("total_loss_db"), (nm.get("summary") or {}).get("total_loss_db")
    dt = round(tn - tb, 3) if tb is not None and tn is not None else None
    eb = (bm.get("events") or [{}])[-1].get("distance_m")
    en = (nm.get("events") or [{}])[-1].get("distance_m")
    de = round(en - eb, 1) if eb is not None and en is not None else None
    tot_st = "OK"
    if dt is not None:
        tot_st = "BAD" if abs(dt) >= p["otdr_bad_db"] else "WARN" if abs(dt) >= p["otdr_warn_db"] else "OK"
    end_st = "OK"
    if de is not None and eb:
        pct = abs(de) / eb * 100.0
        end_st = "BAD" if pct > p["otdr_normal_pct"] * 2 else "WARN" if pct > p["otdr_normal_pct"] else "OK"
    sts = [r["status"] for r in out] + [tot_st, end_st]
    overall = "BAD" if "BAD" in sts else "WARN" if "WARN" in sts else "OK"
    return {"overall": overall, "total": {"base": tb, "new": tn, "delta": dt, "status": tot_st},
            "end": {"base_m": eb, "new_m": en, "delta_m": de, "status": end_st}, "events": out,
            "wavelength_diff_nm": round(((nm.get("fixed") or {}).get("wavelength_nm") or 0) - ((bm.get("fixed") or {}).get("wavelength_nm") or 0), 1)}


@app.get("/api/otdr/sor/{sor_id}/compare")
def otdr_sor_compare(sor_id: int, base_id: Optional[int] = None):
    with db() as conn:
        cursor = conn.cursor()
        r, nm = _sor_load(cursor, sor_id)
        key = _sor_bkey(nm)
        if base_id is None and r["is_baseline"]:
            raise HTTPException(status_code=400, detail="Berkas ini sendiri adalah baseline")
        if base_id is None:
            for o in cursor.execute("SELECT id, meta FROM sor_traces WHERE is_baseline = 1 AND id != ?", (sor_id,)).fetchall():
                if _sor_bkey(json.loads(o["meta"] or "{}")) == key:
                    base_id = o["id"]
                    break
        if base_id is None:
            raise HTTPException(status_code=404, detail="Belum ada baseline untuk kabel/serat/panjang gelombang ini")
        if base_id == sor_id:
            raise HTTPException(status_code=400, detail="Berkas ini sendiri adalah baseline")
        b, bm = _sor_load(cursor, base_id)
        p = _loss_params(cursor)
        res = _sor_compare(bm, nm, p)
        res.update({"base_id": base_id, "base_filename": b["filename"], "base_time": (bm.get("fixed") or {}).get("timestamp"),
                    "new_time": (nm.get("fixed") or {}).get("timestamp"), "key_match": _sor_bkey(bm) == key,
                    "base_trace": (bm.get("trace") or {}).get("points", [])})
    return res


class SorIncident(BaseModel):
    severity: Optional[str] = "Critical"
    note: Optional[str] = ""


@app.post("/api/otdr/sor/{sor_id}/incident")
def otdr_sor_incident(sor_id: int, req: SorIncident):
    with db() as conn:
        cursor = conn.cursor()
        r = cursor.execute("SELECT id, filename, meta, analysis, incident_id FROM sor_traces WHERE id = ?", (sor_id,)).fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="File SOR tidak ditemukan")
        if not r["analysis"]:
            raise HTTPException(status_code=400, detail="Simpan analisis jalur dahulu")
        if r["incident_id"]:
            t = cursor.execute("SELECT ticket_number FROM incidents WHERE id = ?", (r["incident_id"],)).fetchone()
            if t:
                raise HTTPException(status_code=409, detail=f"Tiket {t['ticket_number']} sudah dibuat dari berkas ini")
        a = json.loads(r["analysis"])
        meta = json.loads(r["meta"] or "{}")
    e = a.get("end") or {}
    if e.get("kind") not in ("BREAK", "BEND") or e.get("lat") is None:
        raise HTTPException(status_code=400, detail="Hasil analisis tidak menunjukkan titik gangguan (hanya Putus/Tekukan yang bisa dijadikan tiket)")
    sev = (req.severity or "Critical")
    if sev not in ("Critical", "Major", "Minor", "Warning"):
        sev = "Critical"
    g = meta.get("general") or {}
    core_no = None
    m = re.search(r"(\d+)\s*$", str(e.get("segment_core") or ""))
    if m:
        core_no = int(m.group(1))
    desc = [f"Dibuat otomatis dari OTDR (SOR): {r['filename']}",
            f"Kabel/serat terukur: {g.get('cable_id') or '-'} / {g.get('fiber_id') or '-'}",
            e.get("message") or "",
            f"Estimasi titik: {e['lat']:.6f}, {e['lng']:.6f} (±{e.get('uncertainty_m')} m, 95%)",
            f"Slack terlewati: {e.get('slack_passed')}; dekat {e.get('near_node') or '?'} ({e.get('near_gap_m')} m)"]
    if e.get("method") == "TWOWAY":
        desc.append("Metode: gabungan dua arah (SOR dari kedua ujung)")
    if (req.note or "").strip():
        desc.append("Catatan: " + req.note.strip())
    ticket = "SOR-" + datetime.now().strftime("%y%m%d%H%M%S") + f"-{sor_id}"
    cable_id = e.get("segment_cable_id")
    inc = IncidentCreate(ticket_number=ticket, title=f"{'Fiber putus' if e['kind'] == 'BREAK' else 'Degradasi serat'} - {g.get('cable_id') or e.get('segment_cable') or 'OTDR'}",
                         severity=sev, incident_type="FO Cut" if e["kind"] == "BREAK" else "Fiber Degradation", status="Open",
                         description="\n".join(x for x in desc if x), latitude=e["lat"], longitude=e["lng"], linked_cable_id=cable_id,
                         affected_cores=[core_no] if (core_no and cable_id) else None, reporter=_current_username())
    try:
        res = create_incident(inc)
    except HTTPException as ex:
        if ex.status_code == 400 and inc.affected_cores:
            inc.affected_cores = None
            res = create_incident(inc)
        else:
            raise
    with db() as conn:
        conn.execute("UPDATE sor_traces SET incident_id = ? WHERE id = ?", (res["id"], sor_id))
        _log_event(conn.cursor(), res["id"], "NOTE", f"Sumber: analisis OTDR {r['filename']} (SOR #{sor_id})", {"sor_id": sor_id, "end": {k: e.get(k) for k in ("kind", "lat", "lng", "uncertainty_m")}})
    return {"id": res["id"], "ticket_number": res["ticket_number"], "linked_cable_id": res["linked_cable_id"], "notes": res.get("notes")}


class SorAnalyze2(BaseModel):
    sor_id: int
    sor_id_b: int
    start_node_id: int
    start_port: Optional[str] = None
    direction: Optional[str] = "down"
    cable_ids: Optional[List[int]] = None
    slack_m: Optional[float] = None
    launch_offset_m: Optional[float] = 0.0
    launch_offset_b_m: Optional[float] = 0.0
    save: Optional[bool] = False


@app.post("/api/otdr/sor/analyze2")
def otdr_sor_analyze2(req: SorAnalyze2):
    if req.sor_id == req.sor_id_b:
        raise HTTPException(status_code=400, detail="Pilih dua berkas SOR yang berbeda")
    with db() as conn:
        cursor = conn.cursor()
        ra, ma = _sor_load(cursor, req.sor_id)
        rb, mb = _sor_load(cursor, req.sor_id_b)
        ga, gb = ma.get("general") or {}, mb.get("general") or {}
        warn = []
        if (ga.get("fiber_id") or "") != (gb.get("fiber_id") or ""):
            warn.append(f"ID serat berbeda ({ga.get('fiber_id') or '-'} vs {gb.get('fiber_id') or '-'}); pastikan keduanya serat yang sama")
        p = _loss_params(cursor)
        one = SorAnalyze(sor_id=req.sor_id, start_node_id=req.start_node_id, start_port=req.start_port, direction=req.direction,
                         cable_ids=req.cable_ids, slack_m=req.slack_m, launch_offset_m=req.launch_offset_m)
        out = _sor_run(cursor, one, ma, p)
        direction = (req.direction or "down").lower()
        segs, _n, _nodes = _sor_build_path(cursor, req.start_node_id, (req.start_port or "").strip() or None, direction, req.cable_ids or None)
        slack_m = p["otdr_slack_m"] if req.slack_m is None else float(req.slack_m)
        tl, nodes_on, P = _sor_timeline(segs, slack_m, p["otdr_helix_pct"])
        if not ma.get("events") or not mb.get("events"):
            raise HTTPException(status_code=422, detail="Salah satu SOR tidak punya event")
        da = ma["events"][-1]["distance_m"] - float(req.launch_offset_m or 0)
        dbb = mb["events"][-1]["distance_m"] - float(req.launch_offset_b_m or 0)
        tol = p["otdr_normal_pct"] / 100.0
        if da >= P * (1 - tol) or dbb >= P * (1 - tol):
            raise HTTPException(status_code=422, detail="Dua arah hanya berlaku bila kedua SOR berhenti sebelum ujung jalur (ada putus). "
                                                         f"Jarak A {da:.0f} m, jarak B {dbb:.0f} m, panjang jalur {P:.0f} m.")
        spacing = (ma.get("trace") or {}).get("spacing_m")
        xa, xb = da, P - dbb
        n_a = sum(1 for t in tl if t["kind"] == "slack" and t["end"] <= xa + 1e-9)
        n_b = sum(1 for t in tl if t["kind"] == "slack" and t["start"] >= xb - 1e-9)
        sa, comp_a = _sor_sigma(da, n_a, slack_m, p, spacing)
        sb, comp_b = _sor_sigma(dbb, n_b, slack_m, p, (mb.get("trace") or {}).get("spacing_m"))
        gap = P - da - dbb                       # >0: jumlah dua jarak lebih pendek dari model jalur
        w = sa * sa / (sa * sa + sb * sb)
        x = xa + gap * w
        sf = math.sqrt(sa * sa * sb * sb / (sa * sa + sb * sb))
        consistent = abs(gap) <= 3 * math.sqrt(sa * sa + sb * sb)
        loc = _sor_locate(segs, tl, max(0.0, min(x, P)), p["otdr_helix_pct"])
        near = sorted(nodes_on, key=lambda n: abs(n["optical_m"] - x))[0]
        refl = _sor_reflective(ma["events"][-1]) or _sor_reflective(mb["events"][-1])
        kind = "BREAK" if refl else "BEND"
        seg = segs[loc["segment"]] if loc else None
        msg = (f"Titik cut gabungan dua arah: {x:.0f} m dari titik awal A (A: {xa:.0f} m, B: {xb:.0f} m, selisih {abs(gap):.0f} m). "
               + ("Dua pengukuran konsisten." if consistent else "PERINGATAN: dua pengukuran tidak konsisten (bukan titik yang sama, jalur belum lengkap, atau serat berbeda)."))
        end = {"kind": kind, "method": "TWOWAY", "message": msg, "event_no": ma["events"][-1]["no"], "reflective": refl,
               "end_m": round(da, 1), "path_m": round(P, 1), "diff_m": round(da - P, 1), "diff_pct": round((da - P) / P * 100.0, 2),
               "tolerance_pct": p["otdr_normal_pct"], "lat": loc["lat"] if loc else None, "lng": loc["lng"] if loc else None,
               "uncertainty_m": round(2 * sf, 1), "slack_passed": n_a, "near_node": near["name"], "near_gap_m": round(x - near["optical_m"], 1),
               "segment_cable": seg["cable"]["name"] if seg and seg["cable"] else None, "segment_cable_id": seg["cable"]["id"] if seg and seg["cable"] else None,
               "segment_core": seg["core"] if seg else None,
               "twoway": {"x_a_m": round(xa, 1), "x_b_m": round(xb, 1), "fused_m": round(x, 1), "gap_m": round(gap, 1), "consistent": consistent,
                          "uncertainty_a_m": round(2 * sa, 1), "uncertainty_b_m": round(2 * sb, 1), "uncertainty_fused_m": round(2 * sf, 1),
                          "weight_a": round(w, 3), "sor_b": req.sor_id_b, "sor_b_filename": rb["filename"], "launch_b_m": float(req.launch_offset_b_m or 0)}}
        out["end"] = end
        out["notes"] = (out.get("notes") or []) + warn
        out["method"] = "TWOWAY"
        if req.save:
            cursor.execute("UPDATE sor_traces SET analysis = ? WHERE id = ?", (json.dumps({**out, "request": {
                "start_node_id": req.start_node_id, "start_port": req.start_port, "direction": req.direction, "cable_ids": req.cable_ids,
                "slack_m": req.slack_m, "launch_offset_m": req.launch_offset_m, "sor_id_b": req.sor_id_b, "launch_offset_b_m": req.launch_offset_b_m}}), req.sor_id))
            _audit(cursor, "UPDATE", "OTDR_SOR", req.sor_id, ra["filename"], f"Simpan analisis dua arah dengan {rb['filename']} ({kind})")
    return out


@app.get("/api/otdr/sor/map")
def otdr_sor_map():
    """Titik cut/akhir dari analisis SOR yang tersimpan, untuk layer peta."""
    feats = []
    with db() as conn:
        for r in conn.execute("SELECT id, filename, meta, analysis, uploaded_at FROM sor_traces WHERE analysis IS NOT NULL ORDER BY id DESC LIMIT 200").fetchall():
            a = json.loads(r["analysis"])
            g = (json.loads(r["meta"]).get("general") or {})
            e = a.get("end") or {}
            feats.append({"id": r["id"], "filename": r["filename"], "cable": g.get("cable_id"), "fiber": g.get("fiber_id"),
                          "kind": e.get("kind"), "message": e.get("message"), "lat": e.get("lat"), "lng": e.get("lng"),
                          "uncertainty_m": e.get("uncertainty_m"), "slack_passed": e.get("slack_passed"),
                          "near_node": e.get("near_node"), "near_gap_m": e.get("near_gap_m"),
                          "path": [s["line"] for s in (a.get("path") or {}).get("segments", [])],
                          "start": (a.get("path") or {}).get("start")})
    return feats


@app.get("/api/otdr/sor")
def otdr_sor_list(limit: int = 100):
    out = []
    with db() as conn:
        rows = conn.execute("SELECT id, filename, size, meta, note, uploaded_by, uploaded_at, analysis, incident_id, is_baseline, '{}' AS trace FROM sor_traces "
                            "ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),)).fetchall()
        for r in rows:
            d = _sor_row(r)
            d["n_events"] = len(d.get("events") or [])
            d.pop("events", None)
            an = d.get("analysis") or {}
            d["analysis"] = {"kind": (an.get("end") or {}).get("kind"), "start": (an.get("path") or {}).get("start"),
                             "method": (an.get("end") or {}).get("method")} if an else None
            d["is_baseline"] = bool(r["is_baseline"])
            d["bkey"] = _sor_bkey(json.loads(r["meta"] or "{}"))
            d["incident"] = None
            if r["incident_id"]:
                t = conn.execute("SELECT id, ticket_number FROM incidents WHERE id = ?", (r["incident_id"],)).fetchone()
                d["incident"] = dict(t) if t else None
            out.append(d)
    return out


@app.get("/api/otdr/sor/{sor_id}")
def otdr_sor_get(sor_id: int):
    with db() as conn:
        r = conn.execute("SELECT id, filename, size, meta, trace, note, uploaded_by, uploaded_at, analysis, incident_id, is_baseline FROM sor_traces WHERE id = ?",
                         (sor_id,)).fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="File SOR tidak ditemukan")
        d = _sor_row(r, full=True)
        d["is_baseline"] = bool(r["is_baseline"])
        d["bkey"] = _sor_bkey(json.loads(r["meta"] or "{}"))
        d["incident"] = None
        if r["incident_id"]:
            t = conn.execute("SELECT id, ticket_number FROM incidents WHERE id = ?", (r["incident_id"],)).fetchone()
            d["incident"] = dict(t) if t else None
    return d


@app.get("/api/otdr/sor/{sor_id}/file")
def otdr_sor_file(sor_id: int):
    with db() as conn:
        r = conn.execute("SELECT filename, file_blob FROM sor_traces WHERE id = ?", (sor_id,)).fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="File SOR tidak ditemukan")
    fn = re.sub(r"[^A-Za-z0-9._-]", "_", r["filename"] or f"trace_{sor_id}.sor")
    return Response(content=bytes(r["file_blob"]), media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{fn}"'})


@app.delete("/api/otdr/sor/{sor_id}")
def otdr_sor_delete(sor_id: int):
    with db() as conn:
        cursor = conn.cursor()
        r = cursor.execute("SELECT id, filename FROM sor_traces WHERE id = ?", (sor_id,)).fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="File SOR tidak ditemukan")
        cursor.execute("DELETE FROM sor_traces WHERE id = ?", (sor_id,))
        _audit(cursor, "DELETE", "OTDR_SOR", sor_id, r["filename"], f"Hapus file SOR {r['filename']}")
    return {"message": "File SOR dihapus"}


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
def otdr_map(cluster: str = "ALL", area: str = "ALL", events: str = "issues", region: str = "ALL"):
    """GeoJSON layer OTDR: kabel yang punya hasil ukur (warna = status terburuk) + titik event (default hanya yang bermasalah)."""
    sc, sp = _scope_conds(cluster, area, "c", region)
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
def otdr_segments(q: str = "", cluster: str = "ALL", area: str = "ALL", limit: int = 300, region: str = "ALL"):
    """Daftar segmen (kabel) yang bisa diukur: kabel + aset di ujung A dan B."""
    limit = max(1, min(int(limit), 1000))
    sc, sp = _scope_conds(cluster, area, "c", region)
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


# =====================================================================================
# CEK COVERAGE MASSAL: impor Excel -> hitung per lokasi (catuan terdekat, tarikan, biaya KHS) -> unduh Excel
# =====================================================================================
BULKCOV_MAX_ROWS = 500          # lokasi per berkas
BULKCOV_RUN_MAX = 25            # lokasi per permintaan hitung (antarmuka memecah per 8)
BULKCOV_FAR_M = 5000.0          # > 5 km dari catuan layak: status T + keterangan penarikan baru jauh
BULKCOV_DETOUR = 1.3            # rute jalan tidak tersedia -> garis lurus x 1,3
BULKCOV_ROUTE_PER_MIN = 200
BULKCOV_ELIG_TOP = 2            # catuan layak terdekat (garis lurus) yang dihitung rute jalannya
BULKCOV_TEMPLATE_HEAD = ["No", "Nama Lokasi", "Area", "Titik Koordinat"]


def _bc_norm(v) -> str:
    return re.sub(r"[^a-z0-9]", "", str(v or "").lower())


def _bc_parse_coord(text):
    """'-3.3136240, 114.58983' (juga koma desimal / spasi) -> (lat, lng) atau None."""
    s = str(text or "").replace("−", "-").strip()
    if not s:
        return None
    nums = re.findall(r"-?\d+(?:[.,]\d+)?", s)
    if len(nums) != 2:
        return None
    try:
        a, b = (float(x.replace(",", ".")) for x in nums)
    except ValueError:
        return None
    if abs(a) > 90 and abs(b) <= 90:      # terbalik (lng, lat)
        a, b = b, a
    if not (-90 <= a <= 90 and -180 <= b <= 180):
        return None
    return a, b


def _bc_find_header(rows):
    """Cari baris judul (maks 40 baris pertama). -> (indeks_baris, {kolom: indeks}) atau None."""
    for ri, row in enumerate(rows[:40]):
        cols = {}
        for j, v in enumerate(row):
            k = _bc_norm(v)
            if not k:
                continue
            if "name" not in cols and (k in ("nama", "lokasi", "namalokasi", "namaoutlet", "namatitik", "outlet", "lokasiatm") or k.startswith("namalokasi")):
                cols["name"] = j
            elif "coord" not in cols and ("koordinat" in k or k in ("latlong", "latlng", "latitudelongitude", "koordinatlatlong")):
                cols["coord"] = j
            elif "lat" not in cols and k in ("lat", "latitude", "lintang"):
                cols["lat"] = j
            elif "lng" not in cols and k in ("lng", "long", "lon", "longitude", "bujur"):
                cols["lng"] = j
            elif "area" not in cols and k in ("area", "kota", "kabupaten", "kotakabupaten", "kabkota", "wilayah", "kotamadya"):
                cols["area"] = j
            elif "no" not in cols and k in ("no", "nomor", "nourut"):
                cols["no"] = j
        if "name" in cols and ("coord" in cols or ("lat" in cols and "lng" in cols)):
            return ri, cols
    return None


def _bc_parse_rows(raw: bytes):
    sheets = _xlsx_read(raw)
    found = None
    for nm, rows in sheets:
        h = _bc_find_header(rows)
        if h:
            found = (nm, rows, h)
            break
    if not found:
        raise HTTPException(status_code=400, detail="Judul kolom tidak dikenali. Butuh kolom 'Nama Lokasi' dan 'Titik Koordinat' "
                                                    "(atau kolom Lat dan Lng terpisah). Unduh template untuk contoh format.")
    nm, rows, (hri, cols) = found
    out, skipped = [], 0
    for ri in range(hri + 1, len(rows)):
        row = rows[ri]

        def g(k):
            j = cols.get(k)
            return str(row[j]).strip() if j is not None and j < len(row) and row[j] is not None else ""
        if not any(str(c).strip() for c in row):
            continue
        name, area = g("name"), g("area")
        if not name and not g("coord") and not (g("lat") or g("lng")):
            skipped += 1
            continue
        no_raw = g("no")
        try:
            no = int(float(no_raw)) if no_raw else len(out) + 1
        except ValueError:
            no = len(out) + 1
        coord_txt = g("coord") if "coord" in cols else f"{g('lat')}, {g('lng')}"
        pc = _bc_parse_coord(coord_txt)
        item = {"no": no, "name": name[:200] or f"Lokasi {no}", "area": area[:80], "coord_text": coord_txt[:80],
                "lat": pc[0] if pc else None, "lng": pc[1] if pc else None, "error": None, "excel_row": ri + 1}
        if not pc:
            item["error"] = "Koordinat tidak terbaca (format: -3.3136, 114.5898)" if coord_txt.strip(", ") else "Koordinat kosong"
        out.append(item)
        if len(out) > BULKCOV_MAX_ROWS:
            raise HTTPException(status_code=413, detail=f"Maksimal {BULKCOV_MAX_ROWS} lokasi per berkas; pecah berkas Anda.")
    if not out:
        raise HTTPException(status_code=400, detail="Tidak ada baris lokasi di bawah judul kolom.")
    return nm, out, skipped


class BulkCovParse(BaseModel):
    filename: str = ""
    content_base64: str = ""


def _bc_decode(payload: BulkCovParse) -> bytes:
    if not (payload.filename or "").lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail="Gunakan berkas Excel .xlsx")
    try:
        raw = base64.b64decode(payload.content_base64 or "", validate=False)
    except Exception:
        raise HTTPException(status_code=400, detail="Isi berkas tidak valid")
    if not raw or len(raw) > IMPORT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Berkas kosong atau terlalu besar")
    return raw


@app.post("/api/coverage/bulk/parse")
def coverage_bulk_parse(payload: BulkCovParse):
    sheet, rows, skipped = _bc_parse_rows(_bc_decode(payload))
    with db() as conn:
        cursor = conn.cursor()
        _khs_ready(cursor)
        regs = [g for g in KHS_REGIONS if cursor.execute("SELECT 1 FROM khs_items WHERE prices LIKE ? LIMIT 1", (f'%"{g}"%',)).fetchone()]
        _bm, bset = _boq_cfg(cursor)
        meta = _get_setting(cursor, "khs_meta") or {}
    bad = sum(1 for r in rows if r["error"])
    return {"sheet": sheet, "rows": rows, "valid": len(rows) - bad, "invalid": bad, "skipped": skipped,
            "regions": regs or KHS_REGIONS, "default_region": bset["default_region"], "tax_pct": bset["tax_pct"],
            "khs_source": meta.get("source"), "max_rows": BULKCOV_MAX_ROWS, "run_max": BULKCOV_RUN_MAX}


@app.get("/api/coverage/bulk/template")
def coverage_bulk_template():
    rows = [[1, "ATM CONTOH A", "BANJARMASIN", "-3.3136240, 114.58983"],
            [2, "CRM CONTOH B", "BANJAR", "-3.408728, 114.848023"]]
    return Response(content=_xlsx_bytes([("Lokasi", BULKCOV_TEMPLATE_HEAD, rows)]), media_type=XLSX_MEDIA,
                    headers={"Content-Disposition": 'attachment; filename="template_coverage_massal.xlsx"'})


class BulkCovRow(BaseModel):
    no: Optional[int] = None
    name: str = Field("", max_length=200)
    area: Optional[str] = Field("", max_length=80)
    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)


class BulkCovRun(BaseModel):
    rows: List[BulkCovRow]
    radius_m: float = Field(5000, ge=50, le=5000)   # Y bila tarikan <= radius (bawaan 5 km); T hanya di atas radius
    region: Optional[str] = "AUTO"            # AUTO (cluster catuan, lalu bawaan BOQ) atau salah satu wilayah KHS
    installation: Optional[str] = "Udara"
    cores: Optional[int] = Field(1, ge=1, le=2)
    with_cost: Optional[bool] = True
    include_tax: Optional[bool] = True
    use_poles: Optional[bool] = True
    use_slack: Optional[bool] = True
    slack_pref_m: Optional[float] = Field(1000, ge=0, le=20000)   # ODP/Closure layak dalam jarak ini didahulukan; Slack hanya bila tidak ada


def _bc_route_len(origin, lat, lng):
    """Panjang rute (m) catuan -> lokasi: OSRM; tidak tersedia / tidak wajar / terlalu jauh -> garis lurus x 1,3."""
    d = _haversine_m(origin["latitude"], origin["longitude"], lat, lng)
    if d > MAX_ROUTE_KM * 1000:
        return round(d * BULKCOV_DETOUR, 1), "perkiraan", d
    rt = _route_between([(origin["latitude"], origin["longitude"]), (lat, lng)])
    L = _polyline_length_m(rt["coords"])
    if rt["source"] != "osrm" or L > d * 2.5 + 300:
        return round(d * BULKCOV_DETOUR, 1), "perkiraan", d
    return round(L, 1), "jalan", d


def _bc_scan(cursor, ctx, pool, lat, lng, cores, want):
    """Aset layak terdekat (garis lurus) dari pool; juga aset tak layak terdekat beserta alasannya."""
    near = sorted(((_haversine_m(lat, lng, n["latitude"], n["longitude"]), n) for n in pool), key=lambda x: x[0])
    elig, bad = [], None
    for d, n in near[:250]:
        av = ctx["av"].get(n["id"])
        if av is None:
            av = ctx["av"][n["id"]] = _node_availability(cursor, n, ctx["cables"])
        ok = bool(av["eligible"]) and (av["free"] or 0) >= cores and not (cores >= 2 and (n["type"] or "").upper() == "ODP")
        if ok:
            elig.append((d, n, av))
            if len(elig) >= want:
                break
        elif bad is None:
            bad = (d, n, (av["reason"] or ("hanya ODP 1 core; layanan 2 core butuh Closure/Slack" if av["eligible"] else "tidak bisa dipakai")))
    return elig, bad


def _bc_pick(cursor, ctx, lat, lng, cores, slack_pref_m=1000.0):
    """Catuan layak. ODP/Closure didahulukan; Slack (perlu closure + ODP baru) hanya bila tidak ada ODP/Closure layak
    dalam slack_pref_m (garis lurus). -> (layak[], terdekat_tak_layak, pop_fallback)."""
    # ODP terdekat dan Closure terdekat dicari terpisah: <1 km memakai ODP, >= 1 km memakai Closure
    e1, b1 = _bc_scan(cursor, ctx, ctx["odp"], lat, lng, cores, 1)
    e2, b2 = _bc_scan(cursor, ctx, ctx["clo"], lat, lng, cores, 1)
    elig = sorted(e1 + e2, key=lambda x: x[0])
    bads0 = [b_ for b_ in (b1, b2) if b_]
    bad = min(bads0, key=lambda b_: b_[0]) if bads0 else None
    if not any(d <= slack_pref_m for d, _n, _a in elig):
        es, bs = _bc_scan(cursor, ctx, ctx["slack"], lat, lng, cores, BULKCOV_ELIG_TOP)
        elig = sorted(elig + es, key=lambda x: x[0])[:BULKCOV_ELIG_TOP]
        bads = [b_ for b_ in (bad, bs) if b_]
        bad = min(bads, key=lambda b_: b_[0]) if bads else None
    pop = None
    if not elig:
        pops = sorted(((_haversine_m(lat, lng, p["latitude"], p["longitude"]), p) for p in ctx["pops"]), key=lambda x: x[0])
        pop = pops[0] if pops else None
    return elig, bad, pop


def _bc_one(cursor, ctx, row, opt):
    lat, lng = row.lat, row.lng
    base = {"no": row.no, "name": row.name, "area": row.area or "", "lat": lat, "lng": lng,
            "status": "T", "covered": False, "catuan": None, "alternatives": [], "pull_m": None, "cable_total_m": None,
            "cost": None, "scenario": None, "cable": None, "keterangan": "", "warnings": [], "boq": []}
    elig, bad, pop = _bc_pick(cursor, ctx, lat, lng, opt["cores"], opt["slack_pref_m"])
    cands = []
    for d, n, av in elig:
        L, src, dd = _bc_route_len(n, lat, lng)
        cands.append({"id": n["id"], "name": n["name"], "type": n["type"], "status": n["status"], "cluster": n["cluster"],
                      "latitude": n["latitude"], "longitude": n["longitude"], "free": av["free"], "total": av["total"],
                      "detail": av["detail"], "straight_m": round(dd, 1), "route_m": L, "route_source": src,
                      "needs_closure": (n["type"] or "").upper() == "SLACK"})
    new_odp_origin = False
    if cands:
        cands.sort(key=lambda c: (c["status"] != "Active", c["route_m"]))
        best = cands[0]
        odps = [c for c in cands if (c["type"] or "").upper() == "ODP"]
        clos = [c for c in cands if (c["type"] or "").upper() == "CLOSURE"]
        dmax = float(DEFAULT_PLAN_RULES["drop_max_m"])
        if any(c["needs_closure"] for c in cands):
            pass                                # tidak ada ODP/Closure layak di sekitar: Slack dipakai (perlu closure + ODP baru)
        elif odps and odps[0]["route_m"] < dmax:
            best = odps[0]                      # < 1 km: ODP terdekat yang layak
        elif clos and clos[0]["route_m"] < dmax:
            best = clos[0]                      # ODP terdekat tidak layak/penuh/jauh: Closure terdekat + ODP baru
            new_odp_origin = (opt["cores"] == 1)
        elif clos:
            best = clos[0]                      # >= 1 km: Closure terdekat (kabel distribusi + closure + ODP baru)
        cands.sort(key=lambda c: (c["id"] != best["id"], c["status"] != "Active", c["route_m"]))
    elif pop:
        d, p = pop
        L, src, dd = _bc_route_len(p, lat, lng)
        best = {"id": p["id"], "name": p["name"], "type": p["type"], "status": p["status"], "cluster": p["cluster"],
                "latitude": p["latitude"], "longitude": p["longitude"], "free": None, "total": None,
                "detail": "POP (tidak ada ODP/Closure/Slack yang layak)", "straight_m": round(dd, 1), "route_m": L, "route_source": src}
        cands = [best]
        base["warnings"].append("Tidak ada catuan layak (ODP/Closure/Slack); dihitung dari POP terdekat")
    else:
        base["keterangan"] = "Tidak ada aset jaringan di database untuk dijadikan catuan."
        return base
    base["catuan"] = best
    base["alternatives"] = cands
    eff = best["route_m"]
    covered = bool(elig) and eff <= opt["radius_m"]
    base["covered"], base["status"] = covered, "Y" if covered else "T"
    base["pull_m"] = eff
    # --- rencana tarikan + biaya KHS ---
    plan = None
    far_cost = best["straight_m"] > MAX_ROUTE_KM * 1000
    if opt["with_cost"] and best["straight_m"] < 3:
        base["warnings"].append("Lokasi tepat di catuan (< 3 m): tidak ada tarikan, biaya tidak dihitung")
    elif opt["with_cost"] and far_cost:
        base["warnings"].append(f"Jarak > {MAX_ROUTE_KM:.0f} km melebihi batas perhitungan biaya; perlu survei")
    elif opt["with_cost"]:
        try:
            plan = _compute_plan(cursor, PlanRequest(
                origin_type="NODE", origin_id=best["id"], dest_lat=lat, dest_lng=lng, dest_name=row.name,
                installation=opt["installation"], route_mode="road", create_customer=False,
                use_poles=opt["use_poles"], use_slack=opt["use_slack"], customer_cores=opt["cores"],
                termination="DROPCORE_ROSET", scenario="AUTO", detour_factor=BULKCOV_DETOUR, new_odp_origin=new_odp_origin))
        except HTTPException as exc:
            base["warnings"].append(f"Biaya tidak dihitung: {exc.detail}")
        except Exception as exc:   # noqa: BLE001
            base["warnings"].append(f"Biaya tidak dihitung: {exc}")
    if plan:
        sm = plan["summary"]
        base["pull_m"] = sm["route_length_m"]
        base["cable_total_m"] = sm["cable_total_m"]
        base["scenario"] = sm["scenario"]
        segs = sm.get("segments") or []
        base["cable"] = " + ".join(f"{s_['cable_label']} {s_['cable_capacity']} ({s_['installation']}) {s_['route_length_m']:.0f} m" for s_ in segs)
        base["plan"] = {"poles_new": sm["poles_new"], "poles_existing": sm["poles_existing"], "hh_new": sm["hh_new"],
                        "hh_existing": sm["hh_existing"], "slack_count": sm["slack_count"],
                        "hub": bool(plan.get("hub")), "new_odp": bool(sm.get("new_odp")),
                        "loss_db": sm.get("loss_db"), "loss_status": sm.get("loss_status"), "route_source": sm.get("route_source")}
        base["warnings"].extend((plan.get("warnings") or [])[:3])
        try:
            bq = _boq_compute(cursor, BoqRequest(summary=sm, region=(opt["region"] if opt["region"] != "AUTO" else None),
                                                 origin_cluster=best.get("cluster")))
            t = bq["totals"]
            tot = t["total"] if opt["include_tax"] else t["subtotal"]
            base["cost"] = {"region": bq["region"], "material": t["material"], "jasa": t["jasa"], "subtotal": t["subtotal"],
                            "tax_pct": t["tax_pct"], "tax": t["tax"] if opt["include_tax"] else 0, "total": tot,
                            "include_tax": bool(opt["include_tax"])}
            base["boq"] = [{"component": l_.get("component"), "code": l_.get("code"), "desc": l_.get("description") or l_.get("label"),
                            "unit": l_.get("unit"), "qty": l_.get("qty"), "unit_price": l_.get("unit_price"), "total": l_.get("total")}
                           for l_ in bq["lines"]]
            base["warnings"].extend((bq.get("warnings") or [])[:2])
        except HTTPException as exc:
            base["warnings"].append(f"Harga KHS tidak dapat dihitung: {exc.detail}")
    # --- penanda infrastruktur baru (closure/ODP) ---
    if plan:
        sm_ = plan["summary"]
        hub_ = plan.get("hub") if sm_.get("scenario") == "HUB" else None
        onc_ = sm_.get("origin_new_closure")
        clo = bool(hub_) or bool(onc_) or bool(best.get("needs_closure"))
        odp = (bool(hub_ and hub_.get("odp")) or bool(onc_ and onc_.get("odp")) or bool(best.get("needs_closure") and onc_ is None)
               or bool(sm_.get("suggest_new_odp") and not hub_))
        base["need_new"] = {"closure": clo, "odp": odp,
                            "label": ("Closure + ODP baru" if clo and odp else "Closure baru" if clo else "ODP baru" if odp else "Tidak")}
    else:
        base["need_new"] = None
    # --- keterangan ---
    nm = f"{best['name']} ({best['type']})"
    if covered:
        k = f"Tercover: catuan {nm} ±{eff:.0f} m" + (f", {best['detail']}" if best.get("detail") else "") + "."
    elif eff > BULKCOV_FAR_M:
        k = f"Jauh (> 5 km) dari catuan terdekat {nm} ±{eff:.0f} m; perlu penarikan baru."
    else:
        k = f"Di luar radius {opt['radius_m']:.0f} m dari catuan {nm} ±{eff:.0f} m; perlu penarikan baru."
    if best.get("needs_closure"):
        onc = ((plan or {}).get("summary") or {}).get("origin_new_closure")
        k += " Catuan berupa Slack: perlu pemasangan closure" + (" + ODP" if (onc is None or onc.get("odp")) else "") + " baru di titik slack" + (" (biaya sudah termasuk)." if plan else ".")
    if plan:
        sm = plan["summary"]
        if sm["scenario"] == "HUB" and plan.get("hub"):
            h = plan["hub"]
            k += (f" Tarikan {sm['route_length_m']:.0f} m: kabel distribusi baru + closure baru"
                  + (f" + ODP baru {h['odp']['ratio']}" if h.get("odp") else "") + f", lalu dropcore {h['to_customer_m']:.0f} m ke lokasi.")
        else:
            k += f" Tarikan {sm['route_length_m']:.0f} m (dropcore" + (", tanpa tiang baru)." if not (sm["poles_new"] or sm["hh_new"]) else ").")
        pole = sm["poles_existing"] + sm["hh_existing"]
        if pole:
            k += f" {pole} tiang/handhole eksisting dipakai ulang."
    if new_odp_origin and plan:
        k += " ODP terdekat tidak layak/penuh atau di luar 1 km: ODP baru dipasang di closure catuan."
    nn = base.get("need_new")
    if nn and nn["label"] != "Tidak":
        k += f" Perlu pemasangan baru: {nn['label'].replace(' baru', '')}."
    if opt["with_cost"] and far_cost:
        k += f" Estimasi biaya tidak dihitung (jarak > {MAX_ROUTE_KM:.0f} km); perlu survei."
    if bad and bad[0] < best["straight_m"]:
        k += f" Catatan: {bad[1]['name']} ({bad[1]['type']}) lebih dekat (±{bad[0]:.0f} m) tetapi tidak bisa dipakai: {bad[2]}."
    base["keterangan"] = k
    return base


def _bc_ctx(cursor):
    cables = cursor.execute("SELECT * FROM cables").fetchall()
    nodes = cursor.execute("SELECT * FROM nodes WHERE type IN ('ODP', 'CLOSURE', 'SLACK') AND latitude IS NOT NULL AND longitude IS NOT NULL").fetchall()
    pops = cursor.execute("SELECT * FROM nodes WHERE type = 'POP' AND latitude IS NOT NULL AND longitude IS NOT NULL").fetchall()
    return {"cables": cables, "main": [n for n in nodes if n["type"] != "SLACK"],
            "odp": [n for n in nodes if n["type"] == "ODP"], "clo": [n for n in nodes if n["type"] == "CLOSURE"], "slack": [n for n in nodes if n["type"] == "SLACK"], "pops": pops, "av": {}}


@app.post("/api/coverage/bulk/run")
def coverage_bulk_run(req: BulkCovRun):
    if not req.rows:
        raise HTTPException(status_code=400, detail="Tidak ada lokasi")
    if len(req.rows) > BULKCOV_RUN_MAX:
        raise HTTPException(status_code=400, detail=f"Maksimal {BULKCOV_RUN_MAX} lokasi per permintaan")
    region = (req.region or "AUTO").upper()
    if region != "AUTO" and region not in KHS_REGIONS:
        raise HTTPException(status_code=400, detail=f"Wilayah harga harus AUTO atau salah satu dari: {', '.join(KHS_REGIONS)}")
    inst = req.installation or "Udara"
    if inst not in INSTALLATIONS:
        raise HTTPException(status_code=400, detail="Pemasangan harus Udara atau Tanah")
    opt = {"radius_m": float(req.radius_m), "region": region, "installation": inst, "cores": int(req.cores or 1),
           "with_cost": req.with_cost is not False, "include_tax": req.include_tax is not False,
           "use_poles": req.use_poles is not False, "use_slack": req.use_slack is not False,
           "slack_pref_m": float(req.slack_pref_m if req.slack_pref_m is not None else 1000)}
    tok = BULK_ROUTE_CAP.set(BULKCOV_ROUTE_PER_MIN)
    try:
        with db() as conn:
            cursor = conn.cursor()
            ctx = _bc_ctx(cursor)
            out = []
            for r in req.rows:
                try:
                    out.append(_bc_one(cursor, ctx, r, opt))
                except Exception as exc:   # noqa: BLE001 - satu lokasi gagal tidak menggagalkan yang lain
                    out.append({"no": r.no, "name": r.name, "area": r.area or "", "lat": r.lat, "lng": r.lng, "status": "ERR",
                                "covered": False, "catuan": None, "alternatives": [], "pull_m": None, "cost": None,
                                "keterangan": f"Gagal dihitung: {exc}", "warnings": [], "boq": []})
    finally:
        BULK_ROUTE_CAP.reset(tok)
    return {"results": out}


class BulkCovExport(BaseModel):
    results: List[dict] = Field(..., max_length=BULKCOV_MAX_ROWS)
    params: Optional[dict] = None
    source_name: Optional[str] = ""


def _bc_num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


@app.post("/api/coverage/bulk/export")
def coverage_bulk_export(req: BulkCovExport):
    head = ["No", "Nama Lokasi", "Area", "Titik Koordinat", "Tercover FO (Y/T)", "Catuan Terdekat", "Panjang Tarikan (m)",
            "Estimasi Biaya", "Keterangan", "Jenis Catuan", "Status Catuan", "Sisa Port/Core", "Jarak Garis Lurus (m)",
            "Sumber Jarak", "Jenis Kabel", "Skenario", "Perlu Closure/ODP Baru", "Total Kabel + Slack (m)", "Tiang/HH Baru", "Tiang/HH Eksisting Dipakai",
            "Redaman (dB)", "Wilayah KHS", "Biaya Material", "Biaya Jasa", "PPN", "Latitude", "Longitude"]
    rows, alt, boq = [], [], []
    ny = nt = ne = 0
    tot_len = tot_cost = 0.0
    for i, r in enumerate(req.results, start=1):
        if not isinstance(r, dict):
            continue
        c = r.get("catuan") or {}
        cost = r.get("cost") or {}
        pl = r.get("plan") or {}
        st = r.get("status")
        ny += st == "Y"; nt += st == "T"; ne += st == "ERR"
        pull = _bc_num(r.get("pull_m"))
        tot_len += pull or 0
        tot_cost += _bc_num(cost.get("total")) or 0
        src = {"jalan": "Rute jalan", "perkiraan": "Perkiraan (garis lurus x 1,3)"}.get(c.get("route_source"), c.get("route_source") or "")
        free = c.get("free")
        rows.append([r.get("no") if r.get("no") is not None else i, str(r.get("name") or "")[:200], str(r.get("area") or "")[:80],
                     f"{r.get('lat')}, {r.get('lng')}" if r.get("lat") is not None else "",
                     {"Y": "Y", "T": "T"}.get(st, "ERR"), (f"{c.get('name')} ({c.get('type')})" if c else "-"),
                     round(pull, 1) if pull is not None else None,
                     round(cost["total"]) if _bc_num(cost.get("total")) is not None else None,
                     str(r.get("keterangan") or "")[:900], c.get("type") or "", c.get("status") or "",
                     free if _bc_num(free) is not None else "", _bc_num(c.get("straight_m")), src,
                     str(r.get("cable") or "")[:300], r.get("scenario") or "", (r.get("need_new") or {}).get("label") or ("-" if not pl else "Tidak"), _bc_num(r.get("cable_total_m")),
                     (pl.get("poles_new", 0) or 0) + (pl.get("hh_new", 0) or 0) if pl else None,
                     (pl.get("poles_existing", 0) or 0) + (pl.get("hh_existing", 0) or 0) if pl else None,
                     _bc_num(pl.get("loss_db")), cost.get("region") or "", _bc_num(cost.get("material")), _bc_num(cost.get("jasa")),
                     _bc_num(cost.get("tax")), _bc_num(r.get("lat")), _bc_num(r.get("lng"))])
        for k, a in enumerate((r.get("alternatives") or [])[:5], start=1):
            if isinstance(a, dict):
                alt.append([r.get("no") if r.get("no") is not None else i, str(r.get("name") or "")[:200], k, a.get("name"), a.get("type"),
                            a.get("status"), a.get("free") if _bc_num(a.get("free")) is not None else "", _bc_num(a.get("straight_m")),
                            _bc_num(a.get("route_m")), "Ya" if c and a.get("id") == c.get("id") else ""])
        for b in (r.get("boq") or [])[:40]:
            if isinstance(b, dict):
                boq.append([r.get("no") if r.get("no") is not None else i, str(r.get("name") or "")[:200], b.get("component"), b.get("code"),
                            str(b.get("desc") or "")[:200], b.get("unit"), _bc_num(b.get("qty")), _bc_num(b.get("unit_price")), _bc_num(b.get("total"))])
    p = req.params if isinstance(req.params, dict) else {}
    summ = [["Berkas sumber", str(req.source_name or "")[:120]], ["Dibuat", _now_str()], ["Oleh", _current_username()],
            ["Jumlah lokasi", len(rows)], ["Tercover (Y)", ny], ["Tidak tercover (T)", nt], ["Gagal dihitung", ne],
            ["Total panjang tarikan (m)", round(tot_len, 1)], ["Total estimasi biaya (Rp)", round(tot_cost)],
            ["Catatan biaya", "Estimasi di luar transportasi dan biaya lainnya."], ["Radius tercover (m)", p.get("radius_m")], ["Wilayah harga KHS", p.get("region")], ["Pemasangan", p.get("installation")],
            ["Layanan (core)", p.get("cores")], ["Biaya sudah termasuk PPN", "Ya" if p.get("include_tax") else "Tidak"],
            ["Aturan", "Y bila catuan layak (ODP/Closure/Slack dengan port/core kosong) dalam radius (bawaan 5 km) mengikuti rute; T hanya bila di atas radius atau tidak ada catuan layak; "
                       "tarikan < 1000 m = dropcore dari ODP terdekat yang layak (bila tidak ada: ODP baru di closure), tanpa tiang baru dan tanpa slack; "
                       "1000 m atau lebih = kabel distribusi 12/24/48/96C dari closure terdekat + closure + ODP baru + dropcore, ditandai pada kolom Perlu Closure/ODP Baru. "
                       "Harga KHS: kabel, closure, ODP, dan tiang baru jasa saja; slack, roset, dan lainnya material + jasa; ditambah biaya survei sesuai jarak. Jarak mengikuti jalan (OSRM), "
                       "bila tidak tersedia garis lurus x 1,3. Harga dari katalog KHS."]]
    with db() as conn:
        _audit(conn.cursor(), "EXPORT", "COVERAGE", None, (req.source_name or "coverage massal")[:120],
               f"Ekspor hasil Cek Coverage massal: {len(rows)} lokasi ({ny} Y, {nt} T), total biaya Rp {round(tot_cost):,}")
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    data = _xlsx_bytes([("Hasil Coverage", head, rows), ("Alternatif Catuan", ["No", "Nama Lokasi", "Peringkat", "Catuan", "Jenis", "Status", "Sisa Port/Core", "Jarak Garis Lurus (m)", "Jarak Rute (m)", "Terpilih"], alt),
                        ("Rincian BOQ", ["No", "Nama Lokasi", "Komponen", "Kode KHS", "Uraian", "Satuan", "Volume", "Harga Satuan", "Jumlah"], boq),
                        ("Ringkasan", ["Keterangan", "Nilai"], summ)])
    return Response(content=data, media_type=XLSX_MEDIA,
                    headers={"Content-Disposition": f'attachment; filename="coverage_massal_{stamp}.xlsx"'})


# ===== NAMA ASET OTOMATIS (selain POP dan PELANGGAN) =====
NAME_AUTO_NODE = {"CLOSURE": "CLO", "ODP": "ODP", "SLACK": "SLK", "TIANG": "TNG", "HH": "HH"}
NAME_AUTO_CABLE = {"Backbone": "BB", "Feeder": "FD", "Distribution": "DS", "Drop": "DC"}
NAMING_DEFAULT = {"template": "{TIPE}-{CLUSTER}-{AREA}-{NO}", "digits": 5, "codes": {}, "import_rename": True}
NAMING_TOKENS = ("{TIPE}", "{CLUSTER}", "{AREA}", "{NO}")
NAMING_PRESETS = [
    {"id": "A", "label": "Tipe + area + nomor", "template": "{TIPE}-{AREA}-{NO}", "digits": 4},
    {"id": "C", "label": "Tipe + cluster + area + nomor (bawaan)", "template": "{TIPE}-{CLUSTER}-{AREA}-{NO}", "digits": 5},
    {"id": "D", "label": "Tipe + nomor saja", "template": "{TIPE}-{NO}", "digits": 6},
]


def _naming_cfg(cursor):
    c = _get_setting(cursor, "naming") or {}
    out = dict(NAMING_DEFAULT)
    if isinstance(c.get("template"), str) and "{NO}" in c["template"]:
        out["template"] = c["template"]
    if isinstance(c.get("digits"), int) and 2 <= c["digits"] <= 8:
        out["digits"] = c["digits"]
    if isinstance(c.get("import_rename"), bool):
        out["import_rename"] = c["import_rename"]
    if isinstance(c.get("codes"), dict):
        out["codes"] = {str(k).upper(): str(v).upper() for k, v in c["codes"].items() if re.match(r"^[A-Za-z0-9]{1,8}$", str(v))}
    return out


def _name_code(value, codes):
    v = str(value or "").strip()
    if not v:
        return "XXX"
    if v.upper() in codes:
        return codes[v.upper()]
    words = [w for w in re.split(r"[^A-Za-z0-9]+", v) if w]
    if len(words) >= 2:
        return "".join(w[0] for w in words[:4]).upper()
    return re.sub(r"[^A-Za-z0-9]", "", v)[:3].upper() or "XXX"


def _naming_validate(template, digits, codes):
    if not isinstance(template, str) or "{NO}" not in template:
        raise HTTPException(status_code=400, detail="Template wajib memuat {NO}")
    rest = template
    for t in NAMING_TOKENS:
        rest = rest.replace(t, "")
    if re.search(r"[{}]", rest):
        raise HTTPException(status_code=400, detail="Token tidak dikenal. Gunakan: " + ", ".join(NAMING_TOKENS))
    if len(template) > 60 or not re.match(r"^[A-Za-z0-9{}\-_./ ]+$", template):
        raise HTTPException(status_code=400, detail="Template hanya huruf, angka, spasi, - _ . / dan token")
    if template.count("{NO}") != 1:
        raise HTTPException(status_code=400, detail="{NO} harus muncul satu kali")
    if not isinstance(digits, int) or not (2 <= digits <= 8):
        raise HTTPException(status_code=400, detail="Jumlah digit nomor 2 sampai 8")
    for k, v in (codes or {}).items():
        if not re.match(r"^[A-Za-z0-9]{1,8}$", str(v)):
            raise HTTPException(status_code=400, detail=f"Kode '{v}' untuk '{k}' harus 1-8 huruf/angka")


def _name_stem(cfg, prefix, cluster, area):
    t = cfg["template"]
    return (t.replace("{TIPE}", prefix).replace("{CLUSTER}", _name_code(cluster, cfg["codes"]))
             .replace("{AREA}", _name_code(area, cfg["codes"])))


def _name_head_tail(cfg, kind, typ, cluster, area, capacity=None, installation=None):
    """Bagian depan/belakang nama sebelum/sesudah nomor. Kabel: kode jenis + kapasitas + KU|KT (mis. DS-12C-KU-EKO-BAN-00001, BB-96C-KT-..., DC-2C-KU-...)."""
    prefix = NAME_AUTO_NODE.get((typ or "").upper()) if kind == "NODE" else NAME_AUTO_CABLE.get(typ)
    if not prefix:
        raise HTTPException(status_code=400, detail="Nama otomatis tidak tersedia untuk jenis ini (POP dan Pelanggan diberi nama manual)")
    if kind == "CABLE":
        n = _core_total(capacity) if capacity else 0
        prefix = prefix + (f"-{n}C" if n > 0 else "") + "-" + ("KT" if installation == "Tanah" else "KU")
    return _name_stem(cfg, prefix, cluster, area).split("{NO}", 1)


def _gen_name(cursor, kind, typ, cluster, area, cfg=None, taken=None, capacity=None, installation=None, counter=None):
    """Nama berikutnya yang belum dipakai untuk jenis/wilayah ini, mis. CLO-EKO-BJM-00012 atau DS-12C-KU-EKO-BJM-00003."""
    cfg = cfg or _naming_cfg(cursor)
    head, tail = _name_head_tail(cfg, kind, typ, cluster, area, capacity, installation)
    if counter is not None and (head, tail) in counter:      # impor besar: lanjut dari nomor terakhir tanpa memindai ulang
        counter[(head, tail)] += 1
        return head + str(counter[(head, tail)]).zfill(cfg["digits"]) + tail
    rx = re.compile("^" + re.escape(head) + r"(\d+)" + re.escape(tail) + "$", re.I)
    like = head.replace("%", r"\%").replace("_", r"\_") + "%"
    mx = 0
    for tbl in ("nodes", "cables"):
        for r in cursor.execute(f"SELECT name FROM {tbl} WHERE name LIKE ? ESCAPE '\\'", (like,)).fetchall():
            m = rx.match(r["name"] or "")
            if m:
                mx = max(mx, int(m.group(1)))
    if taken:
        for nm in taken:
            m = rx.match(nm)
            if m:
                mx = max(mx, int(m.group(1)))
    n = mx + 1
    while True:
        cand = head + str(n).zfill(cfg["digits"]) + tail
        if not _find_node_dup(cursor, "name", cand, None) and not _find_cable_dup(cursor, cand):
            if counter is not None:
                counter[(head, tail)] = n
            return cand
        n += 1


def _naming_examples(cursor, cfg):
    cl, ar = _wil_maps(cursor)
    pairs = [(a["cname"], a["name"]) for a in list(ar.values())[:2]] or [("EKO", "BANJARMASIN")]
    out = []
    cases = [("NODE", t, None, None) for t in NAME_AUTO_NODE]
    cases += [("CABLE", "Backbone", "48C", "Udara"), ("CABLE", "Feeder", "24C", "Tanah"), ("CABLE", "Distribution", "12C", "Udara"),
              ("CABLE", "Distribution", "24C", "Tanah"), ("CABLE", "Drop", "2C", "Udara")]
    for (c, a) in pairs:
        for kind, typ, cap, inst in cases:
            nm = _gen_name(cursor, kind, typ, c, a, cfg, None, cap, inst)
            head, tail = _name_head_tail(cfg, kind, typ, c, a, cap, inst)
            try:
                num = int(nm[len(head):len(nm) - len(tail) if tail else None])
            except ValueError:
                num = 1
            lab = typ if kind == "NODE" else f"{typ} {cap} {inst}"
            out.append({"kind": kind, "type": lab, "cluster": c, "area": a,
                        "names": [head + str(num + i).zfill(cfg["digits"]) + tail for i in range(3)]})
    return out


class NamingPayload(BaseModel):
    template: str
    digits: int = 5
    codes: dict = Field(default_factory=dict)
    import_rename: bool = True


@app.get("/api/naming")
def get_naming():
    with db() as conn:
        cur = conn.cursor()
        cfg = _naming_cfg(cur)
        cl, ar = _wil_maps(cur)
        wil = [{"cluster": a["cname"], "area": a["name"], "cluster_code": _name_code(a["cname"], cfg["codes"]), "area_code": _name_code(a["name"], cfg["codes"])}
               for a in ar.values()]
        return {"config": cfg, "presets": NAMING_PRESETS, "tokens": list(NAMING_TOKENS), "prefix_node": NAME_AUTO_NODE, "prefix_cable": NAME_AUTO_CABLE,
                "wilayah": wil[:300], "examples": _naming_examples(cur, cfg), "excluded": ["POP", "PELANGGAN"]}


@app.post("/api/naming/preview")
def naming_preview(req: NamingPayload):
    _naming_validate(req.template, req.digits, req.codes)
    cfg = {"template": req.template, "digits": req.digits, "codes": {str(k).upper(): str(v).upper() for k, v in req.codes.items()}, "import_rename": bool(req.import_rename)}
    with db() as conn:
        return {"config": cfg, "examples": _naming_examples(conn.cursor(), cfg)}


@app.put("/api/naming")
def put_naming(req: NamingPayload):
    _naming_validate(req.template, req.digits, req.codes)
    cfg = {"template": req.template, "digits": req.digits, "codes": {str(k).upper(): str(v).upper() for k, v in req.codes.items()}, "import_rename": bool(req.import_rename)}
    with db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES ('naming', ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                    (json.dumps(cfg), _now_str(), _current_username()))
        _audit(cur, "UPDATE", "SETTING", None, "penamaan aset", f"Format nama otomatis: {cfg['template']} ({cfg['digits']} digit)")
        return {"message": "Format nama disimpan", "config": cfg, "examples": _naming_examples(cur, cfg)}


class AutonameReq(BaseModel):
    nodes: List[int] = Field(default_factory=list)
    cables: List[int] = Field(default_factory=list)


@app.post("/api/assets/autoname")
def assets_autoname(req: AutonameReq):
    """Beri nama otomatis ke aset terpilih (kecuali POP dan Pelanggan). Nama lama diganti; tercatat di riwayat."""
    nodes = list(dict.fromkeys(req.nodes or []))
    cables = list(dict.fromkeys(req.cables or []))
    if not nodes and not cables:
        raise HTTPException(status_code=400, detail="Tidak ada aset yang dipilih")
    if len(nodes) + len(cables) > BULK_UPDATE_MAX:
        raise HTTPException(status_code=400, detail=f"Maksimal {BULK_UPDATE_MAX} aset per permintaan")
    done, failed = [], []
    for kind, ids in (("NODE", nodes), ("CABLE", cables)):
        for aid in ids:
            try:
                with db() as conn:
                    cur = conn.cursor()
                    row = cur.execute(f"SELECT * FROM {_table_of(kind)} WHERE id = ?", (aid,)).fetchone()
                    if not row:
                        raise HTTPException(status_code=404, detail="Aset tidak ditemukan")
                    new = _gen_name(cur, kind, row["type"], row["cluster"], row["area"], None, None, row["capacity"], row["installation"] if kind == "CABLE" else None)
                old = row["name"]
                if kind == "NODE":
                    update_node(aid, NodeUpdate(name=new))
                else:
                    update_cable(aid, CableUpdate(name=new))
                done.append({"kind": kind, "id": aid, "old": old, "new": new})
            except HTTPException as exc:
                failed.append({"kind": kind, "id": aid, "name": f"#{aid}", "reason": str(exc.detail)})
    return {"message": f"{len(done)} aset diberi nama otomatis" + (f", {len(failed)} dilewati" if failed else ""), "renamed": len(done),
            "items": done[:50], "failed": failed[:50], "failed_total": len(failed)}


# ===== RAPIKAN TOPOLOGI (hasil impor KMZ): kapasitas dari nama kabel, pecah kabel massal, ujung kabel yatim =====
TOPOFIX_MAX_ROWS = 3000
TOPOFIX_APPLY_MAX = 300
TOPOFIX_SPLIT_TYPES = ("CLOSURE", "ODP", "POP")
TOPOFIX_END_TYPES = ("CLOSURE", "ODP", "POP", "SLACK", "PELANGGAN")
_TF_KU = re.compile(r"^\s*(KU|KT)\s*[-_ ]?\s*(\d{1,3})\s*C?(?![0-9])", re.I)
_TF_C = re.compile(r"^\s*(\d{1,3})\s*C(?![A-Za-z0-9])", re.I)


def _tf_parse_cable_name(name):
    """Petunjuk dari nama kabel: 'KU12-...' (kabel udara 12 core), 'KT24-...' (kabel tanah), '2C-...' (2 core).
    -> {cores, installation, kind} atau None bila nama tidak memuat petunjuk."""
    n = str(name or "")
    cores = inst = None
    m = _TF_KU.match(n)
    if m:
        inst = "Udara" if m.group(1).upper() == "KU" else "Tanah"
        cores = int(m.group(2))
    else:
        m = _TF_C.match(n)
        if m:
            cores = int(m.group(1))
    if cores is not None and not (1 <= cores <= 288):
        cores = None
    if cores is None and inst is None:
        return None
    up = n.upper()
    kind = None
    if cores is not None and cores <= 2:
        kind = "Drop"
    elif re.search(r"\bFEEDER\b", up):
        kind = "Feeder"
    elif re.search(r"\bBACKBONE\b|\bBB\b", up):
        kind = "Backbone"
    elif re.search(r"DISTRIBUSI", up):
        kind = "Distribution"
    return {"cores": cores, "installation": inst, "kind": kind}


def _tf_capacity_rows(cursor):
    out = []
    for c in cursor.execute("SELECT * FROM cables ORDER BY id").fetchall():
        p = _tf_parse_cable_name(c["name"])
        if not p:
            continue
        new_cap = f"{p['cores']}C" if p["cores"] else None
        cur_cap = (c["capacity"] or "").strip().upper()
        d = {"id": c["id"], "name": c["name"], "type": c["type"], "capacity": c["capacity"], "installation": c["installation"],
             "new_capacity": new_cap if new_cap and new_cap != cur_cap else None,
             "new_type": p["kind"] if p["kind"] and p["kind"] != c["type"] else None,
             "new_installation": p["installation"] if p["installation"] and p["installation"] != c["installation"] else None,
             "blocked": None}
        if not (d["new_capacity"] or d["new_type"] or d["new_installation"]):
            continue
        if d["new_capacity"]:
            try:
                _check_capacity_fits(cursor, "CABLE", c["id"], None, d["new_capacity"])
            except HTTPException as exc:
                d["blocked"] = str(exc.detail)
        out.append(d)
    return out


class TopofixCapApply(BaseModel):
    ids: List[int] = Field(default_factory=list)
    capacity: bool = True
    type: bool = False
    installation: bool = True


@app.post("/api/topofix/capacity/preview")
def topofix_capacity_preview():
    with db() as conn:
        rows = _tf_capacity_rows(conn.cursor())
        total = len(rows)
    return {"total": total, "rows": rows[:TOPOFIX_MAX_ROWS], "truncated": total > TOPOFIX_MAX_ROWS,
            "blocked": sum(1 for r in rows if r["blocked"])}


@app.post("/api/topofix/capacity/apply")
def topofix_capacity_apply(req: TopofixCapApply):
    ids = list(dict.fromkeys(req.ids or []))
    if not ids:
        raise HTTPException(status_code=400, detail="Tidak ada kabel yang dipilih")
    if len(ids) > TOPOFIX_APPLY_MAX:
        raise HTTPException(status_code=400, detail=f"Maksimal {TOPOFIX_APPLY_MAX} kabel per permintaan")
    if not (req.capacity or req.type or req.installation):
        raise HTTPException(status_code=400, detail="Pilih minimal satu kolom yang diterapkan")
    ok, failed = 0, []
    for cid in ids:
        with db() as conn:
            c = conn.execute("SELECT * FROM cables WHERE id = ?", (cid,)).fetchone()
        if not c:
            failed.append({"id": cid, "name": f"#{cid}", "reason": "Kabel tidak ditemukan"})
            continue
        p = _tf_parse_cable_name(c["name"])
        data = {}
        if p:
            if req.capacity and p["cores"] and f"{p['cores']}C" != (c["capacity"] or "").strip().upper():
                data["capacity"] = f"{p['cores']}C"
            if req.type and p["kind"] and p["kind"] != c["type"]:
                data["type"] = p["kind"]
            if req.installation and p["installation"] and p["installation"] != c["installation"]:
                data["installation"] = p["installation"]
        if not data:
            failed.append({"id": cid, "name": c["name"], "reason": "Tidak ada perubahan (nama tidak memuat petunjuk, atau sudah sesuai)"})
            continue
        try:
            update_cable(cid, CableUpdate(**data))
            ok += 1
        except HTTPException as exc:
            failed.append({"id": cid, "name": c["name"], "reason": str(exc.detail)})
    with db() as conn:
        _audit(conn.cursor(), "UPDATE", "BULK", None, "rapikan topologi",
               f"Rapikan topologi: kapasitas/jenis/pemasangan {ok} kabel diperbarui dari nama kabel" + (f"; {len(failed)} dilewati" if failed else ""))
    return {"message": f"{ok} kabel diperbarui" + (f", {len(failed)} dilewati" if failed else ""), "updated": ok,
            "failed": failed[:50], "failed_total": len(failed)}


# --- pecah kabel massal di aset yang berada di atas jalur kabel ---
class TopofixSplitReq(BaseModel):
    max_m: float = Field(15.0, ge=1.0, le=50.0)
    types: List[str] = Field(default_factory=lambda: list(TOPOFIX_SPLIT_TYPES))


class TopofixSplitPair(BaseModel):
    node_id: int
    cable_id: int


class TopofixSplitApply(BaseModel):
    pairs: List[TopofixSplitPair] = Field(default_factory=list)
    max_m: float = Field(15.0, ge=1.0, le=50.0)


def _tf_types(types, allowed):
    t = [str(x).upper() for x in (types or []) if str(x).upper() in allowed]
    if not t:
        raise HTTPException(status_code=400, detail=f"Pilih minimal satu jenis aset ({', '.join(allowed)})")
    return t


def _tf_split_candidates(cursor, max_m, types):
    q = ",".join("?" * len(types))
    nodes = [dict(r) for r in cursor.execute(
        f"SELECT id, name, type, latitude, longitude FROM nodes WHERE type IN ({q}) AND latitude IS NOT NULL AND longitude IS NOT NULL", types).fetchall()]
    pad_lat = max_m / 111320.0 + 1e-5
    cands = []
    for cab in cursor.execute("SELECT * FROM cables ORDER BY id").fetchall():
        coords = _cable_coords(cab)
        if not coords:
            continue
        lngs = [p[0] for p in coords]
        lats = [p[1] for p in coords]
        pad_lng = pad_lat / max(0.2, math.cos(math.radians(sum(lats) / len(lats))))
        lo_lng, hi_lng, lo_lat, hi_lat = min(lngs) - pad_lng, max(lngs) + pad_lng, min(lats) - pad_lat, max(lats) + pad_lat
        for n in nodes:
            if n["id"] in (cab["from_node_id"], cab["to_node_id"]):
                continue
            if not (lo_lat <= n["latitude"] <= hi_lat and lo_lng <= n["longitude"] <= hi_lng):
                continue
            sn = _snap_to_polyline(coords, n["latitude"], n["longitude"])
            if sn["offset_m"] > max_m or sn["along_m"] < 3 or sn["total_m"] - sn["along_m"] < 3:
                continue
            cands.append({"node": n, "cable": cab, "offset_m": round(sn["offset_m"], 1), "along_m": round(sn["along_m"], 1),
                          "total_m": round(sn["total_m"], 1)})
    return cands


@app.post("/api/topofix/split/preview")
def topofix_split_preview(req: TopofixSplitReq):
    types = _tf_types(req.types, TOPOFIX_SPLIT_TYPES)
    with db() as conn:
        cands = _tf_split_candidates(conn.cursor(), float(req.max_m), types)
    best = {}
    for c in cands:
        k = c["node"]["id"]
        if k not in best or c["offset_m"] < best[k]:
            best[k] = c["offset_m"]
    ncab = {}
    for c in cands:
        ncab[c["node"]["id"]] = ncab.get(c["node"]["id"], 0) + 1
    rows = []
    for c in sorted(cands, key=lambda x: (x["cable"]["id"], x["along_m"])):
        cab, n = c["cable"], c["node"]
        cust = (cab["type"] == "Drop") or bool(_TF_C.match(cab["name"] or "") and (_tf_parse_cable_name(cab["name"]) or {}).get("cores", 99) <= 2)
        warn = []
        if ncab[n["id"]] > 1:
            warn.append(f"dekat {ncab[n['id']]} kabel")
        if cust:
            warn.append("kabel pelanggan/dropcore")
        nearest = c["offset_m"] <= best[n["id"]] + 3.0      # kabel yang berimpit (satu jalur) ikut dicentang
        rows.append({"node_id": n["id"], "node_name": n["name"], "node_type": n["type"], "cable_id": cab["id"], "cable_name": cab["name"],
                     "cable_type": cab["type"], "capacity": cab["capacity"], "offset_m": c["offset_m"], "along_m": c["along_m"],
                     "total_m": c["total_m"], "warn": warn, "checked": bool(nearest and not cust)})
    nodes_n = len({r["node_id"] for r in rows})
    cables_n = len({r["cable_id"] for r in rows})
    return {"total": len(rows), "nodes": nodes_n, "cables": cables_n, "rows": rows[:TOPOFIX_MAX_ROWS], "truncated": len(rows) > TOPOFIX_MAX_ROWS}


@app.post("/api/topofix/split/apply")
def topofix_split_apply(req: TopofixSplitApply):
    pairs = list(dict.fromkeys((p.cable_id, p.node_id) for p in req.pairs))
    if not pairs:
        raise HTTPException(status_code=400, detail="Tidak ada pasangan aset-kabel yang dipilih")
    if len(pairs) > TOPOFIX_APPLY_MAX:
        raise HTTPException(status_code=400, detail=f"Maksimal {TOPOFIX_APPLY_MAX} pasangan per permintaan")
    by_cable = {}
    for cid, nid in pairs:
        by_cable.setdefault(cid, []).append(nid)
    done_cables = done_nodes = new_segments = 0
    failed = []
    for cid, nids in by_cable.items():
        try:
            with db() as conn:
                cur = conn.cursor()
                cab0 = cur.execute("SELECT * FROM cables WHERE id = ?", (cid,)).fetchone()
                if not cab0:
                    raise HTTPException(status_code=404, detail="Kabel tidak ditemukan")
                coords0 = _cable_coords(cab0)
                if not coords0:
                    raise HTTPException(status_code=400, detail="Geometri kabel tidak valid")
                nodes = []
                for nid in nids:
                    n = cur.execute("SELECT id, name, latitude, longitude FROM nodes WHERE id = ?", (nid,)).fetchone()
                    if not n:
                        raise HTTPException(status_code=404, detail=f"Aset #{nid} tidak ditemukan")
                    sn = _snap_to_polyline(coords0, n["latitude"], n["longitude"])
                    if sn["offset_m"] > float(req.max_m):
                        raise HTTPException(status_code=400, detail=f"{n['name']} berjarak {sn['offset_m']:.0f} m dari kabel (maks {req.max_m:g} m)")
                    nodes.append((sn["along_m"], n))
                nodes.sort(key=lambda x: x[0])
                pieces = [cid]
                for _al, n in nodes:
                    best = None
                    for pid in pieces:
                        pr = cur.execute("SELECT * FROM cables WHERE id = ?", (pid,)).fetchone()
                        pc = _cable_coords(pr)
                        if not pc:
                            continue
                        sn = _snap_to_polyline(pc, n["latitude"], n["longitude"])
                        if best is None or sn["offset_m"] < best[0]:
                            best = (sn["offset_m"], pid)
                    if best is None:
                        raise HTTPException(status_code=400, detail="Geometri kabel tidak valid")
                    cur.execute("SAVEPOINT tfsplit")
                    try:
                        res = _split_cable_at(cur, best[1], n["latitude"], n["longitude"], None, {}, existing_node_id=n["id"])
                    except HTTPException as exc:      # satu aset gagal tidak menggagalkan aset lain pada kabel yang sama
                        cur.execute("ROLLBACK TO tfsplit")
                        cur.execute("RELEASE tfsplit")
                        why = str(exc.detail)
                        if "ujung kabel" in why:
                            why = "Berimpit dengan titik sambung/ujung kabel lain (dilewati)"
                        failed.append({"cable_id": cid, "node_id": n["id"], "node_name": n["name"], "reason": why})
                        continue
                    cur.execute("RELEASE tfsplit")
                    pieces.append(res["cable_b_id"])
                    done_nodes += 1
                for k, pid in enumerate(pieces[1:], start=2):     # nama segmen rapi: <nama>-seg2, -seg3, ...
                    want = _norm_name(f"{cab0['name']}-seg{k}")
                    cur_nm = cur.execute("SELECT name FROM cables WHERE id = ?", (pid,)).fetchone()["name"]
                    if cur_nm == want:
                        continue
                    nm = _unique_cable_name(cur, want)
                    cur.execute("UPDATE cables SET name = ? WHERE id = ?", (nm, pid))
                new_segments += len(pieces) - 1
                if len(pieces) == 1:
                    continue
                _audit(cur, "UPDATE", "CABLE", cid, cab0["name"],
                       f"Rapikan topologi: kabel '{cab0['name']}' dipecah di {len(pieces) - 1} aset menjadi {len(pieces)} segmen")
            done_cables += 1
        except HTTPException as exc:
            failed.append({"cable_id": cid, "reason": str(exc.detail)})
    return {"message": f"{done_nodes} aset menyambung ke kabel: {done_cables} kabel dipecah menjadi {done_cables + new_segments} segmen"
                       + (f"; {len(failed)} kabel dilewati" if failed else ""),
            "cables": done_cables, "nodes": done_nodes, "new_segments": new_segments, "failed": failed[:50], "failed_total": len(failed)}


# --- ujung kabel yatim: tempel ke aset terdekat ---
class TopofixEndsReq(BaseModel):
    max_m: float = Field(100.0, ge=5.0, le=300.0)
    types: List[str] = Field(default_factory=lambda: list(TOPOFIX_END_TYPES))


class TopofixEndPair(BaseModel):
    cable_id: int
    end: str          # "from" | "to"
    node_id: int


class TopofixEndsApply(BaseModel):
    items: List[TopofixEndPair] = Field(default_factory=list)
    max_m: float = Field(100.0, ge=5.0, le=300.0)


def _tf_end_rows(cursor, max_m, types):
    q = ",".join("?" * len(types))
    nodes = [dict(r) for r in cursor.execute(
        f"SELECT id, name, type, latitude, longitude FROM nodes WHERE type IN ({q}) AND latitude IS NOT NULL AND longitude IS NOT NULL", types).fetchall()]
    rows, unmatched = [], []
    for cab in cursor.execute("SELECT * FROM cables WHERE from_node_id IS NULL OR to_node_id IS NULL ORDER BY id").fetchall():
        coords = _cable_coords(cab)
        if not coords:
            continue
        for end, col, pt in (("from", "from_node_id", coords[0]), ("to", "to_node_id", coords[-1])):
            if cab[col] is not None:
                continue
            other = cab["to_node_id"] if end == "from" else cab["from_node_id"]
            near = sorted(((_haversine_m(pt[1], pt[0], n["latitude"], n["longitude"]), n) for n in nodes if n["id"] != other), key=lambda x: x[0])
            near = [(d, n) for d, n in near if d <= max_m][:3]
            if not near:
                unmatched.append({"cable_id": cab["id"], "cable_name": cab["name"], "end": end})
                continue
            d0, n0 = near[0]
            rows.append({"cable_id": cab["id"], "cable_name": cab["name"], "cable_type": cab["type"], "end": end,
                         "node_id": n0["id"], "node_name": n0["name"], "node_type": n0["type"], "distance_m": round(d0, 1),
                         "alternatives": [{"node_id": n["id"], "node_name": n["name"], "node_type": n["type"], "distance_m": round(d, 1)} for d, n in near[1:]],
                         "checked": d0 <= 50})
    return rows, unmatched


@app.post("/api/topofix/ends/preview")
def topofix_ends_preview(req: TopofixEndsReq):
    types = _tf_types(req.types, TOPOFIX_END_TYPES)
    with db() as conn:
        rows, unmatched = _tf_end_rows(conn.cursor(), float(req.max_m), types)
    return {"total": len(rows), "rows": rows[:TOPOFIX_MAX_ROWS], "unmatched": unmatched[:200], "unmatched_total": len(unmatched)}


@app.post("/api/topofix/ends/apply")
def topofix_ends_apply(req: TopofixEndsApply):
    items = list(dict.fromkeys((i.cable_id, i.end, i.node_id) for i in req.items))
    if not items:
        raise HTTPException(status_code=400, detail="Tidak ada ujung kabel yang dipilih")
    if len(items) > TOPOFIX_APPLY_MAX:
        raise HTTPException(status_code=400, detail=f"Maksimal {TOPOFIX_APPLY_MAX} ujung per permintaan")
    ok, failed = 0, []
    for cid, end, nid in items:
        try:
            if end not in ("from", "to"):
                raise HTTPException(status_code=400, detail="Ujung harus from atau to")
            with db() as conn:
                cur = conn.cursor()
                cab = cur.execute("SELECT * FROM cables WHERE id = ?", (cid,)).fetchone()
                n = cur.execute("SELECT id, name, type, latitude, longitude FROM nodes WHERE id = ?", (nid,)).fetchone()
                if not cab or not n:
                    raise HTTPException(status_code=404, detail="Kabel atau aset tidak ditemukan")
                coords = _cable_coords(cab)
                col = "from_node_id" if end == "from" else "to_node_id"
                if cab[col] is not None:
                    raise HTTPException(status_code=409, detail="Ujung ini sudah terhubung ke aset")
                other = cab["to_node_id"] if end == "from" else cab["from_node_id"]
                if other == nid:
                    raise HTTPException(status_code=400, detail="Kedua ujung kabel tidak boleh ke aset yang sama")
                pt = coords[0] if end == "from" else coords[-1]
                d = _haversine_m(pt[1], pt[0], n["latitude"], n["longitude"])
                if d > float(req.max_m):
                    raise HTTPException(status_code=400, detail=f"{n['name']} berjarak {d:.0f} m dari ujung kabel (maks {req.max_m:g} m)")
                cur.execute(f"UPDATE cables SET {col} = ? WHERE id = ?", (nid, cid))
                _audit(cur, "UPDATE", "CABLE", cid, cab["name"],
                       f"Rapikan topologi: ujung {'asal' if end == 'from' else 'tujuan'} kabel '{cab['name']}' ditempel ke {n['type']} '{n['name']}' ({d:.0f} m)")
            ok += 1
        except HTTPException as exc:
            failed.append({"cable_id": cid, "end": end, "reason": str(exc.detail)})
    return {"message": f"{ok} ujung kabel ditempel" + (f", {len(failed)} dilewati" if failed else ""), "updated": ok,
            "failed": failed[:50], "failed_total": len(failed)}


# ===== PAKET 2-4: sambung core massal dari POP, pelanggan -> ODP, komponen terpisah & hubungkan manual =====
_TF_BATCH_RE = r"^[A-Za-z0-9_-]{4,40}$"
TOPOFIX_CONN_MAX = 120


def _tf_graph(cursor):
    """Graf topologi dari kabel yang kedua ujungnya sudah tersambung ke node. depth = jumlah hop dari POP terdekat (None = belum terjangkau)."""
    nodes = {r["id"]: dict(r) for r in cursor.execute("SELECT id, name, type, latitude, longitude, capacity, cluster, area, city FROM nodes").fetchall()}
    cabs = [dict(r) for r in cursor.execute("SELECT id, name, type, capacity, from_node_id, to_node_id FROM cables ORDER BY id").fetchall()]
    adj = {}
    for c in cabs:
        a, b = c["from_node_id"], c["to_node_id"]
        if a in nodes and b in nodes and a != b:
            adj.setdefault(a, []).append(b)
            adj.setdefault(b, []).append(a)
    depth, dq = {}, deque()
    for i, n in nodes.items():
        if (n["type"] or "").upper() == "POP":
            depth[i] = 0
            dq.append(i)
    while dq:
        x = dq.popleft()
        for y in adj.get(x, ()):
            if y not in depth:
                depth[y] = depth[x] + 1
                dq.append(y)
    return nodes, cabs, adj, depth


def _tf_direction(nodes, depth, cab):
    """(hulu, hilir) sebuah kabel: ujung yang lebih dekat ke POP = hulu; sama dekat -> menurut urutan jenis aset (POP < closure < ODP < pelanggan)."""
    a, b = nodes.get(cab["from_node_id"]), nodes.get(cab["to_node_id"])
    if not a or not b:
        return None
    ta, tb = (a["type"] or "").upper(), (b["type"] or "").upper()
    if ta not in AC_RANK or tb not in AC_RANK:
        return None
    ka = (depth.get(a["id"], 10 ** 6), AC_RANK[ta])
    kb = (depth.get(b["id"], 10 ** 6), AC_RANK[tb])
    return (a, b) if ka <= kb else (b, a)


def _tf_orient_all(cursor, nodes, cabs, depth, dry=False):
    """Samakan arah kabel: node asal (from) = hulu (lebih dekat ke POP). Hanya kabel tanpa sambungan core dan yang arahnya tidak seri.
    Geometri ikut dibalik. Return jumlah kabel yang dibalik."""
    used = {r[0] for r in cursor.execute("SELECT DISTINCT via_cable_id FROM core_connections WHERE via_cable_id IS NOT NULL").fetchall()}
    n = 0
    for c in cabs:
        d = _tf_direction(nodes, depth, c)
        if not d or c["id"] in used:
            continue
        a, b = nodes[c["from_node_id"]], nodes[c["to_node_id"]]
        ka = (depth.get(a["id"], 10 ** 6), AC_RANK[(a["type"] or "").upper()])
        kb = (depth.get(b["id"], 10 ** 6), AC_RANK[(b["type"] or "").upper()])
        if ka == kb or d[0]["id"] == c["from_node_id"]:
            continue
        n += 1
        if dry:
            continue
        row = cursor.execute("SELECT geojson_geometry FROM cables WHERE id = ?", (c["id"],)).fetchone()
        coords = _cable_coords(row) if row else None
        if not coords:
            n -= 1
            continue
        cursor.execute("UPDATE cables SET geojson_geometry = ?, from_node_id = ?, to_node_id = ? WHERE id = ?",
                       (json.dumps({"type": "LineString", "coordinates": list(reversed(coords))}), c["to_node_id"], c["from_node_id"], c["id"]))
        c["from_node_id"], c["to_node_id"] = c["to_node_id"], c["from_node_id"]
    return n


def _tf_default_cores(up, down, cab):
    tu, td = (up["type"] or "").upper(), (down["type"] or "").upper()
    if td == "ODP" or tu == "ODP":
        return 1
    if td == "PELANGGAN":      # dedicated 2 core dari closure/POP; dari ODP 1 core
        return 2 if (_core_total(down["capacity"]) >= 2 and _core_total(cab["capacity"]) >= 2) else 1
    return 1


class TopofixConnReq(BaseModel):
    trunk: bool = False       # sertakan kabel antar closure/POP (sambung penuh); bawaan: hanya kabel akses ke ODP/pelanggan


class TopofixConnItem(BaseModel):
    cable_id: int
    cores: Optional[int] = Field(None, ge=1, le=288)
    full: bool = False
    via_cores: Optional[List[str]] = None     # core tertentu pada kabel ini (label), mis. "Tube 1 - Core 3"
    from_cores: Optional[List[str]] = None    # core kabel hulu yang dipetakan ke via_cores (urutan sama): pemetaan hulu->hilir
    upstream_cable_id: Optional[int] = None   # kabel hulu yang memasok core (bila closure punya beberapa kabel hulu)


class TopofixConnApply(BaseModel):
    items: List[TopofixConnItem] = Field(default_factory=list)
    batch: str = Field(..., pattern=_TF_BATCH_RE)
    trunk: bool = False
    orient: bool = True       # samakan arah kabel hulu -> hilir lebih dulu (kabel impor KMZ arahnya acak)


@app.post("/api/topofix/connect/preview")
def topofix_connect_preview(req: TopofixConnReq):
    with db() as conn:
        cur = conn.cursor()
        nodes, cabs, adj, depth = _tf_graph(cur)
        done = {r[0] for r in cur.execute("SELECT DISTINCT via_cable_id FROM core_connections WHERE via_cable_id IS NOT NULL").fetchall()}
        rows, skipped_open = [], 0
        for c in cabs:
            if not (c["from_node_id"] in nodes and c["to_node_id"] in nodes):
                skipped_open += 1
                continue
            d = _tf_direction(nodes, depth, c)
            if not d:
                continue
            up, down = d
            td = (down["type"] or "").upper()
            access = td in ("ODP", "PELANGGAN")
            if not access and not req.trunk:
                continue
            du, dd = depth.get(up["id"]), depth.get(down["id"])
            if c["id"] in done:
                state, reason = "sudah", "Kabel ini sudah punya sambungan core"
            elif du is None or dd is None:
                state, reason = "terpisah", "Belum terhubung ke POP lewat rantai kabel (rapikan topologi dulu)"
            else:
                state, reason = "siap", None
            full = (not access)
            rows.append({"cable_id": c["id"], "cable_name": c["name"], "cable_type": c["type"], "capacity": c["capacity"],
                         "up_id": up["id"], "up_name": up["name"], "up_type": up["type"],
                         "down_id": down["id"], "down_name": down["name"], "down_type": down["type"],
                         "depth": dd, "cores": None if full else _tf_default_cores(up, down, c), "full": full,
                         "state": state, "reason": reason, "checked": state == "siap"})
        rows.sort(key=lambda r: (r["depth"] if r["depth"] is not None else 10 ** 6, AC_RANK.get((r["down_type"] or "").upper(), 9), r["cable_id"]))
        orient_needed = _tf_orient_all(cur, nodes, cabs, depth, dry=True)
        summ = {}
        for r in rows:
            summ[r["state"]] = summ.get(r["state"], 0) + 1
    total = len(rows)
    return {"total": total, "summary": summ, "open_ends_cables": skipped_open, "orient_needed": orient_needed, "rows": rows[:TOPOFIX_MAX_ROWS], "truncated": total > TOPOFIX_MAX_ROWS}


@app.post("/api/topofix/connect/apply")
def topofix_connect_apply(req: TopofixConnApply):
    if not req.items:
        raise HTTPException(status_code=400, detail="Tidak ada kabel yang dipilih")
    if len(req.items) > TOPOFIX_CONN_MAX:
        raise HTTPException(status_code=400, detail=f"Maksimal {TOPOFIX_CONN_MAX} kabel per permintaan")
    ok_n, links_n, failed, results = 0, 0, [], []
    tag = f" [TFB#{req.batch}]"
    with db() as conn:
        cur = conn.cursor()
        nodes, cabs, adj, depth = _tf_graph(cur)
        flipped = _tf_orient_all(cur, nodes, cabs, depth) if req.orient else 0
        by_id = {c["id"]: c for c in cabs}
        for it in req.items:
            cab = by_id.get(it.cable_id)
            if not cab:
                failed.append({"cable_id": it.cable_id, "name": f"#{it.cable_id}", "reason": "Kabel tidak ditemukan"})
                continue
            d = _tf_direction(nodes, depth, cab)
            if not d:
                failed.append({"cable_id": cab["id"], "name": cab["name"], "reason": "Kedua ujung kabel harus tersambung ke POP/closure/slack/ODP/pelanggan"})
                continue
            up, down = d
            if depth.get(up["id"]) is None or depth.get(down["id"]) is None:
                failed.append({"cable_id": cab["id"], "name": cab["name"], "reason": "Belum terhubung ke POP lewat rantai kabel"})
                continue
            if cur.execute("SELECT 1 FROM core_connections WHERE via_cable_id = ? LIMIT 1", (cab["id"],)).fetchone():
                failed.append({"cable_id": cab["id"], "name": cab["name"], "reason": "Sudah punya sambungan core (dilewati)"})
                continue
            n = int(it.cores or _tf_default_cores(up, down, cab))
            vc = [str(x) for x in it.via_cores] if it.via_cores else None
            if vc:
                n = len(vc)
            arq = AutoConnectReq(cores=n, respect_order=True, reverse=(up["id"] == cab["to_node_id"]), full=bool(it.full) and not vc, via_cores=vc,
                                  from_ports=([str(x) for x in it.from_cores] if (vc and it.from_cores) else None),
                                  upstream_cable_id=it.upstream_cable_id if (vc and it.from_cores) else None)
            before = cur.execute("SELECT COALESCE(MAX(id), 0) AS m FROM core_connections").fetchone()["m"]
            cur.execute("SAVEPOINT tfconn")
            try:
                out = _ac_plan_and_apply(cur, cab["id"], arq)
                if not out.get("ok") and out.get("needs_choice"):
                    # beberapa kabel hulu: pilih yang hulunya paling dekat ke POP
                    cur.execute("ROLLBACK TO tfconn")
                    def _hop(cd):
                        cc = by_id.get(cd["id"])
                        return (depth.get(cc["from_node_id"], 10 ** 6), cd["id"]) if cc else (10 ** 6, cd["id"])
                    pick = min(out["needs_choice"], key=_hop)
                    out = _ac_plan_and_apply(cur, cab["id"], arq.model_copy(update={"upstream_cable_id": pick["id"]}))
                if not out.get("ok") and n == 2 and (down["type"] or "").upper() == "PELANGGAN":
                    # dedicated 2 core tidak tersedia -> coba 1 core dan beri catatan
                    cur.execute("ROLLBACK TO tfconn")
                    out = _ac_plan_and_apply(cur, cab["id"], arq.model_copy(update={"cores": 1}))
                    if out.get("ok"):
                        out.setdefault("warnings", []).append("2 core dedicated tidak tersedia; disambung 1 core")
            except HTTPException as exc:
                out = {"ok": False, "reason": str(exc.detail)}
            if out.get("ok"):
                cur.execute("UPDATE core_connections SET notes = TRIM(COALESCE(notes, '') || ?) WHERE id > ?", (tag, before))
                cur.execute("RELEASE tfconn")
                made = cur.execute("SELECT COUNT(*) AS n FROM core_connections WHERE id > ?", (before,)).fetchone()["n"]
                ok_n += 1
                links_n += made
                results.append({"cable_id": cab["id"], "name": cab["name"], "links": made, "warnings": out.get("warnings") or []})
            else:
                cur.execute("ROLLBACK TO tfconn")
                cur.execute("RELEASE tfconn")
                failed.append({"cable_id": cab["id"], "name": cab["name"], "reason": out.get("reason") or "Gagal",
                               "needs_choice": bool(out.get("needs_choice"))})
        if ok_n or flipped:
            _audit(cur, "CONNECT", "BULK", None, f"sambung massal {req.batch}",
                   f"Sambung core massal (batch {req.batch}): {ok_n} kabel, {links_n} sambungan" + (f"; {flipped} kabel dibalik arahnya (hulu→hilir)" if flipped else "")
                   + (f"; {len(failed)} dilewati" if failed else ""))
    return {"message": f"{ok_n} kabel tersambung ({links_n} sambungan)" + (f", {len(failed)} dilewati" if failed else ""),
            "connected": ok_n, "links": links_n, "flipped": flipped, "results": results[:100], "failed": failed[:100], "failed_total": len(failed)}


@app.delete("/api/topofix/connect/batch/{batch}")
def topofix_connect_undo(batch: str):
    if not re.match(_TF_BATCH_RE, batch):
        raise HTTPException(status_code=400, detail="Kode batch tidak valid")
    with db() as conn:
        cur = conn.cursor()
        rows = cur.execute("SELECT * FROM core_connections WHERE notes LIKE ?", (f"%[TFB#{batch}]%",)).fetchall()
        if not rows:
            raise HTTPException(status_code=404, detail="Tidak ada sambungan untuk batch ini")
        for r in rows:
            _audit(cur, "DISCONNECT", "CONNECTION", r["id"], _conn_label(cur, r), f"Batal sambung massal {batch}", snapshot=_row_dict(r))
            cur.execute("DELETE FROM core_connections WHERE id = ?", (r["id"],))
    return {"message": f"{len(rows)} sambungan dibatalkan", "removed": len(rows)}


@app.get("/api/topofix/connect/batches")
def topofix_connect_batches():
    """Batch sambung massal yang masih ada (untuk tombol Batalkan)."""
    out = {}
    with db() as conn:
        for r in conn.execute("SELECT notes FROM core_connections WHERE notes LIKE '%[TFB#%'").fetchall():
            for m in re.finditer(r"\[TFB#([A-Za-z0-9_-]+)\]", r["notes"] or ""):
                out[m.group(1)] = out.get(m.group(1), 0) + 1
    return {"batches": [{"batch": k, "links": v} for k, v in sorted(out.items(), reverse=True)]}


# --- Paket 3: pelanggan tanpa kabel -> ODP (tag PON + kemiripan nama + jarak, port OUT kosong) ---
_TF_CUST_TAG = re.compile(r"\[\s*(\d+)\s*@\s*(\d+/\d+/\d+)\s*\]")
_TF_ODP_TAG = re.compile(r"\[\s*(\d+/\d+/\d+)\s*\]")
_TF_STOP = {"odp", "spliter", "splitter", "level", "lv", "pt", "cv", "dan", "the", "cabang", "kantor"}


def _tf_name_tokens(name):
    s = re.sub(r"\[[^\]]*\]", " ", str(name or "").lower())
    s = re.sub(r"\b\d{6,}\b-?", " ", s)               # ID pelanggan
    return {t for t in re.split(r"[^a-z0-9]+", s) if len(t) >= 3 and not t.isdigit() and t not in _TF_STOP}


class TopofixCustReq(BaseModel):
    max_m: float = Field(500.0, ge=20.0, le=3000.0)
    all_customers: bool = False       # False: hanya pelanggan yang belum punya kabel


def _tf_customer_rows(cursor, max_m, all_customers=False):
    nodes, cabs, adj, depth = _tf_graph(cursor)
    ends = set()
    for c in cabs:
        if c["from_node_id"]:
            ends.add(c["from_node_id"])
        if c["to_node_id"]:
            ends.add(c["to_node_id"])
    odps, pending = [], {}
    for n in nodes.values():
        if (n["type"] or "").upper() != "ODP" or n["latitude"] is None or n["longitude"] is None:
            continue
        outs = [x for x in _port_labels("NODE", "ODP", n["capacity"]) if x.startswith("OUT-")]
        used = _used_ports(cursor, "NODE", n["id"])
        # port OUT yang sudah 'dijanjikan' oleh kabel yang berujung di ODP ini tetapi belum disambung
        odps.append({"n": n, "free": len([x for x in outs if x not in used]), "tokens": _tf_name_tokens(n["name"]),
                     "pon": (_TF_ODP_TAG.search(n["name"]).group(1) if _TF_ODP_TAG.search(n["name"]) else None),
                     "reach": depth.get(n["id"]) is not None})
    rows, none_found = [], []
    custs = [n for n in nodes.values() if (n["type"] or "").upper() == "PELANGGAN" and n["latitude"] is not None and n["longitude"] is not None
             and (all_customers or n["id"] not in ends)]
    custs.sort(key=lambda n: n["id"])
    pad = max_m / 111320.0 + 1e-5
    for cu in custs:
        m = _TF_CUST_TAG.search(cu["name"])
        pon = m.group(2) if m else None
        toks = _tf_name_tokens(cu["name"])
        cands = []
        for o in odps:
            on = o["n"]
            if abs(on["latitude"] - cu["latitude"]) > pad:
                continue
            d = _haversine_m(cu["latitude"], cu["longitude"], on["latitude"], on["longitude"])
            if d > max_m:
                continue
            inter = len(toks & o["tokens"])
            name_ok = bool(inter and inter / max(1, min(len(toks), len(o["tokens"]))) >= 0.5)
            tag_ok = bool(pon and o["pon"] and pon == o["pon"])
            cands.append({"o": o, "d": d, "name_ok": name_ok, "tag_ok": tag_ok})
        cands = [c for c in cands if c["o"]["free"] - pending.get(c["o"]["n"]["id"], 0) > 0]
        if not cands:
            none_found.append({"customer_id": cu["id"], "customer_name": cu["name"]})
            continue
        cands.sort(key=lambda c: (not c["name_ok"], not c["o"]["reach"], not c["tag_ok"], c["d"]))
        best = cands[0]
        basis = "nama cocok" if best["name_ok"] else ("tag PON sama" if best["tag_ok"] else "terdekat")
        pending[best["o"]["n"]["id"]] = pending.get(best["o"]["n"]["id"], 0) + 1
        rows.append({"customer_id": cu["id"], "customer_name": cu["name"], "odp_id": best["o"]["n"]["id"], "odp_name": best["o"]["n"]["name"],
                     "distance_m": round(best["d"], 1), "basis": basis, "odp_free": best["o"]["free"], "odp_reachable": best["o"]["reach"],
                     "alternatives": [{"odp_id": c["o"]["n"]["id"], "odp_name": c["o"]["n"]["name"], "distance_m": round(c["d"], 1)} for c in cands[1:4]],
                     "checked": bool(best["o"]["reach"] and (best["name_ok"] or best["d"] <= 300))})
    return rows, none_found


@app.post("/api/topofix/customers/preview")
def topofix_customers_preview(req: TopofixCustReq):
    with db() as conn:
        rows, none_found = _tf_customer_rows(conn.cursor(), float(req.max_m), bool(req.all_customers))
    return {"total": len(rows), "rows": rows[:TOPOFIX_MAX_ROWS], "unmatched": none_found[:200], "unmatched_total": len(none_found),
            "truncated": len(rows) > TOPOFIX_MAX_ROWS}


class TopofixCustPair(BaseModel):
    customer_id: int
    odp_id: int


class TopofixCustApply(BaseModel):
    items: List[TopofixCustPair] = Field(default_factory=list)
    max_m: float = Field(500.0, ge=20.0, le=3000.0)
    connect: bool = True              # sambungkan core (ODP OUT -> pelanggan) setelah kabel drop dibuat


@app.post("/api/topofix/customers/apply")
def topofix_customers_apply(req: TopofixCustApply):
    items = list(dict.fromkeys((i.customer_id, i.odp_id) for i in req.items))
    if not items:
        raise HTTPException(status_code=400, detail="Tidak ada pelanggan yang dipilih")
    if len(items) > TOPOFIX_APPLY_MAX:
        raise HTTPException(status_code=400, detail=f"Maksimal {TOPOFIX_APPLY_MAX} pelanggan per permintaan")
    made, connected, failed, notes = 0, 0, [], []
    for cid, oid in items:
        try:
            with db() as conn:
                cur = conn.cursor()
                cu = cur.execute("SELECT * FROM nodes WHERE id = ? AND type = 'PELANGGAN'", (cid,)).fetchone()
                od = cur.execute("SELECT * FROM nodes WHERE id = ? AND type = 'ODP'", (oid,)).fetchone()
                if not cu or not od:
                    raise HTTPException(status_code=404, detail="Pelanggan atau ODP tidak ditemukan")
                if cur.execute("SELECT 1 FROM cables WHERE from_node_id = ? OR to_node_id = ? LIMIT 1", (cid, cid)).fetchone():
                    raise HTTPException(status_code=409, detail="Pelanggan sudah punya kabel")
                d = _haversine_m(cu["latitude"], cu["longitude"], od["latitude"], od["longitude"])
                if d > float(req.max_m):
                    raise HTTPException(status_code=400, detail=f"ODP {od['name']} berjarak {d:.0f} m (maks {req.max_m:g} m)")
                outs = [x for x in _port_labels("NODE", "ODP", od["capacity"]) if x.startswith("OUT-")]
                if not [x for x in outs if x not in _used_ports(cur, "NODE", oid)]:
                    raise HTTPException(status_code=409, detail=f"Port OUT {od['name']} sudah penuh")
                base = re.sub(r"\[[^\]]*\]", " ", cu["name"]).strip()
                base = re.sub(r"^\d{6,}\s*-\s*", "", base).strip() or cu["name"]
                nm = _unique_cable_name(cur, f"DC-{base}"[:80])
            r = create_cable(CableCreate(name=nm, type="Drop", status="Active",
                                         coordinates=[[od["longitude"], od["latitude"]], [cu["longitude"], cu["latitude"]]],
                                         cluster=cu["cluster"], area=cu["area"], city=cu["city"], capacity="2C", installation="Udara",
                                         from_node_id=oid, to_node_id=cid))
            made += 1
            if req.connect:
                with db() as conn:
                    cur = conn.cursor()
                    cur.execute("SAVEPOINT tfcust")
                    try:
                        out = _ac_plan_and_apply(cur, r["id"], AutoConnectReq(cores=1, respect_order=True))
                    except HTTPException as exc:
                        out = {"ok": False, "reason": str(exc.detail)}
                    if out.get("ok"):
                        cur.execute("RELEASE tfcust")
                        connected += 1
                    else:
                        cur.execute("ROLLBACK TO tfcust")
                        cur.execute("RELEASE tfcust")
                        notes.append({"customer_id": cid, "name": cu["name"], "reason": "Kabel dibuat, core belum tersambung: " + str(out.get("reason"))})
        except HTTPException as exc:
            failed.append({"customer_id": cid, "reason": str(exc.detail)})
    with db() as conn:
        _audit(conn.cursor(), "CREATE", "BULK", None, "pelanggan ke ODP",
               f"Pelanggan -> ODP: {made} kabel drop dibuat, {connected} tersambung core" + (f"; {len(failed)} dilewati" if failed else ""))
    return {"message": f"{made} pelanggan dihubungkan ke ODP ({connected} sudah tersambung core)" + (f", {len(failed)} dilewati" if failed else ""),
            "created": made, "connected": connected, "failed": failed[:50], "failed_total": len(failed), "notes": notes[:50]}


# --- Paket 4: komponen terpisah dari POP + hubungkan ujung kabel manual (seret-lepas di peta) ---
class TopofixCompReq(BaseModel):
    max_m: float = Field(500.0, ge=20.0, le=3000.0)


def _tf_components(nodes, cabs, depth):
    adj = {}
    for c in cabs:
        a, b = c["from_node_id"], c["to_node_id"]
        if a in nodes and b in nodes and a != b:
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)
    seen, comps = set(), []
    for i in nodes:
        if i in seen or i not in adj:
            continue
        comp, st = [], [i]
        seen.add(i)
        while st:
            x = st.pop()
            comp.append(x)
            for y in adj.get(x, ()):
                if y not in seen:
                    seen.add(y)
                    st.append(y)
        comps.append(comp)
    return comps, adj


@app.post("/api/topofix/components/preview")
def topofix_components_preview(req: TopofixCompReq):
    link_types = {"CLOSURE", "ODP", "POP", "SLACK"}
    with db() as conn:
        cur = conn.cursor()
        nodes, cabs, adj, depth = _tf_graph(cur)
        comps, _ = _tf_components(nodes, cabs, depth)
        in_comp = {x for comp in comps for x in comp}
        # titik lepas (tanpa kabel) yang berarti: closure/ODP/pelanggan
        singles = [[i] for i, n in nodes.items() if i not in in_comp and (n["type"] or "").upper() in ("CLOSURE", "ODP", "PELANGGAN")]
        comps += singles
        grid = {}
        for i in depth:
            n = nodes[i]
            if (n["type"] or "").upper() in link_types and n["latitude"] is not None:
                grid.setdefault((int(n["latitude"] / 0.01), int(n["longitude"] / 0.01)), []).append(i)
        rows, unmatched = [], 0
        lim = float(req.max_m)
        for comp in comps:
            if any(i in depth for i in comp):
                continue
            ty = {}
            for i in comp:
                t = (nodes[i]["type"] or "").upper()
                ty[t] = ty.get(t, 0) + 1
            if not (ty.get("CLOSURE") or ty.get("ODP") or ty.get("PELANGGAN")):
                continue
            best = None
            for i in comp:
                n = nodes[i]
                if (n["type"] or "").upper() not in link_types or n["latitude"] is None:
                    continue
                gx, gy = int(n["latitude"] / 0.01), int(n["longitude"] / 0.01)
                span = int(lim / 1000.0) + 1
                for dx in range(-span, span + 1):
                    for dy in range(-span, span + 1):
                        for j in grid.get((gx + dx, gy + dy), ()):
                            m = nodes[j]
                            d = _haversine_m(n["latitude"], n["longitude"], m["latitude"], m["longitude"])
                            if d <= lim and (best is None or d < best[0]):
                                best = (d, i, j)
            pick = min(comp, key=lambda i: nodes[i]["name"] or "")
            row = {"component": comp[0], "size": len(comp), "closures": ty.get("CLOSURE", 0), "odps": ty.get("ODP", 0),
                   "customers": ty.get("PELANGGAN", 0), "sample": nodes[pick]["name"], "lat": nodes[pick]["latitude"], "lng": nodes[pick]["longitude"]}
            if best:
                a, b = nodes[best[2]], nodes[best[1]]
                row.update({"from_id": a["id"], "from_name": a["name"], "from_type": a["type"], "to_id": b["id"], "to_name": b["name"],
                            "to_type": b["type"], "distance_m": round(best[0], 1)})
            else:
                unmatched += 1
            rows.append(row)
        rows.sort(key=lambda r: (r.get("distance_m") is None, r.get("distance_m") or 0))
        reachable = {t: 0 for t in ("CLOSURE", "ODP", "PELANGGAN")}
        for i in depth:
            t = (nodes[i]["type"] or "").upper()
            if t in reachable:
                reachable[t] += 1
    return {"total": len(rows), "rows": rows[:TOPOFIX_MAX_ROWS], "unmatched": unmatched, "reachable": reachable}


class TopofixLinkReq(BaseModel):
    from_id: int
    to_id: int
    capacity: str = Field("12C", pattern=r"^\d{1,3}C$")
    cable_type: str = "Distribution"


class TopofixLinkApply(BaseModel):
    items: List[TopofixLinkReq] = Field(default_factory=list)


@app.post("/api/topofix/components/apply")
def topofix_components_apply(req: TopofixLinkApply):
    """Buat kabel penghubung lurus antara dua aset (komponen terpisah -> jaringan POP). Kabel diberi nama LINK-... agar mudah dikoreksi/diganti jalurnya."""
    if not req.items:
        raise HTTPException(status_code=400, detail="Tidak ada pasangan yang dipilih")
    if len(req.items) > 100:
        raise HTTPException(status_code=400, detail="Maksimal 100 pasangan per permintaan")
    made, failed = 0, []
    for it in req.items:
        try:
            if it.cable_type not in CABLE_TYPES:
                raise HTTPException(status_code=400, detail="Jenis kabel tidak valid")
            with db() as conn:
                cur = conn.cursor()
                a = cur.execute("SELECT * FROM nodes WHERE id = ?", (it.from_id,)).fetchone()
                b = cur.execute("SELECT * FROM nodes WHERE id = ?", (it.to_id,)).fetchone()
                if not a or not b or a["id"] == b["id"]:
                    raise HTTPException(status_code=404, detail="Aset tidak ditemukan atau sama")
                nm = _unique_cable_name(cur, f"LINK-{a['name']} - {b['name']}"[:100])
            create_cable(CableCreate(name=nm, type=it.cable_type, status="Active",
                                     coordinates=[[a["longitude"], a["latitude"]], [b["longitude"], b["latitude"]]],
                                     cluster=a["cluster"], area=a["area"], city=a["city"], capacity=it.capacity, installation="Udara",
                                     from_node_id=a["id"], to_node_id=b["id"]))
            made += 1
        except HTTPException as exc:
            failed.append({"from_id": it.from_id, "to_id": it.to_id, "reason": str(exc.detail)})
    return {"message": f"{made} kabel penghubung dibuat" + (f", {len(failed)} dilewati" if failed else ""), "created": made,
            "failed": failed[:50], "failed_total": len(failed)}


class TopofixEndLink(BaseModel):
    cable_id: int
    end: str
    node_id: Optional[int] = None     # None = lepaskan ujung (buat yatim kembali)


@app.post("/api/topofix/ends/link")
def topofix_end_link(req: TopofixEndLink):
    """Seret-lepas di peta: hubungkan satu ujung kabel ke aset pilihan pengguna (tanpa batas jarak otomatis; jarak dilaporkan) atau lepaskan."""
    if req.end not in ("from", "to"):
        raise HTTPException(status_code=400, detail="Ujung harus from atau to")
    col = "from_node_id" if req.end == "from" else "to_node_id"
    with db() as conn:
        cur = conn.cursor()
        cab = cur.execute("SELECT * FROM cables WHERE id = ?", (req.cable_id,)).fetchone()
        if not cab:
            raise HTTPException(status_code=404, detail="Kabel tidak ditemukan")
        coords = _cable_coords(cab)
        if not coords:
            raise HTTPException(status_code=400, detail="Geometri kabel tidak valid")
        if req.node_id is None:
            if cab[col] is None:
                raise HTTPException(status_code=409, detail="Ujung ini memang belum terhubung")
            if cur.execute("SELECT 1 FROM core_connections WHERE via_cable_id = ? LIMIT 1", (cab["id"],)).fetchone():
                raise HTTPException(status_code=409, detail="Kabel sudah memiliki sambungan core; hapus sambungan dulu")
            cur.execute(f"UPDATE cables SET {col} = NULL WHERE id = ?", (cab["id"],))
            _audit(cur, "UPDATE", "CABLE", cab["id"], cab["name"], f"Lepas ujung {'asal' if req.end == 'from' else 'tujuan'} kabel '{cab['name']}' (seret-lepas)")
            return {"message": "Ujung dilepas", "distance_m": None}
        n = cur.execute("SELECT id, name, type, latitude, longitude FROM nodes WHERE id = ?", (req.node_id,)).fetchone()
        if not n:
            raise HTTPException(status_code=404, detail="Aset tidak ditemukan")
        other = cab["to_node_id"] if req.end == "from" else cab["from_node_id"]
        if other == n["id"]:
            raise HTTPException(status_code=400, detail="Kedua ujung kabel tidak boleh ke aset yang sama")
        if cab[col] is not None and cab[col] != n["id"] and cur.execute("SELECT 1 FROM core_connections WHERE via_cable_id = ? LIMIT 1", (cab["id"],)).fetchone():
            raise HTTPException(status_code=409, detail="Kabel sudah memiliki sambungan core; hapus sambungan dulu sebelum memindahkan ujung")
        pt = coords[0] if req.end == "from" else coords[-1]
        d = _haversine_m(pt[1], pt[0], n["latitude"], n["longitude"])
        if d > 2000:
            raise HTTPException(status_code=400, detail=f"{n['name']} berjarak {d:.0f} m dari ujung kabel (maks 2000 m)")
        cur.execute(f"UPDATE cables SET {col} = ? WHERE id = ?", (n["id"], cab["id"]))
        _audit(cur, "UPDATE", "CABLE", cab["id"], cab["name"],
               f"Seret-lepas: ujung {'asal' if req.end == 'from' else 'tujuan'} kabel '{cab['name']}' dihubungkan ke {n['type']} '{n['name']}' ({d:.0f} m)")
    return {"message": f"Ujung dihubungkan ke {n['name']}", "distance_m": round(d, 1)}


@app.get("/api/topofix/open-ends")
def topofix_open_ends():
    """Semua ujung kabel yang belum tersambung ke aset (untuk mode seret-lepas di peta)."""
    rows = []
    with db() as conn:
        for cab in conn.execute("SELECT * FROM cables WHERE from_node_id IS NULL OR to_node_id IS NULL ORDER BY id").fetchall():
            coords = _cable_coords(cab)
            if not coords:
                continue
            for end, col, pt in (("from", "from_node_id", coords[0]), ("to", "to_node_id", coords[-1])):
                if cab[col] is None:
                    rows.append({"cable_id": cab["id"], "cable_name": cab["name"], "cable_type": cab["type"], "end": end, "lat": pt[1], "lng": pt[0]})
    return {"total": len(rows), "rows": rows[:TOPOFIX_MAX_ROWS], "truncated": len(rows) > TOPOFIX_MAX_ROWS}


# ===== MODE PUZZLE: rangkai rantai aset -> kabel -> core, kabel yang belum ada dibuat otomatis =====
PZ_MAX_CHAIN = 80
PZ_WARN_M = 3000
PZ_DEFAULT_CAP = {"Backbone": "48C", "Feeder": "24C", "Distribution": "12C", "Drop": "2C"}
PZ_TYPES = ("POP", "CLOSURE", "SLACK", "ODP", "PELANGGAN")


class PzNew(BaseModel):
    name: str = ""
    type: Optional[str] = None
    capacity: Optional[str] = None
    installation: str = "Udara"


class PzLink(BaseModel):
    cable_id: Optional[int] = None           # kabel yang sudah ada; kosong = buat kabel baru
    new: Optional[PzNew] = None
    cores: Optional[List[str]] = None        # core yang dipilih (label); kosong = otomatis
    from_cores: Optional[List[str]] = None   # core kabel hulu yang dipetakan ke `cores` (urutan sama); kosong = otomatis
    upstream_cable_id: Optional[int] = None
    to_ports: Optional[List[str]] = None     # port hilir pilihan (ODP IN / port pelanggan), urutan sama dengan `cores`
    done: bool = False                       # sambungan ini sudah diterapkan sebelumnya (hanya jadi induk cabang baru)
    n: Optional[int] = Field(None, ge=1, le=288)


class PzReq(BaseModel):
    chain: List[int]                          # id node berurutan hulu -> hilir (urutan penambahan)
    parents: Optional[List[int]] = None       # indeks induk tiap node (mulai dari node ke-2); kosong = rantai lurus (induk = node sebelumnya)
    links: List[PzLink] = Field(default_factory=list)
    batch: Optional[str] = Field(None, pattern=_TF_BATCH_RE)


def _pz_cable_between(cursor, a, b):
    return [dict(r) for r in cursor.execute(
        "SELECT * FROM cables WHERE (from_node_id = ? AND to_node_id = ?) OR (from_node_id = ? AND to_node_id = ?) ORDER BY id",
        (a, b, b, a)).fetchall()]


def _pz_used_labels(cursor, cable_id):
    used = set()
    for r in cursor.execute("SELECT from_asset_type, from_asset_id, from_port_core, to_asset_type, to_asset_id, to_port_core, via_cable_id, via_core "
                            "FROM core_connections WHERE via_cable_id = ? OR (from_asset_type = 'CABLE' AND from_asset_id = ?) "
                            "OR (to_asset_type = 'CABLE' AND to_asset_id = ?)", (cable_id, cable_id, cable_id)).fetchall():
        if r["via_cable_id"] == cable_id and r["via_core"]:
            used.add(r["via_core"])
        if r["from_asset_type"] == "CABLE" and r["from_asset_id"] == cable_id and r["from_port_core"]:
            used.add(r["from_port_core"])
        if r["to_asset_type"] == "CABLE" and r["to_asset_id"] == cable_id and r["to_port_core"]:
            used.add(r["to_port_core"])
    return used


def _pz_new_type(up, down):
    tu, td = (up["type"] or "").upper(), (down["type"] or "").upper()
    if td == "PELANGGAN":
        return "Drop"
    if tu == "POP":
        return "Feeder"
    return "Distribution"


def _pz_cable_info(cursor, cab, up_id):
    total = _core_total(cab["capacity"])
    used = _pz_used_labels(cursor, cab["id"])
    labels = _core_labels(total)
    return {"id": cab["id"], "name": cab["name"], "type": cab["type"], "capacity": cab["capacity"], "installation": cab["installation"],
            "length_m": round(_cable_len_km(cab) * 1000), "total": total, "free": [l for l in labels if l not in used],
            "used": len(used), "reversed": cab["from_node_id"] != up_id}


def _pz_plan(cursor, req: PzReq):
    if len(req.chain) < 2:
        raise HTTPException(status_code=400, detail="Rantai butuh minimal 2 aset")
    if len(req.chain) > PZ_MAX_CHAIN:
        raise HTTPException(status_code=400, detail=f"Rantai maksimal {PZ_MAX_CHAIN} aset")
    if len(set(req.chain)) != len(req.chain):
        raise HTTPException(status_code=400, detail="Satu aset tidak boleh muncul dua kali dalam rantai")
    nodes = {}
    for nid in req.chain:
        r = cursor.execute("SELECT * FROM nodes WHERE id = ?", (nid,)).fetchone()
        if not r:
            raise HTTPException(status_code=404, detail=f"Aset #{nid} tidak ditemukan")
        if (r["type"] or "").upper() not in PZ_TYPES:
            raise HTTPException(status_code=400, detail=f"'{r['name']}' ({r['type']}) tidak bisa dirangkai (hanya POP, closure, slack, ODP, pelanggan)")
        nodes[nid] = dict(r)
    for nid in req.chain[1:]:
        if (nodes[nid]["type"] or "").upper() == "POP":
            raise HTTPException(status_code=400, detail="POP hanya boleh di awal rantai")
    if len(req.chain) - 1 != len(req.links):
        raise HTTPException(status_code=400, detail="Jumlah sambungan harus sama dengan jumlah aset - 1")
    par = list(req.parents) if req.parents is not None else list(range(len(req.chain) - 1))
    if len(par) != len(req.chain) - 1 or any((not isinstance(x, int)) or x < 0 or x > i for i, x in enumerate(par)):
        raise HTTPException(status_code=400, detail="Susunan cabang tidak valid (induk harus aset yang lebih dulu ditambahkan)")
    plan, taken = [], []
    for i, lk in enumerate(req.links):
        up, down = nodes[req.chain[par[i]]], nodes[req.chain[i + 1]]
        dist = _haversine_m(up["latitude"], up["longitude"], down["latitude"], down["longitude"])
        existing = [_pz_cable_info(cursor, c, up["id"]) for c in _pz_cable_between(cursor, up["id"], down["id"])]
        item = {"index": i, "from": {"id": up["id"], "name": up["name"], "type": up["type"]}, "to": {"id": down["id"], "name": down["name"], "type": down["type"]},
                "distance_m": round(dist), "existing": existing, "warnings": [], "errors": []}
        if dist > PZ_WARN_M:
            item["warnings"].append(f"Jarak {round(dist)} m cukup jauh; pastikan aset yang dipilih sudah benar")
        if not lk.done and (down["type"] or "").upper() == "ODP":
            _in = [x for x in _port_labels("NODE", "ODP", down["capacity"]) if x.startswith("IN-")]
            _uu = _used_ports(cursor, "NODE", down["id"])
            if _in and all(x in _uu for x in _in) and not (lk.cable_id and cursor.execute("SELECT 1 FROM core_connections WHERE via_cable_id = ? LIMIT 1", (lk.cable_id,)).fetchone()):
                item["errors"].append(f"Port IN {down['name']} sudah terpakai (ODP ini sudah tersambung dari kabel lain)")
        if lk.cable_id:
            cab = cursor.execute("SELECT * FROM cables WHERE id = ?", (lk.cable_id,)).fetchone()
            if not cab or {cab["from_node_id"], cab["to_node_id"]} != {up["id"], down["id"]}:
                item["errors"].append("Kabel yang dipilih tidak menghubungkan kedua aset ini")
                item["mode"] = "ada"
                plan.append(item)
                continue
            info = _pz_cable_info(cursor, cab, up["id"])
            item["mode"], item["cable"] = "ada", info
            if not lk.done and cursor.execute("SELECT 1 FROM core_connections WHERE via_cable_id = ? LIMIT 1", (cab["id"],)).fetchone():
                if (down["type"] or "").upper() == "ODP":
                    item["already"] = True                  # ODP hanya 1 core: sudah tersambung = selesai
                    item["warnings"].append("Kabel ini sudah tersambung ke ODP; dilewati")
                else:
                    item["warnings"].append(f"Kabel ini sudah punya {info['used']} core tersambung; core tambahan akan ditambahkan")
            if lk.done or item.get("already"):
                item["done"], item["cores"] = True, []
                plan.append(item)
                continue
            labels_free = info["free"]
            if info["reversed"]:
                item["warnings"].append("Kabel digambar dari arah sebaliknya; arah sambungan mengikuti rantai Anda")
        else:
            nw = lk.new or PzNew()
            typ = nw.type if nw.type in PZ_DEFAULT_CAP else _pz_new_type(up, down)
            cap = nw.capacity or PZ_DEFAULT_CAP[typ]
            if _core_total(cap) <= 0:
                item["errors"].append(f"Kapasitas '{cap}' tidak valid (contoh 12C)")
                total = 0
            else:
                total = _core_total(cap)
            if nw.installation not in INSTALLATIONS:
                item["errors"].append("Pemasangan harus Udara atau Tanah")
            nm = _norm_name(nw.name)
            if not nm:
                nm = _gen_name(cursor, "CABLE", typ, up["cluster"] or down["cluster"], up["area"] or down["area"], None, taken, cap, nw.installation)
            elif _find_cable_dup(cursor, nm) or nm in taken:
                item["errors"].append(f"Nama kabel '{nm}' sudah dipakai")
            taken.append(nm)
            item["mode"] = "baru"
            item["cable"] = {"id": None, "name": nm, "type": typ, "capacity": cap, "installation": nw.installation, "length_m": round(dist),
                             "total": total, "free": _core_labels(total), "used": 0, "reversed": False}
            labels_free = item["cable"]["free"]
        n = lk.n or _tf_default_cores(up, down, {"capacity": item["cable"]["capacity"]})
        chosen = lk.cores if lk.cores else labels_free[:n]
        bad = [c for c in chosen if c not in labels_free]
        if bad:
            item["errors"].append("Core sudah terpakai atau tidak ada: " + ", ".join(bad[:4]))
        if not chosen and not item["errors"]:
            item["errors"].append("Tidak ada core bebas pada kabel ini")
        if len(set(chosen)) != len(chosen):
            item["errors"].append("Core dipilih ganda")
        fc = list(lk.from_cores or [])
        if fc:
            if len(fc) != len(chosen) or len(set(fc)) != len(fc):
                item["errors"].append("Pemetaan core hulu→hilir tidak valid (jumlah harus sama, tanpa duplikat)")
            else:
                item["from_cores"] = fc
                item["upstream_cable_id"] = lk.upstream_cable_id
        if lk.to_ports:
            if len(lk.to_ports) != len(chosen) or len(set(lk.to_ports)) != len(lk.to_ports):
                item["errors"].append("Port hilir harus sebanyak core dan tanpa duplikat")
            else:
                item["to_ports"] = list(lk.to_ports)
        item["cores"] = chosen
        plan.append(item)
    return plan, nodes


@app.get("/api/topofix/topology")
def topofix_topology(root: int, limit: int = 400, up: bool = True):
    """Pohon topologi dari satu aset (biasanya POP): aset, kabel, dan pemakaian core. Dibatasi `limit` aset agar ringan."""
    limit = max(20, min(int(limit), 800))
    with db() as conn:
        cur = conn.cursor()
        nodes, cabs, adj, _depth = _tf_graph(cur)
        if root not in nodes:
            raise HTTPException(status_code=404, detail="Aset tidak ditemukan")
        requested = root
        if up and (nodes[root]["type"] or "").upper() != "POP":
            # naik ke POP terdekat agar diagram selalu terbaca hulu (kiri) -> hilir (kanan)
            sn, dq0, pop_found = {root}, deque([root]), None
            while dq0 and pop_found is None:
                x = dq0.popleft()
                for y in adj.get(x, ()):
                    if y in sn:
                        continue
                    sn.add(y)
                    if (nodes[y]["type"] or "").upper() == "POP":
                        pop_found = y
                        break
                    dq0.append(y)
            if pop_found is not None:
                root = pop_found
        seen, order, parent, trunc = {root: 0}, [root], {}, False
        dq = deque([root])
        while dq:
            x = dq.popleft()
            for y in adj.get(x, ()):
                if y in seen:
                    continue
                if len(order) >= limit:
                    trunc = True
                    continue
                seen[y] = seen[x] + 1
                parent[y] = x
                order.append(y)
                dq.append(y)
        sel = set(order)
        ph = ",".join("?" * len(order))
        ninfo = {r["id"]: dict(r) for r in cur.execute(f"SELECT id, name, type, status, capacity, latitude, longitude, cluster, area FROM nodes WHERE id IN ({ph})", order).fetchall()}
        cids = [c["id"] for c in cabs if c["from_node_id"] in sel and c["to_node_id"] in sel and c["from_node_id"] != c["to_node_id"]]
        out_c = []
        if cids:
            ph2 = ",".join("?" * len(cids))
            used = {r[0]: r[1] for r in cur.execute(f"SELECT via_cable_id, COUNT(*) FROM core_connections WHERE via_cable_id IN ({ph2}) GROUP BY via_cable_id", cids).fetchall()}
            for r in cur.execute(f"SELECT * FROM cables WHERE id IN ({ph2}) ORDER BY id", cids).fetchall():
                out_c.append({"id": r["id"], "name": r["name"], "type": r["type"], "status": r["status"], "capacity": r["capacity"], "installation": r["installation"],
                              "from": r["from_node_id"], "to": r["to_node_id"], "length_m": round(_cable_len_km(r) * 1000),
                              "used": used.get(r["id"], 0), "total": _core_total(r["capacity"])})
        out_n = []
        for i in order:
            if i not in ninfo:
                continue
            nd = {**ninfo[i], "depth": seen[i], "parent": parent.get(i)}
            t = (nd["type"] or "").upper()
            if t == "POP":      # OTB: port tetap di POP
                nd["pt"] = {"kind": "otb", "used": len(_used_ports(cur, "NODE", i)), "total": len(_port_labels("NODE", t, nd["capacity"]))}
            elif t in JUNCTION_TYPES:   # closure/slack: core yang diteruskan (joint)
                nd["pt"] = {"kind": "core", "used": len(_used_ports(cur, "NODE", i)), "total": _core_total(nd["capacity"])}
            out_n.append(nd)
        return {"root": root, "requested": requested, "truncated": trunc, "nodes": out_n, "cables": out_c}


@app.get("/api/topofix/core-table")
def topofix_core_table(node_id: Optional[int] = None, cable_id: Optional[int] = None):
    """Tabel sambungan core arah hulu -> hilir untuk satu aset (semua sambungan yang menyentuhnya) atau satu kabel."""
    if node_id is None and cable_id is None:
        raise HTTPException(status_code=400, detail="node_id atau cable_id wajib")
    with db() as conn:
        cur = conn.cursor()
        if node_id is not None:
            rows = cur.execute("SELECT * FROM core_connections WHERE (from_asset_type='NODE' AND from_asset_id=?) OR (to_asset_type='NODE' AND to_asset_id=?) ORDER BY id LIMIT 600", (node_id, node_id)).fetchall()
        else:
            rows = cur.execute("SELECT * FROM core_connections WHERE via_cable_id=? OR (from_asset_type='CABLE' AND from_asset_id=?) OR (to_asset_type='CABLE' AND to_asset_id=?) ORDER BY id LIMIT 600", (cable_id, cable_id, cable_id)).fetchall()
        nn = {r["id"]: r["name"] for r in cur.execute("SELECT id, name FROM nodes").fetchall()} if rows else {}
        cn = {r["id"]: r["name"] for r in cur.execute("SELECT id, name FROM cables").fetchall()} if rows else {}

        def desc(t, i, port):
            nm = (nn if t == "NODE" else cn).get(i, f"#{i}")
            return {"type": t, "id": i, "name": nm, "port": port}
        out = []
        for r in rows:
            via = {"id": r["via_cable_id"], "name": cn.get(r["via_cable_id"], ""), "core": r["via_core"]} if r["via_cable_id"] else None
            out.append({"id": r["id"], "hulu": desc(r["from_asset_type"], r["from_asset_id"], r["from_port_core"]), "kabel": via,
                        "hilir": desc(r["to_asset_type"], r["to_asset_id"], r["to_port_core"]), "notes": (r["notes"] or "")[:80]})
        def key(x):
            m = re.findall(r"\d+", (x["kabel"] or {}).get("core") or x["hulu"]["port"] or "")
            return (x["hulu"]["name"], int(m[-1]) if m else 0)
        out.sort(key=key)
        return {"rows": out, "truncated": len(rows) >= 600}


def _free_node_ports(cur, node, direction):
    """Port bebas sebuah aset: direction 'out' = sisi hulu bagi kabel di bawahnya (POP: port OTB, ODP: OUT);
    'in' = sisi hilir (ODP: IN, pelanggan: port)."""
    t = (node["type"] or "").upper()
    lab = _port_labels("NODE", t, node["capacity"])
    if t == "ODP":
        lab = [x for x in lab if x.startswith("OUT-" if direction == "out" else "IN-")]
    used = _used_ports(cur, "NODE", node["id"])
    return lab, [x for x in lab if x not in used]


@app.get("/api/topofix/node-ports")
def topofix_node_ports(node_id: int, dir: str = "out"):
    with db() as conn:
        cur = conn.cursor()
        n = cur.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if not n:
            raise HTTPException(status_code=404, detail="Aset tidak ditemukan")
        lab, free = _free_node_ports(cur, n, "in" if dir == "in" else "out")
        return {"node": {"id": n["id"], "name": n["name"], "type": n["type"]}, "total": len(lab), "ports": free[:400]}


@app.get("/api/topofix/upstream")
def topofix_upstream(node_id: int, exclude_cable: Optional[int] = None):
    """Core hulu yang bisa dipetakan ke kabel di bawah node (closure/slack): joint yang sudah tiba + core bebas tiap kabel hulu."""
    with db() as conn:
        cur = conn.cursor()
        up = cur.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if not up:
            raise HTTPException(status_code=404, detail="Aset tidak ditemukan")
        if (up["type"] or "").upper() not in JUNCTION_TYPES:
            lab, free = _free_node_ports(cur, up, "out")
            return {"junction": False, "node": {"id": up["id"], "name": up["name"], "type": up["type"]}, "ready": [], "cables": [], "ports": free[:400]}
        ins, outs = _joint_dirs(cur, up["id"])
        order = {}
        cabs = []
        for a2 in _cables_at_node(cur, up, cur.execute("SELECT * FROM cables").fetchall()):
            c = a2["row"]
            if not a2["at_end"] or c["id"] == exclude_cable:
                continue
            lab = _core_labels(_core_total(c["capacity"]))
            for x in lab:
                order.setdefault(x, len(order))
            free = [x for x in lab if x not in _used_ports(cur, "CABLE", c["id"]) and x not in ins]
            cabs.append({"id": c["id"], "name": c["name"], "type": c["type"], "capacity": c["capacity"], "total": len(lab), "free": free, "labels": lab})
        ready = sorted((p for p in ins if p not in outs), key=lambda x: order.get(x, 10 ** 6))
        return {"junction": True, "node": {"id": up["id"], "name": up["name"], "type": up["type"]}, "ready": ready, "cables": cabs}


@app.get("/api/topofix/puzzle/nodes")
def topofix_puzzle_nodes(from_id: Optional[int] = None, q: str = "", limit: int = 40, lat: Optional[float] = None, lng: Optional[float] = None):
    """Daftar aset untuk dipilih sebagai blok berikutnya. Dengan from_id: yang sudah punya kabel ke aset itu lebih dulu, lalu terdekat."""
    limit = max(1, min(int(limit), 100))
    qq = q.strip().lower()
    with db() as conn:
        cur = conn.cursor()
        rows = [dict(r) for r in cur.execute(
            "SELECT id, name, type, latitude, longitude, cluster, area FROM nodes WHERE UPPER(type) IN ('POP','CLOSURE','SLACK','ODP','PELANGGAN')").fetchall()]
        src = next((r for r in rows if r["id"] == from_id), None) if from_id else None
        linked = {}
        if src:
            for c in cur.execute("SELECT from_node_id, to_node_id, name FROM cables WHERE from_node_id = ? OR to_node_id = ?", (from_id, from_id)).fetchall():
                o = c["to_node_id"] if c["from_node_id"] == from_id else c["from_node_id"]
                linked.setdefault(o, []).append(c["name"])
        out = []
        for r in rows:
            if r["id"] == from_id:
                continue
            if src and (r["type"] or "").upper() == "POP":
                continue
            if qq and qq not in (r["name"] or "").lower():
                continue
            ref = src or ({"latitude": lat, "longitude": lng} if lat is not None and lng is not None else None)
            d = _haversine_m(ref["latitude"], ref["longitude"], r["latitude"], r["longitude"]) if ref else None
            if src and not qq and r["id"] not in linked and d > 1500:
                continue
            out.append({"id": r["id"], "name": r["name"], "type": r["type"], "cluster": r["cluster"], "area": r["area"], "lat": r["latitude"], "lng": r["longitude"],
                        "distance_m": round(d) if d is not None else None, "cables": linked.get(r["id"], [])[:3]})
        if src:
            out.sort(key=lambda x: (0 if x["cables"] else 1, x["distance_m"]))
        elif lat is not None and lng is not None:
            out.sort(key=lambda x: x["distance_m"])      # titik awal: aset terdekat dari pusat peta
        else:
            out.sort(key=lambda x: (0 if (x["type"] or "").upper() == "POP" else 1, (x["name"] or "").lower()))
        return {"items": out[:limit], "total": len(out)}


@app.post("/api/topofix/puzzle/preview")
def topofix_puzzle_preview(req: PzReq):
    with db() as conn:
        plan, _ = _pz_plan(conn.cursor(), req)
    errs = sum(len(p["errors"]) for p in plan)
    return {"links": plan, "new_cables": sum(1 for p in plan if p["mode"] == "baru"), "errors": errs, "ready": errs == 0}


@app.post("/api/topofix/puzzle/apply")
def topofix_puzzle_apply(req: PzReq):
    """Buat kabel yang belum ada (garis lurus antar aset, tampil di peta & inventory) lalu sambung core sepanjang rantai."""
    if not req.batch:
        raise HTTPException(status_code=400, detail="Kode batch wajib")
    with db() as conn:
        plan, nodes = _pz_plan(conn.cursor(), req)
    bad = [e for p in plan for e in p["errors"]]
    if bad:
        raise HTTPException(status_code=400, detail="; ".join(bad[:3]))
    created, tag = [], f" [TFB#{req.batch}]"
    try:
        for p in plan:
            if p["mode"] != "baru":
                continue
            up, down = nodes[p["from"]["id"]], nodes[p["to"]["id"]]
            c = p["cable"]
            res = create_cable(CableCreate(
                name=c["name"], type=c["type"], status="Active", capacity=c["capacity"], installation=c["installation"],
                coordinates=[[up["longitude"], up["latitude"]], [down["longitude"], down["latitude"]]],
                cluster=up["cluster"] or down["cluster"], area=up["area"] or down["area"], city=up["city"] if "city" in up else None,
                from_node_id=up["id"], to_node_id=down["id"]))
            p["cable"]["id"] = res["id"]
            p["cable"]["name"] = res.get("name") or c["name"]
            created.append(res["id"])
        made_total, results = 0, []
        with db() as conn:
            cur = conn.cursor()
            for p in plan:
                if p.get("done"):
                    continue
                cid = p["cable"]["id"]
                cab = cur.execute("SELECT * FROM cables WHERE id = ?", (cid,)).fetchone()
                before = cur.execute("SELECT COALESCE(MAX(id), 0) AS m FROM core_connections").fetchone()["m"]
                arq = AutoConnectReq(cores=len(p["cores"]), respect_order=True, reverse=(cab["from_node_id"] != p["from"]["id"]), via_cores=p["cores"],
                                  from_ports=p.get("from_cores"), upstream_cable_id=p.get("upstream_cable_id"), to_ports=p.get("to_ports"))
                cur.execute("SAVEPOINT pz")
                try:
                    out = _ac_plan_and_apply(cur, cid, arq)
                except HTTPException as exc:
                    out = {"ok": False, "reason": str(exc.detail)}
                if not out.get("ok"):
                    cur.execute("ROLLBACK TO pz")
                    raise HTTPException(status_code=400, detail=f"{p['from']['name']} → {p['to']['name']}: {out.get('reason') or 'sambungan gagal'}"
                                        + (" (kabel hulu lebih dari satu; pilih dulu di Sambung Core)" if out.get("needs_choice") else ""))
                cur.execute("UPDATE core_connections SET notes = TRIM(COALESCE(notes, '') || ?) WHERE id > ?", (tag, before))
                cur.execute("RELEASE pz")
                made = cur.execute("SELECT COUNT(*) AS n FROM core_connections WHERE id > ?", (before,)).fetchone()["n"]
                made_total += made
                results.append({"index": p["index"], "cable_id": cid, "name": p["cable"]["name"], "links": made, "new": p["mode"] == "baru"})
            _audit(cur, "CONNECT", "BULK", None, f"puzzle {req.batch}",
                   f"Rangkai puzzle (batch {req.batch}): {sum(1 for x in plan if not x.get('done'))} sambungan, {len(created)} kabel baru, {made_total} sambungan core")
    except Exception:
        for cid in created:                      # gagal di tengah: batalkan kabel yang baru dibuat
            try:
                delete_cable(cid)
            except Exception:
                pass
        raise
    return {"message": f"Rantai tersambung: {sum(1 for x in plan if not x.get('done'))} langkah, {len(created)} kabel baru dibuat, {made_total} sambungan core",
            "created_cables": created, "links": made_total, "results": results}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
