// Onadapt theme engine (platform pattern, saved locally in this browser).
(function () {
  const THEMES = [
    ["ms-blue", "Microsoft blue", "#0078D4"],
    ["sky", "Sky", "#5b8def"],
    ["mint", "Mint", "#3fbfa4"],
    ["sunflower", "Sunflower", "#eab308"],
    ["amber-gold", "Amber gold", "#ca8a04"],
    ["slate", "Slate", "#64748b"],
    ["neutral", "Neutral", "#475569"],
    ["light", "Light", "#2563eb"]
  ];
  const STORAGE_KEY = "manufacturing-ui-theme";
  let saved = "ms-blue";
  try {
    saved = localStorage.getItem(STORAGE_KEY) || "ms-blue";
  } catch (_) {}
  let pending = saved;

  function apply(theme) {
    document.documentElement.dataset.theme = theme;
  }

  function renderSwatches() {
    const grid = document.getElementById("oaThemes");
    grid.innerHTML = THEMES.map(
      ([id, label, color]) => `
        <button type="button" class="oa-theme-option${id === pending ? " selected" : ""}" data-theme-id="${id}">
          <span class="oa-theme-swatch" style="background:${color}"></span>${label}
        </button>`
    ).join("");
    grid.querySelectorAll("[data-theme-id]").forEach((button) => {
      button.addEventListener("click", () => {
        pending = button.dataset.themeId;
        apply(pending);
        renderSwatches();
      });
    });
  }

  function build() {
    if (document.getElementById("oaSettings")) {
      return;
    }
    const overlay = document.createElement("div");
    overlay.id = "oaSettings";
    overlay.className = "oa-settings-overlay";
    overlay.innerHTML = `
      <div class="oa-settings-card">
        <h3>Settings</h3>
        <span class="oa-settings-label">Color theme</span>
        <div id="oaThemes" class="oa-theme-grid"></div>
        <div class="oa-settings-actions">
          <button type="button" class="cancel">Cancel</button>
          <button type="button" class="save">Save</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) {
        close();
      }
    });
    overlay.querySelector(".cancel").addEventListener("click", close);
    overlay.querySelector(".save").addEventListener("click", () => {
      saved = pending;
      try {
        localStorage.setItem(STORAGE_KEY, saved);
      } catch (_) {}
      apply(saved);
      overlay.classList.remove("open");
    });
  }

  function close() {
    apply(saved);
    document.getElementById("oaSettings").classList.remove("open");
  }

  window.oaOpenSettings = function () {
    build();
    pending = saved;
    renderSwatches();
    document.getElementById("oaSettings").classList.add("open");
  };

  apply(saved);
})();
