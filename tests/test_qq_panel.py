from __future__ import annotations

from typing import Any

import pytest

from codex_qq_bridge.qq_panel import PANEL_ITEMS, PANEL_REMARK, sync_panel_with_api


class FakeApi:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []

    async def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.requests.append((method, path, body))
        return self.responses.pop(0)


def test_panel_items_fit_official_limits_and_keep_qq_prefix() -> None:
    assert len(PANEL_ITEMS) <= 20
    for item in PANEL_ITEMS:
        assert str(item["name"]).startswith("/qq-")
        assert len(str(item["name"])) <= 14
        assert len(str(item["desc"])) <= 30


async def test_sync_panel_creates_owner_specific_c2c_panel() -> None:
    api = FakeApi([{"records": [], "is_end": True}, {"panel_id": "panel-1", "version": 1}])

    result = await sync_panel_with_api(api, "owner")

    assert result == {"action": "created", "panel_id": "panel-1", "version": 1}
    assert api.requests[1] == (
        "POST",
        "/v2/panels",
        {
            "scope": "c2c",
            "target_type": "specific",
            "user_openids": ["owner"],
            "panel": {"items": list(PANEL_ITEMS), "remark": PANEL_REMARK},
        },
    )


async def test_sync_panel_updates_existing_managed_panel() -> None:
    api = FakeApi(
        [
            {
                "records": [
                    {
                        "panel_id": "panel/1",
                        "scope": "c2c",
                        "target_type": "specific",
                        "panel": {"remark": PANEL_REMARK},
                    }
                ]
            },
            {"version": 2},
        ]
    )

    result = await sync_panel_with_api(api, "owner")

    assert result == {"action": "updated", "panel_id": "panel/1", "version": 2}
    assert api.requests[1][0:2] == ("PUT", "/v2/panels/panel%2F1")
    assert api.requests[1][2] == {
        "panel": {"items": list(PANEL_ITEMS), "remark": PANEL_REMARK}
    }


async def test_sync_panel_rejects_duplicate_managed_panels() -> None:
    record = {
        "scope": "c2c",
        "target_type": "specific",
        "panel": {"remark": PANEL_REMARK},
    }
    api = FakeApi([{"records": [record, record]}])

    with pytest.raises(RuntimeError, match="多个"):
        await sync_panel_with_api(api, "owner")
