# AppTime

Screen time for your Windows PC, like the one on your phone. AppTime records how
long each app is on your screen and shows the totals per day, week, month, year
and lifetime. What you make of the numbers is up to you.

## Download

Get **AppTime.exe** from the [latest release](https://github.com/HusniHeartnet/AppTime/releases/latest)
and run it. No installation and no Python needed.

Windows may show "Windows protected your PC" because the app isn't code-signed.
Click **More info → Run anyway**.

A clock icon appears in the system tray. Click it to open your dashboard. Right-click it to
pause tracking, turn on **Start with Windows**, or quit.

## How it counts

- Every second it checks which apps you can actually see: the focused app plus any window
  that's at least partly uncovered on screen. Minimized windows, windows on other virtual
  desktops and windows hidden behind others don't count.
- If 3 apps are on screen together for an hour, each gets that hour. Screen time stays real
  clock time (1 hour), so app times can add up to more than it.
- With no keyboard or mouse input for 5 minutes (changeable at the bottom of the dashboard),
  it stops counting until you're back. Sleep and the lock screen aren't counted.
- Counting starts when you first run it.

## Your data

Everything is stored on your PC in `%LOCALAPPDATA%\AppTime\apptime.db`. Only app names and
time are stored, never window titles. Delete that file to start over.

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
pythonw apptime.py
```

Use `python apptime.py --no-tray` to run it in a console instead.

## Making a release

Push a version tag and GitHub Actions builds `AppTime.exe` and attaches it to a release:

```
git tag v1.2.0
git push origin v1.2.0
```

The community server lives in [`server/`](server/README.md).
