const assert = require("node:assert/strict");
const test = require("node:test");

const {
  addAcademicEventRange,
  initializeAcademicEventDefinitionForm,
  initializeAcademicEventFacultySelection,
  setAcademicEventRangeDeleted,
} = require("../static/schedules/academic-event-form.js");

test("adding an academic event date range creates the next blank form", () => {
  const start = {
    name: "additional_ranges-__prefix__-start_date",
    id: "id_additional_ranges-__prefix__-start_date",
    value: "2026-08-10",
    checked: true,
  };
  const end = {
    name: "additional_ranges-__prefix__-end_date",
    id: "id_additional_ranges-__prefix__-end_date",
    value: "2026-08-11",
    checked: true,
  };
  const remove = {
    name: "additional_ranges-__prefix__-DELETE",
    id: "id_additional_ranges-__prefix__-DELETE",
    value: "on",
    checked: true,
  };
  const startLabel = { htmlFor: start.id };
  const endLabel = { htmlFor: end.id };
  const removeLabel = { htmlFor: remove.id };
  const range = {
    querySelectorAll(selector) {
      return selector === "input" ? [start, end, remove] : [startLabel, endLabel, removeLabel];
    },
  };
  const rangeTemplate = { content: { cloneNode: () => range } };
  const rangeContainer = { appended: [], append(item) { this.appended.push(item); } };
  const rangeTotal = {
    id: "id_additional_ranges-TOTAL_FORMS",
    name: "additional_ranges-TOTAL_FORMS",
    value: "0",
  };

  assert.equal(addAcademicEventRange(rangeTemplate, rangeContainer, rangeTotal), true);
  assert.deepEqual(rangeContainer.appended, [range]);
  assert.equal(rangeTotal.value, "1");
  for (const input of [start, end, remove]) {
    assert.match(input.name, /^additional_ranges-0-/);
    assert.match(input.id, /^id_additional_ranges-0-/);
    assert.equal(input.value, "");
    assert.equal(input.checked, false);
  }
  for (const label of [startLabel, endLabel, removeLabel]) {
    assert.match(label.htmlFor, /^id_additional_ranges-0-/);
  }
});

test("removing and restoring a date range retains its form values", () => {
  const deletion = { checked: false, name: "additional_ranges-3-DELETE" };
  const startDate = { name: "additional_ranges-3-start_date", value: "2026-08-14" };
  const endDate = { name: "additional_ranges-3-end_date", value: "2026-08-15" };
  const undo = { hidden: true };
  const entry = {
    querySelector(selector) {
      return selector === "[data-academic-event-range-undo]" ? undo : null;
    },
  };
  const range = {
    hidden: false,
    closest(selector) {
      return selector === "[data-academic-event-range-entry]" ? entry : null;
    },
    querySelector(selector) {
      return selector === 'input[name$="-DELETE"]' ? deletion : null;
    },
    querySelectorAll(selector) {
      return selector === "input" ? [startDate, endDate, deletion] : [];
    },
  };

  assert.equal(setAcademicEventRangeDeleted(range, true), true);
  assert.equal(deletion.checked, true);
  assert.equal(range.hidden, true);
  assert.equal(undo.hidden, false);
  assert.equal(startDate.value, "2026-08-14");
  assert.equal(endDate.value, "2026-08-15");

  assert.equal(setAcademicEventRangeDeleted(range, false), true);
  assert.equal(deletion.checked, false);
  assert.equal(range.hidden, false);
  assert.equal(undo.hidden, true);
  assert.equal(startDate.value, "2026-08-14");
  assert.equal(endDate.value, "2026-08-15");
});

test("all available Faculty mutes individual controls without clearing selections", () => {
  const listeners = new Map();
  const allAvailableFaculty = {
    checked: true,
    addEventListener(type, listener) {
      listeners.set(type, listener);
    },
  };
  const filter = {};
  const choices = [{ checked: true }, { checked: false }];
  const allAvailable = { hidden: true };
  const tokens = { hidden: false };
  const empty = { hidden: false };
  const instruction = { hidden: false };
  const attributes = new Map();
  const picker = {
    toggleAttribute(name, enabled) {
      if (enabled) attributes.set(name, "");
      else attributes.delete(name);
    },
    querySelector(selector) {
      return {
        "[data-physician-picker-filter]": filter,
        "[data-physician-picker-all-available]": allAvailable,
        "[data-physician-picker-tokens]": tokens,
        "[data-physician-picker-empty]": empty,
        "[data-physician-picker-selected-instruction]": instruction,
      }[selector] || null;
    },
    querySelectorAll(selector) {
      return selector === 'input[type="checkbox"]' ? choices : [];
    },
  };
  const form = {
    elements: { all_available_faculty: allAvailableFaculty },
    querySelector(selector) {
      return selector === "[data-physician-picker]" ? picker : null;
    },
  };

  assert.equal(initializeAcademicEventFacultySelection(form), true);
  assert.equal(filter.disabled, true);
  assert.equal(choices.every((choice) => choice.disabled), true);
  assert.equal(allAvailable.hidden, false);
  assert.equal(tokens.hidden, true);
  assert.equal(attributes.has("data-all-available-faculty"), true);

  allAvailableFaculty.checked = false;
  listeners.get("change")();
  assert.equal(filter.disabled, false);
  assert.equal(choices.every((choice) => !choice.disabled), true);
  assert.equal(choices[0].checked, true);
  assert.equal(allAvailable.hidden, true);
  assert.equal(tokens.hidden, false);
  assert.equal(empty.hidden, true);
});

test("definition mode disables monthly quarter submission and restores yearly requirement", () => {
  const listeners = new Map();
  const schedulingMode = {
    value: "monthly_rules",
    addEventListener(type, listener) { listeners.set(type, listener); },
  };
  const attributes = new Map();
  const expectedQuarter = {
    closest(selector) {
      return selector === "[data-expected-quarter-field]" ? {
        toggleAttribute(name, enabled) {
          if (enabled) attributes.set(name, "");
          else attributes.delete(name);
        },
      } : null;
    },
  };
  const durationAttributes = new Map();
  const defaultDuration = {
    closest(selector) {
      return selector === "[data-default-duration-field]" ? {
        toggleAttribute(name, enabled) {
          if (enabled) durationAttributes.set(name, "");
          else durationAttributes.delete(name);
        },
      } : null;
    },
  };
  const initialRuleControl = {};
  const initialRule = {
    hidden: true,
    querySelectorAll(selector) {
      return selector === "input, select, textarea" ? [initialRuleControl] : [];
    },
  };
  const allAvailableFaculty = { checked: false, addEventListener() {} };
  const picker = {
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
  const form = {
    elements: {
      scheduling_mode: schedulingMode,
      expected_quarter: expectedQuarter,
      default_duration_days: defaultDuration,
      all_available_faculty: allAvailableFaculty,
    },
    querySelector(selector) {
      if (selector === "[data-physician-picker]") return picker;
      return selector === "[data-initial-monthly-rule-section]" ? initialRule : null;
    },
  };

  assert.equal(initializeAcademicEventDefinitionForm(form), true);
  assert.equal(expectedQuarter.disabled, true);
  assert.equal(expectedQuarter.required, false);
  assert.equal(attributes.has("data-disabled"), true);
  assert.equal(defaultDuration.disabled, true);
  assert.equal(defaultDuration.required, false);
  assert.equal(durationAttributes.has("data-disabled"), true);
  assert.equal(initialRule.hidden, false);
  assert.equal(initialRuleControl.disabled, false);

  schedulingMode.value = "yearly";
  listeners.get("change")();
  assert.equal(expectedQuarter.disabled, false);
  assert.equal(expectedQuarter.required, true);
  assert.equal(attributes.has("data-disabled"), false);
  assert.equal(defaultDuration.disabled, false);
  assert.equal(defaultDuration.required, true);
  assert.equal(durationAttributes.has("data-disabled"), false);
  assert.equal(initialRule.hidden, true);
  assert.equal(initialRuleControl.disabled, true);
});
