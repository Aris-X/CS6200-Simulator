#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Digital China (神州数码) CS6200 Switch Interactive CLI Terminal
Entry Point: cli.py

Features:
1. Automatically discovers and loads all 9 JSON command rule files in the
   project directory and attaches CS6200CommandHandlers to CS6200CLIEngine.
2. Full prompt_toolkit TTY integration:
   - <Tab>   : Hardware-authentic keyword completion (auto-appends space on
               unique match; prints aligned candidate columns on ambiguity).
   - <?>     : Immediate context-sensitive help WITHOUT pressing Enter, then
               redraws the current prompt and preserves typed input buffer.
   - <Ctrl+Z>: Instantly exits any configuration view back to privileged EXEC
               mode (Switch# / <hostname>#) and refreshes the prompt.
3. Built-in local debug command:
   - export-json [filename] : Dumps engine.export_state() to a formatted UTF-8
     JSON file without polluting switch running-config or state tree.
4. Configuration persistence:
   - `write` / `write memory` saves startup_config to startup-config.json and
     it is automatically restored the next time cli.py starts.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.application import run_in_terminal
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.styles import Style
except ImportError as exc:
    sys.stderr.write(
        "[ERROR] Missing dependency 'prompt_toolkit'.\n"
        "Please install it via: pip install prompt_toolkit\n"
    )
    raise SystemExit(1) from exc

from engine import CS6200CLIEngine
from handlers import CS6200CommandHandlers


# Base directory containing cli.py, engine.py, handlers.py, and JSON rule files
BASE_DIR = Path(__file__).resolve().parent

# Startup configuration persisted by 'write' and restored automatically on launch
STARTUP_CONFIG_PATH = BASE_DIR / "startup-config.json"

# Canonical 9 JSON rule files extracted from the CS6200 Command Reference
RULE_FILENAMES: List[str] = [
    "core_01_base.json",
    "core_02_l2.json",
    "core_03_l3.json",
    "core_04_security.json",
    "03_system.json",
    "04_system.json",
    "05_system.json",
    "01_system.json",
    "02_system.json",
]


def resolve_rule_paths(base_dir: Path = BASE_DIR) -> List[str]:
    """
    Locate all 9 CS6200 JSON rule files relative to BASE_DIR (also checks
    optional ./rules or ./data subdirectories). Raises FileNotFoundError if
    none of the rule sets can be found.
    """
    search_dirs = [base_dir, base_dir / "rules", base_dir / "data"]
    resolved: List[str] = []
    missing: List[str] = []

    for filename in RULE_FILENAMES:
        found_path: Optional[Path] = None
        for directory in search_dirs:
            candidate = directory / filename
            if candidate.is_file():
                found_path = candidate
                break
        if found_path is not None:
            resolved.append(str(found_path))
        else:
            missing.append(filename)

    if not resolved:
        raise FileNotFoundError(
            f"No CS6200 JSON rule files found in {base_dir}. "
            f"Expected: {', '.join(RULE_FILENAMES)}"
        )

    if missing:
        print(f"[WARN] Optional rule files not found and skipped: {', '.join(missing)}")

    return resolved


def normalize_completions(raw_items: Sequence[Any]) -> List[Tuple[str, str]]:
    """
    Defensively normalize engine.get_completions() results into a sorted,
    deduplicated list of (keyword, description) tuples regardless of whether
    the engine returns List[str], List[Tuple[str, str]], or List[dict].
    """
    seen = set()
    normalized: List[Tuple[str, str]] = []

    for item in raw_items or []:
        if isinstance(item, str):
            token, desc = item.strip(), ""
        elif isinstance(item, (tuple, list)) and len(item) >= 1:
            token = str(item[0]).strip()
            desc = str(item[1]).strip() if len(item) > 1 else ""
        elif isinstance(item, dict):
            token = str(item.get("token") or item.get("word") or "").strip()
            desc = str(item.get("desc") or "").strip()
        else:
            continue

        if token and token not in seen:
            seen.add(token)
            normalized.append((token, desc))

    normalized.sort(key=lambda pair: pair[0].lower())
    return normalized


def longest_common_prefix(words: Sequence[str]) -> str:
    """Compute the longest common prefix across a non-empty list of strings."""
    if not words:
        return ""
    prefix = words[0]
    for word in words[1:]:
        while not word.lower().startswith(prefix.lower()) and prefix:
            prefix = prefix[:-1]
        if not prefix:
            break
    # Preserve canonical casing from the first matching candidate
    return words[0][: len(prefix)]


def query_context_help(engine: CS6200CLIEngine, text_before_cursor: str) -> str:
    """
    Retrieve context-sensitive help text from CS6200CLIEngine without mutating
    the active configuration view stack.
    """
    if hasattr(engine, "get_context_help") and callable(engine.get_context_help):
        return str(engine.get_context_help(text_before_cursor) or "")
    # Fallback: engine.execute_command("<input>?") handles trailing '?' natively
    return str(engine.execute_command(f"{text_before_cursor}?") or "")


def format_candidate_columns(candidates: List[Tuple[str, str]], cols: int = 4, col_width: int = 22) -> str:
    """Render multiple Tab completion candidates in neat switch-style columns."""
    lines: List[str] = []
    row: List[str] = []
    for idx, (token, _) in enumerate(candidates, start=1):
        row.append(f"{token:<{col_width}}")
        if idx % cols == 0:
            lines.append("  " + "".join(row).rstrip())
            row = []
    if row:
        lines.append("  " + "".join(row).rstrip())
    return "\n".join(lines)


def build_key_bindings(engine: CS6200CLIEngine) -> KeyBindings:
    """
    Create prompt_toolkit KeyBindings implementing authentic CS6200 console
    ergonomics: <Tab> completion, immediate <?> help, and <Ctrl+Z> to '#'.
    """
    kb = KeyBindings()

    @kb.add("tab")
    def handle_tab(event: Any) -> None:
        buf = event.current_buffer
        text = buf.document.text_before_cursor
        trailing_space = len(text) == 0 or text[-1].isspace()
        parts = text.split()
        current_token = "" if trailing_space else parts[-1]

        try:
            raw = engine.get_completions(text)
        except Exception:
            raw = []

        candidates = normalize_completions(raw)
        if not candidates:
            return

        if len(candidates) == 1:
            # Unique match: complete token in place and append a trailing space
            completed_word = candidates[0][0]
            if not trailing_space and current_token:
                buf.delete_before_cursor(count=len(current_token))
            buf.insert_text(f"{completed_word} ")
            return

        # Multiple matches: extend to longest common prefix if possible
        words = [c[0] for c in candidates]
        lcp = longest_common_prefix(words)
        if not trailing_space and len(lcp) > len(current_token):
            buf.delete_before_cursor(count=len(current_token))
            buf.insert_text(lcp)

        # Display candidate list cleanly below current line and redraw prompt
        prompt_str = engine.get_prompt()
        updated_text = buf.document.text
        columns_output = format_candidate_columns(candidates)

        def _print_candidates() -> None:
            print(f"{prompt_str}{updated_text}")
            print(columns_output)

        run_in_terminal(_print_candidates)

    @kb.add("?")
    def handle_question_mark(event: Any) -> None:
        buf = event.current_buffer
        text = buf.document.text_before_cursor
        prompt_str = engine.get_prompt()

        try:
            help_text = query_context_help(engine, text)
        except Exception as exc:
            help_text = f"% Help lookup error: {exc}"

        def _print_help() -> None:
            print(f"{prompt_str}{text}?")
            if help_text.strip():
                print(help_text.rstrip())
            else:
                print("  <cr>")

        run_in_terminal(_print_help)

    @kb.add("c-z")
    def handle_ctrl_z(event: Any) -> None:
        buf = event.current_buffer
        prompt_before = engine.get_prompt()

        # Execute Ctrl+Z / end semantics in engine to return to priv ('#') mode
        try:
            if hasattr(engine, "view_stack") and len(engine.view_stack) > 1:
                output = engine.execute_command("\x1a")
            else:
                output = ""
        except Exception:
            output = engine.execute_command("end")

        buf.reset()

        def _print_ctrl_z() -> None:
            print(f"{prompt_before}^Z")
            if output and str(output).strip():
                print(str(output).rstrip())

        run_in_terminal(_print_ctrl_z)
        event.app.invalidate()

    return kb


def handle_export_json(engine: CS6200CLIEngine, raw_line: str) -> None:
    """
    Built-in local debug command: `export-json [filename]`
    Exports engine.export_state() to a pretty-printed UTF-8 JSON file on disk.
    """
    try:
        tokens = shlex.split(raw_line)
    except ValueError:
        tokens = raw_line.strip().split()

    filename = tokens[1] if len(tokens) > 1 else "cs6200_state.json"
    out_path = Path(filename)
    if not out_path.is_absolute():
        out_path = Path.cwd() / out_path

    snapshot = engine.export_state()
    if isinstance(snapshot, str):
        # Re-parse & format if export_state() already returned a JSON string
        try:
            parsed = json.loads(snapshot)
            serialized = json.dumps(parsed, indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            serialized = snapshot
    else:
        serialized = json.dumps(snapshot, indent=2, ensure_ascii=False)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(serialized + "\n", encoding="utf-8")
    byte_len = len(serialized.encode("utf-8"))
    print(f"[DEBUG] State snapshot exported -> {out_path} ({byte_len} bytes)")


def print_banner(rule_count: int) -> None:
    """Display CS6200 startup console banner and local simulator hotkeys."""
    banner = f"""
==============================================================================
  Digital China Networks (神州数码) CS6200-28X-EI Switch CLI Simulator
  Loaded {rule_count} JSON rule modules | Interactive TTY Mode Ready
------------------------------------------------------------------------------
  Console Shortcuts & Local Debug Commands:
    <Tab>                  Auto-complete command keyword or list candidates
    <?>                    Instant context-sensitive help (no Enter needed)
    <Ctrl+Z>               Exit current config view directly to Switch#
    write / write memory   Save running-config to startup-config.json (auto-restored on next launch)
    export-json [file]     Export engine state tree to JSON (default: cs6200_state.json)
    quit / exit-cli        Terminate local CLI simulator session
==============================================================================
"""
    print(banner.strip("\n"))


def main() -> int:
    """Initialize CS6200CLIEngine + CS6200CommandHandlers and start TTY loop."""
    try:
        rule_paths = resolve_rule_paths(BASE_DIR)
    except FileNotFoundError as exc:
        sys.stderr.write(f"[FATAL] {exc}\n")
        return 1

    engine = CS6200CLIEngine(rule_paths, persist_path=str(STARTUP_CONFIG_PATH))
    _handlers = CS6200CommandHandlers(engine)  # Binds engine.handler_callback

    # 自动恢复上次 'write' 保存的启动配置
    if engine.load_startup_config():
        print(f"[INFO] Startup configuration restored from {STARTUP_CONFIG_PATH.name}")
    elif STARTUP_CONFIG_PATH.exists():
        print(f"[WARN] Failed to load startup configuration: {STARTUP_CONFIG_PATH}")

    print_banner(len(rule_paths))

    style = Style.from_dict({"prompt": "bold ansicyan"})
    session: PromptSession[str] = PromptSession(
        history=InMemoryHistory(),
        key_bindings=build_key_bindings(engine),
        style=style,
    )

    while True:
        try:
            raw_line = session.prompt(
                lambda: FormattedText([("class:prompt", engine.get_prompt())])
            )
        except KeyboardInterrupt:
            # Ctrl+C clears current line just like physical switch console
            print("^C")
            continue
        except EOFError:
            # Ctrl+D exits simulator cleanly
            print("\n[INFO] Console session closed.")
            break

        stripped = raw_line.strip()
        if not stripped:
            continue

        # Local simulator meta-commands (excluded from switch state machine)
        lowered = stripped.lower()
        if lowered in {"quit", "exit-cli"}:
            print("[INFO] Exiting CS6200 CLI simulator.")
            break

        if lowered == "export-json" or lowered.startswith("export-json "):
            handle_export_json(engine, stripped)
            continue

        # Dispatch command into CS6200CLIEngine -> CS6200CommandHandlers
        output = engine.execute_command(raw_line)
        if output is not None and str(output).strip():
            print(str(output).rstrip())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
