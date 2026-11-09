const assert = require("node:assert/strict");
const test = require("node:test");

const {
  initializeScheduleReportControls,
} = require("../static/schedules/schedule-report-controls.js");

class TestElement {
  constructor(value, { checked = false, name = "" } = {}) {
    this.value = value;
    this.checked = checked;
    this.name = name;
    this.disabled = false;
    this.hidden = false;
    this.dataset = {};
  }

  setCustomValidity(message) {
    this.validationMessage = message;
  }
}

function makeRadioGroup(inputs) {
  const states = new Map(inputs.map((input) => [input, input.checked]));
  for (const input of inputs) {
    Object.defineProperty(input, "checked", {
      configurable: true,
      get: () => states.get(input),
      set: (checked) => {
        if (checked) {
          inputs.forEach((choice) => states.set(choice, choice === input));
        } else {
          states.set(input, false);
        }
      },
    });
  }
}

function scheduleReportFixture({ scheduleValue = "precepting" } = {}) {
  const schedule = new TestElement(scheduleValue, { name: "schedule" });
  const academicPeriod = new TestElement("academic-year", { name: "period" });
  const calendarPeriod = new TestElement("calendar-year", { name: "period" });
  const customPeriod = new TestElement("custom", { checked: true, name: "period" });
  const history = new TestElement("history", { checked: true, name: "coverage" });
  const combined = new TestElement("history-and-scheduled", { name: "coverage" });
  const scheduled = new TestElement("scheduled", { name: "coverage" });
  const periodInputs = [academicPeriod, calendarPeriod, customPeriod];
  const coverageInputs = [history, combined, scheduled];
  makeRadioGroup(periodInputs);
  makeRadioGroup(coverageInputs);
  const startDate = new TestElement("2025-02-02", { name: "start_date" });
  const endDate = new TestElement("2025-02-02", { name: "end_date" });
  const academicYear = new TestElement("2024", { name: "academic_year" });
  const calendarYear = new TestElement("2025", { name: "calendar_year" });
  const coverageMessage = new TestElement("");
  const effectiveRange = new TestElement("");
  const includedDates = new TestElement("");
  const includedDatesValue = new TestElement("");
  const periodValues = ["academic-year", "calendar-year", "custom"].map((value) => {
    const element = new TestElement("");
    element.dataset.scheduleReportPeriodValue = value;
    return element;
  });
  const coverageChoices = new Map(
    [history, combined, scheduled].map((input) => [
      `[data-schedule-report-coverage-choice="${input.value}"]`,
      new TestElement(""),
    ]),
  );
  const listeners = new Map();
  let submitCount = 0;
  const form = {
    dataset: { scheduleReportToday: "2025-02-03" },
    elements: {
      schedule,
      academic_year: academicYear,
      calendar_year: calendarYear,
      start_date: startDate,
      end_date: endDate,
    },
    addEventListener(type, listener) {
      listeners.set(type, [...(listeners.get(type) || []), listener]);
    },
    checkValidity() {
      return Boolean(schedule.value) && !startDate.validationMessage && !endDate.validationMessage;
    },
    reportValidity() {
      const invalid = [startDate, endDate].find((input) => input.validationMessage);
      if (invalid) invalid.reported = true;
      return this.checkValidity();
    },
    requestSubmit() {
      submitCount += 1;
    },
    dispatchChange(target) {
      for (const listener of listeners.get("change") || []) listener({ target });
    },
    selectRadio(input) {
      input.checked = true;
      this.dispatchChange(input);
    },
    querySelector(selector) {
      return {
        "[data-schedule-report-coverage-message]": coverageMessage,
        "[data-range-controls-effective-range]": effectiveRange,
        "[data-schedule-report-included-dates]": includedDates,
        "[data-schedule-report-included-dates-value]": includedDatesValue,
        ...Object.fromEntries(coverageChoices),
      }[selector] || null;
    },
    querySelectorAll(selector) {
      if (selector === "input[name='period']") {
        return periodInputs;
      }
      if (selector === "input[name='coverage']") return coverageInputs;
      if (selector === "[data-range-control-value], [data-schedule-report-period-value]") return periodValues;
      return [];
    },
  };

  return {
    academicPeriod,
    academicYear,
    calendarPeriod,
    calendarYear,
    combined,
    customPeriod,
    endDate,
    form,
    history,
    scheduled,
    schedule,
    startDate,
    submitCount: () => submitCount,
  };
}

test("selects combined coverage after a range change begins spanning history and scheduled dates", () => {
  const { combined, endDate, form, history, scheduled, submitCount } = scheduleReportFixture();

  assert.equal(initializeScheduleReportControls(form), true);
  assert.equal(history.checked, true);
  assert.equal(combined.checked, false);

  endDate.value = "2025-02-03";
  form.dispatchChange(endDate);
  assert.equal(combined.checked, true);
  assert.equal(submitCount(), 1);

  form.selectRadio(history);
  assert.equal(history.checked, true);
  assert.equal(combined.checked, false);
  assert.equal(scheduled.checked, false);
  assert.equal(submitCount(), 2);
});

test("submits each valid report control change once", () => {
  const {
    academicPeriod,
    academicYear,
    calendarPeriod,
    calendarYear,
    combined,
    customPeriod,
    form,
    history,
    submitCount,
    schedule,
  } = scheduleReportFixture();

  initializeScheduleReportControls(form);
  schedule.value = "vacation";
  form.dispatchChange(schedule);
  assert.equal(submitCount(), 1);

  academicPeriod.checked = true;
  academicYear.value = "2025";
  form.dispatchChange(academicYear);
  assert.equal(submitCount(), 2);

  calendarPeriod.checked = true;
  calendarYear.value = "2025";
  form.dispatchChange(calendarPeriod);
  assert.equal(submitCount(), 3);

  customPeriod.checked = true;
  form.dispatchChange(customPeriod);
  assert.equal(submitCount(), 4);

  form.selectRadio(combined);
  assert.equal(submitCount(), 5);
});

test("does not submit report controls until a real schedule is selected", () => {
  const { form, schedule, submitCount } = scheduleReportFixture({ scheduleValue: "" });

  initializeScheduleReportControls(form);
  form.dispatchChange(schedule);
  assert.equal(submitCount(), 0);

  schedule.value = "precepting";
  form.dispatchChange(schedule);
  assert.equal(submitCount(), 1);
});

test("does not submit incomplete or invalid custom ranges and reports feedback on the invalid field", () => {
  const { endDate, form, startDate, submitCount } = scheduleReportFixture();

  initializeScheduleReportControls(form);
  endDate.value = "";
  form.dispatchChange(endDate);
  assert.equal(submitCount(), 0);
  assert.notEqual(endDate.validationMessage, "");
  assert.equal(startDate.validationMessage, "");
  assert.equal(endDate.reported, true);

  endDate.value = "2025-02-01";
  startDate.value = "2025-02-02";
  form.dispatchChange(startDate);
  assert.equal(submitCount(), 0);
  assert.notEqual(startDate.validationMessage, "");
  assert.notEqual(endDate.validationMessage, "");
  assert.equal(startDate.reported, true);
});
