<p align="center"><img src="assets/logo.png" width="160" alt="AppTime logo"></p>

# AppTime

Screen time for your Windows PC or Mac, like the one on your phone. AppTime records how
long each app is on your screen and shows the totals per day, week, month, year
and lifetime. What you make of the numbers is up to you.

## Download

From the [latest release](https://github.com/HusniHeartnet/AppTime/releases/latest):

**Windows:** download **AppTime-Setup.exe** and run it. It installs AppTime for your user
account (no admin password needed), adds it to the Start menu, and starts it. Windows may show
"Windows protected your PC" because the app isn't code-signed: click **More info → Run anyway**.
The AppTime cat appears in the system tray: click it to open your dashboard, right-click it to
pause tracking or quit. AppTime starts automatically when you sign in; you can turn that off
in the tray menu or at the bottom of the dashboard. To uninstall, use **Settings → Apps**.

**Mac** (Apple Silicon, M1 or newer): download **AppTime.dmg**, open it, and drag **AppTime**
into **Applications**. The first time you open it, macOS will say it can't check the app for
malicious software, because it isn't notarized by Apple. Click **Done**, then open
**System Settings → Privacy & Security**, scroll down and click **Open Anyway**. The AppTime cat
appears in the menu bar at the top of the screen. Click it for the dashboard, pausing, or
quitting. It opens automatically when you log in; you can turn that off with **Open at Login**
in the menu. AppTime needs no special permissions on Mac. To uninstall, quit it and drag it
from Applications to the Trash.

## How it counts

- Every second it checks which apps you can actually see: the focused app plus any window
  that's at least partly uncovered on screen. Minimized windows, windows on other virtual
  desktops and windows hidden behind others don't count.
- If 3 apps are on screen together for an hour, each gets that hour. Screen time stays real
  clock time (1 hour), so app times can add up to more than it.
- With no keyboard or mouse input for 5 minutes (changeable at the bottom of the dashboard),
  it stops counting until you're back. Sleep and the lock screen aren't counted.
- Counting starts when you first run it.
- The system's own tools and interface aren't counted: File Explorer, Task Manager and Settings
  on Windows; Finder, Activity Monitor and System Settings on Mac; plus menus, search,
  system dialogs and AppTime itself.

## Light on your PC

AppTime runs quietly in the background. Measured on a normal Windows 11 laptop:

- **Background tracker:** about 0.02% CPU and about 30 MB of memory.
- **Dashboard window:** about 110 MB more while it's open (a trimmed-down web view),
  all of it freed the moment you close the window.

You'll find it as **AppTime** in Task Manager (or Activity Monitor on Mac), and the bottom of the dashboard
shows its current CPU and memory use.

## Updating

AppTime checks for new versions by itself. When one is out, a bar appears at the top of the
dashboard: click **Update now** and AppTime downloads it, checks it's exactly the published file,
installs it and restarts. Your data and settings stay as they are. You can also click
**Check for updates** at the bottom of the dashboard.

## Your data

Everything is stored on your computer: `%LOCALAPPDATA%\AppTime\apptime.db` on Windows,
`~/Library/Application Support/AppTime/apptime.db` on Mac. Only app names and
time are stored, never window titles. Delete that file to start over.

### Moving to a new computer, or reinstalling

Your history lives only on your computer, so back it up before reinstalling or switching:

1. In the dashboard, click **Export…** (bottom of the page) and save the backup file somewhere
   safe: a USB stick, Google Drive, OneDrive or iCloud.
2. On the new computer, install AppTime, click **Import…** and pick that file.

Importing adds the backup to whatever is already there, and importing the same backup twice
never counts anything double. Backups work between Windows and Mac too.

## Community sharing (optional)

On first launch AppTime asks whether you'd like to share **anonymous daily app totals** with
the AppTime community. The combined results (which apps the most people use, average screen
time, how much of a hivemind we are) get posted in our community and on social media.

- It's off unless you say yes, and AppTime works the same either way.
- Only finished days from the day you agree are sent: total screen time and each app's name
  and time, under a random ID. Never window titles, websites or anything personal.
- Under **Community sharing** at the bottom of the dashboard you can see exactly what's sent,
  hide apps, turn sharing off, or delete everything you've shared.

Details: [PRIVACY.md](PRIVACY.md)

## Run from source

```
pip install -r requirements.txt
pythonw apptime.py        # Windows
python3 apptime.py        # Mac
```

Use `--no-tray` to run it in a console, or `--selftest` to check tracking works and exit.

## Making a release

Push a version tag and GitHub Actions builds the Windows installer and the Mac disk image,
tests them (including install, in-app update and uninstall), and attaches them to a release:

```
git tag v1.8.0
git push origin v1.8.0
```

The community server lives in [`server/`](server/README.md).
