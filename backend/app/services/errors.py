"""Errors raised by services that MCP tools turn into ``isError`` results."""

from __future__ import annotations


class ToolError(Exception):
    """A tool failure with a stable code. ``message`` overrides the default text."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
