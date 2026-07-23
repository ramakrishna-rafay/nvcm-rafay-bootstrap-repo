#
# Copyright (c) 2026  Rafay Systems, All rights reserved
# Author: Ramakrishna, Rafay
# Revision history:
#   2026-07-15  Ramakrishna, Rafay  Initial version.
#
"""STCTenantIB — provision one tenant's InfiniBand plane (PKey + SR-IOV rails).

This is the IB counterpart to `STCTenantOverlay` (which does the Ethernet plane). It is **config-ready**:
everything it needs is computed from the single-source allocations and rendered here; the parts that
touch real hardware are gated behind `execute` and clearly marked, because our lab is Cumulus VX Ethernet
with no InfiniBand fabric.

Two planes, two delivery paths:
  * **PKey** → NVCM already ships UFM-driven workflows (`ib_pkey_creation`, `ib_pkey_member_add`). With
    `execute=True` and a `ufm_host`, this job drives them. With `execute=False` (default) it emits the
    exact request payloads for review — no UFM needed.
  * **Rail** → NV-IPAM `IPPool` CRDs applied on the GPU Kubernetes cluster (not via NVCM). The job always
    emits the manifests as YAML for the cluster operator to apply; there is no switch/NVCM execution path.

Run: `nautobot-server runjob stc_ib.jobs.STCTenantIB -u admin -l -d '{"tenant_number":3}'` (dry-run).
"""
import json
import urllib.request

import yaml
from nautobot.apps.jobs import BooleanVar, IntegerVar, Job, StringVar, register_jobs

from ..stc_tenant.allocations import pkey, rail_subnets

from .ib_render import pkey_creation_payload, pkey_member_add_payload, rail_ippool_crds

name = "STC InfiniBand"

TEMPORAL_API = "http://nv-config-manager-temporal-api:9000"


def _req(method, url, body=None, timeout=30):
    r = urllib.request.Request(
        url, data=(json.dumps(body).encode() if body is not None else None),
        method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as x:  # noqa: S310 (in-cluster service call)
        raw = x.read().decode()
        try:
            return x.status, json.loads(raw)
        except ValueError:
            return x.status, raw


class STCTenantIB(Job):
    """Render (and optionally provision) a tenant's InfiniBand PKey + SR-IOV rail IPPools."""

    tenant_number = IntegerVar(min_value=3, max_value=100, description="Tenant number T (3–100).")
    ufm_host = StringVar(default="", required=False,
                         description="UFM host/device NVCM authenticates to (required only when execute=True).")
    member_guids = StringVar(default="", required=False,
                             description="Comma-separated HCA/GPU port GUIDs to add to the PKey. "
                                         "Blank = none (in production these come from ib_port_guid_discovery).")
    membership_type = StringVar(default="full", description="PKey membership: full | limited.")
    rail_namespace = StringVar(default="kube-system",
                               description="Namespace for the NV-IPAM IPPool CRDs on the GPU cluster.")
    execute = BooleanVar(default=False,
                         description="False (default) = dry-run: emit PKey payloads + rail IPPool YAML only. "
                                     "True = drive NVCM's UFM ib_pkey workflows (needs ufm_host + a live UFM).")

    def run(self, *args, **kwargs):
        t = int(kwargs["tenant_number"])
        ufm = (kwargs.get("ufm_host") or "").strip()
        guids = [g.strip() for g in (kwargs.get("member_guids") or "").split(",") if g.strip()]
        membership = kwargs.get("membership_type") or "full"
        ns = kwargs.get("rail_namespace") or "kube-system"
        execute = bool(kwargs.get("execute", False))

        pk = pkey(t)
        self.logger.info("tenant%s InfiniBand intent: PKey %s (dec %s); rails %s",
                         t, pk["hex"], pk["dec"], ", ".join(rail_subnets(t)))

        # ---- Rail: always emit NV-IPAM IPPool CRDs (applied on the GPU cluster, not here) ----
        crds = rail_ippool_crds(t, namespace=ns)
        manifest = yaml.safe_dump_all(crds, sort_keys=False)
        self.logger.info("tenant%s NV-IPAM rail IPPools (%d) — apply on the GPU cluster:\n%s",
                         t, len(crds), manifest)

        # ---- PKey: emit payloads (dry-run) or drive NVCM's UFM workflows (execute) ----
        create = pkey_creation_payload(t, ufm or "<UFM_HOST>")
        add = pkey_member_add_payload(t, ufm or "<UFM_HOST>", guids, membership_type=membership)
        if not execute:
            self.logger.info("tenant%s PKey — dry-run (set execute=True + ufm_host to provision via UFM):\n"
                             "  ib_pkey_creation  <- %s\n  ib_pkey_member_add <- %s",
                             t, json.dumps(create), json.dumps(add))
            return {"pkey": pk["hex"], "rails": rail_subnets(t), "rail_ippools": len(crds), "executed": False}

        if not ufm:
            self.logger.failure("execute=True requires ufm_host — aborting")
            return {"pkey": pk["hex"], "executed": False, "error": "ufm_host required"}
        # drive NVCM's UFM workflows (untested against a real UFM in the VX lab — pending IB hardware).
        # POLL each workflow to completion (not fire-and-forget) and gate member-add on PKey creation.
        self.logger.warning("executing against UFM %s — this path is unverified on the VX lab (no IB fabric)", ufm)
        if not self._run_wf("ib_pkey_creation", create):
            self.logger.failure("ib_pkey_creation did not complete — not adding members")
            return {"pkey": pk["hex"], "executed": False, "error": "ib_pkey_creation failed"}
        if guids:
            self._run_wf("ib_pkey_member_add", add)
        else:
            self.logger.info("no member GUIDs supplied — PKey created, no members added "
                             "(production: discover GUIDs via ib_port_guid_discovery)")
        return {"pkey": pk["hex"], "rails": rail_subnets(t), "rail_ippools": len(crds), "executed": True}

    def _run_wf(self, kind, body, tries=75, delay=4):
        """POST an NVCM workflow and poll it to completion. Returns True only on COMPLETED. Mirrors the
        deploy/backup/drift polling pattern so the execute path verifies rather than fires-and-forgets."""
        import time
        _s, b = _req("POST", f"{TEMPORAL_API}/v1/workflow/ngc/{kind}", body)
        wf = b.get("id") if isinstance(b, dict) else None
        if not wf:
            self.logger.failure("%s: no workflow id (%s %s)", kind, _s, str(b)[:150])
            return False
        for _ in range(tries):
            time.sleep(delay)
            try:
                _g, st = _req("GET", f"{TEMPORAL_API}/v1/workflow/{wf}")
            except Exception:  # noqa: BLE001
                continue
            if isinstance(st, dict) and st.get("status") and st["status"] != "RUNNING":
                status = st["status"]
                log = self.logger.info if status == "COMPLETED" else self.logger.failure
                log("%s: %s (failed_stage=%s)", kind, status, st.get("failed_stage"))
                return status == "COMPLETED"
        self.logger.failure("%s: timed out waiting for completion", kind)
        return False

    class Meta:
        name = "STC InfiniBand — PKey + SR-IOV rails (config-ready)"
        version = "1.0.0"
        has_sensitive_variables = False
        description = ("Render a tenant's InfiniBand PKey (0x8000+T) and 8 SR-IOV rail /24s: emit the NVCM "
                       "ib_pkey_* payloads + NV-IPAM IPPool manifests (dry-run), or drive NVCM's UFM PKey "
                       "workflows (execute=True, pending a live UFM + IB fabric). Rails are applied on the "
                       "GPU cluster by NV-IPAM.")


register_jobs(STCTenantIB)
