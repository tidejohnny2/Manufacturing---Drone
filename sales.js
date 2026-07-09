// Sales & invoicing: customer maintenance, sales order entry, and one-click
// Ship & Invoice against /api/sales endpoints.
const customersBody = document.querySelector("#customersBody");
const customerMsg = document.querySelector("#customerMsg");
const ordersBody = document.querySelector("#ordersBody");
const invoicesBody = document.querySelector("#invoicesBody");
const orderMsg = document.querySelector("#orderMsg");
const soCustomer = document.querySelector("#soCustomer");
const soLines = document.querySelector("#soLines");

let listPrices = {};

function esc(value) {
  const div = document.createElement("div");
  div.textContent = value ?? "";
  return div.innerHTML;
}

function money(value) {
  return Number(value).toLocaleString("en-US", { style: "currency", currency: "USD" });
}

function setMsg(node, text, ok) {
  node.textContent = text;
  node.className = `kit-verdict ${ok ? "kit-release" : "kit-hold"}`;
}

function lineRow() {
  const row = document.createElement("div");
  row.className = "so-line";
  row.innerHTML = `
    <select class="so-sku">
      <option value="DRN-FG-600">DRN-FG-600 — Drone</option>
      <option value="CASE-FG-500">CASE-FG-500 — Transport Case</option>
    </select>
    <input class="so-qty" type="number" min="1" value="1" aria-label="Quantity" />
    <input class="so-price" type="number" min="0" step="0.01" aria-label="Unit price" />
    <button type="button" class="so-remove" aria-label="Remove line">&times;</button>`;
  const sku = row.querySelector(".so-sku");
  const price = row.querySelector(".so-price");
  const setPrice = () => (price.value = listPrices[sku.value] ?? "");
  sku.addEventListener("change", setPrice);
  setPrice();
  row.querySelector(".so-remove").addEventListener("click", () => row.remove());
  soLines.appendChild(row);
}

function renderSales(data) {
  listPrices = Object.fromEntries(data.price_list.map((p) => [p.sku, Number(p.list_price)]));
  document.querySelector("#sumDroneStock").textContent = `${data.stock["DRN-FG-600"] ?? 0} each`;
  document.querySelector("#sumCaseStock").textContent = `${data.stock["CASE-FG-500"] ?? 0} each`;
  document.querySelector("#sumDronePrice").textContent = money(listPrices["DRN-FG-600"] ?? 0);
  document.querySelector("#sumCasePrice").textContent = money(listPrices["CASE-FG-500"] ?? 0);
  document.querySelector("#sumInvoiced").textContent = money(
    data.invoices.reduce((sum, inv) => sum + Number(inv.subtotal), 0)
  );

  customersBody.innerHTML = data.customers
    .map(
      (c) => `
        <tr data-id="${c.id}">
          <td><input class="acct-input cust-field" data-field="name" maxlength="120" value="${esc(c.name)}" /></td>
          <td><input class="acct-input cust-field" data-field="contact" maxlength="120" value="${esc(c.contact)}" /></td>
          <td><input class="acct-input cust-field" data-field="terms" maxlength="40" value="${esc(c.terms)}" /></td>
          <td>${c.order_count}</td>
          <td>
            <button type="button" class="std-save cust-save">Save</button>
            ${c.order_count === 0 ? '<button type="button" class="acct-delete cust-delete">Delete</button>' : ""}
          </td>
        </tr>`
    )
    .join("");

  const selected = soCustomer.value;
  soCustomer.innerHTML = data.customers
    .map((c) => `<option value="${c.id}" ${String(c.id) === selected ? "selected" : ""}>${esc(c.name)}</option>`)
    .join("");

  ordersBody.innerHTML = data.orders.length
    ? data.orders
        .map((o) => {
          const lines = o.lines.map((l) => `${l.quantity} × ${l.sku} @ ${money(l.unit_price)}`).join("<br>");
          const stockChip =
            o.status !== "open"
              ? ""
              : o.can_fulfill
                ? '<span class="kit-chip kit-available">IN STOCK</span>'
                : '<span class="kit-chip kit-short">SHORT</span>';
          const action =
            o.status === "open"
              ? `<button type="button" class="std-save so-invoice" data-so="${esc(o.so_no)}">Ship &amp; Invoice</button>`
              : "";
          return `
            <tr>
              <td>${esc(o.so_no)}</td>
              <td>${esc(o.customer)}</td>
              <td>${lines}</td>
              <td>${money(o.subtotal)}</td>
              <td>${esc(o.status)}</td>
              <td>${stockChip}</td>
              <td>${action}</td>
            </tr>`;
        })
        .join("")
    : '<tr><td colspan="7">No sales orders yet.</td></tr>';

  invoicesBody.innerHTML = data.invoices.length
    ? data.invoices
        .map(
          (inv) => `
            <tr>
              <td>${esc(inv.invoice_no)}</td>
              <td>${esc(inv.so_no)}</td>
              <td>${esc(inv.customer)}</td>
              <td>${new Date(inv.invoiced_at).toLocaleString()}</td>
              <td>${money(inv.subtotal)}</td>
              <td>${money(inv.cogs)}</td>
              <td><strong>${money(inv.margin)}</strong></td>
            </tr>`
        )
        .join("")
    : '<tr><td colspan="7">Invoices appear here after Ship &amp; Invoice.</td></tr>';
}

async function getSales() {
  const response = await fetch("/api/sales");
  const data = await response.json();
  if (!response.ok || data.error) {
    throw new Error(data.error ?? "Unable to load sales.");
  }
  renderSales(data);
}

async function post(path, body, msgNode, successText) {
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok || data.error) {
      setMsg(msgNode, data.error ?? "Request failed.", false);
      return null;
    }
    setMsg(msgNode, successText(data), true);
    await getSales();
    return data;
  } catch (error) {
    setMsg(msgNode, String(error), false);
    return null;
  }
}

document.querySelector("#customerCreate").addEventListener("submit", async (event) => {
  event.preventDefault();
  const created = await post(
    "/api/sales/customer",
    {
      action: "create",
      name: document.querySelector("#newCustomerName").value.trim(),
      contact: document.querySelector("#newCustomerContact").value.trim(),
      terms: document.querySelector("#newCustomerTerms").value.trim()
    },
    customerMsg,
    (d) => `Added ${d.name}.`
  );
  if (created) {
    document.querySelector("#newCustomerName").value = "";
    document.querySelector("#newCustomerContact").value = "";
  }
});

customersBody.addEventListener("click", async (event) => {
  const row = event.target.closest("tr[data-id]");
  if (!row) {
    return;
  }
  const customerId = Number(row.dataset.id);
  const field = (name) => row.querySelector(`[data-field="${name}"]`).value.trim();
  if (event.target.closest(".cust-save")) {
    await post(
      "/api/sales/customer",
      { action: "update", customerId, name: field("name"), contact: field("contact"), terms: field("terms") },
      customerMsg,
      (d) => `Saved ${d.name}.`
    );
  }
  if (event.target.closest(".cust-delete")) {
    if (!window.confirm("Delete this customer? Only possible with no sales orders.")) {
      return;
    }
    await post(
      "/api/sales/customer",
      { action: "delete", customerId, name: field("name") },
      customerMsg,
      () => "Customer deleted."
    );
  }
});

document.querySelector("#soAddLine").addEventListener("click", lineRow);

document.querySelector("#soForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const lines = [...soLines.querySelectorAll(".so-line")].map((row) => ({
    sku: row.querySelector(".so-sku").value,
    quantity: Number(row.querySelector(".so-qty").value),
    unitPrice: row.querySelector(".so-price").value === "" ? null : Number(row.querySelector(".so-price").value)
  }));
  const created = await post(
    "/api/sales/order",
    {
      customerId: Number(soCustomer.value),
      requestedDate: document.querySelector("#soDate").value,
      lines
    },
    orderMsg,
    (d) => `Created ${d.soNo} (${d.lines} line${d.lines === 1 ? "" : "s"}).`
  );
  if (created) {
    soLines.innerHTML = "";
    lineRow();
  }
});

ordersBody.addEventListener("click", async (event) => {
  const button = event.target.closest(".so-invoice");
  if (!button) {
    return;
  }
  button.disabled = true;
  const result = await post(
    "/api/sales/invoice",
    { soNo: button.dataset.so },
    orderMsg,
    (d) => `${d.invoiceNo}: ${d.customer} billed ${money(d.subtotal)} · COGS ${money(d.cogs)} · margin ${money(d.margin)}.`
  );
  if (!result) {
    button.disabled = false;
  }
});

if (window.location.protocol !== "file:") {
  getSales().catch((error) => setMsg(orderMsg, error.message, false));
  lineRow();
  setInterval(() => {
    getSales().catch(() => {});
  }, 15000);
} else {
  lineRow();
}

// Exposed for DOM-level testing.
window.__sales = { renderSales, lineRow };
