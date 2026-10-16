"""Release check (Windows): the real AppTime 1.8.0 (Python) updates itself to
the new build with "Update now", and the history it recorded is kept.

Usage: python tests/upgrade_from_1x.py dist/AppTime-Setup.exe
"""
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NEW_SETUP = os.path.abspath(sys.argv[1])
OLD_SETUP_URL = "https://github.com/HusniHeartnet/AppTime/releases/download/v1.8.0/AppTime-Setup.exe"
APP_DIR = os.path.join(os.environ["LOCALAPPDATA"], "Programs", "AppTime")
APP = os.path.join(APP_DIR, "AppTime.exe")
LOCAL = "http://127.0.0.1:47820"
FAKE = "http://127.0.0.1:47899"

new_bytes = open(NEW_SETUP, "rb").read()
release = {
    "tag_name": "v9.9.9",
    "html_url": FAKE + "/notes",
    "assets": [{"name": "AppTime-Setup.exe", "size": len(new_bytes),
                "digest": "sha256:" + hashlib.sha256(new_bytes).hexdigest(),
                "browser_download_url": FAKE + "/AppTime-Setup.exe"}],
}


class FakeGitHub(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps(release).encode() if self.path == "/latest" else new_bytes
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def call(path, body=None):
    req = urllib.request.Request(LOCAL + path, data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.loads(res.read())


def wait_for(check, seconds):
    end = time.time() + seconds
    while time.time() < end:
        try:
            if check():
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def fail(message):
    print("FAILED:", message)
    sys.exit(1)


threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", 47899), FakeGitHub).serve_forever, daemon=True).start()

old_setup = os.path.join(os.environ["TEMP"], "AppTime-1.8.0-Setup.exe")
urllib.request.urlretrieve(OLD_SETUP_URL, old_setup)
subprocess.run([old_setup, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], check=True)
subprocess.Popen([APP, "--background"], env={**os.environ, "APPTIME_UPDATE_API": FAKE + "/latest"})
if not wait_for(lambda: call("/api/update")["current"] == "1.8.0", 90):
    fail("AppTime 1.8.0 didn't start")
call("/api/share", {"enabled": False})  # answer the sharing question, as a real user would have
time.sleep(20)  # let 1.8.0 record some history
before = call("/api/stats?range=all")
print(f"1. AppTime 1.8.0 installed and running, {before['total']:.0f} s of history, apps: {[a['exe'] for a in before['apps']]}")

state = call("/api/update", {"action": "check"})
if not (state["available"] and state["can_install"]):
    fail(f"1.8.0 didn't offer the update: {state}")
call("/api/update", {"action": "install"})
print("2. clicked Update now in 1.8.0")

new_version = None


def upgraded():
    global new_version
    new_version = call("/api/update")["current"]
    return new_version not in (None, "1.8.0")


if not wait_for(upgraded, 240):
    fail("the new version didn't start after the update")
print(f"3. updated: now running {new_version}")

after = call("/api/stats?range=all")
if after["total"] + 1 < before["total"]:
    fail(f"history lost: {before['total']} s before, {after['total']} s after")
missing = {a["exe"] for a in before["apps"]} - {a["exe"] for a in after["apps"]}
if missing:
    fail(f"apps missing after the update: {missing}")
share = call("/api/share")
if not share["decided"]:
    fail("the sharing answer was lost")
print(f"4. history kept ({after['total']:.0f} s), all apps kept, settings kept")

subprocess.run(["taskkill", "/F", "/IM", "AppTime.exe"], capture_output=True)
subprocess.run([os.path.join(APP_DIR, "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], check=True)
print("5. uninstalled. Upgrade from 1.x OK")
