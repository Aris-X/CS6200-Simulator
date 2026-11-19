# CS6200 交换机 CLI 模拟器

基于《神州数码 CS6200 系列交换机操作手册》实现的可交互命令行模拟器，用于网络配置练习、命令验证与自动化测试。模拟器只仿真 **CLI 视图、配置状态树与 show 回显**，不进行真实数据面转发。

> 非官方项目，仅供学习与实验使用。

## 功能特性

- **完整视图栈**：用户模式 / 特权模式 / 全局配置 / 接口 / VLAN / 路由协议 / DHCP 地址池 / ACL / time-range / VRF / route-map / MSDP Peer 等
- **228 条命令、227 条唯一语法**，来源于 9 份规则文件
- **交互增强**：
  - `Tab` 关键字补全（唯一匹配自动补全，多匹配分栏展示）
  - `?` 即时上下文帮助（无需回车）
  - `Ctrl+Z` 直接退回特权模式 `Switch#`
- **配置持久化**：`write` 保存到 `startup-config.json`，下次启动自动恢复
- **状态导出**：`export-json [file]` 导出完整状态树，便于调试与自动化断言
- **二层业务**：VLAN、Access / Trunk / Hybrid、LACP、STP/MSTP、端口安全、端口环路检测、LLDP、BPDU Tunnel、EFM OAM
- **三层业务**：SVI / Loopback / Tunnel、IPv4 / IPv6、静态路由与黑洞路由、DHCP、OSPF / RIP / BGP / MSDP、VRF、ACL、route-map、prefix-list、time-range
- **Loopback 完整配置**：`ip address`、`ipv6 address`、`description`、`bandwidth`、`mtu`、`vrf forwarding`、`shutdown` 等
- **丰富的 show 输出**：`show running-config`、`show interface`、`show ip interface`、`show ip route`、`show vlan`、`show spanning-tree`、`show port-security`、`show loopback-detection` 等
- **真机对齐**：视图权限、`no` 形式、参数校验、`show running-config current-mode`、`% Invalid input detected at '^' marker.` 错误定位

## 环境要求

- Python **3.10+**
- [prompt_toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit) 3.x

## 安装与运行

```bash
# 1. 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2. 安装依赖
pip install prompt_toolkit

# 3. 启动模拟器
python cli.py
```

启动后会看到类似输出：

```text
==============================================================================
  Digital China Networks (神州数码) CS6200-28X-EI Switch CLI Simulator
  Loaded 9 JSON rule modules | Interactive TTY Mode Ready
------------------------------------------------------------------------------
  <Tab>                  Auto-complete command keyword or list candidates
  <?>                    Instant context-sensitive help (no Enter needed)
  <Ctrl+Z>               Exit current config view directly to Switch#
  write / write memory   Save running-config to startup-config.json
  export-json [file]     Export engine state tree to JSON
  quit / exit-cli        Terminate local CLI simulator session
==============================================================================
```

## 快速体验

```text
Switch> enable
Switch# config terminal
Switch(config)# hostname CS6200-Core
CS6200-Core(config)# vlan 10
CS6200-Core(Config-Vlan10)# name OFFICE
CS6200-Core(Config-Vlan10)# exit

CS6200-Core(config)# interface loopback 1
CS6200-Core(Config-If-Loopback1)# description Router-ID
CS6200-Core(Config-If-Loopback1)# ip address 10.255.255.1 255.255.255.255
CS6200-Core(Config-If-Loopback1)# mtu 3924
CS6200-Core(Config-If-Loopback1)# exit

CS6200-Core(config)# interface vlan 10
CS6200-Core(Config-If-Vlan10)# ip address 192.168.10.1 255.255.255.0
CS6200-Core(Config-If-Vlan10)# no shutdown
CS6200-Core(Config-If-Vlan10)# exit

CS6200-Core(config)# end
CS6200-Core# write memory
Building configuration...
[OK]

CS6200-Core# show ip route
CS6200-Core# show running-config current-mode
```

## 快捷键与本地命令

| 输入 | 作用 |
|---|---|
| `Tab` | 补全命令关键字或列出候选 |
| `?` | 显示当前上下文帮助 |
| `Ctrl+Z` | 立即退回 `Switch#` |
| `write` / `write memory` | 保存运行配置到 `startup-config.json` |
| `export-json [文件名]` | 导出状态树 JSON（默认 `cs6200_state.json`） |
| `quit` / `exit-cli` | 退出模拟器 |

## 配置持久化

- `write` 会将当前运行配置写入 `startup-config.json`
- 下次运行 `python cli.py` 时自动加载并恢复
- 想恢复出厂状态，删除 `startup-config.json` 后重新启动即可
- `startup-config.json` 中不保存 `port_loopback` 等运行时诊断状态

## 项目结构

| 文件 | 说明 |
|---|---|
| `cli.py` | 交互式终端入口，负责规则加载、Tab/`?`/`Ctrl+Z` 与持久化恢复 |
| `engine.py` | CLI 有限状态机：词法解析、视图栈、命令匹配、歧义检测、错误定位 |
| `handlers.py` | 命令语义处理：状态树更新、前置条件校验、show 表格渲染 |
| `core_01_base.json` | 基础系统命令（enable/config/hostname/认证/接口入口等） |
| `core_02_l2.json` | 二层命令（VLAN、switchport、LACP、STP、环路检测等） |
| `core_03_l3.json` | 三层命令（IP 地址、路由、DHCP、show ip interface 等） |
| `core_04_security.json` | 安全命令（ACL、端口安全、time-range、防火墙） |
| `01_system.json` | 手册第 1 章系统管理命令摘录 |
| `02_system.json` | 手册第 2 章二层扩展命令摘录（LLDP、MTU、OAM 等） |
| `03_system.json` | 接口/VLAN/VRF/Tunnel/DHCP 等第 3 章命令 |
| `04_system.json` | 路由策略、OSPF、RIP、BGP、route-map、prefix-list |
| `05_system.json` | 组播、PIM、IGMP/MLD Snooping、MSDP |
| `all_rules.txt` | 9 份规则文件与 `startup-config.json` 的汇总快照 |
| `startup-config.json` | 模拟器启动时加载的持久化配置 |
| `test_*.py` | 验收与回归测试脚本 |

## 规则文件格式

每条规则采用统一扁平结构：

```json
{
  "mode": ["if"],
  "syntax": "description <text>",
  "params": { "<text>": "接口描述信息" },
  "desc": "配置接口描述信息",
  "requires": "",
  "state_key": "interface.description",
  "no_cmd": "no description",
  "output_example": ""
}
```

- `mode`：命令所属视图，支持 `user` / `priv` / `global` / `if` / `vlan` / `dhcp-pool` / `router-ospf` / `route-map` 等
- `syntax`：正反向语法模板，支持 `<param>`、`{a | b}`、`[optional]`、`<prefix>/<length>` 复合参数
- `no_cmd`：`no` 形式；留空表示该命令没有 `no` 形式
- `state_key`：状态树键名，由 `handlers.py` 负责落地

## 运行测试

```bash
for f in test_rules_alignment.py test_loopback.py test_simulator.py \
         test_view_navigation.py test_persistence.py test_001sw.py; do
    echo "== $f"
    python "$f" || break
done
```

| 测试脚本 | 覆盖内容 |
|---|---|
| `test_rules_alignment.py` | 规则库结构、模式、重复、禁用命令、视图隔离、current-mode |
| `test_loopback.py` | Loopback 创建/删除、IPv4/IPv6、description/bandwidth/mtu、show 与持久化 |
| `test_simulator.py` | VLAN、端口安全、LACP、SVI、DHCP、静态路由、OSPF、ACL、环路检测 |
| `test_view_navigation.py` | 视图跳转、子视图直达全局命令、show 不跳视图 |
| `test_persistence.py` | `write` 保存、重启恢复、损坏配置容错 |
| `test_001sw.py` | 完整园区网配置流程与 001SW 场景兼容性 |

## 已知限制

- 仅模拟 CLI 与配置状态，不模拟真实报文转发、ARP 学习、STP 收敛等数据面行为
- `show` 输出为结构化模拟结果，不保证与所有固件版本逐字节一致
- 个别协议（RIP/BGP/MSDP）以视图与命令可执行性为主，协议状态机为简化实现
