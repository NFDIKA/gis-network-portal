import sqlite3
import json

DB_NAME = "gis_network.db"

def migrate():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    print("--- MEMULAI MIGRASI DATABASE ---")

    # 1. Tambah kolom baru ke tabel NODES jika belum ada
    node_columns = [
        ("cluster", "TEXT DEFAULT 'EKO'"),
        ("area", "TEXT DEFAULT 'BANJARMASIN'"),
        ("city", "TEXT DEFAULT 'Kota Banjarmasin'"),
        ("capacity", "TEXT DEFAULT '8 Port'"),
        ("spec_data", "TEXT DEFAULT '{}'")
    ]

    for col_name, col_type in node_columns:
        try:
            cursor.execute(f"ALTER TABLE nodes ADD COLUMN {col_name} {col_type};")
            print(f"[OK] Kolom '{col_name}' berhasil ditambahkan ke tabel 'nodes'.")
        except sqlite3.OperationalError:
            print(f"[INFO] Kolom '{col_name}' sudah ada di tabel 'nodes'.")

    # 2. Tambah kolom baru ke tabel CABLES jika belum ada
    cable_columns = [
        ("cluster", "TEXT DEFAULT 'EKO'"),
        ("area", "TEXT DEFAULT 'BANJARMASIN'"),
        ("city", "TEXT DEFAULT 'Kota Banjarmasin'"),
        ("capacity", "TEXT DEFAULT '24C'"),
        ("core_data", "TEXT DEFAULT '{}'")
    ]

    for col_name, col_type in cable_columns:
        try:
            cursor.execute(f"ALTER TABLE cables ADD COLUMN {col_name} {col_type};")
            print(f"[OK] Kolom '{col_name}' berhasil ditambahkan ke tabel 'cables'.")
        except sqlite3.OperationalError:
            print(f"[INFO] Kolom '{col_name}' sudah ada di tabel 'cables'.")

    # 3. Buat tabel relasi CORE_CONNECTIONS (Untuk melacak end-to-end sambungan)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS core_connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL,       -- 'POP', 'CLOSURE', 'CABLE', 'ODP'
            source_id INTEGER NOT NULL,
            source_port_core TEXT NOT NULL,  -- e.g. 'T1-C1' atau 'Out-1'
            target_type TEXT NOT NULL,       -- 'CLOSURE', 'CABLE', 'ODP', 'PELANGGAN'
            target_id INTEGER NOT NULL,
            target_port_core TEXT NOT NULL,
            status TEXT DEFAULT 'Connected',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    print("[OK] Tabel 'core_connections' siap digunakan.")

    conn.commit()
    conn.close()
    print("--- MIGRASI DATABASE SELESAI ---")

if __name__ == "__main__":
    migrate()