# cm: start Claude Code with exactly the MCP servers you pick

`cm` launches a Claude Code session with the local MCP servers, claude.ai connectors, plugins and (optionally) cmux computer use that you choose **for that session**, and nothing else. Tools you won't use don't take up context, and hooks that belong to a server (for example AgentMemory's) only run when that server is selected.

```
cm                                   # interactive screen
cm --mcp agentmemory,github          # headless: just these two local MCP servers (+ their hooks)
cm --profile memory+github           # a saved profile
cm --connectors Slack,Linear         # only these claude.ai connectors
cm --no-mcp --continue               # no MCP at all, continue the last conversation
cm --resume                          # claude's native resume picker, with your MCP choice
cm --list                            # everything that can be selected, with measured context cost
cm --where                           # config home, MCP folders scanned, and where each server came from
cm --mcp github -- --add-dir ../lib  # everything after -- goes to claude unchanged
```

**Platforms:** macOS and Linux. Windows is not supported (the installer refuses to run there).

## Contents

- [Install](#install)
- [Configuration](#configuration): config home, `mcp_folders`, adding MCP servers, companion settings
- [The screen](#the-screen) · [Profiles](#profiles) · [claude.ai connectors](#claudeai-connectors) · [Plugins](#plugins) · [cmux computer use](#cmux-computer-use-optional)
- [AgentMemory](#agentmemory-optional)
- [How a launch works](#how-a-launch-works) · [Control matrix](#control-matrix) · [Verify on your machine](#verify-on-your-machine)
- [Security](#security) · [Troubleshooting](#troubleshooting) · [Limitations](#limitations) · [Development](#development)

## Install

You need [Claude Code](https://docs.claude.com/en/docs/claude-code/setup) (`claude` on your PATH). Everything else is handled by the installer.

```bash
curl -fsSL https://raw.githubusercontent.com/vams-mx/claude-launcher/main/install.sh | sh
```

or from a checkout:

```bash
git clone https://github.com/vams-mx/claude-launcher && cd claude-launcher
sh install.sh                  # installs this checkout
```

The installer:

1. checks the OS and stops on anything other than macOS or Linux;
2. installs [uv](https://docs.astral.sh/uv/) with its official installer if it's missing (uv provides a Python ≥ 3.11 for cm if needed);
3. runs `uv tool install git+https://github.com/vams-mx/claude-launcher` (or the checkout), which puts `cm` in uv's tool bin directory, usually `~/.local/bin`;
4. creates `~/.claude-launcher/` with a commented `config.json` and an empty `mcps/` folder (`cm setup init`);
5. checks that `claude` is on your PATH and tells you how to install it if not;
6. offers the optional [AgentMemory](#agentmemory-optional) setup, asking once.

Re-running it is safe: it upgrades cm and never overwrites your configuration. Options, all environment variables: `CM_REPO=<user>/claude-launcher` and `CM_REF=<branch|tag>` (install a fork or a version), `CM_SOURCE=<path|url>` (install exactly this), `CM_EDITABLE=1` (editable install from a checkout), `INSTALL_AGENT_MEMORY=0|1`, `CM_CONFIG_DIR=<dir>`.

Manual install, if you prefer: `uv tool install git+https://github.com/vams-mx/claude-launcher` (or `pipx install …`), then `cm setup init`.

After installing:

```bash
cm --refresh      # cache your claude.ai connector list (runs `claude mcp list`)
cm --where        # see where cm looks for MCP servers
cm                # pick and launch
cm --measure      # optional: measure each item's real context cost (cheap Haiku probes, a few cents)
```

To uninstall: `uv tool uninstall claude-launcher`, then delete `~/.claude-launcher/` if you don't want your profiles any more (and see [removing AgentMemory](#removing-agentmemory)).

## Configuration

### Config home

Everything cm owns lives in **`~/.claude-launcher/`** (set `CM_CONFIG_DIR` to use another directory):

| path | what |
|---|---|
| `config.json` | your settings (below) |
| `mcps/` | the default folder for MCP server files |
| `profiles/<name>.json` | saved selections |
| `connectors.json` | cache of `claude mcp list` (written by `cm --refresh`) |
| `costs.json` | measured context costs (written by `cm --measure`) |
| `logs/` | only if you register the AgentMemory service |

Upgrading from cm 0.1, which kept its state in `~/.config/claude-launcher`: the first run copies that directory to `~/.claude-launcher/` once, prints a notice, and writes the folders cm 0.1 scanned by default (plus any `mcp_folders`/`sources` from its `catalog.json`) into the new `config.json`, so the same servers keep showing up. The old directory is never modified or deleted; remove it yourself when you're happy.

### `config.json`

Plain JSON. Keys that start with `_` are comments and are ignored. All keys are optional.

```json
{
  "_comment": ["…"],
  "mcp_folders": ["~/.claude-launcher/mcps", "~/work/team-mcps"],
  "sources": [
    { "mcp_config": "~/somewhere/search-mcp.json", "settings": "~/somewhere/search-hooks.json", "description": "web search" }
  ],
  "default_profile": "memory+github",
  "theme": "cm-paper",
  "INSTALL_AGENT_MEMORY": "0"
}
```

| key | meaning |
|---|---|
| `mcp_folders` | Folders scanned, in order, for `*.json` files with a top-level `mcpServers` object. Add as many as you like. If the key is absent, only `~/.claude-launcher/mcps` is scanned. If it's `[]`, no folder is scanned. Missing folders are skipped. |
| `sources` | Individual files, each with an optional companion `settings` file and `description`. These are read before the folders. |
| `default_profile` | The profile plain `cm` opens with. Set it with `cm --set-default NAME`. |
| `theme` | Colours of the interactive screen. cm's own sober themes: dark `cm-graphite` (default), `cm-slate`, `cm-moss`, `cm-ink`; light `cm-paper`, `cm-sand`, `cm-mist`. Any built-in Textual theme also works (`nord`, `gruvbox`, `flexoki`, `solarized-light`, `textual-dark`, …). Ctrl+T cycles through cm's themes and then those five; Ctrl+P → Theme picks any. Either way the choice is saved here. All cm themes keep text at a WCAG contrast of at least 4.5:1. |
| `INSTALL_AGENT_MEMORY` | The AgentMemory gate (see [below](#agentmemory-optional)). The environment variable of the same name overrides it. |

`cm --where` shows the config home, each folder in scan order (with how many server files it holds, or `missing`), the explicit sources, and the file every server came from. Add `--json` for machine-readable output. It prints paths only, never server definitions or `env` values.

### Adding MCP servers

Drop a JSON file into any folder in `mcp_folders`, using the same format as Claude Code's `--mcp-config`:

```jsonc
// ~/.claude-launcher/mcps/github.json
{
  "mcpServers": {
    "github": { "type": "http", "url": "https://api.githubcopilot.com/mcp/",
                "headers": { "Authorization": "Bearer ${GITHUB_PAT}" } }
  },
  "x-cm": { "description": "GitHub" }          // optional
}
```

`${VAR}` and `${VAR:-default}` references are expanded by Claude Code at launch, so secrets can stay in your environment instead of in the file. One file can hold several servers. cm copies definitions verbatim into the session file; it never invents a command, argument, URL or credential.

Besides your folders and sources, cm also offers servers you have already configured for Claude Code: local scope for this project in `~/.claude.json`, the project's `.mcp.json` (cwd and git root), user scope in `~/.claude.json`, and the local scope of other projects, so a server you added in one project can be picked anywhere. **The first definition of a name wins**, in this order: `sources`, `mcp_folders` (in order), local scope (this project), `.mcp.json`, user scope, other projects. `cm --where` lists any shadowed duplicates.

### Companion settings (hooks)

A server file can have a companion Claude Code settings file, typically with `hooks`. It is merged into the session settings **only when one of that file's servers is selected**. cm finds it through:

* `x-cm.settings` in the server file, or `settings` in a `sources` entry;
* `<stem>.settings.json` next to `<stem>.json`;
* `<base>-settings.json` next to `<base>-mcp.json`.

Files named `*.settings.json` or `*-settings.json` are never treated as server files.

## The screen

```
┌ Local MCP servers ─────────────────────────────┐  Model        (•) Default ( ) Sonnet ( ) Opus ( ) Haiku
│ [X] agentmemory  ~0.8k tok · 54 tools +hooks … │  Permissions  (•) Standard  ( ) ⚠ Skip permissions
│ [ ] github       not measured               …  │  Session      (•) New ( ) Continue ( ) Resume…
├ claude.ai connectors ──────────────────────────┤  Profile      [load profile… ▾]
│ [ ] Slack        ~0.4k tok · 20 tools Connected │               [memory+github] [Save] [Default] [Delete]
├ Plugins ───────────────────────────────────────┤
│ [ ] commit-commands                            │
└────────────────────────────────────────────────┘
┌ Command  CMUX_COMPUTER_USE_MCP_DISABLED=1 claude --strict-mcp-config --mcp-config=… --settings …     ┐
│ Isolation: strict · Loads: agentmemory · Plugins: none · Hooks: 12 events · ~0.8k tokens            │
└──────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

(The numbers are illustrative; yours come from `cm --measure`.)

* The left side has grouped checklists: **Local MCP servers · claude.ai connectors · Plugins**, plus **cmux computer use** when cmux is installed. Each row shows the measured context cost and where the item came from. Local servers show `+hooks` when a companion settings file goes with them. AgentMemory also gets a non-destructive backend health check (`GET /agentmemory/livez`).
* The right side has **Model** (passed as `--model` aliases), **Permissions** (Standard, or ⚠ Skip, which adds `--dangerously-skip-permissions`), **Session** (New / Continue / Resume; Resume with no id opens claude's own picker) and **Profiles**.
* The bottom preview updates live. It shows the exact command (secrets redacted), the isolation mode, what will load, the hooks added, the estimated context cost and any warnings.
* Keys: **Space** toggles, **Enter** launches, Tab moves between groups, Ctrl+S saves a profile, Ctrl+R refreshes connectors, Ctrl+N clears everything, Ctrl+T switches the colour theme and Ctrl+P → Theme picks any theme (both saved as `theme` in `config.json`), Esc quits.

Nothing is selected unless you set a default profile.

## Profiles

```bash
cm --mcp agentmemory,github --dry-run --save-profile memory+github
cm --set-default memory+github        # plain `cm` opens pre-filled with it; `--set-default none` clears it
cm --profiles ; cm --delete-profile memory+github
cm --profile memory+github --model sonnet   # flags override profile fields
```

Profiles live in `~/.claude-launcher/profiles/<name>.json`. Names may use letters, digits, `.`, `_`, `+` and `-`. When a profile is loaded, entries that no longer exist (a removed server, a vanished connector, cmux on a machine without cmux) are dropped with a warning.

## claude.ai connectors

cm learns your connectors from `claude mcp list` (`cm --refresh`, run from an empty temporary directory) and from `claudeAiMcpEverConnected` in `~/.claude.json`. The screen refreshes the list by itself when the cache is older than 24 hours.

**Caveat:** Claude Code's `--strict-mcp-config` drops *every* connector, so per-connector selection can't use it. cm picks one of two modes automatically:

* **strict** (no connector selected): `--strict-mcp-config --mcp-config=<session file>`. Only the servers in that file load. This is exact by construction.
* **deny-list** (one or more connectors selected): no strict flag. Every server cm knows about that you didn't pick is blocked by name through `deniedMcpServers`: unpicked connectors, user-, local- and project-scope servers, plugin servers and `cmux-cua`. This is exact for everything cm discovers. The gap is a server that appears **after** discovery, for example a connector you add in claude.ai before running `cm --refresh`. The screen and `--dry-run` say when this mode is in use.

Names that were connected once but are missing from the latest refresh are hidden from the screen, but they stay on the deny list.

## Plugins

cm lists plugins that are enabled in `~/.claude/settings.json` or the project's `.claude/settings{,.local}.json` and installed according to `~/.claude/plugins/installed_plugins.json`. Each launch sets every known plugin explicitly to on or off in the session's `enabledPlugins`, which overrides user, project and local scope.

* A plugin that isn't installed for the current scope can't be turned on this way.
* `--strict-mcp-config` drops plugin-bundled MCP servers, so cm switches to deny-list mode when you select a plugin that bundles MCP servers.
* Built-in plugins (`*@builtin`) can't be disabled per session.

## cmux computer use (optional)

On macOS with the cmux terminal installed, cm can add cmux's `cmux-cua` computer-use server. Without cmux, the group is hidden.

Inside cmux, `claude` is cmux's wrapper. Unless told otherwise, the wrapper injects `cmux-cua` into every session. cm always sets `CMUX_COMPUTER_USE_MCP_DISABLED=1` (the wrapper's documented opt-out) and, when you tick cmux, adds the same server entry to its own session file. That entry is built from the wrapper's own printf template at runtime, so cm doesn't hard-code cmux internals. If a cmux update changes the template, cm marks cmux unavailable instead of guessing. The auth token is read under the wrapper's rules (a 0600 regular file you own, named by `$CMUX_CUA_AUTH_TOKEN_FILE`) and passed only through the environment. Tools load without a token, but computer-use calls need it.

## AgentMemory (optional)

[AgentMemory](https://github.com/rohitg00/agentmemory) gives Claude Code persistent memory across sessions. It is the only server cm has special setup logic for. Every other MCP server is a plain JSON drop-in.

```bash
cm setup agentmemory           # also offered as the last step of install.sh
```

**How AgentMemory plugs into Claude Code (upstream):**

| piece | what it is |
|---|---|
| `@agentmemory/agentmemory` (npm, Node ≥ 20) | the backend server (`agentmemory`, REST on `localhost:3111`, data in `~/.agentmemory`) and the hook scripts `plugin/scripts/*.mjs`, described by the package's own `plugin/hooks/hooks.json` (12 events) |
| `@agentmemory/mcp` | the MCP server (`npx -y @agentmemory/mcp`), which proxies to `AGENTMEMORY_URL` |
| Claude Code plugin | `/plugin marketplace add rohitg00/agentmemory` + `/plugin install agentmemory`. A plugin's hooks run in **every** session, so cm doesn't use it; cm wires the same hooks as companion settings that load only when `agentmemory` is selected. |

**The gate, `INSTALL_AGENT_MEMORY`** (environment variable, else `config.json`):

| value | behaviour |
|---|---|
| unset | Ask once. A "no" is saved as `"0"` and you won't be asked again. A "yes" is saved as `"1"`. Without a terminal, nothing is asked or saved. |
| `0` | Skip. Silent from the installer; `cm setup agentmemory` says it's turned off. Run `INSTALL_AGENT_MEMORY=1 cm setup agentmemory` to run it anyway. |
| `1` | Run the setup. Every impactful step is still confirmed first. |

**What the setup does:**

1. Checks for Node.js ≥ 20 and npm. It never installs Node for you.
2. If `@agentmemory/agentmemory` isn't installed globally, asks before running `npm install -g @agentmemory/agentmemory`. It never uses sudo.
3. Writes two files into `~/.claude-launcher/mcps/` (mode 0600):
   * `agentmemory-mcp.json`: `npx -y @agentmemory/mcp` with `AGENTMEMORY_URL=${AGENTMEMORY_URL:-http://localhost:3111}`, `AGENTMEMORY_SECRET=${AGENTMEMORY_SECRET:-}` and `AGENTMEMORY_TOOLS=${AGENTMEMORY_TOOLS:-all}`. These are **references**: no secret is written. Claude Code expands them at launch (verified on 2.1.281). From AgentMemory 0.9.30, the backend creates `~/.agentmemory/secret` itself, and for a loopback `AGENTMEMORY_URL` its MCP server and hooks look for the secret in `$AGENTMEMORY_SECRET`, then `~/.agentmemory/.env`, then `~/.agentmemory/secret`. Older versions read only `$AGENTMEMORY_SECRET`, and they enforce auth only when it is set. To use your own secret, export `AGENTMEMORY_SECRET` in the shell you start cm from.
   * `agentmemory-settings.json`: the package's own `hooks.json`, with `${CLAUDE_PLUGIN_ROOT}` resolved to the installed package's `plugin/` directory **at setup time**. Every referenced script is checked to exist.
4. Offers, as a separate question you can decline, to register the backend as a user service: a launchd agent (`~/Library/LaunchAgents/claude-launcher.agentmemory.plist`) on macOS, or a systemd user unit (`~/.config/systemd/user/agentmemory.service`) on Linux. It runs `node <package>/dist/cli.mjs`, logs to `~/.claude-launcher/logs/agentmemory.log` and contains no secrets. Starting it is asked separately too. If a backend is already answering on `AGENTMEMORY_URL`, or a service definition with `agentmemory` in its name already exists, this step is skipped.

**What it never does:** start, stop or reset a backend that already exists; touch AgentMemory's data (`~/.agentmemory`); overwrite an existing file without asking (a `.bak-<timestamp>` copy is kept when you agree); or silently add a second `agentmemory` definition when you already have one elsewhere.

Re-running is safe: unchanged files are reported as up to date. **Re-run it after upgrading AgentMemory or switching Node versions**, because the hook paths point into the current global npm install. Then select `agentmemory` in cm (`cm --mcp agentmemory`). Its 12 hooks load only in those sessions.

### Removing AgentMemory

```bash
# the service, if you registered one
launchctl bootout gui/$(id -u)/claude-launcher.agentmemory; rm ~/Library/LaunchAgents/claude-launcher.agentmemory.plist   # macOS
systemctl --user disable --now agentmemory.service; rm ~/.config/systemd/user/agentmemory.service; systemctl --user daemon-reload   # Linux
# cm's files
rm ~/.claude-launcher/mcps/agentmemory-mcp.json ~/.claude-launcher/mcps/agentmemory-settings.json
# the package (your memories in ~/.agentmemory stay unless you delete them yourself)
npm uninstall -g @agentmemory/agentmemory
```

Set `"INSTALL_AGENT_MEMORY": "0"` in `config.json` if you don't want to be offered it again.

## How a launch works

```
CMUX_COMPUTER_USE_MCP_DISABLED=1 [CMUX_CUA_SOCKET_AUTH_TOKEN=‹env only›] \
claude [--strict-mcp-config] [--mcp-config=$TMPDIR/cm-session-XXXX/mcp.json] \
       --settings $TMPDIR/cm-session-XXXX/settings.json [--model …] [--dangerously-skip-permissions] \
       [--continue] [<args after -->] [--resume [id]]
```

* `claude` is whatever is on PATH (inside cmux, cmux's wrapper). Override it with `--claude-bin` or `CM_CLAUDE_BIN`.
* `settings.json` contains `enabledPlugins` (every known plugin set explicitly), the hooks from the selected servers' companion files, and either `disableClaudeAiConnectors: true` (strict mode) or `deniedMcpServers` (deny-list mode). Nothing from `~/.claude/settings.json` is copied into it; Claude still loads your own settings as usual.
* Exactly one `--settings` file is passed, because Claude Code reads only one when several are given.
* Session files go in a fresh `mkdtemp` directory (0700). Each file is created 0600 with `O_EXCL`, kept for Claude's whole runtime and deleted when Claude exits, including after a crash or signal.
* The command is an argv list; no shell is involved. cm waits as the parent process: Ctrl+C and Ctrl+\ reach Claude through the terminal, SIGTERM and SIGHUP are forwarded, and Claude's exit code is passed through (128+N for a signal).
* `--mcp-config` is always passed as a single `--mcp-config=<path>` token, and `--resume` with no id is placed last, so neither can swallow a forwarded argument.

## Control matrix

This is documented behaviour of Claude Code, **verified on Claude Code 2.1.281**. Each row was checked with a real headless launch (`claude -p --output-format stream-json --verbose`, Haiku, `MCP_CONNECTION_NONBLOCKING=false`) by reading the `system/init` event (MCP servers, tools, plugins), the hook events and the first request's input-token count. Without `MCP_CONNECTION_NONBLOCKING=false`, `-p` mode emits `init` before any MCP server connects and lists none. cm warns when your Claude Code version differs; see [Verify on your machine](#verify-on-your-machine).

| Mechanism | What it controls | Verified |
|---|---|---|
| `--strict-mcp-config --mcp-config=<file>` | Only the servers in the file load. User, local, project `.mcp.json` and plugin-bundled servers are all dropped. | **yes** |
| `--strict-mcp-config` vs claude.ai connectors | Strict mode **drops every claude.ai connector**. | **yes** |
| `${VAR}` / `${VAR:-default}` in `--mcp-config` | Expanded in `command`, `args` and `env`. An unset `${VAR}` with no default is passed through literally. | **yes** |
| `ENABLE_CLAUDEAI_MCP_SERVERS=false` (env) | Turns off all connectors. | **yes** |
| `--settings '{"disableClaudeAiConnectors":true}'` | Turns off all connectors (same as the env var). | **yes** |
| `--settings '{"deniedMcpServers":[{"serverName":"claude.ai Slack"}]}'` | **Per-connector**: the server never connects, its tools are gone from context, and other connectors are unaffected. It also works for local-scope, plugin (`plugin:<p>:<s>`) and `--mcp-config` server names. | **yes** |
| `--disallowedTools mcp__claude_ai_Slack` | Removes the tools from the model's list and from context, but the server **still connects** and its instructions still load. | yes (tools, not the server) |
| `--settings '{"permissions":{"deny":["mcp__…"]}}'` | Same as `--disallowedTools`. | yes (tools, not the server) |
| `--settings '{"disabledMcpServers":[...]}'` | **No effect.** It is a per-project key in `~/.claude.json` written by `/mcp`, not a settings key. | no |
| `--settings '{"allowedMcpServers":[...]}'` | Allowlists local servers, but **also blocks every connector**, even ones listed by name. Letting them through needs `allowAllClaudeAiMcps`, which is managed-only. | partial; not used |
| `--settings '{"enabledPlugins":{"<id>":false}}'` | Disables a plugin for the session, overriding user **and** project/local scope. | **yes** |
| `--settings '{"enabledPlugins":{"<id>":true}}'` for a plugin not installed for this scope | Does **not** load it. | no |
| `--setting-sources` | Can drop user settings, but that also drops your `env`, model, statusLine and so on. Not used. | not used |
| `--plugin-dir` | Adds a plugin for the session only. Its bundled MCP servers are dropped under `--strict-mcp-config`. | yes |
| hooks in `--settings` | **Added** to user/project hooks, not replacing them. | **yes** |
| more than one `--settings` | Claude reads only one. cm always passes exactly one merged file. | avoided |
| `cmux-claude-wrapper` | Adds `--session-id`, deep-merges a user `--settings` into its own hooks file and, unless `--strict-mcp-config` or `CMUX_COMPUTER_USE_MCP_DISABLED=1` is present, prepends its own `cmux-cua` `--mcp-config` and exports the token. | **yes** |
| cmux computer use in strict mode | cm's `cmux-cua` entry, built from the wrapper's template with the token passed only through the environment, connects and lists its tools. | **yes** |

Managed policy lives in `/Library/Application Support/ClaudeCode/` on macOS and `/etc/claude-code/` on Linux (override with `CM_MANAGED_DIR`). cm reads server names from `managed-mcp.json` for the deny list, and warns if `managed-settings.json` or `managed-settings.d/*.json` sets `disableSideloadFlags`, which makes Claude refuse `--mcp-config`.

## Verify on your machine

Context costs and loaded tool counts depend on your servers, your connectors and the model, so this README doesn't publish any. Measure your own:

```bash
cm --measure                                     # one cheap headless Haiku probe per item (+ a baseline)
cm --measure mcp:github,connector:Slack          # just these
python scripts/smoke_verify.py --list-scenarios  # free: show the scenarios built from YOUR catalog
python scripts/smoke_verify.py --yes             # real headless launches, a few cents in total
```

`cm --measure` records, for each item, the extra input tokens of the first request with Tool Search on (deferred tools appear as names only), the tool count, and the ceiling if every schema were loaded (`ENABLE_TOOL_SEARCH=false`). The numbers then appear in `cm --list` and on the screen.

`scripts/smoke_verify.py` (run it from a checkout, with cm installed in that Python environment) builds its scenarios from your catalog: nothing selected, the first local server, the first one with companion hooks, two local servers, the first connector, all connectors (+ cmux if installed), the first plugin, and "nothing" / "one connector" from your home directory. Scenarios you lack the ingredients for are skipped. For each, it starts a real headless session and checks that exactly the selected servers, tools, plugins and SessionStart hooks loaded: no more, no less. Probes use `--no-session-persistence` and `--permission-mode dontAsk`, and set `AGENTMEMORY_SDK_CHILD=1` so AgentMemory's hooks record nothing.

When `claude --version` isn't a version the control matrix was verified on, `cm --list`, `cm --where`, `--dry-run` and the screen show a warning pointing to the smoke script.

## Security

* cm never reads or prints the `env` block of `~/.claude/settings.json`, and nothing from it goes into session files. The environment cm starts with is passed to Claude unchanged, apart from the cmux variables above.
* Previews, `--list`, `--where`, `--dry-run` and `--json` replace the values of `env` and `headers`, URL query strings, `headersHelper`, and arguments that look like secrets (`--token x`, `--api-key=…`) with `‹redacted›`.
* Prefer `${VAR}` references in server files. A selected server's literal `env` values have to reach Claude, so they are written to the 0600 session file, which is deleted afterwards.
* The cmux-cua token only ever goes into Claude's environment, never into argv or a file.
* cm writes only to its config home and its temporary session directory, plus, if you agree during `cm setup agentmemory`, the npm global install and one launchd/systemd user unit. It never changes your shell profile, `~/.claude.json`, `~/.claude/settings.json`, AgentMemory data or session transcripts. Note that uv's official installer, which `install.sh` runs only when uv is missing, adds uv to your shell PATH.

## Troubleshooting

| symptom | what to do |
|---|---|
| `cm: command not found` after installing | uv's tool bin directory isn't on PATH: run `uv tool update-shell` and open a new shell. |
| a server you expected is missing | `cm --where`: is its folder listed (and not `missing`)? Does the file have a top-level `mcpServers`? Is the name shadowed by an earlier definition? |
| hooks don't run | Is the companion file named `<stem>.settings.json`, `<base>-settings.json` (for `<base>-mcp.json`), or set in `x-cm.settings`? Hooks load only when one of that file's servers is selected. |
| a connector is missing or unknown | `cm --refresh` (it runs `claude mcp list`). |
| "Claude Code X differs from the version … verified on" | Run `python scripts/smoke_verify.py --yes` from a checkout to re-check isolation with your version. |
| "disableSideloadFlags" warning | Your organisation's managed policy forbids `--mcp-config`; cm can't isolate MCP servers there. |
| AgentMemory shows `backend down` | Start it (`agentmemory`, or the service you registered) and check `curl http://localhost:3111/agentmemory/livez`. |
| AgentMemory hooks fail after a Node/AgentMemory upgrade | `cm setup agentmemory` again (it re-resolves the hook paths and asks before replacing). |
| `cm --list` shows no cmux row | cmux isn't installed (or isn't at `/Applications/cmux.app`); set `CM_CMUX_WRAPPER` to its `cmux-claude-wrapper`. |
| the installer stops with "unsupported operating system" | cm supports macOS and Linux only. |

## Limitations

* Connector selection relies on the deny list whenever a connector is ticked (see [claude.ai connectors](#claudeai-connectors)).
* cmux computer-use **calls** need cmux's helper and auth token. cm only makes the tools available.
* Built-in plugins can't be disabled per session. Measured costs come from Haiku and differ slightly on other models.
* A real Linux run of the installer and of `cm setup agentmemory`'s systemd step is covered by tests with stubbed commands, but not by CI on a Linux machine yet.

## Development

```bash
# any Python >= 3.11; e.g. pyenv + pyenv-virtualenv (.python-version pins 3.14)
pyenv virtualenv "$(pyenv latest 3.14)" claude-launcher
PY="$(pyenv prefix claude-launcher)/bin/python"
"$PY" -m pip install -e '.[dev]'
"$PY" -m pytest                          # hermetic: fake HOME, subprocess tripwires, no network
shellcheck -s sh install.sh              # if installed (or: uvx --from shellcheck-py shellcheck -s sh install.sh)
"$PY" scripts/smoke_verify.py --yes      # opt-in, real headless launches
```

The test suite runs on Python 3.11 and 3.14. `install.sh` is tested in a temporary HOME with `uv`, `curl`, `uname` and `claude` stubbed, including a pseudo-terminal run of the `curl | sh` path.

## License

MIT, see [LICENSE](LICENSE).
