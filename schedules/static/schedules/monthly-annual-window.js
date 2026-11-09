function initializeMonthlyAnnualWindow(form) {
  const section = form?.querySelector("[data-monthly-annual-window]");
  const months = section?.querySelector("[data-monthly-annual-months]");
  const modeInputs = section ? [...section.querySelectorAll("input[name='annual_window']")] : [];
  const monthInputs = months ? [...months.querySelectorAll("select")] : [];
  if (!section || !months || !modeInputs.length || monthInputs.length !== 2) return false;

  function selectedMode() {
    return modeInputs.find((input) => input.checked)?.value;
  }

  function update() {
    const selectedMonths = selectedMode() === "window";
    months.toggleAttribute("data-disabled", !selectedMonths);
    monthInputs.forEach((input) => {
      input.disabled = !selectedMonths;
      input.required = selectedMonths;
      input.setCustomValidity("");
    });
  }

  modeInputs.forEach((input) => input.addEventListener("change", update));
  form.addEventListener("submit", () => {
    if (selectedMode() === "window") {
      monthInputs.forEach((input) => input.setCustomValidity(input.value ? "" : "Choose a month."));
    }
  });
  update();
  return true;
}

if (typeof module !== "undefined") module.exports = { initializeMonthlyAnnualWindow };
if (typeof document !== "undefined") {
  document.querySelectorAll("form.source-schedule-form").forEach(initializeMonthlyAnnualWindow);
}
