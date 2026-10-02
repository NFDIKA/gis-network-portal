import sqlite3

def run_migration():
    conn = sqlite3.connect("gis_network.db")
    cursor = conn.cursor()
    
    # Ambil daftar kolom yang ada saat ini di tabel incidents
    cursor.execute("PRAGMA table_info(incidents)")
    columns = [row[1] for row in cursor.fetchall()]
    
    print("Kolom saat ini di tabel incidents:", columns)
    
    # Tambahkan linked_cable_id jika belum ada
    if "linked_cable_id" not in columns:
        cursor.execute("ALTER TABLE incidents ADD COLUMN linked_cable_id INTEGER")
        print("-> Kolom 'linked_cable_id' berhasil ditambahkan.")
        
    # Tambahkan linked_node_id jika belum ada
    if "linked_node_id" not in columns:
        cursor.execute("ALTER TABLE incidents ADD COLUMN linked_node_id INTEGER")
        print("-> Kolom 'linked_node_id' berhasil ditambahkan.")
        
    # Tambahkan resolved_at jika belum ada
    if "resolved_at" not in columns:
        cursor.execute("ALTER TABLE incidents ADD COLUMN resolved_at DATETIME")
        print("-> Kolom 'resolved_at' berhasil ditambahkan.")
        
    conn.commit()
    conn.close()
    print("Migrasi database selesai!")

if __name__ == "__main__":
    run_migration()