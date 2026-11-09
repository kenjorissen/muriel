function initializeFormProcessingFeedback(form) {
  const feedback = form.querySelector("[data-processing-feedback]");
  if (!feedback) return false;

  const elapsedSeconds = feedback.querySelector("[data-processing-elapsed-seconds]");
  let elapsedTimer;
  let preservedSubmitter;

  function reset() {
    window.clearInterval(elapsedTimer);
    elapsedTimer = null;
    preservedSubmitter?.remove();
    preservedSubmitter = null;
    feedback.hidden = true;
    form.removeAttribute("aria-busy");
    for (const button of form.querySelectorAll('button[type="submit"]')) button.disabled = false;
    if (elapsedSeconds) elapsedSeconds.value = "0";
  }

  form.addEventListener("submit", (event) => {
    // Native validation can reject a synthetic submit event before navigation.
    if (!form.checkValidity() || feedback.hidden === false) return;
    if (event.submitter?.name) {
      preservedSubmitter = form.ownerDocument.createElement("input");
      preservedSubmitter.type = "hidden";
      preservedSubmitter.name = event.submitter.name;
      preservedSubmitter.value = event.submitter.value;
      form.append(preservedSubmitter);
    }
    feedback.hidden = false;
    form.setAttribute("aria-busy", "true");
    for (const button of form.querySelectorAll('button[type="submit"]')) button.disabled = true;
    if (elapsedSeconds) {
      const startedAt = Date.now();
      elapsedSeconds.value = "0";
      elapsedTimer = window.setInterval(() => {
        elapsedSeconds.value = String(Math.floor((Date.now() - startedAt) / 1000));
      }, 1000);
    }
    feedback.focus();
  });
  window.addEventListener("pageshow", reset);
  return true;
}

function initializeFormProcessingFeedbackForms(root = document) {
  root.querySelectorAll("[data-processing-feedback-form]").forEach(initializeFormProcessingFeedback);
}

if (typeof document !== "undefined") initializeFormProcessingFeedbackForms();

if (typeof module !== "undefined") {
  module.exports = { initializeFormProcessingFeedback, initializeFormProcessingFeedbackForms };
}
