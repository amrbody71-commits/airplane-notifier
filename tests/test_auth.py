"""U2 -- OAuth flow and token persistence (R1, R2, R3).

The Google client objects are mocked throughout: these tests are about our
branching (load / refresh / re-authorize / fail), not about Google's library.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

import pytest
from google.auth.exceptions import RefreshError

from airplanenotifier import auth, paths


def _fake_creds(valid=True, expired=False, refresh_token="refresh-me"):
    creds = MagicMock()
    creds.valid = valid
    creds.expired = expired
    creds.refresh_token = refresh_token
    creds.to_json.return_value = json.dumps({"token": "abc", "refresh_token": refresh_token})
    return creds


@pytest.fixture
def credentials_file(tmp_path, monkeypatch):
    """A stand-in credentials.json so the flow is reachable."""
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"installed": {"client_id": "x", "client_secret": "y"}}))
    monkeypatch.setattr(auth, "credentials_path", lambda: path)
    return path


def test_first_run_with_no_token_runs_the_browser_flow(config_dir, credentials_file):
    """Scenario 1: no token file -> consent flow, token saved."""
    creds = _fake_creds()
    flow = MagicMock()
    flow.run_local_server.return_value = creds

    with patch.object(auth, "InstalledAppFlow") as flow_cls:
        flow_cls.from_client_secrets_file.return_value = flow
        result = auth.get_credentials()

    assert result is creds
    assert paths.TOKEN_PATH.exists()
    assert json.loads(paths.TOKEN_PATH.read_text())["token"] == "abc"


def test_flow_uses_an_ephemeral_port(config_dir, credentials_file):
    """P1 fix: port=0 lets the OS pick, so a busy 8080 cannot break auth."""
    flow = MagicMock()
    flow.run_local_server.return_value = _fake_creds()

    with patch.object(auth, "InstalledAppFlow") as flow_cls:
        flow_cls.from_client_secrets_file.return_value = flow
        auth.get_credentials()

    assert flow.run_local_server.call_args.kwargs["port"] == 0


def test_valid_saved_token_skips_the_browser(config_dir, credentials_file):
    """Scenario 2: a valid token is reused, no browser opens."""
    paths.TOKEN_PATH.write_text(json.dumps({"token": "saved"}))
    creds = _fake_creds(valid=True)

    with patch.object(auth, "Credentials") as creds_cls, \
            patch.object(auth, "InstalledAppFlow") as flow_cls:
        creds_cls.from_authorized_user_file.return_value = creds
        result = auth.get_credentials()

    assert result is creds
    flow_cls.from_client_secrets_file.assert_not_called()


def test_expired_token_with_refresh_token_refreshes_silently(config_dir, credentials_file):
    """Scenario 3: refresh instead of re-prompting, and persist the result."""
    paths.TOKEN_PATH.write_text(json.dumps({"token": "old"}))
    creds = _fake_creds(valid=False, expired=True)

    with patch.object(auth, "Credentials") as creds_cls, \
            patch.object(auth, "InstalledAppFlow") as flow_cls:
        creds_cls.from_authorized_user_file.return_value = creds
        result = auth.get_credentials()

    creds.refresh.assert_called_once()
    flow_cls.from_client_secrets_file.assert_not_called()
    assert result is creds
    assert json.loads(paths.TOKEN_PATH.read_text())["token"] == "abc"


def test_expired_token_without_refresh_token_reauthorizes(config_dir, credentials_file):
    """Scenario 4: nothing to refresh with -> fall through to the flow."""
    paths.TOKEN_PATH.write_text(json.dumps({"token": "old"}))
    stale = _fake_creds(valid=False, expired=True, refresh_token=None)
    fresh = _fake_creds()
    flow = MagicMock()
    flow.run_local_server.return_value = fresh

    with patch.object(auth, "Credentials") as creds_cls, \
            patch.object(auth, "InstalledAppFlow") as flow_cls:
        creds_cls.from_authorized_user_file.return_value = stale
        flow_cls.from_client_secrets_file.return_value = flow
        result = auth.get_credentials()

    assert result is fresh


def test_revoked_refresh_token_falls_back_to_the_flow(config_dir, credentials_file):
    """P1 fix: RefreshError must re-authorize, not crash the app."""
    paths.TOKEN_PATH.write_text(json.dumps({"token": "old"}))
    revoked = _fake_creds(valid=False, expired=True)
    revoked.refresh.side_effect = RefreshError("token has been revoked")
    fresh = _fake_creds()
    flow = MagicMock()
    flow.run_local_server.return_value = fresh

    with patch.object(auth, "Credentials") as creds_cls, \
            patch.object(auth, "InstalledAppFlow") as flow_cls:
        creds_cls.from_authorized_user_file.return_value = revoked
        flow_cls.from_client_secrets_file.return_value = flow
        result = auth.get_credentials()

    assert result is fresh
    flow_cls.from_client_secrets_file.assert_called_once()


def test_corrupt_token_file_is_not_fatal(config_dir, credentials_file):
    """A truncated token must re-authorize rather than raise on startup."""
    paths.TOKEN_PATH.write_text("{not json")
    fresh = _fake_creds()
    flow = MagicMock()
    flow.run_local_server.return_value = fresh

    with patch.object(auth, "InstalledAppFlow") as flow_cls:
        flow_cls.from_client_secrets_file.return_value = flow
        result = auth.get_credentials()

    assert result is fresh


def test_missing_credentials_file_raises_filenotfound(config_dir, tmp_path, monkeypatch):
    """Scenario 5: the caller shows a tray balloon for this specific error."""
    monkeypatch.setattr(auth, "credentials_path", lambda: tmp_path / "nope.json")

    with pytest.raises(FileNotFoundError):
        auth.get_credentials()


def test_clear_credentials_removes_the_token(config_dir):
    """Scenario 7: re-authorize wipes the token first."""
    paths.TOKEN_PATH.write_text("{}")
    auth.clear_credentials()
    assert not paths.TOKEN_PATH.exists()


def test_clear_credentials_is_safe_when_absent(config_dir):
    auth.clear_credentials()  # must not raise


def test_has_credentials_reports_token_presence(config_dir):
    assert auth.has_credentials() is False
    paths.TOKEN_PATH.write_text("{}")
    assert auth.has_credentials() is True


def test_frozen_build_resolves_assets_from_meipass(monkeypatch, tmp_path):
    """Scenario 6: PyInstaller unpacks bundled data under sys._MEIPASS (R3)."""
    monkeypatch.setattr("sys._MEIPASS", str(tmp_path), raising=False)
    assert paths.asset_path("airplane.png") == tmp_path / "assets" / "airplane.png"


def test_frozen_build_prefers_credentials_beside_the_executable(monkeypatch, tmp_path):
    """Review item 7: the secret is not baked into the distributed build."""
    exe_dir = tmp_path / "dist"
    exe_dir.mkdir()
    (exe_dir / "credentials.json").write_text("{}")

    monkeypatch.setattr("sys._MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "airplane-notifier.exe"))

    assert paths.credentials_path() == exe_dir / "credentials.json"


def test_frozen_build_falls_back_to_the_bundled_copy(monkeypatch, tmp_path):
    """Bundling still works for anyone who prefers a single self-contained folder."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "credentials.json").write_text("{}")
    exe_dir = tmp_path / "dist"
    exe_dir.mkdir()

    monkeypatch.setattr("sys._MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "airplane-notifier.exe"))

    assert paths.credentials_path() == bundle / "credentials.json"


def test_frozen_build_points_at_the_exe_directory_when_nothing_is_found(monkeypatch, tmp_path):
    """The error message must name somewhere the user can actually put the file."""
    exe_dir = tmp_path / "dist"
    exe_dir.mkdir()
    monkeypatch.setattr("sys._MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "airplane-notifier.exe"))

    assert paths.credentials_path() == exe_dir / "credentials.json"
