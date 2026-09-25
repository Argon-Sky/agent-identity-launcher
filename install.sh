#!/usr/bin/env bash
# install.sh — link this repository's launcher into place and create one launcher per agent config.
#
#   ./install.sh                  create one launcher per ~/.config/argon-agents/<agent>.env: <prefix><agent>, e.g. argon-claude
#   ./install.sh --prefix my-     use a different launcher prefix (remembered for later runs)
#
# Safe to re-run (after cloning on a new Mac, after moving this repository, or after adding an agent).
# Agent configs live outside this repository and are never touched.
# Anything already at a target path that isn't the right symlink is moved aside to <path>.bak-<timestamp>.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
BIN="$HOME/.local/bin"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/argon-agents"
SHARE="$HOME/.local/share/argon-agents"
SHIMS="$SHARE/shims"
STAMP=$(date +%Y%m%d-%H%M%S)

# The launcher prefix: --prefix, else the one used last time, else argon-.
PREFIX=""
[[ -f $SHARE/prefix ]] && PREFIX=$(< "$SHARE/prefix")
case ${1:-} in
  --prefix)   PREFIX=${2:-} ;;
  --prefix=*) PREFIX=${1#--prefix=} ;;
  "")         PREFIX=${PREFIX:-argon-} ;;
  *)          echo "usage: $0 [--prefix <prefix>]" >&2; exit 1 ;;
esac
[[ $PREFIX =~ ^[A-Za-z0-9._-]+$ ]] || { echo "install.sh: the prefix may contain only letters, digits, '.', '_', and '-'" >&2; exit 1; }
mkdir -p "$SHARE"
echo "$PREFIX" > "$SHARE/prefix"

link() {  # link <target> <path>
  local target=$1 path=$2
  if [[ -L $path && $(readlink "$path") == "$target" ]]; then
    echo "ok      $path"
    return
  fi
  if [[ -e $path || -L $path ]]; then
    mv "$path" "$path.bak-$STAMP"
    echo "moved   $path → $path.bak-$STAMP"
  fi
  mkdir -p "$(dirname "$path")"
  ln -s "$target" "$path"
  echo "linked  $path → $target"
}

chmod +x "$REPO/bin/argon-agent"
link "$REPO/bin/argon-agent" "$BIN/argon-agent"
mkdir -p "$CONFIG"
link "$BIN/argon-agent" "$SHIMS/gh"
shopt -s nullglob
for env in "$CONFIG"/*.env; do
  link "$BIN/argon-agent" "$BIN/$PREFIX$(basename "$env" .env)"
done

# Remove launchers that no longer match a config (after renaming <agent>.env or changing the prefix).
for launcher in "$BIN"/*; do
  name=$(basename "$launcher")
  [[ $name == argon-agent || ! -L $launcher || $(readlink "$launcher") != "$BIN/argon-agent" ]] && continue
  if [[ $name != "$PREFIX"* || ! -f $CONFIG/${name#"$PREFIX"}.env ]]; then
    rm "$launcher"
    echo "removed $launcher (not $PREFIX<agent> for any $CONFIG/<agent>.env)"
  fi
done

echo
echo "Prerequisites:"
for tool in git curl openssl jq gh op; do
  if command -v "$tool" > /dev/null; then
    printf '  %-8s %s\n' "$tool" "$(command -v "$tool")"
  else
    printf '  %-8s MISSING\n' "$tool"
  fi
done
openssl version 2> /dev/null | grep -q '^OpenSSL 3' || echo "  warning: openssl is not OpenSSL 3 (tested with Homebrew openssl@3; macOS LibreSSL is untested)"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "  warning: $BIN is not on PATH" ;;
esac
# `op whoami` fails until a command is authorized in the session, so check for a connected account instead.
[[ $(op account list --format=json 2> /dev/null | jq length 2> /dev/null) -gt 0 ]] 2> /dev/null ||
  echo "  warning: 1Password CLI has no account; enable 1Password → Settings → Developer → Integrate with 1Password CLI (see README)"

echo
envs=("$CONFIG"/*.env)
if (( ${#envs[@]} == 0 )); then
  echo "No agents configured yet. Add one, then re-run ./install.sh:"
  echo "  cp examples/agent.env $CONFIG/claude.env"
  exit 0
fi
echo "Launchers: ${PREFIX}<agent>. Next: run 'argon-agent check <agent>' for each agent:"
for env in "${envs[@]}"; do
  echo "  argon-agent check $(basename "$env" .env)"
done
