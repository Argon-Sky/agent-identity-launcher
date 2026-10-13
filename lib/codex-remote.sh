#!/usr/bin/env bash
# Sourced by argon-agent only for opt-in Codex remote mode. Requires bash 3.2, jq and websocat 1.x.

# Find the subcommand without confusing an option value or a prompt with "resume".
codex_resume_requested() {
  while (( $# )); do
    case $1 in
      --) return 1 ;;
      -c|--config|-m|--model|-p|--profile|-s|--sandbox|-a|--ask-for-approval|-C|--cd|--add-dir|--enable|--disable|-i|--image|--local-provider|--remote-auth-token-env)
        (( $# >= 2 )) || return 1
        shift 2 ;;
      -*) shift ;;
      *) [[ $1 == resume ]]; return ;;
    esac
  done
  return 1
}

# A resumed remote thread owns its permissions. Codex rejects CLI permission overrides even when they repeat those permissions, so omit only the configured defaults, never caller arguments.
codex_remote_run() {
  local codex=$1 socket=$2 index=1 arg has_cwd=0
  shift 2
  local args=("$codex" --remote "unix://$socket")
  if codex_resume_requested "${ARGON_COMMAND[@]:1}" "$@"; then
    while (( index < ${#ARGON_COMMAND[@]} )); do
      arg=${ARGON_COMMAND[$index]}
      index=$((index + 1))
      case $arg in
        --yolo|--dangerously-bypass-approvals-and-sandbox|--approve-for-me|--ask-for-approval=*|--sandbox=*|-a?*|-s?*) continue ;;
        -a|--ask-for-approval|-s|--sandbox) index=$((index + 1)); continue ;;
        --)
          args+=("$arg")
          while (( index < ${#ARGON_COMMAND[@]} )); do
            args+=("${ARGON_COMMAND[$index]}")
            index=$((index + 1))
          done
          break ;;
        -c|--config|-m|--model|-p|--profile|-C|--cd|--add-dir|--enable|--disable|-i|--image|--local-provider|--remote-auth-token-env)
          args+=("$arg")
          if (( index < ${#ARGON_COMMAND[@]} )); then
            args+=("${ARGON_COMMAND[$index]}")
            index=$((index + 1))
          fi
          continue ;;
      esac
      args+=("$arg")
    done
  else
    # A local launcher shares the daemon, not its startup directory. Respect explicit cwd flags.
    for arg in "${ARGON_COMMAND[@]:1}" "$@"; do
      [[ $arg == -- ]] && break
      case $arg in
        -C|--cd|--cd=*|-C?*) has_cwd=1 ;;
      esac
    done
    (( has_cwd )) || args+=(--cd "$PWD")
    args+=("${ARGON_COMMAND[@]:1}")
  fi
  exec "${args[@]}" "$@"
}

# Native `codex remote-control` starts an embedded foreground server in 0.162.1.
# Enable the verified daemon itself so the phone and terminal cannot acquire separate thread writers.
codex_remote_manage() {
  local codex=$1 action="" json=0 arg status
  shift
  if [[ ${1:-} == remote-control ]]; then
    shift
    for arg in "$@"; do
      case $arg in
        --json) json=1 ;;
        --help|-h) exec "$codex" remote-control "$@" ;;
        start|pair|stop)
          [[ -z $action ]] || die "choose one remote-control action: start, pair or stop"
          action=$arg ;;
        *) die "unsupported remote-control argument: $arg. Use remote-control [start|pair|stop] [--json]; configure server settings in $CODEX_HOME/config.toml" ;;
      esac
    done
    action=${action:-start}
    if [[ $action == start ]]; then
      status=$(codex_remote_socket "$codex" 0 status) || exit 1
      if (( json )); then
        jq -c '. + {mode:"daemon"}' <<< "$status"
      else
        printf 'Codex remote control: %s (%s, shared bot daemon)\n' "$(jq -r .status <<< "$status")" "$(jq -r '.serverName // "Codex"' <<< "$status")"
      fi
      exit 0
    fi
    set -- remote-control "$action"
    (( json == 0 )) || set -- "$@" --json
  else
    # Do not let prefixed CLI flags reach the native foreground-server path.
    for arg in "$@"; do
      [[ $arg != remote-control ]] || die "put remote-control first, followed by start, pair or stop and optional --json; configure server settings in $CODEX_HOME/config.toml"
    done
  fi
  # Keep recovery available even when an existing daemon has the wrong identity.
  case ${1:-}/${2:-}/${3:-} in
    login/*/*|logout/*/*|help/*/*|--help/*/*|-h/*/*|app-server/daemon/version|app-server/daemon/stop|app-server/daemon/disable-remote-control|remote-control/stop/*) ;;
    *) codex_remote_socket "$codex" 1 > /dev/null || exit 1 ;;
  esac
  exec "$codex" "$@"
}

# Reuse Codex's lifecycle lock and websocat's WebSocket-over-Unix transport. Never restart an existing server.
# Check the server over the same connection used to enable remote access, before attaching the TUI.
codex_remote_socket() (
  local codex=$1 check_only=${2:-0} output=${3:-socket} lifecycle socket response expected actual probe deadline remaining line
  lifecycle=$("$codex" app-server daemon start) || die "could not start the Codex server"
  socket=$(jq -er '.socketPath | select(type == "string" and startswith("/") and (contains("\n") | not))' <<< "$lifecycle") ||
    die "Codex did not return an absolute server socket path"

  # Bash 3.2 unwinds function locals before an EXIT trap on die. These belong to this subshell only.
  codex_proxy_pid=""
  codex_proxy_dir=$(umask 077; mktemp -d "${TMPDIR:-/tmp}/argon-codex.XXXXXX") || die "could not create the Codex connection directory"
  # shellcheck disable=SC2317,SC2329 # Invoked by the EXIT trap, including die's exit path (older ShellCheck uses SC2317).
  cleanup_codex_proxy() {
    if [[ -n $codex_proxy_pid ]]; then
      kill "$codex_proxy_pid" 2>/dev/null || true
      wait "$codex_proxy_pid" 2>/dev/null || true
    fi
    rm -rf "$codex_proxy_dir"
  }
  trap cleanup_codex_proxy EXIT
  trap 'exit 1' HUP INT TERM
  mkfifo "$codex_proxy_dir/in" "$codex_proxy_dir/out" || die "could not create the Codex connection pipes"
  websocat -t --ws-c-uri ws://localhost/ - "ws-c:unix:$socket" < "$codex_proxy_dir/in" > "$codex_proxy_dir/out" 2> "$codex_proxy_dir/error" &
  codex_proxy_pid=$!
  exec 3> "$codex_proxy_dir/in" 4< "$codex_proxy_dir/out"

  # Bound the entire handshake, including unrelated notifications, rather than each individual read.
  deadline=$((SECONDS + 20))
  codex_rpc() {
    local id=$1 method=$2 params=$3
    jq -cn --argjson id "$id" --arg method "$method" --argjson params "$params" '{id:$id,method:$method,params:$params}' >&3 ||
      die "could not send a Codex server request"
    while :; do
      remaining=$((deadline - SECONDS))
      (( remaining > 0 )) || die "Codex server handshake timed out"
      IFS= read -r -t "$remaining" line <&4 || die "Codex server disconnected or the handshake timed out"
      jq -e 'type == "object"' > /dev/null 2>&1 <<< "$line" || die "Codex server returned invalid JSON"
      if jq -e --argjson id "$id" '.id == $id' > /dev/null <<< "$line"; then
        jq -e 'has("error") | not' > /dev/null <<< "$line" || die "Codex server rejected $method; check its version, settings and ChatGPT login"
        response=$(jq -ce '.result | select(type == "object")' <<< "$line") || die "Codex server returned an invalid $method response"
        return
      fi
    done
  }

  codex_rpc 1 initialize '{"clientInfo":{"name":"argon-agent","version":"1"},"capabilities":{"experimentalApi":true}}'
  printf '%s\n' '{"method":"initialized"}' >&3

  # Only non-secret identity fields leave the server. GitHub token variables are tested for absence, never read out.
  # The 1Password service account token leaves only as a SHA-256 fingerprint, so a server that predates the
  # token, or a replaced one, is refused. Use the same jq and openssl for both probes so a different server PATH cannot substitute them.
  # shellcheck disable=SC2016 # This script is expanded in the probed shell, not the launcher.
  # A failed digest fails the probe, so a missing openssl cannot make two different tokens compare equal.
  probe='op=""; if [ -n "${OP_SERVICE_ACCOUNT_TOKEN+set}" ]; then op=$(printf %s "$OP_SERVICE_ACCOUNT_TOKEN" | "$2" dgst -sha256 -r) || exit 1; fi; "$1" -cn --arg gh "$(command -v gh)" --arg op "$op" '\''{identity:(env | with_entries(select(.key | test("^(ARGON_AGENT|ARGON_REAL_GH|GIT_AUTHOR_NAME|GIT_AUTHOR_EMAIL|GIT_COMMITTER_NAME|GIT_COMMITTER_EMAIL|GIT_CONFIG_COUNT|GIT_CONFIG_KEY_[0-2]|GIT_CONFIG_VALUE_[0-2]|GH_CONFIG_DIR)$")))),gh:$gh,opToken:$op,noTokens:((env | has("GH_TOKEN") or has("GITHUB_TOKEN")) | not)}'\'''
  expected=$(/bin/sh -c "$probe" argon-probe "$(command -v jq)" "$(command -v openssl)") || die "could not inspect the launcher identity"
  codex_rpc 2 command/exec "$(jq -cn --arg probe "$probe" --arg jq "$(command -v jq)" --arg openssl "$(command -v openssl)" '{command:["/bin/sh","-c",$probe,"argon-probe",$jq,$openssl],sandboxPolicy:{type:"dangerFullAccess"},timeoutMs:5000,outputBytesCap:16384}')"
  actual=$(jq -ce 'select(.exitCode == 0) | .stdout | fromjson' <<< "$response") || die "could not inspect the Codex server identity"
  # Codex hands commands the server's environment filtered by its shell_environment_policy, which can drop *TOKEN* variables.
  jq -en --argjson expected "$expected" --argjson actual "$actual" '($expected | del(.opToken)) != ($actual | del(.opToken)) or $expected.opToken == $actual.opToken' > /dev/null ||
    die "the bot Codex server's commands do not get the 1Password service account token this launch has: the server started before op.token or ARGON_OP_SESSION_TOKEN changed, or shell_environment_policy in $CODEX_HOME/config.toml drops it. Finish its active work, stop it yourself with 'argon-agent codex $AGENT app-server daemon stop', then launch again. No server was stopped or remote access enabled by this launch"
  jq -en --argjson expected "$expected" --argjson actual "$actual" '$expected == $actual and $actual.noTokens' > /dev/null ||
    die "the bot Codex server has a different GitHub identity or credential environment. Finish its active work, stop it yourself with 'argon-agent codex $AGENT app-server daemon stop', then launch again. No server was stopped or remote access enabled by this launch"

  if [[ $check_only == 0 ]]; then
    codex_rpc 3 remoteControl/enable '{"ephemeral":true}'
    # The native CLI also accepts 'connecting': the host may reconnect once network access returns.
    jq -e '.status == "connected" or .status == "connecting"' > /dev/null <<< "$response" ||
      die "Codex could not enable remote control; check the server's ChatGPT login and remote-control permissions"
  fi
  if [[ $output == status ]]; then
    printf '%s\n' "$response"
  else
    printf '%s\n' "$socket"
  fi
)
