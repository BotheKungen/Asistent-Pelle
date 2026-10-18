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
  UPLOAD_CHANNEL_ID  (valfri)        Kanal där boten själv ser till att uppladdningspanelen finns
  TODO_CHANNEL_ID    (valfri)        Kanal för to-do-listan (panel + uppgiftskort)
  DASHBOARD_CHANNEL_ID (valfri)      Kanal där dashboarden visas och uppdateras automatiskt
  PUBLIC_URL         (valfri)        Bottens publika adress för 3D-vyn, t.ex. https://min-bot.up.railway.app
                                     (Railway sätter RAILWAY_PUBLIC_DOMAIN själv när du genererar en domän)
  VIEWER_SECRET      (valfri)        Hemlig nyckel för 3D-länkarna (annars används bottens token)

Kräver: discord.py >= 2.4  (och gärna tzdata, för svensk tid på deadlines)

Tips: skapa dessa taggar i forumet så får du statusen som riktiga taggar också:
  "Ny", "Behöver ändras", "Klar"   (finns de inte funkar allt ändå, men utan taggbyte)
"""

import array
import asyncio
import concurrent.futures
import hashlib
import hmac
import io
import json
import logging
import math
import os
import re
import struct
import sys
import threading
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import discord
from discord import app_commands
from discord.ext import commands, tasks

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

# Modellkoll: trianglar som är OK per kategori (matchas mot kategorinamnet, skiftlägesokänsligt)
TRIANGLE_BUDGETS = {
    "bilar": 60_000,
    "bildelar": 15_000,
    "karaktärer": 40_000,
}
DEFAULT_TRIANGLE_BUDGET = 50_000
AUTO_SET_STATUS = True  # Sätt status "Behöver ändras" automatiskt vid allvarliga fel
ANALYZE_MAX_BYTES = 80 * 1024 * 1024  # Större filer analyseras inte (skyddar minnet)

PANEL_TITLE = "📤 Dela dina 3D-projektfiler"
TODO_PANEL_TITLE = "📋 To-do"
DASHBOARD_TITLE = "🎮 Game HQ"

QUICK_LIFETIME = 20  # Korta kvitton ("uppgiften skapad") raderas efter 20 sek

FIELD_TASK_STATUS = "📋 Status"
FIELD_ASSIGNEE = "👤 Ansvarig"
FIELD_DEADLINE = "⏰ Deadline"

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Stockholm")
except Exception:  # tzdata saknas -> använd UTC
    TZ = timezone.utc

log = logging.getLogger("filbot")


# --------------------------------------------------------------------------- #
# Hjälpfunktioner
# --------------------------------------------------------------------------- #
def env_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.isdigit() else None


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
    new_name = f"{truncate(clean_name, 95)} {STATUS_EMOJIS.get(status, '')}"

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


async def safe_delete(message: discord.Message | None) -> bool:
    """Raderar ett meddelande. Returnerar False om boten inte får (saknar Hantera meddelanden)."""
    if message is None:
        return True
    try:
        await message.delete()
        return True
    except discord.NotFound:
        return True
    except discord.Forbidden:
        log.warning(
            "Kunde inte radera meddelandet i kanal %s – boten saknar behörigheten 'Hantera meddelanden'.",
            getattr(message.channel, "id", "?"),
        )
        return False


async def followup_temp(
    interaction: discord.Interaction, content: str, lifetime: float = EPHEMERAL_LIFETIME, **kwargs
) -> None:
    """
    Skickar ett dolt uppföljningssvar som raderas av sig självt efter en stund.
    (Webhook.send/followup.send saknar delete_after i discord.py – därför görs det här manuellt.)
    """
    msg = await interaction.followup.send(content, ephemeral=True, wait=True, **kwargs)

    async def _later() -> None:
        await asyncio.sleep(lifetime)
        try:
            await msg.delete()
        except discord.HTTPException:
            pass

    task = asyncio.create_task(_later())
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


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
    data: bytes | None = None  # Filens bytes (bara för modellkoll)
    modal: bool = False  # True när filen kom direkt från formuläret (ingen chatt)


def add_file_fields(embed: discord.Embed, c: Collected) -> None:
    if c.attachment is not None:
        size_mb = c.attachment.size / (1024 * 1024)
        embed.add_field(name="📄 Filnamn", value=f"`{truncate(c.attachment.filename, 200)}`", inline=True)
        embed.add_field(name="💾 Storlek", value=f"{size_mb:.2f} MB", inline=True)
    elif c.link:
        embed.add_field(name="🔗 Länk", value=truncate(c.link, 1000), inline=False)
    if c.preview_name:
        embed.set_image(url=f"attachment://{c.preview_name}")


async def build_collected(
    interaction: discord.Interaction,
    attachments: list[discord.Attachment],
    message: discord.Message | None,
) -> Collected | None:
    """
    Gemensam kontroll + nedladdning av bifogade filer (från chatt ELLER direkt från formuläret).
    Returnerar None om något är fel (användaren har då fått ett dolt felmeddelande).
    """
    # Välj huvudfil och förhandsbild
    non_images = [a for a in attachments if not is_image(a)]
    main = non_images[0] if non_images else attachments[0]
    preview = next((a for a in attachments if a is not main and is_image(a)), None)

    # Kontrollera filtyp
    if ALLOWED_EXTENSIONS is not None and ext_of(main.filename) not in ALLOWED_EXTENSIONS:
        await safe_delete(message)
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        await followup_temp(interaction, f"❌ Filtypen `{ext_of(main.filename) or 'okänd'}` är inte tillåten.\n"
            f"Tillåtna typer: {allowed}\n"
            f"Tips: packa ihop i en `.zip` eller använd länkfältet.", EPHEMERAL_LIFETIME)
        return None

    # Kontrollera storlek mot serverns uppladdningsgräns (boten måste ladda upp filen igen)
    limit = interaction.guild.filesize_limit if interaction.guild else 10 * 1024 * 1024
    total = main.size + (preview.size if preview else 0)
    if total > limit:
        await safe_delete(message)
        await followup_temp(interaction, f"❌ Filen är för stor för att jag ska kunna lägga den i forumet "
            f"({total / 1024 / 1024:.1f} MB, gränsen är {limit / 1024 / 1024:.0f} MB).\n"
            f"Ladda upp den på Google Drive / Dropbox och klistra in länken i "
            f"länkfältet istället.", EPHEMERAL_LIFETIME)
        return None

    # Ladda ner filerna INNAN originalmeddelandet raderas
    collected = Collected(message=message, attachment=main)
    collected.files.append(await main.to_file())
    if (
        ext_of(main.filename) in (ANALYZABLE_EXTENSIONS | {".zip"})
        and main.size <= ANALYZE_MAX_BYTES
    ):
        collected.data = collected.files[0].fp.getvalue()
    if preview is not None:
        preview_name = f"preview{ext_of(preview.filename) or '.png'}"
        collected.files.append(await preview.to_file(filename=preview_name))
        collected.preview_name = preview_name
    return collected


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
        await followup_temp(interaction, "⏰ Tiden gick ut – ingen fil bifogades. Klicka på knappen och försök igen.", EPHEMERAL_LIFETIME)
        return None

    return await build_collected(interaction, message.attachments, message)


def schedule_delete_original(interaction: discord.Interaction, delay: float) -> None:
    """Raderar interaktionens dolda svar efter en stund (formulär-flödet använder defer)."""

    async def _later() -> None:
        await asyncio.sleep(delay)
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass

    task = asyncio.create_task(_later())
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


async def send_result(interaction: discord.Interaction, text: str, modal: bool) -> None:
    """Skickar sista dolda meddelandet (fungerar både efter defer och efter vanligt svar)."""
    if modal:
        try:
            await interaction.edit_original_response(content=text)
            schedule_delete_original(interaction, EPHEMERAL_LIFETIME)
            return
        except discord.HTTPException:
            pass
    await followup_temp(interaction, text, EPHEMERAL_LIFETIME)


async def finish_prompt(interaction: discord.Interaction) -> None:
    """Tar bort den gamla uppmaningen när allt gått bra."""
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        pass


DELETE_WARNING = (
    "\n⚠️ Jag kunde inte radera ditt filmeddelande i kanalen. "
    "Ge mig behörigheten **Hantera meddelanden** där så städar jag automatiskt."
)


async def publish_new_post(
    interaction: discord.Interaction,
    c: Collected,
    title: str,
    description: str,
    category: str,
    tag_id: int | None,
) -> None:
    bot = interaction.client
    user = interaction.user

    forum = bot.get_channel(FORUM_CHANNEL_ID)
    if forum is None:
        forum = await bot.fetch_channel(FORUM_CHANNEL_ID)
    if not isinstance(forum, discord.ForumChannel):
        await followup_temp(interaction, "❌ Kanalen med det angivna ID:t är inte en Forum-kanal.", EPHEMERAL_LIFETIME)
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

    tag = forum.get_tag(tag_id) if tag_id is not None else None
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

    if c.attachment is not None and is_viewable(c.attachment.filename):
        url = viewer_url(thread.id)
        if url:
            try:
                await created.message.edit(view=ThreadView(viewer_url=url))
            except discord.HTTPException:
                log.exception("Kunde inte lägga till 3D-knappen")

    deleted = await safe_delete(c.message)
    if not c.modal:
        await finish_prompt(interaction)
    bot.request_refresh()
    schedule_model_check(bot, thread, c, user.id, category)

    tip = filename_tip(c.attachment.filename) if c.attachment else ""
    warn = "" if deleted else DELETE_WARNING
    await send_result(
        interaction,
        f"✅ Din fil har laddats upp!\n🏷️ Kategori: **{category}**{tip}{warn}\n{thread.jump_url}",
        c.modal,
    )


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
            await followup_temp(interaction, "❌ Något gick fel när filen skulle postas i forumet. "
                "Kontrollera mina behörigheter och försök igen.", EPHEMERAL_LIFETIME)

    async def _publish(self, interaction: discord.Interaction, c: Collected) -> None:
        await publish_new_post(
            interaction,
            c,
            self.title_input.value.strip(),
            (self.description_input.value or "").strip(),
            self.category,
            self.tag_id,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Modal-fel", exc_info=error)
        msg = "❌ Ett oväntat fel inträffade."
        if interaction.response.is_done():
            await followup_temp(interaction, msg, EPHEMERAL_LIFETIME)
        else:
            await interaction.response.send_message(msg, ephemeral=True, delete_after=EPHEMERAL_LIFETIME)


async def publish_new_version(
    interaction: discord.Interaction,
    thread: discord.Thread,
    c: Collected,
    notes: str,
) -> None:
    user = interaction.user

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

    viewable = c.attachment is not None and is_viewable(c.attachment.filename)
    if viewable and viewer_url(thread.id, version_msg.id):
        try:
            await version_msg.edit(view=make_viewer_view(viewer_url(thread.id, version_msg.id)))
        except discord.HTTPException:
            log.exception("Kunde inte lägga till 3D-knappen på versionen")

    # Uppdatera startmeddelandet: version, länk till senaste filen och status
    if starter_embed is not None:
        set_field(starter_embed, FIELD_VERSION, f"v{version} · [Visa filen]({version_msg.jump_url})")
        set_field(starter_embed, FIELD_STATUS, f"🆕 {STATUS_NEW}")
        starter_embed.color = COLOR_NEW
        start_url = viewer_url(thread.id) if viewable else None
        if start_url:
            await starter.edit(embed=starter_embed, view=ThreadView(viewer_url=start_url))
        else:
            await starter.edit(embed=starter_embed)
    
    await set_thread_status(thread, STATUS_NEW)

    deleted = await safe_delete(c.message)
    if not c.modal:
        await finish_prompt(interaction)
    interaction.client.request_refresh()
    category = ""
    if starter_embed is not None:
        category = next((f.value for f in starter_embed.fields if f.name == "📂 Kategori"), "")
    schedule_model_check(interaction.client, thread, c, user.id, category)

    tip = filename_tip(c.attachment.filename) if c.attachment else ""
    warn = "" if deleted else DELETE_WARNING
    await send_result(
        interaction,
        f"✅ Version **v{version}** är uppladdad!{tip}{warn}\n{version_msg.jump_url}",
        c.modal,
    )


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
            await followup_temp(interaction, "❌ Något gick fel när den nya versionen skulle postas.", EPHEMERAL_LIFETIME)

    async def _publish(self, interaction: discord.Interaction, thread: discord.Thread, c: Collected) -> None:
        await publish_new_version(interaction, thread, c, (self.notes_input.value or "").strip())

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Modal-fel (version)", exc_info=error)
        msg = "❌ Ett oväntat fel inträffade."
        if interaction.response.is_done():
            await followup_temp(interaction, msg, EPHEMERAL_LIFETIME)
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
# Formulär med inbyggd filuppladdning (ingen fil i chatten!)
# --------------------------------------------------------------------------- #
def modal_upload_supported() -> bool:
    """Kräver ny discord.py med Label + FileUpload i modaler."""
    return hasattr(discord.ui, "Label") and hasattr(discord.ui, "FileUpload")


MODAL_UPLOAD = modal_upload_supported()


def category_options(forum: discord.ForumChannel | None) -> list[discord.SelectOption]:
    status_names = {s.casefold() for s in ALL_STATUSES}
    options: list[discord.SelectOption] = []
    if forum is not None:
        tags = [t for t in forum.available_tags if t.name.strip().casefold() not in status_names]
        for tag in tags[:25]:  # Discord tillåter max 25 alternativ
            options.append(
                discord.SelectOption(label=truncate(tag.name, 100), value=str(tag.id), emoji=tag.emoji)
            )
    if not options:
        for name in FALLBACK_CATEGORIES:
            options.append(discord.SelectOption(label=name, value=f"name:{name}"))
    return options


async def reply_error(interaction: discord.Interaction, text: str) -> None:
    """Felmeddelande efter defer (ersätter 'tänker ...'-meddelandet)."""
    try:
        await interaction.edit_original_response(content=text)
        schedule_delete_original(interaction, EPHEMERAL_LIFETIME)
    except discord.HTTPException:
        await followup_temp(interaction, text, EPHEMERAL_LIFETIME)


class UploadModalV2(discord.ui.Modal, title="Ladda upp fil"):
    """Allt i ett formulär: titel, kategori, beskrivning, fil (+ bild) och valfri länk."""

    def __init__(self, forum: discord.ForumChannel | None) -> None:
        super().__init__()
        self.title_input = discord.ui.TextInput(
            placeholder="T.ex. BMW M3 E46",
            style=discord.TextStyle.short,
            required=True,
            max_length=80,
        )
        self.category_select = discord.ui.Select(
            placeholder="Välj kategori ...",
            options=category_options(forum),
            min_values=1,
            max_values=1,
            required=True,
        )
        self.description_input = discord.ui.TextInput(
            placeholder="Berätta lite om projektet ...",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1500,
        )
        self.file_upload = discord.ui.FileUpload(required=False, min_values=0, max_values=2)
        self.link_input = discord.ui.TextInput(
            placeholder="Drive / Dropbox – bara om filen är för stor",
            style=discord.TextStyle.short,
            required=False,
            max_length=300,
        )
        self.add_item(discord.ui.Label(text="Titel / Namn", component=self.title_input))
        self.add_item(discord.ui.Label(text="Kategori", component=self.category_select))
        self.add_item(discord.ui.Label(text="Beskrivning (valfritt)", component=self.description_input))
        self.add_item(
            discord.ui.Label(
                text="Fil + förhandsbild (valfritt)",
                description="Max 2 filer: modellen och gärna en bild på den",
                component=self.file_upload,
            )
        )
        self.add_item(discord.ui.Label(text="Länk (om filen är för stor)", component=self.link_input))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        link = (self.link_input.value or "").strip()
        attachments = list(self.file_upload.values)
        if link and not valid_link(link):
            await reply_error(interaction, "❌ Länken måste börja med http:// eller https://")
            return
        if not attachments and not link:
            await reply_error(interaction, "❌ Bifoga en fil i formuläret eller ange en länk. Försök igen.")
            return

        value = self.category_select.values[0]
        label = next((o.label for o in self.category_select.options if o.value == value), value)
        tag_id = None if value.startswith("name:") else int(value)

        if attachments:
            collected = await build_collected(interaction, attachments, None)
            if collected is None:
                return
        else:
            collected = Collected(link=link)
        collected.modal = True

        try:
            await publish_new_post(
                interaction,
                collected,
                self.title_input.value.strip(),
                (self.description_input.value or "").strip(),
                label,
                tag_id,
            )
        except Exception:
            log.exception("Fel vid uppladdning till forumet")
            await reply_error(
                interaction,
                "❌ Något gick fel när filen skulle postas i forumet. "
                "Kontrollera mina behörigheter och försök igen.",
            )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Modal-fel (formulär)", exc_info=error)
        if interaction.response.is_done():
            await reply_error(interaction, "❌ Ett oväntat fel inträffade.")
        else:
            await interaction.response.send_message(
                "❌ Ett oväntat fel inträffade.", ephemeral=True, delete_after=EPHEMERAL_LIFETIME
            )


class VersionModalV2(discord.ui.Modal, title="Ny version"):
    """Ny version: ändringsnotis, fil (+ bild) och valfri länk i samma formulär."""

    def __init__(self) -> None:
        super().__init__()
        self.notes_input = discord.ui.TextInput(
            placeholder="T.ex. fixade UV:erna, lade till texturer ...",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1500,
        )
        self.file_upload = discord.ui.FileUpload(required=False, min_values=0, max_values=2)
        self.link_input = discord.ui.TextInput(
            placeholder="Drive / Dropbox – bara om filen är för stor",
            style=discord.TextStyle.short,
            required=False,
            max_length=300,
        )
        self.add_item(discord.ui.Label(text="Vad har ändrats? (valfritt)", component=self.notes_input))
        self.add_item(
            discord.ui.Label(
                text="Fil + förhandsbild",
                description="Max 2 filer: modellen och gärna en bild på den",
                component=self.file_upload,
            )
        )
        self.add_item(discord.ui.Label(text="Länk (om filen är för stor)", component=self.link_input))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        link = (self.link_input.value or "").strip()
        attachments = list(self.file_upload.values)
        if link and not valid_link(link):
            await reply_error(interaction, "❌ Länken måste börja med http:// eller https://")
            return
        if not attachments and not link:
            await reply_error(interaction, "❌ Bifoga en fil i formuläret eller ange en länk. Försök igen.")
            return

        thread = await get_thread(interaction)
        if thread is None:
            await reply_error(interaction, "❌ Knappen måste användas inne i en forumtråd.")
            return

        if attachments:
            collected = await build_collected(interaction, attachments, None)
            if collected is None:
                return
        else:
            collected = Collected(link=link)
        collected.modal = True

        try:
            await publish_new_version(interaction, thread, collected, (self.notes_input.value or "").strip())
        except Exception:
            log.exception("Fel vid ny version")
            await reply_error(interaction, "❌ Något gick fel när den nya versionen skulle postas.")

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Modal-fel (ny version, formulär)", exc_info=error)
        if interaction.response.is_done():
            await reply_error(interaction, "❌ Ett oväntat fel inträffade.")
        else:
            await interaction.response.send_message(
                "❌ Ett oväntat fel inträffade.", ephemeral=True, delete_after=EPHEMERAL_LIFETIME
            )


async def try_send_modal_v2(interaction: discord.Interaction, build) -> bool:
    """Försöker öppna formuläret med filuppladdning. Returnerar False om det inte går (då används gamla flödet)."""
    global MODAL_UPLOAD
    if not MODAL_UPLOAD:
        return False
    try:
        await interaction.response.send_modal(build())
        return True
    except Exception:
        MODAL_UPLOAD = False  # Stäng av så ingen drabbas igen – gamla flödet tar över
        log.exception(
            "Formuläret med filuppladdning fungerade inte – faller tillbaka på chatt-uppladdning. "
            "Uppdatera discord.py (pip install -U discord.py)."
        )
        return False


# --------------------------------------------------------------------------- #
# Knappar i forumtråden (permanent view)
# --------------------------------------------------------------------------- #
class ThreadView(discord.ui.View):
    def __init__(self, viewer_url: str | None = None) -> None:
        super().__init__(timeout=None)
        if viewer_url:
            self.add_item(
                discord.ui.Button(
                    label="Öppna 3D-vy",
                    emoji="🔎",
                    style=discord.ButtonStyle.link,
                    url=viewer_url,
                )
            )

    @discord.ui.button(
        label="Ladda upp ny version",
        style=discord.ButtonStyle.primary,
        emoji="🔄",
        custom_id="filbot:new_version",
    )
    async def new_version_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if await try_send_modal_v2(interaction, VersionModalV2):
            return
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
            
        interaction.client.request_refresh()

        thread = await get_thread(interaction)
        if thread is None:
            return

        try:
            await set_thread_status(thread, status)
        except discord.HTTPException:
            log.exception("Kunde inte byta statustagg eller uppdatera namnet")

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
        # Nytt flöde: allt i ett formulär, filen laddas upp direkt i formuläret (inget chattmeddelande)
        cached_forum = interaction.client.get_channel(FORUM_CHANNEL_ID)
        if await try_send_modal_v2(
            interaction,
            lambda: UploadModalV2(cached_forum if isinstance(cached_forum, discord.ForumChannel) else None),
        ):
            return

        # Gammalt flöde (om discord.py är för gammal): välj kategori -> formulär -> bifoga i chatten
        # Säkerhet så boten hinner hämta kategorier utan att dö i Discord (15 min timer)
        await interaction.response.defer(ephemeral=True)
        
        forum = interaction.client.get_channel(FORUM_CHANNEL_ID)
        if not isinstance(forum, discord.ForumChannel):
            try:
                forum = await interaction.client.fetch_channel(FORUM_CHANNEL_ID)
            except discord.HTTPException:
                forum = None

        await followup_temp(
            interaction,
            "📂 Välj vilken **kategori** din fil tillhör:",
            view=CategoryView(forum),
        )


# --------------------------------------------------------------------------- #
# Uppladdningspanel (embed + knapp)
# --------------------------------------------------------------------------- #
def build_panel_embed() -> discord.Embed:
    if MODAL_UPLOAD:
        steps = (
            "**Så här gör du:**\n"
            "1️⃣ Tryck på **Ladda upp fil**\n"
            "2️⃣ Fyll i formuläret: titel, kategori och beskrivning\n"
            "3️⃣ Lägg filen direkt i formuläret – gärna en förhandsbild också 🖼️ "
            "(eller en länk om filen är stor)\n"
            "4️⃣ Skicka! Boten postar den i forumet åt dig ✨\n\n"
            "Inget behöver skickas i chatten – allt sker i formuläret."
        )
    else:
        steps = (
            "**Så här gör du:**\n"
            "1️⃣ Tryck på **Ladda upp fil**\n"
            "2️⃣ Välj kategori i listan\n"
            "3️⃣ Fyll i titel och beskrivning (eller en länk om filen är stor)\n"
            "4️⃣ Bifoga filen i chatten inom 2 minuter – lägg gärna med en "
            "förhandsbild i samma meddelande 🖼️\n"
            "5️⃣ Klart! Boten postar den i forumet åt dig ✨"
        )
    embed = discord.Embed(
        title=PANEL_TITLE,
        description=(
            "Klicka på knappen nedan för att ladda upp en fil "
            "(.blend, .fbx, .obj, .unitypackage m.fl.) till forumet.\n\n"
            + steps
            + "\n\nI varje forumtråd finns knappar för **ny version**, **godkänn** "
            "och **behöver ändras**."
        ),
        color=COLOR_NEW,
    )
    embed.set_footer(text="Kategorierna hämtas från forumets taggar")
    return embed


async def ensure_panel(bot: "FileBot", channel_id: int, title: str, build_embed, build_view) -> None:
    """Postar en panel (embed + knapp) i kanalen om den inte redan finns."""
    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        log.warning("Kanal %s är inte en textkanal.", channel_id)
        return

    async for msg in channel.history(limit=100):
        if msg.author.id == bot.user.id and msg.embeds and msg.embeds[0].title == title:
            # Håll panelen uppdaterad (t.ex. när uppladdningsflödet ändrats)
            new_embed = build_embed()
            if msg.embeds[0].description != new_embed.description:
                try:
                    await msg.edit(embed=new_embed, view=build_view())
                    log.info("Panelen '%s' uppdaterades.", title)
                except discord.HTTPException:
                    log.exception("Kunde inte uppdatera panelen '%s'", title)
            else:
                log.info("Panelen '%s' finns redan.", title)
            return

    await channel.send(embed=build_embed(), view=build_view())
    log.info("Panelen '%s' postad i kanal %s.", title, channel_id)


# --------------------------------------------------------------------------- #
# To-do: uppgiftskort (Discord är databasen – inget sparas på Railways disk)
# --------------------------------------------------------------------------- #
@dataclass
class Task:
    message: discord.Message
    title: str
    notes: str
    assignee_id: int | None
    deadline_ts: int | None
    done: bool


def parse_task(message: discord.Message) -> Task | None:
    if not message.embeds:
        return None
    embed = message.embeds[0]
    status = next((f.value for f in embed.fields if f.name == FIELD_TASK_STATUS), None)
    if status is None:
        return None

    assignee = None
    deadline = None
    for f in embed.fields:
        if f.name == FIELD_ASSIGNEE:
            m = re.search(r"<@!?(\d+)>", f.value or "")
            assignee = int(m.group(1)) if m else None
        elif f.name == FIELD_DEADLINE:
            m = re.search(r"<t:(\d+)", f.value or "")
            deadline = int(m.group(1)) if m else None

    return Task(
        message=message,
        title=embed.title or "(utan titel)",
        notes=embed.description or "",
        assignee_id=assignee,
        deadline_ts=deadline,
        done="klar" in status.casefold(),
    )


def parse_deadline(text: str) -> datetime | None:
    """Förstår: idag, imorgon, 25/10, 25/10 18:00, 2026-10-25, 2026-10-25 18:00."""
    text = text.strip().lower()
    hour, minute = 23, 59
    m = re.search(r"\s(\d{1,2}):(\d{2})$", text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        text = text[: m.start()].strip()

    now = datetime.now(TZ)
    try:
        if text in ("idag", "today"):
            day = now.date()
        elif text in ("imorgon", "tomorrow"):
            day = (now + timedelta(days=1)).date()
        elif (m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)):
            day = datetime(int(m[1]), int(m[2]), int(m[3])).date()
        elif (m := re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})(?:[/.\-](\d{2,4}))?", text)):
            year = int(m[3]) if m[3] else now.year
            if year < 100:
                year += 2000
            day = datetime(year, int(m[2]), int(m[1])).date()
            if not m[3] and day < now.date():  # Utan årtal och redan passerat -> nästa år
                day = datetime(year + 1, int(m[2]), int(m[1])).date()
        else:
            return None
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)
    except ValueError:
        return None


def build_task_embed(title: str, notes: str, user: discord.abc.User, deadline: datetime | None) -> discord.Embed:
    embed = discord.Embed(
        title=truncate(title, 256),
        description=truncate(notes, 4000) if notes else None,
        color=COLOR_NEW,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name=FIELD_TASK_STATUS, value="🔲 Att göra", inline=True)
    embed.add_field(name=FIELD_ASSIGNEE, value="— ledig —", inline=True)
    if deadline is not None:
        ts = int(deadline.timestamp())
        embed.add_field(name=FIELD_DEADLINE, value=f"<t:{ts}:f>\n<t:{ts}:R>", inline=True)
    else:
        embed.add_field(name=FIELD_DEADLINE, value="—", inline=True)
    embed.set_footer(text=f"Skapad av {user.display_name}")
    return embed


class TaskModal(discord.ui.Modal, title="Ny uppgift"):
    title_input = discord.ui.TextInput(
        label="Vad ska göras?",
        placeholder="T.ex. Modellera hjul till bilen",
        style=discord.TextStyle.short,
        required=True,
        max_length=100,
    )
    notes_input = discord.ui.TextInput(
        label="Detaljer (valfritt)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
    )
    deadline_input = discord.ui.TextInput(
        label="Deadline (valfritt)",
        placeholder="imorgon  |  25/10  |  25/10 18:00  |  2026-10-25",
        style=discord.TextStyle.short,
        required=False,
        max_length=30,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = (self.deadline_input.value or "").strip()
        deadline = None
        if raw:
            deadline = parse_deadline(raw)
            if deadline is None:
                await interaction.response.send_message(
                    "❌ Jag förstod inte deadlinen. Skriv t.ex. `imorgon`, `25/10`, "
                    "`25/10 18:00` eller `2026-10-25`.",
                    ephemeral=True,
                    delete_after=QUICK_LIFETIME,
                )
                return

        embed = build_task_embed(
            self.title_input.value.strip(),
            (self.notes_input.value or "").strip(),
            interaction.user,
            deadline,
        )
        await interaction.channel.send(embed=embed, view=TaskView())
        await interaction.response.send_message(
            "✅ Uppgiften är skapad!", ephemeral=True, delete_after=QUICK_LIFETIME
        )
        interaction.client.request_refresh()

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Modal-fel (uppgift)", exc_info=error)
        msg = "❌ Ett oväntat fel inträffade."
        if interaction.response.is_done():
            await followup_temp(interaction, msg, QUICK_LIFETIME)
        else:
            await interaction.response.send_message(msg, ephemeral=True, delete_after=QUICK_LIFETIME)


class TodoPanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Ny uppgift",
        style=discord.ButtonStyle.primary,
        emoji="➕",
        custom_id="filbot:new_task",
    )
    async def new_task_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(TaskModal())


class TaskView(discord.ui.View):
    """Knappar på varje uppgiftskort. done=True visar bara Återöppna + Ta bort."""

    def __init__(self, done: bool = False) -> None:
        super().__init__(timeout=None)
        if done:
            self.remove_item(self.done_button)
            self.remove_item(self.claim_button)
        else:
            self.remove_item(self.reopen_button)

    @discord.ui.button(label="Klar", style=discord.ButtonStyle.success, emoji="✅", custom_id="filbot:task_done")
    async def done_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        embed = interaction.message.embeds[0].copy()
        set_field(embed, FIELD_TASK_STATUS, f"✅ Klar ({interaction.user.display_name})")
        embed.color = COLOR_APPROVED
        await interaction.response.edit_message(embed=embed, view=TaskView(done=True))
        interaction.client.request_refresh()

    @discord.ui.button(label="Återöppna", style=discord.ButtonStyle.secondary, emoji="↩️", custom_id="filbot:task_reopen")
    async def reopen_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        embed = interaction.message.embeds[0].copy()
        set_field(embed, FIELD_TASK_STATUS, "🔲 Att göra")
        embed.color = COLOR_NEW
        await interaction.response.edit_message(embed=embed, view=TaskView(done=False))
        interaction.client.request_refresh()

    @discord.ui.button(label="Ta / släpp", style=discord.ButtonStyle.primary, emoji="🙋", custom_id="filbot:task_claim")
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        embed = interaction.message.embeds[0].copy()
        task = parse_task(interaction.message)
        if task is not None and task.assignee_id == interaction.user.id:
            set_field(embed, FIELD_ASSIGNEE, "— ledig —")
        else:
            set_field(embed, FIELD_ASSIGNEE, interaction.user.mention)
        await interaction.response.edit_message(embed=embed)
        interaction.client.request_refresh()

    @discord.ui.button(label="Ta bort", style=discord.ButtonStyle.danger, emoji="🗑️", custom_id="filbot:task_delete")
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            log.exception("Kunde inte radera uppgiftskort")
        interaction.client.request_refresh()


def build_todo_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title=TODO_PANEL_TITLE,
        description=(
            "Tryck på **➕ Ny uppgift** för att lägga till något som ska göras.\n"
            "Varje uppgift dyker upp som ett kort här nedanför med knapparna:\n"
            "✅ **Klar** · 🙋 **Ta / släpp** (ansvarig) · 🗑️ **Ta bort**\n\n"
            "Boten påminner om deadlines automatiskt och visar allt i dashboarden."
        ),
        color=COLOR_NEW,
    )
    return embed


async def fetch_tasks(bot: "FileBot") -> list[Task]:
    channel_id = env_int("TODO_CHANNEL_ID")
    if channel_id is None:
        return []
    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    found: list[Task] = []
    async for msg in channel.history(limit=200):
        if msg.author.id != bot.user.id:
            continue
        task = parse_task(msg)
        if task is not None:
            found.append(task)
    return found


async def check_reminders(bot: "FileBot") -> None:
    """Påminner om deadlines (en gång när det är <24h kvar, en gång när den passerat)."""
    now = int(datetime.now(timezone.utc).timestamp())
    for task in await fetch_tasks(bot):
        if task.done or task.deadline_ts is None:
            continue

        msg = task.message
        embed = msg.embeds[0].copy()
        footer = embed.footer.text or ""

        if now >= task.deadline_ts and "🔴" not in footer:
            icon, text = "🔴", "Försenad"
        elif task.deadline_ts - 86400 <= now < task.deadline_ts and "🔔" not in footer:
            icon, text = "🔔", "Deadline snart"
        else:
            continue

        mention = ""
        allowed = discord.AllowedMentions.none()
        if task.assignee_id:
            mention = f"<@{task.assignee_id}> "
            allowed = discord.AllowedMentions(users=[discord.Object(id=task.assignee_id)])

        await msg.channel.send(
            f"{mention}{icon} **{text}:** [{truncate(task.title, 80)}]({msg.jump_url}) "
            f"– <t:{task.deadline_ts}:R>",
            allowed_mentions=allowed,
        )
        embed.set_footer(text=f"{footer} {icon}".strip())
        await msg.edit(embed=embed)


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
@dataclass
class ModelInfo:
    title: str
    category: str
    status: str  # "new" | "changes" | "approved"
    version: int
    uploader_id: int | None
    url: str
    updated_ts: int
    viewer: str | None = None


def classify_status(raw: str) -> str:
    low = raw.casefold()
    if STATUS_APPROVED.casefold() in low:
        return "approved"
    if STATUS_CHANGES.casefold() in low:
        return "changes"
    return "new"


async def collect_models(bot: "FileBot") -> list[ModelInfo]:
    forum = bot.get_channel(FORUM_CHANNEL_ID)
    if forum is None:
        forum = await bot.fetch_channel(FORUM_CHANNEL_ID)
    if not isinstance(forum, discord.ForumChannel):
        return []

    threads = {t.id: t for t in forum.threads}
    try:
        async for t in forum.archived_threads(limit=50):
            threads.setdefault(t.id, t)
    except discord.HTTPException:
        log.warning("Kunde inte hämta arkiverade trådar (saknar Read Message History?)")

    sem = asyncio.Semaphore(5)

    async def load(thread: discord.Thread) -> ModelInfo | None:
        async with sem:
            try:
                starter = await thread.fetch_message(thread.id)
            except discord.HTTPException:
                return None
        if starter.author.id != bot.user.id or not starter.embeds:
            return None
        embed = starter.embeds[0]
        status_raw = next((f.value for f in embed.fields if f.name == FIELD_STATUS), None)
        if status_raw is None:
            return None
        category = next((f.value for f in embed.fields if f.name == "📂 Kategori"), "–")
        if thread.last_message_id:
            updated = int(discord.utils.snowflake_time(thread.last_message_id).timestamp())
        else:
            updated = int(starter.created_at.timestamp())
        return ModelInfo(
            title=embed.title or thread.name,
            category=category,
            status=classify_status(status_raw),
            version=current_version(embed),
            uploader_id=uploader_id(embed),
            url=thread.jump_url,
            updated_ts=updated,
            viewer=(
                viewer_url(thread.id)
                if any(is_viewable(a.filename) for a in starter.attachments)
                else None
            ),
        )

    results = await asyncio.gather(*(load(t) for t in list(threads.values())[:40]))
    return [r for r in results if r is not None]


def progress_bar(done: int, total: int, width: int = 12) -> str:
    filled = round(width * done / total) if total else 0
    return "▓" * filled + "░" * (width - filled)


def fit_lines(lines: list[str], limit: int = 1000) -> str:
    out: list[str] = []
    used = 0
    for i, line in enumerate(lines):
        if used + len(line) + 1 > limit - 30:
            out.append(f"… +{len(lines) - i} till")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out) or "—"


async def build_dashboard(bot: "FileBot") -> discord.Embed:
    now = int(datetime.now(timezone.utc).timestamp())
    models = await collect_models(bot)
    task_list = await fetch_tasks(bot)

    models_done = sum(1 for m in models if m.status == "approved")
    open_tasks = [t for t in task_list if not t.done]
    tasks_done = len(task_list) - len(open_tasks)
    overdue = [t for t in open_tasks if t.deadline_ts is not None and t.deadline_ts < now]
    needs_changes = sum(1 for m in models if m.status == "changes")

    embed = discord.Embed(
        title=DASHBOARD_TITLE,
        description=(
            f"🧊 **Modeller**  `{progress_bar(models_done, len(models))}`  {models_done}/{len(models)} klara\n"
            f"📋 **Uppgifter**  `{progress_bar(tasks_done, len(task_list))}`  {tasks_done}/{len(task_list)} klara"
        ),
        timestamp=discord.utils.utcnow(),
    )

    if models and models_done == len(models) and not open_tasks:
        embed.color = COLOR_APPROVED
    elif overdue or needs_changes:
        embed.color = COLOR_CHANGES
    else:
        embed.color = COLOR_NEW

    # Modeller, grupperade efter status
    groups = [
        ("🛠️ Behöver ändras", "changes"),
        ("🆕 Nya", "new"),
        ("✅ Klara", "approved"),
    ]
    for label, key in groups:
        items = sorted((m for m in models if m.status == key), key=lambda m: -m.updated_ts)
        if not items:
            continue
        lines = []
        for m in items:
            line = f"[{truncate(m.title, 40)}]({m.url}) · v{m.version}"
            if m.uploader_id:
                line += f" · <@{m.uploader_id}>"
            line += f" · <t:{m.updated_ts}:R>"
            if m.viewer:
                line += f" · [🔎]({m.viewer})"
            lines.append(line)
        embed.add_field(name=f"{label} ({len(items)})", value=fit_lines(lines), inline=False)

    if not models:
        embed.add_field(name="🧊 Modeller", value="Inga modeller uppladdade än.", inline=False)

    # To-do
    open_sorted = sorted(
        open_tasks,
        key=lambda t: (t.deadline_ts is None, t.deadline_ts or 0),
    )
    lines = []
    for t in open_sorted:
        if t.deadline_ts is not None and t.deadline_ts < now:
            icon = "🔴"
        elif t.deadline_ts is not None and t.deadline_ts - 86400 < now:
            icon = "🟡"
        else:
            icon = "🔲"
        line = f"{icon} [{truncate(t.title, 45)}]({t.message.jump_url})"
        line += f" · <@{t.assignee_id}>" if t.assignee_id else " · ledig"
        if t.deadline_ts is not None:
            line += f" · <t:{t.deadline_ts}:R>"
        lines.append(line)
    todo_value = fit_lines(lines) if lines else "🎉 Inget kvar att göra!"
    embed.add_field(name=f"📋 To-do ({len(open_tasks)} öppna)", value=todo_value, inline=False)

    embed.set_footer(text="Uppdateras automatiskt")
    return embed


async def update_dashboard(bot: "FileBot") -> None:
    channel_id = env_int("DASHBOARD_CHANNEL_ID")
    if channel_id is None:
        return

    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    embed = await build_dashboard(bot)

    message = bot._dashboard_message
    if message is None:
        async for msg in channel.history(limit=100):
            if (
                msg.author.id == bot.user.id
                and msg.embeds
                and msg.embeds[0].title == DASHBOARD_TITLE
            ):
                message = msg
                break

    if message is not None:
        try:
            await message.edit(embed=embed)
            bot._dashboard_message = message
            return
        except discord.NotFound:
            message = None

    bot._dashboard_message = await channel.send(embed=embed)


class HQCog(commands.Cog):
    """Bakgrundsjobb: dashboard-uppdatering och deadline-påminnelser."""

    def __init__(self, bot: "FileBot") -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.dashboard_loop.start()
        self.reminder_loop.start()

    async def cog_unload(self) -> None:
        self.dashboard_loop.cancel()
        self.reminder_loop.cancel()

    @tasks.loop(minutes=10)
    async def dashboard_loop(self) -> None:
        try:
            await update_dashboard(self.bot)
        except Exception:
            log.exception("Dashboard-uppdatering misslyckades")

    @tasks.loop(minutes=30)
    async def reminder_loop(self) -> None:
        try:
            await check_reminders(self.bot)
        except Exception:
            log.exception("Påminnelsekontroll misslyckades")

    @dashboard_loop.before_loop
    @reminder_loop.before_loop
    async def _wait_until_ready(self) -> None:
        await self.bot.wait_until_ready()


# --------------------------------------------------------------------------- #
# Automatisk modellkoll (ren Python – ingen Blender behövs)
# --------------------------------------------------------------------------- #
class SkipAnalysis(Exception):
    """Filen är inte en modell – gör ingen koll och säg ingenting."""


class AnalysisError(Exception):
    """Modellen kunde inte analyseras – meddelandet visas för användaren."""


@dataclass
class ModelStats:
    fmt: str
    triangles: int
    vertices: int | None
    materials: int | None
    uv: str  # "ok" | "missing" | "partial" | "n/a"
    size_m: tuple[float, float, float] | None
    center_m: tuple[float, float, float] | None
    units_known: bool
    notes: list[str] = field(default_factory=list)


def _bbox_result(mn, mx, scale: float = 1.0):
    if mn[0] == math.inf:
        return None, None
    size = tuple((mx[i] - mn[i]) * scale for i in range(3))
    center = tuple(((mx[i] + mn[i]) / 2) * scale for i in range(3))
    return size, center


# ---- glTF / GLB ----------------------------------------------------------- #
def _mat_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def _identity():
    return [[1.0 if i == j else 0.0 for j in range(4)] for i in range(4)]


def _node_matrix(node):
    m = node.get("matrix")
    if m and len(m) == 16:
        return [[float(m[c * 4 + r]) for c in range(4)] for r in range(4)]
    t = node.get("translation", [0, 0, 0])
    x, y, z, w = node.get("rotation", [0, 0, 0, 1])
    s = node.get("scale", [1, 1, 1])
    r = [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]
    return [
        [r[0][0] * s[0], r[0][1] * s[1], r[0][2] * s[2], t[0]],
        [r[1][0] * s[0], r[1][1] * s[1], r[1][2] * s[2], t[1]],
        [r[2][0] * s[0], r[2][1] * s[1], r[2][2] * s[2], t[2]],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _analyze_gltf(data: bytes, is_glb: bool) -> ModelStats:
    try:
        if is_glb:
            if len(data) < 20 or data[:4] != b"glTF":
                raise AnalysisError("Ogiltig GLB-fil.")
            pos, doc = 12, None
            while pos + 8 <= len(data):
                chunk_len, chunk_type = struct.unpack_from("<II", data, pos)
                if chunk_type == 0x4E4F534A:  # "JSON"
                    doc = json.loads(data[pos + 8 : pos + 8 + chunk_len].decode("utf-8"))
                    break
                pos += 8 + chunk_len
            if doc is None:
                raise AnalysisError("Hittade ingen JSON-data i GLB-filen.")
        else:
            doc = json.loads(data.decode("utf-8-sig"))
    except (ValueError, struct.error) as exc:
        raise AnalysisError(f"Kunde inte läsa glTF-filen ({exc}).") from exc

    accessors = doc.get("accessors", [])
    meshes = doc.get("meshes", [])
    nodes = doc.get("nodes", [])
    cache: dict[int, list] = {}

    def mesh_parts(idx: int) -> list:
        if idx in cache:
            return cache[idx]
        parts = []
        if 0 <= idx < len(meshes):
            for prim in meshes[idx].get("primitives", []):
                mode = prim.get("mode", 4)
                attrs = prim.get("attributes", {})
                if mode not in (4, 5, 6) or "POSITION" not in attrs:
                    continue
                pa = accessors[attrs["POSITION"]]
                vcount = pa.get("count", 0)
                if "indices" in prim:
                    icount = accessors[prim["indices"]].get("count", 0)
                else:
                    icount = vcount
                tris = icount // 3 if mode == 4 else max(icount - 2, 0)
                bb = (pa["min"], pa["max"]) if pa.get("min") and pa.get("max") else None
                parts.append((tris, vcount, bb, "TEXCOORD_0" in attrs, prim.get("material")))
        cache[idx] = parts
        return parts

    instances: list[tuple[int, list]] = []

    def walk(idx: int, parent, depth: int) -> None:
        if depth > 64 or len(instances) > 20000 or not (0 <= idx < len(nodes)):
            return
        node = nodes[idx]
        m = _mat_mul(parent, _node_matrix(node))
        if "mesh" in node:
            instances.append((node["mesh"], m))
        for child in node.get("children", []):
            walk(child, m, depth + 1)

    scenes = doc.get("scenes")
    if scenes:
        scene_idx = doc.get("scene", 0)
        scene = scenes[scene_idx if 0 <= scene_idx < len(scenes) else 0]
        roots = scene.get("nodes", [])
    else:
        child_ids = {c for n in nodes for c in n.get("children", [])}
        roots = [i for i in range(len(nodes)) if i not in child_ids]
    for root in roots:
        walk(root, _identity(), 0)
    if not instances and not nodes:
        instances = [(i, _identity()) for i in range(len(meshes))]

    tris = verts = 0
    materials: set[int] = set()
    parts_total = parts_uv = 0
    mn = [math.inf] * 3
    mx = [-math.inf] * 3
    for mesh_idx, m in instances:
        for p_tris, p_verts, bb, has_uv, mat in mesh_parts(mesh_idx):
            tris += p_tris
            verts += p_verts
            parts_total += 1
            parts_uv += 1 if has_uv else 0
            if mat is not None:
                materials.add(mat)
            if bb:
                for cx in (bb[0][0], bb[1][0]):
                    for cy in (bb[0][1], bb[1][1]):
                        for cz in (bb[0][2], bb[1][2]):
                            for i in range(3):
                                v = m[i][0] * cx + m[i][1] * cy + m[i][2] * cz + m[i][3]
                                mn[i] = min(mn[i], v)
                                mx[i] = max(mx[i], v)

    if parts_total == 0:
        uv = "n/a"
    elif parts_uv == parts_total:
        uv = "ok"
    elif parts_uv == 0:
        uv = "missing"
    else:
        uv = "partial"

    size, center = _bbox_result(mn, mx)
    return ModelStats("glTF/GLB", tris, verts, len(materials), uv, size, center, units_known=True)


# ---- OBJ ------------------------------------------------------------------ #
def _analyze_obj(data: bytes) -> ModelStats:
    verts = uvs = tris = 0
    materials: set[str] = set()
    mn = [math.inf] * 3
    mx = [-math.inf] * 3
    for line in data.decode("utf-8", errors="ignore").splitlines():
        if line.startswith("v "):
            parts = line.split()
            try:
                x, y, z = float(parts[1]), float(parts[2]), float(parts[3])
            except (IndexError, ValueError):
                continue
            verts += 1
            if x < mn[0]: mn[0] = x
            if y < mn[1]: mn[1] = y
            if z < mn[2]: mn[2] = z
            if x > mx[0]: mx[0] = x
            if y > mx[1]: mx[1] = y
            if z > mx[2]: mx[2] = z
        elif line.startswith("vt "):
            uvs += 1
        elif line.startswith("f "):
            n = len(line.split()) - 1
            if n >= 3:
                tris += n - 2
        elif line.startswith("usemtl "):
            materials.add(line[7:].strip())
    if verts == 0:
        raise AnalysisError("Hittade inga vertices i OBJ-filen.")
    size, center = _bbox_result(mn, mx)
    return ModelStats(
        "OBJ", tris, verts, len(materials), "ok" if uvs else "missing", size, center,
        units_known=False,
        notes=["OBJ saknar enhetsinfo – storleken antar 1 enhet = 1 meter."],
    )


# ---- STL ------------------------------------------------------------------ #
def _analyze_stl(data: bytes) -> ModelStats:
    mn = [math.inf] * 3
    mx = [-math.inf] * 3
    tris = 0
    binary = False
    if len(data) >= 84:
        n = struct.unpack_from("<I", data, 80)[0]
        binary = 84 + 50 * n == len(data)

    if binary:
        tris = n
        for t in struct.iter_unpack("<12fH", data[84 : 84 + 50 * n]):
            for off in (3, 6, 9):
                x, y, z = t[off], t[off + 1], t[off + 2]
                if x < mn[0]: mn[0] = x
                if y < mn[1]: mn[1] = y
                if z < mn[2]: mn[2] = z
                if x > mx[0]: mx[0] = x
                if y > mx[1]: mx[1] = y
                if z > mx[2]: mx[2] = z
    elif data.lstrip()[:5].lower() == b"solid":
        count = 0
        for line in data.decode("utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("vertex"):
                parts = line.split()
                try:
                    x, y, z = float(parts[1]), float(parts[2]), float(parts[3])
                except (IndexError, ValueError):
                    continue
                count += 1
                if x < mn[0]: mn[0] = x
                if y < mn[1]: mn[1] = y
                if z < mn[2]: mn[2] = z
                if x > mx[0]: mx[0] = x
                if y > mx[1]: mx[1] = y
                if z > mx[2]: mx[2] = z
        tris = count // 3
    else:
        raise AnalysisError("Kunde inte tolka STL-filen.")

    size, center = _bbox_result(mn, mx)
    return ModelStats(
        "STL", tris, None, None, "n/a", size, center, units_known=False,
        notes=["STL saknar enhet och UV – storleken antar 1 enhet = 1 meter (CAD-filer är ofta i mm)."],
    )


# ---- FBX (binär) ----------------------------------------------------------- #
_FBX_MAGIC = b"Kaydara FBX Binary  "
_FBX_PROP_NODES = {"Vertices", "PolygonVertexIndex", "P", "Geometry"}


def _fbx_read_array(data: bytes, pos: int, code: str):
    _length, enc, comp_len = struct.unpack_from("<III", data, pos)
    pos += 12
    raw = data[pos : pos + comp_len]
    if enc == 1:
        raw = zlib.decompress(raw)
    arr = array.array({"d": "d", "f": "f", "i": "i", "l": "q", "b": "b"}[code])
    arr.frombytes(raw)
    if sys.byteorder == "big":
        arr.byteswap()
    return arr, pos + comp_len


def _fbx_props(data: bytes, pos: int, count: int) -> list:
    props: list = []
    for _ in range(count):
        t = chr(data[pos])
        pos += 1
        if t == "Y":
            props.append(struct.unpack_from("<h", data, pos)[0]); pos += 2
        elif t == "C":
            props.append(data[pos]); pos += 1
        elif t == "I":
            props.append(struct.unpack_from("<i", data, pos)[0]); pos += 4
        elif t == "F":
            props.append(struct.unpack_from("<f", data, pos)[0]); pos += 4
        elif t == "D":
            props.append(struct.unpack_from("<d", data, pos)[0]); pos += 8
        elif t == "L":
            props.append(struct.unpack_from("<q", data, pos)[0]); pos += 8
        elif t in "fdilb":
            arr, pos = _fbx_read_array(data, pos, t)
            props.append(arr)
        elif t in "SR":
            n = struct.unpack_from("<I", data, pos)[0]
            props.append(data[pos + 4 : pos + 4 + n].decode("utf-8", "ignore") if t == "S" else None)
            pos += 4 + n
        else:
            raise AnalysisError("FBX-filen innehåller okänd data.")
    return props


def _fbx_nodes(data: bytes, pos: int, end: int, wide: bool, depth: int = 0) -> list:
    nodes = []
    hdr = 25 if wide else 13
    fmt = "<QQQB" if wide else "<IIIB"
    while pos + hdr <= end and depth < 32:
        end_off, nprops, plen, nlen = struct.unpack_from(fmt, data, pos)
        if end_off == 0:
            break
        name_start = pos + hdr
        name = data[name_start : name_start + nlen].decode("ascii", "ignore")
        props_start = name_start + nlen
        children_start = props_start + plen
        props = _fbx_props(data, props_start, nprops) if name in _FBX_PROP_NODES and nprops else []
        children = _fbx_nodes(data, children_start, end_off, wide, depth + 1) if children_start < end_off else []
        nodes.append((name, props, children))
        pos = end_off
    return nodes


def _analyze_fbx(data: bytes) -> ModelStats:
    if data[:5] == b"; FBX":
        raise AnalysisError("ASCII-FBX stöds inte – exportera som binär FBX (standard i Blender).")
    if data[:20] != _FBX_MAGIC or len(data) < 27:
        raise AnalysisError("Ogiltig FBX-fil.")
    version = struct.unpack_from("<I", data, 23)[0]
    try:
        top = _fbx_nodes(data, 27, len(data), wide=version >= 7500)
    except (struct.error, zlib.error, ValueError) as exc:
        raise AnalysisError(f"Kunde inte läsa FBX-filen ({exc}).") from exc

    unit_factor = 1.0  # 1.0 = centimeter i FBX
    for name, _props, children in top:
        if name != "GlobalSettings":
            continue
        for n2, _p2, c2 in children:
            if n2 == "Properties70":
                for n3, p3, _c3 in c2:
                    if n3 == "P" and len(p3) >= 5 and p3[0] == "UnitScaleFactor":
                        unit_factor = float(p3[4])

    tris = verts = materials = 0
    geoms = geoms_uv = 0
    mn = [math.inf] * 3
    mx = [-math.inf] * 3
    for name, _props, children in top:
        if name != "Objects":
            continue
        for n2, p2, c2 in children:
            if n2 == "Material":
                materials += 1
            elif n2 == "Geometry":
                if len(p2) >= 3 and isinstance(p2[2], str) and p2[2] != "Mesh":
                    continue
                vertices = next((p[0] for nm, p, _ in c2 if nm == "Vertices" and p), None)
                indices = next((p[0] for nm, p, _ in c2 if nm == "PolygonVertexIndex" and p), None)
                if vertices is None or indices is None:
                    continue
                geoms += 1
                if any(nm == "LayerElementUV" for nm, _, _ in c2):
                    geoms_uv += 1
                verts += len(vertices) // 3
                n = 0
                for idx in indices:
                    n += 1
                    if idx < 0:
                        tris += max(n - 2, 0)
                        n = 0
                for axis in range(3):
                    column = vertices[axis::3]
                    if len(column):
                        mn[axis] = min(mn[axis], min(column))
                        mx[axis] = max(mx[axis], max(column))

    if geoms == 0:
        raise AnalysisError("Hittade ingen mesh i FBX-filen.")

    if geoms_uv == geoms:
        uv = "ok"
    elif geoms_uv == 0:
        uv = "missing"
    else:
        uv = "partial"

    size, center = _bbox_result(mn, mx, scale=unit_factor / 100.0)
    return ModelStats(
        "FBX", tris, verts, materials, uv, size, center, units_known=True,
        notes=["FBX-storleken är beräknad från filens enhetsinställning (ungefärlig)."],
    )


# ---- Gemensam ingång ------------------------------------------------------- #
ANALYZABLE_EXTENSIONS = {".glb", ".gltf", ".obj", ".stl", ".fbx"}
MODEL_CHECK_EXTENSIONS = ANALYZABLE_EXTENSIONS | {".zip", ".blend"}


def analyze(data: bytes, filename: str) -> ModelStats:
    ext = ext_of(filename)
    if ext == ".glb":
        return _analyze_gltf(data, True)
    if ext == ".gltf":
        return _analyze_gltf(data, False)
    if ext == ".obj":
        return _analyze_obj(data)
    if ext == ".stl":
        return _analyze_stl(data)
    if ext == ".fbx":
        return _analyze_fbx(data)
    if ext == ".blend":
        raise AnalysisError(
            "`.blend`-filer kan inte analyseras automatiskt.\n"
            "Exportera som **.fbx** eller **.glb** och ladda upp som ny version "
            "(🔄-knappen) så får du en modellkoll."
        )
    if ext == ".zip":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                candidates = [
                    i for i in zf.infolist()
                    if not i.is_dir()
                    and ext_of(i.filename) in ANALYZABLE_EXTENSIONS
                    and i.file_size <= ANALYZE_MAX_BYTES
                ]
                if not candidates:
                    raise SkipAnalysis()
                best = max(candidates, key=lambda i: i.file_size)
                stats = analyze(zf.read(best), best.filename)
        except zipfile.BadZipFile as exc:
            raise AnalysisError("Kunde inte öppna zip-filen.") from exc
        extra = f" ({len(candidates)} modellfiler hittades, den största analyserades)" if len(candidates) > 1 else ""
        stats.notes.insert(0, f"Analyserade `{best.filename}` i zip-filen{extra}.")
        return stats
    raise SkipAnalysis()


# ---- Rapport --------------------------------------------------------------- #
def fmt_int(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def build_report(stats: ModelStats, filename: str, category: str) -> tuple[discord.Embed, bool]:
    budget = TRIANGLE_BUDGETS.get(category.strip().casefold(), DEFAULT_TRIANGLE_BUDGET)
    issues: list[tuple[str, str]] = []  # (nivå, text)

    # Trianglar
    if stats.triangles == 0:
        tri_icon = "🔴"
        issues.append(("hard", "Inga trianglar hittades – är filen tom?"))
    elif stats.triangles > budget * 2:
        tri_icon = "🔴"
        issues.append((
            "hard",
            f"Väldigt många trianglar ({fmt_int(stats.triangles)}). Målet för **{category or 'den här kategorin'}** "
            f"är ≈{fmt_int(budget)}. Använd Decimate-modifier eller retopologi.",
        ))
    elif stats.triangles > budget:
        tri_icon = "⚠️"
        issues.append((
            "warn",
            f"Lite tungt: {fmt_int(stats.triangles)} trianglar (mål ≈{fmt_int(budget)}).",
        ))
    else:
        tri_icon = "✅"

    # Skala och origin
    size_text = "–"
    origin_text = "–"
    if stats.size_m is not None:
        sx, sy, sz = stats.size_m
        size_text = f"{sx:.2f} × {sy:.2f} × {sz:.2f} m"
        biggest = max(stats.size_m)
        level_small = "hard" if stats.units_known else "warn"
        if biggest < 0.01:
            issues.append((level_small, f"Modellen är extremt liten ({biggest * 100:.2f} cm). Troligen fel skala – kolla enheterna vid export (1 enhet = 1 m)."))
        elif biggest > 200:
            issues.append((level_small, f"Modellen är gigantisk ({biggest:.0f} m). Troligen fel skala – kolla enheterna vid export."))
        elif biggest < 0.1:
            issues.append(("warn", f"Väldigt liten modell ({biggest * 100:.1f} cm) – stämmer skalan?"))
        elif biggest > 50:
            issues.append(("warn", f"Mycket stor modell ({biggest:.0f} m) – stämmer skalan?"))

        if stats.center_m is not None and biggest > 0:
            offset = math.sqrt(sum(c * c for c in stats.center_m))
            if offset > biggest:
                origin_text = f"⚠️ {offset:.1f} m från modellen"
                issues.append(("warn", f"Origin ligger {offset:.1f} m från modellen. Sätt origin till modellens mitt/botten innan export (Set Origin)."))
            else:
                origin_text = "✅ Rimlig"

    # UV
    uv_text = {"ok": "✅ Finns", "missing": "⚠️ Saknas", "partial": "⚠️ Delvis", "n/a": "–"}[stats.uv]
    if stats.uv == "missing":
        issues.append(("warn", "Saknar UV-kartor – texturer går inte att lägga på (okej om ni bara använder materialfärger)."))
    elif stats.uv == "partial":
        issues.append(("warn", "Vissa delar saknar UV-kartor."))

    # Material
    mat_text = "–"
    if stats.materials is not None:
        mat_text = str(stats.materials)
        if stats.materials == 0:
            mat_text = "⚠️ 0"
            issues.append(("warn", "Inga material tilldelade – modellen blir rosa/grå i Unity."))
        elif stats.materials > 8:
            mat_text = f"⚠️ {stats.materials}"
            issues.append(("warn", f"Många material ({stats.materials}) – varje material är ett extra draw call i Unity. Slå ihop om möjligt."))

    hard = any(level == "hard" for level, _ in issues)
    if hard:
        color, headline = discord.Color.red(), "🔴 **Åtgärda innan Unity:**"
    elif issues:
        color, headline = COLOR_CHANGES, "🟡 **Fungerar, men kolla det här:**"
    else:
        color, headline = COLOR_APPROVED, "🟢 **Ser bra ut för Unity!** Inga problem hittades."

    lines = [headline]
    for level, text in issues:
        lines.append(f"{'🔴' if level == 'hard' else '⚠️'} {text}")
    for note in stats.notes:
        lines.append(f"ℹ️ *{note}*")

    embed = discord.Embed(
        title=truncate(f"🔍 Modellkoll – {filename}", 256),
        description=truncate("\n".join(lines), 4000),
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="🔺 Trianglar", value=f"{tri_icon} {fmt_int(stats.triangles)}", inline=True)
    embed.add_field(name="🔹 Vertices", value=fmt_int(stats.vertices) if stats.vertices is not None else "–", inline=True)
    embed.add_field(name="🎨 Material", value=mat_text, inline=True)
    embed.add_field(name="🗺️ UV", value=uv_text, inline=True)
    embed.add_field(name="📏 Storlek", value=size_text, inline=True)
    embed.add_field(name="🎯 Origin", value=origin_text, inline=True)
    embed.set_footer(text=f"Automatisk analys ({stats.fmt}) – ersätter inte en titt i Unity")
    return embed, hard


# ---- Körning i bakgrunden --------------------------------------------------- #
_BG_TASKS: set = set()


async def mark_needs_changes(thread: discord.Thread) -> None:
    starter = await thread.fetch_message(thread.id)
    if starter.embeds:
        embed = starter.embeds[0].copy()
        set_field(embed, FIELD_STATUS, f"🛠️ {STATUS_CHANGES}")
        embed.color = COLOR_CHANGES
        await starter.edit(embed=embed)
    await set_thread_status(thread, STATUS_CHANGES)


async def run_model_check(
    bot: "FileBot", thread: discord.Thread, filename: str, data: bytes | None,
    category: str, uploader: int,
) -> None:
    ext = ext_of(filename)
    try:
        if data is None and ext != ".blend":
            raise AnalysisError(
                f"Filen är för stor för automatisk modellkoll (gräns {ANALYZE_MAX_BYTES // 1024 // 1024} MB)."
            )
        stats = await asyncio.to_thread(analyze, data or b"", filename)
    except SkipAnalysis:
        return
    except AnalysisError as exc:
        embed = discord.Embed(
            title=truncate(f"🔍 Modellkoll – {filename}", 256),
            description=str(exc),
            color=discord.Color.light_grey(),
        )
        await thread.send(embed=embed)
        return
    except Exception:
        log.exception("Modellkollen kraschade för %s", filename)
        embed = discord.Embed(
            title=truncate(f"🔍 Modellkoll – {filename}", 256),
            description="Kunde inte analysera filen (okänt fel). Den är fortfarande uppladdad som vanligt.",
            color=discord.Color.light_grey(),
        )
        await thread.send(embed=embed)
        return

    embed, hard = build_report(stats, filename, category)
    mention = None
    allowed = discord.AllowedMentions.none()
    if hard:
        if AUTO_SET_STATUS:
            embed.description = truncate(
                (embed.description or "") + f"\n\n🛠️ Status sattes automatiskt till **{STATUS_CHANGES}**.", 4000
            )
            try:
                await mark_needs_changes(thread)
            except discord.HTTPException:
                log.exception("Kunde inte sätta status efter modellkoll")
        mention = f"<@{uploader}>"
        allowed = discord.AllowedMentions(users=[discord.Object(id=uploader)])

    await thread.send(content=mention, embed=embed, allowed_mentions=allowed)
    bot.request_refresh()


async def _guarded_model_check(*args) -> None:
    try:
        await run_model_check(*args)
    except Exception:
        log.exception("Modellkollen misslyckades")


def schedule_model_check(
    bot: "FileBot", thread: discord.Thread, c: "Collected", uploader: int, category: str
) -> None:
    if c.attachment is None or ext_of(c.attachment.filename) not in MODEL_CHECK_EXTENSIONS:
        return
    task = asyncio.create_task(
        _guarded_model_check(bot, thread, c.attachment.filename, c.data, category, uploader)
    )
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


# --------------------------------------------------------------------------- #
# Webbserver: hälsokontroll + 3D-viewer
# --------------------------------------------------------------------------- #
VIEWABLE_EXTENSIONS = {".glb", ".gltf", ".obj", ".stl", ".fbx"}
VIEWER_MAX_BYTES = 60 * 1024 * 1024  # Största fil som skickas till viewern
VIEWER_CACHE_BYTES = 150 * 1024 * 1024  # Minnescache så samma modell inte hämtas om och om igen

WEB_BOT: "FileBot | None" = None
_FILE_CACHE: "OrderedDict[tuple[int, int], bytes]" = OrderedDict()

VIEWER_HTML = r'''<!DOCTYPE html>
<html lang="sv">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
<title>3D-vy</title>
<style>
:root{--bg:#0f1115;--panel:rgba(22,25,32,.88);--text:#e8eaf0;--muted:#9aa3b2;--accent:#5865f2;--ok:#43b581;--warn:#faa61a}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;overflow:hidden;-webkit-tap-highlight-color:transparent}
#c{position:fixed;inset:0;width:100%;height:100%;display:block;touch-action:none;outline:none}
.top{position:fixed;left:0;right:0;top:0;display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:calc(env(safe-area-inset-top,0px) + 10px) 12px 10px;background:linear-gradient(to bottom,var(--panel),rgba(22,25,32,0));pointer-events:none}
.top>*{pointer-events:auto}
h1{font-size:17px;font-weight:700;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1 1 200px}
.chip{font-size:12px;padding:3px 9px;border-radius:99px;background:var(--panel);border:1px solid #2c313c;color:var(--muted);white-space:nowrap}
select,a.back{font:inherit;font-size:13px;color:var(--text);background:var(--panel);border:1px solid #2c313c;border-radius:10px;padding:7px 10px;text-decoration:none;max-width:70vw}
.info{position:fixed;left:12px;bottom:calc(env(safe-area-inset-bottom,0px) + 74px);background:var(--panel);border:1px solid #2c313c;border-radius:12px;padding:9px 12px;font-size:12.5px;line-height:1.55;color:var(--muted);backdrop-filter:blur(6px);display:none}
.info b{color:var(--text);font-weight:600}
.tools{position:fixed;left:0;right:0;bottom:0;display:flex;gap:8px;overflow-x:auto;padding:10px 12px calc(env(safe-area-inset-bottom,0px) + 12px);background:linear-gradient(to top,var(--panel),rgba(22,25,32,0));scrollbar-width:none}
.tools::-webkit-scrollbar{display:none}
button{font:inherit;font-size:14px;font-weight:600;color:var(--text);background:var(--panel);border:1px solid #2c313c;border-radius:12px;padding:11px 14px;white-space:nowrap;cursor:pointer;backdrop-filter:blur(6px)}
button.on{background:var(--accent);border-color:var(--accent)}
.overlay{position:fixed;inset:0;display:flex;flex-direction:column;gap:14px;align-items:center;justify-content:center;background:var(--bg);text-align:center;padding:24px}
.overlay.hide{display:none}
.bar{width:min(320px,70vw);height:8px;background:#232733;border-radius:99px;overflow:hidden}
.bar>i{display:block;height:100%;width:0;background:var(--accent);transition:width .15s}
.err{color:#ff7b7b;max-width:520px}
</style>
</head>
<body>
<canvas id="c"></canvas>
<div class="top">
  <h1 id="title">Laddar ...</h1>
  <span class="chip" id="cat" hidden></span>
  <span class="chip" id="status" hidden></span>
  <select id="ver" hidden aria-label="Version"></select>
  <a class="back" id="back" href="#" hidden>💬 Discord</a>
</div>
<div class="info" id="info"></div>
<div class="tools" id="tools">
  <button data-act="wire">▦ Wireframe</button>
  <button data-act="uv">🏁 UV-rutor</button>
  <button data-act="normals">🧭 Normaler</button>
  <button data-act="grid" class="on">⌗ Rutnät</button>
  <button data-act="spin">🔄 Snurra</button>
  <button data-act="axis">↕ Vänd upp</button>
  <button data-act="bg">🎨 Bakgrund</button>
  <button data-act="info" class="on">ℹ️ Info</button>
  <button data-act="reset">🎯 Återställ</button>
</div>
<div class="overlay" id="ov"><div id="ovmsg">Hämtar modell ...</div><div class="bar"><i id="prog"></i></div></div>

<script type="importmap">
{"imports":{"three":"https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js","three/addons/":"https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/"}}
</script>
<script type="module">
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OBJLoader } from 'three/addons/loaders/OBJLoader.js';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';
import { FBXLoader } from 'three/addons/loaders/FBXLoader.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';

const CFG = __CFG__;
const $ = s => document.querySelector(s);
const BGS = ['#0f1115', '#d9dde4', '#6b7280', '#101a2b'];

const canvas = $('#c');
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
renderer.toneMapping = THREE.ACESFilmicToneMapping;
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(45, 1, 0.01, 1000);
const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
controls.autoRotateSpeed = 2.0;
const pmrem = new THREE.PMREMGenerator(renderer);
scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
scene.add(new THREE.HemisphereLight(0xffffff, 0x444455, 0.5));
const sun = new THREE.DirectionalLight(0xffffff, 1.3);
sun.position.set(3, 6, 4);
scene.add(sun);

function resize() {
  const w = window.innerWidth, h = window.innerHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
window.addEventListener('resize', resize);
resize();

const state = { wire: false, mode: 'shaded', grid: true, spin: false, axis: false, bg: 0, info: true };
let model = null, gridHelper = null, firstLoad = true, lastStats = null, lastMeta = null;

function makeChecker() {
  const c = document.createElement('canvas');
  c.width = c.height = 512;
  const g = c.getContext('2d');
  const n = 16, s = 512 / n;
  for (let y = 0; y < n; y++) for (let x = 0; x < n; x++) {
    g.fillStyle = (x + y) % 2 ? '#fafafa' : '#3b82f6';
    g.fillRect(x * s, y * s, s, s);
  }
  const t = new THREE.CanvasTexture(c);
  t.wrapS = t.wrapT = THREE.RepeatWrapping;
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 8;
  return t;
}
const checkerMat = new THREE.MeshStandardMaterial({ map: makeChecker(), roughness: 0.8, metalness: 0 });
const normalMat = new THREE.MeshNormalMaterial();

function applyMode() {
  if (!model) return;
  checkerMat.wireframe = state.wire;
  normalMat.wireframe = state.wire;
  model.traverse(o => {
    if (!o.isMesh) return;
    if (!o.userData.orig) o.userData.orig = o.material;
    o.material = state.mode === 'uv' ? checkerMat : state.mode === 'normals' ? normalMat : o.userData.orig;
    (Array.isArray(o.material) ? o.material : [o.material]).forEach(m => { m.wireframe = state.wire; });
  });
}

function setBackground() {
  scene.background = new THREE.Color(BGS[state.bg]);
  document.body.style.background = BGS[state.bg];
}
setBackground();

function disposeModel(obj) {
  obj.traverse(o => {
    if (!o.isMesh) return;
    o.geometry && o.geometry.dispose();
    const orig = o.userData.orig || o.material;
    (Array.isArray(orig) ? orig : [orig]).forEach(m => {
      for (const k in m) { if (m[k] && m[k].isTexture) m[k].dispose(); }
      m.dispose && m.dispose();
    });
  });
}

function frame(obj, keepCamera) {
  const box = new THREE.Box3().setFromObject(obj);
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const maxDim = Math.max(size.x, size.y, size.z) || 1;
  if (gridHelper) { scene.remove(gridHelper); gridHelper.geometry.dispose(); }
  gridHelper = new THREE.GridHelper(maxDim * 3, 30, 0x5865f2, 0x3a3f4b);
  gridHelper.position.set(center.x, box.min.y, center.z);
  gridHelper.visible = state.grid;
  scene.add(gridHelper);
  if (!keepCamera) {
    const dist = (maxDim / (2 * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)))) * 1.9;
    camera.position.copy(center).add(new THREE.Vector3(1, 0.65, 1).normalize().multiplyScalar(dist));
    controls.target.copy(center);
    camera.near = maxDim / 500;
    camera.far = maxDim * 200;
    camera.updateProjectionMatrix();
    controls.update();
    controls.saveState();
  }
  return { size, maxDim };
}

function collectStats(obj, ext) {
  let tris = 0, verts = 0, meshes = 0;
  const mats = new Set();
  obj.traverse(o => {
    if (!o.isMesh) return;
    meshes++;
    const pos = o.geometry.attributes.position;
    if (!pos) return;
    verts += pos.count;
    tris += o.geometry.index ? o.geometry.index.count / 3 : pos.count / 3;
    (Array.isArray(o.material) ? o.material : [o.material]).forEach(m => mats.add(m.uuid));
  });
  const box = new THREE.Box3().setFromObject(obj);
  const s = box.getSize(new THREE.Vector3());
  const unit = (ext === 'glb' || ext === 'gltf') ? 'm' : 'enh.';
  return { tris, verts, meshes, mats: mats.size, dims: [s.x, s.y, s.z], unit };
}

const nf = n => Math.round(n).toLocaleString('sv-SE');
function renderInfo() {
  const el = $('#info');
  if (!lastStats || !state.info) { el.style.display = 'none'; return; }
  const s = lastStats;
  el.innerHTML =
    '<b>' + nf(s.tris) + '</b> trianglar · <b>' + nf(s.verts) + '</b> vertices<br>' +
    '<b>' + s.meshes + '</b> meshar · <b>' + s.mats + '</b> material<br>' +
    'Storlek <b>' + s.dims.map(d => d.toFixed(2)).join(' × ') + '</b> ' + s.unit +
    (lastMeta ? '<br><span>' + lastMeta + '</span>' : '');
  el.style.display = 'block';
}

async function loadBuffer(url, onprog) {
  const r = await fetch(url);
  if (!r.ok) throw new Error((await r.text()) || ('HTTP ' + r.status));
  const total = Number(r.headers.get('Content-Length')) || 0;
  const reader = r.body.getReader();
  const chunks = [];
  let got = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    got += value.length;
    onprog(total ? got / total : null);
  }
  const buf = new Uint8Array(got);
  let p = 0;
  for (const c of chunks) { buf.set(c, p); p += c.length; }
  return buf.buffer;
}

function parseModel(buf, ext) {
  return new Promise((resolve, reject) => {
    try {
      if (ext === 'glb' || ext === 'gltf') {
        const data = ext === 'gltf' ? new TextDecoder().decode(buf) : buf;
        new GLTFLoader().parse(data, '', g => resolve(g.scene), reject);
      } else if (ext === 'obj') {
        const g = new OBJLoader().parse(new TextDecoder().decode(buf));
        g.traverse(o => { if (o.isMesh) o.material = new THREE.MeshStandardMaterial({ color: 0xcfd3da, roughness: 0.7 }); });
        resolve(g);
      } else if (ext === 'stl') {
        const geo = new STLLoader().parse(buf);
        const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: 0xb8c0cc, roughness: 0.6, metalness: 0.1 }));
        const g = new THREE.Group();
        g.add(mesh);
        g.rotation.x = -Math.PI / 2;
        resolve(g);
      } else if (ext === 'fbx') {
        resolve(new FBXLoader().parse(buf, ''));
      } else {
        reject(new Error('Filtypen stöds inte i 3D-vyn.'));
      }
    } catch (e) { reject(e); }
  });
}

function showOverlay(msg, isErr) {
  $('#ov').classList.remove('hide');
  $('#ovmsg').textContent = msg;
  $('#ovmsg').className = isErr ? 'err' : '';
  document.querySelector('.bar').style.display = isErr ? 'none' : 'block';
}

async function show(version) {
  showOverlay('Hämtar ' + version.filename + ' ...');
  $('#prog').style.width = '0';
  try {
    const buf = await loadBuffer(CFG.file + version.mid + '/' + version.idx, p => {
      $('#prog').style.width = (p == null ? 50 : Math.round(p * 100)) + '%';
    });
    $('#ovmsg').textContent = 'Bygger modellen ...';
    const obj = await parseModel(buf, version.ext);
    if (model) { scene.remove(model); disposeModel(model); }
    model = obj;
    model.userData.baseRotX = model.rotation.x;
    scene.add(model);
    model.rotation.x = model.userData.baseRotX + (state.axis ? -Math.PI / 2 : 0);
    model.updateMatrixWorld(true);
    lastStats = collectStats(model, version.ext);
    lastMeta = version.label + ' · ' + version.filename;
    applyMode();
    frame(model, !firstLoad);
    firstLoad = false;
    renderInfo();
    $('#ov').classList.add('hide');
  } catch (e) {
    console.error(e);
    showOverlay('Kunde inte visa modellen: ' + (e && e.message ? e.message : e), true);
  }
}

function setActive(act, on) {
  const b = document.querySelector('[data-act="' + act + '"]');
  if (b) b.classList.toggle('on', !!on);
}

$('#tools').addEventListener('click', e => {
  const b = e.target.closest('button');
  if (!b) return;
  const act = b.dataset.act;
  if (act === 'wire') { state.wire = !state.wire; setActive('wire', state.wire); applyMode(); }
  else if (act === 'uv' || act === 'normals') {
    state.mode = state.mode === act ? 'shaded' : act;
    setActive('uv', state.mode === 'uv');
    setActive('normals', state.mode === 'normals');
    applyMode();
  }
  else if (act === 'grid') { state.grid = !state.grid; setActive('grid', state.grid); if (gridHelper) gridHelper.visible = state.grid; }
  else if (act === 'spin') { state.spin = !state.spin; setActive('spin', state.spin); controls.autoRotate = state.spin; }
  else if (act === 'axis' && model) {
    state.axis = !state.axis;
    setActive('axis', state.axis);
    model.rotation.x = model.userData.baseRotX + (state.axis ? -Math.PI / 2 : 0);
    model.updateMatrixWorld(true);
    frame(model, false);
  }
  else if (act === 'bg') { state.bg = (state.bg + 1) % BGS.length; setBackground(); }
  else if (act === 'info') { state.info = !state.info; setActive('info', state.info); renderInfo(); }
  else if (act === 'reset') { controls.reset(); }
});

renderer.setAnimationLoop(() => { controls.update(); renderer.render(scene, camera); });

(async () => {
  try {
    const r = await fetch(CFG.api);
    if (!r.ok) throw new Error((await r.text()) || ('HTTP ' + r.status));
    const meta = await r.json();
    document.title = '3D-vy – ' + meta.title;
    $('#title').textContent = meta.title;
    if (meta.category) { $('#cat').textContent = '📂 ' + meta.category; $('#cat').hidden = false; }
    if (meta.status) { $('#status').textContent = meta.status; $('#status').hidden = false; }
    if (meta.discord_url) { $('#back').href = meta.discord_url; $('#back').hidden = false; }
    const versions = meta.versions || [];
    if (!versions.length) { showOverlay('Hittade ingen 3D-fil i den här tråden (stöds: glb, gltf, obj, stl, fbx).', true); return; }
    const sel = $('#ver');
    versions.forEach((v, i) => {
      const o = document.createElement('option');
      o.value = String(i);
      o.textContent = v.label + (i === 0 ? ' (senaste)' : '') + ' · ' + v.filename + ' · ' + (v.size / 1048576).toFixed(1) + ' MB';
      sel.appendChild(o);
    });
    sel.hidden = versions.length < 2;
    let start = 0;
    if (CFG.m) { const i = versions.findIndex(v => v.mid === CFG.m); if (i >= 0) start = i; }
    sel.value = String(start);
    sel.addEventListener('change', () => show(versions[Number(sel.value)]));
    await show(versions[start]);
  } catch (e) {
    console.error(e);
    showOverlay('Kunde inte öppna 3D-vyn: ' + (e && e.message ? e.message : e), true);
  }
})();
</script>
</body>
</html>
'''


class WebError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def is_viewable(filename: str) -> bool:
    return ext_of(filename) in VIEWABLE_EXTENSIONS


def _viewer_key() -> bytes:
    # VIEWER_SECRET om den finns, annars härleds nyckeln från bottens token
    secret = os.environ.get("VIEWER_SECRET") or os.environ.get("DISCORD_TOKEN") or "dev"
    return hashlib.sha256(secret.encode()).digest()


def viewer_sig(thread_id: int) -> str:
    return hmac.new(_viewer_key(), str(thread_id).encode(), hashlib.sha256).hexdigest()[:32]


def viewer_base_url() -> str | None:
    url = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
    if not url:
        domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
        if domain:
            url = f"https://{domain}"
    return url or None


def viewer_url(thread_id: int, message_id: int | None = None) -> str | None:
    base = viewer_base_url()
    if base is None:
        return None
    url = f"{base}/v/{thread_id}/{viewer_sig(thread_id)}"
    if message_id:
        url += f"?m={message_id}"
    return url


def make_viewer_view(url: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Öppna 3D-vy", emoji="🔎", style=discord.ButtonStyle.link, url=url))
    return view


async def viewer_thread(bot: "FileBot", thread_id: int) -> discord.Thread:
    channel = bot.get_channel(thread_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(thread_id)
        except discord.HTTPException:
            raise WebError(404, "Hittar inte tråden.")
    if not isinstance(channel, discord.Thread) or channel.parent_id != FORUM_CHANNEL_ID:
        raise WebError(404, "Hittar inte tråden.")
    return channel


async def viewer_api(bot: "FileBot", thread_id: int) -> dict:
    thread = await viewer_thread(bot, thread_id)
    title, category, status = thread.name, "", ""
    try:
        starter = await thread.fetch_message(thread.id)
        if starter.embeds:
            embed = starter.embeds[0]
            title = embed.title or title
            category = next((f.value for f in embed.fields if f.name == "📂 Kategori"), "")
            status = next((f.value for f in embed.fields if f.name == FIELD_STATUS), "")
    except discord.HTTPException:
        pass

    versions = []
    async for msg in thread.history(limit=200):  # Nyast först
        if msg.author.id != bot.user.id:
            continue
        for idx, att in enumerate(msg.attachments):
            if not is_viewable(att.filename):
                continue
            if msg.id == thread.id:
                label = "v1"
            else:
                m = re.search(r"v(\d+)", msg.embeds[0].title or "") if msg.embeds else None
                label = f"v{m.group(1)}" if m else "v?"
            versions.append({
                "mid": str(msg.id),
                "idx": idx,
                "label": label,
                "filename": att.filename,
                "size": att.size,
                "ext": ext_of(att.filename).lstrip("."),
            })
    return {
        "title": title,
        "category": category,
        "status": status,
        "discord_url": thread.jump_url,
        "versions": versions,
    }


async def viewer_file(bot: "FileBot", thread_id: int, message_id: int, idx: int) -> tuple[bytes, str]:
    thread = await viewer_thread(bot, thread_id)
    key = (message_id, idx)
    try:
        msg = await thread.fetch_message(message_id)
    except discord.HTTPException:
        raise WebError(404, "Hittar inte filen.")
    if msg.author.id != bot.user.id or idx >= len(msg.attachments):
        raise WebError(404, "Hittar inte filen.")
    att = msg.attachments[idx]
    if not is_viewable(att.filename):
        raise WebError(415, "Filtypen stöds inte i 3D-vyn.")
    if att.size > VIEWER_MAX_BYTES:
        raise WebError(413, f"Filen är för stor för 3D-vyn (max {VIEWER_MAX_BYTES // 1024 // 1024} MB).")

    if key in _FILE_CACHE:
        _FILE_CACHE.move_to_end(key)
        return _FILE_CACHE[key], att.filename

    data = await att.read()
    _FILE_CACHE[key] = data
    total = sum(len(v) for v in _FILE_CACHE.values())
    while total > VIEWER_CACHE_BYTES and len(_FILE_CACHE) > 1:
        _, old = _FILE_CACHE.popitem(last=False)
        total -= len(old)
    return data, att.filename


def run_on_bot(coro, timeout: float = 45):
    """Kör en coroutine i botens event loop från webbtråden."""
    bot = WEB_BOT
    if bot is None or not bot.is_ready():
        coro.close()
        raise WebError(503, "Boten startar – försök igen om en stund.")
    return asyncio.run_coroutine_threadsafe(coro, bot.loop).result(timeout=timeout)


class WebHandler(BaseHTTPRequestHandler):
    server_version = "FileBot"

    def _send(self, code: int, body: bytes, ctype: str = "text/plain; charset=utf-8", headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Robots-Tag", "noindex")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path in ("/", "/health", "/healthz"):
                self._send(200, b"Bot is alive!")
                return

            m = re.fullmatch(r"/v/(\d{1,20})/([0-9a-f]{32})", path)
            if m:
                self._check_sig(m)
                query = parse_qs(parsed.query)
                focus = query.get("m", [""])[0]
                cfg = {
                    "api": f"/api/{m[1]}/{m[2]}",
                    "file": f"/file/{m[1]}/{m[2]}/",
                    "m": focus if focus.isdigit() else None,
                }
                html = VIEWER_HTML.replace("__CFG__", json.dumps(cfg))
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", {"Cache-Control": "no-store"})
                return

            m = re.fullmatch(r"/api/(\d{1,20})/([0-9a-f]{32})", path)
            if m:
                self._check_sig(m)
                data = run_on_bot(viewer_api(WEB_BOT, int(m[1])))
                self._send(200, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8", {"Cache-Control": "no-store"})
                return

            m = re.fullmatch(r"/file/(\d{1,20})/([0-9a-f]{32})/(\d{1,20})/(\d{1,2})", path)
            if m:
                self._check_sig(m)
                data, filename = run_on_bot(viewer_file(WEB_BOT, int(m[1]), int(m[3]), int(m[4])))
                self._send(200, data, "application/octet-stream", {"Cache-Control": "private, max-age=600"})
                return

            self._send(404, b"Not found")
        except WebError as exc:
            self._send(exc.code, exc.message.encode("utf-8"))
        except concurrent.futures.TimeoutError:
            self._send(504, "Det tog för lång tid – försök igen.".encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception("Fel i webbservern för %s", self.path)
            self._send(500, b"Internal error")

    @staticmethod
    def _check_sig(m: "re.Match") -> None:
        if not hmac.compare_digest(viewer_sig(int(m[1])), m[2]):
            raise WebError(403, "Ogiltig länk.")

    def log_message(self, format, *args):  # Tysta HTTP-loggar
        return


def start_http_server() -> None:
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(("0.0.0.0", port), WebHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("Webbserver startad på port %s", port)
    if viewer_base_url():
        log.info("3D-vyn är aktiv på %s", viewer_base_url())
    else:
        log.warning("3D-vyn är avstängd: sätt PUBLIC_URL eller generera en publik domän i Railway.")


# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #
class FileBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # Krävs för !setup och för att läsa bilagor
        super().__init__(command_prefix="!", intents=intents)
        self._panels_checked = False
        self._dashboard_message: discord.Message | None = None
        self._refresh_task: asyncio.Task | None = None
        self._refresh_pending = False

    async def setup_hook(self) -> None:
        log.info(
            "Uppladdning direkt i formulär (fil i modal): %s",
            "PÅ" if MODAL_UPLOAD else "AV – uppdatera discord.py för att slå på den",
        )
        # Registrera permanenta knappar så de fungerar efter omstart
        self.add_view(UploadView())
        self.add_view(ThreadView())
        self.add_view(TodoPanelView())
        self.add_view(TaskView())
        await self.add_cog(SetupCog(self))
        await self.add_cog(HQCog(self))
        await self.tree.sync()

    # -- Dashboard-uppdatering med fördröjning (slår ihop många ändringar) -- #
    def request_refresh(self) -> None:
        self._refresh_pending = True
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._refresh_worker())

    async def _refresh_worker(self) -> None:
        while True:
            await asyncio.sleep(3)
            self._refresh_pending = False
            try:
                await update_dashboard(self)
            except Exception:
                log.exception("Dashboard-uppdatering misslyckades")
            if not self._refresh_pending:
                break

    # -- Forum-händelser som påverkar dashboarden -- #
    async def on_thread_create(self, thread: discord.Thread) -> None:
        if thread.parent_id == FORUM_CHANNEL_ID:
            self.request_refresh()

    async def on_raw_thread_update(self, payload: discord.RawThreadUpdateEvent) -> None:
        if payload.parent_id == FORUM_CHANNEL_ID:
            self.request_refresh()

    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        if payload.parent_id == FORUM_CHANNEL_ID:
            self.request_refresh()

    async def on_ready(self) -> None:
        log.info("Inloggad som %s (ID: %s)", self.user, self.user.id)

        # Kör bara första gången (on_ready kan triggas flera gånger)
        if self._panels_checked:
            return
        self._panels_checked = True

        panels = [
            ("UPLOAD_CHANNEL_ID", PANEL_TITLE, build_panel_embed, UploadView),
            ("TODO_CHANNEL_ID", TODO_PANEL_TITLE, build_todo_panel_embed, TodoPanelView),
        ]
        for env_name, title, build_embed, build_view in panels:
            channel_id = env_int(env_name)
            if channel_id is None:
                continue
            try:
                await ensure_panel(self, channel_id, title, build_embed, build_view)
            except Exception:
                log.exception("Kunde inte säkerställa panelen för %s", env_name)


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

    global WEB_BOT
    bot = FileBot()
    WEB_BOT = bot
    start_http_server()
    bot.run(token)


if __name__ == "__main__":
    main()
