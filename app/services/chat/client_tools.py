from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

TERMINAL_OFFLINE_RESULT = {
    "error": "\u5979\u7684\u7535\u8111\u7ec8\u7aef\u5f53\u524d\u4e0d\u5728\u7ebf\uff0c\u65e0\u6cd5\u6267\u884c\u64cd\u4f5c\u3002",
}


def write_tool_use(service: Any, request_id: str, round_index: int, tool_call: Any) -> None:
    service._write_cot_block(
        request_id,
        round_index,
        "tool_use",
        tool_call.arguments if isinstance(tool_call.arguments, str) else json.dumps(tool_call.arguments, ensure_ascii=False),
        tool_name=tool_call.name,
    )


def persist_client_tool_call(service: Any, session_id: int, tool_call: Any) -> None:
    try:
        service._persist_tool_call(session_id, tool_call)
    except Exception as exc:
        logger.error("Failed to persist client tool call %s: %s", tool_call.name, exc)
        try:
            service.db.rollback()
        except Exception:
            pass


def terminal_bridge_call(tool_call: Any) -> tuple[str, dict[str, Any]]:
    try:
        args = tool_call.arguments if isinstance(tool_call.arguments, dict) else json.loads(tool_call.arguments)
    except (json.JSONDecodeError, TypeError):
        args = {}

    pc_action = args.get("action", "") if tool_call.name == "pc_control" else ""
    device = args.pop("device", None)
    bridge_name = {
        "run": "run_bash",
        "read_file": "read_file",
        "write_file": "write_file",
        "screenshot": "screenshot",
        "click": "mouse_click",
        "type": "keyboard_type",
        "hotkey": "hotkey",
        "scroll": "scroll",
    }.get(pc_action, tool_call.name)

    if pc_action == "run":
        args = {"command": args.get("command", "")}
    elif pc_action == "type":
        args = {"text": args.get("content", "")}
    elif pc_action == "write_file":
        args = {"path": args.get("path", ""), "content": args.get("content", "")}

    if device:
        args["_device"] = device
    return bridge_name, args


def append_screenshot_tool_result(
    service: Any,
    messages: list[dict[str, Any]],
    *,
    request_id: str,
    round_index: int,
    tool_call: Any,
    tool_result: dict[str, Any],
) -> bool:
    if not isinstance(tool_result, dict) or "image" not in tool_result:
        return False

    image_data_url = tool_result["image"]
    result_text = f"\u622a\u5c4f {tool_result.get('width', '?')}x{tool_result.get('height', '?')}"
    service._write_cot_block(
        request_id,
        round_index,
        "tool_result",
        result_text,
        tool_name=tool_call.name,
    )

    image_part: dict[str, Any] = {"type": "text", "text": "[\u622a\u56fe]"}
    if isinstance(image_data_url, str) and image_data_url.startswith("data:"):
        try:
            meta, b64data = image_data_url.split(",", 1)
            media_type = meta.split(":")[1].split(";")[0]
            image_part = {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64data},
            }
        except Exception:
            pass

    messages.append({
        "role": "tool",
        "name": tool_call.name,
        "content": [
            {"type": "text", "text": result_text},
            image_part,
        ],
        "tool_call_id": tool_call.id,
    })
    return True


def persist_terminal_tool_result(service: Any, session_id: int, tool_name: str, tool_result: Any) -> None:
    try:
        persist_result = tool_result
        if isinstance(tool_result, dict) and "image" in tool_result:
            persist_result = {"screenshot": f"{tool_result.get('width')}x{tool_result.get('height')}"}
        service._persist_tool_result(session_id, tool_name, persist_result)
    except Exception as exc:
        logger.error("Failed to persist tool result %s: %s", tool_name, exc)
        try:
            service.db.rollback()
        except Exception:
            pass
