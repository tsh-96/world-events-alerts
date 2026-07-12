"""One-off manual check: posts a single test embed to Discord to confirm the
webhook is wired up correctly. Not part of the regular polling pipeline --
does not touch dedupe state. Run via the "Send test Discord message" GitHub
Action, or locally with DISCORD_WEBHOOK_URL set.
"""

from datetime import datetime, timezone

from alerts.sinks.discord import send

TEST_EVENT = {
    "id": "test:connection-check",
    "source": "test",
    "kind": "news",
    "severity": None,
    "title": "Test alert -- world-events-alerts is connected!",
    "summary": (
        "If you can see this message, the bot can successfully post to this "
        "channel. Real earthquake, disaster, and news alerts will start "
        "appearing automatically on the regular schedule."
    ),
    "lat": None,
    "lon": None,
    "place": None,
    "country": None,
    "time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "url": "https://github.com/tsh-96/world-events-alerts",
}

if __name__ == "__main__":
    send([TEST_EVENT])
    print("Test message sent (check your Discord channel).")
