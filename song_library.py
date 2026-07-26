"""直播清唱歌库扫描器。

歌曲存放在 ``data/anima_chatter/songs/`` 下，支持两种组织方式：

- **单文件**：``歌名.wav`` —— 单轨播放，走 VB-Cable 驱动口型。
- **子文件夹**：``歌名/`` 内含人声轨与伴奏轨 —— 双轨播放，人声进 VB-Cable
  驱动口型、伴奏进独立设备，避免伴奏带动嘴型。人声轨按文件名含 ``vocal`` /
  ``人声`` 识别，伴奏轨按 ``inst`` / ``伴奏`` / ``accomp`` / ``other`` 识别。

**加载策略**：只在 :meth:`SongLibrary.__init__` 扫描一次。运行时所有查询都走
内存快照，不再触发磁盘 IO——``to_schema()`` 每次 LLM 请求都会调用查询接口，
零 IO 才能保证热路径开销可控。新增 / 删除歌曲需要重启 Bot。
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz, process

from src.app.plugin_system.api.log_api import get_logger

from .audio import read_duration_from_path


__all__ = ["SongInfo", "SongLibrary", "format_duration"]


logger = get_logger("anima_chatter.song_library")


# 支持的音频格式。MP3 需要 libsndfile 编译时启用 mp3 支持；环境不确定时建议
# 直接用 wav / flac 无损格式。
_SUPPORTED_FORMATS = (".wav", ".mp3", ".flac", ".ogg", ".m4a")

# 双轨识别关键词（文件名小写匹配）。
_VOCAL_KEYWORDS = ("vocal", "人声")
_INST_KEYWORDS = ("inst", "伴奏", "accomp", "other")

# rapidfuzz 模糊匹配阈值。低于此分视为未命中。
_FUZZY_THRESHOLD = 60


@dataclass(frozen=True, slots=True)
class SongInfo:
    """单首歌的元数据。

    Attributes:
        name: 歌名（单文件取文件名去扩展名；子文件夹双轨取文件夹名）。
        path: 人声 / 主音轨文件绝对路径，这一路进 VB-Cable 驱动口型。
        duration_seconds: 音频时长（秒）；无法读取时为 ``None``。
        inst_path: 伴奏文件绝对路径（仅子文件夹双轨歌有）；单轨歌为 ``None``。
    """

    name: str
    path: Path
    duration_seconds: float | None
    inst_path: Path | None = None


def format_duration(seconds: float | None) -> str:
    """把秒数格式化为 ``M:SS``。

    Args:
        seconds: 时长秒数；``None`` 或负数表示未知。

    Returns:
        ``"3:25"`` 形式的字符串；无效值返回 ``"?"``。
    """

    if seconds is None or seconds < 0:
        return "?"
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}:{secs:02d}"


def _normalize(text: str) -> str:
    """把歌名归一化：去掉括号内容、空格、标点，转小写。

    用于容错匹配"《XX（片段）》"与 "XX" 这类用户输入差异。

    Args:
        text: 原始歌名或关键词。

    Returns:
        归一化后的字符串。
    """

    cleaned = re.sub(r"（[^）]*）|\([^)]*\)", "", text)
    return re.sub(r"[\W_]+", "", cleaned).lower()


class SongLibrary:
    """直播清唱歌库——内存里维护 ``{歌名: SongInfo}`` 映射。"""

    def __init__(self, songs_dir: Path) -> None:
        """初始化歌库并**立即扫描一次**。

        目录不存在时会自动创建。

        Args:
            songs_dir: 歌库根目录绝对路径。
        """

        self._songs_dir = songs_dir.resolve()
        self._song_map: dict[str, SongInfo] = {}

        if not self._songs_dir.is_dir():
            logger.info(f"清唱歌库目录不存在，已自动创建: {self._songs_dir}")
            self._songs_dir.mkdir(parents=True, exist_ok=True)

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
        """重扫歌库目录。

        正常运行流程**不应**主动调用——歌库在构造时扫一次即可。仅供调试 /
        单元测试使用。

        Returns:
            扫描到的歌曲数量。
        """

        if not self._songs_dir.is_dir():
            self._song_map = {}
            return 0

        new_map: dict[str, SongInfo] = {}
        for entry in sorted(self._songs_dir.iterdir()):
            if entry.is_dir():
                info = self._scan_song_folder(entry)
                if info is not None:
                    new_map[info.name] = info
                continue
            if entry.suffix.lower() in _SUPPORTED_FORMATS:
                new_map[entry.stem] = SongInfo(
                    name=entry.stem,
                    path=entry,
                    duration_seconds=read_duration_from_path(entry),
                )

        self._song_map = new_map
        return len(new_map)

    @staticmethod
    def _find_track(folder: Path, keywords: tuple[str, ...]) -> Path | None:
        """在文件夹里按关键词找音轨文件。

        Args:
            folder: 歌曲子文件夹。
            keywords: 文件名需包含的关键词之一（忽略大小写）。

        Returns:
            命中的文件路径；找不到返回 ``None``。
        """

        for entry in sorted(folder.iterdir()):
            if entry.suffix.lower() not in _SUPPORTED_FORMATS:
                continue
            lower = entry.name.lower()
            if any(keyword in lower for keyword in keywords):
                return entry
        return None

    def _scan_song_folder(self, folder: Path) -> SongInfo | None:
        """扫描一个歌曲子文件夹，配对人声 + 伴奏两轨。

        找不到标记为人声的文件时，退化为取第一个音频文件当人声单轨播。

        Args:
            folder: 歌曲子文件夹。

        Returns:
            歌曲元数据；文件夹内无任何音频文件时返回 ``None``。
        """

        vocal = self._find_track(folder, _VOCAL_KEYWORDS)
        inst = self._find_track(folder, _INST_KEYWORDS)

        if vocal is None:
            for entry in sorted(folder.iterdir()):
                if entry.suffix.lower() in _SUPPORTED_FORMATS:
                    vocal = entry
                    break
        if vocal is None:
            return None

        # 伴奏不能和人声是同一个文件。
        if inst == vocal:
            inst = None

        return SongInfo(
            name=folder.name,
            path=vocal,
            duration_seconds=read_duration_from_path(vocal),
            inst_path=inst,
        )

    def get_song_names(self) -> list[str]:
        """获取全部歌名（按字母序，稳定）。

        Returns:
            歌名列表。
        """

        return sorted(self._song_map)

    def get_songs(self) -> list[SongInfo]:
        """获取全部歌曲元数据（按歌名字母序）。

        Returns:
            歌曲元数据列表。
        """

        return [self._song_map[name] for name in sorted(self._song_map)]

    def find_song_info(self, keyword: str) -> SongInfo | None:
        """按关键词查找歌曲。

        匹配优先级（从严到松）：

        1. 精确匹配（忽略大小写）
        2. 归一化匹配（去掉括号 / 空格 / 标点）
        3. **子串包含匹配**（双向）——歌库歌名常带歌手 / 出处前缀，而模型和
           观众往往只说核心歌名，子串命中最符合直觉。命中多个时取归一化后
           最短的（最贴近"纯歌名"那条）。
        4. rapidfuzz ``partial_ratio`` 模糊匹配，阈值 60。

        Args:
            keyword: 歌名关键词。

        Returns:
            匹配到的歌曲元数据；找不到返回 ``None``。
        """

        if not self._song_map:
            return None
        lowered = keyword.lower().strip()
        if not lowered:
            return None

        for name, info in self._song_map.items():
            if name.lower() == lowered:
                return info

        norm_keyword = _normalize(keyword)
        if norm_keyword:
            for name, info in self._song_map.items():
                if _normalize(name) == norm_keyword:
                    return info

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

        result = process.extractOne(
            keyword, list(self._song_map), scorer=fuzz.partial_ratio
        )
        if result is not None and result[1] >= _FUZZY_THRESHOLD:
            return self._song_map[result[0]]

        return None

    def get_random_song_info(self) -> SongInfo | None:
        """随机选一首歌。

        Returns:
            随机歌曲元数据；歌库为空返回 ``None``。
        """

        if not self._song_map:
            return None
        return random.choice(list(self._song_map.values()))
