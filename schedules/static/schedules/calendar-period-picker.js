(() => {
  function pickerMonth(value) {
    const match = /^(\d{4})-(\d{2})$/.exec(value);
    if (!match) {
      return null;
    }
    const month = Number(match[2]);
    return month >= 1 && month <= 12 ? { year: Number(match[1]), month } : null;
  }

  for (const form of document.querySelectorAll("[data-calendar-period-picker]")) {
    const input = form.querySelector(
      "[data-calendar-period-picker-input], input[type='month'], input[type='date'], select[name='academic_year'], select[name='calendar_year']",
    );
    if (!input || typeof form.requestSubmit !== "function") {
      continue;
    }

    const initialValue = input.value;
    if (typeof input.showPicker === "function") {
      input.addEventListener("click", () => {
        try {
          input.showPicker();
        } catch {
          // Browsers that refuse retain their ordinary focused input behavior.
        }
      });
    }

    input.addEventListener("change", () => {
      if (!input.value || !input.checkValidity() || input.value === initialValue) {
        return;
      }
      if (form.hasAttribute("data-calendar-period-picker-sync-academic-year")) {
        const monthInput = form.querySelector("input[name='month']");
        const month = monthInput && pickerMonth(monthInput.value);
        if (!month) {
          return;
        }
        monthInput.value = `${Number(input.value) + (month.month < 7 ? 1 : 0)}-${String(month.month).padStart(2, "0")}`;
      }
      form.requestSubmit();
    });
  }
})();
