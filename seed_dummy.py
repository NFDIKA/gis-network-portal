import sqlite3
import json

def seed_dummy_data():
    conn = sqlite3.connect('gis_network.db')
    cursor = conn.cursor()

    # Clear data lama jika ada
    cursor.execute('DELETE FROM nodes')
    cursor.execute('DELETE FROM cables')

    # 1. Insert Data Dummy Nodes (Titik POP, Closure, ODP)
    dummy_nodes = [
        ('POP Central - Banjarmasin', 'POP', 'Active', -3.3194, 114.5908),
        ('Joint Closure JC-01', 'CLOSURE', 'Active', -3.3220, 114.5930),
        ('ODP-A1/01 (16 Port)', 'ODP', 'Active', -3.3245, 114.5955),
        ('ODP-A1/02 (8 Port)', 'ODP', 'Active', -3.3260, 114.5970),
        ('ODP-B1/01 (16 Port)', 'ODP', 'Maintenance', -3.3210, 114.5880),
        ('Incident Point: Kabel Putus Core 3', 'INCIDENT', 'Cut/Broken', -3.3230, 114.5942),
    ]

    for node in dummy_nodes:
        cursor.execute('''
            INSERT INTO nodes (name, type, status, latitude, longitude)
            VALUES (?, ?, ?, ?, ?)
        ''', node)

    # 2. Insert Data Dummy Cables (Jalur Kabel Feeder & Distribusi)
    # Jalur Feeder 1: Dari POP Central ke Joint Closure JC-01
    feeder_1_geom = {
        "type": "LineString",
        "coordinates": [
            [114.5908, -3.3194],
            [114.5920, -3.3205],
            [114.5930, -3.3220]
        ]
    }

    # Jalur Distribusi 1: Dari JC-01 menuju ODP-A1/01 dan ODP-A1/02
    dist_1_geom = {
        "type": "LineString",
        "coordinates": [
            [114.5930, -3.3220],
            [114.5942, -3.3230],
            [114.5955, -3.3245],
            [114.5970, -3.3260]
        ]
    }

    dummy_cables = [
        ('Kabel Feeder Utama Core 24', 'Feeder', 'Active', json.dumps(feeder_1_geom)),
        ('Kabel Distribusi Cluster A', 'Distribution', 'Active', json.dumps(dist_1_geom))
    ]

    for cable in dummy_cables:
        cursor.execute('''
            INSERT INTO cables (name, type, status, geojson_geometry)
            VALUES (?, ?, ?, ?)
        ''', cable)

    conn.commit()
    conn.close()
    print("Berhasil memasukkan data dummy jaringan fiber optic ke database!")

if __name__ == '__main__':
    seed_dummy_data()