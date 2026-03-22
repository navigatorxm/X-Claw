"""
XClaw Code Agent — code generation, explanation, review, and safe execution.

Supported actions:
  - "generate"  → generate code for params["task"] in params.get("language","python")
  - "explain"   → explain params["code"]
  - "review"    → review and suggest improvements for params["code"]
  - "execute"   → run params["code"] in a sandboxed subprocess (Python only)
  - "debug"     → diagnose params["error"] in the context of params.get("code","")
"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from brain.llm_router import LLMRouter

logger = logging.getLogger(__name__)

_GENERATE_PROMPT = """\
Write clean, well-commented {language} code to accomplish the following task.
Include docstrings and type hints where appropriate.

TASK: {task}
"""

_EXPLAIN_PROMPT = """\
Explain the following code clearly. Cover: what it does, how it works,
potential issues, and any improvements you'd suggest.

CODE:
```
{code}
```
"""

_REVIEW_PROMPT = """\
Review the following code for correctness, style, security, and performance.
List specific issues and suggest improvements with code snippets.

CODE:
```
{code}
```
"""

_DEBUG_PROMPT = """\
Help debug this error. Explain the root cause and provide a corrected version.

ERROR: {error}

CODE:
```
{code}
```
"""

# Execution timeout in seconds
_EXEC_TIMEOUT = 10


class CodeAgent:
    name = "code"

    def __init__(self, llm: "LLMRouter") -> None:
        self._llm = llm

    async def run(self, action: str, params: dict, session_id: str) -> str:
        a = action.lower()

        if "generate" in a or "write" in a or "create" in a or "build" in a:
            return await self._generate(
                params.get("task", action),
                params.get("language", "python"),
                session_id,
            )

        if "explain" in a:
            return await self._explain(params.get("code", action), session_id)

        if "review" in a or "audit" in a:
            return await self._review(params.get("code", action), session_id)

        if "execute" in a or "run" in a:
            return await self._execute(params.get("code", ""))

        if "debug" in a or "fix" in a:
            return await self._debug(
                params.get("error", action),
                params.get("code", ""),
                session_id,
            )

        return await self._generate(params.get("task", action), "python", session_id)

    async def _generate(self, task: str, language: str, session_id: str) -> str:
        logger.info("[code] generate: %s (%s)", task[:60], language)
        prompt = _GENERATE_PROMPT.format(task=task, language=language)
        return await self._llm.complete(prompt, session_id=session_id)

    async def _explain(self, code: str, session_id: str) -> str:
        return await self._llm.complete(_EXPLAIN_PROMPT.format(code=code[:4000]), session_id=session_id)

    async def _review(self, code: str, session_id: str) -> str:
        return await self._llm.complete(_REVIEW_PROMPT.format(code=code[:4000]), session_id=session_id)

    async def _debug(self, error: str, code: str, session_id: str) -> str:
        prompt = _DEBUG_PROMPT.format(error=error, code=code[:3000])
        return await self._llm.complete(prompt, session_id=session_id)

    async def _execute(self, code: str) -> str:
        """
        Execute Python code in a subprocess with a strict timeout.
        WARNING: This is a basic sandbox. For production, use a proper
        container-level sandbox (e.g. gVisor, Firecracker).
        """
        if not code.strip():
            return "No code provided."

        with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w") as f:
            f.write(code)
            tmp = Path(f.name)

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(tmp),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_EXEC_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                return f"Execution timed out after {_EXEC_TIMEOUT}s."

            output = stdout.decode(errors="replace")
            errors = stderr.decode(errors="replace")
            if errors:
                return f"STDOUT:\n{output}\n\nSTDERR:\n{errors}"
            return output or "(no output)"
        finally:
            tmp.unlink(missing_ok=True)
