"""
XClaw Plugin Manager — lightweight, file-based skill/plugin system.

Architecture
────────────
Every file in plugins/ that defines PLUGIN_META is a plugin.
A plugin exposes one or more async functions that become LLM-callable tools.

Plugin file contract:

    PLUGIN_META = {
        "name":              "my_skill",          # unique id
        "display_name":      "My Skill",
        "description":       "What this skill does",
        "version":           "1.0.0",
        "category":          "productivity",      # research|coding|writing|analysis|automation|productivity
        "tags":              ["tag1", "tag2"],
        "enabled_by_default": True,
        "requires":          [],                  # optional: ["playwright", "pandas"]
    }

    async def my_tool(param: str) -> str:
        \"\"\"Tool description the LLM reads to decide when to call it.\"\"\"
        ...

Features
────────
  • Hot-reload: scan plugins/ any time
  • Per-user enable/disable stored in SQLite
  • Dependency check: warns if a pip package is missing
  • Plugin can declare requires=[] — checked before loading
  • Dashboard exposes /plugins GET/POST to manage state
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.memory import Memory
    from core.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)

_PLUGINS_DIR = Path("plugins")
_BUILTIN_CATEGORY_ORDER = ["research", "coding", "writing", "analysis", "automation", "productivity"]


class PluginInfo:
    """Runtime state of a loaded plugin."""

    __slots__ = ("meta", "path", "tools", "loaded", "error", "enabled")

    def __init__(self, meta: dict, path: Path, tools: list[Any], error: str = "") -> None:
        self.meta = meta
        self.path = path
        self.tools = tools          # list of callable functions
        self.loaded = not error
        self.error = error
        self.enabled = meta.get("enabled_by_default", True)

    def to_dict(self) -> dict:
        return {
            "name": self.meta["name"],
            "display_name": self.meta.get("display_name", self.meta["name"]),
            "description": self.meta.get("description", ""),
            "version": self.meta.get("version", "0.0.0"),
            "category": self.meta.get("category", "misc"),
            "tags": self.meta.get("tags", []),
            "enabled": self.enabled,
            "loaded": self.loaded,
            "error": self.error,
            "tool_count": len(self.tools),
            "tool_names": [fn.__name__ for fn in self.tools],
        }


class PluginManager:
    """
    Discovers, loads, and manages XClaw skill plugins.

    Usage:
        pm = PluginManager(memory)
        pm.scan()                          # discover plugins/
        pm.register_all(tool_registry)     # register enabled plugins
        pm.list_plugins()                  # → list[dict]
        pm.set_enabled("my_skill", False)  # disable a plugin
    """

    def __init__(self, memory: "Memory | None" = None, plugins_dir: str | Path = _PLUGINS_DIR) -> None:
        self._memory = memory
        self._dir = Path(plugins_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._plugins: dict[str, PluginInfo] = {}     # name → PluginInfo
        self._registry: "ToolRegistry | None" = None
        if memory:
            self._ensure_schema()

    # ── Schema ──────────────────────────────────────────────────────────────

    def _ensure_schema(self) -> None:
        conn = self._memory._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS plugin_state (
                name        TEXT PRIMARY KEY,
                enabled     INTEGER NOT NULL DEFAULT 1,
                updated_at  TEXT    NOT NULL
            );
        """)
        conn.commit()

    def _load_db_state(self) -> dict[str, bool]:
        if not self._memory:
            return {}
        with self._memory._conn() as conn:
            rows = conn.execute("SELECT name, enabled FROM plugin_state").fetchall()
        return {r["name"]: bool(r["enabled"]) for r in rows}

    def _save_db_state(self, name: str, enabled: bool) -> None:
        if not self._memory:
            return
        now = self._memory._now()
        with self._memory._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO plugin_state (name, enabled, updated_at) VALUES (?,?,?)",
                (name, int(enabled), now),
            )

    # ── Discovery ────────────────────────────────────────────────────────────

    def scan(self) -> int:
        """Scan plugins/ directory and load all valid plugins. Returns loaded count."""
        db_state = self._load_db_state()
        count = 0

        for path in sorted(self._dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                info = self._load_plugin_file(path)
                name = info.meta["name"]
                # Restore persisted enable/disable state
                if name in db_state:
                    info.enabled = db_state[name]
                self._plugins[name] = info
                if info.loaded:
                    count += 1
                    logger.info("[plugins] loaded %s v%s (%d tools)",
                                name, info.meta.get("version", "?"), len(info.tools))
                else:
                    logger.warning("[plugins] failed to load %s: %s", path.name, info.error)
            except Exception as exc:
                logger.warning("[plugins] error loading %s: %s", path.name, exc)

        logger.info("[plugins] scanned %d plugins (%d loaded)", len(self._plugins), count)
        return count

    def _load_plugin_file(self, path: Path) -> PluginInfo:
        """Import a plugin file and extract PLUGIN_META + tool functions."""
        spec = importlib.util.spec_from_file_location(f"xclaw_plugin_{path.stem}", path)
        mod = importlib.util.module_from_spec(spec)

        # Check for PLUGIN_META before full import
        source = path.read_text(encoding="utf-8")
        if "PLUGIN_META" not in source:
            raise ValueError("no PLUGIN_META defined")

        try:
            spec.loader.exec_module(mod)
        except ImportError as exc:
            # Missing optional dependency — create stub
            meta = getattr(mod, "PLUGIN_META", {"name": path.stem, "display_name": path.stem, "description": ""})
            return PluginInfo(meta, path, [], error=f"Missing dependency: {exc}")

        meta = getattr(mod, "PLUGIN_META", None)
        if not meta or not isinstance(meta, dict) or "name" not in meta:
            raise ValueError("PLUGIN_META must be a dict with 'name' key")

        # Check declared requirements
        for req in meta.get("requires", []):
            try:
                __import__(req)
            except ImportError:
                return PluginInfo(meta, path, [], error=f"Missing requirement: pip install {req}")

        # Collect async tool functions (not starting with _)
        tools = []
        for attr in dir(mod):
            if attr.startswith("_") or attr == "PLUGIN_META":
                continue
            fn = getattr(mod, attr)
            if callable(fn) and inspect.isfunction(fn):
                tools.append(fn)

        return PluginInfo(meta, path, tools)

    # ── Registration ─────────────────────────────────────────────────────────

    def register_all(self, registry: "ToolRegistry") -> int:
        """Register all enabled plugins into the tool registry. Returns tool count."""
        self._registry = registry
        total = 0
        for info in self._plugins.values():
            if info.enabled and info.loaded:
                total += self._register_plugin(info, registry)
        logger.info("[plugins] registered %d plugin tools", total)
        return total

    def _register_plugin(self, info: PluginInfo, registry: "ToolRegistry") -> int:
        count = 0
        for fn in info.tools:
            try:
                doc = (fn.__doc__ or "").strip().split("\n")[0]
                registry.register(fn, description=doc, name=fn.__name__)
                count += 1
            except Exception as exc:
                logger.warning("[plugins] could not register %s.%s: %s",
                               info.meta["name"], fn.__name__, exc)
        return count

    # ── Runtime management ────────────────────────────────────────────────────

    def set_enabled(self, name: str, enabled: bool) -> bool:
        """Enable or disable a plugin by name. Returns True if found."""
        if name not in self._plugins:
            return False
        info = self._plugins[name]
        info.enabled = enabled
        self._save_db_state(name, enabled)

        if self._registry:
            if enabled and info.loaded:
                self._register_plugin(info, self._registry)
            elif not enabled:
                # Unregister tools
                for fn in info.tools:
                    try:
                        self._registry.unregister(fn.__name__)
                    except Exception:
                        pass

        logger.info("[plugins] %s %s", name, "enabled" if enabled else "disabled")
        return True

    def reload(self, name: str) -> bool:
        """Hot-reload a single plugin from disk."""
        if name not in self._plugins:
            return False
        path = self._plugins[name].path
        # Remove old module from sys.modules
        mod_key = f"xclaw_plugin_{path.stem}"
        sys.modules.pop(mod_key, None)
        try:
            new_info = self._load_plugin_file(path)
            new_info.enabled = self._plugins[name].enabled
            self._plugins[name] = new_info
            if self._registry and new_info.enabled:
                self._register_plugin(new_info, self._registry)
            return True
        except Exception as exc:
            logger.warning("[plugins] reload failed for %s: %s", name, exc)
            return False

    # ── Queries ────────────────────────────────────────────────────────────────

    def list_plugins(self) -> list[dict]:
        """Return serialisable list of all discovered plugins."""
        order = {cat: i for i, cat in enumerate(_BUILTIN_CATEGORY_ORDER)}
        plugins = list(self._plugins.values())
        plugins.sort(key=lambda p: (order.get(p.meta.get("category", "misc"), 99), p.meta["name"]))
        return [p.to_dict() for p in plugins]

    def get_plugin(self, name: str) -> dict | None:
        p = self._plugins.get(name)
        return p.to_dict() if p else None

    def categories(self) -> list[str]:
        cats = set(p.meta.get("category", "misc") for p in self._plugins.values())
        return sorted(cats)
