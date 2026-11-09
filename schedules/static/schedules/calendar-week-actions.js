function initializeCalendarWeekActions(root = document) {
  const interactiveSelector =
    "a, button, input, select, textarea, label, summary, [contenteditable], [tabindex]";

  for (const week of root.querySelectorAll("[data-calendar-week-action]")) {
    const actionLink = document.getElementById(week.dataset.calendarWeekAction);
    if (!actionLink) {
      continue;
    }

    week.addEventListener("click", (event) => {
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
        week.contains(interactiveContent)
      ) {
        return;
      }
      actionLink.click();
    });
  }
}

if (typeof document !== "undefined") {
  initializeCalendarWeekActions();
}

if (typeof module !== "undefined") {
  module.exports = { initializeCalendarWeekActions };
}
