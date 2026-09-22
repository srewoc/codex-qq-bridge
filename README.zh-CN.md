# Codex QQ Bridge

通过腾讯官方 QQ 机器人私聊，远程控制一个已明确绑定的 Codex CLI 或 Claude Code 终端会话。

[English](README.md) · [架构](docs/architecture.md) · [故障排查](docs/troubleshooting.md)

> 非官方社区项目，与 OpenAI、Anthropic、腾讯和 Kitty 均无隶属关系。

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
