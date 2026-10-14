"""Release check (Windows): install AppTime, update it with "Update now" from a
pretend newer release, check it restarts, then uninstall.

Usage: python tests/update_e2e.py dist/AppTime-Setup.exe
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

SETUP = os.path.abspath(sys.argv[1])
APP_DIR = os.path.join(os.environ["LOCALAPPDATA"], "Programs", "AppTime")
APP = os.path.join(APP_DIR, "AppTime.exe")
LOCAL = "http://127.0.0.1:47820"
FAKE = "http://127.0.0.1:47899"

setup_bytes = open(SETUP, "rb").read()
release = {
    "tag_name": "v9.9.9",
    "html_url": FAKE + "/notes",
    "assets": [{"name": "AppTime-Setup.exe", "size": len(setup_bytes),
                "digest": "sha256:" + hashlib.sha256(setup_bytes).hexdigest(),
                "browser_download_url": FAKE + "/AppTime-Setup.exe"}],
}


class FakeGitHub(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps(release).encode() if self.path == "/latest" else setup_bytes
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def apptime_processes():
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Get-CimInstance Win32_Process -Filter \"Name='AppTime.exe'\" | ForEach-Object { $_.CommandLine }"],
                         capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if line.strip()]


def call(path, action=None):
    data = json.dumps({"action": action}).encode() if action else None
    req = urllib.request.Request(LOCAL + path, data=data, method="POST" if action else "GET",
                                 headers={"Content-Type": "application/json"})
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
    print("AppTime processes:", apptime_processes())
    sys.exit(1)


server = ThreadingHTTPServer(("127.0.0.1", 47899), FakeGitHub)
threading.Thread(target=server.serve_forever, daemon=True).start()

subprocess.run([SETUP, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], check=True)
if not os.path.exists(APP):
    fail("not installed")
print("1. installed")

subprocess.Popen([APP], env={**os.environ, "APPTIME_UPDATE_API": FAKE + "/latest"})
if not wait_for(lambda: call("/api/update"), 60):
    fail("AppTime didn't start")
state = call("/api/update", "check")
print(f"2. running {state['current']}, sees {state['latest']}: available={state['available']} can_install={state['can_install']}")
if not (state["available"] and state["can_install"]):
    fail("update not offered")

call("/api/update", "install")
print("3. clicked Update now")
restarted = wait_for(lambda: any("--after-update" in p for p in apptime_processes()) and call("/api/update"), 180)
if not restarted:
    fail("AppTime didn't come back after the update")
print("4. updated and running again:", call("/api/update")["current"])

subprocess.run(["taskkill", "/F", "/IM", "AppTime.exe"], capture_output=True)
subprocess.run([os.path.join(APP_DIR, "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], check=True)
time.sleep(5)
if os.path.exists(APP):
    fail("still installed after uninstall")
print("5. uninstalled. Update test OK")
