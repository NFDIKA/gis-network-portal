from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Optional
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
    """Inisialisasi database dan membuat semua tabel otomatis saat startup server."""
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
        total_incidents = cursor.execute("SELECT COUNT(*) FROM nodes WHERE status = 'Cut/Broken' OR type = 'INCIDENT'").fetchone()[0]
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


# --- MOUNT STATIC FILES (HARUS PALING BAWAH) ---
app.mount("/", StaticFiles(directory="static", html=True), name="static")