const assert = require("node:assert/strict");
const test = require("node:test");

const { initializeCalendarDayActions } = require("../static/schedules/calendar-day-actions.js");

class TestElement {
  constructor(date, parentElement = null) {
    this.dataset = { calendarDay: date };
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
  const background = new TestElement();
  background.role = "presentation";
  const body = new TestElement(undefined, background);
  body.dataset.calendarDayAction = "interview-add-2026-08-12";
  const header = new TestElement();
  header.dataset.calendarDayAction = "interview-add-2026-08-12";
  const nextHeader = new TestElement();
  nextHeader.dataset.calendarDayAction = "interview-add-2026-08-13";
  const addLink = new TestElement();
  const nextAddLink = new TestElement();
  const root = {
    querySelectorAll: (selector) =>
      selector === "[data-calendar-day-action]" ? [header, body, nextHeader] : [],
  };
  const originalDocument = global.document;
  const originalWindow = global.window;
  global.document = {
    getElementById: (id) =>
      ({
        "interview-add-2026-08-12": addLink,
        "interview-add-2026-08-13": nextAddLink,
      })[id] || null,
  };
  global.window = { getSelection: () => ({ toString: () => "" }) };
  return {
    addLink,
    background,
    body,
    header,
    nextAddLink,
    nextHeader,
    restore: () => {
      global.document = originalDocument;
      global.window = originalWindow;
    },
    root,
  };
}

test("calendar header and body whitespace follow one date action", () => {
  const { addLink, background, body, header, nextAddLink, nextHeader, restore, root } = fixture();
  let followed = 0;
  let nextFollowed = 0;
  addLink.click = () => {
    followed += 1;
  };
  nextAddLink.click = () => {
    nextFollowed += 1;
  };
  body.closest = () => background;

  initializeCalendarDayActions(root);
  header.closest = () => header;
  header.click({ button: 0, detail: 1, target: header });
  body.click({ button: 0, detail: 1, target: body });
  nextHeader.click({ button: 0, detail: 1, target: nextHeader });

  assert.equal(followed, 2);
  assert.equal(nextFollowed, 1);
  restore();
});

test("calendar day actions protect interactive children, selection, modified clicks, and keyboard links", () => {
  const { addLink, body, header, restore, root } = fixture();
  const interactiveContent = new TestElement(undefined, body);
  let followed = 0;
  addLink.click = () => {
    followed += 1;
  };

  initializeCalendarDayActions(root);
  body.click({ button: 0, detail: 1, target: { closest: () => interactiveContent } });
  header.click({ button: 0, detail: 0, target: header });
  header.click({ button: 0, ctrlKey: true, detail: 1, target: header });
  global.window.getSelection = () => ({ toString: () => "Selected date" });
  header.click({ button: 0, detail: 1, target: header });

  assert.equal(followed, 0);
  restore();
});
