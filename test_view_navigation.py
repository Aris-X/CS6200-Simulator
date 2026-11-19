#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Digital China (神州数码) CS6200 CLI Simulator — Direct View Jump Acceptance
Entry Point: test_view_navigation.py

Verifies the DCN real-device behavior of jumping between configuration views
without typing 'exit' first, e.g.:

    SW1(config-if-vlan2017)# interface vlan 2018
    SW1(config)# vlan 20
    SW1(config-if-ethernet1/0/1)# router ospf 1

Key rule under test: the jump only happens when the line has NO local match in
the current sub-view but fully matches a global-mode rule, so in-view commands
such as 'port-group 1 mode on' or 'shutdown' must NOT trigger a jump.
"""

from __future__ import annotations

import sys
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
    """Execute one CLI line, echo prompt + output, return the raw output."""
    prompt = engine.get_prompt()
    out = str(engine.execute_command(cmd) or "")
    print(f"{prompt} {cmd}")
    if out.strip():
        print(out.rstrip())
    assert "% Invalid" not in out, f"Invalid command: {cmd!r} -> {out!r}"
    assert "% Ambiguous" not in out, f"Ambiguous command: {cmd!r} -> {out!r}"
    assert "% Incomplete" not in out, f"Incomplete command: {cmd!r} -> {out!r}"
    return out


def assert_prompt(engine: CS6200CLIEngine, expected: str) -> None:
    actual = engine.get_prompt()
    assert actual == expected, f"Prompt mismatch: {actual!r} != {expected!r}"


def run_view_navigation_suite() -> None:
    engine = CS6200CLIEngine(load_rule_files())
    _handlers = CS6200CommandHandlers(engine)

    print("=" * 78)
    print("  Direct View Jump (DCN behavior) — Acceptance Suite")
    print("=" * 78)

    # ------------------------------------------------------------------
    # 1. Interface -> Interface jump
    # ------------------------------------------------------------------
    print("\n--- [1/5] Loopback1 -> Vlan10 -> back to Loopback1 ---")
    exec_line(engine, "enable")
    exec_line(engine, "config terminal")

    # Native global IPv6 enablement (CS6200 has no ip routing command)
    exec_line(engine, "ipv6 enable")

    exec_line(engine, "interface loopback 1")
    assert_prompt(engine, "Switch(Config-If-Loopback1)#")

    # Tab completion / '?' help in a sub-view must offer global jump commands
    assert "vlan" in engine.get_completions("vl"), engine.get_completions("vl")
    sub_view_completions = engine.get_completions("")
    for kw in ("vlan", "interface", "router", "ip"):
        assert kw in sub_view_completions, (kw, sub_view_completions)
    assert "vlan WORD" in engine.get_context_help(""), engine.get_context_help("")

    exec_line(engine, "ip address 10.255.255.1 255.255.255.255")

    # Direct jump without 'exit' (DCN: SW1(config-if-loopback1)# interface vlan 10)
    exec_line(engine, "interface vlan 10")
    assert_prompt(engine, "Switch(Config-If-Vlan10)#")
    exec_line(engine, "ip address 192.168.10.1 255.255.255.0")
    exec_line(engine, "ipv6 enable")
    exec_line(engine, "ipv6 address 2001:da8:20:1:0::1/96")
    exec_line(engine, "no autostate")
    exec_line(engine, "no shutdown")

    # Jump back to the loopback interface
    exec_line(engine, "interface loopback 1")
    assert_prompt(engine, "Switch(Config-If-Loopback1)#")

    # 'exit' must return to global, not to the previous interface
    exec_line(engine, "exit")
    assert_prompt(engine, "Switch(config)#")

    # ------------------------------------------------------------------
    # 2. Interface -> Vlan -> Router -> DHCP pool -> Interface jumps
    # ------------------------------------------------------------------
    print("\n--- [2/5] if -> vlan -> router -> dhcp-pool -> if ---")
    exec_line(engine, "interface ethernet 1/0/1")
    assert_prompt(engine, "Switch(Config-If-Ethernet1/0/1)#")

    exec_line(engine, "vlan 20")
    assert_prompt(engine, "Switch(Config-Vlan20)#")
    # After the jump, local view keywords still complete normally
    assert "name" in engine.get_completions("na"), engine.get_completions("na")
    exec_line(engine, "name OFFICE")

    exec_line(engine, "router ospf 1")
    assert_prompt(engine, "Switch(Config-Router)#")
    exec_line(engine, "network 192.168.20.0 0.0.0.255 area 0")

    exec_line(engine, "ip dhcp pool P1")
    assert_prompt(engine, "Switch(dhcp-config)#")
    exec_line(engine, "network-address 192.168.20.0 255.255.255.0")

    exec_line(engine, "interface ethernet 1/0/2")
    assert_prompt(engine, "Switch(Config-If-Ethernet1/0/2)#")

    # ------------------------------------------------------------------
    # 3. In-view commands must NOT trigger a jump
    # ------------------------------------------------------------------
    print("\n--- [3/5] Local in-view commands must not jump ---")
    exec_line(engine, "port-group 1 mode on")
    assert_prompt(engine, "Switch(Config-If-Ethernet1/0/2)#")
    assert "Ethernet1/0/2" in engine.state["port_groups"]["1"]["members"]

    exec_line(engine, "shutdown")
    assert_prompt(engine, "Switch(Config-If-Ethernet1/0/2)#")
    assert engine.state["interfaces"]["Ethernet1/0/2"]["shutdown"] is True
    exec_line(engine, "no shutdown")

    # VLAN-mode local command（CS6200 的 vlan 视图仅支持 name，无 shutdown）
    exec_line(engine, "vlan 20")
    assert_prompt(engine, "Switch(Config-Vlan20)#")
    exec_line(engine, "name SALES")
    assert_prompt(engine, "Switch(Config-Vlan20)#")
    assert engine.state["vlans"][20]["name"] == "SALES"

    # ------------------------------------------------------------------
    # 4. Abbreviated jump & 'no interface' from a sub-view
    # ------------------------------------------------------------------
    print("\n--- [4/5] Abbreviated 'int lo 2' & 'no interface loopback' from vlan view ---")
    exec_line(engine, "int lo 2")
    assert_prompt(engine, "Switch(Config-If-Loopback2)#")
    exec_line(engine, "ip address 10.255.255.2 255.255.255.255")

    exec_line(engine, "vlan 20")
    assert_prompt(engine, "Switch(Config-Vlan20)#")
    exec_line(engine, "no interface loopback 2")
    assert_prompt(engine, "Switch(config)#")
    assert "Loopback2" not in engine.state["interfaces"]

    # ------------------------------------------------------------------
    # 5. show commands stay in the current view; 'end' returns to priv
    # ------------------------------------------------------------------
    print("\n--- [5/5] 'show' stays in view & 'end' returns to priv ---")
    exec_line(engine, "interface loopback 1")
    assert_prompt(engine, "Switch(Config-If-Loopback1)#")
    out = exec_line(engine, "show ip interface loopback 1")
    assert "Loopback1 is up" in out, out
    assert_prompt(engine, "Switch(Config-If-Loopback1)#")

    exec_line(engine, "end")
    assert_prompt(engine, "Switch#")

    # `show running-config` must render IPv4/IPv6 routing and dual-stack config
    running = exec_line(engine, "show running-config")
    for expected in (
        "ipv6 enable",
        " ipv6 address 2001:da8:20:1:0::1/96",
        " no autostate",
    ):
        assert expected in running, (expected, running)

    # Final state assertions
    assert engine.state["system"]["ipv6_enabled"] is True
    assert engine.state["interfaces"]["Vlan10"]["ipv6_enable"] is True
    assert engine.state["interfaces"]["Vlan10"]["autostate"] is False
    assert engine.state["interfaces"]["Loopback1"]["ip_address"] == "10.255.255.1/255.255.255.255"
    assert engine.state["interfaces"]["Vlan10"]["ip_address"] == "192.168.10.1/255.255.255.0"
    assert engine.state["vlans"][20]["name"] == "SALES"
    assert any(n["network"] == "192.168.20.0" for n in engine.state["ospf"]["processes"]["1"]["networks"])
    assert engine.state["dhcp_pools"]["P1"]["network"] == "192.168.20.0 255.255.255.0"

    print("\n" + "=" * 78)
    print("  ALL VIEW-JUMP ASSERTIONS PASSED!")
    print("=" * 78)


if __name__ == "__main__":
    try:
        run_view_navigation_suite()
    except AssertionError as err:
        sys.stderr.write(f"\n[VIEW-JUMP ASSERTION FAILED] {err}\n")
        raise SystemExit(1) from err
