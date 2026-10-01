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
        iconClass = "fa-solid fa-ellipsis-vertical"; // Ikon 3 titik lurus tegak / garis tiang
        bgClass = "icon-tiang";
        break;
      case "SLACK":
        iconClass = "fa-solid fa-circle";
        bgClass = "icon-slack";
        break;
    }
  }

  return L.divIcon({
    className: `custom-map-icon ${bgClass}`, // Menempelkan class latar langsung di container Leaflet
    html: `<i class="${iconClass}"></i>`,
    iconSize: [28, 28],
    iconAnchor: [14, 14],
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
            color = "#2638dc";
            weight = 6;
          } else if (type === "Feeder") {
            color = "#ebeb25";
            weight = 4.5;
          } else if (type === "Distribution") {
            color = "#0891b2";
            weight = 3;
          } else if (type === "Drop") {
            color = "#d97706";
            weight = 2;
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

// --- EVENT LISTENER CREATE (MENGGUNAKAN FORM MODAL INTERAKTIF) ---
map.on(L.Draw.Event.CREATED, function (event) {
  const layer = event.layer;
  const type = event.layerType;

  if (type === "marker") {
    const latlng = layer.getLatLng();
    // Buka Form Modal untuk Node/Point (ODP, POP, Closure, Pelanggan, dll)
    openAddAssetModal("NODE", parseFloat(latlng.lat), parseFloat(latlng.lng));
  } else if (type === "polyline") {
    const latlngs = layer.getLatLngs();
    const coords = latlngs.map((pt) => [
      parseFloat(pt.lng),
      parseFloat(pt.lat),
    ]);
    // Buka Form Modal untuk Kabel/Jalur
    openAddAssetModal("CABLE", null, null, coords);
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

// Pemetaan Cluster -> Area
const AREA_MAPPING = {
  EKO: ["BALIKPAPAN", "SAMARINDA", "BANJARMASIN", "TANJUNG SELOR"],
  WKO: ["PONTIANAK", "PALANGKARAYA"],
};

function updateAreaOptions() {
  const clusterSelect = document.getElementById("asset-cluster");
  const areaSelect = document.getElementById("asset-area");
  const selectedCluster = clusterSelect.value;

  areaSelect.innerHTML = "";
  const areas = AREA_MAPPING[selectedCluster] || [];

  areas.forEach((area) => {
    const opt = document.createElement("option");
    opt.value = area;
    opt.textContent = area;
    areaSelect.appendChild(opt);
  });
}

function fetchCityName(lat, lng) {
  const cityInput = document.getElementById("asset-city");
  cityInput.value = "Mendeteksi lokasi...";

  fetch(
    `https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat=${lat}&lon=${lng}`,
  )
    .then((res) => res.json())
    .then((data) => {
      const addr = data.address || {};
      const cityName =
        addr.city ||
        addr.town ||
        addr.city_district ||
        addr.county ||
        addr.state ||
        "Kota Tidak Diketahui";
      cityInput.value = cityName;
    })
    .catch((err) => {
      console.error("Geocoding failed:", err);
      cityInput.value = "Kota Banjarmasin"; // Fallback default jika offline/error
    });
}

function openAddAssetModal(typeCategory, lat, lng, coordsArr = null) {
  document.getElementById("modal-add-asset").style.display = "flex";
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
    fetchCityName(lat, lng);
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
            <option value="Dropcore">Kabel Dropcore</option>
        `;
    // Geocode titik pertama kabel
    fetchCityName(coordsArr[0][1], coordsArr[0][0]);
  }

  onAssetTypeChange();
}

function closeAddAssetModal() {
  document.getElementById("modal-add-asset").style.display = "none";
}

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
  } else if (selectedType === "POP") {
    capacitySelect.innerHTML = `
            <option value="12C">12 Core (12C)</option>
            <option value="24C">24 Core (24C)</option>
            <option value="48C">48 Core (48C)</option>
            <option value="96C">96 Core (96C)</option>
            <option value="144C">144 Core (144C)</option>
        `;
  } else if (selectedType === "CLOSURE") {
    capacitySelect.innerHTML = `
            <option value="12C">12 Core (12C)</option>
            <option value="24C">24 Core (24C)</option>
            <option value="48C">48 Core (48C)</option>
            <option value="96C">96 Core (96C)</option>
            <option value="144C">144 Core (144C)</option>
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
    capacitySelect.innerHTML = `<option value="1 Port">1 Port</option>`;
  } else {
    // Pilihan Kapasitas Kabel (Feeder, Distribution, Backbone, Dropcore)
    capacitySelect.innerHTML = `
            <option value="12C">12 Core (12C)</option>
            <option value="24C">24 Core (24C)</option>
            <option value="48C">48 Core (48C)</option>
            <option value="96C">96 Core (96C)</option>
            <option value="144C">144 Core (144C)</option>
        `;
  }
}

function saveAssetData(e) {
  e.preventDefault();

  const categoryType = document.getElementById("asset-category-type").value;
  const name = document.getElementById("asset-name").value;
  const type = document.getElementById("asset-type").value;
  const status = document.getElementById("asset-status").value;
  const cluster = document.getElementById("asset-cluster").value;
  const area = document.getElementById("asset-area").value;
  const city = document.getElementById("asset-city").value;
  const capacity = document.getElementById("asset-capacity").value;

  if (categoryType === "NODE") {
    const lat = parseFloat(document.getElementById("asset-lat").value);
    const lng = parseFloat(document.getElementById("asset-lng").value);

    const payload = {
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
    };

    fetch("/api/nodes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then((r) => r.json())
      .then((res) => {
        alert("Node berhasil disimpan!");
        closeAddAssetModal();
        location.reload(); // Refresh peta & data
      });
  } else {
    const coordsArr = JSON.parse(
      document.getElementById("asset-coords-json").value,
    );

    const payload = {
      name,
      type,
      status,
      coordinates: coordsArr,
      cluster,
      area,
      city,
      capacity,
      core_data: "{}",
    };

    fetch("/api/cables", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then((r) => r.json())
      .then((res) => {
        alert("Kabel berhasil disimpan!");
        closeAddAssetModal();
        location.reload(); // Refresh peta & data
      });
  }
}

// Variable global penampung data inventory
let allInventoryData = [];

function openInventoryModal() {
  document.getElementById("modal-inventory").style.display = "flex";
  fetchInventoryData();
}

function closeInventoryModal() {
  document.getElementById("modal-inventory").style.display = "none";
}

function fetchInventoryData() {
  // Ambil data Nodes dan Cables secara paralel
  Promise.all([
    fetch("/api/nodes").then((r) => r.json()),
    fetch("/api/cables").then((r) => r.json()),
  ])
    .then(([nodesData, cablesData]) => {
      const nodes = (nodesData.features || []).map((f) => ({
        ...f.properties,
        category: "NODE",
      }));
      const cables = (cablesData.features || []).map((f) => ({
        ...f.properties,
        category: "CABLE",
      }));

      allInventoryData = [...nodes, ...cables];
      renderInventoryTable();
    })
    .catch((err) => console.error("Error loading inventory:", err));
}

function renderInventoryTable() {
  const clusterFilter = document.getElementById("filter-cluster").value;
  const typeFilter = document.getElementById("filter-type").value;
  const searchQuery = document
    .getElementById("inventory-search")
    .value.toLowerCase();
  const tbody = document.getElementById("inventory-table-body");

  tbody.innerHTML = "";

  const filtered = allInventoryData.filter((item) => {
    const matchCluster =
      clusterFilter === "ALL" || item.cluster === clusterFilter;

    let matchType = true;
    if (typeFilter === "CABLE") {
      matchType = item.category === "CABLE";
    } else if (typeFilter !== "ALL") {
      matchType = item.type === typeFilter;
    }

    const matchSearch =
      (item.name || "").toLowerCase().includes(searchQuery) ||
      (item.city || "").toLowerCase().includes(searchQuery);

    return matchCluster && matchType && matchSearch;
  });

  if (filtered.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; padding: 20px; color: #94a3b8;">Tidak ada data aset ditemukan</td></tr>`;
    return;
  }

  filtered.forEach((item) => {
    const tr = document.createElement("tr");
    tr.style.borderBottom = "1px solid #f1f5f9";

    let badgeStatus = `#10b981`; // Active
    if (item.status === "Maintenance") badgeStatus = `#f59e0b`;
    if (item.status === "Cut/Broken") badgeStatus = `#ef4444`;

    tr.innerHTML = `
            <td style="padding: 10px; font-weight: 600;">${item.name}</td>
            <td style="padding: 10px;"><span style="background: #e2e8f0; padding: 2px 6px; border-radius: 4px; font-size: 11px;">${item.type}</span></td>
            <td style="padding: 10px;">${item.cluster || "-"} / ${item.area || "-"}</td>
            <td style="padding: 10px;">${item.city || "-"}</td>
            <td style="padding: 10px; font-weight: 600;">${item.capacity || "-"}</td>
            <td style="padding: 10px;">
                <span style="color: white; background: ${badgeStatus}; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 500;">
                    ${item.status}
                </span>
            </td>
            <td style="padding: 10px; text-align: center;">
                <button onclick="openCoreDetailModal(${item.id}, '${item.category}', '${item.name}')" 
                        style="background: #2563eb; color: white; border: none; padding: 4px 8px; border-radius: 4px; cursor: pointer; font-size: 11px;" title="Lihat Status Core / Port">
                    <i class="fa-solid fa-diagram-project"></i> Core/Port
                </button>
                <button onclick="${item.category === "NODE" ? `deleteNode(${item.id}, '${item.name}')` : `deleteCable(${item.id}, '${item.name}')`}" 
                        style="background: #ef4444; color: white; border: none; padding: 4px 6px; border-radius: 4px; cursor: pointer; font-size: 11px; margin-left: 4px;" title="Hapus Aset">
                    <i class="fa-solid fa-trash"></i>
                </button>
            </td>
        `;
    tbody.appendChild(tr);
  });
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

// --- FUNGSI BUKA MODAL DETAIL CORE / PORT ---
function openCoreDetailModal(id, category, name) {
  const modal = document.getElementById("modal-core-detail");
  const title = document.getElementById("core-modal-title");
  const infoContainer = document.getElementById("asset-detail-info");
  const gridContainer = document.getElementById("core-grid-container");

  modal.style.display = "flex";
  title.innerHTML = `<i class="fa-solid fa-diagram-project"></i> Detail Status: ${name}`;

  const item = allInventoryData.find(
    (x) => x.id === id && x.category === category,
  );

  if (!item) {
    gridContainer.innerHTML =
      '<p style="color: #ef4444;">Data aset tidak ditemukan.</p>';
    return;
  }

  infoContainer.innerHTML = `
    <div><b>Tipe Aset:</b> ${item.type}</div>
    <div><b>Cluster / Area:</b> ${item.cluster || "-"} / ${item.area || "-"}</div>
    <div><b>Kapasitas / Konfigurasi:</b> ${item.capacity || "-"}</div>
  `;

  const assetType = (item.type || "").toUpperCase();

  // KABEL, CLOSURE, dan POP menggunakan tampilan ISO Core Colors
  if (category === "CABLE" || assetType === "CLOSURE" || assetType === "POP") {
    renderCableCores(item, gridContainer);
  } else if (assetType === "ODP") {
    renderODPSplitterGrid(item, gridContainer);
  } else {
    renderNodePorts(item, gridContainer);
  }
}
function closeCoreDetailModal() {
  document.getElementById("modal-core-detail").style.display = "none";
}

// --- RENDER CORE KABEL (BERDASARKAN TUBE & CORE COLOR) ---
// --- RENDER CORE KABEL / CLOSURE / POP (STANDAR TUBE & CORE COLOR ISO) ---
function renderCableCores(item, container) {
  const type = (item.type || "").toUpperCase();
  let labelHeader = "Core Kabel";
  if (type === "CLOSURE") labelHeader = "Joint Closure Core";
  if (type === "POP") labelHeader = "ODF / Patch Panel POP Core";

  let totalCores = parseInt(item.capacity) || 12;

  let html = `<h4 style="margin-bottom: 12px; font-size: 14px; color: #334155;">Visualisasi ${labelHeader} (${item.capacity || totalCores + "C"})</h4>`;
  html += `<div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap: 10px;">`;

  for (let i = 1; i <= totalCores; i++) {
    const colorIndex = (i - 1) % 12;
    const tubeNo = Math.floor((i - 1) / 12) + 1;
    const colorInfo = ISO_FIBER_COLORS[colorIndex];

    html += `
      <div style="border: 1px solid #e2e8f0; border-radius: 6px; padding: 8px; text-align: center; background: #fafafa;">
        <div style="font-size: 10px; color: #64748b; margin-bottom: 4px;">
          Tube ${tubeNo} - Core ${i}
        </div>
        <div style="background-color: ${colorInfo.hex}; color: ${colorInfo.text}; border: ${colorInfo.border ? "1px solid " + colorInfo.border : "none"};
                    padding: 6px; border-radius: 4px; font-weight: bold; font-size: 12px; box-shadow: inset 0 0 4px rgba(0,0,0,0.2);">
          ${colorInfo.name}
        </div>
        <div style="font-size: 10px; margin-top: 5px; color: #16a34a; font-weight: 600;">Available</div>
      </div>
    `;
  }

  html += `</div>`;
  container.innerHTML = html;
}

function renderODPSplitterGrid(odp, container) {
  const cap = odp.capacity || "1 In - 8 Out";

  // Parsing jumlah IN dan OUT dari string kapasitas (misal: "2 In - 8 Out")
  let inCount = 1;
  let outCount = 8;

  const matches = cap.match(/(\d+)\s*In\s*-\s*(\d+)\s*Out/i);
  if (matches) {
    inCount = parseInt(matches[1]);
    outCount = parseInt(matches[2]);
  }

  let html = `<h4 style="margin-bottom: 12px; font-size: 14px; color: #334155;">Konfigurasi Splitter ODP (${cap})</h4>`;

  // --- 1. SECTION INPUT PORTS ---
  html += `<div style="margin-bottom: 16px; background: #f0fdf4; border: 1px solid #bbf7d0; padding: 12px; border-radius: 8px;">`;
  html += `<div style="font-size: 12px; font-weight: bold; color: #166534; margin-bottom: 8px;"><i class="fa-solid fa-arrow-down-to-line"></i> Input Ports (${inCount} IN)</div>`;
  html += `<div style="display: flex; gap: 10px;">`;
  for (let i = 1; i <= inCount; i++) {
    html += `
      <div style="border: 2px solid #16a34a; background: #ffffff; border-radius: 6px; padding: 8px 14px; text-align: center;">
        <div style="font-size: 10px; font-weight: bold; color: #15803d;">IN-${i}</div>
        <i class="fa-solid fa-plug-circle-bolt" style="font-size: 16px; color: #16a34a; margin: 4px 0;"></i>
        <div style="font-size: 10px; color: #166534;">Connected</div>
      </div>
    `;
  }
  html += `</div></div>`;

  // --- 2. SECTION OUTPUT PORTS ---
  html += `<div style="background: #f8fafc; border: 1px solid #e2e8f0; padding: 12px; border-radius: 8px;">`;
  html += `<div style="font-size: 12px; font-weight: bold; color: #334155; margin-bottom: 8px;"><i class="fa-solid fa-network-wired"></i> Output Ports (${outCount} OUT)</div>`;
  html += `<div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(85px, 1fr)); gap: 10px;">`;
  for (let o = 1; o <= outCount; o++) {
    html += `
      <div style="border: 1px dashed #cbd5e1; background: #ffffff; border-radius: 6px; padding: 8px; text-align: center;">
        <div style="font-size: 10px; font-weight: bold; color: #64748b;">OUT-${o}</div>
        <i class="fa-solid fa-plug" style="font-size: 16px; color: #94a3b8; margin: 4px 0;"></i>
        <div style="font-size: 9px; color: #94a3b8;">Idle</div>
      </div>
    `;
  }
  html += `</div></div>`;

  container.innerHTML = html;
}

// --- RENDER PORT GRID ODP / NODE ---
function renderNodePorts(node, container) {
  let totalPorts = parseInt(node.capacity) || 8;

  let html = `<h4 style="margin-bottom: 12px; font-size: 14px; color: #334155;">Grid Port ${node.type} (${totalPorts} Port)</h4>`;
  html += `<div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(90px, 1fr)); gap: 10px;">`;

  for (let p = 1; p <= totalPorts; p++) {
    html += `
      <div style="border: 2px solid #cbd5e1; border-radius: 6px; padding: 10px; text-align: center; background: #ffffff;">
        <div style="font-size: 11px; font-weight: bold; color: #475569; margin-bottom: 4px;">Port ${p}</div>
        <i class="fa-solid fa-plug" style="font-size: 18px; color: #94a3b8;"></i>
        <div style="font-size: 10px; margin-top: 6px; color: #64748b;">Idle</div>
      </div>
    `;
  }

  html += `</div>`;
  container.innerHTML = html;
}
