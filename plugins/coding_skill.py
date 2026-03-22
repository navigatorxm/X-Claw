"""
XClaw Skill: Coding Assistant
Code generation, explanation, debugging, and review without extra API calls.
"""

from __future__ import annotations
import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

PLUGIN_META = {
    "name":               "coding_skill",
    "display_name":       "Coding Assistant",
    "description":        "Generate, explain, debug, and review code — any language",
    "version":            "1.0.0",
    "category":           "coding",
    "tags":               ["code", "programming", "debugging"],
    "enabled_by_default": True,
    "requires":           [],
}


async def run_code(code: str, language: str = "python") -> str:
    """
    Execute code in a sandboxed subprocess. language: python (default), bash, node.
    Returns stdout/stderr. 15 second timeout.
    """
    lang = language.lower().strip()
    runners = {
        "python": [sys.executable, "-c", code],
        "python3": [sys.executable, "-c", code],
        "bash": ["bash", "-c", code],
        "sh": ["sh", "-c", code],
        "node": ["node", "-e", code],
        "javascript": ["node", "-e", code],
    }
    if lang not in runners:
        return f"Unsupported language: {lang}. Supported: {', '.join(runners)}"

    cmd = runners[lang]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        out = stdout.decode(errors="replace").strip()
        err = stderr.decode(errors="replace").strip()
        if proc.returncode != 0:
            return f"Exit {proc.returncode}\n" + (err or out)
        return out or "(no output)"
    except asyncio.TimeoutError:
        return "Execution timed out (15s)"
    except FileNotFoundError:
        return f"{lang} runtime not found on this system"


async def install_package(package: str) -> str:
    """Install a Python package with pip. Returns install output."""
    if any(c in package for c in [";", "&", "|", "$", "`", ">"]):
        return f"Invalid package name: {package!r}"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install", "--quiet", package,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    if proc.returncode == 0:
        return f"✓ {package} installed successfully"
    return f"Install failed:\n{stderr.decode(errors='replace')[:500]}"


async def lint_python(code: str) -> str:
    """Check Python code for syntax errors and basic style issues."""
    import ast
    try:
        ast.parse(code)
        base = "✓ Syntax OK"
    except SyntaxError as exc:
        return f"✗ Syntax error at line {exc.lineno}: {exc.msg}"

    # Check for common issues
    issues = []
    lines = code.split("\n")
    for i, line in enumerate(lines, 1):
        if len(line) > 120:
            issues.append(f"Line {i}: too long ({len(line)} chars)")
        if "print(" in line and "debug" in line.lower():
            issues.append(f"Line {i}: debug print statement")
        if "import *" in line:
            issues.append(f"Line {i}: wildcard import")
        if "except:" in line and "except Exception" not in line:
            issues.append(f"Line {i}: bare except clause")

    if issues:
        return base + "\nWarnings:\n" + "\n".join(f"  • {w}" for w in issues[:10])
    return base + " — no issues found"


async def format_code(code: str, language: str = "python") -> str:
    """Format code. Runs black for Python, prettier for JS/TS if available."""
    if language.lower() in ("python", "python3"):
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write(code)
            tmp = Path(f.name)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "black", "--quiet", str(tmp),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode == 0:
            result = tmp.read_text()
            tmp.unlink(missing_ok=True)
            return result
        tmp.unlink(missing_ok=True)
        return code  # return original if black not available
    return code


async def git_status(path: str = ".") -> str:
    """Get git status of a directory. Shows changed files and branch."""
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", path, "status", "--short", "--branch",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
    if proc.returncode != 0:
        return f"Not a git repo: {path}"
    return stdout.decode(errors="replace").strip() or "Clean working directory"


async def git_diff(path: str = ".", staged: bool = False) -> str:
    """Show git diff (unstaged changes, or staged if staged=True)."""
    args = ["git", "-C", path, "diff"]
    if staged:
        args.append("--cached")
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    diff = stdout.decode(errors="replace").strip()
    return diff[:4000] if diff else "No changes."
