"""Persistent, rotating diagnostics for the web service and worker."""

from __future__ import annotations

import faulthandler
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import threading

from .settings import Settings


LOGGER_NAME = "image_magic"
LOG_FILENAME = "image-magic.log"
FAULT_LOG_FILENAME = "image-magic-fault.log"
_fault_stream = None


def configure_logging(settings: Settings) -> Path:
    """Configure one process-wide application journal and native crash trace."""
    log_dir = Path(settings.log_dir).expanduser().resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / LOG_FILENAME

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(_log_level(settings.log_level))
    logger.disabled = False
    logger.propagate = False
    for name, child in logging.Logger.manager.loggerDict.items():
        if name.startswith(f"{LOGGER_NAME}.") and isinstance(child, logging.Logger):
            child.disabled = False
            child.setLevel(logging.NOTSET)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    handler = RotatingFileHandler(
        log_path,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
        delay=True,
    )
    handler.setLevel(_log_level(settings.log_level))
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)s | pid=%(process)d | "
            "thread=%(threadName)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S%z",
        )
    )
    logger.addHandler(handler)

    _install_exception_hooks(logger)
    _configure_fault_handler(log_dir / FAULT_LOG_FILENAME)
    logger.info(
        "logging.configured path=%s level=%s max_bytes=%d backups=%d",
        log_path,
        logging.getLevelName(logger.level),
        settings.log_max_bytes,
        settings.log_backup_count,
    )
    return log_path


def shutdown_logging() -> None:
    """Close diagnostic files; primarily useful for isolated app/test lifecycles."""
    global _fault_stream
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    if _fault_stream is not None:
        try:
            faulthandler.disable()
            _fault_stream.close()
        except OSError:
            pass
        _fault_stream = None


def _log_level(value: str) -> int:
    resolved = getattr(logging, value.strip().upper(), None)
    return resolved if isinstance(resolved, int) else logging.INFO


def _install_exception_hooks(logger: logging.Logger) -> None:
    def process_exception(exc_type, exc_value, exc_traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logger.critical(
            "process.uncaught_exception",
            exc_info=(exc_type, exc_value, exc_traceback),
        )

    def thread_exception(args: threading.ExceptHookArgs) -> None:
        logger.critical(
            "thread.uncaught_exception thread=%s",
            args.thread.name if args.thread else "unknown",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = process_exception
    threading.excepthook = thread_exception


def _configure_fault_handler(path: Path) -> None:
    global _fault_stream
    if _fault_stream is not None:
        try:
            faulthandler.disable()
            _fault_stream.close()
        except OSError:
            pass
    _fault_stream = path.open("a", encoding="utf-8", buffering=1)
    faulthandler.enable(file=_fault_stream, all_threads=True)
