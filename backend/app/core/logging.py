"""Logging JSON terstruktur. Setiap baris log selalu punya request_id dan tenant_id."""

import logging
import sys
from typing import TextIO
from uuid import UUID

import structlog
from structlog.types import EventDict, Processor, WrappedLogger

CONTEXT_KEYS = ("request_id", "tenant_id")


def _add_default_context(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    for key in CONTEXT_KEYS:
        event_dict.setdefault(key, None)
    return event_dict


def configure_logging(service: str, level: str = "INFO", stream: TextIO | None = None) -> None:
    """Arahkan structlog dan logging stdlib (uvicorn, celery, sqlalchemy) ke output JSON."""
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        _add_default_context,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.CallsiteParameterAdder(
            {structlog.processors.CallsiteParameter.MODULE}
        ),
    ]

    structlog.configure(
        processors=[
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            lambda _, __, event_dict: {"service": service, **event_dict},
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    # Log uvicorn lewat root handler. Access log diganti log request dari middleware.
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True


def bind_request_context(request_id: str) -> None:
    """Reset context per request. tenant_id diisi belakangan oleh auth."""
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id, tenant_id=None)


def bind_tenant(tenant_id: UUID) -> None:
    structlog.contextvars.bind_contextvars(tenant_id=str(tenant_id))


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
