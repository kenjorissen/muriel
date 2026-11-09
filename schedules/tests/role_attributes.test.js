const assert = require("node:assert/strict");
const test = require("node:test");

const { initializeRoleAttributes } = require("../static/schedules/role-attributes.js");

class TestElement {
  constructor() {
    this.hidden = false;
    this.listeners = new Map();
  }

  addEventListener(type, listener) {
    this.listeners.set(type, [...(this.listeners.get(type) || []), listener]);
  }

  dispatchEvent(event) {
    for (const listener of this.listeners.get(event.type) || []) listener(event);
  }
}

function roleAttributesFixture(roleValue) {
  const role = new TestElement();
  const facultyEquivalent = new TestElement();
  const facultyLeadership = new TestElement();
  role.value = roleValue;
  const fields = {
    '[data-role-attribute="faculty-equivalent"]': facultyEquivalent,
    '[data-role-attribute="faculty-leadership"]': facultyLeadership,
  };
  const form = {
    elements: { role },
    querySelector: (selector) => fields[selector] || null,
  };

  return { facultyEquivalent, facultyLeadership, role, form };
}

test("shows only the attributes applicable to the initially selected role", () => {
  for (const [roleValue, equivalentHidden, leadershipHidden] of [
    ["community_preceptor", false, true],
    ["faculty", true, false],
    ["casual_rounder", true, true],
  ]) {
    const fixture = roleAttributesFixture(roleValue);

    assert.equal(initializeRoleAttributes(fixture.form), true);
    assert.equal(fixture.facultyEquivalent.hidden, equivalentHidden);
    assert.equal(fixture.facultyLeadership.hidden, leadershipHidden);
  }
});

test("updates the applicable attributes when the role changes", () => {
  const fixture = roleAttributesFixture("community_preceptor");
  initializeRoleAttributes(fixture.form);

  fixture.role.value = "faculty";
  fixture.role.dispatchEvent(new Event("change"));
  assert.equal(fixture.facultyEquivalent.hidden, true);
  assert.equal(fixture.facultyLeadership.hidden, false);

  fixture.role.value = "";
  fixture.role.dispatchEvent(new Event("change"));
  assert.equal(fixture.facultyEquivalent.hidden, true);
  assert.equal(fixture.facultyLeadership.hidden, true);
});
