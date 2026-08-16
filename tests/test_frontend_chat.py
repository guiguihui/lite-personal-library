from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest


def test_chat_uses_backend_search_api_only() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    root = Path(__file__).resolve().parent.parent
    subprocess.run(
        [node, "--check", str(root / "frontend/chat/agent.js")],
        check=True,
    )
    subprocess.run(
        [node, str(root / "tests/frontend/chat-search-api.test.js")],
        check=True,
    )
