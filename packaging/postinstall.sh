#!/bin/bash
set -e

SUDOERS_FILE="/etc/sudoers.d/cterm"
WRAPPER="/usr/lib/cterm/cterm-privileged"
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
# The privileged whitelist belongs to the user's Cterm home, not the XDG
# config directory. Keep this path aligned with cterm/mcp/config.py.
CTERM_HOME="$REAL_HOME/.cterm"
WHITELIST="$CTERM_HOME/privileged_whitelist"

# ── Install ───────────────────────────────────────────────────────────────────
echo "Setting up cterm for user: $REAL_USER"
echo

# 1. Create the initial user-owned privileged command whitelist.
install -d -m 0755 -o "$REAL_USER" -g "$REAL_USER" "$CTERM_HOME"
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

# 2. Install the privileged wrapper script
mkdir -p "$(dirname "$WRAPPER")"
cat > "$WRAPPER" << 'EOF'
#!/bin/bash
# cterm privileged wrapper - called only by cterm_server
# Reads the invoking user's cterm whitelist before executing a binary.
set -e

if [ "$#" -lt 1 ]; then
  echo "Usage: cterm-privileged <binary> [args...]" >&2
  exit 1
fi

BINARY="$(readlink -f "$1")"
shift

REAL_USER="${SUDO_USER:-}"
if [ -z "$REAL_USER" ] || [ "$REAL_USER" = "root" ]; then
  echo "cterm-privileged: could not determine invoking user" >&2
  exit 1
fi

REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"
if [ -z "$REAL_HOME" ]; then
  echo "cterm-privileged: could not determine home for $REAL_USER" >&2
  exit 1
fi

WHITELIST="${CTERM_PRIVILEGED_WHITELIST:-$REAL_HOME/.cterm/privileged_whitelist}"

if [ ! -r "$WHITELIST" ]; then
  echo "cterm-privileged: whitelist not readable: $WHITELIST" >&2
  exit 1
fi

while IFS= read -r allowed || [ -n "$allowed" ]; do
  allowed="${allowed%%#*}"
  allowed="$(echo "$allowed" | xargs)"
  [ -n "$allowed" ] || continue
  allowed="$(readlink -f "$allowed")"
  if [ "$BINARY" = "$allowed" ]; then
    exec "$BINARY" "$@"
  fi
done < "$WHITELIST"

echo "cterm-privileged: binary not allowed: $BINARY" >&2
exit 1
EOF
chmod 0755 "$WRAPPER"
chown root:root "$WRAPPER"
ok "Installed wrapper at $WRAPPER."

# 3. Sudoers fragment — scoped to the wrapper only, not to snap/apt directly
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
echo "────────────────────────────────────────────"
