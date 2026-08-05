import shutil
import subprocess
from pathlib import Path

import pytest


def test_frontend_knowledge_scripts() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    root = Path(__file__).resolve().parent.parent
    # LQD-fin 采用 LQ-D-desktop 的 3-pane library.js 架构,不搬运 norag-dev 的
    # session.js/reader.js(per-tab session 机制与 A 的 library 冲突)。因此这里
    # 只校验本合并项目实际存在并使用的知识链接脚本。
    scripts = [
        root / "frontend/shared/wikilinks.js",
        root / "frontend/shared/link-popover.js",
        root / "frontend/library/local-graph.js",
        root / "frontend/library/knowledge.js",
        root / "frontend/library/index.js",
        root / "frontend/core/tab-ids.js",
        root / "frontend/core/tabs.js",
        root / "frontend/core/shell.js",
        root / "frontend/library/open-doc.js",
    ]
    for script in scripts:
        subprocess.run([node, "--check", str(script)], check=True)
    subprocess.run([node, "--test", str(root / "tests/frontend/wikilinks.test.js")], check=True)
    subprocess.run([node, "--test", str(root / "tests/frontend/tab-ids.test.js")], check=True)
    subprocess.run([node, "--test", str(root / "tests/frontend/chat-search-api.test.js")], check=True)
