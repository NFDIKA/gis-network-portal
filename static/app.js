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

const backboneGroup = L.layerGroup().addTo(map);
const feederGroup = L.layerGroup().addTo(map);
const distGroup = L.layerGroup().addTo(map);
const dropGroup = L.layerGroup().addTo(map);

const ALL_GROUPS = [
  popGroup,
  odpGroup,
  closureGroup,
  pelangganGroup,
  tiangGroup,
  slackGroup,
  incidentGroup,
  backboneGroup,
  feederGroup,
  distGroup,
  dropGroup,
];

// FeatureGroup gabungan untuk kebutuhan DRAG & DROP / EDITING
const editableGroup = L.featureGroup().addTo(map);

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
};
L.control
  .layers(baseMaps, overlayMaps, {
    position: "topright",
    // Layar sempit (HP/tablet potret): ringkas jadi ikon agar tidak menutupi peta
    collapsed: window.matchMedia
      ? window.matchMedia("(max-width: 900px)").matches
      : false,
  })
  .addTo(map);

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
async function apiRequest(url, method = "GET", body) {
  const options = { method, headers: {}, credentials: "same-origin" };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const res = await fetch(url, options);
  let data = null;
  try {
    data = await res.json();
  } catch (_) {
    /* respons tanpa body JSON */
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

  return L.divIcon({
    className: `custom-map-icon ${bgClass}`,
    html: `<i class="${iconClass}"></i>`,
    iconSize: [28, 28],
    iconAnchor: [14, 14],
  });
}

// ---- Format & metadata jenis aset (dipakai kartu, ringkasan, dan inventory) ----
function fmtLen(m) {
  if (m === null || m === undefined || m === "") return "-";
  const n = Number(m);
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
  return apiRequest("/api/dashboard/summary")
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
  closeSummaryModal();
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
        <span class="sum-ico" style="background:${meta.color}"><i class="fa-solid ${meta.icon}"></i></span>
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
}

function openAssetPopup(key) {
  const layer = markersMap[key];
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
  apiRequest(`/api/cables/${id}/status`, "PUT", { status: newStatus })
    .then(() => loadData())
    .catch((err) => alert("Gagal mengubah status kabel: " + err.message));
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
  Object.keys(markersMap).forEach((k) => delete markersMap[k]);
}

function addNodeToGroup(marker, props) {
  switch ((props.type || "").toUpperCase()) {
    case "POP":
      popGroup.addLayer(marker);
      break;
    case "CLOSURE":
      closureGroup.addLayer(marker);
      break;
    case "PELANGGAN":
      pelangganGroup.addLayer(marker);
      break;
    case "TIANG":
      tiangGroup.addLayer(marker);
      break;
    case "HH":
      hhGroup.addLayer(marker);
      break;
    case "SLACK":
      slackGroup.addLayer(marker);
      break;
    case "INCIDENT":
      incidentGroup.addLayer(marker);
      break;
    default:
      odpGroup.addLayer(marker);
  }
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

      marker.bindPopup(`
        <div class="popup-header">${escapeHtml(props.name)}</div>
        <div class="popup-row"><span>Kategori:</span> <b>${escapeHtml(props.type)}</b></div>
        <div class="popup-row"><span>Latitude:</span> <b>${latlng.lat.toFixed(6)}</b></div>
        <div class="popup-row"><span>Longitude:</span> <b>${latlng.lng.toFixed(6)}</b></div>
        <div class="popup-row"><span>Status:</span> <b>${escapeHtml(props.status)}</b></div>
        ${
          props.repair_kind
            ? `<div class="popup-row"><span>Perbaikan:</span> <b>${props.repair_kind === "TEMPORARY" ? "Sementara (perlu permanen)" : "Permanen"}</b> &middot; ${escapeHtml(props.repair_ticket || "")}</div>
               <div class="popup-actions"><button class="btn-status btn-warning" onclick="openIncidentDetail(${Number(props.repair_incident_id)})"><i class="fa-solid fa-list-check"></i> Detail Tiket</button></div>`
            : ""
        }

        <div class="popup-actions">
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
      `);

      markersMap[`node:${id}`] = marker;
      addNodeToGroup(marker, props);
      editableGroup.addLayer(marker);
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

      const marker = L.marker(latlng, {
        icon: L.divIcon({
          className: `custom-map-icon ${isResolved ? "icon-slack" : "icon-incident"}`,
          html: `<i class="fa-solid fa-triangle-exclamation"></i>`,
          iconSize: [30, 30],
          iconAnchor: [15, 15],
        }),
      });

      if (represented) return marker; // tidak dimasukkan ke peta

      const statusBadge = isResolved
        ? `<span style="color:#16a34a; font-weight:bold;">Resolved</span>`
        : isTemp
          ? `<span style="color:#d97706; font-weight:bold;">Temporary Fix (perlu perbaikan permanen)</span>`
          : `<span style="color:#ef4444; font-weight:bold;">${escapeHtml(props.status)} (${escapeHtml(props.severity)})</span>`;

      marker.bindPopup(`
        <div class="popup-header" style="background:#ef4444; color:white; padding:4px 8px; border-radius:4px;">
          [${escapeHtml(props.ticket_number)}] ${escapeHtml(props.title)}
        </div>
        <div class="popup-row" style="margin-top:8px;"><span>Kategori:</span> <b>${escapeHtml(props.incident_type)}</b></div>
        <div class="popup-row"><span>Status:</span> ${statusBadge}</div>
        <div class="popup-row"><span>Deskripsi:</span> <p style="margin:2px 0;">${escapeHtml(props.description || "-")}</p></div>
        <div class="popup-row"><span>Waktu Lapor:</span> ${timeHtml(props.reported_at)}</div>

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
            can("incident.delete")
              ? `<button class="btn-status btn-outline-danger" onclick="deleteIncidentRecord(${id})">
            <i class="fa-solid fa-trash"></i> Hapus Tiket
          </button>`
              : ""
          }
        </div>
      `);

      markersMap[`incident:${id}`] = marker;
      incidentGroup.addLayer(marker);
      return marker;
    },
  });
}

function renderCables(data) {
  L.geoJSON(data, {
    style: function (feature) {
      return getCableStyle(
        normalizeCableType(feature.properties.type),
        feature.properties.status,
        feature.properties.installation,
      );
    },
    onEachFeature: function (feature, layer) {
      const props = feature.properties;
      const coords = feature.geometry.coordinates;
      const type = normalizeCableType(props.type);
      const id = Number(props.id);

      layer.metaType = "cable";
      layer.metaData = props;

      const lengthInMeters = calculatePolylineLength(coords);
      const formattedLength =
        lengthInMeters > 1000
          ? `${(lengthInMeters / 1000).toFixed(2)} km (${Math.round(lengthInMeters)} m)`
          : `${Math.round(lengthInMeters)} m`;

      const isCut = props.status === "Cut/Broken";
      const btnText = isCut ? "Set Normal (Active)" : "Report Cable Cut";
      const btnClass = isCut ? "btn-success" : "btn-danger";
      const nextStatus = isCut ? "Active" : "Cut/Broken";

      layer.bindPopup(`
        <div class="popup-header">${escapeHtml(props.name)}</div>
        <div class="popup-row"><span>Tipe Jalur:</span> <b>Kabel ${escapeHtml(type)}</b></div>
        <div class="popup-row"><span>Panjang Kabel:</span> <b>${formattedLength}</b></div>
        <div class="popup-row"><span>Pemasangan:</span> <b>${props.installation === "Udara" ? "Kabel Udara" : props.installation === "Tanah" ? "Kabel Tanah" : "Belum diisi"}</b></div>
        <div class="popup-row"><span>Status:</span> <b>${escapeHtml(props.status)}</b></div>

        <div class="popup-actions">
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
      `);

      markersMap[`cable:${id}`] = layer;

      if (type === "Backbone") backboneGroup.addLayer(layer);
      else if (type === "Feeder") feederGroup.addLayer(layer);
      else if (type === "Drop") dropGroup.addLayer(layer);
      else distGroup.addLayer(layer);

      editableGroup.addLayer(layer);
    },
  });
}

// Satu kali fetch paralel, lalu clear + render sekali. Token membuang respons usang,
// sehingga pemanggilan beruntun (mis. setelah edit banyak objek) tidak membuat marker ganda.
function loadData() {
  const token = ++loadToken;
  loadDashboardSummary();

  Promise.all([
    apiRequest("/api/nodes"),
    apiRequest("/api/incidents"),
    apiRequest("/api/cables"),
  ])
    .then(([nodes, incidents, cables]) => {
      if (token !== loadToken) return;
      clearAllLayers();
      renderNodes(nodes);
      renderIncidents(incidents);
      renderCables(cables);
      buildInventory(nodes, cables);
      renderInventoryIfOpen();
    })
    .catch((err) => console.error("Gagal memuat data peta:", err));
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
  fetchInventoryData();
}

function closeInventoryModal() {
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
  return Promise.all([apiRequest("/api/nodes"), apiRequest("/api/cables")])
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
    cluster: document.getElementById("filter-cluster").value,
    type: document.getElementById("filter-type").value,
    status: (document.getElementById("filter-status") || {}).value || "ALL",
    installation:
      (document.getElementById("filter-installation") || {}).value || "ALL",
    sort: inventoryState.sort,
    order: inventoryState.order,
    page: String(inventoryState.page),
    page_size: String(inventoryState.pageSize),
  });
  const token = ++inventoryReqToken;
  const tbody = document.getElementById("inventory-table-body");

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
      tbody.innerHTML = `<tr><td colspan="8" style="text-align:center; padding:20px; color:#ef4444;">Gagal memuat data: ${escapeHtml(err.message)}</td></tr>`;
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
  tbody.innerHTML = "";

  if (items.length === 0) {
    tbody.innerHTML = `<tr><td colspan="8" class="inv-empty"><i class="fa-regular fa-folder-open" style="font-size:22px;display:block;margin-bottom:8px;"></i>Tidak ada aset yang cocok dengan filter ini</td></tr>`;
    return;
  }

  items.forEach((item) => {
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

    tr.innerHTML = `
      <td><div class="inv-name"><span class="inv-ico" style="background:${meta.color}"><i class="fa-solid ${meta.icon}"></i></span>
        <div><b>${escapeHtml(item.name)}</b><span class="inv-sub">${escapeHtml(typeLabel || "-")} &middot; ${escapeHtml(item.city || "-")}</span></div></div></td>
      <td class="col-type"><span class="inv-tag">${escapeHtml(typeLabel || "-")}</span></td>
      <td class="col-loc inv-loc"><b>${escapeHtml(item.city || "-")}</b><small>${escapeHtml(item.cluster || "-")} / ${escapeHtml(item.area || "-")}</small></td>
      <td class="col-cap">${escapeHtml(item.capacity || "-")}</td>
      <td class="col-cable"><b>${isCable ? fmtLen(item.length_m) : "-"}</b></td>
      <td class="col-cable">${isCable ? instHtml : "-"}</td>
      <td><span class="inv-status ${st[0]}">${escapeHtml(st[1])}</span></td>
      <td><div class="inv-actions">
        <button class="inv-btn" onclick="zoomToAsset('${kind}', ${id})" title="Sorot di Peta"><i class="fa-solid fa-crosshairs"></i></button>
        ${passive ? "" : `<button class="inv-btn primary" onclick="openCoreDetailModal(${id}, '${item.category}')" title="Lihat Status Core / Port"><i class="fa-solid fa-diagram-project"></i><span class="lbl"> Core/Port</span></button>`}
        ${can("asset.write") ? `<button class="inv-btn" onclick="${editCall}" title="Edit"><i class="fa-solid fa-pen"></i></button>` : ""}
        ${can("asset.delete") ? `<button class="inv-btn danger" onclick="${deleteCall}" title="Hapus Aset"><i class="fa-solid fa-trash"></i></button>` : ""}
      </div></td>`;
    tbody.appendChild(tr);
  });
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

// Pemetaan Cluster -> Area
const AREA_MAPPING = {
  EKO: ["BANJARMASIN", "SAMARINDA", "BALIKPAPAN", "TANJUNG SELOR"],
  WKO: ["PONTIANAK", "PALANGKARAYA"],
};

function updateAreaOptions() {
  const clusterSelect = document.getElementById("asset-cluster");
  const areaSelect = document.getElementById("asset-area");
  const areas = AREA_MAPPING[clusterSelect.value] || [];

  areaSelect.innerHTML = "";
  areas.forEach((area) => {
    const opt = document.createElement("option");
    opt.value = area;
    opt.textContent = area;
    areaSelect.appendChild(opt);
  });
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

  // Set default cluster & area
  document.getElementById("asset-cluster").value = "EKO";
  updateAreaOptions();

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
    onAssetTypeChange();
    setVal("asset-capacity", props.capacity);
    setVal("asset-cluster", props.cluster);
    updateAreaOptions();
    setVal("asset-area", props.area);
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
    } else {
      setVal("asset-installation", props.installation);
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
  const selectedType = document.getElementById("asset-type").value;
  const capacitySelect = document.getElementById("asset-capacity");

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
  const capacity = document.getElementById("asset-capacity").value;

  if (!name) return alert("Nama aset wajib diisi.");
  if (city === GEOCODE_PENDING_TEXT) {
    return alert("Lokasi masih dideteksi, tunggu sebentar lalu simpan lagi.");
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
        parent_node_id: val("asset-parent-node"),
        upstream_cable_id: val("asset-upstream-cable"),
      };
    } else {
      url = `/api/cables/${Number(editId)}`;
      payload = {
        ...common,
        installation,
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
      spec_data: "{}",
      parent_node_id: val("asset-parent-node"),
      upstream_cable_id: val("asset-upstream-cable"),
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
      core_data: "{}",
      parent_cable_id: val("asset-parent-cable"),
      from_node_id: val("asset-from-node"),
      to_node_id: val("asset-to-node"),
    };
  }

  apiRequest(url, "POST", payload)
    .then(() => {
      alert(successMsg);
      closeAddAssetModal();
      loadData(); // refresh tanpa reload halaman, posisi/zoom peta tetap
    })
    .catch((err) => alert("Gagal menyimpan: " + err.message));
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

// Jumlah core/port sebuah aset (tiang tidak punya port)
function getPortCount(item) {
  if (
    item.category === "NODE" &&
    NO_PORT_TYPES.includes((item.type || "").toUpperCase())
  )
    return 0;
  return parseInt(item.capacity) || 12;
}

function isOdp(item) {
  return item.category === "NODE" && (item.type || "").toUpperCase() === "ODP";
}

// Daftar opsi port/core sebuah aset, dipakai dropdown asal & tujuan sambungan
// usedPorts (Set, opsional): port yang sudah terpakai ditandai & dinonaktifkan
function buildPortOptionsHtml(item, usedPorts = new Set()) {
  const opt = (value, label) => {
    const used = usedPorts.has(value);
    return `<option value="${value}"${used ? " disabled" : ""}>${label}${used ? " (terpakai)" : ""}</option>`;
  };
  let html = "";
  if (isOdp(item)) {
    const { inCount, outCount } = parseOdpCapacity(item.capacity);
    for (let i = 1; i <= inCount; i++) html += opt(`IN-${i}`, `Input IN-${i}`);
    for (let o = 1; o <= outCount; o++)
      html += opt(`OUT-${o}`, `Output OUT-${o}`);
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

  document.getElementById("core-modal-title").innerHTML =
    `<i class="fa-solid fa-diagram-project"></i> Detail Status & Splicing: ${escapeHtml(item.name)}`;

  infoContainer.innerHTML = `
    <div><b>Tipe Aset:</b> ${escapeHtml(item.type || "-")}</div>
    <div><b>Cluster / Area:</b> ${escapeHtml(item.cluster || "-")} / ${escapeHtml(item.area || "-")}</div>
    <div><b>Kapasitas:</b> ${escapeHtml(item.capacity || "-")}</div>
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

      if (
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

  fromSelect.innerHTML =
    '<option value="">-- Pilih Core/Port Aset Ini --</option>' +
    buildPortOptionsHtml(
      currentActiveAsset,
      getConnectedLocalPorts(currentActiveAsset),
    );
  toAssetSelect.innerHTML = '<option value="">-- Pilih Aset Tujuan --</option>';
  if (toCoreSelect)
    toCoreSelect.innerHTML =
      '<option value="">-- Pilih Core/Port Tujuan --</option>';
  if (viaCableSelect) {
    viaCableSelect.innerHTML =
      '<option value="">-- Tanpa Kabel / Langsung (Direct) --</option>';
  }
  const viaWrap = document.getElementById("splice-via-core-wrap");
  if (viaWrap) viaWrap.style.display = "none";

  if (!allInventoryData.length) {
    toAssetSelect.innerHTML =
      '<option value="">-- Data Inventory Kosong --</option>';
    return;
  }

  let availableTargets = 0;
  allInventoryData.forEach((item) => {
    const isSameAsset =
      String(item.id) === String(currentActiveAsset.id) &&
      String(item.category) === String(currentActiveAsset.category);

    if (!isSameAsset) {
      availableTargets++;
      toAssetSelect.innerHTML += `<option value="${item.category}:${Number(item.id)}">${escapeHtml(item.name)} (${escapeHtml(item.type || item.category)})</option>`;
    }

    if (item.category === "CABLE" && viaCableSelect) {
      viaCableSelect.innerHTML += `<option value="${Number(item.id)}">${escapeHtml(item.name)} (${escapeHtml(item.type)}) - ${escapeHtml(item.capacity)}</option>`;
    }
  });

  if (availableTargets === 0) {
    toAssetSelect.innerHTML =
      '<option value="">-- Tidak Ada Aset Lain Tersedia --</option>';
  }
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

  toCoreSelect.innerHTML += buildPortOptionsHtml(targetAsset);

  // tandai port tujuan yang sudah terpakai (diambil dari server)
  apiRequest(
    `/api/connections?asset_type=${encodeURIComponent(cat)}&asset_id=${Number(id)}`,
  )
    .then((conns) => {
      if (document.getElementById("splice-to-asset").value !== targetVal)
        return; // pilihan sudah berganti
      const used = new Set();
      (Array.isArray(conns) ? conns : []).forEach((c) => {
        if (c.from_asset_type === cat && String(c.from_asset_id) === String(id))
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
        buildPortOptionsHtml(targetAsset, used);
    })
    .catch((err) => console.error("Gagal memuat port tujuan:", err));
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
function renderActiveConnectionsTable(
  connections,
  currentAssetType,
  currentAssetId,
) {
  const tbody = document.getElementById("active-connections-tbody");
  tbody.innerHTML = "";

  if (!connections || connections.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; padding:12px; color:#94a3b8;">Belum ada sambungan core/port aktif.</td></tr>`;
    return;
  }

  let rows = "";
  connections.forEach((conn) => {
    const isFromHere =
      String(conn.from_asset_type) === String(currentAssetType) &&
      String(conn.from_asset_id) === String(currentAssetId);
    const isToHere =
      String(conn.to_asset_type) === String(currentAssetType) &&
      String(conn.to_asset_id) === String(currentAssetId);
    // Aset ini hanya DILEWATI sambungan (kabel media) -> bukan ujung sambungan
    const isPassThrough = !isFromHere && !isToHere;

    const assetName = (type, id) => {
      const f = allInventoryData.find(
        (item) =>
          String(item.id) === String(id) &&
          String(item.category).toUpperCase() === String(type).toUpperCase(),
      );
      return f && f.name ? f.name : `${type} #${id}`;
    };

    let localPort, targetAssetHtml, targetPort;
    if (isPassThrough) {
      localPort = conn.via_core || "-";
      targetAssetHtml = `${assetLinkHtml(conn.from_asset_type, conn.from_asset_id, assetName(conn.from_asset_type, conn.from_asset_id))} \u2192 ${assetLinkHtml(conn.to_asset_type, conn.to_asset_id, assetName(conn.to_asset_type, conn.to_asset_id))}`;
      targetPort = `${conn.from_port_core} \u2192 ${conn.to_port_core}`;
    } else {
      localPort = isFromHere ? conn.from_port_core : conn.to_port_core;
      const targetAssetType = isFromHere
        ? conn.to_asset_type
        : conn.from_asset_type;
      const targetAssetId = isFromHere ? conn.to_asset_id : conn.from_asset_id;
      targetPort = isFromHere ? conn.to_port_core : conn.from_port_core;
      targetAssetHtml = assetLinkHtml(
        targetAssetType,
        targetAssetId,
        assetName(targetAssetType, targetAssetId),
      );
    }

    const directionBadge = isPassThrough
      ? `<span style="background:#fef3c7; color:#b45309; padding:2px 6px; border-radius:4px; font-size:10px; font-weight:bold;"><i class="fa-solid fa-route"></i> Melintas</span>`
      : isFromHere
        ? `<span style="background:#dcfce7; color:#15803d; padding:2px 6px; border-radius:4px; font-size:10px; font-weight:bold;"><i class="fa-solid fa-arrow-right"></i> Downstream</span>`
        : `<span style="background:#e0f2fe; color:#0369a1; padding:2px 6px; border-radius:4px; font-size:10px; font-weight:bold;"><i class="fa-solid fa-arrow-left"></i> Upstream</span>`;

    const viaLabel = isPassThrough
      ? "Kabel ini"
      : conn.via_cable_name
        ? assetLinkHtml("CABLE", conn.via_cable_id, conn.via_cable_name) +
          (conn.via_core ? ` (${escapeHtml(conn.via_core)})` : "")
        : conn.via_cable_id
          ? "Kabel #" + Number(conn.via_cable_id)
          : "Langsung / Direct";

    rows += `
      <tr style="border-bottom: 1px solid #f1f5f9;">
        <td style="padding: 8px; font-weight: 600; color: #1e293b;">${escapeHtml(localPort)}</td>
        <td style="padding: 8px; text-align: center;">${directionBadge}</td>
        <td style="padding: 8px; color: #0284c7; font-weight: 500;">${viaLabel}</td>
        <td style="padding: 8px; font-weight: 500;">${targetAssetHtml}</td>
        <td style="padding: 8px; font-weight: 500;">${escapeHtml(targetPort)}</td>
        <td style="padding: 8px; text-align: center;">
          ${
            can("connection.delete")
              ? `<button onclick="disconnectCore(${Number(conn.id)})" style="background:#ef4444; color:white; border:none; padding:4px 8px; border-radius:4px; cursor:pointer; font-size:11px;">
            <i class="fa-solid fa-trash"></i> Putus
          </button>`
              : ""
          }
        </td>
      </tr>
    `;
  });
  tbody.innerHTML = rows;
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
      box.innerHTML = renderTraceHtml(data);
    })
    .catch((err) => {
      console.error("Gagal memuat jalur:", err);
      box.innerHTML = `<span style="color:#ef4444;">Gagal memuat jalur: ${escapeHtml(err.message)}</span>`;
    });
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
    `<span style="background:#e0f2fe;color:#0369a1;padding:3px 8px;border-radius:4px;font-weight:600;">Hulu: ${Number(s.upstream_hops || 0)} hop</span>` +
    `<span style="background:#dcfce7;color:#15803d;padding:3px 8px;border-radius:4px;font-weight:600;">Hilir: ${Number(s.downstream_hops || 0)} hop</span>` +
    `<span style="background:#fee2e2;color:#b91c1c;padding:3px 8px;border-radius:4px;font-weight:600;">Pelanggan terdampak: ${Number(s.customers_affected || 0)}</span></div>`;

  if (d.asset && d.asset.type === "CABLE") {
    html += section(
      "Core yang melintas &amp; dampaknya bila putus",
      "#b45309",
      d.through || [],
      true,
      "Belum ada sambungan yang melewati kabel ini.",
    );
  }
  html += section(
    "Hulu (arah POP)",
    "#0369a1",
    d.upstream || [],
    false,
    "Tidak ada sambungan hulu tercatat.",
  );
  html += section(
    "Hilir (arah pelanggan)",
    "#15803d",
    d.downstream || [],
    true,
    "Tidak ada sambungan hilir tercatat.",
  );

  if ((d.customers || []).length) {
    html +=
      `<div style="font-weight:700;color:#b91c1c;margin-bottom:2px;">Pelanggan di jalur ini</div>` +
      `<div style="color:#475569;">${d.customers.map((c) => escapeHtml(c.name)).join(", ")}</div>`;
  }
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

// --- RENDER CORE KABEL / CLOSURE / POP (STANDAR TUBE & CORE COLOR ISO) ---
function renderCableCores(item, container) {
  const type = (item.type || "").toUpperCase();
  let labelHeader = "Core Kabel";
  if (type === "CLOSURE") labelHeader = "Joint Closure Core";
  if (type === "POP") labelHeader = "ODF / Patch Panel POP Core";

  const totalCores = getPortCount(item);
  const connectedPorts = getConnectedLocalPorts(item);

  const usedCount = Array.from({ length: totalCores }, (_, k) => k + 1).filter(
    (i) =>
      connectedPorts.has(`Tube ${Math.floor((i - 1) / 12) + 1} - Core ${i}`),
  ).length;

  let html = `<h4 style="margin-bottom: 4px; font-size: 14px; color: #334155;">Visualisasi ${labelHeader} (${escapeHtml(item.capacity || totalCores + "C")})</h4>`;
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
    const isConnected = connectedPorts.has(coreLabel);
    const statusText = isConnected ? "Connected" : "Available";
    const statusColor = isConnected ? "#2563eb" : "#16a34a";

    html += `
      <div style="border: 1px solid #e2e8f0; border-radius: 6px; padding: 8px; text-align: center; background: #fafafa;">
        <div style="font-size: 10px; color: #64748b; margin-bottom: 4px;">
          ${coreLabel}
        </div>
        <div style="background-color: ${colorInfo.hex}; color: ${colorInfo.text}; border: ${colorInfo.border ? "1px solid " + colorInfo.border : "none"};
                    padding: 6px; border-radius: 4px; font-weight: bold; font-size: 12px; box-shadow: inset 0 0 4px rgba(0,0,0,0.2);">
          ${colorInfo.name}
        </div>
        <div style="font-size: 10px; margin-top: 5px; color: ${statusColor}; font-weight: 600;">${statusText}</div>
      </div>
    `;
  }

  html += `</div>`;
  container.innerHTML = html;
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

  container.innerHTML = `
    <h4 style="font-size: 13px; color: #1e293b; margin-bottom: 12px; font-weight: 600;">
      Konfigurasi Splitter ODP (${escapeHtml(capacityStr)})
    </h4>

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
  `;
}

// --- RENDER PORT GRID NODE (PELANGGAN / SLACK / dll) ---
function renderNodePorts(node, container) {
  const totalPorts = getPortCount(node);

  if (totalPorts === 0) {
    container.innerHTML = `<p style="font-size: 13px; color: #64748b;">Aset bertipe ${escapeHtml(node.type)} tidak memiliki port/core untuk disambung.</p>`;
    return;
  }

  const connectedPorts = getConnectedLocalPorts(node);

  let html = `<h4 style="margin-bottom: 12px; font-size: 14px; color: #334155;">Grid Port ${escapeHtml(node.type)} (${totalPorts} Port)</h4>`;
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

  html += `</div>`;
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
  if (wantCable && loc.cable) {
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

function onIncidentLinkedChange() {
  const linked = document.getElementById("inc-linked-asset");
  const wrap = document.getElementById("inc-cores-wrap");
  if (wrap)
    wrap.style.display =
      linked && linked.value.startsWith("CABLE:") ? "block" : "none";
}

function openAddIncidentModal(lat, lng) {
  document.getElementById("modal-add-incident").style.display = "flex";
  document.getElementById("inc-lat").value = lat;
  document.getElementById("inc-lng").value = lng;
  document.getElementById("inc-ticket").value =
    "INC-" + Date.now().toString().slice(-6);
  document.getElementById("inc-title").value = "";
  document.getElementById("inc-description").value = "";
  const coresEl = document.getElementById("inc-cores");
  if (coresEl) coresEl.value = "";
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
    cluster: "EKO",
    area: "BANJARMASIN",
    city: "Kota Banjarmasin",
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
      (loc.cable
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
  };
  const changesMap =
    payload.action === "ADD_CLOSURE" || payload.action === "EXTRA_JOINT";
  const msg = changesMap
    ? "Perbaikan ini akan menyisipkan closure/joint di titik gangguan dan MEMECAH kabel menjadi dua segmen (peta, topologi, dan sambungan core diperbarui). Lanjutkan?"
    : "Catat perbaikan ini?";
  if (!confirm(msg)) return;

  apiRequest(`/api/incidents/${id}/repairs`, "POST", payload)
    .then((res) => {
      alert(
        `Perbaikan dicatat. Status tiket: ${res.status}.` +
          (res.node_id
            ? `\nTitik sambung ${res.reused_existing_node ? "(memakai closure yang sudah ada)" : "baru dibuat"} di peta.`
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
function getCableStyle(type, status, installation) {
  let color = "#0891b2";
  let weight = 3;
  let dashArray = null;

  switch (type) {
    case "Backbone":
      color = "#2638dc";
      weight = 6;
      break;
    case "Feeder":
      color = "#ebeb25";
      weight = 4.5;
      break;
    case "Distribution":
      color = "#0891b2";
      weight = 3;
      break;
    case "Drop":
      color = "#d97706";
      weight = 2;
      break;
  }

  // Kabel tanah (duct/tanam langsung) digambar garis-titik; kabel udara garis penuh
  if (installation === "Tanah") dashArray = "10, 5, 2, 5";

  if (status === "Cut/Broken") {
    color = "#ef4444";
    dashArray = "6, 8"; // garis putus-putus
  }

  return { color, weight, dashArray, opacity: 0.9 };
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

function fetchNocIncidentsLog() {
  apiRequest("/api/incidents")
    .then((data) => {
      const tbody = document.getElementById("noc-table-body");
      if (!tbody) return;
      tbody.innerHTML = "";

      const features = data.features || [];
      if (features.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; padding: 15px; color: #94a3b8;">Belum ada catatan history incident.</td></tr>`;
        return;
      }

      features.forEach((f) => {
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

        tr.innerHTML = `
          <td style="padding: 10px; font-weight: 600;">${escapeHtml(props.ticket_number)}</td>
          <td style="padding: 10px;">${escapeHtml(props.title)}</td>
          <td style="padding: 10px;">${escapeHtml(props.incident_type)}</td>
          <td style="padding: 10px;"><span style="color: white; background: ${props.severity === "Critical" ? "#dc2626" : "#f59e0b"}; padding: 2px 6px; border-radius: 4px; font-size: 11px;">${escapeHtml(props.severity)}</span></td>
          <td style="padding: 10px;"><span style="color: white; background: ${badgeColor}; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 500;">${escapeHtml(props.status)}</span></td>
          <td style="padding: 10px;"><small>${timeHtml(props.reported_at)}</small></td>
          <td style="padding: 10px; text-align: center;">
            <button onclick="closeNocMonitorModal(); map.flyTo([${lat}, ${lng}], 18, {duration: 1.5}); setTimeout(() => focusIncidentPopup(${Number(props.id)}, ${props.repair_node_id == null ? "null" : Number(props.repair_node_id)}), 1650);"
                    style="background: #2563eb; color: white; border: none; padding: 4px 8px; border-radius: 4px; cursor: pointer; font-size: 11px;">
              <i class="fa-solid fa-crosshairs"></i> Sorot
            </button>
            <button onclick="closeNocMonitorModal(); openIncidentDetail(${Number(props.id)});"
                    style="background: #0f766e; color: white; border: none; padding: 4px 8px; border-radius: 4px; cursor: pointer; font-size: 11px; margin-left: 4px;">
              <i class="fa-solid fa-list-check"></i> Detail
            </button>
          </td>
        `;
        tbody.appendChild(tr);
      });
    })
    .catch((err) => console.error("Gagal memuat log incident:", err));
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

function showSearchHighlight(lat, lng, html, bbox, zoom = 18) {
  if (searchHighlightMarker) map.removeLayer(searchHighlightMarker);
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
  searchHighlightMarker = L.marker([lat, lng])
    .addTo(map)
    .bindPopup(html)
    .openPopup();
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

function parseCoordinate(query) {
  const m = query.match(
    /^([-+]?\d+(?:\.\d+)?)\s*[,;\s]\s*([-+]?\d+(?:\.\d+)?)$/,
  );
  if (!m) return null;
  const lat = parseFloat(m[1]),
    lng = parseFloat(m[2]);
  return { lat, lng, valid: Math.abs(lat) <= 90 && Math.abs(lng) <= 180 };
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
  const acts =
    (can("incident.write")
      ? `<button class="btn-status btn-warning" onclick="createIncidentAtSearch()"><i class="fa-solid fa-triangle-exclamation"></i> Tiket di sini</button>`
      : "") +
    (can("asset.write")
      ? `<button class="btn-status btn-success" onclick="addAssetAtSearch()"><i class="fa-solid fa-plus"></i> Aset di sini</button>`
      : "");
  return `<div class="popup-header">${escapeHtml(r.name)}</div>
    ${r.label ? `<div class="popup-row">${escapeHtml(r.label)}</div>` : ""}
    <div class="popup-row"><span>Koordinat:</span> <b>${r.lat.toFixed(6)}, ${r.lng.toFixed(6)}</b></div>
    ${nearHtml}${acts ? `<div class="popup-actions">${acts}</div>` : ""}`;
}
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
    html += `<div class="${cls()}" data-i="${idx}" onclick="chooseSearchItem(${idx})"><div><div class="item-title"><i class="fa-solid fa-crosshairs" style="margin-right:6px;"></i>Ke koordinat ${coordItem.c.lat}, ${coordItem.c.lng}</div></div></div>`;
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
        <div class="item-subtitle">${escapeHtml(r.label || r.kind || "")}</div></div></div>`;
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
  if (SEARCH.remote.length && SEARCH.query === q)
    return selectPlace(SEARCH.remote[0]);
  if (q.length < 3)
    return alert(`Aset atau koordinat "${query}" tidak ditemukan.`);
  clearTimeout(SEARCH.timer);
  const myseq = ++SEARCH.seq;
  return runPlaceSearch(query, myseq).then(() => {
    if (SEARCH.remote.length) selectPlace(SEARCH.remote[0]);
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
  [
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
  configureDrawControl();
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

initSidebarState();
bootAuth();
