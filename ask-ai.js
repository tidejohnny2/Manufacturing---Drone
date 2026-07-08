// Ask AI slide-in panel (platform pattern): posts {question} to /api/ask-ai,
// renders {answer, mode, model|note} with lightweight markdown support.
(function () {
  const panel = document.getElementById("askPanel");
  const thread = document.getElementById("askThread");
  const form = document.getElementById("askForm");
  const input = document.getElementById("askInput");
  const statusDot = document.getElementById("askStatus");

  window.toggleAskAI = function () {
    panel.classList.toggle("open");
    if (panel.classList.contains("open")) {
      input.focus();
    }
  };

  function esc(value) {
    const div = document.createElement("div");
    div.textContent = value;
    return div.innerHTML;
  }

  function inline(value) {
    return value.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  }

  function render(markdown) {
    const lines = esc(markdown).replace(/\r/g, "").split("\n");
    const isSeparator = (line) => /^\s*\|?[\s:|-]*-{2,}[\s:|-]*\|?\s*$/.test(line);
    const cells = (row) => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
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
      const bullet = line.trim().match(/^(-|\*|•)\s+(.*)$/);
      if (bullet) {
        html += "<ul>";
        while (i < lines.length) {
          const item = lines[i].trim().match(/^(-|\*|•)\s+(.*)$/);
          if (!item) {
            break;
          }
          html += "<li>" + inline(item[2]) + "</li>";
          i++;
        }
        html += "</ul>";
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

  async function ask(question) {
    bubble("user", esc(question));
    const pending = bubble("assistant", "Analyzing live production data…");
    pending.classList.add("pending");
    try {
      const response = await fetch("/api/ask-ai", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question })
      });
      const data = await response.json();
      pending.remove();
      if (!response.ok || data.error) {
        bubble("assistant", `<span style="color:#dc2626">${esc(data.error ?? "Request failed.")}</span>`);
        return;
      }
      const meta = data.mode === "claude" ? `Answered by ${data.model ?? "Claude"}` : data.note ?? "Offline";
      bubble("assistant", render(data.answer ?? "No response"), meta);
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

  document.querySelectorAll(".ask-chip").forEach((chip) => {
    chip.addEventListener("click", () => ask(chip.textContent.trim()));
  });

  fetch("/api/ask-ai/status")
    .then((response) => response.json())
    .then((data) => {
      statusDot.textContent = data.live ? "● Claude live" : "● Offline";
      statusDot.className = "ask-status " + (data.live ? "live" : "offline");
    })
    .catch(() => {});
})();
