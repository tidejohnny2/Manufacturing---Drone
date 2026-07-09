// Internal audit: render the evidence package assertions, run certifications,
// and show the immutable certification history.
const runAudit = document.querySelector("#runAudit");
const opinionBanner = document.querySelector("#opinionBanner");
const opinionMeta = document.querySelector("#opinionMeta");
const opinionBasis = document.querySelector("#opinionBasis");
const findingsWrap = document.querySelector("#findingsWrap");
const findingsBody = document.querySelector("#findingsBody");
const assertionBody = document.querySelector("#assertionBody");
const assertionSummary = document.querySelector("#assertionSummary");
const packageMeta = document.querySelector("#packageMeta");
const historyBody = document.querySelector("#historyBody");

function esc(value) {
  const div = document.createElement("div");
  div.textContent = value ?? "";
  return div.innerHTML;
}

function showOpinion(cert) {
  const cls = { UNQUALIFIED: "opinion-unqualified", QUALIFIED: "opinion-qualified", ADVERSE: "opinion-adverse" };
  opinionBanner.hidden = false;
  opinionBanner.className = `opinion-banner ${cls[cert.opinion] ?? ""}`;
  opinionBanner.textContent = `${cert.opinion} OPINION`;
  opinionMeta.textContent =
    `Certified ${new Date(cert.certified_at).toLocaleString()} by ${cert.actor} · mode: ${cert.mode}` +
    (cert.model ? ` (${cert.model})` : "") +
    ` · assertions ${cert.assertions_passed ?? cert.assertion_summary?.passed}/` +
    `${cert.assertions_total ?? cert.assertion_summary?.total} · ${cert.package_hash.slice(0, 23)}…`;
  opinionBasis.textContent = cert.basis;
  const findings = cert.findings ?? [];
  findingsWrap.hidden = findings.length === 0;
  findingsBody.innerHTML = findings
    .map(
      (f) => `
        <tr>
          <td><span class="kit-chip ${f.severity === "high" ? "kit-short" : f.severity === "medium" ? "kit-substitute" : "kit-available"}">${esc((f.severity ?? "low").toUpperCase())}</span></td>
          <td>${esc(f.area)}</td>
          <td>${esc(f.detail)}</td>
        </tr>`
    )
    .join("");
}

function renderPackage(pkg) {
  assertionBody.innerHTML = pkg.assertions
    .map(
      (a) => `
        <tr>
          <td>${esc(a.id)}</td>
          <td>${esc(a.check)}</td>
          <td>${esc(String(a.expected))}</td>
          <td>${esc(String(a.actual))}</td>
          <td><span class="kit-chip ${a.pass ? "kit-available" : "kit-short"}">${a.pass ? "PASS" : "FAIL"}</span></td>
        </tr>`
    )
    .join("");
  const s = pkg.assertion_summary;
  assertionSummary.textContent = `${s.passed}/${s.total} PASS`;
  assertionSummary.className = `kit-verdict ${s.passed === s.total ? "kit-release" : "kit-hold"}`;
  packageMeta.textContent =
    `Package as of ${new Date(pkg.meta.as_of).toLocaleString()} · ${pkg.journal_entries.length} journal entries · ` +
    `${pkg.order_costs.length} completed orders · ${pkg.package_hash}`;
}

function renderHistory(data) {
  historyBody.innerHTML = data.certifications.length
    ? data.certifications
        .map(
          (c) => `
            <tr>
              <td>${c.id}</td>
              <td>${new Date(c.certified_at).toLocaleString()}</td>
              <td>${esc(c.actor)}</td>
              <td>${esc(c.mode)}${c.model ? ` (${esc(c.model)})` : ""}</td>
              <td><span class="kit-chip ${c.opinion === "UNQUALIFIED" ? "kit-available" : c.opinion === "QUALIFIED" ? "kit-substitute" : "kit-short"}">${esc(c.opinion)}</span></td>
              <td>${c.assertions_passed}/${c.assertions_total}</td>
              <td class="hash-cell">${esc(c.package_hash.slice(0, 23))}…</td>
              <td>${esc(c.basis)}</td>
            </tr>`
        )
        .join("")
    : '<tr><td colspan="8">No certifications yet.</td></tr>';
}

async function getJson(path) {
  const response = await fetch(path);
  const data = await response.json();
  if (!response.ok || data.error) {
    throw new Error(data.error ?? `Unable to load ${path}`);
  }
  return data;
}

async function loadAll() {
  renderPackage(await getJson("/api/audit/package"));
  const history = await getJson("/api/audit/certifications");
  renderHistory(history);
  if (history.certifications.length) {
    showOpinion(history.certifications[0]);
  }
}

runAudit.addEventListener("click", async () => {
  runAudit.disabled = true;
  runAudit.textContent = "Auditing…";
  opinionMeta.textContent = "Building the evidence package and cross-checking with the LLM auditor…";
  try {
    const response = await fetch("/api/audit/certify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ actor: "manual (audit page)" })
    });
    const cert = await response.json();
    if (!response.ok || cert.error) {
      opinionMeta.textContent = cert.error ?? "Certification failed.";
      return;
    }
    showOpinion(cert);
    renderPackage(await getJson("/api/audit/package"));
    renderHistory(await getJson("/api/audit/certifications"));
  } catch (error) {
    opinionMeta.textContent = "Certification failed.";
  } finally {
    runAudit.disabled = false;
    runAudit.textContent = "Run Internal Audit";
  }
});

if (window.location.protocol !== "file:") {
  loadAll().catch((error) => {
    assertionBody.innerHTML = `<tr><td colspan="5">${esc(error.message)}</td></tr>`;
  });
}
