#!/usr/bin/env python3
"""Seed DAYA OPTIK (Tx / Rx dBm) pada port OTB di POP untuk dataset DUMMY Banjarmasin.
Tiga jenis data: TX saja, RX saja, TX+RX (termasuk port kosong, nilai di bawah batas / terlalu kuat, dan riwayat untuk tren).
Pakai:  python3 seed_daya_otb_dummy.py --url http://127.0.0.1:8765 --user admin --password '...'
        python3 seed_daya_otb_dummy.py ... --hapus     (hapus semua catatan daya dummy ini)
Hanya pustaka standar Python. Butuh dataset kecil + sambungan (seed_sambungan_dummy.py) sudah ada.
Catatan: pada POP/OLT (sisi sumber) status Rx tidak dinilai (tanda –); yang tampil adalah nilai Tx/Rx, Rx sebelumnya, dan tren.
Menjalankan ulang aman: nilai diperbarui, hanya riwayat ukur yang bertambah."""
import json, argparse, urllib.request, urllib.error, http.cookiejar, sys
ap = argparse.ArgumentParser()
ap.add_argument('--url', default='http://127.0.0.1:8765'); ap.add_argument('--user', default='admin'); ap.add_argument('--password', required=True)
ap.add_argument('--hapus', action='store_true', help='hapus catatan daya dummy')
a = ap.parse_args()
cj = http.cookiejar.CookieJar(); op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
def rq(method, p, b=None):
    r = urllib.request.Request(a.url + p, json.dumps(b).encode() if b is not None else None, {'Content-Type': 'application/json'}, method=method)
    try: return json.load(op.open(r)), 200
    except urllib.error.HTTPError as e:
        try: return json.loads(e.read().decode()), e.code
        except Exception: return {}, e.code
d, c = rq('POST', '/api/auth/login', {'username': a.user, 'password': a.password})
if c != 200: sys.exit(f'Login gagal: {d}')
nodes = {f['properties']['name']: f['properties']['id'] for f in rq('GET', '/api/nodes')[0]['features']}
# (POP, port OTB, Tx dBm, Rx dBm, nilai sebelumnya (tx, rx) untuk tren, catatan skenario)
DATA = [
    ('POP-BJM-01', 'OTB-1 / P01',  2.60, None,  None,          'TX saja'),
    ('POP-BJM-01', 'OTB-1 / P02',  3.10, None,  None,          'TX saja'),
    ('POP-BJM-01', 'OTB-1 / P03',  1.80, None,  None,          'TX saja'),
    ('POP-BJM-01', 'OTB-1 / P04',  None, -18.40, None,         'RX saja, normal'),
    ('POP-BJM-01', 'OTB-1 / P05',  None, -25.90, (None, -23.10), 'RX saja + riwayat: tren turun 2.8 dB (badge Cek)'),
    ('POP-BJM-01', 'OTB-1 / P06',  None, -29.50, None,         'RX saja, nilai rendah (-29.5 dBm; di bawah batas -27 untuk sisi penerima)'),
    ('POP-BJM-01', 'OTB-1 / P10',  2.00, -20.00, None,         'TX+RX pada port KOSONG'),
    ('POP-BJM-01', 'OTB-2 / P48',  1.50, -9.20, (1.50, -8.90), 'TX+RX backbone + riwayat: tren turun 0.3 dB'),
    ('POP-BJM-02', 'OTB-1 / P01',  2.40, -17.80, None,         'TX+RX normal'),
    ('POP-BJM-02', 'OTB-1 / P02',  2.90, None,  None,          'TX saja'),
    ('POP-BJM-02', 'OTB-1 / P03',  None, -21.30, None,         'RX saja, normal'),
    ('POP-BJM-02', 'OTB-1 / P04',  2.20, -26.40, None,         'TX+RX, Rx rendah (-26.4 dBm)'),
    ('POP-BJM-02', 'OTB-1 / P05',  None, -6.50, None,          'RX saja, nilai tinggi (-6.5 dBm; di atas -8 untuk sisi penerima)'),
    ('POP-BJM-02', 'OTB-1 / P06',  0.50, None,  None,          'TX saja, daya kirim rendah'),
    ('POP-BJM-02', 'OTB-2 / P48',  1.40, -9.00, None,          'TX+RX backbone'),
]
ok = bad = 0
def put(pop, port, tx, rx, note):
    global ok, bad
    d, c = rq('PUT', '/api/power', {'asset_type': 'NODE', 'asset_id': nodes[pop], 'port_core': port, 'tx_dbm': tx, 'rx_dbm': rx,
                                    'note': 'dummy: ' + note, 'device': 'Power meter dummy'})
    if c == 200: ok += 1
    else: bad += 1; print('  GAGAL', pop, port, c, d.get('detail'))
for pop, port, tx, rx, prev, note in DATA:
    if pop not in nodes: print('  lewati (aset tidak ada):', pop); continue
    if a.hapus: put(pop, port, None, None, note); continue
    if prev: put(pop, port, prev[0], prev[1], 'pengukuran sebelumnya')
    put(pop, port, tx, rx, note)
print(('Dihapus' if a.hapus else 'Disimpan') + f': {ok} catatan, gagal {bad}')