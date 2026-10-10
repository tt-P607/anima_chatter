"""anima_chatter 旧直播配置 scope 迁移测试。"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from plugins.anima_chatter.config import AnimaChatterConfig


def _write_legacy_config(
    path: Path,
    modes: str | None,
    *,
    legacy_pipeline: bool = True,
    legacy_tts: bool = True,
) -> bytes:
    """写入隔离配置并返回原始字节。"""

    mode_value = f"custom_prompt_modes = {modes}\n" if modes is not None else ""
    pipeline_fields = (
        "enabled = false\ntrigger_percent = 0.35\n"
        if legacy_pipeline
        else "song_prepare_lead_seconds = 7.5\n"
    )
    tts_section = (
        "\n[tts]\n"
        'endpoint = "http://127.0.0.1:9876/tts"\n'
        "timeout = 41.5\n"
        "max_parallel_segments = 7\n"
        if legacy_tts
        else ""
    )
    original = (
        "[plugin]\n"
        "enabled = true\n"
        'custom_prompt = "private prompt text"\n'
        f"{mode_value}"
        'model_task = "live-model-task"\n'
        'models = ["live-model"]\n'
        "temperature = 0.45\n"
        "max_tokens = 3210\n"
        f"{tts_section}"
        "\n[vts]\n"
        "enabled = true\n"
        'host = "vts-host"\n'
        "port = 9001\n"
        'audio_output_device = "device@driver"\n'
        'inst_output_device = "instrument-device"\n'
        "\n[vtb_attention]\n"
        "enabled = false\n"
        "enable_programmatic_controller = false\n"
        "\n[audio_drive]\n"
        "enabled = false\n"
        "head_y_gain = 12.5\n"
        "\n[pipelining]\n"
        f"{pipeline_fields}"
        "max_backlog_seconds = 33.0\n"
        "\n[idle_animation]\n"
        "blink_min_interval = 2.1\n"
        "blink_max_interval = 5.2\n"
    ).encode()
    path.write_bytes(original)
    return original


@pytest.mark.parametrize(
    ("modes", "expected_enabled"),
    [('["voice"]', False), ('["vtb_live"]', True), ("[]", False)],
)
def test_load_migrates_legacy_prompt_scope_and_preserves_config(
    tmp_path: Path, modes: str, expected_enabled: bool
) -> None:
    """旧模式列表决定直播提示词开关，其他配置值及原始备份保持不变。"""

    config_path = tmp_path / "config.toml"
    original_bytes = _write_legacy_config(config_path, modes)

    config = AnimaChatterConfig.load(config_path, auto_update=True)

    backup_path = tmp_path / "config.toml.anima_voice.bak"
    assert backup_path.read_bytes() == original_bytes
    assert config.plugin.custom_prompt == "private prompt text"
    assert config.plugin.custom_prompt_enabled is expected_enabled
    assert config.plugin.model_task == "live-model-task"
    assert config.plugin.models == ["live-model"]
    assert config.plugin.temperature == 0.45
    assert config.plugin.max_tokens == 3210
    assert config.vts.host == "vts-host"
    assert config.vts.port == 9001
    assert config.vts.audio_output_device == "device@driver"
    assert config.vts.inst_output_device == "instrument-device"
    assert config.vtb_attention.enabled is False
    assert config.audio_drive.head_y_gain == 12.5
    assert config.pipelining.model_dump() == {
        "song_prepare_lead_seconds": 25.0,
        "max_backlog_seconds": 33.0,
    }
    assert config.idle_animation.blink_min_interval == 2.1

    normalized = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert "custom_prompt_modes" not in normalized["plugin"]
    assert "tts" not in normalized
    assert normalized["plugin"]["custom_prompt_enabled"] is expected_enabled
    assert normalized["pipelining"] == config.pipelining.model_dump()


def test_load_without_legacy_modes_does_not_rewrite_new_config(tmp_path: Path) -> None:
    """没有旧字段的新配置保持原始文件，不触发迁移回写。"""

    config_path = tmp_path / "config.toml"
    original_bytes = _write_legacy_config(
        config_path, None, legacy_pipeline=False, legacy_tts=False
    )

    config = AnimaChatterConfig.load(config_path, auto_update=False)

    assert config.plugin.custom_prompt_enabled is True
    assert config.pipelining.song_prepare_lead_seconds == 7.5
    assert config.pipelining.max_backlog_seconds == 33.0
    assert config_path.read_bytes() == original_bytes
    assert not (tmp_path / "config.toml.anima_voice.bak").exists()


def test_repeated_load_preserves_user_changes_and_original_backup(tmp_path: Path) -> None:
    """迁移后用户修改不被重复迁移重置，原始备份保持原样。"""

    config_path = tmp_path / "config.toml"
    original_bytes = _write_legacy_config(config_path, '["vtb_live"]')
    AnimaChatterConfig.load(config_path, auto_update=True)

    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'custom_prompt = "private prompt text"',
            'custom_prompt = "later user value"',
        ),
        encoding="utf-8",
    )
    config = AnimaChatterConfig.load(config_path, auto_update=True)

    assert config.plugin.custom_prompt == "later user value"
    assert (tmp_path / "config.toml.anima_voice.bak").read_bytes() == original_bytes


def test_existing_different_backup_blocks_migration_without_changing_source(
    tmp_path: Path,
) -> None:
    """备份路径已有不同内容时拒绝迁移且不改源配置。"""

    config_path = tmp_path / "config.toml"
    original_bytes = _write_legacy_config(config_path, '["voice"]')
    backup_path = tmp_path / "config.toml.anima_voice.bak"
    backup_path.write_bytes(b"different backup")

    with pytest.raises(FileExistsError, match="config.toml.anima_voice.bak"):
        AnimaChatterConfig.load(config_path, auto_update=True)

    assert config_path.read_bytes() == original_bytes
    assert backup_path.read_bytes() == b"different backup"


def test_backup_write_failure_leaves_source_unchanged(tmp_path: Path) -> None:
    """备份路径不可用时加载失败且不改源配置。"""

    config_path = tmp_path / "config.toml"
    original_bytes = _write_legacy_config(config_path, '["voice"]')
    (tmp_path / "config.toml.anima_voice.bak").mkdir()

    with pytest.raises((IsADirectoryError, PermissionError)):
        AnimaChatterConfig.load(config_path, auto_update=True)

    assert config_path.read_bytes() == original_bytes


def test_config_defaults_include_round_capacity() -> None:
    """直播默认值包含歌曲尾段窗口与音频容量上限。"""

    config = AnimaChatterConfig()

    assert config.plugin.tick_interval == 1.0
    assert config.vts.enabled is False
    assert config.vtb_attention.enabled is True
    assert config.audio_drive.head_y_gain == 8.0
    assert config.pipelining.song_prepare_lead_seconds == 25.0
    assert config.pipelining.max_backlog_seconds == 60.0
    assert config.idle_animation.blink_min_interval == 1.8
    assert config.plugin.custom_prompt_enabled is True