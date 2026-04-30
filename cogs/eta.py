"""
ETA cog: /traveltime command that posts a self-updating travel-time embed.

Backed by the Google Maps Distance Matrix API. Set GOOGLE_MAPS_API_KEY in .env.
Sessions are persisted to SQLite so they survive bot restarts.
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
    """Stop button — only shown when the session was started with show_stop_button=True."""

    def __init__(self, session: "ETASession"):
        super().__init__(timeout=None)
        self.session = session

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, emoji="\u23f9\ufe0f")
    async def stop_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        # Require Manage Messages to press the button.
        if not interaction.channel.permissions_for(interaction.user).manage_messages:
            await interaction.response.send_message(
                "You need the **Manage Messages** permission to stop this session.",
                ephemeral=True,
            )
            return
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
        show_stop_button: bool = False,
    ) -> None:
        self.cog = cog
        self.message = message
        self.origin = origin
        self.destination = destination
        self.mode = mode
        self.interval_minutes = interval_minutes
        self.interval_seconds = interval_minutes * 60
        self.requested_by = requested_by
        self.show_stop_button = show_stop_button
        self.task: Optional[asyncio.Task] = None
        self._stopped = False
        self._refresh_count = 0

    def start(self) -> None:
        self.task = asyncio.create_task(self._run())

    def _make_view(self, *, disabled: bool = False) -> Optional[ETAStopView]:
        """Return a view with the Stop button, or None if button is disabled."""
        if not self.show_stop_button:
            return None
        view = ETAStopView(self)
        if disabled:
            for child in view.children:
                child.disabled = True
        return view

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
        self._refresh_count += 1
        log_prefix = (
            f"[ETA #{self._refresh_count}] "
            f"{self.origin!r} -> {self.destination!r} ({self.mode})"
        )

        try:
            data = await self.cog.fetch_eta(self.origin, self.destination, self.mode)
        except Exception as e:
            self.cog.bot.logger.warning(f"{log_prefix} | fetch failed: {e}")
            await self._render_error(str(e))
            return

        # Log the result before building the embed.
        rows = data.get("rows") or []
        element = rows[0]["elements"][0] if rows and rows[0].get("elements") else {}
        if element.get("status") == "OK":
            dur = element.get("duration_in_traffic") or element.get("duration") or {}
            dist = element.get("distance") or {}
            dur_secs = dur.get("value")
            arrival_str = ""
            if dur_secs:
                arrival = datetime.now(timezone.utc) + timedelta(seconds=dur_secs)
                arrival_str = f" | arrive ~<t:{int(arrival.timestamp())}:t>"
            self.cog.bot.logger.info(
                f"{log_prefix} | {dur.get('text', '?')} | {dist.get('text', '?')}{arrival_str}"
            )
        else:
            self.cog.bot.logger.warning(
                f"{log_prefix} | status={element.get('status', data.get('status', 'UNKNOWN'))}"
            )

        embed = self._build_embed(data)
        view = self._make_view()
        try:
            await self.message.edit(embed=embed, view=view)
        except discord.NotFound:
            self.cog.bot.logger.info(
                f"{log_prefix} | message deleted — stopping session"
            )
            # Mark stopped directly; no point trying to edit the (deleted) message.
            self._stopped = True
            if self.task and not self.task.done():
                self.task.cancel()
            # Remove from persistence — the message is gone for good.
            if self.cog.bot.session_store:
                await self.cog.bot.session_store.delete(self.message.id)
            if self in self.cog._sessions:
                self.cog._sessions.remove(self)
        except discord.HTTPException as e:
            self.cog.bot.logger.warning(f"{log_prefix} | failed to edit message: {e}")

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
            await self.message.edit(embed=embed, view=self._make_view())
        except Exception:
            pass

    async def stop(self, reason: str = "Stopped", *, persist: bool = False) -> None:
        """Stop this session.

        Args:
            reason: Footer text shown on the stopped embed.
            persist: If True, keep the DB row (used during cog_unload so the
                     session is restored on the next startup).
        """
        if self._stopped:
            return
        self._stopped = True
        if self.task and not self.task.done():
            self.task.cancel()

        # Remove from persistence unless we want it to survive a restart.
        if not persist and self.cog.bot.session_store:
            await self.cog.bot.session_store.delete(self.message.id)

        if self in self.cog._sessions:
            self.cog._sessions.remove(self)

        # Rebuild embed without live fields, then disable the button.
        embed = self.message.embeds[0] if self.message.embeds else discord.Embed(title="Stopped")
        embed.color = COLOR_STOPPED
        stale_fields = {"Next refresh", "Would arrive by"}
        kept = [(f.name, f.value, f.inline) for f in embed.fields if f.name not in stale_fields]
        embed.clear_fields()
        for name, value, inline in kept:
            embed.add_field(name=name, value=value, inline=inline)
        embed.set_footer(text=reason)
        try:
            await self.message.edit(embed=embed, view=self._make_view(disabled=True))
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
        await self._restore_sessions()

    async def _restore_sessions(self) -> None:
        """Re-hydrate any sessions that were running when the bot last shut down."""
        store = getattr(self.bot, "session_store", None)
        if store is None:
            return

        rows = await store.load_all()
        if not rows:
            return

        self.bot.logger.info(f"[ETA restore] Restoring {len(rows)} session(s) from DB...")
        for row in rows:
            try:
                channel = (
                    self.bot.get_channel(row["channel_id"])
                    or await self.bot.fetch_channel(row["channel_id"])
                )
                message = await channel.fetch_message(row["message_id"])
                requested_by = (
                    self.bot.get_user(row["requested_by_id"])
                    or await self.bot.fetch_user(row["requested_by_id"])
                )
            except (discord.NotFound, discord.HTTPException, Exception) as e:
                self.bot.logger.warning(
                    f"[ETA restore] Dropping session {row['message_id']}: {e}"
                )
                await store.delete(row["message_id"])
                continue

            session = ETASession(
                cog=self,
                message=message,
                origin=row["origin"],
                destination=row["destination"],
                mode=row["mode"],
                interval_minutes=row["interval_minutes"],
                requested_by=requested_by,
                show_stop_button=row["show_stop_button"],
            )
            self._sessions.append(session)
            session.start()
            self.bot.logger.info(
                f"[ETA restore] Resumed: {row['origin']!r} -> {row['destination']!r} "
                f"(msg {row['message_id']})"
            )

    async def cog_unload(self) -> None:
        # persist=True keeps DB rows so sessions are restored on next startup.
        for s in list(self._sessions):
            await s.stop(reason="Bot restarting — will resume shortly", persist=True)
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
        show_stop_button="Add a Stop button to the embed (requires Manage Messages to use).",
    )
    @app_commands.choices(mode=MODE_CHOICES)
    async def traveltime(
        self,
        interaction: discord.Interaction,
        from_location: str,
        to_location: str,
        time_to_update: app_commands.Range[int, MIN_INTERVAL_MINUTES, MAX_INTERVAL_MINUTES] = 5,
        mode: Optional[app_commands.Choice[str]] = None,
        show_stop_button: bool = False,
    ) -> None:
        mode_value = mode.value if mode else "driving"

        if not self.api_key:
            await interaction.response.send_message(
                "GOOGLE_MAPS_API_KEY is not set on the host. The bot owner needs to add it to `.env`.",
                ephemeral=True,
            )
            return

        placeholder = discord.Embed(
            title="Fetching ETA\u2026",
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
            show_stop_button=show_stop_button,
        )
        self._sessions.append(session)
        session.start()

        # Persist so the session survives a restart.
        if self.bot.session_store:
            await self.bot.session_store.save(session)

        self.bot.logger.info(
            f"Started /traveltime: {from_location!r} -> {to_location!r} "
            f"every {time_to_update}m via {mode_value} "
            f"stop_button={show_stop_button} "
            f"for {interaction.user} (ID: {interaction.user.id})"
        )


async def setup(bot) -> None:
    await bot.add_cog(ETA(bot))
