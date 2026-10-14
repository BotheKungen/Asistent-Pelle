import discord
from discord.ext import commands
from discord import app_commands

# Byt ut detta mot ditt bot-token från Developer Portal
TOKEN = "DIN_BOT_TOKEN_HÄR"

# Byt ut detta mot ID:t på kanalen där filerna ska hamna (t.ex. er modeller/resurser-kanal)
TARGET_CHANNEL_ID = 123456789012345678  

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 1. Formuläret (Modal) som öppnas när man klickar på knappen
class UploadModal(discord.ui.Modal, title="Ladda upp asset"):
    asset_name = discord.ui.TextInput(
        label="Namn på asset",
        placeholder="T.ex. Volvo 740 Highpoly...",
        required=True
    )
    description = discord.ui.TextInput(
        label="Beskrivning / Ändringar",
        style=discord.TextStyle.long,
        placeholder="T.ex. Fixat UV-maps och lagt till högupplösta textures...",
        required=False
    )

    async def on_submit(self, interaction: discord.Interaction):
        # Svara användaren privat att det är dags att skicka filen
        await interaction.response.send_message(
            f"👍 **{self.asset_name.value}** registrerad!\n"
            f"Bifoga din fil (`.blend`, `.fbx`, `.zip` etc.) som ett vanligt meddelande här nedanför inom 2 minuter.",
            ephemeral=True
        )

        def check(m):
            return m.author.id == interaction.user.id and len(m.attachments) > 0 and m.channel.id == interaction.channel_id

        try:
            # Vänta på att användaren skickar ett meddelande med en fil
            msg = await bot.wait_for('message', check=check, timeout=120.0)
            attachment = msg.attachments[0]

            # Hämta målkanalen
            target_channel = bot.get_channel(TARGET_CHANNEL_ID)
            if not target_channel:
                await interaction.followup.send("Kunde inte hitta målkanalen. Kontrollera TARGET_CHANNEL_ID.", ephemeral=True)
                return

            # Bygg ett snyggt Embed-kort
            embed = discord.Embed(
                title=f"📦 Ny fil uppladdad: {self.asset_name.value}",
                description=self.description.value if self.description.value else "Ingen beskrivning angiven.",
                color=discord.Color.blue()
            )
            embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
            embed.add_field(name="Filnamn", value=attachment.filename, inline=True)
            embed.add_field(name="Storlek", value=f"{round(attachment.size / (1024*1024), 2)} MB", inline=True)

            # Ladda ner och skicka filen vidare till målkanalen med ping
            file_data = await attachment.to_file()
            await target_channel.send(
                content="@everyone En ny fil har laddats upp!", 
                embed=embed, 
                file=file_data
            )

            # Ta bort användarens ursprungliga meddelande så kanalen hålls ren
            try:
                await msg.delete()
            except:
                pass

            await interaction.followup.send("✅ Filen har skickats till målkanalen!", ephemeral=True)

        except TimeoutError:
            await interaction.followup.send("⏰ Tiden gick ut. Klicka på knappen igen när du är redo att ladda upp.", ephemeral=True)

# 2. Knappen "Ladda upp"
class UploadView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Ladda upp fil", style=discord.ButtonStyle.primary, custom_id="upload_button", emoji="📤")
    async def upload_button_callback(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.send_modal(UploadModal())

# 3. Slash-kommando för att placera ut knappen i valfri kanal
@bot.tree.command(name="setup_laddaupp", description="Skickar knappen för filuppladdning i den här kanalen")
async def setup(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Filuppladdning",
        description="Klicka på knappen nedan för att ladda upp `.blend`, `.fbx` eller andra projektfiler.",
        color=discord.Color.blurple()
    )
    await interaction.channel.send(embed=embed, view=UploadView())
    await interaction.response.send_message("Knapp skapad!", ephemeral=True)

@bot.event
async def on_ready():
    await bot.tree.sync()
    bot.add_view(UploadView())
    print(f"Boten är inloggad som {bot.user}")

bot.run(TOKEN)
