"""
XClaw Skill: Productivity
Daily briefs, task breakdowns, time blocking, and habit tracking.
"""

from __future__ import annotations
import json
import re
from datetime import datetime, timezone, timedelta

PLUGIN_META = {
    "name":               "productivity_skill",
    "display_name":       "Productivity",
    "description":        "Daily briefs, task breakdown, Pomodoro timers, habit tracking, goal setting",
    "version":            "1.0.0",
    "category":           "productivity",
    "tags":               ["productivity", "tasks", "goals", "habits"],
    "enabled_by_default": True,
    "requires":           [],
}


async def break_down_task(task: str, max_steps: int = 7) -> str:
    """
    Break a large task into concrete, actionable sub-steps.
    Uses heuristic decomposition — no LLM call needed for structure.
    task: the task description. max_steps: max sub-steps to generate (default 7).
    """
    # Heuristic: identify task type and suggest framework
    task_lower = task.lower()
    steps = []

    if any(w in task_lower for w in ["build", "create", "make", "develop", "write"]):
        steps = [
            "Define the goal and success criteria clearly",
            "Research existing solutions or references",
            "Outline the structure / architecture",
            "Build a minimal working version (MVP)",
            "Test and iterate on the core functionality",
            "Polish, document, and review",
            "Deploy / deliver / share",
        ]
    elif any(w in task_lower for w in ["research", "analyse", "analyze", "study", "investigate"]):
        steps = [
            "Define the research question precisely",
            "Identify the best sources (web, papers, experts)",
            "Gather raw information",
            "Filter and verify key facts",
            "Identify patterns and insights",
            "Synthesise into a summary",
            "Document conclusions and citations",
        ]
    elif any(w in task_lower for w in ["fix", "debug", "solve", "repair"]):
        steps = [
            "Reproduce the problem consistently",
            "Identify the root cause",
            "Research possible solutions",
            "Implement the most promising fix",
            "Test that the fix resolves the issue",
            "Verify no regressions",
            "Document the fix",
        ]
    elif any(w in task_lower for w in ["plan", "organise", "organize", "prepare"]):
        steps = [
            "Define the objective and deadline",
            "List all required resources and stakeholders",
            "Break into phases or milestones",
            "Assign ownership for each item",
            "Identify risks and blockers",
            "Set up tracking / check-ins",
            "Execute phase 1",
        ]
    else:
        steps = [
            "Clarify what 'done' looks like for this task",
            "Identify what you need before you start",
            "Break into the smallest actionable first step",
            "Complete first step and assess",
            "Continue iterating until complete",
            "Review and capture lessons learned",
        ]

    steps = steps[:max_steps]
    output = f"## Task Breakdown: {task}\n\n"
    output += "\n".join(f"{i+1}. {step}" for i, step in enumerate(steps))
    output += "\n\n*Tip: tackle step 1 now — momentum beats planning.*"
    return output


async def time_block_day(tasks: str, work_hours: int = 8) -> str:
    """
    Generate a time-blocked schedule for a list of tasks.
    tasks: comma or newline separated task list. work_hours: available hours (default 8).
    """
    task_list = [t.strip() for t in re.split(r'[,\n]+', tasks) if t.strip()]
    if not task_list:
        return "No tasks provided."

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    # Start at 9am local (approximate)
    start = now.replace(hour=9)
    end_time = start + timedelta(hours=work_hours)

    # Distribute time evenly with buffer
    per_task = max(30, (work_hours * 60 - len(task_list) * 15) // len(task_list))
    per_task = min(per_task, 90)  # cap at 90 min blocks

    lines = [f"## Time-Blocked Schedule ({start.strftime('%a %b %d')})\n"]
    current = start
    for task in task_list:
        if current >= end_time:
            lines.append("⚠️ Out of time — remaining tasks need another day or delegation")
            break
        block_end = current + timedelta(minutes=per_task)
        lines.append(f"**{current.strftime('%H:%M')} – {block_end.strftime('%H:%M')}** {task}")
        current = block_end + timedelta(minutes=15)  # 15 min buffer

    lines.append(f"\nTotal blocks: {len(task_list)} | Time per task: ~{per_task} min")
    return "\n".join(lines)


async def estimate_effort(task: str) -> str:
    """
    Estimate time and effort for a task using heuristics.
    Returns effort level (S/M/L/XL), estimated hours, and reasoning.
    """
    task_lower = task.lower()
    words = len(task.split())
    complexity_keywords = {
        "simple|quick|minor|small|update|fix typo|rename": ("S", 0.5, 2),
        "add|create|write|build|implement|configure": ("M", 2, 6),
        "design|develop|research|analyse|refactor|migrate": ("L", 6, 16),
        "rewrite|architect|launch|deploy|integrate|overhaul": ("XL", 16, 40),
    }
    size, low, high = "M", 2, 6
    for pattern, (s, l, h) in complexity_keywords.items():
        if re.search(pattern, task_lower):
            size, low, high = s, l, h
            break

    size_labels = {"S": "Small", "M": "Medium", "L": "Large", "XL": "Extra Large"}
    return (f"**Effort estimate: {size} ({size_labels[size]})**\n"
            f"Estimated time: {low}–{high} hours\n\n"
            f"Factors: task complexity, assumed no blockers, single person.\n"
            f"Confidence: medium — adjust based on your familiarity with the domain.")


async def morning_brief_template(focus_areas: str = "") -> str:
    """
    Generate a morning planning template. focus_areas: comma-separated areas to focus on today.
    """
    now = datetime.now(timezone.utc)
    day = now.strftime("%A, %B %d, %Y")
    areas = [a.strip() for a in focus_areas.split(",") if a.strip()] if focus_areas else []

    brief = f"""# 🌅 Morning Brief — {day}

## Top 3 Priorities Today
1. [ ] _______________________________________________
2. [ ] _______________________________________________
3. [ ] _______________________________________________

## Energy Level (1-10): ___
## One word for today: _______________

"""
    if areas:
        brief += "## Focus Areas\n"
        for area in areas:
            brief += f"- **{area}**: _______________________________________________\n"
        brief += "\n"

    brief += """## Schedule
| Time | Block |
|------|-------|
| 09:00 | Deep work |
| 11:00 | Meetings / comms |
| 13:00 | Lunch |
| 14:00 | Execution |
| 16:00 | Review + plan tomorrow |
| 17:00 | Shutdown ritual |

## Daily Intention
> _______________________________________________

---
*Use /tasks to see your full task list. Use /schedule for recurring reminders.*"""
    return brief
