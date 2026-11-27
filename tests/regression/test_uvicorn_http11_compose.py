"""R-P4 / T-C4: compose.override.yaml api command includes h11."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_OVERRIDE = REPO_ROOT / "compose.override.yaml"


def test_compose_override_api_command_includes_h11() -> None:
    contents = COMPOSE_OVERRIDE.read_text(encoding="utf-8")

    assert "uvicorn" in contents
    assert "--http" in contents
    assert "h11" in contents

    # Tokens must appear in the api service command that pins HTTP/1.1.
    cmd_idx = contents.index("uvicorn")
    http_idx = contents.index("--http", cmd_idx)
    h11_idx = contents.index("h11", http_idx)
    assert http_idx < h11_idx
