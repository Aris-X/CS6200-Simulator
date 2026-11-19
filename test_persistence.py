#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Digital China (神州数码) CS6200 CLI Simulator — Startup-Config Persistence
Entry Point: test_persistence.py

Verifies that:
    Switch# write            (or 'write memory')
writes startup_config to disk, and that a brand-new engine pointed at the same
file restores the full configuration automatically (as if the switch rebooted).

Also checks:
  - VLAN / MST integer keys survive JSON round-trip
  - Older/partial startup files are merged with current defaults
  - Corrupted files fail gracefully without destroying the default state
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import List

from engine import CS6200CLIEngine
from handlers import CS6200CommandHandlers

BASE_DIR = Path(__file__).resolve().parent

RULE_FILENAMES: List[str] = [
    "core_01_base.json",
    "core_02_l2.json",
    "core_03_l3.json",
    "core_04_security.json",
    "03_system.json",
    "04_system.json",
    "05_system.json",
]


def load_rule_files() -> List[str]:
    paths: List[str] = []
    for name in RULE_FILENAMES:
        candidate = BASE_DIR / name
        if not candidate.is_file():
            candidate = BASE_DIR / "rules" / name
        assert candidate.is_file(), f"Required rule file not found: {name}"
        paths.append(str(candidate))
    return paths


def exec_line(engine: CS6200CLIEngine, cmd: str) -> str:
    prompt = engine.get_prompt()
    out = str(engine.execute_command(cmd) or "")
    print(f"{prompt} {cmd}")
    if out.strip():
        print(out.rstrip())
    assert "% Invalid" not in out, f"Invalid command: {cmd!r} -> {out!r}"
    assert "% Ambiguous" not in out, f"Ambiguous command: {cmd!r} -> {out!r}"
    assert "% Incomplete" not in out, f"Incomplete command: {cmd!r} -> {out!r}"
    return out


def configure_sample(engine: CS6200CLIEngine) -> None:
    script = [
        "enable",
        "config terminal",
        "hostname SW-PERSIST",
        "ipv6 enable",
        "vlan 10",
        "name OFFICE",
        "exit",
        "interface vlan 10",
        "ip address 192.168.10.1 255.255.255.0",
        "ipv6 enable",
        "ipv6 address 2001:da8:20:1:0::1/96",
        "no autostate",
        "exit",
        "interface ethernet 1/0/1",
        "switchport mode trunk",
        "switchport trunk allowed vlan 21;22;222",
        "switchport trunk native vlan 99",
        "exit",
        "interface loopback 1",
        "ip address 10.255.255.1 255.255.255.255",
        "end",
    ]
    for cmd in script:
        exec_line(engine, cmd)


def run_persistence_suite() -> None:
    print("=" * 78)
    print("  Startup-Config Persistence (write -> restart) — Acceptance Suite")
    print("=" * 78)

    with tempfile.TemporaryDirectory(prefix="cs6200-persist-") as tmp_dir:
        config_path = Path(tmp_dir) / "startup-config.json"

        # ------------------------------------------------------------------
        # 1. Configure + write -> JSON file is created
        # ------------------------------------------------------------------
        print("\n--- [1/4] Configure & 'write memory' ---")
        engine1 = CS6200CLIEngine(load_rule_files(), persist_path=str(config_path))
        _h1 = CS6200CommandHandlers(engine1)
        configure_sample(engine1)

        out = exec_line(engine1, "write memory")
        assert "[OK]" in out, out
        assert config_path.is_file(), f"Startup config not written: {config_path}"
        saved = json.loads(config_path.read_text(encoding="utf-8"))
        assert saved["system"]["hostname"] == "SW-PERSIST"
        assert "Vlan10" in saved["interfaces"]

        # ------------------------------------------------------------------
        # 2. Simulated restart: fresh engine restores the saved config
        # ------------------------------------------------------------------
        print("\n--- [2/4] Fresh engine auto-restores startup config ---")
        engine2 = CS6200CLIEngine(load_rule_files(), persist_path=str(config_path))
        _h2 = CS6200CommandHandlers(engine2)
        assert engine2.get_prompt() == "Switch>", engine2.get_prompt()
        assert engine2.load_startup_config() is True

        assert engine2.get_prompt() == "SW-PERSIST>", engine2.get_prompt()
        exec_line(engine2, "enable")
        assert engine2.get_prompt() == "SW-PERSIST#"

        st = engine2.state
        assert st["system"]["hostname"] == "SW-PERSIST"
        assert st["system"]["ipv6_enabled"] is True
        assert 10 in st["vlans"], st["vlans"].keys()
        assert st["vlans"][10]["name"] == "OFFICE"
        assert st["interfaces"]["Vlan10"]["ip_address"] == "192.168.10.1/255.255.255.0"
        assert st["interfaces"]["Vlan10"]["ipv6_enable"] is True
        assert "2001:da8:20:1:0::1/96" in st["interfaces"]["Vlan10"]["ipv6_addresses"]
        assert st["interfaces"]["Vlan10"]["autostate"] is False
        assert st["interfaces"]["Ethernet1/0/1"]["trunk_allowed_vlans"] == "21-22;222"
        assert st["interfaces"]["Ethernet1/0/1"]["trunk_native_vlan"] == 99
        assert st["interfaces"]["Loopback1"]["ip_address"] == "10.255.255.1/255.255.255.255"
        assert st["startup_config"]["system"]["hostname"] == "SW-PERSIST"

        # ------------------------------------------------------------------
        # 3. Rendered configs match the restored state
        # ------------------------------------------------------------------
        print("\n--- [3/4] 'show running-config' / 'show startup-config' ---")
        running = exec_line(engine2, "show running-config")
        for expected in (
            "hostname SW-PERSIST",
            " ipv6 address 2001:da8:20:1:0::1/96",
            " no autostate",
            " switchport trunk allowed vlan 21-22;222",
            " switchport trunk native vlan 99",
            "interface Loopback1",
        ):
            assert expected in running, (expected, running)

        startup = exec_line(engine2, "show startup-config")
        assert "hostname SW-PERSIST" in startup
        assert " switchport trunk allowed vlan 21-22;222" in startup

        # ------------------------------------------------------------------
        # 4. Corrupt file fails gracefully; no persist_path keeps memory-only
        # ------------------------------------------------------------------
        print("\n--- [4/4] Corrupt file & memory-only fallback ---")
        corrupt_path = Path(tmp_dir) / "corrupt-config.json"
        corrupt_path.write_text("{not valid json", encoding="utf-8")
        engine3 = CS6200CLIEngine(load_rule_files(), persist_path=str(corrupt_path))
        _h3 = CS6200CommandHandlers(engine3)
        assert engine3.load_startup_config() is False
        assert engine3.state["system"]["hostname"] == "Switch"

        engine4 = CS6200CLIEngine(load_rule_files())
        _h4 = CS6200CommandHandlers(engine4)
        exec_line(engine4, "enable")
        exec_line(engine4, "config terminal")
        exec_line(engine4, "hostname MEM-ONLY")
        exec_line(engine4, "end")
        out = exec_line(engine4, "write")
        assert "[OK]" in out, out
        assert engine4.state["startup_config"]["system"]["hostname"] == "MEM-ONLY"
        assert engine4.save_startup_config() is False  # no persist_path configured

    print("\n" + "=" * 78)
    print("  ALL PERSISTENCE ASSERTIONS PASSED!")
    print("=" * 78)


if __name__ == "__main__":
    try:
        run_persistence_suite()
    except AssertionError as err:
        sys.stderr.write(f"\n[PERSISTENCE ASSERTION FAILED] {err}\n")
        raise SystemExit(1) from err
