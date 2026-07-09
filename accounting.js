// Accounting: trial balance + controls, the cost journal, and chart of
// accounts maintenance (moved from the Costing page).
const tbBody = document.querySelector("#tbBody");
const controlList = document.querySelector("#controlList");
const tbNote = document.querySelector("#tbNote");
const journalBody = document.querySelector("#journalBody");
const journalFilter = document.querySelector("#journalFilter");
const accountsBody = document.querySelector("#accountsBody");
const accountsMsg = document.querySelector("#accountsMsg");

function esc(value) {
  const div = document.createElement("div");
  div.textContent = value ?? "";
  return div.innerHTML;
}

function money(value) {
  return Number(value).toLocaleString("en-US", { style: "currency", currency: "USD" });
}

function titleCase(value) {
  return String(value).replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function renderTrialBalance(data) {
  tbBody.innerHTML =
    data.accounts
      .map(
        (row) => `
          <tr>
            <td>${esc(row.account_no)}</td>
            <td>${esc(row.name)}</td>
            <td>${money(row.total_debit)}</td>
            <td>${money(row.total_credit)}</td>
            <td>${money(row.balance)}</td>
          </tr>`
      )
      .join("") +
    `
      <tr class="labor-total-row">
        <td></td><td><strong>Totals</strong></td>
        <td><strong>${money(data.total_debit)}</strong></td>
        <td><strong>${money(data.total_credit)}</strong></td>
        <td></td>
      </tr>`;
  controlList.innerHTML = data.controls
    .map(
      (control) => `
        <li>
          <span class="kit-chip ${control.ok ? "kit-available" : "kit-short"}">${control.ok ? "PASS" : "FAIL"}</span>
          <strong>${esc(control.name)}</strong>
          <p>${esc(control.detail)}</p>
        </li>`
    )
    .join("");
  tbNote.textContent = data.note;
  document.querySelector("#sumDebits").textContent = money(data.total_debit);
  document.querySelector("#sumCredits").textContent = money(data.total_credit);
  const allOk = data.controls.every((control) => control.ok);
  const sumControls = document.querySelector("#sumControls");
  sumControls.innerHTML = `<span class="${allOk ? "var-fav" : "var-unfav"}">${
    data.controls.filter((control) => control.ok).length
  }/${data.controls.length} PASS</span>`;
}

function renderJournal(data) {
  journalBody.innerHTML = data.entries.length
    ? data.entries
        .map((entry) => {
          const first = `
            <tr class="journal-entry-row">
              <td>${new Date(entry.posted_at).toLocaleString()}</td>
              <td>${esc(entry.event_ref)}</td>
              <td>${titleCase(entry.event_type)}</td>
              <td>${esc(entry.order_no ?? "")}</td>
              <td colspan="3">${esc(entry.memo)}</td>
            </tr>`;
          const lines = entry.lines
            .map(
              (line) => `
                <tr>
                  <td colspan="4"></td>
                  <td>${esc(line.account_no)} ${esc(line.account_name)}</td>
                  <td>${Number(line.debit) ? money(line.debit) : ""}</td>
                  <td>${Number(line.credit) ? money(line.credit) : ""}</td>
                </tr>`
            )
            .join("");
          return first + lines;
        })
        .join("")
    : '<tr><td colspan="7">No journal entries yet.</td></tr>';
}

let accountTypes = [];

function typeSelect(selected, locked) {
  return `<select class="acct-input" data-field="accountType" ${locked ? "disabled" : ""}>${accountTypes
    .map((t) => `<option value="${t}" ${t === selected ? "selected" : ""}>${t}</option>`)
    .join("")}</select>`;
}

function sideSelect(selected, locked) {
  return `<select class="acct-input" data-field="normalSide" ${locked ? "disabled" : ""}>
    <option value="debit" ${selected === "debit" ? "selected" : ""}>debit</option>
    <option value="credit" ${selected === "credit" ? "selected" : ""}>credit</option>
  </select>`;
}

function renderAccounts(data) {
  accountTypes = data.account_types;
  const newType = document.querySelector("#newAccountType");
  if (!newType.options.length) {
    newType.innerHTML = accountTypes.map((t) => `<option value="${t}">${t}</option>`).join("");
  }
  document.querySelector("#sumAccounts").textContent = data.accounts.length;
  accountsBody.innerHTML = data.accounts
    .map((account) => {
      const locked = account.protected || account.posting_count > 0;
      const lockNote = account.protected
        ? '<span class="kit-chip kit-serialized" title="Used by the posting engine">ENGINE</span>'
        : account.posting_count > 0
          ? '<span class="kit-chip kit-not-stocked" title="Has postings">POSTED</span>'
          : "";
      return `
        <tr data-account="${esc(account.account_no)}">
          <td>${esc(account.account_no)} ${lockNote}</td>
          <td><input class="acct-input acct-name" data-field="name" maxlength="80" value="${esc(account.name)}" /></td>
          <td>${typeSelect(account.account_type, locked)}</td>
          <td>${sideSelect(account.normal_side, locked)}</td>
          <td>${account.posting_count}</td>
          <td>${money(account.balance)}</td>
          <td>
            <button type="button" class="std-save acct-save">Save</button>
            ${!locked ? '<button type="button" class="acct-delete">Delete</button>' : ""}
          </td>
        </tr>`;
    })
    .join("");
}

async function getJson(path) {
  const response = await fetch(path);
  const data = await response.json();
  if (!response.ok || data.error) {
    throw new Error(data.error ?? `Unable to load ${path}`);
  }
  return data;
}

async function loadAccounts() {
  renderAccounts(await getJson("/api/costing/accounts"));
}

async function accountAction(body, successText) {
  accountsMsg.textContent = "Saving…";
  accountsMsg.className = "kit-verdict";
  body.actor = document.querySelector("#accountsActor").value.trim();
  try {
    const response = await fetch("/api/costing/accounts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok || data.error) {
      accountsMsg.textContent = data.error ?? "Save failed.";
      accountsMsg.classList.add("kit-hold");
      return false;
    }
    accountsMsg.textContent = successText;
    accountsMsg.classList.add("kit-release");
    await loadAccounts();
    return true;
  } catch (error) {
    accountsMsg.textContent = "Save failed.";
    accountsMsg.classList.add("kit-hold");
    return false;
  }
}

document.querySelector("#accountCreate").addEventListener("submit", async (event) => {
  event.preventDefault();
  const created = await accountAction(
    {
      action: "create",
      accountNo: document.querySelector("#newAccountNo").value.trim(),
      name: document.querySelector("#newAccountName").value.trim(),
      accountType: document.querySelector("#newAccountType").value,
      normalSide: document.querySelector("#newAccountSide").value
    },
    "Account added."
  );
  if (created) {
    document.querySelector("#newAccountNo").value = "";
    document.querySelector("#newAccountName").value = "";
  }
});

accountsBody.addEventListener("click", async (event) => {
  const row = event.target.closest("tr[data-account]");
  if (!row) {
    return;
  }
  const accountNo = row.dataset.account;
  if (event.target.closest(".acct-save")) {
    const body = { action: "update", accountNo, name: row.querySelector('[data-field="name"]').value.trim() };
    const type = row.querySelector('[data-field="accountType"]');
    const side = row.querySelector('[data-field="normalSide"]');
    if (!type.disabled) {
      body.accountType = type.value;
      body.normalSide = side.value;
    }
    await accountAction(body, `Saved ${accountNo}.`);
  }
  if (event.target.closest(".acct-delete")) {
    if (!window.confirm(`Delete account ${accountNo}? Only possible while it has no postings.`)) {
      return;
    }
    await accountAction({ action: "delete", accountNo }, `Deleted ${accountNo}.`);
  }
});

async function loadJournal() {
  const filter = journalFilter.value.trim();
  const path = filter
    ? `/api/costing/ledger?orderNo=${encodeURIComponent(filter)}&limit=40`
    : "/api/costing/ledger?limit=30";
  renderJournal(await getJson(path));
}

async function loadLive() {
  renderTrialBalance(await getJson("/api/costing/trial-balance"));
  await loadJournal();
}

async function loadAll() {
  await loadLive();
  await loadAccounts();
}

journalFilter.addEventListener("change", () => {
  loadJournal().catch(() => {});
});

if (window.location.protocol !== "file:") {
  loadAll().catch((error) => {
    tbNote.textContent = error.message;
  });
  setInterval(() => {
    loadLive().catch(() => {});
  }, 15000);
}

// Exposed for DOM-level testing.
window.__accounting = { renderTrialBalance, renderJournal, renderAccounts };
