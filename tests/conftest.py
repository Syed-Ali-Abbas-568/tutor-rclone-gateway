"""Shared fixtures.

These tests drive Tutor's real plugin-loading and template-rendering
pipeline rather than a stand-in, so that a break in the way Tutor
discovers this plugin shows up here rather than at ``tutor local
launch``.
"""

from __future__ import annotations

import os
import tempfile
import typing as t

import pytest
from tutor import config as tutor_config
from tutor import env as tutor_env
from tutor import hooks as tutor_hooks
from tutor import plugins

PATCHES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tutorrclonegateway",
    "patches",
)


def patch_names() -> list[str]:
    return sorted(os.listdir(PATCHES_DIR))


def read_patch(name: str) -> str:
    with open(os.path.join(PATCHES_DIR, name), encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="session", autouse=True)
def _loaded_plugin() -> None:
    """Discover and enable the plugin exactly as the Tutor CLI does.

    This exercises the ``tutor.plugin.v1`` entry point, so a typo in
    pyproject.toml fails the suite.
    """
    tutor_hooks.Actions.CORE_READY.do()
    assert "rclonegateway" in list(plugins.iter_installed()), (
        "the 'rclonegateway' plugin is not installed; run `pip install -e .` first"
    )
    plugins.load_all(["rclonegateway"])


@pytest.fixture(scope="session")
def config() -> t.Iterator[dict[str, t.Any]]:
    """A fully rendered Tutor config with this plugin enabled."""
    with tempfile.TemporaryDirectory() as root:
        cfg = tutor_config.load_full(root)
        tutor_config.render_full(cfg)
        yield cfg


@pytest.fixture(scope="session")
def renderer(config: dict[str, t.Any]) -> tutor_env.Renderer:
    return tutor_env.Renderer(config)


@pytest.fixture(scope="session")
def rendered(
    renderer: tutor_env.Renderer,
) -> dict[str, str]:
    """Every patch, rendered against the default config."""
    return {name: renderer.render_str(read_patch(name)) for name in patch_names()}
