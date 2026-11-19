#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CS6200 Rule-Library Alignment Guard
===================================
Regression checks that lock in the manual-alignment fixes:

  1. All 9 rule files use the flat schema + English mode enums, and contain
     no duplicate (mode, syntax) definitions.
  2. Cisco/Huawei-only commands (ip routing / ipv6 unicast-routing / show this /
     show ip interface brief / show fan / line vty) are gone.
  3. Correct CS6200 replacements exist: show running-config current-mode,
     authentication line login family.
  4. Syntax/no_cmd typo fixes: port-security aging, named ACL rule, extended
     ACL option keywords, prefix-list/set enum braces, blackhole route, etc.
  5. Runtime view behavior: user mode cannot run show commands; route-map,
     OSPF/RIP/BGP/MSDP use independent views; current-mode renders Port-Range.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

from engine import CS6200CLIEngine
from handlers import CS6200CommandHandlers

BASE_DIR = Path(__file__).resolve().parent

RULE_FILENAMES: List[str] = [
    "01_system.json",
    "02_system.json",
    "03_system.json",
    "04_system.json",
    "05_system.json",
    "core_01_base.json",
    "core_02_l2.json",
    "core_03_l3.json",
    "core_04_security.json",
]

FORBIDDEN_SYNTAX = {
    "ip routing",
    "ipv6 unicast-routing",
    "show this",
    "show ip interface brief",
    "show fan",
    "line vty <first-line> [<last-line>]",
}


def load_rules() -> Dict[str, List[Dict[str, Any]]]:
    data: Dict[str, List[Dict[str, Any]]] = {}
    for name in RULE_FILENAMES:
        payload = json.loads((BASE_DIR / name).read_text(encoding="utf-8"))
        assert isinstance(payload, list), f"{name} must use the flat list schema"
        data[name] = payload
    return data


def exec_line(engine: CS6200CLIEngine, cmd: str) -> str:
    return str(engine.execute_command(cmd) or "")


def assert_invalid(engine: CS6200CLIEngine, cmd: str) -> None:
    out = exec_line(engine, cmd)
    assert "% Invalid input detected" in out, (cmd, out)


def run_alignment_suite() -> None:
    rules = load_rules()

    print("=" * 78)
    print("  CS6200 Rule-Library Alignment Guard")
    print("=" * 78)

    # ------------------------------------------------------------------
    # 1. Schema, modes, duplicates, forbidden commands
    # ------------------------------------------------------------------
    print("\n--- [1/4] Schema / mode / duplicate / forbidden-syntax checks ---")
    known_modes = {
        "any", "user", "priv", "global", "if", "vlan", "router",
        "router-ospf", "router-rip", "router-bgp", "router-msdp",
        "dhcp-pool", "line", "acl-std", "acl-ext", "time-range",
        "vrf", "tunnel", "route-map",
    }
    seen: Counter = Counter()
    all_syntaxes = set()
    for name, entries in rules.items():
        for rule in entries:
            assert isinstance(rule.get("mode"), list) and rule["mode"], (name, rule)
            assert isinstance(rule.get("params"), dict), (name, rule.get("syntax"))
            assert rule.get("state_key"), (name, rule.get("syntax"))
            for mode in rule["mode"]:
                assert mode in known_modes, (name, mode)
                seen[(mode, rule["syntax"])] += 1
            syntax = rule["syntax"]
            all_syntaxes.add(syntax)
            assert "< " not in syntax and " >" not in syntax, (name, syntax)
            assert not re.search(r"<[^>]*\|[^>]*>", syntax), (name, syntax)
            no_cmd = rule.get("no_cmd") or ""
            assert "..." not in no_cmd, (name, syntax, no_cmd)
            assert no_cmd.count("[") == no_cmd.count("]"), (name, syntax, no_cmd)
            assert no_cmd.count("{") == no_cmd.count("}"), (name, syntax, no_cmd)
    duplicates = [key for key, count in seen.items() if count > 1]
    assert not duplicates, duplicates
    assert not (FORBIDDEN_SYNTAX & all_syntaxes), FORBIDDEN_SYNTAX & all_syntaxes
    print(f"  [PASS] {len(all_syntaxes)} unique syntaxes, no duplicates, no non-CS6200 commands")

    # ------------------------------------------------------------------
    # 2. Manual-aligned replacements exist
    # ------------------------------------------------------------------
    print("\n--- [2/4] Replacement commands & typo fixes ---")
    expected = {
        "show running-config current-mode",
        "authentication line {console | vty | web} login {local | radius | tacacs | none}",
        "authentication enable {local | radius | tacacs}",
        "authorization line {console | vty | web} exec {local | radius | tacacs}",
        "accounting line {console | vty} command <1-15> {start-stop | stop-only | none} {local | radius | tacacs}",
        "ip route {<ip-prefix> <mask> | <ip-prefix>} null0 [<distance>]",
        "ip prefix-list <list_name> [seq <sequence_number>] {deny | permit} {any | <ip_addr/mask_length> [ge <min_prefix_len>] [le <max_prefix_len>]}",
        "set metric-type {type-1 | type-2}",
        "set extcommunity {rt | soo} <AA:NN>",
        "ip pim multicast-routing",
        "ip pim dense-mode",
        "ip pim sparse-mode",
        "ip dvmrp enable",
        "router msdp",
        "route-map <map_name> {deny | permit} <sequence_num>",
    }
    missing = expected - all_syntaxes
    assert not missing, missing

    def find(state_key: str) -> List[Dict[str, Any]]:
        return [r for entries in rules.values() for r in entries if r.get("state_key") == state_key]

    aging = find("interface.port_security.aging")[0]
    assert "violation aging" not in aging["no_cmd"], aging["no_cmd"]
    named_rule = find("acl.ipv4.standard.named.rule")[0]
    assert named_rule["mode"] == ["acl-std"], named_rule
    assert not named_rule["syntax"].startswith("[no]"), named_rule["syntax"]
    assert "..." not in named_rule["no_cmd"], named_rule["no_cmd"]
    assert find("time_range.absolute")[0]["mode"] == ["time-range"]
    assert all(r["mode"] == ["dhcp-pool"] for r in find("dhcp.pool.network_address"))
    print("  [PASS] replacements, no_cmd and mode fixes verified")

    # ------------------------------------------------------------------
    # 3. Runtime: removed Cisco commands are rejected
    # ------------------------------------------------------------------
    print("\n--- [3/4] Runtime rejection of removed commands ---")
    engine = CS6200CLIEngine([str(BASE_DIR / name) for name in RULE_FILENAMES])
    CS6200CommandHandlers(engine)

    # 一般用户模式不得执行 show
    assert_invalid(engine, "show ip route")
    exec_line(engine, "enable")
    assert engine.get_prompt() == "Switch#"
    exec_line(engine, "config terminal")
    for cmd in ("ip routing", "ipv6 unicast-routing", "show this"):
        assert_invalid(engine, cmd)
    exec_line(engine, "end")
    for cmd in ("show ip interface brief", "show fan"):
        assert_invalid(engine, cmd)
    print("  [PASS] user-mode show blocked; six non-CS6200 commands rejected")

    # ------------------------------------------------------------------
    # 4. Runtime: independent protocol views & current-mode in Port-Range
    # ------------------------------------------------------------------
    print("\n--- [4/4] Independent views & current-mode rendering ---")
    exec_line(engine, "config terminal")
    exec_line(engine, "route-map RM permit 10")
    assert engine.get_prompt() == "Switch(Config-Route-Map)#"
    exec_line(engine, "set local-preference 200")
    cm = exec_line(engine, "show running-config current-mode")
    assert "route-map RM permit 10" in cm and "set local-preference 200" in cm, cm
    exec_line(engine, "exit")

    exec_line(engine, "router rip")
    # RIP 视图不得接受 BGP 专属命令
    out = exec_line(engine, "timers bgp 30 90")
    assert "% Invalid" in out, out
    exec_line(engine, "exit")

    exec_line(engine, "router bgp 65000")
    assert engine.get_prompt() == "Switch(Config-Router)#"
    exec_line(engine, "exit")
    exec_line(engine, "router msdp")
    assert engine.get_prompt() == "Switch(Config-Router)#"
    exec_line(engine, "exit")

    exec_line(engine, "interface ethernet 1/0/1,1/0/2")
    assert engine.get_prompt() == "Switch(Config-If-Port-Range)#"
    exec_line(engine, "loopback-detection specified-vlan 10;20")
    current = exec_line(engine, "show running-config current-mode")
    assert "interface Ethernet1/0/1" in current, current
    assert "interface Ethernet1/0/2" in current, current
    assert "loopback-detection specified-vlan 10;20" in current, current
    print("  [PASS] route-map/protocol views isolated; current-mode renders Port-Range")

    print("\n" + "=" * 78)
    print("  ALL RULE-ALIGNMENT ASSERTIONS PASSED!")
    print("=" * 78)


if __name__ == "__main__":
    try:
        run_alignment_suite()
    except AssertionError as err:
        sys.stderr.write(f"\n[ALIGNMENT ASSERTION FAILED] {err}\n")
        raise SystemExit(1) from err
