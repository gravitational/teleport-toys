#!/usr/bin/env bash
# Prepare one Raspberry Pi 3 (32-bit Arch Linux ARM) as a BeamBot simulation node:
#   - time sync (Teleport certificates are picky about clock skew)
#   - Teleport (linux-arm tarball) joined as an SSH node + App Service that publishes the MCP server
#   - beambot-sim systemd service: the official MCP Python SDK in /opt/beambot-sim/venv
#     (falls back to the zero-dependency stdlib server if the SDK can't be installed)
#
# Usage (as root, from the directory containing this script; deploy.sh does this for you):
#   sudo ./setup_pi.sh --role arm --proxy <your-tenant>.beams.run:443 --token-file <file holding the join token> \
#        [--robot beambot-sim] [--real] [--peers <ip>,<ip>,...] [--full-upgrade] [--reset-teleport]
# --token-file is read, then deleted. (--token <join-token> still works, but puts the token in ps and sudo's log.)
#
# Tested on Arch Linux ARM (armv7). Other distros need the package-install lines adapted.
set -euo pipefail

ROLE="" PROXY="" TOKEN="" TOKEN_FILE="" ROBOT="beambot-sim" SIM=1 PEERS="" FULL_UPGRADE=0 RESET=0
TELEPORT_VERSION="${TELEPORT_VERSION:-18.11.1}"  # keep it equal to (or older than) your cluster's version
TELEPORT_PKG="${TELEPORT_PKG:-teleport-ent}"     # Teleport Cloud tenants use the enterprise build
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role) ROLE="$2"; shift 2 ;;
    --proxy) PROXY="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    --token-file) TOKEN_FILE="$2"; shift 2 ;;
    --robot) ROBOT="$2"; shift 2 ;;
    --real) SIM=0; shift ;;
    --peers) PEERS="$2"; shift 2 ;;
    --full-upgrade) FULL_UPGRADE=1; shift ;;
    --reset-teleport) RESET=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

if [[ -n "$TOKEN_FILE" ]]; then
  [[ -r "$TOKEN_FILE" ]] || die "cannot read --token-file $TOKEN_FILE"
  TOKEN="$(cat "$TOKEN_FILE")"
  rm -f "$TOKEN_FILE"   # read once: don't leave the token lying in /var/tmp
fi

[[ $EUID -eq 0 ]] || die "run as root (sudo)"
[[ "$ROLE" =~ ^(safety|vision|drive|arm|sensors)$ ]] || die "--role must be safety|vision|drive|arm|sensors"
[[ -n "$PROXY" ]] || die "--proxy is required (e.g. <your-tenant>.beams.run:443)"
[[ "$(uname -m)" == armv7l ]] || echo "WARN: expected armv7l, got $(uname -m)"
command -v pacman >/dev/null || die "this script targets Arch Linux ARM"

NAME="${ROBOT}-${ROLE}"

# ---------------------------------------------------------------- time
say "Hardening time sync"
mkdir -p /etc/systemd/timesyncd.conf.d
cat > /etc/systemd/timesyncd.conf.d/beambot-sim.conf <<'CONF'
[Time]
NTP=time.cloudflare.com time.google.com
FallbackNTP=0.arch.pool.ntp.org 1.arch.pool.ntp.org 2.arch.pool.ntp.org 3.arch.pool.ntp.org
CONF
timedatectl set-ntp true
systemctl restart systemd-timesyncd
for _ in $(seq 1 30); do
  [[ "$(timedatectl show -p NTPSynchronized --value)" == yes ]] && break
  sleep 1
done
timedatectl show -p NTPSynchronized --value | grep -q yes \
  && echo "clock synced: $(date -u)" \
  || echo "WARN: clock not synced yet; Teleport join may fail with cert errors. Check UDP/123."

# ---------------------------------------------------------------- packages
say "Installing packages"
if [[ $FULL_UPGRADE -eq 1 ]]; then
  pacman -Syu --noconfirm
fi
# -Sy (not -Syu) keeps this fast; use --full-upgrade if pacman complains about versions
pacman -Sy --needed --noconfirm python curl tar i2c-tools libgpiod \
  || die "pacman failed: re-run with --full-upgrade (may need a reboot after)."

# ---------------------------------------------------------------- hardware access
say "Enabling I2C + hardware permissions"
REBOOT_NEEDED=0
CONFIG_TXT=/boot/config.txt
if [[ -f $CONFIG_TXT ]] && ! grep -q '^dtparam=i2c_arm=on' $CONFIG_TXT; then
  echo 'dtparam=i2c_arm=on' >> $CONFIG_TXT
  REBOOT_NEEDED=1
fi
echo i2c-dev > /etc/modules-load.d/i2c-dev.conf
modprobe i2c-dev 2>/dev/null || true

getent group robohw >/dev/null || groupadd --system robohw
cat > /etc/udev/rules.d/90-beambot-sim.rules <<'RULES'
SUBSYSTEM=="i2c-dev", GROUP="robohw", MODE="0660"
SUBSYSTEM=="gpio", KERNEL=="gpiochip*", GROUP="robohw", MODE="0660"
RULES
udevadm control --reload-rules && udevadm trigger || true

id robot >/dev/null 2>&1 || useradd --system --user-group --home-dir /opt/beambot-sim --shell /usr/bin/nologin robot
usermod -aG robohw,video robot

# ---------------------------------------------------------------- robot MCP server
say "Installing the MCP server ($ROLE, sim=$SIM)"
mkdir -p /opt/beambot-sim
rm -rf /opt/beambot-sim/robot_mcp
cp -r "$SRC_DIR/robot_mcp" /opt/beambot-sim/
find /opt/beambot-sim -name '__pycache__' -prune -exec rm -rf {} +
chown -R root:root /opt/beambot-sim && chmod -R a+rX /opt/beambot-sim

MCP_IMPL=sdk
python -m venv /opt/beambot-sim/venv
# mcp 1.19.0 is the newest release that resolves to prebuilt armv7 wheels on CPython 3.14.
/opt/beambot-sim/venv/bin/pip install -q --only-binary=:all: "mcp==1.19.0" || MCP_IMPL=stdlib
/opt/beambot-sim/venv/bin/python -c "import mcp.server.lowlevel, uvicorn" 2>/dev/null || MCP_IMPL=stdlib
[[ $MCP_IMPL == sdk ]] && echo "MCP SDK OK" || echo "WARN: MCP SDK unavailable -> using the stdlib MCP server"

cat > /etc/beambot-sim.env <<ENV
ROBOT_ROLE=$ROLE
ROBOT_SIM=$SIM
MCP_IMPL=$MCP_IMPL
MCP_HOST=127.0.0.1
MCP_PORT=8000
ESTOP_PORT=47474
ESTOP_PEERS=$PEERS
MAX_MOTION_MS=2000
LOG_LEVEL=INFO
ENV
install -m 0644 "$SRC_DIR/systemd/beambot-sim.service" /etc/systemd/system/beambot-sim.service
systemctl daemon-reload
systemctl enable --now beambot-sim
systemctl restart beambot-sim

# ---------------------------------------------------------------- Teleport
say "Installing Teleport $TELEPORT_PKG v$TELEPORT_VERSION (linux-arm)"
if teleport version 2>/dev/null | grep -q "v$TELEPORT_VERSION "; then
  echo "already installed: $(teleport version)"
else
  # Work on the SD card (/var/tmp), never /tmp: /tmp is a RAM tmpfs on Arch.
  TAR="${TELEPORT_PKG}-v${TELEPORT_VERSION}-linux-arm-bin.tar.gz"
  WORK=$(mktemp -d -p /var/tmp teleport-install.XXXXXX)
  curl -fsSL -o "$WORK/$TAR" "https://cdn.teleport.dev/${TAR}"
  curl -fsSL -o "$WORK/$TAR.sha256" "https://cdn.teleport.dev/${TAR}.sha256"
  ( cd "$WORK" && sha256sum --check "${TAR}.sha256" )
  tar -xzf "$WORK/$TAR" -C "$WORK"
  ( cd "$WORK/$TELEPORT_PKG" && ./install )
  rm -rf "$WORK"
  teleport version
fi

if [[ $RESET -eq 1 ]]; then
  systemctl stop teleport 2>/dev/null || true
  rm -rf /var/lib/teleport
fi
mkdir -p /var/lib/teleport

if [[ -n "$TOKEN" ]]; then
  umask 077
  printf '%s' "$TOKEN" > /var/lib/teleport/join-token
  umask 022
elif [[ ! -f /var/lib/teleport/join-token ]]; then
  die "--token is required for the first join"
fi

HW=$([[ $SIM -eq 1 ]] && echo sim || echo real)
sed -e "s|__NAME__|$NAME|g" -e "s|__PROXY__|$PROXY|g" -e "s|__ROBOT__|$ROBOT|g" \
    -e "s|__ROLE__|$ROLE|g" -e "s|__HW__|$HW|g" \
    "$SRC_DIR/teleport.yaml.tmpl" > /etc/teleport.yaml
chmod 0600 /etc/teleport.yaml

if [[ ! -f /etc/systemd/system/teleport.service ]]; then
  teleport install systemd -o /etc/systemd/system/teleport.service
fi
systemctl daemon-reload
systemctl enable teleport
systemctl restart teleport

# ---------------------------------------------------------------- checks
say "Health checks"
sleep 3
curl -fsS --retry 5 --retry-connrefused --retry-delay 2 http://127.0.0.1:8000/healthz && echo
systemctl is-active beambot-sim teleport || true
journalctl -u teleport -n 15 --no-pager || true
echo
echo "Node '$NAME' done. From your laptop:  tsh ls robot=$ROBOT  and  tsh mcp ls robot=$ROBOT"
[[ $REBOOT_NEEDED -eq 1 ]] && echo "NOTE: I2C was just enabled in $CONFIG_TXT -> reboot before using real hardware."
# The staged kit is no longer needed (robot_mcp lives in /opt/beambot-sim now).
case "$SRC_DIR" in /var/tmp/beambot-sim*) rm -rf "$SRC_DIR" ;; esac
exit 0
