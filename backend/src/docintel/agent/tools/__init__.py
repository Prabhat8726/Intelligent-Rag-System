"""Controlled tools for the agent and MCP clients (Module 15)."""

from docintel.agent.tools.base import (
    NOT_FOUND,
    Caller,
    SideEffect,
    Tool,
    ToolEnvironment,
    ToolFailure,
    ToolResult,
    ToolScope,
)
from docintel.agent.tools.catalog import TOOLS
from docintel.agent.tools.registry import ToolRegistry, effective_permissions


def build_registry(environment: ToolEnvironment) -> ToolRegistry:
    return ToolRegistry(TOOLS, environment)


__all__ = [
    "NOT_FOUND",
    "TOOLS",
    "Caller",
    "SideEffect",
    "Tool",
    "ToolEnvironment",
    "ToolFailure",
    "ToolRegistry",
    "ToolResult",
    "ToolScope",
    "build_registry",
    "effective_permissions",
]
