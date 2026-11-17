"""Robin - 친구 서버용 디스코드 노래봇 (discord.py 2.7+, yt-dlp)

- /setup 으로 로빈 전용 채팅 채널을 정하면 그 채널에 조작 패널이 항상 떠 있다.
- 패널의 [부르기] → 누른 사람의 음성 채널로 로빈이 들어온다.
- [노래 추가] → 입력 팝업에 제목/링크를 넣으면 추가된다.
- [노래 목록] → 나만 보이는 목록에서 ✕ 로 노래를 뺄 수 있다.
"""

import asyncio
import itertools
import json
import logging
import os
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_IDS = [int(x) for x in os.getenv("GUILD_IDS", "").replace(" ", "").split(",") if x]
COOKIES = os.getenv("YT_COOKIES", "cookies.txt")
FFMPEG = os.getenv("FFMPEG_PATH", "ffmpeg")
IDLE_TIMEOUT = int(os.getenv("IDLE_TIMEOUT", "1800"))  # 재생이 없으면 몇 초 뒤 퇴장 (30분)
ALONE_TIMEOUT = int(os.getenv("ALONE_TIMEOUT", "60"))  # 채널에 혼자 남으면 몇 초 뒤 퇴장
MAX_PLAYLIST = int(os.getenv("MAX_PLAYLIST", "50"))
DEFAULT_VOLUME = int(os.getenv("DEFAULT_VOLUME", "50"))  # 0~100, 100 = 원본 음량
BITRATE = int(os.getenv("BITRATE", "128"))  # 전송 음질(kbps). 64로 낮추면 트래픽 절반
DATA_FILE = os.getenv("DATA_FILE", "robin_data.json")  # 서버별 전용 채널/패널 저장
STREAM_TTL = 60 * 60  # 스트림 URL 재사용 시간(초)
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))  # 차단 알림 DM 받을 사람. 0이면 봇 소유자

COLOR = 0x5865F2
COLOR_IDLE = 0x4E5058
COLOR_WARN = 0xF0B232

# 유튜브가 서버(IP)를 막았을 때 yt-dlp 오류에 나오는 문구들
YT_BLOCK_HINTS = ("sign in to confirm", "not a bot", "http error 429", "too many requests")


def is_yt_block(error: Exception) -> bool:
    text = str(error).lower()
    return any(h in text for h in YT_BLOCK_HINTS)
LIST_PAGE_SIZE = 10  # [노래 목록] 한 페이지에 보이는 노래 수

log = logging.getLogger("robin")

YDL_BASE = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "source_address": "0.0.0.0",
}
if os.path.isfile(COOKIES):
    YDL_BASE["cookiefile"] = COOKIES

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"


BUFFER_FRAMES = 500   # 미리 받아두는 최대 분량 (20ms × 500 = 10초)
PREFILL_FRAMES = 50   # 재생 시작 전에 채울 분량 (1초)
OPUS_SILENCE = b"\xf8\xff\xfe"  # 20ms 무음 Opus 조각


class BufferedOpusAudio(discord.AudioSource):
    """ffmpeg 출력을 별도 스레드에서 미리 받아두는 버퍼.

    discord.py는 read()가 늦어지면 밀린 조각을 몰아서 보내 따라잡는데,
    이게 '노래가 빨라졌다 느려졌다' 하는 원인이다 (discord.py #9120).
    버퍼에서 바로 꺼내 주고, 버퍼가 비면 따라잡기 대신 짧은 무음을 넣는다.
    """

    def __init__(self, inner: discord.FFmpegOpusAudio):
        self.inner = inner
        self.q: queue.Queue[bytes] = queue.Queue(maxsize=BUFFER_FRAMES)
        self.eof = threading.Event()
        self.closed = threading.Event()
        self.underruns = 0
        threading.Thread(target=self._fill, daemon=True, name="robin-buffer").start()

    def _fill(self):
        try:
            while not self.closed.is_set():
                pkt = self.inner.read()
                if not pkt:
                    break
                while not self.closed.is_set():
                    try:
                        self.q.put(pkt, timeout=0.5)
                        break
                    except queue.Full:
                        continue
        except Exception as e:  # noqa: BLE001
            log.warning("buffer read failed: %s", e)
        finally:
            self.eof.set()

    def wait_prefill(self, frames: int = PREFILL_FRAMES, timeout: float = 8.0):
        """재생 전에 버퍼를 조금 채움 (블로킹, 스레드에서 호출)"""
        end = time.monotonic() + timeout
        while self.q.qsize() < frames and not self.eof.is_set() and time.monotonic() < end:
            time.sleep(0.02)

    def is_opus(self) -> bool:
        return True

    def read(self) -> bytes:
        try:
            return self.q.get_nowait()
        except queue.Empty:
            pass
        if self.eof.is_set():
            try:
                return self.q.get_nowait()  # eof 직전에 들어온 조각
            except queue.Empty:
                return b""  # 곡 끝
        self.underruns += 1
        if self.underruns % 50 == 1:
            log.info("버퍼가 비어서 무음으로 채움 (누적 %d회)", self.underruns)
        return OPUS_SILENCE

    def cleanup(self):
        self.closed.set()
        self.inner.cleanup()


def make_source(stream_url: str, volume: float, start: float = 0.0) -> BufferedOpusAudio:
    """ffmpeg이 볼륨 조절과 Opus 압축까지 끝낸 소리를 버퍼에 미리 받아둠"""
    before = FFMPEG_BEFORE + (f" -ss {start:.2f}" if start > 0 else "")
    return BufferedOpusAudio(discord.FFmpegOpusAudio(
        stream_url, bitrate=BITRATE, executable=FFMPEG,
        before_options=before, options=f"-vn -af volume={volume:.2f} -application audio",
    ))


# ---------------------------------------------------------------- yt-dlp ----

_track_ids = itertools.count(1)


@dataclass
class Track:
    title: str
    url: str
    duration: Optional[int]
    requester: str
    uploader: Optional[str] = None
    thumbnail: Optional[str] = None
    stream_url: Optional[str] = field(default=None, repr=False)
    resolved_at: float = 0.0
    uid: int = field(default_factory=lambda: next(_track_ids))  # 목록에서 ✕로 지울 때 식별용


def fmt_time(sec: Optional[int]) -> str:
    if not sec:
        return "LIVE/?"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def search(query: str, requester: str) -> list[Track]:
    """검색어 또는 URL → Track 목록 (블로킹, 스레드에서 실행)"""
    opts = dict(YDL_BASE)
    is_url = query.startswith(("http://", "https://"))
    if is_url and "list=" in query:
        opts.update(noplaylist=False, extract_flat="in_playlist", playlistend=MAX_PLAYLIST)
    elif not is_url:
        query = f"ytsearch1:{query}"
        opts["extract_flat"] = "in_playlist"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=False)

    entries = info.get("entries") if "entries" in info else [info]
    tracks = []
    for e in entries or []:
        if not e:
            continue
        url = e.get("webpage_url") or e.get("url")
        if url and not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={url}"
        if not url:
            continue
        vid = e.get("id")
        if vid and ("youtube.com" in url or "youtu.be" in url):
            thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        else:
            thumb = e.get("thumbnail")
        t = Track(
            title=e.get("title") or "제목 없음",
            url=url,
            duration=e.get("duration"),
            requester=requester,
            uploader=e.get("channel") or e.get("uploader"),
            thumbnail=thumb,
        )
        # 단일 영상 URL은 이미 스트림 주소까지 받아왔으니 재사용
        if not opts.get("extract_flat") and e.get("url", "").startswith("http"):
            t.stream_url, t.resolved_at = e["url"], time.time()
        tracks.append(t)
    return tracks


def resolve_stream(track: Track) -> str:
    """재생 직전에 실제 오디오 스트림 URL을 얻음 (블로킹)"""
    if track.stream_url and time.time() - track.resolved_at < STREAM_TTL:
        return track.stream_url
    with yt_dlp.YoutubeDL(YDL_BASE) as ydl:
        info = ydl.extract_info(track.url, download=False)
    if "entries" in info:
        info = info["entries"][0]
    track.stream_url, track.resolved_at = info["url"], time.time()
    track.duration = track.duration or info.get("duration")
    track.uploader = track.uploader or info.get("channel") or info.get("uploader")
    track.thumbnail = track.thumbnail or info.get("thumbnail")
    return track.stream_url


# ---------------------------------------------------------------- player ----

class GuildPlayer:
    """음성 채널에 들어가 있는 동안만 존재하는 서버별 재생 상태"""

    def __init__(self, bot: "Robin", guild: discord.Guild):
        self.bot = bot
        self.guild = guild
        self.queue: deque[Track] = deque()
        self.current: Optional[Track] = None
        self.volume = DEFAULT_VOLUME / 100
        self.offset = 0.0  # 볼륨을 바꿔 소스를 갈아끼운 경우의 시작 위치 보정
        self._lock = asyncio.Lock()
        self._leave_task: Optional[asyncio.Task] = None

    @property
    def vc(self) -> Optional[discord.VoiceClient]:
        return self.guild.voice_client  # type: ignore[return-value]

    @property
    def paused(self) -> bool:
        return bool(self.vc and self.vc.is_paused())

    async def update_panel(self):
        await self.bot.refresh_panel(self.guild.id)

    def position(self) -> float:
        """지금 곡을 몇 초째 재생 중인지 (보낸 20ms 조각 수로 계산)"""
        p = getattr(self.vc, "_player", None) if self.vc else None
        return self.offset + (p.loops * 0.02 if p else 0.0)

    def set_volume(self, volume: float):
        """볼륨을 바꾸고, 재생 중이면 지금 위치부터 새 볼륨으로 이어서 재생"""
        self.volume = volume
        vc, t = self.vc, self.current
        if not (vc and t and t.stream_url and (vc.is_playing() or vc.is_paused())):
            return
        was_paused = vc.is_paused()
        pos = self.position()
        old = vc.source
        new = make_source(t.stream_url, volume, pos)
        new.wait_prefill(10, timeout=2.0)  # 0.2초만 채우고 바로 교체 (끊김 최소화)
        vc.source = new  # 내부에서 잠깐 멈췄다 다시 재생함
        if was_paused:
            vc.pause()
        p = getattr(vc, "_player", None)
        self.offset = pos - (p.loops * 0.02 if p else 0.0)  # loops는 소스를 바꿔도 이어서 셈
        if old:
            old.cleanup()  # 이전 ffmpeg 프로세스 정리

    async def clear_speaking(self, delay: float = 0):
        """'말하는 중' 초록 테두리가 남아 있지 않도록 '말 안 하는 중' 신호를 보냄"""
        if delay:
            await asyncio.sleep(delay)
        vc = self.vc
        if vc and vc.is_connected() and not (vc.is_playing() or vc.is_paused()):
            try:
                await vc.ws.speak(discord.SpeakingState.none)
            except Exception as e:  # noqa: BLE001
                log.debug("speak(none) failed: %s", e)

    # --- 재생 흐름 ---
    def _next_track(self) -> Optional[Track]:
        self.current = None
        return self.queue.popleft() if self.queue else None

    async def play_next(self):
        async with self._lock:
            while True:
                vc = self.vc
                if not vc or not vc.is_connected():
                    self.current = None
                    return
                if vc.is_playing() or vc.is_paused():
                    return
                track = self._next_track()
                if track is None:
                    await self.update_panel()
                    self.schedule_leave(IDLE_TIMEOUT)
                    asyncio.create_task(self.clear_speaking(1))
                    return
                try:
                    stream = await asyncio.to_thread(resolve_stream, track)
                except Exception as e:  # noqa: BLE001
                    if is_yt_block(e):
                        # 차단이면 다른 노래도 다 실패하므로 멈춤. 노래는 목록 맨 앞에 되돌려 둠
                        self.queue.appendleft(track)
                        await self.bot.report_block(self.guild, e)
                        self.schedule_leave(IDLE_TIMEOUT)
                        return
                    log.warning("resolve failed: %s (%s)", track.url, e)
                    await self.bot.notify(self.guild.id,
                                          f"⚠️ **{track.title}** 재생에 실패해서 건너뛸게요.\n`{str(e)[:180]}`")
                    continue
                source = make_source(stream, self.volume)
                await asyncio.to_thread(source.wait_prefill)
                if not vc.is_connected() or vc.is_playing() or vc.is_paused():
                    source.cleanup()
                    return
                vc.play(source, after=self._after)
                self.offset = 0.0
                await self.bot.clear_block()
                self.current = track
                self.cancel_leave()
                await self.update_panel()
                return

    def _after(self, error: Optional[Exception]):
        if error:
            log.error("player error: %s", error)
        asyncio.run_coroutine_threadsafe(self.play_next(), self.bot.loop)

    def skip(self):
        if self.vc:
            self.vc.stop()

    def stop_all(self):
        """노래 목록을 비우고 지금 곡도 멈춤 (로빈은 채널에 남음)"""
        self.queue.clear()
        self.current = None
        if self.vc and (self.vc.is_playing() or self.vc.is_paused()):
            self.vc.stop()
        self.schedule_leave(IDLE_TIMEOUT)

    def remove(self, uid: int) -> Optional[Track]:
        for t in self.queue:
            if t.uid == uid:
                self.queue.remove(t)
                return t
        return None

    # --- 자동 퇴장 ---
    def schedule_leave(self, delay: int):
        self.cancel_leave()
        self._leave_task = asyncio.create_task(self._leave_after(delay))

    def cancel_leave(self):
        t = self._leave_task
        if t and not t.done() and t is not asyncio.current_task():
            t.cancel()
        self._leave_task = None

    async def _leave_after(self, delay: int):
        await asyncio.sleep(delay)
        vc = self.vc
        if not vc:
            return
        humans = [m for m in vc.channel.members if not m.bot]
        if humans and vc.is_playing():
            return
        reason = "채널에 아무도 없어서" if not humans else f"{delay // 60}분 동안 재생이 없어서"
        await self.destroy(f"👋 {reason} 나갔어요.")

    async def destroy(self, notice: Optional[str] = None):
        self.queue.clear()
        self.current = None
        self.cancel_leave()
        self.bot.players.pop(self.guild.id, None)  # 먼저 빼서 끊김 이벤트와 중복 처리 방지
        if self.vc:
            await self.vc.disconnect(force=True)
        await self.bot.refresh_panel(self.guild.id, notice=notice)


# ------------------------------------------------------------------ embeds ----

def idle_embed(notice: Optional[str] = None) -> discord.Embed:
    desc = "음성 채널에 들어간 뒤 **부르기**를 눌러주세요."
    if notice:
        desc = f"{notice}\n\n{desc}"
    return discord.Embed(title="🎵 Robin", description=desc, color=COLOR_IDLE)


def panel_embed(player: GuildPlayer) -> discord.Embed:
    t = player.current
    if t:
        e = discord.Embed(title=t.title[:256], url=t.url, color=COLOR)
        lines = []
        if t.uploader:
            lines.append(t.uploader)
        lines.append(f"신청: {t.requester}")
        lines.append(f"길이: {fmt_time(t.duration)}")
        e.description = "\n".join(lines)
        if t.thumbnail:
            e.set_thumbnail(url=t.thumbnail)
        e.set_author(name="⏸ 일시정지됨" if player.paused else "▶ 재생 중")
    else:
        e = discord.Embed(
            title="🎵 Robin",
            description="재생 중인 노래가 없어요.\n**노래 추가**를 눌러 노래를 넣어주세요.",
            color=COLOR,
        )
    where = f"🔊 {player.vc.channel.name} · " if player.vc else ""
    e.set_footer(text=f"{where}노래 목록 {len(player.queue)}곡 · 음량 {int(round(player.volume * 100))}%")
    return e


# ------------------------------------------------------------------- UI ----

async def _get_player(itx: discord.Interaction, need_same_channel: bool = True) -> Optional[GuildPlayer]:
    """버튼 공통 확인. 문제가 있으면 안내 메시지를 보내고 None"""
    bot: Robin = itx.client  # type: ignore[assignment]
    player = bot.players.get(itx.guild_id)
    vc = itx.guild.voice_client if itx.guild else None
    if not player or not vc:
        await itx.response.send_message("로빈이 음성 채널에 없어요. 패널의 **부르기**를 눌러주세요.", ephemeral=True)
        return None
    if need_same_channel and (not itx.user.voice or itx.user.voice.channel != vc.channel):
        await itx.response.send_message(f"**{vc.channel.name}** 음성 채널에 들어와야 조작할 수 있어요.",
                                        ephemeral=True)
        return None
    return player


def _button(view: discord.ui.View, label, cid, style, row, cb, disabled=False):
    b = discord.ui.Button(label=label, custom_id=cid, style=style, row=row, disabled=disabled)
    b.callback = cb
    view.add_item(b)


class IdleView(discord.ui.View):
    """로빈이 음성 채널에 없을 때의 패널: [부르기]"""

    def __init__(self, bot: "Robin"):
        super().__init__(timeout=None)
        self.bot = bot

    async def interaction_check(self, itx: discord.Interaction) -> bool:
        self.bot.adopt_panel(itx)
        return True
        _button(self, "부르기", "robin:summon", discord.ButtonStyle.primary, 0, self.on_summon)

    async def on_summon(self, itx: discord.Interaction):
        if not itx.user.voice or not itx.user.voice.channel:
            return await itx.response.send_message("먼저 음성 채널에 들어가 주세요.", ephemeral=True)
        channel = itx.user.voice.channel
        vc = itx.guild.voice_client
        if vc and vc.channel != channel and vc.is_playing() and any(not m.bot for m in vc.channel.members):
            return await itx.response.send_message(f"이미 **{vc.channel.name}** 에서 재생 중이에요.", ephemeral=True)

        await itx.response.defer()  # 음성 연결이 3초 넘게 걸릴 수 있음
        if vc is None:
            await channel.connect(self_deaf=True)
        elif vc.channel != channel:
            await vc.move_to(channel)
        player = self.bot.player(itx.guild)
        if not player.vc.is_playing():
            player.schedule_leave(IDLE_TIMEOUT)
            asyncio.create_task(player.clear_speaking(2))
            asyncio.create_task(player.clear_speaking(6))
        await self.bot.refresh_after(itx)


class ControlView(discord.ui.View):
    """로빈이 음성 채널에 있을 때의 패널"""

    async def interaction_check(self, itx: discord.Interaction) -> bool:
        self.bot.adopt_panel(itx)
        return True

    def __init__(self, bot: "Robin", player: Optional[GuildPlayer] = None):
        super().__init__(timeout=None)
        self.bot = bot
        has_track = bool(player and player.current)
        has_any = bool(player and (player.current or player.queue))
        paused = bool(player and player.paused)

        S = discord.ButtonStyle
        _button(self, "노래 추가", "robin:add", S.primary, 0, self.on_add)
        _button(self, "재생" if paused else "일시정지", "robin:pause", S.secondary, 0, self.on_pause, not has_track)
        _button(self, "다음 노래", "robin:skip", S.secondary, 0, self.on_skip, not has_track)
        _button(self, "중지", "robin:stop", S.secondary, 1, self.on_stop, not has_any)
        _button(self, "노래 목록", "robin:queue", S.secondary, 1, self.on_queue)
        _button(self, "퇴장", "robin:leave", S.danger, 1, self.on_leave)

    async def on_add(self, itx: discord.Interaction):
        if not await _get_player(itx):
            return
        await itx.response.send_modal(AddModal())

    async def on_pause(self, itx: discord.Interaction):
        if not (player := await _get_player(itx)):
            return
        vc = player.vc
        if vc.is_playing():
            vc.pause()
            player.schedule_leave(IDLE_TIMEOUT)
        elif vc.is_paused():
            vc.resume()
            player.cancel_leave()
        await itx.response.edit_message(**self.bot.panel_kwargs(itx.guild_id))

    async def on_skip(self, itx: discord.Interaction):
        if not (player := await _get_player(itx)):
            return
        if player.vc.is_playing() or player.vc.is_paused():
            player.skip()  # 다음 노래가 시작되면 패널이 자동으로 갱신됨
        await itx.response.defer()

    async def on_stop(self, itx: discord.Interaction):
        if not (player := await _get_player(itx)):
            return
        player.stop_all()
        await itx.response.edit_message(**self.bot.panel_kwargs(itx.guild_id))
        await self.bot.notify(itx.guild_id, f"⏹️ {itx.user.display_name}님이 노래를 멈추고 목록을 비웠어요.", 10)

    async def on_queue(self, itx: discord.Interaction):
        if not (player := await _get_player(itx, need_same_channel=False)):
            return
        await itx.response.send_message(view=QueueView(self.bot, itx.guild_id), ephemeral=True)


    async def on_leave(self, itx: discord.Interaction):
        if not (player := await _get_player(itx)):
            return
        await itx.response.defer()
        notice = f"👋 {itx.user.display_name}님이 로빈을 내보냈어요."
        await player.destroy(notice)
        await self.bot.refresh_after(itx, notice)

class QueueView(discord.ui.LayoutView):
    """[노래 목록] 나만 보이는 목록. 노래마다 오른쪽 ✕ 로 빼기"""

    def __init__(self, bot: "Robin", guild_id: int, page: int = 0, note: Optional[str] = None):
        super().__init__(timeout=14 * 60)  # 상호작용 토큰 만료(15분) 전까지
        self.bot = bot
        self.guild_id = guild_id
        player = bot.players.get(guild_id)
        tracks = list(player.queue) if player else []
        pages = max(1, -(-len(tracks) // LIST_PAGE_SIZE))
        self.page = page = min(max(page, 0), pages - 1)

        box = discord.ui.Container(accent_colour=COLOR)
        head = "### 📜 노래 목록"
        if player and player.current:
            c = player.current
            state = "⏸ 일시정지" if player.paused else "▶ 지금 재생 중"
            head += f"\n{state}: **{c.title}** ({fmt_time(c.duration)}) · {c.requester}"
        box.add_item(discord.ui.TextDisplay(head))
        box.add_item(discord.ui.Separator())

        start = page * LIST_PAGE_SIZE
        for i, t in enumerate(tracks[start:start + LIST_PAGE_SIZE], start + 1):
            x = discord.ui.Button(label="✕", style=discord.ButtonStyle.secondary, custom_id=f"robin:rm:{t.uid}")
            x.callback = self._remover(t.uid)
            box.add_item(discord.ui.Section(
                discord.ui.TextDisplay(f"`{i}.` **{t.title[:80]}** ({fmt_time(t.duration)}) · {t.requester}"),
                accessory=x,
            ))
        if not tracks:
            box.add_item(discord.ui.TextDisplay("목록에 기다리는 노래가 없어요."))

        total = sum(t.duration or 0 for t in tracks)
        foot = f"-# 총 {len(tracks)}곡 · {fmt_time(total) if total else '0:00'}"
        if pages > 1:
            foot += f" · {page + 1}/{pages} 페이지"
        if note:
            foot = f"{note}\n{foot}"
        box.add_item(discord.ui.TextDisplay(foot))
        self.add_item(box)

        if pages > 1:
            row = discord.ui.ActionRow()
            prev = discord.ui.Button(label="◀ 이전", style=discord.ButtonStyle.secondary, disabled=page == 0)
            nxt = discord.ui.Button(label="다음 ▶", style=discord.ButtonStyle.secondary, disabled=page >= pages - 1)
            prev.callback = self._goto(page - 1)
            nxt.callback = self._goto(page + 1)
            row.add_item(prev)
            row.add_item(nxt)
            self.add_item(row)

    def _goto(self, page: int):
        async def cb(itx: discord.Interaction):
            await itx.response.edit_message(view=QueueView(self.bot, self.guild_id, page))
        return cb

    def _remover(self, uid: int):
        async def cb(itx: discord.Interaction):
            if not (player := await _get_player(itx)):
                return
            t = player.remove(uid)
            note = f"🗑️ **{t.title[:80]}** 를 뺐어요." if t else "이미 재생됐거나 빠진 노래예요."
            await itx.response.edit_message(view=QueueView(self.bot, self.guild_id, self.page, note))
            if t:
                await player.update_panel()
        return cb


# -------------------------------------------------------- 노래 추가 팝업 ----

class AddModal(discord.ui.Modal, title="노래 추가"):
    query = discord.ui.Label(
        text="유튜브 링크 또는 노래 제목",
        description="재생목록 링크를 넣으면 최대 50곡까지 한 번에 추가돼요.",
        component=discord.ui.TextInput(
            placeholder="예) 아이유 밤편지  /  https://youtu.be/...",
            max_length=300,
        ),
    )

    async def on_submit(self, itx: discord.Interaction):
        if not (player := await _get_player(itx)):
            return
        await itx.response.defer(ephemeral=True, thinking=True)
        q = self.query.component.value.strip()

        async def reply(text: str, keep: bool = False):
            msg = await itx.followup.send(text, ephemeral=True, wait=True)
            if not keep:  # 성공 메시지는 잠깐 보였다가 사라지게
                await msg.delete(delay=8)

        try:
            tracks = await asyncio.to_thread(search, q, itx.user.display_name)
        except Exception as e:  # noqa: BLE001
            if is_yt_block(e):
                await itx.client.report_block(itx.guild, e)
                return await reply("⚠️ **유튜브가 로빈 서버의 접속을 막아서 노래를 가져올 수 없어요.**\n"
                                   "관리자에게 알림을 보냈어요. 해결될 때까지 기다려주세요.", keep=True)
            return await reply(f"⚠️ 검색 실패: `{str(e)[:300]}`", keep=True)
        if not tracks:
            return await reply(f"`{q[:100]}` 검색 결과가 없어요.", keep=True)

        player.queue.extend(tracks)
        if len(tracks) == 1:
            await reply(f"➕ **{tracks[0].title}** ({fmt_time(tracks[0].duration)}) 추가했어요.")
        else:
            await reply(f"➕ 재생목록에서 **{len(tracks)}곡** 추가했어요.")

        vc = player.vc
        if vc and not (vc.is_playing() or vc.is_paused()):
            await player.play_next()
        else:
            await player.update_panel()

    async def on_error(self, itx: discord.Interaction, error: Exception):
        log.exception("modal error", exc_info=error)
        text = f"⚠️ 오류: {str(error)[:300]}"
        if itx.response.is_done():
            await itx.followup.send(text, ephemeral=True)
        else:
            await itx.response.send_message(text, ephemeral=True)


# ------------------------------------------------------------------- bot ----

class Robin(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.players: dict[int, GuildPlayer] = {}
        self.panels: dict[int, dict] = self._load_panels()  # guild_id → {channel_id, message_id}
        self.yt_block: Optional[str] = None  # 유튜브 차단 감지 시 오류 내용
        self._panels_restored = False

    # --- 저장 ---
    @staticmethod
    def _load_panels() -> dict[int, dict]:
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                return {int(k): v for k, v in json.load(f).get("panels", {}).items()}
        except FileNotFoundError:
            return {}
        except Exception as e:  # noqa: BLE001
            log.error("%s 읽기 실패: %s", DATA_FILE, e)
            return {}

    def _save_panels(self):
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"panels": {str(k): v for k, v in self.panels.items()}}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DATA_FILE)

    def player(self, guild: discord.Guild) -> GuildPlayer:
        if guild.id not in self.players:
            self.players[guild.id] = GuildPlayer(self, guild)
        return self.players[guild.id]

    # --- 패널 ---
    def panel_kwargs(self, guild_id: int, notice: Optional[str] = None) -> dict:
        player = self.players.get(guild_id)
        if player and player.vc:
            kw = {"embed": panel_embed(player), "view": ControlView(self, player)}
        else:
            kw = {"embed": idle_embed(notice), "view": IdleView(self)}
        if self.yt_block:
            e = kw["embed"]
            e.colour = COLOR_WARN
            e.description = ("⚠️ **유튜브가 로빈 서버의 접속을 막았어요.**\n"
                             "관리자가 해결할 때까지 노래를 재생할 수 없어요.\n\n" + (e.description or ""))
        return kw

    # --- 유튜브 차단 ---
    async def report_block(self, guild: discord.Guild, error: Exception):
        """차단 감지: 모든 패널에 경고 표시 + 관리자에게 DM (차단이 풀릴 때까지 1번만)"""
        first = self.yt_block is None
        self.yt_block = str(error)[:500]
        log.error("유튜브 차단 감지 (%s): %s", guild.name, error)
        if not first:
            await self.refresh_panel(guild.id)
            return
        for gid in list(self.panels):
            await self.refresh_panel(gid)
        await self.dm_admin(
            "⚠️ **[Robin] 유튜브가 로빈 서버의 접속을 막았어요.**\n"
            f"- 처음 감지된 곳: **{guild.name}**\n"
            f"- 시각: <t:{int(time.time())}:F>\n"
            f"- 오류: `{str(error)[:300]}`\n\n"
            "**해결 방법**: 서버의 `~/robin` 폴더에 유튜브 쿠키 파일(`cookies.txt`)을 넣고 "
            "`sudo systemctl restart robin` 으로 재시작하세요.\n"
            "-# 노래가 다시 재생되면 자동으로 경고가 사라지고 알려드려요."
        )

    async def clear_block(self):
        if self.yt_block is None:
            return
        self.yt_block = None
        log.info("유튜브 차단 해제 확인")
        for gid in list(self.panels):
            await self.refresh_panel(gid)
        await self.dm_admin("✅ **[Robin] 유튜브 재생이 다시 정상으로 돌아왔어요.**")

    async def dm_admin(self, text: str):
        try:
            uid = ADMIN_USER_ID
            if not uid:
                app = await self.application_info()
                uid = app.owner.id
            user = self.get_user(uid) or await self.fetch_user(uid)
            await user.send(text)
        except Exception as e:  # noqa: BLE001
            log.warning("관리자 DM 실패: %s", e)

    def adopt_panel(self, itx: discord.Interaction):
        """등록된 패널이 없는 서버에서 패널 버튼이 눌리면 그 메시지를 패널로 등록
        (예: 다른 컴퓨터에서 /setup 한 패널을 서버로 옮겨 온 경우)"""
        msg = itx.message
        if not msg or not itx.guild_id or itx.guild_id in self.panels:
            return
        if msg.author.id != self.user.id:
            return
        self.panels[itx.guild_id] = {"channel_id": msg.channel.id, "message_id": msg.id}
        self._save_panels()
        log.info("서버 %s의 패널을 자동 등록했어요", itx.guild_id)

    def panel_channel(self, guild_id: int) -> Optional[discord.TextChannel]:
        cfg = self.panels.get(guild_id)
        return self.get_channel(cfg["channel_id"]) if cfg else None  # type: ignore[return-value]

    async def post_panel(self, channel: discord.TextChannel, notice: Optional[str] = None):
        """채널에 패널을 새로 올리고 저장 (이전 패널은 지움)"""
        old = self.panels.get(channel.guild.id)
        if old:
            old_ch = self.get_channel(old["channel_id"])
            if old_ch:
                try:
                    await old_ch.get_partial_message(old["message_id"]).delete()
                except discord.HTTPException:
                    pass
        msg = await channel.send(**self.panel_kwargs(channel.guild.id, notice))
        self.panels[channel.guild.id] = {"channel_id": channel.id, "message_id": msg.id}
        self._save_panels()

    async def refresh_panel(self, guild_id: int, notice: Optional[str] = None):
        cfg = self.panels.get(guild_id)
        channel = self.panel_channel(guild_id)
        if not cfg or not channel:
            return
        try:
            await channel.get_partial_message(cfg["message_id"]).edit(**self.panel_kwargs(guild_id, notice))
        except discord.NotFound:  # 누가 패널을 지웠으면 다시 올림
            await self.post_panel(channel, notice)
        except discord.HTTPException as e:
            log.warning("panel update failed: %s", e)

    async def refresh_after(self, itx: discord.Interaction, notice: Optional[str] = None):
        """버튼 처리 후 패널 갱신. 등록된 패널이 아닌 메시지에서 눌렀으면 그 메시지도 갱신"""
        await self.refresh_panel(itx.guild_id, notice)
        cfg = self.panels.get(itx.guild_id)
        if itx.message and (not cfg or itx.message.id != cfg["message_id"]):
            try:
                await itx.edit_original_response(**self.panel_kwargs(itx.guild_id, notice))
            except discord.HTTPException:
                pass

    async def notify(self, guild_id: int, text: str, delete_after: float = 20):
        """전용 채널에 잠깐 보였다 사라지는 알림"""
        channel = self.panel_channel(guild_id)
        if channel:
            try:
                await channel.send(text, delete_after=delete_after)
            except discord.HTTPException:
                pass

    # --- 시작/이벤트 ---
    async def setup_hook(self):
        await self.add_cog(Music(self))
        # 재시작 후에도 기존 패널 버튼이 반응하도록 등록
        self.add_view(IdleView(self))
        self.add_view(ControlView(self))
        if GUILD_IDS:
            for gid in GUILD_IDS:
                g = discord.Object(id=gid)
                self.tree.copy_global_to(guild=g)
                await self.tree.sync(guild=g)
            log.info("슬래시 명령어를 서버 %s곳에 등록했어요", len(GUILD_IDS))
        else:
            await self.tree.sync()
            log.info("슬래시 명령어를 전역 등록했어요 (반영까지 시간이 걸릴 수 있음)")

    async def on_ready(self):
        log.info("로그인: %s (서버 %d곳)", self.user, len(self.guilds))
        await self.change_presence(activity=discord.CustomActivity(name="🎵 전용 채널에서 부르기"))
        if not self._panels_restored:  # 재시작 직후 1번만
            self._panels_restored = True
            if not GUILD_IDS:
                # 예전에 서버 지정으로 등록했던 명령어가 전역 명령어와 중복으로 보이지 않게 정리
                for g in self.guilds:
                    try:
                        if await self.tree.fetch_commands(guild=g):
                            self.tree.clear_commands(guild=g)
                            await self.tree.sync(guild=g)
                            log.info("서버 '%s'의 예전 서버 전용 명령어를 지웠어요", g.name)
                    except discord.HTTPException as e:
                        log.warning("명령어 정리 실패 (%s): %s", g.name, e)
            # 패널을 '부르기' 상태로 되돌림
            for gid in list(self.panels):
                await self.refresh_panel(gid)
            log.info("패널 %d개 복구", len(self.panels))

    async def on_voice_state_update(self, member, before, after):
        guild = member.guild
        # 봇이 강제로 끊겼을 때(누가 연결 끊기 등) 정리
        if member.id == self.user.id and after.channel is None:
            player = self.players.pop(guild.id, None)
            if player:
                player.queue.clear()
                player.current = None
                player.cancel_leave()
                await self.refresh_panel(guild.id, notice="👋 로빈이 음성 채널에서 나갔어요.")
            return
        player = self.players.get(guild.id)
        vc = player.vc if player else None
        if not vc:
            return
        humans = [m for m in vc.channel.members if not m.bot]
        if not humans:
            player.schedule_leave(ALONE_TIMEOUT)
        elif vc.is_playing():
            player.cancel_leave()
        else:
            player.schedule_leave(IDLE_TIMEOUT)


class Music(commands.Cog):
    def __init__(self, bot: Robin):
        self.bot = bot

    async def cog_app_command_error(self, itx: discord.Interaction, error: app_commands.AppCommandError):
        msg = str(getattr(error, "original", error))
        log.exception("command error", exc_info=error)
        text = f"⚠️ 오류: {msg[:300]}"
        if itx.response.is_done():
            await itx.followup.send(text, ephemeral=True)
        else:
            await itx.response.send_message(text, ephemeral=True)

    @app_commands.command(name="setup", description="이 채팅 채널을 로빈 전용 채널로 정하고 패널을 띄워요")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def setup(self, itx: discord.Interaction):
        channel = itx.channel
        if not isinstance(channel, discord.TextChannel):
            return await itx.response.send_message("일반 채팅 채널에서 써주세요.", ephemeral=True)
        perms = channel.permissions_for(itx.guild.me)
        missing = [name for name, ok in [("메시지 보내기", perms.send_messages), ("링크 첨부", perms.embed_links)]
                   if not ok]
        await itx.response.defer(ephemeral=True, thinking=True)
        await self.bot.post_panel(channel)
        text = f"✅ {channel.mention} 을(를) 로빈 전용 채널로 정했어요."
        if missing:
            text += f"\n⚠️ 이 채널에서 로빈에게 **{', '.join(missing)}** 권한이 없어요. 채널 설정 → 권한에서 추가해주세요."
        await itx.followup.send(text, ephemeral=True)

    @app_commands.command(name="volume", description="로빈 볼륨 (0~100, 100이 원본 음량)")
    @app_commands.guild_only()
    async def volume(self, itx: discord.Interaction, value: app_commands.Range[int, 0, 100]):
        if not (player := await _get_player(itx)):
            return
        player.set_volume(value / 100)
        await itx.response.send_message(f"🔊 볼륨 {value}%", ephemeral=True)
        await player.update_panel()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(".env 파일에 DISCORD_TOKEN 을 넣어주세요.")
    Robin().run(TOKEN, log_level=logging.INFO, root_logger=True)
