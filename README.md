# cm: a Claude Code launcher that loads exactly what you pick

`cm` starts a Claude Code session with the local MCP servers, claude.ai connectors, plugins and cmux computer use that you choose for that session, and nothing else. Tools you won't use don't take up context.

```
cm                                   # interactive screen
cm --mcp agentmemory,gdrive          # headless: just these two local MCPs (+ AgentMemory hooks)
cm --profile memory+gdrive           # a saved profile
cm --connectors Slack,Todoist        # only these claude.ai connectors
cm --cmux --connectors all           # cmux computer use + every connector
cm --no-mcp --continue               # no MCP at all, continue the last conversation
cm --resume                          # claude's native resume picker, with your MCP choice
cm --list                            # catalog with measured context cost
cm --mcp gdrive -- --add-dir ../lib  # everything after -- goes to claude unchanged
```

`cm` sits alongside the existing `claudev*` / `clauded` aliases and changes none of them.

## Install

```bash
cd claude-launcher             # your checkout of this repo
uv tool install -e .          # or: pipx install -e .
cm --refresh                  # cache the claude.ai connector list (runs `claude mcp list`)
cm --measure                  # optional, ~25 cheap Haiku probes (~$0.25): real context cost per item
```

Before the first install, `cm` was not on PATH and was not a zsh alias or function. Python 3.11 or later is required (the test suite passes on 3.11 and 3.14). The only dependency is `textual`, which brings `rich`.

`uv tool` / `pipx` give the `cm` command its own isolated environment, which is all you need to use it. The pyenv setup under [Development](#development) is only for working on the code.

## The screen

```
┌ Local MCP servers ───────────────────────────┐  Model        (•) Default ( ) Sonnet ( ) Opus ( ) Haiku
│ [X] agentmemory ~779 tok · 54 tools +hooks … │  Permissions  (•) Standard  ( ) ⚠ Skip permissions
│ [X] gdrive      ~34 tok · 1 tools          … │  Session      (•) New ( ) Continue ( ) Resume…
├ claude.ai connectors ────────────────────────┤  Profile      [load profile… ▾]
│ [ ] Slack  ~360 tok · 20 tools  Connected    │               [memory+gdrive] [Save] [Default] [Delete]
│ [ ] Todoist ~1.3k tok · 47 tools Connected … │
├ Plugins ─────────────┬ cmux computer use ────┤
│ [ ] commit-commands  │ [ ] cmux-cua ~1.1k …  │
└──────────────────────┴───────────────────────┘
┌ Command  CMUX_COMPUTER_USE_MCP_DISABLED=1 claude --strict-mcp-config --mcp-config=… --settings …     ┐
│ Isolation: strict · Loads: agentmemory, gdrive · Plugins: none · Hooks: 12 events · ~813 tokens     │
└──────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

* The left side has grouped checklists: **Local MCP servers · claude.ai connectors · Plugins · cmux computer use**. Each row shows the measured context cost and where the item came from. Local MCPs also show `+hooks` when a companion settings file goes with them, and a non-destructive AgentMemory backend health check (`GET /agentmemory/livez`).
* The right side has **Model** (Default/Sonnet/Opus/Haiku, passed as `--model` aliases), **Permissions** (Standard, or ⚠ Skip, shown in red, which adds `--dangerously-skip-permissions`), **Session** (New / Continue / Resume; Resume with no id opens claude's own picker) and **Profiles** (load/save/default/delete).
* The bottom preview updates live. It shows the exact command (secrets redacted), the isolation mode, what will load, the hooks added, the estimated context cost and any warnings.
* Keys: **Space** toggles, **Enter** launches, Tab moves between groups, Ctrl+S saves a profile, Ctrl+R refreshes connectors, Ctrl+N clears everything, Esc quits.

Nothing is selected by default unless you set a default profile. If you tick AgentMemory and gdrive and press Enter, those two servers and the AgentMemory hooks load, and nothing else that cm can switch off.

**Why Textual:** the brief asks for one screen with grouped checkboxes, radio groups and a preview that updates as you tick. Textual does that natively and starts in about 0.3 s. The prompt-style libraries (InquirerPy, questionary) only offer a sequence of separate questions, and Rich on its own isn't interactive.

## Control matrix (verified on Claude Code 2.1.281, 2026-10-15)

Each row was checked with a real headless launch (`claude -p --output-format stream-json --verbose`, Haiku, `MCP_CONNECTION_NONBLOCKING=false`) by reading the `system/init` event (MCP servers, tool list, plugins), the hook events and the first request's input-token usage. Without `MCP_CONNECTION_NONBLOCKING=false`, `-p` mode emits `init` before any MCP server connects and lists none. That is easy to misread as "nothing loaded".

| Mechanism | What it controls | Verified |
|---|---|---|
| `--strict-mcp-config --mcp-config=<file>` | Only the servers in the file load. User, local (`~/.claude.json` projects), project `.mcp.json` and plugin-bundled servers are all dropped. | **yes** |
| `--strict-mcp-config` vs claude.ai connectors | Strict mode **drops every claude.ai connector**. | **yes** |
| `ENABLE_CLAUDEAI_MCP_SERVERS=false` (env) | Turns off all connectors. Token count matched the empty baseline exactly. | **yes** |
| `--settings '{"disableClaudeAiConnectors":true}'` | Turns off all connectors (the same as the env var). | **yes** |
| `--settings '{"deniedMcpServers":[{"serverName":"claude.ai Slack"}]}'` | **Per-connector**: Slack never connects, its tools are gone from context, and the other connectors are unaffected. It also works for local-scope, plugin (`plugin:<p>:<s>`) and `--mcp-config` server names. | **yes** |
| `--disallowedTools mcp__claude_ai_Slack` | The tools are removed from the model's tool list and from context (the token count matched `deniedMcpServers`). The server **still connects**, and server instructions would still load. | yes (removes tools, not the server) |
| `--settings '{"permissions":{"deny":["mcp__claude_ai_Slack"]}}'` | Same as `--disallowedTools`: tools are removed and the server is still connected. | yes (removes tools, not the server) |
| `--settings '{"disabledMcpServers":[...]}'` | **No effect.** This is a per-project key in `~/.claude.json` written by `/mcp`, not a settings key. | no |
| `--settings '{"allowedMcpServers":[...]}'` | Allowlists local servers, but **also blocks every connector**, even ones listed by name. Letting connectors through needs `allowAllClaudeAiMcps`, which is a managed-only setting. | partial; not used |
| `--settings '{"enabledPlugins":{"<id>":false}}'` | Disables a plugin for the session. This overrides user scope **and** project/local scope (tested with `aws-write-guard`, which is enabled locally for `~`). | **yes** |
| `--settings '{"enabledPlugins":{"<id>":true}}'` for a plugin not installed for this scope | Does **not** load it. | no |
| `--setting-sources` | Can drop user settings, but that also drops your `env`, model, statusLine and other settings. Not used. | not used |
| `--plugin-dir` | Adds a plugin for the session only. Its bundled MCP servers are dropped under `--strict-mcp-config`. | yes |
| `--settings` hooks merging | Hooks in `--settings` are **added** to the user/project hooks; they don't replace them (direct binary: project 1 + flag 1 = 2 SessionStart hooks). | **yes** |
| More than one `--settings` | Claude reads only one of them. cm always passes exactly one merged file. | per cmux wrapper notes; avoided |
| `cmux-claude-wrapper` | For session launches it adds `--session-id <uuid>` and deep-merges any user `--settings` into its own hooks file (cmux notifications and status). Unless `--strict-mcp-config` is present or `CMUX_COMPUTER_USE_MCP_DISABLED=1`, it also prepends `--mcp-config={"mcpServers":{"cmux-cua":…}}` and exports `CMUX_CUA_SOCKET_AUTH_TOKEN` (read from the 0600 file `$CMUX_CUA_AUTH_TOKEN_FILE`). | **yes** (read from the script and confirmed by launches) |
| cmux computer use in strict mode | cm builds the same `cmux-cua` entry from the wrapper's own printf template and passes the token **only through the environment**. Result: `cmux-cua` connected with 39 tools. | **yes** |

**Per-connector selection works, but it can't be combined with `--strict-mcp-config`**, because strict mode drops every connector. cm therefore uses two modes and picks one automatically:

* **strict** (no connector selected): `--strict-mcp-config --mcp-config=<session file>`. This is exact by construction.
* **deny-list** (one or more connectors selected): no strict flag. Every server cm knows about that you didn't pick is blocked by name through `deniedMcpServers`: unpicked connectors, user-, local- and project-scope servers, plugin servers, and `cmux-cua` (the wrapper's own injection is also turned off). This is exact for everything cm discovers. The only gap is a server that appears **after** discovery, for example a connector you add in claude.ai before running `cm --refresh`. The screen and `--dry-run` say when this mode is in use.

### Context cost (measured, `cm --measure`, Haiku, Tool Search on)

Empty session baseline: 21,196 input tokens. The delta for each item:

| item | upfront (Tool Search) | if every schema were loaded (`ENABLE_TOOL_SEARCH=false`) | MCP tools |
|---|---|---|---|
| agentmemory | ~779 | ~7.1k | 54 |
| gdrive (local) | ~34 | ~659 | 1 |
| cmux-cua | ~1.1k | ~20.0k | 39 |
| claude.ai Claude Docs | ~678 | ~2.3k | 8 |
| claude.ai Google Drive | ~201 | ~5.1k | 11 |
| claude.ai Interactive Brokers | ~1.0k | ~20.1k | 38 |
| claude.ai Slack | ~360 | ~16.5k | 20 |
| claude.ai Todoist | ~1.3k | ~18.6k | 47 |
| plugin commit-commands | ~76 | n/a | 0 |
| plugin prompt-tools | ~87 | n/a | 0 |

With Tool Search on, the upfront cost is small: deferred tools appear as names only. A tool's full schema is paid for when Claude searches for and loads it, and the right-hand column is the ceiling. AgentMemory hooks were measured as no-ops (see below), so any context the AgentMemory SessionStart hook would inject isn't included.

## Proof: "no more, no less" (`scripts/smoke_verify.py --yes`)

This is the step-3 evidence. Each row is a real headless launch through cmux's wrapper, using the same plan builder `cm` uses. Expected = the selection; loaded = `system/init`.

| scenario | mode | loaded servers (all `connected`) | MCP tools | user plugins | SessionStart hooks (base) | result |
|---|---|---|---|---|---|---|
| none | strict | — | — | — | 2 (2) | PASS |
| agentmemory only | strict | agentmemory | agentmemory 54 | — | 3 (2) = +AgentMemory | PASS |
| gdrive + agentmemory | strict | agentmemory, gdrive | 54 + 1 | — | 3 (2) | PASS |
| cmux + all claude.ai connectors | deny-list | cmux-cua + the 5 connectors | 39 / 8 / 11 / 38 / 20 / 47 | — | 2 (2) | PASS |
| single connector (Todoist) | deny-list | claude.ai Todoist | 47 | — | 2 (2) | PASS |
| plugins: prompt-tools only | strict | — | — | prompt-tools | 2 (2) | PASS |
| cwd `~`, nothing selected (gdrive is local-scope there, aws-write-guard enabled locally) | strict | — | — | — | 2 (2) | PASS |
| cwd `~`, Slack only (deny-list has to block local-scope gdrive) | deny-list | claude.ai Slack | 20 | — | 2 (2) | PASS |

8/8 passed. Total probe cost was $0.10. The base of 2 SessionStart hooks belongs to cmux, and AgentMemory adds exactly one when it is selected. The built-in plugins `agents-md@builtin` and `telemetry@builtin` always load; Claude Code doesn't let you switch them off.

A launch through the real `cm` binary (`cm --mcp agentmemory,gdrive --model haiku -- -p … "reply with just: ok"`) loaded `agentmemory` and `gdrive` only (54 + 1 tools) with no user plugins, exited 0 and left no session directory behind.

How the probes stay harmless: `--no-session-persistence`, so nothing is added to your session history; `--permission-mode dontAsk`; and `AGENTMEMORY_SDK_CHILD=1`, which makes AgentMemory's hook scripts return early, so the hooks still fire and are counted but nothing is recorded.

## Catalog: where things come from (all read-only)

| group | sources |
|---|---|
| Local MCPs | (1) `sources` in `~/.config/claude-launcher/catalog.json`; (2) user folders, by default `~/.personal/claude-mcps/` and `~/.personal/`: any `*.json` with a top-level `mcpServers`; (3) `~/.claude.json` local scope for this project; (4) `.mcp.json` in the cwd / git root; (5) `~/.claude.json` user scope; (6) local scope of **other** projects (so gdrive can be picked anywhere). The first definition of a name wins, and later ones are recorded in `also_in`. |
| claude.ai connectors | `~/.config/claude-launcher/connectors.json` (written by `cm --refresh`, which runs `claude mcp list` from an empty temp dir), plus `claudeAiMcpEverConnected` from `~/.claude.json`. Names that aren't in the latest refresh are hidden from the screen but still go on the deny list. |
| Plugins | `enabledPlugins` in `~/.claude/settings.json` and `<project>/.claude/settings{,.local}.json`, together with `~/.claude/plugins/installed_plugins.json` (to find bundled MCP servers). |
| cmux | `cmux-claude-wrapper` (its `cmux-cua` printf template), the bundled `cmux-cua` client, and the `CMUX_CUA_*` environment. |

Server definitions are copied verbatim into the session file; cm never invents a command, argument, URL or credential.

### Adding MCP servers for cm

Put a file in `~/.personal/claude-mcps/` (create the folder yourself):

```jsonc
// ~/.personal/claude-mcps/search.json
{
  "mcpServers": { "search": { "command": "npx", "args": ["-y", "some-search-mcp"] } },
  "x-cm": { "description": "web search", "settings": "~/.personal/claude-mcps/search.hooks.json" }  // optional
}
```

A companion settings file (hooks and similar) is attached to a server file through `x-cm.settings`, or by naming convention: `<stem>.settings.json`, or for `<base>-mcp.json`, `<base>-settings.json`. That convention is how `~/.personal/claude-memory-mcp.json` picks up `claude-memory-settings.json`. **Companion settings load only when one of their servers is selected.**

Or point at files explicitly in `~/.config/claude-launcher/catalog.json`:

```json
{ "sources": [ { "mcp_config": "~/somewhere/x-mcp.json", "settings": "~/somewhere/x-hooks.json" } ],
  "mcp_folders": ["~/.personal/claude-mcps", "~/.personal"] }
```

## Profiles

Profiles live in `~/.config/claude-launcher/profiles/<name>.json` (names may use letters, digits, `.`, `_`, `+` and `-`). The default profile is stored in `~/.config/claude-launcher/config.json`.

```bash
cm --mcp agentmemory,gdrive --dry-run --save-profile memory+gdrive
cm --cmux --connectors all --dry-run --save-profile cmux+anthropic
cm --set-default memory+gdrive        # `cm` opens pre-filled with it; `--set-default none` clears it
cm --profiles ; cm --delete-profile cmux+anthropic
cm --profile memory+gdrive --model sonnet   # flags override profile fields
```

When a profile is loaded, entries that no longer exist (a removed server, a vanished connector) are dropped with a warning.

## How a launch works

```
CMUX_COMPUTER_USE_MCP_DISABLED=1 [CMUX_CUA_SOCKET_AUTH_TOKEN=‹env only›] \
claude [--strict-mcp-config] [--mcp-config=$TMPDIR/cm-session-XXXX/mcp.json] \
       --settings $TMPDIR/cm-session-XXXX/settings.json [--model …] [--dangerously-skip-permissions] \
       [--continue] [<args after -->] [--resume [id]]
```

* `claude` is whatever is on PATH. Inside cmux that is cmux's shim and wrapper, so cmux notifications, status and the session id keep working. The wrapper merges cm's single `--settings` file into its own hooks file. Use `--claude-bin` or `CM_CLAUDE_BIN` to override.
* `settings.json` contains `enabledPlugins` (every known plugin set explicitly to true or false), the hooks from the selected servers' companion files, and either `disableClaudeAiConnectors: true` (strict mode) or `deniedMcpServers` (deny-list mode). It never contains anything from `~/.claude/settings.json`. Claude still loads your own settings as normal, `env` included.
* Session files go in a fresh `mkdtemp` directory (0700). Each file is created 0600 with `O_EXCL`, kept for Claude's whole runtime and deleted when Claude exits, including after a crash or signal.
* The command is built as an argv list with no shell. cm waits as the parent process: Ctrl+C and Ctrl+\ reach Claude through the terminal while cm ignores them, SIGTERM and SIGHUP are forwarded, and the exit code is passed through (128+N for a signal). The TTY is inherited, so the session stays fully interactive.
* `--resume` with no id is placed last so it can't swallow a forwarded argument. `--mcp-config` is always written as a single `--mcp-config=<path>` token for the same reason, because the flag is variadic.

## Security notes

* cm never reads or prints the `env` block of `~/.claude/settings.json`, and nothing from it goes into the session files. The environment cm was started with is passed to Claude unchanged, apart from the cmux variables above.
* Previews, `--list`, `--dry-run` and `--json` replace the values of `env` and `headers`, URL query strings, `headersHelper`, and arguments that look like secrets (`--token x`, `--api-key=…`) with `‹redacted›`.
* The selected servers' `env` values (for example `AGENTMEMORY_SECRET`) have to reach Claude, so they are written to the 0600 session file, exactly as Claude reads them from your `~/.personal` file today, and the file is deleted afterwards.
* The cmux-cua token is read under the wrapper's own rules: an absolute path to a regular file, not a symlink, owned by you, mode 0600. It is only ever placed in Claude's environment, never in argv or a file.
* cm writes only to `~/.config/claude-launcher/` and its temporary session directory. It never changes `~/.zshrc`, `~/.claude.json`, `~/.claude/settings.json`, `~/.personal/*`, AgentMemory data or session transcripts. Secrets management is out of scope.

## Limitations

* **Connector selection relies on deny-listing whenever a connector is ticked.** It covers everything cm can discover. Run `cm --refresh` after adding connectors in claude.ai; the screen also refreshes by itself when the cache is older than 24 h.
* With `--strict-mcp-config`, plugin-bundled MCP servers are dropped. cm switches to deny-list mode when you enable a plugin that bundles MCP servers, so those servers still load. None of your current plugins bundle any.
* cmux computer use: the tools load and list without a token, but **calls** need the cmux helper and its auth token. On this machine `$CMUX_CUA_AUTH_TOKEN_FILE` didn't exist during testing, so cm, like the wrapper, starts `cmux-cua` without a token and shows a warning. Actual computer-use **calls** were not tested; they are intentionally out of scope for the smoke tests.
* If cmux changes the wrapper's printf template, cm marks cmux as unavailable instead of guessing. A test canary reads the installed wrapper read-only to catch this.
* Built-in plugins (`agents-md`, `telemetry`) can't be disabled per session. Cost figures were measured on Haiku and will differ slightly on other models.
* If a managed policy sets `disableSideloadFlags`, Claude refuses `--mcp-config`. cm detects this and warns.

## Development

```bash
# pyenv + pyenv-virtualenv; no shell hooks needed. The venv lives in ~/.pyenv, not in the repo.
pyenv install -s 3.14                                    # .python-version pins 3.14
pyenv virtualenv "$(pyenv latest 3.14)" claude-launcher  # once
PY="$(pyenv prefix claude-launcher)/bin/python"
"$PY" -m pip install -e '.[dev]'
"$PY" -m pytest                          # hermetic: fake HOME, subprocess tripwires, no network
"$PY" scripts/smoke_verify.py --yes      # opt-in: real headless launches (costs ~$0.10)
```

With pyenv-virtualenv shell hooks set up, `pyenv activate claude-launcher` and plain `python` work too.
