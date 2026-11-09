function initializePhysicianPicker(picker) {
  const filter = picker.querySelector("[data-physician-picker-filter]");
  const filterArea = picker.querySelector("[data-physician-picker-filter-area]");
  const choiceElements = Array.from(picker.querySelectorAll("[data-physician-picker-choice]"));
  const selectedColumn = picker.querySelector("[data-physician-picker-selected-column]");
  const selectedArea = picker.querySelector("[data-physician-picker-selected]");
  const tokens = picker.querySelector("[data-physician-picker-tokens]");
  const empty = picker.querySelector("[data-physician-picker-empty]");
  const choicesList = picker.querySelector("[data-physician-picker-choices]");
  const count = picker.ownerDocument.getElementById(choicesList?.getAttribute("aria-describedby"));
  const selectionInstructions = count?.getAttribute("data-physician-picker-selection-instructions")
    || "Choose the physicians participating in this event.";
  const choices = choiceElements
    .map((element) => ({
      element,
      input: element.querySelector('input[type="checkbox"]'),
      name: element.querySelector("label")?.textContent.trim(),
    }))
    .filter((choice) => choice.input && choice.name);

  if (!filter || !filterArea || !selectedColumn || !selectedArea || !tokens || !empty || !count) {
    return false;
  }

  function selectedCountText(amount) {
    return `${amount} selected ${amount === 1 ? "physician" : "physicians"}. ${selectionInstructions}`;
  }

  function renderSelectedPhysicians() {
    const selected = choices.filter((choice) => choice.input.checked);
    tokens.replaceChildren();
    empty.hidden = selected.length > 0;
    count.textContent = selectedCountText(selected.length);

    selected.forEach((choice) => {
      const item = picker.ownerDocument.createElement("li");
      const remove = picker.ownerDocument.createElement("button");
      const name = picker.ownerDocument.createElement("span");
      const close = picker.ownerDocument.createElement("span");
      remove.type = "button";
      remove.className = "physician-picker-token-remove";
      // TEXT-CONTRACT: physician-picker-remove-name — docs/design.md physician picker.
      // Assistive users need capitalized Remove, one separating space, and the full physician name.
      // Protected by schedules/tests/physicians_picker.test.js (server-rendered selected choices).
      remove.setAttribute("aria-label", `Remove ${choice.name}`);
      name.className = "physician-picker-token-name";
      name.textContent = choice.name;
      close.className = "physician-picker-token-close";
      close.setAttribute("aria-hidden", "true");
      close.textContent = "\u00d7";
      remove.addEventListener("click", () => {
        choice.input.checked = false;
        choice.input.dispatchEvent(new Event("change", { bubbles: true }));
        filter.focus();
      });
      item.className = "physician-picker-token";
      remove.append(name, close);
      item.append(remove);
      tokens.append(item);
    });
  }

  function filterChoices() {
    const query = filter.value.trim().toLocaleLowerCase();
    choices.forEach((choice) => {
      choice.element.hidden = Boolean(query) && !choice.name.toLocaleLowerCase().includes(query);
    });
  }

  choices.forEach((choice) => choice.input.addEventListener("change", renderSelectedPhysicians));
  filter.addEventListener("input", filterChoices);
  filterArea.hidden = false;
  selectedColumn.hidden = false;
  picker.setAttribute("data-physician-picker-ready", "");
  renderSelectedPhysicians();
  return true;
}

function initializePhysicianPickers(root = document) {
  root.querySelectorAll("[data-physician-picker]").forEach(initializePhysicianPicker);
}

if (typeof document !== "undefined") {
  initializePhysicianPickers();
}

if (typeof module !== "undefined") {
  module.exports = { initializePhysicianPicker, initializePhysicianPickers };
}
