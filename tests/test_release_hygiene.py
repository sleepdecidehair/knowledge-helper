import stat
import subprocess
from pathlib import Path

from app import config


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_gitignore_excludes_local_data_but_not_the_directory_marker():
    rules = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "data/*\n" in rules
    assert "!data/.gitkeep\n" in rules
    assert (PROJECT_ROOT / "data" / ".gitkeep").is_file()

    ignored_data = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", "data/agent_conversations.json"],
        cwd=PROJECT_ROOT,
        check=False,
    )
    kept_marker = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", "data/.gitkeep"],
        cwd=PROJECT_ROOT,
        check=False,
    )

    assert ignored_data.returncode == 0
    assert kept_marker.returncode == 1


def test_harden_env_permissions_makes_owner_the_only_reader(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("DEEPSEEK_API_KEY=example\n", encoding="utf-8")
    env_path.chmod(0o644)

    config.harden_env_permissions(env_path)

    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
