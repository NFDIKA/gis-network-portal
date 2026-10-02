#!/usr/bin/env python3
"""
Data dummy NETGIS Enterprise untuk pengujian seluruh fitur.

Pakai:
  1. Jalankan aplikasi (sebaiknya database KOSONG/baru, uvicorn main:app --port 8000).
     Disarankan: NETGIS_ROUTER=off  (rute lurus, hasil BOQ/jarak konsisten tanpa internet).
  2. python seed_dummy_netgis.py --base http://127.0.0.1:8000 --admin-pass "PasswordAdminAnda"
  3. Buka checklist (Checklist_Pengujian_NETGIS.xlsx) dan jalankan skenario satu per satu.

Semua nama aset berawalan DMY- supaya mudah dikenali/dihapus. Skrip aman dijalankan SEKALI per database.
"""
import argparse, base64, http.cookiejar, json, sys, urllib.error, urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://127.0.0.1:8000")
ap.add_argument("--admin-user", default="admin")
ap.add_argument("--admin-pass", required=True)
ap.add_argument("--new-admin-pass", default=None, help="isi bila akun admin masih wajib ganti password")
ap.add_argument("--user-pass", default="Dummy#12345", help="password untuk akun uji viewer1/teknisi1/noc1")
A = ap.parse_args()

cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
FAILS = []


def rq(path, body=None, method=None, soft=False):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(A.base + path, data, {"Content-Type": "application/json"}, method=method)
    try:
        return json.load(op.open(r))
    except urllib.error.HTTPError as e:
        msg = e.read().decode()[:300]
        if soft:
            FAILS.append((path, e.code, msg)); print(f"  ! {path} -> {e.code} {msg}"); return None
        print(f"GAGAL {method or 'POST'} {path}: {e.code} {msg}"); sys.exit(1)


me = rq("/api/auth/login", {"username": A.admin_user, "password": A.admin_pass})
if (me.get("user") or me).get("must_change_password"):
    if not A.new_admin_pass:
        sys.exit("Akun admin wajib ganti password: ulangi dengan --new-admin-pass")
    rq("/api/auth/password", {"current_password": A.admin_pass, "new_password": A.new_admin_pass})
    print("Password admin diganti.")

T = lambda n: f"Tube {(n - 1) // 12 + 1} - Core {n}"
IDS = {}
COORD = {}


def node(name, typ, lat, lng, cluster, area, cap=None, status="Active", city=None):
    d = dict(name=name, type=typ, status=status, latitude=lat, longitude=lng, cluster=cluster, area=area,
             city=city or "Kota " + area.title())
    if cap: d["capacity"] = cap
    IDS[name] = rq("/api/nodes", d)["id"]
    COORD[name] = (lat, lng)
    return IDS[name]


def cable(name, typ, pts, cap, cluster, area, inst="Udara", a=None, b=None, mode="SM"):
    """pts = [(lat,lng),...]"""
    d = dict(name=name, type=typ, status="Active", capacity=cap, coordinates=[[p[1], p[0]] for p in pts],
             installation=inst, cluster=cluster, area=area, city="Kota " + area.title(), fiber_mode=mode)
    if a: d["from_node_id"] = IDS[a]
    if b: d["to_node_id"] = IDS[b]
    IDS[name] = rq("/api/cables", d)["id"]
    return IDS[name]


def conn(ft, fname, fport, tt, tname, tport, via=None, vcore=None):
    return rq("/api/connections", dict(from_asset_type=ft, from_asset_id=IDS[fname], from_port_core=fport,
              to_asset_type=tt, to_asset_id=IDS[tname], to_port_core=tport,
              via_cable_id=IDS[via] if via else None, via_core=vcore, status="Connected"))


def auto(cable_name, cores=1, **kw):
    r = rq(f"/api/cables/{IDS[cable_name]}/auto-connect", dict(cores=cores, **kw))
    print(f"  auto-sambung {cable_name}: {'OK' if r['ok'] else 'GAGAL: ' + str(r['reason'])}")
    return r


E, W = "EKO", "WKO"
BJM, PNK = "BANJARMASIN", "PONTIANAK"
print("== 1. Pengguna uji (viewer / teknisi / noc)")
for u, role, nm in (("viewer1", "viewer", "Viewer Uji"), ("teknisi1", "teknisi", "Teknisi Uji"), ("noc1", "noc", "NOC Uji")):
    rq("/api/users", dict(username=u, full_name=nm, role=role, password=A.user_pass), soft=True)

print("== 2. Wilayah Banjarmasin: POP, closure, ODP")
lat0, lng0 = -3.3190, 114.5900
node("DMY-POP-BJM-01", "POP", lat0, lng0, E, BJM, "48C")
node("DMY-CL-BJM-01", "CLOSURE", lat0, lng0 + 0.0090, E, BJM, "24 Core")
node("DMY-CL-BJM-02", "CLOSURE", lat0, lng0 + 0.0170, E, BJM, "24 Core")
node("DMY-ODP-BJM-01", "ODP", lat0, lng0 + 0.0240, E, BJM, "1 In - 8 Out")
node("DMY-ODP-BJM-02", "ODP", lat0 - 0.0070, lng0 + 0.0170, E, BJM, "1 In - 8 Out")
node("DMY-ODP-BJM-FULL", "ODP", lat0 + 0.0070, lng0 + 0.0170, E, BJM, "1 In - 4 Out")      # akan penuh
node("DMY-ODP-BJM-MT", "ODP", lat0 + 0.0140, lng0 + 0.0170, E, BJM, "1 In - 8 Out", status="Maintenance")
node("DMY-CL-THRU", "CLOSURE", lat0 - 0.0100, lng0 + 0.0100, E, BJM, "24 Core")             # kabel melintas -> uji pecah kabel
node("DMY-ODP-BJM-03", "ODP", lat0 - 0.0100, lng0 + 0.0200, E, BJM, "1 In - 8 Out")
for i, (la, ln, t, c) in enumerate([(0.0, 0.0045, "TIANG", "Tiang 9m"), (0.0, 0.0130, "TIANG", "Tiang 9m"),
                                    (0.0, 0.0205, "HH", "HH Standar"), (0.0, 0.0050, "SLACK", "Slack 20m")], 1):
    node(f"DMY-{t}-BJM-{i:02d}", t, lat0 + la, lng0 + ln, E, BJM, c)

print("== 3. Kabel + sambungan OTOMATIS (Tahap B): POP -> CL1 -> CL2 -> ODP-01")
cable("DMY-FDR-BJM-01", "Feeder", [(lat0, lng0), (lat0, lng0 + 0.0090)], "24C", E, BJM)
cable("DMY-DIST-BJM-01", "Distribution", [(lat0, lng0 + 0.0090), (lat0, lng0 + 0.0170)], "12C", E, BJM)
cable("DMY-DIST-BJM-02", "Distribution", [(lat0, lng0 + 0.0170), (lat0, lng0 + 0.0240)], "12C", E, BJM)
for c in ("DMY-FDR-BJM-01", "DMY-DIST-BJM-01", "DMY-DIST-BJM-02"):
    auto(c)

print("== 4. Rantai MANUAL (joint T1/C2): POP -> CL1 -> CL2 -> ODP-02")
cable("DMY-DIST-BJM-03", "Distribution", [(lat0, lng0 + 0.0170), (lat0 - 0.0070, lng0 + 0.0170)], "12C", E, BJM, a="DMY-CL-BJM-02", b="DMY-ODP-BJM-02")
conn("NODE", "DMY-POP-BJM-01", T(2), "NODE", "DMY-CL-BJM-01", T(2), "DMY-FDR-BJM-01", T(2))
conn("NODE", "DMY-CL-BJM-01", T(2), "NODE", "DMY-CL-BJM-02", T(2), "DMY-DIST-BJM-01", T(2))
conn("NODE", "DMY-CL-BJM-02", T(2), "NODE", "DMY-ODP-BJM-02", "IN-1", "DMY-DIST-BJM-03", T(1))

print("== 5. ODP penuh (4 pelanggan) + ODP-01 (3 pelanggan) + ODP-02 (1 pelanggan)")
cable("DMY-DIST-BJM-04", "Distribution", [(lat0, lng0 + 0.0170), (lat0 + 0.0070, lng0 + 0.0170)], "12C", E, BJM, a="DMY-CL-BJM-02", b="DMY-ODP-BJM-FULL")
# umpan hulu ODP-FULL: core 3 dari POP lewat auto-sambung kabel DIST-04 (hulu CL-02 sudah punya joint T1/C1 & C2 -> butuh umpan baru)
auto("DMY-DIST-BJM-04")
def add_customer(tag, odp, lat, lng, cap="2 Core"):
    nm = f"DMY-PLG-{tag}"
    node(nm, "PELANGGAN", lat, lng, E, BJM, cap)
    olat, olng = COORD[odp]
    dn = f"DMY-DROP-{tag}"
    cable(dn, "Drop", [(olat, olng), (lat, lng)], "2C", E, BJM, a=odp, b=nm)
    return auto(dn)


for i, (tag, odp) in enumerate([("001", "DMY-ODP-BJM-01"), ("002", "DMY-ODP-BJM-01"), ("003", "DMY-ODP-BJM-01"),
                                ("004", "DMY-ODP-BJM-02"),
                                ("F01", "DMY-ODP-BJM-FULL"), ("F02", "DMY-ODP-BJM-FULL"), ("F03", "DMY-ODP-BJM-FULL"), ("F04", "DMY-ODP-BJM-FULL")]):
    base = {"DMY-ODP-BJM-01": (lat0, lng0 + 0.0240), "DMY-ODP-BJM-02": (lat0 - 0.0070, lng0 + 0.0170),
            "DMY-ODP-BJM-FULL": (lat0 + 0.0070, lng0 + 0.0170)}[odp]
    add_customer(tag, odp, base[0] + 0.0006 * (i % 4 + 1), base[1] + 0.0004 * (i % 3 + 1))

print("== 6. Kabel melintas di closure (uji 'Pecah kabel di closure') + ODP-03")
cable("DMY-FDR-THRU", "Feeder", [(lat0, lng0), (lat0 - 0.0100, lng0 + 0.0100), (lat0 - 0.0100, lng0 + 0.0200)], "12C", E, BJM, a="DMY-POP-BJM-01", b="DMY-ODP-BJM-03")
conn("NODE", "DMY-POP-BJM-01", T(10), "NODE", "DMY-ODP-BJM-03", "IN-1", "DMY-FDR-THRU", T(1))

print("== 7. Kasus uji negatif / tepi")
cable("DMY-CBL-YATIM", "Distribution", [(lat0 + 0.03, lng0), (lat0 + 0.03, lng0 + 0.004)], "12C", E, BJM)     # ujung tak di node -> auto-sambung gagal
node("DMY-PLG-LEPAS", "PELANGGAN", lat0 + 0.0300, lng0 + 0.0100, E, BJM, "2 Core")                              # pelanggan belum tersambung
node("DMY-ODP-JAUH", "ODP", lat0 + 0.0500, lng0 + 0.0500, E, BJM, "1 In - 8 Out")                                 # ODP tanpa hulu (dest > 1 km)
node("DMY-TIANG-TANPAPORT", "TIANG", lat0 + 0.002, lng0 + 0.002, E, BJM, "Tiang 7m")

print("== 8. Wilayah Pontianak (uji filter cluster/area)")
node("DMY-POP-PNK-01", "POP", -0.0263, 109.3425, W, PNK, "48C")
node("DMY-CL-PNK-01", "CLOSURE", -0.0263, 109.3495, W, PNK, "24 Core")
node("DMY-ODP-PNK-01", "ODP", -0.0263, 109.3560, W, PNK, "1 In - 8 Out")
cable("DMY-FDR-PNK-01", "Feeder", [(-0.0263, 109.3425), (-0.0263, 109.3495)], "24C", W, PNK)
cable("DMY-DIST-PNK-01", "Distribution", [(-0.0263, 109.3495), (-0.0263, 109.3560)], "12C", W, PNK, mode="MM")
auto("DMY-FDR-PNK-01"); auto("DMY-DIST-PNK-01")

print("== 9. Insiden")
IDS["inc1"] = rq("/api/incidents", dict(ticket_number="DMY-INC-001", title="Putus feeder BJM-01 (core 1-2)", severity="Major",
                 incident_type="FO Cut", description="Galian PU memutus kabel", latitude=lat0, longitude=lng0 + 0.0045,
                 linked_cable_id=IDS["DMY-FDR-BJM-01"], affected_cores=[1, 2], reporter="NOC Uji"))["id"]
IDS["inc2"] = rq("/api/incidents", dict(ticket_number="DMY-INC-002", title="Gangguan redaman tinggi distribusi 03", severity="Minor",
                 incident_type="Network Issue", latitude=lat0 - 0.0035, longitude=lng0 + 0.0170,
                 linked_cable_id=IDS["DMY-DIST-BJM-03"], reporter="Teknisi Uji"))["id"]
IDS["inc3"] = rq("/api/incidents", dict(ticket_number="DMY-INC-003", title="ODP-MT dalam perawatan", severity="Minor",
                 incident_type="Maintenance", latitude=lat0 + 0.014, longitude=lng0 + 0.017, linked_node_id=IDS["DMY-ODP-BJM-MT"]))["id"]
IDS["inc4"] = rq("/api/incidents", dict(ticket_number="DMY-INC-004", title="Putus feeder Pontianak", severity="Critical",
                 incident_type="FO Cut", latitude=-0.0263, longitude=109.346, linked_cable_id=IDS["DMY-FDR-PNK-01"]))["id"]
rq(f"/api/incidents/{IDS['inc2']}/status", dict(status="In Progress", actor="Teknisi Uji", note="Menuju lokasi"), "PUT", soft=True)
# perbaikan sementara pada insiden 2: sisipkan closure pada kabel DIST-03
rq(f"/api/incidents/{IDS['inc2']}/repairs", dict(kind="TEMPORARY", action="ADD_CLOSURE", name="DMY-JC-REPAIR-01",
   technician="Teknisi Uji", notes="Splice sementara"), soft=True)

print("== 10. OTDR + daya optik terukur")
def otdr(cab, core, loss, length, ev=None, wl=1310, when="2026-09-28 10:00:00", frm="A"):
    rq("/api/otdr/manual", dict(cable_id=IDS[cab], from_node=frm, core=core, wavelength_nm=wl, measured_at=when,
       length_m=length, total_loss_db=loss, events=ev or [], notes="data dummy", on_duplicate="replace"), soft=True)
otdr("DMY-FDR-BJM-01", T(1), 1.1, 1000, [dict(distance_m=0, loss_db=0.5, type="Konektor")])
otdr("DMY-FDR-BJM-01", T(2), 1.3, 1000)
otdr("DMY-FDR-BJM-01", T(3), 9.9, 1000, [dict(distance_m=400, loss_db=7.0, type="Bending")])          # BAD
otdr("DMY-DIST-BJM-01", T(1), 0.9, 800)
otdr("DMY-DIST-BJM-02", T(1), 1.4, 820, wl=1550)
otdr("DMY-DIST-BJM-01", T(1), 1.6, 800, when="2026-10-01 10:00:00")                                           # tren naik
def power(node_name, port, tx=None, rx=None, wl=1490):
    rq("/api/power", dict(asset_type="NODE", asset_id=IDS[node_name], port_core=port, tx_dbm=tx, rx_dbm=rx, wavelength_nm=wl, device="Power meter dummy"), "PUT", soft=True)
power("DMY-POP-BJM-01", T(1), tx=3.0)
power("DMY-ODP-BJM-01", "IN-1", rx=-17.5)
power("DMY-ODP-BJM-02", "IN-1", rx=-24.5)          # margin tipis
power("DMY-ODP-BJM-03", "IN-1", rx=-30.0)          # di bawah batas (BAD)

print("== 11. Rencana Pasang Baru (preview/simpan/wujudkan)")
plans = [
    dict(name="DMY-PLAN-01 <1 km dari ODP-01 (DIRECT)", origin_type="NODE", origin_id=IDS["DMY-ODP-BJM-01"], dest_lat=lat0 + 0.0030, dest_lng=lng0 + 0.0260, dest_name="Calon PLG A", scenario="DIRECT"),
    dict(name="DMY-PLAN-02 >1 km hub (HUB, 2 core)", origin_type="NODE", origin_id=IDS["DMY-CL-BJM-02"], dest_lat=lat0 + 0.0150, dest_lng=lng0 + 0.0350, dest_name="Calon PLG B", scenario="HUB", customer_cores=2),
    dict(name="DMY-PLAN-03 dari POP, AUTO", origin_type="NODE", origin_id=IDS["DMY-POP-BJM-01"], dest_lat=lat0 - 0.0040, dest_lng=lng0 + 0.0020, dest_name="Calon PLG C"),
    dict(name="DMY-PLAN-04 titik bebas", origin_type="POINT", origin_lat=lat0 + 0.02, origin_lng=lng0, dest_lat=lat0 + 0.021, dest_lng=lng0 + 0.002, dest_name="Calon PLG D", create_customer=False),
    dict(name="DMY-PLAN-05 ODP penuh", origin_type="NODE", origin_id=IDS["DMY-ODP-BJM-FULL"], dest_lat=lat0 + 0.0080, dest_lng=lng0 + 0.0185, dest_name="Calon PLG E"),
]
for pl in plans:
    r = rq("/api/plans", pl, soft=True)
    if r: IDS[pl["name"]] = r["id"]
if "DMY-PLAN-01 <1 km dari ODP-01 (DIRECT)" in IDS:   # wujudkan satu rencana saja; sisanya dipakai penguji
    rq(f"/api/plans/{IDS['DMY-PLAN-01 <1 km dari ODP-01 (DIRECT)']}/realize", {}, soft=True)

print("== 12. Berkas contoh impor")
print("   (lihat dmy_import_contoh.geojson & dmy_otdr_contoh.csv untuk uji impor)")
print(f"\nSelesai. Aset dibuat: {len(IDS)}. Peringatan/gagal lunak: {len(FAILS)}")
for f in FAILS: print("  -", f)
print(f"Akun uji: viewer1 / teknisi1 / noc1  (password: {A.user_pass})")