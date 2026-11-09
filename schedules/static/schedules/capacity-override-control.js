function initializeCapacityOverrideControl(control) {
  const useOverride = control.querySelector('input[name="use_override"]');
  const capacityOverride = control.querySelector('input[name="capacity_override"]');
  if (!useOverride || !capacityOverride) return false;

  const update = () => {
    capacityOverride.disabled = !useOverride.checked;
  };

  useOverride.addEventListener("change", update);
  window.addEventListener("pageshow", update);
  update();
  return true;
}

function initializeCapacityOverrideControls() {
  document
    .querySelectorAll("[data-capacity-override-control]")
    .forEach(initializeCapacityOverrideControl);
}

if (typeof document !== "undefined") {
  initializeCapacityOverrideControls();
}

if (typeof module !== "undefined") {
  module.exports = { initializeCapacityOverrideControl };
}
