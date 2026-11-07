"""Pipeline bootstrap utilities."""

from importlib import import_module

from .stage import registered_stages

_BUILTIN_STAGE_MODULES = (
    "bot.life.pipeline",
    "bot.core.pipeline.waking_check.stage",
    "bot.core.pipeline.whitelist_check.stage",
    "bot.core.pipeline.session_status_check.stage",
    "bot.core.pipeline.rate_limit_check.stage",
    "bot.core.pipeline.content_safety_check.stage",
    "bot.core.pipeline.preprocess_stage.stage",
    "bot.core.pipeline.process_stage.stage",
    "bot.core.pipeline.result_decorate.stage",
    "bot.core.pipeline.respond.stage",
)

_EXPECTED_STAGE_NAMES = {
    "LifeIngestStage",
    "LifeTurnStage",
    "WakingCheckStage",
    "WhitelistCheckStage",
    "SessionStatusCheckStage",
    "RateLimitStage",
    "ContentSafetyCheckStage",
    "PreProcessStage",
    "ProcessStage",
    "ResultDecorateStage",
    "RespondStage",
}

_builtin_stages_registered = False


def ensure_builtin_stages_registered() -> None:
    """Ensure built-in pipeline stages are imported and registered."""
    global _builtin_stages_registered

    if _builtin_stages_registered:
        return

    stage_names = {stage_cls.__name__ for stage_cls in registered_stages}
    if _EXPECTED_STAGE_NAMES.issubset(stage_names):
        _builtin_stages_registered = True
        return

    for module_path in _BUILTIN_STAGE_MODULES:
        import_module(module_path)

    _builtin_stages_registered = True


__all__ = ["ensure_builtin_stages_registered"]
