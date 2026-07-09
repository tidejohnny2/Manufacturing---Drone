// Station drill-down: everything about one workstation from /api/station.
const zoneId = new URLSearchParams(window.location.search).get("zone") ?? "";
let stationData = null;

function esc(value) {
  const div = document.createElement("div");
  div.textContent = value ?? "";
  return div.innerHTML;
}

function fmtTime(iso) {
  return iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—";
}

function renderStation() {
  if (!stationData) {
    return;
  }
  const data = stationData;
  const zone = data.zone;

  document.title = `${zone.name} · Station Detail`;
  document.querySelector("#stationLine").textContent =
    `${zone.facility_name} — workstation drill-down`;
  document.querySelector("#stationName").innerHTML =
    esc(zone.name) + (zone.bottleneck ? ' <span class="bottleneck-badge">BOTTLENECK ★</span>' : "");
  document.querySelector("#stationDescription").textContent =
    `${zone.description} (${Number(zone.area_sq_ft).toLocaleString()} sq ft · ${zone.primary_flow})`;

  document.querySelector("#capacityInput").value = zone.capacity ?? "";
  document.querySelector("#cycleMinutes").textContent =
    zone.cycle_minutes != null ? `${zone.cycle_minutes} min` : "—";
  document.querySelector("#maxOutput").textContent =
    zone.max_per_hour != null ? `${zone.max_per_hour} units/hr` : "unbounded";
  const running = data.schedule.some((item) => item.running);
  document.querySelector("#freesAt").textContent = data.idle_at
    ? (running || data.schedule.length ? fmtTime(data.idle_at) : "idle now")
    : "idle now";

  document.querySelector("#busyPct").textContent = `${data.utilization.busy_pct_last_hour}%`;
  document.querySelector("#unitsIn").textContent = data.utilization.units_in_24h;
  document.querySelector("#unitsOut").textContent = data.utilization.units_out_24h;
  document.querySelector("#ceilingPct").textContent =
    data.utilization.pct_of_ceiling_24h != null ? `${data.utilization.pct_of_ceiling_24h}%` : "—";

  const scheduleBody = document.querySelector("#scheduleBody");
  scheduleBody.innerHTML = data.schedule.length
    ? data.schedule
        .map((item) => {
          const state = item.running ? "RUNNING" : item.done_here ? "done here" : "upcoming";
          return `
            <tr class="${item.running ? "station-running-row" : ""}">
              <td>${state}</td>
              <td>${esc(item.order_no)}</td>
              <td>${esc(item.finished_good)}</td>
              <td>${item.quantity}</td>
              <td>${fmtTime(item.start)}</td>
              <td>${fmtTime(item.end)}</td>
            </tr>`;
        })
        .join("")
    : '<tr><td colspan="6">Nothing scheduled at this station.</td></tr>';

  const partsBody = document.querySelector("#partsBody");
  partsBody.innerHTML = data.parts.length
    ? data.parts
        .map(
          (row) => `
            <tr>
              <td>${esc(row.area)}</td>
              <td>${esc(row.item_name)}</td>
              <td>${esc(row.part_number)}</td>
              <td>${Number(row.quantity_on_hand)}</td>
              <td>${Number(row.quantity_allocated)}</td>
              <td>${Number(row.quantity_available)}</td>
              <td>${Number(row.min_quantity)}</td>
              <td>${Number(row.max_quantity)}</td>
              <td>${esc(row.status)}</td>
              <td>${esc(row.control_note)}</td>
            </tr>`
        )
        .join("")
    : '<tr><td colspan="10">No inventory is stocked at this station.</td></tr>';

  const planCards = document.querySelector("#planCards");
  if (data.plan) {
    planCards.innerHTML = `
      <article><span>Operation</span><strong>${esc(data.plan.operation_type)} · ${esc(data.plan.labor_minutes)} labor min</strong><p>${esc(data.plan.primary_role)}</p></article>
      <article><span>Work script</span><strong>Steps</strong><p>${esc(data.plan.work_script)}</p></article>
      <article><span>Quality gate</span><strong>Release check</strong><p>${esc(data.plan.quality_gate)}</p></article>
      <article><span>Tools & support</span><strong>${esc(data.plan.output)}</strong><p>${esc(data.plan.tools_support)} · Pull: ${esc(data.plan.material_pull)}</p></article>`;
  } else {
    planCards.innerHTML = "<article><span>Plan</span><strong>—</strong><p>No routing entry for this station.</p></article>";
  }

  const bomBody = document.querySelector("#bomBody");
  bomBody.innerHTML = data.bom_work.length
    ? data.bom_work
        .map(
          (row) => `
            <tr>
              <td>${esc(row.product)}</td>
              <td>${esc(row.part_number)}</td>
              <td>${esc(row.description)}</td>
              <td>${esc(row.category)}</td>
              <td>${Number(row.quantity)} ${esc(row.unit)}</td>
              <td>${esc(row.supply_type)}</td>
            </tr>`
        )
        .join("")
    : '<tr><td colspan="6">No BOM lines are installed at this station.</td></tr>';

  const ledgerBody = document.querySelector("#ledgerBody");
  ledgerBody.innerHTML = data.ledger.length
    ? data.ledger
        .map(
          (row) => `
            <tr>
              <td>${new Date(row.transaction_at).toLocaleTimeString()}</td>
              <td>${esc(row.transaction_type)}</td>
              <td>${Number(row.quantity_in)}</td>
              <td>${Number(row.quantity_out)}</td>
              <td>${esc(row.accounting_event)}</td>
              <td>${esc(row.reference)}</td>
              <td>${esc(row.notes)}</td>
            </tr>`
        )
        .join("")
    : '<tr><td colspan="7">No activity recorded yet.</td></tr>';
}

async function loadStation() {
  if (window.location.protocol === "file:" || !zoneId) {
    return;
  }
  const response = await fetch(`/api/station?zone=${encodeURIComponent(zoneId)}`);
  const data = await response.json();
  if (!response.ok) {
    document.querySelector("#stationName").textContent = data.error ?? "Station not found";
    return;
  }
  stationData = data;
  renderStation();
}

document.querySelector("#capacitySave").addEventListener("click", async () => {
  const raw = document.querySelector("#capacityInput").value.trim();
  const message = document.querySelector("#capacityMsg");
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
    message.textContent = data.capacity ? `Saved: ${data.capacity} at a time` : "Saved: unconstrained";
    loadStation().catch(() => {});
  } catch (error) {
    message.textContent = "Save failed.";
  }
});

loadStation().catch(() => {});
setInterval(() => {
  loadStation().catch(() => {});
}, 12000);
