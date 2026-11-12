// Request assertions only: Playwright/the caller still own pages and cleanup.
// Install before navigation. Await finish before reporting scenario success.
function observeRequests(page) {
  const pending = new Set();
  const expected = new WeakMap();
  const statuses = new WeakMap();
  const failures = [];
  let drained;
  const describe = request => `${request.method()} ${request.url()}`;
  const completed = request => {
    pending.delete(request);
    if (!pending.size) drained?.();
  };
  page.on("request", request => pending.add(request));
  page.on("response", response => {
    statuses.set(response.request(), response.status());
    if (response.status() >= 400 && expected.get(response.request())?.status !== response.status()) {
      failures.push(`${describe(response.request())}: HTTP ${response.status()}`);
    }
  });
  page.on("requestfinished", completed);
  page.on("requestfailed", request => {
    const error = request.failure()?.errorText || "unknown transport error";
    const outcome = expected.get(request);
    if (outcome?.errorText !== error ||
        (outcome.status !== undefined && statuses.get(request) !== outcome.status)) {
      failures.push(`${describe(request)}: ${error}`);
    }
    completed(request);
  });

  return {
    // Scope exceptions to the actual intercepted Request and exact outcome,
    // never to all assets, a URL prefix, or an HTTP/transport error class.
    expectFailure(request, outcome) { expected.set(request, outcome); },
    abortExcluded(route) {
      expected.set(route.request(), { errorText: "net::ERR_FAILED" });
      return route.abort();
    },
    async finish({ timeoutMs = 30_000 } = {}) {
      let timer;
      try {
        await Promise.race([
          (async () => {
            // load covers parser-discovered assets, including async scripts and
            // images. Then await terminal events for already-started fetches.
            // Callers must await their actions, not schedule future background work.
            await page.waitForLoadState("load", { timeout: timeoutMs });
            if (pending.size) await new Promise(resolve => { drained = resolve; });
          })(),
          new Promise((_, reject) => {
            timer = setTimeout(() => reject(new Error(
              `Request completion exceeded ${timeoutMs}ms; pending:\n${[...pending].map(describe).join("\n")}`
            )), timeoutMs);
          }),
        ]);
      } catch (error) {
        failures.push(error.message);
      } finally {
        clearTimeout(timer);
        drained = undefined;
      }
      if (failures.length) throw new Error(`Unexpected browser request failures:\n${failures.join("\n")}`);
    },
  };
}

module.exports = { observeRequests };
