// Order Intake: inbox of AI-classified emails, the draft-SO review queue
// (Accept -> ship or backorder+build; Reject), backorder progress, and a
// test-email injector — all against /api/intake endpoints.
const queueBody = document.querySelector("#queueBody");
const queueMsg = document.querySelector("#queueMsg");
const inboxBody = document.querySelector("#inboxBody");
const inboxMsg = document.querySelector("#inboxMsg");
const mailboxNote = document.querySelector("#mailboxNote");
const testMsg = document.querySelector("#testMsg");

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

const EMAIL_STATUS_CHIPS = {
  new: '<span class="kit-chip kit-not-stocked">NEW</span>',
  order_drafted: '<span class="kit-chip kit-available">ORDER DRAFTED</span>',
  not_order: '<span class="kit-chip kit-serialized">NOT AN ORDER</span>',
  rejected: '<span class="kit-chip kit-short">REJECTED</span>',
  error: '<span class="kit-chip kit-short">ERROR</span>'
};

function renderIntake(data) {
  const drafts = data.queue.filter((q) => q.status === "draft");
  const backorders = data.queue.filter((q) => q.status === "backorder");
  document.querySelector("#sumMailbox").textContent = data.mailbox.configured
    ? "Connected"
    : "Not configured";
  document.querySelector("#sumDrafts").innerHTML = drafts.length
    ? `<span class="var-unfav">${drafts.length}</span>`
    : "0";
  document.querySelector("#sumBackorders").textContent = backorders.length;
  document.querySelector("#sumEmails").textContent = data.emails.length;

  mailboxNote.textContent = data.mailbox.configured
    ? `Mailbox polling every ${data.mailbox.poll_seconds}s. Last check: ${
        data.mailbox.last_poll ? new Date(data.mailbox.last_poll).toLocaleString() : "pending"
      }${data.mailbox.last_error ? ` — last error: ${data.mailbox.last_error}` : ""}`
    : "Mailbox not configured — set IMAP_HOST / IMAP_USER / IMAP_PASSWORD (and optional IMAP_PORT, " +
      "IMAP_FOLDER) in the server environment. The backorder loop and test emails work without it.";

  queueBody.innerHTML = data.queue.length
    ? data.queue
        .map((so) => {
          const lines = so.lines
            .map((l) => `${l.quantity} × ${l.sku} @ ${money(l.unit_price)}`)
            .join("<br>");
          const availability = so.availability
            .map((a) => {
              if (a.short > 0) {
                const building = data.building[a.sku] || 0;
                const hint = building > 0 ? ` (${building} building)` : "";
                return `<span class="kit-chip kit-short">${a.sku} short ${a.short}${hint}</span>`;
              }
              return `<span class="kit-chip kit-available">${a.sku} OK</span>`;
            })
            .join(" ");
          const status = so.status === "draft"
            ? '<span class="kit-chip kit-not-stocked">DRAFT (UNPOSTED)</span>'
            : '<span class="kit-chip kit-serialized">BACKORDER</span>';
          const action = so.status === "draft"
            ? `<button type="button" class="std-save so-accept" data-so="${esc(so.so_no)}">Accept</button>
               <button type="button" class="acct-delete so-reject" data-so="${esc(so.so_no)}">Reject</button>`
            : "auto-ships when stock lands";
          return `
            <tr>
              <td>${esc(so.so_no)}</td>
              <td><span class="acct-code">${esc(so.customer_code ?? "")}</span> ${esc(so.customer)}</td>
              <td>${lines}</td>
              <td>${money(so.value)}</td>
              <td>${so.requested_date ? esc(so.requested_date) : "—"}</td>
              <td>${availability}</td>
              <td>${status}</td>
              <td>${so.email_subject ? esc(so.email_subject) : "—"}</td>
              <td>${action}</td>
            </tr>`;
        })
        .join("")
    : '<tr><td colspan="9">Nothing awaiting review.</td></tr>';

  inboxBody.innerHTML = data.emails.length
    ? data.emails
        .map((e) => {
          const cls = e.classification;
          const verdict = cls
            ? `${cls.is_order ? "Order" : "Not an order"} (${Math.round((cls.confidence ?? 0) * 100)}%)` +
              `<br><span class="kit-note">${esc(cls.reasoning ?? "")}</span>`
            : "—";
          return `
            <tr>
              <td>${new Date(e.received_at).toLocaleString()}</td>
              <td>${esc(e.from_name || e.from_email)}</td>
              <td>${esc(e.subject)}</td>
              <td>${verdict}</td>
              <td>${EMAIL_STATUS_CHIPS[e.status] ?? esc(e.status)}</td>
              <td>${e.so_no ? esc(e.so_no) : ""}</td>
            </tr>`;
        })
        .join("")
    : '<tr><td colspan="6">No emails yet.</td></tr>';
}

async function getIntake() {
  const response = await fetch("/api/intake");
  const data = await response.json();
  if (!response.ok || data.error) {
    throw new Error(data.error ?? "Unable to load intake.");
  }
  renderIntake(data);
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
    await getIntake();
    return data;
  } catch (error) {
    setMsg(msgNode, String(error), false);
    return null;
  }
}

queueBody.addEventListener("click", async (event) => {
  const accept = event.target.closest(".so-accept");
  const reject = event.target.closest(".so-reject");
  if (accept) {
    accept.disabled = true;
    const result = await post(
      "/api/intake/accept",
      { soNo: accept.dataset.so },
      queueMsg,
      (d) =>
        d.outcome === "invoiced"
          ? `${d.soNo} accepted — in stock, shipped & invoiced as ${d.invoice.invoiceNo} (${money(d.invoice.subtotal)}).`
          : `${d.soNo} accepted — BACKORDER${
              d.built.length
                ? `; building the shortfall: ${d.built.map((b) => `${b.quantity} × ${b.sku} (${b.orderNo})`).join(", ")}`
                : ""
            }. Ships automatically when stock lands.`
    );
    if (!result) {
      accept.disabled = false;
    }
  }
  if (reject) {
    if (!window.confirm(`Reject ${reject.dataset.so}? The draft is closed and the email filed as rejected.`)) {
      return;
    }
    await post("/api/intake/reject", { soNo: reject.dataset.so }, queueMsg, (d) => `${d.soNo} rejected.`);
  }
});

document.querySelector("#checkNow").addEventListener("click", async () => {
  await post(
    "/api/intake/check",
    {},
    inboxMsg,
    (d) =>
      `Checked: ${d.fetched} fetched, ${d.drafted} drafted` +
      (d.shipped.length ? `, shipped ${d.shipped.join(", ")}.` : ".")
  );
});

document.querySelector("#testForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const sent = await post(
    "/api/intake/email",
    {
      fromName: document.querySelector("#testFromName").value.trim(),
      fromEmail: document.querySelector("#testFromEmail").value.trim(),
      subject: document.querySelector("#testSubject").value.trim(),
      body: document.querySelector("#testBody").value.trim()
    },
    testMsg,
    (d) =>
      d.status === "order_drafted"
        ? "Delivered — the clerk drafted a sales order (see the review queue)."
        : `Delivered — filed as ${d.status.replaceAll("_", " ")}.`
  );
  if (sent) {
    document.querySelector("#testSubject").value = "";
    document.querySelector("#testBody").value = "";
  }
});

if (window.location.protocol !== "file:") {
  getIntake().catch((error) => setMsg(queueMsg, error.message, false));
  setInterval(() => {
    getIntake().catch(() => {});
  }, 12000);
}

// Exposed for DOM-level testing.
window.__intake = { renderIntake };
