#!/usr/bin/env python3
"""
seed_dummy.py - data dummy + skenario uji NETGIS (hanya library standar Python, tanpa pip install).

Server harus sedang berjalan, mis.:   uvicorn main:app --reload
Lalu di terminal VS Code (folder yang sama):

    python seed_dummy.py seed        # isi jaringan dummy "DMY-*" (POP, closure, ODP, pelanggan, kabel, sambungan core)
    python seed_dummy.py test        # jalankan skenario insiden end-to-end + asersi PASS/FAIL
    python seed_dummy.py all         # reset dummy -> seed -> test
    python seed_dummy.py reset       # hapus semua aset dummy (nama berawalan "DMY-") beserta tiketnya
    python seed_dummy.py reset --all # hapus SEMUA aset & tiket (hati-hati! backup gis_network.db dulu)

Opsi:  --base http://127.0.0.1:8000   (alamat server)   --yes   (lewati konfirmasi reset --all)

Semua aset dummy bernama "DMY-..." sehingga data asli Anda aman dan mudah dibedakan di peta.
Setelah `test`, tiket & perbaikan hasil skenario tetap ada supaya bisa Anda lihat di peta / NOC Monitor.
"""
import argparse
import json
import math
import sys
import getpass
import http.cookiejar
import os
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8000"
PREFIX = "DMY-"

# ----------------------------------------------------------------------------- HTTP kecil
class ApiError(Exception):
    def __init__(self, status, detail):
        super().__init__(f"{status}: {detail}")
        self.status, self.detail = status, detail


# Sesi login disimpan di cookie jar (server memakai cookie HttpOnly)
_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_JAR))


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with _OPENER.open(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            detail = json.loads(raw).get("detail", raw.decode())
        except Exception:
            detail = raw.decode(errors="replace")
        if isinstance(detail, list):
            detail = "; ".join(str(d.get("msg", d)) for d in detail)
        raise ApiError(e.code, detail)
    except urllib.error.URLError as e:
        sys.exit(f"\n[X] Tidak bisa terhubung ke {BASE} ({e.reason}).\n"
                 f"    Jalankan server dulu di terminal lain:  uvicorn main:app --reload\n")


def login(username, password):
    """Masuk sebagai user yang sudah ada (butuh peran Admin agar reset/hapus berjalan penuh)."""
    try:
        d = api("POST", "/api/auth/login", {"username": username, "password": password})
    except ApiError as e:
        sys.exit(f"\n[X] Login gagal ({e}).\n"
                 f"    Admin awal dibuat otomatis saat server pertama kali jalan; password-nya tercetak di terminal uvicorn\n"
                 f"    (atau pakai NETGIS_ADMIN_PASSWORD). Beri kredensial lewat --user/--password atau env NETGIS_USER/NETGIS_PASSWORD.\n")
    u = d["user"]
    if u.get("must_change_password"):
        sys.exit("\n[X] Akun ini wajib ganti password dulu. Login lewat browser, ganti password, lalu jalankan ulang.\n")
    if u["role"] != "admin":
        print(f"[!] Login sebagai {u['username']} ({u['role']}). Sebagian langkah (hapus aset, kelola data) butuh Admin.")
    return u


# ----------------------------------------------------------------------------- util
def haversine(lat1, lng1, lat2, lng2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def point_along(coords, frac):
    """Titik (lat, lng) pada polyline [[lng,lat],...] di fraksi panjang tertentu."""
    seg = [haversine(coords[i][1], coords[i][0], coords[i + 1][1], coords[i + 1][0]) for i in range(len(coords) - 1)]
    target, acc = sum(seg) * frac, 0.0
    for i, s in enumerate(seg):
        if acc + s >= target:
            t = (target - acc) / s if s else 0
            return (coords[i][1] + t * (coords[i + 1][1] - coords[i][1]),
                    coords[i][0] + t * (coords[i + 1][0] - coords[i][0]))
        acc += s
    return coords[-1][1], coords[-1][0]


def features(path):
    return [f["properties"] for f in api("GET", path)["features"]]


def by_name(items, name):
    for it in items:
        if it["name"] == name:
            return it
    return None


# ----------------------------------------------------------------------------- hasil uji
class Report:
    def __init__(self):
        self.ok = self.bad = 0

    def check(self, name, cond, extra=""):
        if cond:
            self.ok += 1
            print(f"  \033[32mPASS\033[0m  {name}")
        else:
            self.bad += 1
            print(f"  \033[31mFAIL\033[0m  {name}  {extra}")

    def title(self, text):
        print(f"\n== {text}")


# ----------------------------------------------------------------------------- reset
def cmd_reset(all_data=False, assume_yes=False):
    if all_data and not assume_yes:
        if input("Ini menghapus SEMUA aset & tiket (bukan hanya dummy). Ketik 'HAPUS' untuk lanjut: ") != "HAPUS":
            sys.exit("Dibatalkan.")
    keep = (lambda n: True) if all_data else (lambda n: (n or "").startswith(PREFIX))

    n_inc = n_cab = n_node = 0
    for p in features("/api/incidents"):
        if all_data or (p.get("ticket_number") or "").startswith(PREFIX) or (p.get("title") or "").startswith(PREFIX):
            api("DELETE", f"/api/incidents/{p['id']}")
            n_inc += 1
    # tiket yang dibuat skenario test berjudul "DMY-..."; tiket AUTO untuk aset dummy ikut dibersihkan di bawah
    for p in features("/api/cables"):
        if keep(p["name"]) or (p["name"] or "").startswith(PREFIX):
            api("DELETE", f"/api/cables/{p['id']}")
            n_cab += 1
    for p in features("/api/nodes"):
        if keep(p["name"]) or (p["name"] or "").startswith(PREFIX):
            api("DELETE", f"/api/nodes/{p['id']}")
            n_node += 1
    if not all_data:
        # tiket otomatis (AUTO-...) yang menunjuk aset dummy sudah kehilangan tautan; hapus yang judulnya memuat DMY-
        for p in features("/api/incidents"):
            if PREFIX in (p.get("title") or ""):
                api("DELETE", f"/api/incidents/{p['id']}")
                n_inc += 1
    print(f"Reset selesai: {n_node} node, {n_cab} kabel, {n_inc} tiket dihapus.")


# ----------------------------------------------------------------------------- seed
LAT0, LNG0 = -3.3194, 114.5906   # Banjarmasin


def cmd_seed():
    if any((p["name"] or "").startswith(PREFIX) for p in features("/api/nodes")):
        sys.exit("Data dummy sudah ada. Jalankan dulu:  python seed_dummy.py reset")

    def node(name, typ, lat, lng, cap, parent=None):
        r = api("POST", "/api/nodes", {
            "name": PREFIX + name, "type": typ, "status": "Active", "latitude": lat, "longitude": lng,
            "cluster": "EKO", "area": "BANJARMASIN", "city": "Kota Banjarmasin",
            "capacity": cap, "spec_data": "{}", "parent_node_id": parent})
        return r["id"]

    def cable(name, typ, coords, cap, frm, to, parent=None, inst="Udara"):
        r = api("POST", "/api/cables", {
            "installation": inst,
            "name": PREFIX + name, "type": typ, "status": "Active", "coordinates": coords,
            "cluster": "EKO", "area": "BANJARMASIN", "city": "Kota Banjarmasin",
            "capacity": cap, "core_data": "{}", "parent_cable_id": parent,
            "from_node_id": frm, "to_node_id": to})
        return r["id"]

    def link(fa, fp, ta, tp, via=None, core=None):
        api("POST", "/api/connections", {
            "from_asset_type": "NODE", "from_asset_id": fa, "from_port_core": fp,
            "to_asset_type": "NODE", "to_asset_id": ta, "to_port_core": tp,
            "via_cable_id": via, "via_core": core, "status": "Connected", "notes": "dummy"})

    core = lambda n: f"Tube {(n - 1) // 12 + 1} - Core {n}"
    print("Membuat node...")
    pop = node("POP-BJM", "POP", LAT0, LNG0, "48C")
    jc1 = node("JC-01", "CLOSURE", LAT0, LNG0 + 0.0100, "48C", pop)
    jc2 = node("JC-02", "CLOSURE", LAT0 - 0.0100, LNG0 + 0.0100, "24C", jc1)
    odp_spec = [("ODP-001", LAT0 + 0.0024, LNG0 + 0.0124, jc1), ("ODP-002", LAT0 - 0.0016, LNG0 + 0.0134, jc1),
                ("ODP-003", LAT0 - 0.0116, LNG0 + 0.0124, jc2), ("ODP-004", LAT0 - 0.0126, LNG0 + 0.0074, jc2)]
    odps = {n: (node(n, "ODP", la, ln, "1 In - 8 Out", par), la, ln, par) for n, la, ln, par in odp_spec}
    custs = {}
    for odp_name, (odp_id, la, ln, _) in odps.items():
        for k, (dla, dln) in enumerate([(0.0004, 0.0005), (-0.0004, 0.0007)], start=1):
            cname = f"CUST-{odp_name[-3:]}-{k}"
            custs[cname] = (node(cname, "PELANGGAN", la + dla, ln + dln, "2 Core", odp_id), odp_id, la + dla, ln + dln)
    for i, (la, ln) in enumerate([(LAT0 - 0.0006, LNG0 + 0.0030), (LAT0 - 0.0006, LNG0 + 0.0060)], start=1):
        node(f"TIANG-{i:02d}", "TIANG", la, ln, "Tiang 9m")
    node("SLACK-01", "SLACK", LAT0 - 0.0001, LNG0 + 0.0080, "Slack 20m")
    node("HH-01", "HH", LAT0 - 0.0003, LNG0 + 0.0102, "HH Standar")   # handhole di dekat JC-01 (jalur kabel tanah)
    node("HH-02", "HH", LAT0 - 0.0050, LNG0 + 0.0103, "HH Standar")

    print("Membuat kabel...")
    f1 = cable("FDR-01", "Feeder", [[LNG0, LAT0], [LNG0 + 0.0045, LAT0 - 0.0006], [LNG0 + 0.0100, LAT0]], "48C", pop, jc1)
    f2 = cable("FDR-02", "Feeder", [[LNG0 + 0.0100, LAT0], [LNG0 + 0.0104, LAT0 - 0.0046], [LNG0 + 0.0100, LAT0 - 0.0100]],
               "24C", jc1, jc2, f1, inst="Tanah")
    dist = {}
    dist["ODP-001"] = cable("DIST-01", "Distribution", [[LNG0 + 0.0100, LAT0], [LNG0 + 0.0124, LAT0 + 0.0024]], "12C", jc1, odps["ODP-001"][0], f1)
    dist["ODP-002"] = cable("DIST-02", "Distribution", [[LNG0 + 0.0100, LAT0], [LNG0 + 0.0134, LAT0 - 0.0016]], "12C", jc1, odps["ODP-002"][0], f1)
    dist["ODP-003"] = cable("DIST-03", "Distribution", [[LNG0 + 0.0100, LAT0 - 0.0100], [LNG0 + 0.0124, LAT0 - 0.0116]], "12C", jc2, odps["ODP-003"][0], f2, inst="Tanah")
    dist["ODP-004"] = cable("DIST-04", "Distribution", [[LNG0 + 0.0100, LAT0 - 0.0100], [LNG0 + 0.0074, LAT0 - 0.0126]], "12C", jc2, odps["ODP-004"][0], f2)
    drops = {}
    for cname, (cid, odp_id, la, ln) in custs.items():
        odp_name = "ODP-" + cname[5:8]
        o_id, o_la, o_ln, _ = odps[odp_name]
        drops[cname] = cable("DROP-" + cname[5:], "Drop", [[o_ln, o_la], [ln, la]], "2C", o_id, cid, dist[odp_name])

    print("Merapikan topologi (kabel pemasok node)...")
    api("PUT", f"/api/nodes/{jc1}", {"upstream_cable_id": f1})
    api("PUT", f"/api/nodes/{jc2}", {"upstream_cable_id": f2})
    for odp_name, (o_id, *_r) in odps.items():
        api("PUT", f"/api/nodes/{o_id}", {"upstream_cable_id": dist[odp_name]})
    for cname, (cid, *_r) in custs.items():
        api("PUT", f"/api/nodes/{cid}", {"upstream_cable_id": drops[cname]})

    print("Membuat sambungan core...")
    link(pop, core(1), jc1, core(1), f1, core(1))
    link(jc1, core(2), jc2, core(1), f2, core(1))
    link(jc1, core(3), odps["ODP-001"][0], "IN-1", dist["ODP-001"], core(1))
    link(jc1, core(4), odps["ODP-002"][0], "IN-1", dist["ODP-002"], core(1))
    link(jc2, core(2), odps["ODP-003"][0], "IN-1", dist["ODP-003"], core(1))
    link(jc2, core(3), odps["ODP-004"][0], "IN-1", dist["ODP-004"], core(1))
    for cname, (cid, odp_id, *_r) in custs.items():
        out = "OUT-1" if cname.endswith("-1") else "OUT-2"
        link(odp_id, out, cid, core(1))

    print(f"\nSelesai: 1 POP, 2 closure, 4 ODP, {len(custs)} pelanggan, "
          f"{2 + len(dist) + len(drops)} kabel, {6 + len(custs)} sambungan core.")
    print("Buka peta, cari 'DMY-' di kolom pencarian, atau jalankan:  python seed_dummy.py test")


# ----------------------------------------------------------------------------- skenario uji
def cmd_test():
    R = Report()
    nodes, cables = features("/api/nodes"), features("/api/cables")
    if not by_name(nodes, PREFIX + "POP-BJM"):
        sys.exit("Data dummy belum ada. Jalankan dulu:  python seed_dummy.py seed")
    st = lambda kind, name: by_name(features(f"/api/{kind}"), PREFIX + name)["status"]
    ids = lambda kind, name: by_name(features(f"/api/{kind}"), PREFIX + name)["id"]

    def cable_coords(name):
        for f in api("GET", "/api/cables")["features"]:
            if f["properties"]["name"] == PREFIX + name:
                return f["geometry"]["coordinates"]
        raise SystemExit(f"Kabel {PREFIX}{name} tidak ditemukan")

    R.title("1. Kondisi awal sehat")
    R.check("semua aset dummy berstatus Active",
            all(p["status"] == "Active" for p in nodes + cables if p["name"].startswith(PREFIX)))
    tr = api("GET", f"/api/trace?asset_type=NODE&asset_id={ids('nodes', 'POP-BJM')}")
    R.check("trace dari POP: 8 pelanggan di hilir", tr["summary"]["customers_affected"] == 8, tr["summary"])
    c1 = ids("nodes", "CUST-001-1")
    up = api("GET", f"/api/trace?asset_type=NODE&asset_id={c1}")["upstream"]
    R.check("trace pelanggan CUST-001-1 sampai POP lewat ODP-001 dan JC-01",
            [h["from"]["name"] for h in up] == [PREFIX + "ODP-001", PREFIX + "JC-01", PREFIX + "POP-BJM"],
            [h["from"]["name"] for h in up])

    R.title("2. Gangguan: kabel FDR-01 putus di tengah jalur (tim lapangan melapor)")
    f1 = by_name(cables, PREFIX + "FDR-01")
    coords = cable_coords("FDR-01")
    lat, lng = point_along(coords, 0.5)
    lat += 0.00015   # titik laporan ~17 m dari jalur
    loc = api("POST", "/api/incidents/locate", {"latitude": lat, "longitude": lng})
    R.check("locate: titik dikenali berada di kabel FDR-01", loc["cable"] and loc["cable"]["id"] == f1["id"], loc)
    inc = api("POST", "/api/incidents", {
        "ticket_number": PREFIX + "TKT-001", "title": PREFIX + "Feeder putus kena galian",
        "severity": "Critical", "incident_type": "FO Cut", "status": "Open",
        "description": "Terkena galian PDAM", "latitude": lat, "longitude": lng, "reporter": "Tim NOC"})
    iid = inc["id"]
    R.check("tiket auto-link ke FDR-01 tanpa memilih aset", inc["linked_cable_id"] == f1["id"], inc["notes"])
    cab = inc["location"]["cable"]
    R.check("jarak ke hulu (POP) + jarak ke hilir (JC-01) = panjang kabel",
            abs(cab["upstream"]["distance_m"] + cab["downstream"]["distance_m"] - cab["length_m"]) < 1
            and cab["upstream"]["name"] == PREFIX + "POP-BJM" and cab["downstream"]["name"] == PREFIX + "JC-01", cab)
    print(f"        titik cut: {cab['upstream']['distance_m']:.0f} m dari POP-BJM, "
          f"{cab['downstream']['distance_m']:.0f} m dari JC-01 (acuan OTDR)")
    imp = inc["impact"]["counts"]
    R.check("dampak downstream: 2 closure/JC, 4 ODP, 8 pelanggan", imp["customers"] == 8 and imp["odp"] == 4, imp)
    R.check("aset hilir Cut/Broken, POP (hulu) tetap Active",
            st("nodes", "JC-02") == "Cut/Broken" and st("nodes", "ODP-004") == "Cut/Broken"
            and st("nodes", "CUST-004-2") == "Cut/Broken" and st("nodes", "POP-BJM") == "Active")

    R.title("3. Perbaikan SEMENTARA: tim memasang closure di titik putus")
    n_cab_before = len(features("/api/cables"))
    rp = api("POST", f"/api/incidents/{iid}/repairs", {
        "kind": "TEMPORARY", "action": "ADD_CLOSURE", "name": PREFIX + "JC-TEMP-01",
        "technician": "Andi", "notes": "Splice darurat 4 core prioritas"})
    R.check("status tiket jadi 'Temporary Fix' (tetap terbuka)", rp["status"] == "Temporary Fix")
    nodes = features("/api/nodes")
    jt = by_name(nodes, PREFIX + "JC-TEMP-01")
    R.check("peta ter-update: closure baru ada tepat di titik putus",
            jt and jt["type"] == "CLOSURE" and jt["id"] == rp["node_id"])
    R.check("kabel FDR-01 terpecah menjadi 2 segmen", len(features("/api/cables")) == n_cab_before + 1
            and by_name(features("/api/cables"), PREFIX + "FDR-01-seg2") is not None)
    conns = api("GET", f"/api/connections?asset_type=NODE&asset_id={rp['node_id']}")
    R.check("core tersambung lewat closure (1 masuk dari POP, 1 keluar ke JC-01)", len(conns) == 2, conns)
    up = api("GET", f"/api/trace?asset_type=NODE&asset_id={ids('nodes', 'JC-01')}")["upstream"]
    R.check("trace JC-01 kini: closure baru -> POP", [h["from"]["name"] for h in up][:2] == [PREFIX + "JC-TEMP-01", PREFIX + "POP-BJM"],
            [h["from"]["name"] for h in up])
    R.check("layanan pulih: JC-02, ODP-004, pelanggan kembali Active",
            st("nodes", "JC-02") == "Active" and st("nodes", "ODP-004") == "Active" and st("nodes", "CUST-004-2") == "Active")
    d = api("GET", f"/api/incidents/{iid}")
    R.check("riwayat tiket mencatat dibuat, dampak, perbaikan, perubahan status",
            {"CREATED", "IMPACT_APPLIED", "REPAIR", "STATUS_CHANGED"} <= {e["event_type"] for e in d["events"]},
            [e["event_type"] for e in d["events"]])
    api("POST", f"/api/incidents/{iid}/events", {"message": "Teknisi on-site, redaman splice 0.05 dB", "actor": "Andi"})

    R.title("4. Perbaikan PERMANEN di titik yang sama (memakai closure yang sudah ada)")
    rp2 = api("POST", f"/api/incidents/{iid}/repairs", {
        "kind": "PERMANENT", "action": "ADD_CLOSURE", "technician": "Budi", "notes": "Closure dipasang permanen + fusion ulang"})
    R.check("closure tidak dobel (dipakai ulang), tiket Resolved",
            rp2["reused_existing_node"] and rp2["node_id"] == rp["node_id"] and rp2["status"] == "Resolved")

    R.title("5. Gangguan kedua: DIST-03 putus, perbaikan permanen dengan extra joint")
    lat3, lng3 = point_along(cable_coords("DIST-03"), 0.4)
    inc2 = api("POST", "/api/incidents", {
        "ticket_number": PREFIX + "TKT-002", "title": PREFIX + "Distribusi ODP-003 putus", "severity": "Major",
        "incident_type": "FO Cut", "latitude": lat3, "longitude": lng3})
    R.check("dampak: ODP-003 + 2 pelanggan saja, ODP lain tidak terdampak",
            inc2["impact"]["counts"]["customers"] == 2 and st("nodes", "ODP-003") == "Cut/Broken" and st("nodes", "ODP-001") == "Active",
            inc2["impact"]["counts"])
    rj = api("POST", f"/api/incidents/{inc2['id']}/repairs", {
        "kind": "PERMANENT", "action": "EXTRA_JOINT", "name": PREFIX + "JT-TKT-002",
        "technician": "Citra", "notes": "Extra joint 12 core"})
    R.check("joint tambahan membuat node baru 'JT-TKT-002' & tiket Resolved",
            by_name(features("/api/nodes"), PREFIX + "JT-TKT-002") is not None and rj["status"] == "Resolved")
    R.check("ODP-003 pulih Active", st("nodes", "ODP-003") == "Active")

    R.title("6. Gangguan level core: redaman tinggi hanya pada core 1 kabel FDR-02")
    inc3 = api("POST", "/api/incidents", {
        "ticket_number": PREFIX + "TKT-003", "title": PREFIX + "Redaman tinggi core 1 FDR-02", "severity": "Minor",
        "incident_type": "Fiber Degradation", "latitude": LAT0 - 0.005, "longitude": LNG0 + 0.0104,
        "linked_cable_id": ids("cables", "FDR-02"), "affected_cores": [1]})
    R.check("hanya yang disuplai core 1 yang terdampak (JC-02 & hilirnya), kabel FDR-02 tidak ditandai putus",
            st("nodes", "JC-02") == "Cut/Broken" and st("cables", "FDR-02") == "Active" and st("nodes", "ODP-001") == "Active",
            inc3["impact"]["counts"] if inc3["impact"] else None)
    api("PUT", f"/api/incidents/{inc3['id']}/status", {"status": "Resolved", "actor": "Andi", "note": "Core diganti"})
    R.check("resolve: JC-02 kembali Active", st("nodes", "JC-02") == "Active")

    R.title("7. Status manual 'Cut/Broken' dari popup otomatis tercatat sebagai tiket")
    before = len(features("/api/incidents"))
    odp1 = ids("nodes", "ODP-001")
    api("PUT", f"/api/nodes/{odp1}/status", {"status": "Cut/Broken"})
    incs = features("/api/incidents")
    auto = [p for p in incs if p.get("source") == "AUTO" and "ODP-001" in (p["title"] or "") and p["status"] == "Open"]
    R.check("tiket AUTO dibuat + pelanggan hilir ikut terdampak", len(incs) == before + 1 and len(auto) == 1
            and st("nodes", "CUST-001-1") == "Cut/Broken")
    api("PUT", f"/api/nodes/{odp1}/status", {"status": "Active"})
    R.check("dipulihkan manual: tiket AUTO ditutup & pelanggan pulih",
            st("nodes", "CUST-001-1") == "Active"
            and [p for p in features("/api/incidents") if p["id"] == auto[0]["id"]][0]["status"] == "Resolved")

    R.title("8. Validasi & integritas")
    def rejects(code, method, path, body):
        try:
            api(method, path, body)
            return False
        except ApiError as e:
            return e.status == code
    R.check("tiket dengan nomor ganda ditolak (409)", rejects(409, "POST", "/api/incidents", {
        "ticket_number": PREFIX + "TKT-001", "title": "x", "severity": "Minor", "incident_type": "FO Cut",
        "latitude": LAT0, "longitude": LNG0}))
    R.check("perbaikan pada tiket Resolved ditolak (409)",
            rejects(409, "POST", f"/api/incidents/{iid}/repairs", {"kind": "TEMPORARY", "action": "OTHER"}))
    R.check("port core yang sama tidak bisa dipakai dua kali (409)", rejects(409, "POST", "/api/connections", {
        "from_asset_type": "NODE", "from_asset_id": ids("nodes", "POP-BJM"), "from_port_core": "Tube 1 - Core 1",
        "to_asset_type": "NODE", "to_asset_id": ids("nodes", "JC-02"), "to_port_core": "Tube 1 - Core 5"}))
    open_dummy = [p for p in features("/api/incidents")
                  if p["status"] != "Resolved" and (PREFIX in (p["title"] or "") or (p["ticket_number"] or "").startswith(PREFIX))]
    R.check("semua tiket skenario berakhir Resolved (tidak ada yang menggantung)", not open_dummy, open_dummy)

    print(f"\nHASIL: {R.ok} lulus, {R.bad} gagal")
    print("Lihat hasilnya di peta (marker closure baru, kabel -seg2) dan NOC Monitor -> tombol Detail.")
    sys.exit(1 if R.bad else 0)


# ----------------------------------------------------------------------------- main
def main():
    global BASE
    ap = argparse.ArgumentParser(description="Data dummy & skenario uji NETGIS")
    ap.add_argument("command", choices=["seed", "test", "all", "reset"])
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--all", action="store_true", help="reset: hapus SEMUA data, bukan hanya DMY-*")
    ap.add_argument("--yes", action="store_true", help="lewati konfirmasi")
    ap.add_argument("--user", default=os.environ.get("NETGIS_USER", "admin"), help="username (atau env NETGIS_USER)")
    ap.add_argument("--password", default=os.environ.get("NETGIS_PASSWORD"), help="password (atau env NETGIS_PASSWORD; kosong = ditanya)")
    args = ap.parse_args()
    BASE = args.base.rstrip("/")
    login(args.user, args.password or getpass.getpass(f"Password {args.user}: "))

    if args.command == "reset":
        cmd_reset(args.all, args.yes)
    elif args.command == "seed":
        cmd_seed()
    elif args.command == "test":
        cmd_test()
    else:
        cmd_reset(False, True)
        cmd_seed()
        cmd_test()


if __name__ == "__main__":
    main()