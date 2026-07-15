# Handoff: getting these events into the map website

This file is for whoever (human or coding agent) is building the other
project the owner has mentioned -- the live world-events map website. It
explains what this repo already does, exactly what data is available, and
the concrete steps to start receiving it. It assumes no prior context.

## What this repo is

`world-events-alerts` polls a few public sources once an hour (GitHub
Actions, no server, no paid infra) and normalizes everything into one
common shape before doing anything with it:

```
sources  ->  normalized events  ->  dedupe  ->  sinks (discord, console, website, ...)
```

Sources (USGS earthquakes, GDACS disasters, a set of RSS news feeds) never
know about Discord or about you; sinks never know where an event came
from. That split is what makes this handoff possible without touching any
source code -- see `README.md`'s "Design notes for future consumers".

## The data: EVENT schema

Every event, from every source, is exactly this shape (full authoritative
definition: `README.md` -> "The EVENT schema", built by
`alerts/normalize.py:make_event`):

| field      | type            | notes |
|------------|-----------------|-------|
| id         | string          | `"<source>:<native_id>"`, globally unique and stable -- same real-world event always produces the same id, forever |
| source     | string          | `"usgs"`, `"gdacs"`, `"bbc"`, `"al-jazeera"`, ... lowercase slug |
| kind       | string          | `"earthquake"`, `"flood"`, `"cyclone"`, `"wildfire"`, `"volcano"`, `"drought"`, `"tsunami"`, `"news"` |
| severity   | number or null  | quake magnitude, GDACS alert level (1=Green/2=Orange/3=Red); null for news |
| title      | string          | plain text, no HTML |
| summary    | string or null  | 1-3 sentences plain text, or null |
| lat        | number or null  | WGS84. Present for USGS/GDACS. **Always null for RSS news** -- no geocoding is done in this repo by design (see `alerts/sources/rss.py`); that step is left to you if you want it |
| lon        | number or null  | same as lat |
| place      | string or null  | human-readable location, or null |
| country    | string or null  | ISO3 if confidently known, else null -- never guessed |
| time_utc   | string          | ISO 8601 UTC, e.g. `"2026-07-11T13:02:11Z"` |
| url        | string          | link back to the original source |
| notable    | boolean         | whether it was significant enough to notify about at all |
| prod_ready | boolean         | whether it cleared the bar for the owner's "trusted" Discord channel, vs. still-on-trial |

Unknown is always `null`, never `""`, never fabricated. Titles/summaries
are the source's own words (headline + short snippet), never full-article
republication -- worth keeping in mind for anything you display publicly.

## What's actually flowing right now

Every enabled source's events are fetched every run regardless of
`notable`/`prod_ready` -- those two fields only control Discord posting,
they don't gate fetching. As of this writing:

- **USGS earthquakes**: only magnitude >= 6.5 (`USGS_MIN_MAGNITUDE` in
  `.github/workflows/poll.yml`) become events at all -- smaller quakes
  aren't fetched into the pipeline, not just filtered from Discord. If you
  want smaller quakes too, that threshold needs to change (owner sign-off
  required, see below).
- **GDACS disasters**: only alert level >= Orange (`GDACS_MIN_SEVERITY: "2"`)
  become events, same caveat.
- **RSS news** (`alerts/config/feeds.yaml`): every configured feed is
  fetched in full every run. Most have `notify: true` (bbc, al-jazeera,
  cnn, nyt, mercopress, euronews, moscow-times, scmp, straits-times-asia,
  rnz-pacific, africanews) -- those are `notable`. One, `times-of-india`,
  has `notify: false` -- it's fetched but never notable, i.e. it exists in
  a given run's event list but wouldn't currently reach any sink that only
  receives `notable_events` (see below).

The Discord "dev" channel currently receives every `notable` event from
all of the above. That's the full set the owner said they want pushed to
you.

## How to actually receive it: the `website` sink

There's no export file, database, or API today -- until now, Discord
webhook messages were the *only* way any of this data left the repo. To
fix that, a new sink has been added: `alerts/sinks/website.py`.

**What it does:** once per poll run, it POSTs a single JSON body to a URL
you provide:

```json
{
  "events": [
    { "id": "usgs:us7000abcd", "source": "usgs", "kind": "earthquake", ... },
    ...
  ]
}
```

Each object is exactly one EVENT as described above. A run with nothing
new sends nothing (no empty POSTs). It's all-or-nothing per run: your
endpoint must return a 2xx status for the *whole* batch to count as
delivered; anything else (including a timeout or connection failure) means
none of that run's events are marked "seen" on this side, and the same
batch is retried next hour, so a temporary outage on your end doesn't lose
events -- it just delays them. There's no per-event retry queue and no
authentication built in yet (see "Open questions" below).

**It is not wired into the live workflow yet.** It's inert code sitting in
the repo -- nothing changes about what currently happens until:

1. You (or whoever owns the website's backend) stand up an HTTPS endpoint
   that accepts `POST` with the JSON body shown above and returns 2xx on
   success.
2. That URL gets added as a GitHub Actions secret in this repo
   (`WEBSITE_WEBHOOK_URL`) -- the owner needs to do this in the repo
   settings, it can't be done from a PR.
3. `.github/workflows/poll.yml` gets two small edits: add
   `WEBSITE_WEBHOOK_URL: ${{ secrets.WEBSITE_WEBHOOK_URL }}` to the `env:`
   block, and add `website` to the `--sinks` list (`--sinks discord
   website`).

Steps 2-3 are a one-line-ish change once a real URL exists -- ping the
owner in chat with the URL and either of us can wire it up.

## Important behavior to design around

- **No backfill, no history dump.** The moment `website` gets added to
  `--sinks`, it only sees events from that point forward -- exactly like
  adding a new Discord webhook (see README "Adding a new webhook never
  replays old events"). If you want the events that have already gone to
  Discord's dev channel before you're wired up, that's not recoverable
  from this repo -- state isn't kept that long (see below) and Discord
  message history isn't re-exported. Get connected early if backlog
  matters to you.
- **Same id, never resent.** Once an event's `id` has been successfully
  delivered to *every* sink in that run's `--sinks` list, it's marked
  "seen" forever (30-day-pruned SQLite store) and will never be sent
  again, even if you lose your own copy. Persist whatever you receive
  durably on your end -- there is no "replay everything since X" endpoint.
- **v1 doesn't detect in-place updates.** If USGS revises a quake's
  magnitude after the fact, or GDACS updates a storm's track, same `id` =
  "already handled", not resent (GDACS storm track updates are the
  exception -- those get a new id per episode, see README "Source notes").
  Don't assume an id you've already seen is final/immutable in the source
  of truth, just that you won't be told about further changes to it.
- **Adding `website` to `--sinks` affects Discord too.** An event only
  gets marked "seen" once *every* requested sink confirms delivery (see
  `alerts/run.py`). If your endpoint is down, dev-channel Discord posting
  for that batch stalls right along with it until your endpoint recovers
  -- it's not an independent side channel today. Keep your endpoint
  reliable, or ask the owner about decoupling this further if that's a
  problem (see below).
- **lat/lon is null for all news events.** If the map website wants to
  plot news stories geographically, geocoding `place`/`title` text is on
  you -- deliberately out of scope here (see `alerts/sources/rss.py`).
- **Times of India isn't in the notable stream.** It's fetched but
  `notable: false`, so it won't reach the `website` sink either (only
  `notable_events` are sent to sinks, see `alerts/run.py:run()`). Ask in
  chat if you want archive-only sources included too -- that'd need a
  small change to what gets sent to which sink.

## Open questions worth raising with the owner before/while building

- **Auth**: right now `website` sink trusts whatever's behind the URL,
  same as Discord's own webhook model (the URL itself is the secret). If
  you want a shared-secret header or signature instead, say so -- easy to
  add, just needs a decision.
- **Volume**: dev-channel volume is deliberately high right now (all RSS
  world/regional news, no cross-outlet dedup applied outside a same-story
  window). If the map website only wants prod-tier-equivalent
  events (currently just USGS/GDACS quakes/disasters), that's a filter to
  agree on rather than something to build defensively against on your
  side.
- **Decoupling**: if you'd rather the website sink's uptime never affect
  Discord's, that's a real tradeoff to discuss (currently: one shared
  all-sinks-must-succeed gate, see above) -- not done unilaterally here
  since it changes existing delivery guarantees.

## Everything else, for context

Full architecture, running locally, adding/tuning sources, and all sink
details live in `README.md` -- start there for anything not covered above.
