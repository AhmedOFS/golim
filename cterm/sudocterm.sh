#!/bin/bash
set -e

SUDOERS_FILE="/etc/sudoers.d/cterm"
WRAPPER="/usr/lib/cterm/cterm-privileged"

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

# ── Args ──────────────────────────────────────────────────────────────────────
case "${1:-install}" in
  install)
    echo "Setting up cterm dev environment for user: $REAL_USER"
    echo

    # 1. Install the privileged wrapper script
    mkdir -p "$(dirname "$WRAPPER")"
    cat > "$WRAPPER" << 'EOF'
#!/bin/bash
# cterm privileged wrapper - called only by cterm_server
# Whitelists which binaries can actually be executed
ALLOWED=( /usr/bin/apt /usr/bin/apt-get /usr/bin/tee /usr/bin/snap )

if [ "$#" -lt 1 ]; then
  echo "Usage: cterm-privileged <binary> [args...]" >&2
  exit 1
fi

BINARY="$1"
shift

for allowed in "${ALLOWED[@]}"; do
  if [ "$BINARY" = "$allowed" ]; then
    exec "$BINARY" "$@"
  fi
done

echo "cterm-privileged: binary not allowed: $BINARY" >&2
exit 1
EOF
    chmod 0755 "$WRAPPER"
    chown root:root "$WRAPPER"
    ok "Installed wrapper at $WRAPPER."

    # 2. Sudoers fragment — scoped to the wrapper only, not to snap/apt directly
    #    This means: sudo snap in a normal terminal still asks for a password
    cat > "$SUDOERS_FILE" << EOF
# cterm MCP server - restricted privileged commands
# Managed by dev_setup.sh - do not edit manually
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
    echo "  Run: python3 cterm_server.py"
    echo "────────────────────────────────────────────"
    ;;

  uninstall)
    echo "Cleaning up cterm dev environment..."
    echo

    if [ -f "$SUDOERS_FILE" ]; then
      rm -f "$SUDOERS_FILE"
      ok "Removed $SUDOERS_FILE."
    else
      warn "Sudoers fragment not found, skipping."
    fi

    if [ -f "$WRAPPER" ]; then
      rm -f "$WRAPPER"
      ok "Removed $WRAPPER."
    else
      warn "Wrapper not found, skipping."
    fi

    ok "Cleanup complete."
    ;;

  *)
    echo "Usage: sudo bash $0 [install|uninstall]"
    exit 1
    ;;
esac