const rangeControls = typeof require !== "undefined"
  ? require("./range-controls.js")
  : window.MurielRangeControls;

function initializeScheduleReportControls(form) {
  const periodInputs = [...form.querySelectorAll("input[name='period']")];
  const coverageInputs = [...form.querySelectorAll("input[name='coverage']")];
  const coverageMessage = form.querySelector("[data-schedule-report-coverage-message]");
  const effectiveRange = form.querySelector("[data-range-controls-effective-range]");
  const includedDates = form.querySelector("[data-schedule-report-included-dates]");
  const includedDatesValue = form.querySelector("[data-schedule-report-included-dates-value]");

  const selectedPeriod = () => rangeControls.selectedChoice(form, "period");

  function selectedRange() {
    const period = selectedPeriod();
    if (period === "custom") {
      const start = form.elements.start_date.value;
      const end = form.elements.end_date.value;
      return start && end ? [start, end] : null;
    }
    const year = Number(form.elements[period === "academic-year" ? "academic_year" : "calendar_year"].value);
    if (!Number.isInteger(year) || year < 1) return null;
    return period === "academic-year" ? [`${year}-07-01`, `${year + 1}-06-30`] : [`${year}-01-01`, `${year}-12-31`];
  }

  function selectedCoverage() {
    return coverageInputs.find((input) => input.checked)?.value;
  }

  function coverageRange(range) {
    if (!range) return null;
    const coverage = selectedCoverage();
    const today = form.dataset.scheduleReportToday;
    if (coverage === "history") return [range[0], range[1] < today ? range[1] : previousDay(today)];
    if (coverage === "scheduled") return [range[0] > today ? range[0] : today, range[1]];
    return range;
  }

  const previousDay = (value) => rangeControls.isoDate(
    new Date(rangeControls.dateValue(value).getTime() - 24 * 60 * 60 * 1000),
  );

  function customRangeIsValid() {
    const valid = rangeControls.customDateRange(form, "period", "custom");
    if (selectedPeriod() !== "custom") {
      form.elements.start_date.setCustomValidity("");
      form.elements.end_date.setCustomValidity("");
    }
    return valid && form.checkValidity();
  }

  function submitIfValid() {
    if (customRangeIsValid()) {
      form.requestSubmit();
    } else {
      form.reportValidity();
    }
  }

  function update() {
    const period = selectedPeriod();
    rangeControls.setPickerVisibility(form, period);
    const range = selectedRange();
    const today = form.dataset.scheduleReportToday;
    const hasHistory = range && range[0] < today;
    const hasScheduled = range && range[1] >= today;
    const hasBoth = hasHistory && hasScheduled;
    let message = "History ends yesterday; Scheduled begins today in the organization timezone.";
    coverageInputs.forEach((input) => {
      const unavailable = (input.value === "history" && !hasHistory)
        || (input.value === "scheduled" && !hasScheduled)
        || (input.value === "history-and-scheduled" && !hasBoth);
      input.disabled = Boolean(range && unavailable);
      form.querySelector(`[data-schedule-report-coverage-choice="${input.value}"]`).hidden = input.value === "history-and-scheduled" && Boolean(range && !hasBoth);
    });
    if (range && !hasHistory) {
      coverageInputs.find((choice) => choice.value === "scheduled").checked = true;
      message = "History is unavailable because this period begins today or later.";
    } else if (range && !hasScheduled) {
      coverageInputs.find((choice) => choice.value === "history").checked = true;
      message = "Scheduled is unavailable because this period ends before today.";
    }
    coverageMessage.textContent = message;
    effectiveRange.value = rangeControls.formatRange(range);
    const includedRange = coverageRange(range);
    const isClipped = range && includedRange && (range[0] !== includedRange[0] || range[1] !== includedRange[1]);
    includedDates.hidden = !isClipped;
    includedDatesValue.value = rangeControls.formatRange(includedRange);
  }

  form.addEventListener("change", (event) => {
    const rangeChanged = periodInputs.includes(event.target)
      || ["academic_year", "calendar_year", "start_date", "end_date"].includes(event.target.name);
    update();
    if (rangeChanged) {
      const range = selectedRange();
      if (range && range[0] < form.dataset.scheduleReportToday && range[1] >= form.dataset.scheduleReportToday) {
        coverageInputs.find((choice) => choice.value === "history-and-scheduled").checked = true;
        update();
      }
    }
    submitIfValid();
  });
  update();
  return true;
}

function initializeScheduleReportControlForms(root = document) {
  const form = root.querySelector("[data-schedule-report-controls]");
  return form ? initializeScheduleReportControls(form) : false;
}

if (typeof document !== "undefined") {
  initializeScheduleReportControlForms();
}

if (typeof module !== "undefined") {
  module.exports = { initializeScheduleReportControls, initializeScheduleReportControlForms };
}
