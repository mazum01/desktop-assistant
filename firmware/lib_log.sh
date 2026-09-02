#!/usr/bin/env bash
# Shared step/verbosity logging helpers for the ESP32 firmware scripts.
#
# Source this, don't execute it:
#     source "$(dirname "${BASH_SOURCE[0]}")/lib_log.sh"
#
# Set VERA_VERBOSE=1 to also echo every external command before it runs and
# to pass -v through to arduino-cli.

VERA_VERBOSE="${VERA_VERBOSE:-0}"

if [[ -t 1 ]]; then
    _C_STEP=$'\033[1;36m'; _C_OK=$'\033[1;32m'; _C_WARN=$'\033[1;33m'
    _C_ERR=$'\033[1;31m';  _C_DIM=$'\033[2m';   _C_OFF=$'\033[0m'
else
    _C_STEP=''; _C_OK=''; _C_WARN=''; _C_ERR=''; _C_DIM=''; _C_OFF=''
fi

_SCRIPT_START_TS="$(date +%s)"
_STEP_NUM=0
_STEP_START_TS=0

_elapsed() {
    local now; now="$(date +%s)"
    printf '%dm%02ds' $(( (now - _SCRIPT_START_TS) / 60 )) $(( (now - _SCRIPT_START_TS) % 60 ))
}

# step "Description" — announce a new phase.
step() {
    _STEP_NUM=$(( _STEP_NUM + 1 ))
    _STEP_START_TS="$(date +%s)"
    printf '%s==> [%d] %s%s %s(t+%s)%s\n' \
        "$_C_STEP" "$_STEP_NUM" "$*" "$_C_OFF" "$_C_DIM" "$(_elapsed)" "$_C_OFF"
}

# step_done [note] — close out the current phase with its duration.
step_done() {
    local now dur
    now="$(date +%s)"
    dur=$(( now - _STEP_START_TS ))
    printf '%s    done%s%s %s(%ds, t+%s)%s\n' \
        "$_C_OK" "${1:+ — $1}" "$_C_OFF" "$_C_DIM" "$dur" "$(_elapsed)" "$_C_OFF"
}

info()  { printf '    %s\n' "$*"; }
warn()  { printf '%s    warning: %s%s\n' "$_C_WARN" "$*" "$_C_OFF" >&2; }
fatal() { printf '%s    error: %s%s\n'   "$_C_ERR"  "$*" "$_C_OFF" >&2; exit 1; }

# vrun <cmd...> — run a command, echoing it first when VERA_VERBOSE=1.
vrun() {
    if [[ "$VERA_VERBOSE" == "1" ]]; then
        printf '%s    $ %s%s\n' "$_C_DIM" "$*" "$_C_OFF"
    fi
    "$@"
}

# Extra flags to hand to arduino-cli when verbose.
cli_verbose_flags() {
    if [[ "$VERA_VERBOSE" == "1" ]]; then
        printf '%s' '-v'
    fi
}
