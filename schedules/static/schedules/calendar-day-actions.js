function initializeCalendarDayActions(root = document) {
  const interactiveSelector =
    "a, button, input, select, textarea, label, summary, [contenteditable], [role], [tabindex]";

  for (const cell of root.querySelectorAll("[data-calendar-day-action]")) {
    const actionLink = document.getElementById(cell.dataset.calendarDayAction);
    if (!actionLink) {
      continue;
    }

    cell.addEventListener("click", (event) => {
      const activeSelection =
        typeof window !== "undefined" && window.getSelection ? window.getSelection() : null;
      const selection = activeSelection ? activeSelection.toString() : "";
      const interactiveContent = event.target.closest(interactiveSelector);
      if (
        event.defaultPrevented ||
        event.detail === 0 ||
        event.button !== 0 ||
        event.metaKey ||
        event.ctrlKey ||
        event.shiftKey ||
        event.altKey ||
        selection ||
        (interactiveContent && interactiveContent !== cell && cell.contains(interactiveContent))
      ) {
        return;
      }
      actionLink.click();
    });
  }
}

if (typeof document !== "undefined") {
  initializeCalendarDayActions();
}

if (typeof module !== "undefined") {
  module.exports = { initializeCalendarDayActions };
}
