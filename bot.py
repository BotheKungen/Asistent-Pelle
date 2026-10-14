"""
Discord-bot för att dela 3D-projektfiler (.blend, .fbx, ...) till en forumkanal.

Flöde:
  /setup_laddaupp (eller !setup)  ->  Embed med permanent knapp "Ladda upp fil"
  Knapp -> dold rullista med kategorier (forumets taggar) -> Modal (titel, beskrivning)
  -> dold uppmaning att bifoga fil
  -> wait_for('message') -> skapar forumtråd med embed + fil + tagg
  -> raderar uppladdningsmeddelandet -> dold bekräftelse med länk.

Miljövariabler:
  DISCORD_TOKEN  (obligatorisk)  Bottens token
  PORT           (valfri)        Port för hälsokontroll-servern (standard 10000)

Kräver: discord.py >= 2.4
"""

import asyncio
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import discord
from discord import app_commands
from discord.ext import commands

# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #
FORUM_CHANNEL_ID = 1559907105279053926
UPLOAD_TIMEOUT_SECONDS = 120
# Används bara om forumet inte har några taggar eller inte kan hämtas
FALLBACK_CATEGORIES = ["Bilar", "Bildelar", "Karaktärer", "Övrigt"]
EMBED_COLOR = discord.Color.from_rgb(88, 101, 242)

log = logging.getLogger("filbot")


# --------------------------------------------------------------------------- #
# HTTP-server (håller Railway nöjd)
# --------------------------------------------------------------------------- #
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"Bot is alive!"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):  # Tysta HTTP-loggar
        return


def start_http_server() -> None:
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("HTTP-server startad på port %s", port)


# --------------------------------------------------------------------------- #
# Hjälpfunktioner
# --------------------------------------------------------------------------- #
def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------- #
# Modal
# --------------------------------------------------------------------------- #
class UploadModal(discord.ui.Modal, title="Ladda upp fil"):
    title_input = discord.ui.TextInput(
        label="Titel / Namn",
        placeholder="T.ex. BMW M3 E46",
        style=discord.TextStyle.short,
        required=True,
        max_length=80,
    )
    description_input = discord.ui.TextInput(
        label="Beskrivning (valfritt)",
        placeholder="Berätta lite om projektet ...",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1500,
    )

    def __init__(self, category: str, tag_id: int | None) -> None:
        super().__init__()
        self.category = category
        self.tag_id = tag_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot: "FileBot" = interaction.client  # type: ignore[assignment]

        title = self.title_input.value.strip()
        category = self.category
        description = (self.description_input.value or "").strip()

        await interaction.response.send_message(
            f"📎 Tack! Bifoga nu din fil i den här kanalen inom "
            f"**{UPLOAD_TIMEOUT_SECONDS // 60} minuter**.\n"
            f"Skicka bara filen som ett vanligt meddelande.",
            ephemeral=True,
        )

        channel = interaction.channel
        user = interaction.user

        def check(m: discord.Message) -> bool:
            return (
                m.author.id == user.id
                and m.channel.id == channel.id
                and len(m.attachments) > 0
            )

        try:
            message: discord.Message = await bot.wait_for(
                "message", check=check, timeout=UPLOAD_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            await interaction.followup.send(
                "⏰ Tiden gick ut – ingen fil bifogades. Klicka på knappen och försök igen.",
                ephemeral=True,
            )
            return

        attachment = message.attachments[0]

        try:
            await self._publish(
                interaction, bot, message, attachment, title, category, description
            )
        except Exception:
            log.exception("Fel vid uppladdning till forumet")
            await interaction.followup.send(
                "❌ Något gick fel när filen skulle postas i forumet. "
                "Kontrollera mina behörigheter och försök igen.",
                ephemeral=True,
            )

    async def _publish(
        self,
        interaction: discord.Interaction,
        bot: "FileBot",
        message: discord.Message,
        attachment: discord.Attachment,
        title: str,
        category: str,
        description: str,
    ) -> None:
        # Hämta forumkanalen
        forum = bot.get_channel(FORUM_CHANNEL_ID)
        if forum is None:
            forum = await bot.fetch_channel(FORUM_CHANNEL_ID)
        if not isinstance(forum, discord.ForumChannel):
            await interaction.followup.send(
                "❌ Kanalen med det angivna ID:t är inte en Forum-kanal.",
                ephemeral=True,
            )
            return

        # Ladda ner filen INNAN vi raderar originalmeddelandet
        file = await attachment.to_file()

        size_mb = attachment.size / (1024 * 1024)
        user = interaction.user

        embed = discord.Embed(
            title=truncate(title, 256),
            description=truncate(description, 4000) if description else None,
            color=EMBED_COLOR,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
        embed.add_field(name="📂 Kategori", value=truncate(category, 1024), inline=True)
        embed.add_field(name="📄 Filnamn", value=f"`{truncate(attachment.filename, 200)}`", inline=True)
        embed.add_field(name="💾 Storlek", value=f"{size_mb:.2f} MB", inline=True)
        embed.set_footer(text=f"Uppladdad av {user.display_name}")

        thread_name = truncate(f"[{category}] {title}", 100)
        tag = forum.get_tag(self.tag_id) if self.tag_id is not None else None

        kwargs = {}
        if tag is not None:
            kwargs["applied_tags"] = [tag]

        created = await forum.create_thread(
            name=thread_name,
            content="@everyone",
            embed=embed,
            file=file,
            allowed_mentions=discord.AllowedMentions(everyone=True),
            **kwargs,
        )
        thread = created.thread

        # Städa upp uppladdningsmeddelandet
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound):
            log.warning("Kunde inte radera uppladdningsmeddelandet (saknar Manage Messages?)")

        tag_info = f"\n🏷️ Kategori: **{category}**"
        await interaction.followup.send(
            f"✅ Din fil har laddats upp!{tag_info}\n{thread.jump_url}",
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Modal-fel", exc_info=error)
        msg = "❌ Ett oväntat fel inträffade."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


# --------------------------------------------------------------------------- #
# Kategorival (rullista)
# --------------------------------------------------------------------------- #
class CategorySelect(discord.ui.Select):
    def __init__(self, forum: discord.ForumChannel | None) -> None:
        options: list[discord.SelectOption] = []
        if forum is not None and forum.available_tags:
            for tag in forum.available_tags[:25]:  # Discord tillåter max 25 alternativ
                options.append(
                    discord.SelectOption(
                        label=truncate(tag.name, 100),
                        value=str(tag.id),
                        emoji=tag.emoji,
                    )
                )
        else:
            for name in FALLBACK_CATEGORIES:
                options.append(discord.SelectOption(label=name, value=f"name:{name}"))

        super().__init__(
            placeholder="Välj kategori ...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        chosen = next(o for o in self.options if o.value == value)

        tag_id = None if value.startswith("name:") else int(value)
        await interaction.response.send_modal(UploadModal(chosen.label, tag_id))


class CategoryView(discord.ui.View):
    def __init__(self, forum: discord.ForumChannel | None) -> None:
        super().__init__(timeout=180)
        self.add_item(CategorySelect(forum))


# --------------------------------------------------------------------------- #
# Permanent View med knapp
# --------------------------------------------------------------------------- #
class UploadView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)  # timeout=None + custom_id => permanent

    @discord.ui.button(
        label="Ladda upp fil",
        style=discord.ButtonStyle.primary,
        emoji="📤",
        custom_id="filbot:upload_button",
    )
    async def upload_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        forum = interaction.client.get_channel(FORUM_CHANNEL_ID)
        if not isinstance(forum, discord.ForumChannel):
            forum = None

        await interaction.response.send_message(
            "📂 Välj vilken **kategori** din fil tillhör:",
            view=CategoryView(forum),
            ephemeral=True,
        )


# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #
class FileBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # Krävs för !setup och för att läsa bilagor
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        # Registrera den permanenta knappen så den fungerar efter omstart
        self.add_view(UploadView())
        await self.add_cog(SetupCog(self))
        await self.tree.sync()

    async def on_ready(self) -> None:
        log.info("Inloggad som %s (ID: %s)", self.user, self.user.id)


class SetupCog(commands.Cog):
    def __init__(self, bot: FileBot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="setup_laddaupp",
        aliases=["setup"],
        description="Skickar ut uppladdningsmeddelandet med knappen.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @app_commands.default_permissions(administrator=True)
    async def setup_laddaupp(self, ctx: commands.Context) -> None:
        embed = discord.Embed(
            title="📤 Dela dina 3D-projektfiler",
            description=(
                "Klicka på knappen nedan för att ladda upp en fil "
                "(.blend, .fbx, .obj m.fl.) till forumet.\n\n"
                "**Så här gör du:**\n"
                "1️⃣ Tryck på **Ladda upp fil**\n"
                "2️⃣ Fyll i titel, kategori och beskrivning\n"
                "3️⃣ Bifoga filen i chatten inom 2 minuter\n"
                "4️⃣ Klart! Boten postar den i forumet åt dig ✨"
            ),
            color=EMBED_COLOR,
        )
        embed.set_footer(text="Kategorier: Bilar • Bildelar • Karaktärer • Övrigt")

        await ctx.send(embed=embed, view=UploadView())

    @setup_laddaupp.error
    async def setup_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, (commands.MissingPermissions, commands.CheckFailure)):
            await ctx.send("🚫 Du behöver administratörsbehörighet för det här kommandot.", ephemeral=True)
        else:
            log.exception("Fel i setup-kommando", exc_info=error)
            await ctx.send("❌ Något gick fel.", ephemeral=True)


# --------------------------------------------------------------------------- #
# Start
# --------------------------------------------------------------------------- #
def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("Miljövariabeln DISCORD_TOKEN saknas.")

    start_http_server()
    FileBot().run(token)


if __name__ == "__main__":
    main()
