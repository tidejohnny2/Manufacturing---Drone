// Shared company switcher for the multi-company accounting pages. The active
// company is stored in localStorage and sent as ?company=N on every scoped
// API call; changing it reloads the page. Company 1 is the Drone Plant.
const COMPANY_KEY = "drones-company";

function companyId() {
  try {
    return Number(localStorage.getItem(COMPANY_KEY) || 1) || 1;
  } catch (_) {
    return 1;
  }
}

// Append the active company to an API path (handles an existing querystring).
function withCompany(path) {
  const sep = path.includes("?") ? "&" : "?";
  return `${path}${sep}company=${companyId()}`;
}

async function initCompanySwitcher() {
  const host = document.querySelector("#companySwitcher");
  if (!host || window.location.protocol === "file:") {
    return;
  }
  let companies = [{ id: 1, name: "Drone Plant" }];
  try {
    const response = await fetch("/api/companies");
    const data = await response.json();
    if (response.ok && data.companies && data.companies.length) {
      companies = data.companies;
    }
  } catch (_) {
    /* keep the default */
  }
  const current = companyId();
  const options = companies
    .map(
      (c) =>
        `<option value="${c.id}" ${Number(c.id) === current ? "selected" : ""}>${
          (c.name ?? "").replace(/[<>&]/g, "")
        }</option>`
    )
    .join("");
  host.innerHTML = `<span class="company-label">Company</span><select id="companySelect" aria-label="Company">${options}</select>`;
  const select = host.querySelector("#companySelect");
  if (!companies.some((c) => Number(c.id) === current)) {
    select.value = String(companies[0].id);
    try {
      localStorage.setItem(COMPANY_KEY, select.value);
    } catch (_) {}
  }
  select.addEventListener("change", () => {
    try {
      localStorage.setItem(COMPANY_KEY, select.value);
    } catch (_) {}
    window.location.reload();
  });
}

initCompanySwitcher();
