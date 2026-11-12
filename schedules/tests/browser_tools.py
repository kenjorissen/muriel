"""Resolve the declared browser installation for Django's embedded Node runners."""

import json
import os
import shutil
import subprocess
from functools import cache
from pathlib import Path


@cache
def require_browser_tools():
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Browser checks require Node 22+; then run ./scripts/setup-browser.")
    probe = Path(__file__).resolve().parents[2] / "scripts/browser-tooling.cjs"
    # Playwright owns launch/action deadlines and browser teardown. Do not kill
    # only its Node parent with a subprocess timeout, leaving Chromium behind.
    completed = subprocess.run([node, str(probe)], capture_output=True, text=True)
    if completed.returncode:
        raise RuntimeError(completed.stderr)
    installation = json.loads(completed.stdout)
    os.environ["PRECEPTOR_PLAYWRIGHT_MODULE"] = installation["module"]
    os.environ["PRECEPTOR_BROWSER_SESSION"] = str(Path(__file__).with_name("browser_session.cjs"))
    os.environ["PRECEPTOR_BROWSER_REQUESTS"] = str(Path(__file__).with_name("browser_requests.cjs"))
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"
    return node, installation["executable"]
