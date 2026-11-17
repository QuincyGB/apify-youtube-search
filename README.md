# YouTube Search — No API Key, Structured Results

Run YouTube searches and get back **structured results** — title,
channel, duration, view count, publish age, and thumbnail for every
video — without a YouTube API key, a Google Cloud project, or quota
management.

Run one query or a whole batch in a single run, and narrow results by
video duration, upload recency, or channel name. One dataset item per
video.

Built for content research, trend and competitor monitoring, dataset
building, and agent workflows that need to find videos programmatically.

## What it does

- Searches YouTube's public results — the same results a person sees —
  and parses them into clean, typed fields.
- Filters are applied *before* results are counted, so asking for
  "10 long videos from this week" returns 10 that actually match, not
  the leftovers of a bigger list.
- When you ask for more results than the first page holds, it digs one
  page deeper automatically.
- Batch mode: give it 20 queries, get one combined dataset, each item
  tagged with the query that found it.

## Pricing

Pay per event: you are charged **once per video result delivered** to
your dataset. A query that fails or matches nothing is **never
charged** — if every query fails, the run fails and you pay nothing
in event charges.

## Input

| Field | Type | Description |
|---|---|---|
| `queries` | list of strings | One search per entry. Several entries = one batch run. |
| `query` | string (optional) | A single query; combined with `queries`. |
| `maxResultsPerQuery` | integer (default 10, max 100) | Cap per query. |
| `duration` | `any` \| `short` \| `medium` \| `long` | Short < 4 min, medium 4–20 min, long > 20 min. |
| `uploadedWithin` | `any` \| `day` \| `week` \| `month` \| `year` | Based on YouTube's displayed publish age. |
| `channel` | string (optional) | Only videos whose channel name contains this text. |

### Example input

```json
{
  "queries": ["lofi hip hop radio", "python asyncio tutorial"],
  "maxResultsPerQuery": 10,
  "duration": "any",
  "uploadedWithin": "year"
}
```

## Output

One dataset item per video:

```json
{
  "query": "python asyncio tutorial",
  "title": "Asyncio in Python — Full Tutorial",
  "videoId": "Qb9s3tDxykA",
  "url": "https://www.youtube.com/watch?v=Qb9s3tDxykA",
  "channel": "Example Channel",
  "duration": "24:10",
  "durationSeconds": 1450,
  "views": "312K views",
  "published": "3 months ago",
  "thumbnail": "https://i.ytimg.com/vi/Qb9s3tDxykA/hqdefault.jpg"
}
```

(`videoId`, titles, and counts above are illustrative; real runs return
the live values YouTube displays.)

## Limits — the honest part

- **Fields are what YouTube displays publicly.** View counts and
  publish ages are YouTube's own rounded display values ("312K views",
  "3 months ago"), not exact analytics figures.
- **Live streams have no duration**, so any duration filter excludes
  them.
- **Result depth:** up to ~120 candidate videos are considered per
  query (first page plus one continuation page) before filters and the
  results cap are applied; extremely narrow filters may return fewer
  than the cap.
- **No transcripts, comments, or channel details** — this actor is
  search only. It does not download videos.
- YouTube occasionally changes its results page or rate-limits heavy
  automated traffic; a failed query is retried once and otherwise
  reported in the run log (and never charged).
