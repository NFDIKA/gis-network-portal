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
    type: str
    status: str
    coordinates: List[List[float]]
    cluster: Optional[str] = "EKO"
    area: Optional[str] = "BANJARMASIN"
    city: Optional[str] = "Kota Banjarmasin"
    capacity: Optional[str] = "24C"
    core_data: Optional[str] = "{}"

class StatusUpdate(BaseModel):
    status: str  # 'Active', 'Maintenance', 'Cut/Broken'


# --- ROUTE API (WAJIB DIDEKLARASIKAN SEBELUM MOUNT STATIC FILES) ---

# 1. GET ALL NODES
@app.get("/api/nodes")
def get_nodes():
    conn = get_db_connection()
    cursor = conn.cursor()
    nodes = cursor.execute("SELECT * FROM nodes").fetchall()
    conn.close()
    
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


# 2. CREATE NODE (POST)
@app.post("/api/nodes")
def create_node(node: NodeCreate):
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
        conn.close()
        print(f"[SUCCESS] Node '{node.name}' berhasil disimpan dengan ID: {node_id}")
        return {"message": "Node berhasil ditambahkan", "id": node_id}
    except Exception as e:
        print(f"[ERROR] Node Insert Failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# 3. GET ALL CABLES
@app.get("/api/cables")
def get_cables():
    conn = get_db_connection()
    cursor = conn.cursor()
    cables = cursor.execute("SELECT * FROM cables").fetchall()
    conn.close()
    
    features = []
    for cable in cables:
        cable_dict = dict(cable)
        
        # Mendukung baik 'geojson_geometry' maupun 'coordinates'
        geom_raw = cable_dict.get("geojson_geometry") or cable_dict.get("coordinates")
        if isinstance(geom_raw, str):
            geom = json.loads(geom_raw)
        else:
            geom = geom_raw

        # Pastikan format berupa GeoJSON Geometry LineString
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


# 4. CREATE CABLE (POST)
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
            INSERT INTO cables (name, type, status, geojson_geometry, cluster, area, city, capacity, core_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            cable.name, cable.type, cable.status, 
            json.dumps(geojson_geom),
            cable.cluster, cable.area, cable.city, cable.capacity, cable.core_data
        ))
        conn.commit()
        cable_id = cursor.lastrowid
        conn.close()
        print(f"[SUCCESS] Cable '{cable.name}' berhasil disimpan dengan ID: {cable_id}")
        return {"message": "Kabel berhasil ditambahkan", "id": cable_id}
    except Exception as e:
        print(f"[ERROR] Cable Insert Failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# 5. API Endpoint: Update Status Node
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


# 6. API Endpoint: Update Status Kabel
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


# 7. API Endpoint: Summary Statistik Dashboard
@app.get("/api/dashboard/summary")
def get_summary():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    total_nodes = cursor.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    total_odp = cursor.execute("SELECT COUNT(*) FROM nodes WHERE type = 'ODP'").fetchone()[0]
    total_incidents = cursor.execute("SELECT COUNT(*) FROM nodes WHERE status = 'Cut/Broken' OR type = 'INCIDENT'").fetchone()[0]
    total_cables = cursor.execute("SELECT COUNT(*) FROM cables").fetchone()[0]
    
    conn.close()
    return {
        "total_nodes": total_nodes,
        "total_odp": total_odp,
        "total_incidents": total_incidents,
        "total_cables": total_cables
    }


# --- MODEL DATA UPDATE ---
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


# --- ENDPOINT UPDATE NODE (PUT) ---
@app.put("/api/nodes/{node_id}")
def update_node(node_id: int, payload: NodeUpdate):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Cek apakah node ada
        existing = cursor.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if not existing:
            conn.close()
            raise HTTPException(status_code=404, detail="Node tidak ditemukan")

        # Update parsial (hanya field yang dikirim)
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
        conn.close()
        print(f"[SUCCESS] Node ID {node_id} berhasil di-update")
        return {"message": "Node berhasil diperbarui"}
    except Exception as e:
        print(f"[ERROR] Node Update Failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# --- ENDPOINT DELETE NODE (DELETE) ---
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
        print(f"[ERROR] Node Delete Failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# --- ENDPOINT UPDATE CABLE (PUT) ---
@app.put("/api/cables/{cable_id}")
def update_cable(cable_id: int, payload: CableUpdate):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        existing = cursor.execute("SELECT * FROM cables WHERE id = ?", (cable_id,)).fetchone()
        if not existing:
            conn.close()
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
        conn.close()
        print(f"[SUCCESS] Cable ID {cable_id} berhasil di-update")
        return {"message": "Kabel berhasil diperbarui"}
    except Exception as e:
        print(f"[ERROR] Cable Delete Failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# --- ENDPOINT DELETE CABLE (DELETE) ---
@app.delete("/api/cables/{cable_id}")
def delete_cable(cable_id: int):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM cables WHERE id = ?", (cable_id,))
        conn.commit()
        conn.close()
        print(f"[SUCCESS] Cable ID {cable_id} berhasil dihapus")
        return {"message": "Kabel berhasil dihapus"}
    except Exception as e:
        print(f"[ERROR] Cable Delete Failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# --- MOUNT STATIC FILES (HARUS PALING BAWAH) ---
app.mount("/", StaticFiles(directory="static", html=True), name="static")