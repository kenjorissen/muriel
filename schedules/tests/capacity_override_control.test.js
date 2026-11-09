const assert = require("node:assert/strict");
const test = require("node:test");

const { initializeCapacityOverrideControl } = require("../static/schedules/capacity-override-control.js");

test("capacity override checkbox updates the entry after changes and browser restoration", () => {
  const listeners = new Map();
  const hadWindow = Object.hasOwn(global, "window");
  const originalWindow = global.window;
  global.window = {
    addEventListener(type, listener) {
      listeners.set(type, listener);
    },
  };
  try {
    const useOverride = {
      checked: false,
      addEventListener(type, listener) {
        listeners.set(type, listener);
      },
    };
    const capacityOverride = { disabled: false };
    const control = {
      querySelector(selector) {
        return {
          'input[name="use_override"]': useOverride,
          'input[name="capacity_override"]': capacityOverride,
        }[selector] || null;
      },
    };

    assert.equal(initializeCapacityOverrideControl(control), true);
    assert.equal(capacityOverride.disabled, true);

    useOverride.checked = true;
    listeners.get("change")();
    assert.equal(capacityOverride.disabled, false);

    useOverride.checked = false;
    listeners.get("pageshow")();
    assert.equal(capacityOverride.disabled, true);
  } finally {
    if (hadWindow) global.window = originalWindow;
    else delete global.window;
  }
});
