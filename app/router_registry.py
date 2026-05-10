from fastapi import Depends, FastAPI

from app.routers import (
    api_providers,
    assistants,
    auth,
    chat,
    core_blocks,
    cot,
    diary,
    ios,
    maintenance,
    media,
    memories,
    messages,
    model_presets,
    pending_memories,
    reflection,
    rin,
    sessions,
    settings,
    settings_summary,
    theater,
    upload,
    user_profile,
    world_books,
    yoru,
    memes,
)
from app.routers.auth import require_auth_token
from app.telegram.router import router as telegram_router


def register_routes(app: FastAPI) -> None:
    auth_deps = [Depends(require_auth_token)]
    app.include_router(chat.router, prefix="/api", tags=["chat"], dependencies=auth_deps)
    app.include_router(messages.router, prefix="/api", tags=["messages"], dependencies=auth_deps)
    app.include_router(sessions.router, prefix="/api", tags=["sessions"], dependencies=auth_deps)
    app.include_router(assistants.router, prefix="/api", tags=["assistants"], dependencies=auth_deps)
    app.include_router(user_profile.router, prefix="/api", tags=["user_profile"], dependencies=auth_deps)
    app.include_router(memories.router, prefix="/api", tags=["memories"], dependencies=auth_deps)
    app.include_router(core_blocks.router, prefix="/api", tags=["core_blocks"], dependencies=auth_deps)
    app.include_router(world_books.router, prefix="/api", tags=["world_books"], dependencies=auth_deps)
    app.include_router(memes.router, prefix="/api", tags=["memes"], dependencies=auth_deps)
    app.include_router(diary.router, prefix="/api", tags=["diary"], dependencies=auth_deps)
    app.include_router(maintenance.router, prefix="/api", tags=["maintenance"], dependencies=auth_deps)
    app.include_router(settings.router, prefix="/api", tags=["settings"], dependencies=auth_deps)
    app.include_router(settings_summary.router, prefix="/api", tags=["settings"], dependencies=auth_deps)
    app.include_router(theater.router, prefix="/api", tags=["theater"], dependencies=auth_deps)
    app.include_router(api_providers.router, prefix="/api", tags=["api_providers"], dependencies=auth_deps)
    app.include_router(model_presets.router, prefix="/api", tags=["model_presets"], dependencies=auth_deps)
    app.include_router(cot.router, prefix="/api", tags=["cot"], dependencies=auth_deps)
    app.include_router(pending_memories.router, prefix="/api", tags=["pending_memories"], dependencies=auth_deps)
    app.include_router(reflection.router, prefix="/api", tags=["reflection"], dependencies=auth_deps)
    app.include_router(upload.router, prefix="/api", tags=["upload"], dependencies=auth_deps)
    app.include_router(auth.router, prefix="/api", tags=["auth"])
    app.include_router(yoru.router, prefix="/api", tags=["yoru"])  # no auth, MCP server calls from localhost
    app.include_router(yoru.deploy_router, prefix="/api", tags=["yoru-deploy"])
    app.include_router(rin.router, prefix="/api", tags=["rin"])  # no auth, MCP server calls from localhost/private network
    app.include_router(media.router, prefix="/api", tags=["media"])
    app.include_router(ios.router, prefix="/api", tags=["ios"])

    from app.routers.wechat import router as wechat_mgmt_router
    from app.qq.router import router as qq_router

    app.include_router(wechat_mgmt_router, prefix="/api", tags=["wechat"], dependencies=auth_deps)
    app.include_router(telegram_router, tags=["telegram"])
    app.include_router(qq_router, tags=["qq"])
