// Standard costing dashboard: cost cards, live WIP valuation, variance
// report, and editable standards. The trial balance, cost journal, and
// chart of accounts live on the Accounting page (accounting.js).
const costCards = document.querySelector("#costCards");
const wipBody = document.querySelector("#wipBody");
const wipTie = document.querySelector("#wipTie");
const varianceBody = document.querySelector("#varianceBody");
const materialsBody = document.querySelector("#materialsBody");
const ratesBody = document.querySelector("#ratesBody");
const auditBody = document.querySelector("#auditBody");
const standardsMsg = document.querySelector("#standardsMsg");

function esc(value) {
  const div = document.createElement("div");
  div.textContent = value ?? "";
  return div.innerHTML;
}

function money(value) {
  return Number(value).toLocaleString("en-US", { style: "currency", currency: "USD" });
}

function variance(value) {
  const amount = Number(value);
  if (Math.abs(amount) < 0.005) {
    return '<span class="var-zero">—</span>';
  }
  return amount > 0
    ? `<span class="var-unfav">${money(amount)} U</span>`
    : `<span class="var-fav">(${money(-amount)}) F</span>`;
}

function titleCase(value) {
  return String(value).replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function renderCards(data) {
  costCards.innerHTML = data.cards
    .map((card) => {
      const dm = card.dm_lines
        .map(
          (line) => `
            <tr>
              <td>${esc(line.part_number)}${line.source === "make" ? ' <span class="kit-chip kit-serialized">MAKE @ STD</span>' : ""}</td>
              <td>${line.quantity} ${esc(line.unit)}</td>
              <td>${money(line.unit_std)}</td>
              <td>${money(line.ext_std)}</td>
            </tr>`
        )
        .join("");
      const dl = card.labor_lines
        .map(
          (line) => `
            <tr>
              <td>${esc(line.station)}</td>
              <td>${esc(line.role)}</td>
              <td>${line.minutes}</td>
              <td>${money(line.rate_std)}/hr</td>
              <td>${money(line.cost_std)}</td>
            </tr>`
        )
        .join("");
      return `
        <article class="cost-card">
          <h3>${esc(card.sku)} — ${money(card.unit_std)} / unit</h3>
          <table class="bom-table">
            <thead><tr><th>Material</th><th>Qty</th><th>Std</th><th>Ext</th></tr></thead>
            <tbody>${dm}
              <tr class="labor-total-row"><td colspan="3">Direct material</td><td>${money(card.dm_std)}</td></tr>
            </tbody>
          </table>
          <table class="bom-table">
            <thead><tr><th>Station</th><th>Role</th><th>Min</th><th>Rate</th><th>Cost</th></tr></thead>
            <tbody>${dl}
              <tr class="labor-total-row"><td colspan="4">Direct labor</td><td>${money(card.dl_std)}</td></tr>
              <tr class="labor-total-row"><td colspan="4">Overhead applied (${card.oh_pct}% of DL)</td><td>${money(card.oh_std)}</td></tr>
              <tr class="labor-total-row"><td colspan="4"><strong>Standard cost per unit</strong></td><td><strong>${money(card.unit_std)}</strong></td></tr>
            </tbody>
          </table>
        </article>`;
    })
    .join("");
  const drone = data.cards.find((card) => card.sku === "DRN-FG-600");
  const kase = data.cards.find((card) => card.sku === "CASE-FG-500");
  document.querySelector("#sumDroneStd").textContent = drone ? money(drone.unit_std) : "—";
  document.querySelector("#sumCaseStd").textContent = kase ? money(kase.unit_std) : "—";
}

function renderWip(data) {
  wipBody.innerHTML = data.orders.length
    ? data.orders
        .map(
          (row) => `
            <tr>
              <td>${esc(row.order_no)}</td>
              <td>${esc(row.sku)}</td>
              <td>${row.quantity}</td>
              <td>${titleCase(row.production_status)}</td>
              <td>${row.stations_done}/${row.stations_total}</td>
              <td>${money(row.dm_absorbed)}</td>
              <td>${money(row.conversion_absorbed)}</td>
              <td><strong>${money(row.absorbed)}</strong></td>
              <td>${money(row.std_at_completion)}</td>
            </tr>`
        )
        .join("")
    : '<tr><td colspan="9">Nothing in process.</td></tr>';
  document.querySelector("#sumWip").textContent = money(data.gl_wip);
  const tied = Math.abs(data.tie_delta) <= 1;
  wipTie.textContent = `GL WIP ${money(data.gl_wip)} vs floor ${money(data.absorbed_total)} — ${
    tied ? "TIED" : `Δ ${money(data.tie_delta)}`
  }`;
  wipTie.className = `kit-verdict ${tied ? "kit-release" : "kit-hold"}`;
}

function renderVariances(data) {
  varianceBody.innerHTML = data.orders.length
    ? data.orders
        .map(
          (row) => `
            <tr>
              <td>${esc(row.order_no)}</td>
              <td>${esc(row.sku)}</td>
              <td>${row.quantity}</td>
              <td>${money(row.std_total)}</td>
              <td>${money(Number(row.std_total) + Number(row.total_variance))}</td>
              <td>${variance(row.ppv)}</td>
              <td>${variance(row.labor_rate_variance)}</td>
              <td>${variance(row.labor_efficiency_variance)}</td>
              <td>${variance(row.oh_variance)}</td>
              <td>${variance(row.total_variance)}</td>
            </tr>`
        )
        .join("") +
      `
        <tr class="labor-total-row">
          <td colspan="3"><strong>Totals (${data.totals.orders} orders)</strong></td>
          <td><strong>${money(data.totals.std_total)}</strong></td>
          <td></td>
          <td>${variance(data.totals.ppv)}</td>
          <td>${variance(data.totals.labor_rate_variance)}</td>
          <td>${variance(data.totals.labor_efficiency_variance)}</td>
          <td>${variance(data.totals.oh_variance)}</td>
          <td>${variance(data.totals.total_variance)}</td>
        </tr>`
    : '<tr><td colspan="10">Variances appear as orders complete.</td></tr>';
  document.querySelector("#sumVariance").innerHTML = variance(data.totals.total_variance);
}

function renderStandards(data) {
  materialsBody.innerHTML = data.materials
    .map(
      (row) => `
        <tr data-type="material" data-key="${esc(row.part_number)}">
          <td>${esc(row.part_number)}</td>
          <td>${esc(row.description ?? "")}</td>
          <td><input class="std-input" data-field="standard" type="number" min="0" step="0.01" value="${Number(row.standard_cost)}" /></td>
          <td><input class="std-input" data-field="actual" type="number" min="0" step="0.01" value="${Number(row.actual_cost)}" /></td>
          <td><button type="button" class="std-save">Save</button></td>
        </tr>`
    )
    .join("");
  ratesBody.innerHTML = data.rates
    .map(
      (row) => `
        <tr data-type="labor" data-key="${esc(row.role)}">
          <td>${esc(row.role)}</td>
          <td><input class="std-input" data-field="standard" type="number" min="0" step="0.01" value="${Number(row.standard_rate)}" /></td>
          <td><input class="std-input" data-field="actual" type="number" min="0" step="0.01" value="${Number(row.actual_rate)}" /></td>
          <td><button type="button" class="std-save">Save</button></td>
        </tr>`
    )
    .join("");
  auditBody.innerHTML = data.audit.length
    ? data.audit
        .map(
          (row) => `
            <tr>
              <td>${new Date(row.changed_at).toLocaleString()}</td>
              <td>${esc(row.actor)}</td>
              <td>${esc(row.item_type)}</td>
              <td>${esc(row.item_key)}</td>
              <td>${esc(row.field)}</td>
              <td>${esc(row.old_value ?? "")}</td>
              <td>${esc(row.new_value ?? "")}</td>
            </tr>`
        )
        .join("")
    : '<tr><td colspan="7">No standards changes recorded.</td></tr>';
}

async function getJson(path) {
  const response = await fetch(path);
  const data = await response.json();
  if (!response.ok || data.error) {
    throw new Error(data.error ?? `Unable to load ${path}`);
  }
  return data;
}

async function loadStandards() {
  renderStandards(await getJson("/api/costing/standards"));
}

async function loadLive() {
  renderWip(await getJson("/api/costing/wip"));
  renderVariances(await getJson("/api/costing/variances"));
}

async function loadAll() {
  renderCards(await getJson("/api/costing/cost-cards"));
  await loadLive();
  await loadStandards();
}

document.querySelector("main").addEventListener("click", async (event) => {
  const button = event.target.closest(".std-save");
  if (!button) {
    return;
  }
  const row = button.closest("tr");
  standardsMsg.textContent = "Saving…";
  standardsMsg.className = "kit-verdict";
  try {
    for (const input of row.querySelectorAll(".std-input")) {
      const response = await fetch("/api/costing/standards", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          itemType: row.dataset.type,
          key: row.dataset.key,
          field: input.dataset.field,
          value: Number(input.value),
          actor: document.querySelector("#standardsActor").value.trim()
        })
      });
      const data = await response.json();
      if (!response.ok || data.error) {
        standardsMsg.textContent = data.error ?? "Save failed.";
        standardsMsg.classList.add("kit-hold");
        return;
      }
    }
    standardsMsg.textContent = `Saved ${row.dataset.key} — future postings use the new figures.`;
    standardsMsg.classList.add("kit-release");
    await loadStandards();
    renderCards(await getJson("/api/costing/cost-cards"));
  } catch (error) {
    standardsMsg.textContent = "Save failed.";
    standardsMsg.classList.add("kit-hold");
  }
});

if (window.location.protocol !== "file:") {
  loadAll().catch((error) => {
    costCards.innerHTML = `<p>${esc(error.message)}</p>`;
  });
  setInterval(() => {
    loadLive().catch(() => {});
  }, 15000);
}
