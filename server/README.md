# AppTime community server

Runs on the VPS (139.190.100.30). It has two parts:

- **Uploads (public, HTTPS on port 8443).** AppTime sends anonymous daily app
  totals here from people who chose to share, and deletes them on request.
  Nothing stored can be read through this port.
- **Report (private).** The community stats page you screenshot for Discord and
  social media. It only listens on the VPS itself, and you open it from your PC
  through an SSH tunnel.

Files: `collector.py` (Python 3.9+ standard library only, data in SQLite) and
`community.html` (the report page).

There's no domain, so instead of a public certificate the server uses its own
certificate. AppTime ships with a copy (`server_cert.pem` in the repo root) and
trusts only that one. Uploads are encrypted, and AppTime can't be tricked into
sending to anyone else.

## Quick install or update

From the AppTime folder on your PC (uses the `cloudpc-vps` SSH login, which has admin rights):

```
scp server/collector.py server/community.html server/setup.sh cloudpc-vps:/tmp/
ssh cloudpc-vps "sudo bash /tmp/setup.sh"
```

`setup.sh` does steps 2 and 4 below. It's safe to run again: it keeps the existing
certificate and data, and restarts the service. The manual steps follow for reference.

The commands below are for Ubuntu or Debian. Run the VPS ones over SSH.

## 1. Copy the files to the VPS

From your PC, in the AppTime folder:

```
scp server/collector.py server/community.html root@139.190.100.30:/tmp/
```

(Use your own username instead of `root` if you log in differently.)

## 2. Install on the VPS

```
sudo useradd --system --home /opt/apptime --shell /usr/sbin/nologin apptime
sudo mkdir -p /opt/apptime
sudo mv /tmp/collector.py /tmp/community.html /opt/apptime/
cd /opt/apptime
sudo openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
  -keyout key.pem -out cert.pem -subj "/CN=AppTime collector" \
  -addext "subjectAltName=IP:139.190.100.30"
sudo chown -R apptime:apptime /opt/apptime
sudo chmod 600 /opt/apptime/key.pem
```

The certificate is valid for 10 years. `key.pem` is secret: it never leaves the VPS.

## 3. Put the certificate into AppTime

On the VPS:

```
cat /opt/apptime/cert.pem
```

Copy everything from `-----BEGIN CERTIFICATE-----` to `-----END CERTIFICATE-----`
into a file called `server_cert.pem` in the AppTime folder on your PC (next to
`apptime.py`), then commit it. It's public and safe to share. Until this file
exists, AppTime doesn't offer sharing at all.

If you ever make a new certificate, update `server_cert.pem` and release a new
AppTime version. Older versions will stop being able to upload.

## 4. Run it as a service

Create `/etc/systemd/system/apptime.service`:

```
[Unit]
Description=AppTime community collector
After=network.target

[Service]
User=apptime
WorkingDirectory=/opt/apptime
ExecStart=/usr/bin/python3 /opt/apptime/collector.py
Restart=always
Environment=APPTIME_DB=/opt/apptime/community.db

[Install]
WantedBy=multi-user.target
```

```
sudo systemctl daemon-reload
sudo systemctl enable --now apptime
sudo systemctl status apptime
```

Open port 8443. If you use `ufw`:

```
sudo ufw allow 8443/tcp
```

Also open TCP 8443 in your VPS provider's firewall panel, if it has one.

Check it from your PC (`-k` skips certificate checking, for this test only):

```
curl -k https://139.190.100.30:8443/
```

It should answer `AppTime collector is running.`

## 5. See the results (private)

From your PC:

```
ssh -L 8081:127.0.0.1:8081 cloudpc-vps
```

While that SSH session is open, visit http://localhost:8081 in your browser. The
page shows the last 7 days, last 30 days or all time:

- people sharing
- average screen time
- the Hivemind score
- the apps the most people use, and the apps with the most hours

Screenshot it for Discord and social media. An app only appears once at least
3 people use it.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `APPTIME_PORT` | 8443 | Public HTTPS upload port |
| `APPTIME_REPORT_PORT` | 8081 | Private report port (127.0.0.1 only) |
| `APPTIME_DB` | `community.db` next to the script | Database file |
| `APPTIME_CERT` / `APPTIME_KEY` | `cert.pem` / `key.pem` next to the script | TLS certificate and key |
| `APPTIME_MIN_USERS` | 3 | An app only appears once this many people use it |
| `APPTIME_RETENTION_DAYS` | 730 | Days of data to keep |

## Updating the server

Copy the new `collector.py` / `community.html` over the old ones in `/opt/apptime`, then:

```
sudo systemctl restart apptime
```

## Backups and data retention

- Back up `/opt/apptime/community.db`, e.g. daily from cron:
  `sqlite3 /opt/apptime/community.db ".backup /opt/apptime/backup.db"`
- Data older than 2 years is deleted automatically once a day (`APPTIME_RETENTION_DAYS`, default 730), matching PRIVACY.md.
