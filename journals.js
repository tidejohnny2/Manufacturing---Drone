// Journals: the cost journal browser (moved from the General Ledger page) —
// type/date/order filters plus keyset "Load older" paging over the ledger.
const journalBody = document.querySelector("#journalBody");
const journalFilter = document.querySelector("#journalFilter");
const journalType = document.querySelector("#journalType");
const journalFrom = document.querySelector("#journalFrom");
const journalTo = document.querySelector("#journalTo");
const journalOlder = document.querySelector("#journalOlder");
const journalCount = document.querySelector("#journalCount");

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

// Journal state: the loaded page(s) plus whether older entries remain.
// "paged" pauses the 15s auto-refresh so loaded history isn't yanked away.
const journalState = { entries: [], hasMore: false, total: 0, paged: false };

const JOURNAL_TYPE_LABELS = { cogs: "COGS", dm_issue: "DM Issue", fg_transfer: "FG Transfer" };

function journalTypeLabel(value) {
  return JOURNAL_TYPE_LABELS[value] ?? titleCase(value);
}

// Analytic tag chips on a journal line (report-only overlay).
function tagChips(tags) {
  if (!tags || !tags.length) {
    return "";
  }
  return (
    " " +
    tags
      .map(
        (t) =>
          `<span class="an-chip" title="${esc(t.group)}"><span class="an-dot" style="background:${esc(
            t.color || "#64748b"
          )}"></span>${esc(t.name)}</span>`
      )
      .join(" ")
  );
}

function syncJournalTypes(types) {
  const current = journalType.value;
  journalType.innerHTML =
    '<option value="">All types</option>' +
    types
      .map((t) => `<option value="${esc(t)}" ${t === current ? "selected" : ""}>${journalTypeLabel(t)}</option>`)
      .join("");
  document.querySelector("#sumTypes").textContent = types.length;
}

function renderJournal(data) {
  if (data) {
    journalState.entries = data.entries;
    journalState.hasMore = Boolean(data.has_more);
    journalState.total = Number(data.total ?? data.entries.length);
    if (data.event_types) {
      syncJournalTypes(data.event_types);
    }
  }
  journalBody.innerHTML = journalState.entries.length
    ? journalState.entries
        .map((entry) => {
          const first = `
            <tr class="journal-entry-row">
              <td>${new Date(entry.posted_at).toLocaleString()}</td>
              <td>${esc(entry.event_ref)}</td>
              <td>${journalTypeLabel(entry.event_type)}</td>
              <td>${esc(entry.order_no ?? "")}</td>
              <td colspan="3">${esc(entry.memo)}</td>
            </tr>`;
          const lines = entry.lines
            .map(
              (line) => `
                <tr>
                  <td colspan="4"></td>
                  <td>${esc(line.account_no)} ${esc(line.account_name)}${tagChips(line.tags)}</td>
                  <td>${Number(line.debit) ? money(line.debit) : ""}</td>
                  <td>${Number(line.credit) ? money(line.credit) : ""}</td>
                </tr>`
            )
            .join("");
          return first + lines;
        })
        .join("")
    : '<tr><td colspan="7">No matching journal entries.</td></tr>';
  journalOlder.hidden = !journalState.hasMore;
  journalCount.textContent = journalState.entries.length
    ? `Showing ${journalState.entries.length} of ${journalState.total} ${
        journalState.total === 1 ? "entry" : "entries"
      }${journalState.hasMore ? "" : " — end of the ledger."}`
    : "";
  const latest = journalState.entries[0];
  document.querySelector("#sumEntries").textContent = journalState.total;
  document.querySelector("#sumLatest").textContent = latest
    ? new Date(latest.posted_at).toLocaleString()
    : "—";
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

function journalQuery(beforeId) {
  const params = new URLSearchParams({ limit: "30" });
  const order = journalFilter.value.trim();
  if (order) {
    params.set("orderNo", order);
  }
  if (journalType.value) {
    params.set("eventType", journalType.value);
  }
  if (journalFrom.value) {
    params.set("dateFrom", journalFrom.value);
  }
  if (journalTo.value) {
    params.set("dateTo", journalTo.value);
  }
  if (beforeId) {
    params.set("beforeId", beforeId);
  }
  return `/api/costing/ledger?${params}`;
}

async function loadJournal() {
  journalState.paged = false;
  renderJournal(await getJson(journalQuery(null)));
}

async function loadOlderJournal() {
  const last = journalState.entries[journalState.entries.length - 1];
  if (!last) {
    return;
  }
  const data = await getJson(journalQuery(last.id));
  journalState.paged = true;
  data.entries = journalState.entries.concat(data.entries);
  renderJournal(data);
}

for (const control of [journalFilter, journalType, journalFrom, journalTo]) {
  control.addEventListener("change", () => {
    loadJournal().catch(() => {});
  });
}

journalOlder.addEventListener("click", () => {
  loadOlderJournal().catch(() => {});
});

document.querySelector("#journalClear").addEventListener("click", () => {
  journalFilter.value = "";
  journalType.value = "";
  journalFrom.value = "";
  journalTo.value = "";
  loadJournal().catch(() => {});
});

if (window.location.protocol !== "file:") {
  loadJournal().catch((error) => {
    journalCount.textContent = error.message;
  });
  setInterval(() => {
    // Skip the refresh once the user has paged into history.
    if (!journalState.paged) {
      loadJournal().catch(() => {});
    }
  }, 15000);
}

// Exposed for DOM-level testing.
window.__journals = { renderJournal };
