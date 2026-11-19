#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Digital China (神州数码) CS6200 Series Switch Command Handlers
=============================================================
核心网络协议状态树联动、前置条件校验 (requires) 与 Show 命令表格渲染 (`handlers.py`)

架构约定：
1. `CS6200CommandHandlers(engine)` 在初始化时将 `self.handle` 绑定到 `engine.handler_callback`，
   并向 `engine` 动态补充注册 5 条二层与 DHCP 查看命令规则（`show vlan`、`show spanning-tree`、
   `show port-group`、`show mac-address-table`、`show ip dhcp binding`）。
2. 视图跳转类命令（`interface.*`、单 VLAN 的 `vlan.create`、`router.ospf`、`dhcp.pool`、
   `acl.*.named`、`time_range.create`、`vrf.instance`、`builtin.*`）在完成状态节点初始化后
   返回 `None`，交由 `engine.py` 继续执行 `push_view()` / `pop_view()` 视图栈切换。
3. 其余已接管命令返回 `""` 或具体的提示/告警/表格字符串。
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from engine import CS6200CLIEngine, CommandRule, SyntaxParser


class CS6200CommandHandlers:
    """神州数码 CS6200 交换机协议状态联动与 Show 输出处理器"""

    # 视图跳转类 state_key（初始化状态后必须返回 None 交由 engine.py 压栈/弹栈）
    VIEW_TRANSITION_KEYS: Set[str] = {
        "builtin.exit",
        "builtin.end",
        "builtin.write",
        "system.enable",
        "system.config",
        "interface.ethernet",
        "interface.vlan",
        "interface.port_channel",
        "interface.tunnel",
        "router.ospf",
        "router.rip",
        "router.bgp",
        "router.msdp",
        "route_map.create",
        "dhcp.pool",
        "system.line_vty",
        "acl.ipv4.standard.named",
        "acl.ipv4.extended.named",
        "time_range.create",
        "vrf.instance",
    }

    @staticmethod
    def _normalize_generic_interface_name(raw: str) -> Optional[str]:
        """把真机 `show ip interface <interface-name>` 的接口名规范化为状态树键名"""
        text = raw.strip()
        patterns = [
            (r"^(?:ethernet|eth|e)\s*(\d+/\d+/\d+)$", "Ethernet"),
            (r"^(?:vlan)\s*(\d+)$", "Vlan"),
            (r"^(?:loopback|lo)\s*(\d+)$", "Loopback"),
            (r"^(?:port-channel|po)\s*(\d+)$", "Port-Channel"),
        ]
        for pattern, prefix in patterns:
            match = re.match(pattern, text, re.IGNORECASE)
            if match:
                return f"{prefix}{match.group(1)}"
        if re.match(r"^\d+/\d+/\d+$", text):
            return f"Ethernet{text}"
        return None

    @staticmethod
    def _render_one_ip_interface(if_name: str, iinfo: Dict[str, Any]) -> str:
        status = "down" if iinfo.get("shutdown") else "up"
        ip_addr = iinfo.get("ip_address") or "unassigned"
        vrf = iinfo.get("vrf") or "default"
        lines = [f"{if_name} is {status}, line protocol is {status}"]
        if iinfo.get("description"):
            lines.append(f"  Description: {iinfo['description']}")
        lines.append(f"  Internet address is {ip_addr}")
        lines.append(f"  VPN Routing/Forwarding \"{vrf}\"")
        return "\n".join(lines)

    def _render_show_ip_interface(self, args: Dict[str, Any], is_no: bool) -> str:
        names: List[str] = []
        if "<interface-name>" in args:
            name = self._normalize_generic_interface_name(str(args["<interface-name>"]))
            if name is None:
                return "% Invalid interface name."
            names = [name]
        elif "<vlan-id>" in args:
            names = [f"Vlan{args['<vlan-id>']}"]
        elif "<interface-list>" in args:
            try:
                names = self.engine._expand_ethernet_list(str(args["<interface-list>"]))
            except Exception:
                names = [self.engine.normalize_interface_name("ethernet", str(args["<interface-list>"]))]
        elif "<port-channel-number>" in args:
            names = [f"Port-Channel{args['<port-channel-number>']}"]
        elif "<loopback-id>" in args:
            names = [f"Loopback{args['<loopback-id>']}"]
        else:
            # 未指定接口：显示所有接口的 IPv4 运行状态
            names = sorted(self.engine.state.get("interfaces", {}).keys())

        blocks: List[str] = []
        for if_name in names:
            iinfo = self.engine.state.get("interfaces", {}).get(if_name)
            if not iinfo:
                blocks.append(f"% Interface {if_name} does not exist.")
                continue
            blocks.append(self._render_one_ip_interface(if_name, iinfo))
        return "\n\n".join(blocks)

    def __init__(self, engine: CS6200CLIEngine) -> None:
        self.engine = engine
        # 绑定统一回调钩子
        self.engine.handler_callback = self.handle
        # 补充缺失的状态树根节点（兼容独立初始化的 engine.state）
        self._ensure_extended_state()
        # 动态注册 5 条二层/DHCP 查看命令
        self._register_extra_rules()

    def _ensure_extended_state(self) -> None:
        """确保 state 中包含端口聚合、DHCP 绑定表、MAC 地址表等扩展结构"""
        self.engine.state.setdefault("port_groups", {})
        self.engine.state.setdefault("dhcp_bindings", [])
        self.engine.state.setdefault("mac_address_table", [])
        self.engine.state.setdefault("ospf", {"processes": {}})
        self.engine.state.setdefault("spanning_tree", {
            "enabled": True,
            "mode": "mstp",
            "mst_priorities": {0: 32768},
        })
        self.engine.state["spanning_tree"].setdefault("mst_priorities", {0: 32768})

    def _register_extra_rules(self) -> None:
        """向 engine 动态注册二层及 DHCP 查看命令（支持 priv 和 global 模式）"""
        extra_specs = [
            ("show vlan", "show.vlan", "显示VLAN配置及端口成员信息"),
            ("show spanning-tree", "show.spanning_tree", "显示生成树协议状态与网桥优先级"),
            ("show port-group", "show.port_group", "显示链路聚合组及成员端口信息"),
            ("show mac-address-table", "show.mac_address_table", "显示MAC地址转发表"),
            ("show ip dhcp binding", "show.ip_dhcp_binding", "显示DHCP服务器地址分配绑定表"),
        ]
        for syntax, state_key, desc in extra_specs:
            rule = CommandRule(
                modes=["priv", "global"],
                syntax=syntax,
                no_cmd="",
                desc=desc,
                requires="",
                state_key=state_key,
                is_builtin=False,
                tokens=SyntaxParser.parse(syntax),
            )
            self.engine._add_rule(rule)

    # ========================================================================
    # 1. 统一命令路由入口 (`handle`)
    # ========================================================================

    def handle(self, state_key: str, args: Dict[str, Any], is_no: bool) -> Optional[str]:
        """
        核心命令分发方法，由 `CS6200CLIEngine.execute_handler()` 调用。
        返回 `None` 时交由 `engine.py` 执行默认视图栈操作；
        返回字符串（含 `""`）时表示命令已由本处理器完整接管。
        """
        # 1) 视图跳转类命令：初始化必要状态后返回 None 让 engine.py 切换视图
        if state_key in self.VIEW_TRANSITION_KEYS:
            self._prepare_view_transition_state(state_key, args, is_no)
            return None

        # 2) VLAN 创建/删除特殊处理：单个 VLAN 需进入 Config-Vlan 子视图 (返回 None)，
        #    批量 VLAN (如 10-20 或 10,20,30) 与 no vlan 留在当前视图 (返回字符串)
        if state_key == "vlan.create":
            return self._handle_vlan_create(args, is_no)

        # 2.1) route-map 节点内的 set 语句：记录到当前 route-map 的最新节点
        if state_key.startswith("route_map.set."):
            map_name = self.engine.current_frame.context_name
            rmap = self.engine.state.get("route_maps", {}).get(map_name)
            entries = rmap.get("entries", []) if isinstance(rmap, dict) else []
            if entries:
                raw = " ".join(args.get("_raw_words", []))
                positive = raw[3:] if raw.lower().startswith("no ") else raw
                if is_no:
                    entries[-1]["sets"] = [s for s in entries[-1].get("sets", []) if s != positive]
                else:
                    entries[-1].setdefault("sets", []).append(positive)
            return ""

        # 3) 路由表映射表
        dispatch_map = {
            # 系统基础
            "system.hostname": self._handle_hostname,
            "system.enable_password": self._handle_enable_password,
            "system.load_balance": self._handle_load_balance,
            "system.authentication_line": self._handle_authentication_line,
            "system.authentication_enable": self._handle_authentication_enable,
            "system.authorization_line": self._handle_authorization_line,
            "system.accounting_line": self._handle_accounting_line,
            "system.username": self._handle_username,
            "system.telnet_server": self._handle_telnet_server,
            "system.mtu": self._handle_system_mtu,
            "system.clock": self._handle_clock_set,
            "ipv6.enable": self._handle_ipv6_enable,
            "firewall.enable": self._handle_firewall_enable,
            "arp.anti_arpscan.enable": self._handle_anti_arpscan,
            # VLAN 与二层接口
            "vlan.name": self._handle_vlan_name,
            "interface.shutdown": self._handle_interface_shutdown,
            "interface.description": self._handle_if_description,
            "interface.bandwidth": self._handle_if_bandwidth,
            "interface.mtu": self._handle_if_mtu,
            "interface.port_loopback": self._handle_if_port_loopback,
            "interface.loopback_detection.specified_vlan": self._handle_if_loopback_detection_vlan,
            "interface.loopback_detection.control": self._handle_if_loopback_detection_control,
            "loopback_detection.interval": self._handle_loopback_detection_interval,
            "loopback_detection.control_recovery": self._handle_loopback_detection_recovery,
            "interface.switchport_mode": self._handle_switchport_mode,
            "interface.access_vlan": self._handle_switchport_access_vlan,
            "interface.trunk_allowed_vlan": self._handle_switchport_trunk_allowed,
            "interface.trunk_native_vlan": self._handle_switchport_trunk_native,
            "interface.hybrid_allowed_vlan": self._handle_switchport_hybrid_allowed,
            "interface.hybrid_native_vlan": self._handle_switchport_hybrid_native,
            # 链路聚合与生成树
            "system.port_group.create": self._handle_global_port_group,
            "interface.port_group_member": self._handle_if_port_group_member,
            "interface.lacp_timeout": self._handle_if_lacp_timeout,
            "interface.lacp_port_priority": self._handle_if_lacp_priority,
            "system.lacp_system_priority": self._handle_global_lacp_priority,
            "spanning_tree.enable": self._handle_stp_enable,
            "spanning_tree.mode": self._handle_stp_mode,
            "spanning_tree.mst.priority": self._handle_stp_mst_priority,
            "interface.spanning_tree.portfast": self._handle_if_stp_portfast,
            # 三层接口、静态路由、OSPF、DHCP
            "interface.ip_address": self._handle_if_ip_address,
            "interface.ipv6_address": self._handle_if_ipv6_address,
            "interface.ipv6_enable": self._handle_if_ipv6_enable,
            "interface.autostate": self._handle_if_autostate,
            "ip.route.static": self._handle_ip_route_static,
            "ip.route.blackhole": self._handle_ip_route_blackhole,
            "router.ospf.network": self._handle_ospf_network,
            "dhcp.pool.network_address": self._handle_dhcp_network,
            "dhcp.pool.default_router": self._handle_dhcp_default_router,
            "dhcp.pool.hardware_address": self._handle_dhcp_hardware_address,
            "dhcp.pool.host": self._handle_dhcp_host,
            "dhcp.pool.client_identifier": self._handle_dhcp_client_id,
            "dhcp.pool.option43.ascii": lambda a, n: self._handle_dhcp_option("43", "ascii", a, n),
            "dhcp.pool.option43.hex": lambda a, n: self._handle_dhcp_option("43", "hex", a, n),
            "dhcp.pool.option43.ip": lambda a, n: self._handle_dhcp_option("43", "ip", a, n),
            "dhcp.pool.option60.ascii": lambda a, n: self._handle_dhcp_option("60", "ascii", a, n),
            "dhcp.pool.option60.hex": lambda a, n: self._handle_dhcp_option("60", "hex", a, n),
            "dhcp.pool.option60.ip": lambda a, n: self._handle_dhcp_option("60", "ip", a, n),
            "dhcp.pool.option43": lambda a, n: self._handle_dhcp_option("43", "clear", a, True),
            "dhcp.pool.option60": lambda a, n: self._handle_dhcp_option("60", "clear", a, True),
            # ACL 与端口安全
            "acl.ipv4.standard.numbered": self._handle_acl_std_numbered,
            "acl.ipv4.extended.numbered": self._handle_acl_ext_numbered,
            "acl.ipv4.standard.named.rule": self._handle_acl_std_named_rule,
            "acl.ipv4.extended.named.rule": self._handle_acl_ext_named_rule,
            "interface.ip_access_group": self._handle_if_ip_access_group,
            "interface.port_security.enable": self._handle_port_security_enable,
            "interface.port_security.maximum": self._handle_port_security_maximum,
            "interface.port_security.violation": self._handle_port_security_violation,
            "interface.port_security.mac_address": self._handle_port_security_mac,
            "interface.port_security.aging": self._handle_port_security_aging,
            # Show 命令全集
            "show.running_config": self._render_running_config,
            "show.startup_config": self._render_startup_config,
            "show.running_config.current_mode": self._render_show_current_mode,
            "show.version": self._render_show_version,
            "show.privilege": self._render_show_privilege,
            "show.users": self._render_show_users,
            "show.slot": self._render_show_slot,
            "show.lldp": self._render_show_lldp,
            "show.transceiver": self._render_show_transceiver,
            "show.vlan": self._render_show_vlan,
            "show.spanning_tree": self._render_show_spanning_tree,
            "show.port_group": self._render_show_port_group,
            "show.ip.route": self._render_show_ip_route,
            "show.interface": self._render_show_interface,
            "show.ipv6.interface": self._render_show_ipv6_interface,
            "show.loopback_detection": self._render_show_loopback_detection,
            "show.port_security": self._render_show_port_security,
            "show.ip.ospf.neighbor": self._render_show_ospf_neighbor,
            "show.mac_address_table": self._render_show_mac_table,
            "show.ip_dhcp_binding": self._render_show_dhcp_binding,
            "show.ip.interface": self._render_show_ip_interface,
        }

        handler_fn = dispatch_map.get(state_key)
        if handler_fn is not None:
            # 当处于多端口范围视图 (Config-If-Port-Range) 时，对范围内每个端口依次执行配置
            if (
                    self.engine.current_mode == "if"
                    and not state_key.startswith("show.")
                    and len(self.engine.current_frame.meta.get("targets", [])) > 1
            ):
                targets = self.engine.current_frame.meta["targets"]
                orig_ctx = self.engine.current_frame.context_name
                outputs: List[str] = []
                try:
                    for t_if in targets:
                        self.engine.current_frame.context_name = t_if
                        res = handler_fn(args, is_no)
                        if res and res not in outputs:
                            outputs.append(res)
                finally:
                    self.engine.current_frame.context_name = orig_ctx
                return "\n".join(outputs)

            return handler_fn(args, is_no)

        # 未显式接管的命令交还 engine 默认处理
        return None

    # ========================================================================
    # 2. 视图切换前置状态准备
    # ========================================================================

    def _prepare_view_transition_state(self, state_key: str, args: Dict[str, Any], is_no: bool) -> None:
        """在 engine.py 压入子视图前，确保状态树中的目标对象结构完整"""
        if is_no:
            return
        if state_key == "interface.vlan":
            vid = args.get("<vlan-id>", "1")
            if vid.isdigit():
                self.engine.state["vlans"].setdefault(
                    int(vid), {"name": f"VLAN{int(vid):04d}"}
                )
        elif state_key == "dhcp.pool":
            pool_name = args.get("<name>", "default")
            self.engine.state["dhcp_pools"].setdefault(pool_name, {
                "network": None,
                "default_router": None,
                "options": {},
                "static_hosts": [],
                "pending_mac": None,
                "pending_client_id": None,
            })
        elif state_key == "router.ospf":
            pid = str(args.get("<id>", "1"))
            self.engine.state["ospf"]["processes"].setdefault(pid, {
                "networks": [],
                "neighbors": [],
            })
        elif state_key == "route_map.create":
            map_name = str(args.get("<map_name>", "RM"))
            action = str(args.get("{deny | permit}") or "permit").lower()
            seq = str(args.get("<sequence_num>", "10"))
            rmap = self.engine.state.setdefault("route_maps", {}).setdefault(
                map_name, {"entries": []}
            )
            if not any(str(e.get("seq")) == seq and e.get("action") == action for e in rmap["entries"]):
                rmap["entries"].append({"action": action, "seq": seq, "sets": []})

    # ========================================================================
    # 3. VLAN 与端口二层模式联动
    # ========================================================================

    @staticmethod
    def _parse_vlan_word(word: str) -> List[int]:
        """解析神州数码 VLAN 列表/范围字符串（如 '10'、'10-20'、'10;20;30-35'、'10,20'）"""
        vlan_ids: Set[int] = set()
        for segment in re.split(r"[;,]+", word):
            seg = segment.strip()
            if not seg:
                continue
            if "-" in seg:
                parts = seg.split("-", 1)
                if parts[0].isdigit() and parts[1].isdigit():
                    start, end = int(parts[0]), int(parts[1])
                    if start > end:
                        start, end = end, start
                    for v in range(max(1, start), min(4094, end) + 1):
                        vlan_ids.add(v)
            elif seg.isdigit():
                v = int(seg)
                if 1 <= v <= 4094:
                    vlan_ids.add(v)
        return sorted(vlan_ids)

    @staticmethod
    def _format_vlan_set(vlan_set: Set[int]) -> str:
        """将 VLAN ID 集合压缩格式化为神州数码标准字符串（VLAN 间以 ';' 分隔，如 '1;10-15;20'）"""
        if not vlan_set:
            return "none"
        if len(vlan_set) == 4094:
            return "all"
        sorted_ids = sorted(vlan_set)
        ranges: List[str] = []
        start = prev = sorted_ids[0]
        for v in sorted_ids[1:]:
            if v == prev + 1:
                prev = v
            else:
                ranges.append(f"{start}-{prev}" if start != prev else str(start))
                start = prev = v
        ranges.append(f"{start}-{prev}" if start != prev else str(start))
        return ";".join(ranges)

    def _handle_vlan_create(self, args: Dict[str, Any], is_no: bool) -> Optional[str]:
        raw_word = str(args.get("WORD") or args.get("<vlan-id>") or "").strip()
        vlan_ids = self._parse_vlan_word(raw_word)
        if not vlan_ids:
            return "% Invalid VLAN ID or range."

        if is_no:
            warnings: List[str] = []
            for vid in vlan_ids:
                if vid == 1:
                    warnings.append("% Default VLAN 1 cannot be deleted.")
                    continue
                self.engine.state["vlans"].pop(vid, None)
                # 同步将划入已删除 VLAN 的 Access 端口回退至 VLAN 1
                for iinfo in self.engine.state["interfaces"].values():
                    if iinfo.get("access_vlan") == vid:
                        iinfo["access_vlan"] = 1
            return "\n".join(warnings)

        # 单个 VLAN 创建：初始化后返回 None 交由 engine.py 进入 Switch(Config-VlanX)#
        if len(vlan_ids) == 1:
            vid = vlan_ids[0]
            self.engine.state["vlans"].setdefault(
                vid, {"name": f"VLAN{vid:04d}"}
            )
            args["<vlan-id>"] = str(vid)
            return None

        # 批量 VLAN 创建 (如 vlan 10-20 或 vlan 10,20,30)：创建全部后停留全局配置模式
        for vid in vlan_ids:
            self.engine.state["vlans"].setdefault(
                vid, {"name": f"VLAN{vid:04d}"}
            )
        return ""

    def _handle_vlan_name(self, args: Dict[str, Any], is_no: bool) -> str:
        ctx = self.engine.current_frame.context_name
        if ctx.isdigit():
            vid = int(ctx)
            vinfo = self.engine.state["vlans"].setdefault(
                vid, {"name": f"VLAN{vid:04d}"}
            )
            vinfo["name"] = f"VLAN{vid:04d}" if is_no else args.get("<vlan-name>", f"VLAN{vid:04d}")
        return ""

    def _get_current_interface(self) -> Optional[Dict[str, Any]]:
        if self.engine.current_mode != "if":
            return None
        return self.engine.state["interfaces"].get(self.engine.current_frame.context_name)

    def _handle_interface_shutdown(self, args: Dict[str, Any], is_no: bool) -> str:
        if_obj = self._get_current_interface()
        if if_obj is not None:
            if_obj["shutdown"] = not is_no
        return ""

    def _handle_if_description(self, args: Dict[str, Any], is_no: bool) -> str:
        """配置接口描述；描述文本允许包含空格（由解析器并入 extra）"""
        if_obj = self._get_current_interface()
        if if_obj is None:
            return "% Command only valid in interface configuration mode."
        if is_no:
            if_obj["description"] = ""
            return ""
        first = str(args.get("<text>") or "").strip()
        rest = str(args.get("extra") or "").strip()
        if_obj["description"] = " ".join(part for part in (first, rest) if part)
        return ""

    def _handle_if_bandwidth(self, args: Dict[str, Any], is_no: bool) -> str:
        """配置接口带宽（kbit/s），Loopback / VLAN 等三层逻辑接口同样支持"""
        if_obj = self._get_current_interface()
        if if_obj is None:
            return "% Command only valid in interface configuration mode."
        if is_no:
            if_obj["bandwidth"] = None
            return ""
        raw = str(args.get("<bandwidth>") or "").strip()
        if not raw.isdigit() or not (1 <= int(raw) <= 100000000):
            return "% Invalid bandwidth value, range is 1-100000000."
        if_obj["bandwidth"] = int(raw)
        return ""

    def _handle_if_mtu(self, args: Dict[str, Any], is_no: bool) -> str:
        """配置接口 MTU，no 形式恢复设备默认值 1500 字节"""
        if_obj = self._get_current_interface()
        if if_obj is None:
            return "% Command only valid in interface configuration mode."
        if is_no:
            if_obj["mtu"] = 1500
            return ""
        raw = str(args.get("<mtu-value>") or "").strip()
        if not raw.isdigit() or not (64 <= int(raw) <= 9216):
            return "% Invalid MTU value, range is 64-9216."
        if_obj["mtu"] = int(raw)
        return ""

    def _handle_if_port_loopback(self, args: Dict[str, Any], is_no: bool) -> str:
        """打开/关闭物理端口的环回测试（诊断功能，不写入 running-config）"""
        if_obj = self._get_current_interface()
        if if_obj is None:
            return "% Command only valid in interface configuration mode."
        if_obj["port_loopback"] = not is_no
        return ""

    @staticmethod
    def _loopback_detection_defaults() -> Dict[str, Any]:
        return {"enabled": False, "vlans": [], "control": None, "status": "Normal"}

    def _handle_if_loopback_detection_vlan(self, args: Dict[str, Any], is_no: bool) -> str:
        """在端口上启动/关闭环路检测，并记录需要检测的 VLAN 列表"""
        if_obj = self._get_current_interface()
        if if_obj is None:
            return "% Command only valid in interface configuration mode."
        ld = if_obj.setdefault("loopback_detection", self._loopback_detection_defaults())
        if is_no:
            ld["enabled"] = False
            ld["vlans"] = []
            return ""
        raw = str(args.get("<vlan-list>") or "").strip()
        vlan_ids = self._parse_vlan_word(raw)
        if not vlan_ids:
            return "% Invalid VLAN list."
        ld["enabled"] = True
        ld["vlans"] = vlan_ids
        return ""

    def _handle_if_loopback_detection_control(self, args: Dict[str, Any], is_no: bool) -> str:
        """配置环路检测受控方式：shutdown / block / learning"""
        if_obj = self._get_current_interface()
        if if_obj is None:
            return "% Command only valid in interface configuration mode."
        ld = if_obj.setdefault("loopback_detection", self._loopback_detection_defaults())
        if is_no:
            ld["control"] = None
            return ""
        ld["control"] = str(args.get("{shutdown | block | learning}") or "shutdown").lower()
        return ""

    def _handle_loopback_detection_interval(self, args: Dict[str, Any], is_no: bool) -> str:
        ld = self.engine.state.setdefault("loopback_detection", {})
        if is_no:
            ld["interval_time"] = None
            ld["no_loopback_time"] = None
            return ""
        raw_interval = str(args.get("<loopback>") or "").strip()
        raw_no_loop = str(args.get("<no-loopback>") or "").strip()
        if not raw_interval.isdigit() or not raw_no_loop.isdigit():
            return "% Invalid interval time."
        interval, no_loop = int(raw_interval), int(raw_no_loop)
        if not (1 <= interval <= 3600) or not (1 <= no_loop <= 3600):
            return "% Invalid interval time, range is 1-3600."
        ld["interval_time"] = interval
        ld["no_loopback_time"] = no_loop
        return ""

    def _handle_loopback_detection_recovery(self, args: Dict[str, Any], is_no: bool) -> str:
        ld = self.engine.state.setdefault("loopback_detection", {})
        if is_no:
            ld["control_recovery"] = None
            return ""
        ld["control_recovery"] = int(args.get("<0-3600>", 0))
        return ""

    def _handle_switchport_mode(self, args: Dict[str, Any], is_no: bool) -> str:
        if_obj = self._get_current_interface()
        if if_obj is None:
            return "% Command only valid in interface configuration mode."
        mode = "access" if is_no else args.get("{trunk | access | hybrid}", "access")
        if_obj["switchport_mode"] = mode
        if mode == "hybrid":
            if_obj.setdefault("hybrid_allowed_vlans", {"tag": [], "untag": [1]})
        return ""

    def _handle_switchport_access_vlan(self, args: Dict[str, Any], is_no: bool) -> str:
        if_obj = self._get_current_interface()
        if if_obj is None:
            return "% Command only valid in interface configuration mode."
        # 前置条件校验：端口必须处于 access 模式
        if if_obj.get("switchport_mode") != "access":
            return "% Command rejected: interface is not in access mode."

        if is_no:
            if_obj["access_vlan"] = 1
            return ""

        raw_vid = str(args.get("<vlan-id>", "1"))
        if not raw_vid.isdigit() or not (1 <= int(raw_vid) <= 4094):
            return "% Invalid VLAN ID."

        vid = int(raw_vid)
        notice = ""
        # 神州数码特性：若目标 VLAN 不存在，自动创建并输出提示
        if vid not in self.engine.state["vlans"]:
            self.engine.state["vlans"][vid] = {
                "name": f"VLAN{vid:04d}",
                "description": "",
                "shutdown": False,
            }
            notice = f"% Access VLAN {vid} does not exist, creating it."

        if_obj["access_vlan"] = vid
        return notice

    def _handle_switchport_trunk_allowed(self, args: Dict[str, Any], is_no: bool) -> str:
        if_obj = self._get_current_interface()
        if if_obj is None:
            return "% Command only valid in interface configuration mode."
        if if_obj.get("switchport_mode") != "trunk":
            return "% Command rejected: interface is not in trunk mode."

        all_vlans = set(range(1, 4095))
        if is_no:
            if_obj["trunk_allowed_vlans"] = "all"
            if_obj["trunk_allowed_vlan_list"] = sorted(all_vlans)
            return ""

        # 初始化当前允许集合
        cur_str = if_obj.get("trunk_allowed_vlans", "all")
        if cur_str == "all":
            current_set = set(all_vlans)
        elif cur_str == "none":
            current_set = set()
        else:
            current_set = set(self._parse_vlan_word(cur_str))

        raw_words = [w.lower() for w in args.get("_raw_words", [])]
        word_arg = str(args.get("WORD", ""))

        if "all" in raw_words:
            current_set = set(all_vlans)
        elif "add" in raw_words:
            current_set |= set(self._parse_vlan_word(word_arg))
        elif "remove" in raw_words:
            current_set -= set(self._parse_vlan_word(word_arg))
        elif "except" in raw_words:
            current_set = all_vlans - set(self._parse_vlan_word(word_arg))
        else:
            current_set = set(self._parse_vlan_word(word_arg))

        if_obj["trunk_allowed_vlans"] = self._format_vlan_set(current_set)
        if_obj["trunk_allowed_vlan_list"] = sorted(current_set)
        return ""

    def _handle_switchport_trunk_native(self, args: Dict[str, Any], is_no: bool) -> str:
        if_obj = self._get_current_interface()
        if if_obj is None:
            return ""
        if if_obj.get("switchport_mode") != "trunk":
            return "% Command rejected: interface is not in trunk mode."
        if_obj["trunk_native_vlan"] = 1 if is_no else int(args.get("<vlan-id>", 1))
        return ""

    def _handle_switchport_hybrid_allowed(self, args: Dict[str, Any], is_no: bool) -> str:
        if_obj = self._get_current_interface()
        if if_obj is None:
            return ""
        if if_obj.get("switchport_mode") != "hybrid":
            return "% Command rejected: interface is not in hybrid mode."
        tag_mode = args.get("{tag | untag}", "untag")
        word_arg = str(args.get("WORD", ""))
        vids = set(self._parse_vlan_word(word_arg))
        hybrid_cfg = if_obj.setdefault("hybrid_allowed_vlans", {"tag": [], "untag": [1]})
        cur = set(hybrid_cfg.get(tag_mode, []))
        if is_no or "remove" in [w.lower() for w in args.get("_raw_words", [])]:
            cur -= vids
        else:
            cur |= vids
        hybrid_cfg[tag_mode] = sorted(cur)
        return ""

    def _handle_switchport_hybrid_native(self, args: Dict[str, Any], is_no: bool) -> str:
        if_obj = self._get_current_interface()
        if if_obj is None:
            return ""
        if if_obj.get("switchport_mode") != "hybrid":
            return "% Command rejected: interface is not in hybrid mode."
        if_obj["hybrid_native_vlan"] = 1 if is_no else int(args.get("<vlan-id>", 1))
        return ""

    # ========================================================================
    # 4. 链路聚合 (Port-Group) 与生成树 (MSTP)
    # ========================================================================

    def _handle_global_port_group(self, args: Dict[str, Any], is_no: bool) -> str:
        pg_id = str(args.get("<port-group-number>", "1"))
        pc_name = f"Port-Channel{pg_id}"
        if is_no:
            pg = self.engine.state["port_groups"].pop(pg_id, None)
            if pg:
                for member in pg.get("members", []):
                    if member in self.engine.state["interfaces"]:
                        self.engine.state["interfaces"][member]["port_group"] = None
            self.engine.state["interfaces"].pop(pc_name, None)
            return ""

        self.engine.state["port_groups"].setdefault(pg_id, {
            "id": pg_id,
            "mode": "on",
            "protocol": "Static",
            "members": [],
        })
        if pc_name not in self.engine.state["interfaces"]:
            self.engine.state["interfaces"][pc_name] = self.engine._default_interface_state(pc_name)
        return ""

    def _handle_if_port_group_member(self, args: Dict[str, Any], is_no: bool) -> str:
        if_name = self.engine.current_frame.context_name
        if_obj = self._get_current_interface()
        if if_obj is None:
            return ""
        if if_name.startswith("Port-Channel"):
            return "% Cannot add a Port-Channel interface to a port-group."

        if is_no:
            old_pg = if_obj.get("port_group")
            if old_pg:
                pg_id = str(old_pg.get("group_id"))
                pg_entry = self.engine.state["port_groups"].get(pg_id)
                if pg_entry and if_name in pg_entry["members"]:
                    pg_entry["members"].remove(if_name)
                if_obj["port_group"] = None
            return ""

        pg_id = str(args.get("<port-group-number>", "1"))
        mode = args.get("{active | passive | on}", "active")
        protocol = "Static" if mode == "on" else "LACP"

        # 确保聚合组与对应逻辑口 Port-Channel<id> 自动创建
        pg_entry = self.engine.state["port_groups"].setdefault(pg_id, {
            "id": pg_id,
            "mode": mode,
            "protocol": protocol,
            "members": [],
        })
        pg_entry["mode"] = mode
        pg_entry["protocol"] = protocol
        if if_name not in pg_entry["members"]:
            pg_entry["members"].append(if_name)

        pc_name = f"Port-Channel{pg_id}"
        if pc_name not in self.engine.state["interfaces"]:
            self.engine.state["interfaces"][pc_name] = self.engine._default_interface_state(pc_name)

        if_obj["port_group"] = {"group_id": pg_id, "mode": mode}
        return ""

    def _handle_if_lacp_timeout(self, args: Dict[str, Any], is_no: bool) -> str:
        if_obj = self._get_current_interface()
        if if_obj is not None:
            if_obj["lacp_timeout"] = "long" if is_no else args.get("{short | long}", "long")
        return ""

    def _handle_if_lacp_priority(self, args: Dict[str, Any], is_no: bool) -> str:
        if_obj = self._get_current_interface()
        if if_obj is not None:
            if_obj["lacp_port_priority"] = 32768 if is_no else int(args.get("<port-priority>", 32768))
        return ""

    def _handle_global_lacp_priority(self, args: Dict[str, Any], is_no: bool) -> str:
        self.engine.state["system"]["lacp_system_priority"] = (
            32768 if is_no else int(args.get("<system-priority>", 32768))
        )
        return ""

    def _handle_stp_enable(self, args: Dict[str, Any], is_no: bool) -> str:
        self.engine.state["spanning_tree"]["enabled"] = not is_no
        return ""

    def _handle_stp_mode(self, args: Dict[str, Any], is_no: bool) -> str:
        self.engine.state["spanning_tree"]["mode"] = (
            "mstp" if is_no else args.get("{mstp | stp | rstp}", "mstp")
        )
        return ""

    def _handle_stp_mst_priority(self, args: Dict[str, Any], is_no: bool) -> str:
        raw_inst = str(args.get("<instance-id>", "0"))
        if not raw_inst.isdigit():
            return "% Invalid MST instance ID."
        inst_id = int(raw_inst)

        if is_no:
            self.engine.state["spanning_tree"]["mst_priorities"][inst_id] = 32768
            return ""

        raw_pri = str(args.get("<bridge-priority>", "32768"))
        if not raw_pri.isdigit():
            return "% Bridge priority must be a numeric value."
        pri = int(raw_pri)

        # 神州数码 CS6200 校验：网桥优先级必须为 4096 的整数倍 (0 ~ 61440)
        if pri < 0 or pri > 61440 or (pri % 4096) != 0:
            return "% Bridge priority must be in increments of 4096 (0-61440)."

        self.engine.state["spanning_tree"]["mst_priorities"][inst_id] = pri
        return ""

    def _handle_if_stp_portfast(self, args: Dict[str, Any], is_no: bool) -> str:
        if_obj = self._get_current_interface()
        if if_obj is not None:
            if is_no:
                if_obj.pop("stp_portfast", None)
            else:
                if_obj["stp_portfast"] = args.get("[bpdufilter | bpduguard]", "enabled")
        return ""

    # ========================================================================
    # 5. 三层路由 (Static, OSPF) 与 DHCP 地址池联动
    # ========================================================================

    @staticmethod
    def _normalize_prefix_and_mask(prefix_or_ip: str, mask_arg: Optional[str] = None) -> Tuple[str, str, int]:
        """将 `<ip> <mask>` 或 `<ip>/<prefix>` 统一解析为 (network_ip, netmask, prefix_len)"""
        if "/" in prefix_or_ip:
            net = ipaddress.IPv4Network(prefix_or_ip, strict=False)
            return str(net.network_address), str(net.netmask), net.prefixlen
        if mask_arg:
            # 兼容通配符反掩码 (如 0.0.0.255) 与标准掩码 (如 255.255.255.0)
            octets = [int(x) for x in mask_arg.split(".")]
            if octets[0] == 0 and octets[-1] in (255, 127, 63, 31, 15, 7, 3, 1):
                inv_mask = ".".join(str(255 - o) for o in octets)
                net = ipaddress.IPv4Network(f"{prefix_or_ip}/{inv_mask}", strict=False)
            else:
                net = ipaddress.IPv4Network(f"{prefix_or_ip}/{mask_arg}", strict=False)
            return str(net.network_address), str(net.netmask), net.prefixlen
        net = ipaddress.IPv4Network(f"{prefix_or_ip}/32", strict=False)
        return str(net.network_address), "255.255.255.255", 32

    def _handle_if_ip_address(self, args: Dict[str, Any], is_no: bool) -> str:
        if_obj = self._get_current_interface()
        if if_obj is None:
            return ""
        if is_no:
            if_obj["ip_address"] = None
            return ""
        ip_str = args.get("<ip-address>", "")
        mask_str = args.get("<mask>", "255.255.255.0")
        try:
            ipaddress.IPv4Interface(f"{ip_str}/{mask_str}")
        except ValueError:
            return "% Invalid IPv4 address or subnet mask."
        if_obj["ip_address"] = f"{ip_str}/{mask_str}"
        return ""

    def _handle_if_ipv6_address(self, args: Dict[str, Any], is_no: bool) -> str:
        if_obj = self._get_current_interface()
        if if_obj is None:
            return ""
        v6 = args.get("<ipv6-address/prefix-length>", "")
        v6_list = if_obj.setdefault("ipv6_addresses", [])
        if is_no:
            if v6 in v6_list:
                v6_list.remove(v6)
        elif v6:
            if v6 not in v6_list:
                v6_list.append(v6)
            # CS6200 真机配置 IPv6 地址后接口 IPv6 协议自动进入使能状态
            if_obj["ipv6_enable"] = True
        return ""

    def _handle_if_ipv6_enable(self, args: Dict[str, Any], is_no: bool) -> str:
        if_obj = self._get_current_interface()
        if if_obj is not None:
            if_obj["ipv6_enable"] = not is_no
            if_obj["ipv6_enable_explicit"] = not is_no
        return ""

    def _handle_if_autostate(self, args: Dict[str, Any], is_no: bool) -> str:
        if_obj = self._get_current_interface()
        if if_obj is not None:
            if_obj["autostate"] = not is_no
        return ""

    def _handle_ip_route_static(self, args: Dict[str, Any], is_no: bool) -> str:
        raw_prefix = args.get("<ip-prefix>", "")
        raw_mask = (
            args.get("<mask> | <prefix-length>")
            or args.get("<mask>")
            or args.get("<prefix-length>")
        )
        next_hop = (
            args.get("<gateway-address> | <gateway-interface>")
            or args.get("<gateway-address>")
            or args.get("<gateway-interface>")
            or ""
        )
        distance = int(args.get("<distance>", 1))

        try:
            net_ip, netmask, plen = self._normalize_prefix_and_mask(raw_prefix, raw_mask)
        except ValueError:
            return "% Invalid destination network or mask."

        routes: List[Dict[str, Any]] = self.engine.state["static_routes"]
        if is_no:
            self.engine.state["static_routes"] = [
                r for r in routes
                if not (
                    r["network"] == net_ip
                    and r["mask"] == netmask
                    and (not next_hop or r["next_hop"] == next_hop)
                )
            ]
            return ""

        # 去重更新
        for r in routes:
            if r["network"] == net_ip and r["mask"] == netmask and r["next_hop"] == next_hop:
                r["distance"] = distance
                return ""

        routes.append({
            "network": net_ip,
            "mask": netmask,
            "prefix_len": plen,
            "next_hop": next_hop,
            "distance": distance,
        })
        return ""

    def _handle_ip_route_blackhole(self, args: Dict[str, Any], is_no: bool) -> str:
        args_copy = dict(args)
        args_copy["<gateway-address> | <gateway-interface>"] = "Null0"
        return self._handle_ip_route_static(args_copy, is_no)

    def _handle_ospf_network(self, args: Dict[str, Any], is_no: bool) -> str:
        ctx = self.engine.current_frame.context_name  # 如 'ospf-1'
        pid = ctx.split("-", 1)[-1] if "-" in ctx else "1"
        proc = self.engine.state["ospf"]["processes"].setdefault(pid, {"networks": [], "neighbors": []})

        raw_net = args.get("<network>", "")
        raw_mask = (
            args.get("<mask> | <prefix>")
            or args.get("<mask>")
            or args.get("<prefix>")
        )
        area_id = str(args.get("<area_id>", "0"))

        try:
            net_ip, netmask, plen = self._normalize_prefix_and_mask(raw_net, raw_mask)
        except ValueError:
            return "% Invalid OSPF network statement."

        entry = {
            "network": net_ip,
            "wildcard_or_mask": raw_mask or f"/{plen}",
            "prefix_len": plen,
            "area": area_id,
        }

        if is_no:
            proc["networks"] = [
                n for n in proc["networks"]
                if not (n["network"] == net_ip and n["area"] == area_id)
            ]
            proc["neighbors"] = [
                nb for nb in proc.get("neighbors", []) if nb.get("area") != area_id
            ]
            return ""

        if not any(n["network"] == net_ip and n["area"] == area_id for n in proc["networks"]):
            proc["networks"].append(entry)

        # 联动生成虚拟 OSPF 邻居状态（匹配直连 L3 接口或基于宣告网段生成）
        self._synthesize_ospf_neighbor(proc, net_ip, plen, area_id)
        return ""

    def _synthesize_ospf_neighbor(self, proc: Dict[str, Any], net_ip: str, plen: int, area_id: str) -> None:
        """为宣告的 OSPF 网段模拟生成 Full/DR 邻接状态"""
        target_net = ipaddress.IPv4Network(f"{net_ip}/{plen}", strict=False)
        matched_if = "Vlan10"
        peer_ip = None

        for if_name, iinfo in self.engine.state["interfaces"].items():
            if iinfo.get("ip_address") and not iinfo.get("shutdown"):
                ip_part = iinfo["ip_address"].split("/")[0]
                if ipaddress.IPv4Address(ip_part) in target_net:
                    matched_if = if_name
                    # 选取同网段下一个可用 IP 作为虚拟对端邻居
                    for host in target_net.hosts():
                        if str(host) != ip_part:
                            peer_ip = str(host)
                            break
                    break

        if peer_ip is None:
            hosts = list(target_net.hosts())
            peer_ip = str(hosts[1]) if len(hosts) > 1 else str(target_net.network_address)

        neighbors: List[Dict[str, Any]] = proc.setdefault("neighbors", [])
        if not any(nb["address"] == peer_ip for nb in neighbors):
            idx = len(neighbors) + 2
            neighbors.append({
                "neighbor_id": f"{idx}.{idx}.{idx}.{idx}",
                "priority": 1,
                "state": "Full/DR",
                "dead_time": "00:00:36",
                "address": peer_ip,
                "interface": matched_if,
                "area": area_id,
            })

    def _get_current_dhcp_pool(self) -> Optional[Tuple[str, Dict[str, Any]]]:
        if self.engine.current_mode != "dhcp-pool":
            return None
        pool_name = self.engine.current_frame.context_name
        pool = self.engine.state["dhcp_pools"].setdefault(pool_name, {
            "network": None,
            "default_router": None,
            "options": {},
            "static_hosts": [],
            "pending_mac": None,
            "pending_client_id": None,
        })
        return pool_name, pool

    def _handle_dhcp_network(self, args: Dict[str, Any], is_no: bool) -> str:
        res = self._get_current_dhcp_pool()
        if not res:
            return ""
        _, pool = res
        if is_no:
            pool["network"] = None
            return ""
        net_num = str(args.get("<network-number>", ""))
        mask = str(args.get("<mask>") or args.get("<prefix-length>") or "").strip()
        if not mask:
            pool["network"] = net_num
        elif mask.isdigit():
            pool["network"] = f"{net_num}/{mask}"
        else:
            pool["network"] = f"{net_num} {mask}"
        return ""

    def _handle_dhcp_default_router(self, args: Dict[str, Any], is_no: bool) -> str:
        res = self._get_current_dhcp_pool()
        if not res:
            return ""
        _, pool = res
        if is_no:
            pool["default_router"] = None
            return ""
        gateways = [
            str(args.get(f"<address{i}>", "")).strip()
            for i in range(1, 9)
            if args.get(f"<address{i}>")
        ]
        pool["default_router"] = " ".join(gateways)
        return ""

    def _handle_dhcp_hardware_address(self, args: Dict[str, Any], is_no: bool) -> str:
        res = self._get_current_dhcp_pool()
        if not res:
            return ""
        pool_name, pool = res
        if is_no:
            pool["pending_mac"] = None
            return ""
        mac = args.get("<hardware-address>", "")
        pool["pending_mac"] = mac
        # 若此前已先配置 host，则合并生成静态绑定
        if pool.get("static_hosts"):
            pool["static_hosts"][-1]["mac"] = mac
            self._sync_dhcp_bindings(pool_name, pool)
        return ""

    def _handle_dhcp_client_id(self, args: Dict[str, Any], is_no: bool) -> str:
        res = self._get_current_dhcp_pool()
        if not res:
            return ""
        _, pool = res
        pool["pending_client_id"] = None if is_no else args.get("<unique-identifier>", "")
        return ""

    def _handle_dhcp_host(self, args: Dict[str, Any], is_no: bool) -> str:
        res = self._get_current_dhcp_pool()
        if not res:
            return ""
        pool_name, pool = res
        ip_addr = args.get("<address>", "")
        mask = args.get("<mask> | <prefix-length>", "255.255.255.0")
        if is_no:
            pool["static_hosts"] = []
            self.engine.state["dhcp_bindings"] = [
                b for b in self.engine.state["dhcp_bindings"] if b.get("pool") != pool_name
            ]
            return ""

        mac = pool.get("pending_mac") or pool.get("pending_client_id") or "0000.0000.0000"
        pool.setdefault("static_hosts", []).append({
            "ip": ip_addr,
            "mask": mask,
            "mac": mac,
        })
        self._sync_dhcp_bindings(pool_name, pool)
        return ""

    def _sync_dhcp_bindings(self, pool_name: str, pool: Dict[str, Any]) -> None:
        """将 DHCP 地址池中的静态绑定同步至全局 dhcp_bindings 表"""
        self.engine.state["dhcp_bindings"] = [
            b for b in self.engine.state["dhcp_bindings"] if b.get("pool") != pool_name
        ]
        for item in pool.get("static_hosts", []):
            self.engine.state["dhcp_bindings"].append({
                "pool": pool_name,
                "ip": item["ip"],
                "mac": item["mac"],
                "lease": "Infinite",
                "type": "Manual",
            })

    def _handle_dhcp_option(self, code: str, fmt: str, args: Dict[str, Any], is_no: bool) -> str:
        res = self._get_current_dhcp_pool()
        if not res:
            return ""
        _, pool = res
        opts = pool.setdefault("options", {})
        if is_no:
            opts.pop(code, None)
            return ""
        val = args.get("LINE") or args.get("WORD") or args.get("A.B.C.D") or ""
        opts[code] = {"format": fmt, "value": val}
        return ""

    # ========================================================================
    # 6. ACL、防火墙包过滤与端口安全 (Port-Security)
    # ========================================================================

    def _handle_acl_std_numbered(self, args: Dict[str, Any], is_no: bool) -> str:
        num = str(args.get("<num>", "1"))
        std_acls = self.engine.state["acls"]["standard_numbered"]
        if is_no:
            std_acls.pop(num, None)
            return ""
        raw_words = args.get("_raw_words", [])
        rule_text = " ".join(raw_words[2:])
        std_acls.setdefault(num, []).append(rule_text)
        return ""

    def _handle_acl_ext_numbered(self, args: Dict[str, Any], is_no: bool) -> str:
        num = str(args.get("<num>", "100"))
        ext_acls = self.engine.state["acls"]["extended_numbered"]
        if is_no:
            ext_acls.pop(num, None)
            return ""
        raw_words = args.get("_raw_words", [])
        rule_text = " ".join(raw_words[2:])
        ext_acls.setdefault(num, []).append(rule_text)
        return ""

    def _handle_acl_std_named_rule(self, args: Dict[str, Any], is_no: bool) -> str:
        acl_name = self.engine.current_frame.context_name
        rules = self.engine.state["acls"]["standard_named"].setdefault(acl_name, [])
        raw_words = args.get("_raw_words", [])
        rule_text = " ".join(raw_words[1:] if is_no else raw_words)
        if is_no:
            if rule_text in rules:
                rules.remove(rule_text)
        else:
            rules.append(rule_text)
        return ""

    def _handle_acl_ext_named_rule(self, args: Dict[str, Any], is_no: bool) -> str:
        acl_name = self.engine.current_frame.context_name
        rules = self.engine.state["acls"]["extended_named"].setdefault(acl_name, [])
        raw_words = args.get("_raw_words", [])
        rule_text = " ".join(raw_words[1:] if is_no else raw_words)
        if is_no:
            if rule_text in rules:
                rules.remove(rule_text)
        else:
            rules.append(rule_text)
        return ""

    def _handle_firewall_enable(self, args: Dict[str, Any], is_no: bool) -> str:
        self.engine.state["firewall_enabled"] = not is_no
        return ""

    def _handle_if_ip_access_group(self, args: Dict[str, Any], is_no: bool) -> str:
        if_obj = self._get_current_interface()
        if if_obj is None:
            return ""

        acl_name = str(args.get("<acl-name>", ""))
        direction = args.get("{in|out}", "in")

        if is_no:
            if_obj.setdefault("ip_access_group", {"in": None, "out": None})[direction] = None
            return ""

        # 前置条件校验：必须先在全局开启 firewall enable
        if not self.engine.state.get("firewall_enabled", False):
            return "% Firewall is not enabled globally."

        if_obj.setdefault("ip_access_group", {"in": None, "out": None})[direction] = acl_name
        return ""

    def _handle_port_security_enable(self, args: Dict[str, Any], is_no: bool) -> str:
        if_obj = self._get_current_interface()
        if if_obj is None:
            return ""
        psec = if_obj.setdefault("port_security", {
            "enabled": False,
            "maximum": 1,
            "violation": "shutdown",
            "mac_addresses": [],
            "aging": {},
        })
        psec["enabled"] = not is_no
        return ""

    def _ensure_port_security_enabled(self) -> Optional[str]:
        """校验当前接口是否已开启 switchport port-security"""
        if_obj = self._get_current_interface()
        if if_obj is None:
            return "% Command only valid in interface configuration mode."
        if not if_obj.get("port_security", {}).get("enabled", False):
            return "% Port-security is not enabled on this interface."
        return None

    def _handle_port_security_maximum(self, args: Dict[str, Any], is_no: bool) -> str:
        err = self._ensure_port_security_enabled()
        if err:
            return err
        if_obj = self._get_current_interface()
        assert if_obj is not None
        if_obj["port_security"]["maximum"] = 1 if is_no else int(args.get("<value>", 1))
        return ""

    def _handle_port_security_violation(self, args: Dict[str, Any], is_no: bool) -> str:
        err = self._ensure_port_security_enabled()
        if err:
            return err
        if_obj = self._get_current_interface()
        assert if_obj is not None
        action = "shutdown" if is_no else args.get("{protect | recovery | restrict | shutdown}", "shutdown")
        if_obj["port_security"]["violation"] = action
        return ""

    def _handle_port_security_mac(self, args: Dict[str, Any], is_no: bool) -> str:
        err = self._ensure_port_security_enabled()
        if err:
            return err
        if_obj = self._get_current_interface()
        assert if_obj is not None
        mac = args.get("<mac-address>", "")
        vid = int(args.get("<vlan-id>", if_obj.get("access_vlan", 1)))
        mac_list: List[Dict[str, Any]] = if_obj["port_security"].setdefault("mac_addresses", [])
        if is_no:
            if_obj["port_security"]["mac_addresses"] = [m for m in mac_list if m["mac"] != mac]
        else:
            if not any(m["mac"] == mac and m["vlan"] == vid for m in mac_list):
                mac_list.append({"mac": mac, "vlan": vid})
        return ""

    def _handle_port_security_aging(self, args: Dict[str, Any], is_no: bool) -> str:
        err = self._ensure_port_security_enabled()
        if err:
            return err
        if_obj = self._get_current_interface()
        assert if_obj is not None
        aging = if_obj["port_security"].setdefault("aging", {})
        if is_no:
            aging.clear()
        elif "<value>" in args:
            aging["time"] = int(args["<value>"])
        elif "{absolute | inactivity}" in args:
            aging["type"] = args["{absolute | inactivity}"]
        else:
            aging["static"] = True
        return ""

    # ========================================================================
    # 7. 其他全局辅助处理器
    # ========================================================================

    def _handle_hostname(self, args: Dict[str, Any], is_no: bool) -> str:
        self.engine.state["system"]["hostname"] = "Switch" if is_no else args.get("<name>", "Switch")
        return ""

    def _handle_enable_password(self, args: Dict[str, Any], is_no: bool) -> str:
        self.engine.state["system"]["enable_password"] = None if is_no else args.get("<password>", "")
        return ""

    def _handle_load_balance(self, args: Dict[str, Any], is_no: bool) -> str:
        self.engine.state["system"]["load_balance"] = (
            "dst-src-mac-ip" if is_no else args.get("{dst-src-mac | dst-src-ip | dst-src-mac-ip }", "dst-src-mac-ip")
        )
        return ""

    def _handle_anti_arpscan(self, args: Dict[str, Any], is_no: bool) -> str:
        self.engine.state["anti_arpscan_enabled"] = not is_no
        return ""

    def _handle_authentication_line(self, args: Dict[str, Any], is_no: bool) -> str:
        line_type = str(args.get("{console | vty | web}") or "vty").lower()
        lines_state = self.engine.state["system"].setdefault("authentication_lines", {})
        if is_no:
            lines_state.pop(line_type, None)
            return ""
        method = str(args.get("{local | radius | tacacs | none}") or "local").lower()
        lines_state[line_type] = method
        return ""

    def _handle_authentication_enable(self, args: Dict[str, Any], is_no: bool) -> str:
        if is_no:
            self.engine.state["system"].pop("authentication_enable", None)
            return ""
        self.engine.state["system"]["authentication_enable"] = str(
            args.get("{local | radius | tacacs | enable}") or "local"
        ).lower()
        return ""

    def _handle_authorization_line(self, args: Dict[str, Any], is_no: bool) -> str:
        line_type = str(args.get("{console | vty | web}") or "vty").lower()
        authz = self.engine.state["system"].setdefault("authorization_lines", {})
        if is_no:
            authz.pop(line_type, None)
            return ""
        authz[line_type] = str(args.get("{local | radius | tacacs}") or "local").lower()
        return ""

    def _handle_accounting_line(self, args: Dict[str, Any], is_no: bool) -> str:
        line_type = str(args.get("{console | vty}") or "vty").lower()
        level = str(args.get("<1-15>", "15"))
        acct = self.engine.state["system"].setdefault("accounting_lines", {})
        if is_no:
            acct.pop(f"{line_type}-{level}", None)
            return ""
        acct[f"{line_type}-{level}"] = {
            "mode": str(args.get("{start-stop | stop-only | none}") or "stop-only").lower(),
            "method": str(args.get("{radius | tacacs}") or "radius").lower(),
        }
        return ""

    def _handle_username(self, args: Dict[str, Any], is_no: bool) -> str:
        usernames = self.engine.state["system"].setdefault("usernames", {})
        name = str(args.get("<username>") or "")
        if not name:
            return "% Invalid username."
        if is_no:
            usernames.pop(name, None)
            return ""
        usernames[name] = {
            "privilege": int(args.get("<privilege>", 15) or 15),
            "password": args.get("<password>") or "",
        }
        return ""

    def _handle_telnet_server(self, args: Dict[str, Any], is_no: bool) -> str:
        self.engine.state["system"]["telnet_server"] = not is_no
        return ""

    def _handle_system_mtu(self, args: Dict[str, Any], is_no: bool) -> str:
        if is_no:
            self.engine.state["system"]["mtu"] = None
            return ""
        raw = str(args.get("<mtu-value>") or "").strip()
        if raw and (not raw.isdigit() or not (64 <= int(raw) <= 12288)):
            return "% Invalid MTU value."
        self.engine.state["system"]["mtu"] = int(raw) if raw else 12288
        return ""

    def _handle_clock_set(self, args: Dict[str, Any], is_no: bool) -> str:
        if is_no:
            return ""
        self.engine.state["system"]["clock"] = {
            "time": args.get("<HH:MM:SS>"),
            "date": args.get("[YYYY.MM.DD]"),
        }
        return ""

    def _handle_ipv6_enable(self, args: Dict[str, Any], is_no: bool) -> str:
        self.engine.state["system"]["ipv6_enabled"] = not is_no
        return ""

    # ========================================================================
    # 8. 完整 `show running-config` 与对齐表格渲染器
    # ========================================================================

    def _interface_config_lines(self, if_name: str, iinfo: Dict[str, Any]) -> List[str]:
        """渲染单个接口的配置块（不含结尾 '!'），供 running-config 与 show running-config current-mode 复用"""
        lines: List[str] = [f"interface {if_name}"]
        if iinfo.get("description"):
            lines.append(f" description {iinfo['description']}")
        if iinfo.get("bandwidth"):
            lines.append(f" bandwidth {iinfo['bandwidth']}")
        if iinfo.get("mtu") and int(iinfo.get("mtu") or 1500) != 1500:
            lines.append(f" mtu {iinfo['mtu']}")
        is_l3_logical = if_name.startswith(("Vlan", "Loopback", "Tunnel"))
        sw_mode = iinfo.get("switchport_mode", "access")
        if not is_l3_logical:
            if sw_mode != "access":
                lines.append(f" switchport mode {sw_mode}")
            if sw_mode == "access" and iinfo.get("access_vlan", 1) != 1:
                lines.append(f" switchport access vlan {iinfo['access_vlan']}")
            if sw_mode == "trunk":
                if iinfo.get("trunk_allowed_vlans") not in (None, "all", "1-4094"):
                    lines.append(f" switchport trunk allowed vlan {iinfo['trunk_allowed_vlans']}")
                if iinfo.get("trunk_native_vlan", 1) != 1:
                    lines.append(f" switchport trunk native vlan {iinfo['trunk_native_vlan']}")

        pg = iinfo.get("port_group")
        if not is_l3_logical and pg:
            lines.append(f" port-group {pg['group_id']} mode {pg['mode']}")

        ld = iinfo.get("loopback_detection") or {}
        if ld.get("vlans"):
            lines.append(f" loopback-detection specified-vlan {self._format_vlan_set(set(ld['vlans']))}")
        if ld.get("control"):
            lines.append(f" loopback-detection control {ld['control']}")

        if iinfo.get("ip_address"):
            ip_part, mask_part = iinfo["ip_address"].split("/")
            lines.append(f" ip address {ip_part} {mask_part}")

        # IPv6 双栈配置（接口使能 / 地址 / autostate）
        # 仅显式执行过 ipv6 enable 时才回显，地址配置触发的隐式使能不写入配置
        if iinfo.get("ipv6_enable_explicit"):
            lines.append(" ipv6 enable")
        for v6_addr in iinfo.get("ipv6_addresses", []):
            lines.append(f" ipv6 address {v6_addr}")
        if iinfo.get("autostate") is False:
            lines.append(" no autostate")

        ag = iinfo.get("ip_access_group", {})
        if ag.get("in"):
            lines.append(f" ip access-group {ag['in']} in")
        if ag.get("out"):
            lines.append(f" ip access-group {ag['out']} out")

        psec = iinfo.get("port_security", {})
        if psec.get("enabled"):
            lines.append(" switchport port-security")
            if psec.get("maximum", 1) != 1:
                lines.append(f" switchport port-security maximum {psec['maximum']}")
            if psec.get("violation", "shutdown") != "shutdown":
                lines.append(f" switchport port-security violation {psec['violation']}")
            for m in psec.get("mac_addresses", []):
                lines.append(f" switchport port-security mac-address {m['mac']} vlan {m['vlan']}")

        if iinfo.get("shutdown"):
            lines.append(" shutdown")
        return lines

    def _vlan_config_lines(self, vid: Any, vinfo: Dict[str, Any]) -> List[str]:
        lines: List[str] = [f"vlan {vid}"]
        if vinfo.get("name") and vinfo["name"] != f"VLAN{int(vid):04d}":
            lines.append(f" name {vinfo['name']}")
        if vinfo.get("description"):
            lines.append(f" description {vinfo['description']}")
        return lines

    def _dhcp_pool_config_lines(self, pool_name: str, pinfo: Dict[str, Any]) -> List[str]:
        lines: List[str] = [f"ip dhcp pool {pool_name}"]
        if pinfo.get("network"):
            lines.append(f" network-address {pinfo['network']}")
        if pinfo.get("default_router"):
            lines.append(f" default-router {pinfo['default_router']}")
        for sh in pinfo.get("static_hosts", []):
            if sh.get("mac"):
                lines.append(f" hardware-address {sh['mac']}")
            lines.append(f" host {sh['ip']} {sh['mask']}")
        for code, opt in sorted(pinfo.get("options", {}).items()):
            lines.append(f" option {code} {opt['format']} {opt['value']}")
        return lines

    def _ospf_config_lines(self, pid: Any, pdata: Dict[str, Any]) -> List[str]:
        lines: List[str] = [f"router ospf {pid}"]
        for net in pdata.get("networks", []):
            lines.append(f" network {net['network']} {net['wildcard_or_mask']} area {net['area']}")
        return lines

    def _render_running_config(self, args: Dict[str, Any], is_no: bool) -> str:
        """遍历完整状态树输出神州数码 CS6200 标准配置脚本"""
        st = self.engine.state
        lines: List[str] = [
            "!",
            f"hostname {st['system'].get('hostname', 'Switch')}",
            "!",
        ]

        if st.get("firewall_enabled"):
            lines.append("firewall enable")
        if st.get("anti_arpscan_enabled"):
            lines.append("anti-arpscan enable")

        # 全局 IPv6 使能（CS6200 无 ip routing / ipv6 unicast-routing 命令）
        sys_state = st.get("system", {})
        if sys_state.get("ipv6_enabled"):
            lines.append("ipv6 enable")
        if sys_state.get("mtu"):
            lines.append(f"mtu {sys_state['mtu']}")

        # 远程登录用户/服务/认证方式
        if sys_state.get("telnet_server"):
            lines.append("telnet-server enable")
        for name, uinfo in sorted((sys_state.get("usernames") or {}).items()):
            line = f"username {name} privilege {uinfo.get('privilege', 15)}"
            if uinfo.get("password"):
                line += f" password 0 {uinfo['password']}"
            lines.append(line)
        auth_lines = sys_state.get("authentication_lines", {}) or {}
        for line_type in ("console", "vty", "web"):
            method = auth_lines.get(line_type)
            if method:
                lines.append(f"authentication line {line_type} login {method}")
        if sys_state.get("authentication_enable"):
            lines.append(f"authentication enable {sys_state['authentication_enable']}")
        for line_type, method in sorted((sys_state.get("authorization_lines") or {}).items()):
            lines.append(f"authorization line {line_type} exec {method}")
        for key, info in sorted((sys_state.get("accounting_lines") or {}).items()):
            line_type, level = key.split("-", 1)
            lines.append(
                f"accounting line {line_type} command {level} {info['mode']} {info['method']}"
            )

        # 生成树全局配置
        stp = st.get("spanning_tree", {})
        if stp.get("enabled"):
            lines.append("spanning-tree")
            if stp.get("mode"):
                lines.append(f"spanning-tree mode {stp['mode']}")
            for inst_id, pri in sorted(stp.get("mst_priorities", {}).items()):
                if pri != 32768:
                    lines.append(f"spanning-tree mst {inst_id} priority {pri}")

        # 全局环路检测配置段
        ld_global = st.get("loopback_detection", {})
        if ld_global.get("interval_time") is not None and ld_global.get("no_loopback_time") is not None:
            lines.append(
                f"loopback-detection interval-time {ld_global['interval_time']} {ld_global['no_loopback_time']}"
            )
        if ld_global.get("control_recovery") is not None:
            lines.append(f"loopback-detection control-recovery timeout {ld_global['control_recovery']}")
        lines.append("!")

        # VLAN 配置段
        for vid, vinfo in sorted(st.get("vlans", {}).items()):
            if int(vid) == 1:
                continue
            lines.extend(self._vlan_config_lines(vid, vinfo))
            lines.append("!")

        # DHCP 地址池配置段
        for pool_name, pinfo in st.get("dhcp_pools", {}).items():
            lines.extend(self._dhcp_pool_config_lines(pool_name, pinfo))
            lines.append("!")

        # ACL 配置段
        acls = st.get("acls", {})
        for num, rules in sorted(acls.get("standard_numbered", {}).items()):
            for r in rules:
                lines.append(f"access-list {num} {r}")
        for num, rules in sorted(acls.get("extended_numbered", {}).items()):
            for r in rules:
                lines.append(f"access-list {num} {r}")
        for name, rules in acls.get("standard_named", {}).items():
            lines.append(f"ip access-list standard {name}")
            for r in rules:
                lines.append(f" {r}")
            lines.append("!")
        for name, rules in acls.get("extended_named", {}).items():
            lines.append(f"ip access-list extended {name}")
            for r in rules:
                lines.append(f" {r}")
            lines.append("!")

        # route-map 配置段
        for map_name, rmap in sorted(st.get("route_maps", {}).items()):
            for entry in rmap.get("entries", []):
                lines.append(f"route-map {map_name} {entry.get('action', 'permit')} {entry.get('seq', '10')}")
                for set_cmd in entry.get("sets", []):
                    lines.append(f" {set_cmd}")
                lines.append("!")

        # 接口配置段
        for if_name, iinfo in st.get("interfaces", {}).items():
            lines.extend(self._interface_config_lines(if_name, iinfo))
            lines.append("!")

        # 静态路由与 OSPF 配置段
        for rt in st.get("static_routes", []):
            dist_suffix = f" {rt['distance']}" if rt.get("distance", 1) != 1 else ""
            lines.append(f"ip route {rt['network']} {rt['mask']} {rt['next_hop']}{dist_suffix}")

        for pid, pdata in sorted(st.get("ospf", {}).get("processes", {}).items()):
            lines.extend(self._ospf_config_lines(pid, pdata))
            lines.append("!")

        lines.append("end")
        return "\n".join(lines)

    def _render_startup_config(self, args: Dict[str, Any], is_no: bool) -> str:
        return self._render_running_config(args, is_no)

    def _render_show_vlan(self, args: Dict[str, Any], is_no: bool) -> str:
        header = f"{'VLAN':<6} {'Name':<20} {'Status':<10} {'Ports'}"
        sep = "-" * 68
        rows = [header, sep]

        for vid, vinfo in sorted(self.engine.state.get("vlans", {}).items()):
            vid_int = int(vid)
            members: List[str] = []
            for if_name, iinfo in self.engine.state.get("interfaces", {}).items():
                if if_name.startswith(("Vlan", "Loopback", "Tunnel")):
                    continue
                sw_mode = iinfo.get("switchport_mode", "access")
                if sw_mode == "access" and iinfo.get("access_vlan", 1) == vid_int:
                    members.append(if_name)
                elif sw_mode == "trunk":
                    allowed = iinfo.get("trunk_allowed_vlan_list")
                    if allowed is None or vid_int in allowed:
                        members.append(f"{if_name}(T)")

            status = "active"
            port_str = ", ".join(members) if members else "-"
            rows.append(f"{vid_int:<6} {vinfo.get('name', ''):<20} {status:<10} {port_str}")
        return "\n".join(rows)

    def _render_show_spanning_tree(self, args: Dict[str, Any], is_no: bool) -> str:
        stp = self.engine.state.get("spanning_tree", {})
        if not stp.get("enabled", False):
            return "% Spanning-tree protocol is currently disabled."

        mode = stp.get("mode", "mstp").upper()
        pri = stp.get("mst_priorities", {}).get(0, 32768)
        out = [
            f"Spanning tree enabled protocol {mode}",
            f"  MST00  Root ID    Priority    {pri}",
            "                    Address     0003.0f12.3400",
            "                    This bridge is the root",
            f"         Bridge ID  Priority    {pri}  (priority {pri} sys-id-ext 0)",
            "                    Address     0003.0f12.3400",
            "",
            f"{'Interface':<22} {'Role':<8} {'Sts':<6} {'Cost':<10} {'Prio.Nbr':<10} {'Type'}",
            "-" * 68,
        ]
        for idx, (if_name, iinfo) in enumerate(self.engine.state.get("interfaces", {}).items(), start=1):
            if if_name.startswith(("Vlan", "Loopback", "Tunnel")) or iinfo.get("shutdown"):
                continue
            out.append(f"{if_name:<22} {'Desg':<8} {'FWD':<6} {'20000':<10} {f'128.{idx}':<10} {'P2p'}")
        return "\n".join(out)

    def _render_show_port_group(self, args: Dict[str, Any], is_no: bool) -> str:
        pgs = self.engine.state.get("port_groups", {})
        if not pgs:
            return "% No port-group configured."

        header = f"{'Group':<8} {'Port-Channel':<18} {'Protocol':<10} {'Mode':<10} {'Member Ports'}"
        rows = [header, "-" * 74]
        for pg_id, pg in sorted(pgs.items()):
            pc_name = f"Port-Channel{pg_id}"
            members = ", ".join(pg.get("members", [])) or "None"
            rows.append(
                f"{pg_id:<8} {pc_name:<18} {pg.get('protocol', 'LACP'):<10} {pg.get('mode', 'active'):<10} {members}"
            )
        return "\n".join(rows)

    def _render_show_ip_route(self, args: Dict[str, Any], is_no: bool) -> str:
        lines = [
            "Codes: C - connected, S - static, O - OSPF, B - BGP, * - candidate default",
            "",
            f"{'Code':<6} {'Destination/Mask':<22} {'AD/Metric':<12} {'Next-Hop':<18} {'Interface'}",
            "-" * 74,
        ]

        # 1) 从活跃三层接口提取直连 (C) 路由
        for if_name, iinfo in self.engine.state.get("interfaces", {}).items():
            if iinfo.get("ip_address") and not iinfo.get("shutdown"):
                try:
                    iface = ipaddress.IPv4Interface(iinfo["ip_address"])
                    cidr = str(iface.network)
                    lines.append(f"{'C':<6} {cidr:<22} {'[0/0]':<12} {'directly':<18} {if_name}")
                except ValueError:
                    pass

        # 2) 静态 (S) 路由
        for rt in self.engine.state.get("static_routes", []):
            cidr = f"{rt['network']}/{rt['prefix_len']}"
            ad_metric = f"[{rt.get('distance', 1)}/0]"
            nh = rt["next_hop"]
            out_if = "Null0" if nh == "Null0" else "via-gateway"
            lines.append(f"{'S':<6} {cidr:<22} {ad_metric:<12} {nh:<18} {out_if}")

        # 3) OSPF (O) 路由
        for pid, pdata in sorted(self.engine.state.get("ospf", {}).get("processes", {}).items()):
            for nb in pdata.get("neighbors", []):
                # 展示远端邻居 Loopback 模拟路由
                rid = nb["neighbor_id"]
                lines.append(f"{'O':<6} {f'{rid}/32':<22} {'[110/2]':<12} {nb['address']:<18} {nb['interface']}")

        return "\n".join(lines)

    def _resolve_show_interface_names(self, args: Dict[str, Any]) -> List[str]:
        """从 show interface / show ipv6 interface 参数中解析目标接口名列表"""
        if "<loopback-id>" in args:
            return [f"Loopback{args['<loopback-id>']}"]
        if "<vlan-id>" in args:
            return [f"Vlan{args['<vlan-id>']}"]
        if "<port-channel-number>" in args:
            return [f"Port-Channel{args['<port-channel-number>']}"]
        if "<interface-list>" in args:
            raw = str(args["<interface-list>"])
            try:
                return self.engine._expand_ethernet_list(raw)
            except Exception:
                return [self.engine.normalize_interface_name("ethernet", raw)]
        # 未指定接口时遍历全部接口（按名称排序，保证输出稳定）
        return sorted(self.engine.state.get("interfaces", {}).keys())

    @staticmethod
    def _interface_mtu(iinfo: Dict[str, Any]) -> int:
        try:
            return int(iinfo.get("mtu") or 1500)
        except (TypeError, ValueError):
            return 1500

    @staticmethod
    def _interface_bandwidth(if_name: str, iinfo: Dict[str, Any]) -> int:
        """返回接口带宽（kbit/s）；未显式配置时按接口类型给出合理默认值"""
        if iinfo.get("bandwidth"):
            try:
                return int(iinfo["bandwidth"])
            except (TypeError, ValueError):
                pass
        return 8000000 if if_name.startswith("Loopback") else 1000000

    def _render_interface_detail(self, if_name: str) -> str:
        """渲染单个接口的 show interface 详细信息块"""
        iinfo = self.engine.state.get("interfaces", {}).get(if_name)
        if not iinfo:
            return f"% Interface {if_name} does not exist."

        status = "down" if iinfo.get("shutdown") else "up"
        vrf = iinfo.get("vrf") or "default"
        mtu = self._interface_mtu(iinfo)
        bandwidth = self._interface_bandwidth(if_name, iinfo)

        lines: List[str] = [f"{if_name} is {status}, line protocol is {status}"]
        if iinfo.get("description"):
            lines.append(f"  Description: {iinfo['description']}")
        lines.append(f"  MTU {mtu} bytes, BW {bandwidth} Kbit, DLY 5000 usec")
        lines.append(f"  Internet address is {iinfo.get('ip_address') or 'unassigned'}")

        if iinfo.get("ipv6_enable") or iinfo.get("ipv6_addresses"):
            lines.append("  IPv6 is enabled")
            for addr in iinfo.get("ipv6_addresses", []):
                lines.append(f"    IPv6 address: {addr}")

        if iinfo.get("port_loopback"):
            lines.append("  Loopback test: enabled")

        lines.append(f"  VPN Routing/Forwarding \"{vrf}\"")
        return "\n".join(lines)

    def _render_show_interface(self, args: Dict[str, Any], is_no: bool) -> str:
        names = self._resolve_show_interface_names(args)
        if not names:
            return "% Interface does not exist."
        blocks = [self._render_interface_detail(name) for name in names]
        return "\n\n".join(blocks)

    def _render_show_ipv6_interface(self, args: Dict[str, Any], is_no: bool) -> str:
        names = self._resolve_show_interface_names(args)
        if not names:
            return "% Interface does not exist."

        blocks: List[str] = []
        for if_name in names:
            iinfo = self.engine.state.get("interfaces", {}).get(if_name)
            if not iinfo:
                blocks.append(f"% Interface {if_name} does not exist.")
                continue
            status = "down" if iinfo.get("shutdown") else "up"
            lines = [f"{if_name} is {status}, line protocol is {status}"]
            addrs = iinfo.get("ipv6_addresses") or []
            if not iinfo.get("ipv6_enable") and not addrs:
                lines.append("  IPv6 is disabled")
            else:
                lines.append("  IPv6 is enabled")
                if addrs:
                    lines.append("  Global unicast address(es):")
                    for addr in addrs:
                        lines.append(f"    {addr}")
                else:
                    lines.append("  No IPv6 address configured")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def _render_show_loopback_detection(self, args: Dict[str, Any], is_no: bool) -> str:
        """显示端口环路检测的全局参数及各端口检测状态"""
        ld_global = self.engine.state.get("loopback_detection", {})
        interval = ld_global.get("interval_time")
        no_loop = ld_global.get("no_loopback_time")
        recovery = ld_global.get("control_recovery")

        lines: List[str] = [
            f"Loopback detection interval time      : {interval if interval is not None else '-'}",
            f"Loopback detection no-loopback time   : {no_loop if no_loop is not None else '-'}",
            f"Loopback detection control recovery   : {recovery if recovery is not None else '-'}",
            "",
        ]

        if "<interface-list>" in args:
            raw = str(args["<interface-list>"])
            try:
                names = self.engine._expand_ethernet_list(raw)
            except Exception:
                names = [self.engine.normalize_interface_name("ethernet", raw)]
        else:
            names = [
                name for name in sorted(self.engine.state.get("interfaces", {}))
                if not name.startswith(("Vlan", "Loopback", "Tunnel"))
            ]

        rows: List[str] = [
            f"{'Interface':<22} {'Detection':<12} {'Control':<12} {'Specified VLAN':<22} {'Status'}",
            "-" * 80,
        ]
        for if_name in names:
            iinfo = self.engine.state.get("interfaces", {}).get(if_name)
            if iinfo is None:
                rows.append(f"{if_name:<22} {'Not Found':<12} {'-':<12} {'-':<22} -")
                continue
            ld = iinfo.get("loopback_detection") or {}
            enabled = bool(ld.get("enabled") or ld.get("vlans"))
            detection = "Enabled" if enabled else "Disabled"
            control = ld.get("control") or "-"
            vlans = self._format_vlan_set(set(ld.get("vlans") or [])) if ld.get("vlans") else "-"
            status = ld.get("status") or "Normal"
            rows.append(f"{if_name:<22} {detection:<12} {control:<12} {vlans:<22} {status}")

        lines.extend(rows)
        return "\n".join(lines)

    def _render_show_current_mode(self, args: Dict[str, Any], is_no: bool) -> str:
        """`show running-config current-mode`：显示当前视图的配置"""
        frame = self.engine.current_frame
        mode = self.engine.current_mode
        st = self.engine.state
        blocks: List[str] = []

        if mode == "if":
            targets = frame.meta.get("targets") or [frame.context_name]
            for if_name in targets:
                iinfo = st.get("interfaces", {}).get(if_name)
                if iinfo:
                    blocks.append("\n".join(self._interface_config_lines(if_name, iinfo)))
        elif mode == "vlan":
            vid = frame.context_name
            if vid.isdigit() and int(vid) in st.get("vlans", {}):
                blocks.append("\n".join(self._vlan_config_lines(int(vid), st["vlans"][int(vid)])))
        elif mode == "router-ospf":
            pid = frame.context_name.split("-", 1)[-1]
            pdata = st.get("ospf", {}).get("processes", {}).get(pid)
            if pdata is not None:
                blocks.append("\n".join(self._ospf_config_lines(pid, pdata)))
        elif mode == "router-rip":
            blocks.append("router rip")
        elif mode == "router-bgp":
            asn = frame.context_name.split("-", 1)[-1]
            blocks.append(f"router bgp {asn}")
        elif mode == "router-msdp":
            blocks.append("router msdp")
        elif mode == "router-msdp-peer":
            blocks.append(f"peer {frame.context_name}")
        elif mode == "route-map":
            map_name = frame.context_name
            rmap = st.get("route_maps", {}).get(map_name)
            if rmap:
                for entry in rmap.get("entries", []):
                    entry_lines = [
                        f"route-map {map_name} {entry.get('action', 'permit')} {entry.get('seq', '10')}"
                    ]
                    entry_lines.extend(f" {cmd}" for cmd in entry.get("sets", []))
                    blocks.append("\n".join(entry_lines))
        elif mode == "dhcp-pool":
            pool_name = frame.context_name
            pinfo = st.get("dhcp_pools", {}).get(pool_name)
            if pinfo is not None:
                blocks.append("\n".join(self._dhcp_pool_config_lines(pool_name, pinfo)))
        else:
            return self._render_running_config(args, is_no)

        if not blocks:
            return "% No configuration in current mode."
        return "\n!\n".join(blocks) + "\n!"

    def _render_show_version(self, args: Dict[str, Any], is_no: bool) -> str:
        return (
            "Digital China Networks CS6200-28X-EI\n"
            "Copyright (c) 2000-2026 Digital China Networks Ltd.\n"
            "DCNOS Software, Version 1.0.0.0\n"
            f"System uptime is 0 days, 0 hours, 0 minutes"
        )

    def _render_show_privilege(self, args: Dict[str, Any], is_no: bool) -> str:
        level = self.engine.state.get("system", {}).get("enable_level", 15)
        return f"Current privilege level is {level}"

    def _render_show_users(self, args: Dict[str, Any], is_no: bool) -> str:
        rows = [
            f"{'Line':<8} {'User':<16} {'Location'}",
            "-" * 44,
            f"{'0':<8} {'-':<16} {'Console'}",
        ]
        for name in sorted((self.engine.state.get("system", {}).get("usernames") or {})):
            rows.append(f"{'vty':<8} {name:<16} {'-':<22}")
        return "\n".join(rows)

    def _render_show_slot(self, args: Dict[str, Any], is_no: bool) -> str:
        slot = args.get("<slot-id>", "1")
        member = args.get("<member-id>", "1")
        return (
            f"{'Member':<8} {'Slot':<6} {'Type':<20} {'Status'}\n"
            + "-" * 46 + "\n"
            + f"{member:<8} {slot:<6} {'CS6200-28X-EI':<20} up"
        )

    def _render_show_lldp(self, args: Dict[str, Any], is_no: bool) -> str:
        return (
            "LLDP is enabled\n"
            f"{'Interface':<22} {'Tx-Interval':<12} {'Status'}\n"
            + "-" * 46 + "\n"
            + f"{'All ports':<22} {'30':<12} {'Enabled'}"
        )

    def _render_show_transceiver(self, args: Dict[str, Any], is_no: bool) -> str:
        if "<interface-list>" in args:
            names = self.engine._expand_ethernet_list(str(args["<interface-list>"]))
        else:
            names = [n for n in sorted(self.engine.state.get("interfaces", {}))
                     if n.startswith("Ethernet")]
        rows = [
            f"{'Interface':<16} {'Temp(C)':<10} {'Voltage(V)':<12} {'RX Power(dBm)':<16} {'TX Power(dBm)'}",
            "-" * 76,
        ]
        for name in names:
            rows.append(f"{name:<16} {'N/A':<10} {'N/A':<12} {'N/A':<16} {'N/A'}")
        return "\n".join(rows)

    def _render_show_port_security(self, args: Dict[str, Any], is_no: bool) -> str:
        header = f"{'Secure Port':<20} {'Enabled':<10} {'MaxAddr':<9} {'CurrentAddr':<13} {'SecurityViolation'}"
        rows = [header, "-" * 72]
        for if_name, iinfo in self.engine.state.get("interfaces", {}).items():
            if if_name.startswith(("Vlan", "Loopback", "Tunnel")):
                continue
            psec = iinfo.get("port_security", {})
            enabled = "Enabled" if psec.get("enabled") else "Disabled"
            max_a = psec.get("maximum", 1)
            cur_a = len(psec.get("mac_addresses", []))
            viol = psec.get("violation", "shutdown")
            rows.append(f"{if_name:<20} {enabled:<10} {max_a:<9} {cur_a:<13} {viol}")
        return "\n".join(rows)

    def _render_show_ospf_neighbor(self, args: Dict[str, Any], is_no: bool) -> str:
        header = f"{'Neighbor ID':<16} {'Pri':<5} {'State':<12} {'Dead Time':<12} {'Address':<16} {'Interface'}"
        rows = [header, "-" * 78]
        found = False
        for pid, pdata in sorted(self.engine.state.get("ospf", {}).get("processes", {}).items()):
            for nb in pdata.get("neighbors", []):
                found = True
                rows.append(
                    f"{nb['neighbor_id']:<16} {nb['priority']:<5} {nb['state']:<12} "
                    f"{nb['dead_time']:<12} {nb['address']:<16} {nb['interface']}"
                )
        if not found:
            return "% OSPF neighbor table is empty."
        return "\n".join(rows)

    def _render_show_mac_table(self, args: Dict[str, Any], is_no: bool) -> str:
        header = f"{'Vlan':<8} {'Mac Address':<20} {'Type':<12} {'Ports'}"
        rows = [header, "-" * 58]
        # 收集所有接口配置的静态安全 MAC
        for if_name, iinfo in self.engine.state.get("interfaces", {}).items():
            for m in iinfo.get("port_security", {}).get("mac_addresses", []):
                rows.append(f"{m['vlan']:<8} {m['mac']:<20} {'STATIC':<12} {if_name}")
        # 默认示例动态表项
        rows.append(f"{1:<8} {'0003.0f12.3401':<20} {'DYNAMIC':<12} {'Ethernet1/0/1'}")
        return "\n".join(rows)

    def _render_show_dhcp_binding(self, args: Dict[str, Any], is_no: bool) -> str:
        bindings = self.engine.state.get("dhcp_bindings", [])
        if not bindings:
            return "% No DHCP bindings found."
        header = f"{'IP Address':<18} {'Client-ID / MAC':<22} {'Lease Expiration':<20} {'Type'}"
        rows = [header, "-" * 72]
        for b in bindings:
            rows.append(f"{b['ip']:<18} {b['mac']:<22} {b['lease']:<20} {b['type']}")
        return "\n".join(rows)
