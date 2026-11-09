const solveRangeControls = typeof require !== "undefined"
  ? require("./range-controls.js")
  : window.MurielRangeControls;

function initializePreceptingSolveControls(form) {
  const effectiveRange = form.querySelector("[data-range-controls-effective-range]");
  const balanceRange = form.querySelector("[data-solve-balance-range]");
  const customDays = form.querySelector("[data-solve-custom-days]");
  function solveRange() {
    const kind = solveRangeControls.selectedChoice(form, "range_kind");
    if (kind === "month") {
      const month = form.elements.month.value;
      if (!/^\d{4}-\d{2}$/.test(month)) return null;
      const [year, monthNumber] = month.split("-").map(Number);
      const end = new Date(Date.UTC(year, monthNumber, 0));
      return [`${month}-01`, solveRangeControls.isoDate(end)];
    }
    if (kind === "quarter") {
      const year = Number(form.elements.academic_year.value);
      const quarter = Number(form.elements.quarter.value);
      if (!Number.isInteger(year) || ![1, 2, 3, 4].includes(quarter)) return null;
      const [yearOffset, firstMonth] = [[0, 6], [0, 9], [1, 0], [1, 3]][quarter - 1];
      const startYear = year + yearOffset;
      const start = `${startYear}-${String(firstMonth + 1).padStart(2, "0")}-01`;
      const end = new Date(Date.UTC(startYear, firstMonth + 3, 0));
      return [start, solveRangeControls.isoDate(end)];
    }
    const start = form.elements.start_date.value;
    const end = form.elements.end_date.value;
    return start && end && start <= end ? [start, end] : null;
  }

  function balanceStart(end) {
    const span = form.elements.balance_span.value;
    const days = { "1-day": 1, "1-week": 7, "2-weeks": 14, "4-weeks": 28 }[span];
    if (days) return solveRangeControls.isoDate(new Date(solveRangeControls.dateValue(end).getTime() - (days - 1) * 86400000));
    const months = { "1-month": 1, "3-months": 3, "6-months": 6, "1-year": 12 }[span];
    if (months) {
      const start = solveRangeControls.dateValue(solveRangeControls.shiftMonths(end, -months));
      start.setUTCDate(start.getUTCDate() + 1);
      return solveRangeControls.isoDate(start);
    }
    const custom = Number(form.elements.balance_custom_days.value);
    return Number.isInteger(custom) && custom > 0
      ? solveRangeControls.isoDate(new Date(solveRangeControls.dateValue(end).getTime() - (custom - 1) * 86400000))
      : null;
  }

  function effectiveSolveRange(range) {
    const today = form.dataset.solveToday;
    if (!range || !/^\d{4}-\d{2}-\d{2}$/.test(today) || range[1] < today) return range;
    return [range[0] < today ? today : range[0], range[1]];
  }

  function update() {
    const kind = solveRangeControls.selectedChoice(form, "range_kind");
    solveRangeControls.setPickerVisibility(form, kind);
    const isCustomRange = kind === "custom";
    const customRangeValid = solveRangeControls.customDateRange(form, "range_kind", "custom");
    if (!isCustomRange) {
      form.elements.start_date.setCustomValidity("");
      form.elements.end_date.setCustomValidity("");
    }
    const isCustomSpan = form.elements.balance_span.value === "custom";
    customDays.hidden = !isCustomSpan;
    form.elements.balance_custom_days.required = isCustomSpan;
    const range = customRangeValid ? solveRange() : null;
    effectiveRange.value = solveRangeControls.formatRange(effectiveSolveRange(range));
    const start = range && balanceStart(range[1]);
    balanceRange.value = start
      ? solveRangeControls.formatRange([start, range[1]])
      : range ? "Enter a positive whole number of days." : "Choose a complete solve range.";
  }

  function dispatchRangeChange(control) {
    control.dispatchEvent(new Event("input", { bubbles: true }));
    control.dispatchEvent(new Event("change", { bubbles: true }));
  }

  for (const button of form.querySelectorAll("[data-solve-step-month]")) {
    button.addEventListener("click", () => {
      const month = form.elements.month;
      if (!/^\d{4}-\d{2}$/.test(month.value)) return;
      const [year, monthNumber] = month.value.split("-").map(Number);
      const shifted = new Date(Date.UTC(year, monthNumber - 1 + Number(button.dataset.solveStepMonth), 1));
      month.value = `${shifted.getUTCFullYear()}-${String(shifted.getUTCMonth() + 1).padStart(2, "0")}`;
      dispatchRangeChange(month);
    });
  }

  for (const button of form.querySelectorAll("[data-solve-step-academic-year]")) {
    button.addEventListener("click", () => {
      const academicYear = form.elements.academic_year;
      const nextYear = Number(academicYear.value) + Number(button.dataset.solveStepAcademicYear);
      if (![...academicYear.options].some((option) => Number(option.value) === nextYear)) return;
      academicYear.value = String(nextYear);
      dispatchRangeChange(academicYear);
    });
  }

  for (const button of form.querySelectorAll("[data-solve-step-quarter]")) {
    button.addEventListener("click", () => {
      const academicYear = form.elements.academic_year;
      const quarter = form.elements.quarter;
      const step = Number(button.dataset.solveStepQuarter);
      let nextQuarter = Number(quarter.value) + step;
      let nextYear = Number(academicYear.value);
      if (nextQuarter === 0) {
        nextQuarter = 4;
        nextYear -= 1;
      } else if (nextQuarter === 5) {
        nextQuarter = 1;
        nextYear += 1;
      }
      if (!Number.isInteger(nextYear) || ![1, 2, 3, 4].includes(nextQuarter)) return;
      if (![...academicYear.options].some((option) => Number(option.value) === nextYear)) return;
      academicYear.value = String(nextYear);
      quarter.value = String(nextQuarter);
      dispatchRangeChange(quarter);
    });
  }

  form.addEventListener("input", update);
  form.addEventListener("change", update);
  update();
  return true;
}

function initializePreceptingSolveControlForms(root = document) {
  const form = root.querySelector("[data-precepting-solve-controls]");
  return form ? initializePreceptingSolveControls(form) : false;
}

if (typeof document !== "undefined") initializePreceptingSolveControlForms();

if (typeof module !== "undefined") {
  module.exports = { initializePreceptingSolveControls, initializePreceptingSolveControlForms };
}
