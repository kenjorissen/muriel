const assert = require("node:assert/strict");
const test = require("node:test");

const { initializePreceptingSolveControls } = require("../static/schedules/precepting-solve-controls.js");

class TestElement {
  constructor(value = "", { checked = false } = {}) {
    this.value = value;
    this.checked = checked;
    this.disabled = false;
    this.hidden = false;
    this.dataset = {};
  }

  setCustomValidity(message) {
    this.validationMessage = message;
  }

  focus() {
    this.focused = true;
  }
}

function solveControlsFixture({ today = "" } = {}) {
  const listeners = new Map();
  const rangeKind = new TestElement("custom", { checked: true });
  const startDate = new TestElement("2027-02-01");
  const endDate = new TestElement("2027-02-05");
  const balanceSpan = new TestElement("6-months");
  const balanceCustomDays = new TestElement("");
  const effectiveRange = new TestElement("");
  const balanceRange = new TestElement("");
  const customDays = new TestElement("");
  const form = {
    dataset: { solveToday: today },
    elements: {
      balance_custom_days: balanceCustomDays,
      balance_span: balanceSpan,
      end_date: endDate,
      start_date: startDate,
    },
    addEventListener(type, listener) {
      listeners.set(type, [...(listeners.get(type) || []), listener]);
    },
    checkValidity() {
      return true;
    },
    dispatch(type) {
      for (const listener of listeners.get(type) || []) listener(new Event(type));
    },
    querySelector(selector) {
      return {
        "[data-range-controls-effective-range]": effectiveRange,
        "[data-solve-balance-range]": balanceRange,
        "[data-solve-custom-days]": customDays,
      }[selector] || null;
    },
    querySelectorAll(selector) {
      if (selector === "input[name='range_kind']") return [rangeKind];
      return [];
    },
  };

  return {
    effectiveRange,
    form,
    rangeKind,
    startDate,
  };
}

test("keeps the effective range clipped at the organization today during control updates", () => {
  const originalWindow = global.window;
  global.window = {
    addEventListener() {},
    clearInterval() {},
    setInterval() {},
  };

  try {
    const { effectiveRange, form, startDate } = solveControlsFixture({ today: "2027-02-03" });
    initializePreceptingSolveControls(form);

    assert.equal(effectiveRange.value, "Feb 3, 2027 through Feb 5, 2027");
    form.dispatch("change");
    assert.equal(effectiveRange.value, "Feb 3, 2027 through Feb 5, 2027");
    startDate.value = "2027-02-04";
    form.dispatch("input");
    assert.equal(effectiveRange.value, "Feb 4, 2027 through Feb 5, 2027");
  } finally {
    global.window = originalWindow;
  }
});
