function initializeFacultyAdminTimeReportControls(root = document) {
  const form = root.querySelector("[data-faculty-admin-time-controls]");
  if (!form || typeof form.requestSubmit !== "function") return false;
  form.addEventListener("change", (event) => {
    if (event.target.name === "coverage" && event.target.checked) form.requestSubmit();
  });
  return true;
}

if (typeof document !== "undefined") {
  initializeFacultyAdminTimeReportControls();
}

if (typeof module !== "undefined") {
  module.exports = { initializeFacultyAdminTimeReportControls };
}
