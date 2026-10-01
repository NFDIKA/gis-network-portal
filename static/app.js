// --- INISIALISASI PETA ---
const map = L.map("map", { zoomControl: false }).setView([-3.323, 114.593], 14);
L.control.zoom({ position: "topleft" }).addTo(map);

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
const incidentGroup = L.layerGroup().addTo(map);

const backboneGroup = L.layerGroup().addTo(map);
const feederGroup = L.layerGroup().addTo(map);
const distGroup = L.layerGroup().addTo(map);
const dropGroup = L.layerGroup().addTo(map);

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
  "Titik Incident": incidentGroup,
};
L.control
  .layers(baseMaps, overlayMaps, { position: "topright", collapsed: false })
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

// --- HELPER FUNCTIONS ---
function calculatePolylineLength(coordinates) {
  let totalDistance = 0;
  for (let i = 0; i < coordinates.length - 1; i++) {
    const coord1 = L.latLng(coordinates[i][1], coordinates[i][0]);
    const coord2 = L.latLng(coordinates[i + 1][1], coordinates[i + 1][0]);
    totalDistance += coord1.distanceTo(coord2);
  }
  return totalDistance;
}

function createCustomIcon(type, status) {
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
        bgClass = "icon-closure";
        break;
      case "PELANGGAN":
        iconClass = "fa-solid fa-house-user";
        bgClass = "icon-pelanggan";
        break;
      case "TIANG":
        iconClass = "fa-solid fa-ellipsis-vertical";
        bgClass = "icon-tiang";
        break;
    }
  }

  return L.divIcon({
    className: "custom-div-icon",
    html: `<div class="custom-map-icon ${bgClass}" style="width: 30px; height: 30px;"><i class="${iconClass}"></i></div>`,
    iconSize: [30, 30],
    iconAnchor: [15, 15],
  });
}

function loadDashboardSummary() {
  fetch("/api/dashboard/summary")
    .then((res) => res.json())
    .then((data) => {
      document.getElementById("stat-odp").innerText = data.total_odp;
      document.getElementById("stat-cables").innerText = data.total_cables;
      document.getElementById("stat-incidents").innerText =
        data.total_incidents;
    });
}

// --- API ACTIONS ---
function updateNodeStatus(id, newStatus) {
  fetch(`/api/nodes/${id}/status`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status: newStatus }),
  }).then((res) => {
    if (res.ok) loadData();
  });
}

function editNodeProperties(
  id,
  currentName,
  currentType,
  currentStatus,
  lat,
  lng,
) {
  const newName = prompt("Ubah Nama Aset:", currentName);
  if (newName === null) return;
  const newType = prompt(
    "Ubah Tipe Aset:\n(ODP / POP / CLOSURE / PELANGGAN / TIANG / INCIDENT)",
    currentType,
  );
  if (newType === null) return;

  fetch(`/api/nodes/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: newName,
      type: newType.trim().toUpperCase(),
      status: currentStatus,
      latitude: lat,
      longitude: lng,
    }),
  }).then((res) => {
    if (res.ok) loadData();
  });
}

function deleteNode(id, name) {
  if (confirm(`Apakah Anda yakin ingin menghapus marker "${name}"?`)) {
    fetch(`/api/nodes/${id}`, { method: "DELETE" }).then((res) => {
      if (res.ok) loadData();
    });
  }
}

function updateCableStatus(id, newStatus) {
  fetch(`/api/cables/${id}/status`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status: newStatus }),
  }).then((res) => {
    if (res.ok) loadData();
  });
}

function editCableProperties(
  id,
  currentName,
  currentType,
  currentStatus,
  coords,
) {
  const newName = prompt("Ubah Nama Jalur Kabel:", currentName);
  if (newName === null) return;
  const newType = prompt(
    "Ubah Tipe Kabel:\n(Backbone / Feeder / Distribution / Drop)",
    currentType,
  );
  if (newType === null) return;

  fetch(`/api/cables/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: newName,
      type: newType.trim(),
      status: currentStatus,
      coordinates: coords,
    }),
  }).then((res) => {
    if (res.ok) loadData();
  });
}

function deleteCable(id, name) {
  if (confirm(`Apakah Anda yakin ingin menghapus jalur kabel "${name}"?`)) {
    fetch(`/api/cables/${id}`, { method: "DELETE" }).then((res) => {
      if (res.ok) loadData();
    });
  }
}

// --- RENDER DATA FROM DATABASE ---
function loadData() {
  popGroup.clearLayers();
  odpGroup.clearLayers();
  closureGroup.clearLayers();
  pelangganGroup.clearLayers();
  tiangGroup.clearLayers();
  incidentGroup.clearLayers();
  backboneGroup.clearLayers();
  feederGroup.clearLayers();
  distGroup.clearLayers();
  dropGroup.clearLayers();
  editableGroup.clearLayers();

  loadDashboardSummary();

  // LOAD NODES
  fetch("/api/nodes")
    .then((res) => res.json())
    .then((data) => {
      L.geoJSON(data, {
        pointToLayer: function (feature, latlng) {
          const props = feature.properties;
          const markerIcon = createCustomIcon(props.type, props.status);
          const marker = L.marker(latlng, { icon: markerIcon });

          marker.metaType = "node";
          marker.metaData = props;

          const isIncident = props.status === "Cut/Broken";
          const btnText = isIncident
            ? "Set Normal (Active)"
            : "Report Incident";
          const btnClass = isIncident ? "btn-success" : "btn-danger";
          const nextStatus = isIncident ? "Active" : "Cut/Broken";

          marker.bindPopup(`
                        <div class="popup-header">${props.name}</div>
                        <div class="popup-row"><span>Kategori:</span> <b>${props.type}</b></div>
                        <div class="popup-row"><span>Latitude:</span> <b>${latlng.lat.toFixed(6)}</b></div>
                        <div class="popup-row"><span>Longitude:</span> <b>${latlng.lng.toFixed(6)}</b></div>
                        <div class="popup-row"><span>Status:</span> <b>${props.status}</b></div>
                        
                        <div class="popup-actions">
                            <button class="btn-status ${btnClass}" onclick="updateNodeStatus(${props.id}, '${nextStatus}')">
                                <i class="fa-solid fa-power-off"></i> ${btnText}
                            </button>
                            <button class="btn-status btn-warning" onclick="editNodeProperties(${props.id}, '${props.name}', '${props.type}', '${props.status}', ${latlng.lat}, ${latlng.lng})">
                                <i class="fa-solid fa-pen"></i> Edit Info Aset
                            </button>
                            <button class="btn-status btn-outline-danger" onclick="deleteNode(${props.id}, '${props.name}')">
                                <i class="fa-solid fa-trash"></i> Hapus Aset
                            </button>
                        </div>
                    `);

          if (props.type === "POP") popGroup.addLayer(marker);
          else if (props.type === "CLOSURE") closureGroup.addLayer(marker);
          else if (props.type === "PELANGGAN") pelangganGroup.addLayer(marker);
          else if (props.type === "TIANG") tiangGroup.addLayer(marker);
          else if (props.type === "INCIDENT" || isIncident)
            incidentGroup.addLayer(marker);
          else odpGroup.addLayer(marker);

          editableGroup.addLayer(marker);
          return marker;
        },
      });
    });

  // LOAD CABLES
  fetch("/api/cables")
    .then((res) => res.json())
    .then((data) => {
      L.geoJSON(data, {
        style: function (feature) {
          const type = feature.properties.type;
          const isCut = feature.properties.status === "Cut/Broken";

          let color = "#0891b2";
          let weight = 3;
          let dashArray = null;

          if (type === "Backbone") {
            color = "#dc2626";
            weight = 6;
          } else if (type === "Feeder") {
            color = "#2563eb";
            weight = 4.5;
          } else if (type === "Distribution") {
            color = "#0891b2";
            weight = 3;
          } else if (type === "Drop") {
            color = "#d97706";
            weight = 2;
            dashArray = "4, 4";
          }

          if (isCut) {
            color = "#ef4444";
            dashArray = "6, 8";
          }

          return {
            color: color,
            weight: weight,
            dashArray: dashArray,
            opacity: 0.9,
          };
        },
        onEachFeature: function (feature, layer) {
          const props = feature.properties;
          const coords = feature.geometry.coordinates;

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
                        <div class="popup-header">${props.name}</div>
                        <div class="popup-row"><span>Tipe Jalur:</span> <b>Kabel ${props.type}</b></div>
                        <div class="popup-row"><span>Panjang Kabel:</span> <b>${formattedLength}</b></div>
                        <div class="popup-row"><span>Status:</span> <b>${props.status}</b></div>

                        <div class="popup-actions">
                            <button class="btn-status ${btnClass}" onclick="updateCableStatus(${props.id}, '${nextStatus}')">
                                <i class="fa-solid fa-power-off"></i> ${btnText}
                            </button>
                            <button class="btn-status btn-warning" onclick='editCableProperties(${props.id}, "${props.name}", "${props.type}", "${props.status}", ${JSON.stringify(coords)})'>
                                <i class="fa-solid fa-pen"></i> Edit Info Kabel
                            </button>
                            <button class="btn-status btn-outline-danger" onclick="deleteCable(${props.id}, '${props.name}')">
                                <i class="fa-solid fa-trash"></i> Hapus Kabel
                            </button>
                        </div>
                    `);

          if (props.type === "Backbone") backboneGroup.addLayer(layer);
          else if (props.type === "Feeder") feederGroup.addLayer(layer);
          else if (props.type === "Drop") dropGroup.addLayer(layer);
          else distGroup.addLayer(layer);

          editableGroup.addLayer(layer);
        },
      });
    });
}

loadData();

// --- EVENT LISTENER: LEAFLET DRAW (EDIT & CREATE) ---
map.on(L.Draw.Event.EDITED, function (e) {
  const layers = e.layers;
  layers.eachLayer(function (layer) {
    if (layer.metaType === "node") {
      const latlng = layer.getLatLng();
      fetch(`/api/nodes/${layer.metaData.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          latitude: parseFloat(latlng.lat),
          longitude: parseFloat(latlng.lng),
        }),
      }).then(() => loadData());
    } else if (layer.metaType === "cable") {
      const latlngs = layer.getLatLngs();
      const newCoords = latlngs.map((pt) => [
        parseFloat(pt.lng),
        parseFloat(pt.lat),
      ]);
      fetch(`/api/cables/${layer.metaData.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ coordinates: newCoords }),
      }).then(() => loadData());
    }
  });
});

map.on(L.Draw.Event.CREATED, function (event) {
  const layer = event.layer;
  const type = event.layerType;

  if (type === "marker") {
    const latlng = layer.getLatLng();
    const name = prompt(
      "Nama Titik (misal: ODP-B2/05, Tiang 7m A-12, Rumah Bpk Ahmad):",
    );
    if (!name) return;
    const nodeType = prompt(
      "Pilih Tipe Titik:\n(ODP / POP / CLOSURE / PELANGGAN / TIANG / INCIDENT)",
      "ODP",
    );

    fetch("/api/nodes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: name,
        type: nodeType ? nodeType.trim().toUpperCase() : "ODP",
        status:
          nodeType && nodeType.toUpperCase() === "INCIDENT"
            ? "Cut/Broken"
            : "Active",
        latitude: parseFloat(latlng.lat),
        longitude: parseFloat(latlng.lng),
      }),
    }).then((res) => {
      if (res.ok) loadData();
    });
  } else if (type === "polyline") {
    const latlngs = layer.getLatLngs();
    const coords = latlngs.map((pt) => [
      parseFloat(pt.lng),
      parseFloat(pt.lat),
    ]);
    const name = prompt("Nama Jalur Kabel:");
    if (!name) return;
    const cableType = prompt(
      "Pilih Tipe Jalur:\n(Backbone / Feeder / Distribution / Drop)",
      "Feeder",
    );

    fetch("/api/cables", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: name,
        type: cableType ? cableType.trim() : "Distribution",
        status: "Active",
        coordinates: coords,
      }),
    }).then((res) => {
      if (res.ok) loadData();
    });
  }
});

// --- MODAL ASSET INVENTORY LOGIC ---
let inventoryCache = [];

function openInventoryModal() {
  document.getElementById("modal-inventory").style.display = "flex";
  fetchInventoryData();
}

function closeInventoryModal() {
  document.getElementById("modal-inventory").style.display = "none";
}

function fetchInventoryData() {
  Promise.all([
    fetch("/api/nodes").then((r) => r.json()),
    fetch("/api/cables").then((r) => r.json()),
  ]).then(([nodesData, cablesData]) => {
    inventoryCache = [];

    // Parsing Nodes (ODP, POP, TIANG, dll)
    nodesData.features.forEach((f) => {
      inventoryCache.push({
        id: f.properties.id,
        name: f.properties.name,
        category: f.properties.type,
        typeGroup: f.properties.type,
        status: f.properties.status,
        details: `${f.geometry.coordinates[1].toFixed(5)}, ${f.geometry.coordinates[0].toFixed(5)}`,
        lat: f.geometry.coordinates[1],
        lng: f.geometry.coordinates[0],
        isCable: false,
      });
    });

    // Parsing Cables
    cablesData.features.forEach((f) => {
      inventoryCache.push({
        id: f.properties.id,
        name: f.properties.name,
        category: `Kabel (${f.properties.type})`,
        typeGroup: "CABLE",
        status: f.properties.status,
        details: `${f.geometry.coordinates.length} titik koordinat`,
        lat: f.geometry.coordinates[0][1],
        lng: f.geometry.coordinates[0][0],
        isCable: true,
      });
    });

    renderInventoryTable(inventoryCache);
  });
}

function renderInventoryTable(data) {
  const tbody = document.getElementById("inventory-table-body");
  tbody.innerHTML = "";

  if (data.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: #94a3b8;">Tidak ada data ditemukan</td></tr>`;
    return;
  }

  data.forEach((item) => {
    const isBroken = item.status === "Cut/Broken";
    const badgeClass = isBroken ? "badge-broken" : "badge-active";

    const tr = document.createElement("tr");
    tr.innerHTML = `
            <td><b>#${item.id}</b></td>
            <td><strong>${item.name}</strong></td>
            <td><span class="badge-status" style="background:#e2e8f0; color:#334155;">${item.category}</span></td>
            <td><span class="badge-status ${badgeClass}">${item.status}</span></td>
            <td><small>${item.details}</small></td>
            <td>
                <button class="btn-locate" onclick="zoomToAsset(${item.lat}, ${item.lng})">
                    <i class="fa-solid fa-crosshairs"></i> Sorot di Peta
                </button>
            </td>
        `;
    tbody.appendChild(tr);
  });
}

function filterInventoryTable() {
  const searchVal = document
    .getElementById("inventory-search")
    .value.toLowerCase();
  const filterType = document.getElementById("inventory-filter-type").value;

  const filtered = inventoryCache.filter((item) => {
    const matchesSearch =
      item.name.toLowerCase().includes(searchVal) ||
      item.category.toLowerCase().includes(searchVal) ||
      item.status.toLowerCase().includes(searchVal);

    let matchesType = true;
    if (filterType === "CABLE") {
      matchesType = item.isCable;
    } else if (filterType !== "ALL") {
      matchesType = item.typeGroup === filterType;
    }

    return matchesSearch && matchesType;
  });

  renderInventoryTable(filtered);
}

function zoomToAsset(lat, lng) {
  closeInventoryModal();
  map.flyTo([lat, lng], 18, { duration: 1.5 });
}
