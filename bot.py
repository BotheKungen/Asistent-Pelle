"""
Discord-bot för att dela 3D-projektfiler (.blend, .fbx, ...) till en forumkanal.

Flöde:
  Panel (embed + knapp "Ladda upp fil")
    -> dold rullista med kategorier (forumets taggar)
    -> formulär (titel, beskrivning, valfri länk)
    -> bifoga fil (+ gärna en förhandsbild i samma meddelande)
    -> forumtråd med embed, fil, tagg och knappar:
         🔄 Ladda upp ny version   ✅ Godkänn   🛠️ Behöver ändras

Miljövariabler:
  DISCORD_TOKEN      (obligatorisk)  Bottens token
  PORT               (valfri)        Port för hälsokontroll-servern (standard 10000)
  UPLOAD_CHANNEL_ID  (valfri)        Kanal där boten själv ser till att panelen alltid finns

Kräver: discord.py >= 2.4

Tips: skapa dessa taggar i forumet så får du statusen som riktiga taggar också:
  "Ny", "Behöver ändras", "Klar"   (finns de inte funkar allt ändå, men utan taggbyte)
"""

import asyncio
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer

import discord
from discord import app_commands
from discord.ext import commands

# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #
FORUM_CHANNEL_ID = 1559907105279053926
UPLOAD_TIMEOUT_SECONDS = 120
EPHEMERAL_LIFETIME = 300  # Dolda meddelanden raderas automatiskt efter 5 min

# Pinga vid ny version i en tråd. Sätt till "" för att stänga av.
NEW_VERSION_PING = "@everyone"

# Används bara om forumet inte har några taggar eller inte kan hämtas
FALLBACK_CATEGORIES = ["Bilar", "Bildelar", "Karaktärer", "Övrigt"]

# Statusnamn (matchas mot forumets taggar, skiftlägesokänsligt)
STATUS_NEW = "Ny"
STATUS_CHANGES = "Behöver ändras"
STATUS_APPROVED = "Klar"
ALL_STATUSES = (STATUS_NEW, STATUS_CHANGES, STATUS_APPROVED)

# Emojis för slutet av trådens titel
STATUS_EMOJIS = {
    STATUS_NEW: "🕒",
    STATUS_CHANGES: "🔴",
    STATUS_APPROVED: "✅"
}

FIELD_STATUS = "📌 Status"
FIELD_VERSION = "🔢 Version"

# Tillåtna filtyper. Sätt till None för att tillåta allt.
ALLOWED_EXTENSIONS: set[str] | None = {
    # 3D
    ".blend", ".fbx", ".obj", ".glb", ".gltf", ".dae", ".stl", ".usd", ".usdz", ".abc",
    # Unity / kod
    ".unitypackage", ".cs", ".shader", ".anim", ".prefab",
    # Texturer / bilder
    ".png", ".jpg", ".jpeg", ".tga", ".psd", ".exr", ".hdr", ".webp", ".gif",
    # Ljud / video
    ".wav", ".mp3", ".ogg", ".mp4", ".mov",
    # Arkiv
    ".zip", ".rar", ".7z",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

COLOR_NEW = discord.Color.from_rgb(88, 101, 242)
COLOR_CHANGES = discord.Color.from_rgb(250, 166, 26)
COLOR_APPROVED = discord.Color.from_rgb(67, 181, 129)

PANEL_TITLE = "📤 Dela dina 3D-projektfiler"

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


def ext_of(filename: str) -> str:
    return os.path.splitext(filename)[1].lower()


def is_image(attachment: discord.Attachment) -> bool:
    return bool(
        (attachment.content_type or "").startswith("image/")
        or ext_of(attachment.filename) in IMAGE_EXTENSIONS
    )


def valid_link(link: str) -> bool:
    return bool(re.match(r"^https?://\S+$", link))


def set_field(embed: discord.Embed, name: str, value: str, inline: bool = True) -> None:
    for i, f in enumerate(embed.fields):
        if f.name == name:
            embed.set_field_at(i, name=name, value=value, inline=inline)
            return
    embed.add_field(name=name, value=value, inline=inline)


def current_version(embed: discord.Embed) -> int:
    for f in embed.fields:
        if f.name == FIELD_VERSION:
            m = re.match(r"v(\d+)", f.value or "")
            if m:
                return int(m.group(1))
    return 1


def uploader_id(embed: discord.Embed) -> int | None:
    url = embed.author.url if embed.author else None
    if url:
        m = re.search(r"/users/(\d+)", url)
        if m:
            return int(m.group(1))
    return None


def find_status_tag(forum: discord.ForumChannel, status: str) -> discord.ForumTag | None:
    for tag in forum.available_tags:
        if tag.name.strip().casefold() == status.casefold():
            return tag
    return None


async def set_thread_status(thread: discord.Thread, status: str) -> None:
    """Byter statustagg på tråden (behåller kategori-taggen) och uppdaterar titel-emojin."""
    forum = thread.parent
    if not isinstance(forum, discord.ForumChannel):
        return

    # Hantera taggar
    status_names = {s.casefold() for s in ALL_STATUSES}
    keep = [t for t in thread.applied_tags if t.name.strip().casefold() not in status_names]
    new_tag = find_status_tag(forum, status)
    if new_tag is not None:
        keep.append(new_tag)
    keep = keep[:5]  # Discord tillåter max 5 taggar per tråd

    # Hantera trådens namn (Titel-emoji)
    current_name = thread.name
    clean_name = current_name
    for e in STATUS_EMOJIS.values():
        if clean_name.endswith(e):
            clean_name = clean_name[:-len(e)].strip()
    
    # Discord tillåter max 100 tecken, vi stannar vid 95 för säkerhets skull
    new_name = f"{truncate(clean_name, 95)} {STATUS_EMOJIS[status]}"

    # Skapa uppdateringen
    edit_kwargs = {"archived": False}
    if new_name != current_name:
        edit_kwargs["name"] = new_name
    
    current_tag_ids = {t.id for t in thread.applied_tags}
    new_tag_ids = {t.id for t in keep}
    if current_tag_ids != new_tag_ids:
        edit_kwargs["applied_tags"] = keep

    if len(edit_kwargs) == 1 and "archived" in edit_kwargs:
        if not thread.archived:
            return  # Inget behöver ändras

    await thread.edit(**edit_kwargs)


async def get_thread(interaction: discord.Interaction) -> discord.Thread | None:
    channel = interaction.channel
    if isinstance(channel, discord.Thread):
        return channel
    try:
        channel = await interaction.client.fetch_channel(interaction.channel_id)
    except discord.HTTPException:
        return None
    return channel if isinstance(channel, discord.Thread) else None


async def safe_delete(message: discord.Message | None) -> None:
    if message is None:
        return
    try:
        await message.delete()
    except (discord.Forbidden, discord.NotFound):
        log.warning("Kunde inte radera meddelande (saknar Manage Messages?)")


def filename_tip(filename: str) -> str:
    if re.search(r"[^A-Za-z0-9._-]", filename):
        return (
            "\n💡 Tips: undvik mellanslag och å/ä/ö i filnamn "
            "(t.ex. `bmw_m3_v1.fbx`) – det kan ställa till det i Unity."
        )
    return ""


# --------------------------------------------------------------------------- #
# Insamling av fil / länk
# --------------------------------------------------------------------------- #
@dataclass
class Collected:
    message: discord.Message | None = None
    attachment: discord.Attachment | None = None
    files: list[discord.File] = field(default_factory=list)
    preview_name: str | None = None
    link: str | None = None


def add_file_fields(embed: discord.Embed, c: Collected) -> None:
    if c.attachment is not None:
        size_mb = c.attachment.size / (1024 * 1024)
        embed.add_field(name="📄 Filnamn", value=f"`{truncate(c.attachment.filename, 200)}`", inline=True)
        embed.add_field(name="💾 Storlek", value=f"{size_mb:.2f} MB", inline=True)
    elif c.link:
        embed.add_field(name="🔗 Länk", value=truncate(c.link, 1000), inline=False)
    if c.preview_name:
        embed.set_image(url=f"attachment://{c.preview_name}")


async def collect_upload(interaction: discord.Interaction, link: str | None) -> Collected | None:
    """
    Svarar på modalen och hämtar antingen länken eller filen (+ valfri förhandsbild).
    Returnerar None om något gick fel (användaren har då fått ett dolt felmeddelande).
    """
    bot = interaction.client

    if link:
        await interaction.response.send_message(
            "🔗 Tack! Publicerar med din länk ...",
            ephemeral=True,
            delete_after=EPHEMERAL_LIFETIME,
        )
        return Collected(link=link)

    await interaction.response.send_message(
        f"📎 Tack! Bifoga nu din fil i den här kanalen inom "
        f"**{UPLOAD_TIMEOUT_SECONDS // 60} minuter**.\n"
        f"🖼️ Vill du ha en förhandsbild? Bifoga en bild i *samma* meddelande.",
        ephemeral=True,
        delete_after=EPHEMERAL_LIFETIME,
    )

    user_id = interaction.user.id
    channel_id = interaction.channel_id

    def check(m: discord.Message) -> bool:
        return (
            m.author.id == user_id
            and m.channel.id == channel_id
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
            delete_after=EPHEMERAL_LIFETIME,
        )
        return None

    # Välj huvudfil och förhandsbild
    attachments = message.attachments
    non_images = [a for a in attachments if not is_image(a)]
    main = non_images[0] if non_images else attachments[0]
    preview = next((a for a in attachments if a is not main and is_image(a)), None)

    # Kontrollera filtyp
    if ALLOWED_EXTENSIONS is not None and ext_of(main.filename) not in ALLOWED_EXTENSIONS:
        await safe_delete(message)
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        await interaction.followup.send(
            f"❌ Filtypen `{ext_of(main.filename) or 'okänd'}` är inte tillåten.\n"
            f"Tillåtna typer: {allowed}\n"
            f"Tips: packa ihop i en `.zip` eller använd länkfältet.",
            ephemeral=True,
            delete_after=EPHEMERAL_LIFETIME,
        )
        return None

    # Kontrollera storlek mot serverns uppladdningsgräns (boten måste ladda upp filen igen)
    limit = interaction.guild.filesize_limit if interaction.guild else 10 * 1024 * 1024
    total = main.size + (preview.size if preview else 0)
    if total > limit:
        await safe_delete(message)
        await interaction.followup.send(
            f"❌ Filen är för stor för att jag ska kunna lägga den i forumet "
            f"({total / 1024 / 1024:.1f} MB, gränsen är {limit / 1024 / 1024:.0f} MB).\n"
            f"Ladda upp den på Google Drive / Dropbox och klistra in länken i "
            f"länkfältet istället.",
            ephemeral=True,
            delete_after=EPHEMERAL_LIFETIME,
        )
        return None

    # Ladda ner filerna INNAN originalmeddelandet raderas
    collected = Collected(message=message, attachment=main)
    collected.files.append(await main.to_file())
    if preview is not None:
        preview_name = f"preview{ext_of(preview.filename) or '.png'}"
        collected.files.append(await preview.to_file(filename=preview_name))
        collected.preview_name = preview_name
    return collected


async def finish_prompt(interaction: discord.Interaction) -> None:
    """Tar bort den gamla uppmaningen när allt gått bra."""
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        pass


# --------------------------------------------------------------------------- #
# Modal: nytt inlägg
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
    link_input = discord.ui.TextInput(
        label="Länk (om filen är för stor)",
        placeholder="Drive / Dropbox-länk. Lämna tomt om du bifogar fil.",
        style=discord.TextStyle.short,
        required=False,
        max_length=300,
    )

    def __init__(self, category: str, tag_id: int | None) -> None:
        super().__init__()
        self.category = category
        self.tag_id = tag_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        link = (self.link_input.value or "").strip()
        if link and not valid_link(link):
            await interaction.response.send_message(
                "❌ Länken måste börja med http:// eller https://",
                ephemeral=True,
                delete_after=EPHEMERAL_LIFETIME,
            )
            return

        collected = await collect_upload(interaction, link or None)
        if collected is None:
            return

        try:
            await self._publish(interaction, collected)
        except Exception:
            log.exception("Fel vid uppladdning till forumet")
            await interaction.followup.send(
                "❌ Något gick fel när filen skulle postas i forumet. "
                "Kontrollera mina behörigheter och försök igen.",
                ephemeral=True,
                delete_after=EPHEMERAL_LIFETIME,
            )

    async def _publish(self, interaction: discord.Interaction, c: Collected) -> None:
        bot = interaction.client
        title = self.title_input.value.strip()
        description = (self.description_input.value or "").strip()
        category = self.category
        user = interaction.user

        forum = bot.get_channel(FORUM_CHANNEL_ID)
        if forum is None:
            forum = await bot.fetch_channel(FORUM_CHANNEL_ID)
        if not isinstance(forum, discord.ForumChannel):
            await interaction.followup.send(
                "❌ Kanalen med det angivna ID:t är inte en Forum-kanal.",
                ephemeral=True,
                delete_after=EPHEMERAL_LIFETIME,
            )
            return

        embed = discord.Embed(
            title=truncate(title, 256),
            description=truncate(description, 4000) if description else None,
            color=COLOR_NEW,
            timestamp=discord.utils.utcnow(),
        )
        # Författar-URL används för att kunna pinga uppladdaren vid statusändring
        embed.set_author(
            name=user.display_name,
            icon_url=user.display_avatar.url,
            url=f"https://discord.com/users/{user.id}",
        )
        embed.add_field(name="📂 Kategori", value=truncate(category, 1024), inline=True)
        add_file_fields(embed, c)
        embed.add_field(name=FIELD_STATUS, value=f"🆕 {STATUS_NEW}", inline=True)
        embed.add_field(name=FIELD_VERSION, value="v1", inline=True)
        embed.set_footer(text=f"Uppladdad av {user.display_name}")

        tag = forum.get_tag(self.tag_id) if self.tag_id is not None else None
        applied: list[discord.ForumTag] = []
        if tag is not None:
            applied.append(tag)
        status_tag = find_status_tag(forum, STATUS_NEW)
        if status_tag is not None:
            applied.append(status_tag)

        kwargs = {}
        if applied:
            kwargs["applied_tags"] = applied
        if c.files:
            kwargs["files"] = c.files

        # Sätt trådens namn direkt med den första emojin (Klockan)
        base_name = f"[{category}] {title}"
        thread_name = f"{truncate(base_name, 95)} {STATUS_EMOJIS[STATUS_NEW]}"

        created = await forum.create_thread(
            name=thread_name,
            content="@everyone",
            embed=embed,
            view=ThreadView(),
            allowed_mentions=discord.AllowedMentions(everyone=True),
            **kwargs,
        )
        thread = created.thread

        await safe_delete(c.message)
        await finish_prompt(interaction)

        tip = filename_tip(c.attachment.filename) if c.attachment else ""
        await interaction.followup.send(
            f"✅ Din fil har laddats upp!\n🏷️ Kategori: **{category}**{tip}\n{thread.jump_url}",
            ephemeral=True,
            delete_after=EPHEMERAL_LIFETIME,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Modal-fel", exc_info=error)
        msg = "❌ Ett oväntat fel inträffade."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True, delete_after=EPHEMERAL_LIFETIME)
        else:
            await interaction.response.send_message(msg, ephemeral=True, delete_after=EPHEMERAL_LIFETIME)


# --------------------------------------------------------------------------- #
# Modal: ny version i befintlig tråd
# --------------------------------------------------------------------------- #
class VersionModal(discord.ui.Modal, title="Ny version"):
    notes_input = discord.ui.TextInput(
        label="Vad har ändrats? (valfritt)",
        placeholder="T.ex. fixade UV:erna, lade till texturer ...",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1500,
    )
    link_input = discord.ui.TextInput(
        label="Länk (om filen är för stor)",
        placeholder="Drive / Dropbox-länk. Lämna tomt om du bifogar fil.",
        style=discord.TextStyle.short,
        required=False,
        max_length=300,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        link = (self.link_input.value or "").strip()
        if link and not valid_link(link):
            await interaction.response.send_message(
                "❌ Länken måste börja med http:// eller https://",
                ephemeral=True,
                delete_after=EPHEMERAL_LIFETIME,
            )
            return

        thread = await get_thread(interaction)
        if thread is None:
            await interaction.response.send_message(
                "❌ Knappen måste användas inne i en forumtråd.",
                ephemeral=True,
                delete_after=EPHEMERAL_LIFETIME,
            )
            return

        # Säkerställ att boten ser meddelanden i tråden (behövs för wait_for)
        try:
            await thread.join()
        except discord.HTTPException:
            pass

        collected = await collect_upload(interaction, link or None)
        if collected is None:
            return

        try:
            await self._publish(interaction, thread, collected)
        except Exception:
            log.exception("Fel vid ny version")
            await interaction.followup.send(
                "❌ Något gick fel när den nya versionen skulle postas.",
                ephemeral=True,
                delete_after=EPHEMERAL_LIFETIME,
            )

    async def _publish(self, interaction: discord.Interaction, thread: discord.Thread, c: Collected) -> None:
        user = interaction.user
        notes = (self.notes_input.value or "").strip()

        starter = await thread.fetch_message(thread.id)
        starter_embed = starter.embeds[0].copy() if starter.embeds else None
        version = current_version(starter_embed) + 1 if starter_embed else 2

        embed = discord.Embed(
            title=f"🔄 Ny version: v{version}",
            description=truncate(notes, 4000) if notes else None,
            color=COLOR_NEW,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
        add_file_fields(embed, c)
        embed.set_footer(text=f"Uppladdad av {user.display_name}")

        kwargs = {}
        if c.files:
            kwargs["files"] = c.files
        version_msg = await thread.send(
            content=NEW_VERSION_PING or None,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(everyone=bool(NEW_VERSION_PING)),
            **kwargs,
        )

        # Uppdatera startmeddelandet: version, länk till senaste filen och status
        if starter_embed is not None:
            set_field(starter_embed, FIELD_VERSION, f"v{version} · [Visa filen]({version_msg.jump_url})")
            set_field(starter_embed, FIELD_STATUS, f"🆕 {STATUS_NEW}")
            starter_embed.color = COLOR_NEW
            await starter.edit(embed=starter_embed)
            
        # Den här funktionen byter nu både tagg OCH emojin i titeln!
        await set_thread_status(thread, STATUS_NEW)

        await safe_delete(c.message)
        await finish_prompt(interaction)

        tip = filename_tip(c.attachment.filename) if c.attachment else ""
        await interaction.followup.send(
            f"✅ Version **v{version}** är uppladdad!{tip}\n{version_msg.jump_url}",
            ephemeral=True,
            delete_after=EPHEMERAL_LIFETIME,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Modal-fel (version)", exc_info=error)
        msg = "❌ Ett oväntat fel inträffade."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True, delete_after=EPHEMERAL_LIFETIME)
        else:
            await interaction.response.send_message(msg, ephemeral=True, delete_after=EPHEMERAL_LIFETIME)


# --------------------------------------------------------------------------- #
# Modal: Behöver ändras
# --------------------------------------------------------------------------- #
class ChangesModal(discord.ui.Modal, title="Vad behöver ändras?"):
    reason_input = discord.ui.TextInput(
        label="Beskrivning av ändringar",
        placeholder="T.ex. fixa texturerna, modellen är för stor...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1500,
    )

    def __init__(self, view: "ThreadView") -> None:
        super().__init__()
        self.thread_view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Skickar vidare texten till status-funktionen
        await self.thread_view._change_status(
            interaction, 
            STATUS_CHANGES, 
            "🛠️", 
            COLOR_CHANGES, 
            reason=self.reason_input.value
        )


# --------------------------------------------------------------------------- #
# Knappar i forumtråden (permanent view)
# --------------------------------------------------------------------------- #
class ThreadView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Ladda upp ny version",
        style=discord.ButtonStyle.primary,
        emoji="🔄",
        custom_id="filbot:new_version",
    )
    async def new_version_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(VersionModal())

    @discord.ui.button(
        label="Godkänn",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="filbot:approve",
    )
    async def approve_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._change_status(interaction, STATUS_APPROVED, "✅", COLOR_APPROVED)

    @discord.ui.button(
        label="Behöver ändras",
        style=discord.ButtonStyle.danger,
        emoji="🛠️",
        custom_id="filbot:changes",
    )
    async def changes_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        # Öppnar textformuläret
        await interaction.response.send_modal(ChangesModal(self))

    async def _change_status(
        self,
        interaction: discord.Interaction,
        status: str,
        emoji: str,
        color: discord.Color,
        reason: str | None = None
    ) -> None:
        message = interaction.message
        if message is None or not message.embeds:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ Hittar inget inlägg att uppdatera.",
                    ephemeral=True,
                    delete_after=EPHEMERAL_LIFETIME,
                )
            return

        embed = message.embeds[0].copy()
        set_field(embed, FIELD_STATUS, f"{emoji} {status}")
        embed.color = color
        
        if interaction.response.is_done():
            await interaction.followup.edit_message(message.id, embed=embed)
        else:
            await interaction.response.edit_message(embed=embed)

        thread = await get_thread(interaction)
        if thread is None:
            return

        try:
            # Nu byter den tagg + emoji i namnet!
            await set_thread_status(thread, status)
        except discord.HTTPException:
            log.exception("Kunde inte byta statustagg eller namn")

        # Meddela i tråden och pinga uppladdaren (om det inte är hen själv)
        uid = uploader_id(embed)
        text = f"{emoji} **{status}** – satt av {interaction.user.mention}"
        
        # Om det finns en "behöver ändras"-kommentar
        if reason:
            text += f"\n📝 **Kommentar:** {reason}"

        mentions = discord.AllowedMentions.none()
        if uid and uid != interaction.user.id:
            text += f"\n<@{uid}>"
            mentions = discord.AllowedMentions(users=[discord.Object(id=uid)])
            
        await thread.send(text, allowed_mentions=mentions)


# --------------------------------------------------------------------------- #
# Kategorival (rullista)
# --------------------------------------------------------------------------- #
class CategorySelect(discord.ui.Select):
    def __init__(self, forum: discord.ForumChannel | None) -> None:
        status_names = {s.casefold() for s in ALL_STATUSES}
        options: list[discord.SelectOption] = []
        if forum is not None:
            # Statustaggar ("Ny", "Klar" ...) är inga kategorier
            category_tags = [
                t for t in forum.available_tags if t.name.strip().casefold() not in status_names
            ]
            for tag in category_tags[:25]:  # Discord tillåter max 25 alternativ
                options.append(
                    discord.SelectOption(
                        label=truncate(tag.name, 100),
                        value=str(tag.id),
                        emoji=tag.emoji,
                    )
                )
        if not options:
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
        super().__init__(timeout=EPHEMERAL_LIFETIME)
        self.add_item(CategorySelect(forum))


# --------------------------------------------------------------------------- #
# Permanent View med knapp i uppladdningskanalen
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
            delete_after=EPHEMERAL_LIFETIME,
        )


# --------------------------------------------------------------------------- #
# Uppladdningspanel (embed + knapp)
# --------------------------------------------------------------------------- #
def build_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title=PANEL_TITLE,
        description=(
            "Klicka på knappen nedan för att ladda upp en fil "
            "(.blend, .fbx, .obj, .unitypackage m.fl.) till forumet.\n\n"
            "**Så här gör du:**\n"
            "1️⃣ Tryck på **Ladda upp fil**\n"
            "2️⃣ Välj kategori i listan\n"
            "3️⃣ Fyll i titel och beskrivning (eller en länk om filen är stor)\n"
            "4️⃣ Bifoga filen i chatten inom 2 minuter – lägg gärna med en "
            "förhandsbild i samma meddelande 🖼️\n"
            "5️⃣ Klart! Boten postar den i forumet åt dig ✨\n\n"
            "I varje forumtråd finns knappar för **ny version**, **godkänn** "
            "och **behöver ändras**."
        ),
        color=COLOR_NEW,
    )
    embed.set_footer(text="Kategorierna hämtas från forumets taggar")
    return embed


async def ensure_panel(bot: "FileBot", channel_id: int) -> None:
    """Postar panelen i kanalen om den inte redan finns."""
    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        log.warning("UPLOAD_CHANNEL_ID pekar inte på en textkanal.")
        return

    async for msg in channel.history(limit=100):
        if (
            msg.author.id == bot.user.id
            and msg.embeds
            and msg.embeds[0].title == PANEL_TITLE
        ):
            log.info("Uppladdningspanelen finns redan i kanalen.")
            return

    await channel.send(embed=build_panel_embed(), view=UploadView())
    log.info("Uppladdningspanel postad i kanal %s.", channel_id)


# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #
class FileBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # Krävs för !setup och för att läsa bilagor
        super().__init__(command_prefix="!", intents=intents)
        self._panel_checked = False

    async def setup_hook(self) -> None:
        # Registrera permanenta knappar så de fungerar efter omstart
        self.add_view(UploadView())
        self.add_view(ThreadView())
        await self.add_cog(SetupCog(self))
        await self.tree.sync()

    async def on_ready(self) -> None:
        log.info("Inloggad som %s (ID: %s)", self.user, self.user.id)

        # Kör bara första gången (on_ready kan triggas flera gånger)
        if self._panel_checked:
            return
        self._panel_checked = True

        raw = os.environ.get("UPLOAD_CHANNEL_ID")
        if raw and raw.isdigit():
            try:
                await ensure_panel(self, int(raw))
            except Exception:
                log.exception("Kunde inte säkerställa uppladdningspanelen")


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
        await ctx.send(embed=build_panel_embed(), view=UploadView())

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
