"""Both GUI workspaces use the same masked local credential workflow."""

from unittest.mock import patch

from core.shared.config import AppConfig
from ui.shared.credentials import get_or_request_api_key


def test_existing_credential_does_not_prompt(tmp_path):
    with patch("ui.shared.credentials.load_environment"), \
         patch("ui.shared.credentials.get_api_key", return_value="synthetic"), \
         patch("ui.shared.credentials.simpledialog.askstring") as prompt:
        assert get_or_request_api_key(AppConfig(), tmp_path, None) == "synthetic"
        prompt.assert_not_called()


def test_cancel_does_not_write_credential(tmp_path):
    with patch("ui.shared.credentials.load_environment"), \
         patch("ui.shared.credentials.get_api_key", return_value=None), \
         patch("ui.shared.credentials.simpledialog.askstring", return_value=None), \
         patch("ui.shared.credentials.save_api_key") as save:
        assert get_or_request_api_key(AppConfig(), tmp_path, None) is None
        save.assert_not_called()


def test_new_credential_is_masked_and_saved_locally(tmp_path):
    config = AppConfig()
    with patch("ui.shared.credentials.load_environment"), \
         patch("ui.shared.credentials.get_api_key", return_value=None), \
         patch("ui.shared.credentials.simpledialog.askstring", return_value="synthetic") as prompt, \
         patch("ui.shared.credentials.save_api_key", return_value="synthetic") as save:
        assert get_or_request_api_key(config, tmp_path, None) == "synthetic"
        assert prompt.call_args.kwargs["show"] == "*"
        save.assert_called_once_with(config, "synthetic", env_path=tmp_path / ".env")
