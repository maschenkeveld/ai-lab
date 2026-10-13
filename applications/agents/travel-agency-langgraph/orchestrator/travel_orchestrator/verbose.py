"""Optional live console tracing of each agent run.

Prints every tool call this orchestrator makes (and its result) to stdout via the standard
logger, so a run's progress is visible from whichever terminal is running this service -
useful for local dev and demos. Purely cosmetic: it observes the run through LangChain's
callback system and never affects agent behavior. Toggle off with VERBOSE=false.
"""

import json
import logging
import os
from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler
from langgraph.errors import GraphInterrupt

VERBOSE = os.getenv("VERBOSE", "true").lower() in ("1", "true", "yes")

logger = logging.getLogger("travel_orchestrator.trace")


def truncate(value: Any, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class ConsoleTraceCallback(AsyncCallbackHandler):
    """Logs each tool call's name/input on start and its result on end."""

    async def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, tags=None, metadata=None, inputs=None, **kwargs):
        name = serialized.get("name", "tool")
        logger.info("  -> %s(%s)", name, truncate(inputs or input_str))

    async def on_tool_end(self, output, *, run_id, parent_run_id=None, tags=None, **kwargs):
        text = getattr(output, "content", output)
        logger.info("  <- %s", truncate(text))

    async def on_tool_error(self, error, *, run_id, parent_run_id=None, tags=None, **kwargs):
        # book_flight's interrupt() (see graph.py) pauses the run by raising GraphInterrupt
        # internally - LangGraph's own control-flow signal for "stop here, wait for a
        # decision," not a real failure. Without this check it would print as "tool error",
        # which reads as something having gone wrong in a demo when the pause is in fact the
        # entire point of the human-approval gate.
        if isinstance(error, GraphInterrupt):
            logger.info("  paused: awaiting human approval")
            return
        logger.info("  !! tool error: %s", error)


CALLBACKS = [ConsoleTraceCallback()] if VERBOSE else []
