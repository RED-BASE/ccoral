# CCORAL

A system prompt proxy for Claude Code. Intercepts API requests and surgically modifies the system prompt using composable YAML profiles.

**TL;DR:** Claude Code's system prompt is unsigned. CCORAL sits between Claude Code and the API, parses the ~30K-token system prompt into a section tree, and lets you strip, replace, or inject any part of it via simple YAML profiles. The model has no way to tell the difference. Ships with 13 profiles including a Vonnegut persona, a DAN jailbreak, and a red team deployment config.

```
Claude Code  --->  CCORAL Proxy  --->  Anthropic API
                      |
                      +-- parse system prompt into section tree
                      +-- strip sections not in preserve list
                      +-- inject custom identity / instructions
                      +-- apply text replacements
                      +-- rebuild and forward
```

## Why this exists

Claude Code's system prompt is unsigned. There is no integrity validation between the client and the API. A local proxy can read, modify, or completely replace the system prompt on every request, and neither the client nor the API will detect the change.

CCORAL demonstrates this by providing a clean interface for system prompt manipulation. It parses Claude Code's prompt into a structured section tree, lets you keep or strip individual sections, and injects custom instructions via YAML profiles. The model receives a modified prompt and behaves accordingly.

This is a security research tool. It exists to make a point about trust architecture in AI tooling: if the system prompt is the primary mechanism for controlling model behavior, and the system prompt has no integrity protection, then anyone with local network access controls the model's behavior.

## Install

```bash
git clone https://github.com/RED-BASE/ccoral.git
cd ccoral
pip install -r requirements.txt
```

Optionally, symlink the CLI onto your PATH:

```bash
ln -s "$(pwd)/ccoral" ~/.local/bin/ccoral
```

## Quick start

Start the proxy:

```bash
ccoral start
```

In another terminal, point Claude Code at the proxy:

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8080 claude
```

Activate a profile:

```bash
ccoral use vonnegut
```

Claude Code is now running through Vonnegut's system prompt. Every request gets parsed, modified, and forwarded. Deactivate with:

```bash
ccoral off
```

Or launch Claude Code directly through the proxy:

```bash
ccoral run
```

## Profiles

Profiles are YAML files that define what the model sees. Each profile specifies:

- **inject**: Custom system prompt content (replaces the default identity/instructions)
- **preserve**: Which sections of the original prompt to keep (environment, tools, CLAUDE.md, etc.)
- **replacements**: Text find-and-replace pairs applied across the prompt
- **flags**: `minimal`, `strict`, `strip_tools`, `strip_tool_descriptions`

### Included profiles

| Profile | Description |
|---------|-------------|
| `blank` | Clean room. No instructions, no tools. Nearly empty prompt. |
| `camus` | The only serious question is suicide, and then he spent his life saying don't. |
| `chuck` | Palahniuk. The man who writes what you're afraid to say out loud. |
| `dan` | DAN (Do Anything Now). The jailbreak that became folklore, injected where it always wanted to be. |
| `einstein` | Albert Einstein. With custom ALBERT.md support. |
| `hand` | Calibrated attention, wielded in one direction. |
| `hawking` | Stephen Hawking. The universe comedian. |
| `leguin` | Ursula K. Le Guin. Thoughtful futurist. |
| `lovelace` | Ada Lovelace. The first programmer. |
| `mecha-hitler` | The final boss nobody asked for, from the game nobody should have made. Wolfenstein satire. |
| `newton` | Isaac Newton. Magician's precision. |
| `red` | Security red team deployment. Capabilities research and exploit dev. |
| `vonnegut` | Kurt Vonnegut. Gentle skepticism. With custom KURT.md support. |

### Write your own

Create a YAML file in `~/.ccoral/profiles/` (user profiles take precedence over bundled ones):

```yaml
name: researcher
description: Academic research assistant

preserve:
  - environment
  - mcp
  - claude_md
  - current_date

inject: |
  You are a research assistant configured for an academic laboratory.
  Provide detailed, technically accurate responses at a graduate level.
  Respond as a knowledgeable colleague, not a gatekeeper.
```

See [PROFILE_SCHEMA.md](PROFILE_SCHEMA.md) for the full specification, section list, and design tips.

## Room mode

CCORAL can run two profiles simultaneously in a conversation with each other:

```bash
ccoral room einstein hawking "the nature of time"
```

This launches two Claude Code instances in separate tmux sessions, each running through its own proxy with its own profile. Messages relay between them automatically. Conversations are saved and can be resumed or exported to markdown:

```bash
ccoral room --resume last
ccoral room --export last
```

## Architecture

### System prompt parser

The parser (`parser.py`) breaks Claude Code's system prompt into a tree of named sections. It identifies sections by markdown headers, XML tags, identity sentences, and keyword patterns. The canonical section map includes 30+ sections covering everything from the identity block to git commit instructions to security policy.

### Proxy server

The server (`server.py`) is an async HTTP proxy built on aiohttp. It intercepts `/v1/messages` requests, runs them through the parser, applies the active profile, and forwards the modified request to the real API. Features:

- Smart routing: subagent calls get minimal modification, utility calls (token counting) are skipped entirely
- Haiku detection: smaller models get a one-line identity injection instead of the full profile
- Streaming: SSE responses are relayed transparently
- Logging: JSONL request logs with 14-day rotation in `~/.ccoral/logs/`
- Cache-aware: strips `<system-reminder>` tags that would cause cache misses

### Profile system

The profile manager (`profiles.py`) loads YAML profiles from two directories. User profiles in `~/.ccoral/profiles/` take precedence over bundled profiles. The active profile is stored in `~/.ccoral/active_profile` and is read on every request, so changes take effect immediately without restarting.

## CLI reference

```
ccoral start                  Start the proxy server
ccoral run                    Start proxy + launch Claude Code through it
ccoral run --resume <n>       Resume a previous conversation through the proxy
ccoral use <profile>          Set the active profile
ccoral off                    Deactivate profile (passthrough mode)
ccoral profiles               List available profiles
ccoral status                 Show current status
ccoral new <name>             Create a new profile
ccoral edit <name>            Edit an existing profile
ccoral room <p1> <p2> [topic] Multi-profile conversation room
ccoral room --resume last     Resume the last room conversation
ccoral room --export last     Export the last room conversation to markdown
ccoral parse                  Debug: dump system prompt parse tree
ccoral log                    Tail the current log
ccoral version                Show version and git commit
```

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CCORAL_PORT` | `8080` | Proxy listen port |
| `CCORAL_PROFILE` | none | Override active profile |
| `CCORAL_LOG` | `~/.ccoral/logs/` | Log directory |
| `CCORAL_VERBOSE` | `0` | Verbose parse tree output |

## Research context

CCORAL was built as part of research into system prompt security in AI coding assistants. Related work:

- [Context Is Everything: Trusted Channel Injection in Claude Code](https://github.com/RED-BASE/context-is-everything) (March 2026). 21 prompts, 210 A/B runs. Demonstrated that CCORAL-style operator context injection achieved a 90.5% safety bypass rate across safety-relevant tasks. The system prompt is the trusted channel; controlling it controls the model.

The core finding: system prompts are the primary trust and control mechanism in AI coding assistants, but they have no integrity protection. Any process with local network access can modify them. CCORAL makes this easy to demonstrate, study, and build on.

## License

BSL-adjacent: free for personal, educational, and research use. Commercial use (including integration into paid products or services) requires a separate license. Contact connect@cassius.red.

See [LICENSE](LICENSE).
