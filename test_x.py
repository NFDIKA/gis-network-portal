import os, sys, json, shutil, math, time
os.environ['NETGIS_ADMIN_PASSWORD'] = 'Admin#12345'; os.environ['NETGIS_ROUTER'] = 'off'
sys.path.insert(0, 'shim'); sys.path.insert(0, 'run')
shutil.copy('/mnt/user-data/uploads/gis_network.db', 'run/gis_network.db')
import main
from starlette.exceptions import HTTPException
PASS = FAIL = 0
def check(n, c, x=""):
    global PASS, FAIL
    if c: PASS += 1; print("  PASS ", n)
    else: FAIL += 1; print("  FAIL ", n, x)
def raises(code, fn, *a, **k):
    try: fn(*a, **k)
    except HTTPException as e: return e.status_code == code
    return False
main.init_db()
main.CURRENT_USER.set({"id": 1, "username": "admin", "role": "admin"})
LAT0, LNG0 = -3.40, 114.60
M_LNG = 111194.9 * math.cos(math.radians(LAT0)); M_LAT = 111194.9
def at(along, off=0.0): return LAT0 + off / M_LAT, LNG0 + along / M_LNG
def mk(name, typ, along, off=0.0):
    la, ln = at(along, off)
    return main.create_node(main.NodeCreate(name=name, type=typ, status='Active', latitude=la, longitude=ln, cluster='EKO', area='BANJARMASIN', capacity='Tiang 7m'))['id']
def REQ(**k):
    la, ln = at(500)
    d = dict(origin_type='POINT', origin_lat=LAT0, origin_lng=LNG0, origin_name='Asal', dest_lat=la, dest_lng=ln, dest_name='Pelanggan X',
             installation='Udara', scenario='DIRECT')
    d.update(k); return main.PlanRequest(**d)
print("== X1. Garis dasar tanpa tiang eksisting")
p0 = main.preview_plan(REQ())
L = p0['route']['length_m']
check("X1 panjang rute ~500 m", 495 < L < 506, L)
check("X1 tanpa eksisting: 9 tiang baru (perilaku lama tetap)", p0['summary']['poles_new'] == 9 and p0['summary']['poles_existing'] == 0, p0['summary'])

print("== X2. Tiang eksisting tidak beraturan di koridor")
t30 = mk('X-T30', 'TIANG', 30, 3); t90a = mk('X-T90A', 'TIANG', 90, 6); t90b = mk('X-T90B', 'TIANG', 91, 2); t150 = mk('X-T150', 'TIANG', 150, -4)
far = mk('X-FAR', 'TIANG', 250, 20); hh = mk('X-HH60', 'HH', 60, 1)
p1 = main.preview_plan(REQ())
A = [a for a in p1['assets'] if a['kind'] == 'TIANG']
ex = [a for a in A if a['existing_id']]
check("X2 3 tiang eksisting terbaca (yang berhimpitan dipilih yang terdekat)", sorted(a['existing_id'] for a in ex) == sorted([t30, t90b, t150]), [a['existing_name'] for a in ex])
check("X2 tiang di luar koridor (20 m) tidak dipakai", far not in [a['existing_id'] for a in ex])
check("X2 HH tidak ikut dibaca pada rencana udara", hh not in [a['existing_id'] for a in ex] and p1['summary']['hh_existing'] == 0)
check("X2 tiang baru hanya mengisi bentang > jarak maksimal: 8 baru, 3 pakai ulang", p1['summary']['poles_new'] == 8 and p1['summary']['poles_existing'] == 3, p1['summary'])
d = [a['distance_m'] for a in A]
check("X2 daftar terurut sepanjang jalur", d == sorted(d))
spans = [b - a for a, b in zip([0.0] + d, d + [L])]
check("X2 tidak ada bentang melebihi jarak maksimal (+2 m)", max(spans) <= 52.5, max(spans))
check("X2 koordinat tiang eksisting mengikuti node aslinya & ada offset_m", all(a['offset_m'] is not None and a['offset_m'] <= 8 for a in ex))
check("X2 BOQ hanya menghitung tiang baru", p1['summary']['poles_new'] == 8)

print("== X3. Aturan & opsi")
check("X3 reuse_radius_m=0 -> semua baru", main.preview_plan(REQ(rules={'reuse_radius_m': 0}))['summary']['poles_new'] == 9)
r25 = main.preview_plan(REQ(rules={'reuse_radius_m': 25}))['summary']
check("X3 radius 25 m ikut membaca tiang 20 m", r25['poles_existing'] == 4, r25)
pn = main.preview_plan(REQ(use_poles=False))
check("X3 tanpa tiang/HH baru -> tidak ada aset tiang", not [a for a in pn['assets'] if a['kind'] == 'TIANG'])
pt = main.preview_plan(REQ(installation='Tanah'))
check("X3 kabel tanah membaca HH (1 pakai ulang), bukan TIANG", pt['summary']['hh_existing'] == 1 and pt['summary']['poles_existing'] == 0, pt['summary'])
check("X3 HH baru mengisi bentang (100 m)", pt['summary']['hh_new'] == 4, pt['summary'])

print("== X4. Hub: satu tiang tidak dipakai dua segmen")
ph = main.preview_plan(REQ(scenario='HUB'))
ids = [a['existing_id'] for a in ph['assets'] if a['existing_id']]
check("X4 tidak ada existing_id ganda", len(ids) == len(set(ids)), ids)

print("== X5. Realisasi")
pl = main.save_plan(REQ())
rz = main.realize_plan(pl['id'])
check("X5 reused_ids = 3 tiang eksisting", sorted(rz['reused_ids']) == sorted([t30, t90b, t150]), rz['reused_ids'])
with main.db() as c:
    n_t = c.execute("SELECT COUNT(*) FROM nodes WHERE type='TIANG' AND name LIKE ?", (f"PSB-{pl['id']:04d}-TIANG-%",)).fetchone()[0]
check("X5 hanya tiang baru yang dibuat (8)", n_t == 8, n_t)
cid = rz['cable_id']

print("== X6. Relasi kabel -> tiang (dihitung dari posisi)")
s = main.get_cable_supports(cid)
check("X6 11 tiang dilewati (3 eksisting + 8 baru)", s['count'] == 11 and s['kinds'] == ['TIANG'], (s['count'], s['kinds']))
check("X6 terurut sepanjang jalur & jarak tegak lurus kecil", [i['along_m'] for i in s['items']] == sorted(i['along_m'] for i in s['items']) and all(i['offset_m'] <= 8 for i in s['items']))
check("X6 tiang di luar koridor tidak ikut", far not in [i['id'] for i in s['items']])
check("X6 tidak ada bentang melebihi aturan", s['spans_over_rule'] == 0 and s['max_span_m'] <= 52.5, (s['spans_over_rule'], s['max_span_m']))
s25 = main.get_cable_supports(cid, radius=25)
check("X6 radius lebih lebar: tiang berhimpitan tetap dipilih yang terdekat (tidak dobel)", s25['count'] == 11 and far not in [i['id'] for i in s25['items']] and s25['radius_m'] == 25)
check("X6 kabel tak ada -> 404; radius salah -> 400", raises(404, main.get_cable_supports, 99999) and raises(400, main.get_cable_supports, cid, 0) and raises(400, main.get_cable_supports, cid, 51))

print("== X7. Relasi tiang -> kabel")
r = main.get_node_cables_through(t90b)
check("X7 tiang eksisting menunjukkan kabel yang menumpang", r['count'] == 1 and r['items'][0]['id'] == cid and r['type'] == 'TIANG', r)
check("X7 tiang yang tidak dilewati kabel -> 0", main.get_node_cables_through(far)['count'] == 0)
check("X7 HH tidak cocok dengan kabel udara -> 0", main.get_node_cables_through(hh)['count'] == 0)
with main.db() as c: pop = c.execute("SELECT id FROM nodes WHERE type='POP' LIMIT 1").fetchone()
check("X7 aset bukan TIANG/HH -> 400; tak ada -> 404", (pop is None or raises(400, main.get_node_cables_through, pop['id'])) and raises(404, main.get_node_cables_through, 99999))

print("== X7b. Tiang eksisting dekat kelipatan jarak: tiang baru tidak menempel (jalur terpisah)")
DL = -0.02
la0, ln0 = LAT0 + DL, LNG0
la1, ln1 = LAT0 + DL, LNG0 + 500 / M_LNG
main.create_node(main.NodeCreate(name='X-T352', type='TIANG', status='Active', latitude=la0 + 2 / M_LAT, longitude=LNG0 + 352 / M_LNG, cluster='EKO', area='BANJARMASIN', capacity='Tiang 7m'))
pb = main.preview_plan(main.PlanRequest(origin_type='POINT', origin_lat=la0, origin_lng=ln0, origin_name='Asal2', dest_lat=la1, dest_lng=ln1, dest_name='P2', installation='Udara', scenario='DIRECT'))
Ab = [a for a in pb['assets'] if a['kind'] == 'TIANG']
close = [(a['distance_m'], b['distance_m']) for a in Ab for b in Ab if a is not b and not a['existing_id'] and abs(a['distance_m'] - b['distance_m']) < 10]
check("X7b tidak ada tiang baru < 10 m dari tiang lain", not close, close)
db_ = [a['distance_m'] for a in Ab]
check("X7b bentang tetap <= jarak maksimal (+2 m)", max(y - x for x, y in zip([0.0] + db_, db_ + [pb['route']['length_m']])) <= 52.5)
check("X7b 1 tiang eksisting terbaca & 8 baru dibagi rata di dua bentang", pb['summary']['poles_existing'] == 1 and pb['summary']['poles_new'] == 8, pb['summary']['poles_new'])

print("== X8. Kinerja (3000 tiang lain)")
with main.db() as c:
    c.executemany("INSERT INTO nodes (name,type,status,latitude,longitude,cluster,area,city,capacity,spec_data) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  [(f"BULK-{i}", 'TIANG', 'Active', -3.5 + (i % 60) * 0.001, 114.7 + (i // 60) * 0.001, 'EKO', 'BJM', 'BJM', 'Tiang 7m', '{}') for i in range(3000)])
t0 = time.time(); main.preview_plan(REQ()); main.get_cable_supports(cid); main.get_node_cables_through(t30)
check("X8 perencanaan + 2 endpoint relasi < 3 detik", time.time() - t0 < 3, time.time() - t0)
print(f"\nHASIL: {PASS} lulus, {FAIL} gagal")