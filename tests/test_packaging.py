"""Packaging metadata smoke tests."""
import importlib
import tomllib


def test_console_script_targets_are_importable_callables():
    with open("pyproject.toml", "rb") as fh:
        data = tomllib.load(fh)

    scripts = data["project"]["scripts"]

    assert scripts == {
        "ia-config": "ia_config_cli:main",
        "ia-dl": "ia_dl:main",
        "ia-easy": "ia_easy:cli_main",
        "ia-minotaur": "ia_minotaur:cli_main",
    }

    for target in scripts.values():
        module_name, attr = target.split(":", 1)
        module = importlib.import_module(module_name)
        assert callable(getattr(module, attr))


def test_packaging_includes_all_flat_modules():
    with open("pyproject.toml", "rb") as fh:
        data = tomllib.load(fh)

    modules = set(data["tool"]["setuptools"]["py-modules"])

    assert {
        "ia_api",
        "ia_config",
        "ia_config_cli",
        "ia_common",
        "ia_dl",
        "ia_downloads",
        "ia_easy",
        "ia_minotaur",
        "ia_organize",
        "ia_paths",
        "ia_state",
    } <= modules
