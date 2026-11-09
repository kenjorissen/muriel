function initializePeriodPicker(form) {
  const inputs = [...form.querySelectorAll("[data-period-picker-input]")];
  const button = form.querySelector("[data-period-picker-submit]");

  if (
    !inputs.length ||
    !button ||
    inputs.some((input) => typeof input.showPicker !== "function") ||
    typeof form.requestSubmit !== "function"
  ) {
    return false;
  }

  for (const input of inputs) {
    const initialValue = input.value;

    input.addEventListener("click", () => {
      try {
        input.showPicker();
      } catch {
        // The browser keeps its ordinary focused input behavior when it refuses.
      }
    });

    input.addEventListener("change", () => {
      if (input.value && input.checkValidity() && input.value !== initialValue) {
        form.requestSubmit();
      }
    });
  }

  button.hidden = true;
  return true;
}

function initializePeriodPickerForms(root = document) {
  root.querySelectorAll("[data-period-picker-form]").forEach(initializePeriodPicker);
}

if (typeof document !== "undefined") {
  initializePeriodPickerForms();
}

if (typeof module !== "undefined") {
  module.exports = { initializePeriodPicker, initializePeriodPickerForms };
}
