#!/usr/bin/env bash
# Install SentrySDR as a systemd service. Run as root from a checkout.
#   sudo ./scripts/install.sh master|slave [--with-decoders]
#
# Debian/Raspberry Pi OS packages are installed when apt is available. On other
# systems, first install: rtl-sdr, ffmpeg, python3 (3.11+), nodejs/npm (master only).
set -euo pipefail

ROLE="${1:-}"
WITH_DECODERS="${2:-}"
if [[ "$ROLE" != "master" && "$ROLE" != "slave" ]]; then
  echo "usage: $0 master|slave [--with-decoders]" >&2
  exit 2
fi
if [[ $EUID -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP=/opt/sentrysdr
CONF=/etc/sentrysdr/sentrysdr.toml

if command -v apt-get >/dev/null; then
  apt-get update
  apt-get install -y rtl-sdr ffmpeg python3-venv rsync
  [[ "$ROLE" == "master" ]] && apt-get install -y npm
  if [[ "$WITH_DECODERS" == "--with-decoders" ]]; then
    apt-get install -y git cmake build-essential pkg-config librtlsdr-dev libusb-1.0-0-dev \
      libncurses-dev libsndfile1-dev libitpp-dev libzstd-dev zlib1g-dev libpulse-dev libcjson-dev
  fi
fi

id sentrysdr >/dev/null 2>&1 || useradd --system --home-dir /var/lib/sentrysdr --shell /usr/sbin/nologin sentrysdr
install -d -o sentrysdr -g sentrysdr -m 0750 /var/lib/sentrysdr /var/log/sentrysdr
install -d -o root -g sentrysdr -m 0750 /etc/sentrysdr

# The application folder stays root-owned, so the service can't modify its own code.
install -d "$APP"
rsync -a --delete --exclude .git --exclude .venv --exclude .cache --exclude .dev --exclude .vendor \
  --exclude node_modules --exclude web/dist "$SRC/" "$APP/"
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install --quiet --upgrade pip
# Exact, tested versions from requirements.lock; then the app itself without re-resolving them.
"$APP/.venv/bin/pip" install --quiet -r "$APP/requirements.lock"
"$APP/.venv/bin/pip" install --quiet --no-deps -e "$APP"

if [[ "$ROLE" == "master" ]]; then
  npm --prefix "$APP/web" ci --no-audit --no-fund
  npm --prefix "$APP/web" run build
  rm -rf "$APP/web/node_modules"
fi
if [[ "$WITH_DECODERS" == "--with-decoders" ]]; then
  "$APP/scripts/build-decoders.sh" "$APP/.vendor" || echo "Some decoders failed to build; see the summary above." >&2
fi

if [[ ! -f "$CONF" ]]; then
  install -o root -g sentrysdr -m 0640 "$APP/config/$ROLE.example.toml" "$CONF"
  echo "Created $CONF. Edit it before starting the service."
fi

install -m 0644 "$APP/deploy/60-sentrysdr-rtlsdr.rules" /etc/udev/rules.d/
install -m 0644 "$APP/deploy/blacklist-rtlsdr.conf" /etc/modprobe.d/
udevadm control --reload-rules && udevadm trigger
install -m 0644 "$APP/deploy/sentrysdr.service" /etc/systemd/system/
systemctl daemon-reload

cat <<EOF

Installed, but not started. Next:
  1. sudo -u sentrysdr $APP/.venv/bin/python -m sentrysdr -c $CONF list-sdrs
  2. Edit $CONF (sdr.serials, and the addresses for your role)
EOF
if [[ "$ROLE" == "master" ]]; then
  echo "  3. sudo -u sentrysdr $APP/.venv/bin/python -m sentrysdr -c $CONF create-admin"
  echo "  4. sudo systemctl enable --now sentrysdr"
  echo "  5. Publish it with an example from $APP/deploy/"
else
  echo "  3. Create an enrolment token on the master (Admin > Nodes) and add it to $CONF"
  echo "  4. sudo systemctl enable --now sentrysdr"
fi
