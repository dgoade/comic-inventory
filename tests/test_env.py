import os

from comic_inventory.db import load_project_env


def test_load_project_env_sets_missing_values_and_keeps_existing(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# comment",
                "INVENTORY_TEST_FROM_FILE=from-file",
                "INVENTORY_TEST_KEEP=from-file",
                'INVENTORY_TEST_QUOTED="hash#not-comment"',
                "INVENTORY_TEST_BRACE=abc{def",
                "",
            ]
        )
    )
    monkeypatch.delenv("INVENTORY_TEST_FROM_FILE", raising=False)
    monkeypatch.setenv("INVENTORY_TEST_KEEP", "from-shell")
    monkeypatch.delenv("INVENTORY_TEST_QUOTED", raising=False)
    monkeypatch.delenv("INVENTORY_TEST_BRACE", raising=False)

    assert load_project_env(env_file) is True
    assert os.environ["INVENTORY_TEST_FROM_FILE"] == "from-file"
    assert os.environ["INVENTORY_TEST_KEEP"] == "from-shell"
    assert os.environ["INVENTORY_TEST_QUOTED"] == "hash#not-comment"
    assert os.environ["INVENTORY_TEST_BRACE"] == "abc{def"


def test_load_project_env_missing_file_is_ok(tmp_path):
    assert load_project_env(tmp_path / "missing.env") is False
