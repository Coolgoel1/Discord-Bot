# ================================================================
#  verification.py  ──  Button-based Verified role assignment
#
#  HOW TO USE:
#    1. Place this file next to your main.py
#    2. In main.py at the top:
#           from verification import register_verification
#    3. Just before bot.run():
#           register_verification(bot)
#
#  NOTE: Do NOT call bot.add_view(VerifyView()) anywhere else.
#        register_verification() handles that automatically.
#
#  SERVER SETUP:
#    • A role named exactly "Verified" must exist in your server.
#    • The bot's own role must be ABOVE "Verified" in the role list.
#    • Run /setup_verify in the channel you want the panel in.
# ================================================================

import discord
from discord import app_commands
from datetime import datetime, timezone

# ── Mirror your main.py config values here ───────────────────────
GUILD_ID           = 1418611875649687777
LOG_CHANNEL_ID     = 1497505822576611368
MANAGER_ROLE_ID    = 1496443008667750600
BOT_DEV_ROLE_ID    = 1497519433487351869
VERIFIED_ROLE_NAME = "Verified"

# IMPORTANT: This custom_id must NEVER change once panels are posted.
# Changing it orphans every existing button in your server.
VERIFY_BUTTON_ID   = "verify_button"


def _now():
    return datetime.now(timezone.utc)


def _is_manager(member: discord.Member) -> bool:
    role_ids = [r.id for r in member.roles]
    return (
        member.guild.owner_id == member.id
        or MANAGER_ROLE_ID in role_ids
        or BOT_DEV_ROLE_ID in role_ids
    )


# ══════════════════════════════════════════════════════════════════
#  PERSISTENT VIEW
# ══════════════════════════════════════════════════════════════════

class VerifyView(discord.ui.View):
    """
    Persistent verify button.
    timeout=None means it lives forever and survives bot restarts.
    The custom_id is stable so Discord can route clicks correctly.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label     = "✅ Verify Me",
        style     = discord.ButtonStyle.success,
        custom_id = VERIFY_BUTTON_ID,   # ← single, stable ID used everywhere
    )
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Defer immediately — gives us 15 min to respond and prevents timeout
        await interaction.response.defer(ephemeral=True)

        member = interaction.user
        guild  = interaction.guild

        # ── Role existence check ──────────────────────────────────
        role = discord.utils.get(guild.roles, name=VERIFIED_ROLE_NAME)
        if role is None:
            return await interaction.followup.send(
                f"❌ The **{VERIFIED_ROLE_NAME}** role doesn't exist yet.\n"
                "Ask an admin to create it, then try again.",
                ephemeral=True,
            )

        # ── Already verified ──────────────────────────────────────
        if role in member.roles:
            return await interaction.followup.send(
                "✅ You're already verified — you're good to go!",
                ephemeral=True,
            )

        # ── Hierarchy check ───────────────────────────────────────
        if role >= guild.me.top_role:
            return await interaction.followup.send(
                "❌ I can't assign that role because it's at or above my highest role.\n"
                "Ask an admin to move my role **above** the Verified role.",
                ephemeral=True,
            )

        # ── Assign role ───────────────────────────────────────────
        try:
            await member.add_roles(role, reason="Self-verification via button")
        except discord.Forbidden:
            return await interaction.followup.send(
                "❌ I don't have the **Manage Roles** permission. Ask an admin to grant it.",
                ephemeral=True,
            )
        except Exception as exc:
            return await interaction.followup.send(
                f"❌ Unexpected error: {exc}", ephemeral=True
            )

        # ── Success ───────────────────────────────────────────────
        await interaction.followup.send(
            f"🎉 You've been given the **{VERIFIED_ROLE_NAME}** role — welcome to the server!",
            ephemeral=True,
        )

        # ── Mod log ───────────────────────────────────────────────
        log_ch = guild.get_channel(LOG_CHANNEL_ID)
        if log_ch:
            embed = discord.Embed(
                title     = "✅ Member Verified",
                color     = 0x2ECC71,
                timestamp = _now(),
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.add_field(name="User",    value=f"{member.mention} (`{member}`)")
            embed.add_field(name="User ID", value=str(member.id))
            embed.add_field(
                name   = "Account Age",
                value  = f"<t:{int(member.created_at.timestamp())}:R>",
                inline = False,
            )
            await log_ch.send(embed=embed)


# ══════════════════════════════════════════════════════════════════
#  REGISTER FUNCTION  ──  call once from main.py
# ══════════════════════════════════════════════════════════════════

def register_verification(bot: discord.Client):
    """
    Call this once, just before bot.run().
    It registers the persistent view AND adds the /setup_verify command.
    Do NOT call bot.add_view(VerifyView()) anywhere else.
    """
    guild_obj = discord.Object(id=GUILD_ID)

    # Single registration point — no duplicates
    bot.add_view(VerifyView())

    @bot.tree.command(
        name        = "setup_verify",
        description = "Post the verification button panel in this channel (managers only)",
        guild       = guild_obj,
    )
    @app_commands.describe(
        panel_title = "Panel title (optional)",
        panel_desc  = "Panel description text (optional)",
    )
    async def slash_setup_verify(
        interaction: discord.Interaction,
        panel_title: str = "🔐 Server Verification",
        panel_desc:  str = (
            "Welcome! Press the button below to verify yourself "
            "and unlock access to the server. 🎉"
        ),
    ):
        if not _is_manager(interaction.user):
            return await interaction.response.send_message(
                "❌ No permission.", ephemeral=True
            )

        # Pre-flight checks before posting the panel
        role = discord.utils.get(interaction.guild.roles, name=VERIFIED_ROLE_NAME)
        if role is None:
            return await interaction.response.send_message(
                f"⚠️ The **{VERIFIED_ROLE_NAME}** role doesn't exist yet.\n"
                "Create it first, then run this command again.",
                ephemeral=True,
            )

        if role >= interaction.guild.me.top_role:
            return await interaction.response.send_message(
                f"⚠️ The **{VERIFIED_ROLE_NAME}** role is above my role in the list.\n"
                "Move my role above it so I can assign it.",
                ephemeral=True,
            )

        panel = discord.Embed(
            title       = panel_title,
            description = panel_desc,
            color       = 0x2ECC71,
        )
        panel.set_footer(text="Click the button below to verify yourself.")

        await interaction.channel.send(embed=panel, view=VerifyView())
        await interaction.response.send_message(
            "✅ Verification panel posted!", ephemeral=True
        )
