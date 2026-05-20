#!/usr/bin/env bash
# Refresh ANTHROPIC_API_KEY in .env by harvesting CLAUDE_CODE_OAUTH_TOKEN
# from env of live Claude Code processes (same user), verify each against
# the real API, and swap .env only when a working token is found.
# Idempotent and safe: never overwrites with a bad token.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"
LOG_FILE="$PROJECT_DIR/logs/token-refresh.log"

# 兼容 Apple Silicon (/opt/homebrew)、Intel Mac (/usr/local) 和 Linux
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.npm-global/bin:$PATH"

mkdir -p "$(dirname "$LOG_FILE")"
exec >>"$LOG_FILE" 2>&1

echo ""
echo "=== [$(date '+%F %T %Z')] refresh start ==="

CURRENT=$(grep -E '^ANTHROPIC_API_KEY=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)
echo ".env current head: ${CURRENT:0:30}..."

# Harvest tokens from all live Claude Code processes, newest first
TOKENS=$(ps -E -o pid=,lstart=,command= -U "$(id -u)" 2>/dev/null \
  | grep -F "Claude/claude-code" \
  | grep -vF "grep" \
  | awk '{pid=$1; $1=$2=$3=$4=$5=$6=""; print pid, $0}' \
  | while read -r pid rest; do
      tok=$(ps -E -p "$pid" 2>/dev/null | tr ' ' '\n' | grep -E '^CLAUDE_CODE_OAUTH_TOKEN=' | head -1 | cut -d= -f2)
      [ -n "${tok:-}" ] && printf "%s\t%s\n" "$pid" "$tok"
    done \
  | sort -k2 -u \
  | awk -F'\t' '{print $2}')

if [ -z "$TOKENS" ]; then
  echo "FAIL: no CLAUDE_CODE_OAUTH_TOKEN found in any live process"
  exit 1
fi

echo "found tokens (unique): $(echo "$TOKENS" | wc -l | tr -d ' ')"

# Test current .env token first (skip rest if still works)
verify_token() {
  local token="$1"
  local out
  out=$(env -i HOME="$HOME" PATH="$PATH" NO_COLOR=1 ANTHROPIC_API_KEY="$token" \
    claude --output-format json -p "reply with exactly: pong" 2>&1 | head -c 800)
  echo "$out" | grep -q '"is_error":false'
}

if [ -n "$CURRENT" ] && verify_token "$CURRENT"; then
  echo "SKIP: current .env token still valid (idempotent)"
  exit 0
fi

echo "current .env token failed verification, trying harvested tokens..."

WORKING=""
while IFS= read -r tok; do
  [ -z "$tok" ] && continue
  [ "$tok" = "$CURRENT" ] && continue
  echo "trying token ${tok:0:30}..."
  if verify_token "$tok"; then
    WORKING="$tok"
    echo "verify OK"
    break
  else
    echo "verify FAIL, trying next"
  fi
done <<< "$TOKENS"

if [ -z "$WORKING" ]; then
  echo "FAIL: no working token found (all harvested tokens are expired/invalid)"
  echo "HINT: open a fresh Claude Code session, or run \`claude setup-token\` in Terminal"
  exit 1
fi

python3 - <<PY
import re, pathlib
p = pathlib.Path("$ENV_FILE")
content = p.read_text()
new_content = re.sub(r'^ANTHROPIC_API_KEY=.*$',
                     'ANTHROPIC_API_KEY=' + "$WORKING",
                     content, flags=re.MULTILINE, count=1)
if new_content == content:
    new_content = content.rstrip() + "\nANTHROPIC_API_KEY=" + "$WORKING" + "\n"
p.write_text(new_content)
PY
echo ".env updated with working token ${WORKING:0:30}..."

pm2 restart ai-media2doc --update-env 2>&1 | tail -3
echo "=== [$(date '+%F %T %Z')] refresh done ==="
