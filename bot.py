"""Бот приватных голосовых комнат (discord.py 2.x).

Заходишь в голосовой канал «Войти» -> бот создаёт комнату «Комната <ник>» на 5 мест
и переносит тебя туда. Управление - кнопками в текстовом канале.
Когда комната пустеет, она удаляется. Если создатель вышел, а в комнате остались люди,
комната остаётся за ним: он может вернуться в неё в любой момент, даже если она закрыта
или скрыта, и управлять ею с панели.
"""
import json
import logging
import os
from pathlib import Path

import discord
from discord.ext import commands

logging.basicConfig(level=logging.INFO)

BASE = Path(__file__).parent
DATA_FILE = BASE / "rooms.json"


def load_env():
    """Читает файл .env рядом с ботом (если он есть)."""
    env = BASE / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
TOKEN = os.environ.get("DISCORD_TOKEN", "")
CREATE_VOICE_ID = int(os.environ.get("CREATE_VOICE_ID", "1557539410755592274"))
CONTROL_TEXT_ID = int(os.environ.get("CONTROL_TEXT_ID", "1557539456914038855"))
ROOM_LIMIT = 5

intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

# id голосового канала -> id владельца
rooms: dict[int, int] = {}


# ---------- хранение ----------
def save_rooms():
    DATA_FILE.write_text(json.dumps({str(k): v for k, v in rooms.items()}), encoding="utf-8")


def load_rooms():
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            rooms.update({int(k): int(v) for k, v in data.items()})
        except Exception:
            logging.exception("Не удалось прочитать rooms.json")


# ---------- вспомогательное ----------
async def get_owned_room(interaction: discord.Interaction):
    """Возвращает комнату нажавшего: ту, в которой он сидит, если он её владелец,
    иначе - его комнату, даже если он сейчас не в ней."""
    member = interaction.user
    voice = getattr(member, "voice", None)
    channel = voice.channel if voice else None
    if channel is not None and rooms.get(channel.id) == member.id:
        return channel

    owned = next((cid for cid, owner in rooms.items() if owner == member.id), None)
    room = interaction.guild.get_channel(owned) if owned else None
    if room is not None:
        return room

    if channel is not None and channel.id in rooms:
        msg = "❌ Управлять комнатой может только её создатель."
    else:
        msg = "❌ У тебя нет приватной комнаты. Зайди в канал «Войти», чтобы создать её."
    await interaction.response.send_message(msg, ephemeral=True)
    return None


# ---------- модальные окна ----------
class RenameModal(discord.ui.Modal, title="Название комнаты"):
    name = discord.ui.TextInput(label="Новое название", max_length=100)

    def __init__(self, room: discord.VoiceChannel):
        super().__init__()
        self.room = room

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            await self.room.edit(name=str(self.name))
            await interaction.followup.send("✅ Название изменено.", ephemeral=True)
        except discord.HTTPException:
            await interaction.followup.send(
                "⚠️ Не удалось сменить название (Discord ограничивает смену имени: 2 раза в 10 минут).",
                ephemeral=True)


class LimitModal(discord.ui.Modal, title="Лимит участников"):
    limit = discord.ui.TextInput(label="Число от 0 до 99 (0 - без лимита)", max_length=2)

    def __init__(self, room: discord.VoiceChannel):
        super().__init__()
        self.room = room

    async def on_submit(self, interaction: discord.Interaction):
        value = str(self.limit).strip()
        if not value.isdigit():
            return await interaction.response.send_message("❌ Введите число.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await self.room.edit(user_limit=int(value))
        await interaction.followup.send(f"✅ Лимит: {value if value != '0' else 'без ограничений'}.",
                                        ephemeral=True)


# ---------- выбор участника ----------
class UserPick(discord.ui.View):
    """action: owner | access | kick | speak"""

    def __init__(self, room: discord.VoiceChannel, action: str):
        super().__init__(timeout=60)
        self.room = room
        self.action = action

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Выберите участника")
    async def pick(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        room = self.room
        if room.id not in rooms or rooms[room.id] != interaction.user.id:
            return await interaction.response.edit_message(content="❌ Комната вам больше не принадлежит.", view=None)

        member = interaction.guild.get_member(select.values[0].id)
        if member is None or member.bot or member.id == interaction.user.id:
            return await interaction.response.edit_message(content="❌ Нельзя выбрать этого пользователя.", view=None)

        in_room = member.voice and member.voice.channel and member.voice.channel.id == room.id

        if self.action == "owner":
            if not in_room:
                return await interaction.response.edit_message(
                    content="❌ Новый создатель должен находиться в комнате.", view=None)
            await transfer_owner(room, interaction.user, member)
            msg = f"👑 Новый создатель комнаты: {member.mention}"

        elif self.action == "access":
            ow = room.overwrites_for(member)
            if ow.connect is True:
                await room.set_permissions(member, overwrite=None)
                msg = f"🚫 Доступ у {member.mention} убран."
            else:
                await room.set_permissions(member, view_channel=True, connect=True)
                msg = f"✅ Доступ выдан {member.mention} (сможет зайти, даже если комната закрыта)."

        elif self.action == "kick":
            if in_room:
                try:
                    await member.move_to(None)
                except discord.HTTPException:
                    pass
            await room.set_permissions(member, connect=False)
            msg = f"🚪 {member.mention} выгнан из комнаты и не сможет зайти снова."

        else:  # speak
            ow = room.overwrites_for(member)
            if ow.speak is False:
                await room.set_permissions(member, speak=None)
                msg = f"🎙 {member.mention} снова может говорить."
            else:
                await room.set_permissions(member, speak=False)
                msg = f"🔇 {member.mention} больше не может говорить."

        await interaction.response.edit_message(content=msg, view=None)


# ---------- возврат доступа ----------
class ReturnAccess(discord.ui.View):
    """Список участников, у которых забран доступ (выгнаны), с возможностью вернуть его."""

    def __init__(self, room: discord.VoiceChannel, banned: list[discord.Member]):
        super().__init__(timeout=60)
        self.room = room
        select = discord.ui.Select(
            placeholder="Кому вернуть доступ",
            options=[discord.SelectOption(label=m.display_name[:100], value=str(m.id),
                                          description=f"@{m.name}"[:100])
                     for m in banned[:25]],
        )
        select.callback = self.on_pick
        self.add_item(select)
        self.select = select

    async def on_pick(self, interaction: discord.Interaction):
        room = self.room
        if room.id not in rooms or rooms[room.id] != interaction.user.id:
            return await interaction.response.edit_message(content="❌ Комната вам больше не принадлежит.", view=None)
        member = interaction.guild.get_member(int(self.select.values[0]))
        if member is None:
            return await interaction.response.edit_message(content="❌ Участник не найден.", view=None)
        ow = room.overwrites_for(member)
        ow.view_channel = True
        ow.connect = True
        await room.set_permissions(member, overwrite=ow)
        await interaction.response.edit_message(
            content=f"↩️ Доступ возвращён {member.mention} (сможет зайти, даже если комната закрыта).",
            view=None)


def banned_members(room: discord.VoiceChannel) -> list[discord.Member]:
    return [t for t, ow in room.overwrites.items()
            if isinstance(t, discord.Member) and ow.connect is False]


async def transfer_owner(room: discord.VoiceChannel, old: discord.abc.User | None, new: discord.Member):
    rooms[room.id] = new.id
    save_rooms()
    await room.set_permissions(new, view_channel=True, connect=True, speak=True)
    if old is not None:
        # забираем личные права старого владельца (если он не выгнан/не заглушён)
        await room.set_permissions(old, overwrite=None)


# ---------- панель ----------
class Panel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def pick_view(self, interaction: discord.Interaction, action: str, text: str):
        room = await get_owned_room(interaction)
        if room:
            await interaction.response.send_message(text, view=UserPick(room, action), ephemeral=True)

    @discord.ui.button(emoji="👑", style=discord.ButtonStyle.secondary, custom_id="pr:owner", row=0)
    async def owner(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.pick_view(interaction, "owner", "Выберите нового создателя комнаты:")

    @discord.ui.button(emoji="📋", style=discord.ButtonStyle.secondary, custom_id="pr:access", row=0)
    async def access(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.pick_view(interaction, "access", "Выдать / забрать доступ к комнате:")

    @discord.ui.button(emoji="👥", style=discord.ButtonStyle.secondary, custom_id="pr:limit", row=0)
    async def limit(self, interaction: discord.Interaction, button: discord.ui.Button):
        room = await get_owned_room(interaction)
        if room:
            await interaction.response.send_modal(LimitModal(room))

    @discord.ui.button(emoji="🔒", style=discord.ButtonStyle.secondary, custom_id="pr:lock", row=0)
    async def lock(self, interaction: discord.Interaction, button: discord.ui.Button):
        room = await get_owned_room(interaction)
        if not room:
            return
        await interaction.response.defer(ephemeral=True)
        everyone = interaction.guild.default_role
        ow = room.overwrites_for(everyone)
        closing = ow.connect is not False
        ow.connect = False if closing else None
        await room.set_permissions(everyone, overwrite=ow)
        await interaction.followup.send("🔒 Комната закрыта." if closing else "🔓 Комната открыта.", ephemeral=True)

    @discord.ui.button(emoji="✏️", style=discord.ButtonStyle.secondary, custom_id="pr:rename", row=1)
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button):
        room = await get_owned_room(interaction)
        if room:
            await interaction.response.send_modal(RenameModal(room))

    @discord.ui.button(emoji="👁️", style=discord.ButtonStyle.secondary, custom_id="pr:hide", row=1)
    async def hide(self, interaction: discord.Interaction, button: discord.ui.Button):
        room = await get_owned_room(interaction)
        if not room:
            return
        await interaction.response.defer(ephemeral=True)
        everyone = interaction.guild.default_role
        ow = room.overwrites_for(everyone)
        hiding = ow.view_channel is not False
        ow.view_channel = False if hiding else None
        await room.set_permissions(everyone, overwrite=ow)
        await interaction.followup.send("🙈 Комната скрыта." if hiding else "👁️ Комната снова видна всем.",
                                        ephemeral=True)

    @discord.ui.button(emoji="🚪", style=discord.ButtonStyle.secondary, custom_id="pr:kick", row=1)
    async def kick(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.pick_view(interaction, "kick", "Кого выгнать из комнаты:")

    @discord.ui.button(emoji="🎙️", style=discord.ButtonStyle.secondary, custom_id="pr:speak", row=1)
    async def speak(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.pick_view(interaction, "speak", "У кого забрать / вернуть право говорить:")

    @discord.ui.button(emoji="↩️", style=discord.ButtonStyle.secondary, custom_id="pr:return", row=2)
    async def return_access(self, interaction: discord.Interaction, button: discord.ui.Button):
        room = await get_owned_room(interaction)
        if not room:
            return
        banned = banned_members(room)
        if not banned:
            return await interaction.response.send_message(
                "✅ Ни у кого из участников доступ не забран.", ephemeral=True)
        text = "Выберите, кому вернуть доступ к комнате:"
        if len(banned) > 25:
            text += f"\n(показаны первые 25 из {len(banned)})"
        await interaction.response.send_message(text, view=ReturnAccess(room, banned), ephemeral=True)


def panel_embed() -> discord.Embed:
    return discord.Embed(
        title="⚙️ Приватные комнаты",
        description=(
            "Измените конфигурацию вашей комнаты с помощью панели управления.\n"
            "👑 назначить нового создателя комнаты\n"
            "📋 управление доступом к комнате\n"
            "👥 задать новый лимит участников\n"
            "🔒 закрыть/открыть комнату\n"
            "✏️ изменить название комнаты\n"
            "👁️ скрыть/открыть комнату\n"
            "🚪 выгнать участника из комнаты\n"
            "🎙️ ограничить/выдать право говорить\n"
            "↩️ вернуть доступ выгнанному участнику\n\n"
            "Если вы вышли из своей комнаты, она остаётся за вами - "
            "вернуться можно через «Войти» или напрямую, даже если комната закрыта.\n"
            "Чтобы создать комнату, зайдите в голосовой канал «Войти»."
        ),
        color=0x2B2D31,
    )


# ---------- события ----------
@bot.event
async def setup_hook():
    bot.add_view(Panel())  # кнопки работают после перезапуска


@bot.event
async def on_ready():
    logging.info("Бот запущен: %s", bot.user)

    # чистим то, что осталось после перезапуска
    for cid in list(rooms):
        ch = bot.get_channel(cid)
        if ch is None:
            rooms.pop(cid)
        elif not ch.members:
            rooms.pop(cid)
            try:
                await ch.delete(reason="Пустая приватная комната")
            except discord.HTTPException:
                pass
    save_rooms()

    # панель: обновляем свою старую или отправляем новую
    channel = bot.get_channel(CONTROL_TEXT_ID)
    if channel is None:
        return logging.error("Текстовый канал %s не найден", CONTROL_TEXT_ID)
    try:
        async for m in channel.history(limit=50):
            if m.author == bot.user and m.embeds:
                await m.edit(embed=panel_embed(), view=Panel())
                return
        await channel.send(embed=panel_embed(), view=Panel())
    except discord.Forbidden:
        logging.error("Нет прав писать/читать историю в канале управления")


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if before.channel == after.channel or member.bot:
        return

    # 1) заход в «Войти» -> создаём комнату
    if after.channel and after.channel.id == CREATE_VOICE_ID:
        existing = next((cid for cid, owner in rooms.items() if owner == member.id), None)
        room = member.guild.get_channel(existing) if existing else None
        if room is None:
            overwrites = {
                member: discord.PermissionOverwrite(view_channel=True, connect=True, speak=True),
            }
            try:
                room = await member.guild.create_voice_channel(
                    name=f"Комната {member.display_name}"[:100],
                    category=after.channel.category,
                    user_limit=ROOM_LIMIT,
                    overwrites=overwrites,
                    reason="Приватная комната",
                )
            except discord.HTTPException:
                logging.exception("Не удалось создать комнату")
                return
            rooms[room.id] = member.id
            save_rooms()
        try:
            await member.move_to(room)
        except discord.HTTPException:
            # человек успел выйти - убираем пустой канал
            if not room.members:
                rooms.pop(room.id, None)
                save_rooms()
                await room.delete()
            return

    # 2) выход из комнаты
    ch = before.channel
    if ch and ch.id in rooms:
        remaining = [m for m in ch.members if not m.bot]
        if not remaining:
            rooms.pop(ch.id, None)
            save_rooms()
            try:
                await ch.delete(reason="Приватная комната опустела")
            except discord.HTTPException:
                pass
        # если вышел создатель, а в комнате остались люди - комната остаётся за ним:
        # его личные права (view_channel/connect/speak) не трогаем, поэтому он может
        # вернуться даже в закрытую или скрытую комнату.


if not TOKEN:
    raise SystemExit("Не задан DISCORD_TOKEN (файл .env или переменная окружения).")
load_rooms()
bot.run(TOKEN)
