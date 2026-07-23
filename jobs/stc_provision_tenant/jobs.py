#
# Copyright (c) 2026  Rafay Systems, All rights reserved
# Author: Ramakrishna, Rafay
#
"""STCTenantProvision — ONE UI action that takes a tenant all the way: Nautobot SoT -> the switches.

Composes the two proven halves so an operator doesn't have to run two jobs:
  1. COMPILE — runs the existing STCTenantOverlay synchronously (writes the tenant's f(T) overlay into
     config_context + creates the VRF object; visible in Nautobot immediately).
  2. DEPLOY  — renders + deploys each affected device via the NVCM DeployWorkflow (the switch apply),
     the same mechanism STCProvisionDC uses. Gated: auto_approve applies; unchecked HOLDS each device
     at the NVCM review gate (production change control).

Result: run this one job -> the tenant appears in Nautobot AND lands on the switches. Reuses
STCTenantOverlay (no reimplementation of f(T)) and the proven render/temporal deploy path.
"""
import json
import time
import urllib.error
import urllib.request

from nautobot.apps.jobs import BooleanVar, IntegerVar, Job, register_jobs
from nautobot.dcim.models import Device
from nautobot.extras.models import Job as JobModel, JobResult
from nautobot.users.models import User

RENDER_API = "http://nv-config-manager-render-api:9000"
TEMPORAL_API = "http://nv-config-manager-temporal-api:9000"
COMPUTE = "RMDC-GPU-LETH01,RMDC-GPU-LETH02"
BORDERS = "RMDC-GPU-LETH05,RMDC-GPU-LETH06"
DCGW = "RMDC-DC-R-01"
# devices a tenant touches (compute leaves + border leaves + DC-GW pair)
AFFECTED = ["RMDC-GPU-LETH01", "RMDC-GPU-LETH02", "RMDC-GPU-LETH05", "RMDC-GPU-LETH06",
            "RMDC-DC-R-01", "RMDC-DC-R-02"]


def _req(method, url, body=None, timeout=60):
    r = urllib.request.Request(
        url, data=(json.dumps(body).encode() if body is not None else None),
        method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as x:  # noqa: S310
            t = x.read().decode()
            try:
                return x.status, json.loads(t)
            except ValueError:
                return x.status, t
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


class STCTenantProvision(Job):
    """One action: compile a tenant's overlay (Nautobot) then deploy it to the switches (gated)."""

    tenant_number = IntegerVar(min_value=3, max_value=100, description="Tenant number T (3–100; f(T)).")
    auto_approve = BooleanVar(
        default=False,
        description="Apply to the switches hands-off. Leave UNCHECKED (default) to HOLD each device at the "
                    "NVCM review gate — approve there to apply (production change control).")

    class Meta:
        name = "STC Tenant — Provision (Nautobot → switch, one action)"
        description = ("Runs STCTenantOverlay to compile the tenant's intent, then renders + deploys the "
                       "affected switches via the NVCM DeployWorkflow. One job = tenant in Nautobot AND on "
                       "the fabric. Gated by auto_approve.")
        has_sensitive_variables = False

    def _deploy_one(self, d, auto):
        did = str(d.id)
        try:
            _req("POST", f"{RENDER_API}/v1/render/{did}/render", timeout=120)
        except Exception as e:  # noqa: BLE001
            self.logger.failure("%s: render failed (%s)", d.name, e)
            return "render-failed"
        _s, b = _req("POST", f"{TEMPORAL_API}/v1/workflow/ngc/deploy", {"device_id": did})
        wf = b.get("id") if isinstance(b, dict) else None
        if not wf:
            self.logger.failure("%s: deploy POST failed (%s)", d.name, str(b)[:120])
            return "deploy-failed"
        ap = set()
        for _ in range(80):
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
                    self.logger.warning("%s: HELD at %s — approve in NVCM (workflow %s) to apply", d.name, pending, wf)
                    return "held"
            status = st.get("status")
            if status and status != "RUNNING":
                (self.logger.info if status == "COMPLETED" else self.logger.failure)(
                    "%s: %s (failed_stage=%s)", d.name, status, st.get("failed_stage"))
                return status
        self.logger.failure("%s: timed out (workflow %s)", d.name, wf)
        return "timeout"

    def run(self, tenant_number, auto_approve=False):
        name = f"tenant{tenant_number}"
        user = self.user or User.objects.filter(is_superuser=True).order_by("id").first()

        # 1. COMPILE (synchronous) — reuse the existing overlay job; writes config_context + VRF.
        overlay = JobModel.objects.filter(module_name="stc_tenant.jobs", job_class_name="STCTenantOverlay",
                                          installed=True).first()
        if not overlay:
            self.logger.failure("STCTenantOverlay not found/installed — cannot compile %s", name)
            return {"tenant": name, "compiled": False}
        self.logger.info("compiling %s intent (STCTenantOverlay, synchronous)…", name)
        jr = JobResult.enqueue_job(
            overlay, user, synchronous=True,
            deployment_name=name, tenant_number=tenant_number,
            compute_leaves=COMPUTE, border_leaves=BORDERS, dcgw_device=DCGW, access_port="")
        if str(jr.status).upper() not in ("SUCCESS", "COMPLETED"):
            self.logger.failure("%s compile failed (status=%s) — aborting before deploy", name, jr.status)
            return {"tenant": name, "compiled": False, "compile_status": str(jr.status)}
        self.logger.success("%s intent compiled in Nautobot (VRF + config_context).", name)

        # 2. DEPLOY the affected devices (gated).
        auto = bool(auto_approve)
        self.logger.info("deploying %d affected device(s) for %s (approve=%s)…",
                         len(AFFECTED), name, "auto" if auto else "manual-hold")
        results = {}
        for dn in AFFECTED:
            d = Device.objects.filter(name=dn).first()
            if not d:
                continue
            results[dn] = self._deploy_one(d, auto)
        completed = [n for n, r in results.items() if r == "COMPLETED"]
        held = [n for n, r in results.items() if r == "held"]
        bad = [n for n, r in results.items() if r not in ("COMPLETED", "held")]
        if bad:
            self.logger.failure("%s: %d applied, %d held, %d FAILED (%s)", name, len(completed), len(held), len(bad), ", ".join(bad))
        elif held:
            self.logger.warning("%s: compiled + %d device(s) HELD at review — approve in NVCM to land on the fabric", name, len(held))
        else:
            self.logger.success("%s PROVISIONED: in Nautobot AND applied to %d device(s).", name, len(completed))
        return {"tenant": name, "compiled": True, "completed": completed, "held": held, "failed": bad}


register_jobs(STCTenantProvision)
