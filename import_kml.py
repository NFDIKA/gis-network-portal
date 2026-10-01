import json
import sqlite3
import geopandas as gpd

def import_kml_to_db(kml_filepath, data_type, layer_category):
    """
    kml_filepath: Path ke file .kml
    data_type: 'node' (titik ODP/POP) atau 'cable' (jalur kabel)
    layer_category: 'ODP', 'POP', 'CLOSURE', 'Feeder', 'Distribution', dll.
    """
    # Membaca KML dengan GeoPandas
    df = gpd.read_file(kml_filepath, driver='KML')
    
    conn = sqlite3.connect('gis_network.db')
    cursor = conn.cursor()

    for _, row in df.iterrows():
        name = row.get('Name', 'Tanpa Nama')
        geom = row['geometry']

        if data_type == 'node' and geom.geom_type == 'Point':
            lat = geom.y
            lon = geom.x
            cursor.execute('''
                INSERT INTO nodes (name, type, latitude, longitude)
                VALUES (?, ?, ?, ?)
            ''', (name, layer_category, lat, lon))

        elif data_type == 'cable' and geom.geom_type in ['LineString', 'MultiLineString']:
            # Konversi geometri jalur ke format GeoJSON string
            geojson_str = json.dumps(geom.__geo_interface__)
            cursor.execute('''
                INSERT INTO cables (name, type, geojson_geometry)
                VALUES (?, ?, ?)
            ''', (name, layer_category, geojson_str))

    conn.commit()
    conn.close()
    print(f"Berhasil mengimpor {len(df)} item dari {kml_filepath} ke database!")

# Contoh Pengujian Script (Hapus tanda komentar jika ingin langsung tes file KML):
# import_kml_to_db('contoh_odp.kml', 'node', 'ODP')
# import_kml_to_db('contoh_feeder.kml', 'cable', 'Feeder')