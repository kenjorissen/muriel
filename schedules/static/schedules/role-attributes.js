function initializeRoleAttributes(form) {
  const role = form.elements.role;
  const facultyEquivalent = form.querySelector('[data-role-attribute="faculty-equivalent"]');
  const facultyLeadership = form.querySelector('[data-role-attribute="faculty-leadership"]');
  if (!role || !facultyEquivalent || !facultyLeadership) return false;

  function updateAttributes() {
    facultyEquivalent.hidden = role.value !== "community_preceptor";
    facultyLeadership.hidden = role.value !== "faculty";
  }

  role.addEventListener("change", updateAttributes);
  updateAttributes();
  return true;
}

function initializeRoleAttributeForms(root = document) {
  root.querySelectorAll("[data-role-attributes]").forEach(initializeRoleAttributes);
}

if (typeof document !== "undefined") {
  initializeRoleAttributeForms();
}

if (typeof module !== "undefined") {
  module.exports = { initializeRoleAttributes, initializeRoleAttributeForms };
}
