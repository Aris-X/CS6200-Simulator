#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Digital China (神州数码) CS6200 CLI Simulator — Loopback Interface Acceptance
Entry Point: test_loopback.py

Verifies native Loopback interface support (no monkey-patching required):
  1. 'interface loopback <id>' creates 'Loopback<id>' and enters
     'Switch(Config-If-Loopback<id>)#' (also via abbreviation 'int lo 2').
  2. Full Layer-3 configuration is stored: ip address /32 (+ 'no ip address'),
     ipv6 address / ipv6 enable, description (with spaces), bandwidth, mtu,
     shutdown / no shutdown.
  3. Layer-2 port commands (switchport ...) are rejected on Loopback.
  4. 'no interface loopback <id>' removes the interface from the state tree.
  5. show interface / show ip interface loopback / show interface
     loopback / show ipv6 interface loopback / show ip route /
     show running-config render Loopback correctly (no switchport lines).
  6. 'write' persists the complete Loopback configuration into startup_config.
"""

from __future__ import annotations

import json
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
    "01_system.json",
    "02_system.json",
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


def extract_block(config: str, header: str) -> str:
    """Return the configuration block starting at `header` up to the next '!'."""
    lines = config.splitlines()
    for idx, line in enumerate(lines):
        if line.strip() == header:
            block: List[str] = []
            for sub in lines[idx + 1:]:
                if sub.strip() == "!":
                    break
                block.append(sub)
            return "\n".join(block)
    return ""


def run_loopback_suite() -> None:
    engine = CS6200CLIEngine(load_rule_files())
    _handlers = CS6200CommandHandlers(engine)

    print("=" * 78)
    print("  Loopback Interface Support — Native Simulator Acceptance Suite")
    print("=" * 78)

    # ------------------------------------------------------------------
    # 1. Create Loopback1 and enter its configuration view
    # ------------------------------------------------------------------
    print("\n--- [1/5] Create Loopback1 & Enter Config View ---")
    exec_line(engine, "enable")
    exec_line(engine, "config terminal")
    exec_line(engine, "interface loopback 1")
    assert engine.get_prompt() == "Switch(Config-If-Loopback1)#", engine.get_prompt()
    assert engine.current_frame.meta.get("if_type") == "loopback"

    # Layer-2 commands are hidden from `?` help / Tab completion in Loopback view
    view_help = engine.get_context_help("")
    assert "switchport" not in view_help, view_help
    for required in ("ip address", "ipv6 address", "description", "bandwidth", "mtu", "no ip address"):
        assert required in view_help, (required, view_help)
    assert "switchport" not in engine.get_completions(""), engine.get_completions("")
    # 物理端口的环回测试命令 'loopback' 不应出现在 Loopback 逻辑接口视图
    assert "loopback" not in engine.get_completions(""), engine.get_completions("")

    exec_line(engine, "ip address 10.255.255.1 255.255.255.255")
    lo1 = engine.state["interfaces"]["Loopback1"]
    assert lo1["ip_address"] == "10.255.255.1/255.255.255.255", lo1["ip_address"]

    # 完整的三层接口属性：描述（含空格）/ 带宽 / MTU / IPv6
    exec_line(engine, "description Core Router-ID")
    exec_line(engine, "bandwidth 1000")
    exec_line(engine, "mtu 9000")
    assert lo1["description"] == "Core Router-ID", lo1["description"]
    assert lo1["bandwidth"] == 1000, lo1["bandwidth"]
    assert lo1["mtu"] == 9000, lo1["mtu"]

    exec_line(engine, "ipv6 address 2001:db8::1/128")
    exec_line(engine, "ipv6 enable")
    assert "2001:db8::1/128" in lo1["ipv6_addresses"], lo1["ipv6_addresses"]
    assert lo1["ipv6_enable"] is True

    # 'no ip address' 清除地址后可重新下发
    exec_line(engine, "no ip address")
    assert lo1["ip_address"] is None, lo1["ip_address"]
    exec_line(engine, "ip address 10.255.255.1 255.255.255.255")
    assert lo1["ip_address"] == "10.255.255.1/255.255.255.255", lo1["ip_address"]

    # Layer-2 commands are unavailable in the Loopback view (device-authentic error)
    print(f"{engine.get_prompt()} switchport mode access")
    out = str(engine.execute_command("switchport mode access") or "")
    print(out.rstrip())
    assert "% Invalid input detected" in out, out

    exec_line(engine, "shutdown")
    assert lo1["shutdown"] is True
    exec_line(engine, "no shutdown")
    assert lo1["shutdown"] is False
    exec_line(engine, "end")
    assert engine.get_prompt() == "Switch#", engine.get_prompt()

    # ------------------------------------------------------------------
    # 2. Compact abbreviation ('int lo 2') & invalid id validation
    # ------------------------------------------------------------------
    print("\n--- [2/5] Abbreviated 'int lo 2' & Invalid Loopback ID ---")
    exec_line(engine, "config terminal")
    exec_line(engine, "int lo 2")
    assert engine.get_prompt() == "Switch(Config-If-Loopback2)#", engine.get_prompt()
    exec_line(engine, "ip address 10.255.255.2 255.255.255.255")
    exec_line(engine, "end")

    bad = engine.execute_command("config terminal")
    assert not bad.strip()
    bad = engine.execute_command("interface loopback 9999")
    assert "% Invalid input detected" in bad, bad
    out = engine.execute_command("interface loopback abc")
    assert "% Invalid input detected" in out, out
    engine.return_to_priv()

    # ------------------------------------------------------------------
    # 3. Show commands render Loopback as a routed interface
    # ------------------------------------------------------------------
    print("\n--- [3/5] show interface / show ip interface loopback 1 ---")
    brief = exec_line(engine, "show interface")
    assert "Loopback1" in brief and "10.255.255.1" in brief, brief
    assert "Loopback2" in brief and "10.255.255.2" in brief, brief

    lo_detail = exec_line(engine, "show ip interface loopback 1")
    assert "Loopback1 is up" in lo_detail, lo_detail
    assert "Internet address is 10.255.255.1/255.255.255.255" in lo_detail, lo_detail
    assert "Description: Core Router-ID" in lo_detail, lo_detail

    lo_phys = exec_line(engine, "show interface loopback 1")
    assert "Loopback1 is up" in lo_phys, lo_phys
    assert "Description: Core Router-ID" in lo_phys, lo_phys
    assert "MTU 9000 bytes" in lo_phys, lo_phys
    assert "BW 1000 Kbit" in lo_phys, lo_phys

    lo_v6 = exec_line(engine, "show ipv6 interface loopback 1")
    assert "IPv6 is enabled" in lo_v6, lo_v6
    assert "2001:db8::1/128" in lo_v6, lo_v6

    # Loopback must not be reported as a VLAN member / STP port
    vlan_out = exec_line(engine, "show vlan")
    assert "Loopback" not in vlan_out, vlan_out
    stp_out = exec_line(engine, "show spanning-tree")
    assert "Loopback" not in stp_out, stp_out

    # ------------------------------------------------------------------
    # 4. Connected /32 route & running-config rendering
    # ------------------------------------------------------------------
    print("\n--- [4/5] show ip route & show running-config ---")
    routes = exec_line(engine, "show ip route")
    assert "10.255.255.1/32" in routes and "Loopback1" in routes, routes

    running = exec_line(engine, "show running-config")
    lo_block = extract_block(running, "interface Loopback1")
    assert "ip address 10.255.255.1 255.255.255.255" in lo_block, lo_block
    assert "description Core Router-ID" in lo_block, lo_block
    assert "bandwidth 1000" in lo_block, lo_block
    assert "mtu 9000" in lo_block, lo_block
    assert "ipv6 address 2001:db8::1/128" in lo_block, lo_block
    assert "switchport" not in lo_block, lo_block

    # ------------------------------------------------------------------
    # 5. Persistence & deletion
    # ------------------------------------------------------------------
    print("\n--- [5/5] write Persistence & 'no interface loopback' Deletion ---")
    exec_line(engine, "write")
    assert "Loopback1" in engine.state["startup_config"]["interfaces"]
    assert "Loopback2" in engine.state["startup_config"]["interfaces"]
    saved_lo1 = engine.state["startup_config"]["interfaces"]["Loopback1"]
    assert saved_lo1["description"] == "Core Router-ID", saved_lo1
    assert saved_lo1["bandwidth"] == 1000, saved_lo1
    assert saved_lo1["mtu"] == 9000, saved_lo1
    assert "2001:db8::1/128" in saved_lo1["ipv6_addresses"], saved_lo1

    exec_line(engine, "config terminal")
    exec_line(engine, "no interface loopback 2")
    assert "Loopback2" not in engine.state["interfaces"], engine.state["interfaces"].keys()
    exec_line(engine, "end")

    snapshot = json.loads(engine.export_state())
    assert "Loopback1" in snapshot["interfaces"]

    print("\n" + "=" * 78)
    print("  ALL LOOPBACK ASSERTIONS PASSED!")
    print("=" * 78)


if __name__ == "__main__":
    try:
        run_loopback_suite()
    except AssertionError as err:
        sys.stderr.write(f"\n[LOOPBACK ASSERTION FAILED] {err}\n")
        raise SystemExit(1) from err
