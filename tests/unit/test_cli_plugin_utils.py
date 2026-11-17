from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest

from bot.cli.utils.plugin import PluginStatus, build_plug_list, download_repository


def test_download_repository_uses_head_without_metadata_lookup(
    monkeypatch, tmp_path, capsys
):
    archive = BytesIO()
    with ZipFile(archive, "w") as zip_file:
        zip_file.writestr("plugin-commit/main.py", "VALUE = 1\n")
    requested_urls = []

    class ArchiveResponse:
        content = archive.getvalue()

        def raise_for_status(self):
            return None

    class ArchiveClient:
        def __init__(self, **kwargs):
            assert kwargs == {"follow_redirects": True, "trust_env": True}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url):
            requested_urls.append(url)
            return ArchiveResponse()

    monkeypatch.setattr("bot.cli.utils.plugin.httpx.Client", ArchiveClient)

    target_path = tmp_path / "plugin"
    download_repository("https://github.com/example/plugin", target_path)

    assert requested_urls == ["https://github.com/example/plugin/archive/HEAD.zip"]
    assert (target_path / "main.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert "default reference HEAD" in capsys.readouterr().out


def write_metadata(plugin_dir: Path, name: str, version: str) -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    plugin_dir.joinpath("metadata.yaml").write_text(
        f"""
name: {name}
desc: local description
version: {version}
author: local-author
repo: https://example.com/{name}
""".strip(),
        encoding="utf-8",
    )


def test_build_plug_list_returns_local_plugins(tmp_path):
    write_metadata(tmp_path / "local-plugin", "local-plugin", "1.0.0")
    tmp_path.joinpath("ignored-file").write_text("not a plugin", encoding="utf-8")

    plugins = build_plug_list(tmp_path)
    plugins_by_name = {plugin["name"]: plugin for plugin in plugins}

    assert list(plugins_by_name) == ["local-plugin"]
    assert plugins_by_name["local-plugin"]["status"] == PluginStatus.INSTALLED
    assert plugins_by_name["local-plugin"]["local_path"].endswith("local-plugin")


@pytest.mark.parametrize("path_kind", ["file", "missing"])
def test_build_plug_list_treats_unusable_path_as_empty_local_set(tmp_path, path_kind):
    plugins_path = tmp_path / "plugins"
    if path_kind == "file":
        plugins_path.write_text("not a directory", encoding="utf-8")
    else:
        plugins_path = tmp_path / "completely_non_existent_path"

    plugins = build_plug_list(plugins_path)

    assert plugins == []


def test_build_plug_list_skips_metadata_without_repo(tmp_path):
    plugin_dir = tmp_path / "incomplete-plugin"
    plugin_dir.mkdir()
    plugin_dir.joinpath("metadata.yaml").write_text(
        "name: incomplete\ndesc: missing repo\nversion: 1.0.0\nauthor: a\n",
        encoding="utf-8",
    )

    assert build_plug_list(tmp_path) == []
