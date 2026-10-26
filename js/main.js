// Entry point: starts the page.
import { $, state } from "./base.js";
import { connectCompanionEvents, getCurrentRepo, moveLocalViews, refreshOnFocus } from "./server.js";
import { updateGitHubStatus } from "./data.js";
import { render } from "./board.js";
import { renderRepoSelect, renderViewsList } from "./settings.js";
import { closeSidebar, openSidebar } from "./sidebar.js";

for (const key of Object.keys(localStorage)) {
  if (key === "ghpr.token" || key === "ghpr.ownerTypes" || key.startsWith("ghpr.cache.")) localStorage.removeItem(key);
}

moveLocalViews().finally(() => {
  renderRepoSelect();
  renderViewsList();
  if (getCurrentRepo()) {
    render();
  } else {
    $("#settings-dialog").showModal();
  }
});

connectCompanionEvents();


setInterval(updateGitHubStatus, 10 * 1000);

document.addEventListener("visibilitychange", refreshOnFocus);

window.__dashboard = { render, state, openSidebar, closeSidebar, updateGitHubStatus };
