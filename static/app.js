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
const drawControl = new L.Control.Draw({
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
  "SLACK",
  "INCIDENT",
];
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
  const options = { method, headers: {} };
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

function loadDashboardSummary() {
  apiRequest("/api/dashboard/summary")
    .then((data) => {
      const set = (id, val) => {
        const el = document.getElementById(id);
        if (el) el.innerText = val;
      };
      set("stat-odp", data.total_odp);
      set("stat-cables", data.total_cables);
      set("stat-incidents", data.total_incidents);
    })
    .catch((err) => console.error("Gagal memuat ringkasan:", err));
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
            <button class="btn-status ${btnClass}" onclick="updateNodeStatus(${id}, '${nextStatus}')">
                <i class="fa-solid fa-power-off"></i> ${btnText}
            </button>
            <button class="btn-status btn-warning" onclick="editNodeProperties(${id})">
                <i class="fa-solid fa-pen"></i> Edit Info Aset
            </button>
            <button class="btn-status btn-outline-danger" onclick="deleteNode(${id})">
                <i class="fa-solid fa-trash"></i> Hapus Aset
            </button>
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
            !isResolved
              ? `<button class="btn-status btn-success" onclick="resolveIncident(${id})">
                  <i class="fa-solid fa-check-circle"></i> Selesaikan Tiket
                </button>`
              : ""
          }
          <button class="btn-status btn-outline-danger" onclick="deleteIncidentRecord(${id})">
            <i class="fa-solid fa-trash"></i> Hapus Tiket
          </button>
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
        <div class="popup-row"><span>Status:</span> <b>${escapeHtml(props.status)}</b></div>

        <div class="popup-actions">
            <button class="btn-status ${btnClass}" onclick="updateCableStatus(${id}, '${nextStatus}')">
                <i class="fa-solid fa-power-off"></i> ${btnText}
            </button>
            <button class="btn-status btn-warning" onclick="editCableProperties(${id})">
                <i class="fa-solid fa-pen"></i> Edit Info Kabel
            </button>
            <button class="btn-status btn-outline-danger" onclick="deleteCable(${id})">
                <i class="fa-solid fa-trash"></i> Hapus Kabel
            </button>
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

// Panggil fungsi pemuatan data awal
loadData();

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
    const action = confirm(
      "Klik OK untuk Tambah Asset Normal (ODP/POP/Tiang/dll)\nKlik CANCEL untuk Buat Tiket Incident Baru",
    );
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
function openInventoryModal() {
  document.getElementById("modal-inventory").style.display = "flex";
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
      drawInventoryRows(res.items || [], tbody);
    })
    .catch((err) => {
      console.error("Gagal memuat inventory:", err);
      tbody.innerHTML = `<tr><td colspan="7" style="text-align:center; padding:20px; color:#ef4444;">Gagal memuat data: ${escapeHtml(err.message)}</td></tr>`;
    });
}

function drawInventoryRows(items, tbody) {
  tbody.innerHTML = "";

  if (items.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; padding: 20px; color: #94a3b8;">Tidak ada data aset ditemukan</td></tr>`;
    return;
  }

  items.forEach((item) => {
    const tr = document.createElement("tr");
    tr.style.borderBottom = "1px solid #f1f5f9";

    let badgeStatus = `#10b981`; // Active
    if (item.status === "Maintenance") badgeStatus = `#f59e0b`;
    if (item.status === "Cut/Broken") badgeStatus = `#ef4444`;

    const id = Number(item.id);
    const kind = item.category.toLowerCase(); // "node" / "cable"
    const deleteCall =
      item.category === "NODE" ? `deleteNode(${id})` : `deleteCable(${id})`;

    tr.innerHTML = `
            <td style="padding: 10px; font-weight: 600;">${escapeHtml(item.name)}</td>
            <td style="padding: 10px;"><span style="background: #e2e8f0; padding: 2px 6px; border-radius: 4px; font-size: 11px;">${escapeHtml(item.type)}</span></td>
            <td style="padding: 10px;">${escapeHtml(item.cluster || "-")} / ${escapeHtml(item.area || "-")}</td>
            <td style="padding: 10px;">${escapeHtml(item.city || "-")}</td>
            <td style="padding: 10px; font-weight: 600;">${escapeHtml(item.capacity || "-")}</td>
            <td style="padding: 10px;">
                <span style="color: white; background: ${badgeStatus}; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 500;">
                    ${escapeHtml(item.status)}
                </span>
            </td>
            <td style="padding: 10px; text-align: center; white-space: nowrap;">
                <button onclick="zoomToAsset('${kind}', ${id})"
                        style="background: #0891b2; color: white; border: none; padding: 4px 8px; border-radius: 4px; cursor: pointer; font-size: 11px;" title="Sorot di Peta">
                    <i class="fa-solid fa-crosshairs"></i>
                </button>
                <button onclick="openCoreDetailModal(${id}, '${item.category}')"
                        style="background: #2563eb; color: white; border: none; padding: 4px 8px; border-radius: 4px; cursor: pointer; font-size: 11px; margin-left: 4px;" title="Lihat Status Core / Port">
                    <i class="fa-solid fa-diagram-project"></i> Core/Port
                </button>
                <button onclick="${deleteCall}"
                        style="background: #ef4444; color: white; border: none; padding: 4px 6px; border-radius: 4px; cursor: pointer; font-size: 11px; margin-left: 4px;" title="Hapus Aset">
                    <i class="fa-solid fa-trash"></i>
                </button>
            </td>
        `;
    tbody.appendChild(tr);
  });
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
  if (item.category === "NODE" && (item.type || "").toUpperCase() === "TIANG")
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

function openCoreDetailModal(id, category) {
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

    let localPort, targetAssetName, targetPort;
    if (isPassThrough) {
      localPort = conn.via_core || "-";
      targetAssetName = `${assetName(conn.from_asset_type, conn.from_asset_id)} \u2192 ${assetName(conn.to_asset_type, conn.to_asset_id)}`;
      targetPort = `${conn.from_port_core} \u2192 ${conn.to_port_core}`;
    } else {
      localPort = isFromHere ? conn.from_port_core : conn.to_port_core;
      const targetAssetType = isFromHere
        ? conn.to_asset_type
        : conn.from_asset_type;
      const targetAssetId = isFromHere ? conn.to_asset_id : conn.from_asset_id;
      targetPort = isFromHere ? conn.to_port_core : conn.from_port_core;
      targetAssetName = assetName(targetAssetType, targetAssetId);
    }

    const directionBadge = isPassThrough
      ? `<span style="background:#fef3c7; color:#b45309; padding:2px 6px; border-radius:4px; font-size:10px; font-weight:bold;"><i class="fa-solid fa-route"></i> Melintas</span>`
      : isFromHere
        ? `<span style="background:#dcfce7; color:#15803d; padding:2px 6px; border-radius:4px; font-size:10px; font-weight:bold;"><i class="fa-solid fa-arrow-right"></i> Downstream</span>`
        : `<span style="background:#e0f2fe; color:#0369a1; padding:2px 6px; border-radius:4px; font-size:10px; font-weight:bold;"><i class="fa-solid fa-arrow-left"></i> Upstream</span>`;

    const viaLabel = isPassThrough
      ? "Kabel ini"
      : conn.via_cable_name
        ? escapeHtml(conn.via_cable_name) +
          (conn.via_core ? ` (${escapeHtml(conn.via_core)})` : "")
        : conn.via_cable_id
          ? "Kabel #" + Number(conn.via_cable_id)
          : "Langsung / Direct";

    rows += `
      <tr style="border-bottom: 1px solid #f1f5f9;">
        <td style="padding: 8px; font-weight: 600; color: #1e293b;">${escapeHtml(localPort)}</td>
        <td style="padding: 8px; text-align: center;">${directionBadge}</td>
        <td style="padding: 8px; color: #0284c7; font-weight: 500;">${viaLabel}</td>
        <td style="padding: 8px; font-weight: 500;">${escapeHtml(targetAssetName)}</td>
        <td style="padding: 8px; font-weight: 500;">${escapeHtml(targetPort)}</td>
        <td style="padding: 8px; text-align: center;">
          <button onclick="disconnectCore(${Number(conn.id)})" style="background:#ef4444; color:white; border:none; padding:4px 8px; border-radius:4px; cursor:pointer; font-size:11px;">
            <i class="fa-solid fa-trash"></i> Putus
          </button>
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
  return `<b>${escapeHtml(e.name)}</b> <span style="color:#94a3b8;">[${escapeHtml(e.port || "-")}]</span>`;
}

function traceHopHtml(h, showCustomers) {
  const via = h.via
    ? `<span style="color:#0284c7;"> &mdash; ${escapeHtml(h.via.name || "Kabel #" + h.via.cable_id)}${h.via.core ? " &middot; " + escapeHtml(h.via.core) : ""} &rarr; </span>`
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
            (r.notes
              ? `<div style="color:#64748b;">${escapeHtml(r.notes)}</div>`
              : "") +
            `</div>`
          );
        })
        .join("")
    : `<span style="color:#94a3b8;">Belum ada perbaikan tercatat.</span>`;

  const form = document.getElementById("incd-repair-form");
  form.style.display = resolved ? "none" : "block";
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
  document.getElementById("incd-status-actions").innerHTML = resolved
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
function getCableStyle(type, status) {
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
function showSearchHighlight(lat, lng, html) {
  if (searchHighlightMarker) map.removeLayer(searchHighlightMarker);
  map.flyTo([lat, lng], 18, { duration: 1.5 });
  searchHighlightMarker = L.marker([lat, lng])
    .addTo(map)
    .bindPopup(html)
    .openPopup();
}

function handleMapSearch() {
  const query = document.getElementById("map-search-input").value.trim();
  if (!query) return;

  // 1. Input berupa koordinat "lat, lng"
  const coord = query.match(
    /^([-+]?\d+(?:\.\d+)?)\s*,\s*([-+]?\d+(?:\.\d+)?)$/,
  );
  if (coord) {
    const lat = parseFloat(coord[1]);
    const lng = parseFloat(coord[2]);
    if (Math.abs(lat) > 90 || Math.abs(lng) > 180) {
      return alert("Koordinat di luar jangkauan (lat -90..90, lng -180..180).");
    }
    showSearchHighlight(
      lat,
      lng,
      `<b>Titik Koordinat Cari:</b><br>${lat}, ${lng}`,
    );
    return;
  }

  // 2. Input berupa nama aset (cocok persis diprioritaskan)
  if (!allInventoryData.length) {
    return alert(
      "Data inventaris belum dimuat sempurna, coba beberapa saat lagi.",
    );
  }

  const q = query.toLowerCase();
  const asset =
    allInventoryData.find((i) => (i.name || "").toLowerCase() === q) ||
    allInventoryData.find((i) => (i.name || "").toLowerCase().includes(q));

  if (!asset) return alert(`Aset atau koordinat "${query}" tidak ditemukan.`);

  document.getElementById("search-autocomplete-results").style.display = "none";
  selectSearchRecommendation(asset.category.toLowerCase(), asset.id);
}

// ==========================================
// FITUR LIVE SEARCH & REKOMENDASI ASET
// ==========================================
function handleLiveSearch(query) {
  const dropdown = document.getElementById("search-autocomplete-results");
  if (!dropdown) return;

  const keyword = query.trim().toLowerCase();
  if (keyword.length < 2) {
    dropdown.innerHTML = "";
    dropdown.style.display = "none";
    return;
  }

  const has = (v) => v && String(v).toLowerCase().includes(keyword);
  const matches = allInventoryData
    .filter((i) => has(i.name) || has(i.type) || has(i.city) || has(i.cluster))
    .slice(0, 8);

  if (matches.length === 0) {
    dropdown.innerHTML = `
      <div class="autocomplete-item" style="cursor: default; color: #94a3b8; padding: 10px 12px; font-size: 12px;">
        <span>Aset tidak ditemukan</span>
      </div>`;
    dropdown.style.display = "block";
    return;
  }

  dropdown.innerHTML = matches
    .map(
      (item) => `
      <div class="autocomplete-item" onclick="selectSearchRecommendation('${item.category.toLowerCase()}', ${Number(item.id)})">
        <div>
          <div class="item-title"><i class="${getItemIconClass(item.type)}" style="margin-right: 6px;"></i>${escapeHtml(item.name)}</div>
          <div class="item-subtitle">${escapeHtml(item.type)} • ${escapeHtml(item.cluster || "General")} (${escapeHtml(item.city || "Area")})</div>
        </div>
        <span class="item-badge">${escapeHtml(item.status || "Active")}</span>
      </div>
    `,
    )
    .join("");

  dropdown.style.display = "block";
}

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
  }
});
