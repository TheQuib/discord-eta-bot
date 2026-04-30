import logging
import os
import platform
import random

import aiosqlite
import discord
from discord.ext import commands, tasks
from discord.ext.commands import Context
from dotenv import load_dotenv

from database import SessionStore

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True


class LoggingFormatter(logging.Formatter):
    # Colors
    black = "[30m"
    red = "[31m"
    green = "[32m"
    yellow = "[33m"
    blue = "[34m"
    gray = "[38m"
    # Styles
    reset = "[0m"
    bold = "[1m"

    COLORS = {
        logging.DEBUG: gray + bold,
        logging.INFO: blue + bold,
        logging.WARNING: yellow + bold,
        logging.ERROR: red,
        logging.CRITICAL: red + bold,
    }

    def format(self, record):
        log_color = self.COLORS[record.levelno]
        fmt = "(black){asctime}(reset) (levelcolor){levelname:<8}(reset) (green){name}(reset) {message}"
        fmt = fmt.replace("(black)", self.black + self.bold)
        fmt = fmt.replace("(reset)", self.reset)
        fmt = fmt.replace("(levelcolor)", log_color)
        fmt = fmt.replace("(green)", self.green + self.bold)
        formatter = logging.Formatter(fmt, "%Y-%m-%d %H:%M:%S", style="{")
        return formatter.format(record)


logger = logging.getLogger("discord_bot")
logger.setLevel(logging.INFO)

# Console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(LoggingFormatter())
# File handler
file_handler = logging.FileHandler(filename="discord.log", encoding="utf-8", mode="w")
file_handler.setFormatter(logging.Formatter(
    "[{asctime}] [{levelname:<8}] {name}: {message}", "%Y-%m-%d %H:%M:%S", style="{"
))

logger.addHandler(console_handler)
logger.addHandler(file_handler)


class DiscordBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned_or((os.getenv("PREFIX") or "!").strip() + " "),
            intents=intents,
            help_command=None,
        )
        self.logger = logger
        self.bot_prefix = os.getenv("PREFIX")
        self.invite_link = os.getenv("INVITE_LINK")
        self.db: aiosqlite.Connection | None = None
        self.session_store: SessionStore | None = None

    async def load_cogs(self) -> None:
        for file in os.listdir(f"{os.path.realpath(os.path.dirname(__file__))}/cogs"):
            if file.endswith(".py"):
                extension = file[:-3]
                try:
                    await self.load_extension(f"cogs.{extension}")
                    self.logger.info(f"Loaded extension '{extension}'")
                except Exception as e:
                    self.logger.error(f"Failed to load extension {extension} | {type(e).__name__}: {e}")

    @tasks.loop(minutes=1.0)
    async def status_task(self) -> None:
        statuses = ["/traveltime magic", "tracking ETAs", "currently mapping"]
        await self.change_presence(activity=discord.Game(random.choice(statuses)))

    @status_task.before_loop
    async def before_status_task(self) -> None:
        await self.wait_until_ready()

    async def setup_hook(self) -> None:
        self.logger.info(f"Logged in as {self.user.name}")
        self.logger.info(f"discord.py API version: {discord.__version__}")
        self.logger.info(f"Python version: {platform.python_version()}")
        self.logger.info(f"Running on: {platform.system()} {platform.release()} ({os.name})")
        self.logger.info("-------------------")

        # Initialise database.
        base_dir = os.path.realpath(os.path.dirname(__file__))
        db_path = os.path.join(base_dir, "database", "bot.db")
        self.db = await aiosqlite.connect(db_path)
        await self.db.execute("PRAGMA journal_mode=WAL")
        schema_path = os.path.join(base_dir, "database", "schema.sql")
        with open(schema_path) as f:
            await self.db.executescript(f.read())
        self.session_store = SessionStore(self.db)
        self.logger.info("Database ready.")

        await self.load_cogs()
        self.status_task.start()

    async def close(self) -> None:
        if self.db:
            await self.db.close()
        await super().close()

    async def on_message(self, message: discord.Message) -> None:
        if message.author == self.user or message.author.bot:
            return
        await self.process_commands(message)

    async def on_command_completion(self, context: Context) -> None:
        full_command_name = context.command.qualified_name
        executed_command = full_command_name.split(" ")[0]
        if context.guild is not None:
            self.logger.info(
                f"Executed {executed_command} command in {context.guild.name} "
                f"(ID: {context.guild.id}) by {context.author} (ID: {context.author.id})"
            )
        else:
            self.logger.info(
                f"Executed {executed_command} command by {context.author} "
                f"(ID: {context.author.id}) in DMs"
            )

    async def on_command_error(self, context: Context, error) -> None:
        if isinstance(error, commands.CommandOnCooldown):
            minutes, seconds = divmod(error.retry_after, 60)
            hours, minutes = divmod(minutes, 60)
            hours = hours % 24
            embed = discord.Embed(
                description=(
                    f"**Please slow down** - You can use this command again in "
                    f"{str(round(hours)) + ' hours' if round(hours) > 0 else ''} "
                    f"{str(round(minutes)) + ' minutes' if round(minutes) > 0 else ''} "
                    f"{str(round(seconds)) + ' seconds' if round(seconds) > 0 else ''}."
                ),
                color=0xE02B2B,
            )
            await context.send(embed=embed)
        elif isinstance(error, commands.NotOwner):
            embed = discord.Embed(description="You are not the owner of the bot!", color=0xE02B2B)
            await context.send(embed=embed)
            if context.guild:
                self.logger.warning(
                    f"{context.author} (ID: {context.author.id}) tried to execute an owner-only "
                    f"command in {context.guild.name} (ID: {context.guild.id})"
                )
            else:
                self.logger.warning(
                    f"{context.author} (ID: {context.author.id}) tried to execute an owner-only command in DMs"
                )
        elif isinstance(error, commands.MissingPermissions):
            embed = discord.Embed(
                description="You are missing the permission(s) `"
                + ", ".join(error.missing_permissions)
                + "` to execute this command!",
                color=0xE02B2B,
            )
            await context.send(embed=embed)
        elif isinstance(error, commands.BotMissingPermissions):
            embed = discord.Embed(
                description="I am missing the permission(s) `"
                + ", ".join(error.missing_permissions)
                + "` to fully perform this command!",
                color=0xE02B2B,
            )
            await context.send(embed=embed)
        elif isinstance(error, commands.MissingRequiredArgument):
            embed = discord.Embed(
                title="Error!",
                description=str(error).capitalize(),
                color=0xE02B2B,
            )
            await context.send(embed=embed)
        elif isinstance(error, commands.CommandNotFound):
            pass
        else:
            raise error


bot = DiscordBot()
bot.run(os.getenv("TOKEN"))
getenv("TOKEN"))