// Per-page floor config: case-floor.html sets window.FLOOR_CONFIG before this
// script loads; index.html omits it and gets the drone floor defaults below.
const floorConfig = window.FLOOR_CONFIG ?? {};

const zones = floorConfig.zones ?? {
  receiving: {
    name: "Receiving",
    description:
      "Receives supplier deliveries, verifies incoming drone components, and releases accepted parts into kitting.",
    area: "1,800 sq ft",
    flow: "Receiving to Kitting",
    status: "Open"
  },
  raw: {
    name: "Drone Component Kitting",
    description:
      "Receives and stages frames, motors, ESCs, flight controllers, batteries, sensors, propellers, fasteners, and packaging.",
    area: "12,500 sq ft",
    flow: "Receiving to Airframe",
    status: "Kits ready"
  },
  ws1: {
    name: "Workstation 1: Airframe + Motors",
    description:
      "Builds the drone frame, mounts arms and motors, routes motor wires, and checks frame alignment before tightening.",
    area: "3,800 sq ft",
    flow: "Kitting to Electronics",
    status: "Torque verified"
  },
  ws2: {
    name: "Workstation 2: Electronics + Power",
    description:
      "Installs ESCs, flight controller, receiver, power distribution, battery leads, GPS, cameras, and sensor modules.",
    area: "4,100 sq ft",
    flow: "Airframe to Firmware",
    status: "ESD controlled"
  },
  ws3: {
    name: "Workstation 3: Firmware + Calibration",
    description:
      "Flashes firmware, configures flight controller orientation, binds transmitter and receiver, and calibrates sensors.",
    area: "3,200 sq ft",
    flow: "Electronics to Motor Test",
    status: "Profiles loaded"
  },
  ws4: {
    name: "Workstation 4: Motor/ESC Test + Props",
    description:
      "Runs motor direction and throttle tests without propellers, then installs matched clockwise and counterclockwise props.",
    area: "3,450 sq ft",
    flow: "Calibration to QA",
    status: "Guarded test stand"
  },
  ws5: {
    name: "Workstation 5: Final QA + Flight Test",
    description:
      "Verifies stability, flight control, GPS, camera/sensor function, communications, balance, and final acceptance.",
    area: "4,800 sq ft",
    flow: "QA to Packaging",
    status: "Flight cage active"
  },
  fg: {
    name: "Finished Goods: Packaged Drones",
    description:
      "Completes final inspection, documentation, and packaging before transfer to finished goods inventory.",
    area: "9,800 sq ft",
    flow: "Packaged to FG Inventory",
    status: "Ready to move"
  },
  inventory: {
    name: "FG Inventory",
    description:
      "Stores packaged finished drones as ready stock for allocation, picking, and outbound shipment release.",
    area: "7,500 sq ft",
    flow: "Packaged to FG Inventory",
    status: "Available"
  }
};

const body = document.body;
const viewButtons = document.querySelectorAll("[data-view]");
const routeToggle = document.querySelector("#routeToggle");
const detailsToggle = document.querySelector("#detailsToggle");
const detailPanel = document.querySelector("#detailPanel");
const zoneNodes = document.querySelectorAll("[data-zone]");
const sequenceSteps = document.querySelectorAll("[data-step]");
const zoneMetricNodes = document.querySelectorAll("[data-zone-metric]");

const zoneName = document.querySelector("#zoneName");
const zoneDescription = document.querySelector("#zoneDescription");
const zoneArea = document.querySelector("#zoneArea");
const zoneFlow = document.querySelector("#zoneFlow");
const zoneStatus = document.querySelector("#zoneStatus");
const dashboardOrder = document.querySelector("#dashboardOrder");
const dashboardStatus = document.querySelector("#dashboardStatus");
const dashboardStation = document.querySelector("#dashboardStation");
const dashboardProgress = document.querySelector("#dashboardProgress");
const dashboardUtilization = document.querySelector("#dashboardUtilization");
let dashboardByZone = {};
let capacityByZone = {};

function titleCase(value) {
  return value
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function compactOrderList(orders = []) {
  if (orders.length <= 1) {
    return orders.join(", ");
  }
  return `${orders[0]} +${orders.length - 1}`;
}

function setView(view) {
  body.classList.remove("view-process", "view-material", "view-rework");
  body.classList.add(`view-${view}`);

  viewButtons.forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
  });
}

function selectZone(zoneId) {
  const zone = zones[zoneId];
  const dashboard = dashboardByZone[zoneId];

  zoneName.textContent = zone.name;
  zoneDescription.textContent = zone.description;
  zoneArea.textContent = zone.area;
  zoneFlow.textContent = zone.flow;
  zoneStatus.textContent = dashboard
    ? `Queue ${dashboard.queued ?? 0} | WIP ${dashboard.wip} | Done ${dashboard.completed} | Hold ${dashboard.hold} | ${compactOrderList(dashboard.orders) || compactOrderList(dashboard.queued_orders) || "No active order"}`
    : zone.status;

  const capacityRow = document.querySelector("#capacityRow");
  if (capacityRow) {
    const capInfo = capacityByZone[zoneId];
    const isWorkstation = capInfo?.zone_type === "workstation";
    capacityRow.hidden = !isWorkstation;
    if (isWorkstation) {
      document.querySelector("#capacityInput").value = capInfo.capacity ?? "";
      document.querySelector("#capacityMsg").textContent =
        capInfo.capacity ? `${capInfo.capacity} unit(s) at a time` : "Unconstrained";
    }
  }

  zoneNodes.forEach((node) => {
    node.classList.toggle("active", node.dataset.zone === zoneId);
  });

  sequenceSteps.forEach((step) => {
    step.classList.toggle("current", step.dataset.step === zoneId);
  });
}

function metricParts(row) {
  const holdText = row.hold > 0 ? ` | Hold ${row.hold}` : "";
  const capacity = capacityByZone[row.zone_id]?.capacity;
  const capText = capacity ? ` | Cap ${capacity}` : "";
  const orderText = compactOrderList(row.orders) || compactOrderList(row.queued_orders);
  return {
    counts: `Q ${row.queued ?? 0} | WIP ${row.wip} | Done ${row.completed}${holdText}${capText}`,
    order: orderText ? `Order ${orderText}` : ""
  };
}

function renderMetricNode(node, row) {
  const parts = metricParts(row);
  const x = node.getAttribute("x");
  node.textContent = "";

  const countsLine = document.createElementNS("http://www.w3.org/2000/svg", "tspan");
  countsLine.setAttribute("x", x);
  countsLine.textContent = parts.counts;
  node.appendChild(countsLine);

  if (parts.order) {
    const orderLine = document.createElementNS("http://www.w3.org/2000/svg", "tspan");
    orderLine.setAttribute("x", x);
    orderLine.setAttribute("dy", "13");
    orderLine.textContent = parts.order;
    node.appendChild(orderLine);
  }
}

function renderDashboard(data) {
  dashboardByZone = {};
  capacityByZone = data.capacities ?? capacityByZone;
  data.zones.forEach((row) => {
    dashboardByZone[row.zone_id] = row;
  });

  if (data.summary) {
    dashboardOrder.textContent = data.summary.display_order_no ?? "None";
    dashboardStatus.textContent = titleCase(data.summary.production_status ?? "none");
    dashboardStation.textContent = data.summary.current_zone ?? "No active order";
    dashboardProgress.textContent = `${data.summary.percent_complete ?? 0}%`;
    dashboardUtilization.textContent = `${data.summary.actual_time_utilization_percent ?? 0}%`;
  }

  zoneMetricNodes.forEach((node) => {
    const row = dashboardByZone[node.dataset.zoneMetric] ?? {
      zone_id: node.dataset.zoneMetric,
      wip: 0,
      completed: 0,
      hold: 0,
      queued: 0,
      orders: [],
      queued_orders: []
    };
    renderMetricNode(node, row);
  });

  const activeZone = document.querySelector(".zone.active")?.dataset.zone;
  if (activeZone) {
    selectZone(activeZone);
  }
}

async function loadDashboard() {
  if (window.location.protocol === "file:") {
    return;
  }

  const facilityQuery = floorConfig.facility ? `?facility=${floorConfig.facility}` : "";
  const response = await fetch(`/api/floor-dashboard${facilityQuery}`);
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error ?? "Unable to load floor dashboard.");
  }
  renderDashboard(data);
}

viewButtons.forEach((button) => {
  button.addEventListener("click", () => setView(button.dataset.view));
});

routeToggle.addEventListener("change", () => {
  body.classList.toggle("routes-hidden", !routeToggle.checked);
});

function setDetailsVisible(isVisible) {
  body.classList.toggle("details-hidden", !isVisible);
  detailPanel.hidden = !isVisible;
  detailsToggle.textContent = isVisible ? "Hide Details" : "Show Details";
  detailsToggle.setAttribute("aria-expanded", String(isVisible));
}

detailsToggle.addEventListener("click", () => {
  setDetailsVisible(body.classList.contains("details-hidden"));
});

const capacitySaveButton = document.querySelector("#capacitySave");
if (capacitySaveButton) {
  capacitySaveButton.addEventListener("click", async () => {
    const zoneId = document.querySelector(".zone.active")?.dataset.zone;
    const raw = document.querySelector("#capacityInput").value.trim();
    const message = document.querySelector("#capacityMsg");
    if (!zoneId) {
      return;
    }
    message.textContent = "Saving…";
    try {
      const response = await fetch("/api/zone-capacity", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ zoneId, capacity: raw === "" ? null : Number(raw) })
      });
      const data = await response.json();
      if (!response.ok || data.error) {
        message.textContent = data.error ?? "Save failed.";
        return;
      }
      if (capacityByZone[zoneId]) {
        capacityByZone[zoneId].capacity = data.capacity;
      }
      message.textContent = data.capacity
        ? `Saved: ${data.capacity} unit(s) at a time`
        : "Saved: unconstrained";
      loadDashboard().catch(() => {});
    } catch (error) {
      message.textContent = "Save failed.";
    }
  });
}

zoneNodes.forEach((node) => {
  node.addEventListener("click", () => selectZone(node.dataset.zone));
  node.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      selectZone(node.dataset.zone);
    }
  });
});

setView("process");
selectZone(floorConfig.initialZone ?? "receiving");
setDetailsVisible(false);
loadDashboard().catch(() => {});
setInterval(() => {
  loadDashboard().catch(() => {});
}, 15000);
