from __future__ import annotations

from typing import Any, Protocol
from urllib.parse import quote

import httpx
from qqbot_agent_sdk import QQApiClient

from .config import Config

PANEL_REMARK = "codex-qq-bridge:c2c-owner"
PANEL_ITEMS: tuple[dict[str, object], ...] = (
    {"type": "command", "name": "/qq-help", "desc": "查看全部手机命令"},
    {"type": "command", "name": "/qq-activate", "desc": "检查机器人和连接状态"},
    {"type": "command", "name": "/qq-list", "desc": "查看已连接的会话"},
    {"type": "command", "name": "/qq-use", "desc": "选择会话，后接序号"},
    {"type": "command", "name": "/qq-new", "desc": "新建会话，需填命令如 codex/claude"},
    {"type": "command", "name": "/qq-screen", "desc": "查看当前屏幕图片"},
    {"type": "command", "name": "/qq-key", "desc": "发送按键，后接按键名"},
    {"type": "command", "name": "/qq-stop", "desc": "停止当前任务"},
    {"type": "command", "name": "/qq-disconnect", "desc": "断开当前会话"},
)


class PanelApi(Protocol):
    async def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


def panel_body() -> dict[str, object]:
    return {
        "items": [dict(item) for item in PANEL_ITEMS],
        "remark": PANEL_REMARK,
    }


async def sync_panel_with_api(api: PanelApi, owner_openid: str) -> dict[str, object]:
    listed = await api.request("GET", "/v2/panels?scope=c2c&limit=50")
    records = listed.get("records", [])
    if not isinstance(records, list):
        raise TypeError("QQ 指令面板列表响应格式错误")
    managed = [
        record
        for record in records
        if isinstance(record, dict)
        and isinstance(record.get("panel"), dict)
        and record["panel"].get("remark") == PANEL_REMARK
    ]
    if len(managed) > 1:
        raise RuntimeError("发现多个 Codex QQ 受管指令面板，请先在开放平台清理重复项")

    if not managed:
        result = await api.request(
            "POST",
            "/v2/panels",
            {
                "scope": "c2c",
                "target_type": "specific",
                "user_openids": [owner_openid],
                "panel": panel_body(),
            },
        )
        return {
            "action": "created",
            "panel_id": result.get("panel_id"),
            "version": result.get("version"),
        }

    record = managed[0]
    if record.get("scope") != "c2c" or record.get("target_type") != "specific":
        raise RuntimeError("现有 Codex QQ 受管面板的作用范围不安全，拒绝覆盖")
    panel_id = record.get("panel_id")
    if not isinstance(panel_id, str) or not panel_id:
        raise RuntimeError("现有 Codex QQ 受管面板缺少 panel_id")
    result = await api.request(
        "PUT",
        f"/v2/panels/{quote(panel_id, safe='')}",
        {"panel": panel_body()},
    )
    return {
        "action": "updated",
        "panel_id": panel_id,
        "version": result.get("version"),
    }


async def sync_command_panel(config: Config) -> dict[str, object]:
    async with httpx.AsyncClient(timeout=30.0, trust_env=True) as http:
        api = QQApiClient(
            config.app_id,
            config.app_secret,
            log_tag="CodexQQPanel",
        )
        api.setup(http)
        return await sync_panel_with_api(api, config.owner_openid)
