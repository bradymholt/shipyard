// Entry point: starts the page.
import { $, LS, state } from "./base.js";
import { connectServerEvents, moveLocalViews, refreshOnFocus } from "./server.js";
import { updateGitHubStatus } from "./data.js";
import { render } from "./board.js";
import { renderRepoSelect, renderViewsList, route } from "./settings.js";
import { closeSidebar, openSidebar } from "./sidebar.js";

for (const key of Object.keys(localStorage)) {
  if (key === "ghpr.token" || key === "ghpr.ownerTypes" || key.startsWith("ghpr.cache.")) localStorage.removeItem(key);
}

moveLocalViews().finally(() => {
  renderRepoSelect();
  renderViewsList();
  render();
});

connectServerEvents();
route();

const themeSelect = $("#theme-select");
themeSelect.value = document.documentElement.dataset.theme || "";
themeSelect.addEventListener("change", () => {
  const theme = themeSelect.value;
  if (theme) {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem(LS.THEME, theme);
  } else {
    delete document.documentElement.dataset.theme;
    localStorage.removeItem(LS.THEME);
  }
});


setInterval(updateGitHubStatus, 10 * 1000);

document.addEventListener("visibilitychange", refreshOnFocus);

window.__dashboard = { render, state, openSidebar, closeSidebar, updateGitHubStatus };
