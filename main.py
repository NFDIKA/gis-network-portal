from collections import deque
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import List, Optional
import json
import math
import re
import sqlite3

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

    # --- AUTO MIGRATION UNTUK DB EXISTING ---
    def add_column_if_missing(table, column, col_type):
        cursor.execute(f"PRAGMA table_info({table})")
        cols = [r[1] for r in cursor.fetchall()]
        if column not in cols:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            print(f"[MIGRATION] Added {column} to {table}")

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
        if t == "TIANG":
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
        (incident_id, event_type, message, actor or "system",
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
                               core_data, parent_cable_id, from_node_id, to_node_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (f"{cab['name']}-seg2", cab["type"], cab["status"], geom(coords_b), cab["cluster"], cab["area"],
         cab["city"], cab["capacity"], cab["core_data"], cable_id, node_id, cab["to_node_id"]))
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
                  "city": "city", "capacity": "capacity", "status": "status", "category": "category"}


@app.get("/api/inventory")
def get_inventory(q: str = "", cluster: str = "ALL", type: str = "ALL", status: str = "ALL",
                  sort: str = "name", order: str = "asc", page: int = 1, page_size: int = 25):
    """Daftar aset (node + kabel) terpaginasi. type='CABLE' = semua kabel; selain itu mencocokkan tipe node/kabel."""
    if sort not in INVENTORY_SORT:
        raise HTTPException(status_code=400, detail=f"Kolom sort tidak valid: {sort}")
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

    base = ("SELECT 'NODE' AS category, id, name, type, status, cluster, area, city, capacity FROM nodes "
            "UNION ALL "
            "SELECT 'CABLE' AS category, id, name, type, status, cluster, area, city, capacity FROM cables")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    with db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM ({base}){where_sql}", params).fetchone()[0]
        pages = max(1, -(-total // page_size))
        page = max(1, min(int(page), pages))  # halaman di luar jangkauan (mis. setelah hapus) -> dirapikan
        rows = conn.execute(
            f"SELECT * FROM ({base}){where_sql} "
            f"ORDER BY {INVENTORY_SORT[sort]} COLLATE NOCASE {order_sql}, category, id "
            f"LIMIT ? OFFSET ?", (*params, page_size, (page - 1) * page_size)).fetchall()
    return {"items": [dict(r) for r in rows], "total": total, "page": page,
            "page_size": page_size, "pages": pages}


@app.get("/api/nodes")
def get_nodes():
    with db() as conn:
        nodes = conn.execute("SELECT * FROM nodes").fetchall()
        # Closure/joint hasil perbaikan lapangan: jenis perbaikan terakhir + tiketnya
        repair_by_node = {}
        for r in conn.execute(
                """SELECT r.node_id, r.kind, r.incident_id, i.ticket_number
                   FROM incident_repairs r JOIN incidents i ON i.id = r.incident_id
                   WHERE r.node_id IS NOT NULL ORDER BY r.id""").fetchall():
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
                                core_data, parent_cable_id, from_node_id, to_node_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            cable.name, cable.type, cable.status, json.dumps(geojson_geom),
            cable.cluster, cable.area, cable.city, cable.capacity, cable.core_data,
            cable.parent_cable_id, cable.from_node_id, cable.to_node_id,
        ))
        cable_id = cursor.lastrowid
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

    return {
        "total_nodes": total_nodes,
        "total_odp": total_odp,
        # Tiket aktif + laporan manual; aset terdampak satu tiket tidak dihitung ganda
        "total_incidents": tickets_open + manual_nodes + manual_cables,
        "affected_assets": broken_nodes + broken_cables,
        "total_cables": total_cables,
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

        sets, values = _build_update(
            "nodes", node_id, data,
            ["name", "type", "status", "latitude", "longitude", "cluster", "area", "city", "capacity", "spec_data"],
            ["parent_node_id", "upstream_cable_id"],
        )
        if sets:
            cursor.execute(f"UPDATE nodes SET {', '.join(sets)} WHERE id = ?", (*values, node_id))
        if "latitude" in data or "longitude" in data:
            _snap_cables_to_node(cursor, node_id)
    print(f"[SUCCESS] Node ID {node_id} berhasil di-update")
    return {"message": "Node berhasil diperbarui"}


def _snap_cables_to_node(cursor, node_id: int):
    """Node dipindah -> ujung kabel yang terhubung (from/to) ikut pindah supaya jalur tetap tersambung."""
    n = cursor.execute("SELECT latitude, longitude FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if not n or n["latitude"] is None or n["longitude"] is None:
        return
    point = [n["longitude"], n["latitude"]]
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


# 9. DELETE NODE
@app.delete("/api/nodes/{node_id}")
def delete_node(node_id: int):
    with db() as conn:
        cursor = conn.cursor()
        if not _exists(cursor, "nodes", node_id):
            raise HTTPException(status_code=404, detail="Node tidak ditemukan")

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

        sets, values = _build_update(
            "cables", cable_id, data,
            ["name", "type", "status", "cluster", "area", "city", "capacity", "core_data"],
            ["parent_cable_id", "from_node_id", "to_node_id"],
        )
        if data.get("coordinates") is not None:
            _check_coordinates(data["coordinates"])
            sets.append("geojson_geometry = ?")
            values.append(json.dumps({"type": "LineString", "coordinates": data["coordinates"]}))
        if sets:
            cursor.execute(f"UPDATE cables SET {', '.join(sets)} WHERE id = ?", (*values, cable_id))
    print(f"[SUCCESS] Cable ID {cable_id} berhasil di-update")
    return {"message": "Kabel berhasil diperbarui"}


# 11. DELETE CABLE
@app.delete("/api/cables/{cable_id}")
def delete_cable(cable_id: int):
    with db() as conn:
        cursor = conn.cursor()
        if not _exists(cursor, "cables", cable_id):
            raise HTTPException(status_code=404, detail="Kabel tidak ditemukan")

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
    return {"message": "Sambungan core berhasil disimpan", "id": new_id}


# 14. DELETE CORE CONNECTION
@app.delete("/api/connections/{connection_id}")
def delete_connection(connection_id: int):
    """Putus/hapus sambungan core berdasarkan ID."""
    with db() as conn:
        cur = conn.execute("DELETE FROM core_connections WHERE id = ?", (connection_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Sambungan tidak ditemukan")
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
                "SELECT incident_id, node_id FROM incident_repairs WHERE node_id IS NOT NULL ORDER BY id").fetchall():
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
        repairs = [_stamp(dict(r), "created_at") for r in cursor.execute(
            "SELECT * FROM incident_repairs WHERE incident_id = ? ORDER BY id", (incident_id,)).fetchall()]
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
                                             position_m, technician, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (incident_id, kind, action, result.get("node_id"), result.get("cable_a_id"),
             result.get("cable_b_id"), result.get("position_m"), payload.technician, payload.notes or ""))
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
        cursor.execute("DELETE FROM incidents WHERE id = ?", (incident_id,))
    return {"message": "Incident berhasil dihapus dan status aset dipulihkan"}


# --- MOUNT STATIC FILES (HARUS PALING BAWAH) ---
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")