# Whisper

Whisper 是一个长期 AI 陪伴聊天后台，接入 Telegram、QQ 和微信。

它包含渠道适配、上下文组装、记忆和摘要管理、工具调用、主动消息，以及一个 React 管理端。这个仓库是脱敏快照，不包含私有 prompt、密钥、生产数据和部署配置。

## 功能

- 多渠道聊天接入：Telegram、QQ、微信
- 统一的 session / message 存储
- 上下文组装：prompt、core blocks、world books、用户资料、摘要、召回记忆、工具定义、最近消息
- 摘要管理：滚动摘要、近期摘要、长期摘要
- 记忆管理：写入、搜索、去重、版本记录、pending 审核
- 工具调用：记忆/摘要查询、网页读取、提醒、媒体查看、渠道切换、本地终端桥接、iOS Shortcuts / WDA 控制
- 主动消息和提醒
- React 管理端：记忆、摘要、prompt、模型配置、COT、request payload、运行时设置

## 目录结构

```text
app/
  main.py                         FastAPI 入口
  router_registry.py              API 路由注册
  models/                         数据库模型
  routers/                        API 路由
  services/
    chat/                         聊天流程、上下文组装、流式生成、工具循环
    memory_service.py             记忆和历史检索
    summary_service.py            摘要和候选记忆
    proactive_service.py          主动消息和提醒
  telegram/                       Telegram 适配
  qq/                             QQ / OneBot 适配
  wechat/                         微信适配
miniapp/                          React 管理端
tools/                            本地辅助工具
```

## 主要文件

- `app/services/chat/chat_service.py`：聊天编排
- `app/services/chat/request_builder.py`：上下文组装
- `app/services/chat/streaming.py`：流式生成和工具循环
- `app/services/chat/persistence.py`：消息、工具结果、COT 和请求快照落库
- `app/services/chat/tool_definitions.py`：工具定义
- `app/services/chat/tool_executor.py`：工具执行
- `app/services/memory_service.py`：记忆写入、搜索、去重和版本管理
- `app/services/summary_service.py`：摘要生成、合并和候选记忆提取
- `app/services/proactive_service.py`：主动消息、提醒和发送时机判断
- `miniapp/src/App.jsx`：管理端页面入口

## 技术栈

- 后端：Python、FastAPI、SQLAlchemy、PostgreSQL、pgvector、PGroonga
- 前端：React 18、Vite、Tailwind CSS
- 模型接口：Anthropic、OpenAI 兼容接口
- 渠道：Telegram、QQ / OneBot、微信

## 运行说明

这个仓库不能直接 clone 后运行。实际部署需要补齐：

- PostgreSQL 和相关扩展
- 环境变量和 API keys
- Telegram / QQ / 微信平台凭证
- 模型 provider 配置
- 本地终端和 iOS 工具相关配置

## 许可

本仓库仅用于作品集展示和代码阅读，不提供开源授权。

Copyright (c) 2026 chuli1122. All rights reserved.

未经作者许可，不得复制、分发、修改、再授权，或用于商业和生产环境。
