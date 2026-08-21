"""The Teams app manifest that installs the notification-only bot."""

import json
from pathlib import Path

MANIFEST = json.loads(
    (Path(__file__).resolve().parent.parent / "teams-app" / "manifest.json").read_text()
)


def test_manifest_declares_a_single_notification_only_team_bot():
    bots = MANIFEST["bots"]

    assert len(bots) == 1
    assert bots[0]["scopes"] == ["team"]
    assert bots[0]["isNotificationOnly"] is True
    assert bots[0]["supportsFiles"] is False


def test_manifest_id_and_bot_id_are_the_same_placeholder():
    assert MANIFEST["id"] == "REPLACE_WITH_BOT_APP_ID"
    assert MANIFEST["bots"][0]["botId"] == "REPLACE_WITH_BOT_APP_ID"


def test_manifest_carries_the_required_developer_metadata():
    developer = MANIFEST["developer"]

    assert developer["name"] == "Sentasity"
    assert developer["websiteUrl"] == "https://sentasity.com"
    assert developer["privacyUrl"] == "https://sentasity.com/privacy"
    assert developer["termsOfUseUrl"] == "https://sentasity.com/terms"


def test_manifest_references_both_icon_files():
    assert MANIFEST["icons"] == {"color": "color.png", "outline": "outline.png"}


import os
import shutil
import subprocess

TEAMS_APP = Path(__file__).resolve().parent.parent / "teams-app"


def staged_build(tmp_path, env):
    """Run the build script against a copy, so committed icons cannot skew it."""
    for name in ("manifest.json", "build-package.sh"):
        shutil.copy2(TEAMS_APP / name, tmp_path / name)
    os.chmod(tmp_path / "build-package.sh", 0o755)
    return subprocess.run(
        [str(tmp_path / "build-package.sh")],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], **env},
        check=False,
    )


def test_build_refuses_without_a_bot_app_id(tmp_path):
    result = staged_build(tmp_path, {})

    assert result.returncode == 1
    assert "BOT_APP_ID is unset" in result.stderr


def test_build_refuses_when_the_icons_are_missing(tmp_path):
    result = staged_build(tmp_path, {"BOT_APP_ID": "app-456"})

    assert result.returncode == 1
    assert "color.png" in result.stderr
