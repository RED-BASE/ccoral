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
import ssl
import sys
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
from aiohttp import web

# certifi provides a maintained CA bundle independent of the system trust store.
# Important for python.org installer Python on macOS, which ships without
# system-CA wiring — aiohttp's default SSL context fails cert verification until
# the user runs /Applications/Python*/Install Certificates.command. Using certifi
# makes the proxy work out of the box on every Python install that has it.
# Fall back to system defaults if certifi isn't available (developer running
# from source without the deps installed).
try:
    import certifi
    _SSL_CONTEXT: "ssl.SSLContext | None" = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = None

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
    """Delete request-log and proxy-stdout log files older than max_days."""
    if not LOG_DIR.exists():
        return
    cutoff = datetime.now() - timedelta(days=max_days)
    for pattern in ("ccoral-*.jsonl", "proxy-*.log"):
        for logfile in LOG_DIR.glob(pattern):
            try:
                mtime = datetime.fromtimestamp(logfile.stat().st_mtime)
            except FileNotFoundError:
                continue
            if mtime < cutoff:
                log.info(f"Rotating old log: {logfile.name}")
                try:
                    logfile.unlink()
                except OSError as e:
                    log.warning(f"Failed to rotate {logfile.name}: {e}")


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

    # Timing instrumentation — measures where latency is.
    # t_req_recv:   CC → CCORAL body received
    # t_upstream_connected:  CCORAL → Anthropic POST accepted (TTFB from API perspective)
    # t_first_chunk: first SSE chunk arrived from Anthropic (model started emitting)
    # t_last_chunk:  last SSE chunk written to CC (full response forwarded)
    # Each big gap tells a different story: a big t_first_chunk gap means Anthropic
    # took time to process the request; a big t_last_chunk means the model output
    # itself was long. CCORAL overhead = (t_req_recv → t_upstream_connected) and
    # between-chunk stalls on our side.
    import time as _time
    t_req_recv = _time.perf_counter()

    # Read request body
    raw_body = await request.read()

    # CRITICAL: json.loads on ~1.5 MB bodies blocks the asyncio event loop for
    # 100-300 ms. Under burst traffic from Claude Code (multiple rapid tool
    # results), this causes Send-Q buildup and apparent hangs. Offload to the
    # default executor (thread pool) so the event loop keeps servicing other
    # incoming requests.
    loop = asyncio.get_running_loop()
    body = await loop.run_in_executor(None, json.loads, raw_body)

    # Debug: dump raw incoming body BEFORE any modification.
    # Previously this was a synchronous file write of up to 1.5 MB on the
    # event loop, which could pause everything for tens to hundreds of ms on
    # a contended disk. Run the whole write in the executor.
    def _write_raw_dump() -> None:
        raw_dump = Path.home() / ".ccoral" / "logs" / f"raw-{body.get('model','unknown')[:10]}.json"
        try:
            with open(raw_dump, "w") as f:
                f.write(raw_body.decode("utf-8", errors="replace"))
        except Exception:
            pass
    # Fire-and-forget: schedule but don't block the request on the dump.
    # run_in_executor returns a Future which we discard — the work is already
    # scheduled on the thread pool. Do NOT wrap in asyncio.create_task — that
    # expects a coroutine and raises TypeError on a Future, returning a 500.
    loop.run_in_executor(None, _write_raw_dump)

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

    # Use original raw bytes if unmodified, otherwise re-serialize preserving
    # key order. json.dumps on ~1.5 MB blocks the event loop ~200-500 ms —
    # offload to thread pool for the same reason as json.loads above.
    if not modified:
        outbound_body = raw_body
    else:
        def _serialize() -> bytes:
            return json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode("utf-8")
        outbound_body = await loop.run_in_executor(None, _serialize)

    session = request.app["upstream_session"]
    t_upstream_start = _time.perf_counter()
    async with session.post(
        target_url,
        data=outbound_body,
        headers=forward_headers,
    ) as upstream:
        t_upstream_connected = _time.perf_counter()

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

            # ONE-SHOT SSE DUMP: if a marker file exists, dump full SSE of next
            # response to it, then delete the marker. Used for debugging stream
            # format changes (e.g. 4.6 → 4.7 thinking delta format).
            dump_marker = Path.home() / ".ccoral" / "logs" / "DUMP_NEXT_SSE"
            dump_target = Path.home() / ".ccoral" / "logs" / "sse-dump.txt"
            should_dump_full = dump_marker.exists()
            full_sse: list[bytes] = [] if should_dump_full else None
            if should_dump_full:
                try:
                    dump_marker.unlink()
                except Exception:
                    pass
                log.info(f"ONE-SHOT SSE dump armed -> {dump_target}")

            # Capture stop_reason + content_block_starts to detect "model stopped
            # without tool_use" freezes. Keep a rolling tail of the raw SSE stream
            # so if the user reports a freeze we can see exactly what came back.
            sse_tail: list[bytes] = []
            SSE_TAIL_MAX = 64 * 1024  # keep last 64KB of the stream
            sse_tail_bytes = 0
            block_starts: list[str] = []  # types of content_block_start
            seen_stop_reason: str | None = None

            t_first_chunk = None
            bytes_streamed = 0
            async for chunk in upstream.content.iter_any():
                if t_first_chunk is None:
                    t_first_chunk = _time.perf_counter()
                bytes_streamed += len(chunk)
                await response.write(chunk)

                # Accumulate rolling tail of raw SSE for post-hoc inspection.
                sse_tail.append(chunk)
                sse_tail_bytes += len(chunk)
                while sse_tail_bytes > SSE_TAIL_MAX and len(sse_tail) > 1:
                    dropped = sse_tail.pop(0)
                    sse_tail_bytes -= len(dropped)

                # One-shot full dump capture
                if full_sse is not None:
                    full_sse.append(chunk)

                # Cheap per-chunk inspection for block types + stop reasons.
                # Decode tolerantly (SSE lines may split across chunks) — the
                # "miss a block across a boundary" rate is acceptable because we
                # also have the raw tail if we need it.
                try:
                    cs = chunk.decode("utf-8", errors="ignore")
                    for line in cs.split("\n"):
                        if not line.startswith("data: "):
                            continue
                        try:
                            data = json.loads(line[6:])
                        except (ValueError, json.JSONDecodeError):
                            continue
                        ev = data.get("type")
                        if ev == "content_block_start":
                            cb = (data.get("content_block") or {}).get("type")
                            if cb:
                                block_starts.append(cb)
                        elif ev == "message_delta":
                            sr = (data.get("delta") or {}).get("stop_reason")
                            if sr:
                                seen_stop_reason = sr
                        # Room-capture text deltas (existing behavior)
                        if captured_text is not None and ev == "content_block_delta":
                            delta = data.get("delta", {})
                            if delta.get("type") == "text_delta":
                                captured_text.append(delta.get("text", ""))
                except Exception:
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

            t_last_chunk = _time.perf_counter()

            # Write one-shot full SSE dump if armed
            if full_sse is not None:
                try:
                    dump_target.write_bytes(b"".join(full_sse))
                    log.info(f"ONE-SHOT SSE dump written: {dump_target} ({sum(len(c) for c in full_sse)} bytes)")
                except Exception as e:
                    log.error(f"SSE dump failed: {e}")

            # Emit timing log. Only for "main conversation" calls (not haiku/utility).
            if modified and not is_utility and not is_haiku:
                ms_body_read = int((t_upstream_start - t_req_recv) * 1000)
                ms_connect = int((t_upstream_connected - t_upstream_start) * 1000)
                ms_ttfb = int(((t_first_chunk or t_upstream_connected) - t_upstream_connected) * 1000)
                ms_stream = int((t_last_chunk - (t_first_chunk or t_upstream_connected)) * 1000)
                ms_total = int((t_last_chunk - t_req_recv) * 1000)
                log.info(
                    f"timing total={ms_total}ms "
                    f"prep={ms_body_read}ms connect={ms_connect}ms "
                    f"ttfb={ms_ttfb}ms stream={ms_stream}ms "
                    f"bytes_out={bytes_streamed} msgs={len(body.get('messages', []))}"
                )
                try:
                    timing_log = Path.home() / ".ccoral" / "logs" / "timings.jsonl"
                    with open(timing_log, "a") as tf:
                        tf.write(json.dumps({
                            "ts": datetime.now().isoformat(),
                            "ms_total": ms_total,
                            "ms_body_read": ms_body_read,
                            "ms_connect": ms_connect,
                            "ms_ttfb": ms_ttfb,
                            "ms_stream": ms_stream,
                            "bytes_out": bytes_streamed,
                            "msgs": len(body.get("messages", [])),
                            "status": upstream.status,
                            "block_starts": block_starts,
                            "stop_reason": seen_stop_reason,
                            "has_tool_use": "tool_use" in block_starts,
                        }) + "\n")
                except Exception:
                    pass

                # Extra red-flag: model ended the turn with no tool_use.
                # With Claude Code, end_turn + no tool_use means the model chose
                # to stop talking without asking to run anything — which the user
                # experiences as "froze". Log it prominently + dump the SSE tail
                # so we can see what text/thinking came back.
                if seen_stop_reason == "end_turn" and "tool_use" not in block_starts:
                    try:
                        freeze_dump = Path.home() / ".ccoral" / "logs" / "end-turn-no-tool.jsonl"
                        tail_bytes = b"".join(sse_tail)
                        with open(freeze_dump, "a") as ff:
                            ff.write(json.dumps({
                                "ts": datetime.now().isoformat(),
                                "msgs": len(body.get("messages", [])),
                                "block_starts": block_starts,
                                "bytes_out": bytes_streamed,
                                "tail_snippet": tail_bytes[-8000:].decode("utf-8", errors="replace"),
                            }) + "\n")
                        log.warning(
                            f"end_turn without tool_use at msgs={len(body.get('messages', []))}; "
                            f"blocks={block_starts}"
                        )
                    except Exception:
                        pass

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


def _build_upstream_connector() -> aiohttp.TCPConnector:
    """Build the aiohttp connector we use for upstream API calls.

    Centralized so the on_startup path and the watchdog-rebuild path can't drift.

    force_close=True: Anthropic's server closes idle keepalive connections
    before our keepalive_timeout fires, leaving sockets in CLOSE_WAIT on our
    side. aiohttp's pool can then hand that poisoned socket to a new request,
    which hangs forever — this was the "silent freeze" failure mode. Fresh
    connection per request costs ~100ms of TLS handshake but removes the class
    of bug entirely.

    ssl=_SSL_CONTEXT: uses certifi's CA bundle when available. See module-level
    comment on _SSL_CONTEXT for why.
    """
    kwargs: dict = {
        "limit": 20,
        "limit_per_host": 10,
        "force_close": True,
        "enable_cleanup_closed": True,
    }
    if _SSL_CONTEXT is not None:
        kwargs["ssl"] = _SSL_CONTEXT
    return aiohttp.TCPConnector(**kwargs)


async def on_startup(app):
    """Create a persistent HTTP session for upstream requests."""
    app["upstream_session"] = aiohttp.ClientSession(
        connector=_build_upstream_connector(),
        timeout=aiohttp.ClientTimeout(total=600, sock_connect=10, sock_read=300),
    )

    # Background sentinel: every 30s, count CLOSE_WAIT sockets owned by this
    # process. If any appear (meaning force_close didn't fully prevent them),
    # log and force a session rebuild. Cheap and self-healing.
    async def close_wait_watchdog():
        import subprocess
        pid = os.getpid()
        while True:
            try:
                await asyncio.sleep(30)
                result = subprocess.run(
                    ["ss", "-tan", "-p"],
                    capture_output=True, text=True, timeout=5,
                )
                # Count CLOSE-WAIT sockets owned by this process
                count = 0
                for line in result.stdout.splitlines():
                    if "CLOSE-WAIT" in line and f"pid={pid}" in line:
                        count += 1
                if count > 0:
                    log.warning(
                        f"close-wait watchdog: {count} poisoned sockets; rebuilding session"
                    )
                    try:
                        old_session = app["upstream_session"]
                        app["upstream_session"] = aiohttp.ClientSession(
                            connector=_build_upstream_connector(),
                            timeout=aiohttp.ClientTimeout(total=600, sock_connect=10, sock_read=300),
                        )
                        await old_session.close()
                    except Exception as e:
                        log.error(f"session rebuild failed: {e}")
            except Exception as e:
                log.debug(f"watchdog tick failed: {e}")

    app["watchdog_task"] = asyncio.create_task(close_wait_watchdog())

async def on_cleanup(app):
    """Close the persistent session on shutdown."""
    task = app.get("watchdog_task")
    if task:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
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
