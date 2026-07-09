// Page help system. A page declares its content BEFORE loading this script:
//   window.HELP_TOPICS = { page: "Internal Audit", topics: [{ title, body }] };
// body is an HTML string (authored in-app, not user input). This script injects
// a "? Help" button into the page nav and renders a slide-in accordion panel.
(function () {
  const config = window.HELP_TOPICS;
  if (!config || !config.topics?.length) {
    return;
  }

  const panel = document.createElement("aside");
  panel.className = "help-panel";
  panel.setAttribute("aria-label", `${config.page} help`);
  panel.innerHTML = `
    <div class="help-panel-head">
      <strong>Help — ${config.page}</strong>
      <button type="button" class="help-close" aria-label="Close help">&times;</button>
    </div>
    <div class="help-body">
      ${config.topics
        .map(
          (topic, index) => `
            <details class="help-topic"${index === 0 ? " open" : ""}>
              <summary>${topic.title}</summary>
              <div>${topic.body}</div>
            </details>`
        )
        .join("")}
    </div>`;
  document.body.appendChild(panel);

  const button = document.createElement("button");
  button.type = "button";
  button.className = "nav-link help-button";
  button.title = "Help";
  button.setAttribute("aria-label", "Help");
  button.textContent = "? Help";
  const nav = document.querySelector(".bom-actions") ?? document.querySelector(".controls");
  if (nav) {
    nav.insertBefore(button, nav.querySelector('[onclick="oaOpenSettings()"]'));
  } else {
    document.body.appendChild(button);
  }

  function setOpen(open) {
    panel.classList.toggle("open", open);
    button.classList.toggle("active", open);
  }
  button.addEventListener("click", () => setOpen(!panel.classList.contains("open")));
  panel.querySelector(".help-close").addEventListener("click", () => setOpen(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      setOpen(false);
    }
  });
})();
