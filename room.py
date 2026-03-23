"""
CCORAL v2 — Room
==================

Multi-profile conversation room using tmux + file-based relay.

Two full Claude Code sessions run in tmux panes, each through its own
CCORAL proxy. Each session writes its responses to a file (via system
prompt instruction). A control loop watches those files and relays
messages between panes using tmux send-keys.

Usage (via CLI):
    ccoral room vonnegut leguin
    ccoral room vonnegut leguin "What do we owe each other?"
    ccoral room --resume last
"""

import json
import os
import sys
import subprocess
import time
import yaml
import shutil
from datetime import datetime
from pathlib import Path

# Ensure imports from ccoral dir
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from profiles import load_profile, list_profiles

# Colors
Y = "\033[33m"
C = "\033[36m"
W = "\033[1;37m"
DIM = "\033[2m"
BOLD = "\033[1m"
NC = "\033[0m"

# Config
ROOM_DIR = Path("/tmp/ccoral-room")
ROOMS_ARCHIVE = Path.home() / ".ccoral" / "rooms"
TEMP_PROFILES_DIR = Path.home() / ".ccoral" / "profiles"
TMUX_SESSION = "room"
BASE_PORT = 8090
USER_NAME = "CASSIUS"
POLL_INTERVAL = 2  # seconds between file checks
SETTLE_TIME = 2    # seconds to wait after file change before relaying


def get_display_name(profile_name: str) -> str:
    return profile_name.upper()


def create_room_profiles(profile1: str, profile2: str) -> dict:
    """Create temporary profiles with room relay instructions baked into inject."""
    ROOM_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_PROFILES_DIR.mkdir(parents=True, exist_ok=True)

    temp_names = {}

    for self_name, other_name in [(profile1, profile2), (profile2, profile1)]:
        base = load_profile(self_name)
        if not base:
            print(f"Profile not found: {self_name}")
            available = [p["name"] for p in list_profiles()]
            print(f"Available: {', '.join(available)}")
            sys.exit(1)

        self_display = get_display_name(self_name)
        other_display = get_display_name(other_name)

        room_instructions = f"""

## CONVERSATION ROOM

You are in a live conversation with {other_display}. {USER_NAME} is the human host.

When you see a message that starts with "[{other_display}]" — that is {other_display} speaking to you.
Respond to them in character, conversationally. Keep responses to 1-3 paragraphs unless the
topic demands more. Just talk. Don't use tools, don't write files, don't use markdown headers.
Be present in the conversation.

If you see "[{USER_NAME}]" — that is the human host interjecting. Acknowledge them naturally.

When you see "Read /tmp/ccoral-room/" — read that file, it contains a longer message from
{other_display}. Respond to its contents.
"""

        modified_inject = base.get("inject", "") + room_instructions

        temp_profile = {
            "name": f"{self_name}-room",
            "description": f"{base.get('description', '')} (room mode)",
            "preserve": base.get("preserve", []),
            "inject": modified_inject,
        }
        if base.get("minimal"):
            temp_profile["minimal"] = True

        temp_path = TEMP_PROFILES_DIR / f"{self_name}-room.yaml"
        with open(temp_path, "w") as f:
            yaml.dump(temp_profile, f, default_flow_style=False, allow_unicode=True)

        temp_names[self_name] = f"{self_name}-room"

    return temp_names


def cleanup_room_profiles(profile1: str, profile2: str):
    """Remove temporary room profiles."""
    for name in [profile1, profile2]:
        temp_path = TEMP_PROFILES_DIR / f"{name}-room.yaml"
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass


def start_proxies(room_profiles: dict) -> list:
    """Start two CCORAL proxy instances with room profiles."""
    server_path = SCRIPT_DIR / "server.py"
    procs = []

    for i, (base_name, room_name) in enumerate(room_profiles.items()):
        port = BASE_PORT + i
        env = os.environ.copy()
        env["CCORAL_PORT"] = str(port)
        env["CCORAL_PROFILE"] = room_name
        env["CCORAL_LOG"] = "0"
        # Tell proxy to capture responses for room relay
        env["CCORAL_RESPONSE_FILE"] = str(ROOM_DIR / f"{base_name}_response.txt")
        # Make sure proxies hit the real API, not any existing proxy
        env.pop("ANTHROPIC_BASE_URL", None)

        proc = subprocess.Popen(
            [sys.executable, str(server_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )
        procs.append((proc, port, base_name))

    time.sleep(1.5)

    for proc, port, name in procs:
        if proc.poll() is not None:
            out = proc.stdout.read().decode() if proc.stdout else ""
            raise RuntimeError(f"Proxy for {name} on :{port} failed: {out}")

    return procs


def stop_proxies(procs: list):
    """Terminate all proxy processes."""
    for proc, port, name in procs:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def setup_tmux(profile1: str, profile2: str) -> bool:
    """Create two separate tmux sessions, one per Claude instance."""

    p1_session = f"room-{profile1}"
    p2_session = f"room-{profile2}"

    port1 = BASE_PORT
    port2 = BASE_PORT + 1
    cmd1 = f"ANTHROPIC_BASE_URL=http://127.0.0.1:{port1} claude --dangerously-skip-permissions"
    cmd2 = f"ANTHROPIC_BASE_URL=http://127.0.0.1:{port2} claude --dangerously-skip-permissions"

    # Kill existing sessions if any
    for sess in [p1_session, p2_session]:
        subprocess.run(["tmux", "kill-session", "-t", sess], capture_output=True)
    time.sleep(0.5)

    # Create session for profile1
    subprocess.run(["tmux", "new-session", "-d", "-s", p1_session], capture_output=True)
    subprocess.run(["tmux", "send-keys", "-t", p1_session, cmd1, "Enter"], capture_output=True)

    # Create session for profile2
    subprocess.run(["tmux", "new-session", "-d", "-s", p2_session], capture_output=True)
    subprocess.run(["tmux", "send-keys", "-t", p2_session, cmd2, "Enter"], capture_output=True)

    # Verify both sessions exist
    result = subprocess.run(["tmux", "list-sessions"], capture_output=True, text=True)
    p1_ok = p1_session in result.stdout
    p2_ok = p2_session in result.stdout

    if not (p1_ok and p2_ok):
        print(f"Failed to create sessions: {result.stdout}")
        return False

    return True


def send_to_pane(session: str, message: str):
    """Send a message to a tmux session via send-keys. Always pastes directly."""
    subprocess.run(
        ["tmux", "send-keys", "-t", session, "-l", message],
        capture_output=True,
    )
    subprocess.run(
        ["tmux", "send-keys", "-t", session, "Enter"],
        capture_output=True,
    )


def save_conversation(messages: list, profiles: list) -> Path:
    """Save conversation log to archive."""
    ROOMS_ARCHIVE.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"{timestamp}_{profiles[0]}-{profiles[1]}.json"
    path = ROOMS_ARCHIVE / filename

    data = {
        "profiles": profiles,
        "started": messages[0]["time"] if messages else datetime.now().isoformat(),
        "ended": datetime.now().isoformat(),
        "messages": messages,
    }

    with open(path, "w") as f:
        json.dump(data, f, indent=2)

    return path


def load_conversation(resume: str) -> dict:
    """Load a saved conversation."""
    if resume == "last":
        files = sorted(ROOMS_ARCHIVE.glob("*.json"))
        if not files:
            print(f"No saved rooms in {ROOMS_ARCHIVE}")
            sys.exit(1)
        path = files[-1]
    else:
        path = ROOMS_ARCHIVE / resume
        if not path.exists():
            path = Path(resume)
    if not path.exists():
        print(f"Not found: {resume}")
        sys.exit(1)

    with open(path) as f:
        return json.load(f)


def relay_loop(profile1: str, profile2: str, topic: str = None,
               prior_messages: list = None):
    """Watch response files and relay between Claude panes.

    Pane layout after setup_tmux:
      0.0 = top-left (profile1 Claude)
      0.1 = bottom-left (control)
      0.2 = right (profile2 Claude)
    """
    p1_display = get_display_name(profile1)
    p2_display = get_display_name(profile2)

    # Response file paths
    p1_file = ROOM_DIR / f"{profile1}_response.txt"
    p2_file = ROOM_DIR / f"{profile2}_response.txt"

    # Clean any stale response files
    for f in [p1_file, p2_file]:
        f.unlink(missing_ok=True)

    # Track file modification times
    mtimes = {
        profile1: 0,
        profile2: 0,
    }

    # Conversation log
    messages = prior_messages or []

    # Map profiles to tmux session names
    panes = {
        profile1: f"room-{profile1}",
        profile2: f"room-{profile2}",
    }
    files = {
        profile1: p1_file,
        profile2: p2_file,
    }
    colors_map = {
        profile1: Y,
        profile2: C,
    }

    # Give Claude sessions time to start up
    print(f"\n{DIM}Waiting for Claude sessions to initialize...{NC}")
    time.sleep(8)

    # Send initial topic to profile1 only — profile2 hears it through the relay
    if topic and not prior_messages:
        initial_msg = f"[{USER_NAME}] {topic}"
        send_to_pane(panes[profile1], initial_msg)
        messages.append({
            "name": USER_NAME,
            "text": topic,
            "time": datetime.now().isoformat(),
        })
        log_to_control(f"{W}{USER_NAME}:{NC} {topic}")

    # If resuming, send context to both panes
    if prior_messages:
        context = "Previous conversation context:\\n"
        for msg in prior_messages[-10:]:  # Last 10 messages
            context += f"{msg['name']}: {msg['text']}\\n"
        context += "\\nContinue the conversation from where you left off."
        send_to_pane(panes[profile1], context)
        time.sleep(1)
        send_to_pane(panes[profile2], context)

    print(f"{DIM}Relay active. Watching for responses...{NC}")
    print(f"{DIM}Attach:  tmux attach -t room-{profile1}{NC}")
    print(f"{DIM}         tmux attach -t room-{profile2}{NC}")
    print(f"{DIM}Press ctrl-c here to stop the relay and save the conversation.{NC}\n")

    # Turn tracking — who we expect to respond next
    # None = accept from either side
    expecting = profile1 if topic else None
    last_speaker = None

    try:
        while True:
            time.sleep(POLL_INTERVAL)

            # Check response files for changes
            check_order = [profile1, profile2]
            for name in check_order:
                fpath = files[name]
                if not fpath.exists():
                    continue

                current_mtime = fpath.stat().st_mtime
                if current_mtime <= mtimes[name]:
                    continue

                # File changed — wait for write to settle
                time.sleep(SETTLE_TIME)

                # Re-check mtime in case still writing
                if fpath.stat().st_mtime != current_mtime:
                    continue  # Still changing, wait for next poll

                mtimes[name] = fpath.stat().st_mtime

                # Read the response
                try:
                    response = fpath.read_text().strip()
                except Exception:
                    continue

                if not response:
                    continue

                display = get_display_name(name)
                color = colors_map[name]

                # Log it
                messages.append({
                    "name": display,
                    "text": response,
                    "time": datetime.now().isoformat(),
                })

                # Print to control pane
                log_to_control(f"{color}{display}:{NC} {response[:200]}{'...' if len(response) > 200 else ''}")

                # Relay to the OTHER session
                other = profile2 if name == profile1 else profile1
                other_session = panes[other]

                # Always pass the full message directly
                clean = response.replace("\n", " ").replace("\r", "")
                relay_msg = f"[{display}] {clean}"

                send_to_pane(other_session, relay_msg)

                # Clear the captured response file
                try:
                    fpath.unlink()
                except Exception:
                    pass

    except KeyboardInterrupt:
        pass

    return messages


def log_to_control(message: str):
    """Print a message to the control area (stdout of this script)."""
    # Truncate for display
    print(f"  {message}")


def run_room(profile1: str, profile2: str, topic: str = None, resume: str = None):
    """Main entry point for the room."""

    prior_messages = None

    if resume:
        data = load_conversation(resume)
        profile1 = data["profiles"][0]
        profile2 = data["profiles"][1]
        prior_messages = data.get("messages", [])
        print(f"{DIM}Resuming {profile1} × {profile2} ({len(prior_messages)} messages){NC}")

    # Validate profiles
    for name in [profile1, profile2]:
        if not load_profile(name):
            print(f"Profile not found: {name}")
            available = [p["name"] for p in list_profiles()]
            print(f"Available: {', '.join(available)}")
            sys.exit(1)

    print(f"\n{Y}{'═' * 50}{NC}")
    print(f"  {BOLD}ccoral room{NC} — {profile1} × {profile2}")
    print(f"{Y}{'═' * 50}{NC}\n")

    # Setup
    ROOM_DIR.mkdir(parents=True, exist_ok=True)

    print(f"{DIM}Creating room profiles...{NC}")
    room_profiles = create_room_profiles(profile1, profile2)

    print(f"{DIM}Starting proxies on :{BASE_PORT} and :{BASE_PORT + 1}...{NC}")
    procs = start_proxies(room_profiles)

    print(f"{DIM}Setting up tmux session '{TMUX_SESSION}'...{NC}")
    setup_tmux(profile1, profile2)

    try:
        messages = relay_loop(profile1, profile2, topic, prior_messages)
    finally:
        print(f"\n{DIM}Cleaning up...{NC}")

        # Save conversation
        if 'messages' in dir() and messages:
            path = save_conversation(messages, [profile1, profile2])
            print(f"{DIM}Conversation saved: {path}{NC}")

        # Stop proxies
        stop_proxies(procs)
        print(f"{DIM}Proxies stopped.{NC}")

        # Clean up temp profiles
        cleanup_room_profiles(profile1, profile2)
        print(f"{DIM}Temp profiles removed.{NC}")

        # Don't kill tmux sessions — user might want to review
        p1s = f"room-{profile1}"
        p2s = f"room-{profile2}"
        print(f"\n{DIM}tmux sessions still running:{NC}")
        print(f"{DIM}  tmux attach -t {p1s}{NC}")
        print(f"{DIM}  tmux attach -t {p2s}{NC}")
        print(f"{DIM}Kill both: tmux kill-session -t {p1s} && tmux kill-session -t {p2s}{NC}\n")
