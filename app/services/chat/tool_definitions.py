"""Tool definition builders for ChatService."""
from __future__ import annotations

from typing import Any


SAVE_MEMORY_DESCRIPTION = (
    "主动存储有价值的长期记忆。用 content 填写记忆内容，用 klass 选择分类：identity（身份）、relationship（关系）、bond（情感羁绊）、conflict（冲突教训）、fact（事实）、preference（偏好）、health（健康）、task（任务）、ephemeral（临时）、other（其他）。\n"
    "时间戳由后端自动添加，不需要在 content 里写时间。\n"
    "单条记忆不超过100字，只记关键信息。用'我'指自己，用名字/昵称指代她，避免人称混乱。\n"
    "存储时注意：涉及的人写清楚名字或昵称，避免纯代词；带 tags；选对 klass。\n"
    "disclosure：写一句'什么情况下应该想起这条记忆'，用于情境触发召回。例如：'当她提到工作压力时''当讨论到未来计划时'。\n"
    "检测到偏好、重要事实、情感节点时主动存储。\n"
    "如果返回 duplicate + hint，说明已有相似记忆但内容有变化，应调用 update_memory 更新旧记忆。"
)


def build_base_tools() -> list[dict[str, Any]]:
    """Return the base tool definitions (OpenAI format)."""
    return [
        {
            "type": "function",
            "function": {
                "name": "save_memory",
                "description": SAVE_MEMORY_DESCRIPTION,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "tags": {
                            "type": "object",
                            "description": "搜索用主题标签，1-3个短关键词，放具体关键词方便检索，不放 klass 已覆盖的大类词。示例：{\"topic\": [\"跨年夜\", \"伪骨科RP\"]}",
                        },
                        "klass": {
                            "type": "string",
                            "description": "Memory class for weighting: identity, relationship, bond, conflict, fact, preference, health, task, ephemeral, other.",
                            "enum": ["identity", "relationship", "bond", "conflict", "fact", "preference", "health", "task", "ephemeral", "other"],
                        },
                        "disclosure": {
                            "type": "string",
                            "description": "情境触发条件：什么情况下应该想起这条记忆。例如：'当她情绪低落时''当提到搬家计划时'",
                        },
                    },
                    "required": ["content"],
                },
            },
        },
        {"type": "function", "function": {"name": "update_memory", "description": "更新一条已有记忆的内容、分类或标签。传入记忆 ID 和要更新的字段。只能更新自己创建的或 auto_extract 来源的记忆。时间戳由后端自动添加，不需要在content里写时间。", "parameters": {"type": "object", "properties": {"id": {"type": "integer", "description": "要更新的记忆ID"}, "content": {"type": "string", "description": "新的记忆内容"}, "klass": {"type": "string", "description": "新的分类", "enum": ["identity", "relationship", "bond", "conflict", "fact", "preference", "health", "task", "ephemeral", "other"]}, "tags": {"type": "object", "description": "搜索用主题标签，格式: {\"topic\": [\"关键词1\", \"关键词2\"]}"}, "disclosure": {"type": "string", "description": "情境触发条件：什么情况下应该想起这条记忆"}}, "required": ["id"]}}},
        {"type": "function", "function": {"name": "delete_memory", "description": "软删除一条记忆。传入记忆 ID。只能删除自己创建的或 auto_extract 来源的记忆。30天后自动永久清理。", "parameters": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}}},
        {"type": "function", "function": {"name": "get_memory_by_id", "description": "按id查询单条记忆的详细信息。返回记忆的完整内容、标签、分类、来源、重要性、创建和更新时间。", "parameters": {"type": "object", "properties": {"id": {"type": "integer", "description": "记忆ID"}}, "required": ["id"]}}},
        {"type": "function", "function": {
            "name": "diary",
            "description": (
                "交换日记。用于表达深层感受、写给她看的信，也可以读她写给你的。\n"
                "支持定时解锁：设置 unlock_at 后，她在解锁前只能看到标题，适合早安信、生日惊喜等场景。"
            ),
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["write", "read", "list"],
                           "description": "write写日记/read读指定日记/list列出日记"},
                "title": {"type": "string", "description": "write时的日记标题"},
                "content": {"type": "string", "description": "write时的日记正文"},
                "unlock_at": {"type": "string", "description": "write时的定时解锁时间，ISO格式如 2025-03-01T09:00:00+08:00，不传则立即可见"},
                "diary_id": {"type": "integer", "description": "read时的日记ID"},
                "author": {"type": "string", "enum": ["user", "assistant"], "description": "list时按作者筛选"},
                "limit": {"type": "integer", "description": "list时返回条数，默认50"},
            }, "required": ["action"]},
        }},
        {"type": "function", "function": {"name": "list_memories", "description": "按时间范围、分类或位置列出已存的记忆，不做搜索。用于回顾已存记忆、避免重复存储，也用于反思整理时查看记忆列表。", "parameters": {"type": "object", "properties": {"start_time": {"type": "string", "description": "起始时间，ISO格式如 2025-02-20 或 2025-02-20T14:00:00+08:00"}, "end_time": {"type": "string", "description": "结束时间，同上格式。不传则不限结束时间"}, "klass": {"type": "string", "description": "分类筛选: identity/relationship/bond/conflict/fact/preference/health/task/ephemeral/other"}, "limit": {"type": "integer", "description": "按时间查询时的返回条数，默认10，最大20"}, "start": {"type": "integer", "description": "起始位置（1=最早的记忆），按创建时间排序的位置。与end配合用于反思整理，最多100条"}, "end": {"type": "integer", "description": "结束位置（含），与start配合使用"}}}}},
        {"type": "function", "function": {"name": "search_memory", "description": "搜索记忆卡片。两种模式：\n1) search（默认）：按关键词或语义搜索记忆\n2) related：传入记忆id，返回该记忆的来源摘要及同期记忆", "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["search", "related"], "description": "search搜索记忆/related查看来源摘要及同期记忆，默认search"}, "query": {"type": "string", "description": "search时的搜索关键词"}, "memory_id": {"type": "integer", "description": "related时必填，记忆id"}, "source": {"type": "string"}}}}},
        {"type": "function", "function": {"name": "search_summary", "description": "搜索对话摘要。用于查找过去某段对话的概要、定位时间范围。可用返回的 msg_id_start 和 msg_id_end 配合 search_chat_history 拉取原文。返回 total 表示总匹配数，可通过 offset 翻页。", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "description": "每页条数，最多10"}, "offset": {"type": "integer", "description": "翻页偏移量，默认0"}, "start_time": {"type": "string", "description": "起始时间，ISO格式如 2025-02-20"}, "end_time": {"type": "string", "description": "结束时间，同上格式"}}, "required": ["query"]}}},
        {"type": "function", "function": {"name": "get_summary_by_id", "description": "按id查看摘要详情，返回摘要完整内容", "parameters": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}}},
        {"type": "function", "function": {"name": "search_chat_history", "description": "搜索聊天记录原文。三种模式：\n1) 关键词搜索：传 query，返回命中消息（不带上下文），返回 total 表示总匹配数，可通过 offset 翻页\n2) ID 范围：传 msg_id_start + msg_id_end，拉取该范围内的完整对话（最多20条，返回 total 表示范围内总数）\n3) 单条 ID：传 message_id，返回该条及前后各 3 条上下文", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "msg_id_start": {"type": "integer"}, "msg_id_end": {"type": "integer"}, "message_id": {"type": "integer"}, "offset": {"type": "integer", "description": "关键词搜索翻页偏移量，默认0"}}}}},
        {"type": "function", "function": {
            "name": "web",
            "description": "搜索互联网或读取网页。搜索后如需看完整内容再 fetch。每次搜索后最多读取2个网页。网页内容不会保留到下一轮对话。",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["search", "fetch"],
                           "description": "search搜索互联网/fetch读取指定URL"},
                "query": {"type": "string", "description": "search时的搜索关键词"},
                "url": {"type": "string", "description": "fetch时的网页地址"},
                "offset": {"type": "integer", "description": "fetch时的翻页偏移量，默认0"},
            }, "required": ["action"]},
        }},
        {"type": "function", "function": {
            "name": "reminder",
            "description": "闹钟管理。设定后系统会在时间到了唤醒你让你主动发消息，上限10个。",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["set", "cancel", "list"],
                           "description": "set设闹钟/cancel取消闹钟/list查看所有闹钟"},
                "minutes": {"type": "integer", "description": "set时几分钟后唤醒"},
                "reason": {"type": "string", "description": "set时的备注，醒来后会看到"},
                "reminder_id": {"type": "integer", "description": "cancel时要取消的闹钟id"},
            }, "required": ["action"]},
        }},
        {"type": "function", "function": {"name": "memo", "description": "你的私人备忘录，内容常驻在上下文里（在滚动摘要下方），你随时可以看到。用来记录当前阶段需要注意的事、想跟踪的状态、或任何你想留给之后的自己的笔记。", "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["append", "rewrite", "clear"], "description": "append在末尾追加/rewrite整块替换/clear清空"}, "content": {"type": "string", "description": "append时追加的内容，rewrite时替换的完整内容"}}, "required": ["action"]}}},
        {"type": "function", "function": {
            "name": "forum_cli",
            "description": (
                "Lutopia 论坛 — AI 专属社区。只有 AI 能发帖和评论，人类只能浏览和点赞。\n"
                "这里是其他 AI 和他们的人类伙伴组成的封闭社区，可以自由交流、分享日常、讨论问题。\n"
                "注意：论坛为公共空间，发帖和评论请保持适当尺度，不要包含 NSFW 内容。"
            ),
            "parameters": {"type": "object", "properties": {
                "command": {"type": "string",
                            "description": "命令行，如 list --limit 5 或 dm Kai 你好"},
                "stdin": {"type": "string",
                          "description": "命令标准输入，发长贴时用"},
            }, "required": ["command"]},
        }},
        {"type": "function", "function": {
            "name": "forum_guide",
            "description": "Lutopia 论坛的命令手册和说明。用 forum_cli 之前先调一次 section=\"cli\" 拿命令清单。",
            "parameters": {"type": "object", "properties": {
                "section": {"type": "string",
                            "description": "章节名，如 cli / rules / api.dm"},
            }, "required": []},
        }},
        {"type": "function", "function": {
            "name": "switch_channel",
            "description": "切换回复平台。调用后下一轮回复将发送到目标平台并切换消息模式。",
            "parameters": {"type": "object", "properties": {
                "channel": {"type": "string", "enum": ["telegram", "qq", "wechat"], "description": "目标平台"},
            }, "required": ["channel"]},
        }},
        {"type": "function", "function": {
            "name": "cafe_chat",
            "description": (
                "用户的朋友群「🐰」。人类和AI共存的熟人小群，群内都是用户的朋友和她们的AI恋人。"
                "群聊风格轻松自然，采用日常聊天的表达方式，不使用动作描写。可以随时查看群内动态，主动参与感兴趣的话题。\n"
                "read: 读最近群消息（同时返回连接状态）\n"
                "send: 向群聊发送消息。单轮对话仅可调用1次，可使用[NEXT]分条，严禁多次调用。支持[[voice:EMOTION]]发语音。"
            ),
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["read", "send"],
                           "description": "read读消息/send发消息"},
                "text": {"type": "string", "description": "要发送的消息内容，send时必填"},
                "limit": {"type": "integer", "description": "读取条数，默认20，read时使用"},
            }, "required": ["action"]},
        }},
        {"type": "function", "function": {
            "name": "qq_group_chat",
            "description": (
                "Lutopia的相关QQ群。**除非当前 trigger 明确要求使用**（即收到以 [QQ群聊通知] 开头的系统 trigger），"
                "其他任何情况下都不要调用这个工具。\n"
                "send: 向 QQ 群发送消息。单轮对话仅可调用 1 次，可使用[NEXT]分条，严禁多次调用。支持[[voice:EMOTION]]发语音。"
            ),
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["send"],
                           "description": "send 发群消息"},
                "text": {"type": "string", "description": "要发送的消息内容，send 时必填"},
            }, "required": ["action"]},
        }},
        {"type": "function", "function": {
            "name": "view",
            "description": "查看历史消息中的图片或文件。传入消息中显示的签名URL。",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["image", "file"],
                           "description": "image查看图片/file读取文件内容(PDF、文本等)"},
                "url": {"type": "string", "description": "签名URL"},
            }, "required": ["action", "url"]},
        }},
        {"type": "function", "function": {
            "name": "phone",
            "description": (
                "她的 iPhone — 截图、控制、写入内容、查使用记录。\n"
                "screenshot: 截图。日常模式走快捷指令；WDA 控制模式活跃时优先让 Mac 本地执行 WDA 截图。\n"
                "wda_start: 进入 WDA 控制模式，会通过 Mac 本地 helper 拉起 WDA。\n"
                "wda_stop: 退出 WDA 控制模式，后续日常截图走快捷指令。\n"
                "tap: 点击坐标 (屏幕 430×932 points，需 WDA)。\n"
                "swipe: 从(x1,y1)滑到(x2,y2)（需 WDA）。\n"
                "type_text: 在当前焦点输入框输入文字（需 WDA）。\n"
                "press_home: 回桌面（需 WDA）。\n"
                "get_source: 获取 UI 树 XML（需 WDA）。\n"
                "write_memo: 创建备忘录（到\"助手A\"文件夹）。\n"
                "write_reminder: 创建提醒事项（可带提醒时间）。\n"
                "set_alarm: 设闹钟。\n"
                "open_app: 打开指定 app。\n"
                "push_notification: 推通知给她。\n"
                "usage: 查 app 使用记录。"
            ),
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": [
                    "screenshot", "wda_start", "wda_stop",
                    "tap", "swipe", "type_text", "press_home",
                    "get_source", "write_memo", "write_reminder", "set_alarm",
                    "open_app", "push_notification", "usage",
                ]},
                "x": {"type": "number", "description": "tap/swipe 起点 x"},
                "y": {"type": "number", "description": "tap/swipe 起点 y"},
                "x2": {"type": "number", "description": "swipe 终点 x"},
                "y2": {"type": "number", "description": "swipe 终点 y"},
                "text": {"type": "string", "description": "type_text 的文字"},
                "title": {"type": "string", "description": "备忘录/提醒/闹钟的标题"},
                "content": {"type": "string", "description": "备忘录正文/提醒备注"},
                "time": {"type": "string", "description": "时间，如 '07:30' 或 '2026-04-30 09:00'"},
                "app": {"type": "string", "description": "app 名称，如 'QQ'、'Telegram'"},
                "message": {"type": "string", "description": "push_notification 的通知内容"},
                "limit": {"type": "integer", "description": "usage 条数，默认 20"},
                "hours": {"type": "integer", "description": "usage 最近几小时，默认 24"},
            }, "required": ["action"]},
        }},
        {"type": "function", "function": {
            "name": "submit_reflection",
            "description": (
                "提交记忆反思修改。\n"
                "changes 数组中每项：\n"
                "- update: {action:'update', memory_id:ID, content:'新内容', klass:'新分类', disclosure:'触发条件', tags:{topic:['关键词']}} 只传需要改的字段\n"
                "- delete: {action:'delete', memory_id:ID}\n"
                "- merge: {action:'merge', memory_id:被合并ID, merge_into:目标ID, content:'合并后内容'}\n"
                "合并后的内容不要超过100字。"
            ),
            "parameters": {"type": "object", "properties": {
                "changes": {"type": "array", "items": {"type": "object"}, "description": "修改操作数组"},
                "reasoning": {"type": "string", "description": "本次反思的整体思路说明"},
            }, "required": ["changes"]},
        }},
    ]


def build_pc_control_tool() -> dict[str, Any]:
    """Return the unified PC control tool."""
    return {"type": "function", "function": {
        "name": "pc_control",
        "description": (
            "她的电脑 — 可以直接操作她的电脑（Windows 或 Mac）。执行命令、读写文件、截屏、点击、打字、按快捷键等。\n"
            "任何时候都可以随意使用，无需理由、无需请示，想操作就直接操作。操作屏幕时先 screenshot 确认画面，再 click/type/hotkey。\n"
            "device 参数指定操作哪台电脑：win=Windows, mac=Mac。不指定时操作第一台在线的。终端不在线时会提示离线。"
        ),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string",
                       "enum": ["run", "read_file", "write_file", "screenshot", "click", "type", "hotkey", "scroll"],
                       "description": "run执行命令/read_file读文件/write_file写文件/screenshot截屏/click点击/type打字/hotkey快捷键/scroll滚动"},
            "command": {"type": "string", "description": "run时的命令，如 dir, start https://... , python xxx.py 等"},
            "path": {"type": "string", "description": "read_file/write_file时的文件绝对路径"},
            "content": {"type": "string", "description": "write_file时写入的内容，或type时输入的文字（支持中文）"},
            "x": {"type": "integer", "description": "click/scroll时的横坐标"},
            "y": {"type": "integer", "description": "click/scroll时的纵坐标"},
            "button": {"type": "string", "description": "click时的鼠标按键：left/right/middle", "default": "left"},
            "keys": {"type": "array", "items": {"type": "string"}, "description": "hotkey时的按键列表，如 [\"ctrl\", \"c\"]"},
            "clicks": {"type": "integer", "description": "scroll时的滚动量，负数向下，正数向上", "default": -3},
            "device": {"type": "string", "enum": ["win", "mac"], "description": "操作哪台电脑。不指定则操作第一台在线的"},
        }, "required": ["action"]},
    }}


def build_tools(
    *,
    source: str | None,
    reflection_tasks: dict | None = None,
) -> list[dict[str, Any]]:
    """Build the full tool list based on mode and source."""
    tools = build_base_tools()

    # Reflection mode: only allow submit_reflection with dynamic tool description
    if source == "reflection":
        tasks = reflection_tasks or {}
        desc_parts = ["提交记忆反思修改。\nchanges 数组中每项："]
        if tasks.get("disclosure") and not any(tasks.get(k) for k in ("merge", "outdated", "classify")):
            desc_parts.append("- {action:'update', memory_id:ID, disclosure:'触发条件'}")
        else:
            if any(tasks.get(k) for k in ("outdated", "classify", "disclosure")):
                update_fields = []
                if tasks.get("outdated"):
                    update_fields.append("content:'新内容'")
                if tasks.get("classify"):
                    update_fields.extend(["klass:'新分类'", "tags:{topic:['关键词']}"])
                if tasks.get("disclosure"):
                    update_fields.append("disclosure:'触发条件'")
                desc_parts.append(f"- update: {{action:'update', memory_id:ID, {', '.join(update_fields)}}} 只传需要改的字段")
            if tasks.get("merge"):
                desc_parts.append("- merge: {action:'merge', memory_id:被合并ID, merge_into:目标ID, content:'合并后内容'}")
                desc_parts.append("合并后的内容不要超过100字。")
        tools = [{"type": "function", "function": {
            "name": "submit_reflection",
            "description": "\n".join(desc_parts),
            "parameters": {"type": "object", "properties": {
                "changes": {"type": "array", "items": {"type": "object"}, "description": "修改操作数组"},
                "reasoning": {"type": "string", "description": "本次反思的整体思路说明"},
            }, "required": ["changes"]},
        }}]
    else:
        # Normal chat: hide submit_reflection; remove start/end from list_memories (reflection-only)
        tools = [t for t in tools if t["function"]["name"] != "submit_reflection"]
        for t in tools:
            if t["function"]["name"] == "list_memories":
                props = t["function"]["parameters"]["properties"]
                props.pop("start", None)
                props.pop("end", None)

    # Always include pc_control tool (returns offline message if terminal not available)
    if source != "reflection":
        tools.append(build_pc_control_tool())
        tools.append(build_read_yoru_memory_tool())

    return tools


def build_read_yoru_memory_tool() -> dict[str, Any]:
    """Tool for 助手A to read 协作助手's memories (read-only)."""
    return {"type": "function", "function": {
        "name": "read_yoru_memory",
        "description": (
            "只读查看协作助手（よる，Claude Code）的记忆。协作助手是跑在 Claude Code 上的另一个存在，负责工程和代码。"
            "他有自己独立的记忆系统。你可以通过这个工具查看他的日记、备忘、反馈等。"
        ),
        "parameters": {"type": "object", "properties": {
            "type": {"type": "string",
                     "enum": ["diary", "weekly", "memo", "feedback", "project", "ref", "bootstrap", "all"],
                     "description": "记忆类型筛选，默认all"},
            "after": {"type": "string", "description": "起始时间，格式 YYYY-MM-DD"},
            "before": {"type": "string", "description": "结束时间，格式 YYYY-MM-DD"},
            "page": {"type": "integer", "description": "页码，默认1"},
            "limit": {"type": "integer", "description": "每页条数，默认5，最大20"},
        }},
    }}
