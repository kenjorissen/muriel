const assert = require("node:assert/strict");
const test = require("node:test");

const { initializeFormProcessingFeedback } = require("../static/schedules/form-processing-feedback.js");

class TestElement {
  constructor(value = "") {
    this.value = value;
    this.disabled = false;
    this.hidden = false;
    this.name = "";
  }

  focus() {
    this.focused = true;
  }
}

function fixture({ valid = true } = {}) {
  const listeners = new Map();
  const attributes = new Map();
  const feedback = new TestElement();
  feedback.hidden = true;
  feedback.querySelector = () => null;
  const previewButton = new TestElement("preview");
  previewButton.name = "action";
  const applyButton = new TestElement("apply");
  applyButton.name = "action";
  const controls = [previewButton, applyButton];
  const form = {
    addEventListener(type, listener) {
      listeners.set(type, [...(listeners.get(type) || []), listener]);
    },
    checkValidity() {
      return valid;
    },
    dispatch(type, event = {}) {
      for (const listener of listeners.get(type) || []) listener(event);
    },
    querySelector(selector) {
      return selector === "[data-processing-feedback]" ? feedback : null;
    },
    querySelectorAll(selector) {
      return selector === 'button[type="submit"]' ? [previewButton, applyButton] : [];
    },
    ownerDocument: {
      createElement() {
        return new TestElement();
      },
    },
    append(control) {
      controls.push(control);
      control.remove = () => controls.splice(controls.indexOf(control), 1);
    },
    removeAttribute(name) {
      attributes.delete(name);
    },
    setAttribute(name, value) {
      attributes.set(name, value);
    },
  };
  return {
    applyButton,
    attributes,
    feedback,
    form,
    previewButton,
    serializedValues() {
      return Object.fromEntries(
        controls.filter((control) => control.name && !control.disabled).map((control) => [control.name, control.value])
      );
    },
  };
}

test("shows processing feedback only for valid submissions and resets it on page restore", () => {
  const pageListeners = new Map();
  const hadWindow = Object.hasOwn(global, "window");
  const originalWindow = global.window;
  global.window = {
    addEventListener(type, listener) {
      pageListeners.set(type, [...(pageListeners.get(type) || []), listener]);
    },
    clearInterval() {},
  };

  try {
    const invalid = fixture({ valid: false });
    initializeFormProcessingFeedback(invalid.form);
    invalid.form.dispatch("submit");
    assert.equal(invalid.feedback.hidden, true);
    assert.equal(invalid.previewButton.disabled, false);

    const { attributes, feedback, form, previewButton } = fixture();
    initializeFormProcessingFeedback(form);
    form.dispatch("submit");
    form.dispatch("submit");
    assert.equal(feedback.hidden, false);
    assert.equal(feedback.focused, true);
    assert.equal(previewButton.disabled, true);
    assert.equal(attributes.get("aria-busy"), "true");

    for (const listener of pageListeners.get("pageshow")) listener(new Event("pageshow"));
    assert.equal(feedback.hidden, true);
    assert.equal(previewButton.disabled, false);
    assert.equal(attributes.has("aria-busy"), false);
  } finally {
    if (hadWindow) global.window = originalWindow;
    else delete global.window;
  }
});

test("preserves the selected Preview or Apply action while disabling import controls", () => {
  const hadWindow = Object.hasOwn(global, "window");
  const originalWindow = global.window;
  global.window = { addEventListener() {}, clearInterval() {} };

  try {
    for (const [buttonName, action] of [
      ["previewButton", "preview"],
      ["applyButton", "apply"],
    ]) {
      const submitted = fixture();
      initializeFormProcessingFeedback(submitted.form);
      submitted.form.dispatch("submit", { submitter: submitted[buttonName] });

      assert.deepEqual(submitted.serializedValues(), { action });
      assert.equal(submitted.previewButton.disabled, true);
      assert.equal(submitted.applyButton.disabled, true);
    }
  } finally {
    if (hadWindow) global.window = originalWindow;
    else delete global.window;
  }
});
