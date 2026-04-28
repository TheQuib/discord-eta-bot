# Discord ETA Bot

A Discord bot that posts a live travel-time estimate between two locations and
updates it on a fixed interval. Backed by the Google Maps Distance Matrix API.

Built on top of the [Python-Discord-Bot-Template by Krypton](https://github.com/kkrypt0nn/Python-Discord-Bot-Template) — see the original docs below for the framework details.

## What it does

`/traveltime` posts a single embed to the channel and rewrites it on every tick
so the channel doesn't get spammed. A red **Stop** button on the message ends
the loop on demand.

### Command options

| Option | Required | Description |
| --- | --- | --- |
| `from_location` | yes | Origin. Address, place name, or `lat,lng`. |
| `to_location` | yes | Destination. Same formats. |
| `time_to_update` | no | Refresh interval in minutes, clamped to 1–30. Default 5. |
| `mode` | no | `Driving` (default), `Transit`, `Walking`, or `Biking`. |

The embed shows the current ETA (with live traffic for driving), distance, the
"normal" time without traffic when relevant, and the refresh interval. It
re-edits the same message every tick until someone presses **Stop**.

## Setup

1. Copy `.env.example` to `.env` and fill in:
   - `TOKEN` — your Discord bot token
   - `PREFIX` — prefix for legacy text commands
   - `INVITE_LINK` — your bot's invite URL
   - `GOOGLE_MAPS_API_KEY` — a Google Maps API key with **Distance Matrix API** enabled. Create one at https://console.cloud.google.com/google/maps-apis/credentials
2. `pip install -r requirements.txt`
3. `python bot.py`
4. After the bot connects, run the owner-only `sync` text command (from `cogs/owner.py`) once so Discord registers the slash command. After that, `/traveltime` is available in any channel where the bot can post.

## Notes

- For driving mode, the bot requests `departure_time=now` so Google returns
  live `duration_in_traffic`.
- The bot does not auto-stop. Use the Stop button — or restart the bot — to end
  a loop. Add an auto-stop condition in `cogs/eta.py` if you want one.
- All running sessions are cancelled cleanly when the cog unloads.