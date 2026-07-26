"""anima_chatter 测试套件的公共 fixture 与路径设置。

把项目根加入 ``sys.path``，让测试可以按 ``plugins.anima_chatter.*`` 的绝对路径
导入被测模块（测试文件不属于插件包内部，不受"插件内必须相对导入"的约束）。
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


@pytest.fixture(autouse=True)
async def _reset_runtime_state() -> AsyncIterator[None]:
    """每个用例前后清空模块级运行时状态，避免用例之间互相污染。

    Yields:
        ``None``——只做状态清理，不产出值。
    """

    from plugins.anima_chatter.runtime import call_state, pipeline_state, sung_history

    await call_state.clear_active_call()
    await pipeline_state.clear_all()
    await sung_history.clear()
    yield
    await call_state.clear_active_call()
    await pipeline_state.clear_all()
    await sung_history.clear()
