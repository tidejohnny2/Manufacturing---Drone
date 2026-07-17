// Analytics: the books sliced by analytic dimension. A tab per tag group;
// the table shows allocated debits/credits, net, and a revenue/COGS/margin
// cut per tag. Report-only — reads /api/analytics (tag overlay), never the TB.
const dimensionTabs = document.querySelector("#dimensionTabs");
const analyticsBody = document.querySelector("#analyticsBody");

let currentGroup = null;

function esc(value) {
  const div = document.createElement("div");
  div.textContent = value ?? "";
  return div.innerHTML;
}

function money(value) {
  return Number(value).toLocaleString("en-US", { style: "currency", currency: "USD" });
}

function cell(value) {
  return Number(value) ? money(value) : "—";
}

function render(data) {
  const groups = data.groups ?? [];
  currentGroup = data.group;
  dimensionTabs.innerHTML = groups
    .map(
      (g) =>
        `<button type="button" class="dimension-tab ${g.name === currentGroup ? "active" : ""}" data-group="${esc(
          g.name
        )}"><span class="an-dot" style="background:${esc(g.color || "#64748b")}"></span>${esc(g.name)}</button>`
    )
    .join("");

  document.querySelector("#sumDimension").textContent = currentGroup ?? "—";
  const t = data.totals ?? {};
  document.querySelector("#sumRevenue").textContent = money(t.revenue ?? 0);
  document.querySelector("#sumCogs").textContent = money(t.cogs ?? 0);
  const marginEl = document.querySelector("#sumMargin");
  marginEl.innerHTML = `<span class="${(t.margin ?? 0) >= 0 ? "var-fav" : "var-unfav"}">${money(t.margin ?? 0)}</span>`;

  analyticsBody.innerHTML = data.rows && data.rows.length
    ? data.rows
        .map(
          (row) => `
            <tr>
              <td>${esc(row.name)}</td>
              <td>${row.reference ? `<span class="acct-code">${esc(row.reference)}</span>` : ""}</td>
              <td>${cell(row.alloc_debit)}</td>
              <td>${cell(row.alloc_credit)}</td>
              <td>${money(row.net)}</td>
              <td>${cell(row.revenue)}</td>
              <td>${cell(row.cogs)}</td>
              <td><strong>${row.margin ? money(row.margin) : "—"}</strong></td>
              <td>${row.lines}</td>
            </tr>`
        )
        .join("") +
      `<tr class="labor-total-row">
        <td colspan="2"><strong>Totals</strong></td>
        <td><strong>${cell(t.alloc_debit)}</strong></td>
        <td><strong>${cell(t.alloc_credit)}</strong></td>
        <td><strong>${money(t.net ?? 0)}</strong></td>
        <td><strong>${cell(t.revenue)}</strong></td>
        <td><strong>${cell(t.cogs)}</strong></td>
        <td><strong>${t.margin ? money(t.margin) : "—"}</strong></td>
        <td></td>
      </tr>`
    : '<tr><td colspan="9">No tagged activity in this dimension yet.</td></tr>';
}

async function load(group) {
  const path = group
    ? `/api/analytics?group=${encodeURIComponent(group)}`
    : "/api/analytics";
  const response = await fetch(withCompany(path));
  const data = await response.json();
  if (!response.ok || data.error) {
    analyticsBody.innerHTML = `<tr><td colspan="9">${esc(data.error ?? "Unable to load analytics.")}</td></tr>`;
    return;
  }
  render(data);
}

dimensionTabs.addEventListener("click", (event) => {
  const tab = event.target.closest(".dimension-tab");
  if (tab) {
    load(tab.dataset.group).catch(() => {});
  }
});

if (window.location.protocol !== "file:") {
  load(null).catch(() => {});
}

// Exposed for DOM-level testing.
window.__analytics = { render };
