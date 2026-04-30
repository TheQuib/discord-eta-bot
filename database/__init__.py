"""
Session persistence for the ETA bot.
"""
from __future__ import annotations

import aiosqlite


class SessionStore:
    """Thin wrapper around the eta_sessions SQLite table."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self.connection = connection

    async def save(self, session) -> None:
        """Insert or replace a session row (keyed on message_id)."""
        await self.connection.execute(
            """
            INSERT OR REPLACE INTO eta_sessions
                (message_id, channel_id, guild_id, origin, destination, mode,
                 interval_minutes, requested_by_id, show_stop_button)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.message.id,
                session.message.channel.id,
                session.message.guild.id if session.message.guild else 0,
                session.origin,
                session.destination,
                session.mode,
                session.interval_minutes,
                session.requested_by.id,
                int(session.show_stop_button),
            ),
        )
        await self.connection.commit()

    async def delete(self, message_id: int) -> None:
        """Remove a session row by message_id."""
        await self.connection.execute(
            "DELETE FROM eta_sessions WHERE message_id = ?", (message_id,)
        )
        await self.connection.commit()

    async def load_all(self) -> list[dict]:
        """Return every persisted session as a plain dict."""
        async with self.connection.execute(
            """
            SELECT message_id, channel_id, guild_id, origin, destination,
                   mode, interval_minutes, requested_by_id, show_stop_button
            FROM eta_sessions
            """
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "message_id":      row[0],
                "channel_id":      row[1],
                "guild_id":        row[2],
                "origin":          row[3],
                "destination":     row[4],
                "mode":            row[5],
                "interval_minutes": row[6],
                "requested_by_id": row[7],
                "show_stop_button": bool(row[8]),
            }
            for row in rows
        ]
