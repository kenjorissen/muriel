// Lifecycle boundary for embedded runners that previously killed only Node on
// a Python deadline. Playwright owns Chromium and its process/profile cleanup.
const { chromium } = require(process.env.PRECEPTOR_PLAYWRIGHT_MODULE);

function withDeadline(work, milliseconds, description) {
  let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${description} exceeded ${milliseconds}ms`)), milliseconds);
  });
  return Promise.race([work, deadline]).finally(() => clearTimeout(timer));
}

async function withBrowserPage(executablePath, work, {
  contextOptions = {},
  beforeClose = () => {},
  actionTimeoutMs = 30_000,
  workTimeoutMs = 90_000,
} = {}) {
  // Startup has its own supported Playwright deadline; the work budget starts
  // only after the page is ready, not while Chromium is starting under load.
  const browser = await chromium.launch({ headless: true, executablePath, timeout: 30_000 });
  let context;
  let failure;
  try {
    const page = await withDeadline((async () => {
      context = await browser.newContext(contextOptions);
      context.setDefaultTimeout(actionTimeoutMs);
      context.setDefaultNavigationTimeout(actionTimeoutMs);
      return context.newPage();
    })(), 30_000, "Browser page setup");
    await withDeadline(work({ page, context }), workTimeoutMs, "Browser scenario");
  } catch (error) {
    failure = error;
  } finally {
    // Release intercepted requests before closing their context. Never let a
    // cleanup error replace the original missing-element/scenario diagnostic.
    try {
      beforeClose();
      await withDeadline(context?.close(), 5_000, "Browser context cleanup");
    } catch (error) {
      if (failure) console.error("Additional context cleanup failure:", error);
      else failure = error;
    }
    try {
      // Always await Playwright's browser close, including its owned force-kill
      // fallback and temporary-directory cleanup. No Python watchdog races it.
      await browser.close();
    } catch (error) {
      if (failure) console.error("Additional browser cleanup failure:", error);
      else failure = error;
    }
  }
  if (failure) throw failure;
}

module.exports = { withBrowserPage };
