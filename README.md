# SkillLocker

本地 Agent Skills 管理器（Windows 桌面端）。扫描中央仓库里的 `SKILL.md` 建立技能库，再通过 junction（失败时回退为复制）分发到 Cursor、Claude Code、Codex、DeepSeek Harness 等 agent 的全局 skills 目录，并用一张「技能 × 工具」矩阵统一查看、修复和清理分发状态。

## 界面预览

<table>
  <tr>
    <td width="50%"><img src="docs/screenshots/01-library.png" alt="技能库与分发覆盖"><br><sub>共有技能库 · 分类 / 备注 / 分发覆盖</sub></td>
    <td width="50%"><img src="docs/screenshots/04-agents.png" alt="智能体与分发开关"><br><sub>智能体列表 · junction 分发开关</sub></td>
  </tr>
  <tr>
    <td width="50%"><img src="docs/screenshots/02-unique.png" alt="独有技能看板"><br><sub>各 agent 独有技能 · 来源与本地副本</sub></td>
    <td width="50%"><img src="docs/screenshots/03-import.png" alt="导入 Skill"><br><sub>导入 Skill · 本地文件夹 / 在线检索</sub></td>
  </tr>
</table>

## 功能

- 扫描中央仓库：登记技能名称、分类、备注与内容哈希
- 分发矩阵：一键建立 / 修复各工具的 skill 链接，识别缺失、漂移与冲突
- 导入外部 skill、撤链、删除，以及把「独有」技能转为「共有」
- 内容哈希冲突检测，`verify` 只报告不改动
- 无边框桌面界面（pywebview）与完整 CLI
- Windows 下所有删除先判断 reparse point，不顺着链接递归删实体

## 环境

- Windows 10 / 11
- Python 3.12
- 桌面端需要 `pywebview` 与 WebView2 Runtime；后端与 CLI 无第三方依赖

## 运行

双击仓库根目录的 `启动 skilllocker.bat`，或用命令行：

```bat
cd kit
python desktop.py           :: 带控制台，便于看日志
python desktop.py --debug   :: 打开 WebView2 调试面板
```

启动脚本优先使用 `D:\develop\python\3.12\pythonw.exe`；找不到时回退到 PATH 中第一个非 WindowsApps 的 `pythonw.exe`。

## CLI

```bat
cd kit
python skilllocker.py scan                     :: 重新扫描中央仓库
python skilllocker.py status                   :: 打印分发矩阵摘要
python skilllocker.py verify                   :: 检查漂移（只报告）
python skilllocker.py sync [--tool 工具] [--fix] :: 建立 / 修复分发
python skilllocker.py import <目录> [--name 名称] :: 从目录导入
python skilllocker.py remove <名称>             :: 删除分发与实体
python skilllocker.py search <关键词>           :: skills.sh 在线检索
python skilllocker.py fetch <owner/repo/skill> :: 在线获取到临时目录
```

## 目录

```text
kit/                 后端模块、桌面壳、自检与测试
  adapters.json      各 agent 的 skills 目录适配表
  assets/            图标等资源
ui/                  前端页面（原生 HTML / CSS / JS，无构建步骤）
启动 skilllocker.bat  桌面端启动入口
```

## 测试

```bat
python kit/test_unit.py
python kit/test_first_run.py
python kit/test_boot_proto.py   :: 需要 playwright 与本机 Edge / Chromium
```
