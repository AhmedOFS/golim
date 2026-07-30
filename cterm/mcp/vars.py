"""Centralized constants for the cterm MCP package."""

# bash_utils
OUTPUT_LINE_LIMIT = 50
READ_FILE_PAGE_SIZE = 200
PRIVILEGED_WRAPPER = "/usr/lib/cterm/cterm-privileged"
FORBIDDEN_CHARS = set("><`\\'()")
_BLOCKED_BINARIES = {}
_PTY_BINARIES = {"apt", "apt-get", "snap"}
_COMMAND_SEPARATORS = ("&&", "||", "|", ";", "&", "(", ")")

# web_utils
EXA_MCP_URL = "https://mcp.exa.ai/mcp"
PARALLEL_MCP_URL = "https://search.parallel.ai/mcp"
MAX_NUM_RESULTS = 20
MAX_RESPONSE_BYTES = 256 * 1024
NO_RESULTS = "No search results found. Please try a different query."

# server
INACTIVITY_TIMEOUT_SECONDS = 1200
