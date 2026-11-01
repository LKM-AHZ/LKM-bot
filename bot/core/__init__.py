import os

from bot.core.config import BotConfig
from bot.core.config.default import DB_PATH
from bot.core.db.sqlite import SQLiteDatabase
from bot.core.file_token_service import FileTokenService
from bot.core.utils.pip_installer import (
    DependencyConflictError as DependencyConflictError,
)
from bot.core.utils.pip_installer import (
    PipInstaller,
)
from bot.core.utils.requirements_utils import (
    RequirementsPrecheckFailed as RequirementsPrecheckFailed,
)
from bot.core.utils.requirements_utils import (
    find_missing_requirements as find_missing_requirements,
)
from bot.core.utils.requirements_utils import (
    find_missing_requirements_or_raise as find_missing_requirements_or_raise,
)
from bot.core.utils.shared_preferences import SharedPreferences
from bot.core.utils.t2i.renderer import HtmlRenderer

from .log import LogBroker, LogManager  # noqa
from .utils.bot_path import get_bot_data_path

os.makedirs(get_bot_data_path(), exist_ok=True)

DEMO_MODE = os.getenv("DEMO_MODE", "False").strip().lower() in ("true", "1", "t")

bot_config = BotConfig()
t2i_base_url = bot_config.get("t2i_endpoint", "https://t2i.soulter.top/text2img")
html_renderer = HtmlRenderer(t2i_base_url)
"""**Deprecated**. For plugin developers, please use `self.context.html_renderer` instead."""
logger = LogManager.GetLogger(log_name="bot")
LogManager.configure_logger(logger, bot_config)
db_helper = SQLiteDatabase(DB_PATH)
sp = SharedPreferences(db_helper=db_helper)
file_token_service = FileTokenService()
pip_installer = PipInstaller(
    bot_config.get("pip_install_arg", ""),
    bot_config.get("pypi_index_url", None),
)
