"""
XClaw Tool Registry — the foundation of v3's agentic architecture.

Tools are Python functions decorated with @registry.tool().
The registry auto-generates OpenAI-compatible JSON schemas from type hints,
handles sync/async dispatch, and injects shared resources (session_id, context).

Usage:
    registry = ToolRegistry()

    @registry.tool("Search the web for current information")
    async def web_search(query: str, max_results: int = 5) -> str:
        ...

    # LLM-facing schema
    tools_json = registry.as_openai_tools()

    # Execute a tool call from LLM response
    result = await registry.call("web_search", '{"query": "OpenAI news"}', session_id="s1")
"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, get_args, get_origin, get_type_hints

logger = logging.getLogger(__name__)

# Parameters injected at call-time, stripped from the public schema
_HIDDEN_PARAMS = frozenset({"session_id", "ctx", "_session_id"})


@dataclass
class ToolCall:
    """Represents a single tool invocation from the LLM."""
    id: str
    name: str
    arguments: str   # raw JSON string


@dataclass
class ToolResponse:
    """Result of executing a ToolCall."""
    tool_call_id: str
    name: str
    content: str


def _py_to_json_type(annotation: Any) -> dict:
    """Convert a Python type annotation to a JSON Schema fragment."""
    origin = get_origin(annotation)
    args = get_args(annotation)

    # Optional[X] → unwrap
    if origin is type(None):
        return {"type": "null"}

    import types
    if origin is types.UnionType or str(origin) in {"typing.Union"}:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _py_to_json_type(non_none[0])

    mapping = {
        str: {"type": "string"},
        int: {"type": "integer"},
        float: {"type": "number"},
        bool: {"type": "boolean"},
        list: {"type": "array"},
        dict: {"type": "object"},
    }
    if annotation in mapping:
        return mapping[annotation]
    if origin is list:
        return {"type": "array"}
    if origin is dict:
        return {"type": "object"}
    return {"type": "string"}   # safe default


class ToolRegistry:
    """
    Registry of callable tools exposed to the LLM.

    Attributes:
        _schemas: name → OpenAI function schema
        _handlers: name → callable (sync or async)
        _bound_args: name → dict of pre-bound keyword args (for resource injection)
    """

    def __init__(self) -> None:
        self._schemas: dict[str, dict] = {}
        self._handlers: dict[str, Callable] = {}
        self._bound_args: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def tool(
        self,
        description: str = "",
        *,
        name: str | None = None,
        hidden: bool = False,
    ) -> Callable:
        """
        Decorator to register a function as an LLM-callable tool.

        @registry.tool("Search the web for current information")
        async def web_search(query: str, max_results: int = 5) -> str: ...
        """
        def decorator(fn: Callable) -> Callable:
            tool_name = name or fn.__name__
            schema = self._build_schema(fn, tool_name, description or (fn.__doc__ or "").strip())
            self._schemas[tool_name] = schema
            self._handlers[tool_name] = fn
            self._bound_args[tool_name] = {}
            if not hidden:
                logger.debug("Tool registered: %s", tool_name)
            return fn
        return decorator

    def register(
        self,
        fn: Callable,
        description: str = "",
        name: str | None = None,
        bound_kwargs: dict | None = None,
    ) -> None:
        """Register a function imperatively (alternative to decorator)."""
        tool_name = name or fn.__name__
        schema = self._build_schema(fn, tool_name, description or (fn.__doc__ or "").strip())
        self._schemas[tool_name] = schema
        self._handlers[tool_name] = fn
        self._bound_args[tool_name] = bound_kwargs or {}

    # ------------------------------------------------------------------
    # LLM interface
    # ------------------------------------------------------------------

    def as_openai_tools(self) -> list[dict]:
        """Return tool schemas in OpenAI function-calling format."""
        return [{"type": "function", "function": s} for s in self._schemas.values()]

    def tool_names(self) -> list[str]:
        return list(self._schemas.keys())

    def unregister(self, name: str) -> bool:
        """Remove a tool from the registry. Returns True if it existed."""
        existed = name in self._schemas
        self._schemas.pop(name, None)
        self._handlers.pop(name, None)
        self._bound_args.pop(name, None)
        return existed

    def as_text_list(self) -> str:
        """Describe all tools in plain text (for prompt-based fallback)."""
        lines = []
        for s in self._schemas.values():
            params = ", ".join(
                f"{p}: {v.get('type', 'string')}" + ("?" if p not in s.get("required", []) else "")
                for p, v in s.get("parameters", {}).get("properties", {}).items()
            )
            lines.append(f"- {s['name']}({params}) → {s['description']}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def call(self, name: str, arguments: str, session_id: str = "") -> str:
        """Execute a tool by name with JSON arguments. Always returns a string."""
        handler = self._handlers.get(name)
        if handler is None:
            return f"[tool error] Unknown tool '{name}'. Available: {', '.join(self._handlers)}"

        try:
            kwargs: dict = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError as exc:
            return f"[tool error] Bad JSON arguments: {exc}"

        # Inject bound resources
        kwargs.update(self._bound_args.get(name, {}))

        # Inject session_id if the function expects it
        sig = inspect.signature(handler)
        if "session_id" in sig.parameters:
            kwargs["session_id"] = session_id

        try:
            result = handler(**kwargs)
            if inspect.isawaitable(result):
                result = await result
            return str(result) if result is not None else "(no output)"
        except TypeError as exc:
            return f"[tool error] {name} called with wrong arguments: {exc}"
        except Exception as exc:  # noqa: BLE001
            logger.exception("[tool] %s raised", name)
            return f"[tool error] {name} failed: {exc}"

    async def call_many(self, calls: list[ToolCall], session_id: str = "") -> list[ToolResponse]:
        """Execute multiple tool calls concurrently and return ordered responses."""
        import asyncio
        results = await asyncio.gather(
            *[self.call(c.name, c.arguments, session_id) for c in calls],
            return_exceptions=True,
        )
        responses = []
        for call, result in zip(calls, results):
            content = str(result) if not isinstance(result, Exception) else f"[tool error] {result}"
            responses.append(ToolResponse(tool_call_id=call.id, name=call.name, content=content))
        return responses

    # ------------------------------------------------------------------
    # Schema generation
    # ------------------------------------------------------------------

    def _build_schema(self, fn: Callable, name: str, description: str) -> dict:
        try:
            hints = get_type_hints(fn)
        except Exception:
            hints = {}

        sig = inspect.signature(fn)
        properties: dict = {}
        required: list[str] = []

        for param_name, param in sig.parameters.items():
            if param_name in _HIDDEN_PARAMS or param_name == "self":
                continue
            annotation = hints.get(param_name, str)
            prop = _py_to_json_type(annotation)
            # Include docstring-based description if available (future: parse from __doc__)
            properties[param_name] = prop
            if param.default is inspect.Parameter.empty:
                required.append(param_name)

        return {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        }
