// --- INISIALISASI PETA ---
const map = L.map("map", { zoomControl: false }).setView([-3.323, 114.593], 14);
L.control.zoom({ position: "topleft" }).addTo(map);

// Popup yang terbuka dekat tepi akan menggeser peta dengan jarak aman dari overlay
// (kartu metrik & kotak pencarian), sehingga isi popup tidak tertutup.
L.Popup.mergeOptions({
  autoPanPaddingTopLeft:
    window.matchMedia && window.matchMedia("(max-width: 768px)").matches
      ? [16, 72]
      : [70, 90],
  autoPanPaddingBottomRight: [16, 16],
});

const osmLayer = L.tileLayer(
  "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
  { maxZoom: 19 },
).addTo(map);
const googleSatLayer = L.tileLayer(
  "https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}",
  { maxZoom: 20 },
);

// --- DEKLARASI LAYER KATEGORI ---
const popGroup = L.layerGroup().addTo(map);
const odpGroup = L.layerGroup().addTo(map);
const closureGroup = L.layerGroup().addTo(map);
const pelangganGroup = L.layerGroup().addTo(map);
const tiangGroup = L.layerGroup().addTo(map);
const slackGroup = L.layerGroup().addTo(map);
const hhGroup = L.layerGroup().addTo(map);
const incidentGroup = L.layerGroup().addTo(map);
// Insiden yang tidak mengubah status aset & disembunyikan: tidak tampil, bisa dinyalakan lewat kontrol layer
const hiddenIncidentGroup = L.layerGroup();

const backboneGroup = L.layerGroup().addTo(map);
const feederGroup = L.layerGroup().addTo(map);
const distGroup = L.layerGroup().addTo(map);
const dropGroup = L.layerGroup().addTo(map);
// Layer hasil ukur OTDR (warna & garis putus-putus berbeda dari jenis kabel); bisa disembunyikan lewat kontrol layer
const otdrGroup = L.layerGroup();

const ALL_GROUPS = [
  popGroup,
  odpGroup,
  closureGroup,
  pelangganGroup,
  tiangGroup,
  slackGroup,
  hhGroup,
  incidentGroup,
  hiddenIncidentGroup,
  backboneGroup,
  feederGroup,
  distGroup,
  dropGroup,
];

// FeatureGroup gabungan untuk kebutuhan DRAG & DROP / EDITING
const editableGroup = L.featureGroup().addTo(map);

// ===================== FOLDER ASET (pohon bersubfolder seperti Google Earth) =====================
// FT.list = daftar jalur folder dari server; FT.off = folder yang dicentang MATI (tidak digambar di peta).
// Centang dipakai bersama oleh panel Layer di peta dan pohon di Inventory.
const FT = {
  list: [],
  tree: null,
  itree: null,
  icounts: null,
  open: new Set(),
  off: new Set(),
  sel: "",
  openInit: false,
};
try {
  const saved = JSON.parse(localStorage.getItem("netgis.folderOff") || "[]");
  if (Array.isArray(saved)) saved.forEach((p) => FT.off.add(String(p)));
} catch (_) {
  /* penyimpanan browser tidak tersedia: abaikan */
}
function ftSaveOff() {
  try {
    localStorage.setItem("netgis.folderOff", JSON.stringify([...FT.off]));
  } catch (_) {
    /* abaikan */
  }
}
const FT_ICON =
  '<svg class="ft-ico" viewBox="0 0 24 24"><path d="M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';

function ftMakeTree(list, countOf) {
  const root = {
    name: "",
    path: "",
    kids: new Map(),
    own: 0,
    total: 0,
    direct: false,
  };
  list.forEach((f) => {
    let n = root;
    const acc = [];
    String(f.path || "")
      .split("/")
      .filter(Boolean)
      .forEach((part) => {
        acc.push(part);
        if (!n.kids.has(part))
          n.kids.set(part, {
            name: part,
            path: acc.join("/"),
            kids: new Map(),
            own: 0,
            total: 0,
            direct: false,
          });
        n = n.kids.get(part);
      });
    if (n !== root) {
      n.own = countOf(f);
      n.direct = true;
    }
  });
  const sum = (n) => {
    n.total = n.own;
    n.kids.forEach((k) => {
      n.total += sum(k);
    });
    return n.total;
  };
  sum(root);
  return root;
}
function ftBuild() {
  FT.tree = ftMakeTree(FT.list, (f) => (f.nodes || 0) + (f.cables || 0));
  ftBuildInv();
  if (!FT.openInit) {
    FT.openInit = true;
    let sv = null;
    try {
      sv = JSON.parse(localStorage.getItem("netgis.folderOpen") || "null");
    } catch (_) {
      /* abaikan */
    }
    if (Array.isArray(sv)) sv.forEach((p) => FT.open.add(String(p)));
    else FT.tree.kids.forEach((k) => FT.open.add(k.path));
  }
  if (FT.sel && !ftFind(FT.sel)) FT.sel = "";
}
// pohon di Inventory: jumlah = aset yang LOLOS filter aktif (jenis, status, cari, wilayah); bila belum dimuat = jumlah total
function ftBuildInv() {
  FT.itree = FT.icounts
    ? ftMakeTree(FT.list, (f) => FT.icounts.get(f.path) || 0)
    : FT.tree;
}
function ftFind(path) {
  let n = FT.tree;
  if (!n) return null;
  for (const part of String(path || "")
    .split("/")
    .filter(Boolean)) {
    n = n.kids.get(part);
    if (!n) return null;
  }
  return n;
}
function ftWalk(n, fn) {
  fn(n);
  n.kids.forEach((k) => ftWalk(k, fn));
}
function ftState(n) {
  let on = 0,
    off = 0;
  ftWalk(n, (m) => {
    if (m.direct) {
      if (FT.off.has(m.path)) off++;
      else on++;
    }
  });
  return on && off ? 0.5 : off ? 0 : 1;
}
function ftSet(n, v) {
  ftWalk(n, (m) => {
    if (m.direct) {
      if (v) FT.off.delete(m.path);
      else FT.off.add(m.path);
    }
  });
  ftAfterToggle();
}
function ftAll(v) {
  if (!FT.tree) return;
  FT.tree.kids.forEach((k) => ftSet0(k, v));
  ftAfterToggle();
}
function ftSet0(n, v) {
  ftWalk(n, (m) => {
    if (m.direct) {
      if (v) FT.off.delete(m.path);
      else FT.off.add(m.path);
    }
  });
}
function ftAfterToggle() {
  ftSaveOff();
  virtSyncNow();
  ftRenderAll();
}
function ftMatch(n, q) {
  return (
    !q ||
    n.name.toLowerCase().includes(q) ||
    [...n.kids.values()].some((k) => ftMatch(k, q))
  );
}
const FT_EYE =
  '<svg viewBox="0 0 24 24"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>';
const FT_EYE_OFF =
  '<svg viewBox="0 0 24 24"><path d="M3 3l18 18"/><path d="M10.6 6.1A10.6 10.6 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.2 4.1M6.6 6.6A17 17 0 0 0 2 12s3.6 7 10 7a10 10 0 0 0 4.4-1"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/></svg>';
function ftEyeHtml(st) {
  const cls = st === 0 ? "off" : st === 0.5 ? "half" : "on";
  const tip =
    st === 0
      ? "Disembunyikan dari peta: klik untuk menampilkan"
      : st === 0.5
        ? "Sebagian subfolder tampil di peta: klik untuk menyembunyikan semua"
        : "Tampil di peta: klik untuk menyembunyikan";
  return `<button type="button" class="ft-eye ${cls}" data-st="${st}" title="${tip}" aria-label="${tip}">${st === 0 ? FT_EYE_OFF : FT_EYE}</button>`;
}
function ftNodeHtml(n, q, showChk, showInv) {
  if (!ftMatch(n, q)) return "";
  const st = showChk ? ftState(n) : 1;
  const kids = [...n.kids.values()].sort((a, b) =>
    a.name.localeCompare(b.name, undefined, {
      numeric: true,
      sensitivity: "base",
    }),
  );
  const open = q ? true : FT.open.has(n.path);
  const p = encodeURIComponent(n.path);
  return (
    `<div class="ft-row${FT.sel === n.path ? " sel" : ""}${showChk && st === 0 ? " hid" : ""}${n.total === 0 && FT.icounts && showInv ? " zero" : ""}" data-p="${p}"${showInv && ftCanW() ? ' draggable="true"' : ""} title="${escapeHtml(n.path)}">` +
    `<span class="ft-car${open ? " o" : ""}" style="visibility:${kids.length ? "visible" : "hidden"}">&#9654;</span>` +
    (showChk
      ? showInv
        ? ftEyeHtml(st)
        : `<input type="checkbox" class="ft-chk" ${st === 1 ? "checked" : ""} data-ind="${st === 0.5 ? 1 : 0}" title="Tampilkan di peta">`
      : "") +
    `<svg class="ft-ico${kids.length ? " has" : ""}" viewBox="0 0 24 24"><path d="M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>` +
    `<span class="ft-n">${escapeHtml(n.name)}</span><span class="ft-cnt">${Number(n.total)}</span></div>` +
    (kids.length
      ? `<div class="ft-kids${open ? "" : " h"}">${kids.map((k) => ftNodeHtml(k, q, showChk, showInv)).join("")}</div>`
      : "")
  );
}
function ftPaint(el, q, withRoot) {
  if (!el) return;
  const tree = withRoot ? FT.itree || FT.tree : FT.tree;
  if (!tree || !tree.kids.size) {
    el.innerHTML = '<div class="ft-empty">Belum ada folder</div>';
    return;
  }
  const kids = [...tree.kids.values()].sort((a, b) =>
    a.name.localeCompare(b.name, undefined, {
      numeric: true,
      sensitivity: "base",
    }),
  );
  const root = withRoot
    ? `<div class="ft-row ft-rootrow${FT.sel === "" ? " sel" : ""}" data-p=""><span class="ft-car" style="visibility:hidden">&#9654;</span>${FT_ICON}<span class="ft-n">Semua Aset</span><span class="ft-cnt">${Number(tree.total)}</span></div>`
    : "";
  el.innerHTML =
    root + kids.map((k) => ftNodeHtml(k, q, true, withRoot)).join("");
  if (el.querySelectorAll)
    el.querySelectorAll("input.ft-chk[data-ind='1']").forEach((i) => {
      i.indeterminate = true;
    });
}
// jumlah per folder menurut filter inventaris aktif
let ftCountToken = 0;
function ftLoadCounts(params) {
  const tk = ++ftCountToken;
  const q = new URLSearchParams();
  ["q", "cluster", "area", "type", "status", "installation"].forEach((k) =>
    q.set(k, params.get(k) || (k === "q" ? "" : "ALL")),
  );
  return apiRequest("/api/folders/counts?" + q)
    .then((r) => {
      if (tk !== ftCountToken) return;
      FT.icounts = new Map(
        ((r && r.folders) || []).map((f) => [f.path, f.count]),
      );
      ftBuildInv();
      ftRenderAll();
    })
    .catch(() => {
      /* hitungan tambahan: abaikan */
    });
}
function ftRenderAll() {
  const inv = document.getElementById("inv-folder-tree");
  if (inv)
    ftPaint(
      inv,
      ((document.getElementById("ft-search") || {}).value || "")
        .trim()
        .toLowerCase(),
      true,
    );
  const lc = document.getElementById("lc-folder-tree");
  if (lc) ftPaint(lc, "", false);
  ftBindDnd();
  const ca = document.getElementById("ft-tools");
  let canW = false;
  try {
    canW = can("asset.write");
  } catch (_) {
    /* belum login */
  }
  if (ca) ca.style.display = canW ? "" : "none";
}
function ftClick(ev, mode) {
  if (ev.stopPropagation) ev.stopPropagation(); // pohon dirender ulang saat klik: jangan sampai terbaca sebagai klik di peta
  const t = ev.target;
  const row = t && t.closest ? t.closest(".ft-row") : null;
  if (!row) return;
  const path = decodeURIComponent(row.dataset.p || "");
  const n = path ? ftFind(path) : FT.tree;
  if (!n) return;
  if (t.closest(".ft-chk")) {
    ftSet(n, t.checked);
    return;
  }
  if (t.closest(".ft-eye")) {
    ftSet(n, ftState(n) === 0);
    return;
  }
  if (t.closest(".ft-car")) {
    if (FT.open.has(path)) FT.open.delete(path);
    else FT.open.add(path);
    ftSaveOpen();
    ftRenderAll();
    return;
  }
  if (mode === "inv") {
    FT.sel = FT.sel === path ? "" : path;
    if (FT.sel) FT.open.add(FT.sel);
    inventoryState.page = 1;
    ftRenderAll();
    renderInventoryTable();
  } else if (path) {
    ftFitFolder(path);
  }
}
function ftFitFolder(path) {
  let s = 90,
    w = 180,
    n = -90,
    e = -180,
    any = false;
  VIRT.items.forEach((it) => {
    if (!(it.folder === path || it.folder.startsWith(path + "/"))) return;
    const bb = it.kind === "node" ? [it.lat, it.lng, it.lat, it.lng] : it.bb;
    s = Math.min(s, bb[0]);
    w = Math.min(w, bb[1]);
    n = Math.max(n, bb[2]);
    e = Math.max(e, bb[3]);
    any = true;
  });
  if (any && typeof map.fitBounds === "function")
    map.fitBounds(
      [
        [s, w],
        [n, e],
      ],
      { padding: [40, 40], maxZoom: 18 },
    );
}
function ftLoad() {
  return apiRequest("/api/folders")
    .then((r) => {
      FT.list = (r && r.folders) || [];
      ftBuild();
      ftRenderAll();
    })
    .catch((err) => console.warn("Folder tidak dimuat:", err && err.message));
}
function ftTogglePanel() {
  const sp = document.getElementById("inv-split");
  if (sp) sp.classList.toggle("nofolders");
}
// --- tindakan folder (izin asset.write) ---
function ftErr(err) {
  alert("Folder: " + ((err && err.message) || err));
}
function ftAfterChange() {
  return Promise.all([ftLoad(), loadData()]).then(() =>
    renderInventoryIfOpen(),
  );
}
function ftNewFolder() {
  const base = FT.sel ? FT.sel + "/" : "";
  const name = prompt(
    FT.sel ? `Nama subfolder baru di "${FT.sel}":` : "Nama folder baru:",
    "",
  );
  if (!name || !name.trim()) return;
  apiRequest("/api/folders", "POST", { path: base + name.trim() })
    .then((r) => {
      FT.sel = r.path || base + name.trim();
      return ftLoad();
    })
    .then(() => renderInventoryTable())
    .catch(ftErr);
}
function ftRename() {
  if (!FT.sel) return alert("Pilih folder yang akan diganti namanya dulu.");
  const parts = FT.sel.split("/");
  const cur = parts.pop();
  const nn = prompt(`Ganti nama folder "${FT.sel}" menjadi:`, cur);
  if (!nn || !nn.trim() || nn.trim() === cur) return;
  const np = (parts.length ? parts.join("/") + "/" : "") + nn.trim();
  const oldSel = FT.sel;
  apiRequest("/api/folders/rename", "POST", { path: oldSel, new_path: np })
    .then((r) => {
      ftRemapPaths(oldSel, r.path || np);
      return ftAfterChange();
    })
    .catch(ftErr);
}
function ftDelete() {
  if (!FT.sel) return alert("Pilih folder kosong yang akan dihapus dulu.");
  if (
    !confirm(
      `Hapus folder kosong "${FT.sel}"? (Aset tidak ikut terhapus; folder yang berisi aset tidak bisa dihapus.)`,
    )
  )
    return;
  apiRequest("/api/folders?path=" + encodeURIComponent(FT.sel), "DELETE")
    .then(() => {
      FT.sel = "";
      return ftLoad();
    })
    .then(() => renderInventoryTable())
    .catch(ftErr);
}
function ftMoveDialog() {
  const items = [...invSel.values()];
  if (!items.length) return;
  const old = document.getElementById("ft-move");
  if (old) old.remove();
  const paths = FT.list.map((f) => f.path);
  const wrap = document.createElement("div");
  wrap.id = "ft-move";
  wrap.className = "modal-overlay";
  wrap.style.display = "flex";
  wrap.style.zIndex = "5000";
  wrap.innerHTML =
    `<div class="modal-container ft-move-box"><div class="modal-header"><h3><i class="fa-solid fa-folder-tree"></i> Pindahkan ${items.length} aset ke folder</h3>` +
    `<button class="btn-close" type="button" onclick="document.getElementById('ft-move').remove()">&times;</button></div>` +
    `<div class="modal-body"><label class="ft-lbl">Pilih folder tujuan</label><select id="ft-move-sel" size="8" class="ft-move-list">` +
    paths
      .map(
        (p) =>
          `<option value="${escapeHtml(p)}"${p === FT.sel ? " selected" : ""}>${escapeHtml(p)}</option>`,
      )
      .join("") +
    `</select>` +
    `<label class="ft-lbl">atau ketik folder baru (gunakan / untuk subfolder)</label><input id="ft-move-new" type="text" class="ft-move-in" placeholder="mis. Proyek 2026/Tahap 1">` +
    `<div class="ft-move-act"><button type="button" class="inv-bar-btn" onclick="document.getElementById('ft-move').remove()">Batal</button>` +
    `<button type="button" class="inv-bar-btn primary" onclick="ftMoveGo()"><i class="fa-solid fa-folder-open"></i> Pindahkan</button></div></div></div>`;
  document.body.appendChild(wrap);
}
function ftMoveGo() {
  const typed = (document.getElementById("ft-move-new") || {}).value || "";
  const picked = (document.getElementById("ft-move-sel") || {}).value || "";
  const folder = (typed.trim() || picked).trim();
  if (!folder) return alert("Pilih atau ketik folder tujuan.");
  const items = [...invSel.values()];
  const body = {
    folder,
    nodes: items.filter((x) => x.kind === "node").map((x) => x.id),
    cables: items.filter((x) => x.kind === "cable").map((x) => x.id),
  };
  apiRequest("/api/assets/move", "POST", body)
    .then((r) => {
      const m = document.getElementById("ft-move");
      if (m) m.remove();
      invSel.clear();
      invLastIdx = null;
      invToast(`${r.moved_total} aset dipindahkan ke "${r.folder}"`);
      return ftAfterChange();
    })
    .catch(ftErr);
}
// --- Folder: buka/lipat semua, centang semua, menu klik-kanan, seret & lepas (folder dan aset) ---
function ftCanW() {
  try {
    return can("asset.write");
  } catch (_) {
    return false;
  }
}
function ftSaveOpen() {
  try {
    localStorage.setItem("netgis.folderOpen", JSON.stringify([...FT.open]));
  } catch (_) {
    /* abaikan */
  }
}
function ftOpenAll(on) {
  if (!FT.tree) return;
  FT.open.clear();
  if (on)
    ftWalk(FT.tree, (n) => {
      if (n.path && n.kids.size) FT.open.add(n.path);
    });
  else if (FT.sel) {
    // lipat semua, tetapi folder terpilih tetap kelihatan
    const parts = FT.sel.split("/");
    for (let i = 1; i < parts.length; i++)
      FT.open.add(parts.slice(0, i).join("/"));
  }
  ftSaveOpen();
  ftRenderAll();
}
function ftOpenBranch(path, on) {
  const n = ftFind(path);
  if (!n) return;
  ftWalk(n, (m) => {
    if (m.kids.size) {
      if (on) FT.open.add(m.path);
      else FT.open.delete(m.path);
    }
  });
  ftSaveOpen();
  ftRenderAll();
}
// tampilkan di peta HANYA folder ini (beserta subfoldernya); folder lain disembunyikan
function ftOnly(path) {
  const n = ftFind(path);
  if (!n || !FT.tree) return;
  FT.tree.kids.forEach((k) => ftSet0(k, false));
  ftSet0(n, true);
  const parts = path.split("/");
  for (let i = 1; i < parts.length; i++)
    FT.open.add(parts.slice(0, i).join("/"));
  ftSaveOpen();
  ftAfterToggle();
}
// jalur lama -> baru setelah folder dipindah/diganti nama (centang, buka-lipat, pilihan ikut berpindah)
function ftRemapPaths(oldP, newP) {
  const mv = (p) =>
    p === oldP
      ? newP
      : p.startsWith(oldP + "/")
        ? newP + p.slice(oldP.length)
        : p;
  FT.off = new Set([...FT.off].map(mv));
  FT.open = new Set([...FT.open].map(mv));
  if (FT.sel) FT.sel = mv(FT.sel);
  ftSaveOff();
  ftSaveOpen();
}
// aturan tujuan seret folder: bukan dirinya/anaknya sendiri, bukan induknya sekarang
function ftFolderDropOk(src, target) {
  if (!src) return false;
  if (target === src || target.startsWith(src + "/")) return false;
  const parent = src.includes("/") ? src.slice(0, src.lastIndexOf("/")) : "";
  return target !== parent;
}
function ftNewPathFor(src, target) {
  const name = src.slice(src.lastIndexOf("/") + 1);
  return (target ? target + "/" : "") + name;
}
function ftMoveFolder(src, target) {
  if (!ftCanW() || !ftFolderDropOk(src, target)) return Promise.resolve(false);
  const np = ftNewPathFor(src, target);
  if (
    ftFind(np) &&
    !confirm(
      `Folder "${target || "(utama)"}" sudah punya folder bernama "${np.slice(np.lastIndexOf("/") + 1)}". Gabungkan isinya?`,
    )
  )
    return Promise.resolve(false);
  return apiRequest("/api/folders/rename", "POST", { path: src, new_path: np })
    .then((r) => {
      ftRemapPaths(src, r.path || np);
      if (target) FT.open.add(target);
      ftSaveOpen();
      invToast(
        `Folder "${src}" dipindahkan ke "${r.path || np}" (${r.moved || 0} aset)`,
      );
      return ftAfterChange().then(() => true);
    })
    .catch((err) => {
      ftErr(err);
      return false;
    });
}
function ftMoveAssetsTo(items, folder) {
  if (!ftCanW() || !folder || !items || !items.length)
    return Promise.resolve(false);
  if (
    items.length >= 50 &&
    !confirm(`Pindahkan ${items.length} aset ke "${folder}"?`)
  )
    return Promise.resolve(false);
  const body = {
    folder,
    nodes: items.filter((x) => x.kind === "node").map((x) => x.id),
    cables: items.filter((x) => x.kind === "cable").map((x) => x.id),
  };
  return apiRequest("/api/assets/move", "POST", body)
    .then((r) => {
      items.forEach((x) => invSel.delete(`${x.kind}:${x.id}`));
      invLastIdx = null;
      invToast(`${r.moved_total} aset dipindahkan ke "${r.folder}"`);
      return ftAfterChange().then(() => true);
    })
    .catch((err) => {
      ftErr(err);
      return false;
    });
}
// ---- seret & lepas ----
const FTDND = { drag: null, hoverT: null, hoverRow: null };
function ftDndClear() {
  FTDND.drag = null;
  if (FTDND.hoverT) {
    clearTimeout(FTDND.hoverT);
    FTDND.hoverT = null;
  }
  FTDND.hoverRow = null;
  document
    .querySelectorAll(".ft-row.drop, .ft-row.nodrop, .ft-row.dragging")
    .forEach((r) => r.classList.remove("drop", "nodrop", "dragging"));
  document
    .querySelectorAll("tr.dragging")
    .forEach((r) => r.classList.remove("dragging"));
}
function ftDropAllowed(drag, target) {
  if (!drag) return false;
  if (drag.type === "folder") return ftFolderDropOk(drag.path, target);
  if (drag.type === "assets") return !!target; // aset harus punya folder: "Semua Aset" bukan tujuan
  return false;
}
function ftDragImage(ev, text) {
  try {
    const g = document.createElement("div");
    g.className = "ft-dragimg";
    g.textContent = text;
    document.body.appendChild(g);
    ev.dataTransfer.setDragImage(g, 10, 10);
    setTimeout(() => g.remove(), 0);
  } catch (_) {
    /* abaikan */
  }
}
function ftOnDragStart(ev) {
  const row =
    ev.target && ev.target.closest ? ev.target.closest(".ft-row") : null;
  if (!row || !ftCanW()) return;
  const path = decodeURIComponent(row.dataset.p || "");
  if (!path) {
    ev.preventDefault();
    return;
  }
  FTDND.drag = { type: "folder", path };
  row.classList.add("dragging");
  try {
    ev.dataTransfer.effectAllowed = "move";
    ev.dataTransfer.setData("text/plain", path);
  } catch (_) {
    /* abaikan */
  }
}
function ftOnDragOver(ev) {
  if (!FTDND.drag) return;
  const row =
    ev.target && ev.target.closest ? ev.target.closest(".ft-row") : null;
  const tree = ev.currentTarget;
  if (tree && tree.getBoundingClientRect) {
    // gulir otomatis di tepi daftar
    const b = tree.getBoundingClientRect();
    if (ev.clientY < b.top + 28) tree.scrollTop -= 12;
    else if (ev.clientY > b.bottom - 28) tree.scrollTop += 12;
  }
  if (!row) return;
  const target = decodeURIComponent(row.dataset.p || "");
  const ok = ftDropAllowed(FTDND.drag, target);
  if (FTDND.hoverRow !== row) {
    document
      .querySelectorAll(".ft-row.drop, .ft-row.nodrop")
      .forEach((r) => r.classList.remove("drop", "nodrop"));
    FTDND.hoverRow = row;
    if (FTDND.hoverT) clearTimeout(FTDND.hoverT);
    FTDND.hoverT = setTimeout(() => {
      // tahan di atas folder bersubfolder: buka otomatis
      const n = target ? ftFind(target) : null;
      if (n && n.kids.size && !FT.open.has(target)) {
        FT.open.add(target);
        ftSaveOpen();
        ftRenderAll();
      }
    }, 700);
  }
  row.classList.add(ok ? "drop" : "nodrop");
  if (ok) {
    ev.preventDefault();
    try {
      ev.dataTransfer.dropEffect = "move";
    } catch (_) {
      /* abaikan */
    }
  }
}
function ftOnDrop(ev) {
  const drag = FTDND.drag;
  const row =
    ev.target && ev.target.closest ? ev.target.closest(".ft-row") : null;
  if (!drag || !row) {
    ftDndClear();
    return;
  }
  const target = decodeURIComponent(row.dataset.p || "");
  ev.preventDefault();
  const ok = ftDropAllowed(drag, target);
  ftDndClear();
  if (!ok) return;
  if (drag.type === "folder") ftMoveFolder(drag.path, target);
  else ftMoveAssetsTo(drag.items, target);
}
function ftOnAssetDragStart(ev) {
  const tr =
    ev.target && ev.target.closest ? ev.target.closest("tr[data-key]") : null;
  if (!tr || !ftCanW()) return;
  const key = tr.dataset.key;
  let items;
  if (invSel.has(key)) items = [...invSel.values()];
  else {
    const [kind, id] = key.split(":");
    items = [{ kind, id: Number(id) }];
  }
  FTDND.drag = { type: "assets", items };
  tr.classList.add("dragging");
  try {
    ev.dataTransfer.effectAllowed = "move";
    ev.dataTransfer.setData("text/plain", `${items.length} aset`);
  } catch (_) {
    /* abaikan */
  }
  ftDragImage(ev, items.length > 1 ? `${items.length} aset` : "1 aset");
}
function ftBindDnd() {
  const tree = document.getElementById("inv-folder-tree");
  if (tree && !tree._ftBound) {
    tree._ftBound = true;
    tree.addEventListener("dragstart", ftOnDragStart);
    tree.addEventListener("dragover", ftOnDragOver);
    tree.addEventListener("drop", ftOnDrop);
    tree.addEventListener("dragend", ftDndClear);
    tree.addEventListener("dragleave", (e) => {
      if (
        e.target &&
        e.target.classList &&
        e.target.classList.contains("ft-tree")
      )
        document
          .querySelectorAll(".ft-row.drop, .ft-row.nodrop")
          .forEach((r) => r.classList.remove("drop", "nodrop"));
    });
    tree.addEventListener("contextmenu", (e) => ftCtxOpen(e, "inv"));
    ftLongPress(tree, "inv");
  }
  const tb = document.getElementById("inventory-table-body");
  if (tb && !tb._ftBound) {
    tb._ftBound = true;
    tb.addEventListener("dragstart", ftOnAssetDragStart);
    tb.addEventListener("dragend", ftDndClear);
  }
}
// ---- menu klik-kanan (tekan lama di layar sentuh) ----
function ftCtxClose() {
  const m = document.getElementById("ft-ctx");
  if (m) m.remove();
}
function ftLongPress(el, mode) {
  let t = null,
    x = 0,
    y = 0;
  const stop = () => {
    if (t) {
      clearTimeout(t);
      t = null;
    }
  };
  el.addEventListener(
    "touchstart",
    (e) => {
      const tt = e.touches && e.touches[0];
      if (!tt) return;
      x = tt.clientX;
      y = tt.clientY;
      const target = e.target;
      stop();
      t = setTimeout(() => {
        t = null;
        ftCtxOpen(
          { target, clientX: x, clientY: y, preventDefault() {} },
          mode,
        );
      }, 650);
    },
    { passive: true },
  );
  el.addEventListener("touchmove", stop, { passive: true });
  el.addEventListener("touchend", stop);
  el.addEventListener("touchcancel", stop);
}
function ftCtxItems(path, mode) {
  const out = [];
  const it = (label, icon, fn, danger) => out.push({ label, icon, fn, danger });
  if (!path) {
    it("Tampilkan semua di peta", "fa-eye", () => ftAll(true));
    it("Sembunyikan semua", "fa-eye-slash", () => ftAll(false));
    it("Buka semua folder", "fa-angles-down", () => ftOpenAll(true));
    it("Lipat semua folder", "fa-angles-up", () => ftOpenAll(false));
    return out;
  }
  const n = ftFind(path);
  it("Hanya folder ini di peta", "fa-bullseye", () => ftOnly(path));
  if (n)
    it(
      ftState(n) === 0 ? "Tampilkan folder ini" : "Sembunyikan folder ini",
      ftState(n) === 0 ? "fa-eye" : "fa-eye-slash",
      () => ftSet(n, ftState(n) === 0),
    );
  if (n && n.kids.size) {
    it("Buka seluruh isi", "fa-angles-down", () => ftOpenBranch(path, true));
    it("Lipat seluruh isi", "fa-angles-up", () => ftOpenBranch(path, false));
  }
  if (mode === "map")
    it("Zoom ke folder", "fa-magnifying-glass-location", () =>
      ftFitFolder(path),
    );
  if (mode === "inv" && ftCanW()) {
    it("Subfolder baru…", "fa-folder-plus", () => {
      FT.sel = path;
      ftRenderAll();
      ftNewFolder();
    });
    it("Ganti nama…", "fa-pen", () => {
      FT.sel = path;
      ftRenderAll();
      ftRename();
    });
    it(
      "Hapus folder kosong",
      "fa-trash",
      () => {
        FT.sel = path;
        ftRenderAll();
        ftDelete();
      },
      true,
    );
  }
  return out;
}
function ftCtxOpen(ev, mode) {
  const row =
    ev.target && ev.target.closest ? ev.target.closest(".ft-row") : null;
  if (!row) return;
  if (ev.preventDefault) ev.preventDefault();
  const path = decodeURIComponent(row.dataset.p || "");
  ftCtxClose();
  const items = ftCtxItems(path, mode);
  const m = document.createElement("div");
  m.id = "ft-ctx";
  m.className = "ft-ctx";
  m.innerHTML =
    `<div class="ft-ctx-t">${escapeHtml(path || "Semua Aset")}</div>` +
    items
      .map(
        (x, i) =>
          `<button type="button" data-i="${i}" class="${x.danger ? "danger" : ""}"><i class="fa-solid ${x.icon}"></i>${escapeHtml(x.label)}</button>`,
      )
      .join("");
  m.addEventListener("click", (e) => {
    e.stopPropagation();
    const b = e.target.closest ? e.target.closest("button[data-i]") : null;
    if (!b) return;
    const fn = items[Number(b.dataset.i)].fn;
    ftCtxClose();
    fn();
  });
  m.addEventListener("contextmenu", (e) => e.preventDefault());
  document.body.appendChild(m);
  const w = m.offsetWidth || 220,
    h = m.offsetHeight || 200;
  m.style.left =
    Math.max(4, Math.min(ev.clientX, (window.innerWidth || 1200) - w - 6)) +
    "px";
  m.style.top =
    Math.max(4, Math.min(ev.clientY, (window.innerHeight || 800) - h - 6)) +
    "px";
  const off = (e) => {
    if (e.type === "keydown") {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      e.preventDefault();
    }
    ftCtxClose();
    document.removeEventListener("click", off, true);
    document.removeEventListener("keydown", off, true);
    window.removeEventListener("scroll", off, true);
  };
  document.addEventListener("keydown", off, true);
  window.addEventListener("scroll", off, true);
  setTimeout(() => {
    document.addEventListener("click", off, true);
  }, 0); // klik yang membuka menu tidak langsung menutupnya
}
// --- panel Folder + Semua/Tidak ada di kontrol Layer peta ---
function lcSetAllTypes(on) {
  Object.values(overlayMaps).forEach((grp) => {
    const g = zoomGateFor(grp);
    if (g) {
      g.wants = on;
      g.force = false;
    } else if (on) {
      if (grp !== hiddenIncidentGroup && !map.hasLayer(grp)) map.addLayer(grp);
    } else if (map.hasLayer(grp)) map.removeLayer(grp);
  });
  applyZoomGates();
  virtSyncNow();
}
const LCFOLD = { types: false, folder: false }; // bagian kontrol Layer yang sedang dilipat
try {
  Object.assign(
    LCFOLD,
    JSON.parse(localStorage.getItem("netgis.lcFold") || "{}"),
  );
} catch (_) {
  /* abaikan */
}
function lcApplyFold() {
  try {
    const ol = layersControl && layersControl._overlaysList;
    if (ol) ol.style.display = LCFOLD.types ? "none" : "";
    const tree = document.getElementById("lc-folder-tree");
    if (tree) tree.style.display = LCFOLD.folder ? "none" : "";
    document.querySelectorAll(".lc-tg").forEach((el) => {
      const car = el.querySelector(".lc-car");
      if (car) car.classList.toggle("o", !LCFOLD[el.dataset.t]);
    });
  } catch (_) {
    /* abaikan */
  }
}
function lcToggleFold(key) {
  LCFOLD[key] = !LCFOLD[key];
  try {
    localStorage.setItem("netgis.lcFold", JSON.stringify(LCFOLD));
  } catch (_) {
    /* abaikan */
  }
  lcApplyFold();
}
function ftMountLayerPanel() {
  try {
    const ol = layersControl && layersControl._overlaysList;
    const list = ol && ol.parentNode;
    if (!list) return;
    const hdr = (key, icon, title) =>
      `<span class="lc-tg" data-t="${key}" title="Lipat / buka"><i class="lc-car o">&#9654;</i>${icon}${title}</span>` +
      '<button type="button" data-a="all">Semua</button><button type="button" data-a="none">Tidak ada</button>';
    const bar = document.createElement("div");
    bar.className = "lc-allbar";
    bar.innerHTML = hdr("types", "", "Jenis layer");
    bar.addEventListener("click", (e) => {
      e.stopPropagation();
      if (e.target.closest && e.target.closest(".lc-tg"))
        return lcToggleFold("types");
      const a = e.target && e.target.dataset && e.target.dataset.a;
      if (a) lcSetAllTypes(a === "all");
    });
    list.insertBefore(bar, ol);
    const box = document.createElement("div");
    box.className = "lc-folder";
    box.innerHTML = `<div class="lc-allbar">${hdr("folder", '<i class="fa-solid fa-folder-tree"></i> ', "Folder")}</div><div id="lc-folder-tree" class="ft-tree"></div>`;
    box.firstChild.addEventListener("click", (e) => {
      e.stopPropagation();
      if (e.target.closest && e.target.closest(".lc-tg"))
        return lcToggleFold("folder");
      const a = e.target && e.target.dataset && e.target.dataset.a;
      if (a) ftAll(a === "all");
    });
    box.lastChild.addEventListener("click", (e) => ftClick(e, "map"));
    box.lastChild.addEventListener("contextmenu", (e) => ftCtxOpen(e, "map"));
    ftLongPress(box.lastChild, "map");
    box.addEventListener("click", (e) => e.stopPropagation());
    list.appendChild(box);
    if (typeof L !== "undefined" && L.DomEvent) {
      L.DomEvent.disableClickPropagation(box);
      L.DomEvent.disableScrollPropagation(box);
      L.DomEvent.disableClickPropagation(bar);
    }
    ftRenderAll();
    lcApplyFold();
  } catch (err) {
    console.warn("Panel folder layer gagal dipasang:", err && err.message);
  }
}

// ===================== VIRTUALISASI PETA (hanya yang terlihat yang digambar) =====================
// Semua aset tetap dibuat sebagai objek Leaflet (markersMap, popup, edit, pencarian tetap jalan), tetapi hanya yang
// (1) foldernya dicentang, (2) berada di area layar (+ margin) dan (3) masuk anggaran jumlah yang dipasang ke peta.
const VIRT = {
  items: [],
  timer: null,
  hint: "",
  CABLE_BUDGET: 3000,
  BLINK_MAX: 250,
};
// semakin jauh zoom, semakin sedikit penanda DOM yang dipasang sekaligus (peta tetap lancar saat digeser)
function virtNodeBudget(z) {
  return z < 12 ? 500 : z < 14 ? 900 : z < 16 ? 1500 : 2500;
}
const NODE_RANK = {
  POP: 0,
  CLOSURE: 1,
  ODP: 2,
  PELANGGAN: 4,
  TIANG: 5,
  HH: 6,
  SLACK: 7,
};
const CABLE_RANK = { Backbone: 0, Feeder: 1, Distribution: 2, Drop: 3 };
function virtBBox(c, bb) {
  if (!Array.isArray(c)) return bb;
  if (typeof c[0] === "number") {
    if (c[1] < bb[0]) bb[0] = c[1];
    if (c[0] < bb[1]) bb[1] = c[0];
    if (c[1] > bb[2]) bb[2] = c[1];
    if (c[0] > bb[3]) bb[3] = c[0];
    return bb;
  }
  c.forEach((x) => virtBBox(x, bb));
  return bb;
}
function virtAdd(layer, props, group, kind, coords) {
  const it = {
    layer,
    group,
    kind,
    folder: String(props.folder_path || ""),
    shown: false,
    pin: false,
    rank: 9,
    h: (Math.imul(VIRT.items.length + 1, 2654435761) >>> 0) / 4294967296,
  }; // urutan acak tetap: himpunan yang tampil stabil saat peta digeser
  if (kind === "node") {
    const ll = layer.getLatLng();
    it.lat = ll.lat;
    it.lng = ll.lng;
    it.rank = NODE_RANK[String(props.type || "").toUpperCase()] ?? 3;
  } else {
    it.bb = virtBBox(coords, [90, 180, -90, -180]);
    it.rank = CABLE_RANK[normalizeCableType(props.type)] ?? 2;
  }
  layer._vi = it;
  VIRT.items.push(it);
  return it;
}
function virtShow(it) {
  if (it.shown) return;
  it.group.addLayer(it.layer);
  if (it.layer._map) editableGroup.addLayer(it.layer);
  it.shown = true;
}
function virtHide(it) {
  if (!it.shown) return;
  if (editableGroup.hasLayer(it.layer)) editableGroup.removeLayer(it.layer);
  it.group.removeLayer(it.layer);
  it.shown = false;
}
function virtPin(it) {
  it.pin = true;
  virtShow(it);
}
function virtInView(it, b) {
  return it.kind === "node"
    ? it.lat >= b[0] && it.lat <= b[2] && it.lng >= b[1] && it.lng <= b[3]
    : !(
        it.bb[2] < b[0] ||
        it.bb[0] > b[2] ||
        it.bb[3] < b[1] ||
        it.bb[1] > b[3]
      );
}
function virtSchedule() {
  clearTimeout(VIRT.timer);
  VIRT.timer = setTimeout(virtSyncNow, 90);
}
function virtSyncNow() {
  clearTimeout(VIRT.timer);
  try {
    virtSync();
  } catch (err) {
    console.error("Sinkron peta gagal:", err);
  }
  applyZoomGates();
}
function virtSync() {
  VIRT.hint = "";
  if (!VIRT.items.length) return;
  let b1 = null,
    b2 = null,
    z = 99;
  try {
    if (typeof map.getBounds === "function") {
      const b = map.getBounds();
      const p1 = b.pad(0.4),
        p2 = b.pad(0.9);
      b1 = [p1.getSouth(), p1.getWest(), p1.getNorth(), p1.getEast()];
      b2 = [p2.getSouth(), p2.getWest(), p2.getNorth(), p2.getEast()];
    }
    if (typeof map.getZoom === "function") z = map.getZoom();
  } catch (_) {
    b1 = b2 = null;
  }
  const gateOf = new Map(ZOOM_GATES.map((g) => [g.group, g]));
  ZOOM_GATES.forEach((g) => {
    g.vcount = 0;
  });
  const candN = [],
    candC = [];
  for (const it of VIRT.items) {
    if (it.pin) {
      virtShow(it);
      continue;
    }
    let ok = !FT.off.has(it.folder);
    if (ok && b1) ok = virtInView(it, it.shown ? b2 : b1);
    if (!ok) {
      virtHide(it);
      continue;
    }
    const g = gateOf.get(it.group);
    if (g) g.vcount++;
    (it.kind === "node" ? candN : candC).push(it);
  }
  const groupOn = (it) => {
    const g = gateOf.get(it.group);
    if (g)
      return (
        g.wants && !(z < g.minZoom && !g.force && g.vcount > GATE_MIN_COUNT)
      );
    return typeof map.hasLayer === "function" ? map.hasLayer(it.group) : true;
  };
  const pick = (list, budget, label) => {
    const act = [];
    for (const it of list) {
      if (groupOn(it)) act.push(it);
      else virtHide(it);
    }
    let keep = act;
    if (act.length > budget) {
      // prioritas: jenis penting dulu (POP > Closure > ODP ...), yang sudah tampil dipertahankan, sisanya urutan tetap
      act.sort(
        (a, b) =>
          a.rank - b.rank || (b.shown ? 1 : 0) - (a.shown ? 1 : 0) || a.h - b.h,
      );
      keep = act.slice(0, budget);
      for (let i = budget; i < act.length; i++) virtHide(act[i]);
      VIRT.hint +=
        (VIRT.hint ? " · " : "") +
        `${label}: menampilkan ${budget.toLocaleString("id-ID")} dari ${act.length.toLocaleString("id-ID")} di layar, perbesar peta untuk melihat semua`;
    }
    keep.forEach(virtShow);
    return keep.length;
  };
  const shownNodes = pick(candN, virtNodeBudget(z), "Titik");
  pick(candC, VIRT.CABLE_BUDGET, "Kabel");
  const calm = shownNodes > VIRT.BLINK_MAX;
  try {
    document.documentElement.setAttribute(
      "data-blink-auto",
      calm ? "off" : "on",
    );
  } catch (_) {
    /* abaikan */
  }
  if (calm)
    VIRT.hint +=
      (VIRT.hint ? " · " : "") +
      "Kedip dimatikan otomatis (banyak aset di layar)";
}
map.on("moveend", virtSchedule);
map.on("overlayadd overlayremove", () => {
  if (!gateBusy) virtSchedule();
});

// Layer Control Box (Kanan Atas)
const baseMaps = {
  OpenStreetMap: osmLayer,
  "Google Satellite": googleSatLayer,
};
const overlayMaps = {
  "Jalur Backbone": backboneGroup,
  "Jalur Feeder": feederGroup,
  "Jalur Distribusi": distGroup,
  "Jalur Drop Cable": dropGroup,
  "POP / Headend": popGroup,
  "Joint Closure": closureGroup,
  ODP: odpGroup,
  Pelanggan: pelangganGroup,
  "Tiang (7m/9m)": tiangGroup,
  "Handhole (HH)": hhGroup,
  "Slack Kabel": slackGroup,
  "Titik Incident": incidentGroup,
  "Incident tanpa dampak": hiddenIncidentGroup,
  "Hasil OTDR (ukur)": otdrGroup,
};
// Layer Control: tampil penuh hanya bila peta cukup lebar; selain itu jadi ikon (buka saat diarahkan/diketuk).
// Lebar aslinya diukur lalu dipesan di grid (--lc-col) sehingga tidak pernah menimpa kolom pencarian.
let layoutLayerTimer = null;
let layersControl = null;
let layersControlWide = null;
const LAYERS_WIDE_MIN_PX = 1400;
function layoutLayerControl() {
  const mv = document.querySelector(".map-viewport");
  if (!mv) return;
  const wide = mv.clientWidth >= LAYERS_WIDE_MIN_PX;
  if (!layersControl || wide !== layersControlWide) {
    if (layersControl) map.removeControl(layersControl);
    layersControlWide = wide;
    layersControl = L.control
      .layers(baseMaps, overlayMaps, { position: "topright", collapsed: !wide })
      .addTo(map);
    ftMountLayerPanel();
  }
  const el = layersControl.getContainer ? layersControl.getContainer() : null;
  const w = el && el.offsetWidth ? el.offsetWidth : wide ? 260 : 36;
  if (mv.style && mv.style.setProperty)
    mv.style.setProperty("--lc-col", Math.round(w + 16) + "px");
  if (mv.classList) mv.classList.toggle("lc-wide", wide);
}
layoutLayerControl();
// Lebar peta juga berubah saat sidebar dibuka/ditutup (tanpa event resize): pantau langsung ukurannya
if (typeof ResizeObserver !== "undefined") {
  const lcObs = new ResizeObserver(() => {
    clearTimeout(layoutLayerTimer);
    layoutLayerTimer = setTimeout(layoutLayerControl, 120);
  });
  const mvEl = document.querySelector(".map-viewport");
  if (mvEl) lcObs.observe(mvEl);
}
window.addEventListener("resize", () => {
  clearTimeout(layoutLayerTimer);
  layoutLayerTimer = setTimeout(layoutLayerControl, 150);
});

// ===================== PERFORMA DATA BESAR =====================
// (A) Kabel dalam jumlah besar digambar di kanvas (satu elemen, bukan ribuan <path>); kabel putus tetap SVG agar berkedip.
// (B) Lapis padat (pelanggan, tiang, handhole, slack, kabel drop) hanya dipasang pada zoom dekat bila isinya banyak.
// (C) Isi popup dibuat saat popup dibuka (lihat bindPopup(() => ...) di renderNodes/renderCables).
const CABLE_CANVAS_MIN = 300; // jumlah kabel di atas ini -> kanvas
const GATE_MIN_COUNT = 250; // lapis dengan isi di atas ini dibatasi menurut zoom
const cableCanvas = L.canvas({ padding: 0.3, tolerance: 6 });
const ZOOM_GATES = [
  {
    group: pelangganGroup,
    minZoom: 16,
    label: "Pelanggan",
    wants: true,
    force: false,
  },
  { group: tiangGroup, minZoom: 16, label: "Tiang", wants: true, force: false },
  { group: hhGroup, minZoom: 16, label: "Handhole", wants: true, force: false },
  { group: slackGroup, minZoom: 15, label: "Slack", wants: true, force: false },
  {
    group: dropGroup,
    minZoom: 16,
    label: "Kabel Drop",
    wants: true,
    force: false,
  },
];
let gateBusy = false;
let gateHintEl = null;
function gateCount(g) {
  const own =
    typeof g.group.getLayers === "function"
      ? g.group.getLayers().length
      : (g.group.layers || []).length;
  return typeof g.vcount === "number" ? Math.max(g.vcount, own) : own;
}
function gateSetHint(list) {
  try {
    if (!gateHintEl && typeof document.createElement === "function") {
      const host = document.querySelector(".map-viewport");
      if (!host || typeof host.appendChild !== "function") return;
      gateHintEl = document.createElement("div");
      gateHintEl.className = "zoom-gate-hint";
      host.appendChild(gateHintEl);
    }
    if (!gateHintEl) return;
    const parts = [];
    if (list.length)
      parts.push("Perbesar peta untuk menampilkan: " + list.join(", "));
    if (VIRT.hint) parts.push(VIRT.hint);
    gateHintEl.style.display = parts.length ? "" : "none";
    gateHintEl.textContent = parts.join(" · ");
  } catch (_) {
    /* petunjuk hanya tambahan */
  }
}
function applyZoomGates() {
  if (!map || typeof map.getZoom !== "function") return;
  const z = map.getZoom();
  const hints = [];
  gateBusy = true;
  try {
    ZOOM_GATES.forEach((g) => {
      if (z >= g.minZoom) g.force = false;
      const gated = z < g.minZoom && !g.force && gateCount(g) > GATE_MIN_COUNT;
      const show = g.wants && !gated;
      const on = map.hasLayer(g.group);
      if (show && !on) {
        map.addLayer(g.group);
        if (g.group.eachLayer)
          g.group.eachLayer((l) => editableGroup.addLayer(l));
      } else if (!show && on) {
        if (g.group.eachLayer)
          g.group.eachLayer((l) => editableGroup.removeLayer(l));
        map.removeLayer(g.group);
      }
      if (g.wants && gated) hints.push(`${g.label} (zoom ${g.minZoom}+)`);
    });
  } finally {
    gateBusy = false;
  }
  gateSetHint(hints);
}
function zoomGateFor(layer) {
  return ZOOM_GATES.find((g) => g.group === layer);
}
map.on("overlayadd", (e) => {
  if (gateBusy) return;
  const g = zoomGateFor(e.layer);
  if (!g) return;
  g.wants = true;
  g.force = map.getZoom() < g.minZoom; // dinyalakan sengaja pada zoom jauh: hormati pilihan pengguna
  if (g.group.eachLayer) g.group.eachLayer((l) => editableGroup.addLayer(l));
  gateSetHint(
    ZOOM_GATES.filter((x) => x.wants && !map.hasLayer(x.group)).map(
      (x) => `${x.label} (zoom ${x.minZoom}+)`,
    ),
  );
});
map.on("overlayremove", (e) => {
  if (gateBusy) return;
  const g = zoomGateFor(e.layer);
  if (!g) return;
  g.wants = false;
  g.force = false;
  gateSetHint(
    ZOOM_GATES.filter((x) => x.wants && !map.hasLayer(x.group)).map(
      (x) => `${x.label} (zoom ${x.minZoom}+)`,
    ),
  );
});
map.on("zoomend", applyZoomGates);
// layer yang baru dibuat ikut daftar penyuntingan hanya bila sedang tampil di peta
function registerEditable(layer) {
  if (layer && layer._map) editableGroup.addLayer(layer);
}

// Setup Toolbar Draw
let drawControl = new L.Control.Draw({
  draw: {
    polygon: false,
    circle: false,
    rectangle: false,
    circlemarker: false,
    marker: true,
    polyline: true,
  },
  edit: {
    featureGroup: editableGroup,
    remove: false,
  },
});
map.addControl(drawControl);

// --- STATE GLOBAL ---
const NODE_TYPES = [
  "ODP",
  "POP",
  "CLOSURE",
  "PELANGGAN",
  "TIANG",
  "HH",
  "SLACK",
  "INCIDENT",
];
const NO_PORT_TYPES = ["TIANG", "HH"]; // aset pasif: tidak punya port/core
const CABLE_TYPES = ["Backbone", "Feeder", "Distribution", "Drop"];

const markersMap = {}; // "node:5" / "cable:2" / "incident:1" -> layer Leaflet
let loadToken = 0; // mencegah respons loadData() lama menimpa yang baru
let allInventoryData = []; // gabungan node + kabel (diisi oleh loadData / fetchInventoryData)
let currentActiveAsset = null;
let activeConnections = [];
let searchHighlightMarker = null;

// --- HELPER FUNCTIONS ---
function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (ch) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[ch],
  );
}

// --- WAKTU: server menyimpan UTC; tampilkan di zona waktu perangkat + keterangan relatif ---
function parseServerTime(raw) {
  if (!raw) return null;
  let s = String(raw).trim();
  if (!/[zZ]$|[+-]\d{2}:?\d{2}$/.test(s)) s = s.replace(" ", "T") + "Z"; // data lama tanpa penanda zona = UTC
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? null : d;
}

function relativeTime(d, now = Date.now()) {
  const sec = Math.round((now - d.getTime()) / 1000);
  if (sec < 45) return "baru saja";
  if (sec < 3600) return `${Math.round(sec / 60)} menit lalu`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} jam lalu`;
  return `${Math.floor(sec / 86400)} hari lalu`;
}

function formatLocalTime(d) {
  try {
    return d.toLocaleString("id-ID", {
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
      timeZoneName: "short",
    });
  } catch (_) {
    return d.toString();
  }
}

// <time> yang diperbarui otomatis tiap 30 detik (lihat ticker di bawah)
function timeHtml(raw) {
  const d = parseServerTime(raw);
  if (!d) return escapeHtml(raw || "-");
  return `<time class="live-time" data-ts="${d.toISOString()}">${escapeHtml(formatLocalTime(d))} &middot; ${escapeHtml(relativeTime(d))}</time>`;
}

function refreshLiveTimes() {
  if (typeof document === "undefined" || !document.querySelectorAll) return;
  document.querySelectorAll("time.live-time[data-ts]").forEach((el) => {
    const d = new Date(el.getAttribute("data-ts"));
    if (!Number.isNaN(d.getTime()))
      el.innerHTML = `${escapeHtml(formatLocalTime(d))} &middot; ${escapeHtml(relativeTime(d))}`;
  });
}
if (typeof setInterval === "function") setInterval(refreshLiveTimes, 30000);

// --- DAMPAK: kelompokkan aset terdampak per tipe supaya jelas node mana saja ---
const IMPACT_NODE_ORDER = [
  ["POP", "POP"],
  ["CLOSURE", "Closure / Joint"],
  ["ODP", "ODP"],
  ["PELANGGAN", "Pelanggan"],
  ["TIANG", "Tiang"],
  ["HH", "Handhole (HH)"],
  ["SLACK", "Slack"],
];

function groupImpactNodes(im) {
  const groups = [];
  const known = new Set(IMPACT_NODE_ORDER.map((x) => x[0]));
  IMPACT_NODE_ORDER.forEach(([type, label]) => {
    const names = (im.nodes || [])
      .filter((n) => (n.type || "").toUpperCase() === type)
      .map((n) => n.name);
    if (names.length) groups.push({ label, names });
  });
  const other = (im.nodes || [])
    .filter((n) => !known.has((n.type || "").toUpperCase()))
    .map((n) => n.name);
  if (other.length) groups.push({ label: "Node lain", names: other });
  const cables = (im.cables || []).map((c) => c.name);
  if (cables.length) groups.push({ label: "Kabel", names: cables });
  return groups;
}

function impactNamesText(im, limit = 8) {
  return groupImpactNodes(im || {})
    .map(
      (g) =>
        `  - ${g.label} (${g.names.length}): ${g.names.slice(0, limit).join(", ")}` +
        (g.names.length > limit ? ` +${g.names.length - limit} lainnya` : ""),
    )
    .join("\n");
}

// Semua request lewat sini: error server (termasuk 422/409) menjadi pesan yang terbaca
// Bar tipis di atas layar: muncul bila ada request yang berjalan > 300 ms (tanda sistem sedang bekerja)
const NETBAR = { n: 0, timer: null, shown: false };
function netBarStart() {
  NETBAR.n++;
  if (NETBAR.timer || NETBAR.shown) return;
  NETBAR.timer = setTimeout(() => {
    NETBAR.timer = null;
    const el = document.getElementById("net-bar");
    if (el && NETBAR.n > 0) {
      el.className = "net-bar on";
      NETBAR.shown = true;
    }
  }, 300);
}
function netBarEnd() {
  NETBAR.n = Math.max(0, NETBAR.n - 1);
  if (NETBAR.n > 0) return;
  if (NETBAR.timer) {
    clearTimeout(NETBAR.timer);
    NETBAR.timer = null;
  }
  if (NETBAR.shown) {
    NETBAR.shown = false;
    const el = document.getElementById("net-bar");
    if (el) el.className = "net-bar";
  }
}
async function apiRequest(url, method = "GET", body) {
  const quiet = String(url).startsWith("/api/import/jobs/"); // polling progres punya indikator sendiri
  const options = { method, headers: {}, credentials: "same-origin" };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  if (!quiet) netBarStart();
  // Aksi tulis (POST/PUT/DELETE): tombol yang diklik diberi spinner & dikunci sampai selesai (cegah klik ganda)
  let busyBtn = null;
  if (!quiet && method !== "GET") {
    const b = document.activeElement;
    if (b && b.tagName === "BUTTON" && !b.disabled) {
      busyBtn = b;
      setBtnBusy(b, true);
    }
  }
  let res,
    data = null;
  try {
    res = await fetch(url, options);
    try {
      data = await res.json();
    } catch (_) {
      /* respons tanpa body JSON */
    }
  } finally {
    if (!quiet) netBarEnd();
    if (busyBtn) {
      busyBtn.className = (busyBtn.className || "")
        .replace(/\bis-busy\b/g, "")
        .trim();
      busyBtn.disabled = false;
    }
  }
  if (res.status === 401 && !url.startsWith("/api/auth/login")) {
    // Sesi habis / belum login: kembali ke layar login (data peta tidak ditampilkan lagi)
    if (!url.startsWith("/api/auth/me"))
      showLogin("Sesi berakhir, silakan masuk lagi.");
    const e = new Error((data && data.detail) || "Belum login");
    e.status = 401;
    throw e;
  }
  if (!res.ok) {
    let msg = `Permintaan gagal (${res.status})`;
    if (data && data.detail) {
      msg = Array.isArray(data.detail)
        ? data.detail.map((d) => d.msg).join("; ")
        : data.detail;
    }
    throw new Error(msg);
  }
  return data;
}

function normalizeCableType(type) {
  return type === "Dropcore" ? "Drop" : type; // data lama
}

function intOrNull(value) {
  const n = parseInt(value, 10);
  return Number.isNaN(n) ? null : n;
}

function calculatePolylineLength(coordinates) {
  let totalDistance = 0;
  for (let i = 0; i < coordinates.length - 1; i++) {
    const coord1 = L.latLng(coordinates[i][1], coordinates[i][0]);
    const coord2 = L.latLng(coordinates[i + 1][1], coordinates[i + 1][0]);
    totalDistance += coord1.distanceTo(coord2);
  }
  return totalDistance;
}

// Glyph garis bergaya fiber optik untuk ikon peta & tabel (SVG, mengikuti warna teks)
const MAP_GLYPH = {
  POP: '<path d="M12 12v9M8 21h8"/><circle cx="12" cy="9" r="1.6" fill="currentColor"/><path d="M8.3 5.8a5 5 0 0 0 0 6.4M15.7 5.8a5 5 0 0 1 0 6.4M5.6 3.6a8.5 8.5 0 0 0 0 10.8M18.4 3.6a8.5 8.5 0 0 1 0 10.8"/>',
  ODP: '<circle cx="4.5" cy="12" r="1.6" fill="currentColor"/><path d="M6 12h4M10 12l8-7M10 12l8-2.5M10 12l8 2.5M10 12l8 7"/><circle cx="19" cy="5" r="1" fill="currentColor"/><circle cx="19" cy="9.5" r="1" fill="currentColor"/><circle cx="19" cy="14.5" r="1" fill="currentColor"/><circle cx="19" cy="19" r="1" fill="currentColor"/>',
  CLOSURE:
    '<rect x="5" y="8" width="14" height="8" rx="4"/><path d="M12 8v8M1.5 12H5M19 12h3.5"/>',
  HH: '<rect x="4" y="4" width="16" height="16" rx="2.5"/><path d="M9 8.5v7M15 8.5v7M9 12h6"/>',
  TIANG: '<path d="M12 2.5v19M6.5 6h11M8.5 10h7"/>',
  PELANGGAN: '<path d="M3.5 11 12 4l8.5 7M6 9.5V20h12V9.5M10 20v-5.5h4V20"/>',
  SLACK:
    '<path d="M2 12h3M19 12h3"/><path d="M5 12a7 7 0 0 1 14 0M8 12a4 4 0 0 1 8 0"/><path d="M5 12a7 7 0 0 0 14 0" stroke-dasharray="2 2.4"/>',
  CABLE:
    '<circle cx="4.5" cy="17.5" r="1.7"/><circle cx="19.5" cy="6.5" r="1.7"/><path d="M6.2 17.5c4.6 0 3-11 7.2-11 2.6 0 2.4 3 4.4 0"/>',
  NEW: '<path d="M12 5v14M5 12h14"/>',
  MERGE: '<path d="M6 4v6a6 6 0 0 0 6 6h0a6 6 0 0 0 6-6V4M12 16v5"/>',
  SKIP: '<circle cx="12" cy="12" r="9"/><path d="M5.6 5.6l12.8 12.8"/>',
  INCIDENT:
    '<path d="M12 3.5 2.8 19.5h18.4L12 3.5z"/><path d="M12 10v4.2M12 17v.4"/>',
};
function mapGlyph(type, px) {
  const g = MAP_GLYPH[String(type || "").toUpperCase()] || MAP_GLYPH.ODP;
  return `<svg class="mk-glyph" viewBox="0 0 24 24" width="${px}" height="${px}" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${g}</svg>`;
}

function createCustomIcon(type, status, repairKind) {
  let iconClass = "fa-solid fa-location-dot";
  let bgClass = "icon-odp";

  if (status === "Cut/Broken" || type === "INCIDENT") {
    iconClass = "fa-solid fa-triangle-exclamation";
    bgClass = "icon-incident";
  } else {
    switch (type) {
      case "POP":
        iconClass = "fa-solid fa-server";
        bgClass = "icon-pop";
        break;
      case "ODP":
        iconClass = "fa-solid fa-box-archive";
        bgClass = "icon-odp";
        break;
      case "CLOSURE":
        iconClass = "fa-solid fa-link";
        // warna dasar selalu sama dengan closure lain; cincin putus-putus = perbaikan sementara (belum permanen)
        bgClass =
          repairKind === "TEMPORARY"
            ? "icon-closure icon-closure-temp"
            : "icon-closure";
        break;
      case "PELANGGAN":
        iconClass = "fa-solid fa-house-user";
        bgClass = "icon-pelanggan";
        break;
      case "TIANG":
        iconClass = "fa-solid fa-ellipsis-vertical";
        bgClass = "icon-tiang";
        break;
      case "HH":
        iconClass = "fa-solid fa-square-h";
        bgClass = "icon-hh";
        break;
      case "SLACK":
        iconClass = "fa-solid fa-circle";
        bgClass = "icon-slack";
        break;
    }
  }

  // Penanda status berkedip: hijau = aktif, merah = down/incident (hanya perangkat aktif; tiang/HH/slack pasif tidak)
  const ACTIVE_TYPES = ["POP", "ODP", "CLOSURE", "PELANGGAN"];
  const isDown = status === "Cut/Broken" || type === "INCIDENT";
  let stClass = "";
  if (isDown) stClass = " st-down";
  else if (ACTIVE_TYPES.includes(type))
    stClass = status === "Maintenance" ? " st-maint" : " st-active";
  // ukuran/anchor HARUS sama dengan ukuran CSS (tiang 18x30) agar titik koordinat tepat di tengah ikon
  const sz = type === "TIANG" && !isDown ? [16, 26] : [22, 22];
  const glyphKey = isDown ? "INCIDENT" : type;
  return L.divIcon({
    className: `custom-map-icon ${bgClass}${stClass}`,
    html: mapGlyph(glyphKey, type === "TIANG" && !isDown ? 26 : 14),
    iconSize: sz,
    iconAnchor: [sz[0] / 2, sz[1] / 2],
  });
}

// ---- Format & metadata jenis aset (dipakai kartu, ringkasan, dan inventory) ----
function fmtLen(m) {
  if (m === null || m === undefined || m === "") return "-";
  const n = m == null || m === "" ? NaN : Number(m);
  if (!Number.isFinite(n)) return "-";
  if (n >= 1000) return (n / 1000).toFixed(2).replace(".", ",") + " km";
  return Math.round(n) + " m";
}
const NODE_TYPE_META = {
  POP: { label: "POP / Headend", color: "#8b5cf6", icon: "fa-server" },
  CLOSURE: { label: "Joint Closure", color: "#f59e0b", icon: "fa-link" },
  ODP: { label: "ODP", color: "#10b981", icon: "fa-box-archive" },
  HH: { label: "Handhole (HH)", color: "#0f766e", icon: "fa-square-h" },
  TIANG: { label: "Tiang", color: "#475569", icon: "fa-ellipsis-vertical" },
  SLACK: { label: "Slack Kabel", color: "#111827", icon: "fa-circle" },
  PELANGGAN: { label: "Pelanggan", color: "#06b6d4", icon: "fa-house-user" },
  INCIDENT: {
    label: "Titik Incident",
    color: "#ef4444",
    icon: "fa-triangle-exclamation",
  },
};
const CABLE_TYPE_META = {
  Backbone: { color: "#2638dc", label: "Backbone" },
  Feeder: { color: "#ca8a04", label: "Feeder" },
  Distribution: { color: "#0891b2", label: "Distribution" },
  Drop: { color: "#d97706", label: "Drop" },
};
const INSTALL_LABEL = {
  Udara: "Kabel Udara",
  Tanah: "Kabel Tanah",
  "Belum diisi": "Belum diisi",
};

let lastSummary = null;

function loadDashboardSummary() {
  return apiRequest("/api/dashboard/summary" + scopeQS("?"))
    .then((data) => {
      lastSummary = data;
      const set = (id, val, html) => {
        const el = document.getElementById(id);
        if (!el) return;
        if (html) el.innerHTML = val;
        else el.innerText = val;
      };
      set("stat-odp", data.total_odp);
      set("stat-cables", data.total_cables);
      set("stat-incidents", data.total_incidents);
      const odp = (data.nodes_by_type || {}).ODP;
      if (odp) {
        set(
          "stat-odp-sub",
          `${odp.Active} aktif` +
            (odp["Cut/Broken"]
              ? ` &middot; <span class="bad">${odp["Cut/Broken"]} gangguan</span>`
              : ""),
          true,
        );
      }
      if (data.cables)
        set("stat-cables-sub", `${fmtLen(data.cables.length_m)} total`);
      set(
        "stat-incidents-sub",
        `${Number(data.tickets_open || 0)} tiket aktif`,
      );
      set(
        "stat-assets",
        Number(data.total_nodes || 0) + Number(data.total_cables || 0),
      );
      set(
        "stat-assets-sub",
        `${Number(data.total_nodes || 0)} node &middot; ${Number(data.total_cables || 0)} kabel`,
        true,
      );
      renderInventoryChips();
      updateScopeCount();
      renderSummaryIfOpen();
      return data;
    })
    .catch((err) => console.error("Gagal memuat ringkasan:", err));
}

// ---- Sidebar: sembunyikan / tampilkan agar peta lebih luas ----
const SIDEBAR_KEY = "netgis_sidebar_collapsed";
function setSidebarCollapsed(collapsed, persist = true) {
  const app = document.querySelector(".app-container");
  if (!app) return;
  app.classList.toggle("sidebar-collapsed", !!collapsed);
  const btn = document.getElementById("sidebar-toggle");
  if (btn) btn.setAttribute("aria-pressed", collapsed ? "true" : "false");
  if (persist) {
    try {
      localStorage.setItem(SIDEBAR_KEY, collapsed ? "1" : "0");
    } catch (_) {
      /* penyimpanan tidak tersedia */
    }
  }
  // peta harus menghitung ulang ukurannya setelah animasi lebar sidebar selesai
  const fix = () => {
    if (map && typeof map.invalidateSize === "function") map.invalidateSize();
  };
  fix();
  setTimeout(fix, 300);
}
function toggleSidebar() {
  const app = document.querySelector(".app-container");
  setSidebarCollapsed(!(app && app.classList.contains("sidebar-collapsed")));
}
function initSidebarState() {
  let saved = null;
  try {
    saved = localStorage.getItem(SIDEBAR_KEY);
  } catch (_) {}
  if (saved === "1") setSidebarCollapsed(true, false);
}

// ---- Ringkasan Aset: kartu per jenis, klik = daftar di Inventory ----
function openSummaryModal() {
  document.getElementById("modal-summary").style.display = "flex";
  if (lastSummary) renderSummaryModal(lastSummary);
  loadDashboardSummary().then((d) => {
    if (d) renderSummaryModal(d);
  });
}
function closeSummaryModal() {
  document.getElementById("modal-summary").style.display = "none";
}
function openSummaryList(opts) {
  // ringkasan tetap terbuka di bawah daftar; menutup daftar = kembali ke ringkasan
  openInventoryModal(opts);
}

function renderSummaryModal(d) {
  const byType = d.nodes_by_type || {};
  const known = Object.keys(NODE_TYPE_META);
  const order = known.concat(
    Object.keys(byType).filter((t) => t && !known.includes(t)),
  );
  const cards = order
    .filter((t) => t !== "INCIDENT" || (byType[t] && byType[t].total > 0))
    .map((t) => {
      const st = byType[t] || {
        total: 0,
        Active: 0,
        Maintenance: 0,
        "Cut/Broken": 0,
      };
      const meta = NODE_TYPE_META[t] || {
        label: t,
        color: "#64748b",
        icon: "fa-location-dot",
      };
      const tot = st.total || 0;
      const pct = (n) => (tot ? ((n / tot) * 100).toFixed(1) : 0);
      return `<button type="button" class="sum-card" onclick="openSummaryList({ type: '${escapeHtml(t)}' })">
      <div class="sum-card-top">
        <span class="sum-ico" style="background:${meta.color};${t === "SLACK" ? "color:#facc15" : ""}">${t === "INCIDENT" ? `<i class="fa-solid ${meta.icon}"></i>` : mapGlyph(t, 22)}</span>
        <div><div class="sum-count">${Number(tot)}</div><div class="sum-label">${escapeHtml(meta.label)}</div></div>
      </div>
      <div class="sum-bar" title="Active / Maintenance / Cut-Broken">${tot ? `<i class="ok" style="width:${pct(st.Active)}%"></i><i class="mt" style="width:${pct(st.Maintenance)}%"></i><i class="bad" style="width:${pct(st["Cut/Broken"])}%"></i>` : ""}</div>
      <div class="sum-legend"><span><b>${Number(st.Active)}</b> aktif</span><span><b>${Number(st.Maintenance)}</b> maintenance</span><span class="bad"><b>${Number(st["Cut/Broken"])}</b> putus</span></div>
    </button>`;
    });
  document.getElementById("summary-nodes").innerHTML = cards.join("");

  const c = d.cables || {
    total: 0,
    length_m: 0,
    by_type: {},
    by_installation: {},
  };
  const maxT = Math.max(
    1,
    ...Object.values(c.by_type || {}).map((v) => v.length_m),
  );
  const maxI = Math.max(
    1,
    ...Object.values(c.by_installation || {}).map((v) => v.length_m),
  );
  const typeRows = Object.entries(c.by_type || {})
    .map(([t, v]) => {
      const color = (CABLE_TYPE_META[t] || { color: "#64748b" }).color;
      return `<button type="button" class="sum-row" onclick="openSummaryList({ type: '${escapeHtml(t)}', sort: 'length', order: 'desc' })">
      <span class="dot" style="background:${color}"></span>
      <span class="nm">${escapeHtml(t)}<small>${Number(v.count)} kabel</small></span><span class="ln">${fmtLen(v.length_m)}</span>
      <span class="meter"><i style="width:${((v.length_m / maxT) * 100).toFixed(1)}%;background:${color}"></i></span></button>`;
    })
    .join("");
  const instRows = Object.entries(c.by_installation || {})
    .map(([k, v]) => {
      const arg = k === "Belum diisi" ? "NONE" : k;
      return `<button type="button" class="sum-row" onclick="openSummaryList({ type: 'CABLE', installation: '${arg}', sort: 'length', order: 'desc' })">
      <span class="dot" style="background:${k === "Udara" ? "#0ea5e9" : k === "Tanah" ? "#a16207" : "#94a3b8"}"></span>
      <span class="nm">${escapeHtml(INSTALL_LABEL[k] || k)}<small>${Number(v.count)} kabel</small></span><span class="ln">${fmtLen(v.length_m)}</span>
      <span class="meter"><i style="width:${((v.length_m / maxI) * 100).toFixed(1)}%"></i></span></button>`;
    })
    .join("");
  document.getElementById("summary-cables").innerHTML = `<div class="sum-cable">
    <div class="sum-cable-head">
      <span class="sum-ico"><i class="fa-solid fa-route"></i></span>
      <div><div class="sum-cable-total">${fmtLen(c.length_m)}</div><div class="sum-label">Total panjang dari ${Number(c.total)} kabel${c.broken ? ` &middot; <span style="color:#ef4444;font-weight:600">${Number(c.broken)} putus</span>` : ""}</div></div>
      <button type="button" class="inv-btn primary" onclick="openSummaryList({ type: 'CABLE', sort: 'length', order: 'desc' })"><i class="fa-solid fa-list"></i> Lihat semua kabel</button>
    </div>
    <div class="sum-cable-cols">
      <div><h5>Per jenis kabel</h5>${typeRows}</div>
      <div><h5>Per cara pemasangan</h5>${instRows}</div>
    </div></div>`;
  renderScopeBreakdown(d.breakdown);
}

// Tombol "Lihat Detail" pada popup peta: membuka modal detail aset (alternatif dari Asset Inventory)
function openMapDetail(category, id) {
  try {
    map.closePopup();
  } catch (_) {}
  openCoreDetailModal(Number(id), category);
}
// Baris ringkasan popup per tipe aset (kapasitas, komposisi OTB, area)
function popupSummaryRows(props, isCable) {
  const row = (k, v) =>
    `<div class="popup-row"><span>${k}:</span> <b>${v}</b></div>`;
  let h = "";
  const t = String(props.type || "").toUpperCase();
  if (props.capacity) {
    if (!isCable && t === "POP") {
      const sz = otbSizes(props.capacity);
      h += row(
        "Kapasitas",
        `${sz.length} OTB &middot; ${sz.reduce((a, b) => a + b, 0)} port`,
      );
    } else {
      h += row("Kapasitas", escapeHtml(props.capacity));
    }
  }
  if (props.cluster || props.area)
    h += row(
      "Cluster / Area",
      `${escapeHtml(props.cluster || "-")} / ${escapeHtml(props.area || "-")}`,
    );
  return h;
}
function popupDetailButton(category, id) {
  return `<button class="btn-status btn-detail" onclick="openMapDetail('${category}', ${Number(id)})"><i class="fa-solid fa-circle-info"></i> Lihat Detail</button>`;
}

function openAssetPopup(key) {
  const layer = markersMap[key];
  if (layer && layer._vi && !layer._map) virtPin(layer._vi); // aset di luar layar / folder tersembunyi: tampilkan dulu
  if (layer && !layer._map) applyZoomGates(); // lapis dibatasi zoom: pasang dulu bila peta sudah cukup dekat
  if (layer && layer._map) layer.openPopup();
}

// --- API ACTIONS ---
function updateNodeStatus(id, newStatus) {
  apiRequest(`/api/nodes/${id}/status`, "PUT", { status: newStatus })
    .then(() => loadData())
    .catch((err) => alert("Gagal mengubah status node: " + err.message));
}

function editNodeProperties(id) {
  const marker = markersMap[`node:${id}`];
  if (marker) openEditAssetModal("NODE", id, marker.metaData);
}

function deleteNode(id, name) {
  const label = name ?? markersMap[`node:${id}`]?.metaData?.name ?? `#${id}`;
  if (!confirm(`Apakah Anda yakin ingin menghapus marker "${label}"?`)) return;

  apiRequest(`/api/nodes/${id}`, "DELETE")
    .then(() => loadData()) // loadData juga menyegarkan tabel Inventory jika sedang terbuka
    .catch((err) => alert("Gagal menghapus aset node: " + err.message));
}

function updateCableStatus(id, newStatus) {
  // "Report Cable Cut": buka form tiket dengan kabel terpilih supaya bisa memilih core terdampak
  // (status langsung tanpa form = seluruh kabel putus). Pemulihan tetap langsung.
  if (
    newStatus === "Cut/Broken" &&
    can("incident.write") &&
    openCableCutForm(id)
  )
    return;
  apiRequest(`/api/cables/${id}/status`, "PUT", { status: newStatus })
    .then(() => loadData())
    .catch((err) => alert("Gagal mengubah status kabel: " + err.message));
}

let incidentPreset = null; // {cableId, name} dari tombol Report Cable Cut

function openCableCutForm(id) {
  const layer = markersMap[`cable:${id}`];
  if (!layer || typeof layer.getLatLngs !== "function") return false;
  let pts = layer.getLatLngs();
  while (Array.isArray(pts) && Array.isArray(pts[0])) pts = pts[0]; // MultiLineString
  if (!Array.isArray(pts) || pts.length < 2) return false;
  const mid = pts[Math.floor(pts.length / 2)];
  const name = (layer.metaData && layer.metaData.name) || `#${id}`;
  try {
    map.closePopup();
  } catch (_) {
    /* abaikan */
  }
  openAddIncidentModal(mid.lat, mid.lng, { cableId: Number(id), name });
  const t = document.getElementById("inc-title");
  if (t) t.value = `Gangguan kabel ${name}`;
  applyIncidentLocateSuggestion(); // langsung pilih kabel (tanpa menunggu deteksi lokasi)
  return true;
}

function editCableProperties(id) {
  const layer = markersMap[`cable:${id}`];
  if (layer) openEditAssetModal("CABLE", id, layer.metaData);
}

function deleteCable(id, name) {
  const label = name ?? markersMap[`cable:${id}`]?.metaData?.name ?? `#${id}`;
  if (!confirm(`Apakah Anda yakin ingin menghapus jalur kabel "${label}"?`))
    return;

  apiRequest(`/api/cables/${id}`, "DELETE")
    .then(() => loadData())
    .catch((err) => alert("Gagal menghapus jalur kabel: " + err.message));
}

// --- RENDER DATA FROM DATABASE ---
function clearAllLayers() {
  ALL_GROUPS.forEach((g) => g.clearLayers());
  editableGroup.clearLayers();
  VIRT.items.length = 0;
  Object.keys(markersMap).forEach((k) => delete markersMap[k]);
}

function addNodeToGroup(marker, props) {
  const t = (props.type || "").toUpperCase();
  if (t === "INCIDENT") {
    incidentGroup.addLayer(marker);
    return;
  }
  const grp =
    t === "POP"
      ? popGroup
      : t === "CLOSURE"
        ? closureGroup
        : t === "PELANGGAN"
          ? pelangganGroup
          : t === "TIANG"
            ? tiangGroup
            : t === "HH"
              ? hhGroup
              : t === "SLACK"
                ? slackGroup
                : odpGroup;
  virtAdd(marker, props, grp, "node"); // dipasang ke peta oleh virtSync (folder + area layar)
}

function renderNodes(data) {
  L.geoJSON(data, {
    pointToLayer: function (feature, latlng) {
      const props = feature.properties;
      const marker = L.marker(latlng, {
        icon: createCustomIcon(props.type, props.status, props.repair_kind),
      });

      marker.metaType = "node";
      marker.metaData = props;

      const isBroken = props.status === "Cut/Broken";
      const btnText = isBroken ? "Set Normal (Active)" : "Report Incident";
      const btnClass = isBroken ? "btn-success" : "btn-danger";
      const nextStatus = isBroken ? "Active" : "Cut/Broken";
      const id = Number(props.id);

      marker.bindPopup(
        () => `
        <div class="popup-header">${escapeHtml(props.name)}</div>
        <div class="popup-row"><span>Kategori:</span> <b>${escapeHtml(props.type)}</b></div>
        <div class="popup-row"><span>Latitude:</span> <b>${latlng.lat.toFixed(6)}</b></div>
        <div class="popup-row"><span>Longitude:</span> <b>${latlng.lng.toFixed(6)}</b></div>
        <div class="popup-row"><span>Status:</span> <b>${escapeHtml(props.status)}</b></div>
        ${popupSummaryRows(props, false)}
        ${props.reg_code ? `<div class="popup-row"><span>Kode Reg:</span> <b>${escapeHtml(props.reg_code)}</b></div>` : ""}
        ${(props.type || "").toUpperCase() === "PELANGGAN" && (props.bandwidth_mbps || props.link_type) ? `<div class="popup-row"><span>Layanan:</span> <b>${escapeHtml(props.service || "-")} &middot; ${escapeHtml(fmtBw(props.bandwidth_mbps))} &middot; ${escapeHtml(props.link_type || "-")}</b></div>` : ""}
        ${(props.type || "").toUpperCase() === "POP" && props.trunk_mbps ? `<div class="popup-row"><span>Trunk:</span> <b>${escapeHtml(fmtBw(props.trunk_mbps))}</b></div>` : ""}
        ${
          props.repair_kind
            ? `<div class="popup-row"><span>Perbaikan:</span> <b>${props.repair_kind === "TEMPORARY" ? "Sementara (perlu permanen)" : "Permanen"}</b> &middot; ${escapeHtml(props.repair_ticket || "")}</div>
               <div class="popup-actions"><button class="btn-status btn-warning" onclick="openIncidentDetail(${Number(props.repair_incident_id)})"><i class="fa-solid fa-list-check"></i> Detail Tiket</button></div>`
            : ""
        }

        <div class="popup-actions">
            ${popupDetailButton("NODE", id)}
            ${
              can("status.write")
                ? `<button class="btn-status ${btnClass}" onclick="updateNodeStatus(${id}, '${nextStatus}')">
                <i class="fa-solid fa-power-off"></i> ${btnText}
            </button>`
                : ""
            }
            ${
              can("asset.write")
                ? `<button class="btn-status btn-warning" onclick="editNodeProperties(${id})">
                <i class="fa-solid fa-pen"></i> Edit Info Aset
            </button>`
                : ""
            }
            ${
              can("asset.delete")
                ? `<button class="btn-status btn-outline-danger" onclick="deleteNode(${id})">
                <i class="fa-solid fa-trash"></i> Hapus Aset
            </button>`
                : ""
            }
            ${
              can("audit.view")
                ? `<button class="btn-status btn-warning" onclick="openAuditModal('NODE', ${id})">
                <i class="fa-solid fa-clock-rotate-left"></i> Riwayat
            </button>`
                : ""
            }
        </div>
      `,
      );

      markersMap[`node:${id}`] = marker;
      marker.on("click", () => {
        if (OVLMAIN.click(marker)) {
          try {
            marker.closePopup();
          } catch (_) {}
          return;
        }
        if (SORPICK.on) {
          try {
            marker.closePopup();
          } catch (_) {}
          sorPickNode(props);
          return;
        }
        if (CONNECT.active) {
          try {
            marker.closePopup();
          } catch (_) {}
          connectPick(marker);
          return;
        }
        onMarkerPicked(latlng.lat, latlng.lng, id);
      }); // dipakai saat memilih titik (Coverage / Pasang Baru) & Hubungkan aset
      marker.on("mouseover", () => connectHover(marker));
      marker.on("mouseout", () => connectHover(null));
      addNodeToGroup(marker, props);
      registerEditable(marker);
      return marker;
    },
  });
}

function renderIncidents(data) {
  L.geoJSON(data, {
    pointToLayer: function (feature, latlng) {
      const props = feature.properties;
      const isResolved = props.status === "Resolved";
      const isTemp = props.status === "Temporary Fix";
      const id = Number(props.id);

      // Perbaikan closure/joint sudah punya penanda sendiri (node closure) di titik yang sama:
      // jangan tumpuk dua ikon berbeda warna. Tiket tetap bisa dibuka dari popup closure & NOC Monitor.
      const represented =
        props.repair_node_id != null && (isResolved || isTemp);

      const hidden = !!props.map_hidden; // insiden tanpa dampak yang disembunyikan pengguna
      const marker = L.marker(latlng, {
        icon: L.divIcon({
          className: `custom-map-icon ${isResolved ? "icon-slack" : "icon-incident st-down"}${hidden ? " icon-hidden-incident" : ""}`,
          html: mapGlyph("INCIDENT", 14),
          iconSize: [22, 22],
          iconAnchor: [11, 11],
        }),
      });

      if (represented) return marker; // tidak dimasukkan ke peta

      const statusBadge = isResolved
        ? `<span style="color:#16a34a; font-weight:bold;">Resolved</span>`
        : isTemp
          ? `<span style="color:#d97706; font-weight:bold;">Temporary Fix (perlu perbaikan permanen)</span>`
          : `<span style="color:#ef4444; font-weight:bold;">${escapeHtml(props.status)} (${escapeHtml(props.severity)})</span>`;

      marker.bindPopup(
        () => `
        <div class="popup-header" style="background:#ef4444; color:white; padding:4px 8px; border-radius:4px;">
          [${escapeHtml(props.ticket_number)}] ${escapeHtml(props.title)}
        </div>
        <div class="popup-row" style="margin-top:8px;"><span>Kategori:</span> <b>${escapeHtml(props.incident_type)}</b></div>
        <div class="popup-row"><span>Status:</span> ${statusBadge}</div>
        <div class="popup-row"><span>Deskripsi:</span> <p style="margin:2px 0;">${escapeHtml(props.description || "-")}</p></div>
        <div class="popup-row"><span>Waktu Lapor:</span> ${timeHtml(props.reported_at)}</div>
        ${props.no_effect ? `<div class="popup-row" style="color:#64748b;"><i class="fa-solid fa-circle-info"></i> Tidak mengubah status aset apa pun${hidden ? " &mdash; disembunyikan dari peta" : ""}.</div>` : ""}

        <div class="popup-actions" style="margin-top:10px;">
          <button class="btn-status btn-warning" onclick="openIncidentDetail(${id})">
            <i class="fa-solid fa-list-check"></i> Detail &amp; Perbaikan
          </button>
          ${
            !isResolved && can("incident.write")
              ? `<button class="btn-status btn-success" onclick="resolveIncident(${id})">
                  <i class="fa-solid fa-check-circle"></i> Selesaikan Tiket
                </button>`
              : ""
          }
          ${
            props.no_effect && can("incident.write")
              ? `<button class="btn-status btn-outline-muted" onclick="setIncidentMapVisibility(${id}, ${hidden ? "false" : "true"})">
                  <i class="fa-solid ${hidden ? "fa-eye" : "fa-eye-slash"}"></i> ${hidden ? "Tampilkan kembali di peta" : "Sembunyikan dari peta"}
                </button>`
              : ""
          }
          ${
            can("incident.delete")
              ? `<button class="btn-status btn-outline-danger" onclick="deleteIncidentRecord(${id})">
            <i class="fa-solid fa-trash"></i> Hapus Tiket
          </button>`
              : ""
          }
        </div>
      `,
      );

      markersMap[`incident:${id}`] = marker;
      (hidden ? hiddenIncidentGroup : incidentGroup).addLayer(marker);
      return marker;
    },
  });
}

// Baris popup untuk kabel dengan gangguan sebagian core
function coreFaultPopupRow(props) {
  const f = props && props.core_fault;
  if (!f) return "";
  const nums = (f.labels || []).map((l) => escapeHtml(l)).join(", ");
  return (
    `<div class="popup-row core-fault-row"><span>Gangguan core:</span> <b>${Number(f.down)} dari ${Number(f.used)} core terpakai down</b>` +
    `<div class="core-fault-sub">${nums} · tiket ${(f.tickets || []).map(escapeHtml).join(", ")}</div></div>`
  );
}

function renderCables(data) {
  const bigCables = ((data && data.features) || []).length > CABLE_CANVAS_MIN;
  L.geoJSON(data, {
    style: function (feature) {
      const st = getCableStyle(
        normalizeCableType(feature.properties.type),
        feature.properties.status,
        feature.properties.installation,
        feature.properties.core_fault,
      );
      // Banyak kabel -> kanvas; kabel putus tetap SVG supaya kedip merahnya terlihat
      if (bigCables && feature.properties.status !== "Cut/Broken")
        st.renderer = cableCanvas;
      return st;
    },
    onEachFeature: function (feature, layer) {
      const props = feature.properties;
      const coords = feature.geometry.coordinates;
      const type = normalizeCableType(props.type);
      const id = Number(props.id);

      layer.metaType = "cable";
      layer.metaData = props;

      const isCut = props.status === "Cut/Broken";
      const btnText = isCut ? "Set Normal (Active)" : "Report Cable Cut";
      const btnClass = isCut ? "btn-success" : "btn-danger";
      const nextStatus = isCut ? "Active" : "Cut/Broken";

      layer.bindPopup(() => {
        const lengthInMeters = calculatePolylineLength(coords);
        const formattedLength =
          lengthInMeters > 1000
            ? `${(lengthInMeters / 1000).toFixed(2)} km (${Math.round(lengthInMeters)} m)`
            : `${Math.round(lengthInMeters)} m`;
        return `
        <div class="popup-header">${escapeHtml(props.name)}</div>
        <div class="popup-row"><span>Tipe Jalur:</span> <b>Kabel ${escapeHtml(type)}</b></div>
        <div class="popup-row"><span>Panjang Kabel:</span> <b>${formattedLength}</b></div>
        <div class="popup-row"><span>Pemasangan:</span> <b>${props.installation === "Udara" ? "Kabel Udara" : props.installation === "Tanah" ? "Kabel Tanah" : "Belum diisi"}</b></div>
        <div class="popup-row"><span>Status:</span> <b>${escapeHtml(props.status)}</b></div>
        ${coreFaultPopupRow(props)}
        ${popupSummaryRows(props, true)}

        <div class="popup-actions">
            ${popupDetailButton("CABLE", id)}
            ${
              can("status.write")
                ? `<button class="btn-status ${btnClass}" onclick="updateCableStatus(${id}, '${nextStatus}')">
                <i class="fa-solid fa-power-off"></i> ${btnText}
            </button>`
                : ""
            }
            ${
              can("asset.write")
                ? `<button class="btn-status btn-warning" onclick="editCableProperties(${id})">
                <i class="fa-solid fa-pen"></i> Edit Info Kabel
            </button>`
                : ""
            }
            ${
              can("asset.delete")
                ? `<button class="btn-status btn-outline-danger" onclick="deleteCable(${id})">
                <i class="fa-solid fa-trash"></i> Hapus Kabel
            </button>`
                : ""
            }
            ${
              can("audit.view")
                ? `<button class="btn-status btn-warning" onclick="openAuditModal('CABLE', ${id})">
                <i class="fa-solid fa-clock-rotate-left"></i> Riwayat
            </button>`
                : ""
            }
        </div>
      `;
      });

      markersMap[`cable:${id}`] = layer;
      layer.on("click", () => {
        if (SORPICK.on) {
          try {
            layer.closePopup();
          } catch (_) {}
          sorPickCable(layer);
        }
      });

      const cg =
        type === "Backbone"
          ? backboneGroup
          : type === "Feeder"
            ? feederGroup
            : type === "Drop"
              ? dropGroup
              : distGroup;
      virtAdd(layer, props, cg, "cable", coords); // dipasang ke peta oleh virtSync (folder + area layar)
    },
  });
}

// Satu kali fetch paralel, lalu clear + render sekali. Token membuang respons usang,
// sehingga pemanggilan beruntun (mis. setelah edit banyak objek) tidak membuat marker ganda.
function loadData() {
  const token = ++loadToken;
  loadDashboardSummary();

  Promise.all([
    apiRequest("/api/nodes" + scopeQS("?")),
    apiRequest("/api/incidents" + scopeQS("?")),
    apiRequest("/api/cables" + scopeQS("?")),
  ])
    .then(([nodes, incidents, cables]) => {
      if (token !== loadToken) return;
      clearAllLayers();
      renderNodes(nodes);
      renderIncidents(incidents);
      renderCables(cables);
      virtSyncNow();
      ftLoad();
      buildInventory(nodes, cables);
      renderInventoryIfOpen();
      fitScopeIfRequested(nodes, cables);
      loadOtdrLayer();
    })
    .catch((err) => console.error("Gagal memuat data peta:", err));
  refreshScopeOptions();
}

// Pemuatan data awal dilakukan setelah login terverifikasi (lihat bootAuth di bagian AUTENTIKASI)

// --- EVENT LISTENER: LEAFLET DRAW (EDIT & CREATE) ---
map.on(L.Draw.Event.EDITED, function (e) {
  const jobs = [];
  e.layers.eachLayer(function (layer) {
    if (layer.metaType === "node") {
      const latlng = layer.getLatLng();
      jobs.push(
        apiRequest(`/api/nodes/${layer.metaData.id}`, "PUT", {
          latitude: latlng.lat,
          longitude: latlng.lng,
        }),
      );
    } else if (layer.metaType === "cable") {
      const coordinates = layer.getLatLngs().map((pt) => [pt.lng, pt.lat]);
      jobs.push(
        apiRequest(`/api/cables/${layer.metaData.id}`, "PUT", { coordinates }),
      );
    }
  });

  // loadData() cukup sekali setelah semua perubahan selesai
  Promise.all(jobs)
    .catch((err) => alert("Sebagian perubahan gagal disimpan: " + err.message))
    .finally(() => loadData());
});

map.on(L.Draw.Event.CREATED, function (event) {
  const layer = event.layer;
  const type = event.layerType;

  if (type === "marker") {
    const latlng = layer.getLatLng();
    // Teknisi tidak boleh menambah aset: langsung ke form tiket
    const action = can("asset.write")
      ? confirm(
          "Klik OK untuk Tambah Asset Normal (ODP/POP/Tiang/dll)\nKlik CANCEL untuk Buat Tiket Incident Baru",
        )
      : false;
    if (action) {
      openAddAssetModal("NODE", latlng.lat, latlng.lng);
    } else {
      openAddIncidentModal(latlng.lat, latlng.lng);
    }
  } else if (type === "polyline") {
    const coords = layer.getLatLngs().map((pt) => [pt.lng, pt.lat]);
    openAddAssetModal("CABLE", null, null, coords);
  }
});

// --- MODAL ASSET INVENTORY LOGIC ---
function openInventoryModal(opts) {
  // dipanggil dari onclick tanpa argumen (menu) atau dengan filter awal (kartu ringkasan)
  const o = opts && typeof opts === "object" && !opts.target ? opts : null;
  document.getElementById("modal-inventory").style.display = "flex";
  if (o) {
    const setv = (id, v) => {
      const el = document.getElementById(id);
      if (el) el.value = v;
    };
    setv("filter-type", o.type || "ALL");
    setv("filter-installation", o.installation || "ALL");
    setv("filter-status", o.status || "ALL");
    setv("inventory-search", "");
    inventoryState.sort = o.sort || "name";
    inventoryState.order = o.order || "asc";
    inventoryState.page = 1;
  }
  renderInventoryChips();
  if (!lastSummary) loadDashboardSummary();
  if (!FT.mobInit) {
    // layar sempit: panel folder disembunyikan dulu (tombol "Folder" membukanya)
    FT.mobInit = true;
    const sp = document.getElementById("inv-split");
    if (sp && sp.classList && window.innerWidth < 900)
      sp.classList.add("nofolders");
  }
  ftRenderAll();
  fetchInventoryData();
}

function closeInventoryModal() {
  if (invBusy) return;
  invSel.clear();
  invLastIdx = null;
  document.getElementById("modal-inventory").style.display = "none";
}

// Membangun allInventoryData dari respons /api/nodes dan /api/cables
function buildInventory(nodesData, cablesData) {
  const nodes = (nodesData.features || []).map((f) => ({
    ...f.properties,
    category: "NODE",
    lat: f.geometry.coordinates[1],
    lng: f.geometry.coordinates[0],
  }));
  const cables = (cablesData.features || []).map((f) => {
    const coords = f.geometry.coordinates;
    const mid = coords[Math.floor(coords.length / 2)];
    return { ...f.properties, category: "CABLE", lat: mid[1], lng: mid[0] };
  });
  allInventoryData = [...nodes, ...cables];
}

function fetchInventoryData() {
  return Promise.all([
    apiRequest("/api/nodes" + scopeQS("?")),
    apiRequest("/api/cables" + scopeQS("?")),
  ])
    .then(([nodesData, cablesData]) => {
      buildInventory(nodesData, cablesData);
      renderInventoryTable();
    })
    .catch((err) => console.error("Error loading inventory:", err));
}

function renderInventoryIfOpen() {
  const modal = document.getElementById("modal-inventory");
  if (modal && modal.style.display !== "none") renderInventoryTable();
}

// --- INVENTORY: filter, urutan & halaman diproses server (/api/inventory) ---
const inventoryState = {
  page: 1,
  pageSize: 25,
  sort: "name",
  order: "asc",
  total: 0,
  pages: 1,
};
let inventoryReqToken = 0;
let inventorySearchTimer = null;

function onInventoryFilterChange() {
  inventoryState.page = 1;
  renderInventoryTable();
}

function onInventorySearchInput() {
  clearTimeout(inventorySearchTimer); // debounce: jangan request tiap ketukan
  inventorySearchTimer = setTimeout(onInventoryFilterChange, 250);
}

function onInventoryPageSizeChange() {
  inventoryState.pageSize =
    parseInt(document.getElementById("inventory-page-size").value, 10) || 25;
  inventoryState.page = 1;
  renderInventoryTable();
}

function goInventoryPage(delta) {
  const next = inventoryState.page + delta;
  if (next < 1 || next > inventoryState.pages) return;
  inventoryState.page = next;
  renderInventoryTable();
}

function setInventorySort(col) {
  if (inventoryState.sort === col) {
    inventoryState.order = inventoryState.order === "asc" ? "desc" : "asc";
  } else {
    inventoryState.sort = col;
    inventoryState.order = "asc";
  }
  inventoryState.page = 1;
  renderInventoryTable();
}

function updateInventoryPager() {
  const st = inventoryState;
  const info = document.getElementById("inventory-page-info");
  if (info) {
    const from = st.total === 0 ? 0 : (st.page - 1) * st.pageSize + 1;
    const to = Math.min(st.page * st.pageSize, st.total);
    info.textContent = `${from}\u2013${to} dari ${st.total} aset \u00b7 Hal. ${st.page}/${st.pages}`;
  }
  const prev = document.getElementById("inventory-prev");
  const next = document.getElementById("inventory-next");
  if (prev) prev.disabled = st.page <= 1;
  if (next) next.disabled = st.page >= st.pages;
  document.querySelectorAll("#modal-inventory th[data-sort]").forEach((th) => {
    const ind = th.querySelector(".sort-ind");
    if (ind)
      ind.textContent =
        th.dataset.sort === st.sort
          ? st.order === "asc"
            ? " \u25b2"
            : " \u25bc"
          : "";
  });
}

function renderInventoryTable() {
  const params = new URLSearchParams({
    q: document.getElementById("inventory-search").value.trim(),
    region: SCOPE.region,
    cluster: SCOPE.cluster,
    area: SCOPE.area,
    type: document.getElementById("filter-type").value,
    status: (document.getElementById("filter-status") || {}).value || "ALL",
    installation:
      (document.getElementById("filter-installation") || {}).value || "ALL",
    folder: FT.sel || "",
    sort: inventoryState.sort,
    order: inventoryState.order,
    page: String(inventoryState.page),
    page_size: String(inventoryState.pageSize),
  });
  const token = ++inventoryReqToken;
  ftLoadCounts(params);
  const tbody = document.getElementById("inventory-table-body");
  // pilihan hanya berlaku untuk filter yang sama: ganti filter/cari/cluster = pilihan dikosongkan
  const sig = [
    "q",
    "cluster",
    "area",
    "type",
    "status",
    "installation",
    "folder",
  ]
    .map((k) => params.get(k))
    .join("|");
  if (sig !== invSelSig && !invBusy) {
    invSelSig = sig;
    invSel.clear();
    invLastIdx = null;
  }
  if (!tbody.children.length)
    tbody.innerHTML =
      '<tr class="inv-skel"><td colspan="10"><div></div><div></div><div></div><div></div></td></tr>';

  return apiRequest(`/api/inventory?${params}`)
    .then((res) => {
      if (token !== inventoryReqToken) return; // respons usang (filter sudah berganti)
      inventoryState.page = res.page;
      inventoryState.pages = res.pages;
      inventoryState.total = res.total;
      updateInventoryPager();
      renderInventoryChips();
      renderInventorySummary(res);
      drawInventoryRows(res.items || [], tbody);
    })
    .catch((err) => {
      console.error("Gagal memuat inventory:", err);
      tbody.innerHTML = `<tr><td colspan="10" style="text-align:center; padding:20px; color:#ef4444;">Gagal memuat data: ${escapeHtml(err.message)}</td></tr>`;
    });
}

function isCableFilter(t) {
  return t === "CABLE" || CABLE_TYPES.includes(t);
}

function setInventoryType(t) {
  const sel = document.getElementById("filter-type");
  if (sel) sel.value = t;
  if (!isCableFilter(t)) {
    const inst = document.getElementById("filter-installation");
    if (inst) inst.value = "ALL";
    if (
      inventoryState.sort === "length" ||
      inventoryState.sort === "installation"
    ) {
      inventoryState.sort = "name";
      inventoryState.order = "asc";
    }
  }
  inventoryState.page = 1;
  renderInventoryChips();
  renderInventoryTable();
}

function setInventoryInstall(v) {
  const inst = document.getElementById("filter-installation");
  if (inst) inst.value = v;
  inventoryState.page = 1;
  renderInventoryChips();
  renderInventoryTable();
}

// Chip jenis aset (+ jumlah) dan, untuk kabel, chip jenis kabel & cara pemasangan
function renderInventoryChips() {
  const box = document.getElementById("inv-type-chips");
  if (!box) return;
  const cur = (document.getElementById("filter-type") || {}).value || "ALL";
  const inst =
    (document.getElementById("filter-installation") || {}).value || "ALL";
  const d = lastSummary || {};
  const bt = d.nodes_by_type || {};
  const cab = d.cables || {};
  const cnt = (n) =>
    n === undefined || n === null ? "" : `<span class="n">${Number(n)}</span>`;
  const chip = (label, value, count, active, fn) =>
    `<button type="button" class="inv-chip${active ? " active" : ""}" onclick="${fn}('${value}')" role="tab" aria-selected="${active}">${label}${cnt(count)}</button>`;
  const total = lastSummary
    ? Number(d.total_nodes || 0) + Number(d.total_cables || 0)
    : undefined;
  let html = chip("Semua", "ALL", total, cur === "ALL", "setInventoryType");
  ["POP", "CLOSURE", "ODP", "HH", "TIANG", "SLACK", "PELANGGAN"].forEach(
    (t) => {
      html += chip(
        NODE_TYPE_META[t].label,
        t,
        lastSummary ? (bt[t] ? bt[t].total : 0) : undefined,
        cur === t,
        "setInventoryType",
      );
    },
  );
  html += chip(
    "Kabel",
    "CABLE",
    lastSummary ? cab.total : undefined,
    isCableFilter(cur),
    "setInventoryType",
  );
  if ((bt.INCIDENT && bt.INCIDENT.total) || cur === "INCIDENT") {
    html += chip(
      "Titik Incident",
      "INCIDENT",
      bt.INCIDENT ? bt.INCIDENT.total : 0,
      cur === "INCIDENT",
      "setInventoryType",
    );
  }
  box.innerHTML = html;

  const sub = document.getElementById("inv-cable-filters");
  if (sub) sub.style.display = isCableFilter(cur) ? "flex" : "none";
  const types = document.getElementById("inv-cable-types");
  if (types) {
    let th = chip(
      "Semua jenis",
      "CABLE",
      lastSummary ? cab.total : undefined,
      cur === "CABLE",
      "setInventoryType",
    );
    CABLE_TYPES.forEach((t) => {
      th += chip(
        t,
        t,
        lastSummary && cab.by_type
          ? (cab.by_type[t] || { count: 0 }).count
          : undefined,
        cur === t,
        "setInventoryType",
      );
    });
    types.innerHTML = th;
  }
  const insts = document.getElementById("inv-install-chips");
  if (insts) {
    const bi = cab.by_installation || {};
    const ic = (k) => (lastSummary ? (bi[k] || { count: 0 }).count : undefined);
    insts.innerHTML =
      chip("Semua", "ALL", undefined, inst === "ALL", "setInventoryInstall") +
      chip(
        "Udara",
        "Udara",
        ic("Udara"),
        inst === "Udara",
        "setInventoryInstall",
      ) +
      chip(
        "Tanah",
        "Tanah",
        ic("Tanah"),
        inst === "Tanah",
        "setInventoryInstall",
      ) +
      chip(
        "Belum diisi",
        "NONE",
        ic("Belum diisi"),
        inst === "NONE",
        "setInventoryInstall",
      );
  }
  const table = document.getElementById("inventory-table");
  if (table && table.classList)
    table.classList.toggle("cable-mode", isCableFilter(cur));
}

function renderInventorySummary(res) {
  const el = document.getElementById("inv-summary");
  if (!el) return;
  const sm = res.summary || {};
  let html = `<span><b>${Number(res.total)}</b> aset ditemukan</span>`;
  if (sm.cables)
    html += `<span><b>${Number(sm.cables)}</b> kabel &middot; total panjang <b>${fmtLen(sm.length_m)}</b></span>`;
  el.innerHTML = html;
}

function drawInventoryRows(items, tbody) {
  invApplyCols();
  tbody.innerHTML = "";
  invRows = items.slice();
  invLastIdx = null;
  const tbl = document.getElementById("inventory-table");
  if (tbl) tbl.classList.toggle("inv-nosel", !invCanSel());

  if (items.length === 0) {
    invRefreshSel();
    const af = invActiveFilters();
    tbody.innerHTML =
      `<tr><td colspan="10" class="inv-empty"><i class="fa-regular fa-folder-open" style="font-size:22px;display:block;margin-bottom:8px;"></i>Tidak ada aset yang cocok dengan filter ini` +
      (af.length
        ? `<div class="inv-empty-f">Filter aktif: ${af.map(escapeHtml).join(" &middot; ")}</div><button type="button" class="inv-bar-btn" onclick="invResetFilters()"><i class="fa-solid fa-filter-circle-xmark"></i> Hapus filter jenis / status / cari</button>`
        : "") +
      `</td></tr>`;
    return;
  }

  items.forEach((item, idx) => {
    const tr = document.createElement("tr");
    const isCable = item.category === "CABLE";
    const id = Number(item.id);
    const kind = item.category.toLowerCase(); // "node" / "cable"
    const typeKey = (item.type || "").toUpperCase();
    const meta = isCable
      ? {
          color: (
            CABLE_TYPE_META[normalizeCableType(item.type)] || {
              color: "#64748b",
            }
          ).color,
          icon: "fa-route",
        }
      : NODE_TYPE_META[typeKey] || {
          color: "#64748b",
          icon: "fa-location-dot",
        };
    const st =
      item.status === "Maintenance"
        ? ["mt", "Maintenance"]
        : item.status === "Cut/Broken"
          ? ["bad", "Cut/Broken"]
          : ["ok", item.status || "Active"];
    const deleteCall = isCable ? `deleteCable(${id})` : `deleteNode(${id})`;
    const editCall = isCable
      ? `editCableProperties(${id})`
      : `editNodeProperties(${id})`;
    const inst = item.installation;
    const instHtml =
      inst === "Udara"
        ? `<span class="inv-install udara">Kabel Udara</span>`
        : inst === "Tanah"
          ? `<span class="inv-install tanah">Kabel Tanah</span>`
          : `<span class="inv-install none">Belum diisi</span>`;
    const passive = !isCable && NO_PORT_TYPES.includes(typeKey);
    const typeLabel = isCable
      ? normalizeCableType(item.type)
      : (NODE_TYPE_META[typeKey] || { label: item.type }).label;

    const selKey = `${kind}:${id}`;
    tr.dataset.key = selKey;
    tr.dataset.idx = String(idx);
    if (invSel.has(selKey)) tr.classList.add("sel");
    tr.setAttribute("onclick", "invRowClick(event, this)");
    if (ftCanW()) tr.draggable = true;
    const icoHtml = mapGlyph(isCable ? "CABLE" : typeKey, 17);
    tr.innerHTML = `
      <td class="col-sel"><input type="checkbox" class="inv-chk" ${invSel.has(selKey) ? "checked" : ""} onclick="invChkClick(event, this)" aria-label="Pilih ${escapeHtml(item.name)}"></td>
      <td><div class="inv-name"><span class="inv-ico" style="background:${meta.color}">${icoHtml}</span>
        <div><b>${escapeHtml(item.name)}</b><span class="inv-sub">${escapeHtml(typeLabel || "-")} &middot; ${escapeHtml(item.city || "-")}${item.reg_code ? ` &middot; ${escapeHtml(item.reg_code)}` : ""}${typeKey === "PELANGGAN" && item.bandwidth_mbps ? ` &middot; ${escapeHtml(fmtBw(item.bandwidth_mbps))} ${escapeHtml(item.link_type || "")}` : ""}</span></div></div></td>
      <td class="col-type cv-type"><span class="inv-tag">${escapeHtml(typeLabel || "-")}</span></td>
      <td class="col-loc cv-loc inv-loc"><b>${escapeHtml(item.city || "-")}</b><small>${escapeHtml(item.cluster || "-")} / ${escapeHtml(item.area || "-")}</small></td>
      <td class="col-folder cv-folder">${invFolderCell(item.folder_path)}</td>
      <td class="col-cap cv-cap">${escapeHtml(item.capacity || "-")}</td>
      <td class="col-cable cv-len"><b>${isCable ? fmtLen(item.length_m) : "-"}</b></td>
      <td class="col-cable cv-inst">${isCable ? instHtml : "-"}</td>
      <td class="cv-status"><span class="inv-status ${st[0]}">${escapeHtml(st[1])}</span></td>
      <td><div class="inv-actions">
        <button class="inv-btn" onclick="zoomToAsset('${kind}', ${id})" title="Sorot di Peta"><i class="fa-solid fa-crosshairs"></i></button>
        ${passive ? "" : `<button class="inv-btn primary" onclick="openCoreDetailModal(${id}, '${item.category}')" title="Lihat Status Core / Port"><i class="fa-solid fa-diagram-project"></i><span class="lbl"> Core/Port</span></button>`}
        ${can("asset.write") ? `<button class="inv-btn" onclick="${editCall}" title="Edit"><i class="fa-solid fa-pen"></i></button>` : ""}
        ${can("asset.delete") ? `<button class="inv-btn danger" onclick="${deleteCall}" title="Hapus Aset"><i class="fa-solid fa-trash"></i></button>` : ""}
      </div></td>`;
    tbody.appendChild(tr);
  });
  invRefreshSel();
}

// ---- Inventaris: kolom Folder, pilihan kolom yang tampil, dan Ubah massal ----
function invFolderCell(path) {
  if (!path) return '<span class="muted">-</span>';
  const parts = String(path).split("/").filter(Boolean);
  const short =
    parts.length > 2 ? "…/" + parts.slice(-2).join("/") : parts.join("/");
  return `<a class="inv-fcell" role="button" data-fp="${encodeURIComponent(path)}" onclick="invPickFolder(event, this)" title="${escapeHtml(path)} (klik untuk menyaring folder ini)">${FT_ICON.replace('class="ft-ico"', "")}<span>${escapeHtml(short)}</span></a>`;
}
function invPickFolder(ev, el) {
  if (ev && ev.stopPropagation) ev.stopPropagation();
  const path = decodeURIComponent(el.dataset.fp || "");
  if (!path) return;
  FT.sel = path;
  const parts = path.split("/");
  for (let i = 1; i < parts.length; i++)
    FT.open.add(parts.slice(0, i).join("/"));
  ftSaveOpen();
  inventoryState.page = 1;
  ftRenderAll();
  renderInventoryTable();
}
const INV_COLS = [
  ["type", "Jenis"],
  ["loc", "Lokasi"],
  ["folder", "Folder"],
  ["cap", "Kapasitas"],
  ["len", "Panjang"],
  ["inst", "Pemasangan"],
  ["status", "Status"],
];
const INVCOL = { hide: new Set() };
try {
  const sv = JSON.parse(localStorage.getItem("netgis.invColsHidden") || "[]");
  if (Array.isArray(sv)) sv.forEach((k) => INVCOL.hide.add(String(k)));
} catch (_) {
  /* abaikan */
}
function invApplyCols() {
  const t = document.getElementById("inventory-table");
  if (!t || !t.classList) return;
  INV_COLS.forEach(([k]) => t.classList.toggle("hc-" + k, INVCOL.hide.has(k)));
}
function invColToggle(k, on) {
  if (on) INVCOL.hide.delete(k);
  else INVCOL.hide.add(k);
  try {
    localStorage.setItem(
      "netgis.invColsHidden",
      JSON.stringify([...INVCOL.hide]),
    );
  } catch (_) {
    /* abaikan */
  }
  invApplyCols();
}
function invColMenu(ev) {
  if (ev && ev.stopPropagation) ev.stopPropagation();
  const old = document.getElementById("inv-colmenu");
  if (old) {
    old.remove();
    return;
  }
  const m = document.createElement("div");
  m.id = "inv-colmenu";
  m.className = "ft-colmenu";
  m.innerHTML =
    INV_COLS.map(
      ([k, label]) =>
        `<label><input type="checkbox" data-k="${k}" ${INVCOL.hide.has(k) ? "" : "checked"}> ${label}</label>`,
    ).join("") +
    `<div class="ibu-note" style="margin:4px 6px 2px">Kolom Aset dan Aksi selalu tampil.</div>`;
  m.addEventListener("change", (e) => {
    const c = e.target;
    if (c && c.dataset && c.dataset.k) invColToggle(c.dataset.k, c.checked);
  });
  m.addEventListener("click", (e) => e.stopPropagation());
  document.body.appendChild(m);
  const b =
    ev && ev.currentTarget && ev.currentTarget.getBoundingClientRect
      ? ev.currentTarget.getBoundingClientRect()
      : { right: 300, bottom: 200 };
  m.style.left =
    Math.max(6, Math.min(b.right - 170, (window.innerWidth || 1200) - 180)) +
    "px";
  m.style.top = b.bottom + 4 + "px";
  const off = (e) => {
    if (e.type === "click" && e.target && m.contains && m.contains(e.target))
      return; // klik di dalam menu: tetap terbuka
    if (e.type === "keydown") {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      e.preventDefault();
    }
    m.remove();
    document.removeEventListener("click", off, true);
    document.removeEventListener("keydown", off, true);
  };
  setTimeout(() => {
    document.addEventListener("click", off, true);
    document.addEventListener("keydown", off, true);
  }, 0);
}

// Ubah massal: hanya kolom yang diisi yang diubah; tiap aset memakai aturan ubah satuan di server (dikirim per 100 aset)
const IBU = { busy: false };
function ibuItems() {
  return [...invSel.values()];
}
function ibuOpen() {
  const items = ibuItems();
  if (!items.length || !ftCanW()) return;
  const old = document.getElementById("ibu");
  if (old) old.remove();
  const wrap = document.createElement("div");
  wrap.id = "ibu";
  wrap.className = "modal-overlay";
  wrap.style.display = "flex";
  wrap.style.zIndex = "5000";
  const nodes = items.filter((x) => x.kind === "node").length,
    cables = items.length - nodes;
  const opt = (v, l) =>
    `<option value="${escapeHtml(v)}">${escapeHtml(l)}</option>`;
  wrap.innerHTML =
    `<div class="modal-container ibu-box"><div class="modal-header"><h3><i class="fa-solid fa-pen-to-square"></i> Ubah massal ${items.length} aset</h3>` +
    `<button class="btn-close" type="button" onclick="ibuClose()">&times;</button></div><div class="modal-body">` +
    `<div class="ibu-note">${nodes} titik &middot; ${cables} kabel. Biarkan kolom tetap <b>(tidak diubah)</b> bila tidak perlu diganti.</div>` +
    `<div class="ibu-grid">` +
    `<label for="ibu-status">Status</label><select id="ibu-status" onchange="ibuSummary()">${opt("", "(tidak diubah)")}${opt("Active", "Active")}${opt("Maintenance", "Maintenance")}${opt("Cut/Broken", "Cut/Broken")}</select>` +
    `<label for="ibu-cluster">Cluster</label><select id="ibu-cluster" onchange="ibuClusterChange()">${opt("", "(tidak diubah)")}${clusterNames()
      .map((c) => opt(c, c))
      .join("")}</select>` +
    `<label for="ibu-area">Area</label><select id="ibu-area" onchange="ibuSummary()">${opt("", "(tidak diubah)")}${uniqCI(
      SCOPE_OPTS.pairs.filter((p) => p.registered !== false).map((p) => p.area),
    )
      .map((a) => opt(a, a))
      .join("")}</select>` +
    `<label for="ibu-city">Kota</label><input id="ibu-city" type="text" maxlength="120" placeholder="(tidak diubah)" oninput="ibuSummary()">` +
    `<label for="ibu-inst">Pemasangan</label><select id="ibu-inst" onchange="ibuSummary()">${opt("", "(tidak diubah)")}${opt("Udara", "Udara")}${opt("Tanah", "Tanah")}</select>` +
    `<label for="ibu-folder">Folder</label><select id="ibu-folder" onchange="ibuSummary()">${opt("", "(tidak diubah)")}${FT.list.map((f) => opt(f.path, f.path)).join("")}</select>` +
    (cables
      ? `<label for="ibu-ctype">Jenis kabel</label><select id="ibu-ctype" onchange="ibuSummary()">${opt("", "(tidak diubah)")}${["Backbone", "Feeder", "Distribution", "Drop"].map((t) => opt(t, t === "Drop" ? "Drop (Dropcore)" : t)).join("")}</select>` +
        `<label for="ibu-ccap">Kapasitas kabel</label><select id="ibu-ccap" onchange="ibuSummary()">${opt("", "(tidak diubah)")}${[1, 2, 4, 6, 8, 12, 24, 36, 48, 72, 96, 144, 288].map((c) => opt(c + "C", c + "C")).join("")}</select>` +
        `<label for="ibu-fmode">Jenis serat</label><select id="ibu-fmode" onchange="ibuSummary()">${opt("", "(tidak diubah)")}${opt("SM", "Single Mode (SM)")}${opt("MM", "Multi Mode (MM)")}</select>`
      : "") +
    (nodes
      ? `<label for="ibu-ntype">Jenis titik</label><select id="ibu-ntype" onchange="ibuSummary()">${opt("", "(tidak diubah)")}${["POP", "CLOSURE", "ODP", "HH", "TIANG", "SLACK", "PELANGGAN"].map((t) => opt(t, t)).join("")}</select>` +
        `<label for="ibu-ncap">Kapasitas titik</label><input id="ibu-ncap" type="text" maxlength="60" placeholder="(tidak diubah) mis. 1 In - 8 Out" oninput="ibuSummary()">`
      : "") +
    `</div><div class="ibu-note">Kolom jenis/kapasitas kabel hanya berlaku untuk kabel, jenis/kapasitas titik hanya untuk titik, Pemasangan hanya untuk kabel. Cluster dan Area harus dari daftar Wilayah; bila hanya Area dipilih, Cluster mengikuti Area itu. Nama dan koordinat tidak diubah di sini. Mengubah jenis titik tidak mengubah sambungan yang sudah ada.</div>` +
    `<div id="ibu-sum" class="ibu-sum">Belum ada perubahan yang diisi.</div><div id="ibu-err" class="ibu-err" style="display:none"></div>` +
    `<div class="ibu-act"><button type="button" class="inv-bar-btn" onclick="ibuClose()">Batal</button>` +
    `<button type="button" id="ibu-go" class="inv-bar-btn primary" onclick="ibuGo()" disabled><i class="fa-solid fa-check"></i> Terapkan</button></div></div></div>`;
  document.body.appendChild(wrap);
}
function ibuClose() {
  if (IBU.busy) return;
  const m = document.getElementById("ibu");
  if (m) m.remove();
}
function ibuClusterChange() {
  const c = (document.getElementById("ibu-cluster") || {}).value || "";
  const el = document.getElementById("ibu-area");
  if (!el) return;
  const areas = c
    ? areaNamesFor(c)
    : uniqCI(
        SCOPE_OPTS.pairs
          .filter((p) => p.registered !== false)
          .map((p) => p.area),
      );
  el.innerHTML =
    '<option value="">(tidak diubah)</option>' +
    areas
      .map((a) => `<option value="${escapeHtml(a)}">${escapeHtml(a)}</option>`)
      .join("");
  ibuSummary();
}
function ibuValues() {
  const g = (id) => ((document.getElementById(id) || {}).value || "").trim();
  return {
    status: g("ibu-status"),
    cluster: g("ibu-cluster"),
    area: g("ibu-area"),
    city: g("ibu-city"),
    installation: g("ibu-inst"),
    folder: g("ibu-folder"),
    cable_type: g("ibu-ctype"),
    cable_capacity: g("ibu-ccap"),
    fiber_mode: g("ibu-fmode"),
    node_type: g("ibu-ntype"),
    node_capacity: g("ibu-ncap"),
  };
}
function ibuSummary() {
  const v = ibuValues();
  const lab = {
    status: "Status",
    cluster: "Cluster",
    area: "Area",
    city: "Kota",
    installation: "Pemasangan",
    folder: "Folder",
    cable_type: "Jenis kabel",
    cable_capacity: "Kapasitas kabel",
    fiber_mode: "Jenis serat",
    node_type: "Jenis titik",
    node_capacity: "Kapasitas titik",
  };
  const parts = Object.keys(lab)
    .filter((k) => v[k])
    .map((k) => `${lab[k]} → <b>${escapeHtml(v[k])}</b>`);
  const sum = document.getElementById("ibu-sum"),
    go = document.getElementById("ibu-go");
  const n = ibuItems().length;
  let bad = "";
  if (v.cluster && !v.area) bad = "Pilih juga Area untuk cluster yang dipilih.";
  if (sum)
    sum.innerHTML = bad
      ? escapeHtml(bad)
      : parts.length
        ? `${n} aset akan diubah: ${parts.join("; ")}`
        : "Belum ada perubahan yang diisi.";
  if (go) go.disabled = !parts.length || !!bad;
  return !!parts.length && !bad;
}
async function ibuGo() {
  if (IBU.busy || !ibuSummary()) return;
  const v = ibuValues(),
    items = ibuItems();
  if (!confirm(`Ubah ${items.length} aset sekarang?`)) return;
  IBU.busy = true;
  const go = document.getElementById("ibu-go");
  if (go) go.disabled = true;
  const errBox = document.getElementById("ibu-err");
  const failed = [];
  let updated = 0,
    ignored = 0,
    done = 0,
    na = 0;
  try {
    for (let i = 0; i < items.length; i += 100) {
      const chunk = items.slice(i, i + 100);
      const body = Object.assign({}, v, {
        nodes: chunk.filter((x) => x.kind === "node").map((x) => x.id),
        cables: chunk.filter((x) => x.kind === "cable").map((x) => x.id),
      });
      const r = await apiRequest("/api/assets/bulk-update", "POST", body);
      updated += r.updated || 0;
      ignored += r.installation_ignored_nodes || 0;
      na += r.not_applicable || 0;
      (r.failed || []).forEach((f) => failed.push(f));
      done += chunk.length;
      const sum = document.getElementById("ibu-sum");
      if (sum) sum.textContent = `Memproses ${done} / ${items.length} aset…`;
    }
  } catch (err) {
    failed.push({
      name: "(permintaan)",
      reason: (err && err.message) || String(err),
    });
  }
  IBU.busy = false;
  const failedKeys = new Set(
    failed.filter((f) => f.kind).map((f) => `${f.kind.toLowerCase()}:${f.id}`),
  );
  [...invSel.keys()].forEach((k) => {
    if (!failedKeys.has(k)) invSel.delete(k);
  });
  invLastIdx = null;
  await ftAfterChange();
  invRefreshSel();
  if (!failed.length) {
    ibuClose();
    invToast(
      `${updated} aset diubah${ignored ? ` (Pemasangan diabaikan untuk ${ignored} titik)` : ""}${na ? ` — ${na} aset tidak terpengaruh (kolom tidak berlaku untuk jenis aset itu)` : ""}`,
    );
  } else {
    if (errBox) {
      errBox.style.display = "block";
      errBox.innerHTML =
        `<b>${updated} berhasil, ${failed.length} dilewati:</b><br>` +
        failed
          .slice(0, 30)
          .map(
            (f) => `${escapeHtml(f.name || "")}: ${escapeHtml(f.reason || "")}`,
          )
          .join("<br>");
    }
    const sum = document.getElementById("ibu-sum");
    if (sum)
      sum.textContent = `${updated} aset diubah, ${failed.length} dilewati (masih terpilih agar bisa dicoba lagi).`;
    if (go) go.disabled = false;
  }
}

// ---- Inventory: pilih banyak (checkbox, Shift+klik rentang, Ctrl+klik tambah) & hapus massal dengan progres ----
const invSel = new Map(); // "node:12" -> {kind, id, name, type}
let invRows = [];
let invLastIdx = null;
let invSelSig = "";
let invBusy = false;
const INV_BULK_CHUNK = 20;
const INV_SELECT_ALL_MAX = 2000;

function invActiveFilters() {
  const g = (id) => (document.getElementById(id) || {}).value || "";
  const out = [];
  if (g("inventory-search").trim())
    out.push(`cari "${g("inventory-search").trim()}"`);
  const t = g("filter-type");
  if (t && t !== "ALL") out.push(`jenis ${t}`);
  const st = g("filter-status");
  if (st && st !== "ALL") out.push(`status ${st}`);
  const ins = g("filter-installation");
  if (ins && ins !== "ALL") out.push(`pemasangan ${ins}`);
  if (SCOPE.region && SCOPE.region !== "ALL")
    out.push(
      `regional ${(SCOPE_OPTS.regions.find((r) => r.id === SCOPE.region) || {}).value || SCOPE.region}`,
    );
  if (SCOPE.cluster && SCOPE.cluster !== "ALL")
    out.push(`cluster ${SCOPE.cluster}`);
  if (SCOPE.area && SCOPE.area !== "ALL") out.push(`area ${SCOPE.area}`);
  if (FT.sel) out.push(`folder ${FT.sel}`);
  return out;
}
function invResetFilters() {
  ["inventory-search"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.value = "";
  });
  ["filter-type", "filter-status", "filter-installation"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.value = "ALL";
  });
  inventoryState.page = 1;
  renderInventoryChips();
  renderInventoryTable();
}
function invCanSel() {
  return can("asset.delete") || can("asset.write");
}
function invItemKey(it) {
  return `${String(it.category || "").toLowerCase()}:${Number(it.id)}`;
}
function invPutSel(it, on) {
  const k = invItemKey(it);
  if (on)
    invSel.set(k, {
      kind: String(it.category || "").toLowerCase(),
      id: Number(it.id),
      name: it.name || `#${it.id}`,
      type: it.type || "",
    });
  else invSel.delete(k);
}
function invSetRange(a, b, on) {
  const lo = Math.min(a, b),
    hi = Math.max(a, b);
  for (let i = lo; i <= hi; i++) if (invRows[i]) invPutSel(invRows[i], on);
}
function invChkClick(e, el) {
  e.stopPropagation();
  if (invBusy) {
    e.preventDefault();
    return;
  }
  const idx = Number(el.closest("tr").dataset.idx);
  if (e.shiftKey && invLastIdx != null)
    invSetRange(invLastIdx, idx, el.checked);
  else invPutSel(invRows[idx], el.checked);
  invLastIdx = idx;
  invRefreshSel();
}
function invRowClick(e, tr) {
  if (invBusy || !invCanSel()) return;
  if (e.target.closest("button, a, input, select, label")) return;
  const idx = Number(tr.dataset.idx);
  if (e.shiftKey) {
    const on =
      invLastIdx != null && invRows[invLastIdx]
        ? invSel.has(invItemKey(invRows[invLastIdx])) || true
        : true;
    invSetRange(invLastIdx != null ? invLastIdx : idx, idx, on);
    invLastIdx = idx;
  } else {
    // klik biasa = pilih/batal pilih satu baris (Ctrl/Cmd sama; Shift = rentang)
    invPutSel(invRows[idx], !invSel.has(invItemKey(invRows[idx])));
    invLastIdx = idx;
  }
  invRefreshSel();
}
function invToggleAll(el) {
  if (invBusy) {
    el.checked = !el.checked;
    return;
  }
  invRows.forEach((it) => invPutSel(it, el.checked));
  invLastIdx = null;
  invRefreshSel();
}
function invClearSel() {
  if (invBusy) return;
  invSel.clear();
  invLastIdx = null;
  invRefreshSel();
}
function invRefreshSel() {
  const tbody = document.getElementById("inventory-table-body");
  if (tbody)
    tbody.querySelectorAll("tr[data-key]").forEach((tr) => {
      const on = invSel.has(tr.dataset.key);
      tr.classList.toggle("sel", on);
      const c = tr.querySelector(".inv-chk");
      if (c) c.checked = on;
    });
  const all = document.getElementById("inv-chk-all");
  if (all) {
    const n = invRows.filter((it) => invSel.has(invItemKey(it))).length;
    all.checked = invRows.length > 0 && n === invRows.length;
    all.indeterminate = n > 0 && n < invRows.length;
  }
  const bar = document.getElementById("inv-selbar");
  if (!bar) return;
  const n = invSel.size;
  if (!invCanSel() || (n === 0 && !invBusy)) {
    bar.style.display = "none";
    return;
  }
  bar.style.display = "flex";
  if (invBusy) return; // saat menghapus, isi bar diatur oleh invBulkDelete
  const nodes = [...invSel.values()].filter((x) => x.kind === "node").length;
  const cables = n - nodes;
  const more =
    inventoryState.total > n
      ? `<button type="button" class="inv-bar-btn" onclick="invSelectAllFiltered()">Pilih semua ${Number(inventoryState.total)} hasil filter</button>`
      : "";
  bar.innerHTML =
    `<span class="inv-bar-count"><b>${n}</b> aset dipilih<small>${nodes ? ` &middot; ${nodes} titik` : ""}${cables ? ` &middot; ${cables} kabel` : ""}</small></span>` +
    `<span class="inv-bar-tip">Shift+klik = rentang &middot; Ctrl+klik = tambah satu</span>` +
    `<span class="inv-bar-act">${more}<button type="button" class="inv-bar-btn" onclick="invClearSel()">Batal pilihan</button>` +
    `${can("asset.write") ? `<button type="button" class="inv-bar-btn primary" onclick="ibuOpen()"><i class="fa-solid fa-pen-to-square"></i> Ubah massal</button>` : ""}` +
    `${can("asset.write") ? `<button type="button" class="inv-bar-btn" onclick="invAutoName()"><i class="fa-solid fa-wand-magic-sparkles"></i> Nama otomatis</button>` : ""}` +
    `${can("asset.write") ? `<button type="button" class="inv-bar-btn" onclick="ftMoveDialog()"><i class="fa-solid fa-folder-tree"></i> Pindahkan ke folder</button>` : ""}` +
    `${can("asset.delete") ? `<button type="button" class="inv-bar-btn danger" onclick="invBulkDelete()"><i class="fa-solid fa-trash"></i> Hapus ${n} aset</button>` : ""}</span>`;
}
function invBarProgress(text, pct) {
  const bar = document.getElementById("inv-selbar");
  if (!bar) return;
  bar.style.display = "flex";
  bar.innerHTML = `<div class="inv-prog"><div class="inv-prog-txt">${escapeHtml(text)}</div><div class="inv-prog-track"><div class="inv-prog-fill${pct == null ? " indet" : ""}" style="width:${pct == null ? 40 : Math.max(2, pct)}%"></div></div></div>`;
}
function invFilterParams(extra) {
  return new URLSearchParams(
    Object.assign(
      {
        q: document.getElementById("inventory-search").value.trim(),
        region: SCOPE.region,
        cluster: SCOPE.cluster,
        area: SCOPE.area,
        type: document.getElementById("filter-type").value,
        status: (document.getElementById("filter-status") || {}).value || "ALL",
        installation:
          (document.getElementById("filter-installation") || {}).value || "ALL",
        folder: FT.sel || "",
        sort: inventoryState.sort,
        order: inventoryState.order,
      },
      extra || {},
    ),
  );
}
async function invSelectAllFiltered() {
  if (invBusy) return;
  const total = inventoryState.total;
  if (total > INV_SELECT_ALL_MAX)
    return alert(
      `Hasil filter ada ${total} aset; batas pilih semua ${INV_SELECT_ALL_MAX}. Persempit filter dulu.`,
    );
  invBusy = true;
  try {
    const pages = Math.ceil(total / 200);
    for (let p = 1; p <= pages; p++) {
      invBarProgress(
        `Mengambil daftar aset... ${Math.min((p - 1) * 200, total)} dari ${total}`,
        ((p - 1) / pages) * 100,
      );
      const res = await apiRequest(
        `/api/inventory?${invFilterParams({ page: String(p), page_size: "200" })}`,
      );
      (res.items || []).forEach((it) => invPutSel(it, true));
    }
  } catch (err) {
    alert("Gagal mengambil daftar aset: " + err.message);
  }
  invBusy = false;
  invRefreshSel();
}
async function invBulkDelete() {
  if (invBusy || !invCanSel()) return;
  const list = [...invSel.values()];
  if (!list.length) return;
  const nodes = list.filter((x) => x.kind === "node");
  const cables = list.filter((x) => x.kind === "cable");
  const sample =
    list
      .slice(0, 5)
      .map((x) => x.name)
      .join(", ") + (list.length > 5 ? `, ... (+${list.length - 5})` : "");
  if (
    !confirm(
      `Hapus ${list.length} aset (${nodes.length} titik, ${cables.length} kabel)?\n\n${sample}\n\nSambungan core yang terkait ikut terhapus. Tiap penghapusan tercatat di Riwayat (audit).`,
    )
  )
    return;
  if (list.length >= 20) {
    const t = prompt(
      `Anda akan menghapus ${list.length} aset sekaligus.\nKetik HAPUS untuk melanjutkan:`,
    );
    if (
      String(t || "")
        .trim()
        .toUpperCase() !== "HAPUS"
    )
      return;
  }
  invBusy = true;
  const ordered = cables.concat(nodes); // kabel dulu, baru titik
  const failed = [];
  let done = 0,
    ok = 0;
  invBarProgress(`Menghapus 0 dari ${ordered.length}...`, 0);
  for (let i = 0; i < ordered.length; i += INV_BULK_CHUNK) {
    const chunk = ordered.slice(i, i + INV_BULK_CHUNK);
    try {
      const r = await apiRequest("/api/assets/bulk-delete", "POST", {
        nodes: chunk.filter((x) => x.kind === "node").map((x) => x.id),
        cables: chunk.filter((x) => x.kind === "cable").map((x) => x.id),
      });
      ok += r.deleted_total || 0;
      const bad = new Set(
        (r.failed || []).map((f) => `${f.kind.toLowerCase()}:${f.id}`),
      );
      chunk.forEach((x) => {
        if (!bad.has(`${x.kind}:${x.id}`)) invSel.delete(`${x.kind}:${x.id}`);
      });
      (r.failed || []).forEach((f) =>
        failed.push(
          `${(invSel.get(`${f.kind.toLowerCase()}:${f.id}`) || {}).name || "#" + f.id}: ${f.detail}`,
        ),
      );
    } catch (err) {
      chunk.forEach((x) => failed.push(`${x.name}: ${err.message}`));
    }
    done += chunk.length;
    invBarProgress(
      `Menghapus ${done} dari ${ordered.length}...`,
      (done / ordered.length) * 100,
    );
  }
  invBusy = false;
  invLastIdx = null;
  try {
    await loadData();
  } catch (_) {
    /* tabel disegarkan ulang di bawah */
  }
  renderInventoryTable();
  if (failed.length)
    alert(
      `${ok} aset terhapus, ${failed.length} gagal:\n\n` +
        failed.slice(0, 8).join("\n") +
        (failed.length > 8 ? `\n... dan ${failed.length - 8} lainnya` : ""),
    );
  else invToast(`${ok} aset berhasil dihapus`);
}
function invToast(msg) {
  const bar = document.getElementById("inv-selbar");
  if (!bar) return;
  bar.style.display = "flex";
  bar.className = "inv-selbar ok";
  bar.innerHTML = `<span class="inv-bar-count"><i class="fa-solid fa-circle-check"></i> ${escapeHtml(msg)}</span>`;
  setTimeout(() => {
    if (!invBusy && invSel.size === 0) {
      bar.style.display = "none";
    }
    bar.className = "inv-selbar";
  }, 3500);
}

// Pindah ke aset di peta dari mana pun (modal core, ringkasan, inventory)
function focusAssetOnMap(kind, id) {
  const item = allInventoryData.find(
    (x) => x.category.toLowerCase() === kind && String(x.id) === String(id),
  );
  if (!item || !Number.isFinite(item.lat) || !Number.isFinite(item.lng))
    return alert("Aset tidak ditemukan di peta (mungkin sudah dihapus).");
  closeCoreDetailModal();
  closeInventoryModal();
  closeSummaryModal();
  map.flyTo([item.lat, item.lng], 18, { duration: 1.5 });
  setTimeout(() => openAssetPopup(`${kind}:${id}`), 1650);
}

function zoomToAsset(kind, id) {
  const item = allInventoryData.find(
    (x) => x.category.toLowerCase() === kind && String(x.id) === String(id),
  );
  if (!item) return;
  closeInventoryModal();
  map.flyTo([item.lat, item.lng], 18, { duration: 1.5 });
  setTimeout(() => openAssetPopup(`${kind}:${id}`), 1650);
}

// Cluster & Area di form aset memakai DAFTAR RESMI menu Wilayah (SCOPE_OPTS dari /api/filters/options); hanya admin yang menambah.
const AREA_MAPPING = {}; // dahulu daftar tetap; kini kosong, semua dari menu Wilayah

function uniqCI(list) {
  const seen = new Set();
  return list.filter((v) => {
    const k = String(v || "")
      .trim()
      .toUpperCase();
    if (!k || seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}
function clusterNames() {
  return uniqCI(
    SCOPE_OPTS.clusters
      .filter((c) => c.registered !== false)
      .map((c) => c.value),
  );
}
function areaNamesFor(cluster) {
  const key = String(cluster || "").toUpperCase();
  return uniqCI(
    SCOPE_OPTS.pairs
      .filter(
        (p) =>
          p.registered !== false && String(p.cluster).toUpperCase() === key,
      )
      .map((p) => p.area),
  );
}
function fillAssetClusterOptions(selected) {
  const el = document.getElementById("asset-cluster");
  if (!el) return;
  const names = uniqCI(clusterNames().concat(selected ? [selected] : [])); // nilai lama di luar daftar tetap ditampilkan saat mengedit
  el.innerHTML =
    '<option value="">-- Pilih cluster --</option>' +
    names
      .map((c) => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`)
      .join("");
  const hit =
    selected &&
    names.find((c) => c.toUpperCase() === String(selected).toUpperCase());
  el.value = hit || "";
}
function updateAreaOptions(selected) {
  const clusterSelect = document.getElementById("asset-cluster");
  const areaSelect = document.getElementById("asset-area");
  if (!clusterSelect || !areaSelect) return;
  const areas = uniqCI(
    areaNamesFor(clusterSelect.value).concat(selected ? [selected] : []),
  );
  areaSelect.innerHTML =
    '<option value="">-- Pilih area --</option>' +
    areas
      .map((a) => `<option value="${escapeHtml(a)}">${escapeHtml(a)}</option>`)
      .join("");
  const pick =
    selected &&
    areas.find((a) => a.toUpperCase() === String(selected).toUpperCase());
  areaSelect.value = pick || "";
}
function onAssetClusterChange() {
  updateAreaOptions();
}
function onAssetAreaChange() {
  /* area dipilih dari daftar resmi; tidak ada "+ baru" */
}

const GEOCODE_PENDING_TEXT = "Mendeteksi lokasi...";

function fetchCityName(lat, lng) {
  const cityInput = document.getElementById("asset-city");
  cityInput.value = GEOCODE_PENDING_TEXT;

  fetch(
    `https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat=${lat}&lon=${lng}`,
  )
    .then((res) => res.json())
    .then((data) => {
      const addr = data.address || {};
      cityInput.value =
        addr.city ||
        addr.town ||
        addr.city_district ||
        addr.county ||
        addr.state ||
        "Kota Tidak Diketahui";
    })
    .catch((err) => {
      console.error("Geocoding failed:", err);
      cityInput.value = "Kota Banjarmasin"; // Fallback default jika offline/error
    });
}

// Isi dropdown topologi (Parent/Upstream) dari inventory yang sudah dimuat
function populateTopologyOptions(category) {
  const nodes = allInventoryData.filter((x) => x.category === "NODE");
  const cables = allInventoryData.filter((x) => x.category === "CABLE");
  const options = (list, emptyLabel) =>
    `<option value="">${emptyLabel}</option>` +
    list
      .map(
        (x) =>
          `<option value="${Number(x.id)}">${escapeHtml(x.name)} (${escapeHtml(x.type)})</option>`,
      )
      .join("");

  const fill = (id, html) => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = html;
  };
  fill("asset-parent-node", options(nodes, "-- Tidak ada --"));
  fill("asset-upstream-cable", options(cables, "-- Tidak ada --"));
  fill("asset-from-node", options(nodes, "-- Tidak ada --"));
  fill("asset-to-node", options(nodes, "-- Tidak ada --"));
  fill("asset-parent-cable", options(cables, "-- Tidak ada --"));

  const show = (id, visible) => {
    const el = document.getElementById(id);
    if (el) el.style.display = visible ? "block" : "none";
  };
  show("installation-box", category === "CABLE");
  show("topo-node-box", category === "NODE");
  show("topo-cable-box", category === "CABLE");
}

function openAddAssetModal(
  typeCategory,
  lat,
  lng,
  coordsArr = null,
  skipGeocode = false,
) {
  document.getElementById("modal-add-asset").style.display = "flex";
  document.getElementById("asset-edit-id").value = "";
  document.getElementById("asset-status").disabled = false;
  document.getElementById("asset-category-type").value = typeCategory; // 'NODE' atau 'CABLE'
  document.getElementById("asset-name").value = "";
  OTB_EDIT = [];
  OTB_BASE_SPEC = {};
  assetParamsReset();

  // Cluster & area: ikut filter wilayah yang sedang aktif; bila tidak ada filter, pengguna wajib memilih
  fillAssetClusterOptions(SCOPE.cluster !== "ALL" ? SCOPE.cluster : "");
  updateAreaOptions(SCOPE.area !== "ALL" ? SCOPE.area : undefined);

  const typeSelect = document.getElementById("asset-type");

  if (typeCategory === "NODE") {
    document.getElementById("form-asset-title").innerHTML =
      `<i class="fa-solid fa-location-dot"></i> Tambah Node / Device Baru`;
    document.getElementById("asset-lat").value = lat;
    document.getElementById("asset-lng").value = lng;

    typeSelect.disabled = false;
    typeSelect.innerHTML = `
            <option value="ODP">ODP</option>
            <option value="POP">POP / Headend</option>
            <option value="CLOSURE">Joint Closure</option>
            <option value="PELANGGAN">Pelanggan</option>
            <option value="TIANG">Tiang</option>
            <option value="HH">Handhole (HH)</option>
            <option value="SLACK">Slack Kabel</option>
            <option value="INCIDENT">Titik Incident</option>
        `;
    if (!skipGeocode) fetchCityName(lat, lng);
  } else {
    document.getElementById("form-asset-title").innerHTML =
      `<i class="fa-solid fa-route"></i> Tambah Kabel / Jalur Baru`;
    document.getElementById("asset-coords-json").value =
      JSON.stringify(coordsArr);

    typeSelect.disabled = false;
    typeSelect.innerHTML = `
            <option value="Feeder">Kabel Feeder</option>
            <option value="Distribution">Kabel Distribusi</option>
            <option value="Backbone">Kabel Backbone</option>
            <option value="Drop">Kabel Dropcore</option>
        `;
    // Geocode titik pertama kabel
    if (!skipGeocode) fetchCityName(coordsArr[0][1], coordsArr[0][0]);
  }

  populateTopologyOptions(typeCategory);
  onAssetTypeChange();
  const acBox = document.getElementById("ac-box");
  if (acBox) acBox.style.display = typeCategory === "CABLE" ? "block" : "none";
}

// Mode EDIT memakai form yang sama dengan "Tambah" (posisi/geometri tidak diubah di sini)
function openEditAssetModal(category, id, props) {
  const ready = allInventoryData.length
    ? Promise.resolve()
    : fetchInventoryData();
  ready.then(() => {
    // pakai form tambah sebagai dasar, lalu isi dengan data aset
    openAddAssetModal(
      category,
      props.latitude ?? 0,
      props.longitude ?? 0,
      [
        [0, 0],
        [0, 0],
      ],
      true,
    );
    document.getElementById("asset-edit-id").value = String(id);
    const acb = document.getElementById("ac-box");
    if (acb) acb.style.display = "none";
    document.getElementById("asset-coords-json").value = "";
    document.getElementById("form-asset-title").innerHTML =
      `<i class="fa-solid fa-pen-to-square"></i> Edit ${category === "NODE" ? "Node / Device" : "Kabel / Jalur"}: ${escapeHtml(props.name)}`;

    const setVal = (elId, v) => {
      const el = document.getElementById(elId);
      if (!el) return;
      // pastikan nilai lama tetap tersedia walau tidak ada di daftar pilihan
      if (
        v != null &&
        v !== "" &&
        ![...el.options].some((o) => o.value === String(v))
      ) {
        el.insertAdjacentHTML(
          "beforeend",
          `<option value="${escapeHtml(String(v))}">${escapeHtml(String(v))}</option>`,
        );
      }
      el.value = v == null ? "" : String(v);
    };

    document.getElementById("asset-name").value = props.name || "";
    setVal("asset-type", props.type);
    if (category === "NODE" && (props.type || "").toUpperCase() === "POP")
      otbLoad(props.capacity, props.spec_data);
    onAssetTypeChange();
    if (!(category === "NODE" && (props.type || "").toUpperCase() === "POP"))
      setVal("asset-capacity", props.capacity);
    fillAssetClusterOptions(props.cluster);
    updateAreaOptions(props.area);
    document.getElementById("asset-city").value = props.city || "";
    // status diubah lewat tombol status di popup (menjaga logika dampak insiden)
    setVal("asset-status", props.status);
    document.getElementById("asset-status").disabled = true;

    // topologi: jangan pernah menawarkan diri sendiri sebagai parent/hulu
    const dropSelf = (elId, cat) => {
      const el = document.getElementById(elId);
      if (el && cat === category)
        [...el.options].forEach((o) => {
          if (o.value === String(id)) o.remove();
        });
    };
    ["asset-parent-node", "asset-from-node", "asset-to-node"].forEach((x) =>
      dropSelf(x, "NODE"),
    );
    dropSelf("asset-parent-cable", "CABLE");
    if (category === "NODE") {
      setVal("asset-parent-node", props.parent_node_id);
      setVal("asset-upstream-cable", props.upstream_cable_id);
      assetParamsFill(props);
    } else {
      setVal("asset-installation", props.installation);
      setVal("asset-fiber-mode", props.fiber_mode || "SM");
      setVal("asset-parent-cable", props.parent_cable_id);
      setVal("asset-from-node", props.from_node_id);
      setVal("asset-to-node", props.to_node_id);
    }
  });
}

function closeAddAssetModal() {
  document.getElementById("modal-add-asset").style.display = "none";
  const editId = document.getElementById("asset-edit-id");
  if (editId) editId.value = "";
  const st = document.getElementById("asset-status");
  if (st) st.disabled = false;
}

// --- PARAMETER ASET: kode registrasi, data pelanggan, trunk POP; pemeriksaan keunikan ---
const ASSET_NO_REG = ["HH", "SLACK", "INCIDENT"];
const ASSET_SERVICES = [
  "Internet",
  "Dedicated Internet",
  "IPTV",
  "VPN L2",
  "VPN L3",
  "Lainnya",
];
const ASSET_UNIQ = { timer: null, token: 0, bad: {} };
function assetNormCode(v) {
  return String(v || "")
    .replace(/\s+/g, "")
    .toUpperCase();
}
function bwToMbps(v, unit) {
  const n = parseFloat(String(v == null ? "" : v).replace(",", "."));
  if (!isFinite(n)) return null;
  return unit === "Gbps" ? Math.round(n * 1000 * 1000) / 1000 : n;
}
function bwSplit(mbps) {
  if (mbps == null || mbps === "") return { v: "", u: "Mbps" };
  const n = Number(mbps);
  return n >= 1000 && n % 1000 === 0
    ? { v: String(n / 1000), u: "Gbps" }
    : { v: String(n), u: "Mbps" };
}
function fmtBw(mbps) {
  if (mbps == null || mbps === "" || !isFinite(Number(mbps))) return "-";
  const n = Number(mbps);
  return n >= 1000
    ? `${(n / 1000).toLocaleString("id-ID", { maximumFractionDigits: 2 })} Gbps`
    : `${n.toLocaleString("id-ID", { maximumFractionDigits: 2 })} Mbps`;
}
function assetEl(id) {
  return document.getElementById(id);
}
function assetParamsToggle() {
  const cat = (assetEl("asset-category-type") || {}).value;
  const t = ((assetEl("asset-type") || {}).value || "").toUpperCase();
  const isNode = cat === "NODE";
  const show = (id, on) => {
    const e = assetEl(id);
    if (e) e.style.display = on ? "" : "none";
  };
  show("reg-box", isNode && !ASSET_NO_REG.includes(t));
  show("cust-box", isNode && t === "PELANGGAN");
  show("trunk-box", isNode && t === "POP");
  const dl = assetEl("asset-service-list");
  if (dl && !dl.innerHTML)
    dl.innerHTML = ASSET_SERVICES.map(
      (x) => `<option value="${escapeHtml(x)}">`,
    ).join("");
}
function assetParamsReset() {
  [
    "asset-reg",
    "asset-service",
    "asset-bw",
    "asset-sn",
    "asset-trunk",
    "asset-overbook",
  ].forEach((i) => {
    const e = assetEl(i);
    if (e) e.value = "";
  });
  const lt = assetEl("asset-linktype");
  if (lt) lt.value = "";
  const u = assetEl("asset-bw-unit");
  if (u) u.value = "Mbps";
  const tu = assetEl("asset-trunk-unit");
  if (tu) tu.value = "Gbps";
  ASSET_UNIQ.bad = {};
  ["asset-name-msg", "asset-reg-msg", "asset-sn-msg"].forEach((i) => {
    const e = assetEl(i);
    if (e) {
      e.textContent = "";
      e.className = "ap-msg";
    }
  });
}
function assetParamsFill(props) {
  const set = (i, v) => {
    const e = assetEl(i);
    if (e) e.value = v == null ? "" : String(v);
  };
  set("asset-reg", props.reg_code);
  set("asset-service", props.service);
  set("asset-sn", props.device_sn);
  set("asset-linktype", props.link_type);
  const bw = bwSplit(props.bandwidth_mbps);
  set("asset-bw", bw.v);
  set("asset-bw-unit", bw.u);
  const tr = bwSplit(props.trunk_mbps);
  set("asset-trunk", tr.v);
  set("asset-trunk-unit", props.trunk_mbps == null ? "Gbps" : tr.u);
  set("asset-overbook", props.trunk_overbook);
}
function assetNormCodes() {
  const r = assetEl("asset-reg"),
    s = assetEl("asset-sn");
  if (r) r.value = assetNormCode(r.value);
  if (s) s.value = assetNormCode(s.value);
  assetUniqDebounce();
}
function assetUniqDebounce() {
  clearTimeout(ASSET_UNIQ.timer);
  ASSET_UNIQ.timer = setTimeout(assetUniqCheck, 350);
}
function assetUniqCheck() {
  const cat = (assetEl("asset-category-type") || {}).value;
  const type = (assetEl("asset-type") || {}).value || "";
  const name = ((assetEl("asset-name") || {}).value || "").trim();
  const regOn = cat === "NODE" && !ASSET_NO_REG.includes(type.toUpperCase());
  const reg = regOn ? assetNormCode((assetEl("asset-reg") || {}).value) : "";
  const sn =
    cat === "NODE" && type.toUpperCase() === "PELANGGAN"
      ? assetNormCode((assetEl("asset-sn") || {}).value)
      : "";
  const msg = (id, ok, text) => {
    const e = assetEl(id);
    if (e) {
      e.textContent = text || "";
      e.className = "ap-msg" + (text ? (ok ? " ok" : " bad") : "");
    }
  };
  if (!name && !reg && !sn) {
    ASSET_UNIQ.bad = {};
    msg("asset-name-msg");
    msg("asset-reg-msg");
    msg("asset-sn-msg");
    return Promise.resolve();
  }
  const q = new URLSearchParams({
    name,
    reg_code: reg,
    device_sn: sn,
    kind: cat === "CABLE" ? "CABLE" : "NODE",
    type,
  });
  const ex = (assetEl("asset-edit-id") || {}).value;
  if (ex) q.set("exclude_id", ex);
  const tok = ++ASSET_UNIQ.token;
  return apiRequest(`/api/nodes/check-unique?${q}`)
    .then((r) => {
      if (tok !== ASSET_UNIQ.token || !r) return;
      const who = (c) =>
        c ? `${c.name} (${c.type}${c.id != null ? ", ID " + c.id : ""})` : "";
      ASSET_UNIQ.bad = {};
      if (r.name) {
        ASSET_UNIQ.bad.name = !r.name.ok;
        msg(
          "asset-name-msg",
          r.name.ok,
          r.name.ok
            ? "\u2713 Nama tersedia"
            : `Nama sudah dipakai: ${who(r.name.conflict)} \u2014 huruf besar/kecil dianggap sama`,
        );
      } else msg("asset-name-msg");
      if (r.reg_code) {
        ASSET_UNIQ.bad.reg_code = !r.reg_code.ok;
        msg(
          "asset-reg-msg",
          r.reg_code.ok,
          r.reg_code.ok
            ? "\u2713 Kode tersedia"
            : r.reg_code.valid_format === false
              ? "Format kode tidak valid (huruf/angka/. _ / -, 2\u201340 karakter)"
              : `Kode sudah dipakai: ${who(r.reg_code.conflict)}`,
        );
      } else msg("asset-reg-msg");
      if (r.device_sn) {
        ASSET_UNIQ.bad.device_sn = !r.device_sn.ok;
        msg(
          "asset-sn-msg",
          r.device_sn.ok,
          r.device_sn.ok
            ? "\u2713 SN tersedia"
            : r.device_sn.valid_format === false
              ? "Format SN tidak valid (huruf/angka/. _ : -, 4\u201340 karakter)"
              : `SN sudah dipakai: ${who(r.device_sn.conflict)}`,
        );
      } else msg("asset-sn-msg");
    })
    .catch(() => {});
}
function assetGenReg() {
  const t = (assetEl("asset-type") || {}).value || "ODP";
  return apiRequest(`/api/nodes/next-reg-code?type=${encodeURIComponent(t)}`)
    .then((r) => {
      const e = assetEl("asset-reg");
      if (e && r && r.reg_code) e.value = r.reg_code;
      return assetUniqCheck();
    })
    .catch((err) => alert("Gagal membuat kode: " + err.message));
}
// isi parameter aset untuk payload (null = kosongkan); validasi sisi klien. Mengembalikan {extra} atau {error}
function assetExtraPayload(type, creating) {
  const t = (type || "").toUpperCase();
  const v = (id) => ((assetEl(id) || {}).value || "").trim();
  const ex = {
    reg_code: null,
    service: null,
    bandwidth_mbps: null,
    device_sn: null,
    link_type: null,
    trunk_mbps: null,
    trunk_overbook: null,
  };
  if (!ASSET_NO_REG.includes(t))
    ex.reg_code = assetNormCode(v("asset-reg")) || null;
  if (t === "PELANGGAN") {
    ex.service = v("asset-service") || null;
    ex.link_type = v("asset-linktype") || null;
    ex.device_sn = assetNormCode(v("asset-sn")) || null;
    if (v("asset-bw")) {
      ex.bandwidth_mbps = bwToMbps(v("asset-bw"), v("asset-bw-unit"));
      if (ex.bandwidth_mbps == null || ex.bandwidth_mbps <= 0)
        return { error: "Bandwidth harus berupa angka lebih dari 0." };
    }
  }
  if (t === "POP") {
    if (v("asset-trunk")) {
      ex.trunk_mbps = bwToMbps(v("asset-trunk"), v("asset-trunk-unit"));
      if (ex.trunk_mbps == null || ex.trunk_mbps <= 0)
        return { error: "Kapasitas trunk harus berupa angka lebih dari 0." };
    }
    if (v("asset-overbook")) {
      ex.trunk_overbook = parseFloat(v("asset-overbook").replace(",", "."));
      if (
        !isFinite(ex.trunk_overbook) ||
        ex.trunk_overbook < 1 ||
        ex.trunk_overbook > 100
      )
        return { error: "Rasio overbooking harus 1 sampai 100." };
    }
  }
  return { extra: ex };
}

// --- EDITOR OTB (form aset bertipe POP) ---
let OTB_EDIT = [];
let OTB_BASE_SPEC = {};
function otbBlank(ports) {
  return {
    ports: ports || 24,
    kind: "",
    label: "",
    rack: "",
    unit: "",
    slot: "",
    tray: "",
    loc: "",
  };
}
function otbLoad(capacity, specData) {
  OTB_BASE_SPEC = otbSpec(specData);
  const det = Array.isArray(OTB_BASE_SPEC.otb) ? OTB_BASE_SPEC.otb : [];
  OTB_EDIT = capacity
    ? otbSizes(capacity).map((sz, i) =>
        Object.assign(otbBlank(sz), det[i] || {}, { ports: sz }),
      )
    : [otbBlank(24)];
  renderOtbEditor();
}
function otbSyncCapacity() {
  const sel = document.getElementById("asset-capacity");
  if (!sel) return;
  const v = otbCapacityString(OTB_EDIT.map((o) => Number(o.ports) || 12));
  sel.innerHTML = `<option value="${v}">${v}</option>`;
  sel.value = v;
}
function renderOtbEditor() {
  const box = document.getElementById("otb-rows");
  if (!box) return;
  const editing = !!(document.getElementById("asset-edit-id") || { value: "" })
    .value;
  box.innerHTML = OTB_EDIT.map((o, i) => {
    const sizes = OTB_SIZES.includes(Number(o.ports))
      ? OTB_SIZES
      : OTB_SIZES.concat([Number(o.ports)]).sort((a, b) => a - b);
    const f = (key, ph, w) =>
      `<input value="${escapeHtml(o[key] || "")}" placeholder="${ph}" style="width:${w}px" oninput="otbSet(${i},'${key}',this.value,true)">`;
    let pos = "";
    if (o.kind === "rack")
      pos = f("rack", "Rak", 80) + f("unit", "Unit (U)", 70);
    else if (o.kind === "modular")
      pos =
        f("rack", "Rak", 70) + f("slot", "Slot", 60) + f("tray", "Tray", 60);
    else if (o.kind === "wall")
      pos = f("loc", "Lokasi (mis. dinding ruang POP)", 210);
    const last = i === OTB_EDIT.length - 1;
    return (
      `<div class="otb-row"><div class="otb-row-main"><b>OTB-${i + 1}</b>` +
      `<select id="otb-ports-${i}" onchange="otbSet(${i},'ports',this.value)" title="Jumlah port OTB">${sizes.map((n) => `<option value="${n}"${Number(o.ports) === n ? " selected" : ""}>${n} port</option>`).join("")}</select>` +
      `<input id="otb-label-${i}" value="${escapeHtml(o.label || "")}" placeholder="Keterangan (opsional)" oninput="otbSet(${i},'label',this.value,true)" style="flex:1;min-width:120px">` +
      `<select id="otb-kind-${i}" onchange="otbSet(${i},'kind',this.value)" title="Jenis OTB (menentukan isian posisi)">${OTB_KINDS.map((k) => `<option value="${k[0]}"${o.kind === k[0] ? " selected" : ""}>${escapeHtml(k[1])}</option>`).join("")}</select>` +
      `${last && OTB_EDIT.length > 1 ? `<button type="button" class="btn-mini" onclick="otbRemove(${i})" title="Hapus OTB terakhir${editing ? " (hanya jika portnya belum dipakai)" : ""}"><i class="fa-solid fa-trash"></i></button>` : ""}</div>` +
      `${pos ? `<div class="otb-row-pos">${pos}</div>` : ""}</div>`
    );
  }).join("");
  const total = OTB_EDIT.reduce((a, o) => a + (Number(o.ports) || 0), 0);
  const t = document.getElementById("otb-total");
  if (t) t.textContent = `${OTB_EDIT.length} OTB · ${total} port`;
  otbSyncCapacity();
}
function otbSet(i, key, val, noRender) {
  if (!OTB_EDIT[i]) return;
  OTB_EDIT[i][key] = key === "ports" ? Number(val) : val;
  if (key === "ports") otbSyncCapacity();
  if (!noRender) renderOtbEditor();
}
function otbAdd() {
  if (OTB_EDIT.length >= OTB_MAX_UNITS)
    return alert(`Maksimal ${OTB_MAX_UNITS} OTB per POP.`);
  OTB_EDIT.push(
    otbBlank(
      OTB_EDIT.length ? Number(OTB_EDIT[OTB_EDIT.length - 1].ports) : 24,
    ),
  );
  renderOtbEditor();
}
function otbRemove(i) {
  if (i !== OTB_EDIT.length - 1 || OTB_EDIT.length < 2) return;
  OTB_EDIT.pop();
  renderOtbEditor();
}
// detail OTB yang disimpan di spec_data (urutan = OTB-1, OTB-2, ...); hanya isian yang relevan dengan jenisnya
function otbSpecJson() {
  const keep = {
    rack: ["rack", "unit"],
    modular: ["rack", "slot", "tray"],
    wall: ["loc"],
  };
  const otb = OTB_EDIT.map((o) => {
    const d = { kind: o.kind || "", label: (o.label || "").trim() };
    (keep[o.kind] || []).forEach((k) => {
      d[k] = (o[k] || "").trim();
    });
    return d;
  });
  return JSON.stringify(Object.assign({}, OTB_BASE_SPEC, { otb }));
}

const CORE_OPTIONS = `
            <option value="2C">2 Core (2C)</option>
            <option value="4C">4 Core (4C)</option>
            <option value="8C">8 Core (8C)</option>
            <option value="12C">12 Core (12C)</option>
            <option value="24C">24 Core (24C)</option>
            <option value="48C">48 Core (48C)</option>
            <option value="96C">96 Core (96C)</option>
            <option value="144C">144 Core (144C)</option>
        `;

function onAssetTypeChange() {
  assetParamsToggle();
  assetUniqDebounce();
  const selectedType = document.getElementById("asset-type").value;
  const capacitySelect = document.getElementById("asset-capacity");
  const otbBox = document.getElementById("otb-editor");
  const capWrap = capacitySelect && capacitySelect.parentElement;
  const isPopType = selectedType === "POP";
  if (otbBox) otbBox.style.display = isPopType ? "block" : "none";
  if (capWrap && capWrap.style) capWrap.style.display = isPopType ? "none" : "";
  if (isPopType) {
    if (!OTB_EDIT.length) otbLoad(null, null);
    else renderOtbEditor();
    return;
  }

  if (selectedType === "ODP") {
    capacitySelect.innerHTML = `
            <option value="1 In - 4 Out">1 Input - 4 Output</option>
            <option value="1 In - 8 Out">1 Input - 8 Output</option>
            <option value="2 In - 8 Out">2 Input - 8 Output</option>
            <option value="2 In - 16 Out">2 Input - 16 Output</option>
        `;
  } else if (selectedType === "TIANG") {
    capacitySelect.innerHTML = `
            <option value="Tiang 7m">Tiang 7m</option>
            <option value="Tiang 9m">Tiang 9m</option>
        `;
  } else if (selectedType === "HH") {
    capacitySelect.innerHTML = `
            <option value="HH Kecil">HH Kecil (30x30)</option>
            <option value="HH Standar">HH Standar (50x50)</option>
            <option value="HH Besar">HH Besar (80x80)</option>
        `;
  } else if (selectedType === "SLACK") {
    capacitySelect.innerHTML = `
            <option value="Slack 10m">Slack 10m</option>
            <option value="Slack 20m">Slack 20m</option>
            <option value="Slack 50m">Slack 50m</option>
        `;
  } else if (selectedType === "PELANGGAN") {
    capacitySelect.innerHTML = `
    <option value="2 Core" selected>2 Core</option>
    <option value="4 Core">4 Core</option>
    <option value="8 Core">8 Core</option>
    <option value="12 Core">12 Core</option>
  `;
  } else {
    // POP, CLOSURE, dan kabel (Feeder, Distribution, Backbone, Drop)
    capacitySelect.innerHTML = CORE_OPTIONS;
  }
}

function saveAssetData(e) {
  e.preventDefault();

  const categoryType = document.getElementById("asset-category-type").value;
  const name = document.getElementById("asset-name").value.trim();
  const type = document.getElementById("asset-type").value;
  const status = document.getElementById("asset-status").value;
  const cluster = document.getElementById("asset-cluster").value;
  const area = document.getElementById("asset-area").value;
  const city = document.getElementById("asset-city").value;
  if (categoryType === "NODE" && type === "POP") otbSyncCapacity();
  const capacity = document.getElementById("asset-capacity").value;

  const autoOk =
    !document.getElementById("asset-edit-id").value &&
    !(categoryType === "NODE" && (type === "POP" || type === "PELANGGAN"));
  if (!name && !autoOk)
    return alert(
      "Nama aset wajib diisi (POP dan Pelanggan diberi nama manual).",
    );
  if (!cluster || !area)
    return alert("Pilih Cluster dan Area dari daftar Wilayah terlebih dulu.");
  if (city === GEOCODE_PENDING_TEXT) {
    return alert("Lokasi masih dideteksi, tunggu sebentar lalu simpan lagi.");
  }
  let assetExtra = null;
  if (categoryType === "NODE") {
    const xr = assetExtraPayload(
      type,
      !document.getElementById("asset-edit-id").value,
    );
    if (xr.error) return alert(xr.error);
    assetExtra = xr.extra;
  }

  const val = (id) => {
    const el = document.getElementById(id);
    return el ? intOrNull(el.value) : null;
  };

  let url, payload, successMsg;
  const editId = document.getElementById("asset-edit-id").value;

  if (editId) {
    // MODE EDIT: PUT hanya field yang boleh berubah (posisi & status tidak ikut)
    const common = { name, type, cluster, area, capacity };
    const installation = (
      document.getElementById("asset-installation") || { value: "" }
    ).value;
    if (categoryType === "NODE") {
      url = `/api/nodes/${Number(editId)}`;
      payload = {
        ...common,
        ...assetExtra,
        parent_node_id: val("asset-parent-node"),
        upstream_cable_id: val("asset-upstream-cable"),
      };
      if (type === "POP") payload.spec_data = otbSpecJson();
    } else {
      url = `/api/cables/${Number(editId)}`;
      payload = {
        ...common,
        installation,
        fiber_mode: (
          document.getElementById("asset-fiber-mode") || { value: "SM" }
        ).value,
        parent_cable_id: val("asset-parent-cable"),
        from_node_id: val("asset-from-node"),
        to_node_id: val("asset-to-node"),
      };
    }
    return apiRequest(url, "PUT", payload)
      .then(() => {
        alert("Perubahan aset berhasil disimpan!");
        closeAddAssetModal();
        loadData();
      })
      .catch((err) => alert("Gagal menyimpan perubahan: " + err.message));
  }

  if (categoryType === "NODE") {
    const lat = parseFloat(document.getElementById("asset-lat").value);
    const lng = parseFloat(document.getElementById("asset-lng").value);
    url = "/api/nodes";
    successMsg = "Node berhasil disimpan!";
    payload = {
      name,
      type,
      status,
      latitude: lat,
      longitude: lng,
      cluster,
      area,
      city,
      capacity,
      spec_data: type === "POP" ? otbSpecJson() : "{}",
      parent_node_id: val("asset-parent-node"),
      upstream_cable_id: val("asset-upstream-cable"),
      ...assetExtra,
    };
  } else {
    url = "/api/cables";
    successMsg = "Kabel berhasil disimpan!";
    payload = {
      name,
      type,
      status,
      coordinates: JSON.parse(
        document.getElementById("asset-coords-json").value,
      ),
      cluster,
      area,
      city,
      capacity,
      installation: (
        document.getElementById("asset-installation") || { value: "Udara" }
      ).value,
      fiber_mode: (
        document.getElementById("asset-fiber-mode") || { value: "SM" }
      ).value,
      core_data: "{}",
      parent_cable_id: val("asset-parent-cable"),
      from_node_id: val("asset-from-node"),
      to_node_id: val("asset-to-node"),
    };
  }

  const acMode = categoryType === "CABLE" ? checkedRadioValue("ac-mode") : null;
  apiRequest(url, "POST", payload)
    .then((res) => {
      closeAddAssetModal();
      loadData(); // refresh tanpa reload halaman, posisi/zoom peta tetap
      if (categoryType === "CABLE" && acMode === "auto" && res && res.id) {
        const cv = (document.getElementById("ac-cores") || { value: "1" })
          .value;
        const cores = cv === "full" ? 1 : parseInt(cv) || 1;
        return acStart(res.id, name, cores, cv === "full");
      }
      alert(successMsg);
    })
    .catch((err) => alert("Gagal menyimpan: " + err.message));
}

// ---------- TAHAP B: sambung core otomatis setelah kabel digambar ----------
function checkedRadioValue(name) {
  const r = [...document.querySelectorAll(`input[name="${name}"]`)].find(
    (x) => x.checked,
  );
  return r ? r.value : null;
}
const AC = { cableId: null, name: "", req: null, res: null };
function acStart(cableId, name, cores, full) {
  AC.cableId = cableId;
  AC.name = name;
  AC.req = { cores, reverse: false, upstream_cable_id: null, full: !!full };
  return acPreview();
}
function acPreview() {
  return apiRequest(`/api/cables/${Number(AC.cableId)}/auto-connect`, "POST", {
    ...AC.req,
    preview: true,
  })
    .then((r) => {
      AC.res = r;
      acRender();
    })
    .catch((err) => {
      alert(
        `Kabel ${AC.name} tersimpan, tetapi pratinjau sambungan gagal: ${err.message}`,
      );
    });
}
function acRender() {
  const r = AC.res,
    body = document.getElementById("ac-body");
  if (!body) return;
  document.getElementById("modal-ac").style.display = "flex";
  let html = `<p style="margin-top:0">Kabel <b>${escapeHtml(AC.name)}</b> sudah tersimpan.</p>`;
  if (r.from && r.to)
    html += `<div style="font-size:13px;margin-bottom:8px">Arah: <b>${escapeHtml(r.from.name)}</b> (${escapeHtml(r.from.type)}) &rarr; <b>${escapeHtml(r.to.name)}</b> (${escapeHtml(r.to.type)})</div>`;
  if (r.ok) {
    html +=
      '<div style="font-size:12px;font-weight:700;color:#334155">Sambungan yang akan dibuat:</div><ol style="font-size:12px;margin:4px 0 8px 18px">' +
      r.links.map((l) => `<li>${escapeHtml(l)}</li>`).join("") +
      "</ol>";
  } else {
    html += `<div class="plan-msg warn" style="padding:8px;background:#fef3c7;border-radius:6px;font-size:12px;margin-bottom:8px"><i class="fa-solid fa-triangle-exclamation"></i> ${escapeHtml(r.reason || "Tidak bisa disambung otomatis")}<br><small>Kabel tetap tersimpan; sambungkan manual lewat modal aset bila perlu.</small></div>`;
  }
  (r.warnings || []).forEach((w) => {
    html += `<div style="font-size:11px;color:#b45309">&bull; ${escapeHtml(w)}</div>`;
  });
  if (r.needs_choice) {
    html +=
      '<div style="margin:8px 0"><label style="font-size:12px;font-weight:600">Pilih kabel hulu:</label> <select id="ac-up" onchange="acPickUp()"><option value="">-- pilih --</option>' +
      r.needs_choice
        .map(
          (c) =>
            `<option value="${Number(c.id)}">${escapeHtml(c.name)} (${Number(c.free)} core kosong)</option>`,
        )
        .join("") +
      "</select></div>";
  }
  html +=
    '<div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">' +
    (r.ok
      ? '<button type="button" class="btn-ok" onclick="acApply()"><i class="fa-solid fa-check"></i> Simpan sambungan</button>'
      : "") +
    '<button type="button" class="btn-mini" onclick="acReverse()"><i class="fa-solid fa-right-left"></i> Balik arah</button>' +
    '<button type="button" class="btn-mini" onclick="acClose()">Lewati (manual)</button></div>';
  body.innerHTML = html;
}
function acPickUp() {
  const v = parseInt((document.getElementById("ac-up") || {}).value);
  if (!v) return;
  AC.req.upstream_cable_id = v;
  acPreview();
}
function acReverse() {
  AC.req.reverse = !AC.req.reverse;
  AC.req.upstream_cable_id = null;
  acPreview();
}
function acApply() {
  apiRequest(`/api/cables/${Number(AC.cableId)}/auto-connect`, "POST", {
    ...AC.req,
    preview: false,
  })
    .then((r) => {
      acClose();
      alert(
        `${r.links.length} sambungan core dibuat otomatis. Setiap sambungan dapat diputus lewat modal detail core aset.`,
      );
      loadData();
    })
    .catch((err) => alert("Gagal menyimpan sambungan: " + err.message));
}
function acClose() {
  const m = document.getElementById("modal-ac");
  if (m) m.style.display = "none";
}

// --- STANDAR 12 WARNA FIBER OPTIK (TIA/EIA-598-A) ---
const ISO_FIBER_COLORS = [
  { name: "Blue", hex: "#2563eb", text: "#ffffff" },
  { name: "Orange", hex: "#f97316", text: "#ffffff" },
  { name: "Green", hex: "#16a34a", text: "#ffffff" },
  { name: "Brown", hex: "#78350f", text: "#ffffff" },
  { name: "Slate", hex: "#64748b", text: "#ffffff" },
  { name: "White", hex: "#ffffff", text: "#000000", border: "#cbd5e1" },
  { name: "Red", hex: "#dc2626", text: "#ffffff" },
  { name: "Black", hex: "#000000", text: "#ffffff" },
  { name: "Yellow", hex: "#eab308", text: "#000000" },
  { name: "Violet", hex: "#9333ea", text: "#ffffff" },
  { name: "Rose/Pink", hex: "#ec4899", text: "#ffffff" },
  { name: "Aqua/Turquoise", hex: "#06b6d4", text: "#ffffff" },
];

// --- HELPER PORT / CORE ---
function parseOdpCapacity(capacity) {
  const m = (capacity || "").match(/(\d+)\s*In\s*-\s*(\d+)\s*Out/i);
  return { inCount: m ? parseInt(m[1]) : 1, outCount: m ? parseInt(m[2]) : 8 };
}

// --- POP: port berdasarkan OTB (bisa lebih dari satu OTB), bukan Tube/Core ---
const OTB_SIZES = [8, 12, 16, 24, 36, 48, 72, 96, 144];
const OTB_MAX_UNITS = 16;
const OTB_KINDS = [
  ["", "Tanpa detail posisi"],
  ["rack", 'OTB Rak 19" (Rak + Unit)'],
  ["modular", "ODF Modular (Rak + Slot + Tray)"],
  ["wall", "OTB Dinding / Outdoor (Lokasi)"],
];
function isPop(item) {
  return (
    !!item &&
    item.category === "NODE" &&
    (item.type || "").toUpperCase() === "POP"
  );
}
// '48C' -> [48] (satu OTB); '12+24' -> [12, 24] (dua OTB). Sama dengan backend.
function otbSizes(capacity) {
  const s = String(capacity || "");
  if (/^\s*\d+(\s*\+\s*\d+)*\s*(c|core|port|p)?\s*$/i.test(s)) {
    const nums = (s.match(/\d+/g) || [])
      .map(Number)
      .filter((n) => n > 0 && n <= 576);
    if (nums.length) return nums.slice(0, OTB_MAX_UNITS);
  }
  return [parseInt(s) || 12];
}
function otbCapacityString(sizes) {
  return sizes.length === 1 ? `${sizes[0]}C` : sizes.join("+");
}
function otbLabel(i, p) {
  return `OTB-${i} / P${String(p).padStart(2, "0")}`;
}
function otbPortLabels(itemOrCap) {
  const cap =
    itemOrCap && typeof itemOrCap === "object" ? itemOrCap.capacity : itemOrCap;
  const out = [];
  otbSizes(cap).forEach((sz, k) => {
    for (let p = 1; p <= sz; p++) out.push(otbLabel(k + 1, p));
  });
  return out;
}
function otbSpec(raw) {
  if (raw && typeof raw === "object") return raw;
  try {
    const o = JSON.parse(raw || "{}");
    return o && typeof o === "object" ? o : {};
  } catch (_) {
    return {};
  }
}
function otbDetails(item) {
  const a = otbSpec(item && item.spec_data).otb;
  return Array.isArray(a) ? a : [];
}
function otbPositionText(d) {
  if (!d) return "";
  const bits = [];
  if (d.kind === "rack") {
    if (d.rack) bits.push(`Rak ${d.rack}`);
    if (d.unit) bits.push(`U${d.unit}`);
  } else if (d.kind === "modular") {
    if (d.rack) bits.push(`Rak ${d.rack}`);
    if (d.slot) bits.push(`Slot ${d.slot}`);
    if (d.tray) bits.push(`Tray ${d.tray}`);
  } else if (d.kind === "wall") {
    if (d.loc) bits.push(d.loc);
  }
  return bits.join(" \u00b7 ");
}

// Jumlah core/port sebuah aset (tiang tidak punya port)
function getPortCount(item) {
  if (
    item.category === "NODE" &&
    NO_PORT_TYPES.includes((item.type || "").toUpperCase())
  )
    return 0;
  if (isPop(item)) return otbSizes(item.capacity).reduce((a, b) => a + b, 0);
  return parseInt(item.capacity) || 12;
}

function isOdp(item) {
  return item.category === "NODE" && (item.type || "").toUpperCase() === "ODP";
}

// Daftar opsi port/core sebuah aset, dipakai dropdown asal & tujuan sambungan
// usedPorts (Set, opsional): port yang sudah terpakai ditandai & dinonaktifkan
const JUNCTION_UI = ["CLOSURE", "SLACK"];
function isJunction(item) {
  return (
    !!item &&
    item.category === "NODE" &&
    JUNCTION_UI.includes((item.type || "").toUpperCase())
  );
}
// POP/OTB diperlakukan seperti closure untuk pemakaian port: tiap port menerima 1 kabel masuk + 1 kabel keluar
function isJointLike(item) {
  return (
    isJunction(item) ||
    (!!item &&
      item.category === "NODE" &&
      (item.type || "").toUpperCase() === "POP")
  );
}
// Closure/slack = penghubung kabel: joint masuk (dari hulu) & keluar (ke hilir) dihitung terpisah
function jointDirsLocal(asset) {
  const ins = new Set(),
    outs = new Set();
  activeConnections.forEach((c) => {
    if (
      String(c.to_asset_type) === String(asset.category) &&
      String(c.to_asset_id) === String(asset.id)
    )
      ins.add(c.to_port_core);
    if (
      String(c.from_asset_type) === String(asset.category) &&
      String(c.from_asset_id) === String(asset.id)
    )
      outs.add(c.from_port_core);
  });
  return { ins, outs };
}
// dir: "from" (closure meneruskan ke hilir) | "to" (closure menerima dari hulu); hanya untuk closure/slack
function buildPortOptionsHtml(
  item,
  usedPorts = new Set(),
  dir = null,
  arrived = null,
) {
  const junction = isJointLike(item) && dir;
  const opt = (value, label) => {
    const used = usedPorts.has(value);
    const hint =
      junction && !used && dir === "from" && arrived && arrived.has(value)
        ? " \u2714 sudah tiba dari hulu"
        : "";
    return `<option value="${value}"${used ? " disabled" : ""}>${label}${used ? (junction ? " (sudah dipakai)" : " (terpakai)") : hint}</option>`;
  };
  let html = "";
  if (isOdp(item)) {
    const { inCount, outCount } = parseOdpCapacity(item.capacity);
    for (let i = 1; i <= inCount; i++) html += opt(`IN-${i}`, `Input IN-${i}`);
    for (let o = 1; o <= outCount; o++)
      html += opt(`OUT-${o}`, `Output OUT-${o}`);
  } else if (isPop(item)) {
    otbSizes(item.capacity).forEach((sz, k) => {
      const d = otbDetails(item)[k] || {};
      html += `<optgroup label="${escapeHtml(`OTB-${k + 1}${d.label ? " - " + d.label : ""} (${sz} port)`)}">`;
      for (let p = 1; p <= sz; p++)
        html += opt(otbLabel(k + 1, p), otbLabel(k + 1, p));
      html += "</optgroup>";
    });
  } else {
    const total = getPortCount(item);
    for (let i = 1; i <= total; i++) {
      const label = `Tube ${Math.floor((i - 1) / 12) + 1} - Core ${i}`;
      html += opt(label, label);
    }
  }
  return html;
}

// Port milik ASET INI yang sudah tersambung (sisi lokal ditentukan dari tipe + id)
function getConnectedLocalPorts(asset) {
  const ports = new Set();
  activeConnections.forEach((c) => {
    if (
      String(c.from_asset_type) === String(asset.category) &&
      String(c.from_asset_id) === String(asset.id)
    ) {
      ports.add(c.from_port_core);
    }
    if (
      String(c.to_asset_type) === String(asset.category) &&
      String(c.to_asset_id) === String(asset.id)
    ) {
      ports.add(c.to_port_core);
    }
    // Sambungan yang MELEWATI kabel ini: core milik kabel ini ikut terpakai
    if (
      String(asset.category) === "CABLE" &&
      c.via_cable_id != null &&
      String(c.via_cable_id) === String(asset.id) &&
      c.via_core
    ) {
      ports.add(c.via_core);
    }
  });
  return ports;
}

// --- FUNGSI BUKA MODAL DETAIL CORE / PORT ---
function resetCoreContainers() {
  const grid = document.getElementById("core-grid-container");
  const odp = document.getElementById("odp-visual-container");
  if (grid) {
    grid.style.display = "block";
    grid.innerHTML = "";
  }
  if (odp) {
    odp.style.display = "none";
    odp.innerHTML = "";
  }
}

// Riwayat navigasi antar-aset di modal core (klik tautan aset -> tombol "Kembali")
const coreNav = [];
function updateCoreBack() {
  const btn = document.getElementById("core-back");
  if (!btn) return;
  const top = coreNav[coreNav.length - 1];
  btn.style.display = top ? "inline-flex" : "none";
  const lbl = document.getElementById("core-back-label");
  if (lbl)
    lbl.textContent = top
      ? "Kembali ke " + (top.name || "aset sebelumnya")
      : "Kembali";
}
function assetLinkHtml(type, id, name) {
  const nid = Number(id);
  const label = escapeHtml(name || `${type} #${id}`);
  if (!Number.isFinite(nid) || !id) return label;
  const cat = String(type).toUpperCase() === "CABLE" ? "CABLE" : "NODE";
  return (
    `<a class="asset-link" href="#" onclick="openLinkedAsset('${cat}', ${nid}); return false;" title="Buka detail core/port aset ini">${label}</a>` +
    `<a class="asset-locate" href="#" onclick="focusAssetOnMap('${cat.toLowerCase()}', ${nid}); return false;" title="Tampilkan di peta"><i class="fa-solid fa-location-crosshairs"></i></a>`
  );
}
function openLinkedAsset(category, id) {
  const exists = allInventoryData.some(
    (x) => String(x.id) === String(id) && x.category === category,
  );
  if (!exists) return alert("Aset tidak ditemukan (mungkin sudah dihapus).");
  if (currentActiveAsset)
    coreNav.push({
      id: currentActiveAsset.id,
      category: currentActiveAsset.category,
      name: currentActiveAsset.name,
    });
  initCoreDetailLogic(id, category);
  updateCoreBack();
  const body = document.querySelector("#modal-core-detail .modal-body");
  if (body) body.scrollTop = 0;
}
function coreNavBack() {
  const prev = coreNav.pop();
  if (!prev) return;
  initCoreDetailLogic(prev.id, prev.category);
  updateCoreBack();
}

function openCoreDetailModal(id, category) {
  coreNav.length = 0;
  updateCoreBack();
  document.getElementById("modal-core-detail").style.display = "flex";
  resetCoreContainers();

  const ready = allInventoryData.length
    ? Promise.resolve()
    : fetchInventoryData();
  ready.then(() => initCoreDetailLogic(id, category));
}

function initCoreDetailLogic(id, category) {
  const infoContainer = document.getElementById("asset-detail-info");
  const gridContainer = document.getElementById("core-grid-container");

  const item = allInventoryData.find(
    (x) =>
      String(x.id) === String(id) && String(x.category) === String(category),
  );

  if (!item) {
    gridContainer.innerHTML =
      '<p style="color: #ef4444;">Data aset tidak ditemukan.</p>';
    return;
  }

  currentActiveAsset = item;
  resetCoreContainers();
  cxSyncStatic();

  document.getElementById("core-modal-title").innerHTML =
    `<i class="fa-solid fa-diagram-project"></i> Detail Status & Splicing: ${escapeHtml(item.name)}`;

  infoContainer.innerHTML = `
    <div><b>Tipe Aset:</b> ${escapeHtml(item.type || "-")}</div>
    <div><b>Cluster / Area:</b> ${escapeHtml(item.cluster || "-")} / ${escapeHtml(item.area || "-")}</div>
    <div><b>Kapasitas:</b> ${escapeHtml(item.capacity || "-")}</div>
    ${item.reg_code ? `<div><b>Kode Registrasi:</b> ${escapeHtml(item.reg_code)}</div>` : ""}
    ${
      (item.type || "").toUpperCase() === "PELANGGAN"
        ? `<div><b>Layanan:</b> ${escapeHtml(item.service || "-")} &middot; ${escapeHtml(item.link_type || "-")}</div>
    <div><b>Bandwidth:</b> ${escapeHtml(fmtBw(item.bandwidth_mbps))}</div>
    <div><b>SN Perangkat:</b> ${escapeHtml(item.device_sn || "-")}</div>`
        : ""
    }
  `;

  fetchConnectionsAndRender();
}

function fetchConnectionsAndRender() {
  if (!currentActiveAsset) return;
  const asset = currentActiveAsset;

  apiRequest(
    `/api/connections?asset_type=${encodeURIComponent(asset.category)}&asset_id=${asset.id}`,
  )
    .then((connections) => {
      activeConnections = Array.isArray(connections) ? connections : [];

      resetCoreContainers();
      const gridContainer = document.getElementById("core-grid-container");
      const assetType = (asset.type || "").toUpperCase();

      if (isPop(asset)) {
        if (POPDEV.id !== null && String(POPDEV.id) !== String(asset.id)) {
          POPDEV.editPort = null;
          POPDEV.draft = null;
        }
        if (POPTRUNK.id !== null && String(POPTRUNK.id) !== String(asset.id)) {
          POPTRUNK.id = null;
          POPTRUNK.data = null;
          POPTRUNK.edit = false;
          POPTRUNK.draft = null;
        }
        renderPopOtb(asset, gridContainer);
        popDevLoad(asset, gridContainer);
        popTrunkLoad(asset, gridContainer);
      } else if (
        asset.category === "CABLE" ||
        assetType === "CLOSURE" ||
        assetType === "POP"
      ) {
        renderCableCores(asset, gridContainer);
      } else if (assetType === "ODP") {
        renderODPSplitter(asset);
      } else {
        renderNodePorts(asset, gridContainer);
      }

      populateSplicingDropdowns();
      renderActiveConnectionsTable(activeConnections, asset.category, asset.id);
      loadTracePanel(asset);
      loadAssetLoss(asset);
      loadAssetSupports(asset);
    })
    .catch((err) => {
      console.error("Gagal memuat koneksi:", err);
      activeConnections = [];
      renderActiveConnectionsTable([], asset.category, asset.id);
    });
}

// --- POPULATE DROPDOWNS SPLICING ---
function populateSplicingDropdowns() {
  const fromSelect = document.getElementById("splice-from-core");
  const toAssetSelect = document.getElementById("splice-to-asset");
  const viaCableSelect = document.getElementById("splice-via-cable");
  const toCoreSelect = document.getElementById("splice-to-core");

  if (!fromSelect || !toAssetSelect || !currentActiveAsset) return;

  if (isJunction(currentActiveAsset)) {
    // closure: sisi asal = joint yang MENERUSKAN ke hilir; yang sudah tiba dari hulu (tapi belum diteruskan) didahulukan
    const jd = jointDirsLocal(currentActiveAsset);
    const arrived = new Set([...jd.ins].filter((p) => !jd.outs.has(p)));
    const ordered = buildPortOptionsHtml(
      currentActiveAsset,
      jd.outs,
      "from",
      arrived,
    );
    fromSelect.innerHTML =
      '<option value="">-- Pilih Joint Closure --</option>' + ordered;
    const first = [...arrived][0];
    if (first) fromSelect.value = first;
  } else if (isPop(currentActiveAsset)) {
    // OTB: port yang sudah meneruskan kabel keluar tertutup; port yang baru menerima kabel masuk tetap bisa diteruskan
    const jd = jointDirsLocal(currentActiveAsset);
    fromSelect.innerHTML =
      '<option value="">-- Pilih Port OTB (belakang) --</option>' +
      buildPortOptionsHtml(
        currentActiveAsset,
        jd.outs,
        "from",
        new Set([...jd.ins].filter((p) => !jd.outs.has(p))),
      );
  } else {
    fromSelect.innerHTML =
      '<option value="">-- Pilih Core/Port Aset Ini --</option>' +
      buildPortOptionsHtml(
        currentActiveAsset,
        getConnectedLocalPorts(currentActiveAsset),
      );
  }
  toAssetSelect.innerHTML = '<option value="">-- Pilih Aset Tujuan --</option>';
  if (toCoreSelect)
    toCoreSelect.innerHTML =
      '<option value="">-- Pilih Core/Port Tujuan --</option>';
  if (viaCableSelect) {
    viaCableSelect.innerHTML =
      '<option value="">-- Pilih kabel penghubung (wajib antar aset) --</option>';
  }
  const viaWrap = document.getElementById("splice-via-core-wrap");
  if (viaWrap) viaWrap.style.display = "none";

  if (!allInventoryData.length) {
    toAssetSelect.innerHTML =
      '<option value="">-- Data Inventory Kosong --</option>';
    return;
  }

  // dirangkai sekali lalu dipasang sekali (innerHTML += di dalam loop = parse ulang seluruh dropdown tiap baris -> O(n^2), membekukan browser pada ribuan aset)
  let availableTargets = 0;
  const toParts = [toAssetSelect.innerHTML];
  const viaParts = [viaCableSelect ? viaCableSelect.innerHTML : ""];
  allInventoryData.forEach((item) => {
    const isSameAsset =
      String(item.id) === String(currentActiveAsset.id) &&
      String(item.category) === String(currentActiveAsset.category);

    if (!isSameAsset) {
      availableTargets++;
      toParts.push(
        `<option value="${item.category}:${Number(item.id)}">${escapeHtml(item.name)} (${escapeHtml(item.type || item.category)})</option>`,
      );
    }

    if (item.category === "CABLE" && viaCableSelect) {
      viaParts.push(
        `<option value="${Number(item.id)}">${escapeHtml(item.name)} (${escapeHtml(item.type)}) - ${escapeHtml(item.capacity)}</option>`,
      );
    }
  });

  if (availableTargets === 0) {
    toAssetSelect.innerHTML =
      '<option value="">-- Tidak Ada Aset Lain Tersedia --</option>';
  } else {
    toAssetSelect.innerHTML = toParts.join("");
  }
  if (viaCableSelect) viaCableSelect.innerHTML = viaParts.join("");
}

// Pilih kabel media -> tampilkan core milik kabel tsb (core yang sudah terpakai dinonaktifkan)
function onViaCableChange() {
  const viaId = document.getElementById("splice-via-cable").value;
  const wrap = document.getElementById("splice-via-core-wrap");
  const coreSelect = document.getElementById("splice-via-core");
  if (!wrap || !coreSelect) return;

  coreSelect.innerHTML = '<option value="">-- Pilih Core Kabel --</option>';
  if (!viaId) {
    wrap.style.display = "none";
    return;
  }
  wrap.style.display = "block";

  apiRequest(`/api/cables/${Number(viaId)}/core-usage`)
    .then((usage) => {
      // abaikan respons usang bila pilihan kabel sudah berganti
      if (document.getElementById("splice-via-cable").value !== viaId) return;
      let html = '<option value="">-- Pilih Core Kabel --</option>';
      (usage.cores || []).forEach((c) => {
        html += `<option value="${escapeHtml(c.core)}"${c.used ? " disabled" : ""}>${escapeHtml(c.core)}${c.used ? " (terpakai)" : ""}</option>`;
      });
      coreSelect.innerHTML = html;
    })
    .catch((err) => {
      console.error("Gagal memuat core kabel:", err);
      coreSelect.innerHTML = '<option value="">Gagal memuat core</option>';
    });
}

function onTargetAssetChange() {
  const targetVal = document.getElementById("splice-to-asset").value;
  const toCoreSelect = document.getElementById("splice-to-core");
  toCoreSelect.innerHTML =
    '<option value="">-- Pilih Core/Port Tujuan --</option>';

  if (!targetVal) return;

  const [cat, id] = targetVal.split(":");
  const targetAsset = allInventoryData.find(
    (x) => String(x.id) === String(id) && x.category === cat,
  );
  if (!targetAsset) return;

  toCoreSelect.innerHTML += buildPortOptionsHtml(targetAsset, new Set(), "to");

  // tandai port tujuan yang sudah terpakai (diambil dari server)
  apiRequest(
    `/api/connections?asset_type=${encodeURIComponent(cat)}&asset_id=${Number(id)}`,
  )
    .then((conns) => {
      if (document.getElementById("splice-to-asset").value !== targetVal)
        return; // pilihan sudah berganti
      const used = new Set();
      const junction = isJointLike(targetAsset);
      (Array.isArray(conns) ? conns : []).forEach((c) => {
        // closure sebagai tujuan: hanya joint yang sudah MENERIMA kabel hulu yang tertutup
        if (
          !junction &&
          c.from_asset_type === cat &&
          String(c.from_asset_id) === String(id)
        )
          used.add(c.from_port_core);
        if (c.to_asset_type === cat && String(c.to_asset_id) === String(id))
          used.add(c.to_port_core);
        if (
          cat === "CABLE" &&
          String(c.via_cable_id) === String(id) &&
          c.via_core
        )
          used.add(c.via_core);
      });
      toCoreSelect.innerHTML =
        '<option value="">-- Pilih Core/Port Tujuan --</option>' +
        buildPortOptionsHtml(targetAsset, used, "to");
      autoPickJoint();
    })
    .catch((err) => console.error("Gagal memuat port tujuan:", err));
}

// Tujuan = closure: joint otomatis = core kabel media yang dipilih (core sama menerus di closure)
function autoPickJoint() {
  const toVal = (document.getElementById("splice-to-asset") || {}).value || "";
  const viaCore =
    (document.getElementById("splice-via-core") || {}).value || "";
  const toSel = document.getElementById("splice-to-core");
  if (!toSel || !viaCore) return;
  const [cat, id] = toVal.split(":");
  const t = allInventoryData.find(
    (x) => String(x.id) === String(id) && x.category === cat,
  );
  if (!isJunction(t)) return;
  const o = [...toSel.options].find((x) => x.value === viaCore && !x.disabled);
  if (o) toSel.value = viaCore;
}

// --- SUBMIT CORE CONNECTION ---
function submitSplicingConnection() {
  const fromCore = document.getElementById("splice-from-core").value;
  const viaEl = document.getElementById("splice-via-cable");
  const viaCable = viaEl ? viaEl.value : null;
  const toAssetVal = document.getElementById("splice-to-asset").value;
  const toCore = document.getElementById("splice-to-core").value;
  const viaCoreEl = document.getElementById("splice-via-core");
  const viaCore = viaCable && viaCoreEl ? viaCoreEl.value : "";

  if (!currentActiveAsset) return alert("Data aset aktif tidak ditemukan!");
  if (!fromCore || !toAssetVal || !toCore) {
    return alert("Harap lengkapi semua field pilihan sambungan!");
  }
  if (viaCable && !viaCore) {
    return alert("Pilih core pada kabel media yang dipakai sambungan ini!");
  }
  // aset hanya terhubung karena kabel: sambungan antar dua aset (bukan core kabel langsung) wajib memilih kabel penghubung
  if (
    !viaCable &&
    currentActiveAsset.category === "NODE" &&
    String(toAssetVal).startsWith("NODE:")
  ) {
    return alert(
      "Aset hanya bisa terhubung lewat kabel. Pilih kabel penghubung (dan core-nya) terlebih dahulu.",
    );
  }

  // Format dropdown splice-to-asset: "CATEGORY:ID" (contoh "NODE:5" atau "CABLE:2")
  const [toType, toId] = toAssetVal.split(":");

  const payload = {
    from_asset_type: currentActiveAsset.category,
    from_asset_id: parseInt(currentActiveAsset.id),
    from_port_core: fromCore,
    to_asset_type: toType,
    to_asset_id: parseInt(toId),
    to_port_core: toCore,
    via_cable_id: viaCable ? parseInt(viaCable) : null,
    via_core: viaCable ? viaCore : null,
    status: "Connected",
    notes: "",
  };

  apiRequest("/api/connections", "POST", payload)
    .then(() => {
      alert("Sambungan berhasil disimpan!");
      fetchConnectionsAndRender(); // refresh modal & visual core
    })
    .catch((err) => {
      console.error(err);
      alert("Gagal menyimpan sambungan core: " + err.message);
    });
}

// --- RENDER CONNECTION TABLE ---
// Tabel "Daftar Core Tersambung": satu baris = satu jalur core yang melewati aset ini.
// Susunan kolom menyesuaikan data: both (hulu+hilir) | down (hanya hilir) | up (hanya hulu) | cable (kabel: melintas).
const CONNTBL = {
  rows: [],
  page: 1,
  size: 10,
  q: "",
  dir: "ALL",
  key: "",
  colf: [],
  sortCol: -1,
  sortDir: 1,
  layout: "",
  built: "",
  ncols: 7,
};

function connLayoutCols(layout, assetLabel) {
  const A = `${assetLabel} (aset ini)`;
  if (layout === "both")
    return [
      ["Node Hulu · Port", 16],
      ["Melalui Kabel", 11],
      ["Jalur Upstream", 15],
      [A, 13],
      ["Jalur Downstream", 15],
      ["Melalui Kabel", 11],
      ["Node Hilir · Port", 19],
    ];
  if (layout === "up")
    return [
      ["Node Hulu · Port", 32],
      ["Melalui Kabel", 24],
      ["Jalur Upstream", 18],
      [A, 26],
    ];
  if (layout === "cable")
    return [
      ["Core Kabel", 20],
      ["Node Hulu · Port", 33],
      ["Node Hilir · Port", 33],
      ["Aksi", 14],
    ];
  return [
    [A, 26],
    ["Jalur Downstream", 18],
    ["Melalui Kabel", 24],
    ["Node Hilir · Port", 32],
  ];
}

function renderActiveConnectionsTable(
  connections,
  currentAssetType,
  currentAssetId,
) {
  const key = `${currentAssetType}:${currentAssetId}`;
  if (CONNTBL.key !== key) {
    CONNTBL.key = key;
    CONNTBL.page = 1;
    CONNTBL.q = "";
    CONNTBL.dir = "ALL";
    CONNTBL.colf = [];
    CONNTBL.sortCol = -1;
    ngResetFilters("conn-table");
  }
  const qEl = document.getElementById("conn-search");
  if (qEl) qEl.value = CONNTBL.q;

  const assetName = (type, id) => {
    const f = allInventoryData.find(
      (item) =>
        String(item.id) === String(id) &&
        String(item.category).toUpperCase() === String(type).toUpperCase(),
    );
    return f && f.name ? f.name : `${type} #${id}`;
  };
  const here = (t, i) =>
    String(t) === String(currentAssetType) &&
    String(i) === String(currentAssetId);
  const conns = connections || [];
  const isCable = String(currentAssetType).toUpperCase() === "CABLE";
  const ups = [],
    downs = [],
    passes = [];
  conns.forEach((c) => {
    if (here(c.from_asset_type, c.from_asset_id)) downs.push(c);
    else if (here(c.to_asset_type, c.to_asset_id)) ups.push(c);
    else passes.push(c);
  });
  const layout = isCable
    ? "cable"
    : ups.length && downs.length
      ? "both"
      : ups.length
        ? "up"
        : "down";
  const myName = assetName(currentAssetType, currentAssetId);
  const canDel = can("connection.delete");

  const lightLink = (type, id, name) => {
    const nid = Number(id),
      cat = String(type).toUpperCase() === "CABLE" ? "CABLE" : "NODE";
    if (!Number.isFinite(nid) || !id) return escapeHtml(name);
    return `<a class="asset-link" href="#" onclick="openLinkedAsset('${cat}', ${nid}); return false;">${escapeHtml(name)}</a>`;
  };
  const node = (type, id, port) => {
    const n = assetName(type, id),
      p = port == null || port === "" ? "" : String(port);
    return {
      html: `${lightLink(type, id, n)}${p ? `<span class="cn-port"> · ${escapeHtml(p)}</span>` : ""}`,
      text: p ? `${n} · ${p}` : n,
    };
  };
  const via = (c) => {
    if (!c) return { html: `<span class="jl-chip off">—</span>`, text: "—" };
    if (c.via_cable_name)
      return {
        html: lightLink("CABLE", c.via_cable_id, c.via_cable_name),
        text: c.via_cable_name,
      };
    if (c.via_cable_id) {
      const t = "Kabel #" + Number(c.via_cable_id);
      return { html: escapeHtml(t), text: t };
    }
    return {
      html: `<span class="cn-direct">Langsung</span>`,
      text: "Langsung",
    };
  };
  const jalur = (c, side) => {
    if (!c)
      return {
        html: `<span class="jl-chip off" title="${side === "up" ? "Upstream" : "Downstream"}: belum tersambung">—</span>`,
        text: "—",
      };
    const core = c.via_core ? String(c.via_core) : "—";
    const x = canDel
      ? `<button class="jl-x" onclick="disconnectCore(${Number(c.id)})" title="Putus sambungan ini">✕</button>`
      : "";
    const lab =
      side === "up" ? `${escapeHtml(core)} →` : `→ ${escapeHtml(core)}`;
    return {
      html: `<span class="jl"><span class="jl-chip ${side === "up" ? "up" : "down"} on" title="${escapeHtml(core)}">${lab}</span>${x}</span>`,
      text: core,
    };
  };
  const td = (c, extra) =>
    `<td title="${escapeHtml(c.text)}"${extra || ""}>${c.html}</td>`;

  let rows = [];
  if (layout === "cable") {
    rows = conns.map((c) => {
      const f = node(c.from_asset_type, c.from_asset_id, c.from_port_core),
        t = node(c.to_asset_type, c.to_asset_id, c.to_port_core);
      const core = String(c.via_core || "-");
      const del = canDel
        ? `<button onclick="disconnectCore(${Number(c.id)})" title="Putus sambungan ini" class="jl-del"><i class="fa-solid fa-trash"></i> Putus</button>`
        : "";
      return {
        status: "PASS",
        cells: [core, f.text, t.text, ""],
        text: `${core} ${f.text} ${t.text}`.toLowerCase(),
        html: `<tr><td class="cn-b" title="${escapeHtml(core)}">${escapeHtml(core)}</td>${td(f)}${td(t)}<td class="cn-c">${del}</td></tr>`,
      };
    });
  } else {
    const lab = (c) => String(c.to_port_core == null ? "" : c.to_port_core);
    const dlab = (c) =>
      String(c.from_port_core == null ? "" : c.from_port_core);
    const upBy = {};
    ups.forEach((c) => {
      (upBy[lab(c)] = upBy[lab(c)] || []).push(c);
    });
    const downLabels = new Set(downs.map(dlab));
    const used = new Set();
    // splitter (ODP): satu port IN melayani banyak port OUT -> info hulu diulang di tiap baris
    const fan =
      ups.length &&
      ups.every((c) => /^IN/i.test(lab(c))) &&
      downs.length &&
      downs.every((c) => /^OUT/i.test(dlab(c)))
        ? ups[0]
        : null;
    const pairs = [];
    downs.forEach((d) => {
      const m = (upBy[dlab(d)] || [])[0] || fan || null;
      if (m) used.add(m.id);
      pairs.push({ up: m, down: d });
    });
    ups.forEach((u) => {
      if (!used.has(u.id)) pairs.push({ up: u, down: null });
    });
    pairs.sort((a, b) => {
      const la = a.down ? dlab(a.down) : lab(a.up),
        lb = b.down ? dlab(b.down) : lab(b.up);
      return la.localeCompare(lb, "id", { numeric: true, sensitivity: "base" });
    });
    rows = pairs.map((p) => {
      const u = p.up,
        d = p.down;
      const status = u && d ? "FULL" : u ? "NO_DOWN" : "NO_UP";
      const nu = u
        ? node(u.from_asset_type, u.from_asset_id, u.from_port_core)
        : { html: `<span class="jl-chip off">—</span>`, text: "—" };
      const nd = d
        ? node(d.to_asset_type, d.to_asset_id, d.to_port_core)
        : { html: `<span class="jl-chip off">—</span>`, text: "—" };
      const ul = u ? lab(u) : "",
        dl = d ? dlab(d) : "";
      const local = u && d && ul !== dl ? `${ul} → ${dl}` : d ? dl : ul;
      const here_ = { html: `<b>${escapeHtml(local)}</b>`, text: local };
      const vu = via(u),
        vd = via(d),
        ju = jalur(u, "up"),
        jd = jalur(d, "down");
      let cs, tds;
      if (layout === "both") {
        cs = [nu, vu, ju, here_, jd, vd, nd];
      } else if (layout === "up") {
        cs = [nu, vu, ju, here_];
      } else {
        cs = [here_, jd, vd, nd];
      }
      tds = cs.map((c, i) =>
        td(
          c,
          c === here_
            ? ' class="cn-here"'
            : c === ju || c === jd
              ? ' class="jl-cell"'
              : "",
        ),
      );
      return {
        status,
        cells: cs.map((c) => c.text),
        text: cs
          .map((c) => c.text)
          .join(" ")
          .toLowerCase(),
        html: `<tr>${tds.join("")}</tr>`,
      };
    });
  }
  CONNTBL.rows = rows;
  CONNTBL.layout = layout;

  // (Re)bangun tabel bila susunan kolom / aset berubah, agar grid (filter/urut/lebar) mengikuti kolom baru
  const host = document.getElementById("conn-table-host");
  const built = layout + "|" + key + "|" + myName;
  if (host && (CONNTBL.built !== built || !host.querySelector("#conn-table"))) {
    CONNTBL.built = built;
    const cols = connLayoutCols(layout, myName);
    CONNTBL.ncols = cols.length;
    host.innerHTML =
      `<table id="conn-table" class="conn-table conn-${layout}" data-ng-key="conn-table:${layout}">` +
      `<colgroup>${cols.map((c) => `<col style="width:${c[1]}%">`).join("")}</colgroup>` +
      `<thead><tr>${cols.map((c, i) => `<th class="${/aset ini/.test(c[0]) ? "cn-here-h" : ""}" title="${escapeHtml(c[0])}">${escapeHtml(c[0])}</th>`).join("")}</tr></thead>` +
      `<tbody id="active-connections-tbody"></tbody></table>`;
  }
  const dEl = document.getElementById("conn-dir");
  if (dEl) {
    dEl.style.display = layout === "both" ? "" : "none";
    if (layout !== "both") CONNTBL.dir = "ALL";
    dEl.value = CONNTBL.dir;
  }
  renderConnPage();
}

function renderConnPage() {
  const tbody = document.getElementById("active-connections-tbody");
  if (!tbody) return;
  const all = CONNTBL.rows;
  const q = CONNTBL.q.trim().toLowerCase();
  const cf = CONNTBL.colf.map((x) =>
    String(x || "")
      .trim()
      .toLowerCase(),
  );
  let list = all.filter(
    (r) =>
      (CONNTBL.dir === "ALL" || r.status === CONNTBL.dir) &&
      (!q || r.text.includes(q)) &&
      cf.every((f, c) => !f || (r.cells[c] || "").toLowerCase().includes(f)),
  );
  if (CONNTBL.sortCol >= 0) {
    const c = CONNTBL.sortCol,
      d = CONNTBL.sortDir;
    list = list
      .slice()
      .sort(
        (x, y) =>
          d *
          String(x.cells[c] || "").localeCompare(
            String(y.cells[c] || ""),
            "id",
            { numeric: true, sensitivity: "base" },
          ),
      );
  }
  const pages = Math.max(1, Math.ceil(list.length / CONNTBL.size));
  if (CONNTBL.page > pages) CONNTBL.page = pages;
  if (CONNTBL.page < 1) CONNTBL.page = 1;
  const start = (CONNTBL.page - 1) * CONNTBL.size;
  const slice = list.slice(start, start + CONNTBL.size);
  const nc = CONNTBL.ncols || 7;
  if (!all.length) {
    tbody.innerHTML = `<tr><td colspan="${nc}" style="text-align:center; padding:12px; color:#94a3b8;">Belum ada sambungan core/port aktif.</td></tr>`;
  } else if (!list.length) {
    tbody.innerHTML = `<tr><td colspan="${nc}" style="text-align:center; padding:12px; color:#94a3b8;">Tidak ada sambungan yang cocok dengan filter.</td></tr>`;
  } else {
    tbody.innerHTML = slice.map((r) => r.html).join("");
  }
  const info = document.getElementById("conn-page-info"),
    prev = document.getElementById("conn-prev"),
    next = document.getElementById("conn-next");
  const counts = { FULL: 0, NO_DOWN: 0, NO_UP: 0, PASS: 0 };
  all.forEach((r) => {
    counts[r.status]++;
  });
  if (info) {
    info.textContent = list.length
      ? `${start + 1}–${start + slice.length} dari ${list.length}${list.length !== all.length ? ` (terfilter dari ${all.length})` : ""} · halaman ${CONNTBL.page}/${pages}`
      : `0 dari ${all.length}`;
  }
  const sum = document.getElementById("conn-summary");
  if (sum) {
    if (!all.length) sum.textContent = "";
    else if (CONNTBL.layout === "both")
      sum.textContent = `${all.length} jalur · ${counts.FULL} lengkap${counts.NO_DOWN ? ` · ${counts.NO_DOWN} belum ada hilir` : ""}${counts.NO_UP ? ` · ${counts.NO_UP} belum ada hulu` : ""}`;
    else if (CONNTBL.layout === "cable")
      sum.textContent = `${all.length} core melintas`;
    else
      sum.textContent = `${all.length} jalur ${CONNTBL.layout === "up" ? "upstream" : "downstream"}`;
  }
  if (prev) prev.disabled = CONNTBL.page <= 1;
  if (next) next.disabled = CONNTBL.page >= pages;
  const pager = document.getElementById("conn-pager");
  if (pager)
    pager.style.display =
      all.length > 10 || list.length !== all.length ? "" : "none";
}
function onConnSearch(v) {
  CONNTBL.q = String(v || "");
  CONNTBL.page = 1;
  renderConnPage();
}
function onConnDir(v) {
  CONNTBL.dir = v || "ALL";
  CONNTBL.page = 1;
  renderConnPage();
}
function onConnPageSize(v) {
  CONNTBL.size = Number(v) || 10;
  CONNTBL.page = 1;
  renderConnPage();
}
function goConnPage(d) {
  CONNTBL.page += d;
  renderConnPage();
}

// --- JALUR & DAMPAK (hulu / hilir) ---
function loadTracePanel(asset) {
  const box = document.getElementById("trace-content");
  if (!box) return;
  box.innerHTML = "Memuat jalur...";
  apiRequest(
    `/api/trace?asset_type=${encodeURIComponent(asset.category)}&asset_id=${Number(asset.id)}`,
  )
    .then((data) => {
      // abaikan respons usang bila modal sudah pindah ke aset lain
      if (
        !currentActiveAsset ||
        String(currentActiveAsset.id) !== String(asset.id) ||
        currentActiveAsset.category !== asset.category
      )
        return;
      box.innerHTML =
        renderTraceHtml(data) +
        `<div id="trace-loss" class="trace-loss"></div>`;
      renderTraceDiagramInto();
      setTraceView("diagram");
      loadTraceLoss(asset);
    })
    .catch((err) => {
      console.error("Gagal memuat jalur:", err);
      box.innerHTML = `<span style="color:#ef4444;">Gagal memuat jalur: ${escapeHtml(err.message)}</span>`;
    });
}

function lossStatusChip(st) {
  const lab =
    { OK: "Layak", WARN: "Margin tipis", BAD: "Tidak layak" }[st] || st;
  return `<span class="otdr-badge s-${escapeHtml(st)}">${escapeHtml(lab)}</span>`;
}
function lossBreakdownHtml(L) {
  const b = L.breakdown || {};
  const spl =
    (b.splitters || [])
      .map(
        (s) =>
          `${escapeHtml(s.name)} ${escapeHtml(s.ratio)} (${fmtDb(s.db)} dB)`,
      )
      .join(", ") || "&ndash;";
  return `<div class="loss-grid">
    <div><b>${fmtDb(L.total_db)} dB</b><span>Total redaman</span></div>
    <div><b>${fmtDb(L.rx_dbm)} dBm</b><span>Daya terima</span></div>
    <div><b>${fmtDb(L.margin_db, true)} dB</b><span>Margin ${lossStatusChip(L.status)}</span></div></div>
    <div class="cov-meta">Serat ${Number(b.fiber_km || 0).toFixed(2)} km &middot; ${Number(b.splices || 0)} splice &middot; ${Number(b.connectors || 0)} konektor &middot; splitter: ${spl}</div>`;
}
function loadTraceLoss(asset) {
  apiRequest(
    `/api/loss/path?asset_type=${encodeURIComponent(asset.category)}&asset_id=${Number(asset.id)}`,
  )
    .then((L) => {
      const box = document.getElementById("trace-loss");
      if (
        !box ||
        !currentActiveAsset ||
        String(currentActiveAsset.id) !== String(asset.id)
      )
        return;
      if (!L.breakdown || !L.breakdown.hops) {
        box.innerHTML = "";
        return;
      }
      const ot = (L.otdr || [])
        .slice(0, 8)
        .map(
          (o) =>
            `<tr><td>${escapeHtml(o.cable)}</td><td>${escapeHtml(o.core)}</td><td class="num">${fmtDb(o.loss_db)}</td><td class="num">${fmtDb(o.delta_db, true)}</td><td>${otdrBadge(o.status)}</td></tr>`,
        )
        .join("");
      box.innerHTML =
        `<div class="trace-loss-h">Redaman estimasi dari ${escapeHtml(L.root || "POP")}</div>${lossBreakdownHtml(L)}` +
        (ot
          ? `<table class="imp-table"><thead><tr><th>Kabel</th><th>Core</th><th>Ukur</th><th>Selisih</th><th>Status</th></tr></thead><tbody>${ot}</tbody></table>`
          : "") +
        ((L.notes || []).length
          ? `<div class="cov-meta">${L.notes.map(escapeHtml).join("; ")}</div>`
          : "");
    })
    .catch(() => {});
}

function traceEndHtml(e) {
  const nm = e.id ? assetLinkHtml(e.type, e.id, e.name) : escapeHtml(e.name);
  return `<b>${nm}</b> <span style="color:#94a3b8;">[${escapeHtml(e.port || "-")}]</span>`;
}

function traceHopHtml(h, showCustomers) {
  const via = h.via
    ? `<span style="color:#0284c7;"> &mdash; ${h.via.cable_id ? assetLinkHtml("CABLE", h.via.cable_id, h.via.name || "Kabel #" + h.via.cable_id) : escapeHtml(h.via.name || "Kabel")}${h.via.core ? " &middot; " + escapeHtml(h.via.core) : ""} &rarr; </span>`
    : `<span style="color:#94a3b8;"> &rarr; </span>`;
  const badge =
    showCustomers && h.customers_below
      ? ` <span style="background:#fee2e2;color:#b91c1c;padding:1px 6px;border-radius:10px;font-size:10px;font-weight:600;">${Number(h.customers_below)} pelanggan</span>`
      : "";
  return (
    `<div style="padding:5px 0 5px ${8 + (Number(h.level) - 1) * 14}px;border-bottom:1px solid #f1f5f9;">` +
    `<span style="color:#94a3b8;font-size:10px;">L${Number(h.level)}</span> ` +
    `${traceEndHtml(h.from)}${via}${traceEndHtml(h.to)}${badge}</div>`
  );
}

// --- Diagram topologi Jalur & Dampak (gaya manajemen core di Visio): kotak aset + garis sambungan berlabel kabel/core ---
const TRACE_VIEW = {
  d: null,
  mode: "diagram",
  zoom: 1,
  spare: false,
  collapsed: new Set(),
};
const TRACE_TYPE_STYLE = {
  POP: { fill: "#eef2ff", stroke: "#4f46e5", label: "POP" },
  OLT: { fill: "#eef2ff", stroke: "#4f46e5", label: "OLT" },
  CLOSURE: { fill: "#fffbeb", stroke: "#d97706", label: "CLOSURE" },
  ODP: { fill: "#ecfdf5", stroke: "#059669", label: "ODP" },
  PELANGGAN: { fill: "#eff6ff", stroke: "#2563eb", label: "PELANGGAN" },
  SLACK: { fill: "#f8fafc", stroke: "#64748b", label: "SLACK" },
  CABLE: { fill: "#fff", stroke: "#0284c7", label: "KABEL" },
};
function traceShortPort(p) {
  return String(p || "")
    .replace(/Tube\s*(\d+)\s*-\s*Core\s*(\d+)/i, "T$1·C$2")
    .replace(/OTB-(\d+)\s*\/\s*P(\d+)/i, "OTB$1·P$2");
}
// Warna ISO (TIA-598): tube = urutan tube, core = urutan core dalam siklus 12 warna
function isoOfCore(label) {
  const m = String(label || "").match(/Tube\s*(\d+)\s*-\s*Core\s*(\d+)/i);
  if (!m) return null;
  const t = Number(m[1]),
    c = Number(m[2]);
  return {
    tube: t,
    core: c,
    tc: ISO_FIBER_COLORS[(t - 1) % 12],
    cc: ISO_FIBER_COLORS[(c - 1) % 12],
  };
}
function traceTrunc(t, n) {
  t = String(t == null ? "" : t);
  return t.length > n ? t.slice(0, n - 1) + "…" : t;
}
// Geometri berkas core pada satu sambungan: core diberi jarak, tiap Tube dipisah dan punya label sendiri
const TRACE_DEV_LINES = 4;
const TRACE_CG = 11,
  TRACE_TG = 26,
  TRACE_FOLD_MIN = 48,
  TRACE_TOPZ = 56,
  TRACE_BOTZ = 12,
  TRACE_LANE_GAP = 6;
function traceRanges(nums) {
  const a = nums.slice().sort((x, y) => x - y),
    out = [];
  for (let i = 0; i < a.length; ) {
    let k = i;
    while (k + 1 < a.length && a[k + 1] === a[k] + 1) k++;
    out.push(k > i ? `${a[i]}–${a[k]}` : `${a[i]}`);
    i = k + 1;
  }
  return out.join(", ");
}
function traceStatusColor(s) {
  const t = String(s || "");
  if (/cut|broken|down|incident|putus|gangguan/i.test(t)) return "#dc2626";
  if (/active|aktif|connected/i.test(t)) return "#16a34a";
  return "#d97706";
}
function traceStatusWord(s) {
  const t = String(s || "");
  if (/cut|broken|down|incident|putus|gangguan/i.test(t))
    return "Putus/Gangguan";
  if (/active|aktif/i.test(t)) return "Aktif";
  return t || "-";
}
function traceEdgeGeom(e, spare) {
  const items = e.items || (e.cores || []).map((c) => ({ core: c }));
  const used = items
    .map((it) => {
      const c = isoOfCore(it.core);
      return c ? Object.assign(c, { it }) : null;
    })
    .filter(Boolean)
    .sort((p, q) => p.tube - q.tube || p.core - q.core);
  const cab = e.cab || null;
  let entries = used.slice();
  let tubeFold = {};
  // Mode kapasitas: seluruh core kabel digambar (terpakai = warna ISO, cadangan = abu-abu putus-putus).
  // Kabel >= 48 core: tube yang seluruhnya cadangan dilipat menjadi satu pita (klik untuk membuka).
  if (spare && cab && cab.total) {
    const usedNums = new Map(used.map((u) => [u.core, u]));
    const otherNums = new Set();
    (Array.isArray(cab.used_cores) ? cab.used_cores : []).forEach((l) => {
      const c = isoOfCore(l);
      if (c && !usedNums.has(c.core)) otherNums.add(c.core);
    });
    entries = [];
    const tubesN = Math.ceil(cab.total / 12);
    for (let t = 1; t <= tubesN; t++) {
      const lo = (t - 1) * 12 + 1,
        hi = Math.min(t * 12, cab.total),
        nums = [];
      for (let n = lo; n <= hi; n++) nums.push(n);
      const key = `${e.ck || ""}:${t}`;
      const usedHere = nums.filter(
        (n) => usedNums.has(n) || otherNums.has(n),
      ).length;
      const st = TRACE_VIEW.tubeState && TRACE_VIEW.tubeState[key];
      const fold = st
        ? st === "fold"
        : cab.total >= TRACE_FOLD_MIN && usedHere === 0;
      const tc = ISO_FIBER_COLORS[(t - 1) % 12];
      if (fold) {
        entries.push({
          fold: true,
          tube: t,
          core: lo,
          tc,
          cc: tc,
          count: nums.length,
          usedN: usedHere,
          lo,
          hi,
          key,
        });
        tubeFold[t] = true;
        continue;
      }
      nums.forEach((n) => {
        if (usedNums.has(n)) {
          entries.push(usedNums.get(n));
          return;
        }
        const c = isoOfCore(`Tube ${t} - Core ${n}`);
        if (otherNums.has(n)) c.other = true;
        else c.spare = true;
        entries.push(c);
      });
    }
  }
  const shown = entries;
  const offs = [],
    tubes = [];
  let o = 0;
  shown.forEach((c, i) => {
    if (i > 0)
      o += c.tube !== shown[i - 1].tube ? TRACE_CG + TRACE_TG : TRACE_CG;
    offs.push(o);
    let tb = tubes[tubes.length - 1];
    if (!tb || tb.tube !== c.tube) {
      tb = {
        tube: c.tube,
        tc: c.tc,
        idx: [],
        cores: [],
        used: 0,
        spare: 0,
        key: `${e.ck || ""}:${c.tube}`,
        foldable: !!(spare && cab && cab.total),
      };
      tubes.push(tb);
    }
    tb.idx.push(i);
    if (c.fold) {
      tb.fold = true;
      tb.lo = c.lo;
      tb.hi = c.hi;
      tb.count = c.count;
      tb.usedN = c.usedN;
      for (let n = c.lo; n <= c.hi; n++) tb.cores.push(n);
    } else {
      tb.cores.push(c.core);
      if (c.spare) tb.spare++;
      else if (!c.other) tb.used++;
    }
  });
  const span = offs.length ? offs[offs.length - 1] : 0;
  offs.forEach((v, i) => {
    offs[i] = v - span / 2;
  });
  tubes.forEach((tb) => {
    tb.top = offs[tb.idx[0]];
    tb.bot = offs[tb.idx[tb.idx.length - 1]];
  });
  return {
    iso: used,
    shown,
    offs,
    tubes,
    span,
    extra: 0,
    spareCount: shown.filter((c) => c.spare || c.fold).length,
    height: shown.length ? span + TRACE_TOPZ + TRACE_BOTZ : 24,
  };
}
function traceSideOf(nodes, e) {
  return nodes.get(e.b).layer >= nodes.get(e.a).layer;
}
function buildTraceGraph(d) {
  const nodes = new Map(),
    edges = [];
  const keyOf = (e) => `${e.type}:${e.id}`;
  const nstat = (id) => (d.nodes && d.nodes[id] ? d.nodes[id].status : null);
  const add = (e, layer) => {
    const k = keyOf(e);
    if (!nodes.has(k))
      nodes.set(k, {
        key: k,
        type: e.type,
        id: e.id,
        name: e.name,
        atype: e.asset_type || (e.type === "CABLE" ? "CABLE" : ""),
        layer,
        cust: 0,
        order: nodes.size,
        status: e.type === "NODE" ? nstat(e.id) : null,
        capacity:
          e.type === "NODE" && d.nodes && d.nodes[e.id]
            ? d.nodes[e.id].capacity
            : null,
      });
    else if (nodes.get(k).layer === null) nodes.get(k).layer = layer;
    return nodes.get(k);
  };
  // perangkat yang dipatch ke port OTB POP (dari /api/trace -> nodes[id].devices)
  const devOf = (nodeKey, port) => {
    const n = d.nodes && d.nodes[String(nodeKey).split(":")[1]];
    return n && n.devices && port ? n.devices[port] || null : null;
  };
  const start = d.asset
    ? {
        type: d.asset.type,
        id: d.asset.id,
        name: d.asset.name,
        asset_type: d.asset.asset_type,
      }
    : null;
  const startKey = start ? keyOf(start) : null;
  if (start && start.type === "NODE") add(start, 0);
  const edge = (h, fromLayer, toLayer) => {
    const a = add(h.from, fromLayer),
      b = add(h.to, toLayer);
    if (h.customers_below && b.type !== "CABLE" && b.atype !== "PELANGGAN")
      b.cust = Math.max(b.cust, Number(h.customers_below));
    edges.push({
      a: a.key,
      b: b.key,
      via: h.via,
      fromPort: h.from.port,
      toPort: h.to.port,
      status: h.status,
      cust: Number(h.customers_below || 0),
      fdev: devOf(a.key, h.from.port),
      tdev: devOf(b.key, h.to.port),
    });
  };
  (d.upstream || []).forEach((h) =>
    edge(h, -Number(h.level), -(Number(h.level) - 1)),
  );
  (d.downstream || []).forEach((h) =>
    edge(h, Number(h.level) - 1, Number(h.level)),
  );
  (d.through || []).forEach((h) => edge(h, 0, 1));
  // sambungan paralel antar dua aset (beberapa core pada kabel yang sama) digabung jadi satu garis berlabel daftar core
  const merged = new Map();
  edges.forEach((e) => {
    const k = `${e.a}|${e.b}|${e.via ? e.via.cable_id || e.via.name : ""}`;
    if (!merged.has(k))
      merged.set(k, {
        a: e.a,
        b: e.b,
        via: e.via,
        ck: e.via ? String(e.via.cable_id || e.via.name || "") : "",
        cores: [],
        fromPorts: [],
        toPorts: [],
        items: [],
        bad: false,
        cust: 0,
        cab: e.via && d.cables ? d.cables[e.via.cable_id] || null : null,
      });
    const m = merged.get(k);
    const bad = !!(e.status && String(e.status).toLowerCase() !== "connected");
    if (e.via && e.via.core) {
      m.cores.push(e.via.core);
      m.items.push({
        core: e.via.core,
        from: e.fromPort,
        to: e.toPort,
        bad,
        fdev: e.fdev,
        tdev: e.tdev,
        uid: `${k}#${e.via.core}`,
      });
    }
    [
      [e.a, e.fromPort, e.fdev],
      [e.b, e.toPort, e.tdev],
    ].forEach(([nk, port, dv]) => {
      if (!dv) return;
      const nn = nodes.get(nk);
      if (nn) {
        nn.devs = nn.devs || new Map();
        nn.devs.set(port, dv);
      }
    });
    if (e.fromPort) m.fromPorts.push(e.fromPort);
    if (e.toPort) m.toPorts.push(e.toPort);
    if (bad) m.bad = true;
    m.cust += e.cust;
  });
  edges.length = 0;
  merged.forEach((m) => edges.push(m));
  // lipat/buka cabang: node di sisi hilir/hulu dapat dilipat; turunannya disembunyikan
  const outward = (n) =>
    edges
      .filter((e) => e.a === n.key || e.b === n.key)
      .map((e) => nodes.get(e.a === n.key ? e.b : e.a))
      .filter(
        (m) =>
          m &&
          (n.layer > 0
            ? m.layer > n.layer
            : n.layer < 0
              ? m.layer < n.layer
              : false),
      );
  nodes.forEach((n) => {
    n.kids = outward(n).length;
  });
  const collapsed = TRACE_VIEW.collapsed || new Set();
  const hidden = new Set();
  collapsed.forEach((ck) => {
    const root = nodes.get(ck);
    if (!root) return;
    const q = outward(root);
    while (q.length) {
      const m = q.shift();
      if (hidden.has(m.key)) continue;
      hidden.add(m.key);
      outward(m).forEach((x) => q.push(x));
    }
    root.hiddenCount = 0;
    root.collapsed = true;
  });
  collapsed.forEach((ck) => {
    const root = nodes.get(ck);
    if (root) {
      const seen = new Set();
      const q = outward(root);
      while (q.length) {
        const m = q.shift();
        if (seen.has(m.key)) continue;
        seen.add(m.key);
        outward(m).forEach((x) => q.push(x));
      }
      root.hiddenCount = seen.size;
    }
  });
  hidden.forEach((k) => nodes.delete(k));
  for (let i = edges.length - 1; i >= 0; i--)
    if (!nodes.has(edges[i].a) || !nodes.has(edges[i].b)) edges.splice(i, 1);
  const list = Array.from(nodes.values());
  list.forEach((n) => {
    if (n.layer === null || n.layer === undefined) n.layer = 0;
  });
  if (startKey && nodes.has(startKey)) nodes.get(startKey).isStart = true;
  const layers = Array.from(new Set(list.map((n) => n.layer))).sort(
    (x, y) => x - y,
  );
  const col = {};
  layers.forEach((l) => {
    col[l] = list.filter((n) => n.layer === l);
  });
  const yOf = new Map();
  layers.forEach((l) => col[l].forEach((n, i) => yOf.set(n.key, i)));
  const nb = (n) =>
    edges
      .filter((e) => e.a === n.key || e.b === n.key)
      .map((e) => (e.a === n.key ? e.b : e.a));
  for (let pass = 0; pass < 3; pass++) {
    layers
      .filter((l) => l !== 0)
      .sort((x, y) => Math.abs(x) - Math.abs(y))
      .forEach((l) => {
        const inner = l > 0 ? l - 1 : l + 1;
        const bc = (n) => {
          const ys = nb(n)
            .filter((k) => nodes.get(k) && nodes.get(k).layer === inner)
            .map((k) => yOf.get(k));
          return ys.length
            ? ys.reduce((p, q) => p + q, 0) / ys.length
            : yOf.get(n.key);
        };
        col[l].sort((p, q) => bc(p) - bc(q) || p.order - q.order);
        col[l].forEach((n, i) => yOf.set(n.key, i));
      });
  }
  const rows = Math.max(1, ...layers.map((l) => col[l].length));
  edges.forEach((e) => {
    e.geom = traceEdgeGeom(e, !!TRACE_VIEW.spare);
    e.laneA = 0;
    e.laneB = 0;
  });
  const maxH = Math.max(24, ...edges.map((e) => e.geom.height));
  // lajur: kabel yang keluar/masuk satu sisi node yang sama ditumpuk bertingkat (tidak saling menimpa)
  const groups = new Map();
  const addG = (key, e, k, other) => {
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push({ e, k, other });
  };
  edges.forEach((e) => {
    const fwd = traceSideOf(nodes, e);
    addG(e.a + (fwd ? "|R" : "|L"), e, "laneA", nodes.get(e.b));
    addG(e.b + (fwd ? "|L" : "|R"), e, "laneB", nodes.get(e.a));
  });
  let stackMax = 0;
  groups.forEach((arr) => {
    arr.sort(
      (p, q) =>
        yOf.get(p.other.key) - yOf.get(q.other.key) ||
        p.other.order - q.other.order,
    );
    const hs = arr.map((x) => Math.max(24, x.e.geom.height));
    const T = hs.reduce((p, q) => p + q, 0) + TRACE_LANE_GAP * (arr.length - 1);
    stackMax = Math.max(stackMax, T);
    let cum = 0;
    arr.forEach((x, i) => {
      const has = x.e.geom.shown.length > 0;
      const center = has
        ? cum + TRACE_TOPZ + x.e.geom.span / 2
        : cum + hs[i] / 2;
      x.e[x.k] = center - T / 2 - (has ? (TRACE_TOPZ - TRACE_BOTZ) / 2 : 0);
      cum += hs[i] + TRACE_LANE_GAP;
    });
  });
  const BW = 150,
    BH = 46,
    GX = 340,
    GY = Math.max(26, maxH - BH + 8),
    PAD = 24,
    EXT = Math.max(0, (Math.max(maxH, stackMax) - BH) / 2);
  layers.forEach((l, ci) =>
    col[l].forEach((n, i) => {
      n.x = PAD + ci * (BW + GX);
      n.y =
        PAD +
        14 +
        EXT +
        ((rows - col[l].length) * (BH + GY)) / 2 +
        i * (BH + GY);
    }),
  );
  const devRows = Math.max(
    0,
    ...list.map((n) =>
      n.devs ? Math.min(TRACE_DEV_LINES + 1, n.devs.size) : 0,
    ),
  );
  return {
    nodes: list,
    edges,
    W: PAD * 2 + layers.length * BW + (layers.length - 1) * GX,
    H:
      PAD * 2 +
      14 +
      2 * EXT +
      rows * BH +
      (rows - 1) * GY +
      16 +
      (devRows ? devRows * 11 + 30 : 0),
    BW,
    BH,
    layers,
    rows,
  };
}
function traceDiagramSvg(g, zoom) {
  if (!g.nodes.length) return "";
  const byKey = new Map(g.nodes.map((n) => [n.key, n]));
  const st = (n) =>
    TRACE_TYPE_STYLE[n.atype] ||
    TRACE_TYPE_STYLE[n.type] || {
      fill: "#f8fafc",
      stroke: "#64748b",
      label: n.atype || n.type,
    };
  let svg = "";
  g.edges.forEach((e) => {
    const a = byKey.get(e.a),
      b = byKey.get(e.b);
    if (!a || !b) return;
    const fwd = b.x >= a.x;
    const x1 = fwd ? a.x + g.BW : a.x,
      y1 = a.y + g.BH / 2,
      x2 = fwd ? b.x : b.x + g.BW,
      y2 = b.y + g.BH / 2;
    const ok = !e.bad;
    const geom = e.geom || traceEdgeGeom(e, false),
      dir = fwd ? 1 : -1;
    const LA = e.laneA || 0,
      LB = e.laneB || 0,
      cab = e.cab;
    const dash = ok ? "" : ' stroke-dasharray="5 4"';
    const K = 0.22,
      FAN = 45,
      FLAT = 175;
    const xa = x1 + dir * FAN,
      xb = x1 + dir * (FAN + FLAT),
      xc = x2 - dir * FAN,
      xm = (xb + xc) / 2;
    // jalur satu core/kabel: fan keluar dari node, mendatar (zona label), pindah baris dengan kurva S, fan masuk ke node
    const pathAt = (o) =>
      `M${x1},${y1 + o * K} C${x1 + dir * 22},${y1 + o * K} ${x1 + dir * 22},${y1 + LA + o} ${xa},${y1 + LA + o} L${xb},${y1 + LA + o} C${xm},${y1 + LA + o} ${xm},${y2 + LB + o} ${xc},${y2 + LB + o} C${x2 - dir * 22},${y2 + LB + o} ${x2 - dir * 22},${y2 + o * K} ${x2},${y2 + o * K}`;
    const cx = (xa + xb) / 2;
    let chipTop = y1 + LA - 44;
    if (!geom.shown.length) {
      svg += `<path d="${pathAt(0)}" fill="none" stroke="${ok ? "#0284c7" : "#94a3b8"}" stroke-width="2"${dash} />`;
      const ptx = (x, anc, arr, yy) =>
        arr.length
          ? `<text x="${x}" y="${yy}" text-anchor="${anc}" font-size="8.5" font-weight="600" fill="#1e293b" stroke="#fff" stroke-width="3" paint-order="stroke" stroke-linejoin="round">${escapeHtml(traceShortPort(arr[0]) + (arr.length > 1 ? ` +${arr.length - 1}` : ""))}</text>`
          : "";
      svg +=
        ptx(xa + dir * 8, dir > 0 ? "start" : "end", e.fromPorts, y1 + LA - 5) +
        ptx(xb - dir * 8, dir > 0 ? "end" : "start", e.toPorts, y1 + LA - 5);
    } else {
      // pita (bracket) tipis berwarna Tube di zona label
      geom.tubes.forEach((tb) => {
        svg += `<rect x="${Math.min(xa, xb) + 2}" y="${y1 + LA + tb.top - 6}" width="${Math.abs(xb - xa) - 4}" height="${tb.bot - tb.top + 12}" rx="5" fill="${tb.tc.hex}" fill-opacity="0.13" stroke="${tb.tc.hex}" stroke-opacity="0.6" stroke-width="1" />`;
      });
      geom.shown.forEach((c, i) => {
        const off = geom.offs[i];
        if (c.fold) {
          // tube cadangan dilipat: satu pita untuk 12 core (klik label tube untuk membuka)
          svg += `<path d="${pathAt(off)}" fill="none" stroke="${c.tc.hex}" stroke-opacity="0.55" stroke-width="7" stroke-dasharray="6 4" stroke-linecap="round"><title>${escapeHtml(`Tube ${c.tube} - Core ${c.lo}-${c.hi}: ${c.usedN ? c.usedN + " terpakai, dilipat" : "seluruhnya cadangan (belum dipakai)"} - klik label tube untuk membuka`)}</title></path>`;
          return;
        }
        if (c.other) {
          svg += `<path d="${pathAt(off)}" fill="none" stroke="#93c5fd" stroke-width="2" stroke-dasharray="2 3" stroke-linecap="round"><title>${escapeHtml(`Tube ${c.tube} - Core ${c.core} (${c.cc.name}) - dipakai sambungan lain pada kabel ini`)}</title></path>`;
          return;
        }
        if (c.spare) {
          svg += `<path d="${pathAt(off)}" fill="none" stroke="#cbd5e1" stroke-width="2" stroke-dasharray="4 3" stroke-linecap="round"><title>${escapeHtml(`Tube ${c.tube} - Core ${c.core} (${c.cc.name}) - cadangan (belum dipakai)`)}</title></path>`;
          return;
        }
        const it = c.it || {};
        const tip =
          `Tube ${c.tube} (${c.tc.name}) - Core ${c.core} (${c.cc.name})` +
          (it.from ? ` | dari: ${it.from}` : "") +
          (it.to ? ` | ke: ${it.to}` : "") +
          (e.via ? ` | kabel: ${e.via.name || ""}` : "") +
          (it.bad ? " | TIDAK connected" : "") +
          [it.fdev, it.tdev]
            .filter(Boolean)
            .map(
              (dv) =>
                ` | perangkat: ${dv.device_name} ${dv.interface} (${dv.purpose}${dv.vlan ? ", VLAN " + dv.vlan : ""}${dv.service ? ", " + dv.service : ""}${dv.customer_name ? ", pelanggan " + dv.customer_name : ""})`,
            )
            .join("");
        const uidA = it.uid
          ? ` data-uid="${escapeHtml(it.uid)}" onclick="event.stopPropagation();traceCoreClick(this.getAttribute('data-uid'))" style="cursor:pointer"`
          : "";
        svg +=
          `<g class="tr-core"${uidA}><path class="tr-halo" d="${pathAt(off)}" fill="none" stroke="#94a3b8" stroke-width="4.6"${dash} stroke-linecap="round" />` +
          `<path class="tr-main" d="${pathAt(off)}" fill="none" stroke="${c.cc.hex}" stroke-width="2.8"${dash} stroke-linecap="round"><title>${escapeHtml(tip + (it.uid ? " | klik untuk menyorot jalur" : ""))}</title></path>` +
          (it.uid
            ? `<path d="${pathAt(off)}" fill="none" stroke="transparent" stroke-width="10" pointer-events="stroke" />`
            : "");
        // nomor port/core di ujung zona mendatar (latar putih agar terbaca)
        const yy = y1 + LA + off + 3;
        const lt = traceShortPort(it.from || it.core),
          rt = traceShortPort(it.to || it.core);
        const tx = (x, anc, t) =>
          `<text x="${x}" y="${yy}" text-anchor="${anc}" font-size="8.5" font-weight="600" fill="#1e293b" stroke="#fff" stroke-width="3" paint-order="stroke" stroke-linejoin="round">${escapeHtml(t)}</text>`;
        svg +=
          tx(xa + dir * 8, dir > 0 ? "start" : "end", lt) +
          tx(xb - dir * 8, dir > 0 ? "end" : "start", rt) +
          `</g>`;
      });
      // satu label per Tube, tepat di atas berkas core miliknya
      geom.tubes.forEach((tb, ti) => {
        const txt =
          `Tube ${tb.tube} ${tb.tc.name.split("/")[0]} · Core ${traceRanges(tb.cores)}` +
          (tb.fold
            ? tb.usedN
              ? ` · ${tb.usedN} pakai · dilipat`
              : " · cadangan · dilipat"
            : tb.spare
              ? ` · ${tb.used} pakai`
              : "") +
          (tb.foldable ? (tb.fold ? " ▸" : " ▾") : "");
        const w = Math.min(250, txt.length * 5.4 + 22),
          cy = y1 + LA + tb.top - 24;
        if (ti === 0) chipTop = cy - 34;
        const tgl = tb.foldable
          ? ` onclick="event.stopPropagation();traceTubeToggle('${escapeHtml(tb.key)}', ${tb.fold ? 1 : 0})" style="cursor:pointer"`
          : "";
        svg +=
          `<g class="tr-tube"${tgl}><title>${tb.foldable ? (tb.fold ? "Klik untuk membuka tube" : "Klik untuk melipat tube") : ""}</title><rect x="${cx - w / 2}" y="${cy}" width="${w}" height="15" rx="4" fill="#fff" stroke="${tb.tc.hex}" stroke-width="1.4" />` +
          `<rect x="${cx - w / 2 + 4}" y="${cy + 3.5}" width="8" height="8" rx="2" fill="${tb.tc.hex}" stroke="#334155" stroke-width="0.6" />` +
          `<text x="${cx + 5}" y="${cy + 11}" text-anchor="middle" font-size="9.5" fill="#0f172a">${escapeHtml(txt)}</text></g>`;
      });
      if (geom.extra)
        svg += `<text x="${cx}" y="${y1 + LA + geom.span / 2 + 22}" text-anchor="middle" font-size="9" fill="#64748b">+${geom.extra} core lain tersambung</text>`;
    }
    if (e.via) {
      // chip informasi kabel: nama, jenis · kapasitas · panjang · status, core terpakai
      const nameTxt = traceTrunc(e.via.name || "Kabel", 30);
      const meta = cab
        ? [
            cab.type,
            cab.capacity,
            cab.length_m
              ? cab.length_m >= 1000
                ? (cab.length_m / 1000).toFixed(2) + " km"
                : cab.length_m + " m"
              : null,
          ]
            .filter(Boolean)
            .join(" · ")
        : "";
      const used = cab
        ? `${(cab.used_cores || []).length}/${cab.total} core terpakai`
        : "";
      const stTxt = cab ? traceStatusWord(cab.status) : "";
      const line2 = [meta, used].filter(Boolean).join(" · ");
      const w = Math.max(
        nameTxt.length * 6 + 28,
        (line2.length + (stTxt ? stTxt.length + 3 : 0)) * 5 + 28,
      );
      const cy = geom.shown.length ? chipTop : y1 + LA - 40;
      const dot = cab
        ? `<circle cx="${cx - w / 2 + 9}" cy="${cy + 10}" r="4.5" fill="${traceStatusColor(cab.status)}"><title>${escapeHtml("Status kabel: " + (cab.status || "-"))}</title></circle>`
        : "";
      svg +=
        `<g><title>${escapeHtml(`${e.via.name || "Kabel"}${cab ? " | " + meta + " | " + used + " | status " + (cab.status || "-") : ""}`)}</title><rect x="${cx - w / 2}" y="${cy}" width="${w}" height="${cab ? 30 : 17}" rx="5" fill="#e0f2fe" stroke="${cab ? traceStatusColor(cab.status) : "#7dd3fc"}" stroke-width="1.3" />` +
        dot +
        `<text x="${cx + (cab ? 6 : 0)}" y="${cy + 12}" text-anchor="middle" font-size="10" font-weight="700" fill="#075985">${escapeHtml(nameTxt)}</text>` +
        (cab
          ? `<text x="${cx + 6}" y="${cy + 24}" text-anchor="middle" font-size="8.5" fill="#334155">${escapeHtml(line2 + (stTxt ? " · " + stTxt : ""))}</text>`
          : "") +
        `</g>`;
    }
  });
  g.nodes.forEach((n) => {
    const s = st(n),
      link = n.id != null && !n.isStart;
    const nm = escapeHtml(traceTrunc(n.name, 20));
    const stColor = n.status ? traceStatusColor(n.status) : null;
    svg +=
      `<g class="tn${link ? " tn-link" : ""}" data-nk="${escapeHtml(n.key)}"${link ? ` onclick="openLinkedAsset('${n.type === "CABLE" ? "CABLE" : "NODE"}', ${Number(n.id)})" style="cursor:pointer"` : ""}>` +
      `<title>${escapeHtml(n.name)} (${escapeHtml(s.label)})${n.status ? " - status " + escapeHtml(n.status) : ""}${n.atype === "POP" && n.capacity ? " - " + otbSizes(n.capacity).length + " OTB (" + otbSizes(n.capacity).join(" + ") + " port)" : ""}${n.cust ? " - " + n.cust + " pelanggan di bawah" : ""}</title>` +
      `<rect x="${n.x}" y="${n.y}" width="${g.BW}" height="${g.BH}" rx="8" fill="${s.fill}" stroke="${stColor && stColor === "#dc2626" ? "#dc2626" : s.stroke}" stroke-width="${n.isStart ? 3.5 : 1.6}"${n.type === "CABLE" ? ' stroke-dasharray="5 3"' : ""} />` +
      `<text x="${n.x + 10}" y="${n.y + 17}" font-size="9" font-weight="700" fill="${s.stroke}">${escapeHtml(s.label)}${n.isStart ? " · aset ini" : ""}</text>` +
      `<text x="${n.x + 10}" y="${n.y + 34}" font-size="12" font-weight="700" fill="#0f172a">${nm}</text>` +
      (n.atype === "POP" && n.capacity
        ? `<text x="${n.x + 10}" y="${n.y + g.BH + 12}" font-size="9" fill="#475569">${otbSizes(n.capacity).length} OTB &middot; ${otbSizes(n.capacity).join("+")} port</text>`
        : "") +
      (n.devs && n.devs.size
        ? Array.from(n.devs.entries())
            .slice(0, TRACE_DEV_LINES)
            .map(
              ([pt, dv], i) =>
                `<text x="${n.x + 4}" y="${n.y + g.BH + 26 + i * 11}" font-size="8.5" fill="${dv.purpose === "PTP" ? "#c2410c" : "#1d4ed8"}"><title>${escapeHtml(`${pt} -> ${dv.device_name} ${dv.interface} (${dv.purpose})${dv.vlan ? " VLAN " + dv.vlan : ""}${dv.service ? " | " + dv.service : ""}${dv.customer_name ? " | " + dv.customer_name : ""}`)}</title>${escapeHtml(traceTrunc(`${traceShortPort(pt)} \u2192 ${dv.device_name} ${dv.interface}${dv.purpose === "PTP" && dv.vlan ? " V" + dv.vlan : ""}`, 34))}</text>`,
            )
            .join("") +
          (n.devs.size > TRACE_DEV_LINES
            ? `<text x="${n.x + 4}" y="${n.y + g.BH + 26 + TRACE_DEV_LINES * 11}" font-size="8.5" fill="#64748b">+${n.devs.size - TRACE_DEV_LINES} perangkat lain</text>`
            : "")
        : "") +
      (stColor
        ? `<circle cx="${n.x + g.BW - 11}" cy="${n.y + 12}" r="5" fill="${stColor}" stroke="#fff" stroke-width="1.5" />`
        : "") +
      (n.cust
        ? `<g><rect x="${n.x + g.BW - 44}" y="${n.y - 9}" width="50" height="17" rx="8.5" fill="#dc2626" /><text x="${n.x + g.BW - 19}" y="${n.y + 3}" text-anchor="middle" font-size="10" font-weight="700" fill="#fff">${n.cust} plg</text></g>`
        : "") +
      `</g>`;
    if (n.kids) {
      const lab = n.collapsed ? `+${n.hiddenCount}` : "−",
        w = n.collapsed ? 30 : 18,
        cx2 = n.x + g.BW - 8 - w / 2,
        cy2 = n.y + g.BH + 11;
      svg +=
        `<g class="tn-toggle" style="cursor:pointer" onclick="event.stopPropagation();traceToggle('${n.key}')"><title>${n.collapsed ? "Buka cabang (" + n.hiddenCount + " aset tersembunyi)" : "Lipat cabang"}</title>` +
        `<rect x="${cx2 - w / 2}" y="${cy2 - 8}" width="${w}" height="16" rx="8" fill="${n.collapsed ? "#0f172a" : "#fff"}" stroke="#64748b" />` +
        `<text x="${cx2}" y="${cy2 + 4}" text-anchor="middle" font-size="11" font-weight="700" fill="${n.collapsed ? "#fff" : "#334155"}">${lab}</text></g>`;
    }
  });
  const z = zoom || 1;
  return `<svg xmlns="http://www.w3.org/2000/svg" class="trace-svg" width="${Math.round(g.W * z)}" height="${Math.round(g.H * z)}" viewBox="0 0 ${g.W} ${g.H}" font-family="Inter, Arial, sans-serif"><rect width="${g.W}" height="${g.H}" fill="#ffffff" onclick="traceCoreClear()" />${svg}</svg>`;
}
function traceIsoLegendHtml() {
  return (
    `<span class="tl-sw tl-iso"><b>Warna ISO (TIA-598)</b> Tube/Core 1&ndash;12:</span>` +
    ISO_FIBER_COLORS.map(
      (c, i) =>
        `<span class="tl-sw"><i class="keep-color" style="background:${c.hex};border-color:#334155"></i>${i + 1} ${escapeHtml(c.name.split("/")[0])}</span>`,
    ).join("") +
    `<span class="tl-sw tl-iso">Kotak transparan = warna Tube &middot; garis = warna Core (urutan siklus 12) &middot; angka T·C di ujung = port/core</span>`
  );
}
function traceLegendHtml() {
  return (
    ["POP", "CLOSURE", "ODP", "PELANGGAN"]
      .map(
        (k) =>
          `<span class="tl-sw"><i style="background:${TRACE_TYPE_STYLE[k].fill};border-color:${TRACE_TYPE_STYLE[k].stroke}"></i>${TRACE_TYPE_STYLE[k].label}</span>`,
      )
      .join("") +
    `<span class="tl-sw"><i class="tl-line"></i>Sambungan core (kabel · core)</span><span class="tl-sw"><i class="tl-line off"></i>Tidak Connected</span><span class="tl-sw"><i class="tl-line spare"></i>Core cadangan</span><span class="tl-sw"><i class="tl-line other"></i>Dipakai sambungan lain</span><span class="tl-sw"><i class="fa-solid fa-hand-pointer" style="border:0;width:auto;height:auto"></i>Klik core = sorot jalur</span><span class="tl-sw"><i class="tl-dot" style="background:#16a34a"></i>Aktif</span><span class="tl-sw"><i class="tl-dot" style="background:#dc2626"></i>Putus/Gangguan</span><span class="tl-sw"><b class="tl-tg">&minus;/+N</b>Lipat/buka cabang</span><span class="tl-sw"><b class="tl-cust">N plg</b>Pelanggan terdampak</span>`
  );
}
function renderTraceDiagramInto() {
  const box = document.getElementById("trace-diagram");
  if (!box || !TRACE_VIEW.d) return;
  // pertahankan posisi gulir kanvas saat render ulang (zoom, lipat tube, sorot core)
  const q = (s) => (document.querySelector ? document.querySelector(s) : null);
  const oldCv = q("#trace-diagram .trace-canvas");
  const keep =
    oldCv && TRACE_VIEW.lastZoom
      ? {
          l: oldCv.scrollLeft / TRACE_VIEW.lastZoom,
          t: oldCv.scrollTop / TRACE_VIEW.lastZoom,
        }
      : null;
  const g = buildTraceGraph(TRACE_VIEW.d);
  TRACE_VIEW.g = g;
  box.innerHTML = g.nodes.length
    ? `<div class="trace-canvas keep-color">${traceDiagramSvg(g, TRACE_VIEW.zoom)}</div><div id="trace-core-info" class="trace-core-info" style="display:none"></div><div class="trace-legend">${traceLegendHtml()}</div><div class="trace-legend trace-legend-iso">${traceIsoLegendHtml()}</div>`
    : `<div style="color:#94a3b8;padding:8px;">Belum ada sambungan core untuk digambar.</div>`;
  if (g.nodes.length) traceApplyFocus();
  const cv = q("#trace-diagram .trace-canvas"),
    z = TRACE_VIEW.zoom || 1;
  if (cv && g.nodes.length) {
    if (keep && !TRACE_VIEW.centerStart) {
      cv.scrollLeft = keep.l * z;
      cv.scrollTop = keep.t * z;
    } else {
      // pertama dibuka: arahkan pandangan ke aset yang sedang dilihat (diagram bisa sangat tinggi)
      const s = g.nodes.find((n) => n.isStart) || g.nodes[0];
      if (s && cv.clientHeight) {
        cv.scrollTop = Math.max(0, (s.y + g.BH / 2) * z - cv.clientHeight / 2);
        cv.scrollLeft = Math.max(0, (s.x - 40) * z);
      }
    }
    TRACE_VIEW.centerStart = false;
    TRACE_VIEW.lastZoom = z;
  }
}
function setTraceView(mode) {
  TRACE_VIEW.mode = mode === "list" ? "list" : "diagram";
  const dg = document.getElementById("trace-diagram"),
    ls = document.getElementById("trace-list");
  if (dg) dg.style.display = TRACE_VIEW.mode === "diagram" ? "" : "none";
  if (ls) ls.style.display = TRACE_VIEW.mode === "list" ? "" : "none";
  ["diagram", "list"].forEach((m) => {
    const b = document.getElementById("trace-tab-" + m);
    if (b && b.classList) b.classList.toggle("on", m === TRACE_VIEW.mode);
  });
}
function traceZoom(delta) {
  TRACE_VIEW.zoom = Math.max(
    0.3,
    Math.min(2.5, Math.round((TRACE_VIEW.zoom + delta) * 10) / 10),
  );
  renderTraceDiagramInto();
}
// muat pas: skala diagram mengikuti lebar kanvas
function traceFit() {
  if (!TRACE_VIEW.g) return;
  const cv = document.querySelector
    ? document.querySelector("#trace-diagram .trace-canvas")
    : null;
  const w = cv && cv.clientWidth ? cv.clientWidth - 6 : 0;
  TRACE_VIEW.zoom = w
    ? Math.max(0.3, Math.min(1.5, Math.floor((w / TRACE_VIEW.g.W) * 20) / 20))
    : 1;
  renderTraceDiagramInto();
}
function traceToggle(key) {
  const c = TRACE_VIEW.collapsed || (TRACE_VIEW.collapsed = new Set());
  if (c.has(key)) c.delete(key);
  else c.add(key);
  renderTraceDiagramInto();
}
// ---- Klik core: sorot seluruh jalur sirkuitnya (hulu sampai POP, hilir sampai pelanggan)
const TRACE_JOINT = new Set(["CLOSURE", "SLACK", "HH", "TIANG"]);
function traceCircuit(g, uid) {
  const all = [];
  g.edges.forEach((e) =>
    (e.items || []).forEach((it) => {
      if (it.uid) all.push({ it, e });
    }),
  );
  const start = all.find((x) => x.it.uid === uid);
  if (!start) return null;
  const nodeOf = new Map(g.nodes.map((n) => [n.key, n]));
  const strict = (nk) => {
    const n = nodeOf.get(nk);
    return !!n && TRACE_JOINT.has(String(n.atype || "").toUpperCase());
  };
  // pada closure/slack hanya joint yang sama yang meneruskan; pada ODP/POP semua port (splitter berbagi serat)
  const pick = (cands, port, getPort, nk) => {
    if (!strict(nk) || !port) return cands;
    const same = cands.filter((x) => getPort(x) === port);
    return same.length ? same : cands;
  };
  const set = new Set([start.it.uid]),
    order = [start];
  const walk = (dirDown) => {
    const q = [start];
    while (q.length) {
      const x = q.shift();
      const nk = dirDown ? x.e.b : x.e.a;
      const cur = dirDown ? x.it.to : x.it.from;
      const cands = all.filter(
        (y) => !set.has(y.it.uid) && (dirDown ? y.e.a === nk : y.e.b === nk),
      );
      pick(cands, cur, (y) => (dirDown ? y.it.from : y.it.to), nk).forEach(
        (y) => {
          set.add(y.it.uid);
          order.push(y);
          q.push(y);
        },
      );
    }
  };
  walk(false);
  walk(true);
  const nodes = new Set();
  order.forEach((x) => {
    nodes.add(x.e.a);
    nodes.add(x.e.b);
  });
  return { uids: set, nodes, items: order };
}
function traceApplyFocus() {
  const box = document.getElementById("trace-diagram");
  if (!box || !box.querySelector) return;
  const svg = box.querySelector(".trace-svg"),
    info = document.getElementById("trace-core-info");
  const f =
    TRACE_VIEW.focus && TRACE_VIEW.g
      ? traceCircuit(TRACE_VIEW.g, TRACE_VIEW.focus)
      : null;
  if (!f) {
    TRACE_VIEW.focus = null;
    if (svg && svg.classList) svg.classList.remove("tr-focus");
    if (info) {
      info.style.display = "none";
      info.innerHTML = "";
    }
    return;
  }
  if (svg) {
    svg.classList.add("tr-focus");
    svg
      .querySelectorAll(".tr-core")
      .forEach((el) =>
        el.classList.toggle("on", f.uids.has(el.getAttribute("data-uid"))),
      );
    svg
      .querySelectorAll(".tn")
      .forEach((el) =>
        el.classList.toggle("on", f.nodes.has(el.getAttribute("data-nk"))),
      );
  }
  if (info) {
    info.innerHTML = traceCoreInfoHtml(f);
    info.style.display = "";
  }
}
function traceCoreInfoHtml(f) {
  const g = TRACE_VIEW.g,
    nodeOf = new Map(g.nodes.map((n) => [n.key, n]));
  const first =
    f.items.find((x) => x.it.uid === TRACE_VIEW.focus) || f.items[0];
  const iso = isoOfCore(first.it.core);
  const chain = Array.from(f.nodes)
    .map((k) => nodeOf.get(k))
    .filter(Boolean)
    .sort((p, q) => p.layer - q.layer || p.order - q.order);
  const names = chain.map((n) => escapeHtml(traceTrunc(n.name, 18)));
  const shownNames =
    names.length > 8 ? names.slice(0, 5).concat(["…"], names.slice(-2)) : names;
  const cust = chain.filter(
    (n) => String(n.atype || "").toUpperCase() === "PELANGGAN",
  ).length;
  const bad = f.items.filter((x) => x.it.bad).length;
  const cabName = first.e.via && first.e.via.name ? first.e.via.name : "";
  return (
    `<div class="tci-head"><span class="tci-sw" style="background:${iso ? iso.cc.hex : "#94a3b8"}"></span><b>${escapeHtml(first.it.core)}</b>` +
    `${iso ? ` <span class="tci-muted">(${escapeHtml(iso.tc.name.split("/")[0])} / ${escapeHtml(iso.cc.name.split("/")[0])})</span>` : ""}` +
    `${cabName ? ` <span class="tci-muted">pada ${escapeHtml(cabName)}</span>` : ""}` +
    `<span class="tci-actions"><button type="button" onclick="traceZoomToCircuit()"><i class="fa-solid fa-magnifying-glass-plus"></i> Zoom ke jalur</button>` +
    `<button type="button" onclick="traceCoreClear()"><i class="fa-solid fa-xmark"></i> Hapus sorotan</button></span></div>` +
    `<div class="tci-path">${shownNames.join(' <i class="fa-solid fa-arrow-right"></i> ')}</div>` +
    `<div class="tci-badges"><span>${f.items.length} sambungan</span><span>${chain.length} aset</span><span class="${cust ? "red" : ""}">${cust} pelanggan</span>` +
    `<span class="${bad ? "red" : "green"}">${bad ? bad + " tidak connected" : "semua connected"}</span></div>`
  );
}
function traceCoreClick(uid) {
  TRACE_VIEW.focus = TRACE_VIEW.focus === uid ? null : uid;
  traceApplyFocus();
}
function traceCoreClear() {
  if (!TRACE_VIEW.focus) return;
  TRACE_VIEW.focus = null;
  traceApplyFocus();
}
// zoom diagram supaya seluruh jalur sorotan memenuhi kanvas, lalu gulir ke sana
function traceZoomToCircuit() {
  const g = TRACE_VIEW.g;
  const f = TRACE_VIEW.focus && g ? traceCircuit(g, TRACE_VIEW.focus) : null;
  if (!f) return;
  const ns = g.nodes.filter((n) => f.nodes.has(n.key));
  if (!ns.length) return;
  const x0 = Math.min(...ns.map((n) => n.x)) - 30,
    x1 = Math.max(...ns.map((n) => n.x + g.BW)) + 30;
  const y0 = Math.min(...ns.map((n) => n.y)) - 90,
    y1 = Math.max(...ns.map((n) => n.y + g.BH)) + 90;
  const cv = document.querySelector
    ? document.querySelector("#trace-diagram .trace-canvas")
    : null;
  const cw = cv && cv.clientWidth ? cv.clientWidth - 8 : 0,
    ch = cv && cv.clientHeight ? cv.clientHeight - 8 : 0;
  if (cw && ch)
    TRACE_VIEW.zoom = Math.max(
      0.4,
      Math.min(
        1.6,
        Math.floor(Math.min(cw / (x1 - x0), ch / (y1 - y0)) * 20) / 20,
      ),
    );
  renderTraceDiagramInto();
  const cv2 = document.querySelector
    ? document.querySelector("#trace-diagram .trace-canvas")
    : null;
  if (cv2) {
    cv2.scrollLeft = Math.max(0, x0 * TRACE_VIEW.zoom);
    cv2.scrollTop = Math.max(0, y0 * TRACE_VIEW.zoom);
  }
}
// buka/lipat satu tube pada kabel (kunci = "kabel:tube")
function traceTubeToggle(key, isFolded) {
  const s = TRACE_VIEW.tubeState || (TRACE_VIEW.tubeState = {});
  s[key] = isFolded ? "open" : "fold";
  renderTraceDiagramInto();
}
function traceSpare() {
  TRACE_VIEW.spare = !TRACE_VIEW.spare;
  const b = document.getElementById("trace-spare-btn");
  if (b && b.classList) b.classList.toggle("on", TRACE_VIEW.spare);
  renderTraceDiagramInto();
}
// ringkasan jumlah aset per jenis pada satu arah jalur (bukan sekadar "hop")
function traceCounts(hops, startKey) {
  const names = {
    POP: "POP",
    OLT: "OLT",
    CLOSURE: "closure",
    SLACK: "slack",
    ODP: "ODP",
    PELANGGAN: "pelanggan",
  };
  const seen = new Map();
  (hops || []).forEach((h) =>
    [h.from, h.to].forEach((e) => {
      if (!e || e.type !== "NODE" || `${e.type}:${e.id}` === startKey) return;
      seen.set(`${e.type}:${e.id}`, e.asset_type || "");
    }),
  );
  const by = {};
  seen.forEach((t) => {
    by[t] = (by[t] || 0) + 1;
  });
  const parts = Object.keys(names)
    .filter((k) => by[k])
    .map((k) => `${by[k]} ${names[k]}`);
  return (
    (parts.length ? parts.join(" \u00b7 ") : "0 aset") +
    ` \u00b7 ${(hops || []).length} sambungan core`
  );
}
function traceSvgText() {
  if (!TRACE_VIEW.g) return "";
  return (
    '<?xml version="1.0" encoding="UTF-8"?>\n' +
    traceDiagramSvg(TRACE_VIEW.g, 1)
  );
}
function traceFileName(ext) {
  const nm =
    (TRACE_VIEW.d && TRACE_VIEW.d.asset ? TRACE_VIEW.d.asset.name : "jalur")
      .replace(/[^A-Za-z0-9]+/g, "_")
      .replace(/^_|_$/g, "") || "jalur";
  return `topologi_${nm}.${ext}`;
}
function downloadBlobFile(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => {
    if (a.remove) a.remove();
    URL.revokeObjectURL(url);
  }, 500);
}
function traceExport(kind) {
  const svg = traceSvgText();
  if (!svg) return;
  if (kind === "svg")
    return downloadBlobFile(
      new Blob([svg], { type: "image/svg+xml" }),
      traceFileName("svg"),
    );
  const g = TRACE_VIEW.g,
    scale = 2;
  const img = new Image();
  img.onload = () => {
    const cv = document.createElement("canvas");
    cv.width = g.W * scale;
    cv.height = g.H * scale;
    const c2 = cv.getContext("2d");
    c2.fillStyle = "#fff";
    c2.fillRect(0, 0, cv.width, cv.height);
    c2.drawImage(img, 0, 0, cv.width, cv.height);
    cv.toBlob((b) => {
      if (b) downloadBlobFile(b, traceFileName("png"));
    }, "image/png");
  };
  img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
}

function renderTraceHtml(d) {
  const s = d.summary || {};
  const section = (title, color, hops, showCustomers, emptyText) =>
    `<div style="margin-bottom:10px;"><div style="font-weight:700;color:${color};margin-bottom:2px;">${title}</div>` +
    (hops.length
      ? hops.map((h) => traceHopHtml(h, showCustomers)).join("")
      : `<div style="color:#94a3b8;padding:4px 8px;">${emptyText}</div>`) +
    `</div>`;

  let html =
    `<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px;">` +
    `<span title="${Number(s.upstream_hops || 0)} hop" style="background:#e0f2fe;color:#0369a1;padding:3px 8px;border-radius:4px;font-weight:600;">Hulu: ${escapeHtml(traceCounts(d.upstream, d.asset ? d.asset.type + ":" + d.asset.id : ""))}</span>` +
    `<span title="${Number(s.downstream_hops || 0)} hop" style="background:#dcfce7;color:#15803d;padding:3px 8px;border-radius:4px;font-weight:600;">Hilir: ${escapeHtml(traceCounts(d.downstream, d.asset ? d.asset.type + ":" + d.asset.id : ""))}</span>` +
    `<span style="background:#fee2e2;color:#b91c1c;padding:3px 8px;border-radius:4px;font-weight:600;">Pelanggan terdampak: ${Number(s.customers_affected || 0)}</span></div>`;

  let list = "";
  if (d.asset && d.asset.type === "CABLE") {
    list += section(
      "Core yang melintas &amp; dampaknya bila putus",
      "#b45309",
      d.through || [],
      true,
      "Belum ada sambungan yang melewati kabel ini.",
    );
  }
  list += section(
    "Hulu (arah POP)",
    "#0369a1",
    d.upstream || [],
    false,
    "Tidak ada sambungan hulu tercatat.",
  );
  list += section(
    "Hilir (arah pelanggan)",
    "#15803d",
    d.downstream || [],
    true,
    "Tidak ada sambungan hilir tercatat.",
  );

  if ((d.customers || []).length) {
    list +=
      `<div style="font-weight:700;color:#b91c1c;margin-bottom:2px;">Pelanggan di jalur ini</div>` +
      `<div style="color:#475569;">${d.customers.map((c) => escapeHtml(c.name)).join(", ")}</div>`;
  }
  TRACE_VIEW.d = d;
  TRACE_VIEW.zoom = 1;
  TRACE_VIEW.spare = true; // gambar sesuai kapasitas kabel (cadangan abu-abu)
  TRACE_VIEW.tubeState = {};
  TRACE_VIEW.focus = null;
  TRACE_VIEW.centerStart = true;
  TRACE_VIEW.collapsed = new Set();
  html += `<div class="trace-toolbar">
      <span class="trace-tabs"><button type="button" id="trace-tab-diagram" class="on" onclick="setTraceView('diagram')"><i class="fa-solid fa-diagram-project"></i> Diagram</button>
      <button type="button" id="trace-tab-list" onclick="setTraceView('list')"><i class="fa-solid fa-list"></i> Daftar</button></span>
      <span class="trace-tools"><button type="button" onclick="traceZoom(-0.2)" title="Perkecil">&minus;</button><button type="button" onclick="traceZoom(0.2)" title="Perbesar">+</button>
      <button type="button" onclick="traceFit()" title="Muat pas ke lebar layar"><i class="fa-solid fa-expand"></i> Muat</button>
      <button type="button" id="trace-spare-btn" class="on" onclick="traceSpare()" title="Gambar seluruh core sesuai kapasitas kabel (core cadangan abu-abu putus-putus; kabel 48 core ke atas: tube cadangan dilipat)"><i class="fa-solid fa-grip-lines"></i> Cadangan</button>
      <button type="button" onclick="traceExport('svg')" title="Unduh diagram sebagai SVG (bisa dibuka di Visio/Inkscape)"><i class="fa-solid fa-download"></i> SVG</button>
      <button type="button" onclick="traceExport('png')" title="Unduh sebagai gambar PNG"><i class="fa-solid fa-image"></i> PNG</button></span></div>
    <div id="trace-diagram"></div><div id="trace-list" style="display:none">${list}</div>`;
  return html;
}

function disconnectCore(connectionId) {
  if (!confirm("Apakah Anda yakin ingin memutus sambungan core ini?")) return;

  apiRequest(`/api/connections/${connectionId}`, "DELETE")
    .then(() => {
      alert("Sambungan berhasil diputus!");
      fetchConnectionsAndRender();
    })
    .catch((err) => alert("Gagal memutus sambungan: " + err.message));
}

function closeCoreDetailModal() {
  coreNav.length = 0;
  updateCoreBack();
  document.getElementById("modal-core-detail").style.display = "none";
}

// --- KAPASITAS TRUNK POP ---
const POPTRUNK = { id: null, data: null, edit: false, draft: null };
function popTrunkLoad(item, container) {
  return apiRequest(`/api/nodes/${Number(item.id)}/trunk`)
    .then((d) => {
      if (!d || d.delivered_mbps === undefined) return;
      if (
        !currentActiveAsset ||
        String(currentActiveAsset.id) !== String(item.id)
      )
        return;
      POPTRUNK.id = item.id;
      POPTRUNK.data = d;
      renderPopOtb(item, container);
    })
    .catch(() => {});
}
const POPTRUNK_LABEL = {
  ok: "Normal",
  warn: "Hampir penuh",
  over: "Melebihi kapasitas",
  unset: "Belum diisi",
};
function popTrunkReadForm() {
  const g = (i) => (document.getElementById(i) || {}).value;
  if (g("pt-val") === undefined) return;
  POPTRUNK.draft = { val: g("pt-val"), unit: g("pt-unit"), ob: g("pt-ob") };
}
function popTrunkHtml(item, canEdit) {
  const d =
    POPTRUNK.id != null && String(POPTRUNK.id) === String(item.id)
      ? POPTRUNK.data
      : null;
  if (!d) return "";
  const pct = Math.max(0, Math.min(100, d.util_pct || 0));
  const head =
    `<div class="trunk-head"><b><i class="fa-solid fa-gauge-high"></i> Kapasitas trunk</b>` +
    `<span class="trunk-badge">${escapeHtml(POPTRUNK_LABEL[d.status] || d.status)}</span>` +
    `${canEdit && !POPTRUNK.edit ? `<button type="button" class="btn-mini" onclick="popTrunkEdit()"><i class="fa-solid fa-pen"></i> ${d.trunk_mbps ? "Ubah" : "Atur"}</button>` : ""}</div>`;
  let body = "";
  if (d.trunk_mbps) {
    body +=
      `<div class="trunk-bar" title="${d.util_pct}%"><i style="width:${pct}%"></i></div>` +
      `<div class="trunk-line"><b>${escapeHtml(fmtBw(d.effective_mbps))}</b> dari <b>${escapeHtml(fmtBw(d.trunk_mbps))}</b> (${d.util_pct}%) &middot; sisa <b>${escapeHtml(fmtBw(d.free_mbps))}</b>${d.free_mbps < 0 ? " (kekurangan)" : ""}</div>`;
  } else {
    body += `<div class="trunk-line">Kapasitas trunk belum diatur. Beban saat ini: <b>${escapeHtml(fmtBw(d.effective_mbps))}</b>.</div>`;
  }
  body +=
    `<div class="trunk-stats"><span>Pelanggan terdeliver <b>${d.delivered_count}</b></span><span>Total BW <b>${escapeHtml(fmtBw(d.delivered_mbps))}</b></span>` +
    `<span>Overbooking <b>1 : ${d.overbook}</b></span>${d.inactive_count ? `<span class="warn">Tidak aktif <b>${d.inactive_count}</b></span>` : ""}</div>`;
  const types = Object.keys(d.by_type || {});
  if (types.length)
    body += `<div class="trunk-chips">${types.map((k) => `<span class="pd-chip">${escapeHtml(k === "-" ? "Jenis ?" : k)} &middot; ${d.by_type[k].count} &middot; ${escapeHtml(fmtBw(d.by_type[k].mbps))}</span>`).join("")}</div>`;
  if (d.missing_bw_count)
    body += `<div class="trunk-note">${d.missing_bw_count} pelanggan aktif belum diisi bandwidth-nya; beban sebenarnya bisa lebih besar.</div>`;
  if ((d.top || []).length)
    body += `<details class="trunk-top"><summary>Pelanggan terbesar</summary>${d.top.map((x) => `<div>${escapeHtml(x.name)} <span class="tp-muted">${escapeHtml(x.link_type || "-")} &middot; ${escapeHtml(x.service || "-")}</span> <b>${escapeHtml(fmtBw(x.mbps))}</b></div>`).join("")}</details>`;
  if (POPTRUNK.edit) {
    const dr =
      POPTRUNK.draft ||
      (() => {
        const s = bwSplit(d.trunk_mbps);
        return {
          val: s.v,
          unit: d.trunk_mbps == null ? "Gbps" : s.u,
          ob: d.overbook > 1 ? String(d.overbook) : "",
        };
      })();
    body +=
      `<div class="trunk-form"><label>Kapasitas trunk<span class="ap-row"><input type="number" id="pt-val" min="0" step="any" value="${escapeHtml(dr.val)}" class="ap-input" />` +
      `<select id="pt-unit" class="ap-input ap-unit"><option value="Mbps"${dr.unit === "Mbps" ? " selected" : ""}>Mbps</option><option value="Gbps"${dr.unit === "Gbps" ? " selected" : ""}>Gbps</option></select></span></label>` +
      `<label>Overbooking 1 : n<input type="number" id="pt-ob" min="1" max="100" step="any" value="${escapeHtml(dr.ob)}" placeholder="1" class="ap-input" /></label>` +
      `<span class="pd-actions"><button type="button" class="btn-ok" onclick="popTrunkSave()"><i class="fa-solid fa-check"></i> Simpan</button><button type="button" class="btn-mini" onclick="popTrunkCancel()">Batal</button></span></div>`;
  }
  return `<div class="trunk-card trunk-${escapeHtml(d.status)}">${head}${body}</div>`;
}
function popTrunkEdit() {
  POPTRUNK.edit = true;
  POPTRUNK.draft = null;
  const it = currentActiveAsset,
    c = document.getElementById("core-grid-container");
  if (it && c) renderPopOtb(it, c);
}
function popTrunkCancel() {
  POPTRUNK.edit = false;
  POPTRUNK.draft = null;
  const it = currentActiveAsset,
    c = document.getElementById("core-grid-container");
  if (it && c) renderPopOtb(it, c);
}
function popTrunkSave() {
  const item = currentActiveAsset;
  if (!item) return;
  popTrunkReadForm();
  const dr = POPTRUNK.draft || {};
  let trunk = null,
    ob = null;
  if ((dr.val || "").trim()) {
    trunk = bwToMbps(dr.val, dr.unit);
    if (trunk == null || trunk <= 0)
      return alert("Kapasitas trunk harus berupa angka lebih dari 0.");
  }
  if ((dr.ob || "").trim()) {
    ob = parseFloat(String(dr.ob).replace(",", "."));
    if (!isFinite(ob) || ob < 1 || ob > 100)
      return alert("Rasio overbooking harus 1 sampai 100.");
  }
  return apiRequest(`/api/nodes/${Number(item.id)}`, "PUT", {
    trunk_mbps: trunk,
    trunk_overbook: ob,
  })
    .then(() => {
      item.trunk_mbps = trunk;
      item.trunk_overbook = ob;
      POPTRUNK.edit = false;
      POPTRUNK.draft = null;
      const c = document.getElementById("core-grid-container");
      return popTrunkLoad(item, c);
    })
    .catch((err) => alert("Gagal menyimpan trunk: " + err.message));
}

// --- RENDER POP: tiap OTB berisi grid port (OTB-n / Pxx) + posisi fisik opsional + perangkat yang dipatch ---
const POPDEV = {
  id: null,
  list: [],
  map: new Map(),
  catalog: null,
  editPort: null,
  draft: null,
  host: null,
};
const POPDEV_FALLBACK = {
  purposes: {
    PON: "PON (ke OLT)",
    PTP: "Pelanggan PTP (langsung ke perangkat)",
    UPLINK: "Uplink / backbone",
    LAINNYA: "Lainnya",
  },
  device_types: ["OLT", "Router", "Switch", "Lainnya"],
  vendors: [
    "Huawei",
    "Raisecom",
    "Cisco",
    "ZTE",
    "Nokia",
    "MikroTik",
    "Lainnya",
  ],
};
const POPDEV_SHORT = { PON: "PON", PTP: "PTP", UPLINK: "UPL", LAINNYA: "LAIN" };
function popDevFor(item) {
  return POPDEV.id != null && String(POPDEV.id) === String(item.id)
    ? POPDEV
    : { map: new Map(), list: [], catalog: null };
}
function popDevLoad(item, container) {
  POPDEV.host = container;
  return apiRequest(`/api/nodes/${Number(item.id)}/port-devices`)
    .then((d) => {
      if (
        !currentActiveAsset ||
        String(currentActiveAsset.id) !== String(item.id)
      )
        return;
      POPDEV.id = item.id;
      POPDEV.list = d.devices || [];
      POPDEV.map = new Map(POPDEV.list.map((x) => [x.port_core, x]));
      POPDEV.catalog = d.catalog || null;
      renderPopOtb(item, container);
    })
    .catch(() => {});
}
function popDevSummary(d) {
  const bits = [
    d.device_name,
    d.slot ? `slot ${d.slot}` : null,
    d.interface,
  ].filter(Boolean);
  if (d.vlan) bits.push(`VLAN ${d.vlan}`);
  if (d.customer_name) bits.push(d.customer_name);
  return bits.join(" · ");
}
function renderPopOtb(item, container) {
  if (POPTRUNK.edit) popTrunkReadForm();
  const sizes = otbSizes(item.capacity),
    det = otbDetails(item);
  const pd = popDevFor(item);
  const canEdit = can("asset.write");
  // sisi belakang port OTB seperti joint closure: kabel MASUK (dari hulu/feeder) dan kabel KELUAR (ke hilir/distribusi)
  const inMap = new Map(),
    outMap = new Map(),
    byPort = new Map();
  activeConnections.forEach((c) => {
    if (
      String(c.from_asset_type) === "NODE" &&
      String(c.from_asset_id) === String(item.id)
    ) {
      outMap.set(c.from_port_core, c);
      byPort.set(c.from_port_core, c);
    }
    if (
      String(c.to_asset_type) === "NODE" &&
      String(c.to_asset_id) === String(item.id)
    ) {
      inMap.set(c.to_port_core, c);
      byPort.set(c.to_port_core, c);
    }
  });
  const total = sizes.reduce((a, b) => a + b, 0);
  const usedAll = otbPortLabels(item).filter((l) => byPort.has(l)).length;
  const fullAll = otbPortLabels(item).filter(
    (l) => inMap.has(l) && outMap.has(l),
  ).length;
  let html =
    popTrunkHtml(item, canEdit) +
    cxStart(
      "otbvis",
      "h4",
      `Visualisasi OTB POP (${sizes.length} OTB &middot; ${total} port)`,
      "margin-bottom: 4px; font-size: 14px; color: #334155;",
    ) +
    `<div class="cx-body">` +
    `<div style="margin-bottom: 12px; font-size: 12px; color: #475569;">Terpakai: <b style="color:#2563eb;">${usedAll}</b> &middot; Tersedia: <b style="color:#16a34a;">${total - usedAll}</b> &middot; Total: <b>${total}</b> port` +
    ` &middot; Masuk+keluar lengkap: <b>${fullAll}</b>` +
    ` &middot; Perangkat terhubung: <b>${pd.list.length}</b>${canEdit ? ' <span class="otb-hint">(klik port untuk mencatat perangkat / interface)</span>' : ""}</div>`;
  sizes.forEach((sz, k) => {
    const d = det[k] || {};
    const usedK = Array.from({ length: sz }, (_, i) =>
      otbLabel(k + 1, i + 1),
    ).filter((l) => byPort.has(l)).length;
    const pos = otbPositionText(d),
      kind = (OTB_KINDS.find((x) => x[0] === (d.kind || "")) ||
        OTB_KINDS[0])[1];
    html +=
      `<div class="otb-box"><div class="otb-head"><b>OTB-${k + 1}</b>${d.label ? ` &middot; ${escapeHtml(d.label)}` : ""}` +
      `<span class="otb-meta">${sz} port &middot; ${usedK} terpakai</span>` +
      `${d.kind ? `<small class="otb-pos">${escapeHtml(kind)}${pos ? " &middot; " + escapeHtml(pos) : ""}</small>` : ""}</div><div class="otb-grid">`;
    for (let p = 1; p <= sz; p++) {
      const lab = otbLabel(k + 1, p),
        ci = inMap.get(lab),
        co = outMap.get(lab),
        c = co || ci,
        on = !!c,
        dv = pd.map.get(lab);
      const coreOf = (x) =>
        x && x.via_core ? traceShortPort(x.via_core) : "-";
      const rear =
        ci && co
          ? `\u2191${coreOf(ci)} \u2193${coreOf(co)}`
          : co
            ? `\u2193${coreOf(co)}`
            : ci
              ? `\u2191${coreOf(ci)}`
              : "Idle";
      const tip =
        `${lab} | belakang (kabel): ` +
        (on
          ? [
              ci ? `masuk core ${ci.via_core || "-"}` : null,
              co ? `keluar core ${co.via_core || "-"}` : null,
            ]
              .filter(Boolean)
              .join(", ")
          : "kosong") +
        ` | depan: ` +
        (dv
          ? `${popDevSummary(dv)} (${dv.purpose})${dv.slot ? " slot " + dv.slot : ""}`
          : "belum ada perangkat");
      html +=
        `<div class="otb-port${on ? " on" : ""}${ci && co ? " full" : ""}${dv ? " pd pd-" + escapeHtml(String(dv.purpose).toLowerCase()) : ""}${canEdit ? " clickable" : ""}" title="${escapeHtml(tip)}"${canEdit ? ` onclick="popDevOpen('${lab}')"` : ""}>` +
        `<b>P${String(p).padStart(2, "0")}</b><i class="fa-solid ${on ? "fa-plug-circle-check" : "fa-plug"}"></i>` +
        `<span>${escapeHtml(rear)}</span>` +
        (dv
          ? `<em class="pd-tag">${escapeHtml(POPDEV_SHORT[dv.purpose] || dv.purpose)}</em><small class="pd-dev">${escapeHtml(traceTrunc(dv.device_name, 12))}</small>`
          : "") +
        `</div>`;
    }
    html += "</div></div>";
  });
  html += "</div></div>"; // tutup cx-body + cx-sec "otbvis"
  // tabel perangkat yang terhubung ke port OTB
  html += `<div class="pd-section cx-sec${cxCls("otbdev")}" data-cx="otbdev"><div class="pd-title cx-head" role="button" tabindex="0" aria-expanded="${cxIsOpen("otbdev")}"><i class="fa-solid fa-server"></i> Perangkat terhubung ke port OTB</div><div class="cx-body">`;
  if (pd.list.length) {
    html +=
      `<table class="data-table pd-table"><thead><tr><th>Port OTB</th><th>Peruntukan</th><th>Perangkat</th><th>Slot</th><th>Interface</th><th>VLAN</th><th>Service</th><th>Pelanggan</th>${canEdit ? "<th></th>" : ""}</tr></thead><tbody>` +
      pd.list
        .map(
          (d) =>
            `<tr><td>${escapeHtml(d.port_core)}</td><td><span class="pd-chip pd-${escapeHtml(String(d.purpose).toLowerCase())}">${escapeHtml(POPDEV_SHORT[d.purpose] || d.purpose)}</span></td>` +
            `<td>${escapeHtml(d.device_name)}<small class="pd-sub">${escapeHtml([d.device_type, d.vendor].filter(Boolean).join(" · "))}</small></td><td>${escapeHtml(d.slot || "-")}</td><td>${escapeHtml(d.interface)}</td>` +
            `<td>${escapeHtml(d.vlan || "-")}</td><td>${escapeHtml(d.service || "-")}</td><td>${escapeHtml(d.customer_name || "-")}</td>` +
            `${canEdit ? `<td><button type="button" class="btn-mini" onclick="popDevOpen('${escapeHtml(d.port_core)}')" title="Ubah"><i class="fa-solid fa-pen"></i></button></td>` : ""}</tr>`,
        )
        .join("") +
      "</tbody></table>";
  } else {
    html += `<div class="tp-muted">Belum ada perangkat yang dicatat pada port OTB POP ini.</div>`;
  }
  html += `</div></div><div id="pop-dev-form"></div>`;
  container.innerHTML = html;
  if (POPDEV.editPort && pd === POPDEV) popDevRenderForm(item);
}
function popDevOpen(port) {
  const item = currentActiveAsset;
  if (!item || !isPop(item) || !can("asset.write")) return;
  const pd = popDevFor(item),
    ex = pd.map.get(port);
  POPDEV.editPort = port;
  POPDEV.draft = ex
    ? Object.assign({}, ex)
    : {
        port_core: port,
        purpose: "PON",
        device_type: "OLT",
        vendor: "",
        device_name: "",
        interface: "",
        vlan: "",
        service: "",
        customer_name: "",
        customer_node_id: null,
        notes: "",
      };
  popDevRenderForm(item);
  const f = document.getElementById("pop-dev-form");
  if (f && f.scrollIntoView) {
    try {
      f.scrollIntoView({ block: "nearest" });
    } catch (_) {}
  }
}
function popDevReadForm() {
  const v = (id) => {
    const el = document.getElementById(id);
    return el ? el.value : undefined;
  };
  const d = POPDEV.draft || {};
  [
    "purpose",
    "device_type",
    "vendor",
    "device_name",
    "slot",
    "interface",
    "vlan",
    "service",
    "customer_name",
    "notes",
  ].forEach((k) => {
    const x = v("pd-" + k);
    if (x !== undefined) d[k] = x;
  });
  POPDEV.draft = d;
  return d;
}
function popDevOnPurpose() {
  const d = popDevReadForm();
  if (d.purpose === "PON" && (!d.device_type || d.device_type === "Router"))
    d.device_type = "OLT";
  if (d.purpose === "PTP" && (!d.device_type || d.device_type === "OLT"))
    d.device_type = "Router";
  popDevRenderForm(currentActiveAsset);
}
function popDevRenderForm(item) {
  const box = document.getElementById("pop-dev-form");
  if (!box || !POPDEV.editPort) return;
  const pd = popDevFor(item),
    cat = pd.catalog || POPDEV_FALLBACK,
    d = POPDEV.draft || {};
  const exists = pd.map.has(POPDEV.editPort);
  const c = activeConnections.find(
    (x) =>
      (String(x.from_asset_type) === "NODE" &&
        String(x.from_asset_id) === String(item.id) &&
        x.from_port_core === POPDEV.editPort) ||
      (String(x.to_asset_type) === "NODE" &&
        String(x.to_asset_id) === String(item.id) &&
        x.to_port_core === POPDEV.editPort),
  );
  const opts = (arr, cur) =>
    arr
      .map(
        (o) =>
          `<option value="${escapeHtml(o)}"${o === cur ? " selected" : ""}>${escapeHtml(o)}</option>`,
      )
      .join("");
  const vend =
    cat.vendors.includes(d.vendor) || !d.vendor
      ? cat.vendors
      : cat.vendors.concat([d.vendor]);
  const devNames = Array.from(new Set(pd.list.map((x) => x.device_name)));
  const custs = (allInventoryData || [])
    .filter(
      (x) =>
        x.category === "NODE" && (x.type || "").toUpperCase() === "PELANGGAN",
    )
    .slice(0, 400);
  const ptp = d.purpose === "PTP";
  box.innerHTML =
    `<div class="pd-form"><div class="pd-form-h"><b><i class="fa-solid fa-plug"></i> ${escapeHtml(POPDEV.editPort)}</b>` +
    `<span class="pd-sub">${c ? "Belakang: kabel core " + escapeHtml(c.via_core || "-") : "Belakang: belum disambung ke kabel"} &middot; Depan: 1 perangkat per port</span></div>` +
    `<div class="pd-grid">` +
    `<label>Peruntukan<select id="pd-purpose" onchange="popDevOnPurpose()">${Object.keys(
      cat.purposes,
    )
      .map(
        (k) =>
          `<option value="${k}"${k === d.purpose ? " selected" : ""}>${escapeHtml(cat.purposes[k])}</option>`,
      )
      .join("")}</select></label>` +
    `<label>Jenis perangkat<select id="pd-device_type">${opts(cat.device_types, d.device_type)}</select></label>` +
    `<label>Merek / vendor<select id="pd-vendor"><option value=""${d.vendor ? "" : " selected"}>-</option>${opts(vend, d.vendor)}</select></label>` +
    `<label>Nama perangkat<input id="pd-device_name" list="pd-devnames" value="${escapeHtml(d.device_name || "")}" placeholder="mis. OLT-BJM-1 / RTR-CORE-2"><datalist id="pd-devnames">${devNames.map((n) => `<option value="${escapeHtml(n)}">`).join("")}</datalist></label>` +
    `<label>Slot / PON (manual)<input id="pd-slot" value="${escapeHtml(d.slot || "")}" placeholder="mis. 1/1 (Raisecom) atau 1/1/3 (ADTRAN)"></label>` +
    `<label>Interface / port perangkat<input id="pd-interface" value="${escapeHtml(d.interface || "")}" placeholder="mis. 0/1/1 atau GigabitEthernet0/0/1"></label>` +
    `<label>VLAN<input id="pd-vlan" value="${escapeHtml(d.vlan || "")}" placeholder="mis. 100 atau 100,200 atau 100-110"></label>` +
    `<label class="pd-wide">Service<input id="pd-service" value="${escapeHtml(d.service || "")}" placeholder="mis. Dedicated Internet 100M / VPN L2"></label>` +
    (ptp
      ? `<label class="pd-wide">Pelanggan (PTP)<input id="pd-customer_name" list="pd-custs" value="${escapeHtml(d.customer_name || "")}" placeholder="ketik nama / pilih aset pelanggan"><datalist id="pd-custs">${custs.map((x) => `<option value="${escapeHtml(x.name)}">`).join("")}</datalist></label>`
      : "") +
    `<label class="pd-wide">Catatan<input id="pd-notes" value="${escapeHtml(d.notes || "")}" placeholder="opsional"></label></div>` +
    `<div class="pd-actions"><button type="button" class="btn-ok" onclick="popDevSave()"><i class="fa-solid fa-check"></i> Simpan</button>` +
    `${exists ? '<button type="button" class="btn-mini danger" onclick="popDevDelete()"><i class="fa-solid fa-trash"></i> Hapus catatan</button>' : ""}` +
    `<button type="button" class="btn-mini" onclick="popDevClose()">Tutup</button></div></div>`;
}
function popDevClose() {
  POPDEV.editPort = null;
  POPDEV.draft = null;
  const box = document.getElementById("pop-dev-form");
  if (box) box.innerHTML = "";
}
function popDevSave() {
  const item = currentActiveAsset;
  if (!item || !POPDEV.editPort) return;
  const d = popDevReadForm();
  let custId = null;
  if (d.purpose === "PTP" && d.customer_name) {
    const m = (allInventoryData || []).find(
      (x) =>
        x.category === "NODE" &&
        (x.type || "").toUpperCase() === "PELANGGAN" &&
        x.name === d.customer_name,
    );
    if (m) custId = Number(m.id);
  }
  const payload = {
    port_core: POPDEV.editPort,
    purpose: d.purpose,
    device_type: d.device_type,
    vendor: d.vendor || null,
    device_name: (d.device_name || "").trim(),
    slot: (d.slot || "").trim() || null,
    interface: (d.interface || "").trim(),
    vlan: (d.vlan || "").trim() || null,
    service: (d.service || "").trim() || null,
    customer_name:
      d.purpose === "PTP" ? (d.customer_name || "").trim() || null : null,
    customer_node_id: d.purpose === "PTP" ? custId : null,
    notes: (d.notes || "").trim() || null,
  };
  if (!payload.device_name || !payload.interface)
    return alert("Nama perangkat dan interface/port wajib diisi.");
  if (payload.purpose === "PTP" && !payload.customer_name)
    return alert("Peruntukan PTP: nama pelanggan wajib diisi.");
  return apiRequest(
    `/api/nodes/${Number(item.id)}/port-devices`,
    "PUT",
    payload,
  )
    .then((r) => {
      POPDEV.id = item.id;
      POPDEV.list = r.devices || [];
      POPDEV.map = new Map(POPDEV.list.map((x) => [x.port_core, x]));
      popDevClose();
      const cont = document.getElementById("core-grid-container");
      if (cont) renderPopOtb(item, cont);
      loadTracePanel(item);
    })
    .catch((err) => alert("Gagal menyimpan perangkat port: " + err.message));
}
function popDevDelete() {
  const item = currentActiveAsset;
  if (!item || !POPDEV.editPort) return;
  if (!confirm(`Hapus catatan perangkat pada ${POPDEV.editPort}?`)) return;
  return apiRequest(
    `/api/nodes/${Number(item.id)}/port-devices?port=${encodeURIComponent(POPDEV.editPort)}`,
    "DELETE",
  )
    .then((r) => {
      POPDEV.list = r.devices || [];
      POPDEV.map = new Map(POPDEV.list.map((x) => [x.port_core, x]));
      popDevClose();
      const cont = document.getElementById("core-grid-container");
      if (cont) renderPopOtb(item, cont);
      loadTracePanel(item);
    })
    .catch((err) => alert("Gagal menghapus: " + err.message));
}

// --- RENDER CORE KABEL / CLOSURE / POP (STANDAR TUBE & CORE COLOR ISO) ---
function renderCableCores(item, container) {
  const type = (item.type || "").toUpperCase();
  let labelHeader = "Core Kabel";
  if (type === "CLOSURE") labelHeader = "Joint Closure (penghubung kabel)";
  if (type === "POP") labelHeader = "ODF / Patch Panel POP Core";

  const totalCores = getPortCount(item);
  const connectedPorts = getConnectedLocalPorts(item);
  const jd = isJunction(item) ? jointDirsLocal(item) : null;
  if (jd) {
    connectedPorts.clear();
    jd.ins.forEach((p) => connectedPorts.add(p));
    jd.outs.forEach((p) => connectedPorts.add(p));
  }

  const usedCount = Array.from({ length: totalCores }, (_, k) => k + 1).filter(
    (i) =>
      connectedPorts.has(`Tube ${Math.floor((i - 1) / 12) + 1} - Core ${i}`),
  ).length;

  let html =
    cxStart(
      "corevis",
      "h4",
      `Visualisasi ${labelHeader} (${escapeHtml(item.capacity || totalCores + "C")})`,
      "margin-bottom: 4px; font-size: 14px; color: #334155;",
    ) + `<div class="cx-body">`;
  html += `<div style="margin-bottom: 12px; font-size: 12px; color: #475569;">
    Terpakai: <b style="color:#2563eb;">${usedCount}</b> &middot;
    Tersedia: <b style="color:#16a34a;">${totalCores - usedCount}</b> &middot;
    Total: <b>${totalCores}</b> core</div>`;
  html += `<div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap: 10px;">`;

  for (let i = 1; i <= totalCores; i++) {
    const colorInfo = ISO_FIBER_COLORS[(i - 1) % 12];
    const tubeNo = Math.floor((i - 1) / 12) + 1;
    const coreLabel = `Tube ${tubeNo} - Core ${i}`;

    // Connected jika core INI disambung langsung, atau dipakai sambungan yang melewati kabel ini
    let isConnected = connectedPorts.has(coreLabel);
    let statusText = isConnected ? "Connected" : "Available";
    let statusColor = isConnected ? "#2563eb" : "#16a34a";
    if (jd) {
      const inn = jd.ins.has(coreLabel),
        out = jd.outs.has(coreLabel);
      statusText =
        inn && out
          ? "Tersambung (hulu \u2194 hilir)"
          : inn
            ? "Masuk, belum diteruskan"
            : out
              ? "Hanya keluar"
              : "Available";
      statusColor = inn && out ? "#2563eb" : inn || out ? "#d97706" : "#16a34a";
    }

    html += `
      <div style="border: 1px solid #e2e8f0; border-radius: 6px; padding: 8px; text-align: center; background: #fafafa;">
        <div style="font-size: 10px; color: #64748b; margin-bottom: 4px;">
          ${coreLabel}
        </div>
        <div class="keep-color" style="background-color: ${colorInfo.hex}; color: ${colorInfo.text}; border: ${colorInfo.border ? "1px solid " + colorInfo.border : "none"};
                    padding: 6px; border-radius: 4px; font-weight: bold; font-size: 12px; box-shadow: inset 0 0 4px rgba(0,0,0,0.2);">
          ${colorInfo.name}
        </div>
        <div style="font-size: 10px; margin-top: 5px; color: ${statusColor}; font-weight: 600;">${statusText}</div>
      </div>
    `;
  }

  html += `</div>`;
  if (jd) html += `<div id="junction-box" style="margin-top:14px;"></div>`;
  html += `</div></div>`; // tutup cx-body + cx-sec "corevis"
  container.innerHTML = html;
  if (jd) loadJunctionBox(item);
}

// Closure: daftar kabel yang berujung/melintas + tombol "Pecah kabel di closure" untuk kabel yang hanya melintas
function loadJunctionBox(item) {
  const box = document.getElementById("junction-box");
  if (!box) return;
  apiRequest(`/api/nodes/${Number(item.id)}/junction`)
    .then((d) => {
      const b = document.getElementById("junction-box");
      if (
        !b ||
        !currentActiveAsset ||
        String(currentActiveAsset.id) !== String(item.id)
      )
        return;
      if (!d.cables.length) {
        b.innerHTML =
          '<div class="tp-muted">Belum ada kabel di closure ini.</div>';
        return;
      }
      const canSplit = can("asset.write");
      b.innerHTML =
        '<h4 style="margin:0 0 6px;font-size:13px;color:#334155;">Kabel pada closure ini</h4>' +
        '<table class="data-table" style="width:100%;font-size:12px;"><thead><tr><th>Kabel</th><th>Posisi</th><th>Core terpakai</th><th></th></tr></thead><tbody>' +
        d.cables
          .map(
            (c) => `<tr><td>${assetLinkHtml("CABLE", c.id, c.name)}</td>
          <td>${c.through ? '<span style="color:#b45309;">Melintas</span>' : c.at_end ? "Ujung hilir (masuk)" : "Ujung hulu (keluar)"}</td>
          <td>${c.used}/${c.total}</td>
          <td>${c.through && canSplit ? `<button type="button" class="btn-mini" onclick="splitCableAtClosure(${Number(c.id)}, ${Number(item.id)})" title="Pecah kabel menjadi 2 segmen di closure ini; core yang sudah terpakai otomatis tersambung lewat closure"><i class="fa-solid fa-scissors"></i> Pecah kabel di closure</button>` : ""}</td></tr>`,
          )
          .join("") +
        "</tbody></table>";
    })
    .catch(() => {
      box.innerHTML = "";
    });
}
function splitCableAtClosure(cableId, nodeId) {
  if (
    !confirm(
      "Pecah kabel ini menjadi dua segmen di closure? Core yang sudah terpakai akan otomatis disambung lewat closure.",
    )
  )
    return;
  const full = confirm(
    "Sambung penuh? OK = SEMUA core kabel (sesuai kapasitas) disambung di closure.\nBatal = hanya core yang sudah terpakai.",
  );
  apiRequest(`/api/cables/${Number(cableId)}/split-at-node`, "POST", {
    node_id: Number(nodeId),
    splice_all: full,
  })
    .then((r) => {
      alert(
        `Kabel dipecah. ${Number(r.cores_spliced || 0) + Number(r.cores_full_added || 0)} core disambung lewat closure.`,
      );
      loadData();
      fetchConnectionsAndRender();
    })
    .catch((err) => alert("Gagal memecah kabel: " + err.message));
}

// --- Kolom redaman per core: hitung (km serat + splice) vs ukur (OTDR terbaru) ---
const OTDR_COLOR = { OK: "#14b8a6", WARN: "#f59e0b", BAD: "#db2777" };
const OTDR_LABEL = { OK: "Normal", WARN: "Cek", BAD: "Bermasalah" };
function otdrBadge(st) {
  return st
    ? `<span class="otdr-badge s-${escapeHtml(st)}">${escapeHtml(OTDR_LABEL[st] || st)}</span>`
    : "&ndash;";
}
function fmtDb(v, signed) {
  if (v === null || v === undefined || v === "") return "&ndash;";
  const n = Number(v);
  return (signed && n > 0 ? "+" : "") + n.toFixed(2);
}
const FIBER_LABEL = { SM: "Single-mode", MM: "Multimode" };
function modeBadge(mode) {
  return `<span class="mode-badge m-${escapeHtml(mode || "SM")}">${escapeHtml(mode === "MM" ? "MM" : "SM")}</span>`;
}
// Relasi tiang/HH <-> kabel: DIHITUNG dari posisi (koridor di sekitar jalur kabel), tidak disimpan di database
function loadAssetSupports(asset) {
  const box = document.getElementById("asset-supports-box");
  if (!box) return;
  const t = (asset.type || "").toUpperCase();
  const isCable = asset.category === "CABLE";
  const isSupport = asset.category === "NODE" && (t === "TIANG" || t === "HH");
  if (!isCable && !isSupport) {
    box.innerHTML = "";
    return;
  }
  box.innerHTML = '<small class="muted">Memuat relasi dari posisi...</small>';
  const url = isCable
    ? `/api/cables/${Number(asset.id)}/supports`
    : `/api/nodes/${Number(asset.id)}/cables-through`;
  apiRequest(url)
    .then((d) => {
      if (
        !currentActiveAsset ||
        String(currentActiveAsset.id) !== String(asset.id) ||
        currentActiveAsset.category !== asset.category
      )
        return;
      box.innerHTML = isCable
        ? renderCableSupports(d)
        : renderNodeCablesThrough(d);
    })
    .catch(() => {
      box.innerHTML = "";
    });
}
function renderCableSupports(d) {
  const kind = (d.kinds || []).join("/") || "TIANG/HH";
  const warn = d.spans_over_rule
    ? `<div class="sup-warn"><i class="fa-solid fa-triangle-exclamation"></i> ${d.spans_over_rule} bentang melebihi ${escapeHtml(String(d.spacing_rule_m))} m (terpanjang ${escapeHtml(String(d.max_span_m))} m): kemungkinan ada ${escapeHtml(kind)} yang belum tercatat atau perlu ditambah.</div>`
    : "";
  const rows = (d.items || [])
    .map(
      (i, n) => `<tr>
      <td>${n + 1}</td><td>${assetLinkHtml("NODE", i.id, i.name)}</td><td>${escapeHtml(i.type)}</td>
      <td>${escapeHtml(String(i.along_m))}</td><td>${escapeHtml(String(i.offset_m))}</td><td>${escapeHtml(i.status || "-")}</td></tr>`,
    )
    .join("");
  return (
    cxStart(
      "supports",
      "h4",
      `<i class="fa-solid fa-grip-lines-vertical"></i> ${escapeHtml(kind)} yang dilewati
      <small>(${d.count} aset &middot; dihitung dari posisi, &plusmn;${escapeHtml(String(d.radius_m))} m dari jalur &middot; panjang ${escapeHtml(String(d.length_m))} m)</small>`,
    ) +
    `<div class="cx-body">
    ${warn}
    ${
      d.count
        ? `<div class="sup-scroll"><table class="sup-table"><thead><tr><th>#</th><th>Nama</th><th>Tipe</th><th>Jarak dari awal (m)</th><th>Geser dari jalur (m)</th><th>Status</th></tr></thead><tbody>${rows}</tbody></table></div>`
        : `<div class="muted">Tidak ada ${escapeHtml(kind)} dalam koridor kabel ini.</div>`
    }</div></div>`
  );
}
function renderNodeCablesThrough(d) {
  const rows = (d.items || [])
    .map(
      (i) => `<tr>
      <td>${assetLinkHtml("CABLE", i.id, i.name)}</td><td>${escapeHtml(i.type || "-")} &middot; ${escapeHtml(i.capacity || "-")}</td><td>${escapeHtml(i.installation || "-")}</td>
      <td>${escapeHtml(String(i.along_m))} / ${escapeHtml(String(i.length_m))}</td><td>${escapeHtml(String(i.offset_m))}</td><td>${escapeHtml(i.status || "-")}</td></tr>`,
    )
    .join("");
  return (
    cxStart(
      "supports",
      "h4",
      `<i class="fa-solid fa-grip-lines"></i> Kabel yang menumpang
      <small>(${d.count} kabel &middot; dihitung dari posisi, &plusmn;${escapeHtml(String(d.radius_m))} m dari jalur)</small>`,
    ) +
    `<div class="cx-body">
    ${
      d.count
        ? `<div class="sup-scroll"><table class="sup-table"><thead><tr><th>Kabel</th><th>Jenis</th><th>Pasang</th><th>Posisi / panjang (m)</th><th>Geser (m)</th><th>Status</th></tr></thead><tbody>${rows}</tbody></table></div>`
        : `<div class="muted">Belum ada kabel yang melewati ${escapeHtml(d.type)} ini.</div>`
    }</div></div>`
  );
}

function loadAssetLoss(asset) {
  const box = document.getElementById("asset-loss-box");
  if (!box) return;
  const t = (asset.type || "").toUpperCase();
  if (asset.category === "NODE" && ["TIANG", "HH", "SLACK"].includes(t)) {
    box.innerHTML = "";
    return;
  }
  box.innerHTML = "Memuat redaman...";
  apiRequest(
    `/api/loss/asset?asset_type=${encodeURIComponent(asset.category)}&asset_id=${Number(asset.id)}`,
  )
    .then((d) => {
      if (
        !currentActiveAsset ||
        String(currentActiveAsset.id) !== String(asset.id) ||
        currentActiveAsset.category !== asset.category
      )
        return;
      box.innerHTML =
        d.kind === "CABLE" ? renderCableLoss(d) : renderNodeLoss(d);
    })
    .catch(() => {
      box.innerHTML = "";
    });
}
function powerInputs(i, pw, canEdit, port) {
  if (!canEdit)
    return `<td class="num">${pw && pw.tx_dbm != null ? fmtDb(pw.tx_dbm) : "&ndash;"}</td><td class="num">${pw && pw.rx_dbm != null ? fmtDb(pw.rx_dbm) : "&ndash;"}</td><td></td>`;
  return `<td><input class="pw-in" type="number" step="0.01" id="pw-tx-${i}" value="${pw && pw.tx_dbm != null ? Number(pw.tx_dbm) : ""}" /></td>
    <td><input class="pw-in" type="number" step="0.01" id="pw-rx-${i}" value="${pw && pw.rx_dbm != null ? Number(pw.rx_dbm) : ""}" /></td>
    <td><button type="button" class="pw-save" title="Simpan daya optik" onclick="savePower(${i})"><i class="fa-solid fa-floppy-disk"></i></button></td>`;
}
let LOSSBOX = { ports: [], asset: null };
const SIG_LABEL = {
  OK: "Normal",
  WARN: "Margin tipis",
  BAD: "Di bawah batas",
  HIGH: "Terlalu kuat",
};
function sigBadge(st) {
  return st
    ? `<span class="otdr-badge s-${st === "HIGH" ? "WARN" : escapeHtml(st)}">${escapeHtml(SIG_LABEL[st] || st)}</span>`
    : "&ndash;";
}
function renderNodeLoss(d) {
  LOSSBOX = { ports: d.ports, asset: d.asset };
  const canEdit = can("power.write");
  const isSrc = d.asset.type === "POP" || d.asset.type === "OLT";
  const rows = d.ports
    .map((r, i) => {
      const pw = r.power;
      const prev =
        pw && pw.prev_rx_dbm != null
          ? `${fmtDb(pw.prev_rx_dbm)} <small>(${escapeHtml((pw.prev_at || "").slice(0, 10))})</small>`
          : "&ndash;";
      return `<tr class="${r.used ? "" : "pw-idle"}"><td><b>${escapeHtml(r.port)}</b></td>
      <td class="pw-peer">${r.peer ? escapeHtml(r.peer) : '<span class="tp-muted">kosong</span>'}</td>
      ${powerInputs(i, pw, canEdit, r.port)}
      <td class="num">${isSrc ? "&ndash;" : sigBadge(r.meas_status)}${r.rx_margin_db != null && !isSrc ? ` <small>margin ${fmtDb(r.rx_margin_db, true)}</small>` : ""}</td>
      <td class="num">${prev}</td>
      <td class="num">${r.rx_trend_db != null ? fmtDb(r.rx_trend_db, true) : "&ndash;"} ${r.trend_status ? otdrBadge(r.trend_status) : ""}</td>
      <td><button type="button" class="pw-save" title="Riwayat ukur" onclick="showPowerHistory(${i})"><i class="fa-solid fa-clock-rotate-left"></i></button></td></tr>`;
    })
    .join("");
  return (
    cxStart(
      "power",
      "h4",
      `Daya optik terukur <small>batas Rx ${fmtDb(d.rx_min_dbm)} s.d. ${fmtDb(d.rx_max_dbm)} dBm</small>`,
    ) +
    `<div class="cx-body">
    <div class="imp-scroll"><table class="imp-table pw-table"><thead><tr><th>Port / core</th><th>Terhubung ke</th><th>Tx ukur (dBm)</th><th>Rx ukur (dBm)</th><th></th><th>Status Rx</th><th>Rx sebelumnya</th><th>Tren</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="cov-meta">Status dinilai dari hasil ukur (power meter / DDM perangkat), bukan estimasi, karena tiap perangkat memberi daya berbeda. Tren = Rx terbaru &minus; Rx pengukuran sebelumnya. ${canEdit ? "Isi Tx/Rx lalu simpan; setiap simpan tercatat di riwayat." : ""}</div>
    <div id="pw-history" class="cov-meta"></div></div></div>`
  );
}
function showPowerHistory(i) {
  const r = LOSSBOX.ports[i];
  const box = $id("pw-history");
  if (!r || !box || !LOSSBOX.asset) return;
  box.textContent = "Memuat riwayat...";
  apiRequest(
    `/api/power/history?asset_id=${Number(LOSSBOX.asset.id)}&port_core=${encodeURIComponent(r.port)}`,
  )
    .then((h) => {
      box.innerHTML =
        `<b>Riwayat ${escapeHtml(r.port)}</b>` +
        (h.length
          ? `<table class="imp-table"><thead><tr><th>Waktu</th><th>Tx</th><th>Rx</th><th>Perangkat</th><th>Oleh</th></tr></thead><tbody>${h
              .map(
                (x) =>
                  `<tr><td>${escapeHtml(
                    String(x.at || "")
                      .replace("T", " ")
                      .slice(0, 16),
                  )}</td><td class="num">${fmtDb(x.tx_dbm)}</td><td class="num">${fmtDb(x.rx_dbm)}</td><td>${escapeHtml(x.device || "")}</td><td>${escapeHtml(x.by || "")}</td></tr>`,
              )
              .join("")}</tbody></table>`
          : " belum ada catatan");
    })
    .catch(() => {
      box.textContent = "";
    });
}
function savePower(i) {
  const r = LOSSBOX.ports[i];
  if (!r || !LOSSBOX.asset) return;
  const v = (id) => {
    const s = ($id(id) || {}).value;
    return s === "" || s == null ? null : Number(s);
  };
  apiRequest("/api/power", "PUT", {
    asset_type: "NODE",
    asset_id: LOSSBOX.asset.id,
    port_core: r.port,
    tx_dbm: v(`pw-tx-${i}`),
    rx_dbm: v(`pw-rx-${i}`),
  })
    .then(() => {
      if (currentActiveAsset) loadAssetLoss(currentActiveAsset);
    })
    .catch((err) => alert("Gagal menyimpan daya optik: " + err.message));
}
function renderCableLoss(d) {
  const cores = d.cores || [];
  const meas = cores.filter((c) => c.measured).length;
  const endTxt = (e) => (e ? escapeHtml(e.name) : "?");
  const side = (s, key) =>
    s
      ? `${assetLinkHtml(s.type, s.id, s.name)} <small>[${escapeHtml(s.port || "-")}]</small> <b>${s[key] != null ? fmtDb(s[key]) + " dBm" : "&ndash;"}</b>`
      : "&ndash;";
  const rows = cores
    .map((c) => {
      const m = c.measured;
      return `<tr class="${c.used ? "" : "pw-idle"}"><td>${escapeHtml(c.core)}</td>
      <td class="pw-peer">${side(c.up, "tx_dbm")}</td><td class="pw-peer">${side(c.down, "rx_dbm")}</td>
      <td class="num">${c.pw_loss_db != null ? fmtDb(c.pw_loss_db) : "&ndash;"}</td>
      <td>${sigBadge(c.pw_status)}</td>
      <td class="num">${m ? fmtDb(m.loss_db) : "&ndash;"}</td>
      <td>${m ? otdrBadge(m.status) : "&ndash;"}</td>
      <td class="otdr-date">${m ? escapeHtml(m.date || "") + (m.wavelength_nm ? " &middot; " + Number(m.wavelength_nm) + " nm" : "") : "belum diukur"}</td></tr>`;
    })
    .join("");
  return (
    cxStart(
      "cableloss",
      "h4",
      `Redaman kabel <small>${Number(d.length_km).toFixed(2)} km &middot; ${endTxt(d.ends && d.ends.A)} &harr; ${endTxt(d.ends && d.ends.B)} &middot; ${meas}/${cores.length} core diukur OTDR</small>`,
    ) +
    `<div class="cx-body">
    <div class="imp-scroll"><table class="imp-table pw-table"><thead><tr><th>Core</th><th>Hulu (Tx)</th><th>Hilir (Rx)</th><th>Redaman daya (Tx&minus;Rx)</th><th>Status Rx</th><th>Ukur OTDR (dB)</th><th>Status OTDR</th><th>Tanggal ukur</th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="cov-meta">Redaman daya = Tx di aset hulu &minus; Rx di aset hilir pada sambungan core ini (isi Tx/Rx di Detail Core aset POP/Closure/ODP di ujungnya). Status memakai hasil ukur.</div></div></div>`
  );
}

// --- FUNCTION: RENDER VISUAL SPLITTER ODP (IN / OUT) ---
function renderODPSplitter(asset) {
  const container = document.getElementById("odp-visual-container");
  if (!container) return;

  const coreContainer = document.getElementById("core-grid-container");
  if (coreContainer) coreContainer.style.display = "none";
  container.style.display = "block";

  if (!asset) {
    container.innerHTML =
      "<p style='color: #ef4444;'>Data aset tidak ditemukan.</p>";
    return;
  }

  const capacityStr = asset.capacity || "1 In - 8 Out";
  const { inCount, outCount } = parseOdpCapacity(capacityStr);
  const connectedPorts = getConnectedLocalPorts(asset);

  const portHtml = (portName, accent, bg, shadow) => {
    const isConnected = connectedPorts.has(portName);
    return `
      <div style="
        border: ${isConnected ? `2px solid ${accent.main}` : "1px dashed #cbd5e1"};
        background: ${isConnected ? bg : "#ffffff"};
        border-radius: 8px;
        padding: 10px;
        width: 75px;
        text-align: center;
        box-shadow: ${isConnected ? shadow : "none"};
      ">
        <div style="font-weight: 700; font-size: 11px; color: ${isConnected ? accent.dark : "#64748b"};">${portName}</div>
        <i class="fa-solid ${isConnected ? "fa-plug-circle-check" : "fa-plug"}"
           style="font-size: 18px; margin: 6px 0; color: ${isConnected ? accent.main : "#94a3b8"};"></i>
        <div style="font-size: 10px; font-weight: 600; color: ${isConnected ? accent.dark : "#94a3b8"};">
          ${isConnected ? "Connected" : "Idle"}
        </div>
      </div>
    `;
  };

  const green = { main: "#16a34a", dark: "#15803d" };
  const blue = { main: "#2563eb", dark: "#1d4ed8" };

  let inHtml = "";
  for (let i = 1; i <= inCount; i++) {
    inHtml += portHtml(
      `IN-${i}`,
      green,
      "#f0fdf4",
      "0 2px 4px rgba(22, 163, 74, 0.1)",
    );
  }
  let outHtml = "";
  for (let o = 1; o <= outCount; o++) {
    outHtml += portHtml(
      `OUT-${o}`,
      blue,
      "#eff6ff",
      "0 2px 4px rgba(37, 99, 235, 0.1)",
    );
  }

  container.innerHTML =
    cxStart(
      "odpvis",
      "h4",
      `Konfigurasi Splitter ODP (${escapeHtml(capacityStr)})`,
      "font-size: 13px; color: #1e293b; margin-bottom: 12px; font-weight: 600;",
    ) +
    `<div class="cx-body">

    <!-- Input Section -->
    <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 12px; margin-bottom: 12px;">
      <div style="font-size: 12px; font-weight: 700; color: #15803d; margin-bottom: 8px;">
        <i class="fa-solid fa-right-to-bracket"></i> Input Ports (${inCount} IN)
      </div>
      <div style="display: flex; gap: 10px; flex-wrap: wrap;">
        ${inHtml}
      </div>
    </div>

    <!-- Output Section -->
    <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 12px;">
      <div style="font-size: 12px; font-weight: 700; color: #1d4ed8; margin-bottom: 8px;">
        <i class="fa-solid fa-sitemap"></i> Output Ports (${outCount} OUT)
      </div>
      <div style="display: flex; gap: 10px; flex-wrap: wrap;">
        ${outHtml}
      </div>
    </div>
  </div></div>`;
}

// --- RENDER PORT GRID NODE (PELANGGAN / SLACK / dll) ---
function renderNodePorts(node, container) {
  const totalPorts = getPortCount(node);

  if (totalPorts === 0) {
    container.innerHTML = `<p style="font-size: 13px; color: #64748b;">Aset bertipe ${escapeHtml(node.type)} tidak memiliki port/core untuk disambung.</p>`;
    return;
  }

  const connectedPorts = getConnectedLocalPorts(node);

  let html =
    cxStart(
      "portgrid",
      "h4",
      `Grid Port ${escapeHtml(node.type)} (${totalPorts} Port)`,
      "margin-bottom: 12px; font-size: 14px; color: #334155;",
    ) + `<div class="cx-body">`;
  html += `<div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(90px, 1fr)); gap: 10px;">`;

  for (let p = 1; p <= totalPorts; p++) {
    const tubeNo = Math.floor((p - 1) / 12) + 1;
    const isConnected =
      connectedPorts.has(`Tube ${tubeNo} - Core ${p}`) ||
      connectedPorts.has(`Port ${p}`);

    html += `
      <div style="border: 2px solid ${isConnected ? "#2563eb" : "#cbd5e1"}; border-radius: 6px; padding: 10px; text-align: center; background: ${isConnected ? "#eff6ff" : "#ffffff"};">
        <div style="font-size: 11px; font-weight: bold; color: #475569; margin-bottom: 4px;">Port ${p}</div>
        <i class="fa-solid ${isConnected ? "fa-plug-circle-check" : "fa-plug"}" style="font-size: 18px; color: ${isConnected ? "#2563eb" : "#94a3b8"};"></i>
        <div style="font-size: 10px; margin-top: 6px; color: ${isConnected ? "#1d4ed8" : "#64748b"};">${isConnected ? "Connected" : "Idle"}</div>
      </div>
    `;
  }

  html += `</div></div></div>`; // grid + cx-body + cx-sec "portgrid"
  container.innerHTML = html;
}

// --- FUNGSI ACTION INCIDENT ---
// --- FORM INSIDEN: deteksi kabel/node terdekat dari titik yang diklik ---
const CABLE_INCIDENT_TYPES = ["FO Cut", "Cable Sagging", "Fiber Degradation"];
let incidentLocate = null; // hasil /api/incidents/locate untuk titik yang sedang diisi

function applyIncidentLocateSuggestion() {
  const linked = document.getElementById("inc-linked-asset");
  const hint = document.getElementById("inc-locate-hint");
  if (!linked) return;
  const type = (document.getElementById("inc-type") || {}).value || "FO Cut";
  const wantCable = CABLE_INCIDENT_TYPES.includes(type);
  const loc = incidentLocate || {};
  let value = "",
    text = "";
  if (incidentPreset && wantCable) {
    value = `CABLE:${incidentPreset.cableId}`;
    text = `Kabel ${incidentPreset.name} dipilih dari tombol Report Cable Cut. Pilih core yang bermasalah, atau biarkan kosong bila seluruh kabel putus.`;
  } else if (wantCable && loc.cable) {
    value = `CABLE:${Number(loc.cable.id)}`;
    text = `Terdeteksi di kabel ${loc.cable.name}, ${loc.cable.offset_m} m dari jalur, posisi ±${Math.round(loc.cable.position_m)} m dari ujung awal. Titik akan ditempelkan ke jalur.`;
  } else if (!wantCable && loc.node) {
    value = `NODE:${Number(loc.node.id)}`;
    text = `Terdeteksi di dekat ${loc.node.name} (${loc.node.distance_m} m).`;
  } else if (wantCable) {
    text =
      "Tidak ada kabel dalam radius 100 m dari titik ini. Pilih kabel secara manual bila perlu.";
  }
  linked.value = value;
  if (hint) hint.textContent = text;
  onIncidentLinkedChange();
}

function onIncidentTypeChange() {
  applyIncidentLocateSuggestion();
}

// --- Pemilih core terdampak (daftar centang + pratinjau dampak) ---
const INCCORE = { cableId: null, cores: [], seq: 0, prevSeq: 0 };

function incSelectedCores() {
  const el = document.getElementById("inc-cores");
  const raw = el ? el.value : "";
  return raw
    .split(/[,\s]+/)
    .map((x) => parseInt(x, 10))
    .filter((n) => !Number.isNaN(n));
}

function renderIncCoreList() {
  const box = document.getElementById("inc-core-list");
  if (!box) return;
  const sel = new Set(incSelectedCores());
  if (!INCCORE.cores.length) {
    box.innerHTML = '<div class="inc-core-empty">Memuat core...</div>';
    return;
  }
  const dest = (c) => {
    if (!c.used) return "belum terpakai";
    const parts = [];
    if (c.odps && c.odps.length) parts.push(c.odps.join(", "));
    else if (c.ends && c.ends.length) parts.push(c.ends.join(", "));
    if (c.customers) parts.push(`${c.customers} pelanggan`);
    return parts.length ? parts.join(" · ") : "tersambung";
  };
  box.innerHTML = INCCORE.cores
    .map((c) => {
      const n = Number(c.number);
      return (
        `<label class="inc-core-item${c.used ? "" : " unused"}${c.down ? " has-fault" : ""}">` +
        `<input type="checkbox" value="${n}"${sel.has(n) ? " checked" : ""}${c.used ? "" : " disabled"} onchange="onIncCoreToggle()">` +
        `<span class="inc-core-no">Core ${n}</span>` +
        `<span class="inc-core-dest">&rarr; ${escapeHtml(dest(c))}</span>` +
        (c.down
          ? `<span class="inc-core-badge" title="Tiket aktif">down ${escapeHtml((c.tickets || []).join(", "))}</span>`
          : "") +
        `</label>`
      );
    })
    .join("");
}

function onIncCoreToggle() {
  const box = document.getElementById("inc-core-list");
  const el = document.getElementById("inc-cores");
  if (!box || !el) return;
  const nums = Array.from(box.querySelectorAll("input[type=checkbox]"))
    .filter((x) => x.checked)
    .map((x) => Number(x.value));
  el.value = nums.join(", ");
  updateIncidentPreview();
}

function incCoreSelectAll(on) {
  const box = document.getElementById("inc-core-list");
  if (!box) return;
  box.querySelectorAll("input[type=checkbox]").forEach((x) => {
    if (!x.disabled) x.checked = !!on;
  });
  onIncCoreToggle();
}

function updateIncidentPreview() {
  const out = document.getElementById("inc-preview");
  const linked = document.getElementById("inc-linked-asset");
  if (!out || !linked) return;
  const [t, idRaw] = (linked.value || "").split(":");
  const id = intOrNull(idRaw);
  if (!t || id == null) {
    out.style.display = "none";
    out.innerHTML = "";
    return;
  }
  const payload =
    t === "CABLE" ? { linked_cable_id: id } : { linked_node_id: id };
  const cores = incSelectedCores();
  if (t === "CABLE" && cores.length) payload.affected_cores = cores;
  const my = ++INCCORE.prevSeq;
  out.style.display = "block";
  out.className = "inc-preview";
  out.innerHTML = "Menghitung dampak...";
  apiRequest("/api/incidents/preview-impact", "POST", payload)
    .then((r) => {
      if (my !== INCCORE.prevSeq) return;
      const c = r.counts || {};
      const scope =
        r.scope === "core"
          ? `Core ${(cores || []).join(", ")} saja`
          : r.scope === "kabel"
            ? "SELURUH kabel (semua core)"
            : r.scope === "node"
              ? "Node ini dan semua di hilirnya"
              : "";
      const odp = (r.odp_names || []).length
        ? `<div>ODP terdampak: <b>${r.odp_names.map(escapeHtml).join(", ")}</b></div>`
        : "";
      out.className =
        "inc-preview" + (r.scope === "core" ? " is-core" : " is-wide");
      out.innerHTML =
        `<div class="inc-preview-title">Pratinjau dampak &mdash; ${escapeHtml(scope)}</div>` +
        `<div><b>${Number(c.nodes || 0)}</b> node, <b>${Number(c.customers || 0)}</b> pelanggan, <b>${Number(c.odp || 0)}</b> ODP, <b>${Number(c.cables || 0)}</b> kabel akan ditandai Cut/Broken.</div>` +
        odp;
    })
    .catch((err) => {
      if (my !== INCCORE.prevSeq) return;
      out.className = "inc-preview is-err";
      out.textContent =
        "Pratinjau gagal: " + (err && err.message ? err.message : err);
    });
}

function onIncidentLinkedChange() {
  const linked = document.getElementById("inc-linked-asset");
  const wrap = document.getElementById("inc-cores-wrap");
  const isCable = !!(linked && linked.value.startsWith("CABLE:"));
  if (wrap) wrap.style.display = isCable ? "block" : "none";
  const el = document.getElementById("inc-cores");
  const cableId = isCable ? intOrNull(linked.value.split(":")[1]) : null;
  if (cableId !== INCCORE.cableId) {
    if (el) el.value = "";
    INCCORE.cableId = cableId;
    INCCORE.cores = [];
    renderIncCoreList();
    if (cableId != null) {
      const my = ++INCCORE.seq;
      apiRequest(`/api/cables/${cableId}/core-routes`)
        .then((r) => {
          if (my !== INCCORE.seq) return;
          INCCORE.cores = r.cores || [];
          const sum = document.getElementById("inc-core-summary");
          if (sum) sum.textContent = `${r.used} dari ${r.total} core terpakai`;
          renderIncCoreList();
        })
        .catch((err) => {
          const box = document.getElementById("inc-core-list");
          if (box && my === INCCORE.seq)
            box.innerHTML = `<div class="inc-core-empty">Gagal memuat core: ${escapeHtml((err && err.message) || "")}</div>`;
        });
    }
  }
  updateIncidentPreview();
}

function openAddIncidentModal(lat, lng, preset) {
  incidentPreset = preset || null;
  document.getElementById("modal-add-incident").style.display = "flex";
  document.getElementById("inc-lat").value = lat;
  document.getElementById("inc-lng").value = lng;
  document.getElementById("inc-ticket").value =
    "INC-" + Date.now().toString().slice(-6);
  document.getElementById("inc-title").value = "";
  document.getElementById("inc-description").value = "";
  const coresEl = document.getElementById("inc-cores");
  if (coresEl) coresEl.value = "";
  INCCORE.cableId = null;
  INCCORE.cores = [];
  const repEl = document.getElementById("inc-reporter");
  if (repEl) repEl.value = "";
  const hintEl = document.getElementById("inc-locate-hint");
  if (hintEl) hintEl.textContent = "Mendeteksi kabel/node terdekat...";
  incidentLocate = null;

  // Dropdown aset terdampak: dasar analisis dampak downstream
  const linked = document.getElementById("inc-linked-asset");
  if (linked) {
    const opt = (x) =>
      `<option value="${x.category}:${Number(x.id)}">${escapeHtml(x.name)} (${escapeHtml(x.type)})</option>`;
    const cables = allInventoryData
      .filter((x) => x.category === "CABLE")
      .map(opt)
      .join("");
    const nodes = allInventoryData
      .filter((x) => x.category === "NODE")
      .map(opt)
      .join("");
    linked.innerHTML =
      `<option value="">-- Tanpa aset (hanya titik lokasi) --</option>` +
      (cables ? `<optgroup label="Kabel">${cables}</optgroup>` : "") +
      (nodes ? `<optgroup label="Node / Device">${nodes}</optgroup>` : "");
  }

  // Cari kabel/node terdekat dari titik yang diklik, lalu pilihkan otomatis
  apiRequest("/api/incidents/locate", "POST", {
    latitude: Number(lat),
    longitude: Number(lng),
  })
    .then((loc) => {
      incidentLocate = loc;
      applyIncidentLocateSuggestion();
    })
    .catch((err) => {
      console.error("Gagal mendeteksi lokasi insiden:", err);
      if (hintEl) hintEl.textContent = "";
    });
}

function closeAddIncidentModal() {
  incidentPreset = null;
  document.getElementById("modal-add-incident").style.display = "none";
}

function saveIncidentData(e) {
  e.preventDefault();

  const latVal = parseFloat(document.getElementById("inc-lat").value);
  const lngVal = parseFloat(document.getElementById("inc-lng").value);

  if (isNaN(latVal) || isNaN(lngVal)) {
    return alert("Koordinat Latitude dan Longitude harus diisi dengan angka!");
  }

  const read = (id, fallback) => {
    const el = document.getElementById(id);
    return el && el.value !== "" ? el.value : fallback;
  };

  // "CABLE:3" / "NODE:5" -> linked_cable_id / linked_node_id
  const [linkedType, linkedId] = read("inc-linked-asset", "").split(":");
  const linkedNum = intOrNull(linkedId);

  const payload = {
    ticket_number: read("inc-ticket", "INC-" + Date.now()),
    title: read("inc-title", "Gangguan Fiber Optic"),
    severity: read("inc-severity", "Critical"),
    incident_type: read("inc-type", "FO Cut"),
    status: read("inc-status", "Open"),
    description: read("inc-description", ""),
    latitude: latVal,
    longitude: lngVal,
    linked_cable_id: linkedType === "CABLE" ? linkedNum : null,
    linked_node_id: linkedType === "NODE" ? linkedNum : null,
    reporter: read("inc-reporter", "") || null,
  };

  // Core terdampak (opsional): "3, 5, 7" -> [3, 5, 7]
  const coresRaw = read("inc-cores", "");
  if (coresRaw && payload.linked_cable_id) {
    const cores = coresRaw
      .split(/[,\s]+/)
      .map((x) => parseInt(x, 10))
      .filter((n) => !Number.isNaN(n));
    if (cores.length) payload.affected_cores = cores;
  }

  // Server langsung menghitung dampak & mencatat riwayat saat tiket dibuat
  apiRequest("/api/incidents", "POST", payload)
    .then((res) => {
      const c = (res.impact && res.impact.counts) || {};
      const lines = [`Tiket ${res.ticket_number} dicatat.`];
      if (
        res.location &&
        res.location.cable &&
        res.location.cable.upstream.distance_m != null
      ) {
        const cab = res.location.cable;
        lines.push(
          `Lokasi: kabel ${cab.name}, ${Math.round(cab.upstream.distance_m)} m dari ${cab.upstream.name || "ujung awal"}` +
            (cab.downstream.distance_m != null
              ? ` / ${Math.round(cab.downstream.distance_m)} m dari ${cab.downstream.name || "ujung akhir"}.`
              : "."),
        );
      }
      if (c.nodes || c.cables) {
        lines.push(
          `Dampak: ${c.nodes || 0} node (${c.customers || 0} pelanggan), ${c.cables || 0} kabel ditandai Cut/Broken.`,
        );
        const names = impactNamesText(res.impact);
        if (names) lines.push(names);
      }
      alert(lines.join("\n"));
      closeAddIncidentModal();
      loadData();
      openIncidentDetail(res.id);
    })
    .catch((err) => {
      console.error("Gagal mencatat insiden:", err);
      alert("Gagal mencatat insiden: " + err.message);
    });
}

// Selesaikan Incident
function resolveIncident(incidentId) {
  if (
    !confirm("Selesaikan tiket incident ini? Aset terdampak akan dipulihkan.")
  )
    return;

  apiRequest(`/api/incidents/${incidentId}/status`, "PUT", {
    status: "Resolved",
  })
    .then(() => {
      alert("Incident selesai dan status aset telah dipulihkan!");
      loadData();
    })
    .catch((err) => alert("Gagal menyelesaikan incident: " + err.message));
}

// Hapus Incident
function deleteIncidentRecord(incidentId) {
  if (!confirm("Hapus incident ini? Aset terdampak akan dipulihkan.")) return;

  apiRequest(`/api/incidents/${incidentId}`, "DELETE")
    .then(() => {
      alert("Incident dihapus dan status aset telah dipulihkan!");
      loadData();
    })
    .catch((err) => alert("Gagal menghapus incident: " + err.message));
}

// --- DETAIL INSIDEN: lokasi, dampak, perbaikan lapangan & riwayat ---
let currentIncidentId = null;
const INCIDENT_STATUS_COLOR = {
  Open: "#ef4444",
  "In Progress": "#f59e0b",
  "Temporary Fix": "#d97706",
  Resolved: "#16a34a",
};

function openIncidentDetail(id) {
  currentIncidentId = Number(id);
  document.getElementById("modal-incident-detail").style.display = "flex";
  document.getElementById("incd-summary").innerHTML = "Memuat...";
  loadIncidentDetail();
}

function closeIncidentDetail() {
  document.getElementById("modal-incident-detail").style.display = "none";
  currentIncidentId = null;
}

function loadIncidentDetail() {
  const id = currentIncidentId;
  if (id == null) return Promise.resolve();
  return apiRequest(`/api/incidents/${id}`)
    .then((data) => {
      if (currentIncidentId !== id) return; // modal sudah pindah tiket
      renderIncidentDetail(data);
    })
    .catch((err) => {
      console.error("Gagal memuat detail insiden:", err);
      document.getElementById("incd-summary").innerHTML =
        `<span style="color:#ef4444;">Gagal memuat detail: ${escapeHtml(err.message)}</span>`;
    });
}

function incChip(text, bg, fg) {
  return `<span style="background:${bg};color:${fg};padding:3px 8px;border-radius:4px;font-weight:600;font-size:12px;">${text}</span>`;
}

function renderIncidentDetail(d) {
  const inc = d.incident;
  const color = INCIDENT_STATUS_COLOR[inc.status] || "#64748b";
  const resolved = inc.status === "Resolved";

  document.getElementById("incd-title").innerHTML =
    `<i class="fa-solid fa-triangle-exclamation"></i> [${escapeHtml(inc.ticket_number)}] ${escapeHtml(inc.title)}`;

  document.getElementById("incd-summary").innerHTML =
    `<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;">` +
    `<span style="background:${color};color:#fff;padding:3px 10px;border-radius:12px;font-weight:600;">${escapeHtml(inc.status)}</span>` +
    incChip(escapeHtml(inc.severity), "#fee2e2", "#b91c1c") +
    incChip(escapeHtml(inc.incident_type), "#e0f2fe", "#0369a1") +
    (inc.source === "AUTO"
      ? incChip("Tiket otomatis", "#ede9fe", "#6d28d9")
      : "") +
    `<span style="color:#64748b;">Lapor: ${timeHtml(inc.reported_at)}` +
    (inc.resolved_at ? ` &middot; Selesai: ${timeHtml(inc.resolved_at)}` : "") +
    (inc.reporter ? ` &middot; Pelapor: ${escapeHtml(inc.reporter)}` : "") +
    `</span></div>` +
    (inc.description
      ? `<div style="margin-top:6px;color:#475569;">${escapeHtml(inc.description)}</div>`
      : "") +
    (inc.resolution
      ? `<div style="margin-top:6px;color:#15803d;"><b>Penyelesaian:</b> ${escapeHtml(inc.resolution)}</div>`
      : "");

  // Lokasi
  const loc = d.location || {};
  let locHtml = "";
  if (loc.cable) {
    const c = loc.cable;
    locHtml += `<div><b>Kabel:</b> ${escapeHtml(c.name)} (${escapeHtml(c.type || "-")})</div>`;
    if (c.position_m != null) {
      locHtml +=
        `<div><i class="fa-solid fa-arrow-up"></i> ${Math.round(c.upstream.distance_m)} m dari <b>${escapeHtml(c.upstream.name || "ujung awal")}</b> (hulu)</div>` +
        `<div><i class="fa-solid fa-arrow-down"></i> ${Math.round(c.downstream.distance_m)} m dari <b>${escapeHtml(c.downstream.name || "ujung akhir")}</b> (hilir)</div>` +
        `<div style="color:#94a3b8;font-size:11px;">Panjang kabel ±${Math.round(c.length_m)} m — acuan jarak OTDR</div>`;
    }
  }
  if (loc.node) {
    locHtml += `<div><b>Node:</b> ${escapeHtml(loc.node.name)} (${escapeHtml(loc.node.type || "-")})</div>`;
  }
  if (!locHtml)
    locHtml = `<span style="color:#94a3b8;">Tidak terkait kabel/node tertentu (hanya titik lokasi).</span>`;
  locHtml += `<div style="color:#94a3b8;font-size:11px;">${Number(inc.latitude).toFixed(6)}, ${Number(inc.longitude).toFixed(6)}</div>`;
  if (inc.affected_cores) {
    try {
      locHtml += `<div><b>Core terdampak:</b> ${JSON.parse(inc.affected_cores).map(escapeHtml).join(", ")}</div>`;
    } catch (_) {
      /* abaikan */
    }
  }
  document.getElementById("incd-location").innerHTML = locHtml;

  // Dampak
  const im = d.impact || { counts: {}, customers: [], nodes: [] };
  const cn = im.counts || {};
  let impHtml =
    `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px;">` +
    incChip(`${Number(cn.nodes || 0)} node`, "#e0f2fe", "#0369a1") +
    incChip(`${Number(cn.cables || 0)} kabel`, "#fef3c7", "#b45309") +
    incChip(`${Number(cn.odp || 0)} ODP`, "#dcfce7", "#15803d") +
    incChip(`${Number(cn.customers || 0)} pelanggan`, "#fee2e2", "#b91c1c") +
    `</div>` +
    `<div style="font-size:11px;color:${im.live ? "#b91c1c" : "#15803d"};margin-bottom:4px;">` +
    (im.live
      ? "Dampak AKTIF (aset ditandai Cut/Broken)"
      : "Dampak terakhir — layanan sudah dipulihkan") +
    `</div>`;
  const groups = groupImpactNodes(im);
  // data lama (tanpa daftar node) hanya punya nama pelanggan
  if (!groups.length && (im.customers || []).length)
    groups.push({ label: "Pelanggan", names: im.customers });
  if (groups.length) {
    impHtml +=
      `<div style="font-size:11px;color:#64748b;margin-bottom:4px;">` +
      (inc.affected_cores && loc.cable
        ? `Hanya aset yang disuplai core terdampak pada kabel ${escapeHtml(loc.cable.name)} (core lain tetap normal).`
        : loc.cable
          ? `Semua aset di bawah berada di hilir titik gangguan pada kabel ${escapeHtml(loc.cable.name)}.`
          : "Aset yang ikut terdampak:") +
      `</div>`;
    groups.forEach((g) => {
      const shown = g.names.slice(0, 40);
      impHtml +=
        `<div style="margin:3px 0;"><b>${escapeHtml(g.label)} (${g.names.length}):</b> ` +
        shown
          .map(
            (n) =>
              `<span style="display:inline-block;background:#f1f5f9;border-radius:4px;padding:1px 6px;margin:1px 2px 1px 0;">${escapeHtml(n)}</span>`,
          )
          .join("") +
        (g.names.length > shown.length
          ? ` +${g.names.length - shown.length} lagi`
          : "") +
        `</div>`;
    });
  } else {
    impHtml += `<div style="color:#94a3b8;">Tidak ada aset lain yang terdampak.</div>`;
  }
  document.getElementById("incd-impact").innerHTML = impHtml;

  // Perbaikan
  const KIND = {
    TEMPORARY: ["Sementara", "#fef3c7", "#b45309"],
    PERMANENT: ["Permanen", "#dcfce7", "#15803d"],
  };
  const ACT = {
    ADD_CLOSURE: "Closure baru",
    EXTRA_JOINT: "Joint tambahan",
    OTHER: "Lainnya",
  };
  const reps = d.repairs || [];
  document.getElementById("incd-repairs").innerHTML = reps.length
    ? reps
        .map((r) => {
          const k = KIND[r.kind] || [r.kind, "#e2e8f0", "#334155"];
          return (
            `<div style="padding:5px 0;border-bottom:1px solid #f1f5f9;">` +
            `${incChip(escapeHtml(k[0]), k[1], k[2])} <b>${escapeHtml(ACT[r.action] || r.action)}</b>` +
            (r.position_m != null
              ? ` &middot; ${Math.round(r.position_m)} m dari ujung awal`
              : "") +
            (r.technician ? ` &middot; ${escapeHtml(r.technician)}` : "") +
            ` <span style="color:#94a3b8;font-size:11px;">${timeHtml(r.created_at)}</span>` +
            (r.undone_at
              ? ` ${incChip("DIBATALKAN", "#fee2e2", "#b91c1c")}`
              : "") +
            (r.notes
              ? `<div style="color:#64748b;">${escapeHtml(r.notes)}</div>`
              : "") +
            (r.undone_at
              ? `<div style="color:#b91c1c;font-size:11px;">Dibatalkan ${timeHtml(r.undone_at)}${r.undone_by ? " oleh " + escapeHtml(r.undone_by) : ""}${r.undo_reason ? " &middot; " + escapeHtml(r.undo_reason) : ""}</div>`
              : "") +
            (can("repair.undo") && !r.undone_at
              ? r.can_undo
                ? `<div><button class="adm-btn bad" onclick="undoRepair(${Number(r.id)})"><i class="fa-solid fa-rotate-left"></i> Batalkan perbaikan</button></div>`
                : `<div style="color:#94a3b8;font-size:11px;"><i class="fa-solid fa-circle-info"></i> Tidak bisa dibatalkan: ${escapeHtml(r.undo_reason || "-")}</div>`
              : "") +
            `</div>`
          );
        })
        .join("")
    : `<span style="color:#94a3b8;">Belum ada perbaikan tercatat.</span>`;

  const form = document.getElementById("incd-repair-form");
  form.style.display = resolved || !can("incident.write") ? "none" : "block";
  const noteRow = document.getElementById("incd-note-row");
  if (noteRow) noteRow.style.display = can("incident.write") ? "flex" : "none";
  const hasCable = !!loc.cable;
  ["ADD_CLOSURE", "EXTRA_JOINT"].forEach((v) => {
    const opt = [...document.getElementById("rep-action").options].find(
      (o) => o.value === v,
    );
    if (opt) opt.disabled = !hasCable;
  });
  if (!hasCable) document.getElementById("rep-action").value = "OTHER";

  // Riwayat
  const ICON = {
    CREATED: "fa-flag",
    IMPACT_APPLIED: "fa-bolt",
    IMPACT_RELEASED: "fa-plug-circle-check",
    STATUS_CHANGED: "fa-arrows-rotate",
    REPAIR: "fa-screwdriver-wrench",
    REPAIR_UNDONE: "fa-rotate-left",
    NOTE: "fa-note-sticky",
    DELETED: "fa-trash",
  };
  document.getElementById("incd-timeline").innerHTML =
    (d.events || [])
      .map(
        (e) =>
          `<div style="display:flex;gap:8px;padding:5px 0;border-bottom:1px solid #f1f5f9;">` +
          `<i class="fa-solid ${ICON[e.event_type] || "fa-circle"}" style="color:#64748b;width:16px;margin-top:2px;"></i>` +
          `<div><div>${escapeHtml(e.message || e.event_type)}</div>` +
          `<div style="color:#94a3b8;font-size:11px;">${timeHtml(e.created_at)} &middot; ${escapeHtml(e.actor || "system")}</div></div></div>`,
      )
      .join("") || `<span style="color:#94a3b8;">Belum ada riwayat.</span>`;

  // Aksi status
  const btn = (label, status, bg) =>
    `<button onclick="changeIncidentStatus('${status}')" style="background:${bg};color:#fff;border:none;padding:6px 12px;border-radius:4px;cursor:pointer;">${label}</button>`;
  document.getElementById("incd-status-actions").innerHTML = !can(
    "incident.write",
  )
    ? ""
    : resolved
      ? btn("Buka Kembali Tiket", "Open", "#f59e0b")
      : (inc.status === "Open"
          ? btn("Tandai In Progress", "In Progress", "#f59e0b")
          : "") + btn("Selesaikan Tiket", "Resolved", "#16a34a");
}

function submitIncidentRepair() {
  const id = currentIncidentId;
  if (id == null) return;
  const val = (x) => (document.getElementById(x) || { value: "" }).value.trim();
  const payload = {
    kind: val("rep-kind"),
    action: val("rep-action"),
    name: val("rep-name") || null,
    technician: val("rep-tech") || null,
    notes: val("rep-notes"),
    splice_all: !!(document.getElementById("rep-full") || {}).checked,
  };
  const changesMap =
    payload.action === "ADD_CLOSURE" || payload.action === "EXTRA_JOINT";
  const msg = changesMap
    ? "Perbaikan ini akan menyisipkan closure/joint di titik gangguan dan MEMECAH kabel menjadi dua segmen (peta, topologi, dan sambungan core diperbarui)" +
      (payload.splice_all
        ? ", dengan SAMBUNG PENUH: semua core kabel disambung di closure"
        : "") +
      ". Lanjutkan?"
    : "Catat perbaikan ini?";
  if (!confirm(msg)) return;

  apiRequest(`/api/incidents/${id}/repairs`, "POST", payload)
    .then((res) => {
      alert(
        `Perbaikan dicatat. Status tiket: ${res.status}.` +
          (res.node_id
            ? `\nTitik sambung ${res.reused_existing_node ? "(memakai closure yang sudah ada)" : "baru dibuat"} di peta.`
            : "") +
          (res.cores_full_added
            ? `\nSambung penuh: ${Number(res.cores_spliced || 0) + Number(res.cores_full_added)} dari ${Number(res.cores_total)} core disambung di closure.`
            : "") +
          (res.cores_skipped
            ? `\nPerhatian: ${res.cores_skipped} sambungan lama tanpa info core tidak ikut dipecah.`
            : ""),
      );
      ["rep-name", "rep-notes"].forEach((x) => {
        const el = document.getElementById(x);
        if (el) el.value = "";
      });
      loadData(); // closure baru & kabel terpecah langsung tampil di peta
      return loadIncidentDetail();
    })
    .catch((err) => alert("Gagal mencatat perbaikan: " + err.message));
}

function submitIncidentNote() {
  const id = currentIncidentId;
  const el = document.getElementById("incd-note");
  if (id == null || !el || !el.value.trim()) return;
  const actor =
    (document.getElementById("rep-tech") || { value: "" }).value.trim() || null;
  apiRequest(`/api/incidents/${id}/events`, "POST", {
    message: el.value.trim(),
    actor,
  })
    .then(() => {
      el.value = "";
      return loadIncidentDetail();
    })
    .catch((err) => alert("Gagal menambah catatan: " + err.message));
}

function changeIncidentStatus(status) {
  const id = currentIncidentId;
  if (id == null) return;
  if (
    status === "Resolved" &&
    !confirm("Selesaikan tiket ini? Aset terdampak akan dipulihkan.")
  )
    return;
  const actor =
    (document.getElementById("rep-tech") || { value: "" }).value.trim() || null;
  apiRequest(`/api/incidents/${id}/status`, "PUT", { status, actor })
    .then(() => {
      loadData();
      return loadIncidentDetail();
    })
    .catch((err) => alert("Gagal mengubah status: " + err.message));
}

// Logika rendering warna jalur kabel berdasarkan status
function getCableStyle(type, status, installation, coreFault) {
  let color = "#0891b2";
  let weight = 3;
  let dashArray = null;

  switch (type) {
    case "Backbone":
      color = "#2638dc";
      weight = 4;
      break;
    case "Feeder":
      color = "#ebeb25";
      weight = 3.5;
      break;
    case "Distribution":
      color = "#0891b2";
      weight = 2.5;
      break;
    case "Drop":
      color = "#d97706";
      weight = 2;
      break;
  }

  // Kabel tanah (duct/tanam langsung) digambar garis-titik; kabel udara garis penuh
  if (installation === "Tanah") dashArray = "10, 5, 2, 5";

  let cls;
  if (status === "Active") cls = "cable-active"; // berkedip hijau pelan
  if (status === "Cut/Broken") {
    color = "#ef4444";
    dashArray = "6, 8"; // garis putus-putus
    cls = "cable-down"; // berkedip merah
  }

  // Gangguan sebagian core (tiket aktif dengan core tertentu): oranye putus-putus; kabel tetap berstatus Active
  if (coreFault && status !== "Cut/Broken") {
    color = "#f97316";
    dashArray = "3, 7";
    weight = Math.max(weight, 3.5);
    cls = "cable-partial";
  }

  return { color, weight, dashArray, opacity: 0.9, className: cls };
}

// --- MODAL NOC MONITOR / LOG INCIDENT ---
function openNocMonitorModal() {
  const modal = document.getElementById("modal-noc-monitor");
  if (modal) {
    modal.style.display = "flex";
    fetchNocIncidentsLog();
  }
}

// Tiket yang sudah diwakili closure hasil perbaikan tidak punya marker sendiri: buka popup closure-nya
function focusIncidentPopup(incidentId, repairNodeId) {
  if (markersMap[`incident:${incidentId}`])
    return openAssetPopup(`incident:${incidentId}`);
  if (repairNodeId != null && markersMap[`node:${repairNodeId}`])
    return openAssetPopup(`node:${repairNodeId}`);
}

function closeNocMonitorModal() {
  const modal = document.getElementById("modal-noc-monitor");
  if (modal) modal.style.display = "none";
}

const NOC = {
  all: [],
  f: {
    cluster: "ALL",
    area: "ALL",
    city: "ALL",
    status: "ALL",
    severity: "ALL",
    q: "",
  },
};
const NOC_NO_CITY = "(tanpa kota)";
function nocRegion(props) {
  return {
    cluster:
      String(props.cluster || "")
        .trim()
        .toUpperCase() || "EKO",
    area:
      String(props.area || "")
        .trim()
        .toUpperCase() || "BANJARMASIN",
    city: String(props.city || "").trim() || NOC_NO_CITY,
  };
}
function fetchNocIncidentsLog() {
  showNocScope();
  apiRequest("/api/incidents" + scopeQS("?"))
    .then((data) => {
      NOC.all = (data.features || []).map((f) => ({
        f,
        r: nocRegion(f.properties || {}),
      }));
      renderNocFilters();
      renderNocTable();
    })
    .catch((err) => console.error("Gagal memuat log incident:", err));
}
function nocUniq(list) {
  return Array.from(new Set(list)).sort((a, b) =>
    String(a).localeCompare(String(b)),
  );
}
function renderNocFilters() {
  const f = NOC.f;
  const fill = (id, vals, cur) => {
    const el = $id(id);
    if (!el) return "ALL";
    const keep = cur !== "ALL" && vals.includes(cur) ? cur : "ALL";
    el.innerHTML =
      `<option value="ALL">Semua</option>` +
      vals
        .map(
          (v) => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`,
        )
        .join("");
    el.value = keep;
    return keep;
  };
  // Cluster -> Area -> Kota: pilihan di bawahnya hanya yang ada pada pilihan di atasnya
  f.cluster = fill(
    "noc-f-cluster",
    nocUniq(NOC.all.map((x) => x.r.cluster)),
    f.cluster,
  );
  const inCl = NOC.all.filter(
    (x) => f.cluster === "ALL" || x.r.cluster === f.cluster,
  );
  f.area = fill("noc-f-area", nocUniq(inCl.map((x) => x.r.area)), f.area);
  const inAr = inCl.filter((x) => f.area === "ALL" || x.r.area === f.area);
  f.city = fill("noc-f-city", nocUniq(inAr.map((x) => x.r.city)), f.city);
  ["status", "severity"].forEach((k) => {
    const el = $id(k === "status" ? "noc-f-status" : "noc-f-sev");
    if (el) el.value = f[k];
  });
  const q = $id("noc-f-q");
  if (q && q.value !== f.q) q.value = f.q;
}
function onNocFilter(which) {
  const idMap = {
    cluster: "noc-f-cluster",
    area: "noc-f-area",
    city: "noc-f-city",
    status: "noc-f-status",
    severity: "noc-f-sev",
    q: "noc-f-q",
  };
  const el = $id(idMap[which]);
  NOC.f[which] = el
    ? which === "q"
      ? String(el.value || "").trim()
      : el.value || "ALL"
    : "ALL";
  if (which === "cluster") {
    NOC.f.area = "ALL";
    NOC.f.city = "ALL";
  }
  if (which === "area") NOC.f.city = "ALL";
  if (which === "cluster" || which === "area") renderNocFilters();
  renderNocTable();
}
function resetNocFilters() {
  NOC.f = {
    cluster: "ALL",
    area: "ALL",
    city: "ALL",
    status: "ALL",
    severity: "ALL",
    q: "",
  };
  renderNocFilters();
  renderNocTable();
}
function nocFiltered() {
  const f = NOC.f,
    q = f.q.toLowerCase();
  return NOC.all.filter(({ f: ft, r }) => {
    const p = ft.properties || {};
    if (f.cluster !== "ALL" && r.cluster !== f.cluster) return false;
    if (f.area !== "ALL" && r.area !== f.area) return false;
    if (f.city !== "ALL" && r.city !== f.city) return false;
    if (f.status !== "ALL" && p.status !== f.status) return false;
    if (f.severity !== "ALL" && p.severity !== f.severity) return false;
    if (
      q &&
      ![p.ticket_number, p.title, p.incident_type, p.description].some((v) =>
        String(v || "")
          .toLowerCase()
          .includes(q),
      )
    )
      return false;
    return true;
  });
}
function renderNocTable() {
  const tbody = document.getElementById("noc-table-body");
  if (!tbody) return;
  tbody.innerHTML = "";
  const rows = nocFiltered();
  const sum = $id("noc-summary");
  if (sum) {
    const cnt = (fn) => rows.filter(fn).length;
    sum.innerHTML = [
      ["Total", rows.length, "all"],
      ["Open", cnt((x) => x.f.properties.status === "Open"), "open"],
      [
        "Temporary Fix",
        cnt((x) => x.f.properties.status === "Temporary Fix"),
        "temp",
      ],
      ["Resolved", cnt((x) => x.f.properties.status === "Resolved"), "done"],
      [
        "Critical aktif",
        cnt(
          (x) =>
            x.f.properties.severity === "Critical" &&
            x.f.properties.status !== "Resolved",
        ),
        "crit",
      ],
    ]
      .map(
        ([l, n, c]) =>
          `<span class="noc-chip c-${c}"><b>${n}</b> ${escapeHtml(l)}</span>`,
      )
      .join("");
  }
  const cc = $id("noc-count");
  if (cc)
    cc.textContent = `Menampilkan ${rows.length} dari ${NOC.all.length} tiket`;
  if (rows.length === 0) {
    tbody.innerHTML = `<tr><td colspan="8" style="text-align: center; padding: 15px; color: #94a3b8;">${NOC.all.length ? "Tidak ada tiket yang cocok dengan filter." : "Belum ada catatan history incident."}</td></tr>`;
    return;
  }
  rows.forEach(({ f, r }) => {
    const props = f.properties;
    const tr = document.createElement("tr");
    tr.style.borderBottom = "1px solid #e2e8f0";
    const isResolved = props.status === "Resolved";
    const badgeColor = isResolved
      ? "#16a34a"
      : props.status === "Temporary Fix"
        ? "#d97706"
        : "#ef4444";
    const lat = Number(f.geometry.coordinates[1]);
    const lng = Number(f.geometry.coordinates[0]);
    const noEffect = !!props.no_effect,
      hiddenOnMap = !!props.map_hidden;
    const visBtn =
      noEffect && can("incident.write")
        ? `<button onclick="setIncidentMapVisibility(${Number(props.id)}, ${hiddenOnMap ? "false" : "true"})" class="noc-row-btn" title="${hiddenOnMap ? "Tampilkan penanda di peta" : "Sembunyikan penanda dari peta"}">
          <i class="fa-solid ${hiddenOnMap ? "fa-eye" : "fa-eye-slash"}"></i> ${hiddenOnMap ? "Tampilkan" : "Sembunyikan"}
        </button>`
        : "";
    const tag = hiddenOnMap
      ? `<br><small class="noc-tag hid"><i class="fa-solid fa-eye-slash"></i> disembunyikan dari peta</small>`
      : noEffect
        ? `<br><small class="noc-tag"><i class="fa-solid fa-circle-info"></i> tanpa dampak aset</small>`
        : "";
    tr.innerHTML = `
      <td style="padding: 10px; font-weight: 600;">${escapeHtml(props.ticket_number)}</td>
      <td style="padding: 10px;">${escapeHtml(props.title)}${tag}</td>
      <td style="padding: 10px;"><small><b>${escapeHtml(r.cluster)}</b> &rsaquo; ${escapeHtml(r.area)}<br>${escapeHtml(r.city)}</small></td>
      <td style="padding: 10px;">${escapeHtml(props.incident_type)}</td>
      <td style="padding: 10px;"><span style="color: white; background: ${props.severity === "Critical" ? "#dc2626" : "#f59e0b"}; padding: 2px 6px; border-radius: 4px; font-size: 11px;">${escapeHtml(props.severity)}</span></td>
      <td style="padding: 10px;"><span style="color: white; background: ${badgeColor}; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 500;">${escapeHtml(props.status)}</span></td>
      <td style="padding: 10px;"><small>${timeHtml(props.reported_at)}</small></td>
      <td style="padding: 10px; text-align: center;">
        ${
          hiddenOnMap
            ? ""
            : `<button onclick="closeNocMonitorModal(); map.flyTo([${lat}, ${lng}], 18, {duration: 1.5}); setTimeout(() => focusIncidentPopup(${Number(props.id)}, ${props.repair_node_id == null ? "null" : Number(props.repair_node_id)}), 1650);"
                style="background: #2563eb; color: white; border: none; padding: 4px 8px; border-radius: 4px; cursor: pointer; font-size: 11px;">
          <i class="fa-solid fa-crosshairs"></i> Sorot
        </button>`
        }
        ${visBtn}
        <button onclick="openIncidentDetail(${Number(props.id)});"
                style="background: #0f766e; color: white; border: none; padding: 4px 8px; border-radius: 4px; cursor: pointer; font-size: 11px; margin-left: 4px;">
          <i class="fa-solid fa-list-check"></i> Detail
        </button>
      </td>
    `;
    tbody.appendChild(tr);
  });
}

// --- PENCARIAN PETA ---
// Satu kotak cari untuk: koordinat, aset NETGIS (instan, dari data lokal), dan nama tempat/alamat
// (lewat /api/geocode, seperti kolom cari peta online). Saran bisa dipilih dengan panah + Enter.
const SEARCH = {
  remote: [],
  local: [],
  coord: null,
  active: -1,
  seq: 0,
  timer: null,
  loading: false,
  error: null,
  query: "",
};
const RECENT_KEY = "netgis_recent_searches";

// Marker pencarian / kartu klik: hilang saat popup ditutup, Esc, atau klik area lain
function clearSearchMarker() {
  const m = searchHighlightMarker;
  searchHighlightMarker = null;
  if (m) {
    try {
      map.removeLayer(m);
    } catch (_) {
      /* sudah lepas */
    }
  }
}
function attachSearchMarker(lat, lng, html) {
  clearSearchMarker();
  const m = L.marker([lat, lng]).addTo(map).bindPopup(html);
  searchHighlightMarker = m;
  m.on("popupclose", () => {
    if (searchHighlightMarker === m) clearSearchMarker();
  });
  m.openPopup();
  return m;
}
const FAR_PLACE_M = 50000; // hasil pencarian > 50 km dari tengah peta dianggap "jauh"
function distFromMapCenterM(lat, lng) {
  try {
    const c = map.getCenter();
    return L.latLng(c.lat, c.lng).distanceTo(L.latLng(lat, lng));
  } catch (_) {
    return 0;
  }
}
function fmtFar(m) {
  return m >= 1000 ? `${Math.round(m / 1000)} km` : `${Math.round(m)} m`;
}

function showSearchHighlight(lat, lng, html, bbox, zoom = 18) {
  clearSearchMarker();
  if (bbox && typeof map.flyToBounds === "function") {
    map.flyToBounds(
      [
        [bbox[0], bbox[1]],
        [bbox[2], bbox[3]],
      ],
      { maxZoom: 18, duration: 1.5 },
    );
  } else {
    map.flyTo([lat, lng], zoom, { duration: 1.5 });
  }
  attachSearchMarker(lat, lng, html);
}

function loadRecentSearches() {
  try {
    const v = JSON.parse(localStorage.getItem(RECENT_KEY) || "[]");
    return Array.isArray(v)
      ? v.filter((x) => x && typeof x.text === "string").slice(0, 6)
      : [];
  } catch (_) {
    return [];
  }
}
function saveRecentSearch(text, lat, lng) {
  try {
    const list = loadRecentSearches().filter(
      (x) => x.text.toLowerCase() !== text.toLowerCase(),
    );
    list.unshift({ text, lat, lng });
    localStorage.setItem(RECENT_KEY, JSON.stringify(list.slice(0, 6)));
  } catch (_) {
    /* penyimpanan tidak tersedia: abaikan */
  }
}
function clearRecentSearches() {
  try {
    localStorage.removeItem(RECENT_KEY);
  } catch (_) {}
  renderSearchDropdown();
}

// Koordinat dari kotak pencarian. Mendukung:
//  desimal           -3.3186, 114.5944 | 3.3186 S, 114.5944 E | -3,3186 114,5944 (koma desimal)
//  derajat-menit     3°19.117'S 114°35.667'E | S 3 19.117 E 114 35.667
//  DMS               3°19'07"S 114°35'40"E | -3 19 07 114 35 40 | S3°19'07" E114°35'40"
// Huruf arah: N/S/E/W atau bahasa Indonesia U/S/T/B (juga LU/LS/BT/BB). Tidak dikenali -> null (jatuh ke pencarian nama).
const COORD_DIR = {
  N: ["lat", 1],
  U: ["lat", 1],
  LU: ["lat", 1],
  S: ["lat", -1],
  LS: ["lat", -1],
  E: ["lng", 1],
  T: ["lng", 1],
  BT: ["lng", 1],
  W: ["lng", -1],
  B: ["lng", -1],
  BB: ["lng", -1],
};
function coordComponent(nums, dir) {
  // nums: [derajat], [derajat, menit] atau [derajat, menit, detik]; tanda minus hanya pada angka pertama
  if (!nums.length || nums.length > 3) return null;
  const neg0 = /^-/.test(nums[0]);
  if (nums.slice(1).some((s) => /^[+-]/.test(s))) return null;
  const v = nums.map((s) => parseFloat(s.replace(/^[+-]/, "")));
  if (v.some((x) => !Number.isFinite(x))) return null;
  if (v.length > 1 && !Number.isInteger(v[0])) return null; // 3.5 19 tidak masuk akal
  if (v.length > 2 && !Number.isInteger(v[1])) return null; // menit desimal hanya bila tanpa detik
  if (v.length > 1 && v[1] >= 60) return null;
  if (v.length > 2 && v[2] >= 60) return null;
  let val = v[0] + (v[1] || 0) / 60 + (v[2] || 0) / 3600;
  let sign = neg0 ? -1 : 1;
  if (dir) sign = dir[1] === -1 ? -1 : neg0 ? -1 : 1; // S/W selalu negatif; N/E mengikuti angka (tanda ganda tidak dibalik)
  return { axis: dir ? dir[0] : null, val: sign * val };
}
function parseCoordinate(query) {
  const q0 = String(query || "").trim();
  if (!q0) return null;
  // jalur cepat desimal biasa (perilaku lama)
  const m = q0.match(/^([-+]?\d+(?:\.\d+)?)\s*[,;\s]\s*([-+]?\d+(?:\.\d+)?)$/);
  if (m) {
    const lat = parseFloat(m[1]),
      lng = parseFloat(m[2]);
    return { lat, lng, valid: Math.abs(lat) <= 90 && Math.abs(lng) <= 180 };
  }
  let s = q0.toUpperCase().replace(/[−–—]/g, "-");
  if (!/\d/.test(s)) return null;
  // pemisah antar-komponen: ';' atau satu koma; dua koma atau lebih = koma desimal
  const commas = (s.match(/,/g) || []).length;
  if (s.includes(";")) s = s.replace(/;/g, " | ").replace(/,/g, ".");
  else if (commas === 1) s = s.replace(",", " | ");
  else if (commas >= 2) s = s.replace(/,/g, ".");
  // simbol derajat/menit/detik -> spasi
  s = s
    .replace(/[°º˚]|DERAJAT|DEG/g, " ")
    .replace(/[′’´‘`']{2}|[″”“"]/g, " ")
    .replace(/[′’´‘`']/g, " ");
  s = s
    .replace(/(\d)([A-Z])/g, "$1 $2")
    .replace(/([A-Z])(\d)/g, "$1 $2")
    .replace(/([A-Z])(?=[-+]\d)/g, "$1 ");
  // sisa karakter harus hanya angka, tanda, titik, spasi, '|', dan token arah utuh
  const tokens =
    s.match(/\||[+-]?\d+(?:\.\d+)?|\.\d+|LU|LS|BT|BB|[NSEWUTB]\b|\S+/g) || [];
  const TOK = tokens.map((t) => {
    if (t === "|") return { k: "sep" };
    if (/^[+-]?(\d+(\.\d+)?|\.\d+)$/.test(t))
      return { k: "num", s: t.startsWith(".") ? "0" + t : t };
    if (COORD_DIR[t]) return { k: "dir", d: COORD_DIR[t] };
    return { k: "bad" };
  });
  if (TOK.some((t) => t.k === "bad")) return null;
  const nNum = TOK.filter((t) => t.k === "num").length;
  if (nNum < 2) return null;
  // bentuk grup komponen
  let groups = [];
  const seps = TOK.filter((t) => t.k === "sep").length;
  const nDir = TOK.filter((t) => t.k === "dir").length;
  if (seps > 1 || nDir > 2) return null;
  if (seps === 1) {
    let cur = { nums: [], dir: null };
    TOK.forEach((t) => {
      if (t.k === "sep") {
        groups.push(cur);
        cur = { nums: [], dir: null };
      } else if (t.k === "num") cur.nums.push(t.s);
      else if (t.k === "dir") {
        if (cur.dir) cur.bad = true;
        cur.dir = t.d;
      }
    });
    groups.push(cur);
    if (groups.some((g) => g.bad)) return null;
  } else if (nDir > 0) {
    const first = TOK[0].k === "dir"; // huruf di depan (S 3 19 07) atau di belakang (3 19 07 S)
    let cur = { nums: [], dir: null };
    TOK.forEach((t) => {
      if (t.k === "dir") {
        if (first) {
          if (cur.nums.length || cur.dir) groups.push(cur);
          cur = { nums: [], dir: t.d };
        } else {
          cur.dir = t.d;
          groups.push(cur);
          cur = { nums: [], dir: null };
        }
      } else cur.nums.push(t.s);
    });
    if (cur.nums.length || cur.dir) groups.push(cur);
    if (nDir === 1 && groups.length === 1) {
      // satu huruf saja: bagi dua angka, huruf milik komponen yang dekat
      const g = groups[0],
        h = g.nums.length / 2;
      if (!Number.isInteger(h)) return null;
      const dir = g.dir,
        a = { nums: g.nums.slice(0, h), dir: null },
        b = { nums: g.nums.slice(h), dir: null };
      if (first) a.dir = dir;
      else b.dir = dir;
      groups = [a, b];
    }
  } else {
    if (![2, 4, 6].includes(nNum)) return null;
    const nums = TOK.map((t) => t.s);
    const h = nums.length / 2;
    groups = [
      { nums: nums.slice(0, h), dir: null },
      { nums: nums.slice(h), dir: null },
    ];
  }
  if (groups.length !== 2 || groups.some((g) => !g.nums.length)) return null;
  const comps = groups.map((g) => coordComponent(g.nums, g.dir));
  if (comps.some((c) => !c)) return null;
  let lat, lng;
  const [c1, c2] = comps;
  if (c1.axis && c2.axis) {
    if (c1.axis === c2.axis) return null;
  }
  const ax1 = c1.axis || (c2.axis === "lat" ? "lng" : "lat");
  const ax2 = c2.axis || (ax1 === "lat" ? "lng" : "lat");
  if (ax1 === ax2) return null;
  if (ax1 === "lat") {
    lat = c1.val;
    lng = c2.val;
  } else {
    lat = c2.val;
    lng = c1.val;
  }
  const r = (x) => Math.round(x * 1e8) / 1e8;
  lat = r(lat);
  lng = r(lng);
  return { lat, lng, valid: Math.abs(lat) <= 90 && Math.abs(lng) <= 180 };
}
// Format tampilan: derajat-menit-detik dengan huruf arah (S/N, E/W)
function formatDMS(lat, lng) {
  const one = (v, pos, neg) => {
    const hemi = v < 0 ? neg : pos;
    let a = Math.abs(v);
    let d = Math.floor(a);
    let mi = Math.floor((a - d) * 60);
    let sec = Math.round(((a - d) * 60 - mi) * 60 * 100) / 100;
    if (sec >= 60) {
      sec = 0;
      mi += 1;
    }
    if (mi >= 60) {
      mi = 0;
      d += 1;
    }
    return `${d}°${String(mi).padStart(2, "0")}'${sec.toFixed(2).padStart(5, "0")}"${hemi}`;
  };
  return `${one(lat, "N", "S")} ${one(lng, "E", "W")}`;
}

// Aset NETGIS terdekat dari sebuah titik (node saja; kabel dihitung dari titik pertama tidak akurat)
function nearestAssets(lat, lng, n = 3, maxM = 500) {
  const here = L.latLng(lat, lng);
  return allInventoryData
    .filter(
      (x) =>
        x.category === "NODE" &&
        Number.isFinite(x.lat) &&
        Number.isFinite(x.lng),
    )
    .map((x) => ({ x, d: here.distanceTo(L.latLng(x.lat, x.lng)) }))
    .filter((r) => r.d <= maxM)
    .sort((a, b) => a.d - b.d)
    .slice(0, n);
}

function placePopupHtml(r) {
  const near = nearestAssets(r.lat, r.lng);
  const nearHtml = near.length
    ? `<div class="popup-row" style="margin-top:6px;"><span>Aset jaringan terdekat:</span></div>` +
      near
        .map(
          (n) =>
            `<div class="popup-row" style="margin:0;">&bull; ${escapeHtml(n.x.name)} <b>(${Math.round(n.d)} m)</b></div>`,
        )
        .join("")
    : `<div class="popup-row" style="margin-top:6px;color:#b45309;">Tidak ada aset jaringan dalam radius 500 m.</div>`;
  const planOn = TOOL && TOOL.mode === "plan";
  const acts =
    (planOn && can("plan.write")
      ? `<button class="btn-status btn-outline-muted" onclick="planPointAtSearch('dest')"><i class="fa-solid fa-flag-checkered"></i> Jadikan tujuan</button><button class="btn-status btn-outline-muted" onclick="planPointAtSearch('via')"><i class="fa-solid fa-route"></i> Titik singgah</button><button class="btn-status btn-outline-muted" onclick="planPointAtSearch('hub')"><i class="fa-solid fa-circle-nodes"></i> Titik hub</button>`
      : "") +
    (can("incident.write")
      ? `<button class="btn-status btn-warning" onclick="createIncidentAtSearch()"><i class="fa-solid fa-triangle-exclamation"></i> Tiket di sini</button>`
      : "") +
    (can("asset.write")
      ? `<button class="btn-status btn-success" onclick="addAssetAtSearch()"><i class="fa-solid fa-plus"></i> Aset di sini</button>`
      : "") +
    `<button class="btn-status btn-outline-muted" onclick="coverageAtSearch()"><i class="fa-solid fa-signal"></i> Cek Coverage di sini</button>` +
    (can("plan.write")
      ? `<button class="btn-status btn-outline-muted" onclick="planToSearch()"><i class="fa-solid fa-route"></i> Pasang Baru ke sini</button>`
      : "");
  return `<div class="popup-header">${escapeHtml(r.name)}</div>
    ${r.label ? `<div class="popup-row">${escapeHtml(r.label)}</div>` : ""}
    <div class="popup-row"><span>Koordinat:</span> <b>${r.lat.toFixed(6)}, ${r.lng.toFixed(6)}</b>
      <a href="#" class="popup-mini" onclick="copyCardCoord(${r.lat.toFixed(6)}, ${r.lng.toFixed(6)}); return false;" title="Salin koordinat"><i class="fa-regular fa-copy"></i> Salin</a>
      <a class="popup-mini" target="_blank" rel="noopener" href="https://www.google.com/maps?q=${r.lat.toFixed(6)},${r.lng.toFixed(6)}" title="Buka di Google Maps"><i class="fa-solid fa-up-right-from-square"></i> Maps</a></div>
    <div class="popup-row"><span>DMS:</span> <b>${escapeHtml(formatDMS(r.lat, r.lng))}</b></div>
    ${r.label ? "" : `<div class="popup-row" id="card-addr"><a href="#" class="popup-mini" onclick="loadCardAddress(${r.lat.toFixed(6)}, ${r.lng.toFixed(6)}); return false;"><i class="fa-solid fa-magnifying-glass-location"></i> Cari alamat</a></div>`}
    ${nearHtml}${acts ? `<div class="popup-actions">${acts}</div>` : ""}`;
}
// ---------- TAHAP C: kartu titik saat peta diklik ----------
const MAPUI = { drawing: false };
var CONNECT = {
  active: false,
  step: null,
  points: [],
  segs: [],
  busy: false,
  start: null,
  end: null,
  line: null,
  handles: [],
  coords: [],
  hover: null,
  opts: null,
  reverse: false,
  source: "",
};
["draw:drawstart", "draw:editstart", "draw:deletestart"].forEach((ev) =>
  map.on(ev, () => {
    MAPUI.drawing = true;
  }),
);
["draw:drawstop", "draw:editstop", "draw:deletestop"].forEach((ev) =>
  map.on(ev, () => {
    MAPUI.drawing = false;
  }),
);
function copyCardCoord(lat, lng) {
  const txt = `${lat}, ${lng}`;
  const done = () => {
    const el = document.getElementById("card-copied");
    if (el) el.textContent = "Disalin";
  };
  try {
    if (navigator.clipboard && navigator.clipboard.writeText)
      return navigator.clipboard
        .writeText(txt)
        .then(done)
        .catch(() => prompt("Salin koordinat:", txt));
  } catch (_) {
    /* jatuh ke prompt */
  }
  prompt("Salin koordinat:", txt);
}
// Alamat dimuat hanya bila diminta (geocoder bisa lambat); memakai Nominatim seperti pengisian kota
function loadCardAddress(lat, lng) {
  const el = document.getElementById("card-addr");
  if (!el) return;
  el.innerHTML = '<span class="tp-muted">mencari alamat...</span>';
  fetch(
    `https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat=${lat}&lon=${lng}`,
  )
    .then((r) => r.json())
    .then((d) => {
      const e2 = document.getElementById("card-addr");
      if (e2)
        e2.innerHTML = `<span>Alamat:</span> ${escapeHtml(d.display_name || "tidak ditemukan")}`;
    })
    .catch(() => {
      const e2 = document.getElementById("card-addr");
      if (e2)
        e2.innerHTML =
          '<span class="tp-muted">Alamat tidak tersedia (offline)</span>';
    });
}
function planPointAtSearch(kind) {
  if (!searchHighlightMarker) return;
  const ll = searchHighlightMarker.getLatLng();
  map.closePopup();
  if (kind === "dest") setPlanDest(ll.lat, ll.lng);
  else if (kind === "via") addPlanVia(ll.lat, ll.lng);
  else if (kind === "hub") setPlanHub(ll.lat, ll.lng);
}
function showClickCard(lat, lng) {
  attachSearchMarker(
    lat,
    lng,
    placePopupHtml({ name: "Titik di peta", label: "", lat, lng }),
  );
}
// klik peta: abaikan saat memilih titik (pick), menggambar/mengedit, atau mengklik garis/ikon (aset)
map.on("click", (e) => {
  if (!e || !e.latlng) return;
  if (TOOL.pick || MAPUI.drawing || CONNECT.active) {
    clearSearchMarker();
    return;
  }
  const t = e.originalEvent && e.originalEvent.target;
  if (t && t.closest && t.closest(".leaflet-popup, .leaflet-control")) return;
  if (t && t.closest && t.closest(".leaflet-interactive")) {
    clearSearchMarker();
    return;
  }
  showClickCard(e.latlng.lat, e.latlng.lng);
});

function createIncidentAtSearch() {
  if (!searchHighlightMarker) return;
  const ll = searchHighlightMarker.getLatLng();
  map.closePopup();
  openAddIncidentModal(ll.lat, ll.lng);
}
function addAssetAtSearch() {
  if (!searchHighlightMarker) return;
  const ll = searchHighlightMarker.getLatLng();
  map.closePopup();
  openAddAssetModal("NODE", ll.lat, ll.lng);
}

function hideSearchDropdown() {
  const d = document.getElementById("search-autocomplete-results");
  if (d) d.style.display = "none";
  SEARCH.active = -1;
}

// Daftar item yang tampil, berurutan (dipakai juga untuk navigasi keyboard)
function searchItems() {
  const items = [];
  if (SEARCH.coord && SEARCH.coord.valid)
    items.push({ t: "coord", c: SEARCH.coord });
  SEARCH.local.forEach((a) => items.push({ t: "asset", a }));
  SEARCH.remote.forEach((r, i) => items.push({ t: "place", r, i }));
  return items;
}

function renderSearchDropdown() {
  const dd = document.getElementById("search-autocomplete-results");
  if (!dd) return;
  const q = (
    document.getElementById("map-search-input") || { value: "" }
  ).value.trim();
  let html = "";
  if (q.length < 2) {
    const rec = loadRecentSearches();
    if (!rec.length) return hideSearchDropdown();
    html += `<div class="ac-section">Pencarian terakhir <a href="#" onclick="clearRecentSearches();return false;">hapus</a></div>`;
    html += rec
      .map(
        (r, i) =>
          `<div class="autocomplete-item" onclick="pickRecentSearch(${i})"><div><div class="item-title"><i class="fa-solid fa-clock-rotate-left" style="margin-right:6px;color:#94a3b8;"></i>${escapeHtml(r.text)}</div></div></div>`,
      )
      .join("");
    dd.innerHTML = html;
    dd.style.display = "block";
    return;
  }
  const items = searchItems();
  let idx = 0;
  const cls = () =>
    "autocomplete-item" + (idx === SEARCH.active ? " active" : "");
  const coordItem = items.find((x) => x.t === "coord");
  if (coordItem) {
    html += `<div class="${cls()}" data-i="${idx}" onclick="chooseSearchItem(${idx})"><div><div class="item-title"><i class="fa-solid fa-crosshairs" style="margin-right:6px;"></i>Ke koordinat ${+coordItem.c.lat.toFixed(6)}, ${+coordItem.c.lng.toFixed(6)}</div><div class="item-subtitle">${escapeHtml(formatDMS(coordItem.c.lat, coordItem.c.lng))}</div></div></div>`;
    idx++;
  }
  if (SEARCH.local.length) {
    html += `<div class="ac-section">Aset NETGIS</div>`;
    SEARCH.local.forEach((item) => {
      html += `<div class="${cls()}" data-i="${idx}" onclick="chooseSearchItem(${idx})">
        <div><div class="item-title"><i class="${getItemIconClass(item.type)}" style="margin-right: 6px;"></i>${escapeHtml(item.name)}</div>
        <div class="item-subtitle">${escapeHtml(item.type)} • ${escapeHtml(item.cluster || "General")} (${escapeHtml(item.city || "Area")})</div></div>
        <span class="item-badge">${escapeHtml(item.status || "Active")}</span></div>`;
      idx++;
    });
  }
  html += `<div class="ac-section">Tempat &amp; alamat${SEARCH.loading ? ` <span class="ac-spin">mencari...</span>` : ""}</div>`;
  if (SEARCH.remote.length) {
    SEARCH.remote.forEach((r) => {
      html += `<div class="${cls()}" data-i="${idx}" onclick="chooseSearchItem(${idx})">
        <div><div class="item-title"><i class="fa-solid fa-location-dot" style="margin-right:6px;color:#ef4444;"></i>${escapeHtml(r.name)}</div>
        <div class="item-subtitle">${escapeHtml(r.label || r.kind || "")}${farLabel(r)}</div></div></div>`;
      idx++;
    });
  } else if (!SEARCH.loading) {
    html += `<div class="ac-empty">${escapeHtml(q.length < 3 ? "Ketik minimal 3 huruf untuk mencari tempat" : SEARCH.error || "Tempat tidak ditemukan")}</div>`;
  }
  if (
    !SEARCH.local.length &&
    !SEARCH.remote.length &&
    !SEARCH.loading &&
    !coordItem
  ) {
    // tidak ada apa pun: pesan di atas sudah cukup
  }
  dd.innerHTML = html;
  dd.style.display = "block";
}

function handleLiveSearch(query) {
  const keyword = (query || "").trim().toLowerCase();
  SEARCH.query = keyword;
  SEARCH.coord = parseCoordinate(query || "");
  SEARCH.active = -1;
  const has = (v) => v && String(v).toLowerCase().includes(keyword);
  SEARCH.local =
    keyword.length < 2
      ? []
      : allInventoryData
          .filter(
            (i) => has(i.name) || has(i.type) || has(i.city) || has(i.cluster),
          )
          .slice(0, 5);
  clearTimeout(SEARCH.timer);
  SEARCH.remote = [];
  SEARCH.error = null;
  SEARCH.loading = false;
  const myseq = ++SEARCH.seq;
  if (keyword.length >= 3 && !SEARCH.coord) {
    SEARCH.loading = true;
    SEARCH.timer = setTimeout(() => runPlaceSearch(query.trim(), myseq), 350);
  }
  renderSearchDropdown();
}

function runPlaceSearch(q, seq) {
  const c = map.getCenter ? map.getCenter() : null;
  const bias = c ? `&lat=${c.lat.toFixed(4)}&lng=${c.lng.toFixed(4)}` : "";
  return apiRequest(`/api/geocode?q=${encodeURIComponent(q)}${bias}&limit=6`)
    .then((d) => {
      if (seq !== SEARCH.seq) return; // jawaban usang (user sudah mengetik lagi)
      SEARCH.remote = Array.isArray(d.results) ? d.results : [];
      SEARCH.error = d.error || null;
    })
    .catch((err) => {
      if (seq !== SEARCH.seq || err.status === 401) return;
      SEARCH.remote = [];
      SEARCH.error = "Pencarian tempat tidak tersedia saat ini";
    })
    .finally(() => {
      if (seq !== SEARCH.seq) return;
      SEARCH.loading = false;
      renderSearchDropdown();
    });
}

function farLabel(r) {
  const d = distFromMapCenterM(r.lat, r.lng);
  return d > 1000
    ? ` &bull; <span class="${d > FAR_PLACE_M ? "far-warn" : ""}">${fmtFar(d)} dari tengah peta${d > FAR_PLACE_M ? " (jauh)" : ""}</span>`
    : "";
}
function selectPlace(r) {
  hideSearchDropdown();
  document.getElementById("map-search-input").value = r.name;
  saveRecentSearch(r.name, r.lat, r.lng);
  showSearchHighlight(r.lat, r.lng, placePopupHtml(r), r.bbox, 17);
}

function chooseSearchItem(i) {
  const it = searchItems()[i];
  if (!it) return;
  if (it.t === "coord") return goToCoordinate(it.c);
  if (it.t === "asset") {
    document.getElementById("map-search-input").value = it.a.name;
    saveRecentSearch(it.a.name);
    return selectSearchRecommendation(it.a.category.toLowerCase(), it.a.id);
  }
  selectPlace(it.r);
}

function pickRecentSearch(i) {
  const r = loadRecentSearches()[i];
  if (!r) return;
  const inp = document.getElementById("map-search-input");
  inp.value = r.text;
  if (Number.isFinite(r.lat) && Number.isFinite(r.lng)) {
    hideSearchDropdown();
    return showSearchHighlight(
      r.lat,
      r.lng,
      placePopupHtml({ name: r.text, label: "", lat: r.lat, lng: r.lng }),
    );
  }
  handleLiveSearch(r.text);
}

function goToCoordinate(c) {
  hideSearchDropdown();
  if (!c.valid)
    return alert("Koordinat di luar jangkauan (lat -90..90, lng -180..180).");
  showSearchHighlight(
    c.lat,
    c.lng,
    placePopupHtml({
      name: "Titik Koordinat",
      label: "",
      lat: c.lat,
      lng: c.lng,
    }),
  );
}

function onMapSearchFocus() {
  renderSearchDropdown();
}

function onMapSearchKey(ev) {
  const k = ev.key;
  const items = searchItems();
  const dd = document.getElementById("search-autocomplete-results");
  const open = dd && dd.style.display === "block";
  if ((k === "ArrowDown" || k === "ArrowUp") && open && items.length) {
    ev.preventDefault();
    SEARCH.active =
      k === "ArrowDown"
        ? (SEARCH.active + 1) % items.length
        : (SEARCH.active - 1 + items.length) % items.length;
    renderSearchDropdown();
    const el =
      dd.querySelector && dd.querySelector(".autocomplete-item.active");
    if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest" });
  } else if (k === "Enter") {
    ev.preventDefault();
    if (SEARCH.active >= 0 && items[SEARCH.active])
      chooseSearchItem(SEARCH.active);
    else handleMapSearch();
  } else if (k === "Escape") {
    hideSearchDropdown();
  }
}

// Tombol cari / Enter tanpa memilih saran: koordinat -> aset (cocok nama) -> tempat teratas
function handleMapSearch() {
  const query = document.getElementById("map-search-input").value.trim();
  if (!query) return;

  const coord = parseCoordinate(query);
  if (coord) return goToCoordinate(coord);

  const q = query.toLowerCase();
  const asset =
    allInventoryData.find((i) => (i.name || "").toLowerCase() === q) ||
    allInventoryData.find((i) => (i.name || "").toLowerCase().includes(q));
  if (asset) {
    hideSearchDropdown();
    saveRecentSearch(asset.name);
    return selectSearchRecommendation(asset.category.toLowerCase(), asset.id);
  }

  // Bukan aset: pakai hasil tempat yang sudah ada, atau cari sekarang
  const pickTop = () => {
    const top = SEARCH.remote[0];
    // hasil teratas jauh dari area kerja: jangan langsung terbang, biarkan pengguna memilih dari daftar
    if (
      SEARCH.remote.length > 1 &&
      distFromMapCenterM(top.lat, top.lng) > FAR_PLACE_M
    ) {
      renderSearchDropdown();
      return;
    }
    selectPlace(top);
  };
  if (SEARCH.remote.length && SEARCH.query === q) return pickTop();
  if (q.length < 3)
    return alert(`Aset atau koordinat "${query}" tidak ditemukan.`);
  clearTimeout(SEARCH.timer);
  const myseq = ++SEARCH.seq;
  return runPlaceSearch(query, myseq).then(() => {
    if (SEARCH.remote.length) pickTop();
    else
      alert(
        `"${query}" tidak ditemukan sebagai aset, koordinat, maupun tempat.${SEARCH.error ? "\n(" + SEARCH.error + ")" : ""}`,
      );
  });
}

// ==========================================
// FITUR LIVE SEARCH & REKOMENDASI ASET
// ==========================================
// Helper ikon berdasarkan tipe aset
function getItemIconClass(type) {
  switch ((type || "").toUpperCase()) {
    case "ODP":
      return "fa-solid fa-microchip";
    case "POP":
      return "fa-solid fa-server";
    case "CLOSURE":
      return "fa-solid fa-box";
    case "TIANG":
      return "fa-solid fa-archway";
    case "HH":
      return "fa-solid fa-square-h";
    case "SLACK":
      return "fa-solid fa-circle-nodes";
    case "CABLE":
    case "FEEDER":
    case "BACKBONE":
    case "DISTRIBUTION":
    case "DROP":
      return "fa-solid fa-route";
    default:
      return "fa-solid fa-location-dot";
  }
}

// Handler saat opsi rekomendasi diklik / pencarian nama dikonfirmasi
function selectSearchRecommendation(kind, id) {
  const dropdown = document.getElementById("search-autocomplete-results");
  if (dropdown) dropdown.style.display = "none";

  const item = allInventoryData.find(
    (x) => x.category.toLowerCase() === kind && String(x.id) === String(id),
  );
  if (!item || !Number.isFinite(item.lat) || !Number.isFinite(item.lng)) {
    return alert("Koordinat aset tidak valid.");
  }

  map.flyTo([item.lat, item.lng], 18, { duration: 1.5 });
  setTimeout(() => openAssetPopup(`${kind}:${id}`), 1650);
}

// Tutup dropdown jika mengklik di luar area search
document.addEventListener("click", function (e) {
  const searchContainer = document.querySelector(".map-search-overlay");
  const dropdown = document.getElementById("search-autocomplete-results");
  if (searchContainer && !searchContainer.contains(e.target) && dropdown) {
    dropdown.style.display = "none";
    SEARCH.active = -1;
  }
});

// =====================================================================
// AUTENTIKASI, PERAN, ACCESS CONTROL, RIWAYAT PERUBAHAN
// Server adalah penentu hak akses; pengecekan di sini hanya untuk menyembunyikan tombol
// yang pasti ditolak agar antarmuka tidak membingungkan.
// =====================================================================
const AUTH = { user: null };

function can(perm) {
  return !!(
    AUTH.user &&
    Array.isArray(AUTH.user.permissions) &&
    AUTH.user.permissions.includes(perm)
  );
}

function $id(x) {
  return document.getElementById(x);
}

function showAuthMsg(id, text, info) {
  const el = $id(id);
  if (!el) return;
  el.className = "auth-msg" + (info ? " info" : "");
  el.textContent = text || "";
  el.style.display = text ? "block" : "none";
}

function showLogin(message) {
  AUTH.user = null;
  try {
    clearAllLayers();
    if (searchHighlightMarker) {
      map.removeLayer(searchHighlightMarker);
      searchHighlightMarker = null;
    }
  } catch (_) {
    /* peta belum siap */
  }
  try {
    closeToolPanel(true);
  } catch (_) {
    /* panel belum siap */
  }
  [
    "modal-data",
    "modal-users",
    "modal-audit",
    "modal-password",
    "modal-inventory",
    "modal-noc-monitor",
    "modal-incident-detail",
    "modal-add-asset",
    "modal-core-detail",
    "modal-add-incident",
  ].forEach((m) => {
    if ($id(m)) $id(m).style.display = "none";
  });
  $id("user-box").style.display = "none";
  $id("login-overlay").style.display = "flex";
  $id("login-pass").value = "";
  showAuthMsg("login-msg", message || "", !message);
  setTimeout(() => {
    try {
      $id("login-user").focus();
    } catch (_) {}
  }, 50);
}

function configureDrawControl() {
  try {
    map.removeControl(drawControl);
  } catch (_) {
    /* belum terpasang */
  }
  const canAsset = can("asset.write");
  if (!canAsset && !can("incident.write")) return; // viewer: tanpa toolbar gambar
  drawControl = new L.Control.Draw({
    draw: {
      polygon: false,
      circle: false,
      rectangle: false,
      circlemarker: false,
      marker: true,
      polyline: canAsset,
    },
    edit: canAsset ? { featureGroup: editableGroup, remove: false } : false,
  });
  map.addControl(drawControl);
}

function applyPermissions() {
  const u = AUTH.user;
  $id("user-box").style.display = u ? "flex" : "none";
  if (!u) return;
  $id("user-box-name").textContent = u.full_name || u.username;
  $id("user-box-role").textContent = u.role_label || u.role;
  $id("nav-audit").style.display = can("audit.view") ? "" : "none";
  $id("nav-users").style.display = can("user.manage") ? "" : "none";
  if ($id("nav-naming"))
    $id("nav-naming").style.display = can("asset.write") ? "" : "none";
  $id("nav-wilayah").style.display = ""; // semua peran boleh melihat daftar Wilayah; tombol ubah hanya untuk admin
  $id("nav-plan").style.display = can("plan.write") ? "" : "none";
  if ($id("nav-covbulk"))
    $id("nav-covbulk").style.display = can("plan.write") ? "" : "none";
  if ($id("nav-topofix"))
    $id("nav-topofix").style.display = can("asset.write") ? "" : "none";
  $id("noc-hide-actions").style.display = can("incident.write") ? "" : "none";
  configureDrawControl();
  const cb = $id("connect-btn");
  if (cb) cb.style.display = can("asset.write") ? "" : "none";
}

function onLoggedIn(user) {
  AUTH.user = user;
  $id("login-overlay").style.display = "none";
  applyPermissions();
  if (user.must_change_password) {
    openPasswordModal(true); // data dimuat setelah password diganti
  } else {
    loadData();
  }
  loadScopeOptions();
}

function bootAuth() {
  apiRequest("/api/auth/me")
    .then((d) => onLoggedIn(d.user))
    .catch(() => showLogin(""));
}

function submitLogin(ev) {
  if (ev && ev.preventDefault) ev.preventDefault();
  const btn = $id("login-btn");
  btn.disabled = true;
  apiRequest("/api/auth/login", "POST", {
    username: $id("login-user").value.trim(),
    password: $id("login-pass").value,
  })
    .then((d) => {
      showAuthMsg("login-msg", "");
      onLoggedIn(d.user);
    })
    .catch((err) => {
      $id("login-pass").value = "";
      showAuthMsg("login-msg", err.message);
    })
    .finally(() => {
      btn.disabled = false;
    });
}

function doLogout() {
  apiRequest("/api/auth/logout", "POST")
    .catch(() => {})
    .finally(() => showLogin("Anda sudah keluar."));
}

// ---- Ganti password ----
let passwordForced = false;
function openPasswordModal(forced) {
  passwordForced = !!forced;
  ["pw-old", "pw-new", "pw-new2"].forEach((i) => {
    $id(i).value = "";
  });
  showAuthMsg("pw-msg", "");
  $id("pw-cancel").style.display = passwordForced ? "none" : "";
  $id("pw-sub").textContent = passwordForced
    ? "Password Anda perlu diganti sebelum melanjutkan (minimal 8 karakter)."
    : "Gunakan minimal 8 karakter.";
  $id("modal-password").style.display = "flex";
}

function closePasswordModal() {
  if (passwordForced) return; // tidak bisa ditutup sebelum diganti
  $id("modal-password").style.display = "none";
}

function submitPasswordChange(ev) {
  if (ev && ev.preventDefault) ev.preventDefault();
  if ($id("pw-new").value !== $id("pw-new2").value)
    return showAuthMsg("pw-msg", "Konfirmasi password baru tidak sama.");
  apiRequest("/api/auth/password", "POST", {
    current_password: $id("pw-old").value,
    new_password: $id("pw-new").value,
  })
    .then(() => {
      const wasForced = passwordForced;
      passwordForced = false;
      $id("modal-password").style.display = "none";
      if (AUTH.user) AUTH.user.must_change_password = false;
      alert("Password berhasil diganti.");
      if (wasForced) loadData();
    })
    .catch((err) => showAuthMsg("pw-msg", err.message));
}

// ---- Access Control: kelola user (admin) ----
function openUsersModal() {
  if (!can("user.manage"))
    return alert("Hanya Admin yang dapat mengelola user.");
  $id("modal-users").style.display = "flex";
  loadUsers();
}
function closeUsersModal() {
  $id("modal-users").style.display = "none";
}

function loadUsers() {
  apiRequest("/api/users")
    .then((d) => {
      const sel = $id("usr-role");
      sel.innerHTML = d.roles
        .map(
          (r) =>
            `<option value="${escapeHtml(r.value)}">${escapeHtml(r.label)}</option>`,
        )
        .join("");
      const me = AUTH.user ? AUTH.user.id : null;
      $id("users-tbody").innerHTML = d.users
        .map((u) => {
          const opts = d.roles
            .map(
              (r) =>
                `<option value="${escapeHtml(r.value)}"${r.value === u.role ? " selected" : ""}>${escapeHtml(r.label)}</option>`,
            )
            .join("");
          const status = !u.active
            ? `<span class="chip red">Nonaktif</span>`
            : u.locked
              ? `<span class="chip amber">Terkunci</span>`
              : u.must_change_password
                ? `<span class="chip blue">Wajib ganti password</span>`
                : `<span class="chip green">Aktif</span>`;
          return `<tr>
          <td><b>${escapeHtml(u.username)}</b>${u.id === me ? " (Anda)" : ""}<div style="color:#64748b">${escapeHtml(u.full_name)}</div></td>
          <td><select onchange="changeUserRole(${Number(u.id)}, this.value)">${opts}</select></td>
          <td>${status}</td>
          <td>${u.last_login_at ? timeHtml(u.last_login_at) : "-"}</td>
          <td>
            <button class="adm-btn ${u.active ? "bad" : "ok"}" onclick="toggleUserActive(${Number(u.id)}, ${u.active ? "false" : "true"})">${u.active ? "Nonaktifkan" : "Aktifkan"}</button>
            <button class="adm-btn warn" onclick="resetUserPassword(${Number(u.id)}, '${escapeHtml(u.username).replace(/'/g, "&#39;")}')">Reset password</button>
          </td></tr>`;
        })
        .join("");
    })
    .catch((err) => alert("Gagal memuat user: " + err.message));
}

function submitNewUser() {
  const payload = {
    username: $id("usr-name").value.trim(),
    full_name: $id("usr-full").value.trim() || null,
    role: $id("usr-role").value,
    password: $id("usr-pass").value,
  };
  if (!payload.username || !payload.password)
    return alert("Username dan password awal wajib diisi.");
  apiRequest("/api/users", "POST", payload)
    .then((d) => {
      alert(d.message || "User dibuat.");
      ["usr-name", "usr-full", "usr-pass"].forEach((i) => {
        $id(i).value = "";
      });
      loadUsers();
    })
    .catch((err) => alert("Gagal menambah user: " + err.message));
}

function changeUserRole(id, role) {
  apiRequest(`/api/users/${id}`, "PUT", { role })
    .then(loadUsers)
    .catch((err) => {
      alert("Gagal mengubah peran: " + err.message);
      loadUsers();
    });
}

function toggleUserActive(id, active) {
  if (
    !active &&
    !confirm("Nonaktifkan user ini? Sesi aktifnya langsung berakhir.")
  )
    return;
  apiRequest(`/api/users/${id}`, "PUT", { active })
    .then(loadUsers)
    .catch((err) => alert("Gagal: " + err.message));
}

function resetUserPassword(id, username) {
  if (
    !confirm(
      `Reset password "${username}"? Password sementara dibuat otomatis dan hanya ditampilkan sekali.`,
    )
  )
    return;
  apiRequest(`/api/users/${id}/reset-password`, "POST", {})
    .then((d) => {
      alert(
        `Password sementara untuk ${username}:\n\n${d.new_password}\n\nCatat sekarang, tidak akan ditampilkan lagi. User wajib menggantinya saat login.`,
      );
      loadUsers();
    })
    .catch((err) => alert("Gagal reset password: " + err.message));
}

// ---- Riwayat Perubahan ----
const AUDIT = { offset: 0, limit: 25, total: 0, entityId: null };
const AUDIT_ACTION = {
  CREATE: ["Tambah", "green"],
  UPDATE: ["Ubah", "blue"],
  DELETE: ["Hapus", "red"],
  STATUS: ["Status", "amber"],
  CONNECT: ["Sambung", "green"],
  DISCONNECT: ["Putus", "red"],
  REPAIR: ["Perbaikan", "green"],
  UNDO_REPAIR: ["Batal perbaikan", "amber"],
  RESTORE: ["Pulihkan", "blue"],
  LOGIN: ["Login", ""],
  LOGIN_FAILED: ["Login gagal", "red"],
  LOGOUT: ["Logout", ""],
  IMPORT: ["Impor data", "green"],
  EXPORT: ["Ekspor data", ""],
  USER_CREATED: ["User baru", "green"],
  USER_UPDATED: ["Ubah user", "blue"],
  PASSWORD_CHANGED: ["Ganti password", "amber"],
  PASSWORD_RESET: ["Reset password", "amber"],
};
const AUDIT_TYPE = {
  NODE: "Node",
  CABLE: "Kabel",
  CONNECTION: "Sambungan",
  INCIDENT: "Tiket",
  USER: "User",
  PLAN: "Rencana",
  DATA: "Data",
  SETTING: "Pengaturan",
};

function openAuditModal(entityType, entityId, entityName) {
  if (!can("audit.view"))
    return alert("Riwayat perubahan hanya untuk NOC dan Admin.");
  try {
    map.closePopup();
  } catch (_) {}
  $id("aud-q").value = "";
  $id("aud-type").value = entityType || "ALL";
  $id("aud-action").value = "ALL";
  AUDIT.entityId = entityType && entityId != null ? Number(entityId) : null;
  if (!entityName && AUDIT.entityId != null) {
    const hit = allInventoryData.find(
      (x) => x.category === entityType && Number(x.id) === AUDIT.entityId,
    );
    entityName = hit ? hit.name : null;
  }
  const scope = $id("aud-scope");
  scope.style.display = AUDIT.entityId != null ? "block" : "none";
  scope.innerHTML =
    AUDIT.entityId != null
      ? `Menampilkan riwayat satu objek: <b>${escapeHtml(entityName || "#" + AUDIT.entityId)}</b> &middot; <a href="#" onclick="clearAuditScope();return false;">tampilkan semua</a>`
      : "";
  $id("modal-audit").style.display = "flex";
  loadAudit(0);
}
function closeAuditModal() {
  $id("modal-audit").style.display = "none";
}
function clearAuditScope() {
  AUDIT.entityId = null;
  $id("aud-scope").style.display = "none";
  loadAudit(0);
}

function loadAudit(dir, relative) {
  AUDIT.offset = relative ? Math.max(0, AUDIT.offset + dir * AUDIT.limit) : 0;
  const p = new URLSearchParams({ limit: AUDIT.limit, offset: AUDIT.offset });
  const t = $id("aud-type").value,
    a = $id("aud-action").value,
    q = $id("aud-q").value.trim();
  if (t !== "ALL") p.set("entity_type", t);
  if (a !== "ALL") p.set("action", a);
  if (q) p.set("q", q);
  if (AUDIT.entityId != null) p.set("entity_id", AUDIT.entityId);
  apiRequest("/api/audit?" + p.toString())
    .then((d) => {
      AUDIT.total = d.total;
      const rows = d.items.map(renderAuditRow).join("");
      $id("audit-tbody").innerHTML =
        rows ||
        `<tr><td colspan="6" style="text-align:center;color:#94a3b8;padding:16px;">Belum ada riwayat.</td></tr>`;
      const from = d.total ? d.offset + 1 : 0,
        to = Math.min(d.offset + d.limit, d.total);
      $id("aud-page").textContent = `${from}-${to} dari ${d.total}`;
      $id("aud-prev").disabled = d.offset <= 0;
      $id("aud-next").disabled = d.offset + d.limit >= d.total;
    })
    .catch((err) => alert("Gagal memuat riwayat: " + err.message));
}

function auditChangesHtml(ch) {
  if (!ch || typeof ch !== "object") return "";
  const fmt = (v) =>
    v === null || v === undefined || v === "" ? "—" : String(v);
  const entries = Object.entries(ch).filter(
    ([, v]) => Array.isArray(v) && v.length === 2,
  );
  if (!entries.length) return "";
  return (
    `<div class="aud-changes">` +
    entries
      .slice(0, 5)
      .map(
        ([k, v]) =>
          `${escapeHtml(k)}: ${escapeHtml(fmt(v[0]))} &rarr; <b>${escapeHtml(fmt(v[1]))}</b>`,
      )
      .join("<br>") +
    (entries.length > 5 ? `<br>+${entries.length - 5} perubahan lain` : "") +
    `</div>`
  );
}

function renderAuditRow(it) {
  const a = AUDIT_ACTION[it.action] || [it.action, ""];
  const restoreBtn =
    it.restorable && can("audit.restore")
      ? `<button class="adm-btn ok" onclick="restoreAudit(${Number(it.id)})"><i class="fa-solid fa-rotate-left"></i> Pulihkan</button>`
      : it.restored_at
        ? `<span class="chip">Sudah dipulihkan</span>`
        : "";
  return `<tr>
    <td>${timeHtml(it.ts)}</td>
    <td>${escapeHtml(it.username || "-")}<div style="color:#94a3b8;font-size:11px;">${escapeHtml(it.role || "")}</div></td>
    <td><span class="chip ${a[1]}">${escapeHtml(a[0])}</span></td>
    <td>${escapeHtml(AUDIT_TYPE[it.entity_type] || it.entity_type || "")}<div style="font-weight:600;">${escapeHtml(it.entity_name || "")}</div></td>
    <td>${escapeHtml(it.summary || "")}${auditChangesHtml(it.changes)}</td>
    <td>${restoreBtn}</td></tr>`;
}

function restoreAudit(id) {
  if (
    !confirm(
      "Pulihkan objek ini beserta hubungan & sambungan core-nya (ID aslinya dipakai kembali)?",
    )
  )
    return;
  apiRequest(`/api/audit/${id}/restore`, "POST")
    .then((d) => {
      alert(
        [d.message || "Berhasil dipulihkan."].concat(d.notes || []).join("\n"),
      );
      loadAudit(0);
      loadData();
    })
    .catch((err) => alert("Gagal memulihkan: " + err.message));
}

// ---- Batalkan perbaikan ----
function undoRepair(repairId) {
  const id = currentIncidentId;
  if (id == null) return;
  const reason = prompt(
    "Batalkan perbaikan ini?\nJika perbaikan memecah kabel, kedua segmen digabung kembali dan closure dihapus; tiket kembali ke status sebelumnya.\n\nAlasan pembatalan (opsional):",
    "",
  );
  if (reason === null) return;
  apiRequest(`/api/incidents/${id}/repairs/${repairId}/undo`, "POST", {
    reason: reason.trim() || null,
  })
    .then((d) => {
      alert(d.message || "Perbaikan dibatalkan.");
      loadData();
      openIncidentDetail(id);
    })
    .catch((err) => alert("Tidak dapat membatalkan: " + err.message));
}

// =====================================================================
// ROUND 9: CEK COVERAGE, PASANG BARU, IMPORT/EXPORT, SEMBUNYIKAN INSIDEN
// =====================================================================
const TOOL = {
  mode: null, // 'coverage' | 'plan'
  pick: null, // 'cov-point' | 'plan-origin' | 'plan-dest' | 'plan-via'
  cov: { lat: null, lng: null, data: null, seq: 0 },
  plan: {
    origin: null,
    dest: null,
    via: [],
    rmode: "road",
    bends: [],
    hub: null,
    result: null,
    id: null,
    status: null,
    rules: null,
    defaults: null,
    seq: 0,
    timer: null,
  },
};
const toolLayer = L.layerGroup().addTo(map);
const ORIGIN_TYPES = ["ODP", "CLOSURE", "SLACK", "POP"];
const TYPE_LABEL = {
  ODP: "ODP",
  CLOSURE: "Closure",
  SLACK: "Slack",
  POP: "POP",
  TIANG: "Tiang",
  HH: "Handhole",
  PELANGGAN: "Pelanggan",
};

function fmtM(m) {
  const n = m == null || m === "" ? NaN : Number(m);
  if (!Number.isFinite(n)) return "-";
  return n >= 1000
    ? `${(n / 1000).toFixed(2).replace(".", ",")} km`
    : `${Math.round(n)} m`;
}

// ---------- Panel alat ----------
function openToolPanel(mode) {
  if (TOOL.mode && TOOL.mode !== mode) clearToolLayer();
  TOOL.mode = mode;
  cancelPick();
  const panel = $id("tool-panel");
  panel.style.display = "flex";
  if (typeof npFit === "function") npFit(panel);
  if (panel.classList) panel.classList.remove("collapsed");
  $id("tp-coverage").style.display = mode === "coverage" ? "" : "none";
  $id("tp-plan").style.display = mode === "plan" ? "" : "none";
  $id("tp-title").innerHTML =
    mode === "coverage"
      ? '<i class="fa-solid fa-signal"></i> Cek Coverage'
      : '<i class="fa-solid fa-route"></i> Pasang Baru';
}
function planHasUnsaved() {
  const p = TOOL.plan;
  return !!(p && p.result && !p.id);
}
function resetPlanForm() {
  const P = TOOL.plan;
  if (P.timer) {
    clearTimeout(P.timer);
    P.timer = null;
  }
  P.seq = (P.seq || 0) + 1; // abaikan respons hitung yang masih berjalan
  Object.assign(P, {
    origin: null,
    dest: null,
    via: [],
    rmode: "road",
    bends: [],
    hub: null,
    result: null,
    id: null,
    boqId: null,
    status: null,
    stale: false,
    keepView: false,
    viaMarkers: [],
  });
  const setV = (id, v) => {
    const e = $id(id);
    if (e) e.value = v;
  };
  const setC = (id, v) => {
    const e = $id(id);
    if (e) e.checked = v;
  };
  [
    "plan-origin-input",
    "plan-dest-name",
    "plan-svc",
    "plan-bw",
    "plan-sn",
    "plan-reg",
    "plan-remarks",
  ].forEach((i) => setV(i, ""));
  setV("plan-linktype", "");
  setV("plan-bw-unit", "Mbps");
  setV("plan-cores", "1");
  setV("plan-scen", "AUTO");
  setV("plan-splitter", "1:8");
  setC("plan-poles", true);
  setC("plan-slack", true);
  setC("plan-customer", true);
  const radio = (name, val) =>
    document.querySelectorAll(`input[name=${name}]`).forEach((r) => {
      r.checked = r.value === val;
    });
  radio("plan-inst", "Udara");
  radio("plan-rmode", "road");
  radio("plan-term", "DROPCORE_ROSET");
  radio("plan-hubmode", "NEAREST");
  radio("plan-otbmode", "AUTO");
  const setT = (id, t) => {
    const e = $id(id);
    if (e) e.textContent = t;
  };
  setT("plan-origin-chip", "Belum dipilih");
  setT("plan-dest-chip", "Belum dipilih");
  setT("plan-hub-chip", "Hub belum dipilih");
  ["plan-via-list", "plan-result", "plan-boq"].forEach((i) => {
    const e = $id(i);
    if (e) e.innerHTML = "";
  });
  const eb = $id("plan-edit-box");
  if (eb) eb.style.display = "none";
  const rb = $id("plan-realize-btn");
  if (rb) rb.style.display = "none";
  setPlanMsg("", "");
  try {
    if (typeof planUpdateBendCount === "function") planUpdateBendCount();
  } catch (_) {
    /* tidak ada */
  }
  try {
    if (typeof onPlanServiceChange === "function") onPlanServiceChange();
  } catch (_) {
    /* tidak ada */
  }
}
function resetCoverageForm() {
  TOOL.cov.lat = null;
  TOOL.cov.lng = null;
  TOOL.cov.data = null;
  TOOL.cov.seq = (TOOL.cov.seq || 0) + 1;
  const l = $id("cov-list");
  if (l) l.innerHTML = "";
  const v = $id("cov-verdict");
  if (v) {
    v.style.display = "none";
    v.textContent = "";
  }
  renderCoveragePointText();
}
function closeToolPanel(force) {
  if (
    !force &&
    TOOL.mode === "plan" &&
    planHasUnsaved() &&
    typeof confirm === "function" &&
    !confirm("Rencana ini belum disimpan. Tutup dan buang isian?")
  )
    return;
  cancelPick();
  clearToolLayer();
  if (TOOL.mode === "plan") resetPlanForm();
  else if (TOOL.mode === "coverage") resetCoverageForm();
  TOOL.mode = null;
  const panel = $id("tool-panel");
  if (panel) {
    panel.style.display = "none";
    if (panel.classList) panel.classList.remove("collapsed");
  }
}
// Esc menutup panel Cek Coverage / Pasang Baru (bila tidak sedang memilih titik, menghubungkan aset, atau ada dialog terbuka)
function toolPanelEscape() {
  const panel = $id("tool-panel");
  if (!panel || panel.style.display === "none" || !TOOL.mode) return;
  const open = Array.from(
    document.querySelectorAll(".modal-overlay, .auth-overlay"),
  ).some((m) => m.style && m.style.display && m.style.display !== "none");
  if (open) return;
  closeToolPanel();
}
function toggleToolPanelCollapse() {
  const panel = $id("tool-panel");
  if (panel && panel.classList) panel.classList.toggle("collapsed");
}
function clearToolLayer() {
  toolLayer.clearLayers();
  TOOL.plan.viaMarkers = [];
}

// ---------- Memilih titik di peta ----------
const PICK_HINT = {
  "cov-point": "Klik lokasi calon pelanggan di peta (Esc untuk batal)",
  "plan-origin": "Klik ODP / Closure / Slack / POP di peta (Esc untuk batal)",
  "plan-dest": "Klik lokasi pelanggan di peta (Esc untuk batal)",
  "plan-via": "Klik titik yang harus dilewati jalur (Esc untuk batal)",
  "plan-hub": "Klik lokasi closure/ODP hub di peta (Esc untuk batal)",
};
function startPick(kind) {
  TOOL.pick = kind;
  const c = map.getContainer && map.getContainer();
  if (c && c.classList) c.classList.add("pick-cursor");
  const panel = $id("tool-panel");
  // di layar sempit panel menutupi peta: ciutkan selama memilih titik
  if (
    panel &&
    panel.classList &&
    window.matchMedia &&
    window.matchMedia("(max-width: 768px)").matches
  )
    panel.classList.add("collapsed");
  const hintEl = TOOL.mode === "coverage" ? $id("cov-point") : null;
  if (hintEl) hintEl.textContent = PICK_HINT[kind];
  if (kind === "plan-origin") setPlanMsg(PICK_HINT[kind], "info");
  else if (kind === "plan-dest") setPlanMsg(PICK_HINT[kind], "info");
  else if (kind === "plan-via") setPlanMsg(PICK_HINT[kind], "info");
  else if (kind === "plan-hub") setPlanMsg(PICK_HINT[kind], "info");
}
function cancelPick() {
  TOOL.pick = null;
  const c = map.getContainer && map.getContainer();
  if (c && c.classList) c.classList.remove("pick-cursor");
}
function handlePick(kind, lat, lng, nodeId) {
  if (!kind) return;
  cancelPick();
  const panel = $id("tool-panel");
  if (panel && panel.classList) panel.classList.remove("collapsed");
  try {
    map.closePopup();
  } catch (_) {
    /* tidak ada popup */
  }
  if (kind === "cov-point") setCoveragePoint(lat, lng);
  else if (kind === "plan-dest") setPlanDest(lat, lng);
  else if (kind === "plan-via") addPlanVia(lat, lng);
  else if (kind === "plan-hub") setPlanHub(lat, lng);
  else if (kind === "plan-origin") pickPlanOriginAt(lat, lng, nodeId);
}
map.on("click", (e) => {
  if (TOOL.pick && e && e.latlng)
    handlePick(TOOL.pick, e.latlng.lat, e.latlng.lng);
});
// Klik langsung pada marker aset tidak memicu klik peta: tangani di sini
function onMarkerPicked(lat, lng, nodeId) {
  if (TOOL.pick) handlePick(TOOL.pick, lat, lng, nodeId);
}
document.addEventListener("keydown", (e) => {
  const escBusy = !!(TOOL.pick || CONNECT.active || searchHighlightMarker);
  if (e && e.key === "Escape" && searchHighlightMarker) {
    try {
      map.closePopup();
    } catch (_) {
      /* tidak ada popup */
    }
    clearSearchMarker();
  }
  if (e && e.key === "Escape" && CONNECT.active) connectCancel();
  if (e && e.key === "Escape" && TOOL.pick) {
    cancelPick();
    if (TOOL.mode === "coverage") renderCoveragePointText();
    else setPlanMsg("", "");
  }
  if (e && e.key === "Escape" && !escBusy && !e.defaultPrevented)
    toolPanelEscape();
});

function toolPinIcon(cls, label) {
  return L.divIcon({
    className: `tool-pin ${cls}`,
    html: `<span>${escapeHtml(label || "")}</span>`,
    iconSize: [26, 26],
    iconAnchor: [13, 13],
  });
}

// ---------- CEK COVERAGE ----------
function openCoveragePanel() {
  openToolPanel("coverage");
  if (TOOL.cov.lat == null) startPick("cov-point");
}
function renderCoveragePointText() {
  const el = $id("cov-point");
  if (!el) return;
  el.textContent =
    TOOL.cov.lat == null
      ? "Belum ada titik dipilih"
      : `Titik: ${TOOL.cov.lat.toFixed(6)}, ${TOOL.cov.lng.toFixed(6)}`;
}
function setCoveragePoint(lat, lng) {
  if (TOOL.pick === "cov-point") cancelPick();
  const tp = $id("tool-panel");
  if (tp && tp.classList) tp.classList.remove("collapsed");
  TOOL.cov.lat = lat;
  TOOL.cov.lng = lng;
  renderCoveragePointText();
  runCoverage();
}
function rerunCoverage() {
  if (TOOL.cov.lat != null) runCoverage();
}
function coverageAtSearch() {
  if (!searchHighlightMarker) return;
  const ll = searchHighlightMarker.getLatLng();
  map.closePopup();
  openToolPanel("coverage");
  setCoveragePoint(ll.lat, ll.lng);
}
function setCovVerdict(text, cls) {
  const el = $id("cov-verdict");
  if (!el) return;
  el.style.display = text ? "block" : "none";
  el.className = "tp-verdict " + (cls || "");
  el.textContent = text || "";
}
function runCoverage() {
  const seq = ++TOOL.cov.seq;
  const radius = Number($id("cov-radius").value) || 500;
  setCovVerdict(
    "Memeriksa ODP, closure, dan slack di sekitar titik...",
    "info",
  );
  $id("cov-list").innerHTML = "";
  apiRequest(
    `/api/coverage?lat=${TOOL.cov.lat}&lng=${TOOL.cov.lng}&radius=${radius}&limit=8&route=1`,
  )
    .then((d) => {
      if (seq !== TOOL.cov.seq) return;
      TOOL.cov.data = d;
      renderCoverage(d);
    })
    .catch((err) => {
      if (seq !== TOOL.cov.seq) return;
      setCovVerdict("Gagal memeriksa coverage: " + err.message, "bad");
    });
}
function covChip(c) {
  if (!c.eligible)
    return `<span class="cov-chip bad">${c.status === "Cut/Broken" ? "Putus" : /terpakai/i.test(c.reason || "") ? "Penuh" : "Tidak tersedia"}</span>`;
  if (c.status === "Maintenance")
    return `<span class="cov-chip warn">Layak - Maintenance</span>`;
  return c.in_range
    ? `<span class="cov-chip ok">Layak - dalam jangkauan</span>`
    : `<span class="cov-chip warn">Layak - di luar jangkauan</span>`;
}
function renderCoverage(d) {
  clearToolLayer();
  const ll = [d.point.latitude, d.point.longitude];
  toolLayer.addLayer(
    L.circle(ll, {
      radius: d.radius_m,
      color: "#2563eb",
      weight: 1.5,
      fillColor: "#3b82f6",
      fillOpacity: 0.07,
      dashArray: "4 4",
      interactive: false,
    }),
  );
  toolLayer.addLayer(
    L.marker(ll, { icon: toolPinIcon("pin-target", ""), interactive: false }),
  );
  d.candidates.forEach((c, i) => {
    if (c.route_coords && c.route_coords.length > 1) {
      const best = c.node_id === d.best_node_id;
      toolLayer.addLayer(
        L.polyline(
          c.route_coords.map((p) => [p[1], p[0]]),
          {
            color: best ? "#16a34a" : "#64748b",
            weight: best ? 4 : 3,
            dashArray: "8 6",
            opacity: 0.9,
            interactive: false,
          },
        ),
      );
    }
  });
  setCovVerdict(
    d.message,
    d.covered ? "ok" : d.nearest_eligible_node_id ? "warn" : "bad",
  );
  const planAllowed = can("plan.write");
  $id("cov-list").innerHTML = d.candidates.length
    ? d.candidates
        .map((c, i) => {
          const dist =
            c.route_m != null
              ? `&asymp;${fmtM(c.route_m)} via jalan <small>(garis lurus ${fmtM(c.distance_m)})</small>`
              : `${fmtM(c.distance_m)} <small>garis lurus</small>`;
          const kind = `${escapeHtml(TYPE_LABEL[c.type] || c.type)}${c.type === "SLACK" && c.at_cable_end ? " &middot; ujung kabel" : ""}`;
          const cables = (c.cables || []).length
            ? `<div class="cov-meta">Kabel: ${c.cables.map((k) => `${escapeHtml(k.name)} (${k.free}/${k.total} core kosong)`).join(", ")}</div>`
            : "";
          const sep =
            !c.eligible && (i === 0 || d.candidates[i - 1].eligible)
              ? `<div class="cov-sep">Aset terdekat yang tidak bisa dipakai <small>(ditampilkan agar tidak terlewat)</small></div>`
              : "";
          return (
            sep +
            `<div class="cov-card ${c.eligible ? (c.in_range ? "ok" : "far") : "bad"}">
          <div class="cov-main"><span class="cov-dot t-${escapeHtml(c.type)}"></span><b>${escapeHtml(c.name)}</b><span class="cov-type">${kind}</span></div>
          <div class="cov-chips">${covChip(c)}</div>
          <div class="cov-meta">${c.eligible ? escapeHtml(c.detail) : escapeHtml(c.reason || "")}</div>
          ${c.eligible && c.needs_closure ? `<div class="cov-meta"><b>Slack bukan titik sambung:</b> perlu pemasangan closure + ODP baru${c.slack_deferred ? " (ada ODP/Closure layak lebih diutamakan di sekitar)" : ""}.</div>` : ""}
          ${c.eligible ? cables : ""}
          <div class="cov-meta">${dist}${c.eligible ? ` &middot; <b>${escapeHtml(c.suggested_cable)}</b>` : ""}</div>
          <div class="cov-act">
            <button type="button" onclick="focusCoverageCandidate(${i})"><i class="fa-solid fa-crosshairs"></i> Lihat</button>
            ${c.eligible && planAllowed ? `<button type="button" class="primary" onclick="planFromCandidate(${i})"><i class="fa-solid fa-route"></i> Buat rencana</button>` : ""}
          </div></div>`
          );
        })
        .join("")
    : "";
  if (d.candidates.length) {
    const pts = [ll].concat(
      d.candidates
        .filter((c) => c.eligible)
        .slice(0, 3)
        .map((c) => [c.latitude, c.longitude]),
    );
    if (pts.length > 1) map.fitBounds(pts, { padding: [60, 60], maxZoom: 18 });
    else map.setView(ll, 17);
  } else {
    map.setView(ll, 16);
  }
}
function focusCoverageCandidate(i) {
  const d = TOOL.cov.data;
  const c = d && d.candidates[i];
  if (!c) return;
  map.fitBounds(
    [
      [d.point.latitude, d.point.longitude],
      [c.latitude, c.longitude],
    ],
    { padding: [70, 70], maxZoom: 19 },
  );
  setTimeout(() => openAssetPopup(`node:${c.node_id}`), 500);
}
function planFromCandidate(i) {
  const d = TOOL.cov.data;
  const c = d && d.candidates[i];
  if (!c) return;
  openPlanPanel();
  setPlanOrigin({
    id: c.node_id,
    name: c.name,
    type: c.type,
    lat: c.latitude,
    lng: c.longitude,
  });
  setPlanDest(d.point.latitude, d.point.longitude);
  runPlanPreview();
}

// ---------- PASANG BARU ----------
function openPlanPanel() {
  if (!can("plan.write")) return;
  openToolPanel("plan");
  fillOriginList();
  loadPlanRules();
  loadSavedPlans();
  const psl = $id("plan-svc-list");
  if (psl && !psl.innerHTML)
    psl.innerHTML = ASSET_SERVICES.map(
      (x) => `<option value="${escapeHtml(x)}">`,
    ).join("");
  if (typeof onPlanServiceChange === "function") onPlanServiceChange();
}
function planToSearch() {
  if (!searchHighlightMarker || !can("plan.write")) return;
  const ll = searchHighlightMarker.getLatLng();
  map.closePopup();
  openPlanPanel();
  setPlanDest(ll.lat, ll.lng);
}
function originCandidates() {
  return allInventoryData.filter(
    (x) =>
      x.category === "NODE" &&
      ORIGIN_TYPES.includes((x.type || "").toUpperCase()) &&
      Number.isFinite(x.lat) &&
      Number.isFinite(x.lng),
  );
}
function fillOriginList() {
  const dl = $id("plan-origin-list");
  if (!dl) return;
  dl.innerHTML = originCandidates()
    .map(
      (n) =>
        `<option value="${escapeHtml(n.name)}">${escapeHtml(TYPE_LABEL[n.type] || n.type)}</option>`,
    )
    .join("");
}
function onPlanOriginInput() {
  const v = ($id("plan-origin-input").value || "").trim().toLowerCase();
  if (!v) return;
  const hit = originCandidates().find(
    (n) => (n.name || "").toLowerCase() === v,
  );
  if (hit) setPlanOrigin(hit);
}
function pickPlanOriginAt(lat, lng, nodeId) {
  let hit = null;
  if (nodeId != null)
    hit = originCandidates().find((n) => String(n.id) === String(nodeId));
  if (!hit) {
    const here = L.latLng(lat, lng);
    hit = originCandidates()
      .map((n) => ({ n, d: here.distanceTo(L.latLng(n.lat, n.lng)) }))
      .filter((r) => r.d <= 60)
      .sort((a, b) => a.d - b.d)
      .map((r) => r.n)[0];
  }
  if (!hit)
    return setPlanMsg(
      "Tidak ada ODP / Closure / Slack / POP dalam 60 m dari titik yang diklik. Coba klik tepat pada ikonnya.",
      "warn",
    );
  setPlanMsg("", "");
  setPlanOrigin(hit);
}
function setPlanOrigin(n) {
  TOOL.plan.origin = {
    id: n.id,
    name: n.name,
    type: n.type,
    lat: n.lat,
    lng: n.lng,
  };
  PLANOTB.picked = [];
  $id("plan-origin-input").value = n.name;
  $id("plan-origin-chip").textContent =
    `${n.name} (${TYPE_LABEL[n.type] || n.type})`;
  planOtbRefresh();
  planDirty();
  drawPlanEndpoints();
}
function setPlanDest(lat, lng) {
  TOOL.plan.dest = { lat, lng };
  $id("plan-dest-chip").textContent =
    `Pelanggan: ${lat.toFixed(6)}, ${lng.toFixed(6)}`;
  planDirty();
  drawPlanEndpoints();
}
function addPlanVia(lat, lng) {
  if (TOOL.plan.via.length >= 8)
    return setPlanMsg("Maksimal 8 titik singgah", "warn");
  TOOL.plan.via.push([lat, lng]);
  renderPlanVia();
  planDirty();
  if (TOOL.plan.result) runPlanPreview();
  else drawPlanEndpoints();
}
function removePlanVia(i) {
  TOOL.plan.via.splice(i, 1);
  renderPlanVia();
  planDirty();
  if (TOOL.plan.result) runPlanPreview();
  else drawPlanEndpoints();
}
function renderPlanVia() {
  const el = $id("plan-via-list");
  if (!el) return;
  el.innerHTML = TOOL.plan.via.length
    ? TOOL.plan.via
        .map(
          (v, i) =>
            `<span class="tp-via"><b>${i + 1}</b> ${v[0].toFixed(5)}, ${v[1].toFixed(5)} <button type="button" onclick="removePlanVia(${i})" title="Hapus titik singgah" aria-label="Hapus titik singgah ${i + 1}">&times;</button></span>`,
        )
        .join("")
    : '<span class="tp-muted">Tidak ada. Jalur mengikuti rute jalan tercepat.</span>';
}
function planInstallation() {
  const r = document.querySelectorAll
    ? document.querySelectorAll('input[name="plan-inst"]')
    : [];
  for (const x of r || []) if (x.checked) return x.value;
  return TOOL.plan.installation || "Udara";
}
function planDirty() {
  TOOL.plan.id = null;
  TOOL.plan.boqId = null;
  TOOL.plan.status = null;
  const had = !!TOOL.plan.result;
  if (had) TOOL.plan.stale = true;
  // Tombol Simpan rencana / Unduh PDF selalu ada; bila hasil usang, keduanya menghitung ulang otomatis lebih dulu.
  const act = $id("plan-actions");
  if (act) act.style.display = "flex";
  const sv = $id("plan-save-btn");
  if (sv) sv.style.display = "";
  const rl = $id("plan-realize-btn");
  if (rl) rl.style.display = "none";
  if (had)
    setPlanMsg(
      'Parameter berubah. Klik "Hitung & gambar rute", atau langsung Simpan rencana / Unduh PDF (dihitung ulang otomatis).',
      "info",
    );
}
// Data pelanggan (nama, layanan, bandwidth, SN, kode registrasi) tidak mengubah rute/BOQ: hasil hitung & tombol Simpan/PDF tetap.
// Hanya rencana yang sudah tersimpan tidak lagi cocok, jadi Simpan membuat draft baru dan PDF memakai data terkini.
function planCustDirty() {
  const p = TOOL.plan;
  if (!p.result) return planDirty();
  if (p.id != null || p.boqId != null) {
    p.id = null;
    p.boqId = null;
    p.status = null;
    const sv = $id("plan-save-btn");
    if (sv) sv.style.display = "";
    const rl = $id("plan-realize-btn");
    if (rl) rl.style.display = "none";
    setPlanMsg(
      "Data pelanggan diubah; klik Simpan rencana untuk menyimpan sebagai draft baru.",
      "info",
    );
  }
}
function setPlanMsg(text, cls) {
  const el = $id("plan-msg");
  if (!el) return;
  el.style.display = text ? "block" : "none";
  el.className = "tp-verdict " + (cls || "");
  el.textContent = text || "";
}

// Aturan perencanaan
function tierRowsHtml(tiers) {
  return tiers
    .map((t, i) => {
      const last = i === tiers.length - 1;
      return `<div class="tier-row">
      <span class="tier-lt">${last ? "selebihnya" : "&lt;"}</span>
      <input type="number" id="tier-max-${i}" min="1" step="50" value="${t.max_m == null ? "" : t.max_m}" ${last ? "disabled" : ""} oninput="planDirty()" aria-label="Batas panjang tingkat ${i + 1} (m)" />
      <span class="tier-m">${last ? "" : "m"}</span>
      <select id="tier-type-${i}" onchange="planDirty()" aria-label="Jenis kabel tingkat ${i + 1}">${CABLE_TYPES.map((c) => `<option value="${c}" ${c === t.type ? "selected" : ""}>${c}</option>`).join("")}</select>
      <input type="text" id="tier-cap-${i}" value="${escapeHtml(t.capacity)}" maxlength="5" oninput="planDirty()" aria-label="Kapasitas tingkat ${i + 1}" />
    </div>`;
    })
    .join("");
}
function fillRuleInputs(r) {
  TOOL.plan.rules = r;
  $id("rule-pole").value = r.pole_spacing_m;
  if ($id("rule-dropnew")) $id("rule-dropnew").checked = !!r.drop_new_poles;
  $id("rule-hh").value = r.hh_spacing_m;
  $id("rule-slack-len").value = r.slack_length_m;
  $id("rule-slack-n").value = r.slack_count;
  $id("rule-tiers").innerHTML = tierRowsHtml(r.cable_tiers);
}
function readRuleInputs() {
  const base = TOOL.plan.rules || TOOL.plan.defaults;
  if (!base) return null; // aturan belum termuat: server memakai aturan bawaan
  const num = (id) => {
    const v = parseFloat($id(id).value);
    return Number.isFinite(v) ? v : undefined;
  };
  const tiers = base.cable_tiers.map((t, i) => {
    const gv = (k) => {
      const el = $id(`tier-${k}-${i}`);
      return el ? el.value : "";
    };
    const mx = parseFloat(gv("max"));
    return {
      max_m:
        i === base.cable_tiers.length - 1
          ? null
          : Number.isFinite(mx)
            ? mx
            : t.max_m,
      type: gv("type") || t.type,
      capacity: (gv("cap") || t.capacity).trim().toUpperCase(),
      label: t.label,
    };
  });
  return {
    pole_spacing_m: num("rule-pole"),
    hh_spacing_m: num("rule-hh"),
    slack_length_m: num("rule-slack-len"),
    slack_count: num("rule-slack-n"),
    drop_new_poles: !!($id("rule-dropnew") || {}).checked,
    cable_tiers: tiers,
  };
}
function loadPlanRules() {
  apiRequest("/api/plan/rules")
    .then((d) => {
      TOOL.plan.defaults = d.defaults;
      fillRuleInputs(d.rules);
      const b = $id("plan-rules-save");
      if (b) b.style.display = can("plan.rules") ? "" : "none";
    })
    .catch((err) => setPlanMsg("Gagal memuat aturan: " + err.message, "bad"));
}
function resetPlanRules() {
  if (TOOL.plan.defaults) {
    fillRuleInputs(JSON.parse(JSON.stringify(TOOL.plan.defaults)));
    planDirty();
  }
}
function savePlanRulesDefault() {
  if (
    !confirm("Simpan aturan ini sebagai bawaan untuk semua rencana berikutnya?")
  )
    return;
  apiRequest("/api/plan/rules", "PUT", { rules: readRuleInputs() })
    .then((d) => {
      fillRuleInputs(d.rules);
      alert("Aturan bawaan disimpan.");
    })
    .catch((err) => alert("Gagal menyimpan aturan: " + err.message));
}

// Gambar di peta
function drawPlanEndpoints() {
  const p = TOOL.plan;
  if (p.result) return; // rute sudah tergambar; endpoint ikut digambar ulang saat hitung
  toolLayer.clearLayers();
  p.viaMarkers = [];
  if (p.origin)
    toolLayer.addLayer(
      L.marker([p.origin.lat, p.origin.lng], {
        icon: toolPinIcon("pin-origin", "A"),
        title: "Titik asal (A)",
        interactive: false,
      }),
    );
  if (p.dest)
    toolLayer.addLayer(
      L.marker([p.dest.lat, p.dest.lng], {
        icon: toolPinIcon("pin-dest", "B"),
        title: "Lokasi pelanggan (B)",
        interactive: false,
      }),
    );
  p.via.forEach((v, i) => addViaMarker(v, i));
  const pts = [
    p.origin && [p.origin.lat, p.origin.lng],
    p.dest && [p.dest.lat, p.dest.lng],
  ].filter(Boolean);
  if (pts.length === 2) map.fitBounds(pts, { padding: [70, 70], maxZoom: 18 });
  else if (pts.length === 1) map.setView(pts[0], 17);
}
function addViaMarker(v, i) {
  const m = L.marker(v, {
    icon: toolPinIcon("pin-via", String(i + 1)),
    draggable: true,
    title: `Titik singgah ${i + 1} (seret untuk memindah)`,
  });
  m.on("dragend", () => {
    const ll = m.getLatLng();
    TOOL.plan.via[i] = [ll.lat, ll.lng];
    renderPlanVia();
    planDirty();
    clearTimeout(TOOL.plan.timer);
    TOOL.plan.timer = setTimeout(() => runPlanPreview(), 400);
  });
  toolLayer.addLayer(m);
  (TOOL.plan.viaMarkers = TOOL.plan.viaMarkers || []).push(m);
}
// ---------- Mode jalur: ikuti jalan / garis lurus / edit manual ----------
const PLAN_MAX_BENDS = 30;
function planRMode() {
  return TOOL.plan.rmode || "road";
}
function planMetric(lat0) {
  const k = Math.cos((lat0 * Math.PI) / 180);
  return (p) => [p[1] * 111320 * k, p[0] * 110540];
}
// Sederhanakan polyline [[lng,lat],...] menjadi titik belokan (di luar ujung) maksimal maxPts (Douglas-Peucker)
function planSimplifyPath(coords, maxPts) {
  const pts = (coords || []).map((c) => [c[1], c[0]]);
  if (pts.length <= 2) return [];
  const M = planMetric(pts[0][0]);
  const xy = pts.map(M);
  const rdp = (tol) => {
    const keep = new Set([0, pts.length - 1]);
    const stack = [[0, pts.length - 1]];
    while (stack.length) {
      const [a, b] = stack.pop();
      let dmax = 0,
        idx = -1;
      const ax = xy[a][0],
        ay = xy[a][1],
        bx = xy[b][0],
        by = xy[b][1];
      const dx = bx - ax,
        dy = by - ay,
        len2 = dx * dx + dy * dy || 1;
      for (let i = a + 1; i < b; i++) {
        const t = Math.max(
          0,
          Math.min(1, ((xy[i][0] - ax) * dx + (xy[i][1] - ay) * dy) / len2),
        );
        const d = Math.hypot(xy[i][0] - ax - t * dx, xy[i][1] - ay - t * dy);
        if (d > dmax) {
          dmax = d;
          idx = i;
        }
      }
      if (idx >= 0 && dmax > tol) {
        keep.add(idx);
        stack.push([a, idx], [idx, b]);
      }
    }
    return Array.from(keep).sort((x, y) => x - y);
  };
  let tol = 2,
    keep = rdp(tol);
  for (let it = 0; it < 40 && keep.length - 2 > maxPts; it++) {
    tol *= 1.6;
    keep = rdp(tol);
  }
  return keep.slice(1, -1).map((i) => [pts[i][0], pts[i][1]]);
}
function planModeUi() {
  const m = planRMode();
  const box = $id("plan-edit-box");
  if (box) box.style.display = m === "custom" ? "" : "none";
  const via = $id("plan-via-field");
  if (via) via.style.display = m === "custom" ? "none" : "";
  const cnt = $id("plan-bend-count");
  if (cnt)
    cnt.textContent = `${TOOL.plan.bends.length} dari ${PLAN_MAX_BENDS} titik belokan`;
  const rs = document.querySelectorAll
    ? document.querySelectorAll('input[name="plan-rmode"]')
    : [];
  Array.from(rs || []).forEach((x) => {
    x.checked = x.value === m;
  });
}
function planSetMode(m, opts) {
  const p = TOOL.plan;
  const prev = planRMode();
  if (m === prev) return planModeUi();
  p.rmode = m;
  if (m === "custom") {
    // titik awal edit = jalur yang sedang tampil (disederhanakan), supaya tinggal digeser
    p.bends =
      p.result && p.result.route
        ? planSimplifyPath(p.result.route.coords, PLAN_MAX_BENDS)
        : [];
  } else {
    p.bends = [];
  }
  planModeUi();
  planDirty();
  if (p.origin && p.dest && !(opts && opts.noRun)) return runPlanPreview();
  if (!p.origin || !p.dest)
    setPlanMsg(
      "Mode jalur disimpan. Tentukan aset asal dan lokasi pelanggan, lalu klik Hitung.",
      "info",
    );
}
function planRouteModeChanged() {
  return planSetMode(checkedRadio("plan-rmode", "road"));
}
function planBendsClear() {
  TOOL.plan.bends = [];
  planModeUi();
  planDirty();
  if (TOOL.plan.origin && TOOL.plan.dest) return runPlanPreview();
}
function planBendsAfterEdit() {
  planModeUi();
  planDirty();
  clearTimeout(TOOL.plan.timer);
  TOOL.plan.keepView = true; // jangan geser/zoom peta saat menyunting jalur
  TOOL.plan.timer = setTimeout(() => {
    Promise.resolve(runPlanPreview()).finally(() => {
      TOOL.plan.keepView = false;
    });
  }, 350);
}
function planBendRemove(i) {
  TOOL.plan.bends.splice(i, 1);
  planBendsAfterEdit();
}
function planBendMove(i, lat, lng) {
  TOOL.plan.bends[i] = [lat, lng];
  planBendsAfterEdit();
}
// Sisipkan titik belokan pada ruas jalur terdekat dari titik klik
function planBendInsert(lat, lng) {
  const p = TOOL.plan;
  if (!p.origin || !p.dest) return;
  if (p.bends.length >= PLAN_MAX_BENDS)
    return setPlanMsg(`Maksimal ${PLAN_MAX_BENDS} titik belokan`, "warn");
  const path = [[p.origin.lat, p.origin.lng]].concat(p.bends, [
    [p.dest.lat, p.dest.lng],
  ]);
  const M = planMetric(lat);
  const c = M([lat, lng]);
  let best = 0,
    bd = Infinity;
  for (let j = 0; j < path.length - 1; j++) {
    const a = M(path[j]),
      b = M(path[j + 1]);
    const dx = b[0] - a[0],
      dy = b[1] - a[1],
      len2 = dx * dx + dy * dy || 1;
    const t = Math.max(
      0,
      Math.min(1, ((c[0] - a[0]) * dx + (c[1] - a[1]) * dy) / len2),
    );
    const d = Math.hypot(c[0] - a[0] - t * dx, c[1] - a[1] - t * dy);
    if (d < bd) {
      bd = d;
      best = j;
    }
  }
  p.bends.splice(best, 0, [lat, lng]);
  planBendsAfterEdit();
}
function addBendMarker(v, i) {
  const m = L.marker(v, {
    icon: L.divIcon({
      className: "plan-bend",
      html: "<i></i>",
      iconSize: [16, 16],
      iconAnchor: [8, 8],
    }),
    draggable: true,
    title: `Titik belokan ${i + 1}: seret untuk memindah, klik untuk menghapus`,
  });
  m.on("dragend", () => {
    const ll = m.getLatLng();
    planBendMove(i, ll.lat, ll.lng);
  });
  m.on("click", () => planBendRemove(i));
  toolLayer.addLayer(m);
}
function drawPlanResult(d) {
  toolLayer.clearLayers();
  TOOL.plan.viaMarkers = [];
  const custom = planRMode() === "custom";
  const latlngs = d.route.coords.map((c) => [c[1], c[0]]);
  const segs = d.segments && d.segments.length > 1 ? d.segments : null;
  if (segs) {
    segs.forEach((sg, i) =>
      toolLayer.addLayer(
        L.polyline(
          sg.coords.map((c) => [c[1], c[0]]),
          {
            color: i === 0 ? "#7c3aed" : "#0ea5e9",
            weight: 5,
            opacity: 0.85,
            dashArray: custom
              ? null
              : d.route.source === "osrm"
                ? "10 7"
                : "3 9",
            interactive: custom,
          },
        ).on("click", (e) => {
          if (custom && e.latlng) planBendInsert(e.latlng.lat, e.latlng.lng);
        }),
      ),
    );
  } else {
    toolLayer.addLayer(
      L.polyline(latlngs, {
        color: "#7c3aed",
        weight: 5,
        opacity: 0.85,
        dashArray: custom ? null : d.route.source === "osrm" ? "10 7" : "3 9",
        interactive: custom,
      }).on("click", (e) => {
        if (custom && e.latlng) planBendInsert(e.latlng.lat, e.latlng.lng);
      }),
    );
  }
  if (d.hub)
    toolLayer.addLayer(
      L.marker([d.hub.latitude, d.hub.longitude], {
        icon: toolPinIcon("pin-hub", d.hub.odp ? "O" : "C"),
        title: d.hub.odp ? "Closure + ODP baru (hub)" : "Closure baru (hub)",
        interactive: false,
      }),
    );
  d.assets.forEach((a) => {
    const ll = [a.latitude, a.longitude];
    if (a.kind === "SLACK")
      toolLayer.addLayer(
        L.marker(ll, {
          icon: toolPinIcon("pin-slack", "S"),
          title: `Slack @ ${Math.round(a.distance_m)} m`,
          interactive: false,
        }),
      );
    else
      toolLayer.addLayer(
        L.circleMarker(ll, {
          radius: 5,
          color: a.existing_id ? "#16a34a" : "#1e293b",
          weight: 2,
          fillColor: a.existing_id ? "#bbf7d0" : "#f8fafc",
          fillOpacity: 1,
          interactive: false,
        }),
      );
  });
  toolLayer.addLayer(
    L.marker([d.origin.lat, d.origin.lng], {
      icon: toolPinIcon("pin-origin", "A"),
      title: "Titik asal (A)",
      interactive: false,
    }),
  );
  toolLayer.addLayer(
    L.marker([d.dest.lat, d.dest.lng], {
      icon: toolPinIcon("pin-dest", "B"),
      title: "Lokasi pelanggan (B)",
      interactive: false,
    }),
  );
  if (custom) TOOL.plan.bends.forEach((v, i) => addBendMarker(v, i));
  else TOOL.plan.via.forEach((v, i) => addViaMarker(v, i));
  if (!(custom && TOOL.plan.keepView))
    map.fitBounds(latlngs, { padding: [70, 70], maxZoom: 18 });
}
function checkedRadio(name, fallback) {
  const r = document.querySelectorAll
    ? document.querySelectorAll(`input[name="${name}"]`)
    : [];
  for (const x of r || []) if (x.checked) return x.value;
  return fallback;
}
function planTerm() {
  return checkedRadio("plan-term", "DROPCORE_ROSET");
}
function planHubMode() {
  return checkedRadio("plan-hubmode", "NEAREST");
}
// --- Port OTB POP asal: otomatis (usulan) atau manual (pilih sendiri) ---
const PLANOTB = { mode: "AUTO", picked: [], data: null, seq: 0 };
const OTB_STATE_LABEL = {
  free: "kosong",
  in: "sudah ada kabel masuk",
  out: "kabel keluar terpakai",
  full: "masuk + keluar terpakai",
};
function planOtbIsPop() {
  const o = TOOL.plan && TOOL.plan.origin;
  return !!o && String(o.type || "").toUpperCase() === "POP";
}
function planOtbCores() {
  return Number(($id("plan-cores") || { value: 1 }).value) || 1;
}
function planOtbRefresh() {
  const box = $id("plan-otb-box");
  if (!box) return;
  if (!planOtbIsPop()) {
    box.style.display = "none";
    PLANOTB.data = null;
    PLANOTB.picked = [];
    return;
  }
  box.style.display = "";
  const o = TOOL.plan.origin,
    seq = ++PLANOTB.seq;
  return apiRequest(`/api/nodes/${Number(o.id)}/otb-ports?n=${planOtbCores()}`)
    .then((d) => {
      if (seq !== PLANOTB.seq) return;
      PLANOTB.data = d;
      planOtbRender();
    })
    .catch((e) => {
      const l = $id("plan-otb-list");
      if (l)
        l.innerHTML = `<span class="tp-muted">Gagal memuat port OTB: ${escapeHtml(e.message)}</span>`;
    });
}
function planOtbMode() {
  const r = document.querySelectorAll
    ? document.querySelectorAll('input[name="plan-otbmode"]')
    : [];
  PLANOTB.mode = "AUTO";
  for (const x of r || []) if (x.checked) PLANOTB.mode = x.value;
  if (PLANOTB.mode === "MANUAL" && !PLANOTB.picked.length && PLANOTB.data)
    PLANOTB.picked = (PLANOTB.data.suggest || []).slice(0, planOtbCores());
  planOtbRender();
  planDirty();
}
function planOtbToggle(port) {
  const need = planOtbCores(),
    i = PLANOTB.picked.indexOf(port);
  if (i >= 0) PLANOTB.picked.splice(i, 1);
  else {
    if (PLANOTB.picked.length >= need) PLANOTB.picked.shift();
    PLANOTB.picked.push(port);
  }
  planOtbRender();
  planDirty();
}
function planOtbShort(l) {
  return String(l).replace(/^OTB-(\d+)\s*\/\s*P(\d+)$/i, (m, a, b) => `P${b}`);
}
function planOtbRender() {
  const list = $id("plan-otb-list"),
    hint = $id("plan-otb-hint"),
    d = PLANOTB.data;
  if (!list || !d) return;
  const need = planOtbCores();
  if (PLANOTB.mode !== "MANUAL") {
    const dev = (p) => {
      const x = d.ports.find((y) => y.port === p);
      return x && x.device
        ? ` &middot; ${escapeHtml(x.device.device_name)}${x.device.slot ? " slot " + escapeHtml(x.device.slot) : ""}`
        : "";
    };
    list.innerHTML =
      d.suggest && d.suggest.length
        ? `<div class="otb-auto">Usulan: ${d.suggest.map((p) => `<b>${escapeHtml(p)}</b>${dev(p)}`).join("<br>")}</div>`
        : `<div class="otb-auto bad">${escapeHtml(d.suggest_error || "Tidak ada port yang bisa dipilih otomatis")}</div>`;
    hint.innerHTML =
      "Port kosong dengan perangkat PON/OLT di depan didahulukan; port yang dicadangkan PTP/uplink dilewati. Port yang hanya menerima kabel masuk dipakai paling akhir dengan peringatan.";
    return;
  }
  const groups = {};
  d.ports.forEach((x) => {
    const k = (/^OTB-(\d+)/i.exec(x.port) || [0, 1])[1];
    (groups[k] = groups[k] || []).push(x);
  });
  list.innerHTML = Object.keys(groups)
    .map(
      (k) =>
        `<div class="otb-pick-g"><b>OTB-${k}</b><div class="otb-pick-grid">` +
        groups[k]
          .map((x) => {
            const blocked = x.state === "out" || x.state === "full",
              sel = PLANOTB.picked.includes(x.port);
            const dv = x.device
              ? ` | depan: ${x.device.device_name}${x.device.slot ? " slot " + x.device.slot : ""} (${x.device.purpose})`
              : "";
            return `<button type="button" class="otb-pk st-${x.state}${sel ? " sel" : ""}${x.device ? " dev" : ""}"${blocked ? " disabled" : ""} onclick="planOtbToggle('${escapeHtml(x.port)}')" title="${escapeHtml(x.port + " | " + OTB_STATE_LABEL[x.state] + dv)}">${escapeHtml(planOtbShort(x.port))}</button>`;
          })
          .join("") +
        "</div></div>",
    )
    .join("");
  const ok = PLANOTB.picked.length === need;
  hint.innerHTML =
    `Dipilih <b class="${ok ? "ok" : "bad"}">${PLANOTB.picked.length}/${need}</b> port: ${PLANOTB.picked.map(escapeHtml).join(", ") || "-"}.` +
    ` <span class="otb-lg"><i class="st-free"></i>kosong <i class="st-in"></i>masuk <i class="st-out"></i>terpakai <i class="dev"></i>ada perangkat</span>`;
}
function planOtbPayload() {
  return {
    pop_ports:
      planOtbIsPop() && PLANOTB.mode === "MANUAL" && PLANOTB.picked.length
        ? PLANOTB.picked.slice()
        : null,
  };
}
function planOtbResultHtml(o) {
  const pp = o && o.port_plan;
  if (!pp) return "";
  const mode = pp.mode === "MANUAL" ? "manual" : "otomatis";
  return (
    `<div class="pr-cust"><i class="fa-solid fa-grip"></i> Port OTB (${mode}): ` +
    (pp.ports && pp.ports.length
      ? pp.ports.map((p) => `<b>${escapeHtml(p)}</b>`).join(", ")
      : `<span class="bad">${escapeHtml(pp.error || "belum ada")}</span>`) +
    `</div>`
  );
}
function onPlanServiceChange() {
  const cores = Number(($id("plan-cores") || { value: 1 }).value);
  if (PLANOTB.picked.length > cores)
    PLANOTB.picked = PLANOTB.picked.slice(0, cores);
  planOtbRefresh();
  const scen = ($id("plan-scen") || { value: "AUTO" }).value;
  const hubBox = $id("plan-hub-box");
  if (hubBox) hubBox.style.display = scen === "DIRECT" ? "none" : "block";
  const pick = $id("plan-hub-pick");
  if (pick)
    pick.style.display =
      planHubMode() === "MAP" && scen !== "DIRECT" ? "block" : "none";
  const spl = $id("plan-splitter-row");
  const odpOk = cores === 1 && planTerm() === "DROPCORE_ROSET";
  if (spl) spl.style.display = odpOk ? "block" : "none";
  const hint = $id("plan-service-hint");
  if (hint) {
    hint.textContent =
      cores >= 2
        ? `Layanan ${cores} core dedicated (Tx-Rx): diambil langsung dari closure, tidak bisa dari ODP (ODP hanya 1 core).`
        : "Layanan 1 core: bisa dari ODP. Bila jarak > batas dropcore, dibuat closure + ODP baru di hub.";
  }
  planDirty();
}
function setPlanHub(lat, lng) {
  TOOL.plan.hub = [lat, lng];
  const chip = $id("plan-hub-chip");
  if (chip) chip.textContent = `Hub: ${lat.toFixed(6)}, ${lng.toFixed(6)}`;
  planDirty();
  if (TOOL.plan.result && TOOL.plan.origin && TOOL.plan.dest) runPlanPreview();
}
function planCustToggle() {
  const box = $id("plan-cust-box");
  if (box) box.style.display = $id("plan-customer").checked ? "" : "none";
  planDirty();
}
function planNormCodes() {
  ["plan-sn", "plan-reg"].forEach((i) => {
    const e = $id(i);
    if (e) e.value = assetNormCode(e.value);
  });
}
function planCustPayload() {
  const v = (id) => (($id(id) || { value: "" }).value || "").trim();
  const bw = v("plan-bw") ? bwToMbps(v("plan-bw"), v("plan-bw-unit")) : null;
  return {
    cust_service: v("plan-svc") || null,
    cust_bw_mbps: bw,
    cust_sn: assetNormCode(v("plan-sn")) || null,
    cust_link_type: v("plan-linktype") || null,
    cust_reg_code: assetNormCode(v("plan-reg")) || null,
  };
}
function planCustFill(ci) {
  const set = (i, x) => {
    const e = $id(i);
    if (e) e.value = x == null ? "" : String(x);
  };
  ci = ci || {};
  set("plan-svc", ci.service);
  set("plan-linktype", ci.link_type);
  set("plan-sn", ci.device_sn);
  set("plan-reg", ci.reg_code);
  const b = bwSplit(ci.bandwidth_mbps);
  set("plan-bw", b.v);
  set("plan-bw-unit", b.u);
}
function planTrunkHtml(list) {
  return (list || [])
    .map((t) => {
      const cls = t.status_after || "ok";
      if (!t.trunk_mbps)
        return `<div class="pr-trunk trunk-unset"><b>Trunk ${escapeHtml(t.pop_name)}</b> belum diatur; beban setelah pelanggan ini ${escapeHtml(fmtBw(t.after_mbps))}.</div>`;
      return (
        `<div class="pr-trunk trunk-${escapeHtml(cls)}"><div class="pr-trunk-h"><b><i class="fa-solid fa-gauge-high"></i> Trunk ${escapeHtml(t.pop_name)}</b>` +
        `<span>${t.before_pct}% &rarr; <b>${t.after_pct}%</b></span></div>` +
        `<div class="trunk-bar"><i style="width:${Math.max(0, Math.min(100, t.after_pct))}%"></i></div>` +
        `<small>${escapeHtml(fmtBw(t.after_mbps))} dari ${escapeHtml(fmtBw(t.trunk_mbps))} setelah pelanggan ini${t.overbook > 1 ? ` (overbooking 1:${t.overbook})` : ""}</small></div>`
      );
    })
    .join("");
}
function planCustInfoHtml(ci) {
  if (!ci || !Object.keys(ci).length) return "";
  const rows = [
    ["Layanan", ci.service],
    ["Jenis", ci.link_type],
    ["Bandwidth", ci.bandwidth_mbps != null ? fmtBw(ci.bandwidth_mbps) : null],
    ["SN perangkat", ci.device_sn],
    ["Kode registrasi", ci.reg_code],
  ]
    .filter((r) => r[1])
    .map((r) => `<span>${r[0]}: <b>${escapeHtml(String(r[1]))}</b></span>`)
    .join("");
  return `<div class="pr-cust"><i class="fa-solid fa-user-tag"></i> ${rows}</div>`;
}
function planPayload() {
  const p = TOOL.plan;
  const scen = ($id("plan-scen") || { value: "AUTO" }).value;
  const hubMap = planHubMode() === "MAP" && scen !== "DIRECT";
  return {
    customer_cores: Number(($id("plan-cores") || { value: 1 }).value),
    termination: planTerm(),
    scenario: scen,
    hub_mode: hubMap ? "MAP" : "NEAREST",
    hub_lat: hubMap && p.hub ? p.hub[0] : null,
    hub_lng: hubMap && p.hub ? p.hub[1] : null,
    splitter: ($id("plan-splitter") || { value: "1:8" }).value,
    origin_type: p.origin ? "NODE" : "POINT",
    origin_id: p.origin ? p.origin.id : null,
    dest_lat: p.dest.lat,
    dest_lng: p.dest.lng,
    dest_name: ($id("plan-dest-name").value || "").trim() || null,
    remarks:
      (($id("plan-remarks") || { value: "" }).value || "").trim() || null,
    installation: planInstallation(),
    via: p.via,
    route_mode: p.rmode || "road",
    custom_path: p.rmode === "custom" ? p.bends : null,
    rules: readRuleInputs(),
    create_customer: !!$id("plan-customer").checked,
    use_poles: !!$id("plan-poles").checked,
    use_slack: !!$id("plan-slack").checked,
    ...planCustPayload(),
    ...planOtbPayload(),
  };
}
function runPlanPreview() {
  const p = TOOL.plan;
  if (!p.origin)
    return setPlanMsg(
      "Pilih aset asal terlebih dahulu (ketik nama atau klik di peta).",
      "warn",
    );
  if (!p.dest)
    return setPlanMsg(
      "Tentukan lokasi pelanggan di peta terlebih dahulu.",
      "warn",
    );
  const seq = ++p.seq;
  setPlanMsg("Menghitung rute jalan dan kebutuhan aset...", "info");
  const btn = $id("plan-calc-btn");
  if (btn) btn.disabled = true;
  return apiRequest("/api/plan/preview", "POST", planPayload())
    .then((d) => {
      if (seq !== p.seq) return;
      p.result = d;
      p.stale = false;
      p.id = null;
      p.boqId = null;
      p.status = null;
      setPlanMsg("", "");
      renderPlanResult(d);
      drawPlanResult(d);
      boqReset(true);
      boqCalc();
      $id("plan-actions").style.display = "flex";
      $id("plan-save-btn").style.display = "";
      $id("plan-realize-btn").style.display = "none";
    })
    .catch((err) => {
      if (seq === p.seq) setPlanMsg("Gagal menghitung: " + err.message, "bad");
    })
    .finally(() => {
      if (seq === p.seq && btn) btn.disabled = false;
    });
}
function renderPlanResult(d) {
  const s = d.summary;
  const passive =
    s.use_poles === false
      ? `<div class="pr-stat"><b>&ndash;</b><span>Tanpa ${s.installation === "Udara" ? "tiang" : "handhole"} baru</span></div>`
      : s.installation === "Udara"
        ? `<div class="pr-stat"><b>${s.poles_new}</b><span>Tiang baru${s.poles_existing ? ` <small>(+${s.poles_existing} dipakai ulang)</small>` : ""}</span></div>`
        : `<div class="pr-stat"><b>${s.hh_new}</b><span>Handhole baru${s.hh_existing ? ` <small>(+${s.hh_existing} dipakai ulang)</small>` : ""}</span></div>`;
  const slackStat =
    s.use_slack === false
      ? `<div class="pr-stat"><b>&ndash;</b><span>Tanpa slack</span></div>`
      : `<div class="pr-stat"><b>${s.slack_count}</b><span>Slack &times; ${s.slack_length_m} m</span></div>`;
  $id("plan-result").innerHTML = `
    <div class="pr-card">
      <div class="pr-head"><span class="pr-type t-${escapeHtml(s.cable_type)}">${escapeHtml(d.cable.label)}</span>
        <span class="pr-cap">${escapeHtml(d.cable.capacity)} &middot; ${s.installation === "Udara" ? "Kabel Udara" : "Kabel Tanah"}</span></div>
      <div class="pr-grid">
        <div class="pr-stat"><b>${fmtM(s.route_length_m)}</b><span>Panjang rute${s.route_source === "osrm" ? " (jalan)" : s.route_source === "manual" ? " (disunting)" : " (garis lurus)"}</span></div>
        <div class="pr-stat"><b>${fmtM(s.cable_total_m)}</b><span>Total kabel <small>(+ slack ${fmtM(s.slack_total_m)})</small></span></div>
        ${passive}
        ${slackStat}
      </div>
      <table class="pr-boq"><thead><tr><th>Kebutuhan aset</th><th>Jumlah</th></tr></thead><tbody>
        ${d.boq_items.map((b) => `<tr><td>${escapeHtml(b.label)}</td><td class="num">${b.unit === "m" ? fmtM(b.qty) : `${b.qty} ${escapeHtml(b.unit)}`}</td></tr>`).join("")}
      </tbody></table>
      ${planOtbResultHtml(d.origin)}${planCustInfoHtml(d.customer_info)}${planTrunkHtml(d.trunk_check)}
      ${d.loss ? `<div class="pr-loss"><div class="trace-loss-h">Redaman estimasi (POP &rarr; pelanggan)</div>${lossBreakdownHtml(d.loss)}</div>` : ""}
      ${s.segments && s.segments.length > 1 ? `<table class="pr-boq"><thead><tr><th>Segmen</th><th>Kabel</th><th>Panjang</th></tr></thead><tbody>${s.segments.map((g) => `<tr><td>${escapeHtml(g.label)}</td><td>${escapeHtml(g.cable_label)} ${escapeHtml(g.cable_capacity)}</td><td class="num">${fmtM(g.route_length_m)}</td></tr>`).join("")}</tbody></table>` : ""}
      ${(s.notes || []).length ? `<ul class="pr-notes">${s.notes.map((n) => `<li><i class="fa-solid fa-circle-info"></i> ${escapeHtml(n)}</li>`).join("")}</ul>` : ""}
      ${s.suggest_new_odp ? `<div class="pr-suggest"><i class="fa-solid fa-circle-plus"></i> Port ODP asal penuh: BOQ otomatis memuat <b>ODP baru + splitter</b>.</div>` : ""}
      ${d.warnings.length ? `<ul class="pr-warn">${d.warnings.map((w) => `<li><i class="fa-solid fa-triangle-exclamation"></i> ${escapeHtml(w)}</li>`).join("")}</ul>` : ""}
      <div class="pr-note">Jenis kabel dipilih dari panjang rute (tanpa slack). Core dialokasikan otomatis saat diwujudkan (port/core kosong pertama); bila tidak bisa, alokasi manual lewat Detail Core.</div>
    </div>`;
}
// Pastikan hasil hitung sesuai parameter terkini; bila usang/belum ada, hitung ulang dulu (dipakai Simpan & PDF).
function planEnsureFresh() {
  const p = TOOL.plan;
  if (p.result && !p.stale) return Promise.resolve(true);
  if (!p.origin) {
    setPlanMsg(
      "Pilih aset asal terlebih dahulu (ketik nama atau klik di peta).",
      "warn",
    );
    return Promise.resolve(false);
  }
  if (!p.dest) {
    setPlanMsg("Tentukan lokasi pelanggan di peta terlebih dahulu.", "warn");
    return Promise.resolve(false);
  }
  return Promise.resolve(runPlanPreview()).then(
    () => !!TOOL.plan.result && !TOOL.plan.stale,
  );
}
function savePlanDraft() {
  return planEnsureFresh().then((ok) => {
    if (ok) return savePlanDraftNow();
  });
}
function savePlanDraftNow() {
  const p = TOOL.plan;
  if (!p.result) return;
  const dn = ($id("plan-dest-name").value || "").trim();
  apiRequest("/api/plans", "POST", {
    ...planPayload(),
    name: dn ? `Pasang baru ${dn}` : null,
  })
    .then((d) => {
      p.id = d.id;
      p.boqId = d.id;
      p.status = "Draft";
      setPlanMsg(`Rencana #${d.id} "${d.name}" tersimpan sebagai Draft.`, "ok");
      $id("plan-realize-btn").style.display = can("plan.realize") ? "" : "none";
      loadSavedPlans();
      return boqHasAdjust() ? boqSave(true) : null;
    })
    .then(() => renderBoq())
    .catch((err) => setPlanMsg("Gagal menyimpan: " + err.message, "bad"));
}
function realizeCurrentPlan() {
  if (TOOL.plan.id != null) realizePlanById(TOOL.plan.id);
}
function realizePlanById(id) {
  if (
    !confirm(
      "Wujudkan rencana ini? Kabel, tiang/handhole, slack, dan titik pelanggan akan dibuat sebagai aset Active di peta.",
    )
  )
    return;
  apiRequest(`/api/plans/${id}/realize`, "POST")
    .then((d) => {
      alert(d.message);
      TOOL.plan.result = null;
      TOOL.plan.id = null;
      clearToolLayer();
      $id("plan-result").innerHTML = "";
      $id("plan-boq").innerHTML = "";
      BOQ.data = null;
      $id("plan-actions").style.display = "flex";
      $id("plan-save-btn").style.display = "";
      $id("plan-realize-btn").style.display = "none";
      setPlanMsg(
        "Rencana diwujudkan. Lanjutkan alokasi core di Detail Core.",
        "ok",
      );
      loadData();
      loadSavedPlans();
    })
    .catch((err) => alert("Gagal mewujudkan rencana: " + err.message));
}
function loadSavedPlans() {
  apiRequest("/api/plans")
    .then((list) => {
      const el = $id("plan-saved");
      if (!el) return;
      el.innerHTML = list.length
        ? list
            .slice(0, 15)
            .map((r) => {
              const s = r.summary || {};
              const draft = r.status === "Draft";
              return `<div class="plan-item ${draft ? "" : "done"}">
              <div class="plan-item-main"><b>${escapeHtml(r.name)}</b> <span class="cov-chip ${draft ? "warn" : "ok"}">${draft ? "Draft" : "Terwujud"}</span></div>
              <div class="cov-meta">${escapeHtml(r.origin_name || "-")} &rarr; ${escapeHtml(r.dest_name || "-")} &middot; ${fmtM(s.route_length_m)} &middot; ${escapeHtml(s.cable_label || "")}</div>
              <div class="cov-act">
                <button type="button" onclick="openSavedPlan(${Number(r.id)})"><i class="fa-solid fa-eye"></i> Buka</button>
                ${draft && can("plan.realize") ? `<button type="button" class="primary" onclick="realizePlanById(${Number(r.id)})"><i class="fa-solid fa-hammer"></i> Wujudkan</button>` : ""}
                <button type="button" onclick="downloadPlanPdfById(${Number(r.id)}, 'lapangan', this)" title="PDF asplan untuk tim lapangan (tanpa harga)"><i class="fa-solid fa-file-pdf"></i> PDF</button>
                ${can("plan.write") ? `<button type="button" onclick="downloadPlanPdfById(${Number(r.id)}, 'lengkap', this)" title="PDF lengkap dengan BOQ harga"><i class="fa-solid fa-file-pdf"></i> +BOQ</button>` : ""}
                ${draft ? `<button type="button" class="danger" onclick="deleteSavedPlan(${Number(r.id)})"><i class="fa-solid fa-trash"></i></button>` : ""}
              </div></div>`;
            })
            .join("")
        : '<span class="tp-muted">Belum ada rencana tersimpan.</span>';
    })
    .catch(() => {});
}
function openSavedPlan(id) {
  apiRequest(`/api/plans/${id}`)
    .then((d) => {
      const r = d.plan;
      const p = TOOL.plan;
      p.origin =
        r.origin.type === "NODE"
          ? {
              id: r.origin.id,
              name: r.origin.name,
              type: r.origin.kind,
              lat: r.origin.lat,
              lng: r.origin.lng,
            }
          : null;
      p.dest = { lat: r.dest.lat, lng: r.dest.lng };
      p.via = r.via || [];
      p.rmode = (r.route && r.route.mode) || "road";
      p.bends =
        p.rmode === "custom" && r.route && r.route.bends
          ? r.route.bends.map((b) => [b[0], b[1]])
          : [];
      planModeUi();
      $id("plan-origin-input").value = r.origin.name || "";
      $id("plan-origin-chip").textContent = p.origin
        ? `${p.origin.name} (${TYPE_LABEL[p.origin.type] || p.origin.type})`
        : "Titik bebas";
      $id("plan-dest-chip").textContent =
        `Pelanggan: ${r.dest.lat.toFixed(6)}, ${r.dest.lng.toFixed(6)}`;
      $id("plan-dest-name").value =
        r.dest.name === "Pelanggan baru" ? "" : r.dest.name;
      {
        const rm = $id("plan-remarks");
        if (rm) rm.value = r.remarks || "";
      }
      $id("plan-customer").checked = r.create_customer !== false;
      PLANOTB.picked =
        r.origin && r.origin.pop_ports ? r.origin.pop_ports.slice() : [];
      PLANOTB.mode = PLANOTB.picked.length ? "MANUAL" : "AUTO";
      document.querySelectorAll('input[name="plan-otbmode"]').forEach((x) => {
        x.checked = x.value === PLANOTB.mode;
      });
      planCustFill(r.customer_info);
      if ($id("plan-cust-box"))
        $id("plan-cust-box").style.display =
          r.create_customer === false ? "none" : "";
      $id("plan-poles").checked = r.use_poles !== false;
      $id("plan-slack").checked = r.use_slack !== false;
      document.querySelectorAll('input[name="plan-inst"]').forEach((x) => {
        x.checked = x.value === r.cable.installation;
      });
      if ($id("plan-cores"))
        $id("plan-cores").value = String(r.customer_cores || 1);
      document.querySelectorAll('input[name="plan-term"]').forEach((x) => {
        x.checked = x.value === (r.termination || "DROPCORE_ROSET");
      });
      if ($id("plan-scen")) $id("plan-scen").value = r.hub ? "HUB" : "DIRECT";
      document.querySelectorAll('input[name="plan-hubmode"]').forEach((x) => {
        x.checked = x.value === (r.hub ? r.hub.mode : "NEAREST");
      });
      p.hub = r.hub ? [r.hub.latitude, r.hub.longitude] : null;
      if (r.hub && r.hub.odp && $id("plan-splitter"))
        $id("plan-splitter").value = r.hub.odp.ratio;
      onPlanServiceChange();
      TOOL.plan.installation = r.cable.installation;
      fillRuleInputs(r.rules);
      renderPlanVia();
      p.result = r;
      p.id = d.status === "Draft" ? d.id : null;
      p.boqId = d.id;
      p.status = d.status;
      renderPlanResult(r);
      drawPlanResult(r);
      boqReset(false);
      if (d.boq_adjust) {
        const b = d.boq_adjust;
        BOQ.adjust = {
          region: b.region || null,
          tax_pct: b.tax_pct == null ? null : b.tax_pct,
          lines: b.lines || {},
          extra: b.extra || [],
        };
        BOQ.manualRegion = !!b.region;
        BOQ.manualTax = b.tax_pct != null;
      }
      boqCalc();
      p.stale = false;
      $id("plan-actions").style.display = "flex";
      $id("plan-save-btn").style.display = "none";
      $id("plan-realize-btn").style.display =
        d.status === "Draft" && can("plan.realize") ? "" : "none";
      setPlanMsg(
        d.status === "Draft"
          ? `Rencana #${d.id} (Draft)`
          : `Rencana #${d.id} sudah diwujudkan sebagai kabel ${d.realized ? d.realized.cable_name : "-"}.`,
        d.status === "Draft" ? "info" : "ok",
      );
    })
    .catch((err) => setPlanMsg("Gagal membuka rencana: " + err.message, "bad"));
}
// ---------- Laporan PDF rencana (asplan) ----------
function planPdfTz() {
  return -new Date().getTimezoneOffset();
}
function planPdfMap() {
  const s = $id("plan-pdf-map");
  return s && ["osm", "satelit", "off"].includes(s.value) ? s.value : "osm";
}
function planDownloadPdf() {
  return planEnsureFresh().then((ok) => {
    if (ok) return planDownloadPdfNow();
  });
}
function planDownloadPdfNow() {
  const p = TOOL.plan;
  if (!p.result) return;
  const sel = $id("plan-pdf-variant");
  const variant =
    sel && sel.value === "lengkap" && can("plan.write")
      ? "lengkap"
      : "lapangan";
  const btn = $id("plan-pdf-btn");
  if (btn) setBtnBusy(btn, true);
  const dn = ($id("plan-dest-name").value || "").trim();
  let job;
  if (p.boqId != null) {
    // rencana tersimpan: PDF mengikuti data yang tersimpan (BOQ disimpan dulu bila ada perubahan)
    job = (
      variant === "lengkap" && boqHasAdjust()
        ? boqSave(true)
        : Promise.resolve()
    ).then(() =>
      downloadFromApi(
        `/api/plans/${p.boqId}/pdf?variant=${variant}&tz=${planPdfTz()}&basemap=${planPdfMap()}`,
      ),
    );
  } else {
    job = downloadFromApi("/api/plan/pdf", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...planPayload(),
        name: dn ? `Pasang baru ${dn}` : null,
        variant,
        basemap: planPdfMap(),
        boq_adjust: BOQ.adjust,
        tz_min: planPdfTz(),
      }),
    });
  }
  return job
    .then((r) => setPlanMsg(`PDF diunduh: ${r.name}`, "ok"))
    .catch((err) => setPlanMsg("Gagal membuat PDF: " + err.message, "bad"))
    .finally(() => {
      if (btn) setBtnBusy(btn, false);
    });
}
function downloadPlanPdfById(id, variant, btn) {
  if (btn) setBtnBusy(btn, true);
  return downloadFromApi(
    `/api/plans/${Number(id)}/pdf?variant=${variant === "lengkap" ? "lengkap" : "lapangan"}&tz=${planPdfTz()}&basemap=${planPdfMap()}`,
  )
    .then((r) => setPlanMsg(`PDF diunduh: ${r.name}`, "ok"))
    .catch((err) => alert("Gagal membuat PDF: " + err.message))
    .finally(() => {
      if (btn) setBtnBusy(btn, false);
    });
}

function deleteSavedPlan(id) {
  if (!confirm("Hapus rencana Draft ini?")) return;
  apiRequest(`/api/plans/${id}`, "DELETE")
    .then(() => loadSavedPlans())
    .catch((err) => alert("Gagal menghapus: " + err.message));
}

// ---------- BOQ (KHS) ----------
const KHS = { data: null, region: null, pick: null, edit: null, seq: 0 };
const BOQ = {
  adjust: { region: null, tax_pct: null, lines: {}, extra: [] },
  data: null,
  seq: 0,
  manualRegion: false,
  manualTax: false,
};
const BOQ_LABEL = {
  CABLE: "Kabel",
  SURVEY: "Biaya survei",
  TIANG: "Tiang baru",
  TIANG_REUSE: "Aksesoris tiang eksisting",
  DUCT: "Pipa duct",
  GALIAN: "Galian",
  HH: "Handhole baru",
  SLACK: "Slack",
  PELANGGAN: "Roset pelanggan",
  SPLICE: "Splicing",
  CLOSURE: "Closure hub",
  ODP_BARU: "ODP baru",
  SPLITTER: "Splitter",
  OTB: "OTB pelanggan",
  PIGTAIL: "Adapter + pigtail",
  CABLE_S1: "Kabel distribusi",
  CABLE_S2: "Kabel hub-pelanggan",
};
const BOQ_PARTS_LABEL = {
  both: "Material + Jasa",
  material: "Material saja",
  jasa: "Jasa saja",
};

function fmtRp(n) {
  const v = Math.round(Number(n));
  if (!Number.isFinite(v)) return "-";
  return (
    (v < 0 ? "-" : "") +
    "Rp " +
    String(Math.abs(v)).replace(/\B(?=(\d{3})+(?!\d))/g, ".")
  );
}
function boqReset(keepManual) {
  BOQ.adjust = {
    region: keepManual && BOQ.manualRegion ? BOQ.adjust.region : null,
    tax_pct: keepManual && BOQ.manualTax ? BOQ.adjust.tax_pct : null,
    lines: {},
    extra: [],
  };
  if (!keepManual) {
    BOQ.manualRegion = false;
    BOQ.manualTax = false;
  }
}
function boqPayload() {
  const r = TOOL.plan.result;
  const dn = ($id("plan-dest-name").value || "").trim();
  return {
    summary: r.summary,
    rules: r.rules,
    origin_cluster: (r.origin && r.origin.cluster) || null,
    region: BOQ.adjust.region || null,
    adjust: BOQ.adjust,
    plan_name: dn ? `Pasang baru ${dn}` : "Rencana pasang baru",
  };
}
function boqCalc() {
  if (!TOOL.plan.result || !can("plan.write")) {
    BOQ.data = null;
    renderBoq();
    return Promise.resolve();
  }
  const seq = ++BOQ.seq;
  return (KHS.data ? Promise.resolve() : loadKhs().catch(() => {}))
    .then(() =>
      seq === BOQ.seq
        ? apiRequest("/api/boq/calc", "POST", boqPayload())
        : null,
    )
    .then((d) => {
      if (!d || seq !== BOQ.seq) return;
      BOQ.data = d;
      renderBoq();
    })
    .catch((err) => {
      if (seq === BOQ.seq) {
        BOQ.data = null;
        $id("plan-boq").innerHTML =
          `<div class="tp-verdict bad">BOQ gagal dihitung: ${escapeHtml(err.message)}</div>`;
      }
    });
}
function boqHasAdjust() {
  const a = BOQ.adjust;
  return !!(
    BOQ.manualRegion ||
    BOQ.manualTax ||
    Object.keys(a.lines || {}).length ||
    (a.extra || []).length
  );
}
function renderBoq() {
  const el = $id("plan-boq");
  if (!el) return;
  const d = BOQ.data;
  if (!d || !TOOL.plan.result) {
    el.innerHTML = "";
    return;
  }
  const regs = (KHS.data && KHS.data.regions) || [d.region];
  const t = d.totals;
  const removed = Object.keys(BOQ.adjust.lines || {}).filter(
    (k) => BOQ.adjust.lines[k] && BOQ.adjust.lines[k].removed,
  );
  const rows = d.lines
    .map((l) => {
      const id = escapeHtml(l.id);
      const extra = l.id.charAt(0) === "X";
      return `<tr class="${l.note ? "bq-warn" : ""}">
      <td><b>${escapeHtml(l.component)}</b>
        <div class="bq-item">${l.code ? `<code>${escapeHtml(l.code)}</code> ` : ""}${escapeHtml(l.description)}</div>
        ${l.note ? `<div class="bq-note"><i class="fa-solid fa-triangle-exclamation"></i> ${escapeHtml(l.note)}</div>` : ""}
        <div class="bq-act"><button type="button" onclick="boqPickItem('${id}')">Ganti item</button>
          <button type="button" onclick="boqRemove('${id}')">Hapus</button>
          <select class="bq-parts" onchange="boqSetParts('${id}', this.value)" aria-label="Komponen harga ${escapeHtml(l.component)}" title="Harga yang dipakai dari item KHS">${Object.keys(
            BOQ_PARTS_LABEL,
          )
            .map(
              (k) =>
                `<option value="${k}" ${k === (l.parts || "both") ? "selected" : ""}>${BOQ_PARTS_LABEL[k]}</option>`,
            )
            .join("")}</select></div></td>
      <td class="num"><input type="number" min="0" step="any" value="${l.qty}" onchange="boqSetQty('${id}', this.value)" aria-label="Volume ${escapeHtml(l.component)}" />
        <small>${escapeHtml(l.unit || "")}${l.manual && !extra && l.auto_qty !== l.qty ? ` <a href="#" onclick="boqSetQty('${id}', ''); return false;" title="Kembalikan ke hitungan otomatis (${l.auto_qty})">auto</a>` : ""}</small></td>
      <td class="num">${fmtRp(l.total)}<small>@ ${fmtRp(l.unit_price)}</small></td></tr>`;
    })
    .join("");
  el.innerHTML = `
    <div class="pr-card bq-card">
      <div class="pr-head"><span class="pr-type" style="background:#6d28d9">BOQ &amp; Anggaran</span>
        <span class="pr-cap">Harga KHS${KHS.data && KHS.data.meta && KHS.data.meta.source ? ` &middot; ${escapeHtml(KHS.data.meta.source)}` : ""}</span></div>
      <div class="bq-opts">
        <label>Wilayah harga<select id="bq-region" onchange="boqSetRegion(this.value)">${regs.map((g) => `<option value="${g}" ${g === d.region ? "selected" : ""}>${g}</option>`).join("")}</select></label>
        <label>PPN (%)<input type="number" id="bq-tax" min="0" max="100" step="0.5" value="${t.tax_pct}" onchange="boqSetTax(this.value)" /></label>
      </div>
      <div class="bq-scroll"><table class="pr-boq bq-table"><thead><tr><th>Uraian (item KHS)</th><th>Volume</th><th>Jumlah</th></tr></thead><tbody>${rows}</tbody>
        <tfoot>
          <tr><td colspan="2">Material</td><td class="num">${fmtRp(t.material)}</td></tr>
          <tr><td colspan="2">Jasa</td><td class="num">${fmtRp(t.jasa)}</td></tr>
          <tr><td colspan="2">Subtotal</td><td class="num">${fmtRp(t.subtotal)}</td></tr>
          <tr><td colspan="2">PPN ${t.tax_pct}%</td><td class="num">${fmtRp(t.tax)}</td></tr>
          <tr class="bq-total"><td colspan="2">TOTAL</td><td class="num">${fmtRp(t.total)}</td></tr>
        </tfoot></table></div>
      ${removed.length ? `<div class="bq-removed">Dihapus: ${removed.map((k) => `<button type="button" onclick="boqRestore('${escapeHtml(k)}')">${escapeHtml(BOQ_LABEL[k] || k)} &#8634;</button>`).join(" ")}</div>` : ""}
      ${d.warnings.length ? `<ul class="pr-warn">${d.warnings.map((w) => `<li><i class="fa-solid fa-triangle-exclamation"></i> ${escapeHtml(w)}</li>`).join("")}</ul>` : ""}
      ${(d.notes || []).length ? `<div class="bq-notes"><b>Catatan BOQ</b><ul class="pr-notes">${d.notes.map((n) => `<li>${escapeHtml(n)}</li>`).join("")}</ul></div>` : ""}
      <div class="tp-row bq-btns">
        <button type="button" class="tp-ghost" onclick="boqAddItem()"><i class="fa-solid fa-plus"></i> Tambah item KHS</button>
        <button type="button" class="tp-ghost" onclick="openKhsModal()"><i class="fa-solid fa-list"></i> ${can("khs.edit") ? "Atur harga KHS" : "Lihat harga KHS"}</button>
      </div>
      <div class="tp-row bq-btns">
        <button type="button" class="tp-primary" id="bq-export" onclick="boqExport()"><i class="fa-solid fa-file-excel"></i> Ekspor Excel</button>
        <button type="button" class="tp-ghost" id="bq-save" onclick="boqSave()" ${TOOL.plan.boqId == null ? 'disabled title="Simpan rencana dahulu"' : ""}><i class="fa-solid fa-floppy-disk"></i> Simpan BOQ</button>
      </div>
      <div class="pr-note">${escapeHtml(d.estimate_note || "Estimasi di luar transportasi dan biaya lainnya.")} Volume kabel sudah termasuk slack. Kabel, closure, ODP, dan tiang baru bawaannya "Jasa saja" (material disediakan sendiri); ubah per baris lewat pilihan harga. Jointing: 2 core (skenario &lt; 1 km) atau 5 core (skenario hub). Biaya survei otomatis sesuai jarak tarikan.</div>
    </div>`;
}
function boqSetRegion(v) {
  BOQ.adjust.region = v;
  BOQ.manualRegion = true;
  boqCalc();
}
function boqSetTax(v) {
  const n = Number(v);
  BOQ.adjust.tax_pct =
    Number.isFinite(n) && v !== "" ? Math.max(0, Math.min(100, n)) : null;
  BOQ.manualTax = BOQ.adjust.tax_pct != null;
  boqCalc();
}
function boqSetQty(id, v) {
  const line = BOQ.data && BOQ.data.lines.find((l) => l.id === id);
  if (id.charAt(0) === "X") {
    const i = Number(id.slice(1)) - 1;
    const ex = BOQ.adjust.extra.filter((e) => e && e.key && Number(e.qty) > 0)[
      i
    ];
    if (ex) ex.qty = Number(v) > 0 ? Number(v) : ex.qty;
  } else {
    const o = (BOQ.adjust.lines[id] = BOQ.adjust.lines[id] || {});
    if (v === "" || v == null || (line && Number(v) === line.auto_qty))
      delete o.qty;
    else o.qty = Number(v);
    if (!Object.keys(o).length) delete BOQ.adjust.lines[id];
  }
  boqCalc();
}
function boqSetParts(id, v) {
  const parts = ["both", "material", "jasa"].includes(v) ? v : "both";
  const line = BOQ.data && BOQ.data.lines.find((l) => l.id === id);
  const dflt = (line && line.default_parts) || "both";
  if (id.charAt(0) === "X") {
    const valid = BOQ.adjust.extra.filter(
      (e) => e && e.key && Number(e.qty) > 0,
    );
    const ex = valid[Number(id.slice(1)) - 1];
    if (ex) {
      if (parts === "both") delete ex.parts;
      else ex.parts = parts;
    }
  } else {
    const o = (BOQ.adjust.lines[id] = BOQ.adjust.lines[id] || {});
    if (parts === dflt) delete o.parts;
    else o.parts = parts;
    if (!Object.keys(o).length) delete BOQ.adjust.lines[id];
  }
  boqCalc();
}
function boqRemove(id) {
  if (id.charAt(0) === "X") {
    const valid = BOQ.adjust.extra.filter(
      (e) => e && e.key && Number(e.qty) > 0,
    );
    BOQ.adjust.extra = BOQ.adjust.extra.filter(
      (e) => e !== valid[Number(id.slice(1)) - 1],
    );
  } else {
    (BOQ.adjust.lines[id] = BOQ.adjust.lines[id] || {}).removed = true;
  }
  boqCalc();
}
function boqRestore(id) {
  const o = BOQ.adjust.lines[id];
  if (o) {
    delete o.removed;
    if (!Object.keys(o).length) delete BOQ.adjust.lines[id];
  }
  boqCalc();
}
function boqPickItem(id) {
  openKhsModal((key) => {
    if (id.charAt(0) === "X") {
      const valid = BOQ.adjust.extra.filter(
        (e) => e && e.key && Number(e.qty) > 0,
      );
      const ex = valid[Number(id.slice(1)) - 1];
      if (ex) ex.key = key;
    } else {
      (BOQ.adjust.lines[id] = BOQ.adjust.lines[id] || {}).key = key;
    }
    boqCalc();
  }, "Pilih item KHS pengganti");
}
function boqAddItem() {
  openKhsModal((key) => {
    BOQ.adjust.extra.push({ key, qty: 1 });
    boqCalc();
  }, "Pilih item KHS yang ditambahkan");
}
function boqExport() {
  if (!TOOL.plan.result) return;
  const btn = $id("bq-export");
  if (btn) btn.disabled = true;
  return downloadFromApi("/api/boq/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(boqPayload()),
  })
    .then((r) => setPlanMsg(`BOQ diunduh: ${r.name}`, "ok"))
    .catch((err) => setPlanMsg("Gagal mengekspor BOQ: " + err.message, "bad"))
    .finally(() => {
      if (btn) btn.disabled = false;
    });
}
function boqSave(silent) {
  const id = TOOL.plan.boqId != null ? TOOL.plan.boqId : null;
  if (id == null)
    return silent
      ? Promise.resolve()
      : setPlanMsg(
          "Simpan rencana terlebih dahulu agar BOQ-nya ikut tersimpan.",
          "warn",
        );
  return apiRequest(`/api/plans/${id}/boq`, "PUT", {
    adjust: {
      ...BOQ.adjust,
      region: BOQ.adjust.region || (BOQ.data && BOQ.data.region),
    },
  })
    .then((d) => {
      if (!silent)
        setPlanMsg(
          `BOQ rencana #${id} tersimpan (${d.region}, ${fmtRp(d.totals.total)}).`,
          "ok",
        );
    })
    .catch((err) => setPlanMsg("Gagal menyimpan BOQ: " + err.message, "bad"));
}

// Katalog KHS
function setKhsMsg(text, cls) {
  const el = $id("khs-msg");
  if (!el) return;
  el.style.display = text ? "block" : "none";
  el.className = "tp-verdict " + (cls || "");
  el.textContent = text || "";
}
function loadKhs() {
  return apiRequest("/api/khs").then((d) => {
    KHS.data = d;
    if (!KHS.region || !d.regions.includes(KHS.region))
      KHS.region = (d.settings && d.settings.default_region) || d.regions[0];
    return d;
  });
}
function openKhsModal(pickCb, title) {
  KHS.pick = typeof pickCb === "function" ? pickCb : null;
  KHS.edit = null;
  $id("modal-khs").style.display = "flex";
  $id("khs-title").textContent = KHS.pick
    ? "Pilih item KHS"
    : "Katalog Harga KHS";
  const pb = $id("khs-pickbar");
  pb.style.display = KHS.pick ? "block" : "none";
  pb.textContent = KHS.pick
    ? (title || "Pilih item") + ": klik 'Pilih' pada baris yang sesuai."
    : "";
  $id("khs-admin").style.display = can("khs.edit") ? "" : "none";
  setKhsMsg("", "");
  const done = () => {
    if (BOQ.data && KHS.pick) KHS.region = BOQ.data.region;
    renderKhsControls();
    renderKhs();
    renderKhsMap();
  };
  (KHS.data ? Promise.resolve() : loadKhs())
    .then(done)
    .catch((err) => setKhsMsg("Gagal memuat katalog: " + err.message, "bad"));
}
function closeKhsModal() {
  $id("modal-khs").style.display = "none";
  KHS.pick = null;
  KHS.edit = null;
}
function renderKhsControls() {
  const d = KHS.data;
  if (!d) return;
  const js = $id("khs-jenis"),
    rg = $id("khs-region");
  const cj = js.value || "ALL";
  js.innerHTML =
    `<option value="ALL">Semua jenis</option>` +
    d.jenis
      .map((j) => `<option value="${escapeHtml(j)}">${escapeHtml(j)}</option>`)
      .join("");
  js.value = d.jenis.includes(cj) ? cj : "ALL";
  rg.innerHTML = d.regions
    .map((g) => `<option value="${g}">Harga ${g}</option>`)
    .join("");
  rg.value = KHS.region;
  $id("khs-meta").textContent =
    `${d.total} item` +
    (d.meta && d.meta.source ? ` · sumber: ${d.meta.source}` : "") +
    (d.meta && d.meta.imported_at ? ` · diimpor ${d.meta.imported_at}` : "");
}
function khsFilterChanged() {
  KHS.region = $id("khs-region").value || KHS.region;
  KHS.edit = null;
  renderKhs();
}
function renderKhs() {
  const d = KHS.data,
    tb = $id("khs-rows");
  if (!d || !tb) return;
  const q = ($id("khs-q").value || "").trim().toLowerCase();
  const jn = $id("khs-jenis").value || "ALL";
  const reg = KHS.region;
  const list = d.items.filter(
    (i) =>
      (jn === "ALL" || i.jenis === jn) &&
      (!q ||
        i.description.toLowerCase().includes(q) ||
        i.code.toLowerCase().includes(q) ||
        i.key.toLowerCase().includes(q)),
  );
  const edit = can("khs.edit");
  tb.innerHTML = list.length
    ? list
        .map((i) => {
          const p = i.prices[reg] || [null, null];
          const k = escapeHtml(i.key);
          const editing = KHS.edit === i.key;
          const m = editing
            ? `<input type="number" id="khs-e-m" min="0" step="any" value="${p[0] == null ? "" : p[0]}" />`
            : p[0] == null
              ? "-"
              : fmtRp(p[0]);
          const j = editing
            ? `<input type="number" id="khs-e-j" min="0" step="any" value="${p[1] == null ? "" : p[1]}" />`
            : p[1] == null
              ? "-"
              : fmtRp(p[1]);
          const act = editing
            ? `<button type="button" class="primary" onclick="khsSaveEdit('${k}')">Simpan</button><button type="button" onclick="khsEdit(null)">Batal</button>`
            : (KHS.pick
                ? `<button type="button" class="primary" onclick="khsPick('${k}')">Pilih</button>`
                : "") +
              (edit && !editing
                ? `<button type="button" onclick="khsEdit('${k}')" title="Ubah harga ${escapeHtml(reg)}" aria-label="Ubah harga ${k}">Ubah</button>`
                : "");
          return `<tr><td><code>${k}</code></td><td>${escapeHtml(i.description)}<div class="khs-jenis">${escapeHtml(i.jenis || "")}</div></td>
      <td>${escapeHtml(i.unit || "")}</td><td class="num">${m}</td><td class="num">${j}</td>
      <td class="num"><b>${fmtRp((p[0] || 0) + (p[1] || 0))}</b></td><td class="khs-act">${act}</td></tr>`;
        })
        .join("")
    : `<tr><td colspan="7" class="tp-muted">Tidak ada item yang cocok.</td></tr>`;
}
function khsPick(key) {
  const cb = KHS.pick;
  closeKhsModal();
  if (cb) cb(key);
}
function khsEdit(key) {
  KHS.edit = key;
  renderKhs();
}
function khsSaveEdit(key) {
  const val = (id) => {
    const v = ($id(id).value || "").trim();
    return v === "" ? null : Number(v);
  };
  apiRequest(`/api/khs/${encodeURIComponent(key)}`, "PUT", {
    prices: {
      [KHS.region]: { material: val("khs-e-m"), jasa: val("khs-e-j") },
    },
  })
    .then((r) => {
      setKhsMsg(`${r.message}: ${key} (${KHS.region})`, "ok");
      KHS.edit = null;
      return loadKhs();
    })
    .then(() => {
      renderKhsControls();
      renderKhs();
      if (BOQ.data) boqCalc();
    })
    .catch((err) => setKhsMsg("Gagal menyimpan harga: " + err.message, "bad"));
}
function khsExport() {
  return downloadFromApi("/api/khs/export")
    .then((r) => setKhsMsg(`Diunduh: ${r.name}`, "ok"))
    .catch((err) => setKhsMsg("Gagal mengunduh: " + err.message, "bad"));
}
function khsImport() {
  const f = $id("khs-file").files && $id("khs-file").files[0];
  if (!f) return setKhsMsg("Pilih berkas KHS (.xlsx) terlebih dahulu.", "warn");
  if (f.size > 8 * 1024 * 1024)
    return setKhsMsg("Berkas lebih dari 8 MB.", "bad");
  const mode = $id("khs-imp-mode").value;
  if (
    mode === "replace" &&
    !confirm(
      "Ganti SELURUH katalog harga dengan isi berkas ini? Perubahan harga manual akan hilang.",
    )
  )
    return;
  const rd = new FileReader();
  rd.onload = () => {
    setKhsMsg("Mengimpor KHS...", "info");
    apiRequest("/api/khs/import", "POST", {
      filename: f.name,
      content_base64: bufToBase64(rd.result),
      mode,
    })
      .then((r) => {
        setKhsMsg(
          r.message +
            (r.missing_map && r.missing_map.length
              ? `. Perhatian: item pada pemetaan tidak ada: ${r.missing_map.join(", ")}`
              : ""),
          r.missing_map && r.missing_map.length ? "warn" : "ok",
        );
        return loadKhs();
      })
      .then(() => {
        renderKhsControls();
        renderKhs();
        renderKhsMap();
        if (BOQ.data) boqCalc();
      })
      .catch((err) => setKhsMsg("Gagal impor: " + err.message, "bad"));
  };
  rd.onerror = () => setKhsMsg("Berkas tidak dapat dibaca.", "bad");
  rd.readAsArrayBuffer(f);
}
const KHS_MAP_LABEL = {
  pole_default: "Tiang (bawaan)",
  pole_reuse: "Aksesoris tiang eksisting",
  splice: "Splicing (fusion)",
  hh: "Handhole baru",
  slack_udara: "Slack (udara)",
  slack_tanah: "Slack (tanah)",
  duct: "Pipa duct (tanah)",
  trench: "Galian (tanah)",
};
function renderKhsMap() {
  const el = $id("khs-map");
  if (!el || !KHS.data || !can("khs.edit")) return;
  const m = KHS.data.map,
    st = KHS.data.settings;
  const dl = `<datalist id="khs-keys">${KHS.data.items.map((i) => `<option value="${escapeHtml(i.key)}">${escapeHtml(i.description.slice(0, 60))}</option>`).join("")}</datalist>`;
  const row = (grp, k, label) =>
    `<label>${escapeHtml(label)}<input type="text" list="khs-keys" data-grp="${grp}" data-k="${escapeHtml(k)}" value="${escapeHtml(grp ? m[grp][k] : m[k])}" /></label>`;
  el.innerHTML =
    dl +
    `<div class="khs-map-grid">` +
    Object.keys(m.cable)
      .map((k) => row("cable", k, `Kabel ${k.replace(":", " / ")} core`))
      .join("") +
    Object.keys(m.pole)
      .map((k) => row("pole", k, k))
      .join("") +
    Object.keys(m.customer)
      .map((k) => row("customer", k, `Pelanggan ${k} core`))
      .join("") +
    Object.keys(KHS_MAP_LABEL)
      .map((k) => row("", k, KHS_MAP_LABEL[k]))
      .join("") +
    `<label>PPN bawaan (%)<input type="number" id="khs-map-tax" min="0" max="100" step="0.5" value="${st.tax_pct}" /></label>
     <label>Splicing per pelanggan (min 2, hitungan redaman)<input type="number" id="khs-map-splice" min="2" max="48" step="1" value="${st.splice_per_customer || 2}" /></label>
     <label>Jointing BOQ skenario &lt; 1 km (core)<input type="number" id="khs-map-spl-direct" min="1" max="48" step="1" value="${st.splice_direct || 2}" /></label>
     <label>Jointing BOQ skenario hub (core)<input type="number" id="khs-map-spl-hub" min="1" max="48" step="1" value="${st.splice_hub || 5}" /></label>
     <label>Wilayah bawaan<select id="khs-map-region">${KHS.data.regions.map((g) => `<option value="${g}" ${g === st.default_region ? "selected" : ""}>${g}</option>`).join("")}</select></label></div>`;
}
function khsSaveMap() {
  const map = { cable: {}, pole: {}, customer: {} };
  document.querySelectorAll("#khs-map [data-k]").forEach((inp) => {
    const grp = inp.getAttribute("data-grp"),
      k = inp.getAttribute("data-k"),
      v = (inp.value || "").trim();
    if (!v) return;
    if (grp) map[grp][k] = v;
    else map[k] = v;
  });
  apiRequest("/api/boq/map", "PUT", {
    map,
    settings: {
      tax_pct: $id("khs-map-tax").value,
      default_region: $id("khs-map-region").value,
      splice_per_customer: $id("khs-map-splice").value,
      splice_direct: ($id("khs-map-spl-direct") || {}).value,
      splice_hub: ($id("khs-map-spl-hub") || {}).value,
    },
  })
    .then((r) => {
      setKhsMsg(r.message, "ok");
      return loadKhs();
    })
    .then(() => {
      renderKhsMap();
      renderKhsControls();
      if (BOQ.data) boqCalc();
    })
    .catch((err) =>
      setKhsMsg("Gagal menyimpan pemetaan: " + err.message, "bad"),
    );
}

// ---------- IMPORT / EXPORT ----------
const DATA = {
  tab: "export",
  name: "",
  b64: "",
  preview: null,
  seq: 0,
  statusMode: "asset",
};

function openDataModal(tab, fromInventory) {
  $id("modal-data").style.display = "flex";
  // Cluster/Area ekspor dimulai dari filter wilayah aktif (boleh diubah)
  fillExportScopeSelects(SCOPE.cluster, SCOPE.area, SCOPE.region);
  fillExportFolderSelect(fromInventory ? FT.sel || "" : "");
  if (fromInventory) {
    const t = $id("filter-type").value || "ALL";
    $id("exp-type").value = t === "INCIDENT" ? "ALL" : t;
    $id("exp-status").value = $id("filter-status").value || "ALL";
    $id("exp-install").value = $id("filter-installation").value || "ALL";
    $id("exp-q").value = ($id("inventory-search").value || "").trim();
    $id("exp-scope").value =
      t === "CABLE" || CABLE_TYPES.includes(t)
        ? "cables"
        : t !== "ALL"
          ? "nodes"
          : "all";
  }
  setDataTab(tab || DATA.tab);
}
function closeDataModal() {
  $id("modal-data").style.display = "none";
}
// Penutupan oleh pengguna (tombol Tutup, ×, Esc): kosongkan semua tampilan berkas supaya tidak perlu refresh.
// closeDataModal() murni menyembunyikan dan dipakai alur yang kembali ke modal (pilih jalur di peta, tampil di peta).
function dismissDataModal() {
  resetImport();
  resetOtdr();
  resetSor();
  setDataMsg("exp-msg", "", "");
  closeDataModal();
}
function setDataTab(tab) {
  DATA.tab = tab;
  $id("data-export").style.display = tab === "export" ? "" : "none";
  $id("data-import").style.display = tab === "import" ? "" : "none";
  $id("data-otdr").style.display = tab === "otdr" ? "" : "none";
  ["export", "import", "otdr"].forEach((t) => {
    const b = $id("dtab-" + t);
    if (b && b.classList) b.classList.toggle("active", t === tab);
  });
  if (tab === "import") {
    const ok = can("data.import");
    $id("imp-denied").style.display = ok ? "none" : "block";
    $id("imp-form").style.display = ok ? "" : "none";
    if (ok) {
      loadImportLimits();
      loadImportSessions();
    }
  }
  if (tab === "otdr") initOtdrTab();
  impLayout();
  onExportFormatChange();
}
function onExportFormatChange() {
  const fmt = $id("exp-format").value;
  const csv = fmt === "csv";
  const tabular = csv || fmt === "xlsx";
  $id("exp-delim-wrap").style.display = csv ? "" : "none";
  const opt = $id("exp-scope-conn");
  if (opt) opt.disabled = !tabular;
  const inc = $id("exp-scope-inc");
  if (inc) inc.disabled = fmt === "kml";
  if (!tabular && $id("exp-scope").value === "connections")
    $id("exp-scope").value = "all";
  if (fmt === "kml" && $id("exp-scope").value === "incidents")
    $id("exp-scope").value = "all";
  onExportScopeChange();
}
// Cakupan Incident memakai status tiket (Open/In Progress/...), bukan status aset
const INCIDENT_STATUS_OPTIONS =
  '<option value="ALL">Semua status</option><option value="Open">Open</option><option value="In Progress">In Progress</option><option value="Temporary Fix">Temporary Fix</option><option value="Resolved">Resolved</option>';
const ASSET_STATUS_OPTIONS =
  '<option value="ALL">Semua status</option><option value="Active">Active</option><option value="Maintenance">Maintenance</option><option value="Cut/Broken">Cut/Broken</option>';
function onExportScopeChange() {
  const inc = $id("exp-scope").value === "incidents";
  const st = $id("exp-status");
  if (DATA.statusMode !== (inc ? "inc" : "asset")) {
    DATA.statusMode = inc ? "inc" : "asset";
    st.innerHTML = inc ? INCIDENT_STATUS_OPTIONS : ASSET_STATUS_OPTIONS;
    st.value = "ALL";
  }
  const lbl = $id("exp-status-label");
  if (lbl) lbl.textContent = inc ? "Status tiket" : "Status";
  ["exp-type", "exp-install"].forEach((id) => {
    const el = $id(id);
    if (el) el.disabled = inc;
  });
}
// ---- Indikator progres (persentase nyata atau berjalan/indeterminate) + stopwatch ----
const DPROG = {};
function fmtElapsed(ms) {
  const s = Math.floor(ms / 1000);
  return s < 60 ? `${s} dtk` : `${Math.floor(s / 60)} mnt ${s % 60} dtk`;
}
function renderDataProgress(id) {
  const st = DPROG[id],
    el = $id(id);
  if (!el || !st) return;
  const det = typeof st.pct === "number";
  el.style.display = "block";
  el.innerHTML =
    `<div class="dp-head"><span class="dp-text">${escapeHtml(st.text || "")}</span>` +
    `<span class="dp-meta">${det ? `<b>${Math.round(st.pct)}%</b> &middot; ` : ""}${fmtElapsed(Date.now() - st.t0)}</span></div>` +
    `<div class="dp-track"><div class="dp-fill${det ? "" : " indet"}" style="${det ? `width:${Math.max(2, Math.min(100, st.pct))}%` : ""}"></div></div>`;
}
// pct: angka 0-100 = persentase nyata; null/undefined = indeterminate
function startDataProgress(id, text, pct) {
  stopDataProgress(id);
  DPROG[id] = {
    t0: Date.now(),
    text,
    pct,
    timer: setInterval(() => renderDataProgress(id), 500),
  };
  renderDataProgress(id);
}
function updateDataProgress(id, text, pct) {
  const st = DPROG[id];
  if (!st) return;
  if (text != null) st.text = text;
  st.pct = pct;
  renderDataProgress(id);
}
function stopDataProgress(id) {
  const st = DPROG[id];
  if (st && st.timer) clearInterval(st.timer);
  delete DPROG[id];
  const el = $id(id);
  if (el) {
    el.style.display = "none";
    el.innerHTML = "";
  }
}
function setBtnBusy(btn, on) {
  if (!btn) return;
  btn.disabled = !!on;
  btn.className =
    (btn.className || "").replace(/\bis-busy\b/g, "").trim() +
    (on ? " is-busy" : "");
}
function setDataMsg(id, text, cls) {
  const el = $id(id);
  if (!el) return;
  el.style.display = text ? "block" : "none";
  el.className = "data-msg " + (cls || "");
  el.textContent = text || "";
}
function bufToBase64(buf) {
  const bytes = new Uint8Array(buf);
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000)
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}
// Unduh berkas dari API (cookie sesi ikut terkirim) lalu simpan lewat tautan sementara
async function downloadFromApi(url, init, onProgress) {
  netBarStart();
  let res;
  try {
    res = await fetch(url, { credentials: "same-origin", ...(init || {}) });
  } finally {
    netBarEnd();
  }
  if (!res.ok) {
    let msg = `Permintaan gagal (${res.status})`;
    try {
      const j = await res.json();
      if (j && j.detail) msg = typeof j.detail === "string" ? j.detail : msg;
    } catch (_) {}
    if (res.status === 401) showLogin("Sesi berakhir, silakan masuk lagi.");
    throw new Error(msg);
  }
  let blob;
  const total = Number(
    (res.headers && res.headers.get && res.headers.get("Content-Length")) || 0,
  );
  if (onProgress && res.body && res.body.getReader) {
    // Baca bertahap supaya persentase unduhan nyata
    const rd = res.body.getReader(),
      parts = [];
    let got = 0;
    for (;;) {
      const { done, value } = await rd.read();
      if (done) break;
      parts.push(value);
      got += value.length;
      onProgress(got, total);
    }
    blob = new Blob(parts, {
      type: res.headers.get("Content-Type") || "application/octet-stream",
    });
  } else {
    blob = await res.blob();
    if (onProgress) onProgress(blob.size || total, total || blob.size);
  }
  const cd =
    (res.headers &&
      res.headers.get &&
      res.headers.get("Content-Disposition")) ||
    "";
  const m = cd.match(/filename="?([^";]+)"?/);
  const name = m ? m[1] : "netgis-export";
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  (document.body || document).appendChild(a);
  a.click();
  if (a.remove) a.remove();
  setTimeout(() => {
    try {
      URL.revokeObjectURL(a.href);
    } catch (_) {}
  }, 3000);
  const g = (h) =>
    (res.headers && res.headers.get && res.headers.get(h)) || null;
  return {
    name,
    nodes: g("X-Export-Nodes"),
    cables: g("X-Export-Cables"),
    incidents: g("X-Export-Incidents"),
  };
}
function downloadExport() {
  const q = new URLSearchParams({
    format: $id("exp-format").value,
    scope: $id("exp-scope").value,
    type: $id("exp-type").value,
    status: $id("exp-status").value,
    installation: $id("exp-install").value,
    region: ($id("exp-region") || {}).value || "ALL",
    cluster: $id("exp-cluster").value,
    area: $id("exp-area").value || "ALL",
    folder: ($id("exp-folder") || {}).value || "",
    q: ($id("exp-q").value || "").trim(),
    delimiter: $id("exp-delim").value || ",",
  });
  const btn = $id("exp-btn");
  setBtnBusy(btn, true);
  setDataMsg("exp-msg", "", "");
  startDataProgress("exp-prog", "Server menyiapkan berkas...", null);
  const fmtKb = (n) =>
    n >= 1048576
      ? (n / 1048576).toFixed(1) + " MB"
      : Math.max(1, Math.round(n / 1024)) + " KB";
  return downloadFromApi(
    "/api/export?" + q.toString(),
    undefined,
    (got, total) =>
      updateDataProgress(
        "exp-prog",
        total
          ? `Mengunduh ${fmtKb(got)} dari ${fmtKb(total)}`
          : `Mengunduh ${fmtKb(got)}...`,
        total ? (got * 100) / total : null,
      ),
  )
    .then((r) => {
      stopDataProgress("exp-prog");
      setDataMsg(
        "exp-msg",
        `Diunduh: ${r.name}${r.nodes != null ? ` (${r.nodes} node, ${r.cables} kabel${Number(r.incidents) ? `, ${r.incidents} incident` : ""})` : ""}`,
        "ok",
      );
    })
    .catch((err) => {
      stopDataProgress("exp-prog");
      setDataMsg("exp-msg", "Gagal mengunduh: " + err.message, "err");
    })
    .finally(() => {
      setBtnBusy(btn, false);
    });
}
function downloadTemplate(kind, format) {
  return downloadFromApi(
    `/api/import/template?kind=${kind}&delimiter=${encodeURIComponent(";")}${format ? "&format=" + format : ""}`,
  ).catch((err) =>
    setDataMsg("imp-msg", "Gagal mengunduh template: " + err.message, "err"),
  );
}
// ===== RAPIKAN TOPOLOGI (hasil impor KMZ): kapasitas dari nama kabel, pecah kabel massal, ujung kabel yatim =====
const TF = {
  tab: "puzzle",
  busy: false,
  data: {
    puzzle: null,
    cap: null,
    split: null,
    ends: null,
    conn: null,
    cust: null,
    comp: null,
  },
  sel: {
    puzzle: new Set(),
    cap: new Set(),
    split: new Set(),
    ends: new Set(),
    conn: new Set(),
    cust: new Set(),
    comp: new Set(),
  },
  chosen: {},
  q: "",
  max: { split: 15, ends: 100, cust: 500, comp: 500 },
  opt: { orient: true, trunk: false, connect: true },
  batches: [],
  lastBatch: null,
  types: {
    split: ["CLOSURE", "ODP", "POP"],
    ends: ["CLOSURE", "ODP", "POP", "SLACK", "PELANGGAN"],
  },
};
TF.cores = {}; // cable_id -> { free: [label], chosen: [label], open: bool }
// hanya Rangkai (puzzle) dan Topologi yang tampil; tab perapian massal lama disembunyikan (kode & endpoint tetap ada)
const TF_TABS = [
  ["puzzle", "Rangkai (puzzle)"],
  ["topo", "Topologi"],
];
const TF_PATH = {
  cap: "capacity",
  split: "split",
  ends: "ends",
  conn: "connect",
  cust: "customers",
  comp: "components",
};
function tfSelectable(tab, x) {
  return tab === "cap"
    ? !x.blocked
    : tab === "conn"
      ? x.state === "siap"
      : tab === "comp"
        ? !!x.from_id
        : true;
}
function openTopofixModal() {
  if (!can("asset.write")) return;
  $id("modal-topofix").style.display = "flex";
  tfRenderTabs();
  tfScan(TF.tab);
}
function closeTopofixModal() {
  if (TF.busy) return;
  $id("modal-topofix").style.display = "none";
}
function tfSetMsg(text, cls) {
  setDataMsg("tf-msg", text, cls);
}
function tfTab(t) {
  if (TF.busy) return;
  TF.tab = t;
  TF.q = "";
  if ($id("tf-body")) $id("tf-body").innerHTML = ""; // buang input tab sebelumnya agar tidak terbaca sebagai parameter tab ini
  tfRenderTabs();
  if (t !== "puzzle" && t !== "topo" && !TF.data[t]) tfScan(t);
  else tfRenderBody();
}
function tfRenderTabs() {
  $id("tf-tabs").innerHTML = TF_TABS.map(([k, l]) => {
    const d = TF.data[k];
    return `<button type="button" class="tf-tab ${k === TF.tab ? "on" : ""}" onclick="tfTab('${k}')">${escapeHtml(l)}${d ? ` <span class="tf-cnt">${d.rows.length}</span>` : ""}</button>`;
  }).join("");
}
function tfParams() {
  const num = (id, dflt) => {
    const e = $id(id);
    const n = e ? Number(e.value) : NaN;
    return Number.isFinite(n) && n > 0 ? n : dflt;
  };
  if (TF.tab === "split" || TF.tab === "ends") {
    const k = TF.tab,
      all =
        k === "split"
          ? ["CLOSURE", "ODP", "POP"]
          : ["CLOSURE", "ODP", "POP", "SLACK", "PELANGGAN"];
    if ($id("tf-max"))
      TF.max[k] = Math.min(k === "split" ? 50 : 300, num("tf-max", TF.max[k]));
    if ($id("tf-t-" + all[0]))
      TF.types[k] = all.filter((t) => {
        const e = $id("tf-t-" + t);
        return e && e.checked;
      });
    return { max_m: TF.max[k], types: TF.types[k].length ? TF.types[k] : all };
  }
  if (TF.tab === "conn") {
    if ($id("tf-o-orient")) TF.opt.orient = $id("tf-o-orient").checked;
    if ($id("tf-o-trunk")) TF.opt.trunk = $id("tf-o-trunk").checked;
    return { trunk: TF.opt.trunk };
  }
  if (TF.tab === "cust" || TF.tab === "comp") {
    const k = TF.tab;
    if ($id("tf-max"))
      TF.max[k] = Math.min(3000, Math.max(20, num("tf-max", TF.max[k])));
    if (k === "cust" && $id("tf-o-connect"))
      TF.opt.connect = $id("tf-o-connect").checked;
    return { max_m: TF.max[k] };
  }
  return {};
}
function tfScan(tab) {
  if (tab === "puzzle" || tab === "topo") {
    tfRenderTabs();
    tfRenderBody();
    return Promise.resolve();
  }
  if (TF.busy) return Promise.resolve();
  TF.busy = true;
  tfSetMsg("Memindai data…", "");
  const path = TF_PATH[tab];
  return apiRequest(
    `/api/topofix/${path}/preview`,
    "POST",
    tab === TF.tab ? tfParams() : {},
  )
    .then(async (r) => {
      TF.data[tab] = r;
      TF.sel[tab] = new Set();
      r.rows.forEach((x) => {
        if (tab === "cap" ? !x.blocked : x.checked)
          TF.sel[tab].add(tfKey(tab, x));
      });
      TF.chosen = tab === "ends" || tab === "cust" ? {} : TF.chosen;
      if (tab === "conn") {
        TF.cores = {};
        try {
          TF.batches =
            (await apiRequest("/api/topofix/connect/batches")).batches || [];
        } catch (_) {
          TF.batches = [];
        }
      }
      tfSetMsg(
        r.truncated
          ? `Hanya ${r.rows.length} baris pertama yang ditampilkan.`
          : "",
        "",
      );
    })
    .catch((err) => {
      TF.data[tab] = { rows: [] };
      tfSetMsg("Gagal memindai: " + err.message, "err");
    })
    .finally(() => {
      TF.busy = false;
      tfRenderTabs();
      tfRenderBody();
    });
}
function tfKey(tab, x) {
  return tab === "cap"
    ? String(x.id)
    : tab === "split"
      ? `${x.cable_id}:${x.node_id}`
      : tab === "conn"
        ? String(x.cable_id)
        : tab === "cust"
          ? String(x.customer_id)
          : tab === "comp"
            ? `${x.component}:${x.from_id || 0}:${x.to_id || 0}`
            : `${x.cable_id}:${x.end}`;
}
function tfRows() {
  const d = TF.data[TF.tab];
  if (!d) return [];
  const q = (TF.q || "").trim().toLowerCase();
  if (!q) return d.rows;
  return d.rows.filter((x) =>
    JSON.stringify([
      x.name,
      x.cable_name,
      x.node_name,
      x.node_type,
      x.cable_type,
      x.up_name,
      x.down_name,
      x.customer_name,
      x.odp_name,
      x.sample,
      x.from_name,
      x.to_name,
    ])
      .toLowerCase()
      .includes(q),
  );
}
function tfToggle(key, on) {
  const s = TF.sel[TF.tab];
  if (on) s.add(key);
  else s.delete(key);
  tfRenderCount();
}
function tfAll(on) {
  const s = TF.sel[TF.tab];
  tfRows().forEach((x) => {
    if (!tfSelectable(TF.tab, x)) return;
    const k = tfKey(TF.tab, x);
    if (on) s.add(k);
    else s.delete(k);
  });
  tfRenderBody();
}
function tfRenderCount() {
  const n = TF.sel[TF.tab].size,
    go = $id("tf-go");
  if (go) {
    go.disabled = !n || TF.busy;
    const sp = go.querySelector ? go.querySelector("span") : null;
    if (sp) sp.textContent = `Terapkan ${n} terpilih`;
  }
}
function tfChoose(key, nodeId) {
  TF.chosen[key] = Number(nodeId);
}
function tfRenderBody() {
  const el = $id("tf-body");
  if (!el) return;
  const own = TF.tab === "puzzle" || TF.tab === "topo";
  const goBtn = $id("tf-go");
  if (goBtn) goBtn.style.display = own ? "none" : "";
  const pzBox = $id("tf-puzzle");
  if (pzBox) pzBox.style.display = TF.tab === "puzzle" ? "" : "none";
  const tpBox = $id("tf-topo");
  if (tpBox) tpBox.style.display = TF.tab === "topo" ? "" : "none";
  el.style.display = own ? "none" : "";
  if (TF.tab === "puzzle") {
    pzOpen();
    return;
  }
  if (TF.tab === "topo") {
    topoOpen();
    return;
  }
  const t = TF.tab,
    d = TF.data[t];
  const intro = {
    cap: "Nama kabel memuat petunjuk: KU12 = kabel udara 12 core, KT24 = kabel tanah 24 core, 2C- = 2 core. Pilih kolom yang diterapkan, lalu centang baris.",
    split:
      "Closure/ODP/POP yang berada tepat di atas jalur kabel tetapi bukan ujungnya. Kabel dipecah di titik itu agar rantai POP → closure → ODP tersambung. Slack tidak dipecah.",
    ends: "Kabel yang salah satu ujungnya belum tersambung ke aset. Usulan adalah aset terdekat dalam jarak maksimum; ujung kabel tidak dipindahkan, hanya dihubungkan. Untuk pengecualian, gunakan 'Seret di peta'.",
    conn: "Menyambung core massal dari POP: tiap kabel ke ODP/pelanggan disambung (ODP 1 core; pelanggan 1 core dari ODP atau 2 core dedicated dari closure) dan umpan core ke hulu sampai POP dialokasikan otomatis. Urut dari yang terdekat ke POP. Satu batch bisa dibatalkan seluruhnya.",
    cust: "Pelanggan yang belum punya kabel dipasangkan ke ODP: nama yang mirip, lalu tag PON sama ([olt@1/1/1] dengan [1/1/1]), lalu terdekat, hanya ODP yang masih punya port OUT. Menerapkan membuat kabel Drop 2C (ODP → pelanggan) dan, bila dipilih, menyambung 1 core.",
    comp: "Kelompok aset (closure/ODP/pelanggan) yang belum terhubung ke POP, dengan aset jaringan POP terdekat. 'Hubungkan' membuat kabel penghubung garis lurus bernama LINK-… (ubah jalurnya di peta bila perlu). Tidak ada yang dicentang otomatis: periksa tiap baris.",
  }[t];
  let opts = "";
  if (t === "cap")
    opts = `<label class="cb-check"><input type="checkbox" id="tf-o-cap" checked> Kapasitas</label><label class="cb-check"><input type="checkbox" id="tf-o-type"> Jenis kabel</label><label class="cb-check"><input type="checkbox" id="tf-o-inst" checked> Pemasangan</label>`;
  if (t === "split")
    opts =
      `<label>Jarak maks (m) <input type="number" id="tf-max" min="1" max="50" step="1" value="${TF.max.split}" style="width:70px"></label>` +
      ["CLOSURE", "ODP", "POP"]
        .map(
          (x) =>
            `<label class="cb-check"><input type="checkbox" id="tf-t-${x}" ${TF.types.split.includes(x) ? "checked" : ""}> ${x}</label>`,
        )
        .join("") +
      `<button type="button" class="data-ghost" onclick="tfScan('split')"><i class="fa-solid fa-rotate"></i> Pindai ulang</button>`;
  if (t === "ends")
    opts =
      `<label>Jarak maks (m) <input type="number" id="tf-max" min="5" max="300" step="5" value="${TF.max.ends}" style="width:70px"></label>` +
      ["CLOSURE", "ODP", "POP", "SLACK", "PELANGGAN"]
        .map(
          (x) =>
            `<label class="cb-check"><input type="checkbox" id="tf-t-${x}" ${TF.types.ends.includes(x) ? "checked" : ""}> ${x}</label>`,
        )
        .join("") +
      `<button type="button" class="data-ghost" onclick="tfScan('ends')"><i class="fa-solid fa-rotate"></i> Pindai ulang</button>`;
  const mx = (k, lo, hi) =>
    `<label>Jarak maks (m) <input type="number" id="tf-max" min="${lo}" max="${hi}" step="10" value="${TF.max[k]}" style="width:80px"></label>`;
  if (t === "ends")
    opts += `<button type="button" class="data-ghost" onclick="tfStartDrag()"><i class="fa-solid fa-hand"></i> Seret di peta</button>`;
  if (t === "conn")
    opts =
      `<label class="cb-check"><input type="checkbox" id="tf-o-orient" ${TF.opt.orient ? "checked" : ""}> Samakan arah kabel hulu→hilir lebih dulu${d && d.orient_needed ? ` (${d.orient_needed} kabel akan dibalik)` : ""}</label>` +
      `<label class="cb-check"><input type="checkbox" id="tf-o-trunk" ${TF.opt.trunk ? "checked" : ""}> Sertakan kabel antar closure/POP (sambung penuh)</label><button type="button" class="data-ghost" onclick="tfScan('conn')"><i class="fa-solid fa-rotate"></i> Pindai ulang</button>`;
  if (t === "cust")
    opts =
      mx("cust", 20, 3000) +
      `<label class="cb-check"><input type="checkbox" id="tf-o-connect" ${TF.opt.connect ? "checked" : ""}> Sambungkan 1 core (ODP OUT → pelanggan)</label><button type="button" class="data-ghost" onclick="tfScan('cust')"><i class="fa-solid fa-rotate"></i> Pindai ulang</button>`;
  if (t === "comp")
    opts =
      mx("comp", 20, 3000) +
      `<button type="button" class="data-ghost" onclick="tfScan('comp')"><i class="fa-solid fa-rotate"></i> Pindai ulang</button>`;
  const rows = tfRows();
  let head = "",
    trs = "";
  const chk = (k, x) =>
    `<input type="checkbox" ${tfSelectable(t, x) ? "" : "disabled"} ${TF.sel[t].has(k) ? "checked" : ""} onchange="tfToggle('${k}', this.checked)">`;
  if (t === "conn") {
    head = `<th></th><th>Kabel</th><th>Hulu → Hilir</th><th class="num">Core</th><th>Status</th>`;
    trs = rows
      .map((x) => {
        const k = tfKey(t, x);
        return (
          `<tr class="${x.state === "terpisah" ? "tf-bad" : ""}"><td>${chk(k, x)}</td><td>${escapeHtml(x.cable_name)}<br><small>${escapeHtml(x.cable_type || "")} ${escapeHtml(x.capacity || "")}</small></td>` +
          `<td>${escapeHtml(x.up_name)} <small>${escapeHtml(x.up_type)}</small> &rarr; ${escapeHtml(x.down_name)} <small>${escapeHtml(x.down_type)}</small></td><td class="num">${tfCoreCell(x)}</td>` +
          `<td>${x.state === "siap" ? "siap" : `<span class="tf-ket">${escapeHtml(x.state)}</span><div class="adm-hint">${escapeHtml(x.reason || "")}</div>`}</td></tr>`
        );
      })
      .join("");
  } else if (t === "cust") {
    head = `<th></th><th>Pelanggan</th><th>ODP usulan</th><th class="num">Jarak (m)</th><th>Dasar</th><th class="num">Port kosong</th>`;
    trs = rows
      .map((x) => {
        const k = tfKey(t, x);
        const sel = (x.alternatives || []).length
          ? `<select onchange="tfChoose('${k}', this.value)" aria-label="ODP tujuan">` +
            [
              {
                odp_id: x.odp_id,
                odp_name: x.odp_name,
                distance_m: x.distance_m,
              },
              ...x.alternatives,
            ]
              .map(
                (a) =>
                  `<option value="${a.odp_id}">${escapeHtml(a.odp_name)} (${a.distance_m} m)</option>`,
              )
              .join("") +
            `</select>`
          : `<b>${escapeHtml(x.odp_name)}</b>`;
        return `<tr><td>${chk(k, x)}</td><td>${escapeHtml(x.customer_name)}</td><td>${sel}${x.odp_reachable ? "" : `<div class="tf-ket">ODP ini belum terhubung ke POP</div>`}</td><td class="num">${x.distance_m}</td><td>${escapeHtml(x.basis)}</td><td class="num">${x.odp_free}</td></tr>`;
      })
      .join("");
  } else if (t === "comp") {
    head = `<th></th><th>Kelompok terpisah</th><th class="num">Isi</th><th>Hubungkan ke jaringan POP</th><th class="num">Jarak (m)</th><th></th>`;
    trs = rows
      .map((x) => {
        const k = tfKey(t, x);
        return (
          `<tr><td>${chk(k, x)}</td><td>${escapeHtml(x.sample)}<br><small>${x.size} aset</small></td><td class="num"><small>${x.closures} closure, ${x.odps} ODP, ${x.customers} pelanggan</small></td>` +
          `<td>${x.from_id ? `${escapeHtml(x.from_name)} <small>${escapeHtml(x.from_type)}</small> &rarr; ${escapeHtml(x.to_name)} <small>${escapeHtml(x.to_type)}</small>` : `<span class="tf-ket">tidak ada aset jaringan dalam ${TF.max.comp} m</span>`}</td>` +
          `<td class="num">${x.distance_m == null ? "-" : x.distance_m}</td><td><button type="button" class="data-ghost" onclick="tfFly(${Number(x.lat)}, ${Number(x.lng)})">Lihat</button></td></tr>`
        );
      })
      .join("");
  } else if (t === "cap") {
    head = `<th></th><th>Kabel</th><th>Jenis</th><th>Kapasitas</th><th>Pemasangan</th>`;
    trs = rows
      .map((x) => {
        const k = tfKey(t, x);
        const ch = (a, b) =>
          b
            ? `${escapeHtml(a || "-")} &rarr; <b>${escapeHtml(b)}</b>`
            : escapeHtml(a || "-");
        return (
          `<tr class="${x.blocked ? "tf-bad" : ""}"><td><input type="checkbox" ${x.blocked ? "disabled" : ""} ${TF.sel[t].has(k) ? "checked" : ""} onchange="tfToggle('${k}', this.checked)" aria-label="Pilih ${escapeHtml(x.name)}"></td>` +
          `<td>${escapeHtml(x.name)}${x.blocked ? `<div class="adm-hint">${escapeHtml(x.blocked)}</div>` : ""}</td><td>${ch(x.type, x.new_type)}</td><td>${ch(x.capacity, x.new_capacity)}</td><td>${ch(x.installation, x.new_installation)}</td></tr>`
        );
      })
      .join("");
  } else if (t === "split") {
    head = `<th></th><th>Aset</th><th>Kabel</th><th class="num">Jarak ke kabel (m)</th><th class="num">Posisi (m)</th><th>Catatan</th>`;
    trs = rows
      .map((x) => {
        const k = tfKey(t, x);
        return (
          `<tr><td><input type="checkbox" ${TF.sel[t].has(k) ? "checked" : ""} onchange="tfToggle('${k}', this.checked)" aria-label="Pilih ${escapeHtml(x.node_name)}"></td>` +
          `<td><b>${escapeHtml(x.node_name)}</b><br><small>${escapeHtml(x.node_type)}</small></td><td>${escapeHtml(x.cable_name)}<br><small>${escapeHtml(x.cable_type || "")} ${escapeHtml(x.capacity || "")}</small></td>` +
          `<td class="num">${x.offset_m}</td><td class="num">${fmtInt(x.along_m)} / ${fmtInt(x.total_m)}</td><td class="tf-ket">${(x.warn || []).map((w) => escapeHtml(w)).join("; ")}</td></tr>`
        );
      })
      .join("");
  } else {
    head = `<th></th><th>Kabel</th><th>Ujung</th><th>Hubungkan ke</th><th class="num">Jarak (m)</th>`;
    trs = rows
      .map((x) => {
        const k = tfKey(t, x);
        const alts = (x.alternatives || []).length
          ? `<select onchange="tfChoose('${k}', this.value)" aria-label="Aset tujuan">` +
            [
              {
                node_id: x.node_id,
                node_name: x.node_name,
                node_type: x.node_type,
                distance_m: x.distance_m,
              },
              ...x.alternatives,
            ]
              .map(
                (a) =>
                  `<option value="${a.node_id}">${escapeHtml(a.node_name)} (${escapeHtml(a.node_type)}, ${a.distance_m} m)</option>`,
              )
              .join("") +
            `</select>`
          : `<b>${escapeHtml(x.node_name)}</b> <small>${escapeHtml(x.node_type)}</small>`;
        return (
          `<tr><td><input type="checkbox" ${TF.sel[t].has(k) ? "checked" : ""} onchange="tfToggle('${k}', this.checked)" aria-label="Pilih ${escapeHtml(x.cable_name)}"></td>` +
          `<td>${escapeHtml(x.cable_name)}<br><small>${escapeHtml(x.cable_type || "")}</small></td><td>${x.end === "from" ? "Asal" : "Tujuan"}</td><td>${alts}</td><td class="num">${x.distance_m}</td></tr>`
        );
      })
      .join("");
  }
  let extra = "";
  if (t === "conn" && d) {
    const sm = d.summary || {};
    extra += `<div class="adm-hint">Siap: ${sm.siap || 0}, sudah tersambung: ${sm.sudah || 0}, terpisah dari POP: ${sm.terpisah || 0}.${d.open_ends_cables ? ` ${d.open_ends_cables} kabel masih punya ujung tanpa aset (selesaikan di tab Ujung kabel yatim).` : ""}</div>`;
    if (TF.batches.length)
      extra +=
        `<div class="tf-batches"><b>Batch sambung massal</b>: ` +
        TF.batches
          .slice(0, 6)
          .map(
            (b) =>
              `${escapeHtml(b.batch)} (${b.links} sambungan) <button type="button" class="data-ghost" onclick="tfUndoBatch('${escapeHtml(b.batch)}')">Batalkan</button>`,
          )
          .join(" &middot; ") +
        `</div>`;
  }
  if (t === "cust" && d && d.unmatched_total)
    extra += `<div class="adm-hint">${d.unmatched_total} pelanggan tidak punya ODP dengan port kosong dalam ${TF.max.cust} m: ${(
      d.unmatched || []
    )
      .slice(0, 6)
      .map((u) => escapeHtml(u.customer_name))
      .join(", ")}${d.unmatched_total > 6 ? ", …" : ""}</div>`;
  if (t === "comp" && d)
    extra += `<div class="adm-hint">Terhubung ke POP saat ini: ${d.reachable.CLOSURE} closure, ${d.reachable.ODP} ODP, ${d.reachable.PELANGGAN} pelanggan. ${d.unmatched || 0} kelompok tidak punya pasangan dalam jarak ini.</div>`;
  const un =
    extra +
    (t === "ends" && d && d.unmatched_total
      ? `<div class="adm-hint">${d.unmatched_total} ujung kabel tidak punya aset dalam jarak ini (naikkan jarak maksimum atau hubungkan manual): ${(
          d.unmatched || []
        )
          .slice(0, 8)
          .map(
            (u) =>
              escapeHtml(u.cable_name) +
              " (" +
              (u.end === "from" ? "asal" : "tujuan") +
              ")",
          )
          .join(", ")}${d.unmatched_total > 8 ? ", …" : ""}</div>`
      : "");
  el.innerHTML =
    `<p class="data-note">${escapeHtml(intro)}</p><div class="tf-tools tf-opts">${opts}</div>` +
    `<div class="tf-tools"><button type="button" class="data-ghost" onclick="tfAll(true)">Pilih semua</button><button type="button" class="data-ghost" onclick="tfAll(false)">Kosongkan</button>` +
    `<input type="search" id="tf-q" placeholder="Cari nama" value="${escapeHtml(TF.q)}" oninput="TF.q = this.value; tfRenderBody(); const q = $id('tf-q'); if (q) { q.focus(); q.setSelectionRange(q.value.length, q.value.length); }" autocomplete="off"><span class="adm-hint">${rows.length} baris &middot; terpilih ${TF.sel[t].size}</span></div>` +
    (rows.length
      ? `<div class="tf-scroll"><table class="adm-table tf-table"><thead><tr>${head}</tr></thead><tbody>${trs}</tbody></table></div>`
      : `<div class="adm-hint">${d ? "Tidak ada temuan. Data sudah rapi untuk bagian ini." : "Memindai…"}</div>`) +
    un;
  tfRenderCount();
}
async function tfGo() {
  if (TF.busy) return;
  const t = TF.tab,
    d = TF.data[t];
  const keys = d
    ? d.rows
        .filter((x) => TF.sel[t].has(tfKey(t, x)) && tfSelectable(t, x))
        .map((x) => tfKey(t, x))
    : [];
  if (!d || !keys.length) return;
  if (TF.tab === "conn" || TF.tab === "cust" || TF.tab === "comp") tfParams();
  if (
    !confirm(
      `Terapkan perubahan pada ${keys.length} baris sekarang? Perubahan tercatat di riwayat.`,
    )
  )
    return;
  TF.busy = true;
  const go = $id("tf-go");
  if (go) go.disabled = true;
  const byKey = new Map(d.rows.map((x) => [tfKey(t, x), x]));
  let ok = 0,
    links = 0,
    flipped = 0;
  const failed = [],
    infos = [];
  const batch = "B" + Date.now().toString(36);
  try {
    const par = { max_m: TF.max[t] || 15 };
    for (let i = 0; i < keys.length; i += 100) {
      const chunk = keys
        .slice(i, i + 100)
        .map((k) => byKey.get(k))
        .filter(Boolean);
      let r;
      if (t === "cap")
        r = await apiRequest("/api/topofix/capacity/apply", "POST", {
          ids: chunk.map((x) => x.id),
          capacity: !$id("tf-o-cap") || $id("tf-o-cap").checked,
          type: !!($id("tf-o-type") && $id("tf-o-type").checked),
          installation: !$id("tf-o-inst") || $id("tf-o-inst").checked,
        });
      else if (t === "split")
        r = await apiRequest("/api/topofix/split/apply", "POST", {
          pairs: chunk.map((x) => ({
            node_id: x.node_id,
            cable_id: x.cable_id,
          })),
          max_m: Math.max(par.max_m || 15, 15),
        });
      else if (t === "conn")
        r = await apiRequest("/api/topofix/connect/apply", "POST", {
          items: chunk.map((x) => {
            const cs = TF.cores[x.cable_id];
            const mp = coreMapPayload("tf:" + x.cable_id);
            const vc = mp
              ? mp.via
              : cs && cs.chosen && cs.chosen.length
                ? cs.chosen
                : null;
            return {
              cable_id: x.cable_id,
              cores: vc ? vc.length : x.cores || null,
              full: !vc && !!x.full,
              via_cores: vc,
              ...(mp
                ? { from_cores: mp.from, upstream_cable_id: mp.upstream }
                : {}),
            };
          }),
          batch,
          trunk: TF.opt.trunk,
          orient: TF.opt.orient,
        });
      else if (t === "cust")
        r = await apiRequest("/api/topofix/customers/apply", "POST", {
          items: chunk.map((x) => ({
            customer_id: x.customer_id,
            odp_id: TF.chosen[tfKey(t, x)] || x.odp_id,
          })),
          max_m: TF.max.cust,
          connect: TF.opt.connect,
        });
      else if (t === "comp")
        r = await apiRequest("/api/topofix/components/apply", "POST", {
          items: chunk
            .filter((x) => x.from_id)
            .map((x) => ({ from_id: x.from_id, to_id: x.to_id })),
        });
      else
        r = await apiRequest("/api/topofix/ends/apply", "POST", {
          items: chunk.map((x) => ({
            cable_id: x.cable_id,
            end: x.end,
            node_id: TF.chosen[tfKey(t, x)] || x.node_id,
          })),
          max_m: Math.max(par.max_m || 100, 100),
        });
      ok +=
        t === "split"
          ? r.nodes || 0
          : t === "conn"
            ? r.connected || 0
            : t === "cust" || t === "comp"
              ? r.created || 0
              : r.updated || 0;
      links += r.links || 0;
      flipped += r.flipped || 0;
      (r.notes || []).forEach((n) => infos.push(n));
      (r.failed || []).forEach((f) => failed.push(f));
      tfSetMsg(
        `Memproses ${Math.min(i + 100, keys.length)} / ${keys.length}…`,
        "",
      );
    }
  } catch (err) {
    failed.push({ reason: (err && err.message) || String(err) });
  }
  TF.busy = false;
  TF.data = {
    cap: null,
    split: null,
    ends: null,
    conn: null,
    cust: null,
    comp: null,
  };
  if (t === "conn" && ok) TF.lastBatch = batch;
  await ftAfterChange();
  await tfScan(t);
  const nm = (f) =>
    f.node_name ||
    f.name ||
    (f.customer_id
      ? "pelanggan #" + f.customer_id
      : f.cable_id
        ? "kabel #" + f.cable_id
        : "");
  const extraTxt =
    (t === "conn"
      ? ` (${links} sambungan${flipped ? `, ${flipped} kabel dibalik arahnya` : ""}; batch ${batch})`
      : "") +
    (infos.length
      ? `. Catatan: ${infos
          .slice(0, 3)
          .map((n) => (n.name || "") + " " + n.reason)
          .join(" | ")}`
      : "");
  tfSetMsg(
    `${ok} berhasil${extraTxt}${
      failed.length
        ? `, ${failed.length} dilewati: ` +
          failed
            .slice(0, 6)
            .map((f) => `${nm(f)}: ${f.reason}`)
            .join(" | ")
        : ""
    }`,
    failed.length ? "err" : "ok",
  );
}

async function tfUndoBatch(b) {
  if (
    TF.busy ||
    !confirm(
      `Batalkan semua sambungan batch ${b}? Kabel tidak berubah, hanya sambungan core yang dihapus.`,
    )
  )
    return;
  try {
    const r = await apiRequest(
      `/api/topofix/connect/batch/${encodeURIComponent(b)}`,
      "DELETE",
    );
    await ftAfterChange();
    TF.data.conn = null;
    await tfScan("conn");
    tfSetMsg(r.message || "Batch dibatalkan", "ok");
  } catch (err) {
    tfSetMsg("Gagal membatalkan: " + err.message, "err");
  }
}
function tfFly(lat, lng) {
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) return;
  $id("modal-topofix").style.display = "none";
  map.flyTo([lat, lng], 17, { duration: 1.2 });
}
// Seret-lepas ujung kabel yatim di peta (untuk pengecualian)
const TFD = { layer: null, bar: null, rows: [], left: 0 };
function tfdMsg(text, cls) {
  if (TFD.bar) {
    const m = TFD.bar.querySelector(".tfd-msg");
    if (m) {
      m.textContent = text;
      m.className = "tfd-msg " + (cls || "");
    }
  }
}
async function tfStartDrag() {
  if (TF.busy || !can("asset.write")) return;
  let r;
  try {
    r = await apiRequest("/api/topofix/open-ends");
  } catch (err) {
    tfSetMsg("Gagal memuat ujung kabel: " + err.message, "err");
    return;
  }
  if (!r.rows.length) {
    tfSetMsg("Tidak ada ujung kabel yatim.", "ok");
    return;
  }
  $id("modal-topofix").style.display = "none";
  tfStopDrag(true);
  TFD.layer = L.layerGroup().addTo(map);
  TFD.rows = r.rows;
  TFD.left = r.rows.length;
  r.rows.forEach((row) => {
    const mk = L.marker([row.lat, row.lng], {
      draggable: true,
      zIndexOffset: 1500,
      icon: L.divIcon({ className: "tfd-end", iconSize: [20, 20] }),
      title: `${row.cable_name} (${row.end === "from" ? "asal" : "tujuan"})`,
    });
    mk.bindTooltip(
      `${row.cable_name} · ujung ${row.end === "from" ? "asal" : "tujuan"}`,
    );
    mk.on("dragend", () => tfDrop(mk, row));
    mk.addTo(TFD.layer);
  });
  const bar = document.createElement("div");
  bar.className = "tfd-bar";
  bar.innerHTML = `<b>Seret ujung kabel</b> <span class="tfd-msg"></span> <button type="button" class="data-ghost" onclick="tfStopDrag()">Selesai</button>`;
  document.body.appendChild(bar);
  TFD.bar = bar;
  tfdMsg(
    `${TFD.left} ujung belum tersambung (penanda merah). Seret ke aset tujuan lalu lepas.`,
  );
  const pts = r.rows.slice(0, 500).map((x) => [x.lat, x.lng]);
  if (pts.length) map.fitBounds(pts, { maxZoom: 17, padding: [60, 60] });
}
function tfStopDrag(silent) {
  if (TFD.layer) {
    map.removeLayer(TFD.layer);
    TFD.layer = null;
  }
  if (TFD.bar) {
    TFD.bar.remove();
    TFD.bar = null;
  }
  if (!silent && $id("modal-topofix")) {
    TF.data.ends = null;
    $id("modal-topofix").style.display = "flex";
    tfScan(TF.tab);
  }
}
async function tfDrop(mk, row) {
  const ll = mk.getLatLng();
  const types = ["CLOSURE", "ODP", "POP", "SLACK", "PELANGGAN"];
  let best = null;
  allInventoryData.forEach((x) => {
    if (
      x.category !== "NODE" ||
      !types.includes(String(x.type || "").toUpperCase())
    )
      return;
    const d = map.distance(ll, [x.lat, x.lng]);
    if (d <= 300 && (!best || d < best.d)) best = { d, x };
  });
  const back = () => mk.setLatLng([row.lat, row.lng]);
  if (!best) {
    tfdMsg(
      "Tidak ada aset dalam 300 m dari titik lepas; penanda dikembalikan.",
      "err",
    );
    back();
    return;
  }
  const dEnd = map.distance([row.lat, row.lng], [best.x.lat, best.x.lng]);
  if (
    !confirm(
      `Hubungkan ujung ${row.end === "from" ? "asal" : "tujuan"} kabel '${row.cable_name}' ke ${best.x.type} '${best.x.name}' (${Math.round(dEnd)} m dari ujung kabel)?`,
    )
  ) {
    back();
    return;
  }
  try {
    await apiRequest("/api/topofix/ends/link", "POST", {
      cable_id: row.cable_id,
      end: row.end,
      node_id: best.x.id,
    });
    TFD.layer.removeLayer(mk);
    TFD.left -= 1;
    tfdMsg(`Tersambung ke ${best.x.name}. Sisa ${TFD.left} ujung.`, "ok");
    TF.data.ends = null;
    TF.data.conn = null;
    ftAfterChange();
  } catch (err) {
    tfdMsg("Gagal: " + err.message, "err");
    back();
  }
}

// ---------- CEK COVERAGE MASSAL: impor Excel -> hitung per lokasi -> tabel/peta/unduh Excel ----------
const CB = {
  name: "",
  b64: "",
  info: null,
  rows: [],
  results: [],
  running: false,
  cancel: false,
  filter: "ALL",
  q: "",
  layer: null,
  params: null,
};
const CB_CHUNK = 8;
const cbRp = (n) => (n == null ? "-" : "Rp " + fmtInt(n));
function openCovBulkModal() {
  if (!can("plan.write")) return;
  $id("modal-covbulk").style.display = "flex";
  CB.cancel = false;
}
function closeCovBulkModal() {
  CB.cancel = true;
  $id("modal-covbulk").style.display = "none";
  cbClearMap(); // menutup form = titik hasil di peta ikut dihapus ("Lihat di peta" hanya menyembunyikan form)
}
function cbSetMsg(text, cls) {
  setDataMsg("cb-msg", text, cls);
}
function cbReset() {
  if (CB.running) return;
  cbClearMap();
  Object.assign(CB, {
    name: "",
    b64: "",
    info: null,
    rows: [],
    results: [],
    filter: "ALL",
    q: "",
    params: null,
  });
  $id("cb-file-name").textContent = "Pilih berkas Excel (.xlsx) daftar lokasi";
  $id("cb-step1").style.display = "";
  ["cb-params", "cb-preview", "cb-results"].forEach((i) => {
    $id(i).style.display = "none";
  });
  $id("cb-preview").innerHTML = "";
  $id("cb-results").innerHTML = "";
  $id("cb-run").innerHTML = '<i class="fa-solid fa-play"></i> Proses';
  ["cb-reset", "cb-map", "cb-dl", "cb-run"].forEach((i) => {
    $id(i).style.display = "none";
  });
  stopDataProgress("cb-prog");
  cbSetMsg("", "");
}
function cbTemplate() {
  return downloadFromApi("/api/coverage/bulk/template").catch((err) =>
    cbSetMsg("Gagal mengunduh template: " + err.message, "err"),
  );
}
function cbFileChosen(ev) {
  const f = ev && ev.target && ev.target.files && ev.target.files[0];
  if (!f) return;
  if (f.size > 8 * 1024 * 1024)
    return cbSetMsg("Berkas lebih dari 8 MB.", "err");
  if (!/\.xlsx?m?$/i.test(f.name) || /\.xls$/i.test(f.name))
    return cbSetMsg("Gunakan berkas Excel .xlsx.", "err");
  const rd = new FileReader();
  rd.onload = () => {
    CB.name = f.name;
    CB.b64 = bufToBase64(rd.result);
    $id("cb-file-name").textContent = f.name;
    cbParse();
  };
  rd.onerror = () => cbSetMsg("Berkas tidak dapat dibaca.", "err");
  rd.readAsArrayBuffer(f);
  try {
    ev.target.value = "";
  } catch (e) {
    /* abaikan */
  }
}
function cbParse() {
  cbSetMsg("", "");
  return apiRequest("/api/coverage/bulk/parse", "POST", {
    filename: CB.name,
    content_base64: CB.b64,
  })
    .then((d) => {
      CB.info = d;
      CB.rows = d.rows;
      CB.results = [];
      const sel = $id("cb-region");
      sel.innerHTML =
        `<option value="AUTO">Otomatis (cluster catuan, bawaan ${escapeHtml(d.default_region)})</option>` +
        d.regions
          .map(
            (g) => `<option value="${escapeHtml(g)}">${escapeHtml(g)}</option>`,
          )
          .join("");
      $id("cb-tax-pct").textContent =
        `(${String(d.tax_pct).replace(".", ",")}%)`;
      $id("cb-khs-note").textContent = d.khs_source
        ? `Sumber harga: ${d.khs_source}. Katalog dan PPN diatur di menu KHS.`
        : "";
      $id("cb-params").style.display = "";
      $id("cb-step1").style.display = "none";
      $id("cb-reset").style.display = "";
      $id("cb-run").style.display = d.valid ? "" : "none";
      $id("cb-run").innerHTML = '<i class="fa-solid fa-play"></i> Proses';
      $id("cb-dl").style.display = "none";
      $id("cb-map").style.display = "none";
      $id("cb-results").style.display = "none";
      $id("cb-results").innerHTML = "";
      cbRenderPreview();
      cbSetMsg(
        `${d.valid} lokasi siap diproses` +
          (d.invalid
            ? `, ${d.invalid} baris koordinatnya tidak terbaca (ditandai merah, tetap tampil di hasil sebagai galat).`
            : ".") +
          (d.skipped
            ? ` ${d.skipped} baris tanpa nama dan koordinat dilewati.`
            : ""),
        d.invalid ? "warn" : "ok",
      );
    })
    .catch((err) => cbSetMsg(err.message, "err"));
}
function cbRenderPreview() {
  const el = $id("cb-preview");
  el.style.display = "";
  el.innerHTML =
    `<div class="cb-scroll"><table class="adm-table cb-table"><thead><tr><th>No</th><th>Nama lokasi</th><th>Area</th><th>Koordinat</th><th>Status berkas</th></tr></thead><tbody>` +
    CB.rows
      .map(
        (r) =>
          `<tr><td>${Number(r.no)}</td><td>${escapeHtml(r.name)}</td><td>${escapeHtml(r.area)}</td><td>${escapeHtml(r.coord_text)}</td>` +
          (r.error
            ? `<td class="cb-bad">${escapeHtml(r.error)}</td>`
            : `<td>OK</td>`) +
          `</tr>`,
      )
      .join("") +
    `</tbody></table></div>`;
}
function cbParams() {
  return {
    radius_m: Math.max(
      50,
      Math.min(5000, Number($id("cb-radius").value) || 5000),
    ),
    region: $id("cb-region").value || "AUTO",
    installation: $id("cb-inst").value,
    cores: Number($id("cb-cores").value) || 1,
    slack_pref_m: Math.max(
      0,
      Math.min(20000, Number($id("cb-slackpref").value) || 0),
    ),
    with_cost: $id("cb-cost").checked,
    include_tax: $id("cb-tax").checked,
  };
}
async function cbRun() {
  if (CB.running || !CB.rows.length) return;
  CB.running = true;
  CB.cancel = false;
  cbClearMap();
  CB.params = cbParams();
  const todo = CB.rows.filter((r) => !r.error);
  const out = new Map();
  CB.rows
    .filter((r) => r.error)
    .forEach((r) =>
      out.set(r.no + ":" + r.excel_row, {
        no: r.no,
        name: r.name,
        area: r.area,
        lat: null,
        lng: null,
        status: "ERR",
        covered: false,
        catuan: null,
        alternatives: [],
        cost: null,
        boq: [],
        warnings: [],
        keterangan: r.error,
      }),
    );
  setBtnBusy($id("cb-run"), true);
  ["cb-params", "cb-preview", "cb-results"].forEach((i) => {
    $id(i).style.display = "none";
  });
  $id("cb-reset").style.display = "none";
  cbSetMsg("", "");
  startDataProgress("cb-prog", `Menghitung 0 dari ${todo.length} lokasi...`, 0);
  let done = 0,
    failMsg = "";
  try {
    for (let i = 0; i < todo.length && !CB.cancel; i += CB_CHUNK) {
      const part = todo.slice(i, i + CB_CHUNK);
      try {
        const d = await apiRequest(
          "/api/coverage/bulk/run",
          "POST",
          Object.assign({}, CB.params, {
            rows: part.map((r) => ({
              no: r.no,
              name: r.name,
              area: r.area,
              lat: r.lat,
              lng: r.lng,
            })),
          }),
        );
        d.results.forEach((res, k) =>
          out.set(part[k].no + ":" + part[k].excel_row, res),
        );
      } catch (err) {
        failMsg = err.message;
        part.forEach((r) =>
          out.set(r.no + ":" + r.excel_row, {
            no: r.no,
            name: r.name,
            area: r.area,
            lat: r.lat,
            lng: r.lng,
            status: "ERR",
            covered: false,
            catuan: null,
            alternatives: [],
            cost: null,
            boq: [],
            warnings: [],
            keterangan: "Gagal dihitung: " + err.message,
          }),
        );
      }
      done += part.length;
      updateDataProgress(
        "cb-prog",
        `Menghitung ${done} dari ${todo.length} lokasi...`,
        (done * 100) / todo.length,
      );
    }
  } finally {
    CB.running = false;
    setBtnBusy($id("cb-run"), false);
    stopDataProgress("cb-prog");
  }
  CB.results = CB.rows
    .map((r) => out.get(r.no + ":" + r.excel_row))
    .filter(Boolean);
  $id("cb-params").style.display = "";
  $id("cb-preview").style.display = "none";
  $id("cb-reset").style.display = "";
  $id("cb-run").style.display = "";
  $id("cb-run").innerHTML = '<i class="fa-solid fa-rotate"></i> Hitung ulang';
  $id("cb-dl").style.display = CB.results.length ? "" : "none";
  $id("cb-map").style.display = CB.results.some((r) => r.lat != null)
    ? ""
    : "none";
  cbRenderResults();
  cbSetMsg(
    CB.cancel
      ? `Dihentikan: ${CB.results.length} dari ${CB.rows.length} lokasi sudah dihitung.`
      : failMsg
        ? `Selesai dengan galat: ${failMsg}`
        : `Selesai: ${CB.results.length} lokasi dihitung.`,
    failMsg ? "warn" : "ok",
  );
}
function cbFiltered() {
  const q = (CB.q || "").trim().toLowerCase();
  return CB.results.filter(
    (r) =>
      (CB.filter === "ALL" ||
        (CB.filter === "NEW"
          ? !!(r.need_new && r.need_new.label !== "Tidak")
          : r.status === CB.filter)) &&
      (!q ||
        (r.name + " " + (r.area || "") + " " + (r.catuan ? r.catuan.name : ""))
          .toLowerCase()
          .includes(q)),
  );
}
function cbSummary() {
  const R = CB.results;
  const y = R.filter((r) => r.status === "Y").length,
    t = R.filter((r) => r.status === "T").length,
    e = R.filter((r) => r.status === "ERR").length;
  const len = R.reduce((a, r) => a + (Number(r.pull_m) || 0), 0),
    cost = R.reduce((a, r) => a + ((r.cost && Number(r.cost.total)) || 0), 0);
  return { y, t, e, len, cost, n: R.length };
}
function cbRenderResults() {
  const el = $id("cb-results");
  const S = cbSummary();
  el.style.display = "";
  const rows = cbFiltered();
  el.innerHTML =
    `<div class="cb-sum"><span class="cb-chip">${S.n} lokasi</span><span class="cb-chip y">Tercover (Y): ${S.y}</span><span class="cb-chip t">Tidak tercover (T): ${S.t}</span>` +
    (S.e ? `<span class="cb-chip">Galat: ${S.e}</span>` : "") +
    `<span class="cb-chip">Total tarikan: ${fmtInt(S.len)} m</span><span class="cb-chip">Total estimasi biaya: ${cbRp(S.cost)}</span></div>` +
    `<div class="cb-tools"><select id="cb-f" onchange="CB.filter = this.value; cbRenderResults()" aria-label="Filter status"><option value="ALL">Semua</option><option value="Y">Tercover (Y)</option><option value="T">Tidak tercover (T)</option><option value="NEW">Perlu closure/ODP baru</option><option value="ERR">Galat</option></select>` +
    `<input type="search" id="cb-q" placeholder="Cari nama / area / catuan" value="${escapeHtml(CB.q)}" oninput="CB.q = this.value; cbRenderResultRows()" autocomplete="off" /><span class="adm-hint" id="cb-count"></span></div>` +
    `<div class="cb-scroll"><table class="adm-table cb-table"><thead><tr><th>No</th><th>Nama lokasi</th><th>Area</th><th>Tercover</th><th>Catuan terdekat</th><th>Closure/ODP baru</th><th class="num">Tarikan (m)</th><th class="num">Estimasi biaya</th><th>Keterangan</th></tr></thead><tbody id="cb-rows"></tbody></table></div>`;
  const fsel = $id("cb-f");
  if (fsel) fsel.value = CB.filter;
  cbRenderResultRows();
}
function cbRenderResultRows() {
  const rows = cbFiltered();
  const body = $id("cb-rows");
  if (!body) return;
  body.innerHTML =
    rows
      .map((r) => {
        const c = r.catuan;
        const warn = (r.warnings || []).length
          ? `<div class="adm-hint">${(r.warnings || []).map((w) => escapeHtml(w)).join("<br>")}</div>`
          : "";
        return (
          `<tr><td>${Number(r.no)}</td><td>${escapeHtml(r.name)}</td><td>${escapeHtml(r.area || "")}</td><td><span class="cb-yn ${escapeHtml(r.status)}">${escapeHtml(r.status === "ERR" ? "!" : r.status)}</span></td>` +
          `<td>${c ? `<b>${escapeHtml(c.name)}</b><br><small>${escapeHtml(c.type)}${c.free != null ? ` &middot; sisa ${Number(c.free)}` : ""} &middot; garis lurus ${fmtInt(c.straight_m)} m${c.route_source === "perkiraan" ? " &middot; rute perkiraan" : ""}</small>` : "-"}</td>` +
          `<td>${r.need_new ? (r.need_new.label === "Tidak" ? "Tidak" : `<span class="cb-new">${escapeHtml(r.need_new.label)}</span>`) : "-"}</td><td class="num">${r.pull_m != null ? fmtInt(r.pull_m) : "-"}</td><td class="num">${r.cost ? cbRp(r.cost.total) : "-"}</td><td class="cb-ket">${escapeHtml(r.keterangan || "")}${warn}</td></tr>`
        );
      })
      .join("") ||
    `<tr><td colspan="9" class="adm-hint">Tidak ada baris yang cocok.</td></tr>`;
  const ct = $id("cb-count");
  if (ct) ct.textContent = `${rows.length} dari ${CB.results.length} baris`;
}
function cbDownload() {
  if (!CB.results.length) return;
  const btn = $id("cb-dl");
  setBtnBusy(btn, true);
  return downloadFromApi("/api/coverage/bulk/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      results: CB.results,
      params: CB.params || cbParams(),
      source_name: CB.name,
    }),
  })
    .then((r) => cbSetMsg(`Diunduh: ${r.name}`, "ok"))
    .catch((err) => cbSetMsg("Gagal mengunduh: " + err.message, "err"))
    .finally(() => setBtnBusy(btn, false));
}
function cbClearMap() {
  if (CB.layer) {
    try {
      map.removeLayer(CB.layer);
    } catch (e) {
      /* abaikan */
    }
    CB.layer = null;
  }
  const b = $id("cb-maplegend");
  if (b && b.parentNode) b.parentNode.removeChild(b);
}
function cbShowOnMap() {
  const pts = CB.results.filter((r) => r.lat != null);
  if (!pts.length) return;
  cbClearMap();
  const g = L.layerGroup();
  const col = { Y: "#059669", T: "#dc2626", ERR: "#d97706" };
  const bounds = [];
  pts.forEach((r) => {
    const c = r.catuan;
    const html =
      `<b>${escapeHtml(r.name)}</b><br>${escapeHtml(r.area || "")}<br>Tercover: <b>${escapeHtml(r.status)}</b>` +
      (c ? `<br>Catuan: ${escapeHtml(c.name)} (${escapeHtml(c.type)})` : "") +
      (r.pull_m != null ? `<br>Tarikan: ${fmtInt(r.pull_m)} m` : "") +
      (r.need_new && r.need_new.label !== "Tidak"
        ? `<br>Perlu: <b>${escapeHtml(r.need_new.label)}</b>`
        : "") +
      (r.cost ? `<br>Estimasi biaya: ${cbRp(r.cost.total)}` : "") +
      `<br><small>${escapeHtml(r.keterangan || "")}</small>`;
    L.circleMarker([r.lat, r.lng], {
      radius: 8,
      color: "#fff",
      weight: 2,
      fillColor: col[r.status] || col.ERR,
      fillOpacity: 0.95,
    })
      .bindPopup(html)
      .addTo(g);
    bounds.push([r.lat, r.lng]);
    if (c && c.latitude != null) {
      L.polyline(
        [
          [c.latitude, c.longitude],
          [r.lat, r.lng],
        ],
        {
          color: col[r.status] || col.ERR,
          weight: 2,
          dashArray: "5 6",
          opacity: 0.8,
        },
      ).addTo(g);
      bounds.push([c.latitude, c.longitude]);
    }
  });
  g.addTo(map);
  CB.layer = g;
  try {
    map.fitBounds(bounds, { padding: [40, 40], maxZoom: 15 });
  } catch (e) {
    /* abaikan */
  }
  const S = cbSummary();
  const bar = document.createElement("div");
  bar.id = "cb-maplegend";
  bar.className = "cb-maplegend";
  bar.innerHTML = `<b>Coverage massal</b> &middot; <span style="color:#059669">&#9679; Y ${S.y}</span> &nbsp;<span style="color:#dc2626">&#9679; T ${S.t}</span> <button type="button" onclick="openCovBulkModal()">Buka tabel</button><button type="button" onclick="cbClearMap()">Tutup</button>`;
  document.body.appendChild(bar);
  $id("modal-covbulk").style.display = "none";
}
// ---------- IMPORT: sesi tersimpan di server, koreksi per baris, batas yang diatur admin ----------
const IMP = {
  sess: null,
  limits: null,
  list: [],
  filter: "error",
  q: "",
  page: 1,
  size: 50,
  data: null,
  reasons: [],
  editing: null,
  pick: null,
  seq: 0,
  qt: null,
  mt: null,
  sel: new Set(),
  anchor: null,
  undo: null,
  forceAct: null,
};
const IMP_NODE_TYPES = [
  "POP",
  "CLOSURE",
  "ODP",
  "TIANG",
  "HH",
  "SLACK",
  "PELANGGAN",
];
const IMP_CABLE_TYPES = ["Backbone", "Feeder", "Distribution", "Drop"];
const ACTION_LABEL = {
  create: "Dibuat",
  update: "Diperbarui",
  skip: "Dilewati",
  error: "Galat",
};
const fmtInt = (n) =>
  String(Math.round(Number(n) || 0)).replace(/\B(?=(\d{3})+(?!\d))/g, ".");
const fmtMb = (n) => String(Math.round(Number(n) * 10) / 10).replace(".", ",");
function loadImportLimits() {
  return apiRequest("/api/import/limits")
    .then((d) => {
      IMP.limits = d;
      renderImportLimits();
    })
    .catch(() => {});
}
function renderImportLimits() {
  const L = IMP.limits;
  if (!L) return;
  $id("imp-limit-info").textContent =
    `Maksimal ${fmtMb(L.max_mb)} MB dan ${fmtInt(L.max_records)} data per berkas. Titik = node, garis = kabel.`;
  const admin = can("import.config");
  $id("imp-limit-admin").style.display = admin ? "" : "none";
  if (admin) {
    $id("imp-lim-mb").value = L.max_mb;
    $id("imp-lim-rec").value = L.max_records;
    $id("imp-lim-hint").textContent =
      `Batas atas keras ${L.hard_max_mb} MB dan ${fmtInt(L.hard_max_records)} data (bawaan ${L.default_mb} MB / ${fmtInt(L.default_records)}). Naikkan juga batas ukuran body di proxy/web server bila ada.`;
  }
}
function saveImportLimits() {
  const mb = Number(String($id("imp-lim-mb").value).replace(",", ".")),
    rec = Number($id("imp-lim-rec").value);
  return apiRequest("/api/import/limits", "PUT", {
    max_mb: mb,
    max_records: rec,
  })
    .then((d) => {
      IMP.limits = d;
      renderImportLimits();
      setDataMsg("imp-msg", d.message, "ok");
    })
    .catch((err) => setDataMsg("imp-msg", err.message, "err"));
}
function loadImportSessions() {
  return apiRequest("/api/import/sessions")
    .then((d) => {
      IMP.list = d.sessions || [];
      renderImportSessions();
    })
    .catch(() => {});
}
function renderImportSessions() {
  const el = $id("imp-sessions");
  const cur = IMP.sess && IMP.sess.id;
  const rest = (IMP.list || []).filter((s) => s.id !== cur);
  if (!rest.length) {
    el.style.display = "none";
    el.innerHTML = "";
    return;
  }
  el.style.display = "";
  el.innerHTML =
    `<div class="imp-meta"><b>Sesi impor tersimpan</b> &middot; koreksi disimpan di server, bisa dilanjutkan kapan saja</div>` +
    rest
      .map(
        (s) =>
          `<div class="imp-sess"><div><b>${escapeHtml(s.filename)}</b> <small>#${s.id} &middot; ${s.total} data &middot; ${(s.counts && s.counts.error) || 0} galat &middot; ${s.edited} dikoreksi &middot; ${escapeHtml(s.owner)} &middot; ${escapeHtml(String(s.updated_at || "").slice(0, 16))}</small></div>` +
          `<div><button type="button" class="data-ghost" onclick="openImportSession(${s.id})">Lanjutkan</button> <button type="button" class="data-ghost" onclick="discardImportSession(${s.id})">Buang</button></div></div>`,
      )
      .join("");
}
// Unggah berkas mentah (tanpa base64) ke sesi baru
async function apiUploadRaw(url, file) {
  netBarStart();
  let res,
    data = null;
  try {
    res = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/octet-stream" },
      body: file,
    });
    try {
      data = await res.json();
    } catch (_) {
      /* tanpa body JSON */
    }
  } finally {
    netBarEnd();
  }
  if (res.status === 401) {
    showLogin("Sesi berakhir, silakan masuk lagi.");
    const e = new Error((data && data.detail) || "Belum login");
    e.status = 401;
    throw e;
  }
  if (!res.ok)
    throw new Error(
      (data && typeof data.detail === "string" && data.detail) ||
        `Permintaan gagal (${res.status})`,
    );
  return data;
}
function onImportFileChosen(ev) {
  const f = ev && ev.target && ev.target.files && ev.target.files[0];
  if (!f) return;
  try {
    ev.target.value = "";
  } catch (e) {
    /* abaikan */
  }
  const L = IMP.limits;
  if (L && f.size > L.max_bytes) {
    return setDataMsg(
      "imp-msg",
      `Berkas ${fmtMb(f.size / 1048576)} MB melebihi batas ${fmtMb(L.max_mb)} MB.` +
        (can("import.config")
          ? ' Anda Admin: naikkan batas pada bagian "Batas impor (Admin)" di bawah.'
          : " Minta Admin menaikkan batas impor, atau pecah berkas."),
      "err",
    );
  }
  const seq = ++IMP.seq;
  setDataMsg("imp-msg", "", "");
  startDataProgress("imp-prog", `Mengunggah dan memeriksa ${f.name}...`, null);
  const q = new URLSearchParams({
    filename: f.name,
    on_duplicate: $id("imp-dup").value || "skip",
  });
  return apiUploadRaw("/api/import/sessions/upload?" + q.toString(), f)
    .then((d) => {
      if (seq !== IMP.seq) return;
      stopDataProgress("imp-prog");
      impStartSession(d);
      loadImportSessions();
    })
    .catch((err) => {
      if (seq !== IMP.seq) return;
      stopDataProgress("imp-prog");
      setDataMsg("imp-msg", err.message, "err");
    });
}
function impStartSession(d) {
  {
    const f_ = $id("imp-form");
    if (f_ && f_.classList) f_.classList.remove("opts");
  }
  IMP.sess = d;
  IMP.filter = d.counts && d.counts.error ? "error" : "all";
  IMP.q = "";
  IMP.page = 1;
  IMP.editing = null;
  IMP.pick = null;
  $id("imp-q").value = "";
  $id("imp-file-name").textContent = d.filename;
  $id("imp-dup").value = d.on_duplicate || "skip";
  $id("imp-preview").style.display = "";
  renderImportSummary();
  return Promise.all([loadImportRows(), loadImportReasons()]);
}
function openImportSession(id) {
  setDataMsg("imp-msg", "", "");
  return apiRequest("/api/import/sessions/" + Number(id))
    .then((d) => {
      if (d.status !== "open") {
        setDataMsg("imp-msg", "Sesi ini sudah ditutup.", "err");
        return loadImportSessions();
      }
      return impStartSession(d).then(loadImportSessions);
    })
    .catch((err) => setDataMsg("imp-msg", err.message, "err"));
}
function discardImportSession(id) {
  const sid = id != null ? Number(id) : IMP.sess && IMP.sess.id;
  if (!sid) return resetImport();
  if (
    !confirm(
      `Buang sesi impor #${sid}? Berkas di server dan semua koreksi yang sudah dibuat akan dihapus. Data aset tidak berubah.`,
    )
  )
    return;
  return apiRequest("/api/import/sessions/" + sid, "DELETE")
    .then(() => {
      if (IMP.sess && IMP.sess.id === sid) resetImport();
      setDataMsg("imp-msg", "Sesi impor dibuang.", "ok");
      return loadImportSessions();
    })
    .catch((err) => setDataMsg("imp-msg", err.message, "err"));
}
function onImportDupChange() {
  if (!IMP.sess) return;
  return apiRequest("/api/import/sessions/" + IMP.sess.id, "PUT", {
    on_duplicate: $id("imp-dup").value || "skip",
  })
    .then((d) => {
      IMP.sess = d;
      IMP.page = 1;
      renderImportSummary();
      return Promise.all([loadImportRows(), loadImportReasons()]);
    })
    .catch((err) => setDataMsg("imp-msg", err.message, "err"));
}
function renderImportSummary() {
  const s = IMP.sess;
  if (!s) return;
  const c = s.counts || { create: 0, update: 0, skip: 0, error: 0 };
  const types = Object.keys(s.by_type || {})
    .map(
      (k) =>
        `<span class="imp-type">${escapeHtml(k)} <b>${s.by_type[k]}</b></span>`,
    )
    .join("");
  $id("imp-sess-head").innerHTML =
    `Sesi #${s.id} &middot; <b>${escapeHtml(s.filename)}</b> &middot; ${s.total} data &middot; tersimpan di server, boleh ditutup dan dilanjutkan nanti <button type="button" class="data-ghost imp-opts-btn" onclick="impToggleOpts()"><i class="fa-solid fa-sliders"></i> Berkas &amp; opsi</button>`;
  $id("imp-sum").innerHTML = `
      <div class="imp-box create"><b>${c.create}</b><span>akan dibuat</span></div>
      <div class="imp-box update"><b>${c.update}</b><span>diperbarui</span></div>
      <div class="imp-box skip"><b>${c.skip}</b><span>dilewati</span></div>
      <div class="imp-box error"><b>${c.error}</b><span>bergalat</span></div>`;
  const tab = (f, label, n) =>
    `<button type="button" class="imp-tab${IMP.filter === f ? " active" : ""}" data-f="${f}" onclick="impFilter('${f}')">${label} <b>${n}</b></button>`;
  $id("imp-tabs").innerHTML =
    tab("error", "Galat", c.error) +
    tab("warning", "Peringatan", s.warnings || 0) +
    tab("edited", "Dikoreksi", s.edited || 0) +
    tab("all", "Semua", s.total);
  $id("imp-meta").innerHTML =
    `${escapeHtml(String(s.format || "").toUpperCase())} &middot; ${s.nodes || 0} titik &middot; ${s.cables || 0} garis ${types ? "&middot; " + types : ""}` +
    (c.error
      ? ` &middot; <span class="err">Baris bergalat tidak diimpor kecuali dikoreksi</span>`
      : "");
  $id("imp-commit").disabled = c.create + c.update === 0;
  $id("imp-validate").disabled = false;
  $id("imp-report").disabled = false;
  renderImportBaskets();
  renderImportSessions();
  impLayout();
}
function impFilter(f) {
  IMP.filter = f;
  IMP.page = 1;
  IMP.editing = null;
  IMP.sel = new Set();
  IMP.anchor = null;
  renderImportSummary();
  return loadImportRows();
}
function onImpSearch() {
  clearTimeout(IMP.qt);
  IMP.qt = setTimeout(() => {
    IMP.q = ($id("imp-q").value || "").trim();
    IMP.page = 1;
    IMP.editing = null;
    IMP.sel = new Set();
    IMP.anchor = null;
    loadImportRows();
  }, 250);
}
function impPagerHtml(d) {
  if (!d.total) return "";
  const p = d.page,
    n = d.pages,
    sz = d.size || IMP.size;
  const from = (p - 1) * sz + 1,
    to = Math.min(d.total, p * sz);
  const keep = Array.from(new Set([1, 2, p - 1, p, p + 1, n - 1, n]))
    .filter((x) => x >= 1 && x <= n)
    .sort((a, b) => a - b);
  let nums = "",
    last = 0;
  keep.forEach((x) => {
    if (x - last > 1) nums += '<span class="pg-gap">&hellip;</span>';
    nums += `<button type="button" class="pg-n${x === p ? " on" : ""}" onclick="impGo(${x})"${x === p ? ' aria-current="page"' : ""}>${x}</button>`;
    last = x;
  });
  const sizes = [25, 50, 100, 200]
    .map(
      (v) => `<option value="${v}"${v === sz ? " selected" : ""}>${v}</option>`,
    )
    .join("");
  return (
    `<div class="pg-info"><b>${fmtInt(from)}&ndash;${fmtInt(to)}</b> dari ${fmtInt(d.total)} baris &middot; Halaman ${p} dari ${n}</div>` +
    `<div class="pg-ctl"><button type="button" class="pg-b" onclick="impGo(1)"${p <= 1 ? " disabled" : ""} title="Halaman pertama">&laquo;</button>` +
    `<button type="button" class="pg-b" onclick="impPage(-1)"${p <= 1 ? " disabled" : ""} title="Sebelumnya">&lsaquo;</button>${nums}` +
    `<button type="button" class="pg-b" onclick="impPage(1)"${p >= n ? " disabled" : ""} title="Berikutnya">&rsaquo;</button>` +
    `<button type="button" class="pg-b" onclick="impGo(${n})"${p >= n ? " disabled" : ""} title="Halaman terakhir">&raquo;</button></div>` +
    `<div class="pg-size"><label>Per halaman <select onchange="impSetSize(this.value)">${sizes}</select></label>` +
    `<label>Ke hal. <input type="number" min="1" max="${n}" value="${p}" onkeydown="if(event.key==='Enter')impGo(this.value)" onchange="impGo(this.value)"></label></div>`
  );
}
function impGo(p) {
  const n = (IMP.data && IMP.data.pages) || 1;
  const v = Math.min(n, Math.max(1, parseInt(p, 10) || 1));
  if (v === IMP.page) return Promise.resolve();
  IMP.page = v;
  IMP.editing = null;
  return loadImportRows().then(() => {
    const el = $id("imp-rows");
    if (el) el.scrollTop = 0;
  });
}
function impSetSize(v) {
  const n = parseInt(v, 10);
  if (![25, 50, 100, 200].includes(n)) return;
  IMP.size = n;
  IMP.page = 1;
  IMP.editing = null;
  return loadImportRows();
}
function impToggleOpts() {
  const f = $id("imp-form");
  if (f && f.classList) f.classList.toggle("opts");
}
function impLayout() {
  const c = document.querySelector("#modal-data .data-container");
  if (c && c.classList)
    c.classList.toggle("imp-session", DATA.tab === "import" && !!IMP.sess);
  if (!IMP.sess) {
    const f = $id("imp-form");
    if (f && f.classList) f.classList.remove("opts");
  }
}
function impPage(d) {
  IMP.page = Math.max(1, IMP.page + d);
  IMP.editing = null;
  return loadImportRows();
}
function loadImportRows() {
  const s = IMP.sess;
  if (!s) return Promise.resolve();
  const seq = IMP.seq;
  const q = new URLSearchParams({
    filter: IMP.filter,
    q: IMP.q,
    page: IMP.page,
    size: IMP.size,
  });
  return apiRequest(`/api/import/sessions/${s.id}/rows?` + q.toString())
    .then((d) => {
      if (seq !== IMP.seq || !IMP.sess) return;
      IMP.data = d;
      renderImportRows();
    })
    .catch((err) => setDataMsg("imp-msg", err.message, "err"));
}
function loadImportReasons() {
  const s = IMP.sess;
  if (!s) return Promise.resolve();
  return apiRequest(`/api/import/sessions/${s.id}/reasons`)
    .then((d) => {
      IMP.reasons = d.reasons || [];
      renderImportReasons();
    })
    .catch(() => {});
}
function impOrigText(r) {
  const o = r.orig || {};
  const bits = [];
  if (o.name !== undefined && o.name !== r.name)
    bits.push("nama: " + (o.name || "(kosong)"));
  if (
    o.type &&
    String(o.type).toUpperCase() !== String(r.type || "").toUpperCase()
  )
    bits.push("jenis: " + o.type);
  if (o.lat != null && (o.lat !== r.latitude || o.lng !== r.longitude))
    bits.push(`koordinat: ${o.lat}, ${o.lng}`);
  return bits.join("; ");
}
function impRowHtml(r) {
  const notes = r.errors
    .map((x) => `<span class="imp-err">${escapeHtml(x)}</span>`)
    .concat(r.warnings.map((x) => escapeHtml(x)))
    .join("<br>");
  const meta =
    r.kind === "CABLE"
      ? `${fmtM(r.length_m)}${r.installation ? " &middot; " + escapeHtml(r.installation) : ""}`
      : "";
  const orig = r.edited ? impOrigText(r) : "";
  const ov = r.override || {};
  const tag = r.edited
    ? `<span class="imp-fix">dikoreksi${ov.action === "merge" ? ": gabung ke " + escapeHtml(r.existing_name || "") : ov.action === "skip" ? ": dilewati" : ""}</span>`
    : "";
  const isSel = IMP.sel.has(r.rid);
  return (
    `<tr class="imp-${r.action}${r.edited ? " imp-edited" : ""}${isSel ? " sel" : ""}" data-rid="${r.rid}" draggable="true" ondragstart="impDragStart(event, ${r.rid})" ondragend="impDragEnd()" onclick="impRowClick(event, ${r.rid})">` +
    `<td class="imp-selcell"><label class="imp-cbw"><input type="checkbox" class="imp-cb"${isSel ? " checked" : ""} onclick="impToggle(event, ${r.rid})" /> <span>${r.rid + 1}</span></label><small>${escapeHtml(r.src)}</small></td>` +
    `<td><b>${escapeHtml(r.name || "-")}</b> ${tag}${r.sys_name && r.action === "create" ? `<div class="imp-sys">akan menjadi: <b>${escapeHtml(r.sys_name)}</b></div>` : ""}<br><small>${escapeHtml(r.type || "?")}${r.kind === "NODE" ? " &middot; titik" : r.kind === "CABLE" ? " &middot; kabel" : ""}${meta ? " &middot; " + meta : ""}</small>${orig ? `<br><small class="imp-orig">asal: ${escapeHtml(orig)}</small>` : ""}</td>` +
    `<td><span class="imp-act ${r.action}">${ACTION_LABEL[r.action]}</span></td><td class="imp-notes">${notes || "&ndash;"}</td>` +
    `<td><button type="button" class="imp-edit-btn" onclick="impEdit(${r.rid})">${IMP.editing === r.rid ? "Tutup" : r.edited ? "Ubah" : "Koreksi"}</button></td></tr>` +
    (IMP.editing === r.rid ? impEditHtml(r) : "")
  );
}
function impSel(id, opts, cur, blank) {
  return `<select id="${id}">${blank ? `<option value="">${blank}</option>` : ""}${opts.map((o) => `<option value="${o}"${String(cur || "").toUpperCase() === o.toUpperCase() ? " selected" : ""}>${o}</option>`).join("")}</select>`;
}
function impEditHtml(r) {
  const node = r.kind !== "CABLE";
  const ov = r.override || {};
  const act =
    IMP.forceAct && IMP.editing === r.rid
      ? IMP.forceAct
      : ov.action || "create";
  const radio = (v, label) =>
    `<label><input type="radio" name="ie-act" value="${v}"${act === v ? " checked" : ""} onchange="impActChanged()" /> ${label}</label>`;
  return `<tr class="imp-edit-row"><td colspan="5"><div class="imp-edit">
    <div class="imp-edit-grid">
      <label>Nama<input id="ie-name" name="imp_nama" autocomplete="off" value="${escapeHtml(r.name || "")}" maxlength="120" /></label>
      <label>Jenis aset${impSel("ie-type", node ? IMP_NODE_TYPES : IMP_CABLE_TYPES, r.type, "(pilih jenis)")}</label>
      <label>Status${impSel("ie-status", ["Active", "Maintenance"], r.status)}</label>
      <label>Kapasitas<input id="ie-cap" name="imp_kapasitas" autocomplete="off" placeholder="mis. 1 In - 8 Out" value="${escapeHtml(r.capacity || "")}" maxlength="40" /></label>
      ${
        node
          ? `<label>Latitude<input id="ie-lat" name="imp_lat" autocomplete="off" value="${r.latitude != null ? r.latitude : ""}" /></label><label>Longitude<input id="ie-lng" name="imp_lng" autocomplete="off" value="${r.longitude != null ? r.longitude : ""}" /></label>`
          : `<label>Pemasangan${impSel("ie-inst", ["Udara", "Tanah"], r.installation, "(ikuti berkas)")}</label>`
      }
    </div>
    ${r.suggest_name ? `<div class="imp-sug">Nama sudah dipakai aset lain. <a href="#" onclick="impUseSuggest(${JSON.stringify(r.suggest_name).replace(/"/g, "&quot;")}); return false;">Pakai nama ${escapeHtml(r.suggest_name)}</a></div>` : ""}
    <div class="imp-edit-act">${radio("create", "Impor sebagai aset baru")}${radio("merge", "Gabung ke aset yang sudah ada")}${radio("skip", "Lewati baris ini")}</div>
    <div class="imp-merge" id="ie-merge" style="display:${act === "merge" ? "" : "none"}">
      <input id="ie-mq" name="imp_cari" autocomplete="off" placeholder="Cari nama aset yang sudah ada..." oninput="impMergeSearch(${r.rid})" />
      <div id="ie-mres" class="imp-mres"></div>
      <div id="ie-mpick" class="imp-mpick">${ov.action === "merge" && r.existing_name ? "Terpilih: <b>" + escapeHtml(r.existing_name) + "</b>" : "Belum ada aset dipilih"}</div>
      <small>Gabung hanya memperbarui atribut aset tujuan (cluster/area dan data tambahan); nama, posisi, jenis, dan kapasitasnya tidak berubah.</small>
    </div>
    <div class="imp-edit-btns"><button type="button" class="data-primary" onclick="impSaveRow(${r.rid})">Simpan koreksi</button>
      <button type="button" class="data-ghost" onclick="impResetRow(${r.rid})">Kembalikan ke asli</button>
      <button type="button" class="data-ghost" onclick="impEdit(null)">Batal</button></div>
  </div></td></tr>`;
}
function renderImportRows() {
  const d = IMP.data;
  if (!d) return;
  const body = d.rows.map(impRowHtml).join("");
  $id("imp-rows").innerHTML = d.rows.length
    ? `<table class="imp-table"><thead><tr><th class="imp-selcell"><label class="imp-cbw"><input type="checkbox" id="imp-selall" class="imp-cb" onclick="impSelPage(event)"${d.rows.length && d.rows.every((x) => IMP.sel.has(x.rid)) ? " checked" : ""} /> Baris</label></th><th>Aset</th><th>Aksi</th><th>Catatan</th><th></th></tr></thead><tbody>${body}</tbody></table>`
    : `<div class="imp-empty">${IMP.filter === "error" ? "Tidak ada baris bergalat. " : ""}Tidak ada baris untuk ditampilkan pada filter ini.</div>`;
  renderImportSelbar();
  $id("imp-pager").innerHTML = impPagerHtml(d);
}
function renderImportReasons() {
  const el = $id("imp-reasons");
  const items = (IMP.reasons || [])
    .filter(
      (x) =>
        x.level === "error" ||
        /tidak dikenal|ditebak|tidak sesuai/.test(x.message),
    )
    .slice(0, 8);
  if (!items.length) {
    el.style.display = "none";
    el.innerHTML = "";
    return;
  }
  const typeOpts = `<option value="">Ganti jenis ke...</option><optgroup label="Titik">${IMP_NODE_TYPES.map((t) => `<option>${t}</option>`).join("")}</optgroup><optgroup label="Kabel">${IMP_CABLE_TYPES.map((t) => `<option>${t}</option>`).join("")}</optgroup>`;
  el.style.display = "";
  el.innerHTML =
    `<details><summary class="imp-meta"><b>Koreksi sekaligus</b> &middot; terapkan keputusan yang sama ke semua baris dengan alasan serupa (${items.length} alasan; klik untuk membuka)</summary>` +
    items
      .map(
        (x, i) =>
          `<div class="imp-reason"><span class="imp-rtxt"><span class="imp-lvl ${x.level}">${x.level === "error" ? "galat" : "peringatan"}</span> ${escapeHtml(x.message)}</span><b>${x.count} baris</b>` +
          `<select id="ir-type-${i}">${typeOpts}</select><button type="button" class="data-ghost" onclick="impBulk(${i}, 'type')">Terapkan jenis</button>` +
          `<button type="button" class="data-ghost" onclick="impBulk(${i}, 'skip')">Lewati semua</button></div>`,
      )
      .join("") +
    "</details>";
  IMP.reasonItems = items;
}
function impBulk(i, mode) {
  const x = (IMP.reasonItems || [])[i];
  const s = IMP.sess;
  if (!x || !s) return;
  const set =
    mode === "skip" ? { action: "skip" } : { type: $id("ir-type-" + i).value };
  if (mode !== "skip" && !set.type)
    return setDataMsg("imp-msg", "Pilih jenis aset yang dituju dulu.", "err");
  const where = {
    status: x.level === "error" ? "error" : "any",
    reason: x.message,
  };
  const url = `/api/import/sessions/${s.id}/bulk`;
  return apiRequest(url, "POST", { where, set, dry: true })
    .then((d) => {
      if (!d.applied)
        return setDataMsg(
          "imp-msg",
          `Tidak ada baris yang cocok untuk diubah${d.ignored ? ` (${d.ignored} baris berbeda jenis data, mis. kabel vs titik, diabaikan)` : ""}.`,
          "err",
        );
      const sample =
        d.sample && d.sample.length ? `\nContoh: ${d.sample.join(", ")}` : "";
      const what = mode === "skip" ? "dilewati" : `dijadikan ${set.type}`;
      if (
        !confirm(
          `${d.applied} baris akan ${what}.${sample}${d.ignored ? `\n${d.ignored} baris cocok tetapi tidak sesuai jenis datanya dan diabaikan.` : ""}\n\nLanjutkan?`,
        )
      )
        return;
      return apiRequest(url, "POST", { where, set }).then((r) => {
        IMP.sess = r.session;
        IMP.page = 1;
        IMP.editing = null;
        setDataMsg("imp-msg", `${r.applied} baris dikoreksi sekaligus.`, "ok");
        renderImportSummary();
        return Promise.all([loadImportRows(), loadImportReasons()]);
      });
    })
    .catch((err) => setDataMsg("imp-msg", err.message, "err"));
}

// ---------- Keranjang aset: pilih baris (ala Excel) lalu seret ke keranjang ----------
function impBasketGlyph(key, cls) {
  return String(key).startsWith("inst:")
    ? key === "inst:Udara"
      ? "TIANG"
      : "CABLE"
    : cls === "cable"
      ? "CABLE"
      : key === "create"
        ? "NEW"
        : key === "merge"
          ? "MERGE"
          : key === "skip"
            ? "SKIP"
            : key;
}
function impBasketColor(key, cls) {
  if (cls === "cable")
    return (CABLE_TYPE_META[key] || { color: "#64748b" }).color;
  if (key === "inst:Udara") return "#0ea5e9";
  if (key === "inst:Tanah") return "#a16207";
  if (key === "create") return "#2563eb";
  if (key === "merge") return "#7c3aed";
  if (key === "skip") return "#94a3b8";
  return (NODE_TYPE_META[key] || { color: "#64748b" }).color;
}
const IMP_BASKET_ICON = { merge: "fa-link", create: "fa-plus", skip: "fa-ban" };
function renderImportBaskets() {
  const el = $id("imp-baskets");
  if (!el || !IMP.sess) return;
  const bt = IMP.sess.by_type || {};
  const tile = (key, label, cls, sub) =>
    `<button type="button" class="imp-bkt ${cls}" data-k="${key}" title="Seret baris terpilih ke sini, atau pilih baris lalu klik" ondragover="impDragOver(event, '${key}')" ondragleave="impDragLeave(event)" ondrop="impDrop(event, '${key}')" onclick="impDropTo('${key}')">` +
    `<span class="bk-ic" style="background:${impBasketColor(key, cls)}">${mapGlyph(impBasketGlyph(key, cls), 18)}</span><b>${label}</b><small>${sub}</small></button>`;
  el.style.display = "";
  el.innerHTML =
    `<div class="imp-bk-head"><b>Pindahkan baris terpilih ke:</b> <small>seret baris ke salah satu kartu di bawah &mdash; atau klik kartunya.</small></div>` +
    `<div class="imp-bk-group"><span>Ganti jenis &middot; titik</span>${IMP_NODE_TYPES.map((t) => tile(t, t, "node", bt[t] ? bt[t] + " aset" : "belum ada")).join("")}</div>` +
    `<div class="imp-bk-group"><span>Ganti jenis &middot; kabel</span>${IMP_CABLE_TYPES.map((t) => tile(t, t, "cable", bt[t] ? bt[t] + " aset" : "belum ada")).join("")}</div>` +
    `<div class="imp-bk-group"><span>Pemasangan &middot; kabel</span>${tile("inst:Udara", "Kabel Udara", "act", "tiang / aerial")}${tile("inst:Tanah", "Kabel Tanah", "act", "duct / tanam langsung")}</div>` +
    `<div class="imp-bk-group"><span>Tindakan</span>${tile("create", "Impor sebagai baru", "act", "nama sama diberi nomor")}${tile("merge", "Gabung ke aset ada", "act", "pilih 1 baris")}${tile("skip", "Lewati", "act", "tidak diimpor")}</div>`;
}
function renderImportSelbar() {
  const el = $id("imp-selbar");
  if (!el) return;
  const d = IMP.data;
  if (!d || !d.rows.length) {
    el.style.display = "none";
    el.innerHTML = "";
    return;
  }
  const n = IMP.sel.size;
  el.style.display = "";
  el.innerHTML = n
    ? `<b>${fmtInt(n)}</b> baris dipilih ${d.total > n ? `&middot; <a href="#" onclick="impSelAll(); return false;">Pilih semua ${fmtInt(d.total)} baris pada filter ini</a>` : ""} &middot; <a href="#" onclick="impSelClear(); return false;">Kosongkan pilihan</a>`
    : `<small>Klik baris untuk memilih &middot; Shift+klik = rentang &middot; Ctrl+klik = tambah/kurangi &middot; Ctrl+A = semua pada filter ini &middot; Esc = kosongkan</small>`;
}
function renderImportUndo() {
  const el = $id("imp-undo");
  if (!el) return;
  const u = IMP.undo;
  if (!u) {
    el.style.display = "none";
    el.innerHTML = "";
    return;
  }
  el.style.display = "";
  el.innerHTML =
    `<span>${escapeHtml(u.text)}</span> ` +
    (u.before && Object.keys(u.before).length
      ? `<button type="button" class="data-ghost" onclick="impUndo()"><i class="fa-solid fa-rotate-left"></i> Urungkan</button>`
      : "");
}
function impPaintSel() {
  const rows = document.querySelectorAll("#imp-rows tr[data-rid]") || [];
  Array.from(rows).forEach((tr) => {
    const on = IMP.sel.has(Number(tr.dataset && tr.dataset.rid));
    if (tr.classList) tr.classList.toggle("sel", on);
    const cb = tr.querySelector && tr.querySelector("input.imp-cb");
    if (cb) cb.checked = on;
  });
  const all = $id("imp-selall");
  if (all && IMP.data)
    all.checked =
      IMP.data.rows.length > 0 &&
      IMP.data.rows.every((x) => IMP.sel.has(x.rid));
  renderImportSelbar();
}
function impPageRids() {
  return ((IMP.data && IMP.data.rows) || []).map((r) => r.rid);
}
function impSelect(rid, o) {
  const ids = impPageRids();
  if (o.shift && IMP.anchor != null && ids.includes(IMP.anchor)) {
    const a = ids.indexOf(IMP.anchor),
      b = ids.indexOf(rid);
    if (!o.ctrl) IMP.sel = new Set();
    for (let i = Math.min(a, b); i <= Math.max(a, b); i++) IMP.sel.add(ids[i]);
  } else if (o.ctrl) {
    if (IMP.sel.has(rid)) IMP.sel.delete(rid);
    else IMP.sel.add(rid);
    IMP.anchor = rid;
  } else {
    IMP.sel =
      IMP.sel.size === 1 && IMP.sel.has(rid) ? new Set() : new Set([rid]);
    IMP.anchor = rid;
  }
  impPaintSel();
}
function impRowClick(e, rid) {
  const t = e && e.target;
  if (t && t.closest && t.closest("button, input, select, a, label, textarea"))
    return;
  impSelect(rid, {
    shift: !!(e && e.shiftKey),
    ctrl: !!(e && (e.ctrlKey || e.metaKey)),
  });
}
function impToggle(e, rid) {
  if (e && e.stopPropagation) e.stopPropagation();
  impSelect(rid, { shift: !!(e && e.shiftKey), ctrl: true });
}
function impSelPage(e) {
  if (e && e.stopPropagation) e.stopPropagation();
  const ids = impPageRids();
  const all = ids.length && ids.every((x) => IMP.sel.has(x));
  ids.forEach((x) => (all ? IMP.sel.delete(x) : IMP.sel.add(x)));
  impPaintSel();
}
function impSelClear() {
  IMP.sel = new Set();
  IMP.anchor = null;
  impPaintSel();
}
function impSelAll() {
  const s = IMP.sess;
  if (!s) return Promise.resolve();
  const q = new URLSearchParams({ filter: IMP.filter, q: IMP.q, ids_only: 1 });
  return apiRequest(`/api/import/sessions/${s.id}/rows?` + q.toString())
    .then((d) => {
      IMP.sel = new Set(d.rids || []);
      impPaintSel();
      setDataMsg(
        "imp-msg",
        `${fmtInt(IMP.sel.size)} baris dipilih (seluruh filter, lintas halaman).`,
        "info",
      );
    })
    .catch((err) => setDataMsg("imp-msg", err.message, "err"));
}
function impKey(e) {
  if (!e) return;
  if ((e.ctrlKey || e.metaKey) && String(e.key).toLowerCase() === "a") {
    if (e.preventDefault) e.preventDefault();
    impSelAll();
  } else if (e.key === "Escape") impSelClear();
}
function impDragStart(e, rid) {
  if (!IMP.sel.has(rid)) {
    IMP.sel = new Set([rid]);
    IMP.anchor = rid;
    impPaintSel();
  }
  const n = IMP.sel.size;
  const dt = e && e.dataTransfer;
  if (dt) {
    try {
      dt.setData("text/plain", "netgis-import:" + n);
      dt.effectAllowed = "move";
    } catch (_) {
      /* abaikan */
    }
    try {
      const g = document.createElement("div");
      g.className = "imp-ghost";
      g.textContent = `${n} baris`;
      document.body.appendChild(g);
      dt.setDragImage(g, 14, 14);
      setTimeout(() => {
        try {
          document.body.removeChild(g);
        } catch (_) {
          /* abaikan */
        }
      }, 0);
    } catch (_) {
      /* abaikan */
    }
  }
  IMP.dragging = true;
  const box = $id("imp-baskets");
  if (box && box.classList) box.classList.add("dragging");
}
function impDragEnd() {
  IMP.dragging = false;
  const box = $id("imp-baskets");
  if (box && box.classList) box.classList.remove("dragging");
}
function impDragOver(e, key) {
  if (!IMP.dragging) return;
  if (e && e.preventDefault) e.preventDefault();
  if (e && e.dataTransfer) {
    try {
      e.dataTransfer.dropEffect = "move";
    } catch (_) {
      /* abaikan */
    }
  }
  const t = e && e.currentTarget;
  if (t && t.classList) t.classList.add("over");
}
function impDragLeave(e) {
  const t = e && e.currentTarget;
  if (t && t.classList) t.classList.remove("over");
}
function impDrop(e, key) {
  if (e && e.preventDefault) e.preventDefault();
  impDragLeave(e);
  impDragEnd();
  return impDropTo(key);
}
function impBasketLabel(key) {
  return key === "skip"
    ? "Lewati"
    : key === "create"
      ? "Impor sebagai baru"
      : key === "merge"
        ? "Gabung"
        : key === "inst:Udara"
          ? "Kabel Udara"
          : key === "inst:Tanah"
            ? "Kabel Tanah"
            : key;
}
function impDropTo(key) {
  const s = IMP.sess;
  if (!s) return;
  const rids = Array.from(IMP.sel);
  if (!rids.length)
    return setDataMsg(
      "imp-msg",
      "Pilih baris dulu (klik baris di tabel), lalu seret ke keranjang atau klik keranjangnya.",
      "err",
    );
  if (key === "merge") {
    if (rids.length !== 1)
      return setDataMsg(
        "imp-msg",
        "Gabung dilakukan per baris: pilih tepat 1 baris, lalu pilih aset tujuan. Untuk banyak baris, gunakan keranjang jenis atau Impor sebagai baru.",
        "err",
      );
    if (!impPageRids().includes(rids[0]))
      return setDataMsg(
        "imp-msg",
        "Baris yang dipilih tidak ada di halaman ini; buka halamannya dulu.",
        "err",
      );
    IMP.editing = rids[0];
    IMP.forceAct = "merge";
    IMP.pick = null;
    renderImportRows();
    return;
  }
  const set =
    key === "skip"
      ? { action: "skip" }
      : key === "create"
        ? { action: "create" }
        : key.startsWith("inst:")
          ? { installation: key.slice(5) }
          : { type: key };
  const label = impBasketLabel(key);
  if (
    rids.length > 300 &&
    !confirm(
      `${fmtInt(rids.length)} baris akan dimasukkan ke "${label}". Lanjutkan?`,
    )
  )
    return;
  const el = $id("imp-baskets");
  if (el && el.classList) el.classList.add("busy");
  return apiRequest(`/api/import/sessions/${s.id}/bulk`, "POST", {
    where: { rids },
    set,
  })
    .then((r) => {
      if (el && el.classList) el.classList.remove("busy");
      IMP.sess = r.session;
      IMP.editing = null;
      IMP.sel = new Set();
      IMP.anchor = null;
      const text =
        `${fmtInt(r.applied)} baris dimasukkan ke ${label}.` +
        (r.ignored
          ? ` ${fmtInt(r.ignored)} baris diabaikan (jenisnya tidak cocok: titik vs kabel).`
          : "");
      IMP.undo = r.applied ? { before: r.before || {}, text } : null;
      setDataMsg("imp-msg", r.applied ? "" : text, r.applied ? "ok" : "err");
      renderImportUndo();
      renderImportSummary();
      return Promise.all([loadImportRows(), loadImportReasons()]);
    })
    .catch((err) => {
      if (el && el.classList) el.classList.remove("busy");
      setDataMsg("imp-msg", err.message, "err");
    });
}
function impUndo() {
  const s = IMP.sess,
    u = IMP.undo;
  if (!s || !u) return;
  return apiRequest(`/api/import/sessions/${s.id}/bulk`, "POST", {
    restore: u.before,
  })
    .then((r) => {
      IMP.sess = r.session;
      IMP.undo = null;
      IMP.sel = new Set();
      renderImportUndo();
      setDataMsg(
        "imp-msg",
        `Dibatalkan: ${fmtInt(r.restored)} baris dikembalikan.`,
        "ok",
      );
      renderImportSummary();
      return Promise.all([loadImportRows(), loadImportReasons()]);
    })
    .catch((err) => setDataMsg("imp-msg", err.message, "err"));
}
function impEdit(rid) {
  IMP.editing = rid == null || IMP.editing === rid ? null : rid;
  IMP.pick = null;
  IMP.forceAct = null;
  renderImportRows();
}
function impActChanged() {
  const sel = Array.from(
    document.querySelectorAll('input[name="ie-act"]'),
  ).find((e) => e.checked);
  $id("ie-merge").style.display = sel && sel.value === "merge" ? "" : "none";
}
function impUseSuggest(name) {
  $id("ie-name").value = name;
}
function impMergeSearch(rid) {
  clearTimeout(IMP.mt);
  IMP.mt = setTimeout(() => {
    const r = ((IMP.data && IMP.data.rows) || []).find((x) => x.rid === rid);
    if (!r) return;
    const q = new URLSearchParams({
      kind: r.kind === "CABLE" ? "CABLE" : "NODE",
      q: ($id("ie-mq").value || "").trim(),
      limit: 12,
    });
    if (r.kind !== "CABLE" && r.orig && r.orig.lat != null) {
      q.set("lat", r.orig.lat);
      q.set("lng", r.orig.lng);
    }
    apiRequest("/api/import/assets?" + q.toString())
      .then((d) => {
        IMP.found = d.assets || [];
        $id("ie-mres").innerHTML = IMP.found.length
          ? IMP.found
              .map(
                (a, i) =>
                  `<div class="imp-mrow" onclick="impPickMerge(${i})"><b>${escapeHtml(a.name)}</b> <small>${escapeHtml(a.type)}${a.capacity ? " &middot; " + escapeHtml(a.capacity) : ""}${a.distance_m != null ? " &middot; " + fmtM(a.distance_m) : ""}</small></div>`,
              )
              .join("")
          : `<div class="imp-empty">Tidak ada aset yang cocok.</div>`;
      })
      .catch((err) => setDataMsg("imp-msg", err.message, "err"));
  }, 200);
}
function impPickMerge(i) {
  const a = (IMP.found || [])[i];
  if (!a) return;
  IMP.pick = a;
  $id("ie-mpick").innerHTML =
    `Terpilih: <b>${escapeHtml(a.name)}</b> (${escapeHtml(a.type)})`;
}
function impSaveRow(rid) {
  try {
    return impSaveRow0(rid);
  } catch (err) {
    console.error(err);
    return setDataMsg(
      "imp-msg",
      "Koreksi gagal disimpan: " + ((err && err.message) || err),
      "err",
    );
  }
}
function impSaveRow0(rid) {
  const s = IMP.sess;
  const r = ((IMP.data && IMP.data.rows) || []).find((x) => x.rid === rid);
  if (!s || !r) return;
  const node = r.kind !== "CABLE";
  const body = {};
  const nm = ($id("ie-name").value || "").trim();
  if (nm !== (r.name || "")) body.name = nm;
  const t = $id("ie-type").value;
  if (t && t.toUpperCase() !== String(r.type || "").toUpperCase())
    body.type = t;
  const stt = $id("ie-status").value;
  if (stt && stt !== r.status) body.status = stt;
  const cap = ($id("ie-cap").value || "").trim();
  if (cap !== (r.capacity || "")) body.capacity = cap;
  if (node) {
    const lat = String($id("ie-lat").value || "")
        .trim()
        .replace(",", "."),
      lng = String($id("ie-lng").value || "")
        .trim()
        .replace(",", ".");
    if (
      lat !== String(r.latitude != null ? r.latitude : "") ||
      lng !== String(r.longitude != null ? r.longitude : "")
    ) {
      if (lat === "" || lng === "" || isNaN(Number(lat)) || isNaN(Number(lng)))
        return setDataMsg(
          "imp-msg",
          "Isi latitude dan longitude dengan angka.",
          "err",
        );
      body.lat = Number(lat);
      body.lng = Number(lng);
    }
  } else {
    const inst = $id("ie-inst").value;
    if (inst && inst !== (r.installation || "")) body.installation = inst;
  }
  const sel = Array.from(
    document.querySelectorAll('input[name="ie-act"]'),
  ).find((e) => e.checked);
  const act = sel ? sel.value : "create";
  const prev = (r.override && r.override.action) || "create";
  if (act === "merge") {
    const pick =
      IMP.pick ||
      (r.override && r.override.merge_id ? { id: r.override.merge_id } : null);
    if (!pick)
      return setDataMsg(
        "imp-msg",
        "Pilih aset tujuan gabung dulu (cari namanya lalu klik).",
        "err",
      );
    body.action = "merge";
    body.merge_id = pick.id;
  } else if (act !== prev || (act === "create" && r.action === "skip")) {
    body.action =
      act === "create"
        ? r.action === "skip" && !(r.override && r.override.action)
          ? "create"
          : ""
        : act;
  }
  if (!Object.keys(body).length) {
    IMP.editing = null;
    return renderImportRows();
  }
  return apiRequest(`/api/import/sessions/${s.id}/rows/${rid}`, "PUT", body)
    .then((d) =>
      impAfterRowChange(d, "Koreksi disimpan; seluruh data divalidasi ulang."),
    )
    .catch((err) => setDataMsg("imp-msg", err.message, "err"));
}
function impResetRow(rid) {
  const s = IMP.sess;
  if (!s) return;
  return apiRequest(`/api/import/sessions/${s.id}/rows/${rid}`, "PUT", {
    reset: true,
  })
    .then((d) => impAfterRowChange(d, "Baris dikembalikan ke isi berkas asli."))
    .catch((err) => setDataMsg("imp-msg", err.message, "err"));
}
function impAfterRowChange(d, msg) {
  IMP.sess = d.session;
  IMP.editing = null;
  IMP.pick = null;
  IMP.forceAct = null;
  IMP.undo = null;
  renderImportUndo();
  setDataMsg("imp-msg", msg, "ok");
  renderImportSummary();
  return Promise.all([loadImportRows(), loadImportReasons()]);
}
function validateImport() {
  const s = IMP.sess;
  if (!s) return;
  startDataProgress("imp-prog", "Memvalidasi ulang seluruh data...", null);
  return apiRequest(`/api/import/sessions/${s.id}/validate`, "POST")
    .then((d) => {
      stopDataProgress("imp-prog");
      IMP.sess = d;
      setDataMsg(
        "imp-msg",
        `Validasi ulang selesai: ${d.counts.create} dibuat, ${d.counts.update} diperbarui, ${d.counts.skip} dilewati, ${d.counts.error} bergalat.`,
        d.counts.error ? "info" : "ok",
      );
      renderImportSummary();
      return Promise.all([loadImportRows(), loadImportReasons()]);
    })
    .catch((err) => {
      stopDataProgress("imp-prog");
      setDataMsg("imp-msg", err.message, "err");
    });
}
function downloadImportReport() {
  if (!IMP.sess) return;
  return downloadFromApi(`/api/import/sessions/${IMP.sess.id}/report.csv`)
    .then((r) => setDataMsg("imp-msg", `Laporan diunduh: ${r.name}`, "ok"))
    .catch((err) =>
      setDataMsg("imp-msg", "Gagal mengunduh laporan: " + err.message, "err"),
    );
}
// Hanya mengosongkan tampilan; sesi di server TIDAK dibuang (gunakan "Buang sesi" untuk itu)
function resetImport() {
  IMP.sess = null;
  IMP.data = null;
  IMP.reasons = [];
  IMP.reasonItems = [];
  IMP.editing = null;
  IMP.pick = null;
  IMP.q = "";
  IMP.filter = "error";
  IMP.page = 1;
  IMP.seq++;
  IMP.sel = new Set();
  IMP.anchor = null;
  IMP.undo = null;
  IMP.forceAct = null;
  IMP.dragging = false;
  $id("imp-file").value = "";
  $id("imp-file-name").textContent =
    "Pilih berkas Excel / CSV / GeoJSON / KML / KMZ";
  $id("imp-preview").style.display = "none";
  [
    "imp-sum",
    "imp-tabs",
    "imp-meta",
    "imp-reasons",
    "imp-rows",
    "imp-pager",
    "imp-sess-head",
    "imp-baskets",
    "imp-selbar",
    "imp-undo",
  ].forEach((i) => {
    const e_ = $id(i);
    if (e_) {
      e_.innerHTML = "";
      if (/baskets|selbar|undo/.test(i)) e_.style.display = "none";
    }
  });
  $id("imp-q").value = "";
  $id("imp-commit").disabled = true;
  $id("imp-validate").disabled = true;
  $id("imp-report").disabled = true;
  setDataMsg("imp-msg", "", "");
  renderImportSessions();
  impLayout();
}
function commitImport() {
  const s = IMP.sess;
  if (!s) return;
  const btn = $id("imp-commit");
  setBtnBusy(btn, true);
  setDataMsg("imp-msg", "", "");
  startDataProgress("imp-prog", "Validasi ulang (tahap 1 dari 2)...", null);
  const finishOk = (r) => {
    stopDataProgress("imp-prog");
    setBtnBusy(btn, false);
    resetImport();
    setDataMsg(
      "imp-msg",
      r.message +
        (r.unlinked_cables
          ? ` ${r.unlinked_cables} kabel belum terhubung ke node di ujungnya (tidak ada node dalam 30 m).`
          : ""),
      "ok",
    );
    loadData();
    loadImportSessions();
  };
  const finishErr = (msg) => {
    stopDataProgress("imp-prog");
    setDataMsg("imp-msg", "Gagal menerapkan: " + msg, "err");
    setBtnBusy(btn, false);
    btn.disabled = false;
  };
  const poll = (jobId) =>
    apiRequest("/api/import/jobs/" + jobId).then((j) => {
      if (j.status === "done") return finishOk(j.result);
      if (j.status === "error") return finishErr(j.error || "tidak diketahui");
      const det = j.total > 0;
      updateDataProgress(
        "imp-prog",
        det ? `${j.stage} (${j.done} dari ${j.total} data)` : j.stage,
        det ? j.percent : null,
      );
      return new Promise((res) => setTimeout(res, 350)).then(() => poll(jobId));
    });
  return apiRequest(`/api/import/sessions/${s.id}/validate`, "POST")
    .then((v) => {
      stopDataProgress("imp-prog");
      IMP.sess = v;
      renderImportSummary();
      const c = v.counts;
      if (c.create + c.update === 0) {
        setBtnBusy(btn, false);
        btn.disabled = true;
        return setDataMsg(
          "imp-msg",
          "Tidak ada baris yang bisa diterapkan; koreksi baris bergalat dulu.",
          "err",
        );
      }
      if (
        !confirm(
          `Terapkan impor "${v.filename}"?\n${c.create} dibuat, ${c.update} diperbarui, ${c.skip} dilewati, ${c.error} bergalat (tidak diimpor).${v.edited ? `\n${v.edited} baris sudah dikoreksi.` : ""}\n\nValidasi akhir (tahap 2 dari 2) dijalankan sekali lagi terhadap data terbaru. Semua perubahan tercatat di Riwayat Perubahan.`,
        )
      ) {
        setBtnBusy(btn, false);
        return;
      }
      setBtnBusy(btn, true);
      startDataProgress("imp-prog", "Memulai impor...", 0);
      return apiRequest(`/api/import/sessions/${s.id}/commit`, "POST").then(
        (r) => poll(r.job_id),
      );
    })
    .catch((err) => finishErr(err.message));
}

// =====================================================================================
// ROUND 12 - OTDR: unggah, bandingkan dengan redaman hitung, layer peta (hide/unhide)
// =====================================================================================
const OTDR = {
  name: "",
  b64: "",
  preview: null,
  seq: 0,
  layerOn: false,
  userOff: false,
  summary: null,
  legend: null,
};

// ===================== OTDR: berkas SOR =====================
const SOR = {
  name: "",
  b64: "",
  data: null,
  savedId: null,
  view: null,
  seq: 0,
  info: {},
  base: null,
  list: [],
};
const SOR_FIBER = {
  652: "G.652",
  651: "G.651",
  653: "G.653",
  654: "G.654",
  655: "G.655",
  656: "G.656",
  657: "G.657",
};
function sorFmtTs(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  return isNaN(d.getTime())
    ? "-"
    : d.toISOString().replace("T", " ").slice(0, 16) + " UTC";
}
function sorKm(m) {
  return m == null
    ? "-"
    : m >= 2000
      ? (m / 1000).toFixed(3) + " km"
      : Math.round(m) + " m";
}
function sorEventKind(e) {
  const c = String(e.type_code || "");
  const refl = c[0] === "1" || c[0] === "2";
  const last = c[1] === "E" || c[1] === "O";
  if (e.no === 1 && e.distance_m === 0) return "Awal (konektor)";
  if (last) return refl ? "Ujung serat (reflektif)" : "Ujung serat";
  if (refl) return "Reflektif (konektor/patah)";
  return "Non-reflektif (splice/tekukan)";
}
function onSorFileChosen(ev) {
  const f = ev && ev.target && ev.target.files && ev.target.files[0];
  if (!f) return;
  if (f.size > 8 * 1024 * 1024)
    return setDataMsg("sor-msg", "Berkas lebih dari 8 MB.", "err");
  const rd = new FileReader();
  rd.onload = () => {
    sorClearView();
    SOR.name = f.name;
    SOR.b64 = bufToBase64(rd.result);
    SOR.savedId = null;
    $id("sor-file-name").textContent = f.name;
    parseSor();
  };
  rd.onerror = () => setDataMsg("sor-msg", "Berkas tidak dapat dibaca.", "err");
  rd.readAsArrayBuffer(f);
  try {
    ev.target.value = "";
  } catch (e) {
    /* abaikan */
  }
}
const SOR_FILE_HINT =
  "Pilih berkas .sor (Telcordia SR-4731: EXFO, Viavi, Yokogawa, Anritsu, Fujikura, dll.)";
// kosongkan grafik, perbandingan, analisis, dan gambar di peta (tanpa menyentuh daftar tersimpan)
function sorClearView() {
  SOR.seq++;
  SOR.data = null;
  SOR.view = null;
  SOR.info = {};
  SOR.base = null;
  if ($id("sor-view")) $id("sor-view").innerHTML = "";
  if ($id("sor-cmp")) $id("sor-cmp").innerHTML = "";
  sorAnReset();
  if (typeof sorMapGroup !== "undefined" && sorMapGroup)
    sorMapGroup.clearLayers();
}
function resetSor() {
  sorClearView();
  SOR.name = "";
  SOR.b64 = "";
  SOR.savedId = null;
  if ($id("sor-file")) $id("sor-file").value = "";
  if ($id("sor-file-name")) $id("sor-file-name").textContent = SOR_FILE_HINT;
  setDataMsg("sor-msg", "", "");
}
function parseSor() {
  if (!SOR.b64) return;
  const seq = ++SOR.seq;
  setDataMsg("sor-msg", "Membaca berkas SOR...", "info");
  $id("sor-view").innerHTML = "";
  return apiRequest("/api/otdr/sor/parse", "POST", {
    filename: SOR.name,
    content_base64: SOR.b64,
  })
    .then((d) => {
      if (seq !== SOR.seq) return;
      setDataMsg("sor-msg", "", "");
      showSor(d, false);
    })
    .catch((err) => {
      if (seq !== SOR.seq) return;
      SOR.data = null;
      setDataMsg("sor-msg", err.message, "err");
    });
}
function showSor(d, saved) {
  SOR.data = d;
  const pts = (d.trace && d.trace.points) || [];
  SOR.view = { x0: 0, x1: pts.length ? pts[pts.length - 1][0] : 1 };
  SOR.info = saved
    ? { is_baseline: !!d.is_baseline, incident: d.incident || null }
    : {};
  SOR.base = null;
  renderSor(saved);
  sorAnReset();
  if (saved && d.analysis) sorRestoreAnalysis(d);
  sorLoadCompare();
}
function sorMetaRows(d) {
  const g = d.general || {},
    fx = d.fixed || {},
    s = d.supplier || {},
    sm = d.summary || {};
  const rows = [
    ["Format", d.format],
    ["Kabel", g.cable_id],
    ["Serat / core", g.fiber_id],
    ["Jenis serat", SOR_FIBER[g.fiber_type] || g.fiber_type || "-"],
    [
      "Lokasi A / B",
      [g.location_a, g.location_b].filter(Boolean).join(" / ") || "-",
    ],
    ["Operator", g.operator],
    ["Waktu ukur", sorFmtTs(fx.timestamp)],
    [
      "Panjang gelombang",
      fx.wavelength_nm != null ? fx.wavelength_nm + " nm" : "-",
    ],
    [
      "Lebar pulsa",
      fx.pulse_width_ns != null ? fx.pulse_width_ns + " ns" : "-",
    ],
    [
      "Rata-rata",
      fx.averages != null
        ? fx.averages + " kali / " + fx.avg_time_s + " dtk"
        : "-",
    ],
    ["Indeks bias (IOR)", fx.group_index],
    ["Rentang", d.trace && d.trace.length_m ? sorKm(d.trace.length_m) : "-"],
    [
      "Resolusi sampel",
      d.trace && d.trace.spacing_m ? d.trace.spacing_m.toFixed(3) + " m" : "-",
    ],
    ["Titik data", d.trace ? d.trace.n : "-"],
    [
      "Alat / modul",
      [s.supplier, s.module, s.module_sn && "SN " + s.module_sn]
        .filter(Boolean)
        .join(" · ") || "-",
    ],
    ["Perangkat lunak", s.software || "-"],
    [
      "Total loss",
      sm.total_loss_db != null ? sm.total_loss_db.toFixed(3) + " dB" : "-",
    ],
    ["ORL", sm.orl_db ? sm.orl_db.toFixed(2) + " dB" : "-"],
    [
      "Ambang loss / refl / ujung",
      `${fx.loss_threshold_db} dB / ${fx.refl_threshold_db} dB / ${fx.eot_threshold_db} dB`,
    ],
  ];
  return rows
    .map(
      (r) =>
        `<tr><th>${escapeHtml(r[0])}</th><td>${escapeHtml(r[1] == null || r[1] === "" ? "-" : String(r[1]))}</td></tr>`,
    )
    .join("");
}
function sorNiceStep(span, n) {
  const raw = span / n,
    p = Math.pow(10, Math.floor(Math.log10(raw))),
    f = raw / p;
  return (f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10) * p;
}
function sorChartSvg(d, view, w, h) {
  const pts = ((d.trace && d.trace.points) || []).filter(
    (p) => p[0] >= view.x0 - 1e-6 && p[0] <= view.x1 + 1e-6,
  );
  const L = 52,
    R = 12,
    T = 12,
    B = 30,
    pw = w - L - R,
    ph = h - T - B;
  if (pts.length < 2)
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}"><text x="10" y="20">Tidak ada data pada rentang ini</text></svg>`;
  const bpts = (SOR.base || []).filter(
    (p) => p[0] >= view.x0 - 1e-6 && p[0] <= view.x1 + 1e-6,
  );
  let y0 = Infinity,
    y1 = -Infinity;
  pts.concat(bpts).forEach((p) => {
    if (p[1] < y0) y0 = p[1];
    if (p[1] > y1) y1 = p[1];
  });
  const pad = (y1 - y0) * 0.05 || 1;
  y0 -= pad;
  y1 += pad;
  const X = (m) => L + ((m - view.x0) / (view.x1 - view.x0 || 1)) * pw;
  const Y = (v) => T + (1 - (v - y0) / (y1 - y0 || 1)) * ph;
  let g = `<rect x="${L}" y="${T}" width="${pw}" height="${ph}" fill="#fff" stroke="#cbd5e1"/>`;
  const sx = sorNiceStep(view.x1 - view.x0, 8),
    sy = sorNiceStep(y1 - y0, 6);
  for (let x = Math.ceil(view.x0 / sx) * sx; x <= view.x1 + 1e-6; x += sx) {
    g += `<line x1="${X(x)}" y1="${T}" x2="${X(x)}" y2="${T + ph}" stroke="#e2e8f0"/><text x="${X(x)}" y="${h - 12}" font-size="10" text-anchor="middle" fill="#475569">${x >= 2000 || view.x1 > 5000 ? (x / 1000).toFixed(sx < 1000 ? 2 : 1) + " km" : Math.round(x) + " m"}</text>`;
  }
  for (let y = Math.ceil(y0 / sy) * sy; y <= y1 + 1e-6; y += sy) {
    g += `<line x1="${L}" y1="${Y(y)}" x2="${L + pw}" y2="${Y(y)}" stroke="#e2e8f0"/><text x="${L - 4}" y="${Y(y) + 3}" font-size="10" text-anchor="end" fill="#475569">${y.toFixed(sy < 1 ? 1 : 0)}</text>`;
  }
  g += `<text x="12" y="${T + ph / 2}" font-size="10" fill="#475569" transform="rotate(-90 12 ${T + ph / 2})" text-anchor="middle">Level relatif (dB)</text>`;
  if (bpts.length > 1)
    g += `<polyline fill="none" stroke="#94a3b8" stroke-width="1.2" stroke-dasharray="4 3" points="${bpts.map((p) => X(p[0]).toFixed(1) + "," + Y(p[1]).toFixed(1)).join(" ")}"/>`;
  g += `<polyline fill="none" stroke="#0f766e" stroke-width="1.2" points="${pts.map((p) => X(p[0]).toFixed(1) + "," + Y(p[1]).toFixed(1)).join(" ")}"/>`;
  (d.events || []).forEach((e) => {
    if (e.distance_m < view.x0 || e.distance_m > view.x1) return;
    const x = X(e.distance_m);
    g += `<line x1="${x}" y1="${T}" x2="${x}" y2="${T + ph}" stroke="#db2777" stroke-dasharray="4 3"/><circle cx="${x}" cy="${T + 8}" r="8" fill="#db2777"/><text x="${x}" y="${T + 12}" font-size="10" text-anchor="middle" fill="#fff">${Number(e.no)}</text>`;
  });
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" font-family="sans-serif">${g}</svg>`;
}
function renderSor(saved) {
  const d = SOR.data;
  if (!d) return;
  const ev = (d.events || [])
    .map(
      (
        e,
      ) => `<tr><td>${Number(e.no)}</td><td>${escapeHtml(sorKm(e.distance_m))}</td><td>${escapeHtml(sorEventKind(e))}<br><small>${escapeHtml(e.type_code)}</small></td>
    <td class="num">${e.splice_loss_db != null ? e.splice_loss_db.toFixed(3) : "-"}</td><td class="num">${e.reflectance_db != null && e.reflectance_db > -1000 ? e.reflectance_db.toFixed(2) : "-"}</td><td class="num">${e.slope_db_km != null ? e.slope_db_km.toFixed(3) : "-"}</td>
    <td><button type="button" class="data-ghost" onclick="sorZoomEvent(${Number(e.no)})" title="Perbesar ke event ini">Zoom</button></td></tr>`,
    )
    .join("");
  const canSave = can("otdr.upload") && !saved && !SOR.savedId;
  $id("sor-view").innerHTML = `
    <div class="sor-chart" id="sor-chart" onwheel="sorWheel(event)">${sorChartSvg(d, SOR.view, 760, 280)}</div>
    <div class="sor-zoom">
      <button type="button" class="data-ghost" onclick="sorZoom(0.5)" title="Perbesar">Perbesar +</button>
      <button type="button" class="data-ghost" onclick="sorZoom(2)" title="Perkecil">Perkecil &minus;</button>
      <button type="button" class="data-ghost" onclick="sorPan(-0.3)" title="Geser kiri">&larr;</button>
      <button type="button" class="data-ghost" onclick="sorPan(0.3)" title="Geser kanan">&rarr;</button>
      <button type="button" class="data-ghost" onclick="sorZoomReset()">Penuh</button>
      <button type="button" class="data-ghost" onclick="sorExportPng()">PNG</button>
      ${canSave ? '<button type="button" class="data-primary" onclick="saveSor()">Simpan</button>' : ""}
      ${can("otdr.upload") && (SOR.savedId || saved) ? (SOR.info.is_baseline ? '<button type="button" class="data-ghost" onclick="sorSetBaseline(false)">Lepas baseline</button>' : '<button type="button" class="data-ghost" onclick="sorSetBaseline(true)">Jadikan baseline</button>') : ""}
      ${SOR.info.is_baseline ? '<span class="sor-badge">Baseline</span>' : ""}
    </div>
    <h4 class="otdr-h">Event (${(d.events || []).length})</h4>
    <div class="imp-scroll"><table class="imp-table"><thead><tr><th>#</th><th>Jarak</th><th>Jenis</th><th>Loss (dB)</th><th>Refl. (dB)</th><th>Redaman (dB/km)</th><th></th></tr></thead><tbody>${ev || '<tr><td colspan="7">Tidak ada event tercatat</td></tr>'}</tbody></table></div>
    <h4 class="otdr-h">Informasi pengukuran</h4>
    <div class="imp-scroll"><table class="imp-table sor-meta"><tbody>${sorMetaRows(d)}</tbody></table></div>`;
}
function sorRedraw() {
  const el = $id("sor-chart");
  if (el && SOR.data) el.innerHTML = sorChartSvg(SOR.data, SOR.view, 760, 280);
}
function sorFull() {
  const p = (SOR.data.trace && SOR.data.trace.points) || [];
  return p.length ? p[p.length - 1][0] : 1;
}
function sorClamp(x0, x1) {
  const full = sorFull();
  let span = Math.max(5, Math.min(x1 - x0, full));
  x0 = Math.max(0, Math.min(x0, full - span));
  SOR.view = { x0, x1: x0 + span };
}
function sorZoom(f, center) {
  if (!SOR.data) return;
  const v = SOR.view,
    c = center != null ? center : (v.x0 + v.x1) / 2,
    span = (v.x1 - v.x0) * f;
  const ratio = (c - v.x0) / (v.x1 - v.x0 || 1);
  sorClamp(c - span * ratio, c - span * ratio + span);
  sorRedraw();
}
function sorPan(f) {
  if (!SOR.data) return;
  const s = SOR.view.x1 - SOR.view.x0;
  sorClamp(SOR.view.x0 + s * f, SOR.view.x1 + s * f);
  sorRedraw();
}
function sorZoomReset() {
  if (!SOR.data) return;
  SOR.view = { x0: 0, x1: sorFull() };
  sorRedraw();
}
function sorZoomEvent(no) {
  const e = ((SOR.data || {}).events || []).find((x) => x.no === no);
  if (!e) return;
  const span = Math.max(40, sorFull() / 20);
  sorClamp(e.distance_m - span / 2, e.distance_m + span / 2);
  sorRedraw();
}
function sorWheel(ev) {
  if (!SOR.data || !ev) return;
  if (ev.preventDefault) ev.preventDefault();
  const box =
    ev.currentTarget && ev.currentTarget.getBoundingClientRect
      ? ev.currentTarget.getBoundingClientRect()
      : null;
  let c = null;
  if (box && box.width) {
    const fx = Math.min(
      1,
      Math.max(
        0,
        (((ev.clientX - box.left) / box.width) * 760 - 52) / (760 - 64),
      ),
    );
    c = SOR.view.x0 + fx * (SOR.view.x1 - SOR.view.x0);
  }
  sorZoom(ev.deltaY < 0 ? 0.7 : 1.4, c);
}
function sorExportPng() {
  if (!SOR.data) return;
  const svg = sorChartSvg(SOR.data, SOR.view, 1200, 440);
  const name = (SOR.name || "trace").replace(/\.sor$/i, "") + ".png";
  try {
    const img = new Image();
    img.onload = () => {
      const cv = document.createElement("canvas");
      cv.width = 1200;
      cv.height = 440;
      const cx = cv.getContext("2d");
      cx.fillStyle = "#fff";
      cx.fillRect(0, 0, 1200, 440);
      cx.drawImage(img, 0, 0);
      cv.toBlob((b) => b && downloadBlobFile(b, name), "image/png");
    };
    img.onerror = () => setDataMsg("sor-msg", "Gagal membuat PNG.", "err");
    img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
  } catch (e) {
    setDataMsg("sor-msg", "Gagal membuat PNG.", "err");
  }
}
function saveSor() {
  if (!SOR.b64) return;
  return apiRequest("/api/otdr/sor", "POST", {
    filename: SOR.name,
    content_base64: SOR.b64,
  })
    .then((r) => {
      SOR.savedId = r.id;
      SOR.info = { is_baseline: false, incident: null };
      setDataMsg("sor-msg", r.message || "Tersimpan", "ok");
      renderSor(true);
      sorLoadCompare();
      if (SORAN.data) renderSorAnalysis();
      loadSorList();
    })
    .catch((err) => setDataMsg("sor-msg", err.message, "err"));
}
function loadSorList() {
  return apiRequest("/api/otdr/sor")
    .then((list) => {
      SOR.list = list;
      sorFillTwoSelect();
      const el = $id("sor-list");
      if (!el) return;
      if (!list.length) {
        el.innerHTML =
          '<div class="data-hint">Belum ada berkas SOR tersimpan.</div>';
        return;
      }
      const del = can("otdr.upload");
      el.innerHTML =
        `<table class="imp-table"><thead><tr><th>Berkas</th><th>Kabel / serat</th><th>Waktu ukur</th><th>Event</th><th></th></tr></thead><tbody>` +
        list
          .map((r) => {
            const g = r.general || {},
              fx = r.fixed || {};
            return `<tr><td><b>${escapeHtml(r.filename)}</b>${r.is_baseline ? ' <span class="sor-badge">Baseline</span>' : ""}${r.analysis && r.analysis.method === "TWOWAY" ? ' <span class="sor-badge">2 arah</span>' : ""}${r.incident ? ` <span class="sor-badge warn">${escapeHtml(r.incident.ticket_number)}</span>` : ""}<br><small>${escapeHtml(r.uploaded_by || "")}</small></td><td>${escapeHtml(g.cable_id || "-")}<br><small>${escapeHtml(g.fiber_id || "")} &middot; ${fx.wavelength_nm != null ? Number(fx.wavelength_nm) + " nm" : ""}</small></td>
      <td>${escapeHtml(sorFmtTs(fx.timestamp))}</td><td class="num">${Number(r.n_events)}</td>
      <td><button type="button" class="data-ghost" onclick="openSor(${Number(r.id)})">Buka</button>
      ${r.analysis ? `<button type="button" class="data-ghost" onclick="sorShowSavedOnMap(${Number(r.id)})">Peta</button>` : ""}
      <a class="data-ghost" href="/api/otdr/sor/${Number(r.id)}/file" download>SOR</a>
      ${del ? `<button type="button" class="data-ghost" onclick="deleteSor(${Number(r.id)})">Hapus</button>` : ""}</td></tr>`;
          })
          .join("") +
        "</tbody></table>";
    })
    .catch(() => {});
}
function openSor(id) {
  return apiRequest("/api/otdr/sor/" + id)
    .then((d) => {
      SOR.name = d.filename;
      SOR.b64 = "";
      SOR.savedId = id;
      setDataMsg("sor-msg", "", "");
      showSor(d, true);
    })
    .catch((err) => setDataMsg("sor-msg", err.message, "err"));
}
function deleteSor(id) {
  if (!confirm("Hapus berkas SOR ini?")) return;
  return apiRequest("/api/otdr/sor/" + id, "DELETE")
    .then(() => {
      if (SOR.savedId === id) {
        SOR.data = null;
        $id("sor-view").innerHTML = "";
      }
      loadSorList();
    })
    .catch((err) => setDataMsg("sor-msg", err.message, "err"));
}

// ----- OTDR SOR: jalur, slack, estimasi titik cut -----
const SORAN = {
  starts: [],
  nodeId: null,
  data: null,
  seq: 0,
  nodes: [],
  mode: "one",
  manualIds: null,
  manualNames: null,
};
let sorMapGroup = null;
const SOR_KIND = {
  NORMAL: ["Normal", "ok"],
  BREAK: ["Putus / patah", "bad"],
  BEND: ["Tekukan / sambungan buruk", "warn"],
  LONGER: ["Data jalur belum lengkap", "warn"],
};
function sorAnReset() {
  SORAN.data = null;
  SORAN.nodeId = null;
  SORAN.starts = [];
  SORAN.mode = "one";
  SORAN.manualIds = null;
  SORAN.manualNames = null;
  const box = $id("sor-an");
  if (box) box.style.display = SOR.data ? "" : "none";
  if ($id("sor-an-result")) $id("sor-an-result").innerHTML = "";
  if ($id("sor-start-q")) $id("sor-start-q").value = "";
  if ($id("sor-start-port"))
    $id("sor-start-port").innerHTML =
      '<option value="">-- pilih aset awal dahulu --</option>';
  setDataMsg("sor-an-msg", "", "");
}
let sorStartTimer = null;
function onSorStartInput() {
  clearTimeout(sorStartTimer);
  sorStartTimer = setTimeout(() => {
    const q = ($id("sor-start-q").value || "").trim();
    if (!q) return;
    apiRequest("/api/otdr/sor/start-nodes?q=" + encodeURIComponent(q))
      .then((list) => {
        SORAN.nodes = list;
        $id("sor-start-dl").innerHTML = list
          .map(
            (n) =>
              `<option value="${escapeHtml(n.name)}">${escapeHtml(n.type || "")}</option>`,
          )
          .join("");
      })
      .catch(() => {});
  }, 200);
}
function onSorStartPick() {
  const q = ($id("sor-start-q").value || "").trim().toLowerCase();
  const n = (SORAN.nodes || []).find((x) => (x.name || "").toLowerCase() === q);
  const sel = $id("sor-start-port");
  if (!n) {
    SORAN.nodeId = null;
    sel.innerHTML = '<option value="">-- pilih aset awal dahulu --</option>';
    return Promise.resolve();
  }
  SORAN.nodeId = n.id;
  return apiRequest(`/api/nodes/${Number(n.id)}/otdr-starts`)
    .then((d) => {
      SORAN.starts = d.starts || [];
      sel.innerHTML = SORAN.starts.length
        ? SORAN.starts
            .map(
              (s, i) =>
                `<option value="${i}">${escapeHtml(s.port || "-")} ${s.direction === "down" ? "&rarr;" : "&larr;"} ${escapeHtml(s.to || "?")}${s.cable ? ` (kabel ${escapeHtml(s.cable)}${s.core ? " core " + escapeHtml(String(s.core)) : ""})` : ""}</option>`,
            )
            .join("")
        : '<option value="">Aset ini belum punya sambungan (pakai jalur manual)</option>';
    })
    .catch((err) => setDataMsg("sor-an-msg", err.message, "err"));
}
function sorAnBody(save) {
  const body = { start_node_id: SORAN.nodeId, save: !!save };
  const manual = ($id("sor-manual").value || "").trim();
  const sel = $id("sor-start-port").value;
  const st = sel !== "" ? SORAN.starts[Number(sel)] : null;
  if (st) {
    body.start_port = st.port;
    body.direction = st.direction;
  }
  if (
    manual &&
    SORAN.manualIds &&
    SORAN.manualNames &&
    manual === SORAN.manualNames.join(", ")
  ) {
    body.cable_ids = SORAN.manualIds.slice();
  } else if (manual) {
    const segs = OTDR.segs || [];
    const ids = [];
    for (const nm of manual
      .split(",")
      .map((x) => x.trim())
      .filter(Boolean)) {
      const s = segs.find(
        (x) => (x.name || "").toLowerCase() === nm.toLowerCase(),
      );
      if (!s) return { error: `Kabel '${nm}' tidak ditemukan` };
      ids.push(s.id);
    }
    body.cable_ids = ids;
  } else if (!st)
    return { error: "Pilih port/arah pada aset awal, atau isi jalur manual." };
  const sl = $id("sor-slack").value;
  if (sl !== "") body.slack_m = Number(sl);
  body.launch_offset_m = Number($id("sor-launch").value || 0);
  if (SOR.savedId) body.sor_id = SOR.savedId;
  else body.content_base64 = SOR.b64;
  return { body };
}
function runSorAnalysis(save) {
  if (!SOR.data) return;
  if (!SORAN.nodeId)
    return setDataMsg(
      "sor-an-msg",
      "Pilih aset awal dari daftar saran.",
      "err",
    );
  const b = sorAnBody(save);
  if (b.error) return setDataMsg("sor-an-msg", b.error, "err");
  const seq = ++SORAN.seq;
  setDataMsg("sor-an-msg", "Menganalisis jalur...", "info");
  return apiRequest("/api/otdr/sor/analyze", "POST", b.body)
    .then((d) => {
      if (seq !== SORAN.seq) return;
      SORAN.mode = "one";
      setDataMsg(
        "sor-an-msg",
        save ? "Analisis tersimpan" : "",
        save ? "ok" : "",
      );
      SORAN.data = d;
      renderSorAnalysis();
    })
    .catch((err) => {
      if (seq !== SORAN.seq) return;
      setDataMsg("sor-an-msg", err.message, "err");
    });
}
function sorIncidentBtn(e) {
  if (
    !SOR.savedId ||
    !can("incident.write") ||
    !e ||
    e.lat == null ||
    !(e.kind === "BREAK" || e.kind === "BEND")
  )
    return "";
  if (SOR.info.incident)
    return `<button type="button" class="data-ghost" onclick="sorOpenIncident()">Tiket ${escapeHtml(SOR.info.incident.ticket_number)}</button>`;
  return `<select id="sor-sev"><option value="Critical">Critical</option><option value="Major">Major</option><option value="Minor">Minor</option></select> <button type="button" class="data-primary" onclick="sorCreateIncident()">Buat tiket gangguan</button>`;
}
function renderSorAnalysis() {
  const d = SORAN.data;
  if (!d) return;
  const p = d.path,
    e = d.end;
  const k = e ? SOR_KIND[e.kind] || [e.kind, "warn"] : null;
  const endHtml = e
    ? `<div class="loc-card sor-end sor-${k[1]}"><b>${escapeHtml(k[0])}</b><div>${escapeHtml(e.message)}</div>
      <div class="cov-meta">Event akhir #${Number(e.event_no)} pada ${escapeHtml(sorKm(e.end_m))} &middot; jalur ${escapeHtml(sorKm(e.path_m))} &middot; selisih ${e.diff_m} m (${e.diff_pct}%, toleransi &plusmn;${e.tolerance_pct}%)</div>
      ${e.twoway ? `<div class="cov-meta"><b>Dua arah</b>: A ${escapeHtml(sorKm(e.twoway.x_a_m))} (&plusmn;${e.twoway.uncertainty_a_m}) &middot; B ${escapeHtml(sorKm(e.twoway.x_b_m))} (&plusmn;${e.twoway.uncertainty_b_m}) &middot; gabungan ${escapeHtml(sorKm(e.twoway.fused_m))} (&plusmn;${e.twoway.uncertainty_fused_m}) &middot; ${e.twoway.consistent ? "konsisten" : "<b>TIDAK konsisten</b>"}</div>` : ""}${
        e.lat != null
          ? `<div><b>${e.lat.toFixed(6)}, ${e.lng.toFixed(6)}</b> <button type="button" class="data-ghost" onclick="copyText('${e.lat.toFixed(6)}, ${e.lng.toFixed(6)}')">Salin</button>
        <div class="cov-meta">&plusmn;${Number(e.uncertainty_m)} m (95%) &middot; ${Number(e.slack_passed)} slack terlewati &middot; dekat ${escapeHtml(e.near_node || "?")} (${e.near_gap_m >= 0 ? "+" : ""}${e.near_gap_m} m)</div></div>`
          : ""
      }</div>`
    : "";
  const rows = (d.events || [])
    .map((x) =>
      x.before_start
        ? `<tr><td>${Number(x.no)}</td><td colspan="6"><small>sebelum titik awal (launch)</small></td></tr>`
        : `<tr><td>${Number(x.no)}</td><td>${escapeHtml(sorKm(x.optical_m))}</td><td>${escapeHtml(sorKm(x.path_m))}</td><td class="num">${Number(x.slack_passed)}</td>
      <td class="num">&plusmn;${Number(x.uncertainty_m)}</td><td>${x.lat != null && !x.beyond_path ? `${x.lat.toFixed(5)}, ${x.lng.toFixed(5)}` : "&ndash;"}</td><td>${escapeHtml(x.near_node || "")}<br><small>${x.near_gap_m >= 0 ? "+" : ""}${x.near_gap_m} m</small></td></tr>`,
    )
    .join("");
  const nodes = p.nodes
    .map(
      (n) =>
        `${escapeHtml(n.name)} <small>(${escapeHtml(sorKm(n.optical_m))}${n.slack ? ", slack" : ""})</small>`,
    )
    .join(" &rarr; ");
  $id("sor-an-result").innerHTML = `${endHtml}
    <div class="cov-meta">Jalur: ${nodes}<br>Panjang optik ${escapeHtml(sorKm(p.optical_total_m))} (peta ${escapeHtml(sorKm(p.geo_total_m))} + helix ${p.helix_pct}% + ${Number(p.slack_count)} slack &times; ${p.slack_m} m)${p.manual ? " &middot; jalur manual" : ""}</div>
    ${(d.notes || []).map((w) => `<div class="pr-suggest">${escapeHtml(w)}</div>`).join("")}
    <div class="imp-scroll"><table class="imp-table"><thead><tr><th>#</th><th>Jarak OTDR</th><th>Jarak jalur</th><th>Slack</th><th>Galat 95% (m)</th><th>Lat, Lng</th><th>Dekat aset</th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="sor-zoom"><button type="button" class="data-primary" onclick="sorShowOnMap()">Tampilkan di peta</button>
      ${can("otdr.upload") && SOR.savedId ? `<button type="button" class="data-ghost" onclick="${SORAN.mode === "two" ? "runSorTwoWay(true)" : "runSorAnalysis(true)"}">Simpan analisis</button>` : SOR.savedId ? "" : "<small>Simpan berkas SOR untuk menyimpan analisis.</small>"}
      <button type="button" class="data-ghost" onclick="sorOpenReport()">Laporan (PDF)</button>
      ${sorIncidentBtn(e)}</div>`;
}
function sorDrawOnMap(a, label) {
  if (!a || !a.path) return false;
  if (!sorMapGroup) sorMapGroup = L.layerGroup().addTo(map);
  sorMapGroup.clearLayers();
  const bounds = [];
  (a.path.segments || []).forEach((s) => {
    if (!s.line || s.line.length < 2) return;
    sorMapGroup.addLayer(
      L.polyline(s.line, { color: "#7c3aed", weight: 5, opacity: 0.75 }),
    );
    s.line.forEach((pt) => bounds.push(pt));
  });
  const st = a.path.start;
  if (st)
    sorMapGroup.addLayer(
      L.circleMarker([st.lat, st.lng], {
        radius: 8,
        color: "#fff",
        weight: 2,
        fillColor: "#16a34a",
        fillOpacity: 1,
      }).bindPopup(`<b>Titik awal ukur</b><br>${escapeHtml(st.name)}`),
    );
  (a.events || []).forEach((x) => {
    if (x.lat == null || x.beyond_path || x.before_start) return;
    sorMapGroup.addLayer(
      L.circleMarker([x.lat, x.lng], {
        radius: 5,
        color: "#fff",
        weight: 1.5,
        fillColor: "#db2777",
        fillOpacity: 1,
      }).bindPopup(
        `<b>Event #${Number(x.no)}</b><br>${escapeHtml(sorKm(x.optical_m))} &middot; ${Number(x.slack_passed)} slack<br>&plusmn;${Number(x.uncertainty_m)} m<br>Dekat ${escapeHtml(x.near_node || "?")}`,
      ),
    );
  });
  const e = a.end;
  if (e && e.lat != null) {
    const k = SOR_KIND[e.kind] || [e.kind];
    sorMapGroup.addLayer(
      L.circle([e.lat, e.lng], {
        radius: Math.max(5, e.uncertainty_m || 0),
        color: "#dc2626",
        weight: 1.5,
        fillColor: "#dc2626",
        fillOpacity: 0.12,
      }),
    );
    const mk = L.circleMarker([e.lat, e.lng], {
      radius: 11,
      color: "#fff",
      weight: 3,
      fillColor: "#dc2626",
      fillOpacity: 1,
    }).bindPopup(
      `<b>${escapeHtml(k[0])}</b> ${label ? "&middot; " + escapeHtml(label) : ""}<br>${escapeHtml(e.message)}<br>&plusmn;${Number(e.uncertainty_m)} m (95%) &middot; ${Number(e.slack_passed)} slack terlewati<br>Dekat ${escapeHtml(e.near_node || "?")} (${e.near_gap_m} m)`,
    );
    sorMapGroup.addLayer(mk);
    bounds.push([e.lat, e.lng]);
    setTimeout(() => mk.openPopup(), 300);
  }
  if (bounds.length) map.fitBounds(bounds, { padding: [60, 60], maxZoom: 18 });
  return true;
}
function sorShowOnMap() {
  if (!SORAN.data) return;
  sorDrawOnMap(SORAN.data, SOR.name);
  closeDataModal();
}
function sorShowSavedOnMap(id) {
  return apiRequest("/api/otdr/sor/" + id)
    .then((d) => {
      if (!d.analysis)
        return setDataMsg(
          "sor-msg",
          "Berkas ini belum punya analisis tersimpan.",
          "err",
        );
      sorDrawOnMap(d.analysis, d.filename);
      closeDataModal();
    })
    .catch((err) => setDataMsg("sor-msg", err.message, "err"));
}
function sorRestoreAnalysis(d) {
  sorAnReset();
  const a = d.analysis;
  if (!a) return;
  SORAN.data = a;
  const rq = a.request || {};
  SORAN.nodeId = rq.start_node_id || null;
  if (a.path && a.path.start) $id("sor-start-q").value = a.path.start.name;
  if (rq.slack_m != null) $id("sor-slack").value = rq.slack_m;
  $id("sor-launch").value = rq.launch_offset_m || 0;
  renderSorAnalysis();
}

// ----- SOR lanjutan: baseline, perbandingan, tiket, dua arah, jalur lewat peta, laporan -----
function sorFillTwoSelect() {
  const sel = $id("sor-b-sel");
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML =
    '<option value="">-- pilih berkas --</option>' +
    (SOR.list || [])
      .filter((r) => r.id !== SOR.savedId)
      .map((r) => {
        const g = r.general || {};
        return `<option value="${Number(r.id)}">${escapeHtml(r.filename)} (${escapeHtml(g.cable_id || "-")} / ${escapeHtml(g.fiber_id || "-")})</option>`;
      })
      .join("");
  if (cur) sel.value = cur;
}
function sorSetBaseline(on) {
  if (!SOR.savedId)
    return setDataMsg("sor-msg", "Simpan berkas SOR dahulu.", "err");
  return apiRequest(
    `/api/otdr/sor/${Number(SOR.savedId)}/baseline`,
    on ? "POST" : "DELETE",
  )
    .then((r) => {
      SOR.info.is_baseline = !!on;
      setDataMsg("sor-msg", r.message || "", "ok");
      SOR.base = null;
      renderSor(true);
      loadSorList();
      return sorLoadCompare();
    })
    .catch((err) => setDataMsg("sor-msg", err.message, "err"));
}
const CMP_LABEL = {
  SAME: "Sama",
  CHANGED: "Berubah",
  NEW: "Baru",
  LOST: "Hilang",
};
function sorLoadCompare() {
  const box = $id("sor-cmp");
  if (!box) return Promise.resolve();
  if (!SOR.savedId) {
    box.innerHTML = "";
    return Promise.resolve();
  }
  if (SOR.info.is_baseline) {
    box.innerHTML =
      '<div class="data-hint">Berkas ini adalah baseline untuk kabel/serat/panjang gelombangnya. Ukur ulang, unggah, lalu bandingkan.</div>';
    return Promise.resolve();
  }
  const id = SOR.savedId;
  return apiRequest(`/api/otdr/sor/${Number(id)}/compare`)
    .then((r) => {
      if (id !== SOR.savedId) return;
      SOR.base = r.base_trace || null;
      SOR.cmp = r;
      sorRedraw();
      box.innerHTML = sorCompareHtml(r);
    })
    .catch((err) => {
      if (id !== SOR.savedId) return;
      SOR.base = null;
      SOR.cmp = null;
      sorRedraw();
      box.innerHTML = `<div class="data-hint">${escapeHtml(err.message)}</div>`;
    });
}
function sorCompareHtml(r) {
  const t = r.total || {},
    e = r.end || {};
  const rows = (r.events || [])
    .map(
      (
        x,
      ) => `<tr class="${x.status === "BAD" ? "cmp-bad" : x.status === "WARN" ? "cmp-warn" : ""}"><td>${escapeHtml(CMP_LABEL[x.kind] || x.kind)}</td><td>${escapeHtml(sorKm(x.distance_m))}${x.shift_m ? `<br><small>${x.shift_m > 0 ? "+" : ""}${x.shift_m} m</small>` : ""}</td>
    <td class="num">${x.base_loss != null ? Number(x.base_loss).toFixed(3) : "&ndash;"}</td><td class="num">${x.new_loss != null ? Number(x.new_loss).toFixed(3) : "&ndash;"}</td>
    <td class="num">${x.delta_loss != null ? (x.delta_loss > 0 ? "+" : "") + x.delta_loss : "&ndash;"}</td><td class="num">${x.delta_refl != null ? (x.delta_refl > 0 ? "+" : "") + x.delta_refl : "&ndash;"}</td><td>${otdrBadge(x.status)}</td></tr>`,
    )
    .join("");
  return `<h4 class="otdr-h">Perbandingan dengan baseline ${otdrBadge(r.overall)}</h4>
    <div class="cov-meta">Baseline: <b>${escapeHtml(r.base_filename || "")}</b> (${escapeHtml(sorFmtTs(r.base_time))}) &middot; garis abu-abu putus-putus pada grafik${r.wavelength_diff_nm && Math.abs(r.wavelength_diff_nm) > 10 ? " &middot; <b>panjang gelombang berbeda " + escapeHtml(String(r.wavelength_diff_nm)) + " nm</b>" : ""}<br>
    Total loss: ${t.base != null ? Number(t.base).toFixed(3) : "-"} &rarr; ${t.new != null ? Number(t.new).toFixed(3) : "-"} dB (${t.delta != null ? (t.delta > 0 ? "+" : "") + t.delta : "-"}) ${otdrBadge(t.status)}<br>
    Ujung serat: ${escapeHtml(sorKm(e.base_m))} &rarr; ${escapeHtml(sorKm(e.new_m))} (${e.delta_m != null ? (e.delta_m > 0 ? "+" : "") + e.delta_m + " m" : "-"}) ${otdrBadge(e.status)}</div>
    <div class="imp-scroll"><table class="imp-table"><thead><tr><th>Status</th><th>Jarak</th><th>Loss lama</th><th>Loss baru</th><th>&Delta; loss</th><th>&Delta; refl.</th><th></th></tr></thead><tbody>${rows || '<tr><td colspan="7">Tidak ada event</td></tr>'}</tbody></table></div>`;
}

// tiket gangguan
function sorCreateIncident() {
  if (!SOR.savedId || !SORAN.data) return;
  const sev = ($id("sor-sev") || {}).value || "Critical";
  const run = SORAN.mode === "two" ? runSorTwoWay(true) : runSorAnalysis(true);
  return Promise.resolve(run)
    .then(() =>
      apiRequest(`/api/otdr/sor/${Number(SOR.savedId)}/incident`, "POST", {
        severity: sev,
      }),
    )
    .then((r) => {
      SOR.info.incident = { id: r.id, ticket_number: r.ticket_number };
      setDataMsg("sor-an-msg", `Tiket ${r.ticket_number} dibuat.`, "ok");
      renderSorAnalysis();
      loadSorList();
      try {
        loadData();
      } catch (_) {
        /* peta dimuat ulang bila tersedia */
      }
    })
    .catch((err) => setDataMsg("sor-an-msg", err.message, "err"));
}
function sorOpenIncident() {
  const i = SOR.info.incident;
  if (!i) return;
  closeDataModal();
  openIncidentDetail(i.id);
}

// dua arah
function runSorTwoWay(save) {
  if (!SOR.data) return;
  if (!SOR.savedId)
    return setDataMsg(
      "sor-an-msg",
      "Simpan berkas SOR ini dahulu (dua arah memakai dua berkas tersimpan).",
      "err",
    );
  const bid = Number(($id("sor-b-sel") || {}).value || 0);
  if (!bid)
    return setDataMsg("sor-an-msg", "Pilih SOR dari ujung seberang.", "err");
  if (!SORAN.nodeId)
    return setDataMsg(
      "sor-an-msg",
      "Pilih aset awal dari daftar saran.",
      "err",
    );
  const b = sorAnBody(false);
  if (b.error) return setDataMsg("sor-an-msg", b.error, "err");
  const body = Object.assign({}, b.body, {
    sor_id: SOR.savedId,
    sor_id_b: bid,
    launch_offset_b_m: Number($id("sor-launch-b").value || 0),
    save: !!save === true,
  });
  delete body.content_base64;
  const seq = ++SORAN.seq;
  setDataMsg("sor-an-msg", "Menganalisis dua arah...", "info");
  return apiRequest("/api/otdr/sor/analyze2", "POST", body)
    .then((d) => {
      if (seq !== SORAN.seq) return;
      SORAN.mode = "two";
      setDataMsg(
        "sor-an-msg",
        save === true ? "Analisis dua arah tersimpan" : "",
        save === true ? "ok" : "",
      );
      SORAN.data = d;
      renderSorAnalysis();
    })
    .catch((err) => {
      if (seq !== SORAN.seq) return;
      setDataMsg("sor-an-msg", err.message, "err");
    });
}

// jalur manual lewat peta
const SORPICK = { on: false, start: null, cables: [], layers: [] };
function sorPickBanner() {
  const names =
    SORPICK.cables.map((c) => escapeHtml(c.name)).join(" &rarr; ") || "&ndash;";
  connectBanner(`<i class="fa-solid fa-route"></i> Jalur OTDR: ${SORPICK.start ? "awal <b>" + escapeHtml(SORPICK.start.name) + "</b>" : "klik <b>aset awal</b> dahulu"} &middot; kabel: ${names}
    <button type="button" class="data-ghost" onclick="sorPickUndo()">Urungkan</button> <button type="button" class="data-primary" onclick="sorPickDone()">Selesai</button> <button type="button" class="data-ghost" onclick="sorPickCancel()">Batal</button> <small>(Esc)</small>`);
}
function sorPickStart() {
  if (CONNECT.active)
    return setDataMsg(
      "sor-an-msg",
      "Selesaikan mode Hubungkan aset dahulu.",
      "err",
    );
  SORPICK.on = true;
  SORPICK.start = null;
  SORPICK.cables = [];
  SORPICK.layers = [];
  closeDataModal();
  sorPickBanner();
}
function sorPickNode(p) {
  if (!SORPICK.on) return;
  if (SORPICK.start) {
    sorPickBanner();
    return;
  }
  SORPICK.start = { id: Number(p.id), name: p.name };
  sorPickBanner();
}
function sorPickCable(layer) {
  if (!SORPICK.on) return;
  const p = layer.metaData || {};
  if (!SORPICK.start) {
    sorPickBanner();
    return;
  }
  const last = SORPICK.cables[SORPICK.cables.length - 1];
  if (last && last.id === Number(p.id)) return;
  SORPICK.cables.push({ id: Number(p.id), name: p.name });
  if (layer.setStyle) {
    layer._sorOrig = layer._sorOrig || {
      color: layer.options.color,
      weight: layer.options.weight,
    };
    layer.setStyle({ color: "#7c3aed", weight: 7 });
    SORPICK.layers.push(layer);
  }
  sorPickBanner();
}
function sorPickUndo() {
  if (SORPICK.cables.length) SORPICK.cables.pop();
  else SORPICK.start = null;
  sorPickBanner();
}
function sorPickRestore() {
  SORPICK.layers.forEach((l) => {
    if (l._sorOrig && l.setStyle) l.setStyle(l._sorOrig);
  });
  SORPICK.layers = [];
}
function sorPickCancel() {
  SORPICK.on = false;
  sorPickRestore();
  connectBanner("");
  $id("modal-data").style.display = "flex";
}
function sorPickDone() {
  if (!SORPICK.start) return sorPickBanner();
  const picked = { start: SORPICK.start, cables: SORPICK.cables.slice() };
  SORPICK.on = false;
  sorPickRestore();
  connectBanner("");
  $id("modal-data").style.display = "flex";
  setDataTab("otdr");
  SORAN.nodeId = picked.start.id;
  $id("sor-start-q").value = picked.start.name;
  SORAN.nodes = [{ id: picked.start.id, name: picked.start.name }];
  SORAN.manualIds = picked.cables.map((c) => c.id);
  SORAN.manualNames = picked.cables.map((c) => c.name);
  $id("sor-manual").value = SORAN.manualNames.join(", ");
  $id("sor-start-port").innerHTML = '<option value="">(jalur manual)</option>';
  SORAN.starts = [];
  setDataMsg(
    "sor-an-msg",
    picked.cables.length
      ? "Jalur manual terisi dari peta. Klik Analisis."
      : "Pilih minimal satu kabel.",
    picked.cables.length ? "info" : "err",
  );
}
document.addEventListener("keydown", (e) => {
  if (e && e.key === "Escape" && SORPICK.on) sorPickCancel();
});

// laporan cetak / PDF
function sorPathSvg(a, w, h) {
  const segs = ((a.path || {}).segments || []).filter(
    (s) => s.line && s.line.length > 1,
  );
  const all = [];
  segs.forEach((s) => s.line.forEach((p) => all.push(p)));
  const e = a.end || {};
  if (e.lat != null) all.push([e.lat, e.lng]);
  if (!all.length) return "";
  const lat0 = all.reduce((t, p) => t + p[0], 0) / all.length,
    kx = Math.cos((lat0 * Math.PI) / 180);
  let minX = Infinity,
    maxX = -Infinity,
    minY = Infinity,
    maxY = -Infinity;
  all.forEach((p) => {
    const x = p[1] * kx,
      y = -p[0];
    minX = Math.min(minX, x);
    maxX = Math.max(maxX, x);
    minY = Math.min(minY, y);
    maxY = Math.max(maxY, y);
  });
  const pad = 30,
    sx = (w - 2 * pad) / (maxX - minX || 1e-9),
    sy = (h - 2 * pad) / (maxY - minY || 1e-9),
    s = Math.min(sx, sy);
  const X = (p) =>
    pad + (p[1] * kx - minX) * s + (w - 2 * pad - (maxX - minX) * s) / 2;
  const Y = (p) =>
    pad + (-p[0] - minY) * s + (h - 2 * pad - (maxY - minY) * s) / 2;
  const pxPerM = s / (110540 / 1); // derajat lintang -> meter
  let g = `<rect width="${w}" height="${h}" fill="#f8fafc" stroke="#cbd5e1"/>`;
  segs.forEach((sg) => {
    g += `<polyline fill="none" stroke="#7c3aed" stroke-width="3" points="${sg.line.map((p) => X(p).toFixed(1) + "," + Y(p).toFixed(1)).join(" ")}"/>`;
  });
  ((a.path || {}).nodes || []).forEach((n) => {
    if (n.lat == null) return;
    g += `<circle cx="${X([n.lat, n.lng]).toFixed(1)}" cy="${Y([n.lat, n.lng]).toFixed(1)}" r="5" fill="${n.slack ? "#f59e0b" : "#16a34a"}" stroke="#fff"/><text x="${(X([n.lat, n.lng]) + 8).toFixed(1)}" y="${(Y([n.lat, n.lng]) - 6).toFixed(1)}" font-size="11" fill="#334155">${escapeHtml(n.name)}</text>`;
  });
  if (e.lat != null) {
    const cx = X([e.lat, e.lng]),
      cy = Y([e.lat, e.lng]),
      r = Math.max(6, (e.uncertainty_m || 0) * pxPerM);
    g += `<circle cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="${r.toFixed(1)}" fill="#dc2626" fill-opacity="0.12" stroke="#dc2626"/><circle cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="6" fill="#dc2626" stroke="#fff" stroke-width="2"/>`;
  }
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}" width="100%" style="max-width:${w}px">${g}</svg>`;
}
function sorReportHtml() {
  const d = SOR.data,
    a = SORAN.data;
  const e = a.end || {},
    k = SOR_KIND[e.kind] || [e.kind || "-"];
  const full = { x0: 0, x1: sorFull() };
  const links =
    e.lat != null
      ? `<a href="https://www.google.com/maps?q=${e.lat.toFixed(6)},${e.lng.toFixed(6)}">Google Maps</a> &middot; <a href="https://www.openstreetmap.org/?mlat=${e.lat.toFixed(6)}&amp;mlon=${e.lng.toFixed(6)}#map=18/${e.lat.toFixed(6)}/${e.lng.toFixed(6)}">OpenStreetMap</a>`
      : "";
  const tw = e.twoway
    ? `<p><b>Dua arah:</b> A ${escapeHtml(sorKm(e.twoway.x_a_m))} &middot; B ${escapeHtml(sorKm(e.twoway.x_b_m))} &middot; gabungan ${escapeHtml(sorKm(e.twoway.fused_m))} (&plusmn;${e.twoway.uncertainty_fused_m} m) &middot; ${e.twoway.consistent ? "konsisten" : "<b>TIDAK konsisten</b>"}</p>`
    : "";
  const evRows = (a.events || [])
    .map((x) =>
      x.before_start
        ? ""
        : `<tr><td>${Number(x.no)}</td><td>${escapeHtml(sorKm(x.optical_m))}</td><td>${escapeHtml(sorKm(x.path_m))}</td><td>${Number(x.slack_passed)}</td><td>&plusmn;${Number(x.uncertainty_m)}</td><td>${x.lat != null && !x.beyond_path ? x.lat.toFixed(6) + ", " + x.lng.toFixed(6) : "&ndash;"}</td><td>${escapeHtml(x.near_node || "")} (${x.near_gap_m} m)</td></tr>`,
    )
    .join("");
  const segRows = ((a.path || {}).segments || [])
    .map(
      (s, i) =>
        `<tr><td>${i + 1}</td><td>${escapeHtml(s.cable || "-")}</td><td>${escapeHtml(s.core || "-")}</td><td>${escapeHtml(s.from)} &rarr; ${escapeHtml(s.to || "?")}</td><td>${escapeHtml(sorKm(s.geo_m))}</td></tr>`,
    )
    .join("");
  const cmp = SOR.cmp
    ? `<h2>Perbandingan dengan baseline</h2>${sorCompareHtml(SOR.cmp).replace(/<h4[^>]*>.*?<\/h4>/, "")}`
    : "";
  const css =
    "body{font:13px/1.45 Arial,sans-serif;color:#0f172a;margin:24px}h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:18px 0 6px;border-bottom:1px solid #cbd5e1;padding-bottom:3px}table{border-collapse:collapse;width:100%;margin:4px 0}th,td{border:1px solid #cbd5e1;padding:4px 6px;text-align:left;vertical-align:top}th{background:#f1f5f9}.card{border:2px solid #dc2626;border-radius:6px;padding:10px 14px;margin:10px 0;background:#fef2f2}.ok{border-color:#16a34a;background:#f0fdf4}.warn{border-color:#f59e0b;background:#fffbeb}small{color:#64748b}svg{max-width:100%;height:auto}@media print{body{margin:10mm}.noprint{display:none}h2{page-break-after:avoid}table,svg{page-break-inside:avoid}}";
  const cls =
    (SOR_KIND[e.kind] || [0, "warn"])[1] === "ok"
      ? "ok"
      : (SOR_KIND[e.kind] || [0, "warn"])[1] === "warn"
        ? "warn"
        : "";
  return `<!doctype html><html lang="id"><head><meta charset="utf-8"><title>Laporan titik cut - ${escapeHtml(SOR.name || "OTDR")}</title><style>${css}</style></head><body>
    <button class="noprint" onclick="window.print()">Cetak / Simpan PDF</button>
    <h1>Laporan Estimasi Titik Cut OTDR</h1><small>Dibuat ${escapeHtml(new Date().toISOString().replace("T", " ").slice(0, 16))} UTC &middot; berkas ${escapeHtml(SOR.name || "-")}</small>
    <div class="card ${cls}"><b style="font-size:16px">${escapeHtml(k[0])}</b><br>${escapeHtml(e.message || "")}
      ${e.lat != null ? `<p style="font-size:16px;margin:8px 0 2px"><b>${e.lat.toFixed(6)}, ${e.lng.toFixed(6)}</b></p><div>&plusmn;${Number(e.uncertainty_m)} m (95%) &middot; ${Number(e.slack_passed)} slack terlewati &middot; dekat ${escapeHtml(e.near_node || "?")} (${e.near_gap_m} m)${e.segment_cable ? " &middot; kabel " + escapeHtml(e.segment_cable) : ""}</div><div>${links}</div>` : ""}
      ${tw}</div>
    <h2>Jalur ukur</h2>${sorPathSvg(a, 700, 300)}<p><small>Ungu = jalur kabel, hijau = aset, oranye = aset dengan slack, merah = titik cut dan lingkaran galat. Skema tanpa peta dasar.</small></p>
    <p>${((a.path || {}).nodes || []).map((n) => escapeHtml(n.name) + " <small>(" + escapeHtml(sorKm(n.optical_m)) + ")</small>").join(" &rarr; ")}<br>Panjang optik ${escapeHtml(sorKm((a.path || {}).optical_total_m))} (peta ${escapeHtml(sorKm((a.path || {}).geo_total_m))} + helix ${(a.path || {}).helix_pct}% + ${Number((a.path || {}).slack_count)} slack &times; ${(a.path || {}).slack_m} m)</p>
    <table><thead><tr><th>#</th><th>Kabel</th><th>Core</th><th>Segmen</th><th>Panjang peta</th></tr></thead><tbody>${segRows}</tbody></table>
    <h2>Trace OTDR</h2>${sorChartSvg(d, full, 760, 280)}
    <h2>Event</h2><table><thead><tr><th>#</th><th>Jarak OTDR</th><th>Jarak jalur</th><th>Slack</th><th>Galat 95% (m)</th><th>Lat, Lng</th><th>Dekat aset</th></tr></thead><tbody>${evRows}</tbody></table>
    ${cmp}
    <h2>Informasi pengukuran</h2><table><tbody>${sorMetaRows(d)}</tbody></table>
    <p><small>Estimasi berdasarkan data jalur kabel di sistem. Galat 95% menggabungkan galat slack, rute peta, helix, dan resolusi alat. Verifikasi di lapangan sebelum penggalian.</small></p></body></html>`;
}
function sorOpenReport() {
  if (!SOR.data || !SORAN.data)
    return setDataMsg("sor-an-msg", "Jalankan analisis jalur dahulu.", "err");
  const html = sorReportHtml();
  const w = window.open("", "_blank");
  if (!w)
    return setDataMsg(
      "sor-an-msg",
      "Peramban memblokir jendela laporan; izinkan pop-up lalu coba lagi.",
      "err",
    );
  w.document.open();
  w.document.write(html);
  w.document.close();
  setTimeout(() => {
    try {
      w.focus();
      w.print();
    } catch (_) {
      /* pengguna bisa cetak manual */
    }
  }, 500);
}
function initOtdrTab() {
  const ok = can("otdr.upload");
  $id("otdr-denied").style.display = ok ? "none" : "block";
  $id("otdr-form").style.display = ok ? "" : "none";
  loadOtdrSegments();
  loadOtdrList();
  $id("sor-form").style.display = ok ? "" : "none";
  loadSorList();
  $id("loss-admin").style.display = can("loss.edit") ? "" : "none";
  if (can("loss.edit")) loadLossParams();
}
function onOtdrFileChosen(ev) {
  const f = ev && ev.target && ev.target.files && ev.target.files[0];
  if (!f) return;
  if (f.size > 8 * 1024 * 1024)
    return setDataMsg("otdr-msg", "Berkas lebih dari 8 MB.", "err");
  const rd = new FileReader();
  rd.onload = () => {
    OTDR.name = f.name;
    OTDR.b64 = bufToBase64(rd.result);
    $id("otdr-file-name").textContent = f.name;
    previewOtdr();
  };
  rd.onerror = () =>
    setDataMsg("otdr-msg", "Berkas tidak dapat dibaca.", "err");
  rd.readAsArrayBuffer(f);
  try {
    ev.target.value = "";
  } catch (e) {
    /* abaikan */
  }
}
function otdrSeg() {
  const id = ($id("otdr-seg") || {}).value;
  const seg = id
    ? (OTDR.segs || []).find((s) => String(s.id) === String(id))
    : null;
  return { seg, from: ($id("otdr-seg-from") || {}).value || "A" };
}
function otdrBody() {
  const s = otdrSeg();
  return {
    filename: OTDR.name,
    content_base64: OTDR.b64,
    on_duplicate: $id("otdr-dup").value || "skip",
    default_cable_id: s.seg ? s.seg.id : null,
    default_from: s.seg ? s.from : null,
  };
}
function loadOtdrSegments() {
  return apiRequest(
    "/api/otdr/segments" +
      scopeQS("?") +
      (scopeQS("?") ? "&" : "?") +
      "limit=500",
  )
    .then((list) => {
      OTDR.segs = list;
      const sel = $id("otdr-seg");
      if (!sel) return;
      const cur = sel.value;
      sel.innerHTML =
        '<option value="">-- Ikuti kolom Kabel di berkas --</option>' +
        list
          .map(
            (s) =>
              `<option value="${Number(s.id)}">${escapeHtml(s.name)} (${escapeHtml(s.a ? s.a.name : "?")} → ${escapeHtml(s.b ? s.b.name : "?")}, ${fmtM(s.length_m)})</option>`,
          )
          .join("");
      if (cur) sel.value = cur;
      onOtdrSegChange();
    })
    .catch(() => {});
}
function onOtdrSegChange() {
  const s = otdrSeg();
  const info = $id("otdr-seg-info");
  const fromSel = $id("otdr-seg-from");
  if (!info) return;
  if (!s.seg) {
    info.textContent =
      "Pilih kabel agar arah ukur (dari mana ke mana) jelas. Dipakai oleh unggah, input manual, dan pencarian titik putus di bawah.";
    return;
  }
  const a = s.seg.a ? s.seg.a.name : "ujung A (tanpa aset)",
    b = s.seg.b ? s.seg.b.name : "ujung B (tanpa aset)";
  if (fromSel)
    fromSel.innerHTML = `<option value="A">${escapeHtml(a)} (ujung A)</option><option value="B">${escapeHtml(b)} (ujung B)</option>`;
  if (fromSel && s.from) fromSel.value = s.from;
  const f = fromSel ? fromSel.value : "A";
  info.innerHTML = `Diukur dari <b>${escapeHtml(f === "A" ? a : b)}</b> ke <b>${escapeHtml(f === "A" ? b : a)}</b> &middot; ${fmtM(s.seg.length_m)} &middot; ${escapeHtml(s.seg.capacity || "")}`;
}
function saveOtdrManual() {
  const s = otdrSeg();
  if (!s.seg)
    return setDataMsg(
      "om-msg",
      "Pilih kabel/segmen di langkah 1 terlebih dahulu.",
      "err",
    );
  const n = (id) => {
    const v = ($id(id) || {}).value;
    return v === "" || v == null ? null : Number(v);
  };
  const core = (($id("om-core") || {}).value || "").trim();
  if (!core) return setDataMsg("om-msg", "Isi nomor core.", "err");
  const evD = n("om-ev-d"),
    evL = n("om-ev-l");
  const body = {
    cable_id: s.seg.id,
    from_node: s.from,
    core,
    wavelength_nm: n("om-wl"),
    measured_at: ($id("om-date") || {}).value || null,
    length_m: n("om-len"),
    total_loss_db: n("om-loss"),
    events:
      evD != null || evL != null
        ? [
            {
              distance_m: evD,
              loss_db: evL,
              type: ($id("om-ev-t") || {}).value || "",
            },
          ]
        : [],
    on_duplicate: ($id("otdr-dup") || {}).value || "skip",
  };
  setDataMsg("om-msg", "Menyimpan...", "info");
  return apiRequest("/api/otdr/manual", "POST", body)
    .then((r) => {
      const it = r.item || {};
      setDataMsg(
        "om-msg",
        `${r.message}. ${it.from_name ? it.from_name + " → " + (it.to_name || "?") + ": " : ""}ukur ${fmtDb(it.total_loss_db)} dB, hitung ${fmtDb(it.expected_db)} dB, ${OTDR_LABEL[it.status] || it.status || ""}`,
        it.status === "BAD" ? "warn" : "ok",
      );
      OTDR.userOff = false;
      loadOtdrList();
      loadOtdrLayer(true);
    })
    .catch((err) =>
      setDataMsg("om-msg", "Gagal menyimpan: " + err.message, "err"),
    );
}
// Locator titik putus: jarak OTDR -> koordinat di peta + aset di sekitarnya
let cutGroup = null;
function clearCutMarker() {
  if (cutGroup) cutGroup.clearLayers();
  const r = $id("loc-result");
  if (r) r.innerHTML = "";
}
function locateCut() {
  const s = otdrSeg();
  if (!s.seg)
    return setDataMsg(
      "loc-msg",
      "Pilih kabel/segmen di langkah 1 terlebih dahulu.",
      "err",
    );
  const dist = ($id("loc-dist") || {}).value;
  if (dist === "" || dist == null)
    return setDataMsg("loc-msg", "Isi jarak dari ujung ukur (m).", "err");
  const len = ($id("loc-len") || {}).value;
  setDataMsg("loc-msg", "Mencari titik...", "info");
  const qs =
    `cable_id=${Number(s.seg.id)}&from_end=${encodeURIComponent(s.from)}&distance_m=${encodeURIComponent(dist)}` +
    (len ? `&otdr_length_m=${encodeURIComponent(len)}` : "");
  return apiRequest(`/api/otdr/locate?${qs}`)
    .then((d) => {
      setDataMsg("loc-msg", "", "");
      if (!cutGroup) cutGroup = L.layerGroup().addTo(map);
      cutGroup.clearLayers();
      const mk = L.circleMarker([d.lat, d.lng], {
        radius: 11,
        color: "#fff",
        weight: 3,
        fillColor: "#dc2626",
        fillOpacity: 1,
      });
      const lab = (a) =>
        a ? `${escapeHtml(a.name)} (${fmtM(Math.abs(a.gap_m))})` : "&ndash;";
      const html = `<b>Titik ${fmtM(d.distance_m)} dari ${escapeHtml(d.from_name || "ujung " + d.from_end)}</b><br>Kabel ${escapeHtml(d.cable_name)} &rarr; ${escapeHtml(d.to_name || "?")}<br>Setelah: ${lab(d.before)}<br>Sebelum: ${lab(d.after)}`;
      mk.bindPopup(html);
      cutGroup.addLayer(mk);
      map.setView([d.lat, d.lng], 18);
      mk.openPopup();
      $id("loc-result").innerHTML =
        `<div class="loc-card"><div><b>${d.lat.toFixed(6)}, ${d.lng.toFixed(6)}</b> <button type="button" class="tp-ghost" onclick="copyText('${d.lat.toFixed(6)}, ${d.lng.toFixed(6)}')">Salin koordinat</button></div>
        <div class="cov-meta">${html}</div>
        ${(d.warnings || []).map((w) => `<div class="pr-suggest">${escapeHtml(w)}</div>`).join("")}</div>`;
      closeDataModal();
    })
    .catch((err) => setDataMsg("loc-msg", err.message, "err"));
}
function copyText(t) {
  try {
    navigator.clipboard.writeText(t);
  } catch (_) {
    /* tidak tersedia */
  }
}
function previewOtdr() {
  if (!OTDR.b64) return;
  const seq = ++OTDR.seq;
  $id("otdr-commit").disabled = true;
  setDataMsg("otdr-msg", "Membaca & mengevaluasi berkas...", "info");
  $id("otdr-preview").innerHTML = "";
  return apiRequest("/api/otdr/preview", "POST", otdrBody())
    .then((d) => {
      if (seq !== OTDR.seq) return;
      OTDR.preview = d;
      setDataMsg("otdr-msg", "", "");
      renderOtdrPreview(d);
    })
    .catch((err) => {
      if (seq !== OTDR.seq) return;
      OTDR.preview = null;
      setDataMsg("otdr-msg", err.message, "err");
    });
}
function renderOtdrPreview(d) {
  const sc = d.status_counts || {};
  const rows = (d.items || [])
    .map(
      (
        i,
      ) => `<tr class="${i.duplicate ? "imp-skip" : ""}"><td><b>${escapeHtml(i.cable_name)}</b>${i.from_name ? `<br><small>${escapeHtml(i.from_name)} &rarr; ${escapeHtml(i.to_name || "?")}</small>` : ""}<br><small>${escapeHtml(i.core)} &middot; ${Number(i.wavelength_nm)} nm &middot; ${escapeHtml(i.measured_at)}</small></td>
    <td class="num">${fmtDb(i.total_loss_db)}</td><td class="num">${fmtDb(i.expected_db)}</td><td class="num">${fmtDb(i.delta_db, true)}</td>
    <td>${otdrBadge(i.status)}${i.duplicate ? " <small>(sudah ada)</small>" : ""}</td>
    <td class="imp-notes">${(i.reasons || []).map(escapeHtml).join("; ") || "&ndash;"}${i.events ? `<br><small>${Number(i.events)} event</small>` : ""}</td></tr>`,
    )
    .join("");
  const errs = (d.errors || [])
    .map((e) => `<li>Baris ${Number(e.row)}: ${escapeHtml(e.message)}</li>`)
    .join("");
  $id("otdr-preview").innerHTML = `
    <div class="imp-sum">
      <div class="imp-box create"><b>${Number(sc.OK || 0)}</b><span>normal</span></div>
      <div class="imp-box update"><b>${Number(sc.WARN || 0)}</b><span>perlu dicek</span></div>
      <div class="imp-box error"><b>${Number(sc.BAD || 0)}</b><span>bermasalah</span></div>
      <div class="imp-box skip"><b>${Number(d.error_count || 0)}</b><span>baris bergalat</span></div>
    </div>
    <div class="imp-scroll"><table class="imp-table"><thead><tr><th>Kabel / core</th><th>Ukur (dB)</th><th>Hitung (dB)</th><th>Selisih</th><th>Status</th><th>Catatan</th></tr></thead><tbody>${rows}</tbody></table></div>
    ${errs ? `<ul class="pr-warn">${errs}</ul>` : ""}
    ${d.duplicates ? `<div class="imp-meta">${Number(d.duplicates)} data sudah ada (lihat pilihan lewati/timpa).</div>` : ""}`;
  $id("otdr-commit").disabled = !(d.count > 0);
}
function resetOtdr() {
  OTDR.name = "";
  OTDR.b64 = "";
  OTDR.preview = null;
  OTDR.seq++;
  $id("otdr-file").value = "";
  $id("otdr-file-name").textContent = "Pilih berkas hasil OTDR (Excel / CSV)";
  $id("otdr-preview").innerHTML = "";
  $id("otdr-commit").disabled = true;
  setDataMsg("otdr-msg", "", "");
}
function commitOtdr() {
  const d = OTDR.preview;
  if (!d) return;
  if (
    !confirm(
      `Simpan ${d.count} hasil OTDR dari "${OTDR.name}"?\nHasil akan tampil di peta sebagai layer "Hasil OTDR (ukur)".`,
    )
  )
    return;
  $id("otdr-commit").disabled = true;
  setDataMsg("otdr-msg", "Menyimpan...", "info");
  return apiRequest("/api/otdr/commit", "POST", otdrBody())
    .then((r) => {
      resetOtdr();
      setDataMsg("otdr-msg", r.message, "ok");
      OTDR.userOff = false;
      loadOtdrList();
      loadOtdrLayer(true);
    })
    .catch((err) => {
      setDataMsg("otdr-msg", "Gagal menyimpan: " + err.message, "err");
      $id("otdr-commit").disabled = false;
    });
}
function downloadOtdrTemplate(fmt) {
  return downloadFromApi(
    `/api/otdr/template?format=${encodeURIComponent(fmt || "xlsx")}`,
  ).catch((err) => setDataMsg("otdr-msg", err.message, "err"));
}
function loadOtdrList() {
  return apiRequest("/api/otdr?limit=30")
    .then((list) => {
      const el = $id("otdr-list");
      if (!el) return;
      el.innerHTML = list.length
        ? `<table class="imp-table"><thead><tr><th>Kabel / core</th><th>Tanggal</th><th>Ukur</th><th>Selisih</th><th>Status</th><th></th></tr></thead><tbody>` +
          list
            .map(
              (
                r,
              ) => `<tr><td><b>${escapeHtml(r.cable_name || "#" + r.cable_id)}</b>${r.from_name ? `<br><small>${escapeHtml(r.from_name)} &rarr; ${escapeHtml(r.to_name || "?")}</small>` : ""}<br><small>${escapeHtml(r.core)} &middot; ${Number(r.wavelength_nm)} nm</small></td>
            <td>${escapeHtml(r.measured_at)}</td><td class="num">${fmtDb(r.total_loss_db)}</td><td class="num">${fmtDb(r.delta_db, true)}</td><td>${otdrBadge(r.status)}</td>
            <td>${can("otdr.upload") ? `<button type="button" class="danger" onclick="deleteOtdr(${Number(r.id)})"><i class="fa-solid fa-trash"></i></button>` : ""}</td></tr>`,
            )
            .join("") +
          `</tbody></table>`
        : '<span class="tp-muted">Belum ada hasil OTDR.</span>';
    })
    .catch(() => {});
}
function deleteOtdr(id) {
  if (!confirm("Hapus hasil OTDR ini?")) return;
  apiRequest(`/api/otdr/${id}`, "DELETE")
    .then(() => {
      loadOtdrList();
      loadOtdrLayer();
    })
    .catch((err) => alert(err.message));
}

// Layer peta OTDR
function otdrPopupHtml(p) {
  if (p.kind === "OTDR_EVENT") {
    return `<b>${escapeHtml(p.name)}</b> &middot; ${escapeHtml(p.core)}<br>Event ${escapeHtml(p.event_type)} di ${fmtM(p.distance_m)}: <b>${fmtDb(p.loss_db)} dB</b> ${otdrBadge(p.status)}<br><small>Ukur ${escapeHtml(p.date || "")}</small>`;
  }
  return `<b>${escapeHtml(p.name)}</b> ${otdrBadge(p.status)}<br>${Number(p.cores_measured)}/${Number(p.cores_total)} core terukur &middot; normal ${Number(p.ok)}, cek ${Number(p.warn)}, bermasalah ${Number(p.bad)}<br>
    Core terburuk: ${escapeHtml(p.worst_core)} (selisih ${fmtDb(p.worst_delta_db, true)} dB, maks ${fmtDb(p.max_loss_db)} dB)<br><small>Ukur terakhir ${escapeHtml(p.last_date || "")}</small>`;
}
function renderOtdrLayer(gj) {
  otdrGroup.clearLayers();
  (gj.features || []).forEach((f) => {
    const p = f.properties || {};
    const col = OTDR_COLOR[p.status] || "#14b8a6";
    let lyr;
    if (p.kind === "OTDR_CABLE") {
      const pts = f.geometry.coordinates.map((c) => [c[1], c[0]]);
      lyr = L.polyline(pts, {
        color: col,
        weight: 7,
        opacity: 0.8,
        dashArray: "2 9",
        lineCap: "round",
      });
    } else {
      const c = f.geometry.coordinates;
      lyr = L.circleMarker([c[1], c[0]], {
        radius: 7,
        color: "#fff",
        weight: 2,
        fillColor: col,
        fillOpacity: 1,
      });
    }
    lyr.bindPopup(otdrPopupHtml(p));
    otdrGroup.addLayer(lyr);
  });
}
function loadOtdrLayer(forceShow) {
  return apiRequest("/api/otdr/map" + scopeQS("?"))
    .then((gj) => {
      OTDR.summary = gj.summary || null;
      renderOtdrLayer(gj);
      const has = gj.summary && gj.summary.cables > 0;
      // Tampil otomatis saat ada data, kecuali pengguna sudah mematikannya lewat kontrol layer
      if (has && (forceShow || !OTDR.userOff) && !map.hasLayer(otdrGroup))
        map.addLayer(otdrGroup);
      updateOtdrLegend();
    })
    .catch(() => {});
}
function updateOtdrLegend() {
  const s = OTDR.summary;
  const show = s && s.cables > 0 && map.hasLayer(otdrGroup);
  if (!OTDR.legend) {
    OTDR.legend = L.control({ position: "bottomleft" });
    OTDR.legend.onAdd = () => {
      const d = L.DomUtil.create("div", "otdr-legend");
      d.id = "otdr-legend";
      return d;
    };
    OTDR.legend.addTo(map);
  }
  const el =
    document.getElementById("otdr-legend") ||
    (OTDR.legend.getContainer && OTDR.legend.getContainer());
  if (!el) return;
  el.style.display = show ? "" : "none";
  if (show) {
    el.innerHTML = `<b>Hasil OTDR</b> <small>${s.cables} kabel &middot; ${s.events} titik event</small>
      <div><i style="background:${OTDR_COLOR.OK}"></i> Normal <i style="background:${OTDR_COLOR.WARN}"></i> Cek <i style="background:${OTDR_COLOR.BAD}"></i> Bermasalah</div>`;
  }
}
map.on("overlayadd", (e) => {
  if (e.layer === otdrGroup) {
    OTDR.userOff = false;
    updateOtdrLegend();
  }
});
map.on("overlayremove", (e) => {
  if (e.layer === otdrGroup) {
    OTDR.userOff = true;
    updateOtdrLegend();
  }
});

// Parameter redaman (Admin)
const LOSS_FIELDS = [
  ["fiber_db_km", "Serat (dB/km)"],
  ["splice_db", "Splice (dB)"],
  ["connector_db", "Konektor (dB)"],
  ["tx_dbm", "Daya kirim OLT (dBm)"],
  ["rx_min_dbm", "Sensitivitas ONT (dBm)"],
  ["margin_db", "Margin cadangan (dB)"],
  ["otdr_warn_db", "OTDR: selisih cek (dB)"],
  ["otdr_bad_db", "OTDR: selisih bermasalah (dB)"],
  ["event_warn_db", "Event cek (dB)"],
  ["event_bad_db", "Event bermasalah (dB)"],
  ["otdr_slack_m", "SOR: slack per titik (m)"],
  ["otdr_normal_pct", "SOR: toleransi ujung normal (%)"],
  ["otdr_helix_pct", "SOR: helix serat (%)"],
  ["otdr_route_err_pct", "SOR: galat rute peta (%)"],
  ["mm_fiber_db_km_850", "Multimode 850 nm (dB/km)"],
  ["mm_fiber_db_km_1300", "Multimode 1300 nm (dB/km)"],
  ["mm_tx_dbm", "Daya kirim MM (dBm)"],
  ["mm_rx_min_dbm", "Sensitivitas MM (dBm)"],
];
function loadLossParams() {
  return apiRequest("/api/loss/params")
    .then((d) => {
      const p = d.params;
      $id("loss-fields").innerHTML =
        LOSS_FIELDS.map(
          ([k, l]) =>
            `<label>${escapeHtml(l)}<input type="number" step="0.01" data-loss="${k}" value="${Number(p[k])}" /></label>`,
        ).join("") +
        `<label class="data-wide">Redaman splitter (dB) &mdash; format rasio:nilai, pisah koma<input type="text" id="loss-splitters" value="${escapeHtml(
          Object.entries(p.splitter_db)
            .map(([k, v]) => k + "=" + v)
            .join(", "),
        )}" /></label>`;
    })
    .catch(() => {});
}
function saveLossParams() {
  const params = {};
  document.querySelectorAll("#loss-fields [data-loss]").forEach((i) => {
    params[i.getAttribute("data-loss")] = i.value;
  });
  const sp = {};
  ($id("loss-splitters").value || "").split(",").forEach((t) => {
    const m = t.trim().match(/^(1:\d+)\s*=\s*([\d.]+)$/);
    if (m) sp[m[1]] = m[2];
  });
  params.splitter_db = sp;
  apiRequest("/api/loss/params", "PUT", { params })
    .then((r) => {
      setDataMsg("loss-msg", r.message || "Parameter tersimpan.", "ok");
      loadLossParams();
    })
    .catch((err) =>
      setDataMsg("loss-msg", "Gagal menyimpan: " + err.message, "err"),
    );
}

// ---------- SEMBUNYIKAN INSIDEN TANPA DAMPAK ----------
function setIncidentMapVisibility(id, hidden) {
  return apiRequest(`/api/incidents/${id}/map-visibility`, "PUT", {
    hidden: !!hidden,
  })
    .then(() => {
      try {
        map.closePopup();
      } catch (_) {}
      loadData();
      const noc = $id("modal-noc-monitor");
      if (noc && noc.style.display === "flex") fetchNocIncidentsLog();
    })
    .catch((err) => alert(err.message));
}
function bulkIncidentVisibility(hidden) {
  const q = hidden
    ? "Sembunyikan SEMUA penanda insiden yang tidak mengubah status aset dari peta?\n(Insiden yang berdampak pada aset tetap tampil. Bisa ditampilkan kembali kapan saja.)"
    : "Tampilkan kembali semua penanda insiden yang disembunyikan?";
  if (!confirm(q)) return;
  return apiRequest("/api/incidents/map-visibility", "POST", {
    hidden: !!hidden,
  })
    .then((r) => {
      alert(
        r.changed
          ? `${r.changed} penanda ${hidden ? "disembunyikan" : "ditampilkan kembali"}.`
          : "Tidak ada penanda yang perlu diubah.",
      );
      loadData();
      fetchNocIncidentsLog();
    })
    .catch((err) => alert(err.message));
}

// =====================================================================================
// ROUND 10 - Filter wilayah global (Cluster + Area)
// =====================================================================================
const SCOPE_KEY = "netgis_scope";
const SCOPE = { region: "ALL", cluster: "ALL", area: "ALL" };
const SCOPE_OPTS = { regions: [], clusters: [], areas: [], pairs: [] };
let SCOPE_FIT = false; // true = peta difokuskan ke hasil filter setelah data berikutnya dimuat
let scopeOptsToken = 0;

function restoreScope() {
  try {
    const j = JSON.parse(localStorage.getItem(SCOPE_KEY) || "null");
    if (j && typeof j === "object") {
      SCOPE.region = String(j.region || "ALL");
      SCOPE.cluster = String(j.cluster || "ALL");
      SCOPE.area = String(j.area || "ALL");
      SCOPE_FIT = scopeActive();
    }
  } catch (_) {
    /* penyimpanan tidak tersedia */
  }
}
restoreScope();

function scopeActive() {
  return (
    SCOPE.region !== "ALL" || SCOPE.cluster !== "ALL" || SCOPE.area !== "ALL"
  );
}
function scopeQS(prefix) {
  const p = [];
  if (SCOPE.region !== "ALL")
    p.push("region=" + encodeURIComponent(SCOPE.region));
  if (SCOPE.cluster !== "ALL")
    p.push("cluster=" + encodeURIComponent(SCOPE.cluster));
  if (SCOPE.area !== "ALL") p.push("area=" + encodeURIComponent(SCOPE.area));
  return p.length ? (prefix || "?") + p.join("&") : "";
}
function scopeLabel() {
  if (!scopeActive()) return "Semua wilayah";
  const rg =
    SCOPE.region !== "ALL"
      ? (SCOPE_OPTS.regions.find((r) => r.id === SCOPE.region) || {}).value ||
        SCOPE.region
      : null;
  return [
    rg,
    SCOPE.cluster !== "ALL" ? SCOPE.cluster : rg ? null : "Semua cluster",
    SCOPE.area !== "ALL" ? SCOPE.area : null,
  ]
    .filter(Boolean)
    .join(" › ");
}
function sameCI(a, b) {
  return String(a || "").toUpperCase() === String(b || "").toUpperCase();
}

// Area yang tersedia untuk sebuah cluster (berdasarkan data), lengkap dengan jumlah aset
function inRegion(regionId, item) {
  // item = {region, registered}; regionId = nama regional atau "__NONE__"
  if (!regionId || regionId === "ALL") return true;
  if (regionId === "__NONE__") return item.registered === false;
  return sameCI(item.region, regionId);
}
function scopeClusterOptions(region) {
  return SCOPE_OPTS.clusters
    .filter((c) => inRegion(region, c))
    .map((c) => ({ value: c.value, assets: c.assets }));
}
function scopeRegionHtml(selected, allLabel) {
  const list = SCOPE_OPTS.regions;
  const has = selected === "ALL" || list.some((r) => r.id === selected);
  const extra = !has ? [{ id: selected, value: selected, assets: 0 }] : [];
  return (
    `<option value="ALL">${allLabel}</option>` +
    list
      .concat(extra)
      .map(
        (r) =>
          `<option value="${escapeHtml(r.id)}">${escapeHtml(r.value)}${r.assets != null ? ` (${Number(r.assets)})` : ""}</option>`,
      )
      .join("")
  );
}
function scopeAreaOptions(cluster, region) {
  const m = new Map();
  SCOPE_OPTS.pairs.forEach((p) => {
    if (cluster !== "ALL" && !sameCI(p.cluster, cluster)) return;
    if (cluster === "ALL" && !inRegion(region, p)) return;
    const k = String(p.area).toUpperCase();
    const cur = m.get(k) || { value: p.area, assets: 0, incidents: 0 };
    cur.assets += Number(p.assets || 0);
    cur.incidents += Number(p.incidents || 0);
    m.set(k, cur);
  });
  return [...m.values()].sort((a, b) => a.value.localeCompare(b.value));
}
function scopeOptHtml(list, selected, allLabel) {
  const has = list.some((o) => sameCI(o.value, selected));
  const extra =
    selected !== "ALL" && !has ? [{ value: selected, assets: 0 }] : []; // pilihan tersimpan yang datanya sudah tidak ada
  return (
    `<option value="ALL">${allLabel}</option>` +
    list
      .concat(extra)
      .map(
        (o) =>
          `<option value="${escapeHtml(o.value)}">${escapeHtml(o.value)}${o.assets != null ? ` (${Number(o.assets)})` : ""}</option>`,
      )
      .join("")
  );
}
function matchOpt(list, v) {
  if (v === "ALL") return "ALL";
  const f = list.find((o) => sameCI(o.value, v));
  return f ? f.value : v;
}

// Sinkronkan semua kontrol (strip di atas peta + toolbar Inventory) dengan SCOPE
function syncScopeControls() {
  const clusters = scopeClusterOptions(SCOPE.region);
  const areas = scopeAreaOptions(SCOPE.cluster, SCOPE.region);
  const rHtml = scopeRegionHtml(SCOPE.region, "Semua regional");
  const cHtml = scopeOptHtml(clusters, SCOPE.cluster, "Semua cluster");
  const aHtml = scopeOptHtml(areas, SCOPE.area, "Semua area");
  const cVal = matchOpt(clusters, SCOPE.cluster),
    aVal = matchOpt(areas, SCOPE.area);
  [
    ["scope-region", rHtml, SCOPE.region],
    [
      "filter-region",
      rHtml.replace("Semua regional", "Semua Regional"),
      SCOPE.region,
    ],
    ["scope-cluster", cHtml, cVal],
    ["scope-area", aHtml, aVal],
    ["filter-cluster", cHtml.replace("Semua cluster", "Semua Cluster"), cVal],
    ["filter-area", aHtml.replace("Semua area", "Semua Area"), aVal],
  ].forEach(([id, html, val]) => {
    const el = $id(id);
    if (!el) return;
    el.innerHTML = html;
    el.value = val;
  });
  const strip = $id("scope-strip");
  if (strip && strip.classList) strip.classList.toggle("active", scopeActive());
  const rb = $id("scope-reset");
  if (rb) rb.style.display = scopeActive() ? "" : "none";
  showNocScope();
  updateScopeCount();
}
function updateScopeCount() {
  const el = $id("scope-count");
  if (!el) return;
  const d = lastSummary;
  if (!d) {
    el.textContent = "";
    return;
  }
  const n = Number(d.total_nodes || 0),
    c = Number(d.total_cables || 0);
  el.textContent =
    scopeActive() && n + c === 0
      ? "Tidak ada aset di wilayah ini"
      : `${n} node · ${c} kabel · ${Number(d.tickets_open || 0)} tiket aktif`;
}
function showNocScope() {
  const el = $id("noc-scope");
  if (!el) return;
  el.style.display = scopeActive() ? "" : "none";
  el.textContent = scopeActive() ? "Filter wilayah: " + scopeLabel() : "";
}

function setScope(cluster, area, opts) {
  opts = opts || {};
  let region = opts.region !== undefined ? opts.region || "ALL" : SCOPE.region;
  cluster = matchOpt(SCOPE_OPTS.clusters, cluster || "ALL"); // pakai penulisan resmi dari data
  // cluster di luar regional terpilih: regional dilepas (mis. klik baris ringkasan wilayah lain)
  if (region !== "ALL" && cluster !== "ALL") {
    const co = SCOPE_OPTS.clusters.find((c) => sameCI(c.value, cluster));
    if (co && !inRegion(region, co)) region = "ALL";
  }
  area = matchOpt(SCOPE_OPTS.areas, area || "ALL");
  // area yang bukan milik cluster terpilih dibuang agar hasil tidak kosong tanpa sebab
  if (
    cluster !== "ALL" &&
    area !== "ALL" &&
    SCOPE_OPTS.pairs.length &&
    !SCOPE_OPTS.pairs.some(
      (p) => sameCI(p.cluster, cluster) && sameCI(p.area, area),
    )
  )
    area = "ALL";
  const changed =
    region !== SCOPE.region ||
    !sameCI(cluster, SCOPE.cluster) ||
    !sameCI(area, SCOPE.area);
  SCOPE.region = region;
  SCOPE.cluster = cluster;
  SCOPE.area = area;
  try {
    localStorage.setItem(SCOPE_KEY, JSON.stringify(SCOPE));
  } catch (_) {
    /* tanpa penyimpanan */
  }
  syncScopeControls();
  if (!changed && !opts.force) return;
  inventoryState.page = 1;
  SCOPE_FIT = opts.fit !== false;
  loadData(); // juga memuat ulang ringkasan, Inventory (bila terbuka) dan opsi filter
  const noc = $id("modal-noc-monitor");
  if (noc && noc.style.display !== "none") fetchNocIncidentsLog();
}
function onScopeSelect(which) {
  if (which === "region")
    return setScope("ALL", "ALL", { region: $id("scope-region").value });
  const c = $id("scope-cluster").value,
    a = $id("scope-area").value;
  if (which === "cluster") setScope(c, "ALL");
  else setScope(SCOPE.cluster, a);
}
function onInventoryScopeChange() {
  const rEl = $id("filter-region");
  if (rEl && rEl.value !== SCOPE.region)
    return setScope("ALL", "ALL", { region: rEl.value });
  const c = $id("filter-cluster").value;
  // mengganti cluster di toolbar Inventory mengosongkan area; mengganti area mempertahankan cluster
  const area = $id("filter-area").value;
  setScope(c, c !== SCOPE.cluster ? "ALL" : area);
}
function resetScope() {
  setScope("ALL", "ALL", { region: "ALL" });
}

function loadScopeOptions() {
  const token = ++scopeOptsToken;
  return apiRequest("/api/filters/options")
    .then((d) => {
      if (token !== scopeOptsToken) return;
      SCOPE_OPTS.regions = d.regions || [];
      SCOPE_OPTS.clusters = d.clusters || [];
      SCOPE_OPTS.areas = d.areas || [];
      SCOPE_OPTS.pairs = d.pairs || [];
      // pilihan tersimpan yang datanya sudah tidak ada -> kembali ke semua
      const rOk =
        SCOPE.region === "ALL" ||
        SCOPE_OPTS.regions.some((r) => r.id === SCOPE.region);
      const cOk =
        SCOPE.cluster === "ALL" ||
        SCOPE_OPTS.clusters.some((c) => sameCI(c.value, SCOPE.cluster));
      const aOk =
        SCOPE.area === "ALL" ||
        SCOPE_OPTS.areas.some((a) => sameCI(a.value, SCOPE.area));
      if (!rOk || !cOk || !aOk)
        setScope(cOk ? SCOPE.cluster : "ALL", aOk ? SCOPE.area : "ALL", {
          fit: false,
          region: rOk ? SCOPE.region : "ALL",
        });
      else syncScopeControls();
      fillExportScopeSelects();
    })
    .catch((err) => console.error("Gagal memuat opsi filter:", err));
}
function refreshScopeOptions() {
  return loadScopeOptions();
}

// Setelah filter berganti, arahkan peta ke aset hasil filter
function fitScopeIfRequested(nodes, cables) {
  if (!SCOPE_FIT) return;
  SCOPE_FIT = false;
  const pts = [];
  ((nodes && nodes.features) || []).forEach((f) => {
    const c = f.geometry && f.geometry.coordinates;
    if (c) pts.push([c[1], c[0]]);
  });
  ((cables && cables.features) || []).forEach((f) => {
    ((f.geometry && f.geometry.coordinates) || []).forEach((c) =>
      pts.push([c[1], c[0]]),
    );
  });
  if (!pts.length) return;
  try {
    map.fitBounds(pts, { padding: [60, 60], maxZoom: 17 });
  } catch (_) {
    /* peta belum siap */
  }
}

// ---- Ringkasan: banner filter + rincian per Cluster/Area ----
function renderSummaryIfOpen() {
  const m = $id("modal-summary");
  if (m && m.style.display !== "none" && lastSummary)
    renderSummaryModal(lastSummary);
}
function renderScopeBreakdown(rows) {
  const box = $id("summary-breakdown"),
    ban = $id("summary-scope");
  if (ban) {
    ban.style.display = scopeActive() ? "flex" : "none";
    ban.innerHTML = scopeActive()
      ? `<span><i class="fa-solid fa-filter"></i> Ringkasan di bawah hanya untuk <b>${escapeHtml(scopeLabel())}</b></span><button type="button" onclick="resetScope()">Tampilkan semua wilayah</button>`
      : "";
  }
  if (!box) return;
  rows = rows || [];
  if (!rows.length) {
    box.innerHTML = "";
    return;
  }
  const tot = rows.reduce(
    (a, r) => ({
      nodes: a.nodes + r.nodes,
      odp: a.odp + r.odp,
      cables: a.cables + r.cables,
      length_m: a.length_m + r.length_m,
      broken: a.broken + r.broken,
      incidents_open: a.incidents_open + r.incidents_open,
    }),
    { nodes: 0, odp: 0, cables: 0, length_m: 0, broken: 0, incidents_open: 0 },
  );
  const act = (r) =>
    (SCOPE.cluster === "ALL" || sameCI(SCOPE.cluster, r.cluster)) &&
    (SCOPE.area === "ALL" || sameCI(SCOPE.area, r.area)) &&
    scopeActive();
  const body = rows
    .map(
      (
        r,
      ) => `<tr class="${act(r) ? "active" : ""}" onclick="setScope('${escapeHtml(r.cluster).replace(/'/g, "&#39;")}', '${escapeHtml(r.area).replace(/'/g, "&#39;")}')" title="Filter ke ${escapeHtml(r.cluster)} › ${escapeHtml(r.area)}">
      <td><b>${escapeHtml(r.cluster)}</b></td><td>${escapeHtml(r.area)}</td><td>${r.nodes}</td><td>${r.odp}</td><td>${r.cables}</td>
      <td>${fmtLen(r.length_m)}</td><td class="${r.broken ? "bad" : ""}">${r.broken}</td><td class="${r.incidents_open ? "bad" : ""}">${r.incidents_open}</td></tr>`,
    )
    .join("");
  box.innerHTML = `<div class="sum-breakdown"><h5><i class="fa-solid fa-table-cells"></i> Sebaran per Cluster &amp; Area <small>klik baris untuk memfilter</small></h5>
    <div class="sum-bd-scroll"><table><thead><tr><th>Cluster</th><th>Area</th><th>Node</th><th>ODP</th><th>Kabel</th><th>Panjang</th><th>Rusak</th><th>Tiket aktif</th></tr></thead>
    <tbody>${body}</tbody><tfoot><tr><td colspan="2">Total</td><td>${tot.nodes}</td><td>${tot.odp}</td><td>${tot.cables}</td><td>${fmtLen(tot.length_m)}</td><td>${tot.broken}</td><td>${tot.incidents_open}</td></tr></tfoot></table></div></div>`;
}

// ---- Ekspor: pilihan Cluster/Area ----
function fillExportScopeSelects(cluster, area, region) {
  const cEl = $id("exp-cluster"),
    aEl = $id("exp-area"),
    rEl = $id("exp-region");
  if (!cEl || !aEl) return;
  const curR = region != null ? region : (rEl && rEl.value) || "ALL";
  if (rEl) {
    rEl.innerHTML = scopeRegionHtml(curR, "Semua regional");
    rEl.value = curR;
  }
  const curC = cluster != null ? cluster : cEl.value || "ALL";
  const curA = area != null ? area : aEl.value || "ALL";
  const clusters = scopeClusterOptions(curR);
  cEl.innerHTML = scopeOptHtml(clusters, curC, "Semua cluster");
  cEl.value = matchOpt(clusters, curC);
  const areas = scopeAreaOptions(cEl.value, curR);
  aEl.innerHTML = scopeOptHtml(areas, curA, "Semua area");
  aEl.value = matchOpt(areas, curA);
  fillExportFolderSelect();
}
function fillExportFolderSelect(want) {
  const fEl = $id("exp-folder");
  if (!fEl) return;
  const cur = want != null ? want : fEl.value || "";
  const paths = (FT.list || []).map((f) => f.path).filter(Boolean);
  fEl.innerHTML =
    '<option value="">Semua folder</option>' +
    paths
      .map((p) => `<option value="${escapeHtml(p)}">${escapeHtml(p)}</option>`)
      .join("");
  fEl.value = paths.includes(cur) ? cur : "";
}
function onExportClusterChange() {
  fillExportScopeSelects(
    $id("exp-cluster").value,
    "ALL",
    ($id("exp-region") || {}).value || "ALL",
  );
}
function onExportRegionChange() {
  fillExportScopeSelects("ALL", "ALL", $id("exp-region").value);
}

// =====================================================================================
// WILAYAH: Regional > Cluster > Area (semua pengguna boleh melihat; hanya admin yang mengubah)
// =====================================================================================
const WIL = { tree: null, open: new Set(), q: "", dlg: null };
function wilCanEdit() {
  return can("wilayah.edit");
}
function wilMsg(text, kind) {
  setDataMsg("wil-msg", text, kind || "ok");
}
function openWilayahModal() {
  $id("modal-wilayah").style.display = "flex";
  $id("wil-tools").style.display = wilCanEdit() ? "" : "none";
  WIL.dlg = null;
  wilRenderDlg();
  return wilLoad();
}
function closeWilayahModal() {
  $id("modal-wilayah").style.display = "none";
}
function wilLoad() {
  return apiRequest("/api/wilayah")
    .then((d) => {
      WIL.tree = d;
      if (!WIL.open.size) d.regions.forEach((g) => WIL.open.add("r" + g.id));
      wilRender();
    })
    .catch((err) => wilMsg(err.message, "err"));
}
function wilAfterChange(msg) {
  if (msg) wilMsg(msg, "ok");
  WIL.dlg = null;
  wilRenderDlg();
  return Promise.all([wilLoad(), refreshScopeOptions(), loadData()]);
}
function wilToggle(key) {
  if (WIL.open.has(key)) WIL.open.delete(key);
  else WIL.open.add(key);
  wilRender();
}
function wilFmt(n) {
  return Number(n || 0).toLocaleString("id-ID");
}
function wilRender() {
  const box = $id("wil-tree");
  if (!box) return;
  const d = WIL.tree;
  if (!d) {
    box.innerHTML = "";
    return;
  }
  const q = WIL.q.trim().toUpperCase();
  const ed = wilCanEdit();
  const btn = (act, ico, title, cls) =>
    `<button type="button" class="wil-b${cls ? " " + cls : ""}" title="${title}" onclick="event.stopPropagation();${act}"><i class="fa-solid fa-${ico}"></i></button>`;
  const cnt = (o) =>
    `<span class="wil-cnt">${wilFmt(o.assets)} aset${o.incidents ? ` · ${wilFmt(o.incidents)} tiket` : ""}</span>`;
  let html = "",
    shown = 0;
  d.regions.forEach((g) => {
    const gHit = !q || g.name.toUpperCase().includes(q);
    let inner = "";
    g.clusters.forEach((c) => {
      const cHit = gHit || c.name.toUpperCase().includes(q);
      const areas = c.areas.filter(
        (a) => cHit || a.name.toUpperCase().includes(q),
      );
      if (!cHit && !areas.length) return;
      const ckey = "c" + c.id,
        copen = q ? true : WIL.open.has(ckey);
      let ar = "";
      if (copen)
        ar = areas
          .map(
            (
              a,
            ) => `<div class="wil-row wil-a" data-k="a${a.id}"><span class="wil-car"></span><i class="fa-solid fa-location-dot wil-ico"></i><span class="wil-name">${escapeHtml(a.name)}</span>${cnt(a)}
        ${ed ? `<span class="wil-acts">${btn(`wilAsk('rename','area',${a.id})`, "pen", "Ubah nama")}${btn(`wilMoveDlg('area',${a.id})`, "right-left", "Pindah ke cluster lain")}${btn(`wilDelete('area',${a.id})`, "trash", "Hapus", "bad")}</span>` : ""}</div>`,
          )
          .join("");
      shown++;
      inner += `<div class="wil-row wil-c" data-k="c${c.id}" onclick="wilToggle('${ckey}')"><span class="wil-car${copen ? " o" : ""}"><i class="fa-solid fa-chevron-right"></i></span><i class="fa-solid fa-layer-group wil-ico"></i><span class="wil-name">${escapeHtml(c.name)}</span><small class="wil-sub">${c.areas.length} area</small>${cnt(c)}
        ${ed ? `<span class="wil-acts">${btn(`wilAsk('add','area',${c.id})`, "plus", "Tambah area")}${btn(`wilAsk('rename','cluster',${c.id})`, "pen", "Ubah nama")}${btn(`wilMoveDlg('cluster',${c.id})`, "right-left", "Pindah ke regional lain")}${btn(`wilDelete('cluster',${c.id})`, "trash", "Hapus", "bad")}</span>` : ""}</div>${ar}`;
    });
    if (q && !gHit && !inner) return;
    const gkey = "r" + g.id,
      gopen = q ? true : WIL.open.has(gkey);
    html += `<div class="wil-row wil-g" data-k="r${g.id}" onclick="wilToggle('${gkey}')"><span class="wil-car${gopen ? " o" : ""}"><i class="fa-solid fa-chevron-right"></i></span><i class="fa-solid fa-earth-asia wil-ico"></i><span class="wil-name">${escapeHtml(g.name)}</span><small class="wil-sub">${g.clusters.length} cluster</small>${cnt(g)}
      ${ed ? `<span class="wil-acts">${btn(`wilAsk('add','cluster',${g.id})`, "plus", "Tambah cluster")}${btn(`wilAsk('rename','region',${g.id})`, "pen", "Ubah nama")}${btn(`wilDelete('region',${g.id})`, "trash", "Hapus", "bad")}</span>` : ""}</div>${gopen ? inner : ""}`;
  });
  if (d.unregistered && d.unregistered.length) {
    html += `<div class="wil-unreg"><b><i class="fa-solid fa-triangle-exclamation"></i> Belum terdaftar (${wilFmt(d.unregistered_assets)} aset)</b>
      <p>Aset berikut memakai cluster/area yang tidak ada di daftar. Tetap tampil di peta, tetapi sebaiknya admin menambahkannya atau memindahkan asetnya.</p>
      <ul>${d.unregistered.map((u) => `<li><b>${escapeHtml(u.cluster)}</b> › ${escapeHtml(u.area)} <span class="wil-cnt">${wilFmt(u.assets)} aset</span></li>`).join("")}</ul></div>`;
  }
  box.innerHTML =
    html || '<div class="wil-empty">Tidak ada wilayah yang cocok.</div>';
  const tot = d.regions.reduce((a, g) => a + g.clusters.length, 0);
  const tar = d.regions.reduce(
    (a, g) => a + g.clusters.reduce((x, c) => x + c.areas.length, 0),
    0,
  );
  $id("wil-summary").textContent =
    `${d.regions.length} regional · ${tot} cluster · ${tar} area`;
}
function wilSearch() {
  WIL.q = $id("wil-q").value || "";
  wilRender();
}
function wilExpandAll(open) {
  if (!WIL.tree) return;
  WIL.open.clear();
  if (open)
    WIL.tree.regions.forEach((g) => {
      WIL.open.add("r" + g.id);
      g.clusters.forEach((c) => WIL.open.add("c" + c.id));
    });
  wilRender();
}
// ---- dialog kecil di dalam modal ----
function wilFind(level, id) {
  const T = WIL.tree;
  for (const g of T.regions) {
    if (level === "region" && g.id === id) return { o: g };
    for (const c of g.clusters) {
      if (level === "cluster" && c.id === id) return { o: c, parent: g };
      for (const a of c.areas)
        if (level === "area" && a.id === id)
          return { o: a, parent: c, grand: g };
    }
  }
  return null;
}
function wilRenderDlg() {
  const el = $id("wil-dlg");
  if (!el) return;
  const d = WIL.dlg;
  if (!d) {
    el.style.display = "none";
    el.innerHTML = "";
    return;
  }
  el.style.display = "";
  el.innerHTML = d.html;
  const f = el.querySelector("input,select,textarea");
  if (f && f.focus) f.focus();
}
const WIL_LBL = { region: "Regional", cluster: "Cluster", area: "Area" };
function wilAsk(mode, level, id) {
  if (!wilCanEdit()) return;
  const rn = mode === "rename";
  const cur = rn ? wilFind(level, id) : null;
  const title = rn
    ? `Ubah nama ${WIL_LBL[level].toLowerCase()}`
    : `Tambah ${WIL_LBL[level].toLowerCase()}`;
  let note = "";
  if (rn && level !== "region")
    note = `<small>Semua aset, tiket, dan folder bawaan yang memakai nama ini ikut diganti.</small>`;
  if (!rn && level === "area") {
    const p = wilFind("cluster", id);
    note = `<small>Area baru masuk ke cluster <b>${escapeHtml(p.o.name)}</b>.</small>`;
  }
  if (!rn && level === "cluster") {
    const p = wilFind("region", id);
    note = `<small>Cluster baru masuk ke regional <b>${escapeHtml(p.o.name)}</b>.</small>`;
  }
  WIL.dlg = {
    html: `<div class="wil-dlg-box"><b>${title}</b><input type="text" id="wil-in" maxlength="60" value="${rn ? escapeHtml(cur.o.name) : ""}" placeholder="Nama ${WIL_LBL[level].toLowerCase()}" onkeydown="if(event.key==='Enter')wilAskOk('${mode}','${level}',${id});if(event.key==='Escape')wilDlgClose()" />${note}
    <div class="wil-dlg-btns"><button type="button" class="data-primary" onclick="wilAskOk('${mode}','${level}',${id})">Simpan</button><button type="button" onclick="wilDlgClose()">Batal</button></div></div>`,
  };
  wilRenderDlg();
}
function wilDlgClose() {
  WIL.dlg = null;
  wilRenderDlg();
}
function wilAskOk(mode, level, id) {
  const name = ($id("wil-in").value || "").trim();
  if (!name) return wilMsg("Nama wajib diisi.", "err");
  const call =
    mode === "rename"
      ? apiRequest(`/api/wilayah/${level}/${id}`, "PUT", { name })
      : apiRequest("/api/wilayah", "POST", {
          level: level === "region" ? "region" : level,
          name,
          parent_id: id,
        });
  return call
    .then((r) => wilAfterChange(r.message))
    .catch((err) => wilMsg(err.message, "err"));
}
function wilParents(level, curParentId) {
  const T = WIL.tree;
  if (level === "cluster")
    return T.regions.map((g) => ({ id: g.id, label: g.name }));
  return T.regions.flatMap((g) =>
    g.clusters.map((c) => ({ id: c.id, label: `${c.name}  (${g.name})` })),
  );
}
function wilMoveDlg(level, id) {
  const f = wilFind(level, id);
  const opts = wilParents(level).filter((p) => p.id !== f.parent.id);
  if (!opts.length)
    return wilMsg(
      `Tidak ada ${level === "area" ? "cluster" : "regional"} lain sebagai tujuan.`,
      "err",
    );
  WIL.dlg = {
    html: `<div class="wil-dlg-box"><b>Pindahkan ${WIL_LBL[level].toLowerCase()} ${escapeHtml(f.o.name)}</b>
    <label>Ke ${level === "area" ? "cluster" : "regional"}:<select id="wil-tgt">${opts.map((p) => `<option value="${p.id}">${escapeHtml(p.label)}</option>`).join("")}</select></label>
    ${level === "area" ? `<small>Cluster pada semua aset &amp; tiket di area ini ikut diganti.</small>` : `<small>Semua area dan asetnya ikut pindah regional.</small>`}
    <div class="wil-dlg-btns"><button type="button" class="data-primary" onclick="wilMoveOk('${level}',${id})">Pindahkan</button><button type="button" onclick="wilDlgClose()">Batal</button></div></div>`,
  };
  wilRenderDlg();
}
function wilMoveOk(level, id) {
  const pid = Number($id("wil-tgt").value);
  return apiRequest(`/api/wilayah/${level}/${id}`, "PUT", { parent_id: pid })
    .then((r) => wilAfterChange(r.message))
    .catch((err) => wilMsg(err.message, "err"));
}
function wilDelete(level, id) {
  if (!wilCanEdit()) return;
  const f = wilFind(level, id);
  if (level === "area" && (f.o.assets || f.o.incidents)) {
    // masih dipakai: admin wajib menunjuk area tujuan
    const opts = wilParents("area").length
      ? WIL.tree.regions.flatMap((g) =>
          g.clusters.flatMap((c) =>
            c.areas
              .filter((a) => a.id !== id)
              .map((a) => ({ id: a.id, label: `${a.name} — ${c.name}` })),
          ),
        )
      : [];
    if (!opts.length)
      return wilMsg(
        "Tidak ada area lain sebagai tujuan pemindahan aset.",
        "err",
      );
    WIL.dlg = {
      html: `<div class="wil-dlg-box"><b>Hapus area ${escapeHtml(f.o.name)}</b>
      <p>Area ini masih dipakai <b>${wilFmt(f.o.assets)}</b> aset dan <b>${wilFmt(f.o.incidents)}</b> tiket. Pindahkan ke area lain sebelum dihapus:</p>
      <label>Area tujuan:<select id="wil-tgt">${opts.map((p) => `<option value="${p.id}">${escapeHtml(p.label)}</option>`).join("")}</select></label>
      <div class="wil-dlg-btns"><button type="button" class="data-primary bad" onclick="wilDeleteOk('area',${id},true)">Pindahkan &amp; hapus</button><button type="button" onclick="wilDlgClose()">Batal</button></div></div>`,
    };
    return wilRenderDlg();
  }
  if (!confirm(`Hapus ${WIL_LBL[level].toLowerCase()} "${f.o.name}"?`)) return;
  return wilDeleteOk(level, id, false);
}
function wilDeleteOk(level, id, withMove) {
  const qs = withMove ? "?move_to=" + Number($id("wil-tgt").value) : "";
  return apiRequest(`/api/wilayah/${level}/${id}${qs}`, "DELETE")
    .then((r) => wilAfterChange(r.message))
    .catch((err) => wilMsg(err.message, "err"));
}
function wilAddRegion() {
  wilAsk("add", "region", 0);
}
function wilPasteDlg() {
  WIL.dlg = {
    html: `<div class="wil-dlg-box wil-paste"><b>Tempel daftar wilayah</b>
    <small>Satu baris per area, format <code>Regional &gt; Cluster &gt; Area</code> (boleh disalin dari Excel: kolom dipisah tab). Yang sudah ada dipakai ulang; cluster/area yang ada di induk lain akan dipindah.</small>
    <textarea id="wil-paste" rows="9" spellcheck="false" placeholder="REGIONAL 1 &gt; EKO &gt; BANJARMASIN&#10;REGIONAL 1 &gt; EKO &gt; SAMARINDA"></textarea>
    <div id="wil-paste-res" class="wil-paste-res"></div>
    <div class="wil-dlg-btns"><button type="button" onclick="wilPasteGo(true)">Pratinjau</button><button type="button" class="data-primary" onclick="wilPasteGo(false)">Simpan</button><button type="button" onclick="wilDlgClose()">Tutup</button></div></div>`,
  };
  wilRenderDlg();
}
function wilPasteGo(dry) {
  const text = $id("wil-paste").value || "";
  if (!text.trim()) return wilMsg("Tempel daftar dulu.", "err");
  return apiRequest("/api/wilayah/bulk", "POST", { text, dry_run: dry })
    .then((r) => {
      const errs = (r.errors || [])
        .slice(0, 8)
        .map(
          (e) =>
            `<li>Baris ${e.line}: ${escapeHtml(e.message)} <small>${escapeHtml(e.text)}</small></li>`,
        )
        .join("");
      const res = $id("wil-paste-res");
      if (res)
        res.innerHTML = `<b>${escapeHtml(r.message)}</b>${r.assets_updated ? `<div>${wilFmt(r.assets_updated)} aset ikut disesuaikan.</div>` : ""}${errs ? `<ul>${errs}</ul>` : ""}`;
      if (!dry) return wilAfterChange(r.message);
    })
    .catch((err) => wilMsg(err.message, "err"));
}

// Hanya diperlukan agar tombol Reset/strip tampil benar saat data wilayah tersimpan dipulihkan
syncScopeControls();

initSidebarState();
bootAuth();

// ---------- UI/UX: tema Dark/Light & animasi berkedip ----------
const UIPREF = { theme: "light", blink: true };
function uiPrefGet(k) {
  try {
    return localStorage.getItem(k);
  } catch (_) {
    return null;
  }
}
function uiPrefSet(k, v) {
  try {
    localStorage.setItem(k, v);
  } catch (_) {
    /* penyimpanan tidak tersedia */
  }
}
function applyUiPrefs() {
  const root = document.documentElement || document.body;
  if (root && root.setAttribute) {
    root.setAttribute("data-theme", UIPREF.theme);
    root.setAttribute("data-blink", UIPREF.blink ? "on" : "off");
  }
  const tb = document.getElementById("theme-toggle");
  if (tb) {
    tb.title =
      UIPREF.theme === "dark" ? "Ganti ke tema terang" : "Ganti ke tema gelap";
    tb.setAttribute("aria-pressed", UIPREF.theme === "dark" ? "true" : "false");
    tb.innerHTML =
      UIPREF.theme === "dark"
        ? '<i class="fa-solid fa-sun"></i><span>Terang</span>'
        : '<i class="fa-solid fa-moon"></i><span>Gelap</span>';
  }
  const bb = document.getElementById("blink-toggle");
  if (bb) {
    bb.title = UIPREF.blink
      ? "Matikan animasi berkedip"
      : "Nyalakan animasi berkedip";
    bb.setAttribute("aria-pressed", UIPREF.blink ? "true" : "false");
    bb.classList.toggle("off", !UIPREF.blink);
  }
}
function toggleTheme() {
  UIPREF.theme = UIPREF.theme === "dark" ? "light" : "dark";
  uiPrefSet("netgis-theme", UIPREF.theme);
  applyUiPrefs();
}
function toggleBlink() {
  UIPREF.blink = !UIPREF.blink;
  uiPrefSet("netgis-blink", UIPREF.blink ? "on" : "off");
  applyUiPrefs();
}
(function initUiPrefs() {
  const t = uiPrefGet("netgis-theme");
  if (t === "dark" || t === "light") UIPREF.theme = t;
  else if (
    typeof window !== "undefined" &&
    window.matchMedia &&
    window.matchMedia("(prefers-color-scheme: dark)").matches
  )
    UIPREF.theme = "dark";
  UIPREF.blink = uiPrefGet("netgis-blink") !== "off";
  applyUiPrefs();
})();

// =====================================================================
// MODE "HUBUNGKAN ASET" (multi titik): klik aset awal, lalu aset-aset berikutnya (urut hulu -> hilir).
// Tiap pasangan = 1 kabel yang rute-nya mengikuti jalan (bisa diedit). Core dipilih sistem (bisa diganti),
// "Sambung penuh" memakai semua core sesuai kapasitas. Simpan = kabel + sambungan sekaligus.
// =====================================================================
const CONNECT_TYPES = ["POP", "CLOSURE", "SLACK", "ODP", "PELANGGAN"];
const CONNECT_CABLE_TYPES = [
  ["Feeder", "Feeder"],
  ["Distribution", "Distribusi"],
  ["Backbone", "Backbone"],
  ["Drop", "Dropcore"],
];
const CONNECT_MANUAL_MAX = 12;
const CONNECT_CAPS = ["2C", "4C", "8C", "12C", "24C", "48C", "96C", "144C"];
let connectLayer = L.layerGroup().addTo(map);

function connectBanner(html) {
  const b = document.getElementById("connect-banner");
  if (!b) return;
  b.innerHTML = html || "";
  b.style.display = html ? "block" : "none";
}
function toggleConnect() {
  if (CONNECT.active) connectCancel();
  else connectStart();
}
function connectStart() {
  if (!can("asset.write")) return;
  try {
    map.closePopup();
  } catch (_) {}
  clearSearchMarker();
  connectReset(true);
  CONNECT.active = true;
  CONNECT.step = "start";
  const b = document.getElementById("connect-btn");
  if (b) b.classList.add("on");
  connectBanner(
    '<i class="fa-solid fa-link"></i> <b>Hubungkan aset</b> &mdash; arahkan kursor ke aset <b>AWAL</b> lalu klik. <small>(Esc untuk batal)</small>',
  );
  try {
    map.getContainer().classList.add("connect-mode");
  } catch (_) {}
}
function connectReset(keepActive) {
  connectLayer.clearLayers();
  CONNECT.points = [];
  CONNECT.segs = [];
  CONNECT.segCfg = [];
  CONNECT.collapsed = false;
  CONNECT.hover = null;
  CONNECT.opts = null;
  CONNECT.reverse = false;
  CONNECT.busy = false;
  CONNECT.feedCable = null;
  // kompatibilitas: alias untuk kabel tunggal
  CONNECT.start = CONNECT.end = CONNECT.line = null;
  CONNECT.handles = [];
  CONNECT.coords = [];
  CONNECT.source = "";
  const p = document.getElementById("connect-panel");
  if (p) {
    p.style.display = "none";
    p.innerHTML = "";
  }
  if (!keepActive) {
    CONNECT.active = false;
    CONNECT.step = null;
    connectBanner("");
    const b = document.getElementById("connect-btn");
    if (b) b.classList.remove("on");
    try {
      map.getContainer().classList.remove("connect-mode");
    } catch (_) {}
  }
}
function connectCancel() {
  connectReset(false);
}

function connectEligible(marker) {
  const t =
    marker &&
    marker.metaData &&
    String(marker.metaData.type || "").toUpperCase();
  return !!t && CONNECT_TYPES.indexOf(t) >= 0;
}
function connectIconLatLng(marker) {
  try {
    const r = marker._icon.getBoundingClientRect(),
      c = map.getContainer().getBoundingClientRect();
    if (r && r.width)
      return map.containerPointToLatLng([
        r.left + r.width / 2 - c.left,
        r.top + r.height / 2 - c.top,
      ]);
  } catch (_) {
    /* tanpa DOM ikon */
  }
  return marker.getLatLng();
}
function connectRing(latlng, color, label, perm) {
  const ring = L.circleMarker(latlng, {
    radius: 22,
    color,
    weight: 3,
    fill: false,
    dashArray: perm ? null : "4 4",
    className: "connect-ring" + (perm ? " fixed" : ""),
  });
  if (label)
    ring.bindTooltip(label, {
      permanent: !!perm,
      direction: "top",
      offset: [0, -16],
      className: "connect-tip",
    });
  return ring;
}
function connectInPicking() {
  return (
    CONNECT.active &&
    !CONNECT.busy &&
    (CONNECT.step === "start" || CONNECT.step === "next")
  );
}
function connectHover(marker) {
  if (!connectInPicking()) return;
  if (CONNECT.hover) {
    connectLayer.removeLayer(CONNECT.hover);
    CONNECT.hover = null;
  }
  if (!marker || !connectEligible(marker)) return;
  const id = Number(marker.metaData.id);
  if (CONNECT.points.some((p) => p.id === id)) return; // sudah dipilih
  const isStart = CONNECT.step === "start";
  CONNECT.hover = connectRing(
    connectIconLatLng(marker),
    isStart ? "#16a34a" : "#dc2626",
    (isStart ? "Titik awal: " : "Titik berikut: ") + marker.metaData.name,
    false,
  );
  connectLayer.addLayer(CONNECT.hover);
  if (CONNECT.hover.openTooltip)
    try {
      CONNECT.hover.openTooltip();
    } catch (_) {}
}
function connectPick(marker) {
  if (!connectInPicking()) return;
  if (!connectEligible(marker)) {
    connectBanner(
      '<i class="fa-solid fa-triangle-exclamation"></i> Hanya POP, Closure, Slack, ODP, dan Pelanggan yang bisa dihubungkan. Pilih aset lain, atau Esc.',
    );
    return;
  }
  const p = marker.metaData,
    ll = marker.getLatLng();
  const info = {
    id: Number(p.id),
    name: p.name,
    type: String(p.type).toUpperCase(),
    lat: ll.lat,
    lng: ll.lng,
    cluster: p.cluster,
    area: p.area,
    city: p.city,
  };
  if (CONNECT.points.some((x) => x.id === info.id)) return;
  if (CONNECT.hover) {
    connectLayer.removeLayer(CONNECT.hover);
    CONNECT.hover = null;
  }
  const first = CONNECT.points.length === 0;
  CONNECT.points.push(info);
  connectLayer.addLayer(
    connectRing(
      connectIconLatLng(marker),
      first ? "#16a34a" : "#0ea5e9",
      (first ? "Awal: " : CONNECT.points.length + ": ") + info.name,
      true,
    ),
  );
  if (first) {
    CONNECT.start = info;
    CONNECT.step = "next";
    connectBanner(
      '<i class="fa-solid fa-link"></i> Awal: <b>' +
        escapeHtml(info.name) +
        "</b>. Klik aset <b>berikutnya</b>. <small>(Esc untuk batal)</small>",
    );
    return;
  }
  CONNECT.end = info;
  CONNECT.step = "route";
  CONNECT.busy = true;
  connectBanner(
    '<i class="fa-solid fa-spinner fa-spin"></i> Mencari rute mengikuti jalan...',
  );
  return connectFetchSeg(CONNECT.points.length - 2);
}
// segmen i = kabel dari points[i] ke points[i+1]
function connectFetchSeg(i) {
  const s = CONNECT.points[i],
    e = CONNECT.points[i + 1];
  return apiRequest("/api/route", "POST", {
    points: [
      [s.lat, s.lng],
      [e.lat, e.lng],
    ],
  })
    .then((r) => ({
      coords: r.coords.map((c) => [c[0], c[1]]),
      source: r.source || "",
      note: r.note || "",
    }))
    .catch(() => ({
      coords: [
        [s.lng, s.lat],
        [e.lng, e.lat],
      ],
      source: "straight",
      note: "Rute jalan tidak tersedia; garis lurus",
    }))
    .then((r) => {
      const seg =
        CONNECT.segs[i] ||
        (CONNECT.segs[i] = {
          coords: [],
          line: null,
          handles: [],
          source: "",
          note: "",
        });
      seg.coords = r.coords;
      seg.source = r.source;
      seg.note = r.note;
      connectDrawSeg(i, true);
      CONNECT.busy = false;
      return connectAfterRoute();
    });
}
function connectAfterRoute() {
  CONNECT.step = "next";
  const sg = CONNECT.segs[0];
  CONNECT.coords = sg.coords;
  CONNECT.line = sg.line;
  CONNECT.handles = sg.handles;
  CONNECT.source = sg.source; // alias kabel pertama
  const single = CONNECT.segs.length === 1;
  const p = single ? connectLoadOptions() : Promise.resolve();
  return p.then(() => {
    connectBanner(
      '<i class="fa-solid fa-link"></i> ' +
        CONNECT.segs.length +
        " kabel siap. Klik aset <b>berikutnya</b> untuk menambah titik, atau simpan. Seret titik biru = ubah jalur, klik garis = tambah titik, dobel-klik titik = hapus.",
    );
    connectRenderPanel();
  });
}
// Kurangi titik agar bisa diedit dengan nyaman (maks ~120 handle), ujung tetap
function connectDecimate(coords, max) {
  if (coords.length <= max) return coords.slice();
  const out = [coords[0]],
    step = (coords.length - 1) / (max - 1);
  for (let i = 1; i < max - 1; i++) out.push(coords[Math.round(i * step)]);
  out.push(coords[coords.length - 1]);
  return out;
}
function connectSegLengthM(seg) {
  let t = 0;
  const c = seg.coords;
  for (let i = 0; i < c.length - 1; i++)
    t += map.distance([c[i][1], c[i][0]], [c[i + 1][1], c[i + 1][0]]);
  return t;
}
function connectDrawSeg(i, fit) {
  const seg = CONNECT.segs[i];
  if (seg.line) connectLayer.removeLayer(seg.line);
  seg.handles.forEach((h) => connectLayer.removeLayer(h));
  seg.handles = [];
  seg.coords = connectDecimate(seg.coords, 120);
  const s = CONNECT.points[i],
    e = CONNECT.points[i + 1];
  seg.coords[0] = [s.lng, s.lat];
  seg.coords[seg.coords.length - 1] = [e.lng, e.lat];
  const ll = () => seg.coords.map((c) => [c[1], c[0]]);
  seg.line = L.polyline(ll(), {
    color: "#0ea5e9",
    weight: 5,
    opacity: 0.9,
    dashArray: "8 6",
    className: "connect-line",
  });
  connectLayer.addLayer(seg.line);
  seg.line.on("click", (ev) => {
    if (ev && ev.originalEvent && ev.originalEvent.stopPropagation)
      ev.originalEvent.stopPropagation();
    connectInsertVertex(i, ev.latlng);
  });
  seg.coords.forEach((c, k) => {
    if (k === 0 || k === seg.coords.length - 1) return;
    const h = L.marker([c[1], c[0]], {
      draggable: true,
      icon: L.divIcon({ className: "connect-handle", iconSize: [12, 12] }),
      zIndexOffset: 500,
    });
    h._ci = k;
    h.on("drag", (ev) => {
      const p = ev.target.getLatLng();
      seg.coords[ev.target._ci] = [p.lng, p.lat];
      seg.line.setLatLngs(ll());
    });
    h.on("dragend", () => connectUpdateInfo());
    h.on("dblclick", (ev) => {
      if (ev && ev.originalEvent && ev.originalEvent.stopPropagation)
        ev.originalEvent.stopPropagation();
      connectRemoveVertex(i, h._ci);
    });
    seg.handles.push(h);
    connectLayer.addLayer(h);
  });
  if (i === 0) {
    CONNECT.coords = seg.coords;
    CONNECT.line = seg.line;
    CONNECT.handles = seg.handles;
  }
  if (fit) {
    try {
      const b = L.latLngBounds
        ? L.latLngBounds(CONNECT.points.map((p) => [p.lat, p.lng]))
        : seg.line.getBounds();
      map.fitBounds(b, {
        paddingTopLeft: [60, 100],
        paddingBottomRight: [400, 80],
      });
    } catch (_) {}
  }
}
function connectInsertVertex(i, latlng) {
  const c = CONNECT.segs[i].coords;
  let best = 0,
    bd = Infinity;
  for (let k = 0; k < c.length - 1; k++) {
    const mid = [(c[k][1] + c[k + 1][1]) / 2, (c[k][0] + c[k + 1][0]) / 2];
    const d = map.distance(latlng, mid);
    if (d < bd) {
      bd = d;
      best = k;
    }
  }
  c.splice(best + 1, 0, [latlng.lng, latlng.lat]);
  connectDrawSeg(i);
  connectUpdateInfo();
}
function connectRemoveVertex(i, k) {
  const seg = CONNECT.segs[i];
  if (seg.coords.length <= 2 || k <= 0 || k >= seg.coords.length - 1) return;
  seg.coords.splice(k, 1);
  connectDrawSeg(i);
  connectUpdateInfo();
}
function connectUseRoad() {
  CONNECT.busy = true;
  connectBanner(
    '<i class="fa-solid fa-spinner fa-spin"></i> Mencari rute mengikuti jalan...',
  );
  return Promise.all(CONNECT.segs.map((_, i) => connectFetchSegQuiet(i))).then(
    () => {
      CONNECT.busy = false;
      return connectAfterRoute();
    },
  );
}
function connectFetchSegQuiet(i) {
  const s = CONNECT.points[i],
    e = CONNECT.points[i + 1];
  return apiRequest("/api/route", "POST", {
    points: [
      [s.lat, s.lng],
      [e.lat, e.lng],
    ],
  })
    .then((r) => {
      CONNECT.segs[i].coords = r.coords.map((c) => [c[0], c[1]]);
      CONNECT.segs[i].source = r.source || "";
      CONNECT.segs[i].note = r.note || "";
    })
    .catch(() => {
      CONNECT.segs[i].coords = [
        [s.lng, s.lat],
        [e.lng, e.lat],
      ];
      CONNECT.segs[i].source = "straight";
    })
    .then(() => connectDrawSeg(i));
}
function connectStraight() {
  CONNECT.segs.forEach((seg, i) => {
    const s = CONNECT.points[i],
      e = CONNECT.points[i + 1];
    seg.coords = [
      [s.lng, s.lat],
      [e.lng, e.lat],
    ];
    seg.source = "straight";
    seg.note = "Garis lurus (manual)";
    connectDrawSeg(i);
  });
  connectRenderPanel();
}
// hapus titik terakhir (kembali satu langkah)
function connectUndoPoint() {
  if (CONNECT.busy) return;
  if (CONNECT.points.length <= 1) return connectCancel();
  const seg = CONNECT.segs.pop();
  if (seg) {
    if (seg.line) connectLayer.removeLayer(seg.line);
    seg.handles.forEach((h) => connectLayer.removeLayer(h));
  }
  CONNECT.points.pop();
  // gambar ulang cincin penanda
  connectLayer.clearLayers();
  CONNECT.hover = null;
  CONNECT.points.forEach((pt, k) => {
    connectLayer.addLayer(
      connectRing(
        [pt.lat, pt.lng],
        k === 0 ? "#16a34a" : "#0ea5e9",
        (k === 0 ? "Awal: " : k + 1 + ": ") + pt.name,
        true,
      ),
    );
  });
  CONNECT.segs.forEach((sg) => {
    connectLayer.addLayer(sg.line);
    sg.handles.forEach((h) => connectLayer.addLayer(h));
  });
  CONNECT.start = CONNECT.points[0];
  CONNECT.end = CONNECT.points[CONNECT.points.length - 1];
  if (CONNECT.points.length === 1) {
    CONNECT.step = "next";
    CONNECT.segs = [];
    CONNECT.opts = null;
    const p = document.getElementById("connect-panel");
    if (p) {
      p.style.display = "none";
      p.innerHTML = "";
    }
    connectBanner(
      '<i class="fa-solid fa-link"></i> Awal: <b>' +
        escapeHtml(CONNECT.points[0].name) +
        "</b>. Klik aset <b>berikutnya</b>. <small>(Esc untuk batal)</small>",
    );
    return;
  }
  return connectAfterRoute();
}
function connectUpdateInfo() {
  const el = document.getElementById("cn-len");
  if (el)
    el.textContent =
      Math.round(CONNECT.segs.reduce((t, sg) => t + connectSegLengthM(sg), 0)) +
      " m";
}

// Jumlah core dipakai: berurutan 1..kapasitas kabel (dropdown sampai 48; di atasnya input angka)
const CONNECT_CORE_SELECT_MAX = 48;
function connectCoreChoices(cap) {
  const total = Math.max(1, parseInt(cap) || 12);
  return Array.from({ length: total }, (_, k) => k + 1);
}
function connectClampCores(n, cap) {
  const total = Math.max(1, parseInt(cap) || 12);
  return Math.min(total, Math.max(1, parseInt(n) || 1));
}
// Kontrol pilih jumlah core: <select> bila kapasitas kecil, <input number> bila besar
function connectCoreControl(id, cap, n, onchange, disabled) {
  const total = Math.max(1, parseInt(cap) || 12);
  const dis = disabled ? " disabled" : "";
  if (total <= CONNECT_CORE_SELECT_MAX) {
    return `<select id="${id}" onchange="${onchange}"${dis}>${connectCoreChoices(
      total,
    )
      .map((c) => `<option${c === n ? " selected" : ""}>${c}</option>`)
      .join("")}</select>`;
  }
  return `<input type="number" id="${id}" min="1" max="${total}" step="1" value="${n}" onchange="${onchange}"${dis} title="1 sampai ${total}">`;
}
function connectCoresNeeded() {
  const n =
    parseInt((document.getElementById("cn-cores") || { value: "1" }).value) ||
    1;
  const capEl = document.getElementById("cn-cap");
  return capEl ? connectClampCores(n, capEl.value) : n;
}
function connectIsFull() {
  const el = document.getElementById("cn-full");
  return !!(el && el.checked);
}
function connectLoadOptions() {
  if (CONNECT.segs.length !== 1) return Promise.resolve();
  const s = CONNECT.points[0],
    e = CONNECT.points[1];
  const cap = (document.getElementById("cn-cap") || { value: "12C" }).value;
  const cores = connectCoresNeeded();
  return apiRequest("/api/connect/options", "POST", {
    from_node_id: s.id,
    to_node_id: e.id,
    cores,
    capacity: cap,
    reverse: CONNECT.reverse,
    full: connectIsFull(),
  })
    .then((o) => {
      CONNECT.opts = o;
    })
    .catch((err) => {
      CONNECT.opts = {
        ok: false,
        reason: err.message,
        from_options: [],
        to_options: [],
        cable_cores: [],
        defaults: { from_ports: [], to_ports: [], via_cores: [] },
        warnings: [],
      };
    });
}
function connectReload() {
  return connectLoadOptions().then(() => connectRenderPanel());
}
function connectSelOpts(id, options, chosen, n) {
  let html = "";
  for (let i = 0; i < n; i++) {
    html +=
      `<select id="${id}-${i}" class="cn-sel">` +
      options
        .map(
          (o) =>
            `<option value="${escapeHtml(o.port || o)}"${(o.port || o) === chosen[i] ? " selected" : ""}>${escapeHtml(o.label || o.port || o)}</option>`,
        )
        .join("") +
      "</select>";
  }
  return html;
}
function connectSegDefault(i) {
  const a = CONNECT.points[i],
    b = CONNECT.points[i + 1] || a;
  const type =
    a.type === "POP"
      ? "Feeder"
      : b.type === "PELANGGAN"
        ? "Drop"
        : "Distribution";
  return {
    type,
    cap: type === "Feeder" ? "24C" : type === "Drop" ? "2C" : "12C",
    n: 1,
    full: false,
  };
}
function connectSegCfg(i) {
  if (!CONNECT.segCfg) CONNECT.segCfg = [];
  if (!CONNECT.segCfg[i]) CONNECT.segCfg[i] = connectSegDefault(i);
  return CONNECT.segCfg[i];
}
function connectSegSet(i, key, val) {
  const c = connectSegCfg(i);
  c[key] = key === "full" ? !!val : key === "n" ? parseInt(val) || 1 : val;
  connectRenderPanel();
}
function connectSegRows() {
  const pts = CONNECT.points;
  if (CONNECT.segCfg)
    CONNECT.segCfg.length = Math.min(
      CONNECT.segCfg.length,
      CONNECT.segs.length,
    );
  return CONNECT.segs
    .map((sg, i) => {
      const c = connectSegCfg(i);
      const types = CONNECT_CABLE_TYPES.map(
        (t) =>
          `<option value="${t[0]}"${t[0] === c.type ? " selected" : ""}>${t[1]}</option>`,
      ).join("");
      const caps = CONNECT_CAPS.map(
        (x) => `<option${x === c.cap ? " selected" : ""}>${x}</option>`,
      ).join("");
      c.n = connectClampCores(c.n, c.cap);
      const coreCtl = connectCoreControl(
        `cn-s${i}-n`,
        c.cap,
        c.n,
        `connectSegSet(${i},'n',this.value)`,
        c.full,
      );
      return (
        `<div class="cn-seg" id="cn-seg-${i}"><div class="cn-seg-h"><b>${i + 1}.</b> <span class="cn-a">${escapeHtml(pts[i].name)}</span> &rarr; <span class="cn-b">${escapeHtml(pts[i + 1].name)}</span> <small>${Math.round(connectSegLengthM(sg))} m</small></div>` +
        `<div class="cn-seg-f"><label>Jenis<select id="cn-s${i}-type" onchange="connectSegSet(${i},'type',this.value)">${types}</select></label>` +
        `<label>Kapasitas<select id="cn-s${i}-cap" onchange="connectSegSet(${i},'cap',this.value)">${caps}</select></label>` +
        `<label>Core${coreCtl}</label>` +
        `<label class="cn-seg-full"><input type="checkbox" id="cn-s${i}-full" onchange="connectSegSet(${i},'full',this.checked)"${c.full ? " checked" : ""}> Sambung penuh</label></div></div>`
      );
    })
    .join("");
}
function connectRenderPanel() {
  const p = document.getElementById("connect-panel");
  if (!p) return;
  const pts = CONNECT.points,
    multi = CONNECT.segs.length > 1;
  const keep = (id, def) => {
    const el = document.getElementById(id);
    return el ? el.value : def;
  };
  if (
    multi &&
    !(CONNECT.segCfg && CONNECT.segCfg[0]) &&
    document.getElementById("cn-type")
  ) {
    // peralihan dari kabel tunggal: pakai isian yang sudah dipilih untuk segmen pertama
    CONNECT.segCfg = [
      {
        type: keep("cn-type", "Distribution"),
        cap: keep("cn-cap", "12C"),
        n: connectCoresNeeded(),
        full: connectIsFull(),
      },
    ];
  }
  const full = connectIsFull();
  const o = CONNECT.opts || {};
  const up = !multi && CONNECT.reverse ? pts[1] : pts[0],
    dn = !multi && CONNECT.reverse ? pts[0] : pts[1];
  const type = keep(
    "cn-type",
    up.type === "POP"
      ? "Feeder"
      : dn.type === "PELANGGAN"
        ? "Drop"
        : "Distribution",
  );
  const cap = keep("cn-cap", "12C");
  const n = connectCoresNeeded();
  const inst = keep("cn-inst", "Udara");
  const name = keep("cn-name", `KB-${up.name}-${dn.name}`);
  let h =
    '<div class="cn-head"><b><i class="fa-solid fa-link"></i> Hubungkan aset' +
    (multi ? ` (${CONNECT.segs.length} kabel)` : "") +
    `</b><span><button type="button" class="cn-x" id="cn-min" onclick="connectTogglePanel()" title="Ciutkan / buka panel (agar aset di bawah panel bisa diklik)">${CONNECT.collapsed ? "&#9662;" : "&#9652;"}</button><button type="button" class="cn-x" onclick="connectCancel()" title="Batal (Esc)">&times;</button></span></div><div class="cn-body">`;
  if (multi) {
    h += '<div class="cn-segs">' + connectSegRows() + "</div>";
  } else {
    h += `<div class="cn-dir"><span class="cn-a">${escapeHtml(up.name)}</span> <small>(${escapeHtml(up.type)})</small> &rarr; <span class="cn-b">${escapeHtml(dn.name)}</span> <small>(${escapeHtml(dn.type)})</small> <button type="button" class="btn-mini" onclick="connectFlip()"><i class="fa-solid fa-right-left"></i> Balik arah</button></div>`;
  }
  h += '<div class="cn-grid">';
  if (!multi)
    h += `<label>Nama kabel<input id="cn-name" value="${escapeHtml(name)}"></label>`;
  else h += "<label>Nama kabel<small>otomatis: KB-asal-tujuan</small></label>";
  if (!multi)
    h += `<label>Jenis<select id="cn-type">${CONNECT_CABLE_TYPES.map((t) => `<option value="${t[0]}"${t[0] === type ? " selected" : ""}>${t[1]}</option>`).join("")}</select></label>`;
  if (!multi)
    h += `<label>Kapasitas kabel<select id="cn-cap" onchange="connectReload()">${CONNECT_CAPS.map((c) => `<option${c === cap ? " selected" : ""}>${c}</option>`).join("")}</select></label>`;
  if (!multi)
    h += `<label>Jumlah core dipakai${connectCoreControl("cn-cores", cap, n, "connectReload()", full)}</label>`;
  h += `<label>Pemasangan<select id="cn-inst"><option${inst === "Udara" ? " selected" : ""}>Udara</option><option${inst === "Tanah" ? " selected" : ""}>Tanah</option></select></label>`;
  h += `<label>Panjang rute<b id="cn-len">${Math.round(CONNECT.segs.reduce((t, sg) => t + connectSegLengthM(sg), 0))} m</b><small>${escapeHtml(CONNECT.segs[0].note || "")}</small></label>`;
  h += "</div>";
  if (!multi)
    h += `<label class="cn-full"><input type="checkbox" id="cn-full" onchange="connectReload()"${full ? " checked" : ""}><span><b>Sambung penuh</b> &mdash; semua core sesuai kapasitas kabel tersambung otomatis</span></label>`;
  h +=
    '<div class="cn-route-btns"><button type="button" class="btn-mini" onclick="connectUseRoad()"><i class="fa-solid fa-road"></i> Ikuti jalan lagi</button><button type="button" class="btn-mini" onclick="connectStraight()"><i class="fa-solid fa-minus"></i> Garis lurus</button>' +
    '<button type="button" class="btn-mini" onclick="connectUndoPoint()"><i class="fa-solid fa-rotate-left"></i> Hapus titik terakhir</button></div>';
  if (multi) {
    h += `<div class="cn-note">Kabel disimpan berurutan sesuai pengaturan tiap segmen; core dipilih sistem (core terendah yang masih kosong).</div>`;
  } else if (!o.ok) {
    h += `<div class="cn-warn"><i class="fa-solid fa-triangle-exclamation"></i> ${escapeHtml(o.reason || "Tidak bisa menentukan core")}<br><small>Kabel tetap bisa disimpan tanpa sambungan core (manual nanti).</small></div>`;
  } else {
    h += connectPairTable(o, up, dn, full);
  }
  const canLinks = multi ? true : !!o.ok;
  h += `</div><div class="cn-actions"><button type="button" class="btn-ok" onclick="connectSave(${canLinks ? "true" : "false"})"><i class="fa-solid fa-check"></i> ${canLinks ? (multi ? "Simpan semua kabel + sambungan" : "Simpan kabel + sambungan") : "Simpan kabel saja"}</button><button type="button" class="btn-mini" onclick="connectCancel()">Batal</button></div>`;
  p.innerHTML = h;
  p.style.display = "block";
  if (p.classList) p.classList.toggle("collapsed", !!CONNECT.collapsed);
  if (!multi && o.ok && CONNECT.pairRows) connectPairCheck();
}
// ---- Tabel pasangan core: core hulu (pilih sendiri) -> core kabel baru (-> port hilir) ----
CONNECT.feedCable = null;
CONNECT.pairList = [];
CONNECT.pairFeed = new Set();
CONNECT.pairRows = 0;
function connectFromChoices(o) {
  const ready = (o.from_options || []).filter((x) => x.port !== "AUTO");
  const fcs = o.feed_cables || [];
  const fc = fcs.find((c) => c.id === CONNECT.feedCable) || fcs[0] || null;
  CONNECT.feedCable = fc ? fc.id : null;
  const feed = fc
    ? fc.cores.map((l) => ({
        port: l,
        label: `${l} (dari kabel ${fc.name})`,
        state: "feed",
      }))
    : [];
  return { list: ready.concat(feed), feed, fc, fcs };
}
function connectPairTable(o, up, dn, full) {
  const d = o.defaults || {};
  const m = (d.via_cores || []).length;
  const ch = connectFromChoices(o);
  CONNECT.pairList = ch.list.map((x) => x.port);
  CONNECT.pairFeed = new Set(ch.feed.map((x) => x.port));
  CONNECT.pairRows = m;
  let fromDef = (d.from_ports || []).slice();
  if (
    fromDef.length < m ||
    fromDef.some((x) => x === "AUTO" || !CONNECT.pairList.includes(x))
  )
    fromDef = CONNECT.pairList.slice(0, m);
  const viaOpts = o.cable_cores || [];
  const toN = (o.to_options || []).length ? (dn.type === "ODP" ? 1 : m) : 0;
  const short = (t) =>
    String(t).replace(/Tube (\d+) - Core (\d+)/, "T$1 \u00b7 Core $2");
  const opt = (arr, sel) =>
    arr
      .map((x) => {
        const v = x.port || x;
        const lb = x.state === "feed" ? v : x.label || v;
        return `<option value="${escapeHtml(v)}" title="${escapeHtml(x.label || v)}"${v === sel ? " selected" : ""}>${escapeHtml(short(lb))}</option>`;
      })
      .join("");
  let rows = "";
  for (let i = 0; i < m; i++) {
    rows +=
      `<tr><td class="cn-pn">${full ? `<input type="checkbox" id="cn-use-${i}" checked onchange="connectPairCheck()" title="Pakai core ini">` : ""}${i + 1}</td>` +
      `<td><select id="cn-from-${i}" onchange="connectPairCheck()">${opt(ch.list, fromDef[i])}</select></td><td class="cn-arr">&rarr;</td>` +
      `<td><select id="cn-via-${i}" onchange="connectPairCheck()">${opt(viaOpts, (d.via_cores || [])[i])}</select></td>` +
      (toN
        ? `<td>${i < toN ? `<select id="cn-to-${i}" onchange="connectPairCheck()">${opt(o.to_options, (d.to_ports || [])[i])}</select>` : ""}</td>`
        : "") +
      "</tr>";
  }
  const startOpts = ch.list
    .map((x, k) => `<option value="${k}">${escapeHtml(x.port)}</option>`)
    .join("");
  let h = '<div class="cn-cores">';
  h += `<div class="cn-note">${full ? `<b>Sambung penuh:</b> ${m} core bisa disambung. Hilangkan centang pada core yang tidak dipakai. ` : `<b>${m} core</b> akan disambung. `}Atur pasangan <b>core hulu &rarr; core kabel baru</b> pada tiap baris.</div>`;
  h += '<div class="cn-pair-tools">';
  if (ch.fcs.length > 1)
    h += `<label>Kabel hulu <select id="cn-feedcab" onchange="connectFeedCab(this.value)">${ch.fcs.map((c) => `<option value="${Number(c.id)}"${ch.fc && c.id === ch.fc.id ? " selected" : ""}>${escapeHtml(c.name)} (${escapeHtml(c.capacity || "")})</option>`).join("")}</select></label>`;
  else if (ch.fc)
    h += `<span>Kabel hulu: <b>${escapeHtml(ch.fc.name)}</b> (${escapeHtml(ch.fc.capacity || "")})</span>`;
  if (ch.list.length > m)
    h += `<label>Mulai dari core hulu <select id="cn-fromstart" onchange="connectPairStart(this.value)">${startOpts}</select></label>`;
  h +=
    '<button type="button" class="btn-mini" onclick="connectPairSame()" title="Core kabel baru diberi nomor yang sama dengan core hulu bila ada">Samakan nomor</button>';
  h += '<span id="cn-pair-count" class="cn-pair-count"></span></div>';
  h += `<div class="cn-pair"><table><thead><tr><th>#</th><th>Core hulu (${escapeHtml(up.name)})</th><th></th><th>Core kabel baru</th>${toN ? `<th>Port ${escapeHtml(dn.name)}</th>` : ""}</tr></thead><tbody>${rows}</tbody></table></div>`;
  if (!toN)
    h += `<div class="cn-note">Di ${escapeHtml(dn.name)}, joint memakai label core kabel baru (sambungan menerus).</div>`;
  (o.warnings || []).forEach((w) => {
    h += `<div class="cn-note">&bull; ${escapeHtml(w)}</div>`;
  });
  h += "</div>";
  return h;
}
function connectPairCheck() {
  return connectPairCollect();
}
function connectFeedCab(id) {
  CONNECT.feedCable = parseInt(id) || null;
  connectRenderPanel();
}
function connectPairStart(k) {
  const start = parseInt(k) || 0;
  for (let i = 0; i < CONNECT.pairRows; i++) {
    const el = document.getElementById(`cn-from-${i}`),
      v = CONNECT.pairList[start + i];
    if (el && v !== undefined) el.value = v;
  }
  connectPairCheck();
}
function connectPairSame() {
  const num = (s) => (String(s).match(/Core\s+(\d+)/i) || [])[1];
  for (let i = 0; i < CONNECT.pairRows; i++) {
    const f = document.getElementById(`cn-from-${i}`),
      v = document.getElementById(`cn-via-${i}`);
    if (!f || !v) continue;
    const n = num(f.value);
    const hit = n && [...v.options].find((op) => num(op.value) === n);
    if (hit) v.value = hit.value;
  }
  connectPairCheck();
}
// Kumpulkan pasangan terpilih; tandai nilai ganda. Return {from, via, to, n, upstream} atau {error}
function connectPairCollect() {
  const m = CONNECT.pairRows,
    from = [],
    via = [],
    to = [];
  const dupMark = (prefix, vals) => {
    const seen = {};
    vals.forEach((x) => {
      seen[x.v] = (seen[x.v] || 0) + 1;
    });
    let bad = false;
    vals.forEach((x) => {
      const el = document.getElementById(`${prefix}-${x.i}`);
      const dup = seen[x.v] > 1;
      if (el && el.classList) el.classList.toggle("dup", dup);
      if (dup) bad = true;
    });
    return bad;
  };
  const F = [],
    V = [],
    T = [];
  for (let i = 0; i < m; i++) {
    const use = document.getElementById(`cn-use-${i}`);
    if (use && !use.checked) {
      ["from", "via", "to"].forEach((p) => {
        const e = document.getElementById(`cn-${p}-${i}`);
        if (e && e.classList) e.classList.remove("dup");
      });
      continue;
    }
    const f = document.getElementById(`cn-from-${i}`),
      v = document.getElementById(`cn-via-${i}`),
      t = document.getElementById(`cn-to-${i}`);
    if (f) {
      F.push({ i, v: f.value });
      from.push(f.value);
    }
    if (v) {
      V.push({ i, v: v.value });
      via.push(v.value);
    }
    if (t) {
      T.push({ i, v: t.value });
      to.push(t.value);
    }
  }
  const dup = [
    dupMark("cn-from", F),
    dupMark("cn-via", V),
    dupMark("cn-to", T),
  ].some(Boolean);
  const cnt = document.getElementById("cn-pair-count");
  if (cnt)
    cnt.textContent = `Dipakai: ${from.length} dari ${m} core${dup ? " \u00b7 ADA NILAI GANDA" : ""}`;
  const feedUsed = from.some((x) => CONNECT.pairFeed.has(x));
  return {
    from,
    via,
    to,
    n: from.length,
    dup,
    upstream: feedUsed ? CONNECT.feedCable : null,
  };
}
function connectTogglePanel() {
  CONNECT.collapsed = !CONNECT.collapsed;
  const p = document.getElementById("connect-panel");
  if (p && p.classList) p.classList.toggle("collapsed", CONNECT.collapsed);
  const b = document.getElementById("cn-min");
  if (b) b.innerHTML = CONNECT.collapsed ? "&#9662;" : "&#9652;";
}
function connectFlip() {
  CONNECT.reverse = !CONNECT.reverse;
  connectReload();
}
function connectSelValues(prefix, n) {
  const out = [];
  for (let i = 0; i < n; i++) {
    const el = document.getElementById(`${prefix}-${i}`);
    if (el) out.push(el.value);
  }
  return out;
}
// simpan 1 segmen (kabel + sambungan); return Promise<{name, links, note}>
function connectSaveSeg(i, withLinks, common) {
  const pts = CONNECT.points,
    multi = CONNECT.segs.length > 1;
  const rev = !multi && CONNECT.reverse;
  const a = rev ? pts[i + 1] : pts[i],
    b = rev ? pts[i] : pts[i + 1];
  const seg = CONNECT.segs[i];
  const coords = rev ? seg.coords.slice().reverse() : seg.coords.slice();
  const name = multi
    ? `KB-${a.name}-${b.name}`
    : (document.getElementById("cn-name") || {}).value ||
      `KB-${a.name}-${b.name}`;
  const payload = {
    name,
    type: common.type,
    status: "Active",
    coordinates: coords,
    cluster: a.cluster || b.cluster,
    area: a.area || b.area,
    city: a.city || b.city,
    capacity: common.cap,
    installation: common.inst,
    fiber_mode: "SM",
    core_data: "{}",
    from_node_id: a.id,
    to_node_id: b.id,
  };
  return apiRequest("/api/cables", "POST", payload).then((res) => {
    if (!withLinks || !res || !res.id)
      return { name, links: 0, note: "tanpa sambungan core" };
    const req = {
      cores: common.n,
      reverse: false,
      respect_order: true,
      upstream_cable_id: null,
      full: common.full,
    };
    if (!multi && !common.full && common.manual)
      Object.assign(req, common.manual);
    return apiRequest(`/api/cables/${Number(res.id)}/auto-connect`, "POST", {
      ...req,
      preview: true,
    }).then((r) => {
      if (r.ok && !r.needs_choice) {
        return apiRequest(
          `/api/cables/${Number(res.id)}/auto-connect`,
          "POST",
          { ...req, preview: false },
        ).then((r2) => ({
          name,
          links: r2.links.length,
          cores: r2.cores_used ? r2.cores_used.length : common.n,
          note: (r2.warnings || [])[0] || "",
        }));
      }
      return {
        name,
        links: 0,
        pending: { cableId: res.id, req, res: r },
        note: r.reason || "perlu pilihan",
      };
    });
  });
}
function connectSave(withLinks) {
  const o = CONNECT.opts || {};
  const multi = CONNECT.segs.length > 1;
  const inst = document.getElementById("cn-inst").value;
  const common = multi
    ? { inst, manual: null }
    : {
        type: document.getElementById("cn-type").value,
        cap: document.getElementById("cn-cap").value,
        inst,
        n: connectCoresNeeded(),
        full: connectIsFull(),
        manual: null,
      };
  const commonFor = (i) => {
    if (!multi) return common;
    const c = connectSegCfg(i);
    return {
      type: c.type,
      cap: c.cap,
      inst,
      n: c.n,
      full: c.full,
      manual: null,
    };
  };
  if (withLinks && !multi && o.ok && CONNECT.pairRows) {
    const sel = connectPairCollect();
    if (!sel.n) {
      alert("Pilih minimal 1 core untuk disambung.");
      return;
    }
    if (sel.dup) {
      alert(
        "Ada core hulu / core kabel / port yang dipilih lebih dari sekali. Perbaiki baris yang bertanda merah.",
      );
      return;
    }
    common.manual = {
      from_ports: sel.from,
      via_cores: sel.via,
      upstream_cable_id: sel.upstream,
    };
    if (sel.to.length) common.manual.to_ports = sel.to;
    common.n = sel.n;
    common.full = false;
  }
  const total = CONNECT.segs.length,
    done = [];
  CONNECT.busy = true;
  let chain = Promise.resolve();
  CONNECT.segs.forEach((_, i) => {
    chain = chain.then(() => {
      if (done.stop) return;
      return connectSaveSeg(i, withLinks, commonFor(i)).then((r) => {
        done.push(r);
        if (r.pending) done.stop = r;
      });
    });
  });
  return chain
    .then(() => {
      const stop = done.stop;
      const unsaved = total - done.length;
      connectCancel();
      loadData();
      const lines = done.map(
        (r) => `• ${r.name}: ${r.links ? r.links + " sambungan core" : r.note}`,
      );
      if (stop) {
        AC.cableId = stop.pending.cableId;
        AC.name = stop.name;
        AC.req = stop.pending.req;
        AC.res = stop.pending.res;
        alert(
          `${done.length} dari ${total} kabel tersimpan:\n${lines.join("\n")}${unsaved ? `\n${unsaved} kabel berikutnya belum dibuat.` : ""}\nKabel terakhir butuh pilihan; lanjut di jendela berikut.`,
        );
        acRender();
        return;
      }
      alert(`${total} kabel tersimpan:\n${lines.join("\n")}`);
    })
    .catch((err) => {
      CONNECT.busy = false;
      alert(
        "Gagal menyimpan: " +
          err.message +
          (done.length
            ? `\n${done.length} kabel sebelumnya sudah tersimpan.`
            : ""),
      );
      loadData();
    });
}

// =====================================================================
// MODAL BERTINGKAT: Esc / klik area gelap menutup SATU lapis teratas (kembali ke modal di bawahnya),
// modal yang dibuka belakangan selalu tampil paling atas.
// =====================================================================
const MODAL_CLOSERS = {
  "modal-ac": () => acClose(),
  "modal-add-asset": () => closeAddAssetModal(),
  "modal-core-detail": () => closeCoreDetailModal(),
  "modal-add-incident": () => closeAddIncidentModal(),
  "modal-noc-monitor": () => closeNocMonitorModal(),
  "modal-incident-detail": () => closeIncidentDetail(),
  "modal-password": () => closePasswordModal(),
  "modal-khs": () => closeKhsModal(),
  "modal-users": () => closeUsersModal(),
  "modal-wilayah": () => closeWilayahModal(),
  "modal-audit": () => closeAuditModal(),
  "modal-data": () => dismissDataModal(),
  "modal-covbulk": () => closeCovBulkModal(),
  "modal-summary": () => closeSummaryModal(),
  "modal-inventory": () => closeInventoryModal(),
  "modal-topofix": () => closeTopofixModal(),
  "modal-naming": () => closeNamingModal(),
};
let MODAL_SEQ = 0;
function modalVisible(m) {
  return !!m && m.style && m.style.display && m.style.display !== "none";
}
function modalSyncStack(m) {
  if (modalVisible(m)) {
    if (!m._open) {
      m._open = true;
      m._seq = ++MODAL_SEQ;
      try {
        m.style.zIndex = String(2000 + m._seq);
      } catch (_) {}
    }
  } else if (m._open) {
    m._open = false;
    m._seq = 0;
    try {
      m.style.zIndex = "";
    } catch (_) {}
  }
}
function topModal() {
  const all = Array.prototype.slice
    .call(document.querySelectorAll(".modal-overlay") || [])
    .filter(modalVisible);
  if (!all.length) return null;
  return all.reduce((a, b) => ((b._seq || 0) >= (a._seq || 0) ? b : a));
}
function closeTopModal() {
  const m = topModal();
  if (!m) return false;
  if (m.id === "modal-core-detail" && coreNav.length) {
    coreNavBack();
    return true;
  } // di dalam rantai aset: kembali satu langkah
  const fn = MODAL_CLOSERS[m.id];
  if (fn) fn();
  else m.style.display = "none";
  return true;
}
(function initModalStack() {
  try {
    if (typeof MutationObserver === "function") {
      const mo = new MutationObserver((list) =>
        list.forEach((r) => modalSyncStack(r.target)),
      );
      Array.prototype.forEach.call(
        document.querySelectorAll(".modal-overlay") || [],
        (m) => {
          modalSyncStack(m);
          mo.observe(m, { attributes: true, attributeFilter: ["style"] });
        },
      );
    }
  } catch (_) {
    /* tanpa MutationObserver: urutan DOM */
  }
  let downTarget = null;
  document.addEventListener(
    "keydown",
    (e) => {
      if (!e || e.key !== "Escape") return;
      if (
        document.getElementById("ft-ctx") ||
        document.getElementById("inv-colmenu")
      )
        return; // menu kecil terbuka: Esc hanya menutup menu itu
      if (closeTopModal()) {
        if (e.stopPropagation) e.stopPropagation();
        if (e.preventDefault) e.preventDefault();
      }
    },
    true,
  );
  document.addEventListener(
    "mousedown",
    (e) => {
      downTarget = e ? e.target : null;
    },
    true,
  );
  document.addEventListener(
    "click",
    (e) => {
      const t = e && e.target;
      if (
        !t ||
        !t.classList ||
        !t.classList.contains("modal-overlay") ||
        downTarget !== t
      )
        return;
      if (t.id === "modal-password") return;
      closeTopModal();
    },
    true,
  );
})();

// =====================================================================
// GRID TABEL (semua tabel data): filter per kolom, urut, atur lebar kolom (tarik pemisah header,
// klik dua kali = sesuaikan isi), teks penuh / satu baris. Lebar & mode teks diingat per tabel.
// Tabel dengan data bergulir dari server (mis. Asset Inventory) hanya memakai atur lebar + teks penuh.
// =====================================================================
const NG = { hooks: {}, store: null, timer: null, uid: 0 };
function ngStore() {
  if (NG.store) return NG.store;
  try {
    NG.store = JSON.parse(localStorage.getItem("netgis-grid") || "{}") || {};
  } catch (_) {
    NG.store = {};
  }
  return NG.store;
}
function ngPersist() {
  try {
    localStorage.setItem("netgis-grid", JSON.stringify(NG.store || {}));
  } catch (_) {
    /* penyimpanan tidak tersedia */
  }
}
function ngHeads(t) {
  return Array.from(t.tHead.rows[0].cells).filter(
    (c) => !c.classList.contains("ng-skip"),
  );
}
function ngKey(t) {
  return t.dataset && t.dataset.ngKey
    ? "id:" + t.dataset.ngKey
    : t.id
      ? "id:" + t.id
      : "h:" +
        ngHeads(t)
          .map((c) => c.textContent.trim())
          .join("|");
}
function ngEligible(t) {
  if (!t.tHead || t.tHead.rows.length !== 1 || t.tHead.rows[0].cells.length < 2)
    return false;
  if (t.dataset && t.dataset.ng === "off") return false;
  if (t.closest && t.closest(".cn-pair, .ng-off")) return false;
  return true;
}
function ngBtn(label, title, onclick) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "ng-btn";
  b.title = title;
  b.setAttribute("aria-label", title);
  b.textContent = label;
  b.addEventListener("click", onclick);
  return b;
}
function ngEnhance(t) {
  const dsFeat = (t.dataset && t.dataset.ng) || "filter,sort,resize,wrap";
  const feats = new Set(dsFeat.split(","));
  const hook = t.id ? NG.hooks[t.id] : null;
  const body = t.tBodies[0];
  if (!hook && body && body.querySelector("input, select, textarea")) {
    feats.delete("filter");
    feats.delete("sort");
  }
  const heads = Array.from(t.tHead.rows[0].cells);
  const st = {
    feats,
    n: heads.length,
    fv: heads.map(() => ""),
    sortCol: -1,
    sortDir: 1,
    open: false,
    ws: null,
    wrap: false,
    orig: null,
    first: null,
  };
  t._ng = st;
  t.classList.add("ng");
  const cols = Array.from(t.querySelectorAll(":scope > colgroup > col"));
  st.orig = {
    layout: t.style.tableLayout,
    width: t.style.width,
    cols: cols.map((c) => c.style.width),
    ths: heads.map((h) => h.style.width),
  };
  let cs = null;
  try {
    cs = getComputedStyle(t);
  } catch (_) {
    /* tidak tersedia */
  }
  if (cs && cs.tableLayout === "fixed") t.classList.add("ng-fixed");

  // ---- toolbar kecil di atas tabel ----
  const bar = document.createElement("div");
  bar.className = "ng-bar";
  bar._ngTable = t;
  if (feats.has("filter")) {
    st.bFilter = ngBtn("Filter", "Filter per kolom", () => ngToggleFilter(t));
    bar.appendChild(st.bFilter);
  }
  if (feats.has("wrap")) {
    st.bWrap = ngBtn("Teks penuh", "Tampilkan teks penuh / satu baris", () =>
      ngToggleWrap(t),
    );
    bar.appendChild(st.bWrap);
  }
  bar.appendChild(
    ngBtn("Reset", "Reset lebar kolom, filter, dan urutan", () => ngReset(t)),
  );
  let anchor = t;
  const par = t.parentElement;
  if (par) {
    let pcs = null;
    try {
      pcs = getComputedStyle(par);
    } catch (_) {
      /* abaikan */
    }
    if (
      pcs &&
      /(auto|scroll)/.test(pcs.overflowX + pcs.overflowY) &&
      !/modal-body/.test(par.className || "") &&
      (par.children.length === 1 || /scroll|wrap/.test(par.className || ""))
    )
      anchor = par;
  }
  if (anchor.parentNode) anchor.parentNode.insertBefore(bar, anchor);
  st.bar = bar;

  // ---- header: urut + pegangan lebar ----
  heads.forEach((th, i) => {
    th.classList.add("ng-th");
    if (feats.has("sort")) {
      th.classList.add("ng-sortable");
      th.addEventListener("click", (e) => {
        if (
          e.target &&
          e.target.classList &&
          e.target.classList.contains("ng-grip")
        )
          return;
        ngSort(t, i);
      });
    }
    if (feats.has("resize")) {
      const g = document.createElement("span");
      g.className = "ng-grip";
      g.title =
        "Tarik untuk mengubah lebar · klik dua kali untuk menyesuaikan isi";
      g.addEventListener("click", (e) => e.stopPropagation());
      g.addEventListener("dblclick", (e) => {
        e.stopPropagation();
        ngAutofit(t, i);
      });
      g.addEventListener("pointerdown", (e) => ngDragStart(t, i, e));
      th.appendChild(g);
    }
  });

  // ---- baris filter ----
  if (feats.has("filter")) {
    const tr = document.createElement("tr");
    tr.className = "ng-frow";
    heads.forEach((th, i) => {
      const c = document.createElement("th");
      c.className = "ng-skip";
      if (th.textContent.trim()) {
        const inp = document.createElement("input");
        inp.type = "text";
        inp.placeholder = "Filter";
        inp.setAttribute("aria-label", "Filter kolom " + th.textContent.trim());
        inp.addEventListener("input", () => {
          st.fv[i] = inp.value;
          ngApplyFilter(t);
        });
        c.appendChild(inp);
      }
      tr.appendChild(c);
    });
    t.tHead.appendChild(tr);
    st.frow = tr;
  }
  // tooltip isi lengkap untuk sel yang terpotong
  t.addEventListener("mouseover", (e) => {
    const td = e.target && e.target.closest ? e.target.closest("td") : null;
    if (td && !td.title && td.scrollWidth > td.clientWidth + 1)
      td.title = (td.textContent || "").trim();
  });
  // pulihkan lebar tersimpan
  const saved = ngStore()[ngKey(t)];
  if (
    saved &&
    feats.has("resize") &&
    Array.isArray(saved.w) &&
    saved.w.length === st.n
  )
    ngSetWidths(t, saved.w);
  if (saved && saved.wrap && feats.has("wrap")) {
    st.wrap = true;
    t.classList.add("ng-wrap");
    if (st.bWrap) st.bWrap.classList.add("on");
  }
  st.first = (body && body.rows[0]) || null;
}
function ngSetWidths(t, ws) {
  const st = t._ng,
    ths = Array.from(t.tHead.rows[0].cells),
    cols = Array.from(t.querySelectorAll(":scope > colgroup > col"));
  ths.forEach((th, i) => {
    th.style.width = ws[i] + "px";
    if (cols[i]) cols[i].style.width = ws[i] + "px";
  });
  t.style.tableLayout = "fixed";
  t.style.width = ws.reduce((a, b) => a + b, 0) + "px";
  t.classList.add("ng-fixed");
  st.ws = ws.slice();
  const par = t.parentElement;
  if (par) {
    try {
      if (getComputedStyle(par).overflowX === "visible")
        par.style.overflowX = "auto";
    } catch (_) {
      /* abaikan */
    }
  }
}
function ngFreeze(t) {
  const st = t._ng;
  if (st.ws) return st.ws;
  const ws = Array.from(t.tHead.rows[0].cells).map(
    (th) => Math.round(th.getBoundingClientRect().width) || 80,
  );
  ngSetWidths(t, ws);
  return st.ws;
}
function ngSaveState(t) {
  const st = t._ng,
    s = ngStore();
  s[ngKey(t)] = { w: st.ws || null, wrap: !!st.wrap };
  ngPersist();
}
function ngDragStart(t, i, e) {
  e.preventDefault();
  e.stopPropagation();
  const st = t._ng,
    ws0 = ngFreeze(t).slice(),
    startX = e.clientX,
    startW = ws0[i];
  const mv = (ev) => {
    const ws = ws0.slice();
    ws[i] = Math.max(48, Math.round(startW + ev.clientX - startX));
    ngSetWidths(t, ws);
  };
  const up = () => {
    document.removeEventListener("pointermove", mv);
    document.removeEventListener("pointerup", up);
    ngSaveState(t);
  };
  document.addEventListener("pointermove", mv);
  document.addEventListener("pointerup", up);
}
function ngAutofit(t, i) {
  const ws = ngFreeze(t).slice();
  t.classList.add("ng-measure");
  let max = 48;
  Array.from(t.rows).forEach((r) => {
    const c = r.cells[i];
    if (
      c &&
      c.colSpan === 1 &&
      !(r.classList && r.classList.contains("ng-frow"))
    )
      max = Math.max(max, c.scrollWidth + 4);
  });
  t.classList.remove("ng-measure");
  ws[i] = Math.min(640, max);
  ngSetWidths(t, ws);
  ngSaveState(t);
}
function ngToggleWrap(t) {
  const st = t._ng;
  if (!st.ws && !t.classList.contains("ng-fixed")) ngFreeze(t);
  st.wrap = !st.wrap;
  t.classList.toggle("ng-wrap", st.wrap);
  if (st.bWrap) st.bWrap.classList.toggle("on", st.wrap);
  ngSaveState(t);
}
function ngToggleFilter(t) {
  const st = t._ng;
  st.open = !st.open;
  t.classList.toggle("ng-filters-on", st.open);
  if (st.bFilter) st.bFilter.classList.toggle("on", st.open);
  if (st.open && st.frow) {
    const inp = st.frow.querySelector("input");
    if (inp && inp.focus) inp.focus();
  }
}
function ngDomRows(t) {
  return Array.from(t.tBodies[0].rows).filter((r) => r.cells.length >= t._ng.n);
}
function ngApplyFilter(t) {
  const st = t._ng,
    hook = t.id ? NG.hooks[t.id] : null;
  if (st.bFilter) st.bFilter.classList.toggle("active", st.fv.some(Boolean));
  if (hook && hook.setFilters) {
    hook.setFilters(st.fv.slice());
    return;
  }
  const f = st.fv.map((x) =>
    String(x || "")
      .trim()
      .toLowerCase(),
  );
  ngDomRows(t).forEach((r) => {
    const ok = f.every(
      (x, c) =>
        !x ||
        String((r.cells[c] && r.cells[c].textContent) || "")
          .toLowerCase()
          .includes(x),
    );
    r.style.display = ok ? "" : "none";
  });
}
function ngSort(t, i) {
  const st = t._ng,
    hook = t.id ? NG.hooks[t.id] : null;
  if (st.sortCol === i) {
    if (st.sortDir === 1) st.sortDir = -1;
    else {
      st.sortCol = -1;
      st.sortDir = 1;
    }
  } else {
    st.sortCol = i;
    st.sortDir = 1;
  }
  Array.from(t.tHead.rows[0].cells).forEach((th, c) => {
    if (th.removeAttribute) th.removeAttribute("data-sort-dir");
    if (c === st.sortCol)
      th.setAttribute("data-sort-dir", st.sortDir === 1 ? "asc" : "desc");
  });
  if (hook && hook.setSort) {
    hook.setSort(st.sortCol, st.sortDir);
    return;
  }
  ngApplySort(t);
}
function ngApplySort(t) {
  const st = t._ng;
  if (st.sortCol < 0) return;
  const rows = ngDomRows(t),
    c = st.sortCol,
    d = st.sortDir;
  const val = (r) =>
    String((r.cells[c] && r.cells[c].textContent) || "").trim();
  rows.sort(
    (a, b) =>
      d *
      val(a).localeCompare(val(b), "id", {
        numeric: true,
        sensitivity: "base",
      }),
  );
  const body = t.tBodies[0];
  rows.forEach((r) => body.appendChild(r));
  st.first = body.rows[0] || null;
}
function ngReset(t) {
  const st = t._ng,
    hook = t.id ? NG.hooks[t.id] : null;
  Array.from(t.tHead.rows[0].cells).forEach((th, i) => {
    th.style.width = st.orig.ths[i] || "";
    if (th.removeAttribute) th.removeAttribute("data-sort-dir");
  });
  Array.from(t.querySelectorAll(":scope > colgroup > col")).forEach((c, i) => {
    c.style.width = st.orig.cols[i] || "";
  });
  t.style.tableLayout = st.orig.layout || "";
  t.style.width = st.orig.width || "";
  t.classList.remove("ng-wrap");
  try {
    if (getComputedStyle(t).tableLayout !== "fixed")
      t.classList.remove("ng-fixed");
  } catch (_) {
    /* abaikan */
  }
  st.ws = null;
  st.wrap = false;
  st.sortCol = -1;
  st.sortDir = 1;
  st.fv = st.fv.map(() => "");
  if (st.bWrap) st.bWrap.classList.remove("on");
  if (st.frow)
    st.frow.querySelectorAll("input").forEach((x) => {
      x.value = "";
    });
  if (hook && hook.setFilters) {
    hook.setFilters(st.fv.slice());
    if (hook.setSort) hook.setSort(-1, 1);
  } else {
    ngApplyFilter(t);
    ngDomRows(t).forEach((r) => {
      r.style.display = "";
    });
  }
  if (st.bFilter) st.bFilter.classList.remove("active");
  delete ngStore()[ngKey(t)];
  ngPersist();
}
function ngResetFilters(id) {
  const t =
    typeof document !== "undefined" && document.getElementById
      ? document.getElementById(id)
      : null;
  if (!t || !t._ng) return;
  t._ng.fv = t._ng.fv.map(() => "");
  t._ng.sortCol = -1;
  t._ng.sortDir = 1;
  if (t._ng.frow)
    t._ng.frow.querySelectorAll("input").forEach((x) => {
      x.value = "";
    });
  Array.from(t.tHead.rows[0].cells).forEach((th) => {
    if (th.removeAttribute) th.removeAttribute("data-sort-dir");
  });
  if (t._ng.bFilter) t._ng.bFilter.classList.remove("active");
}
// hook data-level untuk tabel yang dipaginasi di sisi klien
NG.hooks["conn-table"] = {
  setFilters(arr) {
    CONNTBL.colf = arr;
    CONNTBL.page = 1;
    renderConnPage();
  },
  setSort(col, dir) {
    CONNTBL.sortCol = col;
    CONNTBL.sortDir = dir;
    CONNTBL.page = 1;
    renderConnPage();
  },
};
function ngScan() {
  if (typeof document === "undefined" || !document.querySelectorAll) return;
  try {
    npScan();
  } catch (_) {
    /* abaikan */
  }
  document.querySelectorAll(".ng-bar").forEach((b) => {
    if (b._ngTable && b._ngTable.isConnected === false) b.remove();
  });
  document.querySelectorAll("table").forEach((t) => {
    if (t._ng) {
      const body = t.tBodies[0];
      if (body && body.rows[0] !== t._ng.first) {
        // isi tabel dirender ulang oleh aplikasi
        t._ng.first = body.rows[0] || null;
        if (!NG.hooks[t.id]) {
          if (t._ng.fv.some(Boolean)) ngApplyFilter(t);
          if (t._ng.sortCol >= 0) ngApplySort(t);
        }
      }
      return;
    }
    if (ngEligible(t)) {
      try {
        ngEnhance(t);
      } catch (e) {
        if (typeof console !== "undefined") console.warn("ngEnhance", e);
      }
    }
  });
}
function ngSchedule(muts) {
  if (
    muts &&
    muts.every((m) => m.target && m.target.closest && m.target.closest("#map"))
  )
    return; // abaikan perubahan peta
  if (NG.timer) return;
  NG.timer = setTimeout(() => {
    NG.timer = null;
    ngScan();
  }, 80);
}
try {
  if (typeof MutationObserver !== "undefined" && document.body)
    new MutationObserver(ngSchedule).observe(document.body, {
      childList: true,
      subtree: true,
    });
  ngSchedule();
} catch (_) {
  /* abaikan */
}

// =====================================================================
// PANEL BISA DIPINDAH & DIUBAH LEBARNYA (panel melayang di peta + semua modal)
//  - tarik judul = pindah; tarik tepi kiri/kanan = ubah lebar; klik dua kali judul = kembalikan
//  - posisi & lebar diingat per panel; dimatikan pada layar sempit (<= 768 px)
// =====================================================================
const NP = { store: null };
function npStore() {
  if (NP.store) return NP.store;
  try {
    NP.store = JSON.parse(localStorage.getItem("netgis-panels") || "{}") || {};
  } catch (_) {
    NP.store = {};
  }
  return NP.store;
}
function npPersist() {
  try {
    localStorage.setItem("netgis-panels", JSON.stringify(NP.store || {}));
  } catch (_) {
    /* penyimpanan tidak tersedia */
  }
}
function npMobile() {
  return typeof window !== "undefined" && window.innerWidth <= 768;
}
const NP_SKIP =
  "button, a, input, select, textarea, label, summary, [contenteditable='true']";
const NP_EDGE = 8;
function npClamp(v, lo, hi) {
  return Math.min(Math.max(v, lo), Math.max(lo, hi));
}
function npZone(el, e) {
  const r = el.getBoundingClientRect();
  const sb = Math.max(0, el.offsetWidth - el.clientWidth - 2); // lebar scrollbar vertikal (bila ada)
  if (e.clientX - r.left <= NP_EDGE) return "l";
  const fromRight = r.right - e.clientX;
  if (fromRight > sb && fromRight <= NP_EDGE + sb) return "r";
  return "";
}
function npGeom(el) {
  const modal = el._np.kind === "modal";
  if (modal)
    return {
      ox: parseFloat(el.style.left) || 0,
      oy: parseFloat(el.style.top) || 0,
      w: el.getBoundingClientRect().width,
    };
  const par = el.offsetParent || el.parentElement,
    pr = par.getBoundingClientRect(),
    r = el.getBoundingClientRect();
  return {
    left: r.left - pr.left,
    top: r.top - pr.top,
    w: r.width,
    pw: pr.width,
    ph: pr.height,
  };
}
function npFloatPrepare(el, g) {
  el.style.left = g.left + "px";
  el.style.top = g.top + "px";
  el.style.right = "auto";
  el.style.bottom = "auto";
}
// Panel melayang: pastikan tidak keluar dari area peta (posisi lama yang tersimpan di browser bisa
// membuat bagian bawah panel terpotong / tampak kosong). Dipanggil saat dibuka, diubah ukuran, dan setelah digeser.
function npFit(el) {
  if (!el || !el._np || el._np.kind !== "float") return;
  if (npMobile() || !el.style.top) {
    el.style.maxHeight = "";
    return;
  }
  const par = el.offsetParent || el.parentElement;
  if (!par) return;
  const ph = par.clientHeight,
    pw = par.clientWidth;
  if (!ph || !pw) return;
  const MIN_H = 260,
    GAP = 8;
  let top = parseFloat(el.style.top) || 0;
  if (ph - top - GAP < MIN_H) {
    top = Math.max(0, ph - GAP - MIN_H);
    el.style.top = top + "px";
  }
  el.style.maxHeight = Math.max(140, ph - top - GAP) + "px";
  const left = parseFloat(el.style.left) || 0;
  if (left > pw - 80)
    el.style.left = Math.max(0, pw - (el.offsetWidth || 400) - GAP) + "px";
}
try {
  if (typeof window !== "undefined" && window.addEventListener)
    window.addEventListener("resize", () => {
      ["#connect-panel", "#tool-panel"].forEach((q) =>
        npFit(document.querySelector(q)),
      );
    });
} catch (_) {
  /* abaikan */
}
function npDown(e) {
  if (npMobile() || e.button !== 0) return;
  const el = e.currentTarget,
    cfg = el._np;
  if (!cfg) return;
  let mode = "";
  const z = el.classList.contains("collapsed") ? "" : npZone(el, e);
  if (z) mode = "resize-" + z;
  else {
    const h = e.target.closest ? e.target.closest(cfg.handle) : null;
    if (h && el.contains(h) && !(e.target.closest && e.target.closest(NP_SKIP)))
      mode = "move";
  }
  if (!mode) return;
  e.preventDefault();
  const g = npGeom(el),
    x0 = e.clientX,
    y0 = e.clientY,
    modal = cfg.kind === "modal";
  if (modal) el.style.position = "relative";
  else npFloatPrepare(el, g);
  const r0 = el.getBoundingClientRect(),
    vw = window.innerWidth,
    vh = window.innerHeight;
  document.body.classList.add(mode === "move" ? "np-dragging" : "np-resizing");
  const mv = (ev) => {
    const dx = ev.clientX - x0,
      dy = ev.clientY - y0;
    if (mode === "move") {
      if (modal) {
        const ddx = npClamp(dx, 80 - r0.width - r0.left, vw - 80 - r0.left),
          ddy = npClamp(dy, -r0.top, vh - 40 - r0.top);
        el.style.left = g.ox + ddx + "px";
        el.style.top = g.oy + ddy + "px";
      } else {
        el.style.left = npClamp(g.left + dx, 80 - g.w, g.pw - 80) + "px";
        el.style.top = npClamp(g.top + dy, 0, g.ph - 40) + "px";
      }
      return;
    }
    const right = mode === "resize-r";
    const maxW = modal ? vw - 16 : g.pw - 16;
    const w = npClamp(Math.round(g.w + (right ? dx : -dx)), 300, maxW);
    el.style.width = w + "px";
    el.style.maxWidth = "none";
    if (modal) el.style.left = g.ox + ((right ? 1 : -1) * (w - g.w)) / 2 + "px";
    else if (!right)
      el.style.left = npClamp(g.left + (g.w - w), 0, g.pw - 80) + "px";
  };
  const up = () => {
    document.removeEventListener("pointermove", mv);
    document.removeEventListener("pointerup", up);
    document.body.classList.remove("np-dragging", "np-resizing");
    npFit(el);
    npSave(el);
  };
  document.addEventListener("pointermove", mv);
  document.addEventListener("pointerup", up);
}
function npSave(el) {
  const cfg = el._np;
  npStore()[cfg.key] = {
    left: el.style.left,
    top: el.style.top,
    w: el.style.width,
  };
  npPersist();
}
function npHover(e) {
  if (npMobile()) {
    e.currentTarget.style.cursor = "";
    return;
  }
  const el = e.currentTarget;
  el.style.cursor =
    !el.classList.contains("collapsed") &&
    npZone(el, e) &&
    !(e.target.closest && e.target.closest(NP_SKIP))
      ? "ew-resize"
      : "";
}
function npReset(el) {
  const cfg = el._np;
  [
    "left",
    "top",
    "right",
    "bottom",
    "width",
    "maxWidth",
    "maxHeight",
    "position",
  ].forEach((k) => {
    el.style[k] = "";
  });
  delete npStore()[cfg.key];
  npPersist();
}
function npApply(el) {
  npApplyRaw(el);
  npFit(el);
}
function npApplyRaw(el) {
  const s = npStore()[el._np.key];
  if (!s) return;
  if (el._np.kind === "modal") {
    if (s.left || s.top) {
      el.style.position = "relative";
      el.style.left = s.left;
      el.style.top = s.top;
    }
    if (s.w) {
      el.style.width = s.w;
      el.style.maxWidth = "none";
    }
  } else {
    if (s.left && s.top) {
      el.style.left = s.left;
      el.style.top = s.top;
      el.style.right = "auto";
      el.style.bottom = "auto";
    }
    if (s.w) {
      el.style.width = s.w;
      el.style.maxWidth = "none";
    }
  }
}
function npAttach(el, cfg) {
  if (!el || el._np) return;
  el._np = cfg;
  el.addEventListener("pointerdown", npDown);
  el.addEventListener("pointermove", npHover);
  el.addEventListener("dblclick", (e) => {
    const h = e.target.closest ? e.target.closest(cfg.handle) : null;
    if (h && !(e.target.closest && e.target.closest(NP_SKIP))) npReset(el);
  });
  npApply(el);
}
function npScan() {
  if (typeof document === "undefined" || !document.querySelector) return;
  [
    ["#connect-panel", ".cn-head"],
    ["#tool-panel", ".tp-head"],
  ].forEach(([sel, handle]) => {
    const el = document.querySelector(sel);
    if (el) npAttach(el, { key: sel, handle, kind: "float" });
  });
  document.querySelectorAll(".modal-container").forEach((el) => {
    const ov = el.closest ? el.closest(".modal-overlay") : null;
    if (
      !ov ||
      !ov.id ||
      ov.id === "modal-password" ||
      ov.classList.contains("auth-modal-top")
    )
      return;
    npAttach(el, { key: ov.id, handle: ".modal-header", kind: "modal" });
  });
}

// Penanda build: arahkan kursor ke badge "GIS Database" untuk memastikan app.js terbaru yang berjalan
// =====================================================================
// GESER DENGAN MOUSE (drag-to-pan): diagram jalur (.trace-canvas) dan grafik OTDR (#sor-chart)
// =====================================================================
const ND = {
  mode: null,
  el: null,
  x: 0,
  y: 0,
  sl: 0,
  st: 0,
  v0: null,
  moved: false,
  suppress: false,
};
function ndDown(e) {
  if (!e || e.button !== 0 || !e.target || !e.target.closest) return;
  if (e.target.closest("button, input, select, textarea")) return;
  const tc = e.target.closest(".trace-canvas"),
    sc = e.target.closest("#sor-chart");
  const el = tc || sc;
  if (!el) return;
  ND.mode = tc ? "trace" : "sor";
  ND.el = el;
  ND.x = e.clientX;
  ND.y = e.clientY;
  ND.moved = false;
  ND.sl = el.scrollLeft;
  ND.st = el.scrollTop;
  ND.v0 =
    typeof SOR !== "undefined" && SOR.view
      ? { x0: SOR.view.x0, x1: SOR.view.x1 }
      : null;
}
function ndMove(e) {
  if (!ND.mode) return;
  const dx = e.clientX - ND.x,
    dy = e.clientY - ND.y;
  if (!ND.moved) {
    if (Math.abs(dx) + Math.abs(dy) < 4) return;
    ND.moved = true;
    ND.el.classList.add("nd-grabbing");
  }
  if (e.preventDefault) e.preventDefault();
  if (ND.mode === "trace") {
    ND.el.scrollLeft = ND.sl - dx;
    ND.el.scrollTop = ND.st - dy;
  } else if (ND.v0 && typeof SOR !== "undefined" && SOR.data) {
    const w = ND.el.getBoundingClientRect().width || 760;
    const span = ND.v0.x1 - ND.v0.x0,
      shift = (dx / w) * (760 / 696) * span;
    sorClamp(ND.v0.x0 - shift, ND.v0.x1 - shift);
    sorRedraw();
  }
}
function ndUp() {
  if (!ND.mode) return;
  if (ND.moved) {
    ND.suppress = true;
    setTimeout(() => {
      ND.suppress = false;
    }, 0);
  }
  if (ND.el) ND.el.classList.remove("nd-grabbing");
  ND.mode = null;
  ND.el = null;
}
if (typeof document !== "undefined" && document.addEventListener) {
  document.addEventListener("mousedown", ndDown);
  document.addEventListener("mousemove", ndMove);
  document.addEventListener("mouseup", ndUp);
  document.addEventListener(
    "click",
    (e) => {
      if (ND.suppress && e.stopPropagation) {
        e.stopPropagation();
        if (e.preventDefault) e.preventDefault();
      }
    },
    true,
  );
}

// --- Bagian detail yang bisa dilipat (ikon di judul). Status disimpan per kunci bagian, berlaku untuk semua aset ---
const CX = { closed: {} };
try {
  const _s = localStorage.getItem("netgis.cx");
  if (_s) CX.closed = JSON.parse(_s) || {};
} catch (_) {
  /* tanpa penyimpanan: status hanya selama sesi */
}
function cxIsOpen(key) {
  return !CX.closed[key];
}
function cxCls(key) {
  return CX.closed[key] ? " cx-closed" : "";
}
function cxOpen(key) {
  return `<div class="cx-sec${cxCls(key)}" data-cx="${key}">`;
}
function cxStart(key, tag, inner, style) {
  // pembuka bagian + judul yang bisa diklik; isi bagian dibungkus <div class="cx-body"> oleh pemanggil
  return `${cxOpen(key)}<${tag} class="cx-head" role="button" tabindex="0" aria-expanded="${cxIsOpen(key)}"${style ? ` style="${style}"` : ""} title="Klik untuk menutup / membuka">${inner}</${tag}>`;
}
function cxSet(key, closed) {
  if (closed) CX.closed[key] = true;
  else delete CX.closed[key];
  try {
    localStorage.setItem("netgis.cx", JSON.stringify(CX.closed));
  } catch (_) {
    /* abaikan */
  }
}
// Ganti status satu bagian (sec = elemen .cx-sec); bagian yang sama di aset lain mengikuti saat dibuka
function cxToggle(sec) {
  if (!sec || !sec.getAttribute) return false;
  const key = sec.getAttribute("data-cx");
  if (!key) return false;
  const closed = !CX.closed[key];
  cxSet(key, closed);
  if (sec.classList) {
    if (closed) sec.classList.add("cx-closed");
    else sec.classList.remove("cx-closed");
  }
  const h = sec.querySelector ? sec.querySelector(".cx-head") : null;
  if (h && h.setAttribute) h.setAttribute("aria-expanded", String(!closed));
  return closed;
}
// Bagian statis di index.html (Daftar Core Tersambung, Jalur & Dampak): terapkan status tersimpan
function cxSyncStatic() {
  const map = { "cx-conn": "conn", "trace-panel": "trace" };
  Object.keys(map).forEach((id) => {
    const el = document.getElementById(id);
    if (!el || !el.classList) return;
    if (CX.closed[map[id]]) el.classList.add("cx-closed");
    else el.classList.remove("cx-closed");
    const h = el.querySelector ? el.querySelector(".cx-head") : null;
    if (h && h.setAttribute)
      h.setAttribute("aria-expanded", String(!CX.closed[map[id]]));
  });
}
document.addEventListener("click", (e) => {
  const t = e.target;
  if (!t || !t.closest) return;
  const h = t.closest(".cx-head");
  if (!h || t.closest("button, a, input, select, textarea, label, summary"))
    return;
  cxToggle(h.closest(".cx-sec"));
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const t = e.target;
  if (!t || !t.classList || !t.classList.contains("cx-head")) return;
  e.preventDefault();
  cxToggle(t.closest(".cx-sec"));
});

const NETGIS_BUILD = "20261006f";
try {
  const _sb = document.querySelector(".status-badge");
  if (_sb) _sb.title = "Build " + NETGIS_BUILD;
  console.info("NETGIS build", NETGIS_BUILD);
} catch (_) {
  /* abaikan */
}

// Wadah panel (overflow tersembunyi) bisa tergulir sendiri oleh fokus/klik di dalamnya sehingga judul hilang dan bagian bawah kosong.
// Kunci agar selalu kembali ke 0.
(function () {
  try {
    ["tool-panel", "connect-panel"].forEach((id) => {
      const el = document.getElementById(id);
      if (el && el.addEventListener)
        el.addEventListener("scroll", () => {
          if (el.scrollTop) el.scrollTop = 0;
          if (el.scrollLeft) el.scrollLeft = 0;
        });
    });
    ["map-viewport", "main-wrapper"].forEach((c) => {
      const el = document.querySelector && document.querySelector("." + c);
      if (el && el.addEventListener)
        el.addEventListener("scroll", () => {
          if (el.scrollTop) el.scrollTop = 0;
          if (el.scrollLeft) el.scrollLeft = 0;
        });
    });
  } catch (_) {
    /* abaikan */
  }
})();

// =====================================================================
// FORMAT NAMA ASET OTOMATIS
// =====================================================================
const NM = { cfg: null, timer: null, data: null };
function nmMsg(t, k) {
  const e = $id("nm-msg");
  if (e) {
    e.textContent = t || "";
    e.className = "ap-msg" + (k ? " " + k : "");
  }
}
function openNamingModal() {
  $id("modal-naming").style.display = "flex";
  $id("nm-save").style.display = can("wilayah.edit") ? "" : "none";
  nmMsg("");
  return apiRequest("/api/naming")
    .then((d) => {
      NM.data = d;
      NM.cfg = d.config;
      $id("nm-template").value = d.config.template;
      $id("nm-digits").value = d.config.digits;
      if ($id("nm-import"))
        $id("nm-import").checked = d.config.import_rename !== false;
      $id("nm-presets").innerHTML = d.presets
        .map(
          (p) =>
            `<button type="button" class="nm-preset" onclick="nmPreset('${escapeHtml(p.id)}')"><b>${escapeHtml(p.id)}</b> ${escapeHtml(p.label)}<small>${escapeHtml(p.template)}</small></button>`,
        )
        .join("");
      nmRenderCodes(d.wilayah);
      nmRenderExamples(d.examples);
    })
    .catch((e) => nmMsg(e.message, "err"));
}
function closeNamingModal() {
  $id("modal-naming").style.display = "none";
}
function nmPreset(id) {
  const p = (NM.data.presets || []).find((x) => x.id === id);
  if (!p) return;
  $id("nm-template").value = p.template;
  $id("nm-digits").value = p.digits;
  nmPreviewSoon(true);
}
function nmRenderCodes(wil) {
  const codes = (NM.cfg && NM.cfg.codes) || {};
  const seen = new Set();
  const rows = [];
  (wil || []).forEach((w) => {
    [
      ["cluster", w.cluster, w.cluster_code],
      ["area", w.area, w.area_code],
    ].forEach(([k, name, code]) => {
      const key = String(name).toUpperCase();
      if (seen.has(key)) return;
      seen.add(key);
      rows.push(
        `<label>${escapeHtml(name)} <small>${k}</small><input type="text" data-nm-code="${escapeHtml(key)}" value="${escapeHtml(codes[key] || code)}" maxlength="8" oninput="nmPreviewSoon()" /></label>`,
      );
    });
  });
  $id("nm-codes").innerHTML = rows.length
    ? `<h4 class="nm-h">Kode singkatan wilayah</h4><div class="nm-codegrid">${rows.slice(0, 60).join("")}</div>`
    : "";
}
function nmCollect() {
  const codes = {};
  Array.prototype.forEach.call(
    document.querySelectorAll("[data-nm-code]") || [],
    (i) => {
      const v = String(i.value || "").trim();
      if (v) codes[i.getAttribute("data-nm-code")] = v;
    },
  );
  return {
    template: $id("nm-template").value.trim(),
    digits: parseInt($id("nm-digits").value, 10) || 0,
    codes,
    import_rename: !$id("nm-import") || $id("nm-import").checked,
  };
}
function nmRenderExamples(list) {
  const el = $id("nm-examples");
  if (!list || !list.length) {
    el.innerHTML = "";
    return;
  }
  el.innerHTML = list
    .slice(0, 24)
    .map(
      (e) =>
        `<div class="nm-ex"><span class="nm-ex-t">${escapeHtml(e.kind === "CABLE" ? "Kabel " + e.type : e.type)}</span><span class="nm-ex-n">${e.names.map(escapeHtml).join(" &middot; ")}</span></div>`,
    )
    .join("");
}
function nmPreviewSoon(now) {
  clearTimeout(NM.timer);
  NM.timer = setTimeout(
    () => {
      apiRequest("/api/naming/preview", "POST", nmCollect())
        .then((r) => {
          nmMsg("");
          nmRenderExamples(r.examples);
        })
        .catch((e) => nmMsg(e.message, "err"));
    },
    now ? 0 : 300,
  );
}
function nmSave() {
  return apiRequest("/api/naming", "PUT", nmCollect())
    .then((r) => {
      NM.cfg = r.config;
      nmRenderExamples(r.examples);
      nmMsg(r.message, "ok");
    })
    .catch((e) => nmMsg(e.message, "err"));
}
async function invAutoName() {
  if (invBusy || !can("asset.write")) return;
  const list = [...invSel.values()].filter(
    (x) => !(x.kind === "node" && (x.type === "POP" || x.type === "PELANGGAN")),
  );
  if (!list.length)
    return alert(
      "Tidak ada aset yang bisa diberi nama otomatis (POP dan Pelanggan dilewati).",
    );
  const skipped = invSel.size - list.length;
  if (
    !confirm(
      `Ganti nama ${list.length} aset dengan nama otomatis?${skipped ? `\n${skipped} POP/Pelanggan dilewati.` : ""}\nNama lama tercatat di Riwayat (audit) sehingga bisa dilihat kembali.`,
    )
  )
    return;
  invBusy = true;
  let ok = 0;
  const failed = [];
  try {
    for (let i = 0; i < list.length; i += INV_BULK_CHUNK) {
      const ch = list.slice(i, i + INV_BULK_CHUNK);
      invBarProgress(
        `Memberi nama ${i} dari ${list.length}...`,
        (i / list.length) * 100,
      );
      const r = await apiRequest("/api/assets/autoname", "POST", {
        nodes: ch.filter((x) => x.kind === "node").map((x) => x.id),
        cables: ch.filter((x) => x.kind === "cable").map((x) => x.id),
      });
      ok += r.renamed || 0;
      (r.failed || []).forEach((f) => failed.push(`${f.name}: ${f.reason}`));
    }
  } catch (err) {
    failed.push(err.message);
  }
  invBusy = false;
  invClearSel();
  alert(
    `${ok} aset diberi nama otomatis.` +
      (failed.length
        ? `\n\nDilewati/gagal (${failed.length}):\n` +
          failed.slice(0, 8).join("\n")
        : ""),
  );
  try {
    await loadData();
  } catch (_) {
    /* tabel disegarkan di bawah */
  }
  renderInventoryTable();
}

// ===== Aset berdempetan: ikon menyebar (spiderfy) saat diklik, lencana jumlah pada titik tumpuk =====
function ovlCreate(getMap, getMarkers, th) {
  const S = { set: null, orig: new Map(), legs: null };
  const TH = th || 14;
  const api = {
    collapse() {
      if (!S.set) return;
      S.orig.forEach((ll, m) => {
        try {
          m.setLatLng(ll);
        } catch (_) {
          /* abaikan */
        }
      });
      try {
        if (S.legs) S.legs.remove();
      } catch (_) {
        /* abaikan */
      }
      S.set = null;
      S.legs = null;
      S.orig.clear();
    },
    group(marker) {
      const mp = getMap();
      if (!mp) return [];
      const c0 = mp.latLngToContainerPoint(marker.getLatLng());
      return getMarkers().filter(
        (m) =>
          m._map &&
          mp.latLngToContainerPoint(m.getLatLng()).distanceTo(c0) < TH,
      );
    },
    click(marker) {
      const mp = getMap();
      if (!mp || !marker._map) return false;
      if (S.set && S.set.has(marker)) return false;
      api.collapse();
      const grp = api.group(marker);
      if (grp.length < 2) return false;
      const c = mp.latLngToContainerPoint(marker.getLatLng()),
        center = marker.getLatLng(),
        n = grp.length;
      const legs = L.layerGroup();
      S.set = new Set(grp);
      grp.forEach((m, k) => {
        S.orig.set(m, m.getLatLng());
        let ang, rad;
        if (n <= 9) {
          ang = (2 * Math.PI * k) / n - Math.PI / 2;
          rad = Math.max(36, (n * 30) / (2 * Math.PI));
        } else {
          ang = k * 0.85;
          rad = 34 + 6 * k;
        }
        const pt = L.point(
          c.x + rad * Math.cos(ang),
          c.y + rad * Math.sin(ang),
        );
        const ll = mp.containerPointToLatLng(pt);
        L.polyline([center, ll], {
          color: "#334155",
          weight: 1.6,
          opacity: 0.8,
          interactive: false,
        }).addTo(legs);
        m.setLatLng(ll);
      });
      L.circleMarker(center, {
        radius: 3,
        color: "#334155",
        weight: 1,
        fillColor: "#334155",
        fillOpacity: 1,
        interactive: false,
      }).addTo(legs);
      legs.addTo(mp);
      S.legs = legs;
      return true;
    },
    badges() {
      const mp = getMap();
      if (!mp) return;
      const all = getMarkers().filter((m) => m._map && m._icon);
      all.forEach((m) => m._icon.removeAttribute("data-ovl"));
      if (S.set || all.length > 6000) return;
      const cells = new Map();
      all.forEach((m) => {
        const p = mp.latLngToContainerPoint(m.getLatLng()),
          key = Math.floor(p.x / TH) + ":" + Math.floor(p.y / TH);
        (cells.get(key) || cells.set(key, []).get(key)).push(m);
      });
      cells.forEach((arr) => {
        if (arr.length > 1)
          arr[arr.length - 1]._icon.setAttribute(
            "data-ovl",
            String(arr.length),
          );
      });
    },
  };
  return api;
}
const OVLMAIN = ovlCreate(
  () => map,
  () =>
    Object.keys(markersMap)
      .filter((k) => k.startsWith("node:"))
      .map((k) => markersMap[k]),
);
const OVLM = ovlCreate(
  () => PZM.map,
  () =>
    PZM.map
      ? [].concat(
          PZM.bg ? PZM.bg.getLayers() : [],
          PZM.cand ? PZM.cand.getLayers() : [],
        )
      : [],
  14,
);
(function ovlInit() {
  let t = null;
  const later = () => {
    clearTimeout(t);
    t = setTimeout(() => {
      try {
        OVLMAIN.badges();
      } catch (_) {
        /* abaikan */
      }
    }, 300);
  };
  map.on("zoomstart", () => OVLMAIN.collapse());
  map.on("click", () => OVLMAIN.collapse());
  map.on("moveend zoomend", later);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      OVLMAIN.collapse();
      try {
        OVLM.collapse();
      } catch (_) {
        /* abaikan */
      }
    }
  });
  setTimeout(later, 1500);
})();

// ===== MODE PUZZLE (Rapikan Topologi): rangkai aset -> kabel -> core; kabel yang belum ada dibuat otomatis =====
const PZ = {
  chain: [],
  links: [],
  par: [],
  cur: -1,
  doneN: 0,
  prev: null,
  sel: null,
  pick: { open: false, q: "", items: [], total: 0, seq: 0 },
  busy: false,
  timer: null,
  seq: 0,
  last: null,
};
const PZ_CAPS = ["2C", "4C", "6C", "12C", "24C", "48C", "96C", "144C"];
function pzNew() {
  return { name: "", type: "", capacity: "", installation: "Udara" };
}
function pzShort(l) {
  const m = /Core\s*(\d+)/i.exec(l || "");
  return m ? m[1] : String(l || "");
}
function pzReq() {
  return {
    chain: PZ.chain.map((n) => n.id),
    parents: PZ.par.slice(),
    links: PZ.links.map((lk, i) => {
      if (lk.done) return { cable_id: lk.cable_id, done: true };
      const mp = mxPayload(i);
      const base = mp
        ? {
            cores: mp.via,
            from_cores: mp.from,
            to_ports: mp.to,
            upstream_cable_id: mp.upstream,
            n: mp.via.length,
          }
        : {
            cores: lk.cores && lk.cores.length ? lk.cores : null,
            n: lk.n || null,
          };
      return lk.cable_id
        ? { cable_id: lk.cable_id, ...base }
        : {
            new: {
              name: lk.new.name,
              type: lk.new.type || null,
              capacity: lk.new.capacity || null,
              installation: lk.new.installation,
            },
            ...base,
          };
    }),
  };
}
function pzIsDone(i) {
  return i === 0
    ? PZ.links.some((l) => l.done)
    : !!(PZ.links[i - 1] && PZ.links[i - 1].done);
}
function pzCur() {
  if (PZ.cur < 0 || PZ.cur >= PZ.chain.length) PZ.cur = PZ.chain.length - 1;
  return PZ.cur;
}
function pzPreviewSoon() {
  clearTimeout(PZ.timer);
  PZ.timer = setTimeout(pzPreview, 200);
}
function pzPreview() {
  if (PZ.chain.length < 2) {
    PZ.prev = null;
    pzRender();
    return Promise.resolve();
  }
  const seq = ++PZ.seq;
  return apiRequest("/api/topofix/puzzle/preview", "POST", pzReq())
    .then((r) => {
      if (seq !== PZ.seq) return;
      let again = false;
      r.links.forEach((p, i) => {
        const lk = PZ.links[i];
        if (lk && lk.fresh) {
          lk.fresh = false;
          if (!lk.cable_id && p.existing.length) {
            lk.cable_id = p.existing[0].id;
            again = true;
          }
        }
        if (lk && p.already && !lk.done && p.cable) {
          lk.done = true;
          lk.dn = {
            name: p.cable.name,
            capacity: p.cable.capacity,
            installation: p.cable.installation,
            length_m: p.cable.length_m,
          };
          again = true;
        }
      });
      if (again) return pzPreview();
      PZ.prev = r;
      pzRender();
    })
    .catch((err) => {
      PZ.prev = null;
      tfSetMsg("Pratinjau gagal: " + err.message, "err");
      pzRender();
    });
}
function pzOpenPicker() {
  PZ.pick.open = true;
  PZ.pick.q = "";
  PZ.sel = null;
  pzLoadPick();
  pzRender();
}
function pzClosePicker() {
  PZ.pick.open = false;
  pzRender();
}
let pzPickTimer = null;
function pzPickQ(v) {
  PZ.pick.q = v;
  clearTimeout(pzPickTimer);
  pzPickTimer = setTimeout(pzLoadPick, 220);
}
function pzLoadPick() {
  const seq = ++PZ.pick.seq,
    last = PZ.chain[pzCur()];
  const qs = new URLSearchParams({ q: PZ.pick.q || "", limit: "40" });
  if (last) qs.set("from_id", String(last.id));
  else {
    try {
      const c = map.getCenter();
      qs.set("lat", String(c.lat));
      qs.set("lng", String(c.lng));
    } catch (_) {
      /* tanpa referensi peta */
    }
  }
  return apiRequest("/api/topofix/puzzle/nodes?" + qs.toString())
    .then((r) => {
      if (seq !== PZ.pick.seq) return;
      const inChain = new Set(PZ.chain.map((n) => n.id));
      PZ.pick.items = (r.items || []).filter((x) => !inChain.has(x.id));
      PZ.pick.total = r.total || 0;
      const box = $id("pz-pick-list");
      if (box) box.innerHTML = pzPickHtml();
      try {
        pzMapRender();
      } catch (_) {
        /* minimap opsional */
      }
    })
    .catch((err) =>
      tfSetMsg("Gagal memuat daftar aset: " + err.message, "err"),
    );
}
function pzPickHtml() {
  if (!PZ.pick.items.length)
    return `<div class="pz-empty">Tidak ada aset yang cocok. Ketik sebagian nama untuk mencari.</div>`;
  return (
    PZ.pick.items
      .map(
        (x) =>
          `<button type="button" class="pz-pick-row" onclick="pzAdd(${Number(x.id)})" onmouseenter="pzHover(${Number(x.id)})" onmouseleave="pzHover(null)"><span class="pz-ic t-${escapeHtml(String(x.type).toUpperCase())}">${mapGlyph(x.type, 16)}</span>` +
          `<span class="pz-pn">${escapeHtml(x.name)}<small>${escapeHtml(x.type)}${x.area ? " · " + escapeHtml(x.area) : ""}</small></span>` +
          `<span class="pz-pd">${x.distance_m != null ? fmtInt(x.distance_m) + " m" : ""}${x.cables && x.cables.length ? `<em>kabel ada: ${escapeHtml(x.cables[0])}</em>` : ""}</span></button>`,
      )
      .join("") +
    (PZ.pick.total > PZ.pick.items.length
      ? `<div class="pz-empty">${fmtInt(PZ.pick.total)} aset cocok; persempit dengan mengetik nama.</div>`
      : "")
  );
}
function pzAdd(id) {
  const it = PZ.pick.items.find((x) => Number(x.id) === Number(id));
  if (it) pzAddNode(it);
}
function pzAddNode(it) {
  if (!it || PZ.chain.some((n) => n.id === it.id)) return;
  if (PZ.chain.length && String(it.type).toUpperCase() === "POP") {
    tfSetMsg("POP hanya boleh di awal rantai.", "err");
    return;
  }
  const parent = PZ.chain.length ? pzCur() : -1;
  if (
    parent >= 0 &&
    String(PZ.chain[parent].type).toUpperCase() === "PELANGGAN"
  ) {
    tfSetMsg("Pelanggan adalah ujung; tidak bisa punya cabang.", "err");
    return;
  }
  PZ.chain.push({
    id: it.id,
    name: it.name,
    type: it.type,
    lat: it.lat,
    lng: it.lng,
  });
  if (parent >= 0) {
    PZ.par.push(parent);
    PZ.links.push({
      cable_id: null,
      new: pzNew(),
      cores: null,
      n: null,
      fresh: true,
    });
  }
  PZ.cur = PZ.chain.length - 1;
  PZ.pick.open = true;
  PZ.pick.q = "";
  PZ.last = null;
  PZ.sel = null;
  pzLoadPick();
  tfSetMsg("", "");
  pzPreview();
  pzMapFit();
}
function pzRemoveLast() {
  if (PZ.chain.length < 2 || pzIsDone(PZ.chain.length - 1)) return;
  PZ.chain.pop();
  if (PZ.links.length) PZ.links.pop();
  if (PZ.par.length) PZ.cur = PZ.par.pop();
  PZ.sel = null;
  PZ.prev = null;
  PZ.pick.open = false;
  PZ.cur = Math.min(PZ.cur, PZ.chain.length - 1);
  pzPreview();
  pzMapFit();
}
function pzReset() {
  PZ.chain = [];
  PZ.links = [];
  PZ.par = [];
  PZ.cur = -1;
  PZ.doneN = 0;
  PZ.prev = null;
  PZ.sel = null;
  PZ.last = null;
  PZ.pick.open = true;
  pzLoadPick();
  tfSetMsg("", "");
  pzRender();
}
// cabang / lanjut dari blok mana pun (juga yang sudah diterapkan)
function pzBranch(i) {
  if (!PZ.chain[i]) return;
  if (String(PZ.chain[i].type).toUpperCase() === "PELANGGAN") {
    tfSetMsg("Pelanggan adalah ujung; tidak bisa punya cabang.", "err");
    return;
  }
  PZ.cur = i;
  PZ.pick.open = true;
  PZ.pick.q = "";
  PZ.sel = null;
  pzLoadPick();
  pzRender();
}
function pzSel(i) {
  PZ.sel = PZ.sel === i ? null : i;
  PZ.pick.open = false;
  pzRender();
}
function pzUseExisting(i, cid) {
  const lk = PZ.links[i];
  if (!lk) return;
  lk.cable_id = cid ? Number(cid) : null;
  lk.cores = null;
  lk.n = null;
  lk.mx = null;
  pzPreview();
}
function pzSetNew(i, field, v) {
  const lk = PZ.links[i];
  if (!lk) return;
  lk.new[field] = v;
  if (field !== "name") {
    lk.cores = null;
    lk.mx = null;
  }
  if (field === "capacity") lk.n = null;
  pzPreview();
}
function pzSetN(i, v) {
  const lk = PZ.links[i];
  if (!lk) return;
  const n = parseInt(v, 10);
  lk.n = n > 0 ? n : null;
  lk.cores = null;
  pzPreviewSoon();
}
function pzLinkHtml(i) {
  const p = PZ.prev && PZ.prev.links[i],
    lk = PZ.links[i];
  if (!lk) return "";
  if (lk.done) {
    const dc = (p && p.cable) ||
      lk.dn || { name: "kabel", capacity: "", installation: "", length_m: 0 };
    return `<div class="pz-conn have done"><span class="pz-line"></span><span class="pz-chip"><b><i class="fa-solid fa-circle-check"></i> diterapkan</b> ${escapeHtml(dc.name)}<small>${escapeHtml(dc.capacity || "")} · ${escapeHtml(dc.installation || "-")} · ${fmtInt(dc.length_m || 0)} m</small></span></div>`;
  }
  if (!p)
    return `<div class="pz-conn pending"><span class="pz-line"></span><span class="pz-chip">memeriksa…</span></div>`;
  const c = p.cable,
    bad = p.errors.length || p.warnings.length;
  const cls = p.mode === "baru" ? "new" : "have";
  const coreTxt = p.cores.length
    ? "core " + p.cores.map(pzShort).join(", ")
    : "core ?";
  return (
    `<button type="button" class="pz-conn ${cls} ${PZ.sel === i ? "sel" : ""} ${p.errors.length ? "err" : ""}" onclick="pzSel(${i})" title="Klik untuk ubah kabel dan core">` +
    `<span class="pz-line"></span><span class="pz-chip">${p.mode === "baru" ? `<b>+ kabel baru</b>` : `<b>kabel</b>`} ${escapeHtml(c.name)}<small>${escapeHtml(c.capacity || "")} · ${escapeHtml(c.installation || "-")} · ${fmtInt(c.length_m)} m · ${escapeHtml(coreTxt)}</small>${bad ? `<i class="fa-solid fa-triangle-exclamation"></i>` : ""}</span></button>`
  );
}
function pzEditorHtml(i) {
  const p = PZ.prev && PZ.prev.links[i],
    lk = PZ.links[i];
  if (!p || !lk || lk.done) return "";
  let h = `<div class="pz-edit"><div class="pz-edit-h">${escapeHtml(p.from.name)} <i class="fa-solid fa-arrow-right"></i> ${escapeHtml(p.to.name)}<small>${fmtInt(p.distance_m)} m</small></div>`;
  h += `<div class="pz-radio"><label><input type="radio" name="pz-m" ${lk.cable_id ? "" : "checked"} onchange="pzUseExisting(${i}, null)"> Buat kabel baru (otomatis tampil di peta dan inventory)</label>`;
  p.existing.forEach((e) => {
    h += `<label><input type="radio" name="pz-m" ${lk.cable_id === e.id ? "checked" : ""} onchange="pzUseExisting(${i}, ${Number(e.id)})"> Pakai kabel <b>${escapeHtml(e.name)}</b> <small>${escapeHtml(e.capacity || "")} · ${escapeHtml(e.installation || "-")} · ${fmtInt(e.free.length)} core bebas dari ${fmtInt(e.total)}</small></label>`;
  });
  h += `</div>`;
  if (p.mode === "baru") {
    h +=
      `<div class="pz-form"><label>Nama kabel<input type="text" value="${escapeHtml(lk.new.name)}" placeholder="${escapeHtml(p.cable.name)}" maxlength="120" onchange="pzSetNew(${i}, 'name', this.value)"></label>` +
      `<label>Jenis<select onchange="pzSetNew(${i}, 'type', this.value)">${["", "Backbone", "Feeder", "Distribution", "Drop"].map((t) => `<option value="${t}" ${lk.new.type === t ? "selected" : ""}>${t || "Otomatis (" + escapeHtml(p.cable.type) + ")"}</option>`).join("")}</select></label>` +
      `<label>Kapasitas<select onchange="pzSetNew(${i}, 'capacity', this.value)"><option value="">Otomatis (${escapeHtml(p.cable.capacity)})</option>${PZ_CAPS.map((c) => `<option ${lk.new.capacity === c ? "selected" : ""}>${c}</option>`).join("")}</select></label>` +
      `<div class="pz-inst"><span>Pemasangan</span>${["Udara", "Tanah"].map((v) => `<button type="button" class="${lk.new.installation === v ? "on" : ""}" onclick="pzSetNew(${i}, 'installation', '${v}')">${v}</button>`).join("")}</div></div>`;
  }
  h += mxHtml(i);
  p.warnings.forEach((w) => {
    h += `<div class="pz-note warn"><i class="fa-solid fa-triangle-exclamation"></i> ${escapeHtml(w)}</div>`;
  });
  p.errors.forEach((w) => {
    h += `<div class="pz-note err"><i class="fa-solid fa-circle-xmark"></i> ${escapeHtml(w)}</div>`;
  });
  return h + `</div>`;
}

// ===== Matriks splicing sederhana di puzzle: hulu (kiri) -> core kabel (tengah) -> port hilir (kanan) =====
const MXC = {}; // cache daftar hulu/hilir: "u<aset>:<kabel>" / "d<aset>"
function mxS(i) {
  const lk = PZ.links[i];
  return (
    lk.mx ||
    (lk.mx = {
      pairs: [],
      pL: [],
      pR: [],
      text: "",
      showText: false,
      upCable: null,
      err: "",
      isJ: false,
    })
  );
}
function mxShort(l) {
  const m = /Core\s*(\d+)/i.exec(l || "");
  return m ? m[1] : String(l || "");
}
function mxNum(l) {
  const m = /(\d+)\D*$/.exec(l || "");
  return m ? Number(m[1]) : NaN;
}
function mxLoad(i) {
  const p = PZ.prev && PZ.prev.links[i],
    lk = PZ.links[i];
  if (!p || !lk) return;
  const jobs = [
    [
      `u${p.from.id}:${lk.cable_id || 0}`,
      `/api/topofix/upstream?node_id=${Number(p.from.id)}${lk.cable_id ? "&exclude_cable=" + Number(lk.cable_id) : ""}`,
    ],
    [
      `d${p.to.id}`,
      /ODP|PELANGGAN/i.test(p.to.type || "")
        ? `/api/topofix/node-ports?node_id=${Number(p.to.id)}&dir=in`
        : null,
    ],
  ];
  jobs.forEach(([k, u]) => {
    if (!u || MXC[k]) return;
    MXC[k] = { loading: true };
    apiRequest(u)
      .then((r) => {
        MXC[k] = { d: r };
      })
      .catch((e) => {
        MXC[k] = { err: e.message };
      })
      .then(() => {
        if (PZ.sel === i) pzRender();
      });
  });
}
// hulu yang sudah "diambil" sambungan lain pada rantai yang sama (belum diterapkan): label -> nama tujuan
function mxReserved(i, L) {
  const resv = new Map(),
    me = PZ.prev.links[i],
    fid = me.from.id;
  PZ.links.forEach((lk, j) => {
    const pj = PZ.prev.links[j];
    if (j === i || !lk || lk.done || !pj || pj.from.id !== fid) return;
    const name = pj.to.name,
      mx = lk.mx;
    if (mx && mx.pairs.length && mx.pairs.every((x) => x.u))
      mx.pairs.forEach((x) => resv.set(x.u, name));
    else if (j < i && !(mx && mx.pairs.length)) {
      // otomatis: server memakai hulu bebas pertama sebanyak core-nya
      let n = (pj.cores || []).length;
      for (const l of L) {
        if (n <= 0) break;
        if (!resv.has(l)) {
          resv.set(l, name);
          n--;
        }
      }
    }
  });
  return resv;
}
function mxLists(i) {
  const p = PZ.prev.links[i],
    lk = PZ.links[i],
    m = mxS(i);
  const U = MXC[`u${p.from.id}:${lk.cable_id || 0}`],
    Dn = MXC[`d${p.to.id}`];
  let L = [],
    LA = [],
    cabs = [];
  m.isJ = false;
  if (U && U.d) {
    if (U.d.junction) {
      m.isJ = true;
      cabs = U.d.cables || [];
      if (m.upCable == null || !cabs.some((c) => c.id === m.upCable))
        m.upCable = cabs.length ? cabs[0].id : null;
      L = cmAvail({ data: U.d, upCable: m.upCable });
      const cab = cabs.find((c) => c.id === m.upCable);
      LA = [...new Set([...(U.d.ready || []), ...(cab ? cab.labels : [])])];
      const o = cab ? cab.labels : [];
      LA.sort(
        (a, b) =>
          (o.indexOf(a) < 0 ? 1e6 : o.indexOf(a)) -
          (o.indexOf(b) < 0 ? 1e6 : o.indexOf(b)),
      );
    } else {
      L = U.d.ports || [];
      LA = U.d.all || L;
    }
  }
  const resv = L.length ? mxReserved(i, L) : new Map();
  if (resv.size) L = L.filter((l) => !resv.has(l));
  const R = p.cable.free || [],
    tot = p.cable.total || 0;
  const RA = tot ? Array.from({ length: tot }, (_, k) => coreLabelN(k + 1)) : R;
  const toT = String((p.to && p.to.type) || "").toUpperCase();
  return {
    L,
    LA,
    resv,
    R,
    RA,
    D: (Dn && Dn.d && Dn.d.ports) || [],
    DA: (Dn && Dn.d && Dn.d.all) || (Dn && Dn.d && Dn.d.ports) || [],
    cabs,
    mirror: toT === "CLOSURE" || toT === "SLACK",
    uload: !!(U && U.loading),
    uerr: U && U.err,
    loading: !!((U && U.loading) || (Dn && Dn.loading)),
  };
}
function mxSync(i) {
  const lk = PZ.links[i],
    m = mxS(i);
  lk.cores = m.pairs.length ? m.pairs.map((x) => x.c) : null;
  if (m.pairs.length) lk.n = m.pairs.length;
  pzPreviewSoon();
}
function mxTry(i) {
  const m = mxS(i),
    q = mxLists(i),
    noL = !q.L.length;
  if (m.pR.length && (noL || m.pL.length === m.pR.length)) {
    m.pR.forEach((c, k) =>
      m.pairs.push({ u: noL ? null : m.pL[k], c, d: null }),
    );
    m.pL = [];
    m.pR = [];
    m.err = "";
    mxSync(i);
  }
  pzRender();
}
function mxClick(i, side, label, ev) {
  const m = mxS(i),
    q = mxLists(i),
    arr = side === "L" ? q.L : q.R,
    key = side === "L" ? "pL" : "pR";
  const used = new Set(m.pairs.map((x) => (side === "L" ? x.u : x.c)));
  if (used.has(label)) return;
  const sel = new Set(m[key]),
    ak = "mx" + i + side,
    an = CORE_ANCHOR[ak];
  if (ev && ev.shiftKey && an && arr.includes(an)) {
    const a = arr.indexOf(an),
      b = arr.indexOf(label);
    arr.slice(Math.min(a, b), Math.max(a, b) + 1).forEach((l) => {
      if (!used.has(l)) sel.add(l);
    });
  } else if (sel.has(label)) sel.delete(label);
  else sel.add(label);
  CORE_ANCHOR[ak] = label;
  m[key] = arr.filter((l) => sel.has(l));
  mxTry(i);
}
function mxClickD(i, label) {
  const m = mxS(i),
    has = m.pairs.find((x) => x.d === label);
  if (has) has.d = null;
  else {
    const t = m.pairs.find((x) => !x.d);
    if (!t) return;
    t.d = label;
  }
  pzPreviewSoon();
  pzRender();
}
function mxDel(i, k) {
  const m = mxS(i);
  m.pairs.splice(k, 1);
  mxSync(i);
  pzRender();
}
function mxUpCable(i, v) {
  const m = mxS(i);
  m.upCable = Number(v);
  m.pairs = [];
  m.pL = [];
  m.pR = [];
  mxSync(i);
  pzRender();
}
function mxQuick(i, how) {
  const m = mxS(i),
    q = mxLists(i),
    lk = PZ.links[i],
    noL = !q.L.length;
  m.err = "";
  m.pL = [];
  m.pR = [];
  if (how === "auto") {
    m.pairs = [];
    m.text = "";
    lk.cores = null;
    pzPreviewSoon();
    return pzRender();
  }
  if (how === "text") {
    m.showText = !m.showText;
    return pzRender();
  }
  const lim = noL ? q.R.length : Math.min(q.L.length, q.R.length);
  const n = how === "all" ? lim : Math.min(lim, Math.max(1, lk.n || 1));
  if (!n) {
    m.err = "Tidak ada pasangan yang bisa dibuat (hulu atau core bebas kosong)";
    return pzRender();
  }
  m.pairs = [];
  for (let k = 0; k < n; k++)
    m.pairs.push({
      u: noL ? null : q.L[k],
      c: how === "terbalik" ? q.R[n - 1 - k] : q.R[k],
      d: null,
    });
  mxSync(i);
  pzRender();
}
function mxCount(i, v) {
  const lk = PZ.links[i],
    n = parseInt(v, 10);
  lk.n = n > 0 ? n : null;
  if (!mxS(i).pairs.length) {
    lk.cores = null;
    pzPreviewSoon();
  } else pzRender();
}
function mxApplyText(i) {
  const m = mxS(i),
    q = mxLists(i),
    noL = !q.L.length;
  const parts = String(m.text || "")
    .split(/[,;\n]+/)
    .map((x) => x.trim())
    .filter(Boolean);
  if (!parts.length) {
    m.err = "Tempel daftar, mis. 5→1, 6→2";
    return pzRender();
  }
  const out = [];
  for (const pt of parts) {
    const mm = /^(\d+)\s*(?:(?:→|->|=>|>|=|:)\s*(\d+))?$/.exec(pt);
    if (!mm || (mm[2] == null && !noL)) {
      m.err = `Format tidak dikenali: "${pt}" (contoh 5→1)`;
      return pzRender();
    }
    const cn = Number(mm[2] != null ? mm[2] : mm[1]);
    const c = q.R.find((l) => mxNum(l) === cn);
    if (!c) {
      m.err = `Core ${cn} pada kabel ini tidak bebas/ada`;
      return pzRender();
    }
    let u = null;
    if (!noL) {
      u = q.L.find((l) => mxNum(l) === Number(mm[1]));
      if (!u) {
        m.err = `Hulu ${mm[1]} tidak tersedia`;
        return pzRender();
      }
    }
    if (out.some((x) => x.c === c || (u && x.u === u))) {
      m.err = `Ganda pada "${pt}"`;
      return pzRender();
    }
    out.push({ u, c, d: null });
  }
  m.pairs = out;
  m.pL = [];
  m.pR = [];
  m.err = "";
  mxSync(i);
  pzRender();
}
function mxPayload(i) {
  const lk = PZ.links[i],
    m = lk && lk.mx;
  if (!m || !m.pairs.length) return null;
  const hasU = m.pairs.every((x) => x.u);
  let to = null;
  if (m.pairs.some((x) => x.d)) {
    const q = PZ.prev ? mxLists(i) : { D: [] };
    const taken = new Set(m.pairs.map((x) => x.d).filter(Boolean)),
      spare = q.D.filter((x) => !taken.has(x));
    to = m.pairs.map((x) => x.d || spare.shift());
    if (to.some((x) => !x)) to = null;
  }
  return {
    from: hasU ? m.pairs.map((x) => x.u) : null,
    via: m.pairs.map((x) => x.c),
    to,
    upstream: hasU && m.isJ && m.upCable ? m.upCable : null,
  };
}
function mxColHtml(i, side, title, sub, all, free, m, note, resv) {
  const MAX = 288,
    used = new Map(),
    fr = new Set(free);
  m.pairs.forEach((x, k) => {
    const key = side === "L" ? x.u : x.c;
    if (side === "D") {
      if (x.d) used.set(x.d, k);
    } else if (side === "M") {
      used.set(x.c, k);
    } else if (key) used.set(key, k);
  });
  const pend = new Set(
    side === "L" ? m.pL : side === "R" || side === "M" ? m.pR : [],
  );
  const arr = all.length ? all : free;
  const btn =
    arr
      .slice(0, MAX)
      .map((l) => {
        const k = used.get(l),
          gone = !fr.has(l) && k == null;
        const cls =
          k != null
            ? `used c${k % 8}`
            : gone
              ? "gone"
              : pend.has(l)
                ? "sel"
                : "";
        const fn =
          gone || side === "M"
            ? ""
            : side === "D"
              ? `mxClickD(${i}, '${escapeHtml(l)}')`
              : `mxClick(${i}, '${side}', '${escapeHtml(l)}', event)`;
        return `<button type="button" class="mx-i ${cls}" title="${escapeHtml(l)}${gone ? (resv && resv.has(l) ? " · dipakai sambungan ke " + resv.get(l) : " · sudah terpakai") : ""}" ${gone || side === "M" ? "disabled" : `onclick="${fn}"`}>${escapeHtml(mxShort(l))}${k != null ? `<sup>${k + 1}</sup>` : ""}</button>`;
      })
      .join("") ||
    `<small class="mx-none">${escapeHtml(note || "kosong")}</small>`;
  return `<div class="mx-col"><div class="mx-h">${title}<small>${sub}</small></div><div class="mx-list">${btn}${arr.length > MAX ? `<small class="mx-none">+${arr.length - MAX} lagi</small>` : ""}</div></div>`;
}
function mxHtml(i) {
  const p = PZ.prev && PZ.prev.links[i],
    lk = PZ.links[i];
  if (!p || !lk) return "";
  mxLoad(i);
  const m = mxS(i),
    q = mxLists(i),
    noL = !q.L.length;
  const free = q.R.length;
  let h = `<div class="mx"><div class="mx-top"><b>Matriks sambungan core</b><small>${m.pairs.length ? `${m.pairs.length} pasangan dipilih` : `otomatis: ${fmtInt(p.cores.length)} core pertama`} · ${fmtInt(free)} core bebas${q.loading ? " · memuat hulu…" : ""}</small></div>`;
  if (q.L.length > free && free)
    h += `<div class="mx-hint">Kabel ${escapeHtml(p.cable.capacity || "")} membawa maksimal ${fmtInt(q.RA.length)} core; ${fmtInt(q.L.length - free)} hulu lainnya bisa dipakai cabang lain.</div>`;
  h += `<div class="mx-tools"><button type="button" class="${!m.pairs.length ? "on" : ""}" onclick="mxQuick(${i}, 'auto')">Otomatis</button><button type="button" onclick="mxQuick(${i}, 'lurus')" title="Hulu terkecil ↔ core terkecil">Lurus</button><button type="button" onclick="mxQuick(${i}, 'terbalik')" title="Hulu terkecil ↔ core terbesar">Terbalik</button><button type="button" class="mx-all" onclick="mxQuick(${i}, 'all')"><i class="fa-solid fa-bolt"></i> Sambung semua (${fmtInt(noL ? free : Math.min(q.L.length, free))})</button><button type="button" class="${m.showText ? "on" : ""}" onclick="mxQuick(${i}, 'text')">Tempel daftar</button><label>jumlah <input type="number" min="1" max="${Math.max(1, free)}" value="${lk.n || p.cores.length || 1}" onchange="mxCount(${i}, this.value)"></label></div>`;
  if (q.cabs.length > 1)
    h += `<label class="mx-cab">Kabel hulu <select onchange="mxUpCable(${i}, this.value)">${q.cabs.map((c) => `<option value="${Number(c.id)}" ${c.id === m.upCable ? "selected" : ""}>${escapeHtml(c.name)} (${fmtInt(c.free.length)} bebas)</option>`).join("")}</select></label>`;
  if (m.showText)
    h += `<div class="mx-text"><textarea rows="3" placeholder="${noL ? "Nomor core kabel ini: 1, 2, 3 (atau 5→1, 6→2)" : "Hulu→core kabel ini: 5→1, 6→2"}" oninput="mxS(${i}).text = this.value">${escapeHtml(m.text)}</textarea><button type="button" onclick="mxApplyText(${i})">Pakai daftar</button></div>`;
  if (m.err)
    h += `<div class="pz-note err"><i class="fa-solid fa-circle-xmark"></i> ${escapeHtml(m.err)}</div>`;
  const upNm = escapeHtml(p.from.name),
    dnNm = escapeHtml(p.to.name);
  const three = q.D.length || q.DA.length || q.mirror;
  h += `<div class="mx-cols${three ? " three" : ""}">`;
  const noteL = q.uload
    ? "memuat hulu…"
    : q.uerr
      ? "gagal memuat hulu"
      : q.cabs.length || q.LA.length
        ? "semua core hulu terpakai"
        : "belum ada core hulu: kabel hulu belum diterapkan / tidak ada";
  h += mxColHtml(
    i,
    "L",
    `Hulu · ${upNm}`,
    `${fmtInt(q.L.length)} bebas${q.resv && q.resv.size ? " · " + fmtInt(q.resv.size) + " dipakai cabang lain" : ""}`,
    q.LA,
    q.L,
    m,
    noteL,
    q.resv,
  );
  h += mxColHtml(
    i,
    "R",
    `Kabel · ${escapeHtml(p.cable.name)}`,
    `${fmtInt(free)} bebas dari ${fmtInt(q.RA.length)}`,
    q.RA,
    q.R,
    m,
  );
  if (q.D.length || q.DA.length)
    h += mxColHtml(
      i,
      "D",
      `Hilir · ${dnNm}`,
      `${fmtInt(q.D.length)} port bebas`,
      q.DA,
      q.D,
      m,
    );
  else if (q.mirror)
    h += mxColHtml(
      i,
      "M",
      `Hilir · ${dnNm}`,
      "core masuk = core kabel",
      q.RA,
      q.R,
      m,
      "pilih core pada kolom kabel",
    );
  h += `</div>`;
  if (m.pairs.length)
    h += `<div class="mx-pairs">${m.pairs.map((x, k) => `<span class="mx-pair c${k % 8}">${k + 1}. ${x.u ? escapeHtml(mxShort(x.u)) + " → " : ""}${escapeHtml(mxShort(x.c))}${x.d ? " → " + escapeHtml(x.d) : ""}<button type="button" title="Hapus pasangan" onclick="mxDel(${i}, ${k})">&times;</button></span>`).join("")}</div>`;
  h += `<div class="mx-hint">Klik ${noL ? "core kabel" : "hulu lalu core kabel"} untuk memasangkan${q.D.length ? ", lalu klik port hilir untuk menempelkannya ke pasangan" : q.mirror ? "; core yang masuk ke closure berikutnya sama dengan core kabel ini" : ""}. Abu-abu = sudah terpakai. Shift+klik = rentang (jumlah hulu dan core harus sama).</div></div>`;
  return h;
}
function pzNodeHtml(i) {
  const n = PZ.chain[i],
    t = String(n.type).toUpperCase();
  const leaf = i === PZ.chain.length - 1 && i > 0 && !pzIsDone(i);
  return (
    `<div class="pz-node t-${escapeHtml(t)} ${pzCur() === i ? "cur" : ""} ${pzIsDone(i) ? "done" : ""}"><span class="pz-ic t-${escapeHtml(t)}">${mapGlyph(t, 16)}</span><span class="pz-nn">${escapeHtml(n.name)}<small>${escapeHtml(n.type)}</small></span>` +
    (t === "PELANGGAN"
      ? ""
      : `<button type="button" class="pz-br" title="Tambah cabang / lanjut dari sini" onclick="pzBranch(${i})"><i class="fa-solid fa-code-branch"></i></button>`) +
    (leaf
      ? `<button type="button" class="pz-x" title="Lepas blok terakhir" onclick="pzRemoveLast()">&times;</button>`
      : "") +
    `</div>`
  );
}
function pzTreeHtml() {
  if (!PZ.chain.length) return "";
  const kids = PZ.chain.map(() => []);
  PZ.par.forEach((pi, k) => {
    if (kids[pi]) kids[pi].push(k + 1);
  });
  const rows = [],
    root = [pzNodeHtml(0)];
  rows.push(root);
  const walk = (i, row) =>
    kids[i].forEach((c, k) => {
      let r = row;
      if (k > 0) {
        r = [
          `<span class="pz-fork" title="cabang dari ${escapeHtml(PZ.chain[i].name)}"><i class="fa-solid fa-code-branch"></i> ${escapeHtml(PZ.chain[i].name)}</span>`,
        ];
        rows.push(r);
      }
      r.push(pzLinkHtml(c - 1));
      r.push(pzNodeHtml(c));
      walk(c, r);
    });
  walk(0, root);
  return rows.map((r) => `<div class="pz-chain">${r.join("")}</div>`).join("");
}
function pzRender() {
  const el = $id("pz-main");
  if (!el || TF.tab !== "puzzle") return;
  let h = `<div class="tf-intro">Rangkai seperti puzzle: pilih aset awal (biasanya POP), lalu tambahkan aset berikutnya satu per satu. Di antara dua aset, kabel yang sudah ada dipakai; bila belum ada, <b>kabel baru dibuat otomatis</b> (garis putus-putus) beserta sambungan core-nya. Klik kabel di antara dua aset untuk membuka matriks sambungan core (hulu → core kabel → port hilir).</div>`;
  h += pzTreeHtml();
  h += `<div class="pz-chain"><button type="button" class="pz-add ${PZ.pick.open ? "on" : ""}" onclick="${PZ.pick.open ? "pzClosePicker()" : "pzOpenPicker()"}"><i class="fa-solid fa-plus"></i> ${PZ.chain.length ? `pilih aset berikutnya dari <b>${escapeHtml(PZ.chain[pzCur()].name)}</b>` : "pilih aset awal"}</button></div>`;
  if (PZ.pick.open)
    h += `<div class="pz-pick"><input type="text" id="pz-pick-q" value="${escapeHtml(PZ.pick.q)}" placeholder="${PZ.chain.length ? "Cari nama aset (yang punya kabel lalu terdekat dari blok terakhir tampil lebih dulu)…" : "Cari nama aset awal (terdekat dari pusat peta tampil lebih dulu)…"}" oninput="pzPickQ(this.value)" autocomplete="off"><div id="pz-pick-list" class="pz-pick-list">${pzPickHtml()}</div></div>`;
  if (PZ.sel != null && !PZ.pick.open) h += pzEditorHtml(PZ.sel);
  if (PZ.prev) {
    const n = PZ.prev.links.filter((x) => !x.done).length,
      nw = PZ.prev.new_cables;
    h +=
      `<div class="pz-sum"><span><b>${n}</b> sambungan · <b>${n - nw}</b> kabel dipakai · <b class="pz-newc">${nw}</b> kabel baru</span>` +
      `<button type="button" class="data-primary" ${PZ.prev.ready && !PZ.busy && n > 0 ? "" : "disabled"} onclick="pzApply()"><i class="fa-solid fa-puzzle-piece"></i> Terapkan rantai</button></div>`;
    if (!PZ.prev.ready)
      h += `<div class="pz-note err"><i class="fa-solid fa-circle-xmark"></i> Ada masalah pada rantai; klik kabel bertanda segitiga untuk memperbaikinya.</div>`;
  }
  if (PZ.last)
    h += `<div class="pz-done"><i class="fa-solid fa-circle-check"></i> ${escapeHtml(PZ.last.message)}${PZ.last.results.map((r) => `<div>${r.new ? "kabel baru" : "kabel"} <b>${escapeHtml(r.name)}</b>: ${fmtInt(r.links)} sambungan</div>`).join("")}<div class="adm-hint">Rantai tetap tampil: klik ikon cabang pada blok mana pun untuk melanjutkan, atau "Ulangi" untuk rantai baru.</div></div>`;
  if (PZ.chain.length)
    h += `<div class="pz-foot"><button type="button" class="data-ghost" onclick="pzReset()"><i class="fa-solid fa-rotate-left"></i> Ulangi dari awal</button></div>`;
  el.innerHTML = h;
  const q = $id("pz-pick-q");
  if (q && document.activeElement !== q && PZ.pick.open) {
    try {
      q.focus();
    } catch (_) {
      /* abaikan */
    }
  }
  try {
    pzMapInit();
    pzMapRender();
  } catch (_) {
    /* minimap opsional */
  }
}
async function pzApply() {
  if (PZ.busy || !PZ.prev || !PZ.prev.ready) return;
  const nw = PZ.prev.new_cables,
    nl = PZ.prev.links.filter((x) => !x.done).length;
  if (!nl) return;
  if (
    !confirm(
      `Terapkan ${nl} sambungan baru?\n${nw} kabel baru akan dibuat dan core disambung. Tercatat di riwayat.`,
    )
  )
    return;
  PZ.busy = true;
  pzRender();
  const body = {
    chain: PZ.chain.map((n) => n.id),
    parents: PZ.par.slice(),
    links: PZ.prev.links.map((p, i) => {
      const lk = PZ.links[i];
      if (lk.done) return { cable_id: lk.cable_id, done: true };
      const mp = mxPayload(i);
      const ex = mp
        ? {
            from_cores: mp.from,
            to_ports: mp.to,
            upstream_cable_id: mp.upstream,
          }
        : {};
      const cores = mp ? mp.via : p.cores;
      return lk.cable_id
        ? { cable_id: lk.cable_id, cores, ...ex }
        : {
            new: {
              name: p.cable.name,
              type: p.cable.type,
              capacity: p.cable.capacity,
              installation: p.cable.installation,
            },
            cores,
            ...ex,
          };
    }),
    batch: "PZ" + Date.now().toString(36),
  };
  try {
    const r = await apiRequest("/api/topofix/puzzle/apply", "POST", body);
    PZ.last = r;
    (r.results || []).forEach((x) => {
      const lk = PZ.links[x.index],
        pl = PZ.prev.links[x.index];
      if (lk) {
        lk.cable_id = x.cable_id;
        lk.done = true;
        lk.dn = {
          name: x.name,
          capacity: pl && pl.cable ? pl.cable.capacity : "",
          installation: pl && pl.cable ? pl.cable.installation : "",
          length_m: pl && pl.cable ? pl.cable.length_m : 0,
        };
        lk.cores = null;
        lk.new = pzNew();
      }
    });
    TOPO.dirty = true;
    PZ.doneN = PZ.chain.length;
    PZ.cur = PZ.chain.length - 1;
    PZ.prev = null;
    PZ.sel = null;
    PZ.pick.open = false;
    Object.keys(CM).forEach((k) => delete CM[k]);
    Object.keys(MXC).forEach((k) => delete MXC[k]);
    Object.keys(TF.data).forEach((k) => {
      TF.data[k] = null;
    });
    tfSetMsg(r.message, "ok");
    PZM.cabVer = (PZM.cabVer || 0) + 1;
    Promise.resolve(loadData())
      .then(() => {
        PZM.cabVer++;
        try {
          pzMapCables(true);
        } catch (_) {
          /* abaikan */
        }
      })
      .catch(() => {
        /* peta disegarkan berikutnya */
      }); // tidak memblokir UI
    await pzPreview();
  } catch (err) {
    tfSetMsg("Gagal menerapkan: " + err.message, "err");
  }
  PZ.busy = false;
  pzRender();
}
function pzOpen() {
  if (!PZ.chain.length) {
    PZ.pick.open = true;
    pzLoadPick();
  }
  pzRender();
  try {
    setTimeout(() => {
      if (PZM.map) {
        PZM.map.invalidateSize();
        pzMapBg();
      }
    }, 80);
  } catch (_) {
    /* abaikan */
  }
}

// ===== Pilih core tertentu per kabel (tab Sambung core) =====
function tfCoreCell(x) {
  const cs = TF.cores[x.cable_id],
    n = cs && cs.chosen && cs.chosen.length ? cs.chosen.length : null;
  const label = n ? `${n} dipilih` : x.full ? "penuh" : String(x.cores);
  if (x.state !== "siap") return escapeHtml(label);
  const key = "tf:" + Number(x.cable_id);
  return (
    `${escapeHtml(label)}<br><button type="button" class="tf-corebtn" onclick="tfPickCores(${Number(x.cable_id)})">pilih core</button>` +
    (cs && cs.open
      ? `<div class="pz-core-grid tf-coregrid">${cs.free
          .slice(0, 288)
          .map(
            (l) =>
              `<button type="button" class="pz-core ${cs.chosen.includes(l) ? "on" : ""}" title="${escapeHtml(l)}" onclick="coreClick('${key}', '${escapeHtml(l)}', event)">${escapeHtml(pzShort(l))}</button>`,
          )
          .join(
            "",
          )}${cs.free.length ? "" : "<small>tidak ada core bebas</small>"}</div>${coreToolsHtml(key, cs.free.length)}${coreMapHtml(key)}`
      : "")
  );
}
async function tfPickCores(cid) {
  let cs = TF.cores[cid];
  if (cs && cs.free) {
    cs.open = !cs.open;
    return tfRenderBody();
  }
  try {
    const r = await apiRequest(`/api/cables/${cid}/core-usage`);
    TF.cores[cid] = {
      free: (r.cores || []).filter((c) => !c.used).map((c) => c.core),
      chosen: [],
      open: true,
    };
  } catch (err) {
    tfSetMsg("Gagal memuat core: " + err.message, "err");
    return;
  }
  tfRenderBody();
}

// ===== Pilihan core bersama (Sambung core + Puzzle): Shift = rentang, Sambung semua, pemetaan hulu -> hilir =====
const CORE_ANCHOR = {};
const CM = {}; // kunci "tf:<kabel>" / "pz:<indeks>" -> { open, data, mode, upCable, upChosen, text }
function coreLabelN(i) {
  return `Tube ${Math.floor((i - 1) / 12) + 1} - Core ${i}`;
}
function coreCtx(key) {
  const [k, id] = String(key).split(":");
  if (k === "tf") {
    const cs = TF.cores[id];
    if (!cs) return null;
    return {
      free: cs.free,
      chosen: cs.chosen,
      set: (a) => {
        cs.chosen = a;
      },
      refresh: tfRenderBody,
    };
  }
  const i = Number(id),
    lk = PZ.links[i],
    p = PZ.prev && PZ.prev.links[i];
  if (!lk || !p) return null;
  return {
    free: p.cable.free,
    chosen: lk.cores && lk.cores.length ? lk.cores : p.cores,
    set: (a) => {
      lk.cores = a.length ? a : null;
      lk.n = a.length || null;
    },
    refresh: pzPreviewSoon,
  };
}
function coreClick(key, label, ev) {
  const c = coreCtx(key);
  if (!c) return;
  const set = new Set(c.chosen),
    an = CORE_ANCHOR[key];
  if (ev && ev.shiftKey && an && c.free.includes(an)) {
    const a = c.free.indexOf(an),
      b = c.free.indexOf(label);
    c.free.slice(Math.min(a, b), Math.max(a, b) + 1).forEach((l) => set.add(l));
  } else if (set.has(label)) set.delete(label);
  else set.add(label);
  CORE_ANCHOR[key] = label;
  c.set(c.free.filter((l) => set.has(l)));
  c.refresh();
}
function coreBulk(key, how) {
  const c = coreCtx(key);
  if (!c) return;
  const cur = new Set(c.chosen);
  c.set(
    c.free.filter((l) =>
      how === "all" ? true : how === "inv" ? !cur.has(l) : false,
    ),
  );
  CORE_ANCHOR[key] = null;
  c.refresh();
}
function coreToolsHtml(key, nFree) {
  if (!nFree) return "";
  return (
    `<div class="core-tools"><button type="button" class="core-all" onclick="coreBulk('${key}', 'all')" title="Pilih semua core bebas lalu klik Terapkan untuk menyambung semuanya"><i class="fa-solid fa-bolt"></i> Sambung semua (${fmtInt(nFree)})</button>` +
    `<button type="button" onclick="coreBulk('${key}', 'none')">${String(key).startsWith("pz:") ? "Reset otomatis" : "Kosongkan"}</button><button type="button" onclick="coreBulk('${key}', 'inv')">Balik pilihan</button><small>Klik = pilih/lepas &middot; Shift+klik = rentang</small></div>`
  );
}
function cmState(key) {
  return (
    CM[key] ||
    (CM[key] = {
      open: false,
      data: null,
      mode: "auto",
      upCable: null,
      upChosen: [],
      text: "",
      loading: false,
      err: "",
    })
  );
}
function cmTarget(key) {
  const [k, id] = String(key).split(":");
  if (k === "tf") {
    const x = ((TF.data.conn && TF.data.conn.rows) || []).find(
      (r) => String(r.cable_id) === id,
    );
    return x ? { node: x.up_id, cable: x.cable_id } : null;
  }
  const i = Number(id),
    p = PZ.prev && PZ.prev.links[i],
    lk = PZ.links[i];
  return p && p.from
    ? { node: p.from.id, cable: lk && lk.cable_id ? lk.cable_id : null }
    : null;
}
function cmRefresh(key) {
  if (String(key).startsWith("tf:")) tfRenderBody();
  else {
    pzRender();
    pzPreviewSoon();
  }
}
async function cmToggle(key) {
  const m = cmState(key);
  m.open = !m.open;
  if (m.open && !m.data) {
    const t = cmTarget(key);
    if (!t) {
      m.open = false;
      return;
    }
    m.loading = true;
    cmRefresh(key);
    try {
      m.data = await apiRequest(
        `/api/topofix/upstream?node_id=${Number(t.node)}${t.cable ? "&exclude_cable=" + Number(t.cable) : ""}`,
      );
      if (m.data.cables.length) m.upCable = m.data.cables[0].id;
    } catch (err) {
      m.err = err.message;
    }
    m.loading = false;
  }
  cmRefresh(key);
}
function cmSet(key, field, v) {
  const m = cmState(key);
  m[field] = v;
  if (field === "mode" && v !== "auto") m.err = "";
  if (field === "upCable") {
    m.upChosen = [];
    m.upCable = Number(v);
  }
  cmRefresh(key);
}
function cmAvail(m) {
  if (!m.data || !m.data.junction) return [];
  const cab = m.data.cables.find((c) => c.id === m.upCable);
  const set = new Set([...(m.data.ready || []), ...(cab ? cab.free : [])]);
  const order = cab ? cab.labels : [];
  return [...set].sort(
    (a, b) =>
      (order.indexOf(a) < 0 ? 1e6 : order.indexOf(a)) -
      (order.indexOf(b) < 0 ? 1e6 : order.indexOf(b)),
  );
}
function cmUpClick(key, label, ev) {
  const m = cmState(key),
    av = cmAvail(m),
    set = new Set(m.upChosen),
    an = CORE_ANCHOR["up" + key];
  if (ev && ev.shiftKey && an && av.includes(an)) {
    const a = av.indexOf(an),
      b = av.indexOf(label);
    av.slice(Math.min(a, b), Math.max(a, b) + 1).forEach((l) => set.add(l));
  } else if (set.has(label)) set.delete(label);
  else set.add(label);
  CORE_ANCHOR["up" + key] = label;
  m.upChosen = av.filter((l) => set.has(l));
  cmRefresh(key);
}
// "5→1, 6→2" (nomor urut core hulu → nomor urut core kabel ini). Pemisah: → -> => > = :
function cmParse(text, avail, downFree) {
  const parts = String(text || "")
    .split(/[,;\n]+/)
    .map((x) => x.trim())
    .filter(Boolean);
  if (!parts.length) return { err: "Tempel daftar pemetaan, mis. 5→1, 6→2" };
  const from = [],
    via = [];
  for (const pt of parts) {
    const mm = /^(\d+)\s*(?:→|->|=>|>|=|:)\s*(\d+)$/.exec(pt);
    if (!mm) return { err: `Format tidak dikenali: "${pt}" (contoh 5→1)` };
    const u = coreLabelN(Number(mm[1])),
      d = coreLabelN(Number(mm[2]));
    if (!avail.includes(u))
      return { err: `Core hulu ${mm[1]} tidak tersedia di aset hulu` };
    if (!downFree.includes(d))
      return { err: `Core ${mm[2]} pada kabel ini tidak bebas/ada` };
    if (from.includes(u) || via.includes(d))
      return { err: `Core ganda pada "${pt}"` };
    from.push(u);
    via.push(d);
  }
  return { from, via };
}
function cmPairs(key) {
  const m = CM[key];
  if (!m || !m.open || m.mode === "auto" || !m.data || !m.data.junction)
    return null;
  const c = coreCtx(key);
  if (!c) return null;
  const av = cmAvail(m);
  if (m.mode === "manual") return cmParse(m.text, av, c.free);
  const n = c.chosen.length;
  if (!n)
    return {
      err: "Pilih core kabel ini terlebih dulu (atau tekan Sambung semua)",
    };
  const ups = m.upChosen.filter((l) => av.includes(l));
  if (ups.length !== n)
    return { err: `Pilih ${n} core hulu (terpilih ${ups.length})` };
  return {
    from: m.mode === "terbalik" ? ups.slice().reverse() : ups,
    via: c.chosen.slice(),
  };
}
function coreMapPayload(key) {
  const r = cmPairs(key);
  if (!r || r.err) return null;
  return { from: r.from, via: r.via, upstream: cmState(key).upCable || null };
}
function coreMapHtml(key) {
  const t = cmTarget(key);
  if (!t) return "";
  const m = cmState(key);
  if (m.data && !m.data.junction) return "";
  let h = `<div class="cm-box"><button type="button" class="cm-toggle ${m.open ? "on" : ""}" onclick="cmToggle('${key}')"><i class="fa-solid fa-shuffle"></i> Pemetaan core hulu → hilir${m.mode !== "auto" && m.open ? " (aktif)" : ""}</button>`;
  if (m.open) {
    if (m.loading) h += `<div class="cm-note">memuat core hulu…</div>`;
    else if (m.err) h += `<div class="cm-note err">${escapeHtml(m.err)}</div>`;
    else if (m.data && !m.data.junction)
      h += `<div class="cm-note">Aset hulu bukan closure/slack; pemetaan hanya untuk sambungan di closure.</div>`;
    else if (m.data) {
      const av = cmAvail(m);
      h += `<div class="cm-modes">${[
        ["auto", "Otomatis"],
        ["lurus", "Lurus"],
        ["terbalik", "Terbalik"],
        ["manual", "Manual (tempel daftar)"],
      ]
        .map(
          ([v, l]) =>
            `<label><input type="radio" name="cm-${key}" ${m.mode === v ? "checked" : ""} onchange="cmSet('${key}', 'mode', '${v}')"> ${l}</label>`,
        )
        .join("")}</div>`;
      if (m.mode !== "auto") {
        if (m.data.cables.length > 1)
          h += `<label class="cm-cab">Kabel hulu <select onchange="cmSet('${key}', 'upCable', this.value)">${m.data.cables.map((c) => `<option value="${Number(c.id)}" ${c.id === m.upCable ? "selected" : ""}>${escapeHtml(c.name)} (${fmtInt(c.free.length)} bebas)</option>`).join("")}</select></label>`;
        else if (m.data.cables.length === 1)
          h += `<div class="cm-note">Kabel hulu: <b>${escapeHtml(m.data.cables[0].name)}</b></div>`;
        if (m.mode === "manual") {
          h += `<textarea class="cm-text" rows="3" placeholder="Tempel daftar: 5→1, 6→2 (nomor core hulu → nomor core kabel ini)" oninput="CM['${key}'].text = this.value" onchange="cmSet('${key}', 'text', this.value)">${escapeHtml(m.text)}</textarea>`;
        } else {
          h += `<div class="cm-sub">Core hulu yang dipakai <small>(${m.mode === "lurus" ? "urut naik: terkecil ↔ terkecil" : "terbalik: terkecil hulu ↔ terbesar hilir"})</small></div><div class="pz-core-grid">${
            av
              .slice(0, 288)
              .map(
                (l) =>
                  `<button type="button" class="pz-core ${m.upChosen.includes(l) ? "on" : ""}" title="${escapeHtml(l)}" onclick="cmUpClick('${key}', '${escapeHtml(l)}', event)">${escapeHtml(pzShort(l))}</button>`,
              )
              .join("") || "<small>tidak ada core hulu tersedia</small>"
          }</div>`;
        }
        const r = cmPairs(key);
        if (r && r.err)
          h += `<div class="cm-note err">${escapeHtml(r.err)}</div>`;
        else if (r)
          h += `<div class="cm-pairs">${r.from.map((u, i) => `<span>${escapeHtml(pzShort(u))} → ${escapeHtml(pzShort(r.via[i]))}</span>`).join("")}</div>`;
      } else
        h += `<div class="cm-note">Otomatis: core hulu terendah ↔ core kabel ini terendah (urut lurus).</div>`;
    }
  }
  return h + `</div>`;
}

// ===== TOPOLOGI: diagram pohon dari POP; ikut berubah mengikuti puzzle; klik aset/kabel untuk edit, ikon X untuk hapus =====
const TOPO = {
  data: null,
  root: null,
  sel: null,
  tbl: null,
  loading: false,
  dirty: true,
  msg: "",
  zoom: 1,
  seq: 0,
  limit: 250,
};
const TP = { COLW: 340, ROW: 64, PAD: 56 };
const TP_CABCOL = {
  Backbone: "#2638dc",
  Feeder: "#ca8a04",
  Distribution: "#0d9488",
  Drop: "#db2777",
};
function topoOpen() {
  const root = (PZ.chain[0] && PZ.chain[0].id) || TOPO.root;
  if (root && (root !== TOPO.req || TOPO.dirty || !TOPO.data)) {
    TOPO.req = root;
    TOPO.root = root;
    topoLoad();
    return;
  }
  topoRender();
}
async function topoLoad() {
  const seq = ++TOPO.seq;
  TOPO.loading = true;
  TOPO.msg = "";
  topoRender();
  try {
    const d = await apiRequest(
      `/api/topofix/topology?root=${Number(TOPO.req || TOPO.root)}&limit=${TOPO.limit}`,
    );
    if (seq !== TOPO.seq) return;
    TOPO.data = d;
    TOPO.dirty = false;
    TOPO.root = d.root; // akar dinaikkan ke POP agar terbaca hulu (kiri) → hilir (kanan)
  } catch (err) {
    TOPO.data = null;
    TOPO.msg = err.message;
  }
  TOPO.loading = false;
  topoRender();
}
function topoSetRoot(v) {
  TOPO.root = Number(v) || null;
  TOPO.req = TOPO.root;
  TOPO.sel = null;
  TOPO.tbl = null;
  TOPO.dirty = true;
  if (TOPO.root) topoLoad();
  else topoRender();
}
function topoMore() {
  TOPO.limit = Math.min(800, TOPO.limit + 200);
  TOPO.dirty = true;
  topoLoad();
}
function topoZoom(f) {
  TOPO.zoom = f === 0 ? 1 : Math.max(0.4, Math.min(2, TOPO.zoom * f));
  topoRender();
}
// graf = data server + sambungan puzzle yang belum diterapkan (garis putus-putus)
function topoGraph() {
  const d = TOPO.data;
  if (!d) return null;
  const nodes = new Map(d.nodes.map((n) => [n.id, { ...n, virtual: false }]));
  const cabs = d.cables.map((c) => ({ ...c }));
  const have = new Set(
    cabs
      .map((c) => c.from + ":" + c.to)
      .concat(cabs.map((c) => c.to + ":" + c.from)),
  );
  PZ.links.forEach((lk, i) => {
    const a = PZ.chain[PZ.par[i]],
      b = PZ.chain[i + 1];
    if (!a || !b || lk.done || have.has(a.id + ":" + b.id)) return;
    const A = nodes.get(a.id);
    if (!A) return;
    const pl = PZ.prev && PZ.prev.links[i],
      cb = pl && pl.cable;
    if (!nodes.has(b.id))
      nodes.set(b.id, {
        id: b.id,
        name: b.name,
        type: b.type,
        status: "-",
        depth: A.depth + 1,
        parent: a.id,
        virtual: true,
        lat: b.lat,
        lng: b.lng,
      });
    cabs.push({
      id: "v" + i,
      virtual: true,
      name: cb ? cb.name : "kabel baru",
      type: cb ? cb.type : "",
      capacity: cb ? cb.capacity : "",
      from: a.id,
      to: b.id,
      used: 0,
      total: cb ? cb.total : 0,
    });
  });
  const kids = new Map();
  nodes.forEach((n) => {
    if (n.parent != null && nodes.has(n.parent)) {
      if (!kids.has(n.parent)) kids.set(n.parent, []);
      kids.get(n.parent).push(n.id);
    }
  });
  const pos = new Map();
  let leaf = 0,
    maxD = 0;
  const stack = [[d.root, 0, false]];
  // DFS tanpa rekursi: y = rata-rata anak, daun diberi baris berurutan
  const order = [];
  const st = [d.root],
    dep = new Map([[d.root, 0]]);
  while (st.length) {
    const x = st.pop();
    order.push(x);
    (kids.get(x) || [])
      .slice()
      .reverse()
      .forEach((c) => {
        dep.set(c, dep.get(x) + 1);
        st.push(c);
      });
  }
  for (let i = order.length - 1; i >= 0; i--) {
    const x = order[i],
      ks = kids.get(x) || [];
    maxD = Math.max(maxD, dep.get(x));
    if (!ks.length) pos.set(x, { y: 0, leaf: true });
  }
  order.forEach((x) => {
    const p = pos.get(x);
    if (p && p.leaf) p.y = TP.PAD + leaf++ * TP.ROW;
  });
  for (let i = order.length - 1; i >= 0; i--) {
    const x = order[i],
      ks = kids.get(x) || [];
    if (ks.length)
      pos.set(x, { y: ks.reduce((s, c) => s + pos.get(c).y, 0) / ks.length });
  }
  order.forEach((x) => {
    const p = pos.get(x);
    p.x = TP.PAD + dep.get(x) * TP.COLW;
  });
  return {
    nodes,
    cabs,
    kids,
    pos,
    order,
    W: TP.PAD * 2 + maxD * TP.COLW + 120,
    H: TP.PAD * 2 + Math.max(0, leaf - 1) * TP.ROW + 30,
  };
}
function topoNodeClr(t) {
  t = String(t).toUpperCase();
  return t === "SLACK"
    ? ["#111827", "#facc15"]
    : [(NODE_TYPE_META[t] || { color: "#64748b" }).color, "#fff"];
}
function topoTrunc(s, n) {
  s = String(s || "");
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}
function topoSvg(g) {
  const chainIdx = new Map(PZ.chain.map((n, i) => [n.id, i]));
  let sv = "";
  // kabel (garis siku antar induk dan anak)
  const cabOf = new Map();
  g.cabs.forEach((c) => {
    cabOf.set(c.from + ":" + c.to, c);
    cabOf.set(c.to + ":" + c.from, c);
  });
  g.order.forEach((id) => {
    const n = g.nodes.get(id);
    if (!n || n.parent == null || !g.pos.has(n.parent)) return;
    const a = g.pos.get(n.parent),
      b = g.pos.get(id),
      c = cabOf.get(n.parent + ":" + id);
    const x1 = a.x + 20,
      x2 = b.x - 20,
      xm = (x1 + x2) / 2;
    const col =
      c && c.virtual ? "#7c3aed" : TP_CABCOL[c && c.type] || "#64748b";
    const sel =
      TOPO.sel &&
      TOPO.sel.k === "cable" &&
      c &&
      String(TOPO.sel.id) === String(c.id);
    sv += `<g class="tp-e ${sel ? "sel" : ""}" ${c ? `data-e="${escapeHtml(String(c.id))}"` : ""} ${c ? `onclick="topoSel('cable', '${escapeHtml(String(c.id))}')"` : ""}><path d="M${x1} ${a.y} H${xm} V${b.y} H${x2}" fill="none" stroke="${col}" stroke-width="2.5" ${c && c.virtual ? 'stroke-dasharray="6 5"' : ""}/>`;
    if (c) {
      const lx = xm + 24,
        tx = `${topoTrunc(c.name, 20)}`,
        cap = c.virtual ? "baru" : `${c.used}/${c.total} core`;
      sv += `<text class="tp-cl" x="${lx}" y="${b.y - 6}" text-anchor="start">${escapeHtml(tx)}</text><text class="tp-cs" x="${lx}" y="${b.y + 9}" text-anchor="start">${escapeHtml(c.capacity || "")} · ${escapeHtml(cap)}</text>`;
      if (!c.virtual && can("asset.delete"))
        sv += `<g class="tp-x" transform="translate(${xm + 11} ${b.y - 12})" onclick="event.stopPropagation(); topoDel('cable', ${Number(c.id)})"><title>Hapus kabel</title><circle r="7"/><path d="M-2.6 -2.6L2.6 2.6M2.6 -2.6L-2.6 2.6"/></g>`;
    }
    sv += `</g>`;
  });
  g.order.forEach((id) => {
    const n = g.nodes.get(id),
      p = g.pos.get(id),
      t = String(n.type).toUpperCase();
    const [bg, fg] = topoNodeClr(t),
      sel = TOPO.sel && TOPO.sel.k === "node" && TOPO.sel.id === id,
      ci = chainIdx.get(id);
    sv +=
      `<g class="tp-n ${sel ? "sel" : ""} ${n.virtual ? "virt" : ""}" data-n="${Number(id)}" transform="translate(${p.x} ${p.y})" onclick="topoSel('node', ${Number(id)})">` +
      `${ci != null ? `<circle r="25" class="tp-ring"/>` : ""}<circle r="19" fill="${bg}" stroke="#fff" stroke-width="2"/>` +
      `<svg x="-10" y="-10" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="${fg}" style="color:${fg}" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">${MAP_GLYPH[t] || MAP_GLYPH.ODP}</svg>` +
      `<text class="tp-nl" y="35" text-anchor="middle">${escapeHtml(topoTrunc(n.name, 24))}</text>` +
      `${n.pt ? `<text class="tp-cs" y="48" text-anchor="middle">${n.pt.kind === "otb" ? "OTB " + n.pt.used + "/" + n.pt.total + " port" : n.pt.used + "/" + n.pt.total + " core diteruskan"}</text>` : ""}` +
      `${ci != null ? `<g transform="translate(-16 -16)"><circle r="8" fill="#0f172a" stroke="#fff"/><text class="tp-bn" y="3.5" text-anchor="middle">${ci + 1}</text></g>` : ""}` +
      `${can("asset.delete") && !n.virtual && id !== TOPO.root ? `<g class="tp-x" transform="translate(16 -16)" onclick="event.stopPropagation(); topoDel('node', ${Number(id)})"><title>Hapus aset</title><circle r="8"/><path d="M-3 -3L3 3M3 -3L-3 3"/></g>` : ""}</g>`;
  });
  return sv;
}
function topoRender() {
  const box = $id("tf-topo");
  if (!box || TF.tab !== "topo") return;
  const pops =
    typeof allInventoryData !== "undefined"
      ? allInventoryData.filter(
          (x) =>
            x.category === "NODE" && String(x.type).toUpperCase() === "POP",
        )
      : [];
  let h =
    `<div class="tp-bar"><label>Mulai dari POP <select onchange="topoSetRoot(this.value)"><option value="">- pilih -</option>${pops.map((x) => `<option value="${Number(x.id)}" ${Number(x.id) === TOPO.root ? "selected" : ""}>${escapeHtml(x.name)}</option>`).join("")}</select></label>` +
    `<button type="button" class="tf-corebtn" onclick="TOPO.dirty = true; topoOpen()"><i class="fa-solid fa-rotate"></i> Muat ulang</button>` +
    `<span class="tp-zoom"><button type="button" onclick="topoZoom(0.8)">−</button><button type="button" onclick="topoZoom(0)">100%</button><button type="button" onclick="topoZoom(1.25)">+</button></span>` +
    `<span class="tp-leg"><i style="background:#16a34a"></i>sudah ada <i class="v"></i>puzzle (belum diterapkan) <i class="r"></i>rantai puzzle</span></div>`;
  if (TOPO.msg)
    h += `<div class="pz-note err"><i class="fa-solid fa-circle-xmark"></i> ${escapeHtml(TOPO.msg)}</div>`;
  if (!TOPO.root)
    h += `<div class="pz-empty">Pilih POP atau mulai merangkai di tab Rangkai; diagram akan mengikuti rantai Anda.</div>`;
  else if (TOPO.loading && !TOPO.data)
    h += `<div class="pz-empty">Memuat topologi…</div>`;
  else if (TOPO.data) {
    const g = topoGraph(),
      z = TOPO.zoom;
    TOPO.g = g;
    h +=
      `<div class="tp-main"><div class="tp-canvas"><svg class="tp-svg" width="${Math.round(g.W * z)}" height="${Math.round(g.H * z)}" viewBox="0 0 ${g.W} ${g.H}">${topoSvg(g)}</svg></div><div class="tp-panel" id="tp-panel">${topoPanelHtml(g)}</div></div>` +
      `<div class="tp-foot">${g.nodes.size} aset · ${g.cabs.length} kabel${TOPO.data.truncated ? ` · dibatasi ${TOPO.limit} aset <button type="button" class="tf-corebtn" onclick="topoMore()">tampilkan lebih banyak</button>` : ""}</div>`;
  }
  box.innerHTML = h;
}
// pilih aset/kabel: hanya sorotan dan panel yang diperbarui (diagram besar tidak digambar ulang)
function topoPaint() {
  const box = $id("tf-topo");
  if (!box) return;
  box
    .querySelectorAll(".tp-n.sel, .tp-e.sel")
    .forEach((e) => e.classList.remove("sel"));
  const sel = TOPO.sel;
  if (sel) {
    const e = box.querySelector(
      sel.k === "node"
        ? `.tp-n[data-n="${sel.id}"]`
        : `.tp-e[data-e="${String(sel.id).replace(/"/g, "")}"]`,
    );
    if (e) e.classList.add("sel");
  }
  const pn = $id("tp-panel");
  if (pn && TOPO.g) pn.innerHTML = topoPanelHtml(TOPO.g);
}
async function topoSel(k, id) {
  TOPO.sel = { k, id: k === "node" ? Number(id) : String(id) };
  TOPO.tbl = null;
  TOPO.pmsg = "";
  topoPaint();
  if (k === "cable" && String(id).startsWith("v")) return;
  const key = k + ":" + id;
  try {
    const r = await apiRequest(
      `/api/topofix/core-table?${k === "node" ? "node_id" : "cable_id"}=${Number(id)}`,
    );
    if (!TOPO.sel || TOPO.sel.k + ":" + TOPO.sel.id !== key) return;
    TOPO.tbl = { key, rows: r.rows, truncated: r.truncated };
  } catch (err) {
    TOPO.tbl = { key, rows: [], err: err.message };
  }
  topoPaint();
}
function topoPanelHtml(g) {
  const sel = TOPO.sel;
  if (!sel)
    return `<div class="tp-hint"><b>Klik aset atau kabel</b> pada diagram untuk melihat tabel koneksi core (hulu → hilir) dan mengeditnya. Ikon <span class="tp-xi">×</span> menghapus. Garis ungu putus-putus = sambungan puzzle yang belum diterapkan.</div>`;
  const stOpt = (v) =>
    ["Active", "Maintenance", "Cut/Broken"]
      .map((s) => `<option ${s === v ? "selected" : ""}>${s}</option>`)
      .join("");
  let h = "";
  if (sel.k === "node") {
    const n = g.nodes.get(sel.id);
    if (!n) return "";
    const ci = PZ.chain.findIndex((x) => x.id === n.id);
    h += `<div class="tp-ph"><span class="pz-ic t-${escapeHtml(String(n.type).toUpperCase())}">${mapGlyph(n.type, 16)}</span><div><b>${escapeHtml(n.name)}</b><small>${escapeHtml(n.type)}${n.virtual ? " · belum terhubung (puzzle)" : ""}</small></div></div>`;
    if (!n.virtual && can("asset.write")) {
      h +=
        `<div class="tp-form"><label>Nama<input id="tp-name" type="text" value="${escapeHtml(n.name)}" maxlength="120"></label>` +
        `<label>Status<select id="tp-status">${stOpt(n.status)}</select></label>` +
        `<label>Kapasitas<input id="tp-cap" type="text" value="${escapeHtml(n.capacity || "")}" maxlength="60"></label>` +
        `<label>Latitude<input id="tp-lat" type="number" step="any" value="${Number(n.latitude ?? n.lat)}"></label><label>Longitude<input id="tp-lng" type="number" step="any" value="${Number(n.longitude ?? n.lng)}"></label></div>` +
        `<div class="tp-act"><button type="button" class="data-primary" onclick="topoSave()"><i class="fa-solid fa-floppy-disk"></i> Update</button>` +
        `${ci >= 0 && String(n.type).toUpperCase() !== "PELANGGAN" ? `<button type="button" class="data-ghost" onclick="topoToPuzzle(${ci})"><i class="fa-solid fa-code-branch"></i> Lanjutkan rantai dari sini</button>` : ""}</div>`;
    }
  } else {
    const c = g.cabs.find((x) => String(x.id) === String(sel.id));
    if (!c) return "";
    h += `<div class="tp-ph"><span class="pz-ic" style="background:${c.virtual ? "#7c3aed" : TP_CABCOL[c.type] || "#64748b"}">${mapGlyph("CABLE", 16)}</span><div><b>${escapeHtml(c.name)}</b><small>${c.virtual ? "kabel baru (belum diterapkan; ubah di tab Rangkai)" : `${escapeHtml(c.type)} · ${c.used}/${c.total} core terpakai · ${fmtInt(c.length_m)} m`}</small></div></div>`;
    if (!c.virtual && can("asset.write")) {
      h +=
        `<div class="tp-form"><label>Nama<input id="tp-name" type="text" value="${escapeHtml(c.name)}" maxlength="120"></label>` +
        `<label>Jenis<select id="tp-type">${["Backbone", "Feeder", "Distribution", "Drop"].map((s) => `<option ${s === c.type ? "selected" : ""}>${s}</option>`).join("")}</select></label>` +
        `<label>Status<select id="tp-status">${stOpt(c.status)}</select></label><label>Kapasitas<input id="tp-cap" type="text" value="${escapeHtml(c.capacity || "")}" maxlength="20"></label>` +
        `<label>Pemasangan<select id="tp-inst">${["Udara", "Tanah"].map((s) => `<option ${s === c.installation ? "selected" : ""}>${s}</option>`).join("")}</select></label></div>` +
        `<div class="tp-act"><button type="button" class="data-primary" onclick="topoSave()"><i class="fa-solid fa-floppy-disk"></i> Update</button></div>`;
    }
  }
  if (TOPO.pmsg)
    h += `<div class="pz-note ${TOPO.pmsg.startsWith("!") ? "err" : "warn"}">${escapeHtml(TOPO.pmsg.replace(/^!/, ""))}</div>`;
  h += `<div class="tp-th">Tabel koneksi core <small>hulu → kabel/core → hilir</small></div>`;
  if (String(sel.id).startsWith("v"))
    return h + `<div class="pz-empty">Belum ada sambungan core.</div>`;
  const t = TOPO.tbl;
  if (!t) return h + `<div class="pz-empty">memuat…</div>`;
  if (t.err) return h + `<div class="pz-note err">${escapeHtml(t.err)}</div>`;
  if (!t.rows.length)
    return h + `<div class="pz-empty">Belum ada sambungan core.</div>`;
  const cell = (x) =>
    `${escapeHtml(x.name)}<small>${escapeHtml(x.port || "")}</small>`;
  h +=
    `<div class="tp-tbl"><table><thead><tr><th>Hulu</th><th></th><th>Kabel · core</th><th></th><th>Hilir</th>${can("connection.delete") ? "<th></th>" : ""}</tr></thead><tbody>` +
    t.rows
      .map(
        (r) =>
          `<tr><td>${cell(r.hulu)}</td><td class="ar">→</td><td>${r.kabel ? `${escapeHtml(r.kabel.name)}<small>${escapeHtml(r.kabel.core || "")}</small>` : `<small>(joint)</small>`}</td><td class="ar">→</td><td>${cell(r.hilir)}</td>` +
          `${can("connection.delete") ? `<td><button type="button" class="tp-del" title="Lepas sambungan ini" onclick="topoUnlink(${Number(r.id)})">×</button></td>` : ""}</tr>`,
      )
      .join("") +
    `</tbody></table></div>` +
    (t.truncated ? `<div class="pz-empty">Hanya 600 baris pertama.</div>` : "");
  return h;
}
async function topoSave() {
  const sel = TOPO.sel;
  if (!sel) return;
  const v = (id) => {
    const e = $id(id);
    return e ? e.value.trim() : undefined;
  };
  const body = {
    name: v("tp-name"),
    status: v("tp-status"),
    capacity: v("tp-cap"),
  };
  if (!body.name) {
    TOPO.pmsg = "!Nama tidak boleh kosong";
    return topoRender();
  }
  let url;
  if (sel.k === "node") {
    url = `/api/nodes/${sel.id}`;
    body.latitude = parseFloat(v("tp-lat"));
    body.longitude = parseFloat(v("tp-lng"));
    if (!isFinite(body.latitude) || !isFinite(body.longitude)) {
      TOPO.pmsg = "!Koordinat tidak valid";
      return topoRender();
    }
  } else {
    url = `/api/cables/${sel.id}`;
    body.type = v("tp-type");
    body.installation = v("tp-inst");
  }
  try {
    await apiRequest(url, "PUT", body);
    TOPO.pmsg = "Tersimpan.";
    TOPO.dirty = true;
    PZ.chain.forEach((x) => {
      if (sel.k === "node" && x.id === sel.id) {
        x.name = body.name;
        x.lat = body.latitude;
        x.lng = body.longitude;
      }
    });
    try {
      await loadData();
    } catch (_) {
      /* abaikan */
    }
    PZ.prev = null;
    if (PZ.chain.length > 1) pzPreviewSoon();
    await topoLoad();
    topoSel(sel.k, sel.id);
  } catch (err) {
    TOPO.pmsg = "!" + err.message;
    topoRender();
  }
}
// buang aset/kabel yang dihapus dari rantai puzzle
function pzDropNode(id) {
  const idx = PZ.chain.findIndex((n) => n.id === id);
  if (idx < 0) return;
  const gone = new Set([idx]);
  for (let i = 1; i < PZ.chain.length; i++)
    if (gone.has(PZ.par[i - 1])) gone.add(i);
  const keep = PZ.chain.map((_, i) => i).filter((i) => !gone.has(i)),
    remap = new Map(keep.map((o, n) => [o, n]));
  const chain = keep.map((i) => PZ.chain[i]),
    par = [],
    links = [];
  keep.forEach((i) => {
    if (i > 0) {
      par.push(remap.get(PZ.par[i - 1]));
      links.push(PZ.links[i - 1]);
    }
  });
  PZ.chain = chain;
  PZ.par = par;
  PZ.links = links;
  PZ.cur = -1;
  PZ.prev = null;
  PZ.sel = null;
  PZ.doneN = chain.length ? Math.min(PZ.doneN, chain.length) : 0;
  Object.keys(CM).forEach((k) => delete CM[k]);
}
function pzDropCable(cid) {
  PZ.links.forEach((lk) => {
    if (lk.cable_id === cid) {
      lk.cable_id = null;
      lk.done = false;
      lk.dn = null;
      lk.fresh = true;
      lk.cores = null;
      lk.n = null;
    }
  });
  PZ.prev = null;
}
async function topoDel(k, id) {
  const g = topoGraph();
  if (!g) return;
  const nm =
    k === "node"
      ? (g.nodes.get(id) || {}).name
      : (g.cabs.find((c) => c.id === id) || {}).name;
  if (
    !confirm(
      `Hapus ${k === "node" ? "aset" : "kabel"} "${nm}"? Sambungan core yang menyentuhnya ikut terhapus dan tercatat di Riwayat.`,
    )
  )
    return;
  try {
    await apiRequest(
      `/api/${k === "node" ? "nodes" : "cables"}/${id}`,
      "DELETE",
    );
    if (k === "node") pzDropNode(id);
    else pzDropCable(id);
    TOPO.sel = null;
    TOPO.tbl = null;
    TOPO.dirty = true;
    TOPO.pmsg = "";
    try {
      await loadData();
    } catch (_) {
      /* abaikan */
    }
    if (PZ.chain.length > 1) pzPreviewSoon();
    await topoLoad();
  } catch (err) {
    TOPO.pmsg = "!Gagal menghapus: " + err.message;
    topoRender();
  }
}
async function topoUnlink(cid) {
  if (!confirm("Lepas sambungan core ini?")) return;
  try {
    await apiRequest(`/api/connections/${cid}`, "DELETE");
    TOPO.dirty = true;
    const s = TOPO.sel;
    try {
      await loadData();
    } catch (_) {
      /* abaikan */
    }
    await topoLoad();
    if (s) topoSel(s.k, s.id);
  } catch (err) {
    TOPO.pmsg = "!Gagal: " + err.message;
    topoRender();
  }
}
function topoToPuzzle(ci) {
  tfTab("puzzle");
  pzBranch(ci);
}

// ----- Minimap puzzle: peta kecil di panel samping, terhubung dengan daftar pilihan -----
const PZM = {
  all: false,
  map: null,
  cab: null,
  bg: null,
  chain: null,
  cand: null,
  hl: null,
  tiles: null,
  canvas: null,
  candMap: {},
};
const PZ_COL = {
  POP: "#dc2626",
  CLOSURE: "#7c3aed",
  SLACK: "#f59e0b",
  ODP: "#16a34a",
  PELANGGAN: "#0ea5e9",
};
// ikon aset pada minimap = ikon yang sama dengan peta utama (glyph + warna per jenis)
function pzIconMk(t, kind, num) {
  const sz = kind === "chain" ? 32 : kind === "cand" ? 28 : 24;
  const html = `<span class="pzm-i t-${escapeHtml(t)} ${kind}">${mapGlyph(t, kind === "chain" ? 18 : 15)}${num ? `<b>${num}</b>` : ""}</span>`;
  return L.divIcon({
    className: "pzm-wrap",
    html,
    iconSize: [sz, sz],
    iconAnchor: [sz / 2, sz / 2],
  });
}
function pzLegendHtml() {
  return (
    ["POP", "CLOSURE", "SLACK", "ODP", "PELANGGAN"]
      .map(
        (t) =>
          `<span class="pz-lg"><span class="pz-ic t-${t}">${mapGlyph(t, 12)}</span>${NODE_TYPE_META[t].label.replace(/ \/.*|Joint /g, "")}</span>`,
      )
      .join("") +
    `<span class="pz-lg"><span class="pzm-i t-ODP cand lg"></span>Kandidat</span>`
  );
}
function pzMapInit() {
  if (PZM.map || typeof L === "undefined" || !$id("pz-map")) return;
  try {
    PZM.canvas = L.canvas({ padding: 0.2 });
    const lg = document.querySelector(".pz-legend");
    if (lg) lg.innerHTML = pzLegendHtml();
    const c = map.getCenter();
    PZM.map = L.map("pz-map", {
      zoomControl: true,
      attributionControl: false,
      preferCanvas: true,
    }).setView([c.lat, c.lng], Math.min(17, map.getZoom() || 16));
    PZM.tiles = L.tileLayer(
      "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
      { maxZoom: 19 },
    ).addTo(PZM.map);
    PZM.cab = L.layerGroup().addTo(PZM.map);
    PZM.bg = L.layerGroup().addTo(PZM.map);
    PZM.chain = L.layerGroup().addTo(PZM.map);
    PZM.cand = L.layerGroup().addTo(PZM.map);
    PZM.hl = L.layerGroup().addTo(PZM.map);
    PZM.map.on("moveend", pzMapBg);
    PZM.map.on("zoomstart click", () => OVLM.collapse());
    setTimeout(() => {
      try {
        PZM.map.invalidateSize();
        pzMapBg();
      } catch (_) {
        /* abaikan */
      }
    }, 120);
  } catch (_) {
    PZM.map = null;
  }
}
function pzMapBase(sat) {
  if (!PZM.map) return;
  try {
    PZM.map.removeLayer(PZM.tiles);
    PZM.tiles = sat
      ? L.tileLayer("https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}", {
          maxZoom: 20,
        })
      : L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
          maxZoom: 19,
        });
    PZM.tiles.addTo(PZM.map);
    PZM.tiles.bringToBack();
    $id("pz-b-map").className = sat ? "" : "on";
    $id("pz-b-sat").className = sat ? "on" : "";
  } catch (_) {
    /* abaikan */
  }
}
function pzMapSync() {
  if (!PZM.map) return;
  const c = map.getCenter();
  PZM.map.setView([c.lat, c.lng], Math.min(18, map.getZoom()));
}
// aturan tampil sama dengan peta utama (folder dan lapisan yang dimatikan tidak muncul)
function pzNodeShown(x) {
  const mk = markersMap["node:" + x.id],
    it = mk && mk._vi;
  if (!it) return true;
  if (FT.off.has(it.folder)) return false;
  const g = ZOOM_GATES.find((z) => z.group === it.group);
  return g
    ? !!g.wants
    : typeof map.hasLayer === "function"
      ? map.hasLayer(it.group)
      : true;
}
const PZ_NEXT = {
  POP: ["CLOSURE", "SLACK"],
  CLOSURE: ["CLOSURE", "SLACK", "ODP", "PELANGGAN"],
  SLACK: ["CLOSURE", "SLACK", "ODP", "PELANGGAN"],
  ODP: ["PELANGGAN"],
  PELANGGAN: [],
};
function pzMapAll(on) {
  PZM.all = !!on;
  pzMapBg();
}
function pzMapBg() {
  if (!PZM.map || !PZM.bg) return;
  OVLM.collapse();
  PZM.bg.clearLayers();
  pzMapCables();
  const b = PZM.map.getBounds().pad(0.05),
    c = PZM.map.getCenter(),
    inChain = new Set(PZ.chain.map((n) => Number(n.id)));
  if (PZ.pick.open)
    (PZ.pick.items || []).forEach((it) => inChain.add(Number(it.id))); // kandidat digambar di lapisan sendiri: jangan ganda
  let list = allInventoryData.filter(
    (x) =>
      x.category === "NODE" &&
      PZ_COL[String(x.type).toUpperCase()] &&
      Number.isFinite(x.lat) &&
      !inChain.has(Number(x.id)) &&
      b.contains([x.lat, x.lng]) &&
      pzNodeShown(x),
  );
  const seen0 = list.length;
  let ref = c,
    MAXN = 60,
    mode = "";
  if (!PZM.all) {
    const cur = PZ.chain.length ? PZ.chain[pzCur()] : null;
    if (cur) {
      const ok = PZ_NEXT[String(cur.type).toUpperCase()] || [];
      list = list.filter((x) => ok.includes(String(x.type).toUpperCase()));
      if (Number.isFinite(cur.lat)) ref = L.latLng(cur.lat, cur.lng);
      mode = `jenis yang valid setelah ${cur.type}`;
    } else {
      const pops = list.filter((x) => String(x.type).toUpperCase() === "POP");
      if (pops.length) list = pops;
      mode = "POP (titik awal)";
    }
  } else {
    MAXN = 200;
    mode = "semua jenis";
  }
  const total = list.length;
  const d2 = (q) => (q.lat - ref.lat) ** 2 + (q.lng - ref.lng) ** 2;
  if (total > MAXN) {
    list.sort((p, q) => d2(p) - d2(q));
    list = list.slice(0, MAXN);
  }
  list.forEach((x) => {
    const t = String(x.type).toUpperCase();
    const m = L.marker([x.lat, x.lng], { icon: pzIconMk(t, "") });
    m.bindTooltip(`${escapeHtml(x.name)} &middot; ${escapeHtml(x.type)}`, {
      direction: "top",
    });
    m.on("click", () => {
      if (OVLM.click(m)) return;
      pzAddNode({
        id: Number(x.id),
        name: x.name,
        type: x.type,
        lat: x.lat,
        lng: x.lng,
      });
    });
    m.addTo(PZM.bg);
  });
  OVLM.badges();
  const h = $id("pz-maphint");
  if (h)
    h.textContent = `${list.length} aset ditampilkan (${mode}${total > MAXN ? `; ${MAXN} terdekat dari ${total}` : ""}${seen0 > total ? `; ${seen0 - total} jenis lain disembunyikan` : ""}). Klik titik untuk menambahkannya ke rantai.`;
}
function pzMapRender() {
  if (!PZM.map) return;
  PZM.chain.clearLayers();
  PZM.cand.clearLayers();
  PZM.candMap = {};
  const pts = PZ.chain.map((n) => [n.lat, n.lng]);
  for (let i = 0; i < PZ.links.length; i++) {
    const a = PZ.chain[PZ.par[i]],
      b = PZ.chain[i + 1];
    if (!a || !b || !Number.isFinite(a.lat) || !Number.isFinite(b.lat))
      continue;
    const p = PZ.prev && PZ.prev.links[i],
      lk = PZ.links[i],
      neu = p && p.mode === "baru" && !lk.done;
    L.polyline(
      [
        [a.lat, a.lng],
        [b.lat, b.lng],
      ],
      {
        color: lk.done ? "#16a34a" : neu ? "#7c3aed" : "#0f766e",
        weight: PZ.sel === i ? 7 : lk.done ? 5 : 4,
        dashArray: neu ? "6 6" : null,
        opacity: 0.9,
        interactive: false,
      },
    ).addTo(PZM.chain);
  }
  PZ.chain.forEach((n, i) => {
    if (!Number.isFinite(n.lat)) return;
    const t = String(n.type).toUpperCase();
    const mk = L.marker([n.lat, n.lng], {
      icon: pzIconMk(t, "chain", i + 1),
      zIndexOffset: 1000,
    });
    mk.bindTooltip(escapeHtml(n.name), {
      permanent: true,
      direction: "right",
      offset: [10, 0],
      className: "pzm-tip",
    });
    mk.addTo(PZM.chain);
  });
  if (PZ.pick.open) {
    PZ.pick.items.forEach((x) => {
      if (!Number.isFinite(x.lat)) return;
      const m = L.marker([x.lat, x.lng], {
        icon: pzIconMk(String(x.type).toUpperCase(), "cand"),
        zIndexOffset: 500,
      });
      m.bindTooltip(
        `${escapeHtml(x.name)}${x.distance_m != null ? " &middot; " + fmtInt(x.distance_m) + " m" : ""}`,
        { direction: "top" },
      );
      m.on("click", () => {
        if (OVLM.click(m)) return;
        pzAddNode(x);
      });
      m.addTo(PZM.cand);
      PZM.candMap[x.id] = x;
    });
  }
  pzMapBg();
  try {
    pzMainDraw();
  } catch (_) {
    /* overlay peta utama opsional */
  }
}
// rantai (termasuk cabang) juga digambar di peta utama
let PZMAIN = null;
function pzMainDraw() {
  if (typeof map === "undefined") return;
  if (!PZMAIN) PZMAIN = L.layerGroup();
  PZMAIN.clearLayers();
  if (!PZ.chain.length) {
    if (map.hasLayer(PZMAIN)) map.removeLayer(PZMAIN);
    return;
  }
  if (!map.hasLayer(PZMAIN)) PZMAIN.addTo(map);
  PZ.links.forEach((lk, i) => {
    const a = PZ.chain[PZ.par[i]],
      b = PZ.chain[i + 1],
      p = PZ.prev && PZ.prev.links[i];
    if (!a || !b || !Number.isFinite(a.lat) || !Number.isFinite(b.lat)) return;
    const neu = p && p.mode === "baru" && !lk.done;
    L.polyline(
      [
        [a.lat, a.lng],
        [b.lat, b.lng],
      ],
      {
        color: lk.done ? "#16a34a" : neu ? "#7c3aed" : "#0f766e",
        weight: 6,
        dashArray: neu ? "8 8" : null,
        opacity: 0.85,
        interactive: false,
      },
    ).addTo(PZMAIN);
  });
  PZ.chain.forEach((n, i) => {
    if (!Number.isFinite(n.lat)) return;
    const mk = L.marker([n.lat, n.lng], {
      icon: pzIconMk(String(n.type).toUpperCase(), "chain", i + 1),
      zIndexOffset: 900,
      interactive: false,
    });
    mk.bindTooltip(escapeHtml(n.name), {
      permanent: true,
      direction: "right",
      offset: [12, 0],
      className: "pzm-tip",
    });
    mk.addTo(PZMAIN);
  });
}
// kabel yang sudah ada di area minimap (termasuk yang baru diterapkan)
function pzMapCables(force) {
  if (!PZM.map || !PZM.cab) return;
  const b = PZM.map.getBounds().pad(0.1),
    sig = b.toBBoxString() + "|" + PZM.map.getZoom() + "|" + (PZM.cabVer || 0);
  if (!force && PZM.cabSig === sig) return; // peta/data tidak berubah: tidak perlu gambar ulang
  PZM.cabSig = sig;
  PZM.cab.clearLayers();
  let n = 0;
  for (const k in markersMap) {
    if (!k.startsWith("cable:") || n >= 800) continue;
    const ly = markersMap[k];
    let ll;
    try {
      ll = ly.getLatLngs();
    } catch (_) {
      continue;
    }
    let bb = null;
    try {
      bb = ly.getBounds();
    } catch (_) {
      bb = null;
    }
    if (!bb || !b.intersects(bb)) continue;
    const flat =
      Array.isArray(ll) && ll.length && Array.isArray(ll[0])
        ? ll.flat(Infinity)
        : ll;
    if (!flat || !flat.length) continue;
    L.polyline(flat, {
      renderer: PZM.canvas,
      color: (ly.options && ly.options.color) || "#64748b",
      weight: 3,
      opacity: 0.75,
      interactive: false,
    }).addTo(PZM.cab);
    n++;
  }
}
function pzMapFit() {
  if (!PZM.map) return;
  const pts = PZ.chain
    .filter((n) => Number.isFinite(n.lat))
    .map((n) => [n.lat, n.lng]);
  try {
    if (pts.length === 1)
      PZM.map.setView(pts[0], Math.max(PZM.map.getZoom(), 17));
    else if (pts.length > 1)
      PZM.map.fitBounds(L.latLngBounds(pts).pad(0.35), { maxZoom: 18 });
  } catch (_) {
    /* abaikan */
  }
}
function pzHover(id) {
  if (!PZM.map || !PZM.hl) return;
  PZM.hl.clearLayers();
  const x =
    id != null
      ? PZM.candMap[id] ||
        PZ.pick.items.find((i) => Number(i.id) === Number(id))
      : null;
  if (!x || !Number.isFinite(x.lat)) return;
  L.circleMarker([x.lat, x.lng], {
    radius: 16,
    weight: 4,
    color: "#f97316",
    fillOpacity: 0,
    interactive: false,
  }).addTo(PZM.hl);
  try {
    if (!PZM.map.getBounds().contains([x.lat, x.lng]))
      PZM.map.panTo([x.lat, x.lng]);
  } catch (_) {
    /* abaikan */
  }
}
