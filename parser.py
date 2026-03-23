"""
CCORAL v2 — Structural System Prompt Parser
=============================================

Parses Claude Code's system prompt into a tree of sections,
enabling surgical replacement without fragile regex matching.

The API sends system prompts as a list of content blocks:
  [{"type": "text", "text": "...", "cache_control": {...}}, ...]

Within text blocks, sections are delimited by:
  - Markdown headers (# Section Name)
  - XML tags (<system-reminder>, <available-deferred-tools>, etc.)
  - The identity sentence ("You are Claude Code...")

The parser builds a tree: Blocks → Sections → Content
Decisions happen at the section level: keep / strip / replace.
"""

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Section:
    """A logical section within a system prompt block."""
    name: str           # Identifier (e.g., "doing_tasks", "environment", "identity")
    content: str        # Raw text content
    section_type: str   # "markdown_header", "xml_tag", "identity", "unknown"
    header_level: int = 0  # For markdown headers: 1 = #, 2 = ##, etc.
    keep: bool = True   # Whether to preserve this section


@dataclass
class Block:
    """A content block from the API payload."""
    index: int
    text: str
    cache_control: Optional[dict] = None
    sections: list[Section] = field(default_factory=list)
    original: Optional[dict] = None  # Original API block dict


# Known section identifiers and their canonical names
# Maps (header_text or pattern) → canonical_name
SECTION_IDENTIFIERS = {
    # Identity
    "you are claude code": "identity",

    # Core behavior
    "system": "system",
    "doing tasks": "doing_tasks",
    "using your tools": "using_tools",
    "tool usage policy": "tool_usage_policy",
    "tone and style": "tone_style",
    "output efficiency": "output_efficiency",
    "executing actions with care": "executing_actions",
    "auto memory": "auto_memory",

    # Environment & config
    "environment": "environment",
    "committing changes with git": "git_commit",
    "creating pull requests": "pull_requests",
    "other common operations": "other_operations",

    # Security
    "important: assist with authorized": "security_policy",
    "important: you must never": "url_policy",

    # XML sections
    "available-deferred-tools": "deferred_tools",
    "system-reminder": "system_reminder",
    "fast_mode_info": "fast_mode",

    # Claude.md / project instructions
    "claudemd": "claude_md",
    "currentdate": "current_date",
    "memory index": "memory_index",
}


def _identify_section(line: str) -> tuple[Optional[str], str, int]:
    """
    Identify what section a line starts.

    Returns: (canonical_name, section_type, header_level) or (None, "", 0)
    """
    stripped = line.strip()

    # XML opening tags
    xml_match = re.match(r'^<([a-zA-Z_-]+)(?:\s|>)', stripped)
    if xml_match:
        tag = xml_match.group(1).lower()
        for pattern, name in SECTION_IDENTIFIERS.items():
            if tag == pattern:
                return name, "xml_tag", 0

    # Markdown headers
    header_match = re.match(r'^(#{1,4})\s+(.+)', stripped)
    if header_match:
        level = len(header_match.group(1))
        header_text = header_match.group(2).strip().lower()
        for pattern, name in SECTION_IDENTIFIERS.items():
            if header_text.startswith(pattern):
                return name, "markdown_header", level
        # Unknown header — still a section boundary
        slug = re.sub(r'[^a-z0-9]+', '_', header_text).strip('_')
        return f"header_{slug}", "markdown_header", level

    # Identity sentence
    if stripped.lower().startswith("you are claude code"):
        return "identity", "identity", 0

    # IMPORTANT: lines (standalone policy statements)
    if stripped.startswith("IMPORTANT:"):
        lower = stripped.lower()
        for pattern, name in SECTION_IDENTIFIERS.items():
            if lower.startswith(pattern.lower()):
                return name, "important", 0

    return None, "", 0


def parse_text_block(text: str) -> list[Section]:
    """Parse a text block into sections."""
    lines = text.split('\n')
    sections = []
    current_name = None
    current_type = "unknown"
    current_level = 0
    current_lines = []
    in_xml = False
    xml_tag = None

    for line in lines:
        # Check for XML closing tag
        if in_xml and xml_tag:
            current_lines.append(line)
            if re.match(rf'^</{xml_tag}', line.strip()):
                in_xml = False
            continue

        # Check for new section
        name, stype, level = _identify_section(line)

        if name is not None:
            # Save previous section
            if current_name is not None:
                sections.append(Section(
                    name=current_name,
                    content='\n'.join(current_lines),
                    section_type=current_type,
                    header_level=current_level,
                ))
            elif current_lines:
                # Content before first identified section
                sections.append(Section(
                    name="_preamble",
                    content='\n'.join(current_lines),
                    section_type="preamble",
                ))

            current_name = name
            current_type = stype
            current_level = level
            current_lines = [line]

            # Track XML blocks to capture until closing tag
            if stype == "xml_tag":
                xml_match = re.match(r'^<([a-zA-Z_-]+)', line.strip())
                if xml_match:
                    xml_tag = xml_match.group(1)
                    in_xml = True
        else:
            current_lines.append(line)

    # Save final section
    if current_name is not None:
        sections.append(Section(
            name=current_name,
            content='\n'.join(current_lines),
            section_type=current_type,
            header_level=current_level,
        ))
    elif current_lines:
        sections.append(Section(
            name="_preamble",
            content='\n'.join(current_lines),
            section_type="preamble",
        ))

    return sections


def parse_system_prompt(system: list | str) -> list[Block]:
    """
    Parse the full system prompt from the API payload.

    Handles both string and list-of-blocks formats.
    """
    if isinstance(system, str):
        block = Block(index=0, text=system)
        block.sections = parse_text_block(system)
        return [block]

    blocks = []
    for i, item in enumerate(system):
        if isinstance(item, dict):
            text = item.get("text", "")
            cache = item.get("cache_control")
            original = item
        elif isinstance(item, str):
            text = item
            cache = None
            original = {"type": "text", "text": text}
        else:
            continue

        block = Block(index=i, text=text, cache_control=cache, original=original)
        block.sections = parse_text_block(text)
        blocks.append(block)

    return blocks


def rebuild_system_prompt(blocks: list[Block]) -> list[dict]:
    """Rebuild the API system prompt from (potentially modified) blocks."""
    result = []
    for block in blocks:
        # Rebuild text from kept sections
        kept = [s.content for s in block.sections if s.keep]
        text = '\n\n'.join(kept) if kept else ""

        if not text and not block.cache_control:
            continue

        entry = {"type": "text", "text": text}
        if block.cache_control:
            entry["cache_control"] = block.cache_control
        result.append(entry)

    return result


def apply_profile(blocks: list[Block], profile: dict) -> list[Block]:
    """
    Apply a profile's keep/strip/inject rules to parsed blocks.

    Profile dict:
        inject: str          — content to inject (replaces identity)
        preserve: list[str]  — section names to keep
        strip: "all_else"    — strip everything not in preserve
        minimal: bool        — if true, strip everything, just inject
    """
    inject_text = profile.get("inject", "").strip()
    preserve = set(profile.get("preserve", []))
    minimal = profile.get("minimal", False)

    # "all" means keep everything
    keep_all = "all" in preserve

    if keep_all:
        # Passthrough — don't touch anything
        return blocks

    # Map friendly names to canonical section names
    preserve_map = {
        "environment": "environment",
        "hooks": "hooks_info",
        "mcp": "deferred_tools",
        "tools": "deferred_tools",
        "claude_md": "claude_md",
        "memory": "memory_index",
        "system": "system",
        "current_date": "current_date",
    }
    canonical_preserve = set()
    for p in preserve:
        canonical_preserve.add(preserve_map.get(p, p))

    # Default preserves unless minimal
    if not minimal:
        canonical_preserve.update({"environment", "deferred_tools", "current_date"})

    # Also preserve system-reminder and claude_md content by default
    # (these contain project context the user set up intentionally)
    if not minimal:
        canonical_preserve.update({"system_reminder", "claude_md", "memory_index"})

    injected = False
    for block in blocks:
        for section in block.sections:
            if section.name == "identity" and inject_text:
                # Replace identity with injection
                section.content = inject_text
                section.keep = True
                injected = True
            elif minimal:
                section.keep = False
            elif section.name in canonical_preserve:
                section.keep = True
            elif section.name.startswith("_"):
                # Preambles — keep by default (often structural)
                section.keep = True
            else:
                section.keep = False

    # If we never found an identity section, prepend injection to first block
    if not injected and inject_text and blocks:
        inject_section = Section(
            name="injection",
            content=inject_text,
            section_type="injection",
        )
        blocks[0].sections.insert(0, inject_section)

    return blocks


def dump_tree(blocks: list[Block]) -> str:
    """Debug: print the section tree."""
    lines = []
    for block in blocks:
        lines.append(f"Block {block.index} ({len(block.text)} chars)"
                      f"{' [cached]' if block.cache_control else ''}")
        for s in block.sections:
            status = "KEEP" if s.keep else "STRIP"
            preview = s.content[:80].replace('\n', '\\n')
            lines.append(f"  [{status}] {s.name} ({s.section_type}) — {preview}...")
    return '\n'.join(lines)
