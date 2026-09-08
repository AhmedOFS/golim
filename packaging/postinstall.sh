#!/bin/bash
set -e

SUDOERS_FILE="/etc/sudoers.d/golim"
WRAPPER="/usr/lib/golim/golim-privileged"
AUTHD="/usr/lib/golim/golim-authd"
AUTHD_SERVICE="/usr/lib/systemd/system/golim-authd.service"
DEFAULT_ALLOWED=( /usr/bin/apt /usr/bin/apt-get /usr/bin/tee /usr/bin/snap )

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
die()  { echo -e "${RED}✗${NC} $*" >&2; exit 1; }

# ── Must run as root ──────────────────────────────────────────────────────────
[ "$EUID" -eq 0 ] || die "Run with sudo: sudo bash $0"

# ── Who actually invoked sudo ─────────────────────────────────────────────────
REAL_USER="${SUDO_USER:-$USER}"
[ "$REAL_USER" = "root" ] && die "Could not determine the real user. Run via sudo, not as root directly."
REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"
[ -n "$REAL_HOME" ] || die "Could not determine home directory for $REAL_USER."
# The privileged whitelist belongs to the user's Golim home, not the XDG
# config directory. Keep this path aligned with golim/toolset/config.py.
GOLIM_HOME="$REAL_HOME/.golim"
WHITELIST="$GOLIM_HOME/privileged_whitelist"

# ── Install ───────────────────────────────────────────────────────────────────
echo "Setting up golim for user: $REAL_USER"
echo

# 1. Create the initial user-owned privileged command whitelist.
install -d -m 0755 -o "$REAL_USER" -g "$REAL_USER" "$GOLIM_HOME"
: > "$WHITELIST"
for binary in "${DEFAULT_ALLOWED[@]}"; do
  if [ -x "$binary" ]; then
    readlink -f "$binary" >> "$WHITELIST"
  fi
done
sort -u -o "$WHITELIST" "$WHITELIST"
chown "$REAL_USER:$REAL_USER" "$WHITELIST"
chmod 0644 "$WHITELIST"
ok "Installed privileged whitelist at $WHITELIST."

# 2. Install the privileged wrapper script.
#    The wrapper runs as root via a NOPASSWD sudoers rule, so it must
#    verify the caller's session token with golim-authd before
#    executing anything, and fail closed whenever golim-authd cannot
#    confirm it.
cat > "$WRAPPER" << 'EOF'
#!/usr/bin/python3
"""golim privileged wrapper.

Executed as root through a NOPASSWD sudoers rule, so the sudo password
gate does not apply: access control lives entirely in the session token
verified by golim-authd. The token travels in the environment
(GOLIM_SESSION_TOKEN) set by the MCP tool server for privileged
invocations only. Any failure to verify the token is fatal (fail closed).
"""

import json
import os
import pwd
import socket
import sys

AUTHD_SOCKET = "/run/golim/authd.sock"
TOKEN_ENV_VAR = "GOLIM_SESSION_TOKEN"
AUTHD_TIMEOUT_SECONDS = 5


def fail(message):
    print(f"golim-privileged: {message}", file=sys.stderr)
    sys.exit(1)


def verify_token_with_authd(user, token):
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(AUTHD_TIMEOUT_SECONDS)
        sock.connect(AUTHD_SOCKET)
        try:
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "verify",
                "params": {"user": user, "token": token},
            }
            sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    return False
                buf += chunk
            reply = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
            result = reply.get("result") or {}
            return bool(result.get("ok") and result.get("valid"))
        finally:
            sock.close()
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return False


def main():
    if len(sys.argv) < 2:
        print("Usage: golim-privileged <binary> [args...]", file=sys.stderr)
        sys.exit(1)

    real_user = os.environ.get("SUDO_USER", "")
    if not real_user or real_user == "root":
        fail("could not determine invoking user")

    token = os.environ.get(TOKEN_ENV_VAR, "")
    if not token:
        fail("no session token provided")

    if not verify_token_with_authd(real_user, token):
        fail("session token not verified by golim-authd")

    # Canonicalize for the whitelist check but exec the path as invoked:
    # symlink-dispatched multiplexers (kmod applets such as modprobe -> kmod)
    # pick their behavior from argv[0].
    requested = sys.argv[1]
    args = sys.argv[2:]
    binary = os.path.realpath(requested)

    whitelist = os.environ.get(
        "GOLIM_PRIVILEGED_WHITELIST",
        os.path.join(pwd.getpwnam(real_user).pw_dir, ".golim/privileged_whitelist"),
    )
    try:
        with open(whitelist, "r", encoding="utf-8") as handle:
            entries = handle.read().splitlines()
    except OSError:
        fail(f"whitelist not readable: {whitelist}")

    for entry in entries:
        entry = entry.split("#", 1)[0].strip()
        if not entry:
            continue
        if binary == os.path.realpath(entry):
            # Drop the token from the child's environment before exec.
            os.environ.pop(TOKEN_ENV_VAR, None)
            if "/" in requested:
                os.execv(requested, [requested] + args)
            os.execv(binary, [binary] + args)

    fail(f"binary not allowed: {binary}")


if __name__ == "__main__":
    main()
EOF
chmod 0755 "$WRAPPER"
chown root:root "$WRAPPER"
ok "Installed wrapper at $WRAPPER."

# 3. golim-authd and its system service are package files installed
#    by dpkg (/usr/lib/golim/golim-authd and the systemd unit); here
#    they are enabled and started.
if [ -f "$AUTHD" ] && [ -f "$AUTHD_SERVICE" ]; then
  chown root:root "$AUTHD"
  chmod 0755 "$AUTHD"
  chown root:root "$AUTHD_SERVICE"
  chmod 0644 "$AUTHD_SERVICE"
  if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl enable --now golim-authd.service >/dev/null 2>&1 || \
      warn "Could not start golim-authd.service automatically."
  fi
  ok "golim-authd installed at $AUTHD."
else
  die "golim-authd files missing from the package installation."
fi

# 4. Sudoers fragment — NOPASSWD on the wrapper only, with the session
#    token forwarded to it. The wrapper independently verifies the token
#    with golim-authd, so the NOPASSWD rule grants nothing on its own.
#    This means: sudo snap in a normal terminal still asks for a password.
cat > "$SUDOERS_FILE" << EOF
# golim MCP server - restricted privileged commands
# Managed by packaging/postinstall.sh - do not edit manually
Defaults!$WRAPPER env_keep += "GOLIM_SESSION_TOKEN"
$REAL_USER ALL=(root) NOPASSWD: $WRAPPER
EOF

if visudo -c -f "$SUDOERS_FILE"; then
  chmod 0440 "$SUDOERS_FILE"
  chown root:root "$SUDOERS_FILE"
  ok "Sudoers fragment installed at $SUDOERS_FILE."
else
  rm -f "$SUDOERS_FILE"
  die "Sudoers validation failed. Fragment removed."
fi

echo
echo "────────────────────────────────────────────"
ok "Setup complete for $REAL_USER."
echo "────────────────────────────────────────────"
