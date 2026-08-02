import shutil
import subprocess
from pathlib import Path

import pytest


def test_frontend_knowledge_scripts() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    root = Path(__file__).resolve().parent.parent
    scripts = [
        root / "frontend/shared/wikilinks.js",
        root / "frontend/shared/link-popover.js",
        root / "frontend/library/local-graph.js",
        root / "frontend/library/knowledge.js",
        root / "frontend/library/session.js",
        root / "frontend/library/reader.js",
        root / "frontend/library/index.js",
        root / "frontend/core/tab-ids.js",
        root / "frontend/core/tabs.js",
        root / "frontend/core/shell.js",
        root / "frontend/library/open-doc.js",
    ]
    for script in scripts:
        subprocess.run([node, "--check", str(script)], check=True)
    subprocess.run([node, "--test", str(root / "tests/frontend/wikilinks.test.js")], check=True)
    subprocess.run([node, "--test", str(root / "tests/frontend/library-session.test.js")], check=True)
    subprocess.run([node, "--test", str(root / "tests/frontend/tab-ids.test.js")], check=True)
