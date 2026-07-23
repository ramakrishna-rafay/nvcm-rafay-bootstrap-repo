#
# Copyright (c) 2026  Rafay Systems, All rights reserved
# Author: Ramakrishna, Rafay
# Revision history:
#   2026-07-15  Ramakrishna, Rafay  Initial version.
#
"""STCProvisionDC — the "click-and-go" whole-DC apply.

One action (click **Run** in the Nautobot UI, or `runjob`/REST) that **renders + deploys every
NVCM-managed device** from what Nautobot already holds — the entire fabric (underlay/BGP/EVPN/VXLAN) plus
every onboarded tenant — and reports the result. It's the UI/Job form of `stc_deploy_scripts/50_stc_dc_deploy.sh all`.

Approval:
  * `auto_approve = True`  (default, hands-off) — approve each diff and apply; use this to bring up /
    re-converge a **known-good** DC (e.g. reproducing on a fresh OCI). Nothing new to review.
  * `auto_approve = False` — HOLD each device at `perform_configuration_diff` for human review (the
    production gate); approve in NVCM to apply. Use this for change-management (Provisions 2 & 3).

Scope (honest): this **applies** what Nautobot defines. **Seeding** a fresh Nautobot — the fabric
(`STCFabricDesign`) and the tenants (`STCTenantOverlay` / `40_stc_tenant_onboard.sh`) — are the existing
one-click DesignJobs and remain the prerequisite; composing DesignJobs inside this job was deliberately
avoided to keep it reliable. So a from-empty reproduce = seed (those jobs) → then click STCProvisionDC;
an already-seeded DC re-converges in this single click.
"""
import json
import time
import urllib.request

from nautobot.apps.jobs import BooleanVar, Job, register_jobs
from nautobot.dcim.models import Device

name = "STC Provision"

RENDER_API = "http://nv-config-manager-render-api:9000"
TEMPORAL_API = "http://nv-config-manager-temporal-api:9000"


def _req(method, url, body=None, timeout=60):
    r = urllib.request.Request(
        url, data=(json.dumps(body).encode() if body is not None else None),
        method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as x:  # noqa: S310 (in-cluster service call)
            t = x.read().decode()
            try:
                return x.status, json.loads(t)
            except ValueError:
                return x.status, t
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


class STCProvisionDC(Job):
    """Click-and-go: render + deploy the entire DC (every managed device) from Nautobot via NVCM."""

    auto_approve = BooleanVar(
        default=True,
        description="Approve each diff and apply (hands-off — for a known-good DC). Uncheck to HOLD every "
                    "device at the review gate (production change-management).")

    def _managed_devices(self):
        """Every NVCM render-enabled device (fall back to devices with a mgmt IP)."""
        try:
            from nv_config_manager.models import ConfigManagerDeviceStatus as S
            devs = [s.device for s in S.objects.filter(render_enabled=True).select_related("device")]
            if devs:
                return sorted(devs, key=lambda d: d.name)
        except Exception:  # noqa: BLE001
            pass
        return sorted([d for d in Device.objects.all() if d.primary_ip4], key=lambda d: d.name)

    def _deploy_one(self, d, auto):
        did = str(d.id)
        try:
            _req("POST", f"{RENDER_API}/v1/render/{did}/render", timeout=90)
        except Exception as e:  # noqa: BLE001
            self.logger.failure("%s: render failed (%s)", d.name, e)
            return "render-failed"
        _s, b = _req("POST", f"{TEMPORAL_API}/v1/workflow/ngc/deploy", {"device_id": did})
        wf = b.get("id") if isinstance(b, dict) else None
        if not wf:
            self.logger.failure("%s: deploy POST failed (%s)", d.name, str(b)[:120])
            return "deploy-failed"
        ap = set()
        for _ in range(75):
            time.sleep(4)
            _g, st = _req("GET", f"{TEMPORAL_API}/v1/workflow/{wf}")
            if not isinstance(st, dict):
                continue
            if st.get("pending_approval"):
                pending = [stg.get("name") for stg in st.get("stages", [])
                           if stg.get("requires_approval") and stg.get("state") != "COMPLETE"]
                if auto:
                    for nm in pending:
                        if nm not in ap:
                            _req("POST", f"{TEMPORAL_API}/v1/workflow/{wf}/approve/{nm}")
                            ap.add(nm)
                else:
                    self.logger.warning("%s: HELD at %s — review + approve in NVCM (workflow %s)",
                                        d.name, pending, wf)
                    return "held"
            status = st.get("status")
            if status and status != "RUNNING":
                (self.logger.info if status == "COMPLETED" else self.logger.failure)(
                    "%s: %s (failed_stage=%s)", d.name, status, st.get("failed_stage"))
                return status
        self.logger.failure("%s: timed out (workflow %s)", d.name, wf)
        return "timeout"

    def run(self, *args, **kwargs):
        auto = bool(kwargs.get("auto_approve", True))
        devices = self._managed_devices()
        self.logger.info("STCProvisionDC: %d managed device(s); approve=%s",
                         len(devices), "auto" if auto else "manual-hold")
        results = {}
        for d in devices:
            results[d.name] = self._deploy_one(d, auto)
        completed = [n for n, r in results.items() if r == "COMPLETED"]
        held = [n for n, r in results.items() if r == "held"]
        bad = [n for n, r in results.items() if r not in ("COMPLETED", "held")]
        if bad:
            self.logger.failure("PROVISION: %d COMPLETED, %d held, %d FAILED (%s)",
                                len(completed), len(held), len(bad), ", ".join(bad))
        elif held:
            self.logger.warning("PROVISION: %d COMPLETED, %d HELD for review (%s) — approve in NVCM to apply",
                                len(completed), len(held), ", ".join(held))
        else:
            self.logger.info("PROVISION COMPLETE: all %d device(s) applied. Verify with DriftDetect.",
                             len(completed))
        return {"completed": completed, "held": held, "failed": bad}

    class Meta:
        name = "STC Provision — bring up the entire DC (click-and-go)"
        version = "1.0.0"
        has_sensitive_variables = False
        description = ("One action to render + deploy every NVCM-managed device from Nautobot — the whole "
                       "fabric + all onboarded tenants. auto_approve=True applies hands-off (known-good DC); "
                       "False holds each diff at the review gate. Seeding (fabric/tenant DesignJobs) is the "
                       "prerequisite; this applies what Nautobot defines.")


register_jobs(STCProvisionDC)
