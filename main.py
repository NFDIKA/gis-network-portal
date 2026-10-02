from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Optional
from typing import Optional
import sqlite3
import json

app = FastAPI(title="ISP WebGIS Prototype API")

# Allow CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def init_db():
    """Inisialisasi database dan migrasi otomatis kolom yang kurang."""
    conn = sqlite3.connect('gis_network.db')
    cursor = conn.cursor()
    
    # 1. Tabel Nodes
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            type TEXT,
            status TEXT,
            latitude REAL,
            longitude REAL,
            cluster TEXT,
            area TEXT,
            city TEXT,
            capacity TEXT,
            spec_data TEXT
        )
    ''')
    
    # 2. Tabel Cables
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS cables (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            type TEXT,
            status TEXT,
            geojson_geometry TEXT,
            cluster TEXT,
            area TEXT,
            city TEXT,
            capacity TEXT,
            core_data TEXT
        )
    ''')

    # 3. Tabel Core Connections
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS core_connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_asset_type TEXT,
            from_asset_id INTEGER,
            from_port_core TEXT,
            to_asset_type TEXT,
            to_asset_id INTEGER,
            to_port_core TEXT,
            via_cable_id INTEGER,
            status TEXT,
            notes TEXT
        )
    ''')

    # 4. Tabel Incidents (Lengkap)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_number TEXT,
            title TEXT,
            severity TEXT,
            incident_type TEXT,
            status TEXT DEFAULT 'Open',
            description TEXT,
            latitude REAL,
            longitude REAL,
            cluster TEXT,
            area TEXT,
            city TEXT,
            linked_cable_id INTEGER,
            linked_node_id INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            resolved_at DATETIME
        )
    ''')
    
    # --- AUTO MIGRATION (Penambahan Kolom Otomatis untuk DB Lama) ---
    cursor.execute("PRAGMA table_info(incidents)")
    columns = [row[1] for row in cursor.fetchall()]
    
    if "linked_cable_id" not in columns:
        cursor.execute("ALTER TABLE incidents ADD COLUMN linked_cable_id INTEGER")
        print("[MIGRATION] Kolom 'linked_cable_id' berhasil ditambahkan.")

    if "linked_node_id" not in columns:
        cursor.execute("ALTER TABLE incidents ADD COLUMN linked_node_id INTEGER")
        print("[MIGRATION] Kolom 'linked_node_id' berhasil ditambahkan.")

    if "resolved_at" not in columns:
        cursor.execute("ALTER TABLE incidents ADD COLUMN resolved_at DATETIME")
        print("[MIGRATION] Kolom 'resolved_at' berhasil ditambahkan.")

    conn.commit()
    conn.close()
    print("[INFO] Database gis_network.db dan seluruh tabel berhasil terinisialisasi.")
@app.on_event("startup")
def startup_event():
    init_db()



def get_db_connection():
    conn = sqlite3.connect('gis_network.db')
    conn.row_factory = sqlite3.Row
    return conn


# --- MODEL DATA (PYDANTIC) ---
class NodeCreate(BaseModel):
    name: str
    type: str
    status: str
    latitude: float
    longitude: float
    cluster: Optional[str] = "EKO"
    area: Optional[str] = "BANJARMASIN"
    city: Optional[str] = "Kota Banjarmasin"
    capacity: Optional[str] = "8 Port"
    spec_data: Optional[str] = "{}"

class CableCreate(BaseModel):
    name: str
    type: str  # Misal: 'Drop Cable', 'Feeder', 'Distribution', 'Backbone'
    status: str
    coordinates: List[List[float]]
    cluster: Optional[str] = "EKO"
    area: Optional[str] = "BANJARMASIN"
    city: Optional[str] = "Kota Banjarmasin"
    capacity: Optional[str] = "2C"  # Default kapasitas awal (pilihan: '2C', '4C', '8C', '12C', '24C', '48C', '96C')
    core_data: Optional[str] = "{}"

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

class CableUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    status: Optional[str] = None
    coordinates: Optional[List[List[float]]] = None
    cluster: Optional[str] = None
    area: Optional[str] = None
    city: Optional[str] = None
    capacity: Optional[str] = None

class CoreConnectionSchema(BaseModel):
    from_asset_type: str
    from_asset_id: int
    from_port_core: str
    to_asset_type: str
    to_asset_id: int
    to_port_core: str
    via_cable_id: Optional[int] = None  # Tambahkan field ini
    status: Optional[str] = "Connected"
    notes: Optional[str] = ""


# 2. Model Pydantic khusus Incidents
# Model Pydantic di main.py
class IncidentCreate(BaseModel):
    ticket_number: str
    title: str
    severity: str
    incident_type: str
    status: Optional[str] = "Open"
    description: Optional[str] = ""
    latitude: float
    longitude: float
    cluster: Optional[str] = "EKO"
    area: Optional[str] = "BANJARMASIN"
    city: Optional[str] = "Kota Banjarmasin"
    linked_cable_id: Optional[int] = None
    linked_node_id: Optional[int] = None

class IncidentStatusUpdate(BaseModel):
    status: str

# --- ROUTE API ---

# 1. GET ALL NODES
@app.get("/api/nodes")
def get_nodes():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        nodes = cursor.execute("SELECT * FROM nodes").fetchall()
        
        features = []
        for node in nodes:
            node_dict = dict(node)
            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [node_dict["longitude"], node_dict["latitude"]]
                },
                "properties": {
                    "id": node_dict["id"],
                    "name": node_dict["name"],
                    "type": node_dict["type"],
                    "status": node_dict["status"],
                    "cluster": node_dict.get("cluster") or "EKO",
                    "area": node_dict.get("area") or "BANJARMASIN",
                    "city": node_dict.get("city") or "Kota Banjarmasin",
                    "capacity": node_dict.get("capacity") or "8 Port",
                    "spec_data": node_dict.get("spec_data") or "{}"
                }
            })
        return {"type": "FeatureCollection", "features": features}
    except Exception as e:
        print(f"[ERROR] Node Fetch Failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()

# 2. CREATE NODE (POST)
@app.post("/api/nodes")
def create_node(node: NodeCreate):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO nodes (name, type, status, latitude, longitude, cluster, area, city, capacity, spec_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            node.name, node.type, node.status, 
            float(node.latitude), float(node.longitude),
            node.cluster, node.area, node.city, node.capacity, node.spec_data
        ))
        conn.commit()
        node_id = cursor.lastrowid
        print(f"[SUCCESS] Node '{node.name}' berhasil disimpan dengan ID: {node_id}")
        return {"message": "Node berhasil ditambahkan", "id": node_id}
    except Exception as e:
        print(f"[ERROR] Node Insert Failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


# 3. GET ALL CABLES
@app.get("/api/cables")
def get_cables():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cables = cursor.execute("SELECT * FROM cables").fetchall()
        
        features = []
        for cable in cables:
            cable_dict = dict(cable)
            
            geom_raw = cable_dict.get("geojson_geometry") or cable_dict.get("coordinates")
            if isinstance(geom_raw, str):
                geom = json.loads(geom_raw)
            else:
                geom = geom_raw

            if isinstance(geom, list):
                geom = {"type": "LineString", "coordinates": geom}

            features.append({
                "type": "Feature",
                "geometry": geom,
                "properties": {
                    "id": cable_dict["id"],
                    "name": cable_dict["name"],
                    "type": cable_dict["type"],
                    "status": cable_dict["status"],
                    "cluster": cable_dict.get("cluster") or "EKO",
                    "area": cable_dict.get("area") or "BANJARMASIN",
                    "city": cable_dict.get("city") or "Kota Banjarmasin",
                    "capacity": cable_dict.get("capacity") or "24C",
                    "core_data": cable_dict.get("core_data") or "{}"
                }
            })
        return {"type": "FeatureCollection", "features": features}
    except Exception as e:
        print(f"[ERROR] Cable Fetch Failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


# 4. CREATE CABLE (POST)
# CREATE CABLE (POST)
@app.post("/api/cables")
def create_cable(cable: CableCreate):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        geojson_geom = {
            "type": "LineString",
            "coordinates": cable.coordinates
        }
        
        cursor.execute('''
            INSERT INTO cables (name, type, status, geojson_geometry, cluster, area, city, capacity, core_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            cable.name, cable.type, cable.status, 
            json.dumps(geojson_geom),
            cable.cluster, cable.area, cable.city, cable.capacity, cable.core_data
        ))
        conn.commit()
        cable_id = cursor.lastrowid
        print(f"[SUCCESS] Cable '{cable.name}' ({cable.capacity}) berhasil disimpan dengan ID: {cable_id}")
        return {"message": "Kabel berhasil ditambahkan", "id": cable_id}
    except Exception as e:
        print(f"[ERROR] Cable Insert Failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()

# 5. UPDATE STATUS NODE
@app.put("/api/nodes/{node_id}/status")
def update_node_status(node_id: int, payload: StatusUpdate):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE nodes SET status = ? WHERE id = ?", (payload.status, node_id))
        conn.commit()
        print(f"[SUCCESS] Status Node ID {node_id} diperbarui menjadi '{payload.status}'")
        return {"message": "Status node berhasil diperbarui"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


# 6. UPDATE STATUS KABEL
@app.put("/api/cables/{cable_id}/status")
def update_cable_status(cable_id: int, payload: StatusUpdate):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE cables SET status = ? WHERE id = ?", (payload.status, cable_id))
        conn.commit()
        print(f"[SUCCESS] Status Kabel ID {cable_id} diperbarui menjadi '{payload.status}'")
        return {"message": "Status kabel berhasil diperbarui"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


# 7. SUMMARY STATISTIK DASHBOARD
@app.get("/api/dashboard/summary")
def get_summary():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        total_nodes = cursor.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        total_odp = cursor.execute("SELECT COUNT(*) FROM nodes WHERE type = 'ODP'").fetchone()[0]
        
        # Hitung incident dari tabel incidents (yang belum Resolved) + nodes/cables yang Cut/Broken
        incidents_open = cursor.execute("SELECT COUNT(*) FROM incidents WHERE status != 'Resolved'").fetchone()[0]
        nodes_broken = cursor.execute("SELECT COUNT(*) FROM nodes WHERE status = 'Cut/Broken'").fetchone()[0]
        cables_broken = cursor.execute("SELECT COUNT(*) FROM cables WHERE status = 'Cut/Broken'").fetchone()[0]
        
        total_incidents = incidents_open + nodes_broken + cables_broken
        total_cables = cursor.execute("SELECT COUNT(*) FROM cables").fetchone()[0]
        
        return {
            "total_nodes": total_nodes,
            "total_odp": total_odp,
            "total_incidents": total_incidents,
            "total_cables": total_cables
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()

# 8. UPDATE NODE (PUT)
@app.put("/api/nodes/{node_id}")
def update_node(node_id: int, payload: NodeUpdate):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        existing = cursor.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Node tidak ditemukan")

        existing_dict = dict(existing)
        name = payload.name if payload.name is not None else existing_dict["name"]
        type_ = payload.type if payload.type is not None else existing_dict["type"]
        status = payload.status if payload.status is not None else existing_dict["status"]
        lat = payload.latitude if payload.latitude is not None else existing_dict["latitude"]
        lng = payload.longitude if payload.longitude is not None else existing_dict["longitude"]
        cluster = payload.cluster if payload.cluster is not None else existing_dict.get("cluster")
        area = payload.area if payload.area is not None else existing_dict.get("area")
        city = payload.city if payload.city is not None else existing_dict.get("city")
        capacity = payload.capacity if payload.capacity is not None else existing_dict.get("capacity")

        cursor.execute('''
            UPDATE nodes 
            SET name=?, type=?, status=?, latitude=?, longitude=?, cluster=?, area=?, city=?, capacity=?
            WHERE id=?
        ''', (name, type_, status, float(lat), float(lng), cluster, area, city, capacity, node_id))
        
        conn.commit()
        print(f"[SUCCESS] Node ID {node_id} berhasil di-update")
        return {"message": "Node berhasil diperbarui"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Node Update Failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


# 9. DELETE NODE
@app.delete("/api/nodes/{node_id}")
def delete_node(node_id: int):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
        conn.commit()
        print(f"[SUCCESS] Node ID {node_id} berhasil dihapus")
        return {"message": "Node berhasil dihapus"}
    except Exception as e:
        print(f"[ERROR] Node Delete Failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


# 10. UPDATE CABLE (PUT)
@app.put("/api/cables/{cable_id}")
def update_cable(cable_id: int, payload: CableUpdate):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        existing = cursor.execute("SELECT * FROM cables WHERE id = ?", (cable_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Kabel tidak ditemukan")

        existing_dict = dict(existing)
        name = payload.name if payload.name is not None else existing_dict["name"]
        type_ = payload.type if payload.type is not None else existing_dict["type"]
        status = payload.status if payload.status is not None else existing_dict["status"]
        cluster = payload.cluster if payload.cluster is not None else existing_dict.get("cluster")
        area = payload.area if payload.area is not None else existing_dict.get("area")
        city = payload.city if payload.city is not None else existing_dict.get("city")
        capacity = payload.capacity if payload.capacity is not None else existing_dict.get("capacity")

        if payload.coordinates is not None:
            geojson_geom = json.dumps({"type": "LineString", "coordinates": payload.coordinates})
        else:
            geojson_geom = existing_dict["geojson_geometry"]

        cursor.execute('''
            UPDATE cables 
            SET name=?, type=?, status=?, geojson_geometry=?, cluster=?, area=?, city=?, capacity=?
            WHERE id=?
        ''', (name, type_, status, geojson_geom, cluster, area, city, capacity, cable_id))

        conn.commit()
        print(f"[SUCCESS] Cable ID {cable_id} berhasil di-update")
        return {"message": "Kabel berhasil diperbarui"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Cable Update Failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


# 11. DELETE CABLE
@app.delete("/api/cables/{cable_id}")
def delete_cable(cable_id: int):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM cables WHERE id = ?", (cable_id,))
        conn.commit()
        print(f"[SUCCESS] Cable ID {cable_id} berhasil dihapus")
        return {"message": "Kabel berhasil dihapus"}
    except Exception as e:
        print(f"[ERROR] Cable Delete Failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


# 12. GET CORE CONNECTIONS
@app.get("/api/connections")
def get_connections(asset_type: Optional[str] = None, asset_id: Optional[int] = None):
    """Mengambil daftar koneksi core/port. Bisa difilter berdasarkan ID/tipe aset."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        if asset_type and asset_id is not None:
            query = """
                SELECT * FROM core_connections 
                WHERE (from_asset_type = ? AND from_asset_id = ?) 
                   OR (to_asset_type = ? AND to_asset_id = ?)
            """
            rows = cursor.execute(query, (str(asset_type), int(asset_id), str(asset_type), int(asset_id))).fetchall()
        else:
            rows = cursor.execute("SELECT * FROM core_connections").fetchall()

        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[ERROR] Connection Fetch Failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


# 13. CREATE CORE CONNECTION
@app.post("/api/connections")
def create_connection(payload: CoreConnectionSchema):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO core_connections 
            (from_asset_type, from_asset_id, from_port_core, to_asset_type, to_asset_id, to_port_core, via_cable_id, status, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            payload.from_asset_type,
            payload.from_asset_id,
            payload.from_port_core,
            payload.to_asset_type,
            payload.to_asset_id,
            payload.to_port_core,
            payload.via_cable_id,
            payload.status,
            payload.notes
        ))

        conn.commit()
        return {"message": "Sambungan core berhasil disimpan"}
    except Exception as e:
        print(f"[ERROR] Connection Create Failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()

# 14. DELETE CORE CONNECTION
@app.delete("/api/connections/{connection_id}")
def delete_connection(connection_id: int):
    """Putus/hapus sambungan core berdasarkan ID."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM core_connections WHERE id = ?", (connection_id,))
        conn.commit()
        return {"message": "Sambungan core berhasil dihapus"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()

# 15. GET NODE PORT SUMMARY
@app.get("/api/nodes/{node_id}/port-summary")
def get_node_port_summary(node_id: int):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Ambil total koneksi aktif yang terhubung ke node ini
        connected_count = cursor.execute("""
            SELECT COUNT(*) FROM core_connections 
            WHERE (from_asset_type = 'NODE' AND from_asset_id = ?)
               OR (to_asset_type = 'NODE' AND to_asset_id = ?)
        """, (node_id, node_id)).fetchone()[0]

        return {"node_id": node_id, "used_ports": connected_count}
    finally:
        if conn:
            conn.close()

# 16. INCIDENTS MANAGEMENT

@app.get("/api/incidents")
def get_incidents():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        incidents = cursor.execute("SELECT * FROM incidents").fetchall()
        
        features = []
        for inc in incidents:
            item = dict(inc)
            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [item["longitude"], item["latitude"]]
                },
                "properties": item
            })
        return {"type": "FeatureCollection", "features": features}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


@app.post("/api/incidents")
def create_incident(inc: IncidentCreate):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 1. Pastikan tabel incidents memiliki struktur lengkap (termasuk link aset)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_number TEXT,
                title TEXT,
                severity TEXT,
                incident_type TEXT,
                status TEXT DEFAULT 'Open',
                description TEXT,
                latitude REAL,
                longitude REAL,
                cluster TEXT,
                area TEXT,
                city TEXT,
                linked_cable_id INTEGER,
                linked_node_id INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                resolved_at DATETIME
            )
        ''')
        
        # 2. Insert data insiden beserta linked_cable_id & linked_node_id
        cursor.execute('''
            INSERT INTO incidents (
                ticket_number, title, severity, incident_type, status, 
                description, latitude, longitude, cluster, area, city, 
                linked_cable_id, linked_node_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            inc.ticket_number, 
            inc.title, 
            inc.severity, 
            inc.incident_type,
            inc.status or "Open", 
            inc.description or "", 
            inc.latitude, 
            inc.longitude,
            inc.cluster or "EKO", 
            inc.area or "BANJARMASIN", 
            inc.city or "Kota Banjarmasin",
            getattr(inc, 'linked_cable_id', None),
            getattr(inc, 'linked_node_id', None)
        ))
        
        conn.commit()
        last_id = cursor.lastrowid
        return {"message": "Incident berhasil dicatat", "id": last_id}
        
    except Exception as e:
        print(f"[ERROR] Insert Incident Failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


def get_downstream_assets(cursor, start_cable_id):
    """Mencari hanya kabel & node yang berada di downstream dari kabel insiden."""
    if not start_cable_id:
        return set(), set()

    affected_cables = {start_cable_id}
    affected_nodes = set()
    queue = [start_cable_id]
    
    while queue:
        curr_cable = queue.pop(0)
        # Ambil koneksi downstream HANYA yang terhubung dari kabel ini (via_cable_id)
        rows = cursor.execute("""
            SELECT to_asset_type, to_asset_id 
            FROM core_connections 
            WHERE via_cable_id = ?
        """, (curr_cable,)).fetchall()
        
        for r in rows:
            a_type, a_id = r["to_asset_type"], r["to_asset_id"]
            if a_type == "NODE":
                affected_nodes.add(a_id)
            elif a_type == "CABLE" and a_id not in affected_cables:
                affected_cables.add(a_id)
                queue.append(a_id)
                
    return affected_cables, affected_nodes


@app.post("/api/incidents/analyze-impact")
def analyze_incident_impact(incident_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    inc = cursor.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
    if not inc or not inc["linked_cable_id"]:
        conn.close()
        return {"affected_nodes": [], "affected_cables": []}

    # Cari aset downstream
    aff_cables, aff_nodes = get_downstream_assets(cursor, inc["linked_cable_id"])
    
    # Set status aset downstream menjadi 'Cut/Broken'
    if aff_nodes:
        cursor.execute(f"UPDATE nodes SET status = 'Cut/Broken' WHERE id IN ({','.join(map(str, aff_nodes))})")
    if aff_cables:
        cursor.execute(f"UPDATE cables SET status = 'Cut/Broken' WHERE id IN ({','.join(map(str, aff_cables))})")
        
    conn.commit()
    conn.close()
    return {"affected_nodes": list(aff_nodes), "affected_cables": list(aff_cables)}


def recalculate_all_asset_statuses(cursor):
    """
    Fungsi pemulihan: Mereset semua aset menjadi 'Active',
    lalu menerapkan dampak dari incident yang MASIH AKTIF saja.
    """
    # 1. Reset seluruh node & cable menjadi Active
    cursor.execute("UPDATE nodes SET status = 'Active'")
    cursor.execute("UPDATE cables SET status = 'Active'")
    
    # 2. Ambil semua incident yang belum 'Resolved'
    active_incidents = cursor.execute(
        "SELECT linked_cable_id FROM incidents WHERE status != 'Resolved' AND linked_cable_id IS NOT NULL"
    ).fetchall()
    
    # 3. Terapkan ulang dampak incident yang masih aktif
    for inc in active_incidents:
        aff_cables, aff_nodes = get_downstream_assets(cursor, inc["linked_cable_id"])
        if aff_nodes:
            cursor.execute(f"UPDATE nodes SET status = 'Cut/Broken' WHERE id IN ({','.join(map(str, aff_nodes))})")
        if aff_cables:
            cursor.execute(f"UPDATE cables SET status = 'Cut/Broken' WHERE id IN ({','.join(map(str, aff_cables))})")


@app.put("/api/incidents/{incident_id}/status")
def update_incident_status(incident_id: int, payload: IncidentStatusUpdate):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Update status incident dan waktu resolved jika selesai
    if payload.status == "Resolved":
        cursor.execute("UPDATE incidents SET status = ?, resolved_at = CURRENT_TIMESTAMP WHERE id = ?", (payload.status, incident_id))
    else:
        cursor.execute("UPDATE incidents SET status = ? WHERE id = ?", (payload.status, incident_id))
    
    # Hitung ulang status aset secara menyeluruh
    recalculate_all_asset_statuses(cursor)
    
    conn.commit()
    conn.close()
    return {"message": "Status incident diperbarui dan status aset dipulihkan/disesuaikan"}


@app.delete("/api/incidents/{incident_id}")
def delete_incident(incident_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("DELETE FROM incidents WHERE id = ?", (incident_id,))
    
    # Hitung ulang status aset secara menyeluruh setelah incident dihapus
    recalculate_all_asset_statuses(cursor)
    
    conn.commit()
    conn.close()
    return {"message": "Incident berhasil dihapus dan status aset dipulihkan"}

# --- MOUNT STATIC FILES (HARUS PALING BAWAH) ---
app.mount("/", StaticFiles(directory="static", html=True), name="static")