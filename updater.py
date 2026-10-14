"""Checks GitHub for a newer AppTime and installs it.

Windows: downloads the installer (AppTime-Setup.exe) and runs it silently.
It closes this copy, upgrades the installed AppTime and starts it again.
macOS: the AppTime.app bundle is swapped for the new one and started.

The download is checked against the size and SHA-256 checksum GitHub publishes
for the release file before anything is replaced.
"""

import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import urllib.request

REPO = "HusniHeartnet/AppTime"
API_URL = os.environ.get("APPTIME_UPDATE_API", f"https://api.github.com/repos/{REPO}/releases/latest")
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
CHECK_EVERY_SECONDS = 12 * 3600
ASSET_NAME = "AppTime-mac.zip" if sys.platform == "darwin" else "AppTime-Setup.exe"


def version_tuple(text):
    return tuple(int(n) for n in re.findall(r"\d+", text or "")[:3])


def https_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def installed_location():
    """The AppTime.exe / AppTime.app this copy runs from, or None when running from source."""
    if not getattr(sys, "frozen", False):
        return None
    if sys.platform == "darwin":
        bundle = os.path.abspath(os.path.join(os.path.dirname(sys.executable), "..", ".."))
        return bundle if bundle.endswith(".app") else None
    return sys.executable


def old_copy_path(location):
    base, ext = os.path.splitext(location)
    return f"{base}.old{ext}"


def remove_old_copy():
    """After an update: delete the previous version left next to this one."""
    location = installed_location()
    if not location:
        return
    old = old_copy_path(location)
    if os.path.isdir(old):
        shutil.rmtree(old, ignore_errors=True)
    elif os.path.exists(old):
        try:
            os.remove(old)
        except OSError:
            pass  # still closing; it'll be removed next start


class Updater:
    def __init__(self, current_version, user_agent, restart):
        self.current = current_version
        self.user_agent = user_agent
        self.restart = restart  # called with the command that starts the new version
        self.latest = None
        self.notes_url = RELEASES_PAGE
        self.asset = None
        self.status = "idle"  # idle | downloading | installing | error
        self.message = ""
        self.progress = 0.0
        self.lock = threading.Lock()

    # ------------------------------------------------------------ checking

    def check(self):
        req = urllib.request.Request(API_URL, headers={
            "Accept": "application/vnd.github+json", "User-Agent": self.user_agent})
        with urllib.request.urlopen(req, timeout=20, context=https_context()) as res:
            release = json.loads(res.read())
        self.latest = str(release.get("tag_name", "")).lstrip("v") or None
        self.notes_url = release.get("html_url") or RELEASES_PAGE
        self.asset = next((a for a in release.get("assets", []) if a.get("name") == ASSET_NAME), None)

    def check_quietly(self):
        try:
            self.check()
        except Exception:
            pass

    def loop(self, stop_event):
        stop_event.wait(20)  # let startup settle
        while not stop_event.is_set():
            self.check_quietly()
            stop_event.wait(CHECK_EVERY_SECONDS)

    def available(self):
        return bool(self.latest and self.asset and version_tuple(self.latest) > version_tuple(self.current))

    def state(self):
        return {
            "current": self.current,
            "latest": self.latest,
            "available": self.available(),
            "can_install": self.can_install(),
            "notes_url": self.notes_url,
            "status": self.status,
            "message": self.message,
            "progress": round(self.progress, 3),
        }

    # ------------------------------------------------------------ installing

    def can_install(self):
        location = installed_location()
        if not location:
            return False  # running from source code
        if sys.platform != "darwin":
            return True  # the installer takes care of it
        if "/AppTranslocation/" in location:
            return False  # macOS runs un-moved downloads from a read-only copy
        return os.access(os.path.dirname(location), os.W_OK)

    def start_install(self):
        with self.lock:
            if self.status in ("downloading", "installing") or not self.available():
                return
            self.status, self.message, self.progress = "downloading", "", 0.0
        threading.Thread(target=self._install, daemon=True).start()

    def _install(self):
        location = installed_location()
        try:
            if sys.platform == "darwin":
                download = self._download(os.path.dirname(location))
                self.status = "installing"
                command = self._swap_mac(location, download)
            else:
                download = self._download(tempfile.mkdtemp(prefix="apptime-update-"))
                self.status = "installing"
                command = self._run_installer(download)
        except Exception as e:
            self.status, self.message = "error", f"Update failed: {e}"
            return
        self.restart(command)

    def _download(self, folder):
        url, size = self.asset["browser_download_url"], int(self.asset.get("size") or 0)
        expected = str(self.asset.get("digest") or "")
        target = os.path.join(folder, f".{ASSET_NAME}.download")
        sha = hashlib.sha256()
        got = 0
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        with urllib.request.urlopen(req, timeout=60, context=https_context()) as res, open(target, "wb") as out:
            while True:
                chunk = res.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                sha.update(chunk)
                got += len(chunk)
                if size:
                    self.progress = got / size
        try:
            if size and got != size:
                raise ValueError("the download was incomplete")
            if expected.startswith("sha256:") and sha.hexdigest() != expected.split(":", 1)[1].lower():
                raise ValueError("the download didn't match the published file")
        except ValueError:
            os.remove(target)
            raise
        return target

    def _run_installer(self, download):
        setup = os.path.join(os.path.dirname(download), ASSET_NAME)
        os.replace(download, setup)
        # /APPTIMELAUNCH: start AppTime again when done (see installer/AppTime.iss).
        return [setup, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/APPTIMELAUNCH"]

    def _swap_mac(self, bundle, download):
        unpacked = tempfile.mkdtemp(prefix="apptime-update-")
        try:
            subprocess.run(["ditto", "-x", "-k", download, unpacked], check=True, timeout=120)
            new_app = os.path.join(unpacked, "AppTime.app")
            if not os.path.isdir(new_app):
                raise ValueError("the download didn't contain AppTime.app")
            old = old_copy_path(bundle)
            if os.path.exists(old):
                shutil.rmtree(old)
            os.rename(bundle, old)
            try:
                shutil.move(new_app, bundle)
            except OSError:
                os.rename(old, bundle)
                raise
        finally:
            shutil.rmtree(unpacked, ignore_errors=True)
            if os.path.exists(download):
                os.remove(download)
        return ["open", "-n", bundle, "--args", "--after-update"]
