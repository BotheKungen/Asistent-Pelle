import os
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import discord
from discord.ext import commands

# 1. Webbserver för Railway
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
    asset_name = discord.ui.TextInput(label="Titel / Namn", placeholder="Volvo 740...", required=True)
    category = discord.ui.TextInput(label="Kategori / Tagg", placeholder="Bilar / Bildelar...", default="Bilar", required=True)
    description = discord.ui.TextInput(label="Beskrivning", style=discord.TextStyle.long, required=False)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"👍 **{self.asset_name.value}** registreras...\nBifoga din fil i chatten nu!", ephemeral=True)
        def check(m): return m.author.id == interaction.user.id and len(m.attachments) > 0 and m.channel.id == interaction.channel_id
        
        try:
            msg = await bot.wait_for('message', check=check, timeout=120.0)
            attachment = msg.attachments[0]
            forum = bot.get_channel(FORUM_CHANNEL_ID)
            
            tags = [t for t in forum.available_tags if self.category.value.lower() in t.name.lower()]
            
            embed = discord.Embed(title=f"📦 {self.asset_name.value}", description=self.description.value, color=discord.Color.blue())
            embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
            embed.add_field(name="Kategori", value=self.category.value)
            embed.add_field(name="Filnamn", value=attachment.filename)
            
            file_data = await attachment.to_file()
            thread = await forum.create_thread(name=f"[{self.category.value}] {self.asset_name.value}", content="@everyone Ny fil!", embed=embed, file=file_data, applied_tags=tags)
            
            try: await msg.delete()
            except: pass
            
            await interaction.followup.send(f"✅ Klar: {thread.thread.jump_url}", ephemeral=True)
        except asyncio.TimeoutError:
            await interaction.followup.send("⏰ Tiden gick ut.", ephemeral=True)

class UploadView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Ladda upp fil", style=discord.ButtonStyle.primary, custom_id="upload_btn_v3", emoji="📤")
    async def upload_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(UploadModal())

@bot.tree.command(name="setup_laddaupp", description="Skapa uppladdningsknappen")
async def setup(interaction: discord.Interaction):
    embed = discord.Embed(title="Filuppladdning", description="Klicka nedan för att ladda upp till forumet.", color=discord.Color.blurple())
    await interaction.channel.send(embed=embed, view=UploadView())
    await interaction.response.send_message("Knapp skapad!", ephemeral=True)

@bot.event
async def on_ready():
    await bot.tree.sync()
    bot.add_view(UploadView())
    print(f"Boten är inloggad som {bot.user}")

bot.run(TOKEN)
