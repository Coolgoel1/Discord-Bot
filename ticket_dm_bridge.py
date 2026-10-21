# ================================================================
#  ticket_dm_bridge.py  ──  Two-way DM ↔ Ticket-channel bridge
#
#  HOW TO USE:
#    1. Place this file next to main.py
#    2. At the TOP of main.py add:
#         from ticket_dm_bridge import setup_dm_bridge
#    3. At the END of on_ready() in main.py add:
#         await setup_dm_bridge(bot, conn)
#       NOTE: pass your existing `conn` from main.py so we share
#       the same sqlite connection — no stale reads.
# ================================================================

import discord
from discord import app_commands
from datetime import datetime, timezone
import sqlite3

GUILD_ID        = 1418611875649687777
MANAGER_ROLE_ID = 1496443008667750600
BOT_DEV_ROLE_ID = 1497519433487351869

_bot:    discord.Client      = None
_conn:   sqlite3.Connection  = None

def now():
    return datetime.now(timezone.utc)

def db():
    """Always return a fresh cursor from the shared connection."""
    return _conn.cursor()

def is_manager(member: discord.Member) -> bool:
    role_ids = [r.id for r in member.roles]
    return (
        member.guild.owner_id == member.id or
        MANAGER_ROLE_ID in role_ids or
        BOT_DEV_ROLE_ID in role_ids
    )


# ══════════════════════════════════════════════════════════════════
#  MODAL — user types a reply from their DMs
# ══════════════════════════════════════════════════════════════════

class UserDMReplyModal(discord.ui.Modal, title="Send a message to Support"):
    message_text = discord.ui.TextInput(
        label       = "Your message",
        style       = discord.TextStyle.paragraph,
        placeholder = "Type your message to the support team here…",
        max_length  = 1000
    )

    def __init__(self):
        super().__init__()

    async def on_submit(self, interaction: discord.Interaction):
        user = interaction.user

        cur = db()
        cur.execute(
            "SELECT channel_id, guild_id FROM tickets WHERE user_id = ? AND status = 'open'",
            (user.id,)
        )
        row = cur.fetchone()

        if not row:
            return await interaction.response.send_message(
                "❌ You don't have an open ticket right now.", ephemeral=True
            )

        channel_id, guild_id = row
        guild_obj = _bot.get_guild(guild_id)
        channel   = guild_obj.get_channel(channel_id) if guild_obj else None

        if not channel:
            return await interaction.response.send_message(
                "❌ Could not find your ticket channel. It may have been deleted.", ephemeral=True
            )

        relay_embed = discord.Embed(
            title       = "📩 User Reply",
            description = self.message_text.value,
            color       = 0xf39c12,
            timestamp   = now()
        )
        relay_embed.set_author(name=str(user), icon_url=user.display_avatar.url)
        relay_embed.set_footer(text=f"User ID: {user.id} · Relayed from DM")
        await channel.send(embed=relay_embed)

        await interaction.response.send_message(
            "✅ Your message has been sent to the support team!", ephemeral=True
        )


# ══════════════════════════════════════════════════════════════════
#  PERSISTENT VIEW — "Reply to Support" button in user DMs
# ══════════════════════════════════════════════════════════════════

class DMReplyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label     = "✉️ Reply to Support",
        style     = discord.ButtonStyle.primary,
        custom_id = "dm_reply_button"
    )
    async def reply(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(UserDMReplyModal())


# ══════════════════════════════════════════════════════════════════
#  /send — manager uses this in ticket channel to DM the user
# ══════════════════════════════════════════════════════════════════

def register_send_command(bot: discord.Client):
    guild_obj = discord.Object(id=GUILD_ID)

    @bot.tree.command(
        name        = "send",
        description = "Send a message to the ticket user's DMs (use inside a ticket channel)",
        guild       = guild_obj
    )
    @app_commands.describe(message="Message to send to the user")
    async def slash_send_staff(interaction: discord.Interaction, message: str):
        if not is_manager(interaction.user):
            return await interaction.response.send_message("❌ No permission.", ephemeral=True)

        cur = db()
        cur.execute(
            "SELECT user_id FROM tickets WHERE channel_id = ? AND status = 'open'",
            (interaction.channel.id,)
        )
        row = cur.fetchone()

        if not row:
            return await interaction.response.send_message(
                "❌ This command can only be used inside an open ticket channel.", ephemeral=True
            )

        user_id = row[0]
        user    = bot.get_user(user_id) or await bot.fetch_user(user_id)

        if not user:
            return await interaction.response.send_message(
                "❌ Could not find the ticket user.", ephemeral=True
            )

        dm_embed = discord.Embed(
            title       = "📨 Message from Support",
            description = message,
            color       = 0x5865F2,
            timestamp   = now()
        )
        dm_embed.set_author(
            name     = str(interaction.user),
            icon_url = interaction.user.display_avatar.url
        )
        dm_embed.set_footer(text="Use the button below to reply")

        sent = False
        try:
            await user.send(embed=dm_embed, view=DMReplyView())
            sent = True
        except discord.Forbidden:
            await interaction.channel.send(
                f"⚠️ Could not DM {user.mention} — they have DMs disabled.", delete_after=10
            )
        except Exception as e:
            print(f"[bridge /send] error: {e}")

        if sent:
            echo_embed = discord.Embed(
                title       = "📤 Sent to user's DMs",
                description = message,
                color       = 0x2ecc71,
                timestamp   = now()
            )
            echo_embed.set_footer(text=f"By {interaction.user}")
            await interaction.channel.send(embed=echo_embed)

        await interaction.response.send_message(
            "✅ Message delivered to user's DMs." if sent else "⚠️ DM failed — user may have DMs disabled.",
            ephemeral=True
        )


# ══════════════════════════════════════════════════════════════════
#  /debugticket — run this in a ticket channel to verify DB state
#  Remove this command once everything works.
# ══════════════════════════════════════════════════════════════════

def register_debug_command(bot: discord.Client):
    guild_obj = discord.Object(id=GUILD_ID)

    @bot.tree.command(
        name        = "debugticket",
        description = "Debug: show what the DB sees for this channel",
        guild       = guild_obj
    )
    async def slash_debug(interaction: discord.Interaction):
        if not is_manager(interaction.user):
            return await interaction.response.send_message("❌ No permission.", ephemeral=True)

        cur = db()

        # Check if THIS channel is a ticket
        cur.execute(
            "SELECT id, user_id, guild_id, status FROM tickets WHERE channel_id = ?",
            (interaction.channel.id,)
        )
        row = cur.fetchone()

        # Also dump ALL open tickets
        cur.execute("SELECT id, user_id, channel_id, status FROM tickets")
        all_rows = cur.fetchall()

        msg = f"**Channel ID:** `{interaction.channel.id}`\n\n"

        if row:
            ticket_id, user_id, guild_id, status = row
            user = bot.get_user(user_id) or await bot.fetch_user(user_id)
            msg += (
                f"✅ **This channel IS a ticket**\n"
                f"Ticket ID: `{ticket_id}`\n"
                f"User ID: `{user_id}` ({user})\n"
                f"Guild ID: `{guild_id}`\n"
                f"Status: `{status}`\n"
            )
        else:
            msg += "❌ **This channel is NOT found in the tickets table.**\n"

        msg += f"\n**All tickets in DB ({len(all_rows)}):**\n"
        for r in all_rows:
            msg += f"• ID={r[0]} user={r[1]} channel={r[2]} status={r[3]}\n"

        await interaction.response.send_message(msg, ephemeral=True)


# ══════════════════════════════════════════════════════════════════
#  MESSAGE LISTENER
#  Staff types in ticket channel → DM the ticket owner
#  User DMs the bot → relay to ticket channel
# ══════════════════════════════════════════════════════════════════

def register_listener(bot: discord.Client):

    @bot.listen("on_message")
    async def ticket_bridge(message: discord.Message):

        if message.author.bot:
            return

        # ── CASE 1: Staff typed in a guild channel ────────────────────
        if message.guild is not None:

            cur = db()
            cur.execute(
                "SELECT user_id FROM tickets WHERE channel_id = ? AND status = 'open'",
                (message.channel.id,)
            )
            row = cur.fetchone()

            if not row:
                return  # not a ticket channel

            ticket_owner_id = row[0]

            # Don't relay if the sender IS the ticket owner
            if message.author.id == ticket_owner_id:
                return

            # Fetch the ticket owner
            ticket_owner = bot.get_user(ticket_owner_id)
            if ticket_owner is None:
                try:
                    ticket_owner = await bot.fetch_user(ticket_owner_id)
                except Exception as e:
                    print(f"[bridge] fetch_user failed: {e}")
                    return

            dm_embed = discord.Embed(
                title       = "📨 Message from Support",
                description = message.content or "*(no text — may be an attachment)*",
                color       = 0x5865F2,
                timestamp   = now()
            )
            dm_embed.set_author(
                name     = str(message.author),
                icon_url = message.author.display_avatar.url
            )
            dm_embed.set_footer(text="Use the button below to reply")

            try:
                await ticket_owner.send(embed=dm_embed, view=DMReplyView())
                print(f"[bridge] ✅ Staff msg forwarded to {ticket_owner}")
            except discord.Forbidden:
                await message.channel.send(
                    f"⚠️ Could not DM {ticket_owner.mention} — they have DMs disabled.",
                    delete_after=10
                )
            except Exception as e:
                print(f"[bridge] DM error: {e}")

        # ── CASE 2: User sent a plain DM to the bot ───────────────────
        elif isinstance(message.channel, discord.DMChannel):

            cur = db()
            cur.execute(
                "SELECT channel_id, guild_id FROM tickets WHERE user_id = ? AND status = 'open'",
                (message.author.id,)
            )
            row = cur.fetchone()

            if not row:
                return  # no open ticket, ignore

            channel_id, guild_id = row
            guild_obj = bot.get_guild(guild_id)
            channel   = guild_obj.get_channel(channel_id) if guild_obj else None

            if not channel:
                await message.channel.send(
                    "❌ Could not find your ticket channel — it may have been deleted."
                )
                return

            relay_embed = discord.Embed(
                title       = "📩 User Message (via DM)",
                description = message.content or "*(attachment or empty message)*",
                color       = 0xf39c12,
                timestamp   = now()
            )
            relay_embed.set_author(
                name     = str(message.author),
                icon_url = message.author.display_avatar.url
            )
            relay_embed.set_footer(text=f"User ID: {message.author.id}")
            await channel.send(embed=relay_embed)
            await message.add_reaction("✅")
            print(f"[bridge] ✅ DM from {message.author} relayed to ticket channel")


# ══════════════════════════════════════════════════════════════════
#  SETUP — call this from on_ready() in main.py
#  Pass your existing `conn` object so we share one DB connection.
# ══════════════════════════════════════════════════════════════════

async def setup_dm_bridge(bot: discord.Client, conn: sqlite3.Connection):
    global _bot, _conn
    _bot  = bot
    _conn = conn                  # share main.py's connection — no stale reads
    bot.add_view(DMReplyView())   # persistent view survives restarts
    register_send_command(bot)
    register_debug_command(bot)
    register_listener(bot)
    print("✅ DM bridge loaded.")
