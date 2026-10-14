import os
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import discord
from discord.ext import commands

# 1. Webbserver för hälso-check
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

def run_health_check_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=run_health_check_server, daemon=True).start()

# 2. Setup
TOKEN = os.getenv("TOKEN")
FORUM_CHANNEL_ID = 1559907105279053926

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

class UploadModal(discord.ui.Modal, title="Ladda upp fil till Forum"):
    asset_name = discord.ui.TextInput(
        label="Titel / Namn på asset",
        placeholder="T.ex. Volvo 740 Highpoly...",
        required=True
    )
    category = discord.ui.TextInput(
        label="Kategori / Tagg",
        placeholder="Bilar / Bildelar / Karaktärer / Övrigt",
        default="Bilar",
        required=True
    )
    description = discord.ui.TextInput(
        label="Beskrivning / Ändringar",
        style=discord.TextStyle.long,
        placeholder="T.ex. Fixat UV-maps, riggat chassi...",
        required=False
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            f"👍 **{self.asset_name.value}** ({self.category.value}) registrerad!\n"
            f"Bifoga din fil (`.blend`, `.fbx`, `.zip` etc.) som ett vanligt meddelande i den här chatten inom 2 minuter.",
            ephemeral=True
        )

        def check(m):
            return m.author.id == interaction.user.id and len(m.attachments) > 0 and m.channel.id == interaction.channel_id

        try:
            msg = await bot.wait_for('message', check=check, timeout=120.0)
            attachment = msg.attachments[0]

            forum_channel = bot.get_channel(FORUM_CHANNEL_ID)
            if not forum_channel or not isinstance(forum_channel, discord.ForumChannel):
                await interaction.followup.send("Kunde inte hitta forumkanalen. Kontrollera att kanalen är ett Forum.", ephemeral=True)
                return

            applied_tags = []
            for tag in forum_channel.available_tags:
                if tag.name.lower() in self.category.value.lower() or self.category.value.lower() in tag.name.lower():
                    applied_tags.append(tag)
                    break

            embed = discord.Embed(
                title=f"📦 {self.asset_name.value}",
                description=self.description.value if self.description.value else "Ingen beskrivning angiven.",
                color=discord.Color.blue()
            )
            embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
            embed.add_field(name="Kategori", value=self.category.value, inline=True)
            embed.add_field(name="Filnamn", value=attachment.filename, inline=True)
            embed.add_field(name="Storlek", value=f"{round(attachment.size / (1024*1024), 2)} MB", inline=True)

            file_data = await attachment.to_file()

            thread_with_message = await forum_channel.create_thread(
                name=f"[{self.category.value}] {self.asset_name.value}",
                content="@everyone Ny fil uppladdad!",
                embed=embed,
                file=file_data,
                applied_tags=applied_tags
            )

            try:
                await msg.delete()
            except:
                pass

            await interaction.followup.send(f"✅ Inlägget har skapats i forumet: {thread_with_message.thread.jump_url}", ephemeral=True)

        except asyncio.TimeoutError:
            await interaction.followup.send("⏰ Tiden gick ut. Klicka på knappen igen när du vill ladda upp.", ephemeral=True)

class UploadView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Ladda upp fil", style=discord.ButtonStyle.primary, custom_id="upload_button_final", emoji="📤")
    async def upload_button_callback(self, arg1, arg2):
        # Skottsäker lösning som hittar interaktionen oavsett ordning
        interaction = arg1 if hasattr(arg1, 'response') else arg2
        await interaction.response.send_modal(UploadModal())
