"""
XClaw News & Information Tools — all free, no API keys required.

Tools:
  get_hacker_news    — HN top/new/ask/show stories (official Firebase API)
  get_wikipedia      — Wikipedia article summaries (REST API)
  get_weather        — worldwide weather via Open-Meteo (completely free)
  get_rss_feed       — read any RSS/Atom feed
  get_reddit_posts   — Reddit JSON API (public posts, no key)
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from typing import Literal

import httpx

logger = logging.getLogger(__name__)

_HN_API = "https://hacker-news.firebaseio.com/v0"
_WIKI_API = "https://en.wikipedia.org/api/rest_v1"
_GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
_WEATHER = "https://api.open-meteo.com/v1/forecast"
_UA = "XClaw/3.0 (github.com/navigatorxm/XClaw)"


async def get_hacker_news(category: str = "top", limit: int = 10) -> str:
    """
    Get Hacker News stories. category: top, new, best, ask, show, job.
    Returns title, URL, score, and comment count for each story.
    """
    valid = {"top", "new", "best", "ask", "show", "job"}
    cat = category.lower().replace(" ", "").rstrip("stories")
    if cat not in valid:
        cat = "top"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Fetch story IDs
            ids_resp = await client.get(f"{_HN_API}/{cat}stories.json")
            ids_resp.raise_for_status()
            ids = ids_resp.json()[:limit]

            # Fetch story details concurrently
            import asyncio
            async def fetch_story(sid: int) -> dict | None:
                try:
                    r = await client.get(f"{_HN_API}/item/{sid}.json")
                    return r.json()
                except Exception:
                    return None

            stories = await asyncio.gather(*[fetch_story(sid) for sid in ids])

        lines = [f"**Hacker News — {cat.title()} Stories:**\n"]
        for i, s in enumerate(stories, 1):
            if not s:
                continue
            url = s.get("url", f"https://news.ycombinator.com/item?id={s['id']}")
            score = s.get("score", 0)
            comments = s.get("descendants", 0)
            lines.append(f"{i}. **{s.get('title', 'No title')}**\n   ⬆️{score} 💬{comments} | {url}")

        return "\n".join(lines)
    except Exception as exc:
        return f"HN fetch failed: {exc}"


async def get_wikipedia(topic: str, lang: str = "en") -> str:
    """
    Get a Wikipedia summary for any topic. Returns the introduction and key facts.
    """
    # Normalise topic for URL
    topic_encoded = topic.strip().replace(" ", "_")
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": _UA, "Accept": "application/json"},
            timeout=10,
        ) as client:
            resp = await client.get(
                f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{topic_encoded}",
                follow_redirects=True,
            )
            if resp.status_code == 404:
                # Try search as fallback
                search = await client.get(
                    f"https://{lang}.wikipedia.org/w/api.php",
                    params={"action": "opensearch", "search": topic, "limit": 1, "format": "json"},
                )
                results = search.json()
                if results[1]:
                    topic_encoded = results[1][0].replace(" ", "_")
                    resp = await client.get(
                        f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{topic_encoded}",
                        follow_redirects=True,
                    )
            resp.raise_for_status()
            data = resp.json()

        title = data.get("title", topic)
        extract = data.get("extract", "No summary available.")
        url = data.get("content_urls", {}).get("desktop", {}).get("page", "")
        return f"**{title}** (Wikipedia)\n\n{extract}\n\n{url}"
    except Exception as exc:
        return f"Wikipedia lookup failed for '{topic}': {exc}"


async def get_weather(location: str, days: int = 3) -> str:
    """
    Get weather forecast for any city or location worldwide.
    Powered by Open-Meteo (completely free, no API key needed).
    days: 1-7
    """
    days = max(1, min(7, days))
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Geocode
            geo_resp = await client.get(_GEOCODE, params={"name": location, "count": 1, "language": "en", "format": "json"})
            geo_resp.raise_for_status()
            geo_data = geo_resp.json()

            results = geo_data.get("results")
            if not results:
                return f"Location not found: '{location}'. Try a major city name."

            loc = results[0]
            lat, lon = loc["latitude"], loc["longitude"]
            place = f"{loc['name']}, {loc.get('country', '')}"

            # Fetch forecast
            weather_resp = await client.get(_WEATHER, params={
                "latitude": lat,
                "longitude": lon,
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode",
                "hourly": "temperature_2m,precipitation_probability,windspeed_10m",
                "forecast_days": days,
                "timezone": "auto",
            })
            weather_resp.raise_for_status()
            data = weather_resp.json()

        daily = data.get("daily", {})
        dates = daily.get("time", [])
        t_max = daily.get("temperature_2m_max", [])
        t_min = daily.get("temperature_2m_min", [])
        precip = daily.get("precipitation_sum", [])
        codes = daily.get("weathercode", [])

        code_desc = {
            0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
            45: "Foggy", 51: "Light drizzle", 61: "Light rain", 63: "Moderate rain",
            65: "Heavy rain", 71: "Light snow", 80: "Rain showers", 95: "Thunderstorm",
        }

        lines = [f"**Weather: {place}** ({days}-day forecast)\n"]
        for i, date in enumerate(dates):
            desc = code_desc.get(codes[i] if i < len(codes) else 0, "Mixed")
            lines.append(
                f"**{date}** — {desc}\n"
                f"  🌡 {t_min[i]:.0f}°C – {t_max[i]:.0f}°C  🌧 {precip[i]:.1f}mm"
            )
        return "\n\n".join(lines)
    except Exception as exc:
        return f"Weather fetch failed: {exc}"


async def get_rss_feed(url: str, limit: int = 10) -> str:
    """
    Read any RSS or Atom feed and return the latest entries.
    Works with news sites, blogs, podcasts, YouTube channels, Reddit, and more.
    """
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": _UA},
            timeout=12,
            follow_redirects=True,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            xml_content = resp.text

        root = ET.fromstring(xml_content)
        ns = {"atom": "http://www.w3.org/2005/Atom"}

        items: list[dict] = []

        # RSS 2.0
        for item in root.findall(".//item"):
            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            desc = item.findtext("description", "").strip()
            # Strip HTML from description
            desc = re.sub(r"<[^>]+>", " ", desc)
            desc = re.sub(r"\s+", " ", desc).strip()[:200]
            pubdate = item.findtext("pubDate", "")[:30]
            items.append({"title": title, "link": link, "desc": desc, "date": pubdate})

        # Atom
        if not items:
            for entry in root.findall("atom:entry", ns) or root.findall("{http://www.w3.org/2005/Atom}entry"):
                title = (entry.findtext("atom:title", "", ns) or
                         entry.findtext("{http://www.w3.org/2005/Atom}title", "")).strip()
                link_el = entry.find("atom:link[@rel='alternate']", ns) or entry.find("{http://www.w3.org/2005/Atom}link")
                link = (link_el.get("href", "") if link_el is not None else "").strip()
                summary = (entry.findtext("atom:summary", "", ns) or
                           entry.findtext("{http://www.w3.org/2005/Atom}summary", "")).strip()
                summary = re.sub(r"<[^>]+>", " ", summary)[:200]
                updated = (entry.findtext("atom:updated", "", ns) or
                           entry.findtext("{http://www.w3.org/2005/Atom}updated", ""))[:10]
                items.append({"title": title, "link": link, "desc": summary, "date": updated})

        if not items:
            return f"Could not parse feed at {url}. It may not be a valid RSS/Atom feed."

        items = items[:limit]
        feed_title = root.findtext(".//title") or url
        lines = [f"**{feed_title}** (last {len(items)} entries)\n"]
        for item in items:
            lines.append(f"• **{item['title']}**\n  {item['desc']}\n  {item['link']}")
        return "\n\n".join(lines)
    except ET.ParseError:
        return f"Could not parse XML from {url}. Check that it is a valid RSS/Atom URL."
    except Exception as exc:
        return f"RSS fetch failed: {exc}"


async def get_reddit_posts(subreddit: str, sort: str = "hot", limit: int = 10) -> str:
    """
    Get posts from any subreddit. sort: hot/new/top/rising. No API key needed.
    """
    subreddit = subreddit.lstrip("r/")
    sort = sort.lower()
    if sort not in {"hot", "new", "top", "rising"}:
        sort = "hot"
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": _UA},
            timeout=10,
            follow_redirects=True,
        ) as client:
            resp = await client.get(f"https://www.reddit.com/r/{subreddit}/{sort}.json?limit={limit}")
            resp.raise_for_status()
            posts = resp.json()["data"]["children"]

        lines = [f"**r/{subreddit} — {sort.title()} posts:**\n"]
        for p in posts:
            d = p["data"]
            score = d.get("score", 0)
            comments = d.get("num_comments", 0)
            lines.append(
                f"• **{d['title']}**\n"
                f"  ⬆️{score:,} 💬{comments}  | https://reddit.com{d['permalink']}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"Reddit fetch failed: {exc}"
