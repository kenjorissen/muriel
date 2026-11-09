(function (root) {
  function selectedChoice(form, name) {
    return [...form.querySelectorAll(`input[name='${name}']`)].find((input) => input.checked)?.value;
  }

  function formatRange(range) {
    if (!range) return "Choose a complete date range.";
    return range.map(formatDate).join(" through ");
  }

  function formatDate(value) {
    const [year, month, day] = value.split("-").map(Number);
    return `${["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][month - 1]} ${day}, ${year}`;
  }

  function dateValue(value) {
    const [year, month, day] = value.split("-").map(Number);
    return new Date(Date.UTC(year, month - 1, day));
  }

  function isoDate(value) {
    return value.toISOString().slice(0, 10);
  }

  function shiftMonths(value, months) {
    const shifted = dateValue(value);
    const day = shifted.getUTCDate();
    shifted.setUTCDate(1);
    shifted.setUTCMonth(shifted.getUTCMonth() + months);
    const lastDay = new Date(Date.UTC(shifted.getUTCFullYear(), shifted.getUTCMonth() + 1, 0)).getUTCDate();
    shifted.setUTCDate(Math.min(day, lastDay));
    return isoDate(shifted);
  }

  function setPickerVisibility(form, choice) {
    form.querySelectorAll("[data-range-control-value], [data-schedule-report-period-value]").forEach((element) => {
      element.hidden = (element.dataset.rangeControlValue || element.dataset.scheduleReportPeriodValue) !== choice;
    });
  }

  function customDateRange(form, choiceName, customChoice) {
    if (selectedChoice(form, choiceName) !== customChoice) return true;
    const start = form.elements.start_date;
    const end = form.elements.end_date;
    const invalidOrder = start.value && end.value && start.value > end.value;
    const message = invalidOrder ? "End date must be on or after the start date." : "";
    start.setCustomValidity(start.value ? message : "Enter a start date.");
    end.setCustomValidity(end.value ? message : "Enter an end date.");
    return Boolean(start.value && end.value) && !invalidOrder;
  }

  const api = { selectedChoice, formatRange, dateValue, isoDate, shiftMonths, setPickerVisibility, customDateRange };
  root.MurielRangeControls = api;
  if (typeof module !== "undefined") module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
