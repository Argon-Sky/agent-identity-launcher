# AGENTS.md

Guidance for coding agents working in this repository. `CLAUDE.md` is a symlink to this file.

## What this is

Agent Identity Launcher runs a coding agent (Claude Code, Codex, OpenCode, …) under its own GitHub App identity: commits are authored by the app's bot, and `git` and `gh` authenticate with short-lived installation tokens instead of the user's own credentials. See `README.md` for how it works and how it's set up.

## Layout

| Path | Purpose |
|---|---|
| `bin/argon-agent` | The whole tool, one bash script. It acts on the name it's invoked as: `argon-agent` (subcommands), `<prefix><agent>` launchers, and `gh` (the shim). Inside a session it is also git's credential helper. |
| `install.sh` | Links the script into `~/.local/bin`, the `gh` shim into `~/.local/share/argon-agents/shims`, and one launcher per `~/.config/argon-agents/<agent>.env`. |
| `examples/agent.env` | The config template. |
| `docs/` | README images. |
| `.github/workflows/shellcheck.yml` | CI: shellcheck on both scripts. |

Agent configs live in `~/.config/argon-agents`, outside the repository. Never add real configs, App IDs, key references or keys to the repository.

## Conventions

- Stay compatible with bash 3.2 (the macOS default): no associative arrays, no `mapfile`, no `${var,,}`, and no expanding an empty array under `set -u`.
- Both scripts run with `set -euo pipefail`. Report failures through `die` with a clear `argon-agent:` message instead of letting a raw command error escape.
- Keep `shellcheck bin/argon-agent install.sh` clean; CI enforces it.
- `usage()` prints the script's header comment by line range (`sed -n '2,13…'`). Update the range whenever the header changes length.
- Don't wrap lines to a fixed width, in code or Markdown.
- Keep README claims accurate and checkable; this is what people read before trusting the tool with their GitHub access.

## Testing

- Run `shellcheck bin/argon-agent install.sh`.
- Test `install.sh` against a throwaway home, passed through `env` so nothing expands against the real one: `env HOME="$tmp" XDG_CONFIG_HOME= ./install.sh`. Never write `export HOME=… X=$HOME/…` on one line: `$HOME` expands before the export and the install hits the real home.
- `argon-agent check <agent>` tests a real identity end to end. It needs the maintainer's 1Password approval, so ask instead of retrying when it times out.
- Never print, log or paste installation tokens (`argon-agent token`), JWTs or private keys.

## Commits and pull requests

- This is a public repository: everything pushed is published.
- `main` is protected. Work on a branch and open a pull request; it needs the code owner's approval and a passing `shellcheck` check.
- Commit subjects are imperative, sentence case, without a trailing period, e.g. "Harden the launcher's error paths and token cache". One logical change per commit.
- Commits are authored by the agent's own bot identity through the launcher. Don't add `Co-Authored-By` trailers or "Generated with …" footers.
- Never rewrite published history.
