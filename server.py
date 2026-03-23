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
import sys
from datetime import datetime
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


def log_request(entry: dict):
    """Log request/response to JSONL file."""
    if not LOG_REQUESTS:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logfile = LOG_DIR / f"ccoral-{datetime.now():%Y-%m-%d}.jsonl"
    with open(logfile, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def modify_request_body(body: dict, profile: dict) -> dict:
    """Apply profile to the request body's system prompt."""
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

    # Load profile — env override takes precedence over global active
    if PROFILE_OVERRIDE:
        profile = load_profile(PROFILE_OVERRIDE)
        profile_name = PROFILE_OVERRIDE
    else:
        profile = load_active_profile()
        profile_name = get_active_profile()

    if profile:
        log.info(f"Profile: {profile_name}")

        # Log original system prompt for debugging
        orig_system = body.get("system", [])
        if isinstance(orig_system, str):
            orig_size = len(orig_system)
        elif isinstance(orig_system, list):
            orig_size = sum(len(b.get("text", "") if isinstance(b, dict) else str(b)) for b in orig_system)
        else:
            orig_size = 0

        log.info(f"Original system prompt: {orig_size} chars, model: {body.get('model')}")

        if VERBOSE or orig_size < 200:
            # Log small/unusual prompts for debugging
            log.debug(f"System prompt content: {json.dumps(orig_system)[:500]}")

        body = modify_request_body(body, profile)

        # Ensure system prompt is never empty — API requires at least one block
        system_result = body.get("system", [])
        if isinstance(system_result, list) and (not system_result or all(not b.get("text", "").strip() for b in system_result)):
            body["system"] = [{"type": "text", "text": profile.get("inject", ".").strip() or "."}]
            log.warning("System prompt was empty after processing — injected profile directly")

        # Log the modified request
        final_system = body.get("system", [])
        if isinstance(final_system, list):
            final_size = sum(len(b.get("text", "")) for b in final_system)
        else:
            final_size = len(str(final_system))

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

    # Forward headers (preserve auth, content type, etc.)
    forward_headers = {}
    for key in ["x-api-key", "anthropic-version", "anthropic-beta",
                "content-type", "accept", "anthropic-dangerous-direct-browser-access"]:
        if key in request.headers:
            forward_headers[key] = request.headers[key]
    # Also forward any auth headers
    if "authorization" in request.headers:
        forward_headers["authorization"] = request.headers["authorization"]

    target_url = f"{ANTHROPIC_API}{request.path}"
    if request.query_string:
        target_url += f"?{request.query_string}"

    is_streaming = body.get("stream", False)

    async with aiohttp.ClientSession() as session:
        async with session.post(
            target_url,
            json=body,
            headers=forward_headers,
            timeout=aiohttp.ClientTimeout(total=600),
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

    async with aiohttp.ClientSession() as session:
        async with session.request(
            request.method,
            target_url,
            data=raw_body if raw_body else None,
            headers=forward_headers,
            timeout=aiohttp.ClientTimeout(total=300),
        ) as upstream:
            resp_body = await upstream.read()
            return web.Response(
                status=upstream.status,
                body=resp_body,
                content_type=upstream.headers.get("content-type", "application/json"),
            )


def create_app() -> web.Application:
    """Create the CCORAL proxy application."""
    app = web.Application(client_max_size=50 * 1024 * 1024)  # 50MB max

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

    app = create_app()
    web.run_app(app, host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
