from __future__ import annotations

import os
import typing as t
from glob import glob

import importlib_resources
from tutor import exceptions as tutor_exceptions
from tutor import hooks as tutor_hooks
from tutor.__about__ import __version_suffix__

from .__about__ import __version__

# Handle version suffix in main mode, just like tutor core
if __version_suffix__:
    __version__ += "-" + __version_suffix__

HERE = os.path.abspath(os.path.dirname(__file__))

# -----------------------------------------------------------------------------
# rclone as an S3-to-Azure-Blob-Storage gateway for Open edX.
#
# MinIO used to offer a "gateway" mode that translated the S3 API onto
# external backends, including Azure Blob Storage (which has no S3 API of
# its own). MinIO deprecated gateway mode in 2022; RustFS, its replacement
# in this plugin family, never had one. Neither tutor-rustfs nor
# tutor-contrib-s3 can put Open edX's object storage on Azure as a result.
# See:
#   https://discuss.openedx.org/t/rustfs-plugin-to-replace-tutor-minio-thoughts-on-azure-gateway/19670
#
# This plugin evaluates rclone (https://rclone.org) as a replacement for
# that lost capability: Open edX keeps talking to a plain S3 endpoint, and
# `rclone serve s3` translates onto an `azureblob:` remote behind it.
#
# rclone's own documentation labels `serve s3` "Experimental", and this is
# genuinely a different category of thing than tutor-rustfs: that plugin
# runs a native S3-compatible storage *engine* (data lives in RustFS's own
# format); this plugin runs a *translation layer* in front of a backend
# that was never designed to speak S3. Open edX's own community already hit
# a serious failure running exactly this shape of architecture (MinIO
# gateway mode in front of Azure Blob Storage): multipart course-export
# uploads failed to complete, and the only fix anyone found was abandoning
# gateway mode entirely. Read EVALUATION.md before relying on this plugin
# for real course data — it documents the concrete problems this
# architecture inherits (a past CVE in rclone's multipart upload path, a
# documented real-world multi-minute stall on a large multipart upload, no
# bucket-level ACLs, no built-in CORS support) and how they were tested
# here, not just whether they are theoretically possible.
#
# Defaults point at Azurite, Microsoft's free local Azure Storage emulator,
# so `tutor local launch` works with no real Azure account. Override
# RCLONEGW_AZURE_ACCOUNT / RCLONEGW_AZURE_ACCOUNT_KEY / RCLONEGW_AZURE_ENDPOINT
# together to point at a real Azure Storage account instead; see the README.
#
# This plugin cannot run alongside tutor-minio or tutor-rustfs: all three
# configure Open edX's default object storage backend and would conflict.
# See check_plugin_conflict below.
# -----------------------------------------------------------------------------

config: dict[str, dict[str, t.Any]] = {
    "defaults": {
        "VERSION": __version__,
        # Buckets. Default names match tutor-rustfs/tutor-minio's so this
        # remains a drop-in alternative for comparison purposes.
        "BUCKET_NAME": "openedx",
        "FILE_UPLOAD_BUCKET_NAME": "openedxuploads",
        "VIDEO_UPLOAD_BUCKET_NAME": "openedxvideos",
        "GRADES_BUCKET_NAME": "openedxgrades",
        "OPENEDX_LEARNING_BUCKET_NAME": "openedxlearning",
        "DISCOVERY_BUCKET_NAME": "{% if 'discovery' in PLUGINS %}discoveryuploads{% endif %}",  # noqa: E501
        # Hostname. RCLONEGW_HOST serves the S3 API and must be reachable
        # from learners' browsers, not just from the Open edX containers:
        # presigned download URLs and public asset URLs are generated
        # against it.
        "HOST": "files.{{ LMS_HOST }}",
        "REGION": "us-east-1",
        "QUERYSTRING_AUTH": True,
        # https://hub.docker.com/r/rclone/rclone/tags
        # Hard-pinned to an exact tag, never a floating range: this
        # plugin's multipart-upload testing (see EVALUATION.md) is only
        # valid for the exact version tested. 1.75.1 fixes CVE-2026-88045,
        # an unauthenticated memory-exhaustion DoS in the multipart upload
        # path — never go below it. Bump deliberately, and re-run the live
        # conformance suite against the new tag before adopting it.
        "DOCKER_IMAGE": "docker.io/rclone/rclone:1.75.1",
        # The image creates a non-root `rclone` user at this UID/GID but
        # does not switch to it by default; we do so explicitly. Unlike
        # RustFS/MinIO there is no bind-mounted data directory, so there is
        # no chown-before-first-start step tied to this value.
        "UID": 1009,
        "GID": 1009,
        # Azure Blob Storage backend. These three defaults point at the
        # bundled Azurite emulator (see the `azurite` compose/k8s service)
        # for local testing with no real Azure account. Azurite's account
        # name and key are fixed, publicly documented constants — not
        # secrets — the same for every Azurite install everywhere.
        # Override all three together for a real Azure Storage account;
        # see the README before doing so.
        "AZURE_ACCOUNT": "devstoreaccount1",
        "AZURE_ACCOUNT_KEY": "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==",  # noqa: E501
        "AZURE_ENDPOINT": "http://azurite:10000/devstoreaccount1",
        # https://mcr.microsoft.com/en-us/artifact/mar/azure-storage/azurite/tags
        "AZURITE_DOCKER_IMAGE": "mcr.microsoft.com/azure-storage/azurite:3.37.0",
    },
    "unique": {
        "AWS_SECRET_ACCESS_KEY": "{{ 24|random_string }}",
    },
    "overrides": {
        "OPENEDX_AWS_ACCESS_KEY": "openedx",
        "OPENEDX_AWS_SECRET_ACCESS_KEY": "{{ RCLONEGW_AWS_SECRET_ACCESS_KEY }}",
    },
}

tutor_hooks.Filters.CONFIG_DEFAULTS.add_items(
    [(f"RCLONEGW_{key}", value) for key, value in config.get("defaults", {}).items()]
)
tutor_hooks.Filters.CONFIG_UNIQUE.add_items(
    [(f"RCLONEGW_{key}", value) for key, value in config.get("unique", {}).items()]
)
tutor_hooks.Filters.CONFIG_OVERRIDES.add_items(
    list(config.get("overrides", {}).items())
)


@tutor_hooks.Actions.PLUGIN_LOADED.add()
def check_plugin_conflict(plugin_name: str) -> None:
    """Refuse to run alongside tutor-minio or tutor-rustfs.

    All three plugins set STORAGES["default"] and claim the same
    hostnames for Open edX's object storage. Enabling more than one
    produces a stack that starts and then misbehaves in ways that are hard
    to trace back here, so fail loudly instead.
    """
    if plugin_name in ("minio", "rustfs"):
        raise tutor_exceptions.TutorError(
            f"The '{plugin_name}' and 'rclonegateway' plugins cannot be "
            "enabled at the same time: they both configure Open edX's "
            "default object storage. Disable one of them:\n"
            f"    tutor plugins disable {plugin_name}"
        )


# Bucket provisioning. The task name registered here ("rclonegw") must
# match a `<name>-job` service in the local and k8s job patches — Tutor
# appends the "-job" suffix itself when it resolves the task.
with open(
    os.path.join(HERE, "templates", "rclonegateway", "tasks", "rclonegw", "init.sh"),
    encoding="utf-8",
) as fi:
    tutor_hooks.Filters.CLI_DO_INIT_TASKS.add_item(
        ("rclonegw", fi.read()), priority=tutor_hooks.priorities.HIGH
    )

# Add the "templates" folder as a template root
tutor_hooks.Filters.ENV_TEMPLATE_ROOTS.add_item(
    str(importlib_resources.files("tutorrclonegateway") / "templates")
)
# Render the "build" and "apps" folders
tutor_hooks.Filters.ENV_TEMPLATE_TARGETS.add_items(
    [
        ("rclonegateway/build", "plugins"),
        ("rclonegateway/apps", "plugins"),
    ],
)
# Load patches from files
for path in glob(
    str(importlib_resources.files("tutorrclonegateway") / "patches" / "*")
):
    with open(path, encoding="utf-8") as patch_file:
        tutor_hooks.Filters.ENV_PATCHES.add_item(
            (os.path.basename(path), patch_file.read())
        )
