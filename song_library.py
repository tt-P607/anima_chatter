"""anima_chatter 直播清唱歌库扫描器。

独立于 ``singing_plugin``——后者面向 QQ 群聊场景（``send_voice`` 发语音消息），
**这里专门服务直播清唱**：

- 歌曲存放目录：``plugins/anima_chatter/songs/``
- 文件类型：清唱（无背景音乐）的 ``.wav`` / ``.mp3`` / ``.flac`` / ``.ogg`` 等
- 用途：在 vtb_live 模式下被 :class:`SingSongAction` 直接读出来推到 audio_player，
  通过 VB-Cable 让直播间观众听到，**不**走任何消息发送链路

之所以独立而非直接调 singing_plugin：
- 直播清唱目录由用户自己选放哪些歌，不和 QQ 群歌库混；
- 直播清唱期间 VTS 已经接管虚拟形象嘴型，不需要"发语音消息"；
- 解耦后 anima_chatter 可以单独工作，不依赖 singing_plugin 启用。

时长检测策略：
- 通过 ``soundfile.info()`` 读取音频元数据（不解码完整波形，O(1) 开销）。
- WAV / FLAC / OGG 常规格式都支持；MP3 取决于 libsndfile 编译时是否启用 mp3
  解码模块——失败时静默 fallback 到 ``None`` 并记调试日志，不影响选歌。
"""

from __future__ import annotations

import os
import random
import re
from dataclasses import dataclass
from pathlib import Path

import soundfile as sf  # type: ignore
from rapidfuzz import process

from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("anima_chatter.song_library")


# 支持的清唱文件格式。MP3 需要 libsndfile 编译时启用 mp3 支持；
# 若环境不支持，建议直接用 wav / flac 无损格式。
_SUPPORTED_FORMATS = (".wav", ".mp3", ".flac", ".ogg", ".m4a")


# 默认歌库相对路径（相对插件根目录）
_DEFAULT_SONGS_REL_PATH = "songs"


@dataclass(frozen=True, slots=True)
class SongInfo:
    """单首歌的元数据。

    Attributes:
        name: 文件名（不含扩展名）。这就是模型 schema 里看到的歌名。
        path: 文件绝对路径。
        duration_seconds: 音频时长（秒）。无法读取时为 ``None``。
    """

    name: str
    path: Path
    duration_seconds: float | None


def _format_duration(seconds: float | None) -> str:
    """把秒数格式化为 ``M:SS`` 形式；无效值返回 ``"?"``。"""

    if seconds is None or seconds < 0:
        return "?"
    total = int(round(seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes}:{secs:02d}"


def _read_duration(path: Path) -> float | None:
    """读取音频时长（秒）；失败时返回 None 并写调试日志。"""

    try:
        info = sf.info(str(path))
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"读取时长失败 path={path.name}: {exc}")
        return None
    frames = getattr(info, "frames", 0) or 0
    samplerate = getattr(info, "samplerate", 0) or 0
    if frames <= 0 or samplerate <= 0:
        return None
    return float(frames) / float(samplerate)


class SongLibrary:
    """直播清唱歌库——内存里维护 ``{歌名: SongInfo}`` 映射。

    与 :class:`plugins.singing_plugin.services.song_service.SongService` 的
    主要区别：
    - 不再依赖框架的 ``BaseService``（不需要被注册成组件，由 action 直接 new）；
    - 默认根目录写死到 ``plugins/anima_chatter/songs/``；
    - 不暴露 ``get_song_as_b64`` 这种序列化接口，因为 audio_player 直接吃 bytes；
    - 扫描时同步读取每首歌的时长，给 prompt schema 用。

    **mtime 缓存**：``rescan()`` 开销主要来自 ``os.listdir`` + 每首歌的
    ``soundfile.info()``——如果歌库目录文件没有变动，重复扫盘是浪费。本类
    在 :meth:`get_song_names` / :meth:`get_songs` / :meth:`find_song` /
    :meth:`get_random_song_path` 等查询方法里改为先比对目录 mtime，发生变化
    才走真正的 ``rescan()``，否则直接复用上次的内存快照。直播场景下
    ``SingSongAction.to_schema()`` 每次 LLM 请求都会触发查询，缓存能显著降 IO。
    """

    def __init__(self, plugin_dir: Path, songs_rel_path: str = "") -> None:
        """初始化歌库。

        Args:
            plugin_dir: 插件根目录。
            songs_rel_path: 备选的相对歌库目录。如果为空，则使用全局 data 目录。
        """

        if not songs_rel_path:
            # 统一规范：放在全局数据目录 data/anima_chatter/songs/ 下，避免污染插件代码目录且免受 Git 追踪影响
            self._songs_dir = Path(os.getcwd()).resolve() / "data" / "anima_chatter" / "songs"
        else:
            self._songs_dir = (plugin_dir / songs_rel_path).resolve()

        # 歌名 → SongInfo 元数据；扫描时填充。
        self._song_map: dict[str, SongInfo] = {}

        # 上次扫描时记录的目录 mtime（_songs_dir 自身的 mtime）。
        # _ensure_fresh 会用它判断"是否有文件被增删"——目录 mtime 在文件被
        # 创建 / 删除 / 重命名时会变化（Windows / Linux 都满足这个语义）。
        # 仅文件**内容**修改不会让目录 mtime 变化，但歌库场景下文件内容修改
        # 比较罕见——清唱通常只是新增 / 删除，所以 mtime 缓存够用。
        self._cached_dir_mtime: float = -1.0

        if not self._songs_dir.is_dir():
            logger.info(f"清唱歌库目录不存在，已自动创建: {self._songs_dir}")
            self._songs_dir.mkdir(parents=True, exist_ok=True)

        self.rescan()

    def _current_dir_mtime(self) -> float:
        """读当前 ``_songs_dir`` 的 mtime；目录不存在时返回 ``-1.0``。"""

        try:
            return self._songs_dir.stat().st_mtime
        except OSError:
            return -1.0

    def _ensure_fresh(self) -> None:
        """惰性刷新：只有目录 mtime 变了才重新扫盘。

        每次查询接口（:meth:`get_song_names` / :meth:`get_songs` /
        :meth:`find_song` / :meth:`get_random_song_path`）都会先调一次本方法。
        热路径下绝大多数命中缓存，``stat()`` 开销 O(1)。
        """

        current = self._current_dir_mtime()
        if current == self._cached_dir_mtime:
            return
        self.rescan()

    @property
    def songs_dir(self) -> Path:
        """歌库目录绝对路径。"""

        return self._songs_dir

    @property
    def is_empty(self) -> bool:
        """歌库是否为空。"""

        return not self._song_map

    def rescan(self) -> int:
        """**强制**重扫歌库，返回当前歌曲数量。

        外部调用方一般用 :meth:`_ensure_fresh` 即可——只有想跳过 mtime 比对
        强行重扫时（如调试 / 测试覆盖率）才直接调本方法。

        扫描时同步读取每首歌的时长（``soundfile.info``，不解码波形 O(1)），
        所以不会显著拖慢 ``find_song`` / ``get_random_song_path`` 这条热路径。
        """

        if not self._songs_dir.is_dir():
            self._song_map = {}
            self._cached_dir_mtime = -1.0
            return 0

        new_map: dict[str, SongInfo] = {}
        for filename in os.listdir(self._songs_dir):
            lower = filename.lower()
            if not lower.endswith(_SUPPORTED_FORMATS):
                continue
            song_name = os.path.splitext(filename)[0]
            full_path = self._songs_dir / filename
            duration = _read_duration(full_path)
            new_map[song_name] = SongInfo(
                name=song_name,
                path=full_path,
                duration_seconds=duration,
            )

        self._song_map = new_map
        # 扫描成功后才更新 mtime——若扫描中途异常会保留旧 mtime 让下次重试。
        self._cached_dir_mtime = self._current_dir_mtime()
        return len(new_map)

    def get_song_names(self) -> list[str]:
        """获取当前歌库的所有歌名（按文件名字母序，稳定）。"""

        self._ensure_fresh()
        return sorted(self._song_map.keys())

    def get_songs(self) -> list[SongInfo]:
        """获取所有歌曲的元数据列表（按歌名字母序）。"""

        self._ensure_fresh()
        return [self._song_map[name] for name in sorted(self._song_map.keys())]

    def get_song_list_str(self, max_show: int = 100) -> str:
        """返回供 prompt 注入的歌单文本，每首带时长 ``歌名 (M:SS)``。

        Args:
            max_show: 最多展示多少首；超过会截断并显示总数。
        """

        songs = self.get_songs()
        if not songs:
            return "（清唱歌库为空，目前不能唱歌）"
        formatted = [
            f"{song.name}（{_format_duration(song.duration_seconds)}）"
            for song in songs
        ]
        if len(formatted) <= max_show:
            return f"[{'、'.join(formatted)}]（共 {len(formatted)} 首）"
        head = formatted[:max_show]
        return f"[{'、'.join(head)}...]（共 {len(formatted)} 首，仅展示前 {max_show} 首）"

    def find_song(self, keyword: str) -> Path | None:
        """按关键词查找歌曲路径。

        匹配优先级：
        1. 精确匹配（忽略大小写）
        2. 归一化匹配（去掉括号 / 空格 / 标点）
        3. rapidfuzz 模糊匹配（阈值 60）

        Args:
            keyword: 用户传的歌名关键词。

        Returns:
            匹配到的文件 Path；找不到返回 None。
        """

        self._ensure_fresh()
        if not self._song_map:
            return None

        lowered = keyword.lower().strip()
        if not lowered:
            return None

        # 1) 精确匹配
        for name, info in self._song_map.items():
            if name.lower() == lowered:
                return info.path

        # 2) 归一化匹配
        norm_keyword = _normalize(keyword)
        if norm_keyword:
            for name, info in self._song_map.items():
                if _normalize(name) == norm_keyword:
                    return info.path

        # 3) rapidfuzz 模糊匹配
        names = list(self._song_map.keys())
        result = process.extractOne(keyword, names)
        if result is not None and result[1] > 60:
            return self._song_map[result[0]].path

        return None

    def get_random_song_path(self) -> Path | None:
        """随机选一首歌，返回路径；歌库为空返回 None。"""

        self._ensure_fresh()
        if not self._song_map:
            return None
        return random.choice(list(self._song_map.values())).path


def _normalize(text: str) -> str:
    """把歌名归一化：去掉括号内容、空格、标点，转小写。

    用于"《XX（片段）》"和 "XX" 这种用户输入时常见差异的容错匹配。
    """

    cleaned = re.sub(r"（[^）]*）|\([^\)]*\)", "", text)
    return re.sub(r"[\W_]+", "", cleaned).lower()


__all__ = ["SongInfo", "SongLibrary"]
