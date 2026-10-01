from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Optional
import sqlite3
import json

app = FastAPI(title="ISP WebGIS Prototype API - Full CRUD")

# Allow CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db_connection():
    conn = sqlite3.connect('gis_network.db')
    conn.row_factory = sqlite3.Row
    return conn

# Inisialisasi Tabel Database jika belum ada
def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            status TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS cables (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            status TEXT NOT NULL,
            geojson_geometry TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# --- MODEL DATA (PYDANTIC) ---
class NodeCreate(BaseModel):
    name: str
    type: str
    status: str
    latitude: float
    longitude: float

class NodeUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    status: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None

class CableCreate(BaseModel):
    name: str
    type: str
    status: str
    coordinates: List[List[float]]

class CableUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    status: Optional[str] = None
    coordinates: Optional[List[List[float]]] = None

class StatusUpdate(BaseModel):
    status: str  # 'Active', 'Maintenance', 'Cut/Broken'


# --- ROUTE API NODES (MARKER) ---

# 1. READ ALL NODES
@app.get("/api/nodes")
def get_nodes():
    conn = get_db_connection()
    cursor = conn.cursor()
    nodes = cursor.execute("SELECT * FROM nodes").fetchall()
    conn.close()
    
    features = []
    for node in nodes:
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [node["longitude"], node["latitude"]]
            },
            "properties": {
                "id": node["id"],
                "name": node["name"],
                "type": node["type"],
                "status": node["status"]
            }
        })
    return {"type": "FeatureCollection", "features": features}

# 2. CREATE NODE
@app.post("/api/nodes")
def create_node(node: NodeCreate):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO nodes (name, type, status, latitude, longitude)
            VALUES (?, ?, ?, ?, ?)
        ''', (node.name, node.type, node.status, float(node.latitude), float(node.longitude)))
        conn.commit()
        node_id = cursor.lastrowid
        conn.close()
        print(f"[SUCCESS] Node '{node.name}' berhasil disimpan dengan ID: {node_id}")
        return {"message": "Node berhasil ditambahkan", "id": node_id}
    except Exception as e:
        print(f"[ERROR] Node Insert Failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# 3. UPDATE NODE STATUS
@app.put("/api/nodes/{node_id}/status")
def update_node_status(node_id: int, payload: StatusUpdate):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE nodes SET status = ? WHERE id = ?", (payload.status, node_id))
        conn.commit()
        conn.close()
        print(f"[SUCCESS] Status Node ID {node_id} diperbarui menjadi '{payload.status}'")
        return {"message": "Status node berhasil diperbarui"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# 4. UPDATE NODE FULL PROPERTIES
@app.put("/api/nodes/{node_id}")
def update_node(node_id: int, payload: NodeUpdate):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE nodes 
            SET name = COALESCE(?, name),
                type = COALESCE(?, type),
                status = COALESCE(?, status),
                latitude = COALESCE(?, latitude),
                longitude = COALESCE(?, longitude)
            WHERE id = ?
        ''', (payload.name, payload.type, payload.status, payload.latitude, payload.longitude, node_id))
        conn.commit()
        conn.close()
        print(f"[SUCCESS] Node ID {node_id} berhasil diperbarui")
        return {"message": "Node berhasil diperbarui"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# 5. DELETE NODE
@app.delete("/api/nodes/{node_id}")
def delete_node(node_id: int):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
        conn.commit()
        conn.close()
        print(f"[SUCCESS] Node ID {node_id} berhasil dihapus")
        return {"message": "Node berhasil dihapus"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- ROUTE API CABLES (POLYLINE) ---

# 6. READ ALL CABLES
@app.get("/api/cables")
def get_cables():
    conn = get_db_connection()
    cursor = conn.cursor()
    cables = cursor.execute("SELECT * FROM cables").fetchall()
    conn.close()
    
    features = []
    for cable in cables:
        geom = json.loads(cable["geojson_geometry"])
        features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "id": cable["id"],
                "name": cable["name"],
                "type": cable["type"],
                "status": cable["status"]
            }
        })
    return {"type": "FeatureCollection", "features": features}

# 7. CREATE CABLE
@app.post("/api/cables")
def create_cable(cable: CableCreate):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        geojson_geom = {
            "type": "LineString",
            "coordinates": cable.coordinates
        }
        
        cursor.execute('''
            INSERT INTO cables (name, type, status, geojson_geometry)
            VALUES (?, ?, ?, ?)
        ''', (cable.name, cable.type, cable.status, json.dumps(geojson_geom)))
        conn.commit()
        cable_id = cursor.lastrowid
        conn.close()
        print(f"[SUCCESS] Cable '{cable.name}' berhasil disimpan dengan ID: {cable_id}")
        return {"message": "Kabel berhasil ditambahkan", "id": cable_id}
    except Exception as e:
        print(f"[ERROR] Cable Insert Failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# 8. UPDATE CABLE STATUS
@app.put("/api/cables/{cable_id}/status")
def update_cable_status(cable_id: int, payload: StatusUpdate):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE cables SET status = ? WHERE id = ?", (payload.status, cable_id))
        conn.commit()
        conn.close()
        print(f"[SUCCESS] Status Kabel ID {cable_id} diperbarui menjadi '{payload.status}'")
        return {"message": "Status kabel berhasil diperbarui"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# 9. UPDATE CABLE FULL PROPERTIES
@app.put("/api/cables/{cable_id}")
def update_cable(cable_id: int, payload: CableUpdate):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        geojson_str = None
        if payload.coordinates:
            geojson_geom = {
                "type": "LineString",
                "coordinates": payload.coordinates
            }
            geojson_str = json.dumps(geojson_geom)

        cursor.execute('''
            UPDATE cables 
            SET name = COALESCE(?, name),
                type = COALESCE(?, type),
                status = COALESCE(?, status),
                geojson_geometry = COALESCE(?, geojson_geometry)
            WHERE id = ?
        ''', (payload.name, payload.type, payload.status, geojson_str, cable_id))
        conn.commit()
        conn.close()
        print(f"[SUCCESS] Kabel ID {cable_id} berhasil diperbarui")
        return {"message": "Kabel berhasil diperbarui"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# 10. DELETE CABLE
@app.delete("/api/cables/{cable_id}")
def delete_cable(cable_id: int):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM cables WHERE id = ?", (cable_id,))
        conn.commit()
        conn.close()
        print(f"[SUCCESS] Kabel ID {cable_id} berhasil dihapus")
        return {"message": "Kabel berhasil dihapus"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- ROUTE DASHBOARD SUMMARY ---

# 11. GET SUMMARY
@app.get("/api/dashboard/summary")
def get_summary():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    total_nodes = cursor.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    total_odp = cursor.execute("SELECT COUNT(*) FROM nodes WHERE type = 'ODP'").fetchone()[0]
    node_incidents = cursor.execute("SELECT COUNT(*) FROM nodes WHERE status = 'Cut/Broken' OR type = 'INCIDENT'").fetchone()[0]
    cable_incidents = cursor.execute("SELECT COUNT(*) FROM cables WHERE status = 'Cut/Broken'").fetchone()[0]
    total_cables = cursor.execute("SELECT COUNT(*) FROM cables").fetchone()[0]
    
    conn.close()
    return {
        "total_nodes": total_nodes,
        "total_odp": total_odp,
        "total_incidents": node_incidents + cable_incidents,
        "total_cables": total_cables
    }


# --- MOUNT STATIC FILES (HARUS PALING BAWAH) ---
app.mount("/", StaticFiles(directory="static", html=True), name="static")