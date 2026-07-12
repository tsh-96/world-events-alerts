"""Console sink: pretty-prints events. Used for dry runs and testing."""

from __future__ import annotations


def send(events: list[dict]) -> None:
    if not events:
        print("(no new events)")
        return
    for event in events:
        print(f"[{event['source']}/{event['kind']}] {event['title']}")
        print(f"  id:       {event['id']}")
        print(f"  severity: {event['severity']}")
        print(f"  place:    {event['place']}  country: {event['country']}")
        print(f"  coords:   {event['lat']}, {event['lon']}")
        print(f"  time_utc: {event['time_utc']}")
        print(f"  url:      {event['url']}")
        if event["summary"]:
            print(f"  summary:  {event['summary']}")
        print()
