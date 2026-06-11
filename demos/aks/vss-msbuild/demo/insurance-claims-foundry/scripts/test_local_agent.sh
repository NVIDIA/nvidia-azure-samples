#!/usr/bin/env bash
set -euo pipefail

DEFAULT_PROMPT="A customer just submitted a damage video. video_id=toyota. If the VIN is not visible, use policy POL-2025-44912 as a fallback. Run the triage workflow."
PORT="${LOCAL_AGENT_PORT:-8088}"
WAIT_TIMEOUT_SECONDS="${LOCAL_AGENT_WAIT_TIMEOUT_SECONDS:-180}"

usage() {
  cat <<'USAGE'
Usage: ./scripts/test_local_agent.sh [live|mock] [prompt...]

Runs a Foundry agent locally with `azd ai agent run`, invokes its local
Responses endpoint, verifies the expected VSS path, and stops the local server
on exit.

Modes:
  live  Default. Runs insurance-claims-triage-vss with MOCK_VSS=false.
  mock  Runs insurance-claims-triage with MOCK_VSS=true.

If no prompt is supplied, the Toyota smoke prompt is used.
USAGE
}

mode="live"
prompt="$DEFAULT_PROMPT"

if [[ $# -gt 0 ]]; then
  case "$1" in
    live|mock)
      mode="$1"
      shift
      if [[ $# -gt 0 ]]; then
        prompt="$*"
      fi
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      prompt="$*"
      ;;
  esac
fi

case "$mode" in
  live)
    agent="${LOCAL_AGENT_NAME:-insurance-claims-triage-vss}"
    mock_value="false"
    ;;
  mock)
    agent="${LOCAL_AGENT_NAME:-insurance-claims-triage}"
    mock_value="true"
    ;;
  *)
    echo "Invalid mode: $mode" >&2
    usage >&2
    exit 2
    ;;
esac

cd "$(dirname "$0")/.."

agent_log="$(mktemp -t foundry-local-agent.XXXXXX.log)"
invoke_log="$(mktemp -t foundry-local-invoke.XXXXXX.log)"
agent_pid=""

cleanup() {
  status=$?
  if [[ -n "${agent_pid:-}" ]] && kill -0 "$agent_pid" 2>/dev/null; then
    kill "$agent_pid" 2>/dev/null || true
    wait "$agent_pid" 2>/dev/null || true
  fi
  port_pids="$(lsof -ti tcp:"$PORT" 2>/dev/null || true)"
  if [[ -n "$port_pids" ]]; then
    kill $port_pids 2>/dev/null || true
  fi
  exit "$status"
}
trap cleanup EXIT

AZD_BIN="${AZD_BIN:-}"
if [[ -z "$AZD_BIN" ]]; then
  if command -v azd >/dev/null 2>&1; then
    AZD_BIN="$(command -v azd)"
  elif command -v zsh >/dev/null 2>&1; then
    AZD_BIN="$(zsh -lic 'command -v azd' 2>/dev/null || true)"
  elif [[ -x /bin/zsh ]]; then
    AZD_BIN="$(/bin/zsh -lic 'command -v azd' 2>/dev/null || true)"
  fi
fi

if [[ -z "$AZD_BIN" || ! -x "$AZD_BIN" ]]; then
  echo "azd was not found. Install Azure Developer CLI or run with AZD_BIN=/path/to/azd." >&2
  echo "Diagnostic: zsh -lic 'command -v azd'" >&2
  exit 127
fi

if command -v zsh >/dev/null 2>&1; then
  login_zsh_path="$(zsh -lic 'print -r -- $PATH' 2>/dev/null || true)"
elif [[ -x /bin/zsh ]]; then
  login_zsh_path="$(/bin/zsh -lic 'print -r -- $PATH' 2>/dev/null || true)"
else
  login_zsh_path=""
fi

if [[ -n "$login_zsh_path" ]]; then
  export PATH="$login_zsh_path"
fi

azd_env_get() {
  local key="$1"
  "$AZD_BIN" env get-values | awk -F= -v key="$key" '
    $1 == key {
      value = $0
      sub(/^[^=]+=/, "", value)
      gsub(/^"|"$/, "", value)
      print value
      exit
    }
  '
}

agent_project="$("$AZD_BIN" show --output json | tr -d '\n' | sed -n "s/.*\"${agent}\"[^{]*{[^}]*\"project\":\"\\([^\"]*\\)\".*/\\1/p")"
if [[ -z "$agent_project" ]]; then
  case "$agent" in
    insurance-claims-triage) agent_project="src/agent-framework-agent-basic-responses" ;;
    insurance-claims-triage-vss) agent_project="src/agent-framework-agent-vss-responses" ;;
  esac
fi

vss_base="$(azd_env_get VSS_BASE_URL || true)"
if [[ -z "$vss_base" && -f "$agent_project/agent.yaml" ]]; then
  vss_base="$(awk '
    $0 ~ /name:[[:space:]]*VSS_BASE_URL/ {
      getline
      sub(/^[[:space:]]*value:[[:space:]]*/, "", $0)
      gsub(/^"|"$/, "", $0)
      print
      exit
    }
  ' "$agent_project/agent.yaml")"
fi
vss_base="${vss_base:-${VSS_BASE_URL:-}}"
if [[ "$mode" == "live" && -z "$vss_base" ]]; then
  echo "Set VSS_BASE_URL or pass --vss-base-url for live VSS tests." >&2
  exit 2
fi
vss_base="${vss_base%/}"

echo "Mode:        $mode"
echo "Agent:       $agent"
echo "Prompt:      $prompt"
echo "MOCK_VSS:    $mock_value"
echo "VSS_BASE_URL $vss_base"
echo "Agent log:   $agent_log"
echo "Invoke log:  $invoke_log"
echo

"$AZD_BIN" env set MOCK_VSS "$mock_value" >/dev/null
"$AZD_BIN" env get-values | grep '^MOCK_VSS' || true

if [[ "$mode" == "mock" ]]; then
  shopt -s nullglob
  fixtures=("$agent_project"/fixtures/vss/*.txt)
  if (( ${#fixtures[@]} == 0 )); then
    echo "No mock fixtures found under $agent_project/fixtures/vss" >&2
    exit 1
  fi
  if [[ "$prompt" =~ video_id=([^[:space:].,;]+) ]]; then
    video_id="${BASH_REMATCH[1]}"
    if [[ ! -f "$agent_project/fixtures/vss/$video_id.txt" ]]; then
      echo "Warning: no fixture for video_id=$video_id under $agent_project/fixtures/vss; mock fallback will be used." >&2
    fi
  fi
  shopt -u nullglob
else
  status_code="$(curl -sS -o /dev/null -w "%{http_code}" --max-time 10 "$vss_base/" || true)"
  case "$status_code" in
    2*|3*) ;;
    *)
      echo "VSS endpoint is not reachable enough for live mode: $vss_base/ returned HTTP $status_code" >&2
      exit 1
      ;;
  esac
fi

stale_pids="$(lsof -ti tcp:"$PORT" 2>/dev/null || true)"
if [[ -n "$stale_pids" ]]; then
  echo "Stopping stale local process(es) on port $PORT: $stale_pids"
  kill $stale_pids 2>/dev/null || true
  sleep 1
fi

"$AZD_BIN" ai agent run "$agent" --port "$PORT" >"$agent_log" 2>&1 &
agent_pid=$!

deadline=$((SECONDS + WAIT_TIMEOUT_SECONDS))
until curl -sS -o /dev/null --connect-timeout 1 "http://127.0.0.1:$PORT/" 2>/dev/null; do
  if ! kill -0 "$agent_pid" 2>/dev/null; then
    echo "Local agent exited before binding port $PORT." >&2
    tail -n 120 "$agent_log" >&2 || true
    exit 1
  fi
  if (( SECONDS >= deadline )); then
    echo "Timed out waiting for local agent on port $PORT." >&2
    tail -n 120 "$agent_log" >&2 || true
    exit 1
  fi
  sleep 2
done

prompt_json="$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$prompt")"
curl -sS -X POST "http://127.0.0.1:$PORT/responses" \
  -H "Content-Type: application/json" \
  -d "{\"input\":$prompt_json,\"stream\":false}" \
  >"$invoke_log" 2>&1

if ! grep -q "Claim drafted" "$invoke_log"; then
  echo "Local invoke did not draft a claim." >&2
  cat "$invoke_log" >&2
  exit 1
fi

if ! grep -q "Function name: vss_analyze_video" "$agent_log"; then
  echo "vss_analyze_video was not called." >&2
  tail -n 160 "$agent_log" >&2 || true
  exit 1
fi

duration="$(awk '
  /Function name: vss_analyze_video/ { seen = 1; next }
  seen && /Function duration:/ { print $NF; exit }
' "$agent_log")"

if [[ "$mode" == "live" ]]; then
  if ! grep -Fq "$vss_base/chat/stream" "$agent_log"; then
    echo "Requested live mode, but no VSS HTTP POST was observed in the agent log." >&2
    tail -n 180 "$agent_log" >&2 || true
    exit 1
  fi
  verdict="Live fired OK: POST to $vss_base/chat/stream observed"
else
  if grep -Fq "$vss_base/chat/stream" "$agent_log"; then
    echo "Requested mock mode, but live VSS HTTP was observed in the agent log." >&2
    tail -n 180 "$agent_log" >&2 || true
    exit 1
  fi
  if grep -q "MOCK_VSS=true: vss_analyze_video bypassed VSS HTTP call" "$agent_log"; then
    verdict="Mock fired OK: fixture path logged and no live VSS HTTP observed"
  else
    verdict="Mock fired OK: no live VSS HTTP observed"
  fi
fi

echo
echo "=== Agent response ==="
cat "$invoke_log"
echo
echo "=== Verdict ==="
echo "$verdict"
if [[ -n "$duration" ]]; then
  echo "vss_analyze_video duration: $duration"
fi
echo "MOCK_VSS now persists in azd env as $mock_value."
