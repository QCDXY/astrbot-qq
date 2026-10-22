from __future__ import annotations

import importlib.util
import shutil
import tempfile
from pathlib import Path

import pytest

from tests.support.astrbot_harness import AstrBotProcess
from tests.support.mock_llm import ThreadedMockLLM

if importlib.util.find_spec("astrbot") is None:
    pytest.skip("未安装 astrbot（uv sync 后再运行集成测试）", allow_module_level=True)

TOKEN = "it-token-5f2c9d1e8a7b4c3d"
LLM_KEY = "test-llm-key"
BOT_ID = 10001
ADMIN_ID = 99999
ALLOWED_GROUP = 777
OTHER_GROUP = 888
USER_LIMIT = 3

PLUGIN_CONFIG = {
    "group_allowlist": [str(ALLOWED_GROUP)],
    "user_rate_limit_count": USER_LIMIT,
    "user_rate_limit_window": 60,
    "notify_on_limit": True,
}


def bot_env(llm_base_url: str, key: str = LLM_KEY) -> dict[str, str]:
    return {
        "ONEBOT_ACCESS_TOKEN": TOKEN,
        "LLM_API_KEY": key,
        "LLM_BASE_URL": llm_base_url,
        "LLM_MODEL": "mock-model",
        "ADMIN_QQ": str(ADMIN_ID),
    }


@pytest.fixture(scope="session")
def mock_llm():
    with ThreadedMockLLM(expected_key=LLM_KEY) as llm:
        yield llm


@pytest.fixture
def tmp_root():
    root = Path(tempfile.mkdtemp(prefix="astrbot-it-"))
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="module")
def astrbot(mock_llm):
    """模块内共享一个 AstrBot 进程（启动约 30 秒）。"""
    root = Path(tempfile.mkdtemp(prefix="astrbot-it-"))
    bot = AstrBotProcess(root, bot_env(mock_llm.base_url), plugin_config=PLUGIN_CONFIG)
    bot.prepare()
    bot.start()
    yield bot
    bot.stop()
    shutil.rmtree(root, ignore_errors=True)
