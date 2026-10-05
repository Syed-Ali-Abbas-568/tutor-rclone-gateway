"""Configuration and hook registration."""

from __future__ import annotations

import typing as t

import pytest
from tutor import exceptions as tutor_exceptions
from tutor import hooks as tutor_hooks

from tutorrclonegateway.plugin import check_plugin_conflict

# Keys this plugin is expected to define. Kept explicit rather than
# derived from plugin.py so that accidentally dropping (or adding) one
# fails. Unlike tutor-rustfs there is no CONSOLE_HOST (rclone has no web
# console) and no second "client" image key (rclone's own CLI is its own
# admin client).
EXPECTED_KEYS = {
    "RCLONEGW_VERSION",
    "RCLONEGW_BUCKET_NAME",
    "RCLONEGW_FILE_UPLOAD_BUCKET_NAME",
    "RCLONEGW_VIDEO_UPLOAD_BUCKET_NAME",
    "RCLONEGW_GRADES_BUCKET_NAME",
    "RCLONEGW_OPENEDX_LEARNING_BUCKET_NAME",
    "RCLONEGW_DISCOVERY_BUCKET_NAME",
    "RCLONEGW_HOST",
    "RCLONEGW_REGION",
    "RCLONEGW_QUERYSTRING_AUTH",
    "RCLONEGW_DOCKER_IMAGE",
    "RCLONEGW_UID",
    "RCLONEGW_GID",
    "RCLONEGW_AZURE_ACCOUNT",
    "RCLONEGW_AZURE_ACCOUNT_KEY",
    "RCLONEGW_AZURE_ENDPOINT",
    "RCLONEGW_AZURITE_DOCKER_IMAGE",
    "RCLONEGW_AWS_SECRET_ACCESS_KEY",
}


def test_all_expected_keys_are_defined(config: dict[str, t.Any]) -> None:
    missing = EXPECTED_KEYS - set(config)
    assert not missing, f"missing config keys: {sorted(missing)}"


def test_no_unexpected_rclonegw_keys(config: dict[str, t.Any]) -> None:
    """Catches both a dropped key and an accidentally-added one (e.g. a
    stray second "client image" key that would imply, wrongly, that this
    plugin needs one)."""
    actual = {k for k in config if k.startswith("RCLONEGW_")}
    assert actual == EXPECTED_KEYS, (
        f"unexpected: {sorted(actual - EXPECTED_KEYS)}, "
        f"missing: {sorted(EXPECTED_KEYS - actual)}"
    )


def test_no_minio_keys_leak(config: dict[str, t.Any]) -> None:
    leaked = sorted(k for k in config if k.startswith("MINIO_"))
    assert not leaked, f"MINIO_-prefixed config keys leaked in: {leaked}"


def test_no_rustfs_keys_leak(config: dict[str, t.Any]) -> None:
    leaked = sorted(k for k in config if k.startswith("RUSTFS_"))
    assert not leaked, f"RUSTFS_-prefixed config keys leaked in: {leaked}"


def test_plugin_config_is_namespaced(config: dict[str, t.Any]) -> None:
    """Every key this plugin adds must be RCLONEGW_-prefixed.

    tutor-minio shipped an unprefixed ``MC_DOCKER_IMAGE`` which polluted
    the global Tutor namespace. Guard against repeating that.
    """
    unprefixed = {
        "DOCKER_IMAGE",
        "BUCKET_NAME",
        "REGION",
        "UID",
        "GID",
        "AZURE_ACCOUNT",
        "AZURE_ACCOUNT_KEY",
        "AZURE_ENDPOINT",
        "AZURITE_DOCKER_IMAGE",
    }
    collisions = sorted(unprefixed & set(config))
    assert not collisions, f"unprefixed keys pollute the global namespace: {collisions}"


def test_no_console_host_setting(config: dict[str, t.Any]) -> None:
    """rclone has no web console; a setting implying otherwise misleads."""
    assert "RCLONEGW_CONSOLE_HOST" not in config


def test_openedx_credentials_are_wired(config: dict[str, t.Any]) -> None:
    assert config["OPENEDX_AWS_ACCESS_KEY"] == "openedx"
    secret = config["OPENEDX_AWS_SECRET_ACCESS_KEY"]
    assert secret == config["RCLONEGW_AWS_SECRET_ACCESS_KEY"]
    assert len(str(secret)) == 24, "secret should be a generated 24-char string"
    assert "{{" not in str(secret), "secret was not rendered"


def test_host_derives_from_lms_host(config: dict[str, t.Any]) -> None:
    lms = config["LMS_HOST"]
    assert config["RCLONEGW_HOST"] == f"files.{lms}"


def test_region_default(config: dict[str, t.Any]) -> None:
    assert config["RCLONEGW_REGION"] == "us-east-1"


def test_docker_image_is_pinned_exactly(config: dict[str, t.Any]) -> None:
    """A floating '>=' range would decouple whatever gets deployed later
    from whatever this plugin's live conformance suite actually tested —
    rclone's multipart-streaming code was rewritten in 1.75.0 and needed
    a CVE fix (CVE-2026-88045) one release later, so "what we tested"
    must equal "what ships" here more than it would for typical plugin
    pins."""
    assert config["RCLONEGW_DOCKER_IMAGE"] == "docker.io/rclone/rclone:1.75.1"


def test_azurite_defaults_are_internally_consistent(config: dict[str, t.Any]) -> None:
    assert config["RCLONEGW_AZURE_ACCOUNT"] in config["RCLONEGW_AZURE_ENDPOINT"]
    assert config["RCLONEGW_AZURE_ACCOUNT"] == "devstoreaccount1"


def test_init_task_registered_for_rclonegw_service() -> None:
    tasks = dict(tutor_hooks.Filters.CLI_DO_INIT_TASKS.iterate())
    assert "rclonegw" in tasks, f"no init task for 'rclonegw'; got {sorted(tasks)}"
    script = tasks["rclonegw"]
    assert "rclone mkdir" in script


def test_init_task_has_no_bucket_policy_step() -> None:
    """Unlike RustFS/MinIO's `rc`/`mc`, there is nothing here resembling
    a bucket-policy *command*: anonymous read access to the public bucket
    is a property of the two-process gateway topology (see the caddyfile
    and local-docker-compose-services patches), never a flag set on the
    bucket itself. Comments are free to discuss this in English (and do);
    only command lines are checked here."""
    script = dict(tutor_hooks.Filters.CLI_DO_INIT_TASKS.iterate())["rclonegw"]
    command_lines = [
        line
        for line in script.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    for forbidden in ("policy", "anonymous", "acl"):
        offending = [line for line in command_lines if forbidden in line.lower()]
        assert not offending, (
            f"init.sh has a command referencing {forbidden!r}: {offending}; "
            "there is no such mechanism for this backend, and anything "
            "matching this probably means the two-process public/private "
            "design was bypassed"
        )


def test_refuses_to_run_alongside_tutor_minio() -> None:
    with pytest.raises(tutor_exceptions.TutorError, match="cannot be enabled"):
        check_plugin_conflict("minio")


def test_refuses_to_run_alongside_tutor_rustfs() -> None:
    with pytest.raises(tutor_exceptions.TutorError, match="cannot be enabled"):
        check_plugin_conflict("rustfs")


def test_other_plugins_do_not_trigger_conflict() -> None:
    check_plugin_conflict("discovery")
    check_plugin_conflict("mfe")
