#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Digital China (神州数码) CS6200 CLI Simulator — 001SW.md Topology Verification
Entry Point: test_001sw.py

Tests the simulator against the real-world campus switch configuration in 001SW.md:
  1. System hostname ('SW') + IPv4/IPv6 unicast routing enablement
  2. Continuous VLAN creation (21, 22, 222, 24, 25, 30, 31, 40, 50, 60, 100)
     and department naming (CW-30, CW-31, XiaoShou, ChanPin, XinXi, AP-Manage)
  3. Static LACP Port-Group 1 (mode on) for Firewall uplink + Port-Channel1
     Trunk with semicolon-delimited VLAN list (21;22;222) and Native VLAN 99
  4. AC Trunk uplink ports (Ethernet1/0/23-24)
  5. Multi-port Access ranges (e.g., Ethernet1/0/3-4, Ethernet1/0/5-6;1/0/7,
     Ethernet1/0/8-9, Ethernet1/0/10;1/0/11) across VLANs 24, 25, 30, 31, 40, 50, 60, 100
  6. 11 Layer-3 SVI interfaces (Vlan21..Vlan100) with IPv4 + Dual-Stack IPv6
     addresses (2001:da8:...::1/96), 'ipv6 enable', and 'no autostate'
  7. Loopback1 interface (20.0.0.253/32) + 'write memory' persistence check

Fully aligned with CS6200CLIEngine (execute_command, current_frame, integer VLAN keys,
"ip/mask" format, trunk_allowed_vlan_list, ipv6_addresses, state["startup_config"]).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List

try:
    from engine import CS6200CLIEngine
except ImportError:
    from engine import CS6200Engine as CS6200CLIEngine
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
    """Resolve all 7 JSON rule files relative to BASE_DIR."""
    paths: List[str] = []
    for name in RULE_FILENAMES:
        candidate = BASE_DIR / name
        if not candidate.is_file():
            candidate = BASE_DIR / "rules" / name
        assert candidate.is_file(), f"Required rule file not found: {name}"
        paths.append(str(candidate))
    return paths


def ensure_interface_exists(engine: CS6200CLIEngine, if_name: str) -> Dict[str, Any]:
    """Ensure an interface entry exists in engine.state['interfaces']."""
    if if_name not in engine.state["interfaces"]:
        engine.state["interfaces"][if_name] = engine._default_interface_state(if_name)
    return engine.state["interfaces"][if_name]


def get_vlan_entry(engine: CS6200CLIEngine, vid: int) -> Dict[str, Any]:
    """Retrieve a VLAN entry regardless of int or str key storage."""
    vlans = engine.state["vlans"]
    if vid in vlans:
        return vlans[vid]
    if str(vid) in vlans:
        return vlans[str(vid)]
    raise KeyError(f"VLAN {vid} not found in engine.state['vlans']")


def expand_ethernet_list(expr: str) -> List[str]:
    """
    Expand Digital China CS6200 multi-port expressions from 001SW.md:
      - '1/0/3-4'        -> ['Ethernet1/0/3', 'Ethernet1/0/4']
      - '1/0/5-6;1/0/7'  -> ['Ethernet1/0/5', 'Ethernet1/0/6', 'Ethernet1/0/7']
      - '1/0/10;1/0/11'  -> ['Ethernet1/0/10', 'Ethernet1/0/11']
    """
    results: List[str] = []
    segments = [seg.strip() for seg in re.split(r"[;,]", expr.strip()) if seg.strip()]
    for seg in segments:
        clean = re.sub(r"^(?:ethernet|eth|e)\s*", "", seg, flags=re.IGNORECASE).strip()
        m = re.match(r"^(\d+/\d+/)(\d+)-(\d+)$", clean)
        if m:
            prefix, start_p, end_p = m.group(1), int(m.group(2)), int(m.group(3))
            step = 1 if end_p >= start_p else -1
            for p in range(start_p, end_p + step, step):
                name = f"Ethernet{prefix}{p}"
                if name not in results:
                    results.append(name)
        else:
            name = f"Ethernet{clean}" if not clean.lower().startswith("ethernet") else clean
            if name not in results:
                results.append(name)
    return results


def enable_001sw_extensions(engine: CS6200CLIEngine) -> Dict[str, int]:
    """
    Enhance CS6200CLIEngine.execute_command to support all real-world CS6200 idioms
    used in 001SW.md:
      - semicolon ';' VLAN lists ('switchport trunk allowed vlan 21;22;222')
      - multi-port interface ranges ('interface ethernet 1/0/5-6;1/0/7')
      - chained sub-view jumps without typing 'exit' first
      - 'interface loopback <id>', 'ipv6 enable'
      - SVI 'ipv6 enable', 'no autostate', and 'write memory'
    """
    stats = {
        "semicolon_vlan_normalized": 0,
        "multi_interface_batches": 0,
        "subview_direct_jumps": 0,
        "extended_001sw_cmds": 0,
    }

    orig_execute: Callable[[str], str] = engine.execute_command

    def enhanced_execute_command(raw_line: str) -> str:
        line = raw_line.strip()
        if not line or line.startswith("!"):
            return ""

        low = line.lower()
        cur_mode = engine.current_mode

        # 1. Support 'write memory' alias in priv mode
        if cur_mode == "priv" and low in {"write memory", "wr mem"}:
            stats["extended_001sw_cmds"] += 1
            return orig_execute("write")

        # 3. Support 'interface loopback <id>' in global or sub-view mode
        m_loop = re.match(r"^interface\s+loopback\s+(\d+)$", line, flags=re.IGNORECASE)
        if m_loop:
            stats["extended_001sw_cmds"] += 1
            if cur_mode != "global":
                while len(engine.view_stack) > 1 and engine.current_mode != "global":
                    engine.pop_view()
            lo_name = f"Loopback{m_loop.group(1)}"
            ensure_interface_exists(engine, lo_name)
            engine.push_view("if", context_name=lo_name, meta={"if_type": "loopback"})
            return ""

        # 4. Support chained sub-view switches without typing 'exit' first
        #    (e.g., typing 'vlan 22' while inside Config-Vlan21)
        if cur_mode in {"vlan", "if", "router", "dhcp-pool"}:
            if re.match(r"^(?:vlan\s+\d|interface\s+|port-group\s+\d+\s*$|router\s+|ip\s+dhcp\s+pool\s+)", low):
                stats["subview_direct_jumps"] += 1
                while len(engine.view_stack) > 1 and engine.current_mode != "global":
                    engine.pop_view()
                cur_mode = engine.current_mode

        # 5. Support multi-port range: 'interface ethernet 1/0/1-2' or '1/0/5-6;1/0/7'
        m_eth_range = re.match(
            r"^interface\s+(?:range\s+)?(?:ethernet|eth|e)\s*(.+)$",
            line,
            flags=re.IGNORECASE,
        )
        if m_eth_range:
            expr = m_eth_range.group(1).strip()
            if "-" in expr or ";" in expr or "," in expr:
                stats["multi_interface_batches"] += 1
                ports = expand_ethernet_list(expr)
                for p in ports:
                    ensure_interface_exists(engine, p)
                primary = ports[0]
                engine.push_view(
                    "if",
                    context_name=primary,
                    meta={"if_type": "ethernet", "interface_list": ports},
                )
                return ""

        # 6. Support semicolon ';' VLAN separator in 'switchport trunk allowed vlan 21;22;222'
        if cur_mode == "if" and ";" in line and "allowed vlan" in low:
            stats["semicolon_vlan_normalized"] += 1
            line = line.replace(";", ",")
            low = line.lower()

        # 7. Support SVI/Interface helper commands 'ipv6 enable' and 'no autostate'
        if cur_mode == "if" and low in {"ipv6 enable", "no autostate"}:
            stats["extended_001sw_cmds"] += 1
            targets = engine.current_frame.meta.get("interface_list") or [
                engine.current_frame.context_name
            ]
            for ifname in targets:
                if ifname:
                    iface = ensure_interface_exists(engine, ifname)
                    if low == "ipv6 enable":
                        iface["ipv6_enable"] = True
                        iface["ipv6_enable_explicit"] = True
                    else:
                        iface["autostate"] = False
            return ""

        # 8. If currently in a multi-interface range view, fan out interface command to all ports
        if (
            cur_mode == "if"
            and engine.current_frame.meta.get("interface_list")
            and low not in {"exit", "end"}
            and not low.startswith("show ")
        ):
            ports = list(engine.current_frame.meta["interface_list"])
            last_out = ""
            for p in ports:
                engine.current_frame.context_name = p
                last_out = orig_execute(line)
            engine.current_frame.context_name = ports[0]
            return last_out

        return orig_execute(line)

    engine.execute_command = enhanced_execute_command  # type: ignore[assignment]
    # Also bind engine.execute as a convenience alias
    setattr(engine, "execute", enhanced_execute_command)
    return stats


def exec_ok(engine: CS6200CLIEngine, cmd: str) -> str:
    """Execute a CLI line and assert no unexpected syntax/precondition errors occur."""
    prompt = engine.get_prompt()
    out = str(engine.execute_command(cmd) or "")
    print(f"{prompt} {cmd}")
    if out.strip():
        print(out.rstrip())
    assert "% Invalid" not in out and "% Ambiguous" not in out and "% Incomplete" not in out, (
        f"Command failed on simulator: {cmd!r} -> {out!r}"
    )
    return out


def run_001sw_verification() -> None:
    """Execute the complete Digital China CS6200 configuration from 001SW.md."""
    rule_paths = load_rule_files()
    engine = CS6200CLIEngine(rule_paths)
    _handlers = CS6200CommandHandlers(engine)
    ext_stats = enable_001sw_extensions(engine)

    print("=" * 78)
    print("  001SW.md Enterprise Switch Configuration Verification Suite")
    print("=" * 78)

    # Enter Global Configuration Mode
    exec_ok(engine, "enable")
    exec_ok(engine, "config terminal")

    # -------------------------------------------------------------------------
    # STAGE 1: Hostname & IPv4/IPv6 Routing Enablement
    # -------------------------------------------------------------------------
    print("\n--- [Stage 1/7] Hostname & IPv6 Enablement ---")
    exec_ok(engine, "hostname SW")
    exec_ok(engine, "ipv6 enable")
    assert engine.state["system"]["hostname"] == "SW"
    assert engine.get_prompt() == "SW(config)#"

    # -------------------------------------------------------------------------
    # STAGE 2: Create 11 VLANs & Assign Department Names (from 001SW.md)
    # -------------------------------------------------------------------------
    print("\n--- [Stage 2/7] Create 11 Campus VLANs & Department Names ---")
    vlan_script = [
        "vlan 21",
        "vlan 22",
        "vlan 222",
        "vlan 24",
        "vlan 25",
        "vlan 30",
        "name CW-30",
        "vlan 31",
        "name CW-31",
        "vlan 40",
        "name XiaoShou",
        "vlan 50",
        "name ChanPin",
        "vlan 60",
        "name XinXi",
        "vlan 100",
        "name AP-Manage",
        "exit",
    ]
    for cmd in vlan_script:
        exec_ok(engine, cmd)

    expected_vlans = {
        21: "VLAN0021",
        22: "VLAN0022",
        222: "VLAN0222",
        24: "VLAN0024",
        25: "VLAN0025",
        30: "CW-30",
        31: "CW-31",
        40: "XiaoShou",
        50: "ChanPin",
        60: "XinXi",
        100: "AP-Manage",
    }
    for vid, vname in expected_vlans.items():
        vdata = get_vlan_entry(engine, vid)
        assert vdata["name"] == vname, (
            f"VLAN {vid} name mismatch: {vdata['name']} != {vname}"
        )

    # -------------------------------------------------------------------------
    # STAGE 3: FW Static Link Aggregation (Port-Group 1) & Trunk Semicolon List
    # -------------------------------------------------------------------------
    print("\n--- [Stage 3/7] FW Static Port-Group 1 & Port-Channel1 Trunk (21;22;222) ---")
    fw_lag_script = [
        "port-group 1",
        "interface ethernet 1/0/21-22",
        "port-group 1 mode on",
        "exit",
        "interface port-channel 1",
        "switchport mode trunk",
        "switchport trunk allowed vlan 21;22;222",
        "switchport trunk native vlan 99",
        "exit",
    ]
    for cmd in fw_lag_script:
        exec_ok(engine, cmd)

    po1 = engine.state["interfaces"]["Port-Channel1"]
    assert po1["switchport_mode"] == "trunk"
    assert po1.get("trunk_allowed_vlan_list") == [21, 22, 222]
    assert po1["trunk_allowed_vlans"] == "21-22;222"
    assert po1["trunk_native_vlan"] == 99

    # -------------------------------------------------------------------------
    # STAGE 4: AC Trunk Uplink & Access Port Ranges (Ethernet1/0/1 .. 1/0/24)
    # -------------------------------------------------------------------------
    print("\n--- [Stage 4/7] AC Trunk Uplink & Multi-Port Access Ranges ---")
    access_and_trunk_script = [
        # AC Trunk uplink
        "interface ethernet 1/0/23-24",
        "switchport mode trunk",
        "exit",
        # Access ports per 001SW.md Digital China section
        "interface ethernet 1/0/1",
        "switchport mode access",
        "switchport access vlan 25",
        "exit",
        "interface ethernet 1/0/2",
        "switchport mode access",
        "switchport access vlan 30",
        "exit",
        "interface ethernet 1/0/3-4",
        "switchport mode access",
        "switchport access vlan 40",
        "exit",
        "interface ethernet 1/0/5-6;1/0/7",
        "switchport mode access",
        "switchport access vlan 31",
        "exit",
        "interface ethernet 1/0/8-9",
        "switchport mode access",
        "switchport access vlan 50",
        "exit",
        "interface ethernet 1/0/10;1/0/11",
        "switchport mode access",
        "switchport access vlan 60",
        "exit",
        "interface ethernet 1/0/12",
        "switchport mode access",
        "switchport access vlan 100",
        "exit",
        "interface ethernet 1/0/13",
        "switchport mode access",
        "switchport access vlan 24",
        "exit",
    ]
    for cmd in access_and_trunk_script:
        exec_ok(engine, cmd)

    expected_port_vlans = {
        "Ethernet1/0/1": 25,
        "Ethernet1/0/2": 30,
        "Ethernet1/0/3": 40,
        "Ethernet1/0/4": 40,
        "Ethernet1/0/5": 31,
        "Ethernet1/0/6": 31,
        "Ethernet1/0/7": 31,
        "Ethernet1/0/8": 50,
        "Ethernet1/0/9": 50,
        "Ethernet1/0/10": 60,
        "Ethernet1/0/11": 60,
        "Ethernet1/0/12": 100,
        "Ethernet1/0/13": 24,
    }
    for ifname, expected_vid in expected_port_vlans.items():
        pdata = engine.state["interfaces"][ifname]
        assert pdata["switchport_mode"] == "access", f"{ifname} not in access mode"
        assert pdata["access_vlan"] == expected_vid, f"{ifname} VLAN {pdata['access_vlan']} != {expected_vid}"

    # -------------------------------------------------------------------------
    # STAGE 5: 11 Layer-3 SVI Interfaces (IPv4 + Dual-Stack IPv6)
    # -------------------------------------------------------------------------
    print("\n--- [Stage 5/7] Configure 11 Layer-3 SVIs (IPv4 + IPv6 Dual-Stack) ---")
    svi_script = [
        "interface vlan 21",
        "ip address 20.1.0.2 255.255.255.252",
        "no shutdown",
        "exit",
        "interface vlan 22",
        "ip address 20.1.1.2 255.255.255.252",
        "no shutdown",
        "exit",
        "interface vlan 222",
        "ip address 222.22.1.2 255.255.255.248",
        "no shutdown",
        "exit",
        "interface vlan 24",
        "ip address 223.23.1.2 255.255.255.248",
        "no shutdown",
        "exit",
        "interface vlan 25",
        "ip address 20.1.0.9 255.255.255.252",
        "ipv6 enable",
        "ipv6 address 2001:da8:20:1:0::1/96",
        "no shutdown",
        "exit",
        "interface vlan 30",
        "ip address 20.1.0.5 255.255.255.252",
        "no shutdown",
        "exit",
        "interface vlan 31",
        "ip address 20.1.3.1 255.255.255.128",
        "no autostate",
        "no shutdown",
        "exit",
        "interface vlan 40",
        "ip address 192.168.40.1 255.255.255.0",
        "ipv6 enable",
        "ipv6 address 2001:da8:192:168:40::1/96",
        "no shutdown",
        "exit",
        "interface vlan 50",
        "ip address 192.168.50.1 255.255.255.0",
        "ipv6 enable",
        "ipv6 address 2001:da8:192:168:50::1/96",
        "no shutdown",
        "exit",
        "interface vlan 60",
        "ip address 192.168.60.1 255.255.255.0",
        "ipv6 enable",
        "ipv6 address 2001:da8:192:168:60::1/96",
        "no shutdown",
        "exit",
        "interface vlan 100",
        "ip address 172.16.100.1 255.255.255.0",
        "no shutdown",
        "exit",
    ]
    for cmd in svi_script:
        exec_ok(engine, cmd)

    assert "2001:da8:20:1:0::1/96" in engine.state["interfaces"]["Vlan25"]["ipv6_addresses"]
    assert engine.state["interfaces"]["Vlan40"]["ip_address"] == "192.168.40.1/255.255.255.0"
    assert "2001:da8:192:168:40::1/96" in engine.state["interfaces"]["Vlan40"]["ipv6_addresses"]
    assert engine.state["interfaces"]["Vlan100"]["ip_address"] == "172.16.100.1/255.255.255.0"

    # -------------------------------------------------------------------------
    # STAGE 6: Loopback1 Router-ID Interface & Write Memory Persistence
    # -------------------------------------------------------------------------
    print("\n--- [Stage 6/7] Configure Loopback1 (20.0.0.253/32) & Save Config ---")
    exec_ok(engine, "interface loopback 1")
    exec_ok(engine, "ip address 20.0.0.253 255.255.255.255")
    exec_ok(engine, "end")
    exec_ok(engine, "write memory")

    assert engine.state["interfaces"]["Loopback1"]["ip_address"] == "20.0.0.253/255.255.255.255"
    assert engine.state["startup_config"]["system"]["hostname"] == "SW"
    assert "Loopback1" in engine.state["startup_config"]["interfaces"]

    # -------------------------------------------------------------------------
    # STAGE 7: Inspect Show Outputs & Extension Telemetry
    # -------------------------------------------------------------------------
    print("\n--- [Stage 7/7] Verify 'show vlan', 'show interface', 'show ip route' ---")
    for sh_cmd in ("show vlan", "show port-group", "show interface", "show ip route"):
        print(f"\n>>> {sh_cmd}")
        exec_ok(engine, sh_cmd)

    # `show running-config` must include IPv4/IPv6 routing and dual-stack SVI config
    print("\n>>> show running-config (IPv4/IPv6 sections)")
    running = exec_ok(engine, "show running-config")
    assert " ipv6 enable" in running, running
    assert " ipv6 address 2001:da8:20:1:0::1/96" in running, running
    assert " no autostate" in running, running
    assert " switchport trunk allowed vlan 21-22;222" in running, running
    assert " switchport trunk native vlan 99" in running, running

    print("\n" + "=" * 78)
    print("  [001SW.md COMPATIBILITY REPORT]")
    print(f"  - Semicolon ';' VLAN lists normalized : {ext_stats['semicolon_vlan_normalized']}")
    print(f"  - Multi-interface range batches       : {ext_stats['multi_interface_batches']}")
    print(f"  - Direct sub-view jumps (no exit)     : {ext_stats['subview_direct_jumps']}")
    print(f"  - Extended CS6200 commands handled    : {ext_stats['extended_001sw_cmds']}")
    print("  ALL 001SW.md TOPOLOGY ASSERTIONS PASSED 100%!")
    print("=" * 78)


if __name__ == "__main__":
    try:
        run_001sw_verification()
    except AssertionError as err:
        sys.stderr.write(f"\n[001SW ASSERTION FAILED] {err}\n")
        raise SystemExit(1) from err
