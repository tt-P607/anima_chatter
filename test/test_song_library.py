"""清唱歌库扫描与匹配的单元测试。

覆盖单文件 / 双轨文件夹扫描、四级匹配优先级、随机选曲与时长格式化。

用真实的 WAV 文件（``soundfile`` 写入）而非 mock——歌库的核心职责之一就是读取
音频时长，mock 掉反而测不到真实路径。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf  # type: ignore[import-untyped]

from plugins.anima_chatter.song_library import SongLibrary, format_duration


_SAMPLE_RATE = 8000


def _write_wav(path: Path, seconds: float = 1.0) -> None:
    """在指定路径写一个静音 WAV 文件。

    Args:
        path: 目标文件路径（父目录会被自动创建）。
        seconds: 音频时长（秒）。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    samples = np.zeros(int(_SAMPLE_RATE * seconds), dtype="float32")
    sf.write(str(path), samples, _SAMPLE_RATE)


@pytest.fixture
def library(tmp_path: Path) -> SongLibrary:
    """构造含单轨与双轨歌曲的测试歌库。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        已扫描完成的歌库。
    """

    _write_wav(tmp_path / "三Z-STUDIO _ HOYO-MiX - 捉迷藏.wav", 2.0)
    _write_wav(tmp_path / "夜曲（Live 版）.wav", 1.0)
    _write_wav(tmp_path / "双轨歌" / "song_vocal.wav", 3.0)
    _write_wav(tmp_path / "双轨歌" / "song_inst.wav", 3.0)
    return SongLibrary(tmp_path)


def test_creates_missing_directory(tmp_path: Path) -> None:
    """目录不存在时应自动创建并得到空歌库。"""

    songs_dir = tmp_path / "not_exists"
    lib = SongLibrary(songs_dir)

    assert songs_dir.is_dir()
    assert lib.is_empty


def test_scans_single_file_songs(library: SongLibrary) -> None:
    """顶层单文件应按文件名（去扩展名）作为歌名。"""

    assert "夜曲（Live 版）" in library.get_song_names()


def test_reads_duration(library: SongLibrary) -> None:
    """扫描时应同步读出音频时长。"""

    info = library.find_song_info("夜曲（Live 版）")

    assert info is not None
    assert info.duration_seconds == pytest.approx(1.0, abs=0.05)


def test_scans_dual_track_folder(library: SongLibrary) -> None:
    """子文件夹应按文件夹名成歌，并正确配对人声与伴奏。"""

    info = library.find_song_info("双轨歌")

    assert info is not None
    assert info.name == "双轨歌"
    assert info.path.name == "song_vocal.wav"
    assert info.inst_path is not None
    assert info.inst_path.name == "song_inst.wav"


def test_folder_without_vocal_marker_falls_back(tmp_path: Path) -> None:
    """文件夹里没有标记人声的文件时，取第一个音频当单轨。"""

    _write_wav(tmp_path / "无标记" / "a.wav")
    _write_wav(tmp_path / "无标记" / "b.wav")

    info = SongLibrary(tmp_path).find_song_info("无标记")

    assert info is not None
    assert info.path.name == "a.wav"
    assert info.inst_path is None


def test_empty_folder_is_skipped(tmp_path: Path) -> None:
    """不含音频文件的文件夹应被跳过。"""

    (tmp_path / "空文件夹").mkdir()
    _write_wav(tmp_path / "正常歌.wav")

    assert SongLibrary(tmp_path).get_song_names() == ["正常歌"]


def test_non_audio_files_are_ignored(tmp_path: Path) -> None:
    """非音频扩展名的文件应被忽略。"""

    _write_wav(tmp_path / "歌.wav")
    (tmp_path / "readme.txt").write_text("not audio", encoding="utf-8")

    assert SongLibrary(tmp_path).get_song_names() == ["歌"]


def test_exact_match_has_highest_priority(library: SongLibrary) -> None:
    """完整歌名应精确命中。"""

    info = library.find_song_info("三Z-STUDIO _ HOYO-MiX - 捉迷藏")

    assert info is not None
    assert info.name == "三Z-STUDIO _ HOYO-MiX - 捉迷藏"


def test_normalized_match_ignores_brackets(library: SongLibrary) -> None:
    """归一化匹配应忽略括号内容与标点。"""

    info = library.find_song_info("夜曲")

    assert info is not None
    assert info.name == "夜曲（Live 版）"


def test_substring_match_finds_core_title(library: SongLibrary) -> None:
    """只写核心歌名时应通过子串匹配命中带前缀的完整歌名。"""

    info = library.find_song_info("捉迷藏")

    assert info is not None
    assert info.name == "三Z-STUDIO _ HOYO-MiX - 捉迷藏"


def test_unmatched_keyword_returns_none(library: SongLibrary) -> None:
    """歌单外的关键词应返回 None，不做随机回退。"""

    assert library.find_song_info("这首歌绝对不存在于歌库当中") is None


def test_blank_keyword_returns_none(library: SongLibrary) -> None:
    """空白关键词应返回 None（随机选曲由调用方显式发起）。"""

    assert library.find_song_info("   ") is None


def test_random_song_comes_from_library(library: SongLibrary) -> None:
    """随机选曲结果必须来自歌库。"""

    info = library.get_random_song_info()

    assert info is not None
    assert info.name in library.get_song_names()


def test_random_returns_none_for_empty_library(tmp_path: Path) -> None:
    """空歌库随机选曲应返回 None。"""

    assert SongLibrary(tmp_path).get_random_song_info() is None


def test_song_names_are_sorted(library: SongLibrary) -> None:
    """歌名列表应稳定排序，避免每次 schema 顺序抖动。"""

    names = library.get_song_names()

    assert names == sorted(names)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0.0, "0:00"), (65.0, "1:05"), (3599.4, "59:59"), (None, "?"), (-1.0, "?")],
)
def test_format_duration(seconds: float | None, expected: str) -> None:
    """时长格式化应为 ``M:SS``，无效值显示为问号。"""

    assert format_duration(seconds) == expected
