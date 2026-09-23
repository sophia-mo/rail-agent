"""Persist session cookies that Chromium may discard on normal shutdown."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse


def state_directory():
    project = Path(__file__).resolve().parent.parent
    default = project / ".rail-agent" if (project / "pyproject.toml").exists() else Path.home() / ".rail-agent"
    return default.resolve()


def railway_cookie(cookie):
    domain = cookie.get("domain", "").lstrip(".")
    return domain == "12306.cn" or domain.endswith(".12306.cn")


def save_session(context, directory):
    directory = Path(directory)
    # Include session cookies (expires=-1). Local storage stays in the profile.
    cookies = [cookie for cookie in context.cookies() if railway_cookie(cookie)]
    fd, temporary = tempfile.mkstemp(prefix="session-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w") as file:
            json.dump({"cookies": cookies}, file)
        Path(temporary).replace(directory / "session.json")
    finally:
        Path(temporary).unlink(missing_ok=True)


def restore_session(context, directory):
    file = Path(directory) / "session.json"
    if not file.exists():
        return False
    saved = json.loads(file.read_text())
    cookies = [cookie for cookie in saved["cookies"] if railway_cookie(cookie)
               and (cookie.get("expires", -1) == -1 or cookie["expires"] > time.time())]
    if cookies:
        context.add_cookies(cookies)
    return bool(cookies)


def login_status(context):
    """Check inside the page, preserving browser headers and site AJAX hooks.

    A standalone APIRequestContext shares cookies but does not run site JS.
    Check open kyfw tabs (including a tab opened by the login flow). Return only
    a boolean/unknown; never transfer the full response or identity information.
    """
    status = None
    for page in reversed(context.pages):
        if page.is_closed() or urlparse(page.url).hostname != "kyfw.12306.cn":
            continue
        try:
            flag = page.evaluate("""() => new Promise(resolve => {
                const timer = setTimeout(() => resolve(null), 10000);
                const finish = data => {
                    clearTimeout(timer);
                    resolve(typeof data?.data?.flag === 'boolean' ? data.data.flag : null);
                };
                const fail = () => { clearTimeout(timer); resolve(null); };
                if (window.jQuery?.ajax) {
                    window.jQuery.ajax({
                        type: 'POST', url: '/otn/login/checkUser', data: {},
                        dataType: 'json', timeout: 9000,
                        headers: {'If-Modified-Since': '0', 'Cache-Control': 'no-cache'},
                        success: finish, error: fail
                    });
                } else {
                    fetch('/otn/login/checkUser', {
                        method: 'POST', credentials: 'same-origin',
                        headers: {'Content-Type': 'application/x-www-form-urlencoded',
                                  'X-Requested-With': 'XMLHttpRequest', 'Cache-Control': 'no-cache'},
                        body: '', signal: AbortSignal.timeout(9000)
                    }).then(r => r.ok ? r.json() : null).then(finish).catch(fail);
                }
            })""")
            if flag is True:
                return True
            if flag is False:
                status = False
        except Exception:
            continue
    return status
