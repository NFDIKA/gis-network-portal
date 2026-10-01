import sqlite3

def setup_database():
    conn = sqlite3.connect('gis_network.db')
    cursor = conn.cursor()

    # 1. Tabel Nodes (POP, ODP, Closure, Incident)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        type TEXT NOT NULL,          -- 'POP', 'CLOSURE', 'ODP', 'INCIDENT'
        status TEXT DEFAULT 'Active', -- 'Active', 'Incident', 'Maintenance'
        latitude REAL NOT NULL,
        longitude REAL NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    # 2. Tabel Cables (Feeder, Distribution, Drop Core)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS cables (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        type TEXT NOT NULL,          -- 'Feeder', 'Distribution', 'Drop'
        status TEXT DEFAULT 'Active',
        geojson_geometry TEXT NOT NULL, -- Menyimpan koordinat jalur dalam format GeoJSON
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    conn.commit()
    conn.close()
    print("Database 'gis_network.db' berhasil dibuat!")

if __name__ == '__main__':
    setup_database()