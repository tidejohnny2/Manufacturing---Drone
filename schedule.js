// Production Schedule board: per-line station Gantt from /api/schedule, plus a
// what-if planner backed by /api/schedule/preview.
const ganttLines = document.querySelector("#ganttLines");
const previewForm = document.querySelector("#previewForm");
const previewSku = document.querySelector("#previewSku");
const previewQty = document.querySelector("#previewQty");
const previewDue = document.querySelector("#previewDue");
const previewMessage = document.querySelector("#previewMessage");
const previewResult = document.querySelector("#previewResult");
const createPlanned = document.querySelector("#createPlanned");

const ORDER_COLORS = ["#0284c7", "#7c3aed", "#059669", "#d97706", "#0891b2", "#c026d3", "#65a30d", "#e11d48"];
let scheduleData = null;
let preview = null;

previewDue.value = new Date(Date.now() + 7 * 86400000).toISOString().slice(0, 10);

function esc(value) {
  const div = document.createElement("div");
  div.textContent = value;
  return div.innerHTML;
}

function setMessage(text, isError = false) {
  previewMessage.textContent = text;
  previewMessage.classList.toggle("error", isError);
}

function fmtTime(iso) {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function renderLine(line, now) {
  const previewHere = preview && preview.facility_id === line.facility_id ? preview : null;
  const allSegments = line.orders.flatMap((order) => order.segments)
    .concat(previewHere ? previewHere.segments : []);

  let inner = `
    <div class="pipeline-title">
      <strong>${esc(line.facility_name)}</strong>
      <span>${line.orders.length} active order${line.orders.length === 1 ? "" : "s"}${previewHere ? " + preview" : ""}</span>
    </div>`;

  if (!allSegments.length) {
    ganttLines.insertAdjacentHTML(
      "beforeend",
      `<section class="plan-band">${inner}<p class="gantt-empty">Line idle — nothing scheduled.</p></section>`
    );
    return;
  }

  const starts = allSegments.map((segment) => new Date(segment.start).getTime());
  const ends = allSegments.map((segment) => new Date(segment.end).getTime());
  const t0 = Math.min(...starts, now);
  const t1 = Math.max(...ends, now) + (Math.max(...ends) - t0) * 0.03 + 1000;
  const span = t1 - t0;
  const pos = (iso) => ((new Date(iso).getTime() - t0) / span) * 100;

  const legend = line.orders
    .map((order, index) => {
      const onTime = new Date(order.finish) <= new Date(order.due_date + "T23:59:59");
      return `
        <span class="gantt-chip">
          <span class="gantt-dot" style="background:${ORDER_COLORS[index % ORDER_COLORS.length]}"></span>
          ${esc(order.order_no)} (${esc(order.finished_good)}, qty ${order.quantity}) —
          ${order.percent_complete}% · due ${esc(order.due_date)}
          <strong class="${onTime ? "ontime" : "late"}">${onTime ? "on time" : "AT RISK"}</strong>
        </span>`;
    })
    .join("");
  inner += `<div class="gantt-legend">${legend}${previewHere ? '<span class="gantt-chip"><span class="gantt-dot ghost-dot"></span>PREVIEW (not created)</span>' : ""}</div>`;

  const rows = line.stations
    .map((station) => {
      const bars = line.orders
        .map((order, index) => {
          const segment = order.segments.find((item) => item.zone_id === station.zone_id);
          if (!segment) {
            return "";
          }
          const left = pos(segment.start);
          const width = Math.max(pos(segment.end) - left, 0.4);
          return `<span class="gantt-bar" style="left:${left}%;width:${width}%;background:${ORDER_COLORS[index % ORDER_COLORS.length]}"
                    title="${esc(order.order_no)} at ${esc(station.station)}: ${fmtTime(segment.start)} → ${fmtTime(segment.end)}">${esc(order.order_no)}</span>`;
        })
        .join("");
      const ghost = previewHere
        ? (() => {
            const segment = previewHere.segments.find((item) => item.zone_id === station.zone_id);
            if (!segment) {
              return "";
            }
            const left = pos(segment.start);
            const width = Math.max(pos(segment.end) - left, 0.4);
            return `<span class="gantt-bar ghost" style="left:${left}%;width:${width}%"
                      title="PREVIEW at ${esc(station.station)}: ${fmtTime(segment.start)} → ${fmtTime(segment.end)}">PREVIEW</span>`;
          })()
        : "";
      const capacity = station.capacity ? ` (cap ${station.capacity})` : "";
      return `
        <div class="gantt-station">${esc(station.station)}${capacity}</div>
        <div class="gantt-track">${bars}${ghost}</div>`;
    })
    .join("");

  const nowPos = pos(new Date(now).toISOString());
  const nowMarker = nowPos >= 0 && nowPos <= 100
    ? `<span class="gantt-now" style="left:${nowPos}%" title="now"></span>`
    : "";

  const ticks = [0, 0.25, 0.5, 0.75, 1]
    .map((fraction) => `<span class="gantt-tick" style="left:${fraction * 100}%">${fmtTime(new Date(t0 + span * fraction).toISOString())}</span>`)
    .join("");

  ganttLines.insertAdjacentHTML(
    "beforeend",
    `<section class="plan-band">
      ${inner}
      <div class="gantt-grid">
        ${rows}
        <div class="gantt-station"></div>
        <div class="gantt-axis">${ticks}</div>
      </div>
      ${nowMarker ? `<div class="gantt-nowline-note">Red line = now</div>` : ""}
    </section>`
  );

  // Place the now marker inside every track of the section just added.
  if (nowMarker) {
    const section = ganttLines.lastElementChild;
    section.querySelectorAll(".gantt-track").forEach((track) => {
      track.insertAdjacentHTML("beforeend", `<span class="gantt-now" style="left:${nowPos}%"></span>`);
    });
  }
}

function renderSchedule() {
  if (!scheduleData) {
    return;
  }
  ganttLines.innerHTML = "";
  const now = new Date(scheduleData.now).getTime();
  scheduleData.lines.forEach((line) => renderLine(line, now));
}

async function loadSchedule() {
  if (window.location.protocol === "file:") {
    return;
  }
  const response = await fetch("/api/schedule");
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error ?? "Unable to load the schedule.");
  }
  scheduleData = data;
  renderSchedule();
}

function renderPreviewResult(data) {
  const onTime = new Date(data.finish) <= new Date(data.due_date + "T23:59:59");
  const pulls = data.pulls
    .map((pull) => {
      if (pull.short) {
        return `<p class="preview-warn">⚠ ${esc(pull.part_number)}: needs ${pull.required}, only ${pull.available} available —
          a replenishment order for ${pull.shortfall} will be auto-created.</p>`;
      }
      return `<p class="preview-ok">✓ ${esc(pull.part_number)}: needs ${pull.required}, ${pull.available} available.</p>`;
    })
    .join("");
  previewResult.innerHTML = `
    <p><strong>${esc(data.finished_good)}</strong> × ${data.quantity}</p>
    <p>Starts <strong>${fmtTime(data.start)}</strong> · finishes <strong>${fmtTime(data.finish)}</strong>
       (${data.planned_test_minutes} test min; ${data.recorded_minutes} recorded min)</p>
    <p>Due ${esc(data.due_date)} —
       <strong class="${onTime ? "ontime" : "late"}">${onTime ? "makes the due date" : "AT RISK of missing the due date"}</strong></p>
    ${pulls}`;
}

previewForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  setMessage("Projecting…");
  try {
    const response = await fetch("/api/schedule/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        finishedSku: previewSku.value,
        quantity: Number(previewQty.value),
        dueDate: previewDue.value
      })
    });
    const data = await response.json();
    if (!response.ok || data.error) {
      setMessage(data.error ?? "Preview failed.", true);
      return;
    }
    preview = data;
    renderPreviewResult(data);
    renderSchedule();
    createPlanned.hidden = false;
    setMessage("Preview shown as dashed bars on the board. Nothing has been created yet.");
  } catch (error) {
    setMessage(String(error), true);
  }
});

createPlanned.addEventListener("click", async () => {
  if (!preview) {
    return;
  }
  setMessage("Creating order…");
  try {
    const response = await fetch("/api/production-orders", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        finishedSku: preview.finished_good,
        quantity: preview.quantity,
        dueDate: preview.due_date
      })
    });
    const data = await response.json();
    if (!response.ok || data.error) {
      setMessage(data.error ?? "Unable to create the order.", true);
      return;
    }
    setMessage(`Created ${data.order.order_no}.`);
    preview = null;
    createPlanned.hidden = true;
    previewResult.textContent = "Preview an order to see its projected schedule.";
    await loadSchedule().catch(() => {});
  } catch (error) {
    setMessage(String(error), true);
  }
});

loadSchedule().catch((error) => setMessage(error.message, true));
setInterval(() => {
  loadSchedule().catch(() => {});
}, 10000);
