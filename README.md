# world-events-alerts

A Discord alert bot that ingests real-world events (earthquakes, floods,
cyclones, volcanoes, wildfires, breaking news, ...) from public feeds and
posts new ones to a Discord channel.

Built as a pipeline with a strict separation between ingestion and output:

```
sources  ->  normalized events  ->  dedupe  ->  sinks (discord, console, ...)
```

This is deliberate: a future project (a live world-events map website) will
consume the same normalized events by adding its own sink. Sources never
know about Discord; sinks never know where an event came from.

## Layout

```
alerts/
  sources/          # one module per source: fetch + normalize only
    usgs.py          earthquakes (USGS GeoJSON feed)
    gdacs.py          floods, cyclones, volcanoes, wildfires, drought, tsunami (GDACS RSS)
    rss.py            generic news, config-driven (config/feeds.yaml)
  normalize.py       shared helpers: event construction, id/time/text helpers
  dedupe.py          persistent "seen ids" store (SQLite)
  sinks/
    console.py        prints events (dry runs, testing)
    discord.py         posts Discord embeds via webhook
  config/feeds.yaml   RSS feed list + per-feed metadata
  run.py              CLI entrypoint
  state/              dedupe DB + RSS conditional-GET cache (persisted between CI runs)
```

## The EVENT schema

Every source normalizes into exactly this shape. This is the contract other
consumers (like the future map site) will rely on -- fields are never
renamed or repurposed; new optional fields may be added later.

| field      | type            | required | rules |
|------------|-----------------|----------|-------|
| id         | string          | yes      | `"<source>:<native_id>"`, globally unique, stable across polls (the same real-world event always produces the same id) |
| source     | string          | yes      | `"usgs"`, `"gdacs"`, `"bbc-world"`, ... lowercase slug |
| kind       | string          | yes      | `"earthquake"`, `"flood"`, `"cyclone"`, `"wildfire"`, `"volcano"`, `"drought"`, `"tsunami"`, `"news"`, ... lowercase; reuse an existing kind before inventing a new one |
| severity   | number or null  | yes      | source-native scale (quake magnitude, GDACS alert level 1-3); null for news |
| title      | string          | yes      | human-readable one-liner, plain text (no HTML) |
| summary    | string or null  | yes      | 1-3 sentences, plain text; null if none |
| lat        | number or null  | yes      | WGS84. Required for natural events; null allowed for news |
| lon        | number or null  | yes      | same |
| place      | string or null  | yes      | human-readable location; null if unknown |
| country    | string or null  | yes      | ISO3 code if confidently known from the source, else null -- never guessed |
| time_utc   | string          | yes      | ISO 8601 UTC, e.g. `"2026-07-11T13:02:11Z"` |
| url        | string          | yes      | link to the original source page (shown to humans as attribution) |

Unknown is always `null`, never `""`, never fabricated. Titles/summaries
reproduce the source's own words (headline + short snippet + link +
attribution is fine; full article text is never republished).

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Print normalized events, skip state writes and all sinks:
python -m alerts.run --dry-run

# One real poll cycle against a given sink:
python -m alerts.run --sinks console
python -m alerts.run --sinks discord   # requires DISCORD_WEBHOOK_URL, see below
```

Useful env vars:

| var | purpose |
|-----|---------|
| `ALERTS_STATE_DIR` | override where the dedupe DB / RSS cache live (default `alerts/state`) |
| `ENABLE_USGS` / `ENABLE_GDACS` / `ENABLE_RSS` | turn a whole source on/off (`"true"`/`"false"`, default on) |
| `USGS_FEED_URL` | USGS feed to poll (default `all_hour.geojson`; e.g. swap to `2.5_day.geojson` for a calmer feed) |
| `USGS_MIN_MAGNITUDE` | minimum earthquake magnitude that triggers an alert (default `0`, i.e. unfiltered; the live bot runs with `4.5`) |
| `GDACS_FEED_URL` | GDACS feed URL override |
| `GDACS_MIN_SEVERITY` | minimum GDACS alert level that triggers an alert -- `1`=Green, `2`=Orange, `3`=Red (default `1`, i.e. unfiltered; the live bot runs with `2` since Green fires constantly worldwide, mostly minor satellite-detected wildfires) |
| `DISCORD_WEBHOOK_URL` | Discord webhook URL -- **secret**, see below |

## Changing settings on the live bot (no coding needed)

Everything below lives in `.github/workflows/poll.yml`, in the "Easy
settings" comment block at the top of the file, and can be edited straight
in the GitHub website (open the file, click the pencil/edit icon, save):

- **How often it checks:** the `cron: "*/10 * * * *"` line. `*/10` means
  every 10 minutes; change the number (GitHub won't reliably run more often
  than every 5 minutes, so `*/5` is the practical fastest setting).
- **Turn a source off:** change `ENABLE_USGS`, `ENABLE_GDACS`, or
  `ENABLE_RSS` from `"true"` to `"false"`.
- **Earthquake sensitivity:** `USGS_MIN_MAGNITUDE` -- raise it to hear
  about only bigger quakes, lower it to hear about more (smaller) ones.
- **Disaster severity:** `GDACS_MIN_SEVERITY` -- `1` (Green) includes every
  minor event, `2` (Orange) skips low-significance ones, `3` (Red) only
  major disasters.
- **Add/remove/edit news outlets:** see the next section.

Note: checking more often does not mean more Discord messages by itself --
the bot only ever posts something the first time it sees a genuinely new
event (see "Dedupe & state" below). The check frequency only controls how
quickly a new event is noticed, not how much gets posted.

## Adding an RSS news feed

Edit `alerts/config/feeds.yaml`:

```yaml
feeds:
  - slug: bbc-world
    url: https://feeds.bbci.co.uk/news/world/rss.xml
    kind: news
```

- `slug` becomes part of the event id (`source` field too) -- keep it
  stable once added.
- `kind` defaults to `news` if omitted.
- Verify the feed URL still resolves before adding it; outlets change RSS
  paths without notice.
- No code changes needed. No geocoding happens here -- `lat`/`lon`/`place`
  are always null for RSS events in v1.

## Setting the Discord webhook secret

The bot posts through a Discord webhook. Create one in your target channel
(Channel Settings -> Integrations -> Webhooks), then, in this repo:

1. Settings -> Secrets and variables -> Actions -> New repository secret
2. Name: `DISCORD_WEBHOOK_URL`
3. Value: the webhook URL

It is exposed to the scheduled workflow as an env var and is **never**
committed, logged, or echoed anywhere -- this repo is public.

## First-run seeding behavior

The first time the bot runs against an empty state store, posting every
currently-live event to Discord would flood the channel. Instead, on a
run with an empty dedupe store, the bot records every fetched event as
"seen" **without posting them**, logs `seeded N events`, and exits. Only
events that appear on later runs get posted.

This also means: if the CI cache holding `alerts/state` is ever evicted or
lost, the next run quietly re-seeds instead of flooding the channel -- no
harm done, just a silent reset of what counts as "already seen".

## Scheduling (GitHub Actions)

This repo is public, so GitHub Actions minutes are free and unlimited.
`.github/workflows/poll.yml` runs `python -m alerts.run --sinks discord`
every 10 minutes (`workflow_dispatch` is also enabled for manual runs) and
persists `alerts/state` between runs via `actions/cache`.

**Gotcha:** GitHub automatically disables scheduled workflows after 60 days
of repo inactivity. Any commit (or manually re-enabling the workflow under
the Actions tab) revives it.

## Source notes

- **USGS** (earthquakes): configurable feed + minimum magnitude; ids are
  the native USGS feature id.
- **GDACS** (floods, cyclones, volcanoes, wildfires, drought, tsunamis, plus
  its own earthquake alerts): ids combine `gdacs:eventid` with the episode
  id when present, so a significant update to an ongoing disaster (e.g. a
  cyclone's track changing) re-alerts as a "new" event by design.
  GDACS also reports earthquakes (`EQ`) -- these are kept alongside USGS's
  own earthquake events under different, non-colliding ids rather than
  deduped against each other; expect occasional overlap for the same
  real-world quake.
- **RSS** (news): config-driven, see above. Every fetch is guarded so one
  dead/typo'd feed URL never prevents the others from delivering.

## Dedupe & state

A SQLite store (`alerts/state/seen.sqlite3`, table `seen(id, first_seen)`)
tracks every event id ever emitted. An event reaches a sink only the first
time its id is seen. Entries older than 30 days are pruned automatically.

An event is only recorded as "seen" once every requested sink confirms it
was actually delivered -- each sink's `send()` returns the subset of
events it managed to send, and only that subset gets marked. If Discord
rejects a batch (rate limit, a malformed message, a network blip), those
events stay eligible and are retried on the next poll instead of silently
vanishing.

v1 does not detect in-place updates to an already-seen event (e.g. USGS
revising a magnitude) -- same id means "already handled". See the comment
in `alerts/dedupe.py` for where update-handling would go if needed later.

## Design notes for future consumers

The event schema above is the contract. A future sink (e.g. a database
writer for the map website) can be added with zero changes to any source
module -- that's the point of the `sources -> normalize -> dedupe -> sinks`
split. When in doubt about a field's meaning, follow the table exactly.
