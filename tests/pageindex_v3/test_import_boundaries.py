from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from app.index.v3.protocol import encode_request_line
from app.index.v3.worker import execute_request

from .test_worker_pipeline import _corpus, _parent, _request


_HEAVY_MODULES = (
    "app.index.v3.base_builder",
    "app.index.v3.delta_builder",
    "app.index.v3.validator",
    "app.vendor.build_pageindex",
)


def _run_fresh(script: str, *arguments: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script, *(str(value) for value in arguments)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )



def test_no_op_worker_does_not_import_build_modules(tmp_path: Path) -> None:
    content = _corpus(tmp_path)
    pageindex = tmp_path / "pageindex"
    bootstrap = execute_request(_request(content, pageindex, "idx_import_seed"))
    assert bootstrap.state == "ready_to_publish"

    request = _request(
        content,
        pageindex,
        "idx_import_no_op",
        parent=_parent(bootstrap),
    )
    request_path = pageindex / "build" / request.job_id / "request.json"
    request_path.parent.mkdir(parents=True)
    request_path.write_bytes(encode_request_line(request))
    blocked = repr(_HEAVY_MODULES)
    script = (
        "import sys; from pathlib import Path; import app.index.v3; "
        f"blocked={blocked}; "
        "loaded=[name for name in blocked if name in sys.modules]; "
        "assert not loaded, loaded; "
        "from app.index.v3.worker import EXIT_SUCCESS, run_worker; "
        "loaded=[name for name in blocked if name in sys.modules]; "
        "assert not loaded, loaded; "
        "result=run_worker(Path(sys.argv[1])); "
        "assert result == EXIT_SUCCESS, result; "
        "loaded=[name for name in blocked if name in sys.modules]; "
        "assert not loaded, loaded"
    )
    completed = _run_fresh(script, request_path)
    assert completed.returncode == 0, completed.stderr
