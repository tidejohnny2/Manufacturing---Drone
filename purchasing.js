// Purchasing: policy/EOQ settings, replenishment planner, vendor POs with
// receiving, the sourcing catalog, and vendor maintenance — all against
// /api/purchasing endpoints.
const plannerBody = document.querySelector("#plannerBody");
const plannerMsg = document.querySelector("#plannerMsg");
const policyMsg = document.querySelector("#policyMsg");
const poBody = document.querySelector("#poBody");
const poMsg = document.querySelector("#poMsg");
const catalogBody = document.querySelector("#catalogBody");
const catalogMsg = document.querySelector("#catalogMsg");
const vendorsBody = document.querySelector("#vendorsBody");
const vendorMsg = document.querySelector("#vendorMsg");

let currentParts = [];

function esc(value) {
  const div = document.createElement("div");
  div.textContent = value ?? "";
  return div.innerHTML;
}

function money(value) {
  return Number(value).toLocaleString("en-US", { style: "currency", currency: "USD" });
}

function qty(value) {
  return Number(value).toLocaleString("en-US", { maximumFractionDigits: 1 });
}

function setMsg(node, text, ok) {
  node.textContent = text;
  node.className = `kit-verdict ${ok ? "kit-release" : "kit-hold"}`;
}

function renderPurchasing(data) {
  currentParts = data.parts;
  document.querySelector("#sumParts").textContent = data.parts.length;
  const below = data.parts.filter((p) => p.below_reorder);
  document.querySelector("#sumBelow").innerHTML = below.length
    ? `<span class="var-unfav">${below.length}</span>`
    : "0";
  document.querySelector("#sumOpenPos").textContent = data.pos.filter((p) => p.status === "issued").length;
  document.querySelector("#sumAp").textContent = money(data.ap_balance);

  const s = data.settings;
  document.querySelector("#polOrdering").value = Number(s.ordering_cost);
  document.querySelector("#polCarrying").value = Number(s.carrying_rate_pct);
  document.querySelector("#polSafety").value = Number(s.safety_stock_days);
  document.querySelector("#polBuilds").value = Number(s.planned_annual_builds);

  plannerBody.innerHTML = data.parts
    .map((p) => {
      const status = p.below_reorder
        ? '<span class="kit-chip kit-short">REORDER</span>'
        : '<span class="kit-chip kit-available">OK</span>';
      const suggestion = p.below_reorder
        ? `${p.suggested_qty} @ ${money(p.price_at_suggested)}<br>${money(p.order_value)}`
        : "";
      const action = p.below_reorder && p.vendor_id
        ? `<button type="button" class="std-save plan-po" data-part="${esc(p.part_number)}">Create PO</button>`
        : "";
      return `
        <tr data-part="${esc(p.part_number)}">
          <td>${esc(p.part_number)}</td>
          <td>${esc(p.description)}</td>
          <td>${qty(p.available)}</td>
          <td><input class="acct-input mm-input" data-field="min" type="number" min="0" value="${Number(p.min_quantity)}" /></td>
          <td><input class="acct-input mm-input" data-field="max" type="number" min="0" value="${Number(p.max_quantity)}" /></td>
          <td>${qty(p.reorder_point)}</td>
          <td>${qty(p.annual_demand)}</td>
          <td>${p.eoq}</td>
          <td>${p.vendor ? `${esc(p.vendor)}<br>${esc(p.vendor_model)}` : "—"}</td>
          <td>${status}</td>
          <td>${suggestion}</td>
          <td>
            <button type="button" class="std-save mm-save">Save</button>
            ${action}
          </td>
        </tr>`;
    })
    .join("");

  poBody.innerHTML = data.pos.length
    ? data.pos
        .map((po) => {
          const lines = po.lines
            .map((l) => `${l.quantity} × ${l.part_number} @ ${money(l.unit_price)}`)
            .join("<br>");
          const status = po.status === "received"
            ? '<span class="kit-chip kit-available">RECEIVED</span>'
            : '<span class="kit-chip kit-not-stocked">ISSUED</span>';
          const action = po.status === "issued"
            ? `<button type="button" class="std-save po-receive" data-po="${esc(po.po_no)}">Receive</button>`
            : "";
          return `
            <tr>
              <td>${esc(po.po_no)}</td>
              <td><span class="acct-code">${esc(po.vendor_code ?? "")}</span> ${esc(po.vendor)}</td>
              <td>${lines}</td>
              <td>${money(po.total)}</td>
              <td>${status}</td>
              <td>${new Date(po.created_at).toLocaleString()}</td>
              <td>${action}</td>
            </tr>`;
        })
        .join("")
    : '<tr><td colspan="7">No purchase orders yet.</td></tr>';

  catalogBody.innerHTML = data.catalog
    .map((offer) => {
      const breaks = offer.breaks.length
        ? offer.breaks
            .map((b) => `<span class="kit-chip kit-serialized">${b.min_qty}+ ${money(b.unit_price)}</span>`)
            .join(" ")
        : "";
      const preferred = offer.preferred
        ? '<span class="pref-star" title="Preferred offer">&#9733;</span>'
        : `<button type="button" class="std-save cat-prefer" data-offer="${offer.id}">Prefer</button>`;
      return `
        <tr>
          <td>${esc(offer.part_number)}</td>
          <td>${esc(offer.vendor)}</td>
          <td>${esc(offer.vendor_model)}</td>
          <td>${esc(offer.description)}</td>
          <td>${money(offer.unit_price)}</td>
          <td>${breaks}</td>
          <td>${offer.moq}</td>
          <td>${offer.lead_time_days}d</td>
          <td>${esc(offer.availability)}</td>
          <td>${preferred}</td>
        </tr>`;
    })
    .join("");

  vendorsBody.innerHTML = data.vendors
    .map(
      (v) => `
        <tr data-id="${v.id}">
          <td><span class="acct-code">${esc(v.account_code ?? "")}</span></td>
          <td><input class="acct-input vend-field" data-field="name" maxlength="120" value="${esc(v.name)}" /></td>
          <td><input class="acct-input vend-field" data-field="contact" maxlength="120" value="${esc(v.contact)}" /></td>
          <td><input class="acct-input vend-field" data-field="terms" maxlength="40" value="${esc(v.terms)}" /></td>
          <td><input class="acct-input vend-field mm-input" data-field="lead" type="number" min="0" value="${v.lead_time_days}" /></td>
          <td>${v.po_count}</td>
          <td>
            <button type="button" class="std-save vend-save">Save</button>
            ${v.po_count === 0 ? '<button type="button" class="acct-delete vend-delete">Delete</button>' : ""}
          </td>
        </tr>`
    )
    .join("");

  renderPoAudit(data.audit || {});
}

function renderPoAudit(audit) {
  const body = document.querySelector("#poAuditBody");
  const verdict = document.querySelector("#poAuditVerdict");
  const rows = audit.assertions || [];
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="5">PO Audit unavailable.</td></tr>';
    verdict.textContent = "";
    return;
  }
  const fails = rows.filter((a) => !a.pass).length;
  verdict.innerHTML = fails
    ? `<span class="var-unfav">ATTENTION — ${fails} exception${fails === 1 ? "" : "s"}</span>`
    : `<span class="kit-chip kit-available">PASS &middot; ${rows.length} controls</span>`;
  body.innerHTML = rows
    .map((a) => `
      <tr>
        <td>${esc(a.id)}</td>
        <td>${esc(a.check)}</td>
        <td>${esc(a.expected)}</td>
        <td>${esc(a.actual)}</td>
        <td>${a.pass
          ? '<span class="kit-chip kit-available">&#10003; Pass</span>'
          : '<span class="kit-chip kit-short">&#10007; Fail</span>'}</td>
      </tr>`)
    .join("");
}

async function getPurchasing() {
  const response = await fetch("/api/purchasing");
  const data = await response.json();
  if (!response.ok || data.error) {
    throw new Error(data.error ?? "Unable to load purchasing.");
  }
  renderPurchasing(data);
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
    await getPurchasing();
    return data;
  } catch (error) {
    setMsg(msgNode, String(error), false);
    return null;
  }
}

document.querySelector("#policyForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  await post(
    "/api/purchasing/settings",
    {
      orderingCost: Number(document.querySelector("#polOrdering").value),
      carryingRatePct: Number(document.querySelector("#polCarrying").value),
      safetyStockDays: Number(document.querySelector("#polSafety").value),
      plannedAnnualBuilds: Number(document.querySelector("#polBuilds").value)
    },
    policyMsg,
    () => "Policy saved — planner recalculated."
  );
});

plannerBody.addEventListener("click", async (event) => {
  const row = event.target.closest("tr[data-part]");
  if (!row) {
    return;
  }
  const part = row.dataset.part;
  if (event.target.closest(".mm-save")) {
    await post(
      "/api/purchasing/minmax",
      {
        partNumber: part,
        minQuantity: Number(row.querySelector('[data-field="min"]').value),
        maxQuantity: Number(row.querySelector('[data-field="max"]').value)
      },
      plannerMsg,
      () => `Saved min/max for ${part}.`
    );
  }
  if (event.target.closest(".plan-po")) {
    const p = currentParts.find((x) => x.part_number === part);
    if (!p) {
      return;
    }
    await post(
      "/api/purchasing/order",
      { vendorId: p.vendor_id, lines: [{ partNumber: part, quantity: p.suggested_qty }] },
      plannerMsg,
      (d) => `${d.poNo} issued to ${d.vendor} — ${money(d.total)}.`
    );
  }
});

document.querySelector("#orderAll").addEventListener("click", async () => {
  const below = currentParts.filter((p) => p.below_reorder && p.vendor_id && p.suggested_qty > 0);
  if (!below.length) {
    setMsg(plannerMsg, "Nothing is below its reorder point.", true);
    return;
  }
  const byVendor = new Map();
  for (const p of below) {
    if (!byVendor.has(p.vendor_id)) {
      byVendor.set(p.vendor_id, []);
    }
    byVendor.get(p.vendor_id).push({ partNumber: p.part_number, quantity: p.suggested_qty });
  }
  const created = [];
  for (const [vendorId, lines] of byVendor) {
    const result = await post(
      "/api/purchasing/order",
      { vendorId, lines },
      plannerMsg,
      (d) => `${d.poNo} issued to ${d.vendor} — ${money(d.total)}.`
    );
    if (result) {
      created.push(result.poNo);
    }
  }
  if (created.length) {
    setMsg(plannerMsg, `Issued ${created.length} PO${created.length === 1 ? "" : "s"}: ${created.join(", ")}.`, true);
  }
});

poBody.addEventListener("click", async (event) => {
  const button = event.target.closest(".po-receive");
  if (!button) {
    return;
  }
  button.disabled = true;
  const result = await post(
    "/api/purchasing/receive",
    { poNo: button.dataset.po },
    poMsg,
    (d) =>
      `${d.poNo} received from ${d.vendor}: ${money(d.total)} to Raw Materials / Accounts Payable` +
      (d.repriced.length ? ` — actual cost updated for ${d.repriced.join(", ")}.` : ".")
  );
  if (!result) {
    button.disabled = false;
  }
});

catalogBody.addEventListener("click", async (event) => {
  const button = event.target.closest(".cat-prefer");
  if (!button) {
    return;
  }
  await post(
    "/api/purchasing/preferred",
    { vendorPartId: Number(button.dataset.offer) },
    catalogMsg,
    (d) => `Preferred offer updated for ${d.partNumber}.`
  );
});

document.querySelector("#vendorCreate").addEventListener("submit", async (event) => {
  event.preventDefault();
  const created = await post(
    "/api/purchasing/vendor",
    {
      action: "create",
      name: document.querySelector("#newVendorName").value.trim(),
      contact: document.querySelector("#newVendorContact").value.trim(),
      terms: document.querySelector("#newVendorTerms").value.trim(),
      leadTimeDays: Number(document.querySelector("#newVendorLead").value)
    },
    vendorMsg,
    (d) => `Added ${d.name}.`
  );
  if (created) {
    document.querySelector("#newVendorName").value = "";
    document.querySelector("#newVendorContact").value = "";
  }
});

vendorsBody.addEventListener("click", async (event) => {
  const row = event.target.closest("tr[data-id]");
  if (!row) {
    return;
  }
  const vendorId = Number(row.dataset.id);
  const field = (name) => row.querySelector(`[data-field="${name}"]`).value.trim();
  if (event.target.closest(".vend-save")) {
    await post(
      "/api/purchasing/vendor",
      {
        action: "update",
        vendorId,
        name: field("name"),
        contact: field("contact"),
        terms: field("terms"),
        leadTimeDays: Number(field("lead"))
      },
      vendorMsg,
      (d) => `Saved ${d.name}.`
    );
  }
  if (event.target.closest(".vend-delete")) {
    if (!window.confirm("Delete this vendor? Only possible with no purchase orders.")) {
      return;
    }
    await post(
      "/api/purchasing/vendor",
      { action: "delete", vendorId, name: field("name") },
      vendorMsg,
      () => "Vendor deleted."
    );
  }
});

if (window.location.protocol !== "file:") {
  getPurchasing().catch((error) => setMsg(plannerMsg, error.message, false));
  setInterval(() => {
    getPurchasing().catch(() => {});
  }, 20000);
}

// Exposed for DOM-level testing.
window.__purchasing = { renderPurchasing };
