#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Digital China (神州数码) CS6200 Series Switch CLI State Machine Engine
========================================================================
核心命令行状态机引擎 (`engine.py`)

功能特性：
1. 规则加载与清洗：
   - 自动遍历加载 7 份 JSON 命令规则文件，按视图模式（mode）建立高效索引。
   - 针对 `mode == ["any"]` 但 `requires` 注明子视图依赖的规则，自动重映射至
     `acl-std` / `acl-ext` / `time-range` / `vrf` / `tunnel` / `dhcp-pool`，
     杜绝全局模式下的误匹配。
   - 内置通用视图与系统基础命令：`exit`、`end` / `Ctrl+Z`、`write`、接口 `shutdown` / `no shutdown`。
2. CS6200 提示符与视图栈：
   - 完整还原 `Switch>`、`Switch#`、`Switch(config)#`、`Switch(Config-If-Ethernet1/0/1)#`、
     `Switch(Config-If-Vlan10)#`、`Switch(Config-If-Port-Channel1)#`、`Switch(Config-If-Loopback1)#`、
     `Switch(Config-Vlan10)#`、`Switch(Config-Router)#`、`Switch(dhcp-config)#`、`Switch(Config-Line)#` 等提示符。
   - 支持物理口简写自动展开（如 `e1/0/1` -> `Ethernet1/0/1`、`lo1` -> `Loopback1`）。
3. 终端交互核心特性：
   - 唯一前缀缩写匹配（如 `conf t` -> `config terminal`，`sh run` -> `show running-config`）。
   - 歧义检测（`% Ambiguous command.`）与精确游标报错（`% Invalid input detected at '^' marker.`）。
   - `?` 上下文实时帮助与 `get_completions(line)` Tab 自动补全接口。
   - `no` 前缀命令的统一解析与路由分发。
   - 配置子视图内直接输入全局模式命令即可跳转（如
     `Switch(Config-If-Vlan10)# interface vlan 20`），与 DCN 实机一致。
4. 状态树与 Handler 钩子：
   - 结构化 `self.state` 状态字典，预留 `execute_handler(state_key, args, is_no)` 钩子
     无缝对接后续 `handlers.py`，并提供 `export_state()` 导出 JSON 快照。
5. 配置持久化：
   - `write` / `write memory` 将 startup_config 写入磁盘 JSON 文件；
     重启程序时 `load_startup_config()` 自动恢复（cli.py 已接入 startup-config.json）。
"""

from __future__ import annotations

import copy
import glob
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


# ============================================================================
# 1. 语法树 Token 模型与规则定义
# ============================================================================

@dataclass
class SyntaxToken:
    """表示命令语法模板中的单个词法单元"""
    raw: str
    kind: str  # 'keyword' | 'param' | 'choice' | 'optional' | 'group'
    choices: List[str] = field(default_factory=list)
    sub_tokens: List["SyntaxToken"] = field(default_factory=list)
    param_name: str = ""


@dataclass
class CommandRule:
    """标准化后的单条 CLI 命令规则对象"""
    modes: List[str]
    syntax: str
    no_cmd: str
    desc: str
    requires: str
    state_key: str
    params: Dict[str, str] = field(default_factory=dict)
    output_example: str = ""
    is_builtin: bool = False
    tokens: List[SyntaxToken] = field(default_factory=list)
    no_tokens: List[SyntaxToken] = field(default_factory=list)


@dataclass
class ViewFrame:
    """CLI 视图栈帧"""
    mode: str                 # 如 'user', 'priv', 'global', 'if', 'vlan', 'router', 'dhcp-pool', 'line', 等
    context_name: str = ""    # 如 'Ethernet1/0/1', 'Vlan10', '10', 'ospf 1', 'POOL1'
    meta: Dict[str, Any] = field(default_factory=dict)


# ============================================================================
# 2. CS6200 语法模板解析器 (支持 <param>, {a|b}, [opt], 嵌套花括号)
# ============================================================================

class SyntaxParser:
    """将神州数码手册中的 syntax 字符串解析为结构化匹配 Token 序列"""

    @classmethod
    def parse(cls, syntax_str: str) -> List[SyntaxToken]:
        cleaned = syntax_str.strip()
        if not cleaned:
            return []
        raw_parts = cls._split_top_level(cleaned)
        tokens: List[SyntaxToken] = []
        for part in raw_parts:
            tok = cls._parse_part(part)
            if tok is not None:
                tokens.append(tok)
        return tokens

    @classmethod
    def _split_top_level(cls, text: str) -> List[str]:
        """按顶层空白字符切分，保持 <...>, {...}, [...], (...) 内部完整"""
        parts: List[str] = []
        buf: List[str] = []
        depth_angle = 0
        depth_brace = 0
        depth_bracket = 0
        depth_paren = 0

        for ch in text:
            if ch == "<":
                depth_angle += 1
            elif ch == ">" and depth_angle > 0:
                depth_angle -= 1
            elif ch == "{":
                depth_brace += 1
            elif ch == "}" and depth_brace > 0:
                depth_brace -= 1
            elif ch == "[":
                depth_bracket += 1
            elif ch == "]" and depth_bracket > 0:
                depth_bracket -= 1
            elif ch == "(":
                depth_paren += 1
            elif ch == ")" and depth_paren > 0:
                depth_paren -= 1

            in_group = (depth_angle + depth_brace + depth_bracket + depth_paren) > 0
            if ch.isspace() and not in_group:
                if buf:
                    parts.append("".join(buf))
                    buf.clear()
            else:
                buf.append(ch)

        if buf:
            parts.append("".join(buf))
        return parts

    @classmethod
    def _split_alternatives(cls, inner: str) -> List[str]:
        """按顶层 '|' 切分互斥选项"""
        alts: List[str] = []
        buf: List[str] = []
        depth_angle = depth_brace = depth_bracket = depth_paren = 0

        for ch in inner:
            if ch == "<":
                depth_angle += 1
            elif ch == ">" and depth_angle > 0:
                depth_angle -= 1
            elif ch == "{":
                depth_brace += 1
            elif ch == "}" and depth_brace > 0:
                depth_brace -= 1
            elif ch == "[":
                depth_bracket += 1
            elif ch == "]" and depth_bracket > 0:
                depth_bracket -= 1
            elif ch == "(":
                depth_paren += 1
            elif ch == ")" and depth_paren > 0:
                depth_paren -= 1

            if ch == "|" and (depth_angle + depth_brace + depth_bracket + depth_paren) == 0:
                alts.append("".join(buf).strip())
                buf.clear()
            else:
                buf.append(ch)

        if buf:
            alts.append("".join(buf).strip())
        return [a for a in alts if a]

    @classmethod
    def _parse_part(cls, part: str) -> Optional[SyntaxToken]:
        part = part.strip()
        if not part:
            return None

        # 可选块 [ ... ]
        if part.startswith("[") and part.endswith("]"):
            inner = part[1:-1].strip()
            if "|" in inner and not any(c in inner for c in "<{[("):
                choices = [c.strip() for c in inner.split("|") if c.strip()]
                return SyntaxToken(raw=part, kind="optional", choices=choices)
            sub = cls.parse(inner)
            return SyntaxToken(raw=part, kind="optional", sub_tokens=sub)

        # 必选组 { ... } 或 ( ... )
        if (part.startswith("{") and part.endswith("}")) or (part.startswith("(") and part.endswith(")")):
            inner = part[1:-1].strip()
            alts = cls._split_alternatives(inner)
            if len(alts) > 1:
                # 简单关键字枚举 vs 复杂分支
                if all(len(cls._split_top_level(a)) == 1 and not a.startswith("<") and not a.startswith("{") for a in alts):
                    return SyntaxToken(raw=part, kind="choice", choices=alts)
                return SyntaxToken(raw=part, kind="group", choices=alts)
            sub = cls.parse(inner)
            return SyntaxToken(raw=part, kind="group", sub_tokens=sub)

        # 参数占位符 <...> 或大写变量 WORD / IFNAME / LINE / A.B.C.D
        if (part.startswith("<") and part.endswith(">")) or part in {
            "WORD", "IFNAME", "LINE", "A.B.C.D", "X:X::X:X"
        }:
            return SyntaxToken(raw=part, kind="param", param_name=part)

        # 普通命令关键字
        return SyntaxToken(raw=part, kind="keyword", choices=[part])


# ============================================================================
# 3. 核心 CLI 状态机引擎
# ============================================================================

class CS6200CLIEngine:
    """
    神州数码 CS6200 交换机 CLI 有限状态机引擎
    """

    # 视图模式 -> 提示符格式化映射表
    PROMPT_TEMPLATES: Dict[str, str] = {
        "user": "{hostname}>",
        "priv": "{hostname}#",
        "global": "{hostname}(config)#",
        "if": "{hostname}(Config-If-{ctx})#",
        "vlan": "{hostname}(Config-Vlan{ctx})#",
        "router": "{hostname}(Config-Router)#",
        "dhcp-pool": "{hostname}(dhcp-config)#",
        "line": "{hostname}(Config-Line)#",
        "acl-std": "{hostname}(Config-Std-Nacl)#",
        "acl-ext": "{hostname}(Config-Ext-Nacl)#",
        "time-range": "{hostname}(Config-Time-Range)#",
        "vrf": "{hostname}(Config-Vrf)#",
        "tunnel": "{hostname}(Config-If-Tunnel{ctx})#",
    }

    # 需要重映射的 requires 关键词规则 (顺序敏感，优先匹配更具体的规则)
    REQUIRES_VIEW_RULES: List[Tuple[str, str]] = [
        ("命名标准IP访问列表", "acl-std"),
        ("命名扩展IP访问列表", "acl-ext"),
        ("time-range", "time-range"),
        ("VRF配置模式", "vrf"),
        ("隧道配置模式", "tunnel"),
        ("DHCP地址池配置模式", "dhcp-pool"),
    ]

    # 全局通用查询命令（可在任意已登录视图下执行）
    GLOBAL_SHOW_KEYWORDS = ("show", "ping", "debug")

    # 仅适用于二层端口（Ethernet / Port-Channel）的命令状态键。
    # Loopback 等三层逻辑接口视图中，这些命令不会出现在命令表 / `?` 帮助 / Tab 补全中，
    # 直接输入时将按设备行为报 "% Invalid input detected at '^' marker."。
    L2_PORT_STATE_KEYS: Set[str] = {
        "interface.switchport_mode",
        "interface.access_vlan",
        "interface.trunk_allowed_vlan",
        "interface.trunk_native_vlan",
        "interface.hybrid_allowed_vlan",
        "interface.hybrid_native_vlan",
        "interface.port_group_member",
        "interface.port_loopback",
        "interface.loopback_detection.specified_vlan",
        "interface.loopback_detection.control",
        "interface.lacp_timeout",
        "interface.lacp_port_priority",
        "spanning_tree.enable",
        "interface.spanning_tree.portfast",
        "interface.port_security.enable",
        "interface.port_security.maximum",
        "interface.port_security.violation",
        "interface.port_security.mac_address",
        "interface.port_security.aging",
        "interface.anti_arpscan.trust",
        "interface.dhcp_snooping_trust",
        "interface.dhcp_snooping_binding_dot1x",
    }

    def __init__(
        self,
        rule_files: Optional[List[str]] = None,
        rules_dir: Optional[str] = None,
        handler_callback: Optional[Callable[[str, Dict[str, Any], bool], Optional[str]]] = None,
        persist_path: Optional[str] = None,
    ) -> None:
        self.handler_callback = handler_callback

        # 启动配置持久化文件路径（CLI 传入；为空时仅保留内存中的 startup_config）
        self.persist_path = persist_path

        # 视图栈初始化为 user 用户模式
        self.view_stack: List[ViewFrame] = [ViewFrame(mode="user")]

        # 按 mode 索引的命令规则字典: { mode_name: [CommandRule, ...] }
        self.rules_by_mode: Dict[str, List[CommandRule]] = {
            m: [] for m in self.PROMPT_TEMPLATES.keys()
        }
        self.all_rules: List[CommandRule] = []

        # 初始化结构化设备状态树
        self.state: Dict[str, Any] = self._init_default_state()

        # 注册内置基础与视图切换命令
        self._register_builtin_commands()

        # 加载外部 JSON 规则文件
        if rule_files:
            self.load_rule_files(rule_files)
        elif rules_dir:
            json_paths = sorted(glob.glob(os.path.join(rules_dir, "*.json")))
            self.load_rule_files(json_paths)

        # 初始化启动配置快照
        self.state["startup_config"] = copy.deepcopy(self._extract_persistable_state())

    # ------------------------------------------------------------------------
    # 状态树初始化与快照导出
    # ------------------------------------------------------------------------

    def _init_default_state(self) -> Dict[str, Any]:
        """初始化神州数码 CS6200 结构化配置状态树"""
        return {
            "system": {
                "hostname": "Switch",
                "enable_password": None,
                "enable_level": 1,
                "load_balance": "dst-src-mac-ip",
                "vty_lines": {},
                "time_ranges": {},
                "vrfs": {},
                "tunnels": {},
            },
            "vlans": {
                1: {"name": "default", "description": "", "shutdown": False}
            },
            "interfaces": {
                # 预置部分典型业务端口便于直接测试
                "Ethernet1/0/1": self._default_interface_state("Ethernet1/0/1"),
                "Ethernet1/0/2": self._default_interface_state("Ethernet1/0/2"),
                "Ethernet1/0/3": self._default_interface_state("Ethernet1/0/3"),
                "Ethernet1/0/4": self._default_interface_state("Ethernet1/0/4"),
            },
            "port_groups": {},
            "spanning_tree": {
                "enabled": False,
                "mode": "mstp",
                "mst_priorities": {},
            },
            "loopback_detection": {
                "interval_time": None,
                "no_loopback_time": None,
                "control_recovery": None,
            },
            "static_routes": [],
            "ipv6_routes": [],
            "ospf": {
                "processes": {},
            },
            "bgp": {
                "timers": None,
                "default_local_preference": 100,
                "neighbors": {},
            },
            "dhcp_pools": {},
            "dhcp_snooping": {
                "binding_arp": False,
                "conflict_logging": False,
            },
            "acls": {
                "standard_numbered": {},
                "extended_numbered": {},
                "standard_named": {},
                "extended_named": {},
                "source_control": {},
            },
            "prefix_lists": {},
            "route_maps": {},
            "firewall_enabled": False,
            "anti_arpscan_enabled": False,
            "multicast": {
                "igmp_snooping": False,
                "igmp_snooping_vlans": [],
                "mld_snooping": False,
                "mld_snooping_vlans": [],
                "static_rp": [],
                "ssm_range": None,
                "msdp_peers": {},
            },
            "running_config": {},
            "startup_config": {},
        }

    @staticmethod
    def _default_interface_state(if_name: str) -> Dict[str, Any]:
        return {
            "name": if_name,
            "shutdown": False,
            "description": "",
            "bandwidth": None,
            "mtu": 1500,
            "port_loopback": False,
            "loopback_detection": {
                "enabled": False,
                "vlans": [],
                "control": None,
                "status": "Normal",
            },
            "switchport_mode": "access",
            "access_vlan": 1,
            "trunk_allowed_vlans": "1-4094",
            "trunk_native_vlan": 1,
            "hybrid_allowed_vlans": [],
            "hybrid_native_vlan": 1,
            "ip_address": None,
            "ipv6_addresses": [],
            "vrf": None,
            "port_group": None,
            "port_security": {
                "enabled": False,
                "maximum": 1,
                "violation": "shutdown",
                "mac_addresses": [],
                "aging": {},
            },
            "ip_access_group": {"in": None, "out": None},
            "dhcp_snooping_trust": False,
            "anti_arpscan_trust": False,
        }

    def _extract_persistable_state(self) -> Dict[str, Any]:
        """提取需要写入 startup_config 的核心配置节点"""
        exclude_keys = {"running_config", "startup_config"}
        return {k: v for k, v in self.state.items() if k not in exclude_keys}

    def export_state(self, indent: int = 2) -> str:
        """同步 running_config 并导出完整状态树的 JSON 字符串快照"""
        self.state["running_config"] = copy.deepcopy(self._extract_persistable_state())
        return json.dumps(self.state, ensure_ascii=False, indent=indent)

    # ------------------------------------------------------------------------
    # 启动配置持久化：write 保存到文件，程序重启后自动恢复
    # ------------------------------------------------------------------------

    @classmethod
    def _merge_state_defaults(cls, base: Any, saved: Any) -> Any:
        """
        将已保存配置递归合并到默认状态树。
        旧版本保存的配置缺少新增字段时，自动补齐默认值，避免 KeyError。
        """
        if isinstance(base, dict) and isinstance(saved, dict):
            merged = copy.deepcopy(base)
            for key, value in saved.items():
                if key in merged:
                    merged[key] = cls._merge_state_defaults(merged[key], value)
                else:
                    merged[key] = copy.deepcopy(value)
            return merged
        return copy.deepcopy(saved)

    def _normalize_loaded_state(self, state: Dict[str, Any]) -> None:
        """恢复 JSON 序列化过程中变成字符串的整数键，并补齐接口/VLAN 默认字段"""
        # VLAN 使用整数键
        vlans = state.get("vlans")
        if isinstance(vlans, dict):
            normalized_vlans: Dict[int, Dict[str, Any]] = {}
            for raw_key, vinfo in vlans.items():
                if not str(raw_key).isdigit():
                    continue
                vid = int(raw_key)
                default_vlan = {"name": f"VLAN{vid:04d}", "description": "", "shutdown": False}
                normalized_vlans[vid] = self._merge_state_defaults(
                    default_vlan, vinfo if isinstance(vinfo, dict) else {}
                )
            state["vlans"] = normalized_vlans or {1: {"name": "default", "description": "", "shutdown": False}}

        # MST 实例优先级使用整数键
        stp = state.get("spanning_tree")
        if isinstance(stp, dict) and isinstance(stp.get("mst_priorities"), dict):
            stp["mst_priorities"] = {
                int(k): v for k, v in stp["mst_priorities"].items() if str(k).isdigit()
            }

        # 每个接口都补齐默认字段（兼容旧版本保存的配置）
        interfaces = state.get("interfaces")
        if isinstance(interfaces, dict):
            for if_name, iinfo in list(interfaces.items()):
                state["interfaces"][if_name] = self._merge_state_defaults(
                    self._default_interface_state(if_name),
                    iinfo if isinstance(iinfo, dict) else {},
                )

    def save_startup_config(self, path: Optional[str] = None) -> bool:
        """把 startup_config 写入磁盘（模拟 write 保存到 Flash），成功返回 True"""
        target = path or self.persist_path
        if not target:
            return False
        try:
            target_path = Path(target)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            payload = self.state.get("startup_config") or self._extract_persistable_state()
            tmp_path = target_path.with_name(target_path.name + ".tmp")
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp_path, target_path)
            return True
        except (OSError, TypeError, ValueError):
            return False

    def load_startup_config(self, path: Optional[str] = None) -> bool:
        """从磁盘恢复上次 write 保存的配置，成功返回 True"""
        target = path or self.persist_path
        if not target or not os.path.exists(target):
            return False
        try:
            with open(target, "r", encoding="utf-8") as fp:
                saved = json.load(fp)
        except (OSError, ValueError):
            return False
        if not isinstance(saved, dict):
            return False

        merged = self._merge_state_defaults(self._init_default_state(), saved)
        merged.pop("running_config", None)
        merged.pop("startup_config", None)
        self._normalize_loaded_state(merged)

        self.state = merged
        self.state["running_config"] = copy.deepcopy(self._extract_persistable_state())
        self.state["startup_config"] = copy.deepcopy(self.state["running_config"])
        return True

    # ------------------------------------------------------------------------
    # 规则加载、清洗与子视图重映射
    # ------------------------------------------------------------------------

    def load_rule_files(self, file_paths: List[str]) -> int:
        """批量加载 JSON 规则文件并清洗索引，返回加载的规则总数"""
        loaded_count = 0
        for path in file_paths:
            if not os.path.exists(path):
                continue
            with open(path, "r", encoding="utf-8") as fp:
                raw_items = json.load(fp)
            if not isinstance(raw_items, list):
                continue
            for item in raw_items:
                rule = self._normalize_and_remap_rule(item)
                if rule:
                    self._add_rule(rule)
                    loaded_count += 1
        return loaded_count

    def load_rules_from_list(self, raw_items: List[Dict[str, Any]]) -> int:
        """直接从内存字典列表加载规则（便于测试或嵌入式运行）"""
        loaded_count = 0
        for item in raw_items:
            rule = self._normalize_and_remap_rule(item)
            if rule:
                self._add_rule(rule)
                loaded_count += 1
        return loaded_count

    def _normalize_and_remap_rule(self, raw: Dict[str, Any]) -> Optional[CommandRule]:
        """清洗单条规则，处理 mode=['any'] 的子视图重映射与 [no] 前缀剥离"""
        syntax = (raw.get("syntax") or "").strip()
        if not syntax:
            return None

        raw_modes = raw.get("mode") or ["global"]
        requires = (raw.get("requires") or "").strip()
        desc = (raw.get("desc") or "").strip()
        state_key = (raw.get("state_key") or "").strip()
        no_cmd = (raw.get("no_cmd") or "").strip()
        params = raw.get("params") or {}
        output_example = raw.get("output_example") or ""

        # 剥离 syntax 开头自带的 "[no] " 标记（由引擎统一处理 no 前缀）
        has_inline_no = False
        if syntax.startswith("[no] "):
            has_inline_no = True
            syntax = syntax[5:].strip()
            if not no_cmd:
                no_cmd = f"no {syntax}"

        # 若规则本身是一条独立的 `no ...` 命令（如 03_system.json 中的 `no option 60`）
        # 我们依然将其纳入索引，同时确保正向规则也可通过 `no` 路由匹配
        target_modes = list(raw_modes)

        # 关键清洗逻辑：当 mode 为 ["any"] 时，根据 requires 文本重映射至专属子视图
        if target_modes == ["any"]:
            remapped = self._detect_subview_from_requires(requires, syntax)
            if remapped:
                target_modes = [remapped]
            elif any(syntax.startswith(kw) for kw in self.GLOBAL_SHOW_KEYWORDS):
                # show / ping / debug 等查询命令在所有模式均可用
                target_modes = list(self.PROMPT_TEMPLATES.keys())
            elif syntax.startswith("enable"):
                target_modes = ["user", "priv"]

        # 特殊修正：04_system.json 中 `no ip pim multicast-routing` 等全局路由特性
        # 特殊修正：core_04_security.json 中 `absolute start ...` 属于 time-range 子模式
        if syntax.startswith("absolute start ") and target_modes == ["global"]:
            target_modes = ["time-range"]

        # 特殊修正：04_system.json 中 `set ...` 属于 route-map 策略（若未建 route-map 视图可保留在 global 或扩展）
        rule = CommandRule(
            modes=target_modes,
            syntax=syntax,
            no_cmd=no_cmd if (no_cmd or has_inline_no) else "",
            desc=desc,
            requires=requires,
            state_key=state_key,
            params=params,
            output_example=output_example,
            is_builtin=False,
            tokens=SyntaxParser.parse(syntax),
            no_tokens=SyntaxParser.parse(no_cmd) if no_cmd else [],
        )
        return rule

    def _detect_subview_from_requires(self, requires: str, syntax: str) -> Optional[str]:
        """根据 requires 描述及语法特征识别所属子配置视图"""
        for keyword, target_mode in self.REQUIRES_VIEW_RULES:
            if keyword in requires:
                return target_mode
        return None

    def _add_rule(self, rule: CommandRule) -> None:
        """将规则注册到对应的 mode 索引桶中（去重同 syntax + state_key）"""
        self.all_rules.append(rule)
        for m in rule.modes:
            if m not in self.rules_by_mode:
                self.rules_by_mode[m] = []
            # 避免重复添加完全相同的语法
            if not any(r.syntax == rule.syntax and r.state_key == rule.state_key for r in self.rules_by_mode[m]):
                self.rules_by_mode[m].append(rule)

    def _register_builtin_commands(self) -> None:
        """注册通用内置命令：exit、end、write、接口 shutdown 等"""
        all_modes = list(self.PROMPT_TEMPLATES.keys())
        config_modes = [m for m in all_modes if m not in ("user", "priv")]

        builtins = [
            # 视图退回命令
            CommandRule(
                modes=[m for m in all_modes if m != "user"],
                syntax="exit",
                no_cmd="",
                desc="返回上一级配置视图或退出特权模式",
                requires="",
                state_key="builtin.exit",
                is_builtin=True,
                tokens=SyntaxParser.parse("exit"),
            ),
            CommandRule(
                modes=config_modes,
                syntax="end",
                no_cmd="",
                desc="结束当前配置并直接退回特权用户视图 (#)",
                requires="",
                state_key="builtin.end",
                is_builtin=True,
                tokens=SyntaxParser.parse("end"),
            ),
            # 保存配置命令
            CommandRule(
                modes=["priv"],
                syntax="write",
                no_cmd="",
                desc="将当前运行配置 (running-config) 保存至启动配置 (startup-config)",
                requires="",
                state_key="builtin.write",
                is_builtin=True,
                tokens=SyntaxParser.parse("write"),
            ),
            CommandRule(
                modes=["priv"],
                syntax="write memory",
                no_cmd="",
                desc="将当前运行配置保存至启动配置（write 的兼容写法）",
                requires="",
                state_key="builtin.write",
                is_builtin=True,
                tokens=SyntaxParser.parse("write memory"),
            ),
            # 接口视图下的 shutdown / no shutdown
            CommandRule(
                modes=["if"],
                syntax="shutdown",
                no_cmd="no shutdown",
                desc="关闭或激活当前物理/逻辑接口",
                requires="",
                state_key="interface.shutdown",
                is_builtin=True,
                tokens=SyntaxParser.parse("shutdown"),
                no_tokens=SyntaxParser.parse("no shutdown"),
            ),
            # 接口视图下的 IPv6 使能与 autostate（SVI / Loopback 常用）
            CommandRule(
                modes=["if"],
                syntax="ipv6 enable",
                no_cmd="no ipv6 enable",
                desc="在接口上启用 IPv6 功能",
                requires="",
                state_key="interface.ipv6_enable",
                is_builtin=True,
                tokens=SyntaxParser.parse("ipv6 enable"),
                no_tokens=SyntaxParser.parse("no ipv6 enable"),
            ),
            CommandRule(
                modes=["if"],
                syntax="autostate",
                no_cmd="no autostate",
                desc="使能或关闭 SVI 接口的 autostate 状态联动",
                requires="",
                state_key="interface.autostate",
                is_builtin=True,
                tokens=SyntaxParser.parse("autostate"),
                no_tokens=SyntaxParser.parse("no autostate"),
            ),
            # 进入隧道接口的补充入口（配合 03_system.json 中的 tunnel 模式命令）
            CommandRule(
                modes=["global"],
                syntax="interface tunnel <tnl-id>",
                no_cmd="no interface tunnel <tnl-id>",
                desc="创建或进入 Tunnel 隧道接口配置模式",
                requires="",
                state_key="interface.tunnel",
                is_builtin=True,
                tokens=SyntaxParser.parse("interface tunnel <tnl-id>"),
                no_tokens=SyntaxParser.parse("no interface tunnel <tnl-id>"),
            ),
        ]
        for b in builtins:
            self._add_rule(b)

    # ------------------------------------------------------------------------
    # 视图栈与 CS6200 提示符管理
    # ------------------------------------------------------------------------

    @property
    def current_frame(self) -> ViewFrame:
        return self.view_stack[-1]

    @property
    def current_mode(self) -> str:
        return self.current_frame.mode

    def get_prompt(self) -> str:
        """动态生成符合神州数码 CS6200 规范的命令行提示符"""
        hostname = self.state["system"].get("hostname") or "Switch"
        frame = self.current_frame
        template = self.PROMPT_TEMPLATES.get(frame.mode, "{hostname}(config)#")
        return template.format(hostname=hostname, ctx=frame.context_name)

    def _is_loopback_view(self) -> bool:
        """当前是否处于 Loopback 环回接口配置视图"""
        return self.current_mode == "if" and self.current_frame.context_name.startswith("Loopback")

    def _current_view_rules(self) -> List[CommandRule]:
        """返回当前视图的合法规则；Loopback 视图中过滤掉二层端口命令"""
        rules = self.rules_by_mode.get(self.current_mode, [])
        if self._is_loopback_view():
            rules = [r for r in rules if r.state_key not in self.L2_PORT_STATE_KEYS]
        return rules

    def _view_rule_sets(self) -> List[List[CommandRule]]:
        """
        返回用于补全 / 帮助的规则集合：
        1. 当前视图自身规则；
        2. 若处于配置子视图，额外包含 global 规则（实机支持不经 exit 直接跳转执行）。
        """
        rule_sets = [self._current_view_rules()]
        if self.current_mode not in ("user", "priv", "global"):
            rule_sets.append(self.rules_by_mode.get("global", []))
        return rule_sets

    def push_view(self, mode: str, context_name: str = "", meta: Optional[Dict[str, Any]] = None) -> None:
        """压入新视图栈帧"""
        self.view_stack.append(ViewFrame(mode=mode, context_name=context_name, meta=meta or {}))

    def pop_view(self) -> None:
        """退出当前视图栈帧（保留最底层 user 视图）"""
        if len(self.view_stack) > 1:
            self.view_stack.pop()

    def return_to_priv(self) -> None:
        """end / Ctrl+Z 直接退回 priv 特权视图"""
        while len(self.view_stack) > 1 and self.current_mode != "priv":
            self.view_stack.pop()

    def return_to_global(self) -> None:
        """从任意配置子视图直接退回 global 全局配置视图"""
        while len(self.view_stack) > 1 and self.current_mode != "global":
            self.view_stack.pop()

    @staticmethod
    def normalize_interface_name(raw_if_type: str, raw_if_id: str) -> str:
        """
        将简写接口类型与编号规范化为神州数码标准接口名：
        - e1/0/1 | eth 1/0/1 | ethernet 1/0/1 -> Ethernet1/0/1
        - vlan 10 | v10 -> Vlan10
        - port-channel 1 | po1 -> Port-Channel1
        - tunnel 1 | tnl1 -> Tunnel1
        - loopback 1 | lo1 -> Loopback1
        """
        t = raw_if_type.lower()
        ident = raw_if_id.strip()
        if t.startswith("e"):
            return f"Ethernet{ident}"
        if t.startswith("v"):
            return f"Vlan{ident}"
        if t.startswith("po"):
            return f"Port-Channel{ident}"
        if t.startswith("lo"):
            return f"Loopback{ident}"
        if t.startswith("t"):
            return f"Tunnel{ident}"
        return f"{raw_if_type.capitalize()}{ident}"

    @classmethod
    def _expand_ethernet_list(cls, raw_list: str) -> List[str]:
        """将 '1/0/5-6' 或 '1/0/1-3,5' 展开为标准以太网接口名列表"""
        result: List[str] = []
        last_prefix = "1/0/"
        for part in raw_list.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                left, right = part.split("-", 1)
                if "/" in left:
                    prefix, start_s = left.rsplit("/", 1)
                    last_prefix = f"{prefix}/"
                else:
                    start_s = left
                end_s = right.rsplit("/", 1)[-1] if "/" in right else right
                if start_s.isdigit() and end_s.isdigit():
                    s_num, e_num = int(start_s), int(end_s)
                    if s_num > e_num:
                        s_num, e_num = e_num, s_num
                    for idx in range(s_num, e_num + 1):
                        name = cls.normalize_interface_name("ethernet", f"{last_prefix}{idx}")
                        if name not in result:
                            result.append(name)
            else:
                if "/" in part:
                    prefix, _ = part.rsplit("/", 1)
                    last_prefix = f"{prefix}/"
                    full_id = part
                else:
                    full_id = f"{last_prefix}{part}"
                name = cls.normalize_interface_name("ethernet", full_id)
                if name not in result:
                    result.append(name)
        return result or ["Ethernet1/0/1"]

    def _preprocess_input_tokens(self, raw_line: str) -> Tuple[List[str], List[int]]:
        """
        词法切分并记录每个 token 在原始输入行中的起始字符偏移（用于 '^' 报错定位），
        同时自动展开类似 `e1/0/1` 的紧凑型接口缩写为 `ethernet 1/0/1`。
        """
        tokens: List[str] = []
        offsets: List[int] = []

        for match in re.finditer(r"\S+", raw_line):
            word = match.group(0)
            start_pos = match.start()

            # 自动展开紧凑接口名：如 e1/0/1, eth1/0/1, ethernet1/0/1, po1, vlan10, lo1
            m_eth = re.match(r"^(e|eth|ethernet)(\d+/\d+/\d+(?:-\d+)?)$", word, re.IGNORECASE)
            m_po = re.match(r"^(po|port-channel)(\d+)$", word, re.IGNORECASE)
            m_vlan = re.match(r"^(vlan)(\d+)$", word, re.IGNORECASE)
            m_lo = re.match(r"^(lo|loopback)(\d+)$", word, re.IGNORECASE)

            if m_eth and len(tokens) >= 1 and "interface".startswith(tokens[-1].lower()):
                tokens.extend(["ethernet", m_eth.group(2)])
                offsets.extend([start_pos, start_pos + len(m_eth.group(1))])
            elif m_po and len(tokens) >= 1 and "interface".startswith(tokens[-1].lower()):
                tokens.extend(["port-channel", m_po.group(2)])
                offsets.extend([start_pos, start_pos + len(m_po.group(1))])
            elif m_vlan and len(tokens) >= 1 and "interface".startswith(tokens[-1].lower()):
                tokens.extend(["vlan", m_vlan.group(2)])
                offsets.extend([start_pos, start_pos + len(m_vlan.group(1))])
            elif m_lo and len(tokens) >= 1 and "interface".startswith(tokens[-1].lower()):
                tokens.extend(["loopback", m_lo.group(2)])
                offsets.extend([start_pos, start_pos + len(m_lo.group(1))])
            else:
                tokens.append(word)
                offsets.append(start_pos)

        return tokens, offsets

    # ------------------------------------------------------------------------
    # 前缀缩写匹配算法与错误游标定位
    # ------------------------------------------------------------------------

    @staticmethod
    def _match_keyword(input_word: str, target_keyword: str) -> Tuple[bool, bool]:
        """
        匹配单个输入词与目标关键字。
        返回: (是否匹配, 是否完全一致)
        """
        iw = input_word.lower()
        tw = target_keyword.lower()
        if iw == tw:
            return True, True
        if tw.startswith(iw):
            return True, False
        return False, False

    def _match_rule_tokens(
        self,
        input_words: List[str],
        syntax_tokens: List[SyntaxToken],
        allow_partial_tail: bool = False,
    ) -> Tuple[bool, int, Dict[str, Any], List[str]]:
        """
        递归匹配输入词序列与语法 Token 序列。
        返回:
          (matched, max_matched_idx, captured_args, next_possible_candidates)
        """
        def dfs(
            in_idx: int,
            tok_idx: int,
            captured: Dict[str, Any],
        ) -> Tuple[bool, int, Dict[str, Any], List[str]]:
            # 若输入词已全部消耗完
            if in_idx == len(input_words):
                candidates: List[str] = []
                # 收集当前位置及后续连续 optional 节点的可选提示
                scan = tok_idx
                while scan < len(syntax_tokens):
                    st = syntax_tokens[scan]
                    candidates.extend(self._describe_token_candidates(st))
                    if st.kind == "optional":
                        scan += 1
                    else:
                        break
                # 只有剩余部分全是 optional 或已到达语法末尾，才算完整匹配
                is_complete = all(t.kind == "optional" for t in syntax_tokens[tok_idx:])
                if is_complete or allow_partial_tail:
                    return True, in_idx, captured, candidates
                return False, in_idx, captured, candidates

            # 若语法 Token 已耗尽但仍有剩余输入词
            if tok_idx >= len(syntax_tokens):
                # 特殊放行：支持动态附加参数（如选项尾部 options / 多网关等）
                if syntax_tokens and syntax_tokens[-1].raw in ("[options]", "LINE", "<text>"):
                    extra = " ".join(input_words[in_idx:])
                    new_cap = dict(captured)
                    new_cap["extra"] = extra
                    return True, len(input_words), new_cap, []
                return False, in_idx, captured, []

            cur_tok = syntax_tokens[tok_idx]
            word = input_words[in_idx]
            best_idx = in_idx
            collected_cands: List[str] = []

            # 1) 关键字匹配
            if cur_tok.kind == "keyword":
                kw = cur_tok.choices[0]
                ok, _ = self._match_keyword(word, kw)
                if ok:
                    return dfs(in_idx + 1, tok_idx + 1, captured)
                return False, in_idx, captured, [kw]

            # 2) 互斥枚举选择 {a | b | c}
            if cur_tok.kind == "choice":
                matched_choices = [c for c in cur_tok.choices if self._match_keyword(word, c)[0]]
                # 精确匹配优先
                exact = [c for c in matched_choices if c.lower() == word.lower()]
                if exact:
                    matched_choices = exact
                if len(matched_choices) == 1:
                    new_cap = dict(captured)
                    new_cap[cur_tok.raw] = matched_choices[0]
                    return dfs(in_idx + 1, tok_idx + 1, new_cap)
                elif len(matched_choices) > 1:
                    # 歧义前缀标记
                    new_cap = dict(captured)
                    new_cap["__ambiguous__"] = matched_choices
                    return False, in_idx, new_cap, matched_choices
                return False, in_idx, captured, cur_tok.choices

            # 3) 参数占位符 <param>
            if cur_tok.kind == "param":
                if self._validate_param_value(word, cur_tok.param_name):
                    new_cap = dict(captured)
                    new_cap[cur_tok.param_name] = word
                    return dfs(in_idx + 1, tok_idx + 1, new_cap)
                return False, in_idx, captured, [cur_tok.param_name]

            # 4) 复杂组合 { <ip> <mask> | any-source | host-source <ip> }
            if cur_tok.kind == "group":
                if cur_tok.choices:
                    for alt in cur_tok.choices:
                        alt_tokens = SyntaxParser.parse(alt)
                        ok, m_idx, sub_cap, cands = dfs_sub_sequence(in_idx, alt_tokens, tok_idx + 1, captured)
                        if ok:
                            return True, m_idx, sub_cap, cands
                        if m_idx > best_idx:
                            best_idx = m_idx
                        collected_cands.extend(cands)
                    return False, best_idx, captured, collected_cands
                elif cur_tok.sub_tokens:
                    return dfs_sub_sequence(in_idx, cur_tok.sub_tokens, tok_idx + 1, captured)

            # 5) 可选分支 [ ... ]
            if cur_tok.kind == "optional":
                # 分支 A：尝试匹配该可选项
                if cur_tok.choices:
                    matched_choices = [c for c in cur_tok.choices if self._match_keyword(word, c)[0]]
                    if len(matched_choices) == 1:
                        new_cap = dict(captured)
                        new_cap[cur_tok.raw] = matched_choices[0]
                        ok, m_idx, sub_cap, cands = dfs(in_idx + 1, tok_idx + 1, new_cap)
                        if ok:
                            return True, m_idx, sub_cap, cands
                elif cur_tok.sub_tokens:
                    ok, m_idx, sub_cap, cands = dfs_sub_sequence(in_idx, cur_tok.sub_tokens, tok_idx + 1, captured)
                    if ok:
                        return True, m_idx, sub_cap, cands
                    if m_idx > best_idx:
                        best_idx = m_idx

                # 分支 B：跳过该可选项，直接匹配后续 Token
                ok_skip, m_idx_skip, sub_cap_skip, cands_skip = dfs(in_idx, tok_idx + 1, captured)
                if ok_skip:
                    return True, m_idx_skip, sub_cap_skip, cands_skip
                return False, max(best_idx, m_idx_skip), captured, collected_cands + cands_skip

            return False, in_idx, captured, []

        def dfs_sub_sequence(
            in_idx: int,
            sub_tokens: List[SyntaxToken],
            next_tok_idx: int,
            captured: Dict[str, Any],
        ) -> Tuple[bool, int, Dict[str, Any], List[str]]:
            combined = sub_tokens + syntax_tokens[next_tok_idx:]
            # 临时替换外层 syntax_tokens 进行延续匹配
            saved = syntax_tokens[:]
            syntax_tokens[:] = combined
            try:
                return dfs(in_idx, 0, captured)
            finally:
                syntax_tokens[:] = saved

        return dfs(0, 0, {})

    @staticmethod
    def _validate_param_value(word: str, param_name: str) -> bool:
        """基础参数格式校验，防止关键字误吞参数槽位"""
        p = param_name.lower()

        # 显式数值范围占位符（如 <5000-5099>、<1-15>、<30-3600>）必须落在区间内，
        # 否则 `access-list 101 ...` 会同时命中标准扩展 ACL 与 5000-5099 源控制 ACL。
        range_match = re.fullmatch(r"<\s*(\d+)\s*-\s*(\d+)\s*>", param_name.strip())
        if range_match:
            if not word.isdigit():
                return False
            low, high = int(range_match.group(1)), int(range_match.group(2))
            return low <= int(word) <= high
        if "loopback-id" in p:
            return word.isdigit() and 0 <= int(word) <= 1023
        if "ipv6" in p:
            return ":" in word
        if "ip" in p or "a.b.c.d" in p or "gateway" in p or "network" in p:
            # 支持 IPv4 或 IPv4/Prefix
            return bool(re.match(r"^\d{1,3}(\.\d{1,3}){3}(/\d{1,2})?$", word))
        if "mac" in p:
            return bool(re.match(r"^([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}$|^([0-9a-fA-F]{4}\.){2}[0-9a-fA-F]{4}$", word))
        if "vlan-id" in p or "instance-id" in p or "process" in p or "num" in p or "value" in p or "priority" in p:
            return word.isdigit()
        return True

    @classmethod
    def _describe_token_candidates(cls, tok: SyntaxToken) -> List[str]:
        """提取单个语法节点的候选词列表，用于 `?` 帮助与 Tab 补全"""
        if tok.kind in ("keyword", "choice"):
            return list(tok.choices)
        if tok.kind == "param":
            return [tok.param_name]
        if tok.kind in ("group", "optional"):
            if tok.choices:
                return list(tok.choices)
            if tok.sub_tokens:
                return cls._describe_token_candidates(tok.sub_tokens[0])
        return []

    def _format_invalid_marker(self, raw_line: str, error_col: int) -> str:
        """生成与 CS6200 完全一致的 '^' 错误位置指示器"""
        prompt_len = len(self.get_prompt()) + 1  # +1 代表提示符后的空格
        spaces = " " * max(0, prompt_len + error_col)
        return f"{spaces}^\n% Invalid input detected at '^' marker."

    # ------------------------------------------------------------------------
    # `?` 上下文实时帮助与 `get_completions(line)` Tab 补全
    # ------------------------------------------------------------------------

    def get_context_help(self, line_before_q: str) -> str:
        """
        处理用户输入的 `?` 上下文帮助：
        - 空行输入 `?`：列出当前视图所有合法命令及 desc 说明
        - 半截命令后紧跟 `?`（无空格，如 `sh?`）：列出匹配前缀的关键字
        - 半截命令后加空格再输入 `?`（如 `show ?`）：列出下一参数/关键字候选
        """
        rule_sets = self._view_rule_sets()
        stripped = line_before_q.strip()

        # 情况 1：行首直接输入 `?`
        if not stripped:
            lines = []
            seen = set()
            for rules in rule_sets:
                for r in rules:
                    key = r.syntax
                    if key not in seen:
                        seen.add(key)
                        lines.append(f"  {r.syntax:<46} {r.desc}")
            return "\n".join(lines) if lines else "  No commands available in current mode."

        trailing_space = line_before_q.endswith(" ")
        words, _ = self._preprocess_input_tokens(line_before_q)

        # 情况 2：紧跟单词末尾输入 `?`（如 `conf?`）-> 补全当前词
        if not trailing_space:
            prefix = words[-1].lower()
            prev_words = words[:-1]
            matches: Dict[str, str] = {}

            for rules in rule_sets:
                for r in rules:
                    if not prev_words:
                        first_tok = r.tokens[0] if r.tokens else None
                        if first_tok:
                            for cand in self._describe_token_candidates(first_tok):
                                if not cand.startswith("<") and cand.lower().startswith(prefix):
                                    matches[cand] = r.desc
                    else:
                        ok, _, _, next_cands = self._match_rule_tokens(prev_words, r.tokens, allow_partial_tail=True)
                        if ok:
                            for cand in next_cands:
                                if not cand.startswith("<") and cand.lower().startswith(prefix):
                                    matches[cand] = r.desc

            if not matches:
                return "% Unrecognized command"
            return "\n".join(f"  {k:<32} {v}" for k, v in sorted(matches.items()))

        # 情况 3：单词后加空格输入 `?`（如 `switchport ?`）-> 提示下一个词或参数
        next_options: Dict[str, str] = {}
        for rules in rule_sets:
            for r in rules:
                ok, _, _, next_cands = self._match_rule_tokens(words, r.tokens, allow_partial_tail=True)
                if ok:
                    if not next_cands:
                        next_options["<cr>"] = r.desc
                    else:
                        for cand in next_cands:
                            param_hint = r.params.get(cand, r.desc)
                            next_options[cand] = param_hint

        if not next_options:
            return "% Unrecognized command"
        return "\n".join(f"  {k:<32} {v}" for k, v in next_options.items())

    def get_completions(self, line: str) -> List[str]:
        """
        提供给终端前端的 Tab 自动补全接口。
        返回当前光标行末尾单词的所有合法唯一补全候选列表。
        """
        rule_sets = self._view_rule_sets()
        if not line.strip():
            # 空行按 Tab 返回当前视图（含可跳转的 global 视图）所有首关键字
            first_words = set()
            for rules in rule_sets:
                for r in rules:
                    if r.tokens:
                        for c in self._describe_token_candidates(r.tokens[0]):
                            if not c.startswith("<"):
                                first_words.add(c)
            return sorted(first_words)

        trailing_space = line.endswith(" ")
        words, _ = self._preprocess_input_tokens(line)

        if trailing_space:
            prev_words = words
            prefix = ""
        else:
            prev_words = words[:-1]
            prefix = words[-1].lower()

        candidates = set()
        for rules in rule_sets:
            for r in rules:
                if not prev_words:
                    if r.tokens:
                        for c in self._describe_token_candidates(r.tokens[0]):
                            if not c.startswith("<") and c.lower().startswith(prefix):
                                candidates.add(c)
                else:
                    ok, _, _, next_cands = self._match_rule_tokens(prev_words, r.tokens, allow_partial_tail=True)
                    if ok:
                        for c in next_cands:
                            if not c.startswith("<") and c.lower().startswith(prefix):
                                candidates.add(c)

        return sorted(candidates)

    # ------------------------------------------------------------------------
    # 命令执行主入口 & `no` 命令统一分发
    # ------------------------------------------------------------------------

    def _input_matches_any_rule(self, words: List[str], is_no: bool, rules: List[CommandRule]) -> bool:
        """判断输入是否完整匹配给定规则集合中的任意一条（含 `no` 形式）"""
        for rule in rules:
            ok, _, _, _ = self._match_rule_tokens(words, rule.tokens, allow_partial_tail=False)
            if ok:
                return True
            if is_no and not rule.syntax.lower().startswith("no "):
                no_toks = rule.no_tokens if rule.no_tokens else (
                    [SyntaxToken("no", "keyword", ["no"])] + rule.tokens
                )
                ok_no, _, _, _ = self._match_rule_tokens(words, no_toks, allow_partial_tail=False)
                if ok_no:
                    return True
        return False

    def _maybe_jump_to_global(self, words: List[str], is_no: bool) -> bool:
        """
        DCN 实机行为：在配置子视图内可直接输入全局模式命令完成视图跳转，
        例如 `SW1(config-if-vlan2017)# interface vlan 2018`。

        仅当该输入在当前子视图无任何匹配规则、但在 global 视图存在完整匹配规则时
        才自动退回全局视图，避免误吞 `port-group 1 mode on` 等视图内命令。
        """
        if self._input_matches_any_rule(words, is_no, self._current_view_rules()):
            return False
        if not self._input_matches_any_rule(words, is_no, self.rules_by_mode.get("global", [])):
            return False
        self.return_to_global()
        return True

    def execute_command(self, raw_input: str) -> str:
        """
        执行单行 CLI 输入（支持 Ctrl+Z、? 帮助、缩写匹配、歧义检查、no 路由与状态更新）
        """
        # 处理 Ctrl+Z (\x1a)
        if "\x1a" in raw_input:
            if self.current_mode != "user":
                self.return_to_priv()
            return ""

        # 处理 `?` 上下文帮助
        if raw_input.endswith("?"):
            return self.get_context_help(raw_input[:-1])

        line = raw_input.strip()
        if not line:
            return ""

        words, offsets = self._preprocess_input_tokens(raw_input)
        if not words:
            return ""

        # 判断是否为 `no` 否定命令前缀
        is_no = False
        if self._match_keyword(words[0], "no")[0] and len(words) > 1:
            is_no = True

        # DCN 实机行为：配置子视图内直接输入全局模式命令即可跳转视图
        if self.current_mode not in ("user", "priv", "global"):
            self._maybe_jump_to_global(words, is_no)

        # 查找匹配规则
        matched_rules: List[Tuple[CommandRule, Dict[str, Any]]] = []
        exact_rules: List[Tuple[CommandRule, Dict[str, Any]]] = []
        ambiguous_detected = False
        furthest_err_word_idx = 0

        current_rules = self._current_view_rules()

        for rule in current_rules:
            # 1) 优先尝试直接匹配正向 syntax（适用于自带 `no ...` 语法的规则，如 `no ip pim dense-mode`）
            ok, m_idx, captured, _ = self._match_rule_tokens(words, rule.tokens, allow_partial_tail=False)
            if ok:
                is_exact = self._is_exact_keyword_match(words, rule.tokens)
                # 若规则本身就是 `no xxx`，标记 actual_is_no = True
                actual_is_no = rule.syntax.lower().startswith("no ")
                matched_rules.append((rule, {**captured, "__actual_no__": actual_is_no}))
                if is_exact:
                    exact_rules.append((rule, {**captured, "__actual_no__": actual_is_no}))
                continue

            if "__ambiguous__" in captured:
                ambiguous_detected = True
                furthest_err_word_idx = max(furthest_err_word_idx, m_idx)
            else:
                furthest_err_word_idx = max(furthest_err_word_idx, m_idx)

            # 2) 若输入以 `no` 开头，尝试匹配规则的 `no_tokens` 或在正向规则前补 `no`
            if is_no and not rule.syntax.lower().startswith("no "):
                no_toks = rule.no_tokens if rule.no_tokens else ([SyntaxToken("no", "keyword", ["no"])] + rule.tokens)
                ok_no, m_idx_no, cap_no, _ = self._match_rule_tokens(words, no_toks, allow_partial_tail=False)
                if ok_no:
                    is_exact_no = self._is_exact_keyword_match(words, no_toks)
                    matched_rules.append((rule, {**cap_no, "__actual_no__": True}))
                    if is_exact_no:
                        exact_rules.append((rule, {**cap_no, "__actual_no__": True}))
                else:
                    furthest_err_word_idx = max(furthest_err_word_idx, m_idx_no)

        # 精确关键字匹配优先消除歧义
        candidates = exact_rules if exact_rules else matched_rules

        if len(candidates) > 1 or (len(candidates) == 0 and ambiguous_detected):
            return "% Ambiguous command."

        if len(candidates) == 0:
            err_idx = min(furthest_err_word_idx, len(offsets) - 1)
            return self._format_invalid_marker(raw_input, offsets[err_idx])

        selected_rule, args = candidates[0]
        final_is_no = bool(args.pop("__actual_no__", is_no))
        args["_raw_words"] = words

        return self.execute_handler(selected_rule.state_key, args, final_is_no, selected_rule)

    def _is_exact_keyword_match(self, words: List[str], tokens: List[SyntaxToken]) -> bool:
        """检查输入的所有关键字是否与模板完全拼写一致（非缩写）"""
        for w, t in zip(words, tokens):
            if t.kind == "keyword" and w.lower() != t.choices[0].lower():
                return False
        return True

    # ------------------------------------------------------------------------
    # 状态机执行钩子 (`execute_handler`) 与内置动作分发
    # ------------------------------------------------------------------------

    def execute_handler(
        self,
        state_key: str,
        args: Dict[str, Any],
        is_no: bool,
        rule: Optional[CommandRule] = None,
    ) -> str:
        """
        命令执行统一分发钩子：
        1. 优先调用外部注入的 `self.handler_callback(state_key, args, is_no)`（对接 handlers.py）
        2. 随后执行内置视图切换、核心状态树更新及查询回显
        """
        # 1. 外部 handlers.py 钩子回调
        if self.handler_callback is not None:
            ext_out = self.handler_callback(state_key, args, is_no)
            if ext_out is not None:
                return ext_out

        # 2. 内置视图导航与系统基础动作
        if state_key == "builtin.exit":
            self.pop_view()
            return ""

        if state_key == "builtin.end":
            self.return_to_priv()
            return ""

        if state_key == "builtin.write":
            self.state["running_config"] = copy.deepcopy(self._extract_persistable_state())
            self.state["startup_config"] = copy.deepcopy(self.state["running_config"])
            if self.persist_path and not self.save_startup_config():
                return "Building configuration...\n% Failed to save startup configuration."
            return "Building configuration...\n[OK]"

        if state_key == "system.enable":
            if is_no:
                self.pop_view()
                return ""
            level = int(args.get("<1-15>", 15))
            self.state["system"]["enable_level"] = level
            if self.current_mode == "user":
                self.push_view("priv")
            return ""

        if state_key == "system.config":
            if is_no:
                self.pop_view()
            elif self.current_mode == "priv":
                self.push_view("global")
            return ""

        if state_key == "system.hostname":
            self.state["system"]["hostname"] = "Switch" if is_no else args.get("<name>", "Switch")
            return ""

        # 3. 视图入口切换（接口 / VLAN / OSPF / DHCP / VTY / ACL / Time-Range / VRF / Tunnel）
        if state_key == "interface.ethernet":
            if_list = args.get("<interface-list>", "1/0/1")
            expanded_ifs = self._expand_ethernet_list(if_list)
            for norm_name in expanded_ifs:
                if norm_name not in self.state["interfaces"]:
                    self.state["interfaces"][norm_name] = self._default_interface_state(norm_name)
            ctx_label = expanded_ifs[0] if len(expanded_ifs) == 1 else "Port-Range"
            self.push_view(
                "if",
                context_name=ctx_label,
                meta={"if_type": "ethernet", "targets": expanded_ifs},
            )
            return ""

        if state_key == "interface.vlan":
            vid = args.get("<vlan-id>", "1")
            norm_name = self.normalize_interface_name("vlan", vid)
            if is_no:
                self.state["interfaces"].pop(norm_name, None)
                return ""
            if norm_name not in self.state["interfaces"]:
                self.state["interfaces"][norm_name] = self._default_interface_state(norm_name)
            self.push_view("if", context_name=norm_name, meta={"if_type": "vlan"})
            return ""

        if state_key == "interface.port_channel":
            pc_id = args.get("<port-channel-number>", "1")
            norm_name = self.normalize_interface_name("port-channel", pc_id)
            if is_no:
                self.state["interfaces"].pop(norm_name, None)
                return ""
            if norm_name not in self.state["interfaces"]:
                self.state["interfaces"][norm_name] = self._default_interface_state(norm_name)
            self.push_view("if", context_name=norm_name, meta={"if_type": "port-channel"})
            return ""

        if state_key == "interface.loopback":
            lo_id = str(args.get("<loopback-id>", "1"))
            norm_name = self.normalize_interface_name("loopback", lo_id)
            if is_no:
                self.state["interfaces"].pop(norm_name, None)
                return ""
            if norm_name not in self.state["interfaces"]:
                self.state["interfaces"][norm_name] = self._default_interface_state(norm_name)
            self.push_view("if", context_name=norm_name, meta={"if_type": "loopback"})
            return ""

        if state_key == "interface.tunnel":
            tnl_id = args.get("<tnl-id>", "1")
            if is_no:
                self.state["system"]["tunnels"].pop(tnl_id, None)
                return ""
            self.state["system"]["tunnels"].setdefault(tnl_id, {"mode": "gre", "nexthop": None})
            self.push_view("tunnel", context_name=tnl_id)
            return ""

        if state_key == "vlan.create":
            raw_vid = args.get("<vlan-id>") or args.get("WORD", "1")
            if is_no:
                if raw_vid.isdigit():
                    self.state["vlans"].pop(int(raw_vid), None)
                return ""
            if raw_vid.isdigit():
                vid_int = int(raw_vid)
                self.state["vlans"].setdefault(vid_int, {"name": f"VLAN{vid_int:04d}", "description": "", "shutdown": False})
                self.push_view("vlan", context_name=str(vid_int))
            return ""

        if state_key == "router.ospf":
            pid = args.get("<id>", "1")
            if is_no:
                self.state["ospf"]["processes"].pop(pid, None)
                return ""
            self.state["ospf"]["processes"].setdefault(pid, {"networks": []})
            self.push_view("router", context_name=f"ospf-{pid}")
            return ""

        if state_key == "dhcp.pool":
            pool_name = args.get("<name>", "default")
            if is_no:
                self.state["dhcp_pools"].pop(pool_name, None)
                return ""
            self.state["dhcp_pools"].setdefault(pool_name, {"network": None, "default_router": None, "options": {}})
            self.push_view("dhcp-pool", context_name=pool_name)
            return ""

        if state_key == "system.line_vty":
            first_l = args.get("<first-line>", "0")
            self.push_view("line", context_name=f"vty-{first_l}")
            return ""

        if state_key == "acl.ipv4.standard.named":
            acl_name = args.get("<name>", "STD_ACL")
            if is_no:
                self.state["acls"]["standard_named"].pop(acl_name, None)
                return ""
            self.state["acls"]["standard_named"].setdefault(acl_name, [])
            self.push_view("acl-std", context_name=acl_name)
            return ""

        if state_key == "acl.ipv4.extended.named":
            acl_name = args.get("<name>", "EXT_ACL")
            if is_no:
                self.state["acls"]["extended_named"].pop(acl_name, None)
                return ""
            self.state["acls"]["extended_named"].setdefault(acl_name, [])
            self.push_view("acl-ext", context_name=acl_name)
            return ""

        if state_key == "time_range.create":
            tr_name = args.get("<time_range_name>", "TR1")
            if is_no:
                self.state["system"]["time_ranges"].pop(tr_name, None)
                return ""
            self.state["system"]["time_ranges"].setdefault(tr_name, {"rules": []})
            self.push_view("time-range", context_name=tr_name)
            return ""

        if state_key == "vrf.instance":
            vrf_name = args.get("<vrf-name>", "VRF1")
            if is_no:
                self.state["system"]["vrfs"].pop(vrf_name, None)
                return ""
            self.state["system"]["vrfs"].setdefault(vrf_name, {"rd": None, "rt": []})
            self.push_view("vrf", context_name=vrf_name)
            return ""

        # 4. 典型状态树修改示例（其余由 handlers.py 精细接管）
        if state_key == "interface.shutdown" and self.current_mode == "if":
            if_obj = self.state["interfaces"].get(self.current_frame.context_name)
            if if_obj is not None:
                if_obj["shutdown"] = not is_no
            return ""

        if state_key == "interface.switchport_mode" and self.current_mode == "if":
            if_obj = self.state["interfaces"].get(self.current_frame.context_name)
            if if_obj is not None:
                if_obj["switchport_mode"] = args.get("{trunk | access | hybrid}", "access")
            return ""

        if state_key == "interface.access_vlan" and self.current_mode == "if":
            if_obj = self.state["interfaces"].get(self.current_frame.context_name)
            if if_obj is not None:
                if_obj["access_vlan"] = 1 if is_no else int(args.get("<vlan-id>", 1))
            return ""

        if state_key == "interface.ip_address" and self.current_mode == "if":
            if_obj = self.state["interfaces"].get(self.current_frame.context_name)
            if if_obj is not None:
                if_obj["ip_address"] = None if is_no else f"{args.get('<ip-address>')}/{args.get('<mask>')}"
            return ""

        if state_key == "interface.port_security.enable" and self.current_mode == "if":
            if_obj = self.state["interfaces"].get(self.current_frame.context_name)
            if if_obj is not None:
                if_obj["port_security"]["enabled"] = not is_no
            return ""

        if state_key == "firewall.enable":
            self.state["firewall_enabled"] = not is_no
            return ""

        # 5. 基础 show 命令回显
        if state_key == "show.running_config":
            return self._render_running_config()

        if state_key == "show.ip.interface.brief":
            return self._render_ip_int_brief()

        if rule and rule.output_example:
            return rule.output_example

        return ""

    # ------------------------------------------------------------------------
    # 内置配置渲染辅助方法
    # ------------------------------------------------------------------------

    def _render_running_config(self) -> str:
        lines = [
            "!",
            f"hostname {self.state['system']['hostname']}",
            "!",
        ]
        if self.state["firewall_enabled"]:
            lines.append("firewall enable")
        sys_state = self.state.get("system", {})
        if sys_state.get("ip_routing"):
            lines.append("ip routing")
        if sys_state.get("ipv6_unicast_routing") or sys_state.get("ipv6_unicast-routing"):
            lines.append("ipv6 unicast-routing")
        if sys_state.get("ipv6_enabled"):
            lines.append("ipv6 enable")
        for vid, vinfo in sorted(self.state["vlans"].items()):
            if vid != 1:
                lines.append(f"vlan {vid}")
                if vinfo.get("name"):
                    lines.append(f" name {vinfo['name']}")
                lines.append("!")
        for if_name, iinfo in self.state["interfaces"].items():
            lines.append(f"interface {if_name}")
            is_l3_logical = if_name.startswith(("Vlan", "Loopback", "Tunnel"))
            if not is_l3_logical:
                if iinfo.get("switchport_mode") != "access":
                    lines.append(f" switchport mode {iinfo['switchport_mode']}")
                if iinfo.get("switchport_mode") == "trunk":
                    if iinfo.get("trunk_allowed_vlans") not in (None, "all", "1-4094"):
                        lines.append(f" switchport trunk allowed vlan {iinfo['trunk_allowed_vlans']}")
                    if iinfo.get("trunk_native_vlan", 1) != 1:
                        lines.append(f" switchport trunk native vlan {iinfo['trunk_native_vlan']}")
                if iinfo.get("access_vlan") != 1:
                    lines.append(f" switchport access vlan {iinfo['access_vlan']}")
            if iinfo.get("ip_address"):
                ip, mask = iinfo["ip_address"].split("/")
                lines.append(f" ip address {ip} {mask}")
            if iinfo.get("ipv6_enable"):
                lines.append(" ipv6 enable")
            for v6_addr in iinfo.get("ipv6_addresses", []):
                lines.append(f" ipv6 address {v6_addr}")
            if iinfo.get("autostate") is False:
                lines.append(" no autostate")
            if iinfo.get("port_security", {}).get("enabled"):
                lines.append(" switchport port-security")
            if iinfo.get("shutdown"):
                lines.append(" shutdown")
            lines.append("!")
        lines.append("end")
        return "\n".join(lines)

    def _render_ip_int_brief(self) -> str:
        header = f"{'Interface':<22} {'IP-Address':<18} {'Status':<10} {'Protocol':<10}"
        rows = [header, "-" * len(header)]
        for if_name, iinfo in self.state["interfaces"].items():
            ip_str = iinfo["ip_address"].split("/")[0] if iinfo.get("ip_address") else "unassigned"
            status = "down" if iinfo.get("shutdown") else "up"
            rows.append(f"{if_name:<22} {ip_str:<18} {status:<10} {status:<10}")
        return "\n".join(rows)


# ============================================================================
# 4. 交互式命令行自测入口
# ============================================================================

if __name__ == "__main__":
    RULE_FILES = [
        "core_01_base.json",
        "core_02_l2.json",
        "core_03_l3.json",
        "core_04_security.json",
        "03_system.json",
        "04_system.json",
        "05_system.json",
    ]

    engine = CS6200CLIEngine(rule_files=RULE_FILES)
    print(f"[INFO] Loaded {len(engine.all_rules)} CLI rules across {len(engine.rules_by_mode)} views.")
    print("[INFO] Type 'exit' in user mode to quit, or enter '?' for contextual help.\n")

    while True:
        try:
            cmd = input(f"{engine.get_prompt()} ")
            if cmd.strip().lower() in ("quit", "exit") and engine.current_mode == "user":
                break
            output = engine.execute_command(cmd)
            if output:
                print(output)
        except (KeyboardInterrupt, EOFError):
            print("\nExiting CS6200 CLI Engine.")
            break