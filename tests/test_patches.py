"""Template rendering.

Most real breakages in a Tutor plugin are template bugs: a variable that
was renamed in plugin.py but not in a patch, YAML that stops parsing, a
service name that no longer matches the one Tutor looks up. None of those
are caught by lint or type checks, and all of them are caught here
without starting a container.

Several tests below have no tutor-rustfs equivalent at all: they exist
specifically to guard the two-process anonymous/private-bucket split that
rclone's `serve s3` forces this plugin to build (see the caddyfile and
local-docker-compose-services patches), including a template-rendering
bug an adversarial design review caught before it shipped — an unguarded
conditional path clause that would have routed every bucket, not just the
public one, to the unauthenticated backend whenever the `discovery`
plugin was disabled.
"""

from __future__ import annotations

import importlib
import os
import re
import typing as t
from pathlib import Path

import pytest
import yaml
from tutor import env as tutor_env
from tutor import hooks as tutor_hooks

from .conftest import patch_names, read_patch


def _patch_hooks_called_in(package_name: str) -> set[str]:
    """Every ``patch("...")`` call found in an installed package's files.

    Used to check this plugin's own patch *filenames* against hooks that
    are actually wired up somewhere, rather than trusting the filename
    convention by eye.
    """
    try:
        module = importlib.import_module(package_name)
    except ImportError:
        return set()
    assert module.__file__ is not None
    pkg_dir = Path(os.path.dirname(module.__file__))
    hooks: set[str] = set()
    for path in pkg_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        hooks.update(re.findall(r'patch\("([^"]+)"\)', text))
    return hooks


# Patches whose content is YAML (either a document or a fragment that is
# spliced into one).
YAML_PATCHES = [
    "k8s-deployments",
    "k8s-jobs",
    "k8s-override",
    "k8s-services",
    "local-docker-compose-caddy-aliases",
    "local-docker-compose-cms-dependencies",
    "local-docker-compose-dev-services",
    "local-docker-compose-jobs-services",
    "local-docker-compose-lms-dependencies",
    "local-docker-compose-services",
    "openedx-auth",
]

# Patches that are spliced into Django settings modules.
PYTHON_PATCHES = [
    "discovery-common-settings",
    "discovery-development-settings",
    "openedx-cms-common-settings",
    "openedx-common-settings",
    "openedx-development-settings",
    "openedx-lms-production-settings",
    "xqueue-settings",
]


@pytest.mark.parametrize("name", patch_names())
def test_patch_renders(name: str, renderer: tutor_env.Renderer) -> None:
    """Every patch renders against the default config.

    An undefined variable raises TutorError here.
    """
    renderer.render_str(read_patch(name))


@pytest.mark.parametrize("name", patch_names())
def test_no_unrendered_jinja(name: str, rendered: dict[str, str]) -> None:
    out = rendered[name]
    assert "{{" not in out and "{%" not in out, f"{name} has unrendered Jinja"


@pytest.mark.parametrize("name", patch_names())
def test_no_minio_or_rustfs_variables_survive(
    name: str, rendered: dict[str, str]
) -> None:
    """Catch an incomplete adaptation from tutor-rustfs/tutor-minio.

    Comments legitimately mention MinIO/RustFS by name (the architecture
    rationale, bucket-name parity), so only flag the config-variable
    form.
    """
    assert "MINIO_" not in rendered[name], f"{name} still references MINIO_"
    assert "RUSTFS_" not in rendered[name], f"{name} still references RUSTFS_"


@pytest.mark.parametrize("name", YAML_PATCHES)
def test_yaml_patches_parse(name: str, rendered: dict[str, str]) -> None:
    parsed = list(yaml.safe_load_all(rendered[name]))
    assert parsed and parsed[0] is not None, f"{name} rendered to empty YAML"


@pytest.mark.parametrize("name", PYTHON_PATCHES)
def test_python_patches_are_syntactically_valid(
    name: str, rendered: dict[str, str]
) -> None:
    """A syntax error here would only surface as a crashed LMS."""
    compile(rendered[name], f"<{name}>", "exec")


def test_all_patches_are_categorised() -> None:
    """Fail when a new patch is added without a parse check."""
    categorised = set(YAML_PATCHES) | set(PYTHON_PATCHES) | {"caddyfile"}
    uncategorised = set(patch_names()) - categorised
    assert not uncategorised, (
        f"new patches need a parse check in this file: {sorted(uncategorised)}"
    )


def test_patch_names_are_real_tutor_hooks() -> None:
    """A patch registered under a name nothing calls fails silently:
    Tutor raises no error, the content is just never rendered into any
    file. This happened once during development — a NetworkPolicy patch
    was first named "k8s-networkpolicy" (there is no such Tutor core
    hook; the real catch-all for a resource kind with no dedicated hook
    is "k8s-override") — and was only caught by actually rendering a live
    k8s environment and noticing the file was missing, not by any offline
    test. This is the test that makes sure that class of bug can't recur
    silently.
    """
    core_hooks = _patch_hooks_called_in("tutor")
    assert core_hooks, "found no patch() calls in Tutor's own templates at all"
    sibling_hooks = _patch_hooks_called_in("tutordiscovery") | _patch_hooks_called_in(
        "tutorxqueue"
    )

    checkable = set(patch_names())
    if not sibling_hooks:
        # tutor-discovery/tutor-xqueue aren't installed in this
        # environment. They're only needed at runtime if those plugins
        # are actually enabled, not at test time (tutor-rustfs's own dev
        # extras don't install them either) — so a discovery-*/xqueue-*
        # patch can't be confirmed here, but that is an environment gap,
        # not evidence the hook name is wrong.
        checkable = {
            name
            for name in checkable
            if not (name.startswith("discovery-") or name.startswith("xqueue-"))
        }

    unknown = checkable - core_hooks - sibling_hooks
    assert not unknown, (
        f"these patch filenames match no real Tutor patch() call and would "
        f"be registered but never rendered: {sorted(unknown)}"
    )


# --- statelessness: no PVC/volume anywhere ---------------------------------


def test_no_k8s_volumes_patch_exists() -> None:
    """All object data lives in Azure; a PVC here would be a sign
    something regressed back toward RustFS/MinIO's local-disk model."""
    assert "k8s-volumes" not in patch_names()


def test_no_persistentvolumeclaim_in_k8s_deployments(rendered: dict[str, str]) -> None:
    assert "persistentVolumeClaim" not in rendered["k8s-deployments"]
    assert "volumeMounts" not in rendered["k8s-deployments"]


def test_compose_services_have_no_bind_mounted_volumes(
    rendered: dict[str, str],
) -> None:
    services = yaml.safe_load(rendered["local-docker-compose-services"])
    for name in ("rclonegw", "rclonegw-public"):
        assert "volumes" not in services[name], (
            f"{name} should have no local data volume; all object data lives in Azure"
        )


# --- the storage services themselves ---------------------------------------


def test_authenticated_service_matches_config(
    rendered: dict[str, str], config: dict[str, t.Any]
) -> None:
    svc = yaml.safe_load(rendered["local-docker-compose-services"])["rclonegw"]
    assert svc["image"] == config["RCLONEGW_DOCKER_IMAGE"]
    assert svc["user"] == f"{config['RCLONEGW_UID']}:{config['RCLONEGW_GID']}"
    command = svc["command"]
    assert "serve" in command and "s3" in command and "azureblob:" in command
    access_key = config["OPENEDX_AWS_ACCESS_KEY"]
    secret_key = config["OPENEDX_AWS_SECRET_ACCESS_KEY"]
    assert f"--auth-key={access_key},{secret_key}" in command
    env = svc["environment"]
    assert env["RCLONE_CONFIG_AZUREBLOB_ACCOUNT"] == config["RCLONEGW_AZURE_ACCOUNT"]
    assert env["RCLONE_CONFIG_AZUREBLOB_KEY"] == config["RCLONEGW_AZURE_ACCOUNT_KEY"]
    assert env["RCLONE_CONFIG_AZUREBLOB_ENDPOINT"] == config["RCLONEGW_AZURE_ENDPOINT"]


def test_public_service_has_no_auth_key(rendered: dict[str, str]) -> None:
    svc = yaml.safe_load(rendered["local-docker-compose-services"])["rclonegw-public"]
    command = svc["command"]
    assert not any(str(arg).startswith("--auth-key") for arg in command), (
        "rclonegw-public must be reachable with zero credentials by design "
        "(that's the whole point of the two-process split) — an --auth-key "
        "here would just make it a second, redundant authenticated gateway"
    )


def test_public_service_is_read_only(rendered: dict[str, str]) -> None:
    svc = yaml.safe_load(rendered["local-docker-compose-services"])["rclonegw-public"]
    assert "--read-only" in svc["command"]


def test_public_service_include_never_names_a_private_bucket(
    rendered: dict[str, str], config: dict[str, t.Any]
) -> None:
    """The regression guard for the --include scoping that makes
    rclonegw-public structurally unable to see any bucket other than the
    public one(s), even if reached directly, bypassing Caddy entirely."""
    svc = yaml.safe_load(rendered["local-docker-compose-services"])["rclonegw-public"]
    includes = [
        str(arg)[len("--include=") :]
        for arg in svc["command"]
        if str(arg).startswith("--include=")
    ]
    assert includes, "rclonegw-public has no --include scoping at all"
    private_buckets = {
        config["RCLONEGW_FILE_UPLOAD_BUCKET_NAME"],
        config["RCLONEGW_VIDEO_UPLOAD_BUCKET_NAME"],
        config["RCLONEGW_GRADES_BUCKET_NAME"],
        config["RCLONEGW_OPENEDX_LEARNING_BUCKET_NAME"],
    }
    for include in includes:
        for private_bucket in private_buckets:
            assert f"/{private_bucket}/" not in include, (
                f"rclonegw-public's --include scoping references the "
                f"private bucket {private_bucket!r}: {include!r}"
            )
    allowed_public_buckets = {config["RCLONEGW_BUCKET_NAME"]}
    if config["RCLONEGW_DISCOVERY_BUCKET_NAME"]:
        allowed_public_buckets.add(config["RCLONEGW_DISCOVERY_BUCKET_NAME"])
    for include in includes:
        bucket_in_pattern = include.strip("/").split("/", 1)[0]
        assert bucket_in_pattern in allowed_public_buckets, (
            f"unexpected bucket {bucket_in_pattern!r} in rclonegw-public's "
            f"--include list: {include!r}"
        )


def test_azurite_service_is_wired(
    rendered: dict[str, str], config: dict[str, t.Any]
) -> None:
    svc = yaml.safe_load(rendered["local-docker-compose-services"])["azurite"]
    assert svc["image"] == config["RCLONEGW_AZURITE_DOCKER_IMAGE"]


def test_lms_and_cms_depend_on_rclonegw(rendered: dict[str, str]) -> None:
    for name in (
        "local-docker-compose-lms-dependencies",
        "local-docker-compose-cms-dependencies",
    ):
        assert yaml.safe_load(rendered[name]) == ["rclonegw"]


# --- Kubernetes network exposure --------------------------------------------


def test_k8s_services_are_clusterip_not_nodeport(rendered: dict[str, str]) -> None:
    """tutor-rustfs's storage Service is NodePort because RustFS has no
    "this bucket is public, this one isn't" distinction to protect in the
    first place. This plugin does, so NodePort (reachable from any node
    in the cluster, bypassing Caddy's method+path routing entirely) is
    not an acceptable default here."""
    docs = list(yaml.safe_load_all(rendered["k8s-services"]))
    for doc in docs:
        assert doc["spec"]["type"] == "ClusterIP", (
            f"{doc['metadata']['name']} is {doc['spec']['type']}, not ClusterIP"
        )


def test_k8s_networkpolicy_restricts_ingress_to_caddy(rendered: dict[str, str]) -> None:
    """NetworkPolicy has no dedicated Tutor core patch hook, unlike
    Deployments/Jobs/Services/Volumes, so it lives under the generic
    k8s-override extension point instead (see test_patch_names_are_real_tutor_hooks
    for the regression guard that would have caught registering it under a
    hook name nothing actually renders)."""
    docs = [
        doc
        for doc in yaml.safe_load_all(rendered["k8s-override"])
        if doc and doc.get("kind") == "NetworkPolicy"
    ]
    names = {doc["metadata"]["name"] for doc in docs}
    assert names == {"rclonegw", "rclonegw-public"}, (
        f"expected a NetworkPolicy for each gateway Deployment, got {names}"
    )
    for doc in docs:
        selector = doc["spec"]["podSelector"]["matchLabels"]
        assert selector == {"app.kubernetes.io/name": doc["metadata"]["name"]}
        ingress_from = doc["spec"]["ingress"][0]["from"]
        caddy_allowed = any(
            rule.get("podSelector", {})
            .get("matchLabels", {})
            .get("app.kubernetes.io/name")
            == "caddy"
            for rule in ingress_from
        )
        assert caddy_allowed, (
            f"{doc['metadata']['name']}'s NetworkPolicy does not allow "
            "ingress from the Caddy pod — nothing could reach it at all"
        )


def test_k8s_deployments_have_no_fsgroup_or_recreate_strategy(
    rendered: dict[str, str],
) -> None:
    """Both are RustFS/MinIO-specific, tied to their PVC. Nothing here
    needs either since there is no local data volume."""
    docs = list(yaml.safe_load_all(rendered["k8s-deployments"]))
    for doc in docs:
        spec = doc["spec"]["template"]["spec"]
        assert "fsGroup" not in spec.get("securityContext", {})
        assert doc["spec"].get("strategy", {}).get("type") != "Recreate"


# --- the job-service naming contract ----------------------------------------


def test_init_job_service_name_matches_init_task(rendered: dict[str, str]) -> None:
    """Tutor resolves an init task for service X to the ``X-job``
    service. A mismatch makes `tutor local do init` fail with an
    unhelpful "no such service" error.
    """
    service = dict(tutor_hooks.Filters.CLI_DO_INIT_TASKS.iterate())
    assert "rclonegw" in service
    jobs = yaml.safe_load(rendered["local-docker-compose-jobs-services"])
    assert "rclonegw-job" in jobs, f"expected 'rclonegw-job', got {sorted(jobs)}"
    assert jobs["rclonegw-job"]["depends_on"] == ["azurite"], (
        "bucket creation talks to Azure directly over the azureblob: "
        "remote, not through either gateway process"
    )


def test_k8s_job_name_matches_init_task(rendered: dict[str, str]) -> None:
    job = yaml.safe_load(rendered["k8s-jobs"])
    assert job["metadata"]["name"] == "rclonegw-job"


def test_init_job_reuses_the_single_pinned_image(
    rendered: dict[str, str], config: dict[str, t.Any]
) -> None:
    """Unlike RustFS/MinIO, rclone's own CLI is its own admin client, so
    there is no second "client-only" image to pin and track separately."""
    jobs = yaml.safe_load(rendered["local-docker-compose-jobs-services"])
    assert jobs["rclonegw-job"]["image"] == config["RCLONEGW_DOCKER_IMAGE"]


# --- Open edX client settings ------------------------------------------------


def test_openedx_points_at_the_rclonegw_host(
    rendered: dict[str, str], config: dict[str, t.Any]
) -> None:
    out = rendered["openedx-common-settings"]
    assert f'AWS_S3_ENDPOINT_URL = "http://{config["RCLONEGW_HOST"]}"' in out
    assert 'AWS_S3_SIGNATURE_VERSION = "s3v4"' in out
    assert f'AWS_S3_REGION_NAME = "{config["RCLONEGW_REGION"]}"' in out


def test_checksum_workaround_is_present(rendered: dict[str, str]) -> None:
    """boto3 >= 1.36 sends CRC32 checksums by default, which non-AWS S3
    implementations reject. Removing this breaks every upload against
    the rclone gateway for the same reason it would against RustFS."""
    names = ("openedx-common-settings", "discovery-common-settings", "xqueue-settings")
    for name in names:
        out = rendered[name]
        assert "request_checksum_calculation='when_required'" in out, name
        assert "response_checksum_validation='when_required'" in out, name


def test_buckets_referenced_in_settings_are_created_by_init(
    config: dict[str, t.Any],
) -> None:
    """Every bucket Open edX is configured to write to must be created
    by the init task, or the first upload 404s."""
    init = dict(tutor_hooks.Filters.CLI_DO_INIT_TASKS.iterate())["rclonegw"]
    rendered_init = tutor_env.Renderer(config).render_str(init)
    for key in (
        "RCLONEGW_BUCKET_NAME",
        "RCLONEGW_FILE_UPLOAD_BUCKET_NAME",
        "RCLONEGW_VIDEO_UPLOAD_BUCKET_NAME",
        "RCLONEGW_GRADES_BUCKET_NAME",
        "RCLONEGW_OPENEDX_LEARNING_BUCKET_NAME",
    ):
        assert config[key] in rendered_init, f"{key} is never created by init.sh"


# --- the caddyfile: the central design decision in this plugin --------------


def test_caddyfile_routes_public_bucket_to_anonymous_backend(
    rendered: dict[str, str], config: dict[str, t.Any]
) -> None:
    out = rendered["caddyfile"]
    assert config["RCLONEGW_HOST"] in out
    # Not Tutor's `import proxy "host:port"` snippet: it bundles a `log`
    # directive, which Caddy rejects inside a `handle` block. Confirmed
    # by actually starting the stack — see the caddyfile patch comment.
    assert "reverse_proxy rclonegw-public:9000" in out
    assert "reverse_proxy rclonegw:9000" in out
    assert "method GET HEAD" in out
    assert f"/{config['RCLONEGW_BUCKET_NAME']}/*" in out


def test_cors_headers_are_configured(rendered: dict[str, str]) -> None:
    out = rendered["caddyfile"]
    assert "Access-Control-Allow-Origin" in out


def test_caddyfile_discovery_clause_is_safe_when_disabled(
    renderer: tutor_env.Renderer, config: dict[str, t.Any]
) -> None:
    """RCLONEGW_DISCOVERY_BUCKET_NAME renders to an empty string when the
    discovery plugin is disabled (the default). An *unguarded* second
    path token in the caddyfile's @public_read matcher would collapse to
    a bare "/*" here — routing every bucket, including grades and ORA2
    uploads, to the unauthenticated backend. An adversarial design review
    caught exactly this bug before it shipped; this test is what keeps it
    caught.
    """
    assert config["RCLONEGW_DISCOVERY_BUCKET_NAME"] == ""
    out = renderer.render_str(read_patch("caddyfile"))
    path_lines = [line for line in out.splitlines() if line.strip().startswith("path ")]
    assert len(path_lines) == 1, f"expected one path directive, got {path_lines}"
    path_patterns = path_lines[0].split()[1:]
    assert path_patterns == [f"/{config['RCLONEGW_BUCKET_NAME']}/*"], (
        f"discovery is disabled but the path matcher is {path_patterns}; "
        "a bare '/*' token here would route every bucket to the "
        "unauthenticated backend"
    )


def test_caddyfile_discovery_clause_is_present_when_enabled(
    config: dict[str, t.Any],
) -> None:
    cfg = dict(config, RCLONEGW_DISCOVERY_BUCKET_NAME="discoveryuploads")
    out = tutor_env.Renderer(cfg).render_str(read_patch("caddyfile"))
    path_lines = [line for line in out.splitlines() if line.strip().startswith("path ")]
    assert len(path_lines) == 1
    path_patterns = path_lines[0].split()[1:]
    assert path_patterns == [
        f"/{cfg['RCLONEGW_BUCKET_NAME']}/*",
        "/discoveryuploads/*",
    ], path_patterns


# --- cross-plugin integration ------------------------------------------------


def test_discovery_bucket_is_conditional(
    renderer: tutor_env.Renderer, config: dict[str, t.Any]
) -> None:
    assert config["RCLONEGW_DISCOVERY_BUCKET_NAME"] == ""
    with_discovery = dict(config, PLUGINS=["discovery"])
    value = tutor_env.render_str(
        with_discovery,
        "{% if 'discovery' in PLUGINS %}discoveryuploads{% endif %}",
    )
    assert value == "discoveryuploads"


def test_discovery_settings_target_rclonegw(
    renderer: tutor_env.Renderer, config: dict[str, t.Any]
) -> None:
    cfg = dict(config, RCLONEGW_DISCOVERY_BUCKET_NAME="discoveryuploads")
    out = tutor_env.Renderer(cfg).render_str(read_patch("discovery-common-settings"))
    compile(out, "<discovery-common-settings>", "exec")
    assert f'AWS_S3_ENDPOINT_URL = "http://{cfg["RCLONEGW_HOST"]}"' in out
    assert 'AWS_STORAGE_BUCKET_NAME = "discoveryuploads"' in out


def test_xqueue_settings_target_rclonegw(
    rendered: dict[str, str], config: dict[str, t.Any]
) -> None:
    out = rendered["xqueue-settings"]
    assert f'AWS_S3_ENDPOINT_URL = "http://{config["RCLONEGW_HOST"]}"' in out
    assert f'AWS_STORAGE_BUCKET_NAME = "{config["RCLONEGW_BUCKET_NAME"]}"' in out
    assert 'AWS_LOCATION = "xqueueuploads"' in out
