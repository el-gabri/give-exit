"""R-P3: Dockerfile CMD must pin uvicorn to HTTP/1.1 via --http h11."""

from pathlib import Path


def test_dockerfile_cmd_includes_http_h11() -> None:
    dockerfile = Path(__file__).resolve().parents[2] / "Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")

    assert "--http" in text
    assert "h11" in text

    # Prefer the tokens near the uvicorn CMD that serves the API.
    assert "uvicorn" in text
    cmd_idx = text.index("uvicorn")
    http_idx = text.index("--http", cmd_idx)
    h11_idx = text.index("h11", http_idx)
    assert http_idx < h11_idx
