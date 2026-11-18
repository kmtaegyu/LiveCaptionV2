"""Rotating UTF-8 diagnostics shared by the player and stream resolver."""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import platform
import re
import sys
from urllib.parse import urlsplit, urlunsplit

from PySide6.QtCore import QObject, Signal, QtMsgType, qInstallMessageHandler


LOG_PATH = Path(__file__).resolve().parent / "logs" / "livecaption.log"


def redact_urls(text):
    def replace(match):
        try:
            url = urlsplit(match.group())
            host = url.hostname or ""
            if ":" in host:
                host = f"[{host}]"
            if url.port:
                host += f":{url.port}"
            return urlunsplit((url.scheme, host, url.path,
                               "REDACTED" if url.query else "", ""))
        except ValueError:
            return "[invalid URL]"
    return re.sub(r"https?://[^\s\"'<>]+", replace, text)


class SafeFormatter(logging.Formatter):
    def format(self, record):
        return redact_urls(super().format(record))


def log_formatter():
    return SafeFormatter("%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
                         datefmt="%Y-%m-%d %H:%M:%S")


class LogSignals(QObject):
    message = Signal(str)


class WindowLogHandler(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.signals = LogSignals()
        self.setFormatter(log_formatter())

    def emit(self, record):
        self.signals.message.emit(self.format(record))


def configure_logging(path=LOG_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("livecaption")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    # Allow repeated setup without duplicate file handlers.
    for handler in list(logger.handlers):
        if isinstance(handler, RotatingFileHandler):
            logger.removeHandler(handler)
            handler.close()
    handler = RotatingFileHandler(path, maxBytes=2 * 1024 * 1024,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(log_formatter())
    logger.addHandler(handler)
    return logger


def install_diagnostics():
    logger = logging.getLogger("livecaption")

    def exception_hook(kind, value, traceback):
        logger.critical("Unhandled Python exception", exc_info=(kind, value, traceback))
        sys.__excepthook__(kind, value, traceback)

    def qt_message(kind, context, message):
        levels = {QtMsgType.QtDebugMsg: logging.DEBUG, QtMsgType.QtInfoMsg: logging.INFO,
                  QtMsgType.QtWarningMsg: logging.WARNING,
                  QtMsgType.QtCriticalMsg: logging.ERROR, QtMsgType.QtFatalMsg: logging.CRITICAL}
        logger.log(levels.get(kind, logging.INFO), "Qt [%s] %s", context.category, message)

    sys.excepthook = exception_hook
    qInstallMessageHandler(qt_message)
    logger.info("Application starting: Python=%s OS=%s", sys.version.split()[0], platform.platform())
