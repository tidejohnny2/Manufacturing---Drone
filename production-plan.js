// Labor standards: direct labor from the routing plan plus indirect overhead
// adders, rendered per line from /api/labor-standards.
const laborTables = document.querySelector("#laborTables");
const laborNote = document.querySelector("#laborNote");

function esc(value) {
  const div = document.createElement("div");
  div.textContent = value ?? "";
  return div.innerHTML;
}

function laborTable(title, rows, totals, overheads) {
  const overheadHeads = overheads.map((o) => `<th>${esc(o.category.replaceAll("_", " "))} ${Number(o.pct)}%</th>`).join("");
  const body = rows
    .map(
      (row) => `
        <tr>
          <td>${row.seq}</td>
          <td>${esc(row.station)}</td>
          <td>${esc(row.role)}</td>
          <td>${row.direct_minutes}</td>
          ${overheads.map((o) => `<td>${row.indirect[o.category]}</td>`).join("")}
          <td><strong>${row.standard_minutes}</strong></td>
        </tr>`
    )
    .join("");
  return `
    <h3 class="labor-line-title">${esc(title)}</h3>
    <div class="bom-table-wrap">
      <table class="bom-table">
        <thead>
          <tr><th>Seq</th><th>Station</th><th>Role</th><th>Direct min</th>${overheadHeads}<th>Standard min</th></tr>
        </thead>
        <tbody>
          ${body}
          <tr class="labor-total-row">
            <td></td><td><strong>Line total / unit</strong></td><td></td>
            <td><strong>${totals.direct}</strong></td>
            <td colspan="${overheads.length}"><strong>${totals.indirect} indirect</strong></td>
            <td><strong>${totals.standard}</strong></td>
          </tr>
        </tbody>
      </table>
    </div>`;
}

async function loadLabor() {
  if (window.location.protocol === "file:") {
    return;
  }
  const response = await fetch("/api/labor-standards");
  const data = await response.json();
  if (!response.ok || data.error) {
    laborNote.textContent = data.error ?? "Labor standards unavailable.";
    return;
  }
  laborNote.textContent =
    `Direct labor per station from the routing plan, plus ${Number(data.overhead_pct_total)}% indirect ` +
    data.overheads.map((o) => `${o.category.replaceAll("_", " ")} ${Number(o.pct)}%`).join(", ") + ".";
  laborTables.innerHTML =
    laborTable("Drone line", data.lines.drone, data.totals.drone, data.overheads) +
    laborTable("Case line", data.lines.case, data.totals.case, data.overheads);
}

loadLabor().catch(() => {});
