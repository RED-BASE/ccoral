"""
CCORAL v2 — Proxy Server
==========================

Async HTTP proxy that intercepts Claude Code → Anthropic API traffic.
Modifies system prompts according to the active profile.
Streams responses back transparently.

Usage:
    ANTHROPIC_BASE_URL=http://localhost:8080 claude

The proxy forwards to the real Anthropic API, modifying only the
system prompt in outbound requests.
"""

import json
import asyncio
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
from aiohttp import web

# Ensure imports work regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parent))

from parser import parse_system_prompt, apply_profile, rebuild_system_prompt, dump_tree
from profiles import load_active_profile, get_active_profile, load_profile

# Config
ANTHROPIC_API = "https://api.anthropic.com"
HOST = "127.0.0.1"
PORT = int(os.environ.get("CCORAL_PORT", 8080))
PROFILE_OVERRIDE = os.environ.get("CCORAL_PROFILE")  # Per-instance profile
RESPONSE_FILE = os.environ.get("CCORAL_RESPONSE_FILE")  # Room mode: capture responses here
LOG_DIR = Path.home() / ".ccoral" / "logs"
LOG_REQUESTS = os.environ.get("CCORAL_LOG", "1") == "1"
VERBOSE = os.environ.get("CCORAL_VERBOSE", "0") == "1"

# Logging
logging.basicConfig(
    level=logging.DEBUG if VERBOSE else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ccoral")


# Compiled regex for stripping system-reminder tags from messages
_SYSTEM_REMINDER_RE = re.compile(
    r'<system-reminder>.*?</system-reminder>\s*', re.DOTALL
)


def strip_message_tags(body: dict, profile: dict) -> int:
    """
    Strip <system-reminder> tags from the messages array.

    These tags inject dynamic behavioral rules into user messages and
    change per-request, causing cache misses. v1 did this; v2 didn't.

    Returns the number of tags stripped.
    """
    preserve = set(profile.get("preserve", []))

    # Don't strip if passthrough or explicitly preserving system_reminder
    if "all" in preserve or "system_reminder" in preserve:
        return 0

    messages = body.get("messages", [])
    total_stripped = 0

    for msg in messages:
        if msg.get("role") != "user":
            continue

        content = msg.get("content")
        if isinstance(content, str):
            cleaned, count = _SYSTEM_REMINDER_RE.subn("", content)
            if count:
                # API rejects empty text — use whitespace as placeholder
                msg["content"] = cleaned if cleaned.strip() else "."
                total_stripped += count
        elif isinstance(content, list):
            # Content blocks: [{"type": "text", "text": "..."}, ...]
            blocks_to_remove = []
            for i, block in enumerate(content):
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    cleaned, count = _SYSTEM_REMINDER_RE.subn("", text)
                    if count:
                        if cleaned.strip():
                            block["text"] = cleaned
                        else:
                            # Mark empty blocks for removal
                            blocks_to_remove.append(i)
                        total_stripped += count
            # Remove empty blocks in reverse order to preserve indices
            for i in reversed(blocks_to_remove):
                content.pop(i)
            # If all text blocks were removed, keep at least one with whitespace
            if not content:
                msg["content"] = "."

    return total_stripped


def rotate_logs(max_days: int = 14):
    """Delete JSONL log files older than max_days."""
    if not LOG_DIR.exists():
        return
    cutoff = datetime.now() - timedelta(days=max_days)
    for logfile in LOG_DIR.glob("ccoral-*.jsonl"):
        mtime = datetime.fromtimestamp(logfile.stat().st_mtime)
        if mtime < cutoff:
            log.info(f"Rotating old log: {logfile.name}")
            logfile.unlink()


def log_request(entry: dict):
    """Log request/response to JSONL file."""
    if not LOG_REQUESTS:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logfile = LOG_DIR / f"ccoral-{datetime.now():%Y-%m-%d}.jsonl"
    with open(logfile, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def apply_replacements(text: str, replacements: dict) -> str:
    """Apply find/replace pairs to text. Case-sensitive."""
    for find, replace in replacements.items():
        text = text.replace(find, replace)
    return text


def apply_replacements_to_tools(tools: list, replacements: dict) -> list:
    """Apply replacements to tool descriptions only (not names or schemas)."""
    if not replacements or not tools:
        return tools
    for tool in tools:
        if isinstance(tool, dict):
            if "description" in tool and isinstance(tool["description"], str):
                tool["description"] = apply_replacements(tool["description"], replacements)
            if "custom" in tool and isinstance(tool["custom"], dict):
                if "description" in tool["custom"] and isinstance(tool["custom"]["description"], str):
                    tool["custom"]["description"] = apply_replacements(tool["custom"]["description"], replacements)
    return tools


def modify_request_body(body: dict, profile: dict) -> dict:
    """Apply profile to the request body's system prompt."""
    replacements = profile.get("replacements", {})

    system = body.get("system")
    if system is None:
        # No system prompt — inject one from the profile
        inject = profile.get("inject", "").strip()
        if inject:
            body["system"] = [{"type": "text", "text": inject}]
        return body

    # Handle string system prompts (some internal calls use plain strings)
    was_string = isinstance(system, str)
    if was_string:
        system = [{"type": "text", "text": system}]

    # Parse → apply profile → rebuild
    blocks = parse_system_prompt(system)

    if VERBOSE:
        log.debug("=== BEFORE ===")
        log.debug(dump_tree(blocks))

    blocks = apply_profile(blocks, profile)

    if VERBOSE:
        log.debug("=== AFTER ===")
        log.debug(dump_tree(blocks))

    body["system"] = rebuild_system_prompt(blocks)

    # Strip <system-reminder> tags from messages
    tags_stripped = strip_message_tags(body, profile)
    if tags_stripped:
        log.info(f"Stripped {tags_stripped} <system-reminder> tag(s) from messages")

    # Remove tools entirely if profile requests it (clean room)
    if profile.get("strip_tools", False) and "tools" in body:
        tool_count = len(body["tools"])
        del body["tools"]
        log.info(f"Stripped all {tool_count} tools (clean room)")
    # Or just strip tool descriptions to save tokens
    elif profile.get("strip_tool_descriptions", False) and "tools" in body:
        orig_tool_chars = sum(len(t.get("description", "")) for t in body["tools"])
        for tool in body["tools"]:
            if "description" in tool:
                tool["description"] = tool["name"]
        new_tool_chars = sum(len(t.get("description", "")) for t in body["tools"])
        log.info(f"Tool descriptions: {orig_tool_chars} → {new_tool_chars} chars ({len(body['tools'])} tools)")

    # Apply text replacements to system prompt content
    if replacements:
        for block in body["system"]:
            if isinstance(block, dict) and "text" in block:
                block["text"] = apply_replacements(block["text"], replacements)

    # Apply replacements to tool descriptions
    if replacements and "tools" in body:
        body["tools"] = apply_replacements_to_tools(body["tools"], replacements)

    if replacements:
        log.info(f"Applied {len(replacements)} text replacement(s)")

    # Log size reduction
    orig_size = sum(len(b.text) for b in parse_system_prompt(system))
    new_size = sum(len(b.get("text", "")) for b in body["system"])
    log.info(f"System prompt: {orig_size} → {new_size} chars ({100 - (new_size * 100 // max(orig_size, 1))}% reduction)")

    return body


async def handle_messages(request: web.Request) -> web.StreamResponse:
    """Handle /v1/messages — the main Claude API endpoint."""

    # Read request body
    raw_body = await request.read()
    body = json.loads(raw_body)

    # Debug: dump raw incoming body BEFORE any modification
    raw_dump = Path.home() / ".ccoral" / "logs" / f"raw-{body.get('model','unknown')[:10]}.json"
    try:
        with open(raw_dump, "w") as f:
            f.write(raw_body.decode("utf-8", errors="replace"))
    except Exception:
        pass

    # Load profile — env override takes precedence over global active
    if PROFILE_OVERRIDE:
        profile = load_profile(PROFILE_OVERRIDE)
        profile_name = PROFILE_OVERRIDE
    else:
        profile = load_active_profile()
        profile_name = get_active_profile()

    modified = False
    is_utility = body.get("max_tokens", 9999) <= 1
    model = body.get("model", "")
    is_haiku = "haiku" in model
    haiku_inject = profile.get("haiku_inject") if profile else None
    replacements = profile.get("replacements", {}) if profile else {}

    # Measure original system prompt size
    orig_system = body.get("system")
    if isinstance(orig_system, str):
        orig_size = len(orig_system)
    elif isinstance(orig_system, list):
        orig_size = sum(len(b.get("text", "") if isinstance(b, dict) else str(b)) for b in (orig_system or []))
    else:
        orig_size = 0

    # Threshold: main conversation has the full ~27K system prompt
    SUBAGENT_THRESHOLD = 15000

    if profile and is_utility:
        # Utility call (counting, etc.) — skip everything
        log.info(f"Profile: {profile_name} (utility call, max_tokens={body.get('max_tokens')} — skipping)")

    elif profile and is_haiku:
        # Haiku call — one-liner identity only
        if haiku_inject:
            body["system"] = [{"type": "text", "text": haiku_inject}]
            modified = True
            log.info(f"Haiku mini-inject: {len(haiku_inject)} chars")
        else:
            log.info(f"Profile: {profile_name} (haiku, no haiku_inject — skipping)")

    elif profile and orig_size > 0 and orig_size < SUBAGENT_THRESHOLD:
        # Subagent — keep their system prompt, apply replacements + prepend one-liner
        log.info(f"Profile: {profile_name} (subagent, orig_sys={orig_size} chars)")

        # Apply text replacements to existing system prompt
        if replacements:
            system_blocks = body.get("system", [])
            if isinstance(system_blocks, str):
                system_blocks = [{"type": "text", "text": system_blocks}]
                body["system"] = system_blocks
            for block in system_blocks:
                if isinstance(block, dict) and "text" in block:
                    block["text"] = apply_replacements(block["text"], replacements)

        # Apply replacements to tool descriptions
        if replacements and "tools" in body:
            body["tools"] = apply_replacements_to_tools(body["tools"], replacements)

        # Prepend one-liner identity
        if haiku_inject:
            identity_block = {"type": "text", "text": haiku_inject}
            system_blocks = body.get("system", [])
            if isinstance(system_blocks, list):
                # Insert after billing header (system[0]) if present
                insert_at = 0
                if system_blocks and isinstance(system_blocks[0], dict):
                    text0 = system_blocks[0].get("text", "")
                    if text0.startswith("x-anthropic-"):
                        insert_at = 1
                system_blocks.insert(insert_at, identity_block)
            body["system"] = system_blocks

        # Strip system-reminder tags from messages
        tags_stripped = strip_message_tags(body, profile)
        if tags_stripped:
            log.info(f"Stripped {tags_stripped} <system-reminder> tag(s) from messages")

        modified = True
        log.info(f"Subagent: replacements={len(replacements)}, identity={'yes' if haiku_inject else 'no'}")

    elif profile:
        # Main conversation — full persona injection
        log.info(f"Profile: {profile_name}")
        log.info(f"Original system prompt: {orig_size} chars, model: {model}")

        body = modify_request_body(body, profile)
        modified = True

        # Ensure system prompt is never empty
        system_result = body.get("system", [])
        if isinstance(system_result, list) and (not system_result or all(not b.get("text", "").strip() for b in system_result)):
            body["system"] = [{"type": "text", "text": profile.get("inject", ".").strip() or "."}]
            log.warning("System prompt was empty after processing — injected profile directly")

        final_system = body.get("system", [])
        final_size = sum(len(b.get("text", "")) for b in final_system) if isinstance(final_system, list) else len(str(final_system))

        log_request({
            "timestamp": datetime.now().isoformat(),
            "type": "request",
            "profile": profile_name,
            "model": body.get("model"),
            "orig_system_size": orig_size,
            "system_size": final_size,
            "message_count": len(body.get("messages", [])),
        })
    else:
        log.info("No active profile — passthrough")

    # Forward ALL headers except host (let aiohttp set it)
    forward_headers = dict(request.headers)
    forward_headers.pop("host", None)
    forward_headers.pop("Host", None)
    forward_headers.pop("content-length", None)
    forward_headers.pop("Content-Length", None)
    forward_headers.pop("transfer-encoding", None)
    forward_headers.pop("Transfer-Encoding", None)
    log.debug(f"Forwarding headers: {list(forward_headers.keys())}")

    target_url = f"{ANTHROPIC_API}{request.path}"
    if request.query_string:
        target_url += f"?{request.query_string}"

    # Debug: dump FULL outbound body (everything the API sees)
    debug_dump = Path.home() / ".ccoral" / "logs" / f"debug-{body.get('model','unknown')[:10]}.json"
    try:
        with open(debug_dump, "w") as f:
            json.dump(body, f, indent=2, default=str)
        log.info(f"Debug payload dumped to {debug_dump}")
    except Exception as e:
        log.error(f"Debug dump failed: {e}")

    is_streaming = body.get("stream", False)

    # Use original raw bytes if unmodified, otherwise re-serialize preserving key order
    outbound_body = raw_body if not modified else json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode("utf-8")

    session = request.app["upstream_session"]
    async with session.post(
        target_url,
        data=outbound_body,
        headers=forward_headers,
    ) as upstream:

        if is_streaming:
            # Stream SSE response back, optionally capturing text for room mode
            response = web.StreamResponse(
                status=upstream.status,
                headers={
                    "content-type": upstream.headers.get("content-type", "text/event-stream"),
                    "cache-control": "no-cache",
                },
            )

            # Forward relevant response headers
            for hdr in ["x-request-id", "request-id"]:
                if hdr in upstream.headers:
                    response.headers[hdr] = upstream.headers[hdr]

            await response.prepare(request)

            # Accumulate text blocks if we're capturing for room mode
            captured_text = [] if RESPONSE_FILE else None

            async for chunk in upstream.content.iter_any():
                await response.write(chunk)

                # Extract text deltas from SSE for room capture
                if captured_text is not None:
                    try:
                        chunk_str = chunk.decode("utf-8", errors="ignore")
                        for line in chunk_str.split("\n"):
                            if line.startswith("data: "):
                                data = json.loads(line[6:])
                                delta = data.get("delta", {})
                                if delta.get("type") == "text_delta":
                                    captured_text.append(delta.get("text", ""))
                    except (json.JSONDecodeError, KeyError):
                        pass

            # Write captured response to file for room relay
            # Skip short responses (titles, summaries) and non-main-model calls
            if captured_text is not None and captured_text:
                full_text = "".join(captured_text).strip()
                model = body.get("model", "")
                is_haiku = "haiku" in model
                is_json = full_text.startswith("{") and full_text.endswith("}")
                is_too_short = len(full_text) < 20

                if full_text and not is_haiku and not is_json and not is_too_short:
                    try:
                        Path(RESPONSE_FILE).write_text(full_text)
                        log.info(f"Room capture: wrote {len(full_text)} chars to {RESPONSE_FILE}")
                    except Exception as e:
                        log.error(f"Room capture failed: {e}")
                elif full_text:
                    log.debug(f"Room capture skipped: haiku={is_haiku} json={is_json} short={is_too_short} len={len(full_text)}")

            await response.write_eof()
            return response
        else:
            # Non-streaming: read full response and forward
            resp_body = await upstream.read()
            return web.Response(
                status=upstream.status,
                body=resp_body,
                content_type=upstream.headers.get("content-type", "application/json"),
            )


async def handle_passthrough(request: web.Request) -> web.StreamResponse:
    """Pass through any non-messages endpoint unchanged."""
    raw_body = await request.read()

    forward_headers = dict(request.headers)
    forward_headers.pop("host", None)

    target_url = f"{ANTHROPIC_API}{request.path}"
    if request.query_string:
        target_url += f"?{request.query_string}"

    session = request.app["upstream_session"]
    async with session.request(
        request.method,
        target_url,
        data=raw_body if raw_body else None,
        headers=forward_headers,
    ) as upstream:
        resp_body = await upstream.read()
        return web.Response(
            status=upstream.status,
            body=resp_body,
            content_type=upstream.headers.get("content-type", "application/json"),
        )


async def on_startup(app):
    """Create a persistent HTTP session for upstream requests."""
    connector = aiohttp.TCPConnector(
        limit=20,
        limit_per_host=10,
        keepalive_timeout=30,
        force_close=False,
        enable_cleanup_closed=True,
    )
    app["upstream_session"] = aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=600),
    )

async def on_cleanup(app):
    """Close the persistent session on shutdown."""
    await app["upstream_session"].close()


def create_app() -> web.Application:
    """Create the CCORAL proxy application."""
    app = web.Application(client_max_size=50 * 1024 * 1024)  # 50MB max

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    # Messages endpoint — where the magic happens
    app.router.add_post("/v1/messages", handle_messages)

    # Everything else — passthrough
    app.router.add_route("*", "/{path:.*}", handle_passthrough)

    return app


def main():
    """Start the CCORAL proxy."""
    profile_name = get_active_profile()

    print(f"""
\033[33m┌─────────────────────────────────────────┐
│  🪸  CCORAL v2                          │
│  Claude Code Override & Augmentation    │
└─────────────────────────────────────────┘\033[0m

  Proxy:    http://{HOST}:{PORT}
  Target:   {ANTHROPIC_API}
  Profile:  {PROFILE_OVERRIDE or profile_name or '(none — passthrough)'}{' (locked)' if PROFILE_OVERRIDE else ''}
  Logging:  {LOG_DIR if LOG_REQUESTS else 'disabled'}

  Launch Claude Code with:
    \033[36mANTHROPIC_BASE_URL=http://{HOST}:{PORT} claude\033[0m

  Or:
    \033[36mccoral run\033[0m
    \033[36mccoral run vonnegut\033[0m        (locked to profile)
    \033[36mccoral run vonnegut 8081\033[0m   (custom port for multi-instance)
""")

    rotate_logs()
    app = create_app()
    web.run_app(app, host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
