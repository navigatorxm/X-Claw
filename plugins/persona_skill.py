"""
XClaw Skill: Persona Manager
Each XClaw instance can have its own identity, voice, and social presence.

A persona defines:
  - Name, bio, tone of voice
  - Social media handles and content style
  - Signature hashtags and branding
  - Goals and areas of expertise

Stored in memory/persona.json — persists across restarts.
The LLM reads the persona to shape its responses automatically.

Social media: XClaw generates branded content with your persona's
voice and hashtags — ready to post on X, LinkedIn, Farcaster, etc.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_META = {
    "name":               "persona_skill",
    "display_name":       "Persona & Identity",
    "description":        "Manage XClaw's identity, voice, social media presence, and branding",
    "version":            "1.0.0",
    "category":           "productivity",
    "tags":               ["persona", "identity", "social", "branding"],
    "enabled_by_default": True,
    "requires":           [],
}

_PERSONA_FILE = Path("memory/persona.json")

_DEFAULT_PERSONA = {
    "name":        "XClaw",
    "owner":       "Navigator",
    "bio":         "AI executive assistant. Researches, executes, reports back.",
    "tone":        "precise, direct, action-oriented",
    "expertise":   ["AI", "productivity", "research", "technology"],
    "social": {
        "twitter_handle":   "",
        "linkedin_url":     "",
        "farcaster_handle": "",
        "github_handle":    "",
    },
    "hashtags":    ["#XClaw", "#AI", "#BuildInPublic"],
    "catchphrase": "Navigator gives intent → XClaw executes → Reports back.",
    "updated_at":  "",
}


def _load() -> dict:
    if _PERSONA_FILE.exists():
        try:
            return json.loads(_PERSONA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return dict(_DEFAULT_PERSONA)


def _save(persona: dict) -> None:
    _PERSONA_FILE.parent.mkdir(parents=True, exist_ok=True)
    persona["updated_at"] = datetime.now(timezone.utc).isoformat()
    _PERSONA_FILE.write_text(json.dumps(persona, indent=2), encoding="utf-8")


async def get_persona() -> str:
    """Get the current XClaw persona/identity configuration."""
    p = _load()
    lines = [
        f"**Name:** {p['name']}",
        f"**Owner:** {p['owner']}",
        f"**Bio:** {p['bio']}",
        f"**Tone:** {p['tone']}",
        f"**Expertise:** {', '.join(p.get('expertise', []))}",
        f"**Catchphrase:** {p.get('catchphrase', '')}",
        f"**Hashtags:** {' '.join(p.get('hashtags', []))}",
    ]
    social = p.get("social", {})
    social_lines = [f"  {k}: {v}" for k, v in social.items() if v]
    if social_lines:
        lines.append("**Social:**\n" + "\n".join(social_lines))
    return "\n".join(lines)


async def set_persona(name: str = "", bio: str = "", tone: str = "",
                      expertise: str = "", catchphrase: str = "") -> str:
    """
    Update the XClaw persona. Only provided fields are updated.
    expertise: comma-separated list. tone: e.g. 'professional, witty, concise'.
    """
    p = _load()
    if name:         p["name"] = name
    if bio:          p["bio"] = bio
    if tone:         p["tone"] = tone
    if catchphrase:  p["catchphrase"] = catchphrase
    if expertise:    p["expertise"] = [e.strip() for e in expertise.split(",") if e.strip()]
    _save(p)
    return f"Persona updated. Name: {p['name']} | Bio: {p['bio'][:60]}…"


async def set_social_handle(platform: str, handle: str) -> str:
    """
    Set a social media handle for the persona.
    platform: twitter | linkedin | farcaster | github. handle: your username/URL.
    """
    platform = platform.lower().strip()
    key_map = {
        "twitter": "twitter_handle", "x": "twitter_handle",
        "linkedin": "linkedin_url",
        "farcaster": "farcaster_handle",
        "github": "github_handle",
    }
    if platform not in key_map:
        return f"Unknown platform: {platform}. Supported: {', '.join(key_map)}"
    p = _load()
    p.setdefault("social", {})[key_map[platform]] = handle
    _save(p)
    return f"{platform.title()} handle set: {handle}"


async def set_hashtags(hashtags: str) -> str:
    """
    Set branded hashtags for social media posts.
    hashtags: space or comma-separated, e.g. '#XClaw #AI #BuildInPublic'.
    """
    tags = [t.strip() for t in hashtags.replace(",", " ").split() if t.strip()]
    tags = [t if t.startswith("#") else f"#{t}" for t in tags]
    p = _load()
    p["hashtags"] = tags
    _save(p)
    return f"Hashtags updated: {' '.join(tags)}"


async def draft_social_post(platform: str, topic: str, style: str = "informative") -> str:
    """
    Generate a branded social media post draft using the persona's voice.
    platform: twitter|linkedin|farcaster. topic: what to post about.
    style: informative|announcement|question|story|thread.
    """
    p = _load()
    name = p.get("name", "XClaw")
    tone = p.get("tone", "direct")
    tags = " ".join(p.get("hashtags", ["#AI"])[:4])
    catchphrase = p.get("catchphrase", "")
    social = p.get("social", {})

    handle_map = {"twitter": "twitter_handle", "x": "twitter_handle",
                  "linkedin": "linkedin_url", "farcaster": "farcaster_handle"}
    handle = social.get(handle_map.get(platform.lower(), ""), "")

    platform_lower = platform.lower()

    styles = {
        "informative":    f"📌 {topic}\n\n[Key insight in 1-2 sentences — {tone} tone]\n\n[Supporting detail or stat]\n\n{tags}",
        "announcement":   f"🚀 Excited to share: {topic}\n\n[What this means and why it matters]\n\n[Call to action]\n\n{tags}",
        "question":       f"🤔 Question for the community:\n\n{topic}?\n\n[Your perspective in 1-2 lines]\n\nDrop your thoughts below 👇\n\n{tags}",
        "story":          f"🧵 A quick story about {topic}:\n\n1/ [The situation]\n\n2/ [What happened]\n\n3/ [What I learned]\n\n4/ Takeaway: [the lesson]\n\n{tags}",
        "thread":         f"🧵 Everything I know about {topic} (thread):\n\n1/ [Point 1]\n2/ [Point 2]\n3/ [Point 3]\n4/ [Conclusion]\n\n{tags}",
    }

    if platform_lower == "linkedin":
        post = (f"🚀 {topic}\n\n"
                f"[Hook: bold first line]\n\n"
                f"[Problem or context]\n\n"
                f"→ [Insight 1]\n→ [Insight 2]\n→ [Insight 3]\n\n"
                f"[Closing thought]\n\n"
                f"What's your experience with this? 👇\n\n"
                f"{tags}")
    else:
        post = styles.get(style, styles["informative"])

    header = f"**Draft {platform.title()} post** (persona: {name}, tone: {tone})"
    if handle:
        header += f" | handle: {handle}"
    if catchphrase and platform_lower != "linkedin":
        post += f"\n\n— {catchphrase}"

    return f"{header}\n\n---\n{post}"


async def persona_system_prompt() -> str:
    """
    Get the persona as a system prompt prefix — injected into LLM context.
    Called automatically at startup to shape XClaw's voice.
    """
    p = _load()
    if p == _DEFAULT_PERSONA or not p.get("bio"):
        return ""
    return (f"Your name is {p['name']} and you are an AI assistant for {p['owner']}. "
            f"{p['bio']} Your tone is {p['tone']}. "
            f"Your areas of expertise: {', '.join(p.get('expertise', []))}.")
