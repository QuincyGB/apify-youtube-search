"""YouTube search core.

Ported from StallRow's tube-search implementation
(~/workspace/stall-row/app/services/fetch.py, the YouTube search
section). Behaviour is unchanged:

- Searches YouTube's public results page and parses the embedded
  ytInitialData — no API key, no login.
- Every result carries title, video id/URL, channel, duration (text
  and seconds), view count text, publish-age text, and a thumbnail.
- Duration / upload-recency / channel filters are applied BEFORE
  truncation, so a filter never eats the result budget.
- When max_results outruns page one, one continuation page is fetched
  via the page's own Innertube key for deeper results.
- The fetch retries once on transient failure.
- Honesty rule (inherited): a search with no matching results raises
  UpstreamError — the caller never delivers (or charges for) an
  empty or fabricated result set.
"""

from __future__ import annotations

import asyncio
import json
import re

import httpx


class UpstreamError(Exception):
    """The upstream (YouTube) failed or returned unusable data."""


_UA = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}

_TUBE_CANDIDATE_CAP = 60  # parsed per page before filters truncate
_DURATION_BUCKETS = ("short", "medium", "long")
_UPLOADED_WITHIN_S = {
    "day": 86_400,
    "week": 604_800,
    "month": 2_592_000,
    "year": 31_536_000,
}
_AGE_UNIT_S = {
    "second": 1,
    "minute": 60,
    "hour": 3_600,
    "day": 86_400,
    "week": 604_800,
    "month": 2_592_000,
    "year": 31_536_000,
}


def _extract_js_object(html: str, marker: str, label: str) -> dict:
    """Brace-scan the JSON object assigned after `marker` in a YouTube
    page (ytInitialData on results pages)."""
    idx = html.find(marker)
    if idx < 0:
        raise UpstreamError(f"{label} did not contain {marker}")
    start = html.find("{", idx)
    if start < 0:
        raise UpstreamError(f"{label}: malformed {marker}")
    depth = 0
    end = None
    in_str = False
    esc = False
    for i in range(start, len(html)):
        c = html[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        raise UpstreamError(f"{label}: unterminated {marker}")
    try:
        return json.loads(html[start:end])
    except ValueError as e:
        raise UpstreamError(f"{label}: {marker} not JSON: {e}") from e


def _renderer_text(node) -> str:
    """simpleText, or the first run's text, of a YouTube text node."""
    if not isinstance(node, dict):
        return ""
    simple = node.get("simpleText")
    if isinstance(simple, str):
        return simple
    runs = node.get("runs")
    if isinstance(runs, list) and runs and isinstance(runs[0], dict):
        return runs[0].get("text") or ""
    return ""


def _duration_seconds(text: str | None) -> int | None:
    """'10:24' / '1:02:03' -> seconds. Live streams carry no length
    text — None, which any duration filter excludes (documented)."""
    if not text:
        return None
    parts = text.strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) not in (2, 3):
        return None
    total = 0
    for n in nums:
        total = total * 60 + n
    return total


def _published_age_seconds(text: str | None) -> int | None:
    """'3 days ago' / 'Streamed 2 weeks ago' -> age in seconds."""
    if not text:
        return None
    m = re.search(
        r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago",
        text,
        re.IGNORECASE,
    )
    if not m:
        return None
    return int(m.group(1)) * _AGE_UNIT_S[m.group(2).lower()]


def _parse_video_renderer(vr: dict) -> dict:
    video_id = vr["videoId"]
    duration = _renderer_text(vr.get("lengthText")) or None
    return {
        "title": _renderer_text(vr.get("title")),
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "channel": _renderer_text(vr.get("ownerText")) or None,
        "duration": duration,
        "duration_seconds": _duration_seconds(duration),
        "views": _renderer_text(vr.get("viewCountText")) or None,
        "published": _renderer_text(vr.get("publishedTimeText")) or None,
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
    }


def _collect_renderers(node, out: list[dict], cap: int) -> None:
    if len(out) >= cap:
        return
    if isinstance(node, dict):
        vr = node.get("videoRenderer")
        if isinstance(vr, dict) and vr.get("videoId"):
            out.append(_parse_video_renderer(vr))
            return
        for v in node.values():
            _collect_renderers(v, out, cap)
    elif isinstance(node, list):
        for v in node:
            _collect_renderers(v, out, cap)


def _find_continuation_token(node) -> str | None:
    if isinstance(node, dict):
        cir = node.get("continuationItemRenderer")
        if isinstance(cir, dict):
            token = (
                ((cir.get("continuationEndpoint") or {}).get("continuationCommand") or {})
                .get("token")
            )
            if isinstance(token, str) and token:
                return token
        for v in node.values():
            found = _find_continuation_token(v)
            if found:
                return found
    elif isinstance(node, list):
        for v in node:
            found = _find_continuation_token(v)
            if found:
                return found
    return None


def _tube_filter(
    results: list[dict],
    *,
    duration: str | None,
    uploaded_within: str | None,
    channel: str | None,
) -> list[dict]:
    out = []
    for r in results:
        if duration is not None:
            secs = r.get("duration_seconds")
            if secs is None:
                continue
            bucket = "short" if secs < 240 else "medium" if secs <= 1200 else "long"
            if bucket != duration:
                continue
        if uploaded_within is not None:
            age = _published_age_seconds(r.get("published"))
            if age is None or age > _UPLOADED_WITHIN_S[uploaded_within]:
                continue
        if channel is not None:
            if channel.lower() not in (r.get("channel") or "").lower():
                continue
        out.append(r)
    return out


async def _youtube_get(
    client: httpx.AsyncClient, url: str, params: dict | None = None
) -> httpx.Response:
    """GET a YouTube page with one retry on transient failure."""
    last_error: UpstreamError | None = None
    for attempt in range(2):
        if attempt:
            await asyncio.sleep(0.5)
        try:
            resp = await client.get(
                url, params=params, headers=_UA, follow_redirects=True
            )
        except httpx.HTTPError as e:
            last_error = UpstreamError(f"YouTube fetch failed: {e}")
            continue
        if resp.status_code == 200:
            return resp
        last_error = UpstreamError(f"YouTube returned HTTP {resp.status_code}")
    assert last_error is not None
    raise last_error


async def _tube_continuation(
    client: httpx.AsyncClient, first_page_html: str, token: str
) -> list[dict]:
    """One continuation page of search results via the page's own
    Innertube key. Best-effort depth: any failure returns [] and the
    caller keeps the page-one results it already has."""
    key_m = re.search(r'"INNERTUBE_API_KEY"\s*:\s*"([^"]+)"', first_page_html)
    if not key_m:
        return []
    ver_m = re.search(
        r'"INNERTUBE_CONTEXT_CLIENT_VERSION"\s*:\s*"([^"]+)"', first_page_html
    )
    payload = {
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": ver_m.group(1) if ver_m else "2.20241113.07.00",
                "hl": "en",
            }
        },
        "continuation": token,
    }
    try:
        resp = await client.post(
            "https://www.youtube.com/youtubei/v1/search",
            params={"key": key_m.group(1)},
            json=payload,
            headers=_UA,
        )
    except httpx.HTTPError:
        return []
    if resp.status_code != 200:
        return []
    try:
        data = resp.json()
    except ValueError:
        return []
    out: list[dict] = []
    _collect_renderers(data, out, _TUBE_CANDIDATE_CAP)
    return out


async def tube_search(
    client: httpx.AsyncClient,
    query: str,
    max_results: int,
    *,
    duration: str | None = None,
    uploaded_within: str | None = None,
    channel: str | None = None,
) -> dict:
    """One search. Returns {"query", "count", "results": [...]} (plus
    "filters_applied" when filters are set). Raises ValueError for an
    invalid filter value and UpstreamError when YouTube fails or no
    results match."""
    if duration is not None and duration not in _DURATION_BUCKETS:
        raise ValueError(
            f"duration must be one of {', '.join(_DURATION_BUCKETS)}"
        )
    if uploaded_within is not None and uploaded_within not in _UPLOADED_WITHIN_S:
        raise ValueError(
            f"uploaded_within must be one of {', '.join(_UPLOADED_WITHIN_S)}"
        )
    resp = await _youtube_get(
        client, "https://www.youtube.com/results", params={"search_query": query}
    )
    data = _extract_js_object(resp.text, "ytInitialData", "YouTube search page")
    candidates: list[dict] = []
    _collect_renderers(data, candidates, _TUBE_CANDIDATE_CAP)
    results = _tube_filter(
        candidates,
        duration=duration,
        uploaded_within=uploaded_within,
        channel=channel,
    )
    if len(results) < max_results:
        token = _find_continuation_token(data)
        if token:
            seen = {r["video_id"] for r in candidates}
            for extra in await _tube_continuation(client, resp.text, token):
                if extra["video_id"] not in seen:
                    candidates.append(extra)
                    seen.add(extra["video_id"])
            results = _tube_filter(
                candidates,
                duration=duration,
                uploaded_within=uploaded_within,
                channel=channel,
            )
    results = results[:max_results]
    if not results:
        raise UpstreamError(
            "YouTube search returned no video results matching the request"
        )
    out: dict = {"query": query, "count": len(results), "results": results}
    applied = {
        k: v
        for k, v in (
            ("duration", duration),
            ("uploaded_within", uploaded_within),
            ("channel", channel),
        )
        if v is not None
    }
    if applied:
        out["filters_applied"] = applied
    return out
