const assert = require("node:assert/strict");
const test = require("node:test");

const { initializeMonthlyAnnualWindow } = require("../static/schedules/monthly-annual-window.js");

test("annual window mode disables all-year months and requires a complete selected window", () => {
  function radio(value, checked) {
    const listeners = new Map();
    return {
      checked,
      value,
      addEventListener(type, listener) { listeners.set(type, listener); },
      select() {
        modeInputs.forEach((input) => { input.checked = input === this; });
        listeners.get("change")();
      },
    };
  }

  const allYear = radio("all_year", true);
  const selectedMonths = radio("window", false);
  const modeInputs = [allYear, selectedMonths];
  const attributes = new Map();
  const months = {
    toggleAttribute(name, enabled) { if (enabled) attributes.set(name, ""); else attributes.delete(name); },
    querySelectorAll(selector) { return selector === "select" ? monthInputs : []; },
  };
  function monthInput(value) {
    return {
      value,
      validationMessage: "",
      setCustomValidity(message) { this.validationMessage = message; },
    };
  }
  const monthInputs = [
    monthInput(""),
    monthInput("5"),
  ];
  const section = {
    querySelector(selector) {
      return selector === "[data-monthly-annual-months]" ? months : null;
    },
    querySelectorAll(selector) {
      return selector === "input[name='annual_window']" ? modeInputs : [];
    },
  };
  const submitListeners = new Map();
  const form = {
    querySelector(selector) { return selector === "[data-monthly-annual-window]" ? section : null; },
    addEventListener(type, listener) { submitListeners.set(type, listener); },
  };

  assert.equal(initializeMonthlyAnnualWindow(form), true);
  assert.equal(attributes.has("data-disabled"), true);
  assert.equal(monthInputs.every((input) => input.disabled && !input.required), true);

  selectedMonths.select();
  assert.equal(attributes.has("data-disabled"), false);
  assert.equal(monthInputs.every((input) => !input.disabled && input.required), true);
  submitListeners.get("submit")();
  assert.notEqual(monthInputs[0].validationMessage, "");
  assert.equal(monthInputs[1].validationMessage, "");

  allYear.select();
  assert.equal(monthInputs[0].validationMessage, "");
});
