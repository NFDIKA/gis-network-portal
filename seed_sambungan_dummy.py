#!/usr/bin/env python3
"""Seed sambungan core + tiket contoh untuk dataset DUMMY Banjarmasin (setelah impor aset & kabel).
Pakai:  python3 seed_sambungan_dummy.py --url http://127.0.0.1:8765 --user admin --password '...'
Hanya pustaka standar Python. Aman dijalankan ulang (sambungan yang sudah ada dilewati)."""
import json, argparse, urllib.request, urllib.error, http.cookiejar, re, sys
ap = argparse.ArgumentParser()
ap.add_argument('--url', default='http://127.0.0.1:8765'); ap.add_argument('--user', default='admin'); ap.add_argument('--password', required=True)
ap.add_argument('--tiket', type=int, default=0, help='jumlah tiket contoh (0 = tanpa tiket)')
a = ap.parse_args()
cj = http.cookiejar.CookieJar(); op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
def rq(p, b=None):
    r = urllib.request.Request(a.url + p, json.dumps(b).encode() if b is not None else None, {'Content-Type': 'application/json'})
    try: return json.load(op.open(r)), 200
    except urllib.error.HTTPError as e:
        try: return json.loads(e.read().decode()), e.code
        except Exception: return {}, e.code
d, c = rq('/api/auth/login', {'username': a.user, 'password': a.password})
if c != 200: sys.exit(f'Login gagal: {d}')
nodes = {f['properties']['name']: f['properties']['id'] for f in rq('/api/nodes')[0]['features']}
cabs = {f['properties']['name']: f['properties']['id'] for f in rq('/api/cables')[0]['features']}
def core(i): return f"Tube {(i - 1) // 12 + 1} - Core {i}"
J = lambda i: core(i)
ok = skip = bad = 0
def conn(fn, fp, tn, tp, cab, vc):
    global ok, skip, bad
    d, c = rq('/api/connections', {'from_asset_type': 'NODE', 'from_asset_id': nodes[fn], 'from_port_core': fp,
                                   'to_asset_type': 'NODE', 'to_asset_id': nodes[tn], 'to_port_core': tp,
                                   'via_cable_id': cabs[cab], 'via_core': vc})
    if c == 200: ok += 1
    elif c == 409: skip += 1
    else: bad += 1; print('  GAGAL', fn, '->', tn, c, d.get('detail'))
# port POP: OTB-1 P01..P48 lalu OTB-2 P01..P48 ; port terakhir (OTB-2 / P48) dicadangkan untuk backbone
PORTS = [f"OTB-{o} / P{p:02d}" for o in (1, 2) for p in range(1, 49)]
BB_PORT = PORTS[-1]
if 'BB-BJM-01-1' in cabs:
    conn('POP-BJM-01', BB_PORT, 'SLK-BJM-BB-01', J(1), 'BB-BJM-01-1', core(1))
    conn('SLK-BJM-BB-01', J(1), 'SLK-BJM-BB-02', J(1), 'BB-BJM-01-2', core(1))
    conn('SLK-BJM-BB-02', J(1), 'POP-BJM-02', BB_PORT, 'BB-BJM-01-3', core(1))
# peta ODP -> pelanggan lewat kabel drop
id2n = {v: k for k, v in nodes.items()}
drops = {}
for f in rq('/api/cables')[0]['features']:
    p = f['properties']
    if p['name'].startswith('DRP-'):
        drops.setdefault(id2n[p['from_node_id']], []).append((p['name'], id2n[p['to_node_id']]))
pops = {'A': 'POP-BJM-01', 'B': 'POP-BJM-02'}
nport = {'A': 0, 'B': 0}
for code in sorted({m.group(1) for n in nodes for m in [re.match(r'CL-BJM-([AB]\d\d)$', n)] if m}):
    L = code[0]; pop = pops[L]; cl = f"CL-BJM-{code}"
    o = 0
    while f"ODP-BJM-{code}-{o + 1:02d}" in nodes:
        o += 1; odp = f"ODP-BJM-{code}-{o:02d}"
        pp = PORTS[nport[L]]; nport[L] += 1
        if nport[L] > 95: sys.exit('Port POP habis')
        prev, pport = pop, pp
        seg = 1
        while f"FDR-BJM-{code}-{seg}" in cabs:
            nxt = f"SLK-BJM-{code}-{seg:02d}" if f"SLK-BJM-{code}-{seg:02d}" in nodes else cl
            conn(prev, pport, nxt, J(o), f"FDR-BJM-{code}-{seg}", core(o))
            prev, pport = nxt, J(o); seg += 1
        conn(cl, J(o), odp, 'IN-1', f"DST-BJM-{code}-{o:02d}", core(1))
        for i, (dc, cust) in enumerate(sorted(drops.get(odp, []))):
            conn(odp, f"OUT-{i + 1}", cust, 'Tube 1 - Core 1', dc, 'Tube 1 - Core 1')
print(f'Sambungan core: dibuat={ok} dilewati={skip} gagal={bad}')
# tiket contoh (opsional)
if a.tiket:
    ex = [('BJM-T-001', 'Fiber Degradation', 'Major', 'FDR-BJM-A01-1', 'Redaman naik pada feeder A01'),
          ('BJM-T-002', 'FO Cut', 'Critical', 'DST-BJM-B01-01', 'Kabel distribusi putus tertabrak truk'),
          ('BJM-T-003', 'Network Issue', 'Minor', 'DST-BJM-A02-02', 'Pelanggan intermiten di ODP A02-02')]
    feats = {f['properties']['name']: f for f in rq('/api/cables')[0]['features']}
    for t, ty, sv, cb, ttl in ex[:a.tiket]:
        if cb not in feats: continue
        co = feats[cb]['geometry']['coordinates']; m = co[len(co) // 2]
        d, c = rq('/api/incidents', {'ticket_number': t, 'title': ttl, 'severity': sv, 'incident_type': ty,
                                     'latitude': m[1], 'longitude': m[0], 'linked_cable_id': feats[cb]['properties']['id'],
                                     'description': 'Tiket contoh dari dataset dummy'})
        print('Tiket', t, c)
