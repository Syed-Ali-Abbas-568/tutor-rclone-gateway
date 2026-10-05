Azure Blob Storage for Open edX via `rclone <https://rclone.org>`_
====================================================================

This is a plugin for `Tutor <https://docs.tutor.edly.io>`_ that configures Open
edX to use Azure Blob Storage, by running `rclone <https://rclone.org>`_'s
``serve s3`` command as a translation layer: Open edX keeps speaking the S3 API
it always has, and rclone translates those requests onto an Azure Blob Storage
account behind it.

.. warning::

   **Tested, and found not safe for production course content yet.** This
   plugin's own live conformance suite reproducibly hangs
   ``rclone serve s3`` — a livelocked process, pinned CPU, zero network
   progress, no self-recovery — when multipart upload parts arrive out of
   ascending order, a pattern the S3 API explicitly permits, once the upload
   crosses roughly 300 MiB. This was found against the plugin's own default
   backend (Azurite), not a hypothetical. **Read `EVALUATION.md
   <EVALUATION.md>`_ before using this plugin for anything beyond
   evaluation** — it has the full reproduction steps, bisection table, and
   everything else this plugin's testing found, good and bad.

   This plugin exists to answer a question, not to be a default
   recommendation. MinIO used to offer a "gateway" mode that did this same
   kind of translation for several backends, including Azure. MinIO deprecated
   gateway mode in 2022, and RustFS — the plugin that replaced MinIO in this
   family, `tutor-rustfs <https://github.com/edly-io/tutor-rustfs>`_ — never
   had one. Open edX's own community already hit a serious failure running
   almost exactly this architecture with MinIO's gateway mode in front of
   Azure Blob Storage: multipart course-export uploads failed to complete,
   and the only fix anyone found was abandoning the gateway approach
   entirely. This plugin's own finding, above, rhymes with that one closely
   enough to take seriously.

Which plugin do I want?
-----------------------

.. list-table::
   :header-rows: 1

   * - You want
     - Use
   * - An S3 server on your own hardware, alongside Open edX
     - `tutor-rustfs <https://github.com/edly-io/tutor-rustfs>`_
   * - AWS S3, GCS, Ceph, or any S3-compatible provider
     - `tutor-contrib-s3 <https://github.com/cleura/tutor-contrib-s3>`_
   * - Azure Blob Storage
     - **this plugin** — with the caveats above

``tutor-contrib-s3`` only works for backends that already speak the S3 API
natively. Azure Blob Storage doesn't, which is the entire reason this plugin
(and the rclone translation layer inside it) exists.

How it works
------------

Open edX talks to one S3 endpoint, same as it would with ``tutor-rustfs``. On
the other side of that endpoint are **two** ``rclone serve s3`` processes
sharing one Azure Blob Storage account, not one server:

- ``rclonegw`` requires credentials and can read and write every bucket.
- ``rclonegw-public`` has **no credentials at all**, is read-only, and is
  scoped to only the one bucket that must be anonymously readable (forum image
  uploads are plain ``<img>`` tags baked permanently into stored post HTML, so
  presigned URLs don't work for them — they would eventually expire).

A Caddy rule on the single public hostname splits traffic between the two by
HTTP method and path: ``GET``/``HEAD`` on the public bucket's path goes to
``rclonegw-public``; everything else goes to ``rclonegw``. This two-process
split exists because **rclone's ``serve s3`` has no bucket-level ACL or policy
mechanism at all** — unlike RustFS/MinIO, where a single ``rc anonymous set
download`` command does this. See `EVALUATION.md <EVALUATION.md>`_ for the
full architectural reasoning, including the adversarial design review that
caught a routing bug in an earlier draft of this split before it shipped.

Azure containers stay fully private at the Azure IAM layer throughout —
nothing here ever changes a native Azure ACL. "Public" is purely a property of
the two-process topology described above.

Unlike ``tutor-rustfs``, this plugin stores **no data locally at all**: every
object lives in Azure, so there's no bind-mounted volume, no non-root/chown
dance on a fresh Linux install, and no Kubernetes PVC.

Installation
------------

::

    pip install tutor-rclone-gateway
    tutor plugins enable rclonegateway

.. note::

   This plugin cannot run alongside ``tutor-minio`` or ``tutor-rustfs``. All
   three configure Open edX's default object storage. Enabling more than one
   raises an error.

Testing without a real Azure account
-------------------------------------

By default, this plugin points at `Azurite
<https://github.com/Azure/Azurite>`_ — Microsoft's own free, local Azure
Storage emulator — so ``tutor local launch`` works immediately with no Azure
subscription of any kind. Azurite's account name and key are fixed, publicly
documented constants (the same for every Azurite install everywhere), **not
secrets**.

To use a real Azure Storage account instead, override all three of the
following together:

- ``RCLONEGW_AZURE_ACCOUNT``
- ``RCLONEGW_AZURE_ACCOUNT_KEY``
- ``RCLONEGW_AZURE_ENDPOINT`` (e.g. ``https://<account>.blob.core.windows.net``)

See `TESTING.rst <TESTING.rst>`_ and `EVALUATION.md <EVALUATION.md>`_ before
doing this for anything beyond evaluation: the multipart-upload behavior that
matters most for course import/export has only been fully validated against
Azurite by default, and Azurite's own documentation disclaims any performance
guarantee — it does not model the concurrency conditions that matter most for
the known failure mode this plugin was built to test for.

Configuration
-------------

Shared with Open edX (these drive the S3 client settings):

- ``OPENEDX_AWS_ACCESS_KEY`` (default: ``"openedx"``)
- ``OPENEDX_AWS_SECRET_ACCESS_KEY`` (default: randomly generated)

Buckets — the default names match ``tutor-rustfs``'s and ``tutor-minio``'s:

- ``RCLONEGW_BUCKET_NAME`` (default: ``"openedx"``) — also the one bucket
  that is anonymously readable.
- ``RCLONEGW_FILE_UPLOAD_BUCKET_NAME`` (default: ``"openedxuploads"``)
- ``RCLONEGW_VIDEO_UPLOAD_BUCKET_NAME`` (default: ``"openedxvideos"``)
- ``RCLONEGW_GRADES_BUCKET_NAME`` (default: ``"openedxgrades"``)
- ``RCLONEGW_OPENEDX_LEARNING_BUCKET_NAME`` (default: ``"openedxlearning"``)
- ``RCLONEGW_DISCOVERY_BUCKET_NAME`` (default: ``"discoveryuploads"`` when the
  ``discovery`` plugin is enabled, otherwise empty) — also anonymously
  readable, for the same reason as the main bucket.

Hosts:

- ``RCLONEGW_HOST`` (default: ``"files.{{ LMS_HOST }}"``) — the single S3 API
  endpoint for both the authenticated and anonymous backends. **This must
  resolve from learners' browsers**, not only from the Open edX containers.

Server:

- ``RCLONEGW_DOCKER_IMAGE`` (default: ``"docker.io/rclone/rclone:1.75.1"``) —
  **hard-pinned to an exact tag, not a floating range.** This plugin's
  multipart-upload testing (see ``EVALUATION.md``) is only valid for the exact
  version tested. 1.75.1 fixes CVE-2026-88045, an unauthenticated
  memory-exhaustion denial-of-service in the multipart upload path — never go
  below it. If you bump this, re-run the live conformance suite against the
  new tag first.
- ``RCLONEGW_UID`` / ``RCLONEGW_GID`` (both default: ``1009``) — the non-root
  user inside the image.
- ``RCLONEGW_REGION`` (default: ``"us-east-1"``)
- ``RCLONEGW_QUERYSTRING_AUTH`` (default: ``true``)
- ``RCLONEGW_AZURE_ACCOUNT`` / ``RCLONEGW_AZURE_ACCOUNT_KEY`` /
  ``RCLONEGW_AZURE_ENDPOINT`` — see "Testing without a real Azure account"
  above.
- ``RCLONEGW_AZURITE_DOCKER_IMAGE`` (default:
  ``"mcr.microsoft.com/azure-storage/azurite:3.37.0"``)

There is no ``RCLONEGW_CONSOLE_HOST``: rclone has no web console or bucket
browser. Use the Azure Portal, Azure Storage Explorer, or ``rclone ncdu``
against the same ``azureblob:`` remote instead.

DNS records
-----------

``RCLONEGW_HOST`` must point at your server.

Kubernetes
----------

This plugin ships full Kubernetes parity (``tutor k8s ...``), including a
``NetworkPolicy`` that restricts both gateway Deployments to ingress from the
Caddy pod only. This is **not optional hardening** — without it, both
Deployments would be reachable from any pod in the cluster via Service DNS,
which would make ``rclonegw-public``'s anonymous, read-only scoping the
*only* thing standing between a compromised workload anywhere in the cluster
and the storage layer, rather than a defense-in-depth measure behind Caddy's
own routing.

Testing
-------

See `TESTING.rst <TESTING.rst>`_ for the full tiered procedure, and
`EVALUATION.md <EVALUATION.md>`_ for the results of running it and what they
mean for whether this plugin is a good idea for your deployment.

::

    pip install -e ".[dev]"
    make test

License
-------

This work is licensed under the terms of the `GNU Affero General Public License
(AGPL) <https://www.gnu.org/licenses/agpl-3.0.en.html>`_.

Maintained by
--------------

This Tutor plugin is maintained by Syed Ali Abbas from
`Edly <https://edly.io>`__. Community support is available from the official
`Open edX forum <https://discuss.openedx.org>`__, specifically `the thread
that prompted this evaluation
<https://discuss.openedx.org/t/rustfs-plugin-to-replace-tutor-minio-thoughts-on-azure-gateway/19670>`__.
Do you need help with this plugin? See the `troubleshooting
<https://docs.tutor.edly.io/troubleshooting.html>`__ section from the Tutor
documentation.
