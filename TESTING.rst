Testing tutor-rclone-gateway
============================

Five tiers, cheapest first. The first four mirror ``tutor-rustfs``'s own
procedure; the fifth has no equivalent there, because it only matters for a
gateway (a translation layer in front of a backend that was never designed to
speak S3), not a native S3 engine.

.. contents::
   :local:
   :depth: 1

Tier 1 — static checks and template rendering
---------------------------------------------

No Docker, no Open edX, runs in under a second. This is what CI runs on every
pull request.

::

    pip install -e ".[dev]"
    make test

That runs lint, formatting, type checks, and the unit suite. The unit suite
alone::

    make test-unit

What it covers, beyond the usual (every config key is ``RCLONEGW_``-prefixed,
no ``MINIO_``/``RUSTFS_`` key survives, every patch renders, the init-job
naming contract, the ``discovery``/``xqueue`` cross-plugin patches):

- **The anonymous gateway process can never be configured to reference a
  private bucket or accept writes.** ``rclonegw-public``'s rendered command is
  asserted to have no ``--auth-key``, to have ``--read-only``, and its
  ``--include`` list is asserted to name only the intended public bucket(s) —
  never grades, ORA2 uploads, video, or learning-core buckets.
- **The Caddyfile's conditional discovery-bucket clause cannot regress.** An
  adversarial design review caught a real bug before this plugin shipped: an
  unguarded second path token would collapse to a bare ``/*`` whenever the
  ``discovery`` plugin is disabled, routing *every* bucket to the
  unauthenticated backend. ``test_caddyfile_discovery_clause_is_safe_when_disabled``
  renders the patch with discovery off and asserts the path matcher is
  exactly one token.
- **Kubernetes Services are ``ClusterIP``, and a ``NetworkPolicy`` restricts
  ingress to the Caddy pod.** Both gateway Deployments would otherwise be
  reachable from any pod in the cluster via Service DNS.
- **No PVC, bind mount, or local data volume exists anywhere** — all object
  data lives in Azure.

To confirm the suite is not passing vacuously, break something on purpose and
check it goes red::

    sed -i 's/--read-only//' tutorrclonegateway/patches/local-docker-compose-services
    make test-unit   # expect test_public_service_is_read_only to fail
    git checkout tutorrclonegateway/patches/local-docker-compose-services

Tier 2 — generated configuration
---------------------------------

Checks that Tutor produces a valid stack, without starting it. Use a
throwaway root so your real deployment is untouched::

    export TUTOR_ROOT=/tmp/tutor-rclone-gateway-test
    tutor plugins enable rclonegateway
    tutor config save
    tutor local dc config > /dev/null && echo "compose OK"

Then read the rendered output::

    tutor config printvalue RCLONEGW_HOST
    tutor config printvalue RCLONEGW_DOCKER_IMAGE
    grep -A20 '^  rclonegw:' "$TUTOR_ROOT/env/local/docker-compose.yml"

For Kubernetes::

    tutor k8s apply --dry-run=client -f "$TUTOR_ROOT/env/k8s/deployments.yml"
    # The NetworkPolicy resources live in override.yml: there is no
    # dedicated Tutor core patch hook for this resource kind.
    tutor k8s apply --dry-run=client -f "$TUTOR_ROOT/env/k8s/override.yml"

Tier 3 — live deployment
-------------------------

A real stack, backed by the bundled Azurite emulator by default (no real
Azure account needed)::

    tutor local launch

Checks, in order:

1. **The services are up.**

   ::

       tutor local logs rclonegw rclonegw-public azurite --tail 50
       tutor local exec rclonegw rclone version

2. **Containers exist and the init task is idempotent.** Run it twice; the
   second run must also succeed.

   ::

       tutor local do init --limit=rclonegateway
       tutor local do init --limit=rclonegateway

3. **The public bucket is anonymously readable but not writable; a private
   bucket is not readable at all.**

   ::

       # replace <RCLONEGW_HOST> with `tutor config printvalue RCLONEGW_HOST`
       curl -i https://<RCLONEGW_HOST>/openedx/does-not-exist   # 404, not a TLS/connection error
       curl -i -X PUT -d x https://<RCLONEGW_HOST>/openedx/probe.txt  # expect 403/405
       curl -i https://<RCLONEGW_HOST>/openedxgrades/does-not-exist   # expect 403/404, never 200

4. **Open edX can write.** Upload a course asset in Studio and confirm it
   appears in the ``openedx`` bucket (via the Azure Portal, Storage Explorer,
   or ``rclone ls``). Then upload a video and confirm it lands in
   ``openedxvideos`` — video goes through the multipart path, which fails
   differently from small uploads.

5. **Data survives a restart.** Upload something, then::

       tutor local restart rclonegw rclonegw-public

   and confirm it is still there. Since all data lives in Azure rather than a
   local volume, this mostly checks that the gateway reconnects correctly,
   not that data was never actually persisted.

.. note::

   Unlike ``tutor-rustfs``, there is no console to log into and no non-root
   chown step required on a fresh Linux install — see the README for why.

Tier 4 — S3 conformance (local/k8s mode only)
-----------------------------------------------

The live boto3 suite, using the same client configuration Open edX uses. It
is skipped unless ``RCLONEGW_TEST_ENDPOINT`` is set. **Run this in ``tutor
local launch`` mode, not ``tutor dev``**: dev mode runs no Caddy at all, so
the public/private bucket split this suite spends most of its time testing
cannot exist there.

::

    pip install boto3 requests
    export RCLONEGW_TEST_ENDPOINT=https://<RCLONEGW_HOST>
    export RCLONEGW_TEST_ACCESS_KEY="$(tutor config printvalue OPENEDX_AWS_ACCESS_KEY)"
    export RCLONEGW_TEST_SECRET_KEY="$(tutor config printvalue OPENEDX_AWS_SECRET_ACCESS_KEY)"
    pytest -v tests/test_s3_conformance.py

Covers everything ``tutor-rustfs``'s suite does (signed PUT/GET, presigned
URLs, ``ListObjectsV2``, server-side copy, delete), plus several checks with
no ``tutor-rustfs`` equivalent because they only matter for a gateway:

- Anonymous GET succeeds, anonymous PUT/DELETE are rejected, on the public
  bucket — **through Caddy**, which exercises routing and backend scoping
  together.
- Anonymous GET against a **private** bucket, through the same public-facing
  host, is rejected. This is the regression guard for the Caddyfile bug
  mentioned in Tier 1.
- CORS headers are present on a cross-origin request.
- A Range request returns the correct bytes and ``Content-Range`` header.
  **Confirmed quirk, not a test bug**: the HTTP status stays ``200`` rather
  than the spec-required ``206`` — see ``EVALUATION.md`` Finding 3. The test
  accepts both but asserts the data is genuinely sliced either way.
- A **350 MiB**, normally-ordered (boto3-managed) multipart upload — past
  rclone's 256 MiB default streaming buffer. This one passes cleanly and
  quickly. It does **not** mean multipart uploads are safe here — see the
  next paragraph.

**A more adversarial multipart test, run during this plugin's own
evaluation, found a reproducible hang — against Azurite, before ever
reaching Tier 5.** Parts submitted via raw ``upload_part`` calls in
**strictly reversed order** complete fine up to 260 MiB and hang
indefinitely (CPU pinned, zero network progress, no self-recovery) at
300 MiB and above, confirmed at concurrency=1 alone (not concurrency-
dependent). Full details, exact reproduction steps, and the bisection
table are in ``EVALUATION.md``. This is gated the same way as Tier 5 below
(``RCLONEGW_TEST_REAL_AZURE_GATE=1``) but reproduces against the default
Azurite backend — you do not need real Azure credentials to see it.
- A concurrency-latency check: 20 concurrent GETs must not show p99 latency
  scaling linearly with client count, which would reproduce a real,
  previously-shipped rclone bug (a lock held for an entire HTTP transaction,
  fixed in rclone 1.69). This one is **required, not skip-if-flaky** — a
  flaky result here is itself evidence worth keeping, not noise to filter.

Two tests are opt-in and excluded by default:

::

    # Multipart server-side copy: rclone's own docs call this path
    # "unreliable and extremely slow, eventually failing" (issue #7454).
    RCLONEGW_TEST_SLOW=1 pytest -v tests/test_s3_conformance.py -k multipart_server_side_copy

Tier 5 — real Azure: does the already-confirmed hang also happen there?
-------------------------------------------------------------------------

**This plugin already failed Tier 4's adversarial multipart check, against
Azurite, before Tier 5 was ever run — see EVALUATION.md Finding 1.** That
alone is enough to not trust this plugin with production course data; Tier 5
is not what tells you whether to trust it, Tier 4 already did. What Tier 5
*adds* is whether the hang is specifically about rclone's own request
handling (reproduces identically against real Azure) or has an Azure-specific
flavor (different threshold, different symptom, or — less likely given the
CPU/network signature observed — doesn't reproduce at all). Either answer is
worth recording; neither changes the Tier 4 result.

1. Create a real Azure Storage account and override, together::

       tutor config save \
           --set RCLONEGW_AZURE_ACCOUNT=<your-account> \
           --set RCLONEGW_AZURE_ACCOUNT_KEY=<your-key> \
           --set RCLONEGW_AZURE_ENDPOINT=https://<your-account>.blob.core.windows.net
       tutor local launch

2. Reproduce the Tier 4 hang first, at the same size that triggered it
   against Azurite, before spending time on the full concurrency/size sweep::

       export RCLONEGW_TEST_REAL_AZURE_GATE=1
       export RCLONEGW_TEST_REAL_AZURE_SIZE_MIB=300
       pytest -v "tests/test_s3_conformance.py::test_multipart_upload_out_of_order_at_concurrency[1]"

3. If it reproduces (expected, given the hang's CPU/network signature looks
   like rclone's own logic rather than anything Azure-specific), that's the
   answer — no need to run the full concurrency/size sweep to "confirm it
   more." If it does **not** reproduce against real Azure, that is itself an
   important and surprising finding worth the full sweep (concurrency
   1/2/4/8/10, sizes up to several GiB) to characterize.

4. Record whatever happens in ``EVALUATION.md``, including the exact rclone
   image tag tested. The Tier 4 finding's reproduction steps and bisection
   table were written down from actual results, not predicted in advance —
   if real Azure behaves differently, say so plainly rather than smoothing
   over the discrepancy.

This tier costs real Azure spend and needs real credentials — run it
deliberately, not as part of routine development.

Known failure modes
--------------------

.. list-table::
   :header-rows: 1

   * - Symptom
     - Cause
   * - ``SignatureDoesNotMatch``
     - Access key or secret differs between the gateway and Open edX.
   * - Uploads fail with a checksum error
     - The ``request_checksum_calculation`` setting was removed from
       ``openedx-common-settings``.
   * - ``NoSuchBucket`` on first upload
     - ``tutor local do init`` never ran, or ran against a different Azure
       account than the one the gateway is configured for.
   * - Anonymous GET on the public bucket returns 403
     - ``rclonegw-public`` isn't up, or its ``--include`` doesn't cover this
       bucket, or the Caddyfile's path matcher doesn't match — check all
       three before assuming it's an Azure-side permissions issue.
   * - Anonymous GET on a *private* bucket returns 200
     - Stop and treat this as a security incident, not a test failure to
       work around. Check the Caddyfile's discovery-bucket clause (Tier 1)
       and ``rclonegw-public``'s ``--include`` list first.
   * - ``tutor local do init`` says no such service
     - The job service name no longer matches the ``CLI_DO_INIT_TASKS``
       registration (``rclonegw`` / ``rclonegw-job``). Tier 1 catches this.
   * - ``tutor local do init --limit=rclonegw`` silently does nothing
     - ``--limit`` matches by **plugin** name, not service/task name. Use
       ``--limit=rclonegateway``.
   * - A request with no credentials at all returns
       ``400 UnsupportedAlgorithm`` instead of ``403``
     - Confirmed rclone behavior, not a misconfiguration — a
       correctly-signed-but-wrong-key request still gets the expected
       ``403 InvalidAccessKeyId``. See ``EVALUATION.md`` Finding 2.
   * - ``rclonegw`` is pinned near 100%+ CPU with no network I/O, and stops
       responding
     - The multipart hang — see ``EVALUATION.md`` Finding 1.
       ``tutor local restart rclonegw`` clears it; it does not self-recover.
