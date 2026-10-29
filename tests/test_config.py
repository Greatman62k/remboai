"""Settings must read the same values however they arrive: from a .env file (python-dotenv) or from Docker's env_file, which keeps `# text` as part of the value."""
from pathlib import Path

from app.config import Settings

ROOT = Path(__file__).resolve().parent.parent


def test_inline_comments_are_not_part_of_a_value(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db/x")
    monkeypatch.setenv("PUBLIC_BASE_URL", "# https://your-domain, reachable by Safaricom")      # what Docker passes for `KEY=   # note`
    monkeypatch.setenv("MPESA_ENV", "sandbox   # sandbox | production")
    monkeypatch.setenv("TELEGRAM_MODE", "auto  # auto | poll | webhook")
    monkeypatch.setenv("ASR_URL", "https://x.modal.run/transcribe")
    s = Settings(_env_file=None)
    assert (s.public_base_url, s.mpesa_env, s.telegram_mode, s.asr_url) == ("", "sandbox", "auto", "https://x.modal.run/transcribe")


def test_a_hash_inside_a_value_is_kept(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:pa#ss@db/x")
    monkeypatch.setenv("LLM_API_KEY", "abc#def")
    s = Settings(_env_file=None)
    assert s.database_url == "postgresql://u:pa#ss@db/x" and s.llm_api_key == "abc#def"        # only ` #` after whitespace starts a comment


def test_the_example_env_file_has_no_value_lines_with_trailing_comments():
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            assert " #" not in line, f"move this comment to its own line: {line}"
