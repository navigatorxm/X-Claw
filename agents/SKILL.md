# How to Add a New XClaw Agent (Skill)

XClaw is built to be extended. Adding a new agent takes < 15 minutes.

---

## 1. Create the agent file

Create `agents/<your_skill>.py` following this template:

```python
# agents/my_skill.py
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from brain.llm_router import LLMRouter


class MySkillAgent:
    name = "my_skill"          # ← must be unique, lowercase, no spaces

    def __init__(self, llm: "LLMRouter") -> None:
        self._llm = llm

    async def run(self, action: str, params: dict, session_id: str) -> str:
        """
        action  — what Navigator wants done (free-text from the LLM plan)
        params  — structured parameters extracted by the Commander
        returns — plain-text result shown to Navigator
        """
        # Your logic here
        prompt = f"Do: {action}\nParams: {params}"
        return await self._llm.complete(prompt, session_id=session_id)
```

Rules:
- `name` must be a single lowercase string (used as the agent identifier in plans).
- `run()` must be `async` and return a `str`.
- Keep agents focused — one domain per file.

---

## 2. Register the agent in `main.py`

```python
from agents.my_skill import MySkillAgent

router.register(MySkillAgent(llm=llm_router))
```

---

## 3. Tell the Commander it exists

Add your agent name to the `PLAN_PROMPT` in `core/commander.py`:

```
Available agents: research, content, leads, tasks, markets, code, my_skill
```

---

## 4. Done

Restart XClaw. The LLM will now route relevant tasks to your new agent automatically.

---

## Agent Guidelines

| Rule | Why |
|------|-----|
| Never mutate shared state directly | Use `Memory` via dependency injection |
| Always handle exceptions inside `run()` | Prevents one agent from killing the whole plan |
| Return structured markdown | Makes results readable across Telegram / Web / CLI |
| Log at `INFO` level for major actions | Keeps `memory/logs/` useful |
| Keep prompts in module-level constants | Easy to tune without touching logic |

---

## Example: Social Media Agent

```python
class SocialAgent:
    name = "social"

    async def run(self, action, params, session_id):
        if "post" in action.lower():
            content = params.get("content", "")
            platform = params.get("platform", "twitter")
            # integrate with platform API
            return f"Posted to {platform}: {content[:50]}..."
        return "Unknown social action."
```
