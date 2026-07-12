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
  notability.py      cross-outlet duplicate-story suppression (see below)
  sinks/
    console.py        prints events (dry runs, testing)
    discord.py         posts Discord embeds via webhook (dev + prod channels)
  config/feeds.yaml   RSS feed list + per-feed metadata
  run.py              CLI entrypoint
  state/              dedupe DB, RSS conditional-GET cache, per-channel
                      last-Discord-post timestamps, recent-story history
                      (persisted between CI runs)
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
| notable    | boolean         | yes      | whether this event is significant enough to actively notify about (e.g. Discord), as opposed to just being archived for other consumers (e.g. the future website, which wants everything regardless). Defaults `true`; see "Filtering what gets sent to Discord" |
| prod_ready | boolean         | yes      | among notable events, whether this one is trusted enough for the prod Discord channel, vs. dev-channel-only while a source is on trial. Defaults `true`; see "Dev vs. prod Discord channels" |

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

# Mark everything currently new as seen without posting (clears a backlog):
python -m alerts.run --mark-seen-only
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
| `DISCORD_PACE_WINDOW_MINUTES` | new events spread at randomized moments (never less than 4 minutes apart) across roughly this many minutes instead of firing them all within seconds (default `52`; that same 4-minute minimum also applies to the very next post after any earlier run's last post, even for a single new event; nothing is ever left for the next run -- if there isn't enough of the budget to give every event its own message, extras get bundled a few at a time into the same message instead) |

## Changing settings on the live bot (no coding needed)

Everything below lives in `.github/workflows/poll.yml`, in the "Easy
settings" comment block at the top of the file, and can be edited straight
in the GitHub website (open the file, click the pencil/edit icon, save):

- **How often it checks:** this workflow doesn't schedule itself anymore --
  see "Scheduling" below for why, and where the hourly trigger actually
  lives.
- **Turn a source off:** change `ENABLE_USGS`, `ENABLE_GDACS`, or
  `ENABLE_RSS` from `"true"` to `"false"`.
- **Earthquake sensitivity:** `USGS_MIN_MAGNITUDE` -- raise it to hear
  about only bigger quakes, lower it to hear about more (smaller) ones.
- **Disaster severity:** `GDACS_MIN_SEVERITY` -- `1` (Green) includes every
  minor event, `2` (Orange) skips low-significance ones, `3` (Red) only
  major disasters.
- **Message pacing:** `DISCORD_PACE_WINDOW_MINUTES` -- if a run finds
  several new events at once (e.g. after a quiet stretch), they post at
  randomized moments (never less than 4 minutes apart, never more than 10)
  spread across roughly this many minutes, instead of landing in one burst.
  That 4-minute minimum isn't just within one run -- it also covers the gap
  since the *previous* run's last post, so two runs close together won't
  post two messages right on top of each other. Nothing is ever left for
  the next run: since there's only one check per hour, if there isn't
  enough of the budget to give every event its own message, extra events
  get bundled a few at a time into the same message for some of the slots
  instead -- everything this run finds gets posted this run. Keep the
  budget comfortably under an hour so one run reliably finishes before the
  next begins. Messages always post oldest-first, in the order the events
  actually happened -- not the order the bot happened to notice them.
- **Skip a pile of old news:** run the "Clear backlog" workflow once
  (Actions tab -> Clear backlog -> Run workflow) -- see "Clearing a
  backlog" below.
- **Add/remove/edit news outlets:** see the next section.

Note: checking more often does not mean more Discord messages by itself --
the bot only ever posts something the first time it sees a genuinely new
event (see "Dedupe & state" below). The check frequency only controls how
quickly a new event is noticed, not how much gets posted.

## Adding an RSS news feed

Edit `alerts/config/feeds.yaml`:

```yaml
feeds:
  - slug: bbc
    url: https://feeds.bbci.co.uk/news/world/rss.xml
    kind: news
    notify: true
    prod: false
```

- `slug` becomes part of the event id (`source` field too) -- keep it
  stable once added (see "Filtering what gets sent to Discord" below for
  why an outlet can have more than one feed sharing the same slug).
- `kind` defaults to `news` if omitted.
- `notify` controls whether items from this feed post to Discord's dev
  channel at all (default `false`) -- see the next section. It never
  affects whether an item is fetched and kept in the dedupe store.
- `prod` controls whether items from this feed ALSO post to the prod
  channel (default `false`, only meaningful when `notify` is also true).
  New sources should start `prod: false` -- watch how they behave in dev
  for a while, then flip to `prod: true` once you trust them. See "Dev vs.
  prod Discord channels".
- Verify the feed URL still resolves before adding it; outlets change RSS
  paths without notice.
- No code changes needed. No geocoding happens here -- `lat`/`lon`/`place`
  are always null for RSS events in v1.

## Filtering what gets sent to Discord

Every event from every source is always fetched and kept in the dedupe
store, regardless of importance -- nothing is ever thrown away, so a
future consumer (e.g. the map website) still gets the full picture. What's
different is which of those events actually trigger a Discord message: an
event only posts to Discord if it's `notable` (see the EVENT schema
above). USGS and GDACS are `notable` by construction -- they already only
fetch events above `USGS_MIN_MAGNITUDE` / `GDACS_MIN_SEVERITY`, so
whatever they do return has already cleared a significance bar.

News (RSS) is different: a general "everything published in this section"
feed has no sense of importance on its own. So for news, `notable` comes
from `alerts/config/feeds.yaml`'s `notify` flag (see above). Some outlets
publish a second feed of just their editors' top picks, separate from the
full section feed -- when one genuinely exists and is smaller/more
selective than the section feed, `feeds.yaml` lists both under the same
`slug`: the full feed with `notify: false` (archived only) and the
curated feed with `notify: true` (also posts to dev). Overlapping
articles between the two merge into one event, not posted twice.

This leans on an outlet's own editors to decide what's "important"
instead of us guessing with a keyword list -- no tuning required when it
works. It only works when a real curated feed exists, though: BBC, Al
Jazeera, Guardian, and NPR were all checked and none had a real one
(Guardian and NPR were dropped entirely as a result -- their "curated"
candidates turned out no smaller than the full feed). Where no curated
feed exists, an outlet's regular section feed notifies directly instead,
as the best available stand-in -- including NYT now, which was switched
from its (genuinely curated, but not regional) `HomePage.xml` to its
`US.xml` section feed once NYT became a regional rather than world source
(see next section) -- regional accuracy took priority over curation
quality for that one.

### Regional outlets and cross-outlet duplicates

Only BBC, CNN, and Al Jazeera are meant to carry general world news --
they stay on each outlet's "World"/top-stories feed on purpose. Every
other source in `feeds.yaml` is deliberately pointed at that outlet's own
*regional* section (e.g. SCMP's China section, not its World section;
Euronews' "My Europe" feed, not its general home feed) so the bot covers
South America, Europe, Russia, China, India, the Middle East, East/
Southeast Asia, Oceania, and Africa through their own regional lens
instead of everything being filtered through a US/UK "World desk" view.
New regional additions start `prod: false` (dev-channel only) until
reviewed -- see "Dev vs. prod Discord channels" below.

More outlets covering the same world raises an obvious problem: a single
big story (a major earthquake, a war escalation) can get covered by every
outlet at once, and without anything to catch that, it'd post once per
outlet. `alerts/notability.py` catches this: before posting, a new
notable event's headline is compared (by significant-word overlap, not a
paid AI call) against other notable events posted in roughly the last day
and a half. A close match from a *different* outlet gets treated as the
same story and archived without posting again, instead of showing up
redundantly. This is a plain keyword heuristic, not real language
understanding -- it will occasionally miss a genuine duplicate phrased
very differently, or (more rarely) suppress two distinct stories that
happen to share several significant words. `SIMILARITY_THRESHOLD` in that
file is the dial to tune if it's over- or under-suppressing in practice.

## Setting the Discord webhook secret

The bot posts through Discord webhooks. Create one in your target channel
(Channel Settings -> Integrations -> Webhooks), then, in this repo:

1. Settings -> Secrets and variables -> Actions -> New repository secret
2. Name: `DISCORD_WEBHOOK_URL`
3. Value: the webhook URL

It is exposed to the scheduled workflow as an env var and is **never**
committed, logged, or echoed anywhere -- this repo is public.

### Dev vs. prod Discord channels

`DISCORD_WEBHOOK_URL` and `DISCORD_WEBHOOK_URL_2` are **not** mirrors of
each other -- they're deliberately different feeds:

- `DISCORD_WEBHOOK_URL` (dev) gets **every** notable event, including
  brand-new sources still on trial.
- `DISCORD_WEBHOOK_URL_2` (prod) only gets notable events also flagged
  `prod_ready` -- see the EVENT schema above. This is the clean, trusted
  feed.

`DISCORD_PROD_ENABLED` (set in `poll.yml`, default `"false"`) is a master
switch for the entire prod tier: while it isn't exactly `"true"`, prod
gets nothing at all, no matter what any individual source's `prod` flag
says. This exists so prod can be guaranteed silent during a review period
(e.g. while adding a batch of new regional sources and watching how they
behave in dev first) without depending on every source's flag being
correct -- one place to hold everything back, one place to let it
through. Once it's `"true"`, each source still needs its own
`prod: true` to actually reach prod.

A source graduates from dev to prod by editing `alerts/config/feeds.yaml`
(`prod: false` -> `prod: true` for an RSS feed) or `alerts/normalize.py`'s
default for a whole source type -- both config/one-line changes, no new
code. Dev and prod are paced independently (each has its own randomized
timing across the pacing budget) since they usually carry different
content, so their messages won't necessarily land at the same moments
even for an event that reaches both.

Dev's pacing can be overridden independently of prod's via
`DISCORD_DEV_MIN_INTERVAL_SECONDS` / `DISCORD_DEV_PACE_WINDOW_MINUTES` (set
in `poll.yml`) -- useful for clearing a big one-off catch-up batch in dev
quickly instead of waiting out the normal human-paced timing. Prod always
uses the normal `DISCORD_PACE_WINDOW_MINUTES`/4-minute settings regardless
of what dev's overrides are set to.

If you only want one channel, just set `DISCORD_WEBHOOK_URL` -- everything
notable posts there and prod-tier filtering never comes into play. Set
`DISCORD_WEBHOOK_URL_3`, `_4`, ... for additional prod-tier mirrors if you
ever want more than one "clean feed" channel; only the first webhook is
ever treated as dev.

An event only counts as delivered once every webhook it was eligible for
actually received it -- a temporarily-broken channel doesn't cause events
to be lost, just retried next check. Adding a new webhook never replays
old events: the dedupe store already knows what's been seen, so a newly
added channel joins the live stream from that point on with no backlog
dump.

## First-run seeding behavior

The first time the bot runs against an empty state store, posting every
currently-live event to Discord would flood the channel. Instead, on a
run with an empty dedupe store, the bot records every fetched event as
"seen" **without posting them**, logs `seeded N events`, and exits. Only
events that appear on later runs get posted.

This also means: if the CI cache holding `alerts/state` is ever evicted or
lost, the next run quietly re-seeds instead of flooding the channel -- no
harm done, just a silent reset of what counts as "already seen".

## Scheduling

This repo is public, so GitHub Actions minutes are free and unlimited.
`.github/workflows/poll.yml` runs `python -m alerts.run --sinks discord`
and persists `alerts/state` between runs via `actions/cache`.

It does **not** use GitHub's own `schedule:` cron trigger -- that was tried
first, but proved unreliable (it went several hours without firing at all
for no visible reason, with no way to diagnose it from outside GitHub).
Instead, the workflow only has `workflow_dispatch` (button-only / API
trigger), and something outside this repo calls that once an hour on a
reliable timer. There should only be **one** thing calling it regularly --
if more than one timer is running (e.g. a fast one and an hourly one at the
same time), their runs queue up behind each other (see "Dedupe & state"
below on the concurrency lock) and can end up executing an older, already-
superseded version of the code just because that's what was checked out
when they were triggered. Ask whoever set up your hourly trigger before
adding a second one.

**Gotcha:** GitHub automatically disables workflows after 60 days of repo
inactivity. Any commit (or manually re-enabling the workflow under the
Actions tab) revives it.

## Clearing a backlog

If the bot goes quiet for a while (paused, misconfigured secret, GitHub
outage, etc.), new events pile up unposted -- and since pacing never
carries anything into the next run, the very next run would post all of
them at once (bundled a few per message as needed) rather than losing any.
If you'd rather just skip that pile of now-stale news than have it
delivered late, run `.github/workflows/clear-backlog.yml` once (Actions
tab -> Clear backlog -> Run workflow): it marks everything currently
pending as "seen" without posting it, so the next normal run only picks up
things that happen from that point on.

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
