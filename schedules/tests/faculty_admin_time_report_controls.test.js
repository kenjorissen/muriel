const assert = require("node:assert/strict");
const test = require("node:test");

const {
  initializeFacultyAdminTimeReportControls,
} = require("../static/schedules/faculty-admin-time-report-controls.js");

test("submits a coverage change once", () => {
  const listeners = new Map();
  let submitCount = 0;
  const form = {
    addEventListener(type, listener) {
      listeners.set(type, listener);
    },
    requestSubmit() {
      submitCount += 1;
    },
  };
  const root = { querySelector: () => form };

  assert.equal(initializeFacultyAdminTimeReportControls(root), true);
  listeners.get("change")({ target: { name: "coverage", checked: true } });
  listeners.get("change")({ target: { name: "coverage", checked: false } });
  listeners.get("change")({ target: { name: "month", checked: true } });

  assert.equal(submitCount, 1);
});
