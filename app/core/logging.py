import sys

from loguru import logger

from app.core.config import get_settings


def configure_logging() -> None:
    settings = get_settings()
    logger.remove()
    logger.add(
        sys.stdout,
        level=settings.log_level,
        serialize=settings.app_env != "development",
        backtrace=False,
        diagnose=False,
        enqueue=False,
    )


__all__ = ["configure_logging", "logger"]
