#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Digital China (神州数码) CS6200 CLI Simulator — Automated Acceptance Suite
Entry Point: test_simulator.py

End-to-end verification of CS6200CLIEngine + CS6200CommandHandlers across an
enterprise / skills-competition switch configuration workflow:
  1. Privilege & global mode transitions + hostname update (CS6200-Core)
  2. Batch VLAN creation (10,20,30) & VLAN naming
  3. Access port configuration + Port-Security precondition & parameter checks
  4. LACP Port-Group (mode active) & Port-Channel1 Trunk VLAN allow-list
  5. Layer-3 SVI (Vlan10) IP addressing + DHCP Pool (network, gateway, Option 43)
  6. Static default route + OSPF process 1 network advertisement
  7. Extended ACL + Global Firewall precondition enforcement (negative & positive)
  8. Full rendering & inspection of:
     - show running-config
     - show vlan
     - show port-group
     - show ip route
     - show ip ospf neighbor
  9. Port loopback-detection (global interval / port VLAN list & control mode),
     'show loopback-detection' and 'show this' in Config-If-Port-Range view.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List, Optional

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
    """Resolve all 7 JSON rule files relative to BASE_DIR."""
    paths: List[str] = []
    for name in RULE_FILENAMES:
        candidate = BASE_DIR / name
        if not candidate.is_file():
            candidate = BASE_DIR / "rules" / name
        assert candidate.is_file(), f"Required rule file not found: {name}"
        paths.append(str(candidate))
    return paths


def section(step_num: int, title: str) -> None:
    """Print a clean section divider for test readability."""
    print(f"\n{'=' * 78}")
    print(f"[STEP {step_num}/9] {title}")
    print(f"{'=' * 78}")


def exec_cmd(
    engine: CS6200CLIEngine,
    cmd: str,
    *,
    expect_error: bool = False,
    expected_substr: Optional[str] = None,
) -> str:
    """
    Execute a single CLI command against CS6200CLIEngine, print prompt + output,
    and assert error/success expectations.
    """
    prompt = engine.get_prompt()
    output = str(engine.execute_command(cmd) or "")
    print(f"{prompt} {cmd}")
    if output.strip():
        print(output.rstrip())

    has_error = "%" in output and (
        "Invalid" in output
        or "Ambiguous" in output
        or "not enabled" in output
        or "must" in output.lower()
        or "error" in output.lower()
        or "Incomplete" in output
    )

    if expect_error:
        assert has_error, f"Expected CLI error for '{cmd}', but got: {output!r}"
    else:
        assert not has_error, f"Unexpected CLI error for '{cmd}': {output!r}"

    if expected_substr is not None:
        assert expected_substr in output, (
            f"Expected substring {expected_substr!r} in output of '{cmd}', "
            f"got: {output!r}"
        )

    return output


def run_acceptance_suite() -> None:
    """Run all 9 end-to-end configuration & verification stages."""
    rule_paths = load_rule_files()
    engine = CS6200CLIEngine(rule_paths)
    _handlers = CS6200CommandHandlers(engine)

    # -------------------------------------------------------------------------
    # STEP 1: Mode Transitions & Hostname Configuration
    # -------------------------------------------------------------------------
    section(1, "System Mode Transitions & Hostname (CS6200-Core)")
    assert engine.get_prompt() == "Switch>", f"Unexpected initial prompt: {engine.get_prompt()}"

    exec_cmd(engine, "enable")
    assert engine.get_prompt() == "Switch#", f"Expected Switch#, got {engine.get_prompt()}"

    exec_cmd(engine, "config terminal")
    assert engine.get_prompt() == "Switch(config)#", f"Expected Switch(config)#, got {engine.get_prompt()}"

    exec_cmd(engine, "hostname CS6200-Core")
    assert engine.state["system"]["hostname"] == "CS6200-Core"
    assert engine.get_prompt() == "CS6200-Core(config)#"
    print("  [PASS] Hostname updated & prompt transitioned to CS6200-Core(config)#")

    # -------------------------------------------------------------------------
    # STEP 2: Batch VLAN Creation & VLAN Naming
    # -------------------------------------------------------------------------
    section(2, "Batch VLAN Creation (vlan 10,20,30) & Naming")
    exec_cmd(engine, "vlan 10,20,30")
    vlans = engine.state["vlans"]
    for vid in ("10", "20", "30"):
        assert vid in vlans or int(vid) in vlans, f"VLAN {vid} missing from state['vlans']"

    exec_cmd(engine, "vlan 10")
    assert engine.get_prompt() == "CS6200-Core(Config-Vlan10)#"
    exec_cmd(engine, "name Office")
    exec_cmd(engine, "exit")

    exec_cmd(engine, "vlan 20")
    exec_cmd(engine, "name Server")
    exec_cmd(engine, "exit")
    assert engine.get_prompt() == "CS6200-Core(config)#"
    print("  [PASS] VLANs 10 (Office), 20 (Server), and 30 verified in state tree")

    # -------------------------------------------------------------------------
    # STEP 3: Access Port & Port-Security Precondition + Parameters
    # -------------------------------------------------------------------------
    section(3, "Ethernet1/0/1 Access Mode & Port-Security Validation")
    exec_cmd(engine, "interface ethernet 1/0/1")
    assert engine.get_prompt() == "CS6200-Core(Config-If-Ethernet1/0/1)#"

    # Negative check: configuring max MACs before enabling port-security must fail
    exec_cmd(engine, "switchport port-security maximum 2", expect_error=True)

    exec_cmd(engine, "switchport mode access")
    exec_cmd(engine, "switchport access vlan 10")
    exec_cmd(engine, "switchport port-security")
    exec_cmd(engine, "switchport port-security maximum 2")
    exec_cmd(engine, "switchport port-security violation restrict")

    # 端口环回测试命令：必须出现在 Ethernet 端口视图的 'l' 补全列表中
    assert "loopback" in engine.get_completions("l"), engine.get_completions("l")
    exec_cmd(engine, "loopback")
    assert engine.state["interfaces"]["Ethernet1/0/1"]["port_loopback"] is True
    exec_cmd(engine, "no loopback")
    assert engine.state["interfaces"]["Ethernet1/0/1"]["port_loopback"] is False

    exec_cmd(engine, "exit")

    eth1 = engine.state["interfaces"]["Ethernet1/0/1"]
    assert eth1["switchport_mode"] == "access"
    assert int(eth1["access_vlan"]) == 10
    assert eth1["port_security"]["enabled"] is True
    assert int(eth1["port_security"]["maximum"]) == 2
    assert eth1["port_security"]["violation"] == "restrict"
    print("  [PASS] Ethernet1/0/1 Access VLAN 10 & Port-Security (max=2, restrict) verified")

    # -------------------------------------------------------------------------
    # STEP 4: LACP Port-Group Aggregation & Port-Channel1 Trunk
    # -------------------------------------------------------------------------
    section(4, "LACP Link Aggregation (Port-Group 1) & Port-Channel1 Trunk")
    exec_cmd(engine, "interface ethernet 1/0/2")
    exec_cmd(engine, "port-group 1 mode active")
    exec_cmd(engine, "exit")

    exec_cmd(engine, "interface ethernet 1/0/3")
    exec_cmd(engine, "port-group 1 mode active")
    exec_cmd(engine, "exit")

    assert "1" in engine.state["port_groups"] or 1 in engine.state["port_groups"]
    assert "Port-Channel1" in engine.state["interfaces"]

    exec_cmd(engine, "interface port-channel 1")
    assert engine.get_prompt() == "CS6200-Core(Config-If-Port-Channel1)#"
    exec_cmd(engine, "switchport mode trunk")
    exec_cmd(engine, "switchport trunk allowed vlan 10;20")
    exec_cmd(engine, "exit")

    po1 = engine.state["interfaces"]["Port-Channel1"]
    assert po1["switchport_mode"] == "trunk"
    assert 10 in po1["trunk_allowed_vlan_list"] and 20 in po1["trunk_allowed_vlan_list"]
    print("  [PASS] Port-Group 1 active members & Port-Channel1 Trunk (VLAN 10,20) verified")

    # -------------------------------------------------------------------------
    # STEP 5: Layer-3 SVI (Vlan10) & DHCP Pool Configuration
    # -------------------------------------------------------------------------
    section(5, "Layer-3 SVI Interface Vlan10 & DHCP Pool (VLAN10_POOL)")
    exec_cmd(engine, "interface vlan 10")
    assert engine.get_prompt() == "CS6200-Core(Config-If-Vlan10)#"
    exec_cmd(engine, "ip address 192.168.10.1 255.255.255.0")
    exec_cmd(engine, "no shutdown")
    exec_cmd(engine, "exit")

    svi10 = engine.state["interfaces"]["Vlan10"]
    assert svi10["ip_address"] == "192.168.10.1/255.255.255.0"

    exec_cmd(engine, "ip dhcp pool VLAN10_POOL")
    assert engine.get_prompt() == "CS6200-Core(dhcp-config)#"
    exec_cmd(engine, "network-address 192.168.10.0 255.255.255.0")
    exec_cmd(engine, "default-router 192.168.10.1")
    exec_cmd(engine, 'option 43 ascii "192.168.100.10"')
    exec_cmd(engine, "exit")

    pool = engine.state["dhcp_pools"]["VLAN10_POOL"]
    assert pool["network"] == "192.168.10.0 255.255.255.0"
    assert "192.168.10.1" in pool["default_router"]
    print("  [PASS] SVI Vlan10 (192.168.10.1/24) & DHCP pool VLAN10_POOL verified")

    # -------------------------------------------------------------------------
    # STEP 6: Static Default Route & OSPF Dynamic Routing
    # -------------------------------------------------------------------------
    section(6, "Static Routing & OSPF Process 1 Network Advertisement")
    exec_cmd(engine, "ip route 0.0.0.0 0.0.0.0 192.168.10.254")
    assert len(engine.state["static_routes"]) >= 1

    exec_cmd(engine, "router ospf 1")
    assert engine.get_prompt() == "CS6200-Core(Config-Router)#"
    exec_cmd(engine, "network 192.168.10.0 0.0.0.255 area 0")
    exec_cmd(engine, "exit")

    ospf_proc = engine.state["ospf"]["processes"].get("1") or engine.state["ospf"].get(1)
    assert ospf_proc is not None and len(ospf_proc["networks"]) >= 1
    print("  [PASS] Static default route & OSPF Area 0 network verified")

    # -------------------------------------------------------------------------
    # STEP 7: Extended ACL & Global Firewall Precondition Enforcement
    # -------------------------------------------------------------------------
    section(7, "Extended ACL 101 & Global Firewall Precondition Check")
    exec_cmd(
        engine,
        "access-list 101 permit ip 192.168.10.0 0.0.0.255 any-destination",
    )
    ext_acls = engine.state["acls"]["extended_numbered"]
    assert "101" in ext_acls, f"ACL 101 missing from extended_numbered: {ext_acls!r}"

    # Negative check: applying ip access-group BEFORE 'firewall enable' must fail
    exec_cmd(engine, "interface ethernet 1/0/1")
    exec_cmd(
        engine,
        "ip access-group 101 in",
        expect_error=True,
        expected_substr="% Firewall is not enabled globally.",
    )
    exec_cmd(engine, "exit")

    # Enable global firewall and re-bind ACL 101 on Ethernet1/0/1
    exec_cmd(engine, "firewall enable")
    assert engine.state["firewall_enabled"] is True

    exec_cmd(engine, "interface ethernet 1/0/1")
    exec_cmd(engine, "ip access-group 101 in")
    exec_cmd(engine, "end")

    assert engine.get_prompt() == "CS6200-Core#"
    assert engine.state["interfaces"]["Ethernet1/0/1"]["ip_access_group"].get("in") == "101"
    print("  [PASS] Firewall precondition blocked early ACL bind & succeeded after enable")

    # -------------------------------------------------------------------------
    # STEP 8: Render & Visually Inspect All Required 'show' Outputs
    # -------------------------------------------------------------------------
    section(8, "Full 'show' Command Outputs for Visual & Automated Inspection")

    show_commands = [
        ("show running-config", "hostname CS6200-Core"),
        ("show vlan", "Office"),
        ("show port-group", "Port-Channel1"),
        ("show ip route", "192.168.10.0"),
        ("show ip ospf neighbor", "Full"),
    ]

    for cmd, expected_token in show_commands:
        print(f"\n--- {cmd} ---")
        out = exec_cmd(engine, cmd, expected_substr=expected_token)
        if cmd == "show running-config":
            # DCN uses ';' as the VLAN-list separator in running-config
            assert " switchport trunk allowed vlan 10;20" in out, out

    # -------------------------------------------------------------------------
    # STEP 9: Port Loopback-Detection & 'show this' in Port-Range View
    # -------------------------------------------------------------------------
    section(9, "Port Loopback-Detection & 'show this' (Config-If-Port-Range)")
    exec_cmd(engine, "config terminal")
    exec_cmd(engine, "loopback-detection interval-time 35 15")
    exec_cmd(engine, "loopback-detection control-recovery timeout 60")

    exec_cmd(engine, "interface ethernet 1/0/3,1/0/4")
    assert engine.get_prompt() == "CS6200-Core(Config-If-Port-Range)#", engine.get_prompt()
    exec_cmd(engine, "loopback-detection specified-vlan 10;20")
    exec_cmd(engine, "loopback-detection control block")

    # 'show this' must dump every target port of the Port-Range view
    this_out = exec_cmd(engine, "show this")
    assert "interface Ethernet1/0/3" in this_out, this_out
    assert "interface Ethernet1/0/4" in this_out, this_out
    assert "loopback-detection specified-vlan 10;20" in this_out, this_out
    assert "loopback-detection control block" in this_out, this_out
    exec_cmd(engine, "exit")
    exec_cmd(engine, "end")
    assert engine.get_prompt() == "CS6200-Core#", engine.get_prompt()

    ld_eth3 = engine.state["interfaces"]["Ethernet1/0/3"]["loopback_detection"]
    assert ld_eth3["enabled"] is True
    assert ld_eth3["vlans"] == [10, 20], ld_eth3
    assert ld_eth3["control"] == "block", ld_eth3

    ld_eth1 = engine.state["interfaces"]["Ethernet1/0/1"]["loopback_detection"]
    assert ld_eth1["enabled"] is False, ld_eth1

    # Both manual-compatible forms must work
    show_ld = exec_cmd(
        engine,
        "show loopback-detection interface ethernet 1/0/3",
        expected_substr="Ethernet1/0/3",
    )
    assert "Enabled" in show_ld and "block" in show_ld and "10;20" in show_ld, show_ld
    show_ld2 = exec_cmd(engine, "show loopback-detection interface 1/0/3", expected_substr="Ethernet1/0/3")
    assert "Enabled" in show_ld2, show_ld2

    running = exec_cmd(engine, "show running-config", expected_substr="hostname CS6200-Core")
    assert "loopback-detection interval-time 35 15" in running, running
    assert "loopback-detection control-recovery timeout 60" in running, running
    assert " loopback-detection specified-vlan 10;20" in running, running
    assert " loopback-detection control block" in running, running

    # Validate JSON state snapshot export works cleanly
    snapshot = engine.export_state()
    parsed = json.loads(snapshot) if isinstance(snapshot, str) else snapshot
    assert parsed["system"]["hostname"] == "CS6200-Core"

    print(f"\n{'#' * 78}")
    print("  ALL ASSERTIONS PASSED! CS6200 CLI Simulator is 100% Verified.")
    print(f"{'#' * 78}\n")


if __name__ == "__main__":
    try:
        run_acceptance_suite()
    except AssertionError as err:
        sys.stderr.write(f"\n[ASSERTION FAILED] {err}\n")
        raise SystemExit(1) from err
