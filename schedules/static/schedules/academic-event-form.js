function addAcademicEventRange(rangeTemplate, rangeContainer, rangeTotal) {
  const index = Number(rangeTotal.value);
  if (!Number.isInteger(index)) return false;

  const range = rangeTemplate.content.cloneNode(true);
  range.querySelectorAll("input").forEach((input) => {
    input.name = input.name.replace(/additional_ranges-__prefix__-/, `additional_ranges-${index}-`);
    input.id = input.id.replace(/additional_ranges-__prefix__-/, `additional_ranges-${index}-`);
    input.value = "";
    input.checked = false;
  });
  range.querySelectorAll("label").forEach((label) => {
    label.htmlFor = label.htmlFor.replace(
      /additional_ranges-__prefix__-/,
      `additional_ranges-${index}-`,
    );
  });
  rangeContainer.append(range);
  rangeTotal.value = String(index + 1);
  return true;
}

function setAcademicEventRangeDeleted(range, deleted) {
  const entry = range.closest("[data-academic-event-range-entry]");
  const deletion = range.querySelector('input[name$="-DELETE"]');
  const undo = entry?.querySelector("[data-academic-event-range-undo]");
  if (!deletion || !entry || !undo) return false;

  deletion.checked = deleted;
  range.hidden = deleted;
  undo.hidden = !deleted;
  return true;
}

function initializeAcademicEventRanges(form) {
  const rangeContainer = form.querySelector("#additional-academic-event-ranges");
  const rangeTemplate = form.querySelector("#academic-event-range-template");
  const rangeTotal = form.querySelector("#id_additional_ranges-TOTAL_FORMS");
  const addRange = form.querySelector("#add-academic-event-range");
  if (!rangeContainer || !rangeTemplate || !rangeTotal || !addRange) return false;

  rangeContainer.querySelectorAll("[data-academic-event-range]").forEach((range) => {
    const nativeDelete = range.querySelector("[data-academic-event-range-delete]");
    const remove = range.querySelector("[data-academic-event-range-remove]");
    if (!nativeDelete || !remove) return;
    nativeDelete.hidden = true;
    remove.hidden = false;
    if (range.querySelector('input[name$="-DELETE"]')?.checked) {
      setAcademicEventRangeDeleted(range, true);
    }
  });

  addRange.addEventListener("click", () => {
    addAcademicEventRange(rangeTemplate, rangeContainer, rangeTotal);
    const range = rangeContainer.lastElementChild?.querySelector("[data-academic-event-range]");
    const nativeDelete = range?.querySelector("[data-academic-event-range-delete]");
    const remove = range?.querySelector("[data-academic-event-range-remove]");
    if (nativeDelete && remove) {
      nativeDelete.hidden = true;
      remove.hidden = false;
    }
  });
  rangeContainer.addEventListener("click", (event) => {
    const remove = event.target.closest("[data-academic-event-range-remove]");
    const restore = event.target.closest("[data-academic-event-range-restore]");
    const control = remove || restore;
    if (!control) return;
    const range = control.closest("[data-academic-event-range-entry]")?.querySelector(
      "[data-academic-event-range]",
    );
    if (range) setAcademicEventRangeDeleted(range, Boolean(remove));
  });
  return true;
}

function initializeAcademicEventFacultySelection(form) {
  const allAvailableFaculty = form.elements.all_available_faculty;
  const picker = form.querySelector("[data-physician-picker]");
  if (!allAvailableFaculty || !picker) return false;

  const filter = picker.querySelector("[data-physician-picker-filter]");
  const choices = Array.from(picker.querySelectorAll('input[type="checkbox"]'));
  const allAvailable = picker.querySelector("[data-physician-picker-all-available]");
  const tokens = picker.querySelector("[data-physician-picker-tokens]");
  const empty = picker.querySelector("[data-physician-picker-empty]");
  const instruction = picker.querySelector("[data-physician-picker-selected-instruction]");
  if (!filter || !allAvailable || !tokens || !empty || !instruction) return false;

  function update() {
    const active = allAvailableFaculty.checked;
    picker.toggleAttribute("data-all-available-faculty", active);
    filter.disabled = active;
    choices.forEach((choice) => { choice.disabled = active; });
    allAvailable.hidden = !active;
    tokens.hidden = active;
    instruction.hidden = active;
    empty.hidden = active || choices.some((choice) => choice.checked);
  }

  allAvailableFaculty.addEventListener("change", update);
  update();
  return true;
}

function initializeAcademicEventForm(form) {
  if (!form) return false;
  const isCreateForm = form.hasAttribute("data-academic-event-create-form");

  const definition = form.elements.definition;
  const academicYear = form.elements.academic_year;
  const detail = form.elements.suffix;
  const requiredParticipantCount = form.elements.required_participant_count;
  const periodOfDay = form.elements.period_of_day;
  const preceptingImpact = form.elements.precepting_impact;
  const startDate = form.elements.start_date;
  const endDate = form.elements.end_date;
  const preview = document.getElementById("academic-event-display-preview");
  const definitionData = document.getElementById("academic-event-definition-data");
  if (
    !definition ||
    !academicYear ||
    !detail ||
    !requiredParticipantCount ||
    !periodOfDay ||
    !preceptingImpact ||
    !startDate ||
    !endDate ||
    !preview ||
    !definitionData
  ) {
    return false;
  }

  initializeAcademicEventRanges(form);
  initializeAcademicEventFacultySelection(form);

  const definitions = JSON.parse(definitionData.textContent);
  const quarterMonths = { Q1: 7, Q2: 10, Q3: 1, Q4: 4 };

  function selectedDefinition() {
    return definitions[definition.value];
  }

  function updatePreview() {
    const selected = selectedDefinition();
    if (!selected) {
      preview.textContent = "";
      return;
    }
    const eventDetail = detail.value.trim();
    preview.textContent = eventDetail
      ? `${selected.base_name} - ${eventDetail}`
      : selected.base_name;
  }

  function setQuarterDates() {
    const selected = selectedDefinition();
    const year = Number(academicYear.value);
    if (!selected || !Number.isInteger(year)) return;

    const month = quarterMonths[selected.expected_quarter];
    if (!month) return;
    const calendarYear = month < 7 ? year + 1 : year;
    const start = new Date(Date.UTC(calendarYear, month - 1, 1));
    const end = new Date(start);
    end.setUTCDate(end.getUTCDate() + selected.duration_days - 1);
    startDate.value = start.toISOString().slice(0, 10);
    endDate.value = end.toISOString().slice(0, 10);
  }

  function setRequiredParticipantCount() {
    const selected = selectedDefinition();
    if (selected) requiredParticipantCount.value = selected.required_participant_count;
  }

  function setCoverage() {
    const selected = selectedDefinition();
    if (selected) periodOfDay.value = selected.period_of_day;
  }

  function setPreceptingImpact() {
    const selected = selectedDefinition();
    if (selected) preceptingImpact.value = selected.precepting_impact;
  }

  definition.addEventListener("change", () => {
    updatePreview();
    if (isCreateForm) {
      setQuarterDates();
      setCoverage();
      setPreceptingImpact();
      setRequiredParticipantCount();
    }
  });
  academicYear.addEventListener("change", () => {
    if (isCreateForm) {
      setQuarterDates();
      setCoverage();
      setPreceptingImpact();
    }
  });
  detail.addEventListener("input", updatePreview);
  updatePreview();
  if (isCreateForm) {
    setQuarterDates();
    setCoverage();
    setPreceptingImpact();
    setRequiredParticipantCount();
  }
  return true;
}

function initializeAcademicEventDefinitionForm(form) {
  if (!form) return false;
  const schedulingMode = form.elements.scheduling_mode;
  const expectedQuarter = form.elements.expected_quarter;
  const defaultDuration = form.elements.default_duration_days;
  if (!schedulingMode || !expectedQuarter || !defaultDuration) return false;

  function setSectionDisabled(section, disabled) {
    if (!section) return;
    section.hidden = disabled;
    section.querySelectorAll("input, select, textarea").forEach((control) => {
      control.disabled = disabled;
    });
  }

  function update() {
    const monthly = schedulingMode.value === "monthly_rules";
    expectedQuarter.disabled = monthly;
    expectedQuarter.required = !monthly;
    expectedQuarter.closest("[data-expected-quarter-field]")?.toggleAttribute("data-disabled", monthly);
    const duration = defaultDuration.closest("[data-default-duration-field]");
    defaultDuration.disabled = monthly;
    defaultDuration.required = !monthly;
    duration?.toggleAttribute("data-disabled", monthly);
    setSectionDisabled(form.querySelector("[data-initial-monthly-rule-section]"), !monthly);
  }

  schedulingMode.addEventListener("change", update);
  update();
  initializeAcademicEventFacultySelection(form);
  return true;
}

function initializeAcademicEventAssignmentForm(form) {
  return initializeAcademicEventFacultySelection(form);
}

function initializeAcademicEventForms(root = document) {
  root.querySelectorAll("[data-academic-event-form]").forEach(initializeAcademicEventForm);
  root.querySelectorAll("[data-academic-event-definition-form]").forEach(
    initializeAcademicEventDefinitionForm,
  );
  root.querySelectorAll("[data-academic-event-assignment-form]").forEach(
    initializeAcademicEventAssignmentForm,
  );
}

if (typeof document !== "undefined") {
  initializeAcademicEventForms();
}

if (typeof module !== "undefined") {
  module.exports = {
    addAcademicEventRange,
    initializeAcademicEventFacultySelection,
    initializeAcademicEventDefinitionForm,
    initializeAcademicEventForm,
    initializeAcademicEventForms,
    initializeAcademicEventRanges,
    setAcademicEventRangeDeleted,
  };
}
