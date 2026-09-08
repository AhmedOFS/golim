#!/bin/bash
set -u

# Debian removes package-owned files itself. This script removes the
# privileged/systemd integration created by postinstall.sh as well as the
# user's Golim state created for this installation.
SUDOERS_FILE="/etc/sudoers.d/golim"
WRAPPER="/usr/lib/golim/golim-privileged"
AUTHD="/usr/lib/golim/golim-authd"
AUTHD_SERVICE="/usr/lib/systemd/system/golim-authd.service"
SERVICE="/usr/lib/systemd/user/golim-tools.service"

# dpkg calls this script with an action argument. During ``upgrade`` the
# old package's postrm runs while the new package is unpacked but not yet
# configured; the destructive cleanup must only happen on real removal.
if [ "${1:-}" = "upgrade" ] || [ "${1:-}" = "install" ] || [ "${1:-}" = "abort-install" ]; then
  exit 0
fi

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo -e "${RED}✗${NC} postrm must run as root." >&2
  exit 1
fi

REAL_USER="${SUDO_USER:-${PKEXEC_USER:-}}"
if [ -n "$REAL_USER" ] && [ "$REAL_USER" != "root" ]; then
  REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"
  USER_UID="$(id -u "$REAL_USER")"
  if command -v runuser >/dev/null 2>&1 && command -v systemctl >/dev/null 2>&1; then
    runuser -u "$REAL_USER" -- env XDG_RUNTIME_DIR="/run/user/$USER_UID" \
      systemctl --user disable --now golim-tools.service >/dev/null 2>&1 || true
    runuser -u "$REAL_USER" -- env XDG_RUNTIME_DIR="/run/user/$USER_UID" \
      systemctl --user daemon-reload >/dev/null 2>&1 || true
  fi

  if [ -n "$REAL_HOME" ] && [ -d "$REAL_HOME/.golim" ]; then
    rm -rf -- "$REAL_HOME/.golim"
    ok "Removed Golim user state from $REAL_HOME/.golim."
  fi
else
  warn "Could not identify the installing user; user Golim state was left untouched."
fi

if command -v systemctl >/dev/null 2>&1; then
  systemctl disable --now golim-authd.service >/dev/null 2>&1 || true
  systemctl daemon-reload >/dev/null 2>&1 || true
fi

rm -f -- "$SERVICE"
rm -f -- "$WRAPPER"
rm -f -- "$AUTHD"
rm -f -- "$AUTHD_SERVICE"
rm -f -- "$SUDOERS_FILE"
rm -rf -- /run/golim
rmdir --ignore-fail-on-non-empty /usr/lib/golim 2>/dev/null || true
ok "Removed Golim privileged and system integration files."
