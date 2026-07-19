// Ask AI agent panel (platform pattern): self-injecting on every page.
// Posts {question, session_id} to /api/ask-ai; renders {answer, mode,
// model|note, proposed_actions[]}. Write actions render an approval card;
// Execute posts {session_id, approvals} to /api/ask-ai/execute and the
// agent continues with the results (which may propose further actions).
(function () {
  const nav = document.querySelector(".bom-actions") ?? document.querySelector(".controls");

  const button = document.createElement("button");
  button.type = "button";
  button.className = "nav-link ask-ai-button";
  button.textContent = "Ask AI";
  if (nav) {
    nav.insertBefore(button, nav.querySelector('[onclick="oaOpenSettings()"]'));
  } else {
    document.body.appendChild(button);
  }

  const panel = document.createElement("aside");
  panel.id = "askPanel";
  panel.className = "ask-panel";
  panel.setAttribute("aria-label", "Ask AI panel");
  panel.innerHTML = `
    <div class="ask-panel-head">
      <strong>Ask AI</strong>
      <span id="askStatus" class="ask-status offline">● Checking…</span>
      <button type="button" class="ask-close" aria-label="Close">&times;</button>
    </div>
    <div id="askThread" class="ask-thread">
      <div class="ask-bubble">
        Hi! I can answer questions about the plant — and take action: create orders, re-sequence the
        queue, set capacities and cycle times, adjust working hours, edit cost standards, record
        signoffs, or run the internal audit. Every change waits for your approval.
      </div>
    </div>
    <div class="ask-chips">
      <button type="button" class="ask-chip">What is running right now?</button>
      <button type="button" class="ask-chip">Create 2 transport cases due next Friday</button>
      <button type="button" class="ask-chip">Any unfavorable variances lately?</button>
      <button type="button" class="ask-chip">Run the internal audit</button>
    </div>
    <form id="askForm" class="ask-form">
      <input id="askInput" autocomplete="off" placeholder="Ask, or tell me what to do…" />
      <button type="submit">Send</button>
    </form>`;
  document.body.appendChild(panel);

  const thread = panel.querySelector("#askThread");
  const form = panel.querySelector("#askForm");
  const input = panel.querySelector("#askInput");
  const statusDot = panel.querySelector("#askStatus");
  let sessionId = sessionStorage.getItem("manufacturing-ask-session") || "";

  window.toggleAskAI = function () {
    panel.classList.toggle("open");
    if (panel.classList.contains("open")) {
      input.focus();
    }
  };
  button.addEventListener("click", window.toggleAskAI);
  panel.querySelector(".ask-close").addEventListener("click", window.toggleAskAI);

  function esc(value) {
    const div = document.createElement("div");
    div.textContent = value;
    return div.innerHTML;
  }

  function inline(value) {
    return value
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>");
  }

  function render(markdown) {
    const lines = esc(markdown).replace(/\r/g, "").split("\n");
    const isSeparator = (line) => /^\s*\|?[\s:|-]*-{2,}[\s:|-]*\|?\s*$/.test(line);
    const cells = (row) => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
    const listItem = (line) => {
      const match = line.match(/^(\s*)([-*•]|\d+[.)])\s+(.*)$/);
      return match ? { indent: match[1].length, ordered: /^\d/.test(match[2]), text: match[3] } : null;
    };
    let html = "";
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (line.indexOf("|") !== -1 && i + 1 < lines.length && isSeparator(lines[i + 1])) {
        const head = cells(line);
        i += 2;
        html += "<div style=\"overflow-x:auto\"><table><thead><tr>";
        head.forEach((cell) => (html += "<th>" + inline(cell) + "</th>"));
        html += "</tr></thead><tbody>";
        while (i < lines.length && lines[i].indexOf("|") !== -1) {
          html += "<tr>";
          cells(lines[i]).forEach((cell) => (html += "<td>" + inline(cell) + "</td>"));
          html += "</tr>";
          i++;
        }
        html += "</tbody></table></div>";
        continue;
      }
      const heading = line.match(/^#{1,4}\s+(.*)$/);
      if (heading) {
        html += "<div class=\"ask-h\">" + inline(heading[1]) + "</div>";
        i++;
        continue;
      }
      if (listItem(line)) {
        const items = [];
        while (i < lines.length && listItem(lines[i])) {
          items.push(listItem(lines[i]));
          i++;
        }
        const topTag = items[0].ordered ? "ol" : "ul";
        html += "<" + topTag + ">";
        let j = 0;
        while (j < items.length) {
          const item = items[j];
          let li = inline(item.text);
          j++;
          if (j < items.length && items[j].indent > item.indent) {
            const subTag = items[j].ordered ? "ol" : "ul";
            li += "<" + subTag + ">";
            while (j < items.length && items[j].indent > item.indent) {
              li += "<li>" + inline(items[j].text) + "</li>";
              j++;
            }
            li += "</" + subTag + ">";
          }
          html += "<li>" + li + "</li>";
        }
        html += "</" + topTag + ">";
        continue;
      }
      if (line.trim()) {
        html += "<p>" + inline(line) + "</p>";
      }
      i++;
    }
    return html;
  }

  function bubble(role, innerHtml, meta) {
    const div = document.createElement("div");
    div.className = "ask-bubble" + (role === "user" ? " user" : "");
    div.innerHTML = innerHtml + (meta ? `<div class="ask-meta">${esc(meta)}</div>` : "");
    thread.appendChild(div);
    div.scrollIntoView({ behavior: "smooth", block: "end" });
    return div;
  }

  function metaOf(data) {
    return data.mode === "claude" ? `Answered by ${data.model ?? "Claude"}` : data.note ?? "Offline";
  }

  function handleResponse(data) {
    if (data.session_id) {
      sessionId = data.session_id;
      sessionStorage.setItem("manufacturing-ask-session", sessionId);
    }
    if (data.results?.length) {
      const lines = data.results
        .map((r) => {
          const declined = r.result?.status === "declined";
          const failed = r.result?.status === "error";
          const mark = declined ? "✕ declined" : failed ? `✕ ${r.result.message}` : "✓ done";
          return `<li>${inline(esc(r.summary))} — <em>${esc(mark)}</em></li>`;
        })
        .join("");
      bubble("assistant", `<div class="ask-results"><strong>Actions</strong><ul>${lines}</ul></div>`);
    }
    if (data.answer) {
      bubble("assistant", render(data.answer), metaOf(data));
    }
    if (data.proposed_actions?.length) {
      actionsCard(data.proposed_actions);
    }
  }

  function actionsCard(actions) {
    const card = document.createElement("div");
    card.className = "ask-bubble ask-actions-card";
    const rows = actions
      .map(
        (action) => `
          <label class="ask-action-row">
            <input type="checkbox" data-id="${esc(action.id)}" checked />
            <span>${inline(esc(action.summary))}</span>
          </label>`
      )
      .join("");
    card.innerHTML = `
      <div class="ask-actions-head">Proposed action${actions.length > 1 ? "s" : ""} — approve to commit</div>
      ${rows}
      <div class="ask-actions-buttons">
        <button type="button" class="ask-go">Execute</button>
        <button type="button" class="ask-no">Cancel</button>
      </div>`;
    thread.appendChild(card);
    card.scrollIntoView({ behavior: "smooth", block: "end" });
    const head = card.querySelector(".ask-actions-head");

    card.querySelector(".ask-go").addEventListener("click", async () => {
      const approvals = {};
      card.querySelectorAll("input[type=checkbox]").forEach((box) => {
        approvals[box.dataset.id] = box.checked;
      });
      card.querySelectorAll("button, input").forEach((el) => (el.disabled = true));
      head.textContent = "Executing…";
      try {
        const response = await fetch("/api/ask-ai/execute", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: sessionId, approvals })
        });
        const data = await response.json();
        if (!response.ok || data.error) {
          head.textContent = "Failed";
          bubble("assistant", `<span style="color:#dc2626">${esc(data.error ?? "Execute failed.")}</span>`);
          return;
        }
        head.textContent = "Executed ✓";
        handleResponse(data);
      } catch (error) {
        head.textContent = "Failed";
        bubble("assistant", `<span style="color:#dc2626">Execute failed: ${esc(String(error))}</span>`);
      }
    });
    card.querySelector(".ask-no").addEventListener("click", () => {
      card.querySelectorAll("button, input").forEach((el) => (el.disabled = true));
      head.textContent = "Cancelled";
      bubble("assistant", "No changes made.");
    });
    return card;
  }

  async function ask(question) {
    bubble("user", esc(question));
    const pending = bubble("assistant", "Working on it…");
    pending.classList.add("pending");
    try {
      const response = await fetch("/api/ask-ai", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, session_id: sessionId })
      });
      const data = await response.json();
      pending.remove();
      if (!response.ok || data.error) {
        bubble("assistant", `<span style="color:#dc2626">${esc(data.error ?? "Request failed.")}</span>`);
        return;
      }
      handleResponse(data);
      if (!data.answer && !data.proposed_actions?.length) {
        bubble("assistant", "No response.");
      }
    } catch (error) {
      pending.remove();
      bubble("assistant", `<span style="color:#dc2626">Request failed: ${esc(String(error))}</span>`);
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const question = input.value.trim();
    if (!question) {
      return;
    }
    input.value = "";
    ask(question);
  });

  panel.querySelectorAll(".ask-chip").forEach((chip) => {
    chip.addEventListener("click", () => ask(chip.textContent.trim()));
  });

  if (window.location.protocol !== "file:") {
    fetch("/api/ask-ai/status")
      .then((response) => response.json())
      .then((data) => {
        statusDot.textContent = data.live ? "● Claude live" : "● Offline";
        statusDot.className = "ask-status " + (data.live ? "live" : "offline");
      })
      .catch(() => {});
  }

  // Exposed for DOM-level testing.
  window.__askAi = { actionsCard, bubble, handleResponse, render };
})();
