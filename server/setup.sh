#!/usr/bin/env bash
# Installs or updates the AppTime collector on this server. Safe to run again.
# Expects collector.py and community.html in /tmp (copied with scp).
set -euo pipefail

IP="139.190.100.30"
DIR=/opt/apptime

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root (e.g. sudo bash $0)"; exit 1
fi

echo "== Checking Python"
if ! command -v python3 >/dev/null; then
  if command -v apt-get >/dev/null; then apt-get update -qq && apt-get install -y -qq python3; \
  elif command -v dnf >/dev/null; then dnf install -y -q python3; \
  else echo "Install python3 first"; exit 1; fi
fi
python3 -c 'import sys; assert sys.version_info >= (3, 9), sys.version' \
  || { echo "Python 3.9 or newer is needed"; exit 1; }
python3 --version

echo "== Installing files to $DIR"
id apptime >/dev/null 2>&1 || useradd --system --home "$DIR" --shell /usr/sbin/nologin apptime
mkdir -p "$DIR"
install -m 644 /tmp/collector.py /tmp/community.html "$DIR/"

if [ ! -f "$DIR/key.pem" ]; then
  echo "== Creating the server certificate (valid 10 years)"
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
    -keyout "$DIR/key.pem" -out "$DIR/cert.pem" -subj "/CN=AppTime collector" \
    -addext "subjectAltName=IP:$IP" 2>/dev/null
else
  echo "== Keeping the existing certificate"
fi
chown -R apptime:apptime "$DIR"
chmod 600 "$DIR/key.pem"

echo "== Setting up the service"
cat > /etc/systemd/system/apptime.service <<'UNIT'
[Unit]
Description=AppTime community collector
After=network.target

[Service]
User=apptime
WorkingDirectory=/opt/apptime
ExecStart=/usr/bin/env python3 /opt/apptime/collector.py
Restart=always
Environment=APPTIME_DB=/opt/apptime/community.db

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable apptime >/dev/null 2>&1
systemctl restart apptime
sleep 2
systemctl is-active apptime

echo "== Firewall"
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 8443/tcp comment 'AppTime uploads'
elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port=8443/tcp && firewall-cmd --reload
else
  echo "No active ufw/firewalld; nothing to change here"
fi

echo "== Local test"
curl -sk https://127.0.0.1:8443/ || echo "(curl failed)"
rm -f /tmp/collector.py /tmp/community.html /tmp/setup.sh

echo
echo "== Server certificate (public, goes into AppTime as server_cert.pem):"
cat "$DIR/cert.pem"
echo "== Done"
