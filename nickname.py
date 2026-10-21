# ================================================================
#  nickname.py  ──  /nick command to change any member's nickname
#
#  HOW TO ADD TO YOUR BOT:
#    1. Place this file next to your main.py
#    2. Add this to the TOP of main.py:
#         from nickname import register_nick_command
#    3. Call it anywhere AFTER bot is defined (e.g. just before bot.run):
#         register_nick_command(bot)
# ================================================================

import discord
from discord import app_commands
from datetime import datetime, timezone

GUILD_ID       = 1418611875649687777
LOG_CHANNEL_ID = 1497505822576611368
MANAGER_ROLE_ID = 1496443008667750600
BOT_DEV_ROLE_ID = 1497519433487351869

def now():
    return datetime.now(timezone.utc)

def is_manager(member: discord.Member) -> bool:
    role_ids = [r.id for r in member.roles]
    return (
        member.guild.owner_id == member.id or
        MANAGER_ROLE_ID in role_ids or
        BOT_DEV_ROLE_ID in role_ids
    )

def register_nick_command(bot: discord.Client):
    guild_obj = discord.Object(id=GUILD_ID)

    @bot.tree.command(
        name        = "nick",
        description = "Change a member's server nickname",
        guild       = guild_obj
    )
    @app_commands.describe(
        member   = "The member whose nickname you want to change",
        nickname = "The new nickname (leave blank to reset to their username)"
    )
    async def slash_nick(
        interaction: discord.Interaction,
        member:      discord.Member,
        nickname:    str = ""
    ):
        # ── Permission check ─────────────────────────────────────────
        if not is_manager(interaction.user):
            return await interaction.response.send_message(
                "❌ No permission.", ephemeral=True
            )

        # ── Role hierarchy check ──────────────────────────────────────
        # Bot cannot change nickname of someone with a higher/equal role than the bot itself
        if member.top_role >= interaction.guild.me.top_role:
            return await interaction.response.send_message(
                "❌ I can't change the nickname of someone with a higher or equal role than me.",
                ephemeral=True
            )

        # Managers can't nick people above themselves either
        if interaction.user.top_role <= member.top_role and interaction.guild.owner_id != interaction.user.id:
            return await interaction.response.send_message(
                "❌ You can't change the nickname of someone with an equal or higher role than you.",
                ephemeral=True
            )

        old_nick    = member.display_name
        new_nick    = nickname.strip() or None  # None resets to username

        try:
            await member.edit(nick=new_nick, reason=f"Nickname changed by {interaction.user}")
        except discord.Forbidden:
            return await interaction.response.send_message(
                "❌ I don't have permission to change this member's nickname.", ephemeral=True
            )
        except Exception as e:
            return await interaction.response.send_message(
                f"❌ Something went wrong: {e}", ephemeral=True
            )

        display_new = new_nick or member.name  # show username if reset

        # ── Ephemeral confirmation (only sender sees this) ────────────
        await interaction.response.send_message(
            f"✅ Changed **{old_nick}**'s nickname to **{display_new}**.",
            ephemeral=True
        )

        # ── DM the affected member ────────────────────────────────────
        try:
            dm_embed = discord.Embed(
                title       = "✏️ Your nickname was changed",
                color       = 0x3498db,
                timestamp   = now()
            )
            dm_embed.add_field(name="Server",       value=interaction.guild.name, inline=False)
            dm_embed.add_field(name="Old nickname", value=old_nick,               inline=True)
            dm_embed.add_field(name="New nickname", value=display_new,            inline=True)
            dm_embed.add_field(name="Changed by",   value=str(interaction.user),  inline=False)
            await member.send(embed=dm_embed)
        except Exception:
            pass  # Member has DMs off — that's fine

        # ── Log to mod channel ────────────────────────────────────────
        log = bot.get_channel(LOG_CHANNEL_ID)
        if log:
            log_embed = discord.Embed(
                title     = "✏️ Nickname Changed",
                color     = 0x3498db,
                timestamp = now()
            )
            log_embed.add_field(name="User",        value=f"{member.mention} ({member.id})")
            log_embed.add_field(name="Changed By",  value=interaction.user.mention)
            log_embed.add_field(name="Old Nickname",value=old_nick,    inline=True)
            log_embed.add_field(name="New Nickname",value=display_new, inline=True)
            await log.send(embed=log_embed)
