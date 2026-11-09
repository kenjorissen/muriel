const assert = require("node:assert/strict");
const test = require("node:test");

const { initializePhysicianPicker } = require("../static/schedules/physicians-picker.js");

class TestElement {
  constructor(ownerDocument, textContent = "") {
    this.ownerDocument = ownerDocument;
    this.textContent = textContent;
    this.children = [];
    this.attributes = new Map();
    this.listeners = new Map();
    this.hidden = false;
    this.checked = false;
    this.value = "";
  }

  addEventListener(type, listener) {
    this.listeners.set(type, [...(this.listeners.get(type) || []), listener]);
  }

  append(...children) {
    this.children.push(...children);
  }

  dispatchEvent(event) {
    for (const listener of this.listeners.get(event.type) || []) listener(event);
    return true;
  }

  focus() {
    this.focused = true;
  }

  getAttribute(name) {
    return this.attributes.get(name) || null;
  }

  replaceChildren() {
    this.children = [];
  }

  setAttribute(name, value) {
    this.attributes.set(name, value);
  }
}

class TestDocument {
  constructor() {
    this.elementsById = new Map();
  }

  createElement(tagName) {
    const element = new TestElement(this);
    element.tagName = tagName;
    return element;
  }

  getElementById(id) {
    return this.elementsById.get(id) || null;
  }
}

function pickerFixture(selectedNames = [], selectionInstructions = null) {
  const document = new TestDocument();
  const picker = new TestElement(document);
  const filter = new TestElement(document);
  const filterArea = new TestElement(document);
  const selectedColumn = new TestElement(document);
  const selectedArea = new TestElement(document);
  const tokens = new TestElement(document);
  const empty = new TestElement(document);
  const choicesList = new TestElement(document);
  const count = new TestElement(document);
  const choices = ["Avery Participant", "Blair Participant", "Casey Participant"].map((name) => {
    const element = new TestElement(document);
    const input = new TestElement(document);
    const label = new TestElement(document, name);
    input.checked = selectedNames.includes(name);
    element.querySelector = (selector) => {
      if (selector === 'input[type="checkbox"]') return input;
      if (selector === "label") return label;
      return null;
    };
    return { element, input, label, name };
  });

  choicesList.setAttribute("aria-describedby", "participant-count");
  if (selectionInstructions) {
    count.setAttribute("data-physician-picker-selection-instructions", selectionInstructions);
  }
  document.elementsById.set("participant-count", count);
  picker.querySelector = (selector) => {
    const elements = {
      "[data-physician-picker-filter]": filter,
      "[data-physician-picker-filter-area]": filterArea,
      "[data-physician-picker-selected-column]": selectedColumn,
      "[data-physician-picker-selected]": selectedArea,
      "[data-physician-picker-tokens]": tokens,
      "[data-physician-picker-empty]": empty,
      "[data-physician-picker-choices]": choicesList,
    };
    return elements[selector] || null;
  };
  picker.querySelectorAll = (selector) =>
    selector === "[data-physician-picker-choice]" ? choices.map((choice) => choice.element) : [];

  return { choices, count, empty, filter, filterArea, picker, selectedArea, selectedColumn, tokens };
}

function descendants(element) {
  return element.children.flatMap((child) => [child, ...descendants(child)]);
}

function removeButtons(tokens) {
  return descendants(tokens).filter((element) => element.tagName === "button");
}

function exposedText(element) {
  if (element.getAttribute("aria-hidden") === "true") return "";
  return element.textContent + element.children.map(exposedText).join("");
}

function tokenNames(tokens) {
  return removeButtons(tokens).map(exposedText);
}

function removal(tokens, accessibleName) {
  const matches = removeButtons(tokens).filter(
    (button) => button.getAttribute("aria-label") === accessibleName,
  );
  assert.equal(matches.length, 1);
  return matches[0];
}

function assertSelectedCount(countText, amount, noun) {
  const number = `(?<!\\d)${amount}(?!\\d)`;
  const selected = "\\bselected\\b";
  const expectedNoun = `\\b${noun}\\b`;
  assert.match(
    countText,
    new RegExp(
      `(?:${number}\\W+${selected}\\W+${expectedNoun}|${number}\\W+${expectedNoun}\\W+${selected}|${expectedNoun}\\W+${selected}\\W+${number}|${selected}\\W+${expectedNoun}\\W+${number})`,
      "i",
    ),
  );
}

test("initializes from server-rendered selected choices", () => {
  const fixture = pickerFixture(["Avery Participant"]);

  assert.equal(initializePhysicianPicker(fixture.picker), true);
  assert.deepEqual(tokenNames(fixture.tokens), ["Avery Participant"]);
  assertSelectedCount(fixture.count.textContent, 1, "physician");
  assert.equal(fixture.empty.hidden, true);
  assert.equal(fixture.filterArea.hidden, false);
  assert.equal(fixture.selectedColumn.hidden, false);
  const remove = removal(fixture.tokens, "Remove Avery Participant");
  // TEXT-CONTRACT: physician-picker-remove-name — docs/design.md physician picker.
  // Assistive users identify removal by capitalized Remove, one space, and the full name.
  // Paired with schedules/static/schedules/physicians-picker.js.
  assert.equal(remove.getAttribute("aria-label"), "Remove Avery Participant");
});

test("filters native choices case-insensitively and restores them", () => {
  const fixture = pickerFixture(["Blair Participant"]);
  initializePhysicianPicker(fixture.picker);

  fixture.filter.value = "aVeRy";
  fixture.filter.dispatchEvent(new Event("input"));
  assert.equal(fixture.choices[0].element.hidden, false);
  assert.equal(fixture.choices[1].element.hidden, true);
  assert.equal(fixture.choices[2].element.hidden, true);
  assert.equal(fixture.choices[1].input.checked, true);

  fixture.filter.value = "";
  fixture.filter.dispatchEvent(new Event("input"));
  assert.equal(fixture.choices.every((choice) => !choice.element.hidden), true);
});

test("native choice changes synchronize tokens and natural selected counts", () => {
  const fixture = pickerFixture();
  initializePhysicianPicker(fixture.picker);

  assert.deepEqual(tokenNames(fixture.tokens), []);
  assert.equal(fixture.empty.hidden, false);
  assertSelectedCount(fixture.count.textContent, 0, "physicians");

  fixture.choices[0].input.checked = true;
  fixture.choices[0].input.dispatchEvent(new Event("change"));
  assert.deepEqual(tokenNames(fixture.tokens), ["Avery Participant"]);
  assertSelectedCount(fixture.count.textContent, 1, "physician");

  fixture.choices[0].input.dispatchEvent(new Event("change"));
  assert.deepEqual(tokenNames(fixture.tokens), ["Avery Participant"]);

  fixture.choices[1].input.checked = true;
  fixture.choices[1].input.dispatchEvent(new Event("change"));
  assert.deepEqual(tokenNames(fixture.tokens), ["Avery Participant", "Blair Participant"]);
  assertSelectedCount(fixture.count.textContent, 2, "physicians");
});

test("uses the picker-specific selected-count instructions", () => {
  const fixture = pickerFixture([], "Choose the physicians who may use this schedule option.");
  initializePhysicianPicker(fixture.picker);

  fixture.choices[0].input.checked = true;
  fixture.choices[0].input.dispatchEvent(new Event("change"));

  assertSelectedCount(fixture.count.textContent, 1, "physician");
  assert.equal(fixture.count.textContent.includes("Choose the physicians who may use this schedule option."), true);
});

test("removing a token clears only its native choice and keeps remaining chips synchronized", () => {
  const fixture = pickerFixture(["Avery Participant", "Blair Participant"]);
  initializePhysicianPicker(fixture.picker);
  assertSelectedCount(fixture.count.textContent, 2, "physicians");

  const remove = removal(fixture.tokens, "Remove Blair Participant");
  assert.equal(remove.getAttribute("aria-label"), "Remove Blair Participant");
  remove.dispatchEvent(new Event("click"));

  assert.equal(fixture.choices[0].input.checked, true);
  assert.equal(fixture.choices[1].input.checked, false);
  assert.deepEqual(tokenNames(fixture.tokens), ["Avery Participant"]);
  assert.equal(fixture.empty.hidden, true);
  assertSelectedCount(fixture.count.textContent, 1, "physician");
  assert.equal(fixture.filter.focused, true);

  removal(fixture.tokens, "Remove Avery Participant").dispatchEvent(new Event("click"));

  assert.equal(fixture.choices[0].input.checked, false);
  assert.deepEqual(tokenNames(fixture.tokens), []);
  assert.equal(fixture.empty.hidden, false);
  assertSelectedCount(fixture.count.textContent, 0, "physicians");
});
