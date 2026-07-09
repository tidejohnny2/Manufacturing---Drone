const sumOrders = document.querySelector("#sumOrders");
const sumQuantity = document.querySelector("#sumQuantity");
const sumLines = document.querySelector("#sumLines");
const sumCaseStock = document.querySelector("#sumCaseStock");
const sumDroneStock = document.querySelector("#sumDroneStock");
const sumShortages = document.querySelector("#sumShortages");
const pipelinesEl = document.querySelector("#pipelines");
const ordersBody = document.querySelector("#ordersBody");
const inventoryBody = document.querySelector("#inventoryBody");
const shortageTitle = document.querySelector("#shortageTitle");
const shortageDetail = document.querySelector("#shortageDetail");
const completedTitle = document.querySelector("#completedTitle");
const completedDetail = document.querySelector("#completedDetail");
const activityBody = document.querySelector("#activityBody");

const LINE_NAMES = { 1: "Drone floor", 2: "Case line" };

function titleCase(value) {
  return value
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function renderPipelines(pipelines) {
  pipelinesEl.innerHTML = pipelines
    .map(
      (line) => `
        <div class="pipeline">
          <div class="pipeline-title">
            <strong>${line.facility_name}</strong>
            <span>${line.active_orders} active order${line.active_orders === 1 ? "" : "s"}${
              line.ceiling_per_hour
                ? ` · ceiling ${line.ceiling_per_hour}/hr @ ${line.bottleneck_station} ★ · 24 h out ${Number(line.output_24h)}${
                    line.pct_of_ceiling_24h != null ? ` (${line.pct_of_ceiling_24h}% of ceiling)` : ""
                  }`
                : ""
            }</span>
          </div>
          <div class="pipeline-strip">
            ${line.stations
              .map(
                (station) => `
                  <div class="pipeline-station${station.wip > 0 ? " busy" : ""}${station.done > 0 && station.wip === 0 ? " done" : ""}">
                    <span class="pipeline-station-name">${station.station}${station.bottleneck ? " ★" : ""}</span>
                    <span class="pipeline-station-counts">WIP ${station.wip} | Done ${station.done}${station.capacity ? ` | Cap ${station.capacity}` : ""}${
                      station.busy_pct_last_hour > 0 ? ` | Busy ${station.busy_pct_last_hour}%` : ""
                    }</span>
                    ${station.orders.length ? `<span class="pipeline-station-order">${station.orders.join(", ")}</span>` : ""}
                  </div>
                `
              )
              .join('<span class="pipeline-arrow">&#8594;</span>')}
          </div>
        </div>
      `
    )
    .join("");
}

function renderOverview(data) {
  sumOrders.textContent = data.summary.active_orders;
  sumQuantity.textContent = data.summary.active_quantity;
  sumLines.textContent = data.summary.lines_running;
  sumShortages.textContent = data.summary.shortage_count;

  const caseRow = data.inventory_watch.find((row) => row.part_number === "CASE-FG-500");
  const droneRow = data.inventory_watch.find((row) => row.part_number === "DRN-FG-600");
  sumCaseStock.textContent = caseRow ? `${Number(caseRow.quantity_available)} each` : "-";
  sumDroneStock.textContent = droneRow ? `${Number(droneRow.quantity_available)} each` : "-";

  renderPipelines(data.pipelines);

  ordersBody.innerHTML = data.orders.length
    ? data.orders
        .map(
          (order) => `
            <tr>
              <td>${order.order_no}</td>
              <td>${order.finished_good}</td>
              <td>${LINE_NAMES[order.facility_id] ?? "Facility " + order.facility_id}</td>
              <td>${titleCase(order.production_status)}</td>
              <td>${order.current_zone}</td>
              <td>${order.percent_complete ?? 0}%</td>
              <td>${order.quantity} each</td>
              <td>${order.due_date}</td>
            </tr>
          `
        )
        .join("")
    : '<tr><td colspan="8">No active orders.</td></tr>';

  inventoryBody.innerHTML = data.inventory_watch
    .map(
      (row) => `
        <tr>
          <td>${row.area}</td>
          <td>${row.location}</td>
          <td>${row.item_name}</td>
          <td>${row.part_number}</td>
          <td>${Number(row.quantity_on_hand)}</td>
          <td>${Number(row.quantity_allocated)}</td>
          <td>${Number(row.quantity_available)}</td>
          <td>${Number(row.min_quantity)}</td>
          <td>${Number(row.max_quantity)}</td>
          <td>${row.status}</td>
        </tr>
      `
    )
    .join("");

  if (data.shortages.length) {
    shortageTitle.textContent = `${data.shortages.length} short line${data.shortages.length === 1 ? "" : "s"}`;
    shortageDetail.textContent = data.shortages
      .map((s) => `${s.order_no}: ${s.part_number} needs ${Number(s.required_quantity)} ${s.unit}`)
      .join(" | ");
  } else {
    shortageTitle.textContent = "None";
    shortageDetail.textContent = "All inventory-pull lines on open orders are covered by stock.";
  }

  if (data.completed_today.length) {
    completedTitle.textContent = data.completed_today
      .map((c) => `${Number(c.quantity)} ${c.sku}`)
      .join(" + ");
    completedDetail.textContent = data.completed_today
      .map((c) => `${c.orders} order${c.orders === 1 ? "" : "s"} of ${c.sku} booked into stock`)
      .join(" | ");
  } else {
    completedTitle.textContent = "Nothing yet";
    completedDetail.textContent = "Finished orders booked into stock in the last 24 hours appear here.";
  }

  activityBody.innerHTML = data.recent_transactions
    .map(
      (row) => `
        <tr>
          <td>${new Date(row.created_at).toLocaleTimeString()}</td>
          <td>${titleCase(row.transaction_type)}</td>
          <td>${row.part_number}</td>
          <td>${Number(row.quantity)}</td>
          <td>${row.from_zone ?? ""}</td>
          <td>${row.to_zone ?? ""}</td>
          <td>${row.reference}</td>
        </tr>
      `
    )
    .join("");
}

async function loadOverview() {
  if (window.location.protocol === "file:") {
    return;
  }
  const response = await fetch("/api/operations-overview");
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error ?? "Unable to load operations overview.");
  }
  renderOverview(data);
}

loadOverview().catch(() => {});
setInterval(() => {
  loadOverview().catch(() => {});
}, 12000);
