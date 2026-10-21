import discord
from discord.ext import commands, tasks
from discord import app_commands
import logging
from dotenv import load_dotenv
import os
import sqlite3
import asyncio
from datetime import datetime, timezone, timedelta
import webserver
from verification import register_verification
from nickname import register_nick_command

# ================= LOAD =================

load_dotenv()
token = os.getenv('DISCORD_TOKEN')

# ================= CONFIG =================

GUILD_ID             = 1418611875649687777
LOG_CHANNEL_ID       = 1497505822576611368
MANAGER_ROLE_ID      = 1496443008667750600
BOT_DEV_ROLE_ID      = 1497519433487351869

TICKET_CATEGORY_NAME = "Tickets"
TICKET_SUPPORT_ROLE  = MANAGER_ROLE_ID

# Birthday config -- set BIRTHDAY_CHANNEL_ID to the channel where
# birthday announcements should be sent.
BIRTHDAY_CHANNEL_ID  = LOG_CHANNEL_ID   # <- change to your birthday channel ID
BIRTHDAY_ROLE_NAME   = "Birthday"    # role must exist in your server

handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot   = commands.Bot(command_prefix='!', intents=intents)
guild = discord.Object(id=GUILD_ID)

# ================= TIME =================

def now() -> datetime:
    return datetime.now(timezone.utc)

# ================= DATABASE =================

conn   = sqlite3.connect("mod_system.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS modlogs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER,
    guild_id      INTEGER,
    moderator_id  INTEGER,
    action        TEXT,
    reason        TEXT,
    created_at    TEXT,
    expires_at    TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS tickets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER,
    guild_id    INTEGER,
    channel_id  INTEGER,
    status      TEXT DEFAULT 'open',
    created_at  TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS birthdays (
    user_id    INTEGER,
    guild_id   INTEGER,
    month      INTEGER,
    day        INTEGER,
    PRIMARY KEY (user_id, guild_id)
)
""")

conn.commit()

# ================= PERMISSIONS =================

def is_manager(member: discord.Member) -> bool:
    role_ids = [r.id for r in member.roles]
    return (
        member.guild.owner_id == member.id
        or MANAGER_ROLE_ID in role_ids
        or BOT_DEV_ROLE_ID in role_ids
    )

def can_moderate(author: discord.Member, target: discord.Member) -> bool:
    if BOT_DEV_ROLE_ID in [r.id for r in author.roles]:
        return True
    if author.guild_permissions.administrator:
        return True
    return author.top_role > target.top_role

# ================= UTILS =================

def parse_duration(s: str) -> int:
    s = s.strip().lower()
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    try:
        value = int(s[:-1])
        unit  = s[-1]
        if unit not in units:
            raise ValueError
        return value * units[unit]
    except Exception:
        raise ValueError("Invalid duration format")

def safe_commit():
    try:
        conn.commit()
    except Exception as e:
        print(f"[DB] Commit error: {e}")

def create_case(user_id, guild_id, mod_id, action, reason, expires=None):
    cursor.execute("""
    INSERT INTO modlogs (user_id, guild_id, moderator_id, action, reason, created_at, expires_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        user_id, guild_id, mod_id, action, reason,
        now().isoformat(),
        expires.isoformat() if expires else None,
    ))
    case_id = cursor.lastrowid
    safe_commit()
    return case_id

MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
]

def days_in_month(month: int) -> int:
    return [0, 31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month]

# ===============================================================
#  TICKET RELAY HELPERS
# ===============================================================

async def relay_staff_to_user(ticket_owner_id: int, sender: discord.Member, text: str) -> bool:
    """Send a staff message from the ticket channel -> ticket owner's DM."""
    owner = bot.get_user(ticket_owner_id)
    if owner is None:
        try:
            owner = await bot.fetch_user(ticket_owner_id)
        except Exception as e:
            print(f"[relay_staff_to_user] fetch_user failed: {e}")
            return False

    embed = discord.Embed(
        title       = "Support replied to your ticket",
        description = text or "*(attachment -- no text)*",
        color       = 0x5865F2,
        timestamp   = now(),
    )
    embed.set_author(
        name     = f"{sender.display_name}  -  {sender}",
        icon_url = sender.display_avatar.url,
    )
    embed.set_footer(text="Use the button below to reply v")

    try:
        await owner.send(embed=embed, view=DMReplyView())
        return True
    except discord.Forbidden:
        return False
    except Exception as e:
        print(f"[relay_staff_to_user] DM error: {e}")
        return False


async def relay_user_to_channel(user: discord.User | discord.Member, text: str) -> bool:
    """Forward a user DM -> the open ticket channel."""
    cursor.execute(
        "SELECT channel_id, guild_id FROM tickets WHERE user_id = ? AND status = 'open'",
        (user.id,),
    )
    row = cursor.fetchone()
    if not row:
        return False

    channel_id, guild_id = row
    g       = bot.get_guild(guild_id)
    channel = g.get_channel(channel_id) if g else None
    if not channel:
        return False

    embed = discord.Embed(
        title       = "User replied (via DM)",
        description = text or "*(attachment -- no text)*",
        color       = 0xF39C12,
        timestamp   = now(),
    )
    embed.set_author(
        name     = f"{user.display_name}  -  {user}",
        icon_url = user.display_avatar.url,
    )
    embed.set_footer(text=f"User ID: {user.id}")

    try:
        # Attach a "Reply" button so staff can respond directly from here
        await channel.send(embed=embed, view=StaffReplyView(ticket_owner_id=user.id))
        return True
    except Exception as e:
        print(f"[relay_user_to_channel] error: {e}")
        return False

# ===============================================================
#  TICKET VIEWS & MODALS
# ===============================================================

class StaffReplyModal(discord.ui.Modal, title="Reply to User"):
    """Modal that appears when a manager clicks 'Reply to User' in the ticket channel."""

    reply_text = discord.ui.TextInput(
        label       = "Your reply",
        style       = discord.TextStyle.paragraph,
        placeholder = "Type your response to the user here...",
        max_length  = 1000,
    )

    def __init__(self, ticket_owner_id: int):
        super().__init__()
        self.ticket_owner_id = ticket_owner_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # 1. Echo the reply as a proper embed in the ticket channel
        channel_embed = discord.Embed(
            title       = "Reply sent to user",
            description = self.reply_text.value,
            color       = 0x5865F2,
            timestamp   = now(),
        )
        channel_embed.set_author(
            name     = f"{interaction.user.display_name}  -  {interaction.user}",
            icon_url = interaction.user.display_avatar.url,
        )
        await interaction.channel.send(embed=channel_embed)

        # 2. Forward to the user's DM
        success = await relay_staff_to_user(
            self.ticket_owner_id, interaction.user, self.reply_text.value
        )

        if success:
            await interaction.followup.send(" Reply sent to the user's DMs.", ephemeral=True)
        else:
            await interaction.followup.send(
                " Reply posted here but **could not DM the user** "
                "(they may have DMs disabled).",
                ephemeral=True,
            )


class StaffReplyView(discord.ui.View):
    """
    Non-persistent view attached to every user-relay embed in a ticket channel.
    Gives staff a one-click 'Reply to User' button right on the message.
    Non-persistent is intentional -- these buttons are per-message and
    are replaced/recreated on each relay so we always know the owner id.
    """

    def __init__(self, ticket_owner_id: int):
        super().__init__(timeout=None)
        self.ticket_owner_id = ticket_owner_id
        # Dynamically add the button so we can pass ticket_owner_id to the modal
        self.add_item(StaffReplyButton(ticket_owner_id))


class StaffReplyButton(discord.ui.Button):
    def __init__(self, ticket_owner_id: int):
        super().__init__(
            label    = "Reply to User",
            style    = discord.ButtonStyle.primary,
            # No custom_id needed -- non-persistent view (intentional)
        )
        self.ticket_owner_id = ticket_owner_id

    async def callback(self, interaction: discord.Interaction):
        if not is_manager(interaction.user):
            return await interaction.response.send_message(
                " Only managers can reply here.", ephemeral=True
            )
        await interaction.response.send_modal(StaffReplyModal(self.ticket_owner_id))


class DMReplyModal(discord.ui.Modal, title="Reply to Support"):
    """Modal that appears when the user clicks 'Reply to Support' in their DMs."""

    message_text = discord.ui.TextInput(
        label       = "Your message",
        style       = discord.TextStyle.paragraph,
        placeholder = "Type your reply to the support team here...",
        max_length  = 1000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        success = await relay_user_to_channel(interaction.user, self.message_text.value)
        if success:
            await interaction.followup.send(" Your reply was sent to support!", ephemeral=True)
        else:
            await interaction.followup.send(
                " Couldn't find your ticket -- it may have been closed.", ephemeral=True
            )


class DMReplyView(discord.ui.View):
    """Persistent 'Reply to Support' button sent with every DM the user receives."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label     = "Reply to Support",
        style     = discord.ButtonStyle.primary,
        custom_id = "dm_reply_button",
    )
    async def reply(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DMReplyModal())


class TicketOpenView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label     = "Open a Ticket",
        style     = discord.ButtonStyle.primary,
        custom_id = "ticket_open_button",
    )
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        g = interaction.guild

        # One-ticket-per-user guard
        cursor.execute(
            "SELECT channel_id FROM tickets WHERE user_id = ? AND guild_id = ? AND status = 'open'",
            (interaction.user.id, g.id),
        )
        if cursor.fetchone():
            return await interaction.followup.send(
                " You already have an open ticket. Please wait for a response or close it first.",
                ephemeral=True,
            )

        # Category
        category = discord.utils.get(g.categories, name=TICKET_CATEGORY_NAME)
        if category is None:
            category = await g.create_category(TICKET_CATEGORY_NAME)

        # Sequential ticket ID
        cursor.execute("SELECT COUNT(*) FROM tickets WHERE guild_id = ?", (g.id,))
        count     = cursor.fetchone()[0] + 1
        ticket_id = f"ticket-{count:04d}"

        # Channel permissions -- user deliberately cannot see it (DM-only)
        support_role = g.get_role(TICKET_SUPPORT_ROLE)
        overwrites   = {
            g.default_role:   discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=False),
            g.me:             discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }
        if support_role:
            overwrites[support_role] = discord.PermissionOverwrite(
                read_messages=True, send_messages=True
            )

        channel = await g.create_text_channel(
            ticket_id, category=category, overwrites=overwrites
        )

        # Save
        cursor.execute("""
            INSERT INTO tickets (user_id, guild_id, channel_id, status, created_at)
            VALUES (?, ?, ?, 'open', ?)
        """, (interaction.user.id, g.id, channel.id, now().isoformat()))
        safe_commit()

        # Header in ticket channel (with close button)
        header = discord.Embed(
            title       = f"{ticket_id.upper()}",
            description = (
                f"**Opened by:** {interaction.user.mention} (`{interaction.user}`)\n"
                f"**User ID:** `{interaction.user.id}`\n\n"
                " Type your reply or click **Reply to User** on any message the user sends.\n"
                " Every message you send here is **automatically forwarded to the user's DMs**.\n"
                " The user's DM replies will appear here with a **Reply to User** button.\n\n"
                "Click **Close Ticket** when resolved."
            ),
            color     = 0x5865F2,
            timestamp = now(),
        )
        header.set_thumbnail(url=interaction.user.display_avatar.url)
        await channel.send(embed=header, view=TicketCloseView())

        # DM the user
        try:
            dm = discord.Embed(
                title       = f"Ticket `{ticket_id}` opened",
                description = (
                    "Your support ticket has been created!\n\n"
                    "A staff member will respond shortly.\n"
                    "**All replies will appear right here in your DMs.**\n\n"
                    "Use the **Reply to Support** button below to respond at any time."
                ),
                color     = 0x5865F2,
                timestamp = now(),
            )
            await interaction.user.send(embed=dm, view=DMReplyView())
        except discord.Forbidden:
            await channel.send(
                f" Could not DM {interaction.user.mention} -- they have DMs disabled."
            )

        await interaction.followup.send(
            f" Ticket `{ticket_id}` opened! Check your DMs.", ephemeral=True
        )

        log = bot.get_channel(LOG_CHANNEL_ID)
        if log:
            e = discord.Embed(title="Ticket Opened", color=0x5865F2, timestamp=now())
            e.add_field(name="User",    value=f"{interaction.user.mention} (`{interaction.user}`)")
            e.add_field(name="Ticket",  value=ticket_id)
            e.add_field(name="Channel", value=channel.mention)
            await log.send(embed=e)


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label     = "Close Ticket",
        style     = discord.ButtonStyle.danger,
        custom_id = "ticket_close_button",
    )
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        channel = interaction.channel
        cursor.execute(
            "SELECT id, user_id FROM tickets WHERE channel_id = ? AND status = 'open'",
            (channel.id,),
        )
        row = cursor.fetchone()
        if not row:
            return await interaction.followup.send(
                " This is not an open ticket channel.", ephemeral=True
            )

        ticket_db_id, user_id = row

        if interaction.user.id != user_id and not is_manager(interaction.user):
            return await interaction.followup.send(
                " Only the ticket owner or a manager can close this ticket.", ephemeral=True
            )

        cursor.execute(
            "UPDATE tickets SET status = 'closed' WHERE id = ?", (ticket_db_id,)
        )
        safe_commit()

        # DM the user
        user = bot.get_user(user_id)
        if user is None:
            try:
                user = await bot.fetch_user(user_id)
            except Exception:
                user = None
        if user:
            try:
                close_embed = discord.Embed(
                    title       = "Your ticket has been closed",
                    description = (
                        f"**Ticket:** `{channel.name}`\n"
                        f"**Closed by:** {interaction.user.display_name}\n\n"
                        "If you need further help, open a new ticket anytime."
                    ),
                    color     = 0xE74C3C,
                    timestamp = now(),
                )
                await user.send(embed=close_embed)
            except Exception:
                pass

        await interaction.followup.send(
            " Ticket closed. This channel will be deleted in **5 seconds**."
        )
        await discord.utils.sleep_until(now() + timedelta(seconds=5))
        try:
            await channel.delete(reason=f"Ticket closed by {interaction.user}")
        except Exception as e:
            print(f"[close_ticket] delete error: {e}")

        log = bot.get_channel(LOG_CHANNEL_ID)
        if log:
            e = discord.Embed(title="Ticket Closed", color=0xE74C3C, timestamp=now())
            e.add_field(name="Closed by", value=interaction.user.mention)
            e.add_field(name="Ticket",    value=channel.name)
            await log.send(embed=e)

# ===============================================================
#  EVENTS
# ===============================================================

@bot.event
async def on_ready():
    print(f" Logged in as {bot.user}")

    # Persistent views -- must register every view with a stable custom_id
    bot.add_view(TicketOpenView())
    bot.add_view(TicketCloseView())
    bot.add_view(DMReplyView())
    # VerifyView is registered inside register_verification() below

    try:
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync(guild=None)
        print(" Cleared global slash commands.")
        synced = await bot.tree.sync(guild=guild)
        print(f" Synced {len(synced)} slash commands to guild {GUILD_ID}")
    except Exception as e:
        print(f" Sync error: {e}")

    # Start birthday checker loop
    if not birthday_check.is_running():
        birthday_check.start()


# ===============================================================
#  ON MESSAGE  --  Bi-directional ticket bridge
#
#  CASE A: staff types in a ticket channel -> forward to user DM
#  CASE B: user sends a plain DM to the bot -> forward to ticket channel
# ===============================================================

@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)

    if message.author.bot:
        return

    # -- CASE A: guild message in a ticket channel -----------------
    if message.guild is not None:
        cursor.execute(
            "SELECT user_id FROM tickets WHERE channel_id = ? AND status = 'open'",
            (message.channel.id,),
        )
        row = cursor.fetchone()
        if not row:
            return

        ticket_owner_id = row[0]
        if message.author.id == ticket_owner_id:
            return   # owner can't see the channel anyway

        # Forward staff text -> user DM
        success = await relay_staff_to_user(
            ticket_owner_id, message.author, message.content
        )
        if not success:
            await message.channel.send(
                " Could not DM the user -- they may have DMs disabled.",
                delete_after=8,
            )

    # -- CASE B: plain DM typed directly to the bot ---------------
    elif isinstance(message.channel, discord.DMChannel):
        success = await relay_user_to_channel(message.author, message.content)
        if success:
            try:
                await message.add_reaction("")
            except Exception:
                pass


# ===============================================================
#  BIRTHDAY BACKGROUND TASK
# ===============================================================

@tasks.loop(hours=24)
async def birthday_check():
    """Fires once every 24 hours and handles all birthdays for today."""
    today   = datetime.now(timezone.utc)
    month   = today.month
    day     = today.day

    cursor.execute(
        "SELECT user_id, guild_id FROM birthdays WHERE month = ? AND day = ?",
        (month, day),
    )
    rows = cursor.fetchall()

    for user_id, guild_id in rows:
        g = bot.get_guild(guild_id)
        if g is None:
            continue

        member = g.get_member(user_id)
        if member is None:
            try:
                member = await g.fetch_member(user_id)
            except Exception:
                continue

        # -- Assign birthday role ----------------------------------
        bday_role = discord.utils.get(g.roles, name=BIRTHDAY_ROLE_NAME)
        if bday_role:
            try:
                await member.add_roles(bday_role, reason="Birthday role for today")
            except Exception as e:
                print(f"[birthday] Could not add role to {member}: {e}")

        # -- Announce in birthday channel --------------------------
        bday_ch = g.get_channel(BIRTHDAY_CHANNEL_ID)
        if bday_ch:
            embed = discord.Embed(
                title       = "Happy Birthday!",
                description = (
                    f" Everyone wish {member.mention} a very **Happy Birthday**! \n\n"
                    f"Hope your day is amazing, {member.display_name}! "
                ),
                color     = 0xFF69B4,
                timestamp = now(),
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            await bday_ch.send(content=member.mention, embed=embed)

        # -- Schedule role removal after 24 h ----------------------
        if bday_role:
            async def remove_bday_role(m=member, r=bday_role):
                await asyncio.sleep(86400)
                try:
                    await m.remove_roles(r, reason="Birthday role expired (24h)")
                except Exception as e:
                    print(f"[birthday] Could not remove role from {m}: {e}")
            asyncio.create_task(remove_bday_role())


@birthday_check.before_loop
async def before_birthday_check():
    await bot.wait_until_ready()
    # Align the loop to fire at midnight UTC each day
    n      = now()
    target = n.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    wait   = (target - n).total_seconds()
    print(f"[birthday] First check in {wait/3600:.1f}h (at midnight UTC)")
    await asyncio.sleep(wait)


# ===============================================================
#  SLASH COMMANDS
# ===============================================================

tree = bot.tree


# -- /ping --------------------------------------------------------
@tree.command(guild=guild, name="ping", description="Check if the bot is alive")
async def slash_ping(interaction: discord.Interaction):
    await interaction.response.send_message(" Pong!", ephemeral=True)


# -- /say ---------------------------------------------------------
@tree.command(guild=guild, name="say", description="Make the bot send a message in this channel")
@app_commands.describe(message="What the bot should say")
async def slash_say(interaction: discord.Interaction, message: str):
    if not is_manager(interaction.user):
        return await interaction.response.send_message(" No permission.", ephemeral=True)
    await interaction.channel.send(message)
    await interaction.response.send_message(" Message sent.", ephemeral=True)


# -- /directmessage -----------------------------------------------
@tree.command(guild=guild, name="directmessage", description="Send a private DM to any user via the bot")
@app_commands.describe(
    user    = "The user to DM",
    message = "The message to include in the DM",
)
async def slash_dm(
    interaction: discord.Interaction,
    user:        discord.Member,
    message:     str,
):
    if not is_manager(interaction.user):
        return await interaction.response.send_message(" No permission.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    embed = discord.Embed(
        title       = f"Message from {interaction.guild.name}",
        description = message,
        color       = 0x5865F2,
        timestamp   = now(),
    )
    embed.set_author(
        name     = f"Sent by {interaction.user.display_name}",
        icon_url = interaction.user.display_avatar.url,
    )
    embed.set_footer(text=f"{interaction.guild.name}  -  Staff Message")

    try:
        await user.send(
            content = f"Hey {user.display_name}, you have a message from the staff team:",
            embed   = embed,
        )
    except discord.Forbidden:
        return await interaction.followup.send(
            f" Could not DM {user.mention} -- they have DMs disabled.", ephemeral=True
        )
    except Exception as e:
        return await interaction.followup.send(f" Error: {e}", ephemeral=True)

    await interaction.followup.send(
        f" DM sent to {user.mention}.", ephemeral=True
    )

    log = bot.get_channel(LOG_CHANNEL_ID)
    if log:
        le = discord.Embed(title="Direct Message Sent", color=0x5865F2, timestamp=now())
        le.add_field(name="Sent by",    value=interaction.user.mention)
        le.add_field(name="Recipient",  value=user.mention)
        le.add_field(name="Message",    value=message, inline=False)
        await log.send(embed=le)


# -- /setup_tickets -----------------------------------------------
@tree.command(guild=guild, name="setup_tickets", description="Post the ticket open button in this channel")
async def slash_setup_tickets(interaction: discord.Interaction):
    if not is_manager(interaction.user):
        return await interaction.response.send_message(" No permission.", ephemeral=True)

    embed = discord.Embed(
        title       = "Support Tickets",
        description = (
            "Need help from our team? Click the button below to open a ticket.\n\n"
            " All communication happens **privately in your DMs** -- "
            "no one else can see your conversation."
        ),
        color = 0x5865F2,
    )
    await interaction.channel.send(embed=embed, view=TicketOpenView())
    await interaction.response.send_message("Ticket panel posted.", ephemeral=True)


# -- /deleteticket ------------------------------------------------

class DeleteTicketSelect(discord.ui.Select):
    """Dropdown listing all open tickets. Selecting one deletes it."""

    def __init__(self, tickets: list[tuple]):
        # tickets: list of (db_id, user_id, channel_id, created_at, display_name)
        self.ticket_map = {}   # option value -> (db_id, channel_id, user_id)

        options = []
        for db_id, user_id, channel_id, created_at, display_name in tickets:
            # Shorten created_at to just the date portion for the label
            try:
                date_str = created_at[:10]   # "YYYY-MM-DD"
            except Exception:
                date_str = "unknown date"

            label       = f"ticket-{db_id:04d} -- {display_name}"[:100]
            description = f"Opened on {date_str}"[:100]
            value       = str(db_id)

            self.ticket_map[value] = (db_id, channel_id, user_id)
            options.append(discord.SelectOption(
                label       = label,
                description = description,
                value       = value,
            ))

        super().__init__(
            placeholder = "Select a ticket to delete...",
            min_values  = 1,
            max_values  = 1,
            options     = options,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        db_id, channel_id, user_id = self.ticket_map[self.values[0]]

        # Mark closed in DB
        cursor.execute(
            "UPDATE tickets SET status = 'closed' WHERE id = ?", (db_id,)
        )
        safe_commit()

        # Notify the ticket owner via DM
        user = bot.get_user(user_id)
        if user is None:
            try:
                user = await bot.fetch_user(user_id)
            except Exception:
                user = None

        if user:
            try:
                dm = discord.Embed(
                    title       = "Your ticket has been closed",
                    description = (
                        f"Your ticket (ID {db_id:04d}) was closed by a manager.\n\n"
                        "If you still need help, feel free to open a new ticket."
                    ),
                    color     = 0xE74C3C,
                    timestamp = now(),
                )
                await user.send(embed=dm)
            except Exception:
                pass

        # Delete the channel
        channel = interaction.guild.get_channel(channel_id)
        if channel:
            try:
                await channel.delete(reason=f"Ticket {db_id:04d} manually deleted by {interaction.user}")
            except discord.Forbidden:
                await interaction.followup.send(
                    "Ticket marked closed in the database, but I do not have "
                    "permission to delete the channel.", ephemeral=True
                )
                return
            except Exception as e:
                await interaction.followup.send(
                    f"Ticket marked closed but channel deletion failed: {e}", ephemeral=True
                )
                return
        else:
            # Channel already gone; DB is already updated, just continue
            pass

        await interaction.followup.send(
            f"Ticket {db_id:04d} has been deleted.", ephemeral=True
        )

        log = bot.get_channel(LOG_CHANNEL_ID)
        if log:
            e = discord.Embed(title="Ticket Force-Deleted", color=0xE74C3C, timestamp=now())
            e.add_field(name="Deleted by", value=interaction.user.mention)
            e.add_field(name="Ticket ID",  value=f"{db_id:04d}")
            e.add_field(name="Owner",      value=f"<@{user_id}>")
            await log.send(embed=e)


class DeleteTicketView(discord.ui.View):
    def __init__(self, tickets: list[tuple]):
        super().__init__(timeout=60)
        self.add_item(DeleteTicketSelect(tickets))


@tree.command(
    guild=guild,
    name="deleteticket",
    description="Show all open tickets and delete the one you select (managers only)",
)
async def slash_deleteticket(interaction: discord.Interaction):
    if not is_manager(interaction.user):
        return await interaction.response.send_message("No permission.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    # Fetch all open tickets for this guild
    cursor.execute(
        "SELECT id, user_id, channel_id, created_at FROM tickets "
        "WHERE guild_id = ? AND status = 'open' ORDER BY id ASC",
        (interaction.guild.id,),
    )
    rows = cursor.fetchall()

    if not rows:
        return await interaction.followup.send(
            "There are no open tickets right now.", ephemeral=True
        )

    # Resolve display names — try cache first, fall back to fetch
    tickets = []
    for db_id, user_id, channel_id, created_at in rows:
        member = interaction.guild.get_member(user_id)
        if member:
            display_name = str(member)
        else:
            try:
                user = await bot.fetch_user(user_id)
                display_name = str(user)
            except Exception:
                display_name = f"Unknown ({user_id})"

        tickets.append((db_id, user_id, channel_id, created_at, display_name))

    # Discord Select menus are limited to 25 options
    if len(tickets) > 25:
        tickets = tickets[:25]
        note = "\n(Showing the 25 oldest open tickets. Delete some to see more.)"
    else:
        note = ""

    embed = discord.Embed(
        title       = "Delete a Ticket",
        description = (
            f"There are **{len(rows)}** open ticket(s).\n"
            "Select one from the dropdown below to delete it. "
            "The ticket owner will be notified via DM." + note
        ),
        color = 0xE74C3C,
    )

    await interaction.followup.send(embed=embed, view=DeleteTicketView(tickets), ephemeral=True)


# -- /warn --------------------------------------------------------
@tree.command(guild=guild, name="warn", description="Issue a warning to a member")
@app_commands.describe(member="Member to warn", duration="e.g. 10m, 2h, 1d", reason="Reason")
async def slash_warn(
    interaction: discord.Interaction,
    member: discord.Member,
    duration: str,
    reason: str = "No reason provided",
):
    if not is_manager(interaction.user):
        return await interaction.response.send_message(" No permission.", ephemeral=True)
    if not can_moderate(interaction.user, member):
        return await interaction.response.send_message(" Cannot moderate this user.", ephemeral=True)

    try:
        seconds = parse_duration(duration)
        expires = now() + timedelta(seconds=seconds)
    except ValueError:
        return await interaction.response.send_message(
            " Invalid duration (e.g. `10m`, `2h`, `1d`).", ephemeral=True
        )

    case_id = create_case(member.id, interaction.guild.id, interaction.user.id, "warn", reason, expires)

    try:
        dm = discord.Embed(title="You have been warned", color=0xFFCC00, timestamp=now())
        dm.add_field(name="Server",   value=interaction.guild.name, inline=False)
        dm.add_field(name="Reason",   value=reason,                 inline=False)
        dm.add_field(name="Duration", value=duration,               inline=False)
        dm.add_field(name="Case",     value=f"CASE-{case_id}",      inline=False)
        await member.send(embed=dm)
    except Exception:
        pass

    log = bot.get_channel(LOG_CHANNEL_ID)
    if log:
        e = discord.Embed(title="Warning Issued", color=0xFFCC00, timestamp=now())
        e.add_field(name="User",      value=f"{member.mention} (`{member}`)")
        e.add_field(name="Moderator", value=interaction.user.mention)
        e.add_field(name="Reason",    value=reason)
        e.add_field(name="Duration",  value=duration)
        e.add_field(name="Case",      value=f"CASE-{case_id}")
        await log.send(embed=e)

    await interaction.response.send_message(
        f" {member.mention} warned | CASE-{case_id}", ephemeral=True
    )


# -- /rmlwarn -----------------------------------------------------
@tree.command(guild=guild, name="rmlwarn", description="Remove a warning by case ID")
@app_commands.describe(case_id="Case ID to remove")
async def slash_rmlwarn(interaction: discord.Interaction, case_id: int):
    if not is_manager(interaction.user):
        return await interaction.response.send_message(" No permission.", ephemeral=True)
    cursor.execute("DELETE FROM modlogs WHERE id = ? AND action = 'warn'", (case_id,))
    conn.commit()
    await interaction.response.send_message(f" Warning CASE-{case_id} removed.", ephemeral=True)


# -- /history -----------------------------------------------------
@tree.command(guild=guild, name="history", description="View moderation history of a member")
@app_commands.describe(member="Member to look up")
async def slash_history(interaction: discord.Interaction, member: discord.Member):
    if not is_manager(interaction.user):
        return await interaction.response.send_message(" No permission.", ephemeral=True)

    cursor.execute("""
        SELECT id, action, reason, created_at, expires_at
        FROM modlogs WHERE user_id = ? AND guild_id = ?
        ORDER BY id DESC LIMIT 10
    """, (member.id, interaction.guild.id))
    rows = cursor.fetchall()

    if not rows:
        return await interaction.response.send_message(
            f"No moderation history for {member}.", ephemeral=True
        )

    embed    = discord.Embed(title=f"Moderation History -- {member}", color=0x3498DB)
    now_time = now()
    for row in rows:
        try:
            case_id, action, reason, created, expires = row
            try:
                created_ts = int(datetime.fromisoformat(created).timestamp())
            except Exception:
                created_ts = 0
            if expires:
                try:
                    exp    = datetime.fromisoformat(expires)
                    status = "expired" if exp <= now_time else " active"
                except Exception:
                    status = "unknown"
            else:
                status = "permanent"
            embed.add_field(
                name  = f"CASE-{case_id} | {action.upper()}",
                value = f"**Reason:** {reason}\n**When:** <t:{created_ts}:R>\n**Status:** {status}",
                inline= False,
            )
        except Exception as e:
            print(f"History row error: {e}")

    await interaction.response.send_message(embed=embed, ephemeral=True)


# -- /kick --------------------------------------------------------
@tree.command(guild=guild, name="kick", description="Kick a member from the server")
@app_commands.describe(member="Member to kick", reason="Reason for the kick")
async def slash_kick(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason provided",
):
    if not is_manager(interaction.user):
        return await interaction.response.send_message(" No permission.", ephemeral=True)
    if not can_moderate(interaction.user, member):
        return await interaction.response.send_message(" Cannot moderate this user.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    case_id = create_case(member.id, interaction.guild.id, interaction.user.id, "kick", reason)

    try:
        dm = discord.Embed(title="You have been kicked", color=0xE67E22, timestamp=now())
        dm.add_field(name="Server", value=interaction.guild.name, inline=False)
        dm.add_field(name="Reason", value=reason,                 inline=False)
        dm.add_field(name="Case",   value=f"CASE-{case_id}",      inline=False)
        await member.send(embed=dm)
    except Exception:
        pass

    try:
        await member.kick(reason=f"[CASE-{case_id}] {reason}")
    except discord.Forbidden:
        return await interaction.followup.send(" No permission to kick this user.", ephemeral=True)

    log = bot.get_channel(LOG_CHANNEL_ID)
    if log:
        e = discord.Embed(title="Member Kicked", color=0xE67E22, timestamp=now())
        e.add_field(name="User",      value=f"{member.mention} (`{member}`)")
        e.add_field(name="Moderator", value=interaction.user.mention)
        e.add_field(name="Reason",    value=reason)
        e.add_field(name="Case",      value=f"CASE-{case_id}")
        await log.send(embed=e)

    await interaction.followup.send(f" {member} kicked | CASE-{case_id}", ephemeral=True)


# -- /timeout -----------------------------------------------------
@tree.command(guild=guild, name="timeout", description="Timeout (mute) a member")
@app_commands.describe(member="Member to timeout", duration="e.g. 10m, 2h (max 28d)", reason="Reason")
async def slash_timeout(
    interaction: discord.Interaction,
    member: discord.Member,
    duration: str,
    reason: str = "No reason provided",
):
    if not is_manager(interaction.user):
        return await interaction.response.send_message(" No permission.", ephemeral=True)
    if not can_moderate(interaction.user, member):
        return await interaction.response.send_message(" Cannot moderate this user.", ephemeral=True)

    try:
        seconds = parse_duration(duration)
    except ValueError:
        return await interaction.response.send_message(
            " Invalid duration (e.g. `10m`, `2h`, `1d`).", ephemeral=True
        )
    if seconds > 28 * 86400:
        return await interaction.response.send_message(
            " Discord limits timeouts to 28 days max.", ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)
    until   = now() + timedelta(seconds=seconds)
    case_id = create_case(member.id, interaction.guild.id, interaction.user.id, "timeout", reason, until)

    try:
        await member.timeout(until, reason=f"[CASE-{case_id}] {reason}")
    except discord.Forbidden:
        return await interaction.followup.send(" No permission to timeout this user.", ephemeral=True)

    try:
        dm = discord.Embed(title="You have been timed out", color=0x9B59B6, timestamp=now())
        dm.add_field(name="Server",   value=interaction.guild.name,              inline=False)
        dm.add_field(name="Duration", value=duration,                             inline=False)
        dm.add_field(name="Reason",   value=reason,                              inline=False)
        dm.add_field(name="Until",    value=f"<t:{int(until.timestamp())}:F>",   inline=False)
        dm.add_field(name="Case",     value=f"CASE-{case_id}",                   inline=False)
        await member.send(embed=dm)
    except Exception:
        pass

    log = bot.get_channel(LOG_CHANNEL_ID)
    if log:
        e = discord.Embed(title="Member Timed Out", color=0x9B59B6, timestamp=now())
        e.add_field(name="User",      value=f"{member.mention} (`{member}`)")
        e.add_field(name="Moderator", value=interaction.user.mention)
        e.add_field(name="Duration",  value=duration)
        e.add_field(name="Reason",    value=reason)
        e.add_field(name="Until",     value=f"<t:{int(until.timestamp())}:F>")
        e.add_field(name="Case",      value=f"CASE-{case_id}")
        await log.send(embed=e)

    await interaction.followup.send(
        f" {member} timed out for **{duration}** | CASE-{case_id}", ephemeral=True
    )


# -- /ban ---------------------------------------------------------
@tree.command(guild=guild, name="ban", description="Permanently ban a member")
@app_commands.describe(
    member      = "Member to ban",
    reason      = "Reason for the ban",
    delete_days = "Delete their messages from the last N days (0-7)",
)
async def slash_ban(
    interaction: discord.Interaction,
    member:      discord.Member,
    reason:      str = "No reason provided",
    delete_days: app_commands.Range[int, 0, 7] = 0,
):
    if not is_manager(interaction.user):
        return await interaction.response.send_message(" No permission.", ephemeral=True)
    if not can_moderate(interaction.user, member):
        return await interaction.response.send_message(" Cannot moderate this user.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    case_id = create_case(member.id, interaction.guild.id, interaction.user.id, "ban", reason)

    try:
        dm = discord.Embed(title="You have been banned", color=0xE74C3C, timestamp=now())
        dm.add_field(name="Server", value=interaction.guild.name, inline=False)
        dm.add_field(name="Reason", value=reason,                 inline=False)
        dm.add_field(name="Case",   value=f"CASE-{case_id}",      inline=False)
        await member.send(embed=dm)
    except Exception:
        pass

    try:
        await member.ban(reason=f"[CASE-{case_id}] {reason}", delete_message_days=delete_days)
    except discord.Forbidden:
        return await interaction.followup.send(" No permission to ban this user.", ephemeral=True)

    log = bot.get_channel(LOG_CHANNEL_ID)
    if log:
        e = discord.Embed(title="Member Banned", color=0xE74C3C, timestamp=now())
        e.add_field(name="User",            value=f"{member.mention} (`{member}`)")
        e.add_field(name="Moderator",       value=interaction.user.mention)
        e.add_field(name="Reason",          value=reason)
        e.add_field(name="Messages Purged", value=f"{delete_days}d")
        e.add_field(name="Case",            value=f"CASE-{case_id}")
        await log.send(embed=e)

    await interaction.followup.send(f" {member} banned | CASE-{case_id}", ephemeral=True)


# -- /temp-ban ----------------------------------------------------
@tree.command(guild=guild, name="temp-ban", description="Temporarily ban a member")
@app_commands.describe(
    member      = "Member to temp-ban",
    duration    = "Ban duration e.g. 1d, 12h",
    reason      = "Reason",
    delete_days = "Delete their messages (0-7 days)",
)
async def slash_tempban(
    interaction: discord.Interaction,
    member:      discord.Member,
    duration:    str,
    reason:      str = "No reason provided",
    delete_days: app_commands.Range[int, 0, 7] = 0,
):
    if not is_manager(interaction.user):
        return await interaction.response.send_message(" No permission.", ephemeral=True)
    if not can_moderate(interaction.user, member):
        return await interaction.response.send_message(" Cannot moderate this user.", ephemeral=True)

    try:
        seconds = parse_duration(duration)
        expires = now() + timedelta(seconds=seconds)
    except ValueError:
        return await interaction.response.send_message(
            " Invalid duration (e.g. `1d`, `12h`).", ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)
    case_id = create_case(member.id, interaction.guild.id, interaction.user.id, "tempban", reason, expires)

    try:
        dm = discord.Embed(title="You have been temporarily banned", color=0xC0392B, timestamp=now())
        dm.add_field(name="Server",   value=interaction.guild.name,              inline=False)
        dm.add_field(name="Duration", value=duration,                             inline=False)
        dm.add_field(name="Reason",   value=reason,                              inline=False)
        dm.add_field(name="Expires",  value=f"<t:{int(expires.timestamp())}:F>", inline=False)
        dm.add_field(name="Case",     value=f"CASE-{case_id}",                   inline=False)
        await member.send(embed=dm)
    except Exception:
        pass

    try:
        await member.ban(
            reason=f"[TEMPBAN][CASE-{case_id}] {reason} | Expires: {expires.isoformat()}",
            delete_message_days=delete_days,
        )
    except discord.Forbidden:
        return await interaction.followup.send(" No permission to ban this user.", ephemeral=True)

    log = bot.get_channel(LOG_CHANNEL_ID)
    if log:
        e = discord.Embed(title="Member Temp-Banned", color=0xC0392B, timestamp=now())
        e.add_field(name="User",      value=f"{member.mention} (`{member}`)")
        e.add_field(name="Moderator", value=interaction.user.mention)
        e.add_field(name="Duration",  value=duration)
        e.add_field(name="Reason",    value=reason)
        e.add_field(name="Expires",   value=f"<t:{int(expires.timestamp())}:F>")
        e.add_field(name="Case",      value=f"CASE-{case_id}")
        await log.send(embed=e)

    await interaction.followup.send(
        f" {member} temp-banned for **{duration}** | CASE-{case_id}", ephemeral=True
    )

    g = interaction.guild

    async def _auto_unban():
        await discord.utils.sleep_until(expires)
        try:
            await g.unban(member, reason=f"Temp-ban expired | CASE-{case_id}")
            log2 = bot.get_channel(LOG_CHANNEL_ID)
            if log2:
                ue = discord.Embed(
                    title="Temp-Ban Expired", color=0x2ECC71, timestamp=now()
                )
                ue.add_field(name="User", value=f"`{member}`")
                ue.add_field(name="Case", value=f"CASE-{case_id}")
                await log2.send(embed=ue)
        except Exception as exc:
            print(f"[auto_unban] CASE-{case_id}: {exc}")

    asyncio.create_task(_auto_unban())


# -- /purge -------------------------------------------------------
@tree.command(guild=guild, name="purge", description="Delete recent messages in this channel")
@app_commands.describe(amount="Number of messages to delete (1-100)")
async def slash_purge(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    if not is_manager(interaction.user):
        return await interaction.response.send_message(" No permission.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await interaction.channel.purge(limit=amount)
    except discord.Forbidden:
        return await interaction.followup.send(" No permission to delete messages here.", ephemeral=True)

    await interaction.followup.send(f" Deleted {len(deleted)} message(s).", ephemeral=True)
    log = bot.get_channel(LOG_CHANNEL_ID)
    if log:
        e = discord.Embed(title="Purge", color=0xE67E22, timestamp=now())
        e.add_field(name="Moderator", value=interaction.user.mention)
        e.add_field(name="Channel",   value=interaction.channel.mention)
        e.add_field(name="Deleted",   value=str(len(deleted)))
        await log.send(embed=e)


# ===============================================================
#  BIRTHDAY COMMANDS
# ===============================================================

# Choices for month selection
MONTH_CHOICES = [
    app_commands.Choice(name=name, value=i)
    for i, name in enumerate(MONTH_NAMES)
    if i > 0
]


# -- /birthday-set ------------------------------------------------
@tree.command(guild=guild, name="birthday-set", description="Set your own birthday")
@app_commands.describe(month="Your birth month", day="Your birth day (1-31)")
@app_commands.choices(month=MONTH_CHOICES)
async def slash_bday_set(
    interaction: discord.Interaction,
    month: app_commands.Choice[int],
    day:   app_commands.Range[int, 1, 31],
):
    if day > days_in_month(month.value):
        return await interaction.response.send_message(
            f" {month.name} only has {days_in_month(month.value)} days.", ephemeral=True
        )

    # Check if already set
    cursor.execute(
        "SELECT 1 FROM birthdays WHERE user_id = ? AND guild_id = ?",
        (interaction.user.id, interaction.guild.id),
    )
    if cursor.fetchone():
        return await interaction.response.send_message(
            " You already have a birthday set. Use `/birthday-edit` to change it.",
            ephemeral=True,
        )

    cursor.execute(
        "INSERT INTO birthdays (user_id, guild_id, month, day) VALUES (?, ?, ?, ?)",
        (interaction.user.id, interaction.guild.id, month.value, day),
    )
    safe_commit()

    await interaction.response.send_message(
        f" Birthday set to **{month.name} {day}**! "
        "We'll celebrate you on your big day! ",
        ephemeral=True,
    )


# -- /birthday-edit -----------------------------------------------
@tree.command(guild=guild, name="birthday-edit", description="Edit your birthday")
@app_commands.describe(month="Your new birth month", day="Your new birth day (1-31)")
@app_commands.choices(month=MONTH_CHOICES)
async def slash_bday_edit(
    interaction: discord.Interaction,
    month: app_commands.Choice[int],
    day:   app_commands.Range[int, 1, 31],
):
    if day > days_in_month(month.value):
        return await interaction.response.send_message(
            f" {month.name} only has {days_in_month(month.value)} days.", ephemeral=True
        )

    cursor.execute(
        "SELECT 1 FROM birthdays WHERE user_id = ? AND guild_id = ?",
        (interaction.user.id, interaction.guild.id),
    )
    if not cursor.fetchone():
        return await interaction.response.send_message(
            " You don't have a birthday set yet. Use `/birthday-set` first.",
            ephemeral=True,
        )

    cursor.execute(
        "UPDATE birthdays SET month = ?, day = ? WHERE user_id = ? AND guild_id = ?",
        (month.value, day, interaction.user.id, interaction.guild.id),
    )
    safe_commit()

    await interaction.response.send_message(
        f" Birthday updated to **{month.name} {day}**!", ephemeral=True
    )


# -- /birthday-remove ---------------------------------------------
@tree.command(guild=guild, name="birthday-remove", description="Remove your birthday from the system")
async def slash_bday_remove(interaction: discord.Interaction):
    cursor.execute(
        "DELETE FROM birthdays WHERE user_id = ? AND guild_id = ?",
        (interaction.user.id, interaction.guild.id),
    )
    safe_commit()

    if cursor.rowcount == 0:
        return await interaction.response.send_message(
            " You don't have a birthday set.", ephemeral=True
        )

    await interaction.response.send_message(
        " Your birthday has been removed.", ephemeral=True
    )


# -- /birthday-set-manual -----------------------------------------
@tree.command(
    guild=guild,
    name="birthday-set-manual",
    description="(Managers) Set or overwrite a member's birthday",
)
@app_commands.describe(
    member = "The member whose birthday to set",
    month  = "Their birth month",
    day    = "Their birth day (1-31)",
)
@app_commands.choices(month=MONTH_CHOICES)
async def slash_bday_set_manual(
    interaction: discord.Interaction,
    member: discord.Member,
    month:  app_commands.Choice[int],
    day:    app_commands.Range[int, 1, 31],
):
    if not is_manager(interaction.user):
        return await interaction.response.send_message(" No permission.", ephemeral=True)

    if day > days_in_month(month.value):
        return await interaction.response.send_message(
            f" {month.name} only has {days_in_month(month.value)} days.", ephemeral=True
        )

    cursor.execute("""
        INSERT INTO birthdays (user_id, guild_id, month, day) VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, guild_id) DO UPDATE SET month=excluded.month, day=excluded.day
    """, (member.id, interaction.guild.id, month.value, day))
    safe_commit()

    await interaction.response.send_message(
        f" Birthday for {member.mention} set to **{month.name} {day}**.", ephemeral=True
    )


# -- /birthday-view -----------------------------------------------
@tree.command(guild=guild, name="birthday-view", description="View upcoming birthdays in this server")
async def slash_bday_view(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    cursor.execute(
        "SELECT user_id, month, day FROM birthdays WHERE guild_id = ? ORDER BY month, day",
        (interaction.guild.id,),
    )
    rows = cursor.fetchall()

    if not rows:
        return await interaction.followup.send(
            "No birthdays have been set yet.", ephemeral=True
        )

    today = now()
    today_md = (today.month, today.day)

    # Sort so upcoming (from today) come first, wrapping into next year
    def sort_key(r):
        _, m, d = r
        md = (m, d)
        if md >= today_md:
            return (0, m, d)
        return (1, m, d)   # already passed this year -> push to end

    rows_sorted = sorted(rows, key=sort_key)

    embed = discord.Embed(
        title     = "Server Birthdays",
        color     = 0xFF69B4,
        timestamp = now(),
    )

    lines = []
    for user_id, month, day in rows_sorted:
        member = interaction.guild.get_member(user_id)
        name   = member.display_name if member else f"<Unknown user {user_id}>"

        # Calculate how many days away
        this_year   = today.replace(month=month, day=day, hour=0, minute=0, second=0)
        if this_year < today.replace(hour=0, minute=0, second=0):
            next_bday = this_year.replace(year=today.year + 1)
        else:
            next_bday = this_year

        days_away = (next_bday.date() - today.date()).days
        if days_away == 0:
            when = "**Today!**"
        elif days_away == 1:
            when = "Tomorrow"
        else:
            when = f"in {days_away} days"

        lines.append(f"**{name}** -- {MONTH_NAMES[month]} {day}  ({when})")

    # Split into chunks so we don't exceed embed limits
    chunk = "\n".join(lines[:25])
    embed.description = chunk
    if len(lines) > 25:
        embed.set_footer(text=f"Showing first 25 of {len(lines)} birthdays.")

    await interaction.followup.send(embed=embed, ephemeral=True)


# ===============================================================
#  LEGACY PREFIX COMMANDS
# ===============================================================

@bot.command()
async def rmlwarn(ctx, case_id: int):
    if not is_manager(ctx.author):
        return await ctx.send("No permission")
    cursor.execute("DELETE FROM modlogs WHERE id = ? AND action = 'warn'", (case_id,))
    conn.commit()
    await ctx.send(f"Warning CASE-{case_id} removed.")


@bot.command()
async def warn(ctx, member: discord.Member, duration: str, *, reason="No reason"):
    if not is_manager(ctx.author):
        return await ctx.send("No permission")
    if not can_moderate(ctx.author, member):
        return await ctx.send("Cannot moderate this user")
    try:
        seconds = parse_duration(duration)
        expires = now() + timedelta(seconds=seconds)
    except Exception:
        return await ctx.send("Invalid duration format (use 10m, 2h, 1d)")
    case_id = create_case(member.id, ctx.guild.id, ctx.author.id, "warn", reason, expires)
    try:
        e = discord.Embed(title="Warning Received", color=0xFFCC00, timestamp=now())
        e.add_field(name="Reason",   value=reason,            inline=False)
        e.add_field(name="Duration", value=duration,          inline=False)
        e.add_field(name="Case ID",  value=f"CASE-{case_id}", inline=False)
        await member.send(embed=e)
    except Exception:
        pass
    log = bot.get_channel(LOG_CHANNEL_ID)
    if log:
        le = discord.Embed(title="Warning Issued", color=0xFFCC00, timestamp=now())
        le.add_field(name="User",      value=member.mention)
        le.add_field(name="Moderator", value=ctx.author.mention)
        le.add_field(name="Reason",    value=reason)
        le.add_field(name="Duration",  value=duration)
        le.add_field(name="Case",      value=f"CASE-{case_id}")
        await log.send(embed=le)
    await ctx.send(f"{member.mention} warned | CASE-{case_id}")


@bot.command()
async def history(ctx, member: discord.Member):
    if not is_manager(ctx.author):
        return await ctx.send("No permission")
    cursor.execute("""
        SELECT id, action, reason, created_at, expires_at
        FROM modlogs WHERE user_id = ? AND guild_id = ?
        ORDER BY id DESC LIMIT 10
    """, (member.id, ctx.guild.id))
    rows = cursor.fetchall()
    if not rows:
        return await ctx.send("No history found.")
    embed    = discord.Embed(title=f"Moderation History - {member}", color=0x3498DB)
    now_time = now()
    for row in rows:
        try:
            case_id, action, reason, created, expires = row
            try:
                created_ts = int(datetime.fromisoformat(created).timestamp())
            except Exception:
                created_ts = 0
            if expires:
                try:
                    exp    = datetime.fromisoformat(expires)
                    status = "expired" if exp <= now_time else "active"
                except Exception:
                    status = "unknown"
            else:
                status = "permanent"
            embed.add_field(
                name  = f"CASE-{case_id} ({action})",
                value = f"Reason: {reason}\nTime: <t:{created_ts}:R>\nStatus: {status}",
                inline= False,
            )
        except Exception as e:
            print(f"History row error: {e}")
    await ctx.send(embed=embed)


@bot.command()
async def ping(ctx):
    await ctx.send("Pong!")


# ===============================================================
#  ADDON REGISTRATIONS  --  after all tree commands are defined
# ===============================================================

register_verification(bot)
register_nick_command(bot)


# ================= RUN =================
webserver.keep_alive()
bot.run(token, log_handler=handler, log_level=logging.DEBUG)
