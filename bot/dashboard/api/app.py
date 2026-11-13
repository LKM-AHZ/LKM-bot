from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware

from bot.core import DEMO_MODE, LogBroker
from bot.core.core_lifecycle import BotCoreLifecycle
from bot.core.db import BaseDatabase
from bot.core.log import LogManager
from bot.dashboard.responses import ApiError, error
from bot.dashboard.services.agent_config_service import AgentConfigService
from bot.dashboard.services.api_key_service import ApiKeyService
from bot.dashboard.services.auth_service import AuthService
from bot.dashboard.services.backup_service import BackupService
from bot.dashboard.services.bot_config_service import BotConfigService
from bot.dashboard.services.chat_service import ChatService
from bot.dashboard.services.chatui_project_service import ChatUIProjectService
from bot.dashboard.services.command_service import CommandService
from bot.dashboard.services.config_display_service import ConfigDisplayService
from bot.dashboard.services.config_file_service import ConfigFileService
from bot.dashboard.services.config_profile_service import ConfigProfileService
from bot.dashboard.services.config_routing_service import ConfigRoutingService
from bot.dashboard.services.conversation_service import ConversationService
from bot.dashboard.services.cron_service import CronService
from bot.dashboard.services.file_service import FileService
from bot.dashboard.services.knowledge_base_service import KnowledgeBaseService
from bot.dashboard.services.live_chat_service import LiveChatService
from bot.dashboard.services.log_service import LogService
from bot.dashboard.services.open_api_service import OpenApiService
from bot.dashboard.services.persona_service import PersonaService
from bot.dashboard.services.platform_service import PlatformService
from bot.dashboard.services.plugin_page_service import PluginPageService
from bot.dashboard.services.plugin_service import PluginService
from bot.dashboard.services.provider_config_service import ProviderConfigService
from bot.dashboard.services.session_management_service import (
    SessionManagementService,
)
from bot.dashboard.services.skills_service import SkillsService
from bot.dashboard.services.stat_service import StatService
from bot.dashboard.services.t2i_service import T2iService
from bot.dashboard.services.tools_service import ToolsService
from bot.dashboard.services.update_service import (
    UpdateService,
    call_get_dashboard_version,
    call_pip_install,
)

from .router import API_V1_PREFIX, build_api_router
from .static_files import router as static_files_router

CLEAR_SITE_DATA_HEADERS = {"Clear-Site-Data": '"cache"'}


def create_dashboard_asgi_app(
    *,
    core_lifecycle: BotCoreLifecycle,
    db: BaseDatabase,
    jwt_secret: str,
    static_folder: str | None = None,
) -> FastAPI:
    app = FastAPI(
        title="LKMBot OpenAPI",
        version="1.0.0",
        openapi_url=f"{API_V1_PREFIX}/openapi.json",
        docs_url=f"{API_V1_PREFIX}/docs",
        redoc_url=f"{API_V1_PREFIX}/redoc",
    )
    # Compress JS/CSS and other sizable responses; Starlette's GZipMiddleware
    # skips text/event-stream, so SSE endpoints are unaffected.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.state.core_lifecycle = core_lifecycle
    app.state.db = db
    app.state.jwt_secret = jwt_secret
    app.state.dashboard_static_folder = static_folder
    log_broker = getattr(core_lifecycle, "log_broker", None) or LogBroker()
    stats = StatService(
        db,
        core_lifecycle,
        core_lifecycle.bot_config,
        dashboard_static_folder=static_folder,
    )
    app.state.services = SimpleNamespace(
        config_profiles=ConfigProfileService(core_lifecycle, db, runtime=stats.runtime),
        config_display=ConfigDisplayService(core_lifecycle),
        config_files=ConfigFileService(core_lifecycle),
        config_routes=ConfigRoutingService(core_lifecycle),
        api_keys=ApiKeyService(db),
        auth=AuthService(db, core_lifecycle.bot_config),
        backups=BackupService(db, core_lifecycle),
        chat=ChatService(db, core_lifecycle),
        chat_projects=ChatUIProjectService(db),
        commands=CommandService(core_lifecycle.bot_config, core_lifecycle),
        conversations=ConversationService(db, core_lifecycle),
        cron=CronService(core_lifecycle),
        files=FileService(),
        knowledge_bases=KnowledgeBaseService(core_lifecycle),
        live_chat=LiveChatService(db, core_lifecycle),
        logs=LogService(
            log_broker,
            core_lifecycle.bot_config,
            core=core_lifecycle,
            dashboard_static_folder=static_folder,
        ),
        bots=BotConfigService(core_lifecycle),
        platforms=PlatformService(core_lifecycle),
        providers=ProviderConfigService(core_lifecycle),
        personas=PersonaService(core_lifecycle),
        plugins=PluginService(core_lifecycle, core_lifecycle.plugin_manager),
        plugin_pages=PluginPageService(
            core_lifecycle.plugin_manager,
            core_lifecycle=core_lifecycle,
        ),
        open_api=OpenApiService(db, core_lifecycle),
        sessions=SessionManagementService(core_lifecycle, db),
        skills=SkillsService(core_lifecycle),
        stats=stats,
        agent_configs=AgentConfigService(
            core_lifecycle.agent_configs,
            core_lifecycle.agent_runtimes,
            core_lifecycle.bot_config_mgr,
        ),
        t2i=T2iService(core_lifecycle),
        tools=ToolsService(core_lifecycle),
        updates=UpdateService(
            core_lifecycle.bot_updater,
            core_lifecycle,
            get_dashboard_version_func=call_get_dashboard_version,
            pip_install_func=call_pip_install,
            demo_mode=DEMO_MODE,
            clear_site_data_headers=CLEAR_SITE_DATA_HEADERS,
            dashboard_static_folder=static_folder,
        ),
    )

    @app.exception_handler(ApiError)
    async def api_error_handler(_request: Request, exc: ApiError):
        return JSONResponse(
            error(exc.message, exc.data),
            status_code=exc.status_code,
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(_request: Request, exc: ValueError):
        return JSONResponse(error(str(exc)), status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def starlette_http_error_handler(
        _request: Request, exc: StarletteHTTPException
    ):
        if isinstance(exc.detail, str):
            return JSONResponse(
                error(exc.detail), status_code=exc.status_code, headers=exc.headers
            )
        return JSONResponse(
            error("Request failed", exc.detail),
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def catch_all_handler(_request: Request, exc: Exception):
        LogManager.GetLogger("bot.dashboard").error(
            "Unhandled exception in dashboard API",
            exc_info=exc,
        )
        return JSONResponse(
            error("Internal server error"),
            status_code=500,
        )

    app.include_router(build_api_router())
    app.include_router(static_files_router)
    return app
