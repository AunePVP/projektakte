"""Logging for ``python -m projektakte``: one timestamped format for the application and uvicorn,
and an access log without query strings (search terms are project and personal data)."""
from __future__ import annotations

import logging
import logging.config
import re

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class StripQueryString(logging.Filter):
    """Removes the query string from the request line of uvicorn's access log
    (``"GET /suche?q=… HTTP/1.1"`` → ``"GET /suche HTTP/1.1"``).

    uvicorn passes the path as an argument (``'%s - "%s %s HTTP/%s" %d'``): it is cut at the first
    ``?`` there, whatever characters follow (a raw ``"`` from curl must not end the filter). Other
    message shapes fall back to the formatted text, cut up to the closing `` HTTP/x.y"``."""

    _QUERY = re.compile(r'("[A-Z]+ [^ ?]*)\?.*?( HTTP/[0-9.]+")', re.DOTALL)

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if (isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str)
                and record.msg == '%s - "%s %s HTTP/%s" %d'):
            if "?" in args[2]:
                record.args = (*args[:2], args[2].split("?", 1)[0], *args[3:])
            return True
        message = record.getMessage()
        cleaned = self._QUERY.sub(r"\1\2", message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        return True


class DropHealthProbes(logging.Filter):
    """Successful probes of /health and /health/ready (Docker HEALTHCHECK every 30 s, monitoring)
    would drown the access log; failed probes are still logged."""

    _PROBE = re.compile(r'"GET /health(?:/ready)?(?:\?[^ "]*)? HTTP/[0-9.]+" 200\b')

    def filter(self, record: logging.LogRecord) -> bool:
        return not self._PROBE.search(record.getMessage())


def logging_config(level: str = "INFO") -> dict:
    """dictConfig: everything goes through the root handler (same format); uvicorn's loggers
    propagate instead of using their own handlers and colours."""
    level = level.upper()
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"strip_query": {"()": StripQueryString}, "drop_probes": {"()": DropHealthProbes}},
        "formatters": {"default": {"format": LOG_FORMAT}},
        "handlers": {"stderr": {"class": "logging.StreamHandler", "formatter": "default",
                                "stream": "ext://sys.stderr"}},
        "root": {"level": level, "handlers": ["stderr"]},
        "loggers": {
            "uvicorn": {"level": level, "handlers": [], "propagate": True},
            "uvicorn.error": {"level": level, "handlers": [], "propagate": True},
            "uvicorn.access": {"level": level, "handlers": [], "propagate": True, "filters": ["drop_probes", "strip_query"]},
        },
    }


def configure_logging(level: str = "INFO") -> None:
    level = (level or "INFO").upper()
    if not isinstance(logging.getLevelName(level), int):  # unknown name: fall back instead of failing
        level = "INFO"
    logging.config.dictConfig(logging_config(level))
