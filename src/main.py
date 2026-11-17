"""YouTube Search — an Apify actor that runs YouTube searches and
returns one dataset item per video found.

Input: one or more search queries (batch mode), a max-results cap per
query, and optional duration / upload-recency / channel filters. Each
video pushed to the dataset is one pay-per-event charge (event:
"search-result"). A query that fails or matches nothing is logged and
skipped — never charged. The run only fails if EVERY query fails.

Search core: src/youtube.py, ported from StallRow's tube-search
(public results page parsing — no API key, no login).
"""

from __future__ import annotations

import asyncio
import os

import httpx
from apify import Actor

from .youtube import UpstreamError, tube_search

CHARGE_EVENT = "search-result"


def _scrub_no_proxy() -> None:
    """Drop bracketed IPv6 entries ([::1]) from no_proxy/NO_PROXY:
    httpx's proxy parser raises InvalidURL on them when a client is
    constructed. (Same fix the fleet services shipped after hitting
    this in the wild; harmless where the entries are absent.)"""
    for key in ("no_proxy", "NO_PROXY"):
        value = os.environ.get(key)
        if value:
            os.environ[key] = ",".join(
                part for part in value.split(",") if "[" not in part
            )


def _none_if_any(value) -> str | None:
    """Map the input schema's 'any' (or empty) to the core's None."""
    if value is None:
        return None
    v = str(value).strip().lower()
    return None if v in ("", "any") else v


async def main() -> None:
    _scrub_no_proxy()
    async with Actor:
        actor_input = await Actor.get_input() or {}

        queries = [
            str(q).strip()
            for q in (actor_input.get("queries") or [])
            if isinstance(q, str) and q.strip()
        ]
        single = actor_input.get("query")
        if isinstance(single, str) and single.strip():
            queries.insert(0, single.strip())
        if not queries:
            await Actor.fail(
                status_message="No search queries given: fill in 'queries' in the input."
            )
            return

        max_results = int(actor_input.get("maxResultsPerQuery") or 10)
        max_results = max(1, min(max_results, 100))
        duration = _none_if_any(actor_input.get("duration"))
        uploaded_within = _none_if_any(actor_input.get("uploadedWithin"))
        channel_raw = actor_input.get("channel")
        channel = (
            str(channel_raw).strip()
            if isinstance(channel_raw, str) and channel_raw.strip()
            else None
        )

        total_results = 0
        failed_queries = 0
        limit_reached = False

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0)
        ) as client:
            for query in queries:
                if limit_reached:
                    break
                try:
                    outcome = await tube_search(
                        client,
                        query,
                        max_results,
                        duration=duration,
                        uploaded_within=uploaded_within,
                        channel=channel,
                    )
                except (UpstreamError, ValueError) as e:
                    failed_queries += 1
                    Actor.log.warning(f"Query {query!r} failed: {e}")
                    continue

                for video in outcome["results"]:
                    item = {
                        "query": query,
                        "title": video["title"],
                        "videoId": video["video_id"],
                        "url": video["url"],
                        "channel": video["channel"],
                        "duration": video["duration"],
                        "durationSeconds": video["duration_seconds"],
                        "views": video["views"],
                        "published": video["published"],
                        "thumbnail": video["thumbnail"],
                    }
                    await Actor.push_data(item)
                    total_results += 1

                    # Pay-per-event: one charge per result, only after
                    # the result is safely in the dataset. Stop early
                    # if the user's spend limit is reached.
                    charge = await Actor.charge(
                        event_name=CHARGE_EVENT, count=1
                    )
                    if charge.event_charge_limit_reached:
                        limit_reached = True
                        Actor.log.warning(
                            "Charge limit reached; stopping. "
                            f"{total_results} result(s) delivered so far."
                        )
                        break

        if total_results == 0 and failed_queries == len(queries):
            await Actor.fail(
                status_message=(
                    f"All {len(queries)} quer(ies) failed or matched nothing; "
                    "no results delivered, nothing charged."
                )
            )
            return

        Actor.log.info(
            f"Done. {total_results} result(s) across "
            f"{len(queries) - failed_queries} successful quer(ies); "
            f"{failed_queries} failed (failures are never charged)."
        )


if __name__ == "__main__":
    asyncio.run(main())
