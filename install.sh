#!/bin/sh
# Installer for cm (claude-launcher): macOS and Linux only. Windows is not supported.
#
#   curl -fsSL https://raw.githubusercontent.com/vams-mx/claude-launcher/main/install.sh | sh
#   sh install.sh                      # from a checkout: installs that checkout
#
# Safe to re-run: it upgrades cm and never touches your data in ~/.claude-launcher
# (config.json, profiles/, mcps/, caches, sessions/). New machine: README "Reinstall / new machine".
#
# Environment (all optional):
#   CM_REPO=vams-mx/claude-launcher GitHub repo to install from (curl mode; a fork works too)
#   CM_REF=main                     branch, tag or commit (curl mode)
#   CM_SOURCE=<path or URL>         install exactly this (overrides the two above)
#   CM_EDITABLE=1                   from a checkout: editable install (for development)
#   INSTALL_AGENT_MEMORY=0|1        AgentMemory step: 0 = skip, 1 = run (unset = ask once)
#   CM_CONFIG_DIR=<dir>             cm's home (default ~/.claude-launcher)
#
# The whole script is wrapped in main() so a partially downloaded file never runs.

set -eu

DEFAULT_REPO="vams-mx/claude-launcher"

say() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

check_os() {
    os=$(uname -s 2>/dev/null || echo unknown)
    case "$os" in
        Darwin) OS=macos ;;
        Linux) OS=linux ;;
        *) die "unsupported operating system '$os': cm supports macOS and Linux only (Windows is not supported)" ;;
    esac
}

find_checkout() {
    # Run as a file (sh install.sh / ./install.sh)? Then $0 is its path; piped from curl it is just "sh".
    CHECKOUT=""
    case "$0" in
        */install.sh | install.sh)
            dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd) || return 0
            if [ -f "$dir/pyproject.toml" ] && grep -q '^name = "claude-launcher"' "$dir/pyproject.toml"; then
                CHECKOUT=$dir
            fi
            ;;
    esac
}

find_uv() {
    UV=""
    if command -v uv >/dev/null 2>&1; then
        UV=$(command -v uv)
        return 0
    fi
    for c in "${XDG_BIN_HOME:-}/uv" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -x "$c" ]; then
            UV=$c
            return 0
        fi
    done
}

install_uv() {
    find_uv
    if [ -n "$UV" ]; then
        say "uv: $UV"
        return 0
    fi
    say "uv not found; installing it with the official installer (https://docs.astral.sh/uv/)"
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        die "need curl or wget to install uv (or install uv yourself: https://docs.astral.sh/uv/)"
    fi
    find_uv
    [ -n "$UV" ] || die "uv was installed but cannot be found; open a new shell and re-run this script"
    say "uv: $UV"
}

install_cm() {
    if [ -n "${CM_SOURCE:-}" ]; then
        src=$CM_SOURCE
    elif [ -n "$CHECKOUT" ]; then
        src=$CHECKOUT
    else
        repo=${CM_REPO:-$DEFAULT_REPO}
        src="git+https://github.com/$repo@${CM_REF:-main}"
    fi
    say "installing cm from $src"
    if [ "${CM_EDITABLE:-0}" = 1 ] && [ -d "$src" ]; then
        "$UV" tool install --force --editable "$src"
    else
        # --force replaces an existing install, --reinstall-package rebuilds cm itself: a re-run upgrades.
        "$UV" tool install --force --reinstall-package claude-launcher "$src"
    fi
    bindir=$("$UV" tool dir --bin)
    CM="$bindir/cm"
    [ -x "$CM" ] || die "cm was not installed into $bindir"
    case ":$PATH:" in
        *":$bindir:"*) ;;
        *) warn "$bindir is not on your PATH; run \`$UV tool update-shell\` (or add it yourself) and open a new shell" ;;
    esac
}

check_claude() {
    if command -v claude >/dev/null 2>&1; then
        say "claude: $(command -v claude)"
    else
        warn "Claude Code (\`claude\`) is not on your PATH. cm needs it to launch sessions. Install it with"
        warn "  curl -fsSL https://claude.ai/install.sh | bash"
        warn "(or see https://docs.claude.com/en/docs/claude-code/setup), then run \`cm --refresh\`."
    fi
}

agentmemory_step() {
    # Prompts need a terminal: piped from curl, stdin is the script itself, so read answers from /dev/tty.
    if (: </dev/tty) 2>/dev/null; then
        "$CM" setup agentmemory --from-installer </dev/tty || warn "AgentMemory setup did not finish; re-run \`cm setup agentmemory\` any time"
    else
        "$CM" setup agentmemory --from-installer </dev/null || warn "AgentMemory setup did not finish; re-run \`cm setup agentmemory\` any time"
    fi
}

main() {
    check_os
    say "cm installer ($OS)"
    find_checkout
    install_uv
    install_cm
    "$CM" setup init
    check_claude
    agentmemory_step
    say ""
    say "Done. Next: \`cm --refresh\` (claude.ai connectors), \`cm --where\` (config and MCP folders), then \`cm\`."
}

main "$@"
