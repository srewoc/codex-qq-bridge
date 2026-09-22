# Codex QQ Bridge

通过腾讯官方 QQ 机器人私聊，远程控制一个已明确绑定的 Codex CLI 或 Claude Code 终端会话。

[English](README.md) · [架构](docs/architecture.md) · [故障排查](docs/troubleshooting.md)

> 非官方社区项目，与 OpenAI、Anthropic、腾讯和 Kitty 均无隶属关系。

## 功能介绍

- **从 QQ 发送任务**：普通文字和 Agent 原生 slash 命令会被输入当前选中的 Codex CLI 或 Claude Code 终端。
- **把 Agent 反馈带回手机**：自动转发助手回复、追问、权限确认、计划完成、任务中断和上下文压缩通知。
- **发送图片给 Agent 分析**：支持纯图片和图文消息；Codex 通过 Wayland 剪贴板接收，Claude Code 使用本地图片路径。
- **查看并操作终端界面**：可获取当前终端截图或纯文字屏幕，发送受限的方向键、确认键等按键，并停止当前任务。
- **管理多个会话**：查看已连接会话、切换手机当前控制目标、断开一个或全部会话，也能在指定目录新建 Codex/Claude Code 窗口。
- **可靠恢复与隔离**：Bridge 重启后保留显式绑定；校验 Hook 的会话、transcript、Kitty Socket 和窗口身份，拒绝串线事件并去重重复通知。
- **仅主人可用**：只接受 Onboard 时记录的主人 OpenID；机器人凭据和运行状态只保存在本机。
- **可预览、可回滚**：安装前可用 `--dry-run` 查看改动，配置修改自动生成时间戳备份，卸载时只移除本项目管理的条目。

典型场景包括：离开电脑后查看长时间编码任务、用手机回答 Agent 追问、通过受控按键操作终端确认菜单，以及把手机截图或照片发给当前会话分析。

## 五分钟安装

要求：Linux、Kitty、Python 3.11+、uv，以及 Codex CLI 和/或 Claude Code。`wl-copy` 仅在向 Codex 粘贴图片时需要。

```bash
uv tool install git+https://github.com/srewoc/codex-qq-bridge.git
codex-qq --version
codex-qq install-kitty --dry-run
codex-qq install-kitty
codex-qq install-hooks
# 使用 Claude Code 时再执行：codex-qq install-claude-hooks
codex-qq onboard
codex-qq bridge
```

完整退出所有 Kitty 进程后重新打开，并重启 Codex/Claude Code。Codex 用户还要执行 `/hooks`，审核并信任新增 Hook。随后在准备由手机控制的会话里发送 `/qq-connect`。

## 后台服务与诊断

```bash
codex-qq install-service --dry-run
codex-qq install-service --start
codex-qq doctor
codex-qq doctor --json
systemctl --user status codex-qq-bridge.service
```

QQ 内发送 `/qq-help` 可查看当前命令、参数和示例。

## 卸载

```bash
codex-qq uninstall-service --stop
codex-qq uninstall-hooks
codex-qq uninstall-claude-hooks
codex-qq uninstall-kitty
uv tool uninstall codex-qq-bridge
```

配置修改会生成带时间戳的备份，卸载只清理由本项目管理的内容。

## 安全边界与限制

- 本项目可以向绑定终端输入内容并读取可见屏幕，应把 QQ 机器人和本机账户视为高权限入口。
- 凭据以 `0600` 权限保存在本机；不要提交凭据、`.env`、截图、会话记录或运行状态。
- Hook 是否已被 Codex 信任无法自动确认，`doctor` 会显示 `unverified`。
- 仅支持 Linux + Kitty、单 QQ 主人、显式单会话绑定；不支持 App Server、浏览器 UI、Windows 或 macOS。

详见 [安全策略](SECURITY.md) 和 [故障排查](docs/troubleshooting.md)。

## 开发

```bash
git clone https://github.com/srewoc/codex-qq-bridge.git
cd codex-qq-bridge
uv sync --dev
uv run ruff check .
uv run pytest
uv build
```
