# Is rclone a good S3-to-Azure gateway for Open edX? A tested answer.

**Bottom line: no, not for course content, not yet.** `rclone serve s3` reliably
and reproducibly **hangs** — a livelocked process pinned at 100%+ CPU, zero
network progress, that does not self-recover and needs a container restart —
when multipart upload parts arrive out of ascending order, once the upload
crosses a few hundred MiB. This was found in *this evaluation's own testing*,
against the plugin's default backend (Azurite), with nothing more adversarial
than the S3 API's own explicitly-permitted "parts may be uploaded in any
order" behavior. It is not a theoretical risk read off a changelog — it was
triggered twice, independently, and bisected to a size range, in about twenty
minutes of testing. See [Finding 1](#finding-1-the-multipart-hang-headline)
below for exactly how to reproduce it.

Everything else about this plugin — the two-process anonymous/private bucket
split, the Kubernetes network isolation, CORS, concurrency, the generic S3
verbs — worked correctly once built, including surviving an adversarial design
review before a line of code was written. That makes Finding 1 more
significant, not less: this is a careful implementation of the idea, and the
idea still breaks on contact with a real multipart upload pattern.

## What this document is

A comparison between this plugin (`tutor-rclone-gateway`, an S3-to-Azure
translation layer built on [rclone](https://rclone.org)'s `serve s3` command)
and [`tutor-rustfs`](https://github.com/edly-io/tutor-rustfs) (a native,
non-translating S3-compatible storage engine), written to answer a question
raised on [the Open edX
forum](https://discuss.openedx.org/t/rustfs-plugin-to-replace-tutor-minio-thoughts-on-azure-gateway/19670)
after MinIO's own "gateway" mode — which did this same kind of S3-to-Azure
translation — was deprecated in 2022, and RustFS turned out to have no
equivalent. Every claim below is backed by either a specific test in this
repository (`tests/test_s3_conformance.py`, `tests/test_patches.py`) or a
cited external source; see the inline references.

## Architecture: a translation layer, not an engine

This is the load-bearing distinction, and it is worth stating plainly before
any test result: `tutor-rustfs` runs RustFS, a native S3-compatible storage
*engine* — data lives in RustFS's own format, and the S3 API is both the
control plane and the data plane, implemented by a team whose entire product
is that implementation. This plugin instead runs `rclone serve s3` as a
*translation layer* in front of Azure Blob Storage, a backend that was never
designed to speak S3 at all. Every byte that moves through this plugin crosses
a translation boundary between two different storage models (S3's flat
key/multipart-upload model vs. Azure's container/block-blob model) that an
engine like RustFS simply does not have to cross.

MinIO invented this exact pattern — an S3 gateway in front of non-S3 cloud
backends, including Azure — ran it for years, and killed it. The [deprecation
announcement](https://blog.min.io/deprecation-of-the-minio-gateway/) cites the
ongoing cost of keeping per-backend translation correct as the reason, not a
lack of demand. Open edX's own community independently hit the failure mode
this plugin was built to test for: a [forum
thread](https://discuss.openedx.org/t/tutor-qunice-minio-as-gateway-for-azure-storage-issues/12995)
documents MinIO running in gateway mode in front of Azure Blob Storage in
Tutor, where multipart course-import/export uploads failed with "One or more
of the specified parts could not be found," traced to Azure Storage itself —
not MongoDB, not file size limits. The only fix anyone found was abandoning
gateway mode (`MINIO_GATEWAY: null`) and running object storage standalone.
That thread is why `tutor-rustfs` has no gateway mode at all, and it is the
precedent this entire evaluation was measuring rclone against.

## What works

Confirmed by the live conformance suite (`tests/test_s3_conformance.py`),
run against a real `tutor local launch` deployment with the plugin's default
Azurite backend, and separately against the CI compose stack:

- **Generic S3 operations** — `ListBuckets`, `PutObject`/`GetObject`,
  `HeadObject`, presigned URLs, `ListObjectsV2`, server-side `CopyObject`,
  `DeleteObject` — all work exactly as they do against `tutor-rustfs`.
- **The anonymous/private bucket split holds, robustly.** rclone's `serve s3`
  has no bucket-level ACL mechanism at all (unlike RustFS/MinIO's `rc
  anonymous set download`), so this plugin runs two `rclone serve s3`
  processes — one authenticated, one anonymous-read-only-and-scoped — behind
  a Caddy rule that splits traffic by method and path (see the README's "How
  it works"). This was tested three separate ways, and all three passed:
  - Through Caddy: anonymous GET succeeds on the public bucket, anonymous
    PUT/DELETE are rejected, and anonymous GET on a *private* bucket is
    rejected.
  - **Directly against the anonymous process, bypassing Caddy entirely**: a
    private bucket is not merely access-denied, it is structurally invisible
    — `GET /openedxgrades/x` returns a clean `404 NoSuchBucket`, and
    `ListBuckets` on that process lists only the public bucket, never the
    others by name. This confirms rclone's `--include` flag is a real
    VFS-layer boundary, not a listing-only filter, exactly as predicted by
    tracing rclone's own source during design review.
  - The Kubernetes `NetworkPolicy` that restricts both gateway Deployments to
    ingress from the Caddy pod only (closing the exposure that
    `tutor-rustfs`'s own `NodePort`-without-restriction pattern would have
    created here) renders correctly and was validated statically.
- **CORS** works, because this plugin deliberately does not rely on rclone's
  own `--allow-origin` flag (whose documentation ties it to rclone's
  WebDAV/remote-control code path, with no confirmation it covers `serve s3`
  at all) and sets the headers at the Caddy layer instead.
- **Concurrent-GET latency does not scale with client count** — the
  regression guard for a real, previously-shipped rclone bug (a lock held for
  an entire HTTP transaction, serializing every other client, fixed in
  1.69) — passed at 20 concurrent clients.
- **A single, sequential, boto3-managed 350 MiB multipart upload** (past the
  256 MiB default streaming-buffer threshold) completed in under 6 seconds
  against Azurite, with the correct final size.

## What doesn't

### Finding 1: the multipart hang (headline)

**Severity: do not use for production course content until this is
understood or fixed upstream.**

`test_multipart_upload_out_of_order_at_concurrency` (gated behind
`RCLONEGW_TEST_REAL_AZURE_GATE=1`, intended to also run against real Azure —
see [What wasn't tested](#what-wasnt-tested)) uploads a file via raw
`upload_part` calls submitted in **strictly reversed order** — part N first,
part 1 last — which the S3 API explicitly permits clients to do. Run against
this plugin's own default Azurite backend:

| Size | Concurrency | Result |
|---|---|---|
| 40 MiB (5 parts) | 1 | Pass, 1.17s |
| 100 MiB (~13 parts) | 1 | Pass, 2.05s |
| 200 MiB (~25 parts) | 1 | Pass, 3.60s |
| 260 MiB (~33 parts) | 1 | Pass, 4.61s |
| **300 MiB (~38 parts)** | **1** | **Hang — no completion within 70s, killed** |
| **350 MiB (~44 parts)** | **1** | **Hang — no completion within 70s (and separately within 300s), killed** |

The hang is not "slow" — it is a livelock. `docker stats` on the `rclonegw`
container during the hang showed **113–213% CPU, perfectly flat memory
(~634 MiB), and perfectly flat network I/O (0 new bytes in or out)** across
multiple measurements seconds apart. The process is actively burning two CPU
cores while making no observable progress, and does not recover after the
client disconnects — `tutor local restart rclonegw` was required each time to
return it to a clean 0% CPU / 18 MiB baseline. This reproduced identically on
two separate occasions (first discovered running the full concurrency sweep
[1, 2, 4, 8, 10] at 350 MiB, then confirmed in isolation at concurrency=1
alone — so **concurrency is not the trigger; size/part-count crossing
somewhere between 260 and 300 MiB, combined with reversed delivery order,
is**).

This independently reproduces — and arguably exceeds — the real-world incident
in rclone's own issue tracker
([#7453](https://github.com/rclone/rclone/issues/7453)): a user uploading a
2 GiB file hit an unexplained multi-minute stall at roughly 322 MiB / 64
parts, root cause never conclusively pinned, resolved only by lowering
client-side concurrency. This evaluation's finding shows the trigger isn't
concurrency specifically — strictly sequential (concurrency=1) delivery in
reversed order is sufficient on its own.

**Why this matters for Open edX specifically**: course import/export and
video uploads are exactly the workloads that cross the multipart threshold,
and nothing in the S3 API forces a client to upload parts in ascending order —
boto3's own `TransferConfig`-managed uploads happen to do so by default (which
is why the plain 350 MiB test above passed cleanly), but any client,
retry logic, or parallel-upload tool that doesn't guarantee strict ordering
could trigger this. A livelocked gateway process doesn't just fail the one
upload — given this plugin's single `rclonegw` process serves *every* bucket,
it stalls every other concurrent request to any private bucket until someone
notices and restarts it.

**How to reproduce**: `TESTING.rst` Tier 5, or directly:
```
export RCLONEGW_TEST_ENDPOINT=<your endpoint>
export RCLONEGW_TEST_ACCESS_KEY=... RCLONEGW_TEST_SECRET_KEY=...
export RCLONEGW_TEST_REAL_AZURE_GATE=1
export RCLONEGW_TEST_REAL_AZURE_SIZE_MIB=300
pytest -v "tests/test_s3_conformance.py::test_multipart_upload_out_of_order_at_concurrency[1]"
```
Tested against `docker.io/rclone/rclone:1.75.1` (the exact pinned version this
plugin ships), `--vfs-cache-mode off` (this plugin's default), backed by
Azurite 3.37.0. Not yet tested against real Azure Blob Storage — see below —
but nothing about the hang's signature (CPU-pinned, zero I/O, Azurite-local)
points to an Azure-specific cause; it looks like rclone's own out-of-order
part-buffering logic, independent of backend.

### Finding 2: non-standard error code for missing credentials

A request to the authenticated gateway with a correctly-signed-but-wrong
access key gets the expected, standard `403`-shaped
`InvalidAccessKeyId`. A request with **no** `Authorization` header at all gets
`400 UnsupportedAlgorithm: Encountered an unsupported algorithm` — not the
`403 MissingAuthenticationToken` real AWS S3 (and MinIO/RustFS) return. No
data leaks and no write succeeds either way — this is purely an error-message
ergonomics gap — but a client or monitoring system that pattern-matches on
AWS's usual error vocabulary (reasonably, since everything else here mimics
AWS so closely) may mishandle or mislabel this specific case. See
`tests/test_s3_conformance.py`'s `ANONYMOUS_REJECTION_CODES`.

### Finding 3: Range requests don't return 206

A `GET` with a `Range` header returns the **correct bytes** and a **correct
`Content-Range` header** — but HTTP status `200`, not the spec-required `206`
(both RFC 7233 and the S3 API require 206 for a satisfied range request; real
AWS S3, MinIO, and RustFS all return 206). Verified this originates in rclone
itself, not Caddy: Caddy's `reverse_proxy` is a transparent passthrough that
does not rewrite status codes. Most clients that check for `Content-Range`
rather than strictly gating on status 206 will work fine; clients or CDNs that
branch on status code may not treat this as a partial response. Relevant to
video scrubbing, which depends on Range support.

### Finding 4: this plugin needed real infrastructure RustFS doesn't

Not a runtime bug, but a fair point of comparison: building a working,
secure version of this plugin required solving three problems `tutor-rustfs`
never has to:

1. **No bucket ACLs** → a second gateway process plus Caddy routing (see
   "What works" above) instead of one `rc anonymous set download` command.
2. **No native CORS** → headers set at the proxy layer instead of a server
   default.
3. **No "this bucket must stay private" network boundary** → a Kubernetes
   `NetworkPolicy` this plugin has to ship and `tutor-rustfs` doesn't need.

None of these were insurmountable — all three were built, tested, and work —
but they are real operational surface this architecture adds, exactly the
kind of "maintaining correct per-backend translation is expensive" cost MinIO
cited when it killed its own version of this pattern.

### Known, from rclone's own documentation and issue tracker (context for Finding 1, not independently re-verified here)

- `rclone serve s3` is explicitly labeled **"Experimental"** by rclone's own
  docs.
- **CVE-2026-88045** (CVSS 7.5, High): the multipart-streaming code path
  allowed an unauthenticated attacker to exhaust server memory by declaring a
  large `Content-Length` and never sending the body. Fixed in 1.75.1, the
  exact version this plugin pins — but notably, this is the *same area of
  code* (multipart streaming, rewritten in 1.75.0) that Finding 1 lives in,
  one point release later.
- Multipart **server-side copy** is documented upstream as unreliable and
  "extremely slow, eventually failing"
  ([#7454](https://github.com/rclone/rclone/issues/7454)) — not independently
  re-tested here; see `test_multipart_server_side_copy`
  (`RCLONEGW_TEST_SLOW=1`, not run as part of this evaluation).
- A past concurrency bug (fixed in 1.69) held a lock for an entire HTTP
  transaction, serializing every other client — this evaluation's
  concurrent-GET test confirms that *specific* bug is fixed, but the class of
  bug (resource contention in the serve-s3 layer) clearly isn't extinct,
  given Finding 1.

## rclone gateway vs. tutor-rustfs

| | `tutor-rclone-gateway` (this plugin) | `tutor-rustfs` |
|---|---|---|
| Architecture | Translation layer (S3 → Azure) | Native S3 engine |
| Backend maturity | `serve s3`: "Experimental," ~4 months old at time of testing | RustFS 1.0.0 GA |
| Multipart upload (standard order) | Works | Works |
| Multipart upload (out-of-order parts) | **Hangs, reproducibly, ≥~300 MiB** | No equivalent risk — no translation layer |
| Bucket-level anonymous read | Requires a second process + proxy routing | One native command |
| CORS | Requires proxy-layer headers | Native default |
| Local disk / non-root chown dance | None — stateless, no local volume | Required on fresh Linux installs |
| Kubernetes PVC | None needed | Required |
| Web console | None (use Azure Portal/Storage Explorer) | Built in |
| Backend lock-in | None — any rclone-supported remote | RustFS's own format |

## What wasn't tested

**Real Azure Blob Storage.** Every result above is against Azurite, this
plugin's default local emulator. Azurite's own documentation disclaims any
performance guarantee and does not model high-concurrency client behavior —
the dimension most likely to matter for a translation-layer commit-semantics
issue. Finding 1's CPU/memory signature strongly suggests the hang is in
rclone's own request-handling logic rather than anything Azure-specific
(Azurite implements the same Azure Blob block-commit API Azure itself does),
but this has not been confirmed against the real service. Running
`test_multipart_upload_out_of_order_at_concurrency` against a real Azure
Storage account requires a real account and will incur Azure costs — that
needs explicit sign-off and credentials from whoever is evaluating this
plugin further, which were not available during this evaluation. Until that
run happens, treat Finding 1 as confirmed-against-Azurite and
presumed-but-unconfirmed against real Azure.

## Recommendation

Do not point production course data at this plugin. The two-process
anonymous-bucket design works and was validated thoroughly, which is the good
news — but Finding 1 is the kind of result that was exactly worth building
this plugin to find: a real, reproducible, severe failure in the one
operation (large multipart uploads) that broke MinIO's version of this same
architecture for this same community, now independently reproduced in a
different implementation. `tutor-rustfs` carries none of this risk class,
because it never translates between two different storage models in the
first place. If Azure Blob Storage support is a hard requirement, the honest
options today are: wait for upstream rclone to address this (worth filing
against [rclone/rclone](https://github.com/rclone/rclone) with the
reproduction steps above), evaluate a different translation layer (the
original forum thread named `S3Proxy` as unvalidated — still unvalidated), or
accept the operational cost of restarting a stalled gateway process as a
standing risk.
