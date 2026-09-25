#!/usr/bin/env bash
# install.sh — link this repository's launcher and configs into the places argon-agent expects.
#
# Safe to re-run (after cloning on a new Mac, after moving this repository, or after adding an agent).
# Anything already at a target path that isn't the right symlink is moved aside to <path>.bak-<timestamp>.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
BIN="$HOME/.local/bin"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/argon-agents"
SHIMS="$HOME/.local/share/argon-agents/shims"
STAMP=$(date +%Y%m%d-%H%M%S)

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
link "$REPO/config" "$CONFIG"
link "$BIN/argon-agent" "$SHIMS/gh"
for env in "$REPO"/config/*.env; do
  link "$BIN/argon-agent" "$BIN/argon-$(basename "$env" .env)"
done

# Remove launchers whose config is gone (for example after renaming config/<agent>.env).
for launcher in "$BIN"/argon-*; do
  name=$(basename "$launcher")
  [[ $name == argon-agent || ! -L $launcher || $(readlink "$launcher") != "$BIN/argon-agent" ]] && continue
  if [[ ! -f $REPO/config/${name#argon-}.env ]]; then
    rm "$launcher"
    echo "removed $launcher (no config/${name#argon-}.env)"
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
echo "Next: run 'argon-agent check <agent>' for each agent:"
for env in "$REPO"/config/*.env; do
  echo "  argon-agent check $(basename "$env" .env)"
done
