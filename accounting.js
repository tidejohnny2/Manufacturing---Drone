// General Ledger: trial balance + controls and chart of accounts
// maintenance. The cost journal browser lives on the Journals page
// (journals.js).
const tbBody = document.querySelector("#tbBody");
const controlList = document.querySelector("#controlList");
const tbNote = document.querySelector("#tbNote");
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
  // Manual journal entry is available for non-plant companies only.
  manualAccounts = data.accounts;
  const section = document.querySelector("#manualEntrySection");
  if (section) {
    const canManual = companyId() !== 1 && data.accounts.length > 0;
    section.hidden = !canManual;
    if (canManual && !mjeLines.children.length) {
      addMjeLine();
      addMjeLine();
    }
  }
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
  // withCompany (company.js) scopes the fetch to the active company.
  const response = await fetch(withCompany(path));
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
  body.companyId = companyId();
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

// ===== Manual journal entry (non-plant companies) =====
let manualAccounts = [];
const mjeLines = document.querySelector("#mjeLines");
const mjeMsg = document.querySelector("#mjeMsg");

function addMjeLine() {
  if (!mjeLines) {
    return;
  }
  const row = document.createElement("tr");
  const options = manualAccounts
    .map((a) => `<option value="${esc(a.account_no)}">${esc(a.account_no)} — ${esc(a.name)}</option>`)
    .join("");
  row.innerHTML = `
    <td><select class="acct-input mje-account">${options}</select></td>
    <td><input class="acct-input mje-debit" type="number" min="0" step="0.01" placeholder="0.00" /></td>
    <td><input class="acct-input mje-credit" type="number" min="0" step="0.01" placeholder="0.00" /></td>
    <td><button type="button" class="acct-delete mje-remove" aria-label="Remove line">&times;</button></td>`;
  mjeLines.appendChild(row);
}

function mjeReadLines() {
  return [...mjeLines.querySelectorAll("tr")].map((row) => ({
    accountNo: row.querySelector(".mje-account").value,
    debit: Number(row.querySelector(".mje-debit").value) || 0,
    credit: Number(row.querySelector(".mje-credit").value) || 0
  }));
}

function updateMjeBalance() {
  const lines = mjeReadLines();
  const dr = lines.reduce((s, l) => s + l.debit, 0);
  const cr = lines.reduce((s, l) => s + l.credit, 0);
  const balanced = Math.abs(dr - cr) < 0.005 && dr > 0;
  const node = document.querySelector("#mjeBalance");
  node.textContent = `DR ${money(dr)} · CR ${money(cr)} — ${balanced ? "balanced" : "out of balance"}`;
  node.className = `kit-verdict ${balanced ? "kit-release" : "kit-hold"}`;
}

if (mjeLines) {
  document.querySelector("#mjeAddLine").addEventListener("click", addMjeLine);
  mjeLines.addEventListener("input", updateMjeBalance);
  mjeLines.addEventListener("click", (event) => {
    if (event.target.closest(".mje-remove")) {
      event.target.closest("tr").remove();
      updateMjeBalance();
    }
  });
  document.querySelector("#mjePost").addEventListener("click", async () => {
    mjeMsg.textContent = "Posting…";
    mjeMsg.className = "kit-verdict";
    try {
      const response = await fetch("/api/journal-entry", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          companyId: companyId(),
          memo: document.querySelector("#mjeMemo").value.trim(),
          lines: mjeReadLines().filter((l) => l.debit > 0 || l.credit > 0)
        })
      });
      const data = await response.json();
      if (!response.ok || data.error) {
        mjeMsg.textContent = data.error ?? "Post failed.";
        mjeMsg.classList.add("kit-hold");
        return;
      }
      mjeMsg.textContent = `Posted ${data.eventRef} (${money(data.totalDebit)}).`;
      mjeMsg.classList.add("kit-release");
      document.querySelector("#mjeMemo").value = "";
      mjeLines.innerHTML = "";
      addMjeLine();
      addMjeLine();
      updateMjeBalance();
      await loadAll();
    } catch (error) {
      mjeMsg.textContent = "Post failed.";
      mjeMsg.classList.add("kit-hold");
    }
  });
}

async function loadLive() {
  renderTrialBalance(await getJson("/api/costing/trial-balance"));
}

async function loadAll() {
  await loadLive();
  await loadAccounts();
}

if (window.location.protocol !== "file:") {
  loadAll().catch((error) => {
    tbNote.textContent = error.message;
  });
  setInterval(() => {
    loadLive().catch(() => {});
  }, 15000);
}

// Exposed for DOM-level testing.
window.__accounting = { renderTrialBalance, renderAccounts, addMjeLine };
