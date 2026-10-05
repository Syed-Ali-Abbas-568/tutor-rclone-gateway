# Changelog

<!-- scriv-insert-here -->

## Unreleased

- [Feature] Initial release. `tutor-rclone-gateway` configures Open edX to use
  Azure Blob Storage, by running [rclone](https://rclone.org)'s `serve s3`
  command as a translation layer in front of an Azure Blob Storage account.
  It exists to answer a question raised on the Open edX forum after MinIO's
  "gateway" mode (which did this same kind of translation) was deprecated in
  2022, and RustFS — the plugin that replaced MinIO in this family,
  `tutor-rustfs` — turned out to have no equivalent.

  This is architecturally a different kind of plugin than `tutor-rustfs`:
  that plugin runs a native S3-compatible storage *engine* (data lives in
  RustFS's own on-disk format); this plugin runs a *translation layer* in
  front of a backend that was never designed to speak S3. Read
  `EVALUATION.md` before relying on it for real course data.

  - 💥 **Two gateway processes, not one.** rclone's `serve s3` has no
    bucket-level ACL/policy mechanism at all — auth is either full
    read-write to everything served, or fully anonymous full read-write to
    everything served. Open edX needs exactly one bucket (forum image
    uploads, which can't use presigned URLs because they're baked
    permanently into stored post HTML) to be anonymously *readable* but not
    writable. This plugin runs a second, unauthenticated, read-only,
    `--include`-scoped `rclone serve s3` process for that one purpose, and a
    Caddy rule on the single public hostname splits traffic between the two
    by HTTP method and path.
  - 💥 **No web console.** rclone has no bucket-browser UI equivalent to
    MinIO/RustFS's. There is no `RCLONEGW_CONSOLE_HOST`. Use the Azure
    Portal, Azure Storage Explorer, or `rclone ncdu` instead.
  - 💥 **No local data at all.** Every object lives in Azure. Unlike
    `tutor-rustfs`, there is no bind-mounted volume, no non-root/chown dance
    on a fresh Linux install, and no Kubernetes PVC.
  - 💥 **Bucket (Azure container) creation uses `rclone mkdir` directly
    against Azure**, not through either gateway process, and has no
    policy-setting step: anonymous read access is a property of the
    two-process topology above, never a flag set on the bucket itself. Only
    one pinned image is needed for the servers *and* the init job — rclone's
    own CLI is its own admin client, unlike RustFS/MinIO, whose server images
    ship no client and need a separate `rc`/`mc` image.
  - 💥 **Kubernetes Services are `ClusterIP`, with a `NetworkPolicy`
    restricting ingress to the Caddy pod**, not `NodePort` with no
    additional restriction. RustFS has no bucket whose security depends on
    only being reachable through Caddy, so its own Service pattern has never
    needed this; this plugin does.
  - 💥 **CORS is set at the Caddy layer, not via rclone.** MinIO/RustFS send
    permissive CORS by default; rclone's `serve s3` has no bucket CORS
    configuration and an uncertain `--allow-origin` flag, so this plugin
    sets the headers in Caddy instead, where it is certain to work.
  - [Security] The Docker image is hard-pinned to an exact tag
    (`docker.io/rclone/rclone:1.75.1`), not a floating range. 1.75.1 fixes
    CVE-2026-88045, an unauthenticated memory-exhaustion denial-of-service in
    rclone's multipart upload path, introduced in the same 1.75.0 release
    that rewrote multipart streaming. Bumping this requires re-running the
    live conformance suite against the new tag first — see `EVALUATION.md`.

- [Improvement] Add a test suite that goes well beyond `tutor-rustfs`'s,
  specifically because this plugin's architecture needs it: static checks
  that the anonymous gateway process can never be configured to reference a
  private bucket or accept write requests, a static check that a
  Jinja-template bug an adversarial design review caught (an unguarded
  conditional path clause that would have routed every bucket to the
  unauthenticated backend whenever the `discovery` plugin was disabled)
  cannot regress, and a live conformance suite covering the public/private
  bucket split, CORS, Range requests, a realistically-sized multipart
  upload chosen to probe a real documented rclone stall incident, and a
  concurrency-latency regression guard for a previously-shipped rclone
  bug class. See `TESTING.rst`.

- [Documentation] Add `EVALUATION.md`: a dedicated writeup of whether rclone
  is a credible S3-to-Azure gateway, compared against `tutor-rustfs`, backed
  by the test results above rather than documentation alone.

- [Finding] **Actually running this plugin against a live `tutor local
  launch` deployment, not just rendering its templates, surfaced a
  reproducible livelock**: `rclone serve s3` hangs — CPU pinned, zero
  network progress, no self-recovery — when multipart parts arrive out of
  ascending order and the upload crosses roughly 300 MiB, confirmed against
  this plugin's own default Azurite backend and independent of concurrency
  level. See `EVALUATION.md` Finding 1 for the full bisection and
  reproduction steps. This is the central result of this plugin's
  evaluation: build it carefully, test it for real, and it still does not
  survive the one workload (large multipart uploads) that broke MinIO's own
  version of this architecture for this same community.

  Live testing also caught three bugs no offline check could have:
  `k8s-networkpolicy` was registered under a patch hook Tutor never calls
  (the real hook for a resource with no dedicated one is `k8s-override`,
  now covered by `test_patch_names_are_real_tutor_hooks`); a bare
  `- azureblob:` YAML list item was parsed as a mapping key rather than the
  literal string rclone needs (now quoted, `"azureblob:"`); and the
  Caddyfile's use of Tutor's own `import proxy` snippet inside a `handle`
  block crashed Caddy outright (`log` is not an ordered HTTP handler
  directive) — fixed by using `reverse_proxy` directly.
