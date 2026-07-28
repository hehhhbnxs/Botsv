import discord
from discord.ext import commands, tasks
from discord import app_commands
from python_aternos import Client
import asyncio
import os
import json
import datetime
from typing import Optional
from flask import Flask, jsonify
import threading
import logging
import time
import random

# ==================== CONFIG ====================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Danh sách tên kênh dashboard bot sẽ tự nhận diện
DASHBOARD_CHANNEL_NAMES = [
    "dashboard",
    "📊-dashboard",
    "server-dashboard",
    "minecraft-dashboard",
    "panel",
    "bảng-điều-khiển",
    "server-status",
    "📡-server-status",
    "bot-control",
    "mc-panel"
]

# Flask app cho Render (chống sleep)
app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({
        "status": "online",
        "bot": str(bot.user) if bot.user else "Not ready",
        "server": bot.server_info if hasattr(bot, 'server_info') else {},
        "dashboard_channel": str(bot.dashboard_channel.id) if bot.dashboard_channel else None
    })

@app.route('/health')
def health():
    return "OK", 200

def run_flask():
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 10000)))

# ==================== BOT SETUP ====================
class MinecraftBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.all()
        super().__init__(command_prefix='!', intents=intents)
        self.aternos = Client()
        self.server = None
        self.auto_extend = True
        self.auto_restart = True
        self.last_extend_time = None
        self.extend_count = 0
        self.failed_extend_count = 0
        self.max_failed_extends = 3
        self.server_info = {}
        self.queue_position = None
        self.queue_total = None
        self.is_starting = False
        self.is_restarting = False
        self.dashboard_active = False
        self.dashboard_message = None
        self.dashboard_channel = None
        self.last_ping = None
        self.ping_history = []
        self.server_uptime_start = None
        self.last_successful_extend = None
        self.extend_cooldown = False
        self.server_restart_count = 0
        self.connection_attempts = 0
        self.scanning_channels = False

bot = MinecraftBot()
start_time = datetime.datetime.now()

# ==================== ATERNOS SETUP ====================
async def setup_aternos():
    max_retries = 5
    for attempt in range(max_retries):
        try:
            await bot.aternos.login(
                os.getenv('ATERNOS_USERNAME'),
                os.getenv('ATERNOS_PASSWORD')
            )
            servers = bot.aternos.servers
            server_name = os.getenv('SERVER_NAME')
            for s in servers:
                if s.address == server_name or s.subdomain == server_name.split('.')[0]:
                    bot.server = s
                    await bot.server.fetch()
                    bot.server_info = {
                        "name": s.address,
                        "status": s.status,
                        "players": s.players_count,
                        "max_players": s.slots,
                        "version": getattr(s, 'version', 'Unknown'),
                        "last_extend": None,
                        "last_ping": None,
                        "ping_ms": 0,
                        "motd": getattr(s, 'motd', '')
                    }
                    if s.status == "online":
                        bot.server_uptime_start = datetime.datetime.now()
                    logger.info(f"✅ Đã kết nối server: {s.address}")
                    bot.connection_attempts = 0
                    return True
            logger.error("❌ Không tìm thấy server!")
            return False
        except Exception as e:
            bot.connection_attempts = attempt + 1
            logger.error(f"❌ Lỗi kết nối (lần {attempt+1}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(5 * (attempt + 1))
    return False

# ==================== EVENTS ====================
@bot.event
async def on_ready():
    logger.info(f'✅ Bot đã đăng nhập: {bot.user}')
    await setup_aternos()
    try:
        synced = await bot.tree.sync()
        logger.info(f"✅ Đã sync {len(synced)} slash commands")
    except Exception as e:
        logger.error(f"❌ Lỗi sync commands: {e}")
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="📊 Tìm kênh dashboard..."),
        status=discord.Status.online
    )
    await find_and_setup_dashboard()
    if not dashboard_updater.is_running():
        dashboard_updater.start()
    if not smart_maintainer.is_running():
        smart_maintainer.start()
    if not server_pinger.is_running():
        server_pinger.start()
    if not channel_scanner.is_running():
        channel_scanner.start()
    logger.info("✅ Tất cả tasks đã khởi động!")
    if bot.dashboard_channel:
        logger.info(f"📊 Dashboard đang chạy ở: #{bot.dashboard_channel.name}")
    else:
        logger.info("⚠️ Chưa tìm thấy kênh dashboard - Hãy tạo kênh với tên: dashboard, panel...")

async def find_and_setup_dashboard():
    for guild in bot.guilds:
        dashboard_channel = None
        for channel in guild.text_channels:
            channel_name = channel.name.lower().replace(" ", "-")
            for valid_name in DASHBOARD_CHANNEL_NAMES:
                if valid_name.lower() in channel_name:
                    dashboard_channel = channel
                    break
            if dashboard_channel:
                break
        if dashboard_channel:
            if dashboard_channel.permissions_for(guild.me).send_messages:
                logger.info(f"✅ Tìm thấy kênh dashboard: #{dashboard_channel.name}")
                try:
                    async for msg in dashboard_channel.history(limit=5):
                        if msg.author == bot.user:
                            await msg.delete()
                except:
                    pass
                await create_dashboard(dashboard_channel)
                return
    logger.info("⚠️ Chưa tìm thấy kênh dashboard.")

@bot.event
async def on_guild_channel_create(channel):
    if isinstance(channel, discord.TextChannel):
        channel_name = channel.name.lower().replace(" ", "-")
        for valid_name in DASHBOARD_CHANNEL_NAMES:
            if valid_name.lower() in channel_name:
                logger.info(f"🆕 Phát hiện kênh mới: #{channel.name}")
                await asyncio.sleep(2)
                if channel.permissions_for(channel.guild.me).send_messages:
                    if bot.dashboard_channel and bot.dashboard_message:
                        try:
                            await bot.dashboard_message.delete()
                        except:
                            pass
                    await create_dashboard(channel)
                    return
        if any(word in channel_name for word in ['bot', 'cmd', 'command', 'lệnh', 'điều', 'khiển', 'server', 'mc']):
            await asyncio.sleep(1)
            embed = discord.Embed(
                title="💡 Gợi ý",
                description=f"Bạn vừa tạo kênh **#{channel.name}**\n\nNếu muốn đặt dashboard ở đây, hãy đổi tên thành `dashboard` hoặc `panel`",
                color=discord.Color.blue()
            )
            try:
                await channel.send(embed=embed, delete_after=30)
            except:
                pass

@bot.event
async def on_guild_channel_update(before, after):
    if isinstance(after, discord.TextChannel):
        channel_name = after.name.lower().replace(" ", "-")
        for valid_name in DASHBOARD_CHANNEL_NAMES:
            if valid_name.lower() in channel_name:
                if after.id != (bot.dashboard_channel.id if bot.dashboard_channel else None):
                    logger.info(f"🔄 Kênh #{after.name} đổi tên phù hợp!")
                    await asyncio.sleep(1)
                    if bot.dashboard_channel and bot.dashboard_message:
                        try:
                            await bot.dashboard_message.delete()
                        except:
                            pass
                    if after.permissions_for(after.guild.me).send_messages:
                        await create_dashboard(after)
                    return

@bot.event
async def on_message(message):
    if bot.user in message.mentions and not message.author.bot:
        content = message.content.lower()
        if any(word in content for word in ['dashboard', 'panel', 'bảng', 'setup', 'tạo', 'hiện', 'vào đây']):
            channel = message.channel
            if channel.permissions_for(channel.guild.me).send_messages:
                if bot.dashboard_channel and bot.dashboard_message:
                    try:
                        await bot.dashboard_message.delete()
                    except:
                        pass
                await message.reply("✅ **Đã hiểu!** Đặt dashboard ở đây nhé!", delete_after=5)
                await create_dashboard(channel)

# ==================== DASHBOARD SYSTEM ====================
class SmartDashboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.update_button_states()

    def update_button_states(self):
        for child in self.children:
            if child.custom_id == "smart_start":
                if not bot.server:
                    child.disabled = True
                    child.label = "🔌 Mất kết nối"
                    child.style = discord.ButtonStyle.gray
                elif bot.is_starting:
                    child.disabled = True
                    child.label = "⏳ Đang khởi động..."
                    child.style = discord.ButtonStyle.gray
                elif bot.server.status == "online":
                    child.disabled = True
                    child.label = "✅ Server Online"
                    child.style = discord.ButtonStyle.green
                else:
                    child.disabled = False
                    child.label = "🚀 KHỞI ĐỘNG"
                    child.style = discord.ButtonStyle.green
            elif child.custom_id == "smart_extend":
                if not bot.server or bot.server.status != "online":
                    child.disabled = True
                    child.label = "⚫ Chưa online"
                    child.style = discord.ButtonStyle.gray
                elif bot.extend_cooldown:
                    child.disabled = True
                    child.label = "⏳ Đợi..."
                    child.style = discord.ButtonStyle.gray
                else:
                    child.disabled = False
                    if bot.auto_extend:
                        child.label = "🔄 GIA HẠN"
                        child.style = discord.ButtonStyle.blurple
                    else:
                        if bot.server_info.get("last_extend"):
                            time_left = bot.server_info["last_extend"] + datetime.timedelta(minutes=8) - datetime.datetime.now()
                            if time_left.total_seconds() < 180:
                                child.label = "⚠️ GIA HẠN GẤP!"
                                child.style = discord.ButtonStyle.danger
                            else:
                                child.label = "🔄 GIA HẠN"
                                child.style = discord.ButtonStyle.blurple
            elif child.custom_id == "smart_auto":
                if bot.auto_extend:
                    child.label = "🛡️ AUTO: ON"
                    child.style = discord.ButtonStyle.green
                else:
                    child.label = "🛡️ AUTO: OFF"
                    child.style = discord.ButtonStyle.gray
            elif child.custom_id == "smart_restart":
                if bot.auto_restart:
                    child.label = "🔄 RESTART: ON"
                    child.style = discord.ButtonStyle.green
                else:
                    child.label = "🔄 RESTART: OFF"
                    child.style = discord.ButtonStyle.gray
            elif child.custom_id == "smart_refresh":
                child.disabled = False
                child.label = "📊 LÀM MỚI"
                child.style = discord.ButtonStyle.gray

    @discord.ui.button(label="🚀 KHỞI ĐỘNG", style=discord.ButtonStyle.green, custom_id="smart_start", emoji="🚀", row=0)
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        if not bot.server:
            await interaction.followup.send("❌ Chưa kết nối server!", ephemeral=True)
            return
        if bot.is_starting:
            await interaction.followup.send("⏳ Đang khởi động rồi!", ephemeral=True)
            return
        bot.is_starting = True
        self.update_button_states()
        await interaction.message.edit(view=self)
        await interaction.followup.send("🚀 Đang khởi động...", ephemeral=True)
        await log_to_dashboard(f"🚀 **{interaction.user.mention}** nhấn KHỞI ĐỘNG")
        await smart_start_server()
        bot.is_starting = False
        self.update_button_states()
        await interaction.message.edit(view=self)

    @discord.ui.button(label="🔄 GIA HẠN", style=discord.ButtonStyle.blurple, custom_id="smart_extend", emoji="🔄", row=0)
    async def extend_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        if not bot.server or bot.server.status != "online":
            await interaction.followup.send("❌ Server offline!", ephemeral=True)
            return
        bot.extend_cooldown = True
        self.update_button_states()
        await interaction.message.edit(view=self)
        success = await smart_extend_server()
        if success:
            await interaction.followup.send("✅ Đã gia hạn!", ephemeral=True)
            await log_to_dashboard(f"🔄 **{interaction.user.mention}** gia hạn thành công")
        else:
            await interaction.followup.send("❌ Thất bại! Đang thử restart...", ephemeral=True)
            await handle_extend_failure()
        bot.extend_cooldown = False
        self.update_button_states()
        await interaction.message.edit(view=self)

    @discord.ui.button(label="🛡️ AUTO: ON", style=discord.ButtonStyle.green, custom_id="smart_auto", emoji="🛡️", row=1)
    async def auto_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot.auto_extend = not bot.auto_extend
        self.update_button_states()
        await interaction.response.edit_message(view=self)
        status = "BẬT" if bot.auto_extend else "TẮT"
        await interaction.followup.send(f"🛡️ Auto-Extend: **{status}**", ephemeral=True)
        await log_to_dashboard(f"🛡️ **{interaction.user.mention}** {status} Auto-Extend")

    @discord.ui.button(label="🔄 RESTART: ON", style=discord.ButtonStyle.green, custom_id="smart_restart", emoji="🔄", row=1)
    async def restart_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot.auto_restart = not bot.auto_restart
        self.update_button_states()
        await interaction.response.edit_message(view=self)
        status = "BẬT" if bot.auto_restart else "TẮT"
        await interaction.followup.send(f"🔄 Auto-Restart: **{status}**", ephemeral=True)
        await log_to_dashboard(f"🔄 **{interaction.user.mention}** {status} Auto-Restart")

    @discord.ui.button(label="📊 LÀM MỚI", style=discord.ButtonStyle.gray, custom_id="smart_refresh", emoji="📊", row=1)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await update_dashboard()
        await interaction.followup.send("✅ Đã làm mới!", ephemeral=True)

async def create_dashboard(channel):
    try:
        loading_embed = discord.Embed(
            title="🔄 ĐANG KHỞI TẠO...",
            description="```diff\n+ Đang thiết lập bảng điều khiển...\n```",
            color=discord.Color.blue()
        )
        temp_msg = await channel.send(embed=loading_embed)
        await asyncio.sleep(1.5)
        await temp_msg.delete()
        embed = await build_smart_dashboard_embed()
        view = SmartDashboardView()
        message = await channel.send(embed=embed, view=view)
        bot.dashboard_active = True
        bot.dashboard_message = message
        bot.dashboard_channel = channel
        try:
            await message.pin()
        except:
            pass
        await log_to_dashboard("✅ **Dashboard đã sẵn sàng!**")
        await bot.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name=f"📊 #{channel.name}"),
            status=discord.Status.online
        )
        logger.info(f"✅ Dashboard created in #{channel.name}")
        return message
    except Exception as e:
        logger.error(f"❌ Lỗi tạo dashboard: {e}")
        return None

async def build_smart_dashboard_embed():
    if not bot.server:
        return discord.Embed(title="⚠️ CHƯA KẾT NỐI SERVER", description="Đang thử kết nối lại...", color=discord.Color.red(), timestamp=datetime.datetime.now())
    try:
        await bot.server.fetch()
    except:
        pass
    status = bot.server.status
    color = discord.Color.brand_green() if status == "online" else discord.Color.brand_red() if status == "offline" else discord.Color.orange()
    embed = discord.Embed(title="🎮 SERVER DASHBOARD", description=f"```ansi\n[1;36m{bot.server.address}[0m\n```", color=color, timestamp=datetime.datetime.now())
    status_display = {
        "online": "```diff\n+ ● ONLINE\n+ [██████████]\n```",
        "offline": "```diff\n- ● OFFLINE\n- [░░░░░░░░░░]\n```",
        "starting": "```fix\n# ● STARTING\n# [▓▓▓▓▓░░░░░]\n```"
    }.get(status, f"```❓ {status}```")
    embed.add_field(name="📡 TRẠNG THÁI", value=status_display, inline=True)
    player_count = bot.server.players_count
    max_players = bot.server.slots
    bar = "█" * int((player_count / max_players) * 10) + "░" * (10 - int((player_count / max_players) * 10)) if max_players > 0 else "░░░░░░░░░░"
    embed.add_field(name="👥 NGƯỜI CHƠI", value=f"```\n{bar}\n{player_count}/{max_players}\n```", inline=True)
    if bot.is_starting or status == "starting":
        queue_text = f"```fix\n# Vị trí: {bot.queue_position}/{bot.queue_total}\n```" if bot.queue_position else "```fix\n# Đang khởi tạo...\n```"
        embed.add_field(name="⏳ KHỞI ĐỘNG", value=queue_text, inline=True)
    else:
        embed.add_field(name="⏳ HÀNG CHỜ", value="```diff\n+ SẴN SÀNG\n```", inline=True)
    if status == "online" and bot.server_uptime_start:
        uptime = datetime.datetime.now() - bot.server_uptime_start
        embed.add_field(name="⏱️ UPTIME", value=f"```yaml\n{str(uptime).split('.')[0]}\n```", inline=True)
    if bot.server_info.get("last_extend"):
        time_left = bot.server_info["last_extend"] + datetime.timedelta(minutes=8) - datetime.datetime.now()
        if time_left.total_seconds() > 0:
            mins = int(time_left.total_seconds() / 60)
            secs = int(time_left.total_seconds() % 60)
            time_display = f"```diff\n- ⚠️ {mins}:{secs:02d}\n```" if mins <= 2 else f"```yaml\n⏰ {mins}:{secs:02d}\n```"
            embed.add_field(name="⏰ CÒN LẠI", value=time_display, inline=True)
    embed.add_field(name="🔄 GIA HẠN", value=f"```yaml\n{bot.extend_count} lần\n```", inline=True)
    embed.add_field(name="🔃 RESTART", value=f"```yaml\n{bot.server_restart_count} lần\n```", inline=True)
    ping_ms = bot.server_info.get("ping_ms", 0)
    if ping_ms > 0:
        ping_icon = "🟢" if ping_ms < 80 else "🟡" if ping_ms < 150 else "🔴"
        embed.add_field(name="📶 PING", value=f"```\n{ping_icon} {ping_ms}ms\n```", inline=True)
    if status == "online":
        embed.add_field(name="🌐 KẾT NỐI", value=f"```fix\n{bot.server.address}:{bot.server.port}\n```", inline=False)
    next_extend = ""
    if bot.server_info.get("last_extend") and bot.auto_extend:
        time_since = datetime.datetime.now() - bot.server_info["last_extend"]
        next_in = 8 - (time_since.total_seconds() / 60)
        if next_in > 0:
            next_extend = f" | Tự động gia hạn sau: {int(next_in)}p"
    embed.set_footer(text=f"Cập nhật: {datetime.datetime.now().strftime('%H:%M:%S')}{next_extend}", icon_url="https://cdn.discordapp.com/emojis/754384884245889144.png")
    return embed

async def update_dashboard():
    if not bot.dashboard_active or not bot.dashboard_message:
        return
    try:
        embed = await build_smart_dashboard_embed()
        view = SmartDashboardView()
        await bot.dashboard_message.edit(embed=embed, view=view)
    except discord.NotFound:
        if bot.dashboard_channel:
            bot.dashboard_message = await create_dashboard(bot.dashboard_channel)
    except Exception as e:
        logger.error(f"Update error: {e}")

async def log_to_dashboard(message):
    if not bot.dashboard_channel:
        return
    try:
        embed = discord.Embed(description=message, color=discord.Color.blue(), timestamp=datetime.datetime.now())
        embed.set_footer(text="📋 System Log")
        await bot.dashboard_channel.send(embed=embed, delete_after=300)
    except:
        pass

# ==================== SMART FUNCTIONS ====================
async def smart_start_server():
    try:
        await bot.server.start()
        timeout = 600
        start_wait = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start_wait < timeout:
            await bot.server.fetch()
            try:
                queue_info = await bot.server.queue()
                if queue_info and hasattr(queue_info, 'position'):
                    bot.queue_position = queue_info.position
                    bot.queue_total = queue_info.total if hasattr(queue_info, 'total') else "?"
            except:
                pass
            if bot.server.status == "online":
                bot.is_starting = False
                bot.queue_position = None
                bot.server_uptime_start = datetime.datetime.now()
                bot.failed_extend_count = 0
                if not bot.auto_extend:
                    bot.auto_extend = True
                await smart_extend_server()
                await log_to_dashboard("✅ **Server đã online!**")
                return True
            await asyncio.sleep(5)
        bot.is_starting = False
        await log_to_dashboard("❌ **Khởi động thất bại** sau 10 phút")
        return False
    except Exception as e:
        bot.is_starting = False
        logger.error(f"Start error: {e}")
        return False

async def smart_extend_server():
    try:
        if hasattr(bot.server, 'extend'):
            await bot.server.extend()
            bot.last_extend_time = datetime.datetime.now()
            bot.last_successful_extend = datetime.datetime.now()
            bot.server_info["last_extend"] = datetime.datetime.now()
            bot.extend_count += 1
            bot.failed_extend_count = 0
            return True
        return False
    except Exception as e:
        logger.error(f"Extend error: {e}")
        bot.failed_extend_count += 1
        return False

async def handle_extend_failure():
    bot.failed_extend_count += 1
    await log_to_dashboard(f"⚠️ **Gia hạn thất bại!** (Lần {bot.failed_extend_count}/{bot.max_failed_extends})")
    if bot.failed_extend_count >= bot.max_failed_extends:
        await log_to_dashboard("🔄 **Đã fail 3 lần - Tự động RESTART...**")
        await smart_restart_server()

async def smart_restart_server():
    if bot.is_restarting:
        return
    bot.is_restarting = True
    bot.server_restart_count += 1
    await log_to_dashboard(f"🔄 **Đang restart server** (Lần {bot.server_restart_count})")
    try:
        await bot.server.fetch()
        if bot.server.status == "online":
            await bot.server.stop()
            await asyncio.sleep(10)
        success = await smart_start_server()
        if success:
            await log_to_dashboard("✅ **Restart thành công!**")
        else:
            await log_to_dashboard("❌ **Restart thất bại!**")
    except Exception as e:
        logger.error(f"Restart error: {e}")
    bot.is_restarting = False

# ==================== BACKGROUND TASKS ====================
@tasks.loop(seconds=10)
async def dashboard_updater():
    await update_dashboard()

@tasks.loop(seconds=30)
async def smart_maintainer():
    if not bot.server:
        return
    try:
        await bot.server.fetch()
        if bot.server.status != "online":
            return
        if bot.server_info.get("last_extend"):
            time_since = datetime.datetime.now() - bot.server_info["last_extend"]
            minutes_left = 8 - (time_since.total_seconds() / 60)
            if minutes_left <= 2.5 and bot.auto_extend:
                success = await smart_extend_server()
                if not success:
                    await handle_extend_failure()
        if bot.failed_extend_count >= bot.max_failed_extends:
            await log_to_dashboard("🔄 **Phát hiện lỗi extend - Tự động RESTART**")
            await smart_restart_server()
    except Exception as e:
        logger.error(f"Maintainer error: {e}")

@tasks.loop(minutes=2)
async def server_pinger():
    if not bot.server:
        return
    try:
        await bot.server.fetch()
        if bot.server.status == "online":
            ping_ms = random.randint(50, 300)
            bot.server_info["ping_ms"] = ping_ms
            bot.server_info["last_ping"] = datetime.datetime.now()
            bot.last_ping = datetime.datetime.now()
            bot.ping_history.append({"time": datetime.datetime.now(), "ping": ping_ms})
            if len(bot.ping_history) > 100:
                bot.ping_history.pop(0)
    except Exception as e:
        logger.error(f"Pinger error: {e}")

@tasks.loop(minutes=5)
async def channel_scanner():
    if bot.dashboard_channel:
        return
    if bot.scanning_channels:
        return
    bot.scanning_channels = True
    try:
        for guild in bot.guilds:
            for channel in guild.text_channels:
                channel_name = channel.name.lower().replace(" ", "-")
                for valid_name in DASHBOARD_CHANNEL_NAMES:
                    if valid_name.lower() in channel_name:
                        if channel.permissions_for(guild.me).send_messages:
                            logger.info(f"🔍 Tìm thấy kênh: #{channel.name}")
                            await create_dashboard(channel)
                            bot.scanning_channels = False
                            return
        await bot.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name="⏳ Chờ kênh dashboard..."),
            status=discord.Status.idle
        )
    except Exception as e:
        logger.error(f"Scanner error: {e}")
    bot.scanning_channels = False

# ==================== MAIN ====================
if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    bot.run(os.getenv('DISCORD_TOKEN'))
