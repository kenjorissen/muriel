const assert = require("node:assert/strict");
const test = require("node:test");

const { initializePeriodPicker } = require("../static/schedules/period-picker.js");

class TestInput {
  constructor(name, value, { hasPicker = true, valid = true } = {}) {
    this.name = name;
    this.value = value;
    this.valid = valid;
    this.listeners = new Map();
    this.pickerCalls = 0;
    if (hasPicker) this.showPicker = () => this.pickerCalls++;
  }

  addEventListener(type, listener) {
    this.listeners.set(type, [...(this.listeners.get(type) || []), listener]);
  }

  checkValidity() {
    return this.valid;
  }

  dispatchEvent(event) {
    for (const listener of this.listeners.get(event.type) || []) listener(event);
  }
}

function periodPickerFixture(inputs, { hasButton = true, hasRequestSubmit = true } = {}) {
  const button = { hidden: false };
  const form = {
    submissions: [],
    querySelector: (selector) =>
      selector === "[data-period-picker-submit]" && hasButton ? button : null,
    querySelectorAll: (selector) => (selector === "[data-period-picker-input]" ? inputs : []),
  };
  if (hasRequestSubmit) {
    form.requestSubmit = () => {
      form.submissions.push(Object.fromEntries(inputs.map((input) => [input.name, input.value])));
    };
  }
  return { button, form };
}

test("initializes every marked input and submits the complete range after either changes", () => {
  const start = new TestInput("start_date", "2025-02-03");
  const end = new TestInput("end_date", "2025-02-16");
  const { button, form } = periodPickerFixture([start, end]);

  assert.equal(initializePeriodPicker(form), true);
  assert.equal(button.hidden, true);

  start.dispatchEvent(new Event("click"));
  end.dispatchEvent(new Event("click"));
  assert.equal(start.pickerCalls, 1);
  assert.equal(end.pickerCalls, 1);

  start.value = "2025-02-10";
  start.dispatchEvent(new Event("change"));
  assert.deepEqual(form.submissions, [{ start_date: "2025-02-10", end_date: "2025-02-16" }]);

  end.value = "2025-02-23";
  end.dispatchEvent(new Event("change"));
  assert.deepEqual(form.submissions, [
    { start_date: "2025-02-10", end_date: "2025-02-16" },
    { start_date: "2025-02-10", end_date: "2025-02-23" },
  ]);
});

test("keeps the fallback visible without every required capability", () => {
  const start = new TestInput("start_date", "2025-02-03");
  const unsupportedEnd = new TestInput("end_date", "2025-02-16", { hasPicker: false });
  const { button: unsupportedButton, form: unsupportedForm } = periodPickerFixture([
    start,
    unsupportedEnd,
  ]);

  assert.equal(initializePeriodPicker(unsupportedForm), false);
  assert.equal(unsupportedButton.hidden, false);
  start.dispatchEvent(new Event("click"));
  start.dispatchEvent(new Event("change"));
  assert.equal(start.pickerCalls, 0);
  assert.deepEqual(unsupportedForm.submissions, []);

  const singleDate = new TestInput("date", "2025-02-03");
  const { button: noSubmitButton, form: noSubmitForm } = periodPickerFixture([singleDate], {
    hasRequestSubmit: false,
  });

  assert.equal(initializePeriodPicker(noSubmitForm), false);
  assert.equal(noSubmitButton.hidden, false);
});

test("submits only a changed, nonempty, valid input", () => {
  const date = new TestInput("date", "2025-02-03");
  const { form } = periodPickerFixture([date]);

  initializePeriodPicker(form);
  date.dispatchEvent(new Event("change"));
  date.value = "";
  date.dispatchEvent(new Event("change"));
  date.value = "not-a-date";
  date.valid = false;
  date.dispatchEvent(new Event("change"));
  assert.deepEqual(form.submissions, []);

  date.value = "2025-02-10";
  date.valid = true;
  date.dispatchEvent(new Event("change"));
  assert.deepEqual(form.submissions, [{ date: "2025-02-10" }]);
});
