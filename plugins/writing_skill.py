"""
XClaw Skill: Writing Assistant
Structured writing, editing, and content utilities.
"""

from __future__ import annotations
import re
from datetime import datetime, timezone

PLUGIN_META = {
    "name":               "writing_skill",
    "display_name":       "Writing Assistant",
    "description":        "Draft documents, edit text, summarise, translate, and format content",
    "version":            "1.0.0",
    "category":           "writing",
    "tags":               ["writing", "editing", "content"],
    "enabled_by_default": True,
    "requires":           [],
}


async def word_count(text: str) -> str:
    """Count words, sentences, paragraphs, and estimate reading time for the given text."""
    words = len(text.split())
    sentences = len(re.findall(r'[.!?]+', text)) or 1
    paragraphs = len([p for p in text.split('\n\n') if p.strip()])
    chars = len(text)
    read_min = max(1, round(words / 200))
    return (f"Words: {words} | Sentences: {sentences} | Paragraphs: {paragraphs} | "
            f"Characters: {chars} | Reading time: ~{read_min} min")


async def extract_key_points(text: str, max_points: int = 7) -> str:
    """Extract key bullet points from a block of text using heuristics (no LLM call)."""
    # Split into sentences
    sentences = re.split(r'(?<=[.!?])\s+', text)
    # Score by: length (not too short/long), contains numbers, starts fresh paragraph
    scored = []
    for s in sentences:
        s = s.strip()
        if len(s) < 20 or len(s) > 300:
            continue
        score = 0
        if re.search(r'\d', s):
            score += 2
        if re.search(r'\b(important|key|critical|must|should|will|result|because|therefore)\b', s, re.I):
            score += 2
        if s[0].isupper():
            score += 1
        score += min(len(s.split()) / 20, 2)
        scored.append((score, s))
    scored.sort(key=lambda x: -x[0])
    top = [s for _, s in scored[:max_points]]
    if not top:
        return "Could not extract key points (text may be too short or unstructured)."
    return "\n".join(f"• {s}" for s in top)


async def clean_text(text: str) -> str:
    """Clean and normalise text: fix whitespace, remove duplicate lines, fix common typos."""
    # Normalize whitespace
    text = re.sub(r'\t', ' ', text)
    text = re.sub(r' {2,}', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    # Remove duplicate consecutive lines
    lines = text.split('\n')
    seen, out = set(), []
    for line in lines:
        stripped = line.strip()
        if stripped and stripped in seen:
            continue
        if stripped:
            seen.add(stripped)
        out.append(line)
    return '\n'.join(out).strip()


async def generate_template(template_type: str, context: str = "") -> str:
    """
    Generate a document template. template_type: email|report|proposal|meeting_notes|tweet|linkedin_post|readme.
    context: brief description to customise the template.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    templates = {
        "email": f"""Subject: [Your subject here]

Dear [Name],

I hope this message finds you well.

[Opening sentence relevant to: {context or 'your topic'}]

[Main body — keep it to 2-3 short paragraphs]

[Call to action / next step]

Best regards,
[Your name]""",

        "report": f"""# [Report Title]
*{now} | Prepared by: [Name]*

## Executive Summary
[2-3 sentences covering the key finding related to: {context or 'your topic'}]

## Background
[Why this report was written]

## Findings
1. [Finding one]
2. [Finding two]
3. [Finding three]

## Recommendations
- [Recommendation 1]
- [Recommendation 2]

## Next Steps
| Action | Owner | Due Date |
|--------|-------|----------|
| [task] | [who] | [date] |
""",

        "proposal": f"""# Proposal: {context or '[Title]'}
*{now}*

## Problem Statement
[What problem does this solve?]

## Proposed Solution
[High-level description]

## Benefits
- [Benefit 1]
- [Benefit 2]

## Timeline
| Phase | Duration | Deliverable |
|-------|----------|-------------|
| Phase 1 | [time] | [output] |

## Investment Required
[Cost / resource estimate]

## Next Steps
[What happens if approved?]
""",

        "meeting_notes": f"""# Meeting Notes — {now}
**Topic:** {context or '[Meeting topic]'}
**Attendees:** [Names]
**Facilitator:** [Name]

## Agenda
1. [Item 1]
2. [Item 2]

## Discussion
### [Topic 1]
- [Key point]
- [Decision made]

## Action Items
| Item | Owner | Due |
|------|-------|-----|
| [Task] | [Name] | [Date] |

## Next Meeting
Date: [Date] | Time: [Time]
""",

        "tweet": f"""📌 {context or '[Your main point]'}

[Expand in 1-2 sentences]

#XClaw #AI [relevant hashtags]

[Optional: link or CTA]""",

        "linkedin_post": f"""🚀 {context or '[Hook — make it attention-grabbing]'}

[Problem or observation in 1-2 lines]

Here's what I learned:

→ [Insight 1]
→ [Insight 2]
→ [Insight 3]

[Closing thought or question to engage readers]

What's your take? Drop it in the comments 👇

#[Industry] #[Topic] #[Skill]""",

        "readme": f"""# {context or 'Project Name'}

> One-line description of what this does.

## Quick Start

```bash
# Install
pip install {(context or 'package').lower().replace(' ', '-')}

# Run
python main.py
```

## Features
- ✅ [Feature 1]
- ✅ [Feature 2]

## Configuration
Copy `.env.example` to `.env` and fill in your values.

## Contributing
PRs welcome. Open an issue first for large changes.

## License
MIT
""",
    }

    result = templates.get(template_type.lower())
    if result:
        return result
    available = ", ".join(templates.keys())
    return f"Unknown template type: {template_type!r}. Available: {available}"
