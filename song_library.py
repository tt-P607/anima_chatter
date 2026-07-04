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
from rapidfuzz import fuzz, process

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
        name: 歌名（单文件取文件名去扩展名；子文件夹双轨取文件夹名）。
        path: 人声 / 主音轨文件绝对路径。这一路进 VB-Cable 驱动口型。
        duration_seconds: 音频时长（秒）。无法读取时为 ``None``。
        inst_path: 伴奏文件绝对路径（仅子文件夹双轨歌有）。这一路走系统扬声器，
            不进 VB-Cable，避免伴奏带动口型。单轨歌为 ``None``。
    """

    name: str
    path: Path
    duration_seconds: float | None
    inst_path: Path | None = None


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

    **加载策略**：本类**只在 Bot 启动时（``__init__``）扫描一次**。运行时所有
    查询接口（:meth:`get_song_names` / :meth:`get_songs` / :meth:`find_song` /
    :meth:`get_random_song_path`）都直接走内存快照，**不再触发任何磁盘 IO**。
    新增 / 删除歌曲需要重启 Bot 才会生效——这换来 schema 序列化（每次 LLM
    请求都会跑）等热路径 0 IO 开销，且行为稳定可预测。
    需要强制重新加载时调 :meth:`rescan`（仅限调试 / 测试，业务路径不应使用）。
    """

    def __init__(self, plugin_dir: Path, songs_rel_path: str = "") -> None:
        """初始化歌库（**启动时一次性扫描**）。

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

        if not self._songs_dir.is_dir():
            logger.info(f"清唱歌库目录不存在，已自动创建: {self._songs_dir}")
            self._songs_dir.mkdir(parents=True, exist_ok=True)

        # 启动时扫描一次；之后所有查询都走内存快照。
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
        """重扫歌库，返回当前歌曲数量。

        正常运行流程**不应**主动调本方法——歌库在 :meth:`__init__` 里扫一次
        即可。仅供调试 / 单元测试 / 未来"手动 reload"命令使用。

        扫描时同步读取每首歌的时长（``soundfile.info``，不解码波形 O(1)）。
        """

        if not self._songs_dir.is_dir():
            self._song_map = {}
            return 0

        new_map: dict[str, SongInfo] = {}
        for entry in os.listdir(self._songs_dir):
            full = self._songs_dir / entry
            # 子文件夹 = 双轨歌（vocal + inst），文件夹名即歌名。
            if full.is_dir():
                info = self._scan_song_folder(full)
                if info is not None:
                    new_map[info.name] = info
                continue
            # 顶层单文件 = 老格式单轨歌，照旧只播这一个文件。
            if entry.lower().endswith(_SUPPORTED_FORMATS):
                song_name = os.path.splitext(entry)[0]
                new_map[song_name] = SongInfo(
                    name=song_name,
                    path=full,
                    duration_seconds=_read_duration(full),
                )

        self._song_map = new_map
        return len(new_map)

    @staticmethod
    def _find_track(folder: Path, *keywords: str) -> Path | None:
        """在文件夹里按关键词找音轨文件（文件名含任一关键词，忽略大小写）。"""

        for filename in os.listdir(folder):
            lower = filename.lower()
            if not lower.endswith(_SUPPORTED_FORMATS):
                continue
            if any(kw in lower for kw in keywords):
                return folder / filename
        return None

    def _scan_song_folder(self, folder: Path) -> SongInfo | None:
        """扫描一个歌曲子文件夹，配对 vocal + inst 两轨。

        约定：文件名含 ``vocal`` / ``voice`` / ``人声`` 的是人声轨、
        含 ``inst`` / ``伴奏`` / ``accomp`` 的是伴奏轨。
        找不到人声轨时退化——取文件夹里第一个音频文件当人声单轨播。
        """

        vocal = self._find_track(folder, "vocal", "人声")
        inst = self._find_track(folder, "inst", "伴奏", "accomp" ,"other")

        if vocal is None:
            # 没标 vocal：取第一个音频文件兜底当人声单轨。
            for filename in sorted(os.listdir(folder)):
                if filename.lower().endswith(_SUPPORTED_FORMATS):
                    vocal = folder / filename
                    break
        if vocal is None:
            return None  # 空文件夹，跳过

        # 伴奏不能和人声是同一个文件
        if inst is not None and inst == vocal:
            inst = None

        return SongInfo(
            name=folder.name,
            path=vocal,
            duration_seconds=_read_duration(vocal),
            inst_path=inst,
        )

    def get_song_names(self) -> list[str]:
        """获取当前歌库的所有歌名（按文件名字母序，稳定）。"""

        return sorted(self._song_map.keys())

    def get_songs(self) -> list[SongInfo]:
        """获取所有歌曲的元数据列表（按歌名字母序）。"""

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

    def _match_song_info(self, keyword: str) -> SongInfo | None:
        """统一的歌曲匹配核心，返回完整 :class:`SongInfo`；找不到返回 None。

        匹配优先级（从严到松）：
        1. 精确匹配（忽略大小写）
        2. 归一化匹配（去掉括号 / 空格 / 标点）
        3. **子串包含匹配**——歌名包含关键词、或关键词包含歌名。歌库歌名常带
           歌手 / 出处前缀（如 ``三Z-STUDIO _ HOYO-MiX - 捉迷藏``），而模型 /
           观众往往只说核心歌名（``捉迷藏``），所以子串命中最符合直觉。命中多个
           时取归一化后最短的（最贴近"纯歌名"那条）。
        4. rapidfuzz 模糊匹配——用 ``partial_ratio``（对"关键词是歌名子串"的场景
           天然高分），阈值 ``>= 60``。

        Args:
            keyword: 用户 / 模型传的歌名关键词。

        Returns:
            匹配到的 :class:`SongInfo`；找不到返回 None。
        """

        if not self._song_map:
            return None
        lowered = keyword.lower().strip()
        if not lowered:
            return None

        # 1) 精确匹配
        for name, info in self._song_map.items():
            if name.lower() == lowered:
                return info

        # 2) 归一化匹配
        norm_keyword = _normalize(keyword)
        if norm_keyword:
            for name, info in self._song_map.items():
                if _normalize(name) == norm_keyword:
                    return info

        # 3) 子串包含匹配（双向）：歌名含关键词 或 关键词含歌名。
        # 命中多条时取归一化名最短的——最接近"纯歌名"，避免误命中更长的曲目。
        if norm_keyword:
            candidates: list[tuple[int, SongInfo]] = []
            for name, info in self._song_map.items():
                norm_name = _normalize(name)
                if not norm_name:
                    continue
                if norm_keyword in norm_name or norm_name in norm_keyword:
                    candidates.append((len(norm_name), info))
            if candidates:
                candidates.sort(key=lambda pair: pair[0])
                return candidates[0][1]

        # 4) rapidfuzz 模糊匹配（partial_ratio 更适合子串场景，阈值 >= 60）
        names = list(self._song_map.keys())
        result = process.extractOne(keyword, names, scorer=fuzz.partial_ratio)
        if result is not None and result[1] >= 60:
            return self._song_map[result[0]]

        return None

    def find_song(self, keyword: str) -> Path | None:
        """按关键词查找歌曲路径；找不到返回 None。

        匹配规则见 :meth:`_match_song_info`。
        """

        info = self._match_song_info(keyword)
        return info.path if info is not None else None

    def find_song_info(self, keyword: str) -> SongInfo | None:
        """按关键词查找歌曲，返回完整 :class:`SongInfo`（含伴奏轨）。

        匹配规则与 :meth:`find_song` 完全一致（见 :meth:`_match_song_info`），
        只是返回整个 SongInfo 而非单 Path——双轨播放需要拿到 ``inst_path``。
        """

        return self._match_song_info(keyword)

    def get_random_song_path(self) -> Path | None:
        """随机选一首歌，返回路径；歌库为空返回 None。"""

        if not self._song_map:
            return None
        return random.choice(list(self._song_map.values())).path

    def get_random_song_info(self) -> SongInfo | None:
        """随机选一首歌，返回完整 :class:`SongInfo`（含伴奏轨）；空库返回 None。"""

        if not self._song_map:
            return None
        return random.choice(list(self._song_map.values()))


def _normalize(text: str) -> str:
    """把歌名归一化：去掉括号内容、空格、标点，转小写。

    用于"《XX（片段）》"和 "XX" 这种用户输入时常见差异的容错匹配。
    """

    cleaned = re.sub(r"（[^）]*）|\([^\)]*\)", "", text)
    return re.sub(r"[\W_]+", "", cleaned).lower()


__all__ = ["SongInfo", "SongLibrary"]
