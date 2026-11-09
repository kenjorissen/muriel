const assert = require("node:assert/strict");
const test = require("node:test");

const { initializeCalendarWeekActions } = require("../static/schedules/calendar-week-actions.js");

class TestElement {
  constructor(parentElement = null) {
    this.dataset = {};
    this.listeners = new Map();
    this.parentElement = parentElement;
  }

  addEventListener(type, listener) {
    this.listeners.set(type, [...(this.listeners.get(type) || []), listener]);
  }

  click(event) {
    for (const listener of this.listeners.get("click") || []) listener(event);
  }

  contains(element) {
    for (let candidate = element; candidate; candidate = candidate.parentElement) {
      if (candidate === this) return true;
    }
    return false;
  }

  closest() {
    return null;
  }
}

function fixture() {
  const week = new TestElement();
  week.dataset.calendarWeekAction = "preceptor-week-2026-08-03";
  const openLink = new TestElement();
  const root = {
    querySelectorAll: (selector) => (selector === "[data-calendar-week-action]" ? [week] : []),
  };
  const originalDocument = global.document;
  const originalWindow = global.window;
  global.document = {
    getElementById: (id) => (id === "preceptor-week-2026-08-03" ? openLink : null),
  };
  global.window = { getSelection: () => ({ toString: () => "" }) };
  return {
    openLink,
    week,
    root,
    restore: () => {
      global.document = originalDocument;
      global.window = originalWindow;
    },
  };
}

test("a calendar week background opens its ordinary week link", () => {
  const { openLink, restore, root, week } = fixture();
  let followed = 0;
  openLink.click = () => {
    followed += 1;
  };
  initializeCalendarWeekActions(root);
  week.click({ button: 0, detail: 1, target: week });

  assert.equal(followed, 1);
  restore();
});

test("calendar week clicks preserve interactive descendants, selection, and keyboard links", () => {
  const { openLink, restore, root, week } = fixture();
  let followed = 0;
  openLink.click = () => {
    followed += 1;
  };
  initializeCalendarWeekActions(root);

  const status = new TestElement(week);
  week.click({ button: 0, detail: 1, target: { closest: () => status } });
  week.click({ button: 0, detail: 0, target: week });
  week.click({ button: 0, ctrlKey: true, detail: 1, target: week });
  week.click({ button: 0, shiftKey: true, detail: 1, target: week });
  global.window.getSelection = () => ({ toString: () => "Selected assignment" });
  week.closest = () => null;
  week.click({ button: 0, detail: 1, target: week });

  assert.equal(followed, 0);
  restore();
});
