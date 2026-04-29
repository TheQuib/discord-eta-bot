"""
ETA cog: /traveltime command that posts a self-updating travel-time embed.

Backed by the Google Maps Distance Matrix API. Set GOOGLE_MAPS_API_KEY in .env.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

DISTANCE_MATRIX_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"

MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 30

MODE_CHOICES = [
    app_commands.Choice(name="Driving", value="driving"),
    app_commands.Choice(name="Transit", value="transit"),
    app_commands.Choice(name="Walking", value="walking"),
    app_commands.Choice(name="Biking", value="bicycling"),
]

# Embed colors per status.
COLOR_OK = 0x2ECC71
COLOR_PENDING = 0x95A5A6
COLOR_ERROR = 0xE02B2B
COLOR_STOPPED = 0x7F8C8D


class ETAStopView(discord.ui.View):
    """View with a single 'Stop' button. Anyone in the channel can press it."""

    def __init__(self, session: "ETASession"):
        super().__init__(timeout=None)
        self.session = session

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, emoji="⏹️")
    async def stop_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        # Ack the click first so Discord doesn't show "interaction failed"
        # while we edit the message.
        try:
            await interaction.response.defer()
        except discord.InteractionResponded:
            pass
        await self.session.stop(reason=f"Stopped by {interaction.user.display_name}")


class ETASession:
    """One running /traveltime instance: edits a single message on a loop."""

    def __init__(
        self,
        cog: "ETA",
        message: discord.Message,
        origin: str,
        destination: str,
        mode: str,
        interval_minutes: int,
        requested_by: discord.abc.User,
    ) -> None:
        self.cog = cog
        self.message = message
        self.origin = origin
        self.destination = destination
        self.mode = mode
        self.interval_minutes = interval_minutes
        self.interval_seconds = interval_minutes * 60
        self.requested_by = requested_by
        self.task: Optional[asyncio.Task] = None
        self._stopped = False

    def start(self) -> None:
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            while not self._stopped:
                await self._tick()
                if self._stopped:
                    break
                # Sleep in small chunks so stop() responds quickly.
                slept = 0
                while slept < self.interval_seconds and not self._stopped:
                    chunk = min(2, self.interval_seconds - slept)
                    await asyncio.sleep(chunk)
                    slept += chunk
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.cog.bot.logger.exception(f"ETA loop crashed: {e}")
            await self.stop(reason=f"Stopped: loop crashed ({type(e).__name__})")

    async def _tick(self) -> None:
        try:
            data = await self.cog.fetch_eta(self.origin, self.destination, self.mode)
            embed = self._build_embed(data)
        except Exception as e:
            await self._render_error(str(e))
            return

        try:
            await self.message.edit(embed=embed, view=ETAStopView(self))
        except discord.NotFound:
            self._stopped = True
        except discord.HTTPException as e:
            self.cog.bot.logger.warning(f"Failed to edit ETA message: {e}")

    def _build_embed(self, data: dict) -> discord.Embed:
        rows = data.get("rows") or []
        element = (
            rows[0]["elements"][0]
            if rows and rows[0].get("elements")
            else {}
        )
        status = element.get("status", data.get("status", "UNKNOWN"))

        if status != "OK":
            embed = discord.Embed(
                title="Travel time unavailable",
                description=f"Google returned status: `{status}`",
                color=COLOR_ERROR,
            )
            embed.add_field(name="From", value=self.origin, inline=True)
            embed.add_field(name="To", value=self.destination, inline=True)
            return embed

        duration = element.get("duration", {}).get("text", "?")
        duration_in_traffic = element.get("duration_in_traffic", {}).get("text")
        distance = element.get("distance", {}).get("text", "?")

        # Duration in seconds for arrival time calculation.
        duration_secs = (
            element.get("duration_in_traffic", {}).get("value")
            or element.get("duration", {}).get("value")
        )

        origin_addr = (data.get("origin_addresses") or [self.origin])[0] or self.origin
        dest_addr = (data.get("destination_addresses") or [self.destination])[0] or self.destination

        primary_duration = duration_in_traffic or duration
        title = f"{primary_duration} — {self.mode.capitalize()}"

        now = datetime.now(timezone.utc)
        next_refresh = now + timedelta(minutes=self.interval_minutes)

        embed = discord.Embed(title=title, color=COLOR_OK)
        embed.add_field(name="From", value=origin_addr, inline=False)
        embed.add_field(name="To", value=dest_addr, inline=False)
        embed.add_field(name="Distance", value=distance, inline=True)
        if duration_in_traffic and duration_in_traffic != duration:
            embed.add_field(name="Without traffic", value=duration, inline=True)
        if duration_secs:
            arrival = now + timedelta(seconds=duration_secs)
            embed.add_field(
                name="Would arrive by",
                value=f"<t:{int(arrival.timestamp())}:t>",
                inline=True,
            )
        embed.add_field(
            name="Next refresh",
            value=f"<t:{int(next_refresh.timestamp())}:R>",
            inline=True,
        )
        embed.set_footer(text=f"Requested by {self.requested_by.display_name}")
        embed.timestamp = now
        return embed

    async def _render_error(self, msg: str) -> None:
        embed = discord.Embed(
            title="ETA error",
            description=msg[:1900],
            color=COLOR_ERROR,
        )
        embed.add_field(name="From", value=self.origin, inline=True)
        embed.add_field(name="To", value=self.destination, inline=True)
        try:
            await self.message.edit(embed=embed, view=ETAStopView(self))
        except Exception:
            pass

    async def stop(self, reason: str = "Stopped") -> None:
        if self._stopped:
            return
        self._stopped = True
        if self.task and not self.task.done():
            self.task.cancel()

        # Rebuild embed without live fields, then disable the button.
        embed = self.message.embeds[0] if self.message.embeds else discord.Embed(title="Stopped")
        embed.color = COLOR_STOPPED
        # Strip fields that become misleading once stopped.
        stale_fields = {"Next refresh", "Would arrive by"}
        kept = [(f.name, f.value, f.inline) for f in embed.fields if f.name not in stale_fields]
        embed.clear_fields()
        for name, value, inline in kept:
            embed.add_field(name=name, value=value, inline=inline)
        embed.set_footer(text=reason)
        view = ETAStopView(self)
        for child in view.children:
            child.disabled = True
        try:
            await self.message.edit(embed=embed, view=view)
        except Exception:
            pass


class ETA(commands.Cog, name="eta"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.api_key = os.getenv("GOOGLE_MAPS_API_KEY")
        self._session: Optional[aiohttp.ClientSession] = None
        self._sessions: list[ETASession] = []

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession()

    async def cog_unload(self) -> None:
        # Cancel any running sessions and close the HTTP client.
        for s in list(self._sessions):
            await s.stop(reason="Bot reloaded")
        if self._session and not self._session.closed:
            await self._session.close()

    async def fetch_eta(self, origin: str, destination: str, mode: str) -> dict:
        if not self.api_key:
            raise RuntimeError(
                "GOOGLE_MAPS_API_KEY is not set. Add it to .env on the host."
            )
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

        params = {
            "origins": origin,
            "destinations": destination,
            "mode": mode,
            "key": self.api_key,
            "units": "imperial",
        }
        # departure_time=now unlocks live traffic for driving requests.
        if mode == "driving":
            params["departure_time"] = "now"

        async with self._session.get(
            DISTANCE_MATRIX_URL,
            params=params,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            data = await resp.json()

        if data.get("status") != "OK":
            err = data.get("error_message") or ""
            raise RuntimeError(f"Google API error: {data.get('status')} {err}".strip())
        return data

    @app_commands.command(
        name="traveltime",
        description="Post a live ETA between two locations that updates on an interval.",
    )
    @app_commands.describe(
        from_location="Origin: address, place name, or 'lat,lng'.",
        to_location="Destination: address, place name, or 'lat,lng'.",
        time_to_update="How often to refresh, in minutes (1-30). Default 5.",
        mode="Travel mode. Default: driving.",
    )
    @app_commands.choices(mode=MODE_CHOICES)
    async def traveltime(
        self,
        interaction: discord.Interaction,
        from_location: str,
        to_location: str,
        time_to_update: app_commands.Range[int, MIN_INTERVAL_MINUTES, MAX_INTERVAL_MINUTES] = 5,
        mode: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        mode_value = mode.value if mode else "driving"

        if not self.api_key:
            await interaction.response.send_message(
                "GOOGLE_MAPS_API_KEY is not set on the host. The bot owner needs to add it to `.env`.",
                ephemeral=True,
            )
            return

        # Send a placeholder we can edit on each tick.
        placeholder = discord.Embed(
            title="Fetching ETA…",
            description=f"From **{from_location}** to **{to_location}** ({mode_value})",
            color=COLOR_PENDING,
        )
        await interaction.response.send_message(embed=placeholder)
        message = await interaction.original_response()

        session = ETASession(
            cog=self,
            message=message,
            origin=from_location,
            destination=to_location,
            mode=mode_value,
            interval_minutes=time_to_update,
            requested_by=interaction.user,
        )
        self._sessions.append(session)

        # Attach the Stop button immediately so users can cancel even before
        # the first tick lands.
        try:
            await message.edit(view=ETAStopView(session))
        except Exception:
            pass

        session.start()
        self.bot.logger.info(
            f"Started /traveltime: {from_location!r} -> {to_location!r} "
            f"every {time_to_update}m via {mode_value} "
            f"for {interaction.user} (ID: {interaction.user.id})"
        )


async def setup(bot) -> None:
    await bot.add_cog(ETA(bot))
