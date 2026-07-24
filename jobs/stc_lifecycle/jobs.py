#
# Copyright (c) 2026  Rafay Systems, All rights reserved
# Author: Ramakrishna, Rafay
#
"""STC tenant lifecycle — consolidated closed-loop jobs (one file).

Replaces the earlier stc_provision_tenant / stc_hooks / stc_consistency packages with a single module:

  * STCTenantLifecycle     — ONE UI action that takes a tenant all the way, both directions:
        action=create  -> STCTenantOverlay (compile intent) + deploy the affected switches
        action=destroy -> STCTenantOffboard (clear intent)   + deploy the removal (drops it off the switches)
    Reuses the load-bearing STCTenantOverlay/STCTenantOffboard jobs (the f(T) compile + Design Builder
    deployment tracking; 98 deployments are bound to them) — no reimplementation. Gated by auto_approve.

  * STCTenantConsistency   — scheduled read-only SoT drift: desired (tag 'STC Fabric: Deployed') vs intent
        (config_context). Flags A1 desired-not-compiled / A2 compiled-not-desired.

  * STCTenantProvisionHook — JobHookReceiver: when a Tenant becomes desired, auto-COMPILE its intent
        (enqueues STCTenantOverlay). Deploy stays the gated STCTenantLifecycle step (no unreviewed fabric
        change from an object edit).

Config generation itself stays 100% template-driven (l5/l6/l7 Jinja) + data (allocations.py); these jobs
only orchestrate the existing compile + NVCM DeployWorkflow.
"""
import json
import time
import urllib.error
import urllib.request

from nautobot.apps.jobs import BooleanVar, ChoiceVar, IntegerVar, Job, register_jobs
from nautobot.dcim.models import Device
from nautobot.extras.jobs import JobHookReceiver
from nautobot.extras.models import Job as JobModel, JobResult
from nautobot.tenancy.models import Tenant
from nautobot.users.models import User

RENDER_API = "http://nv-config-manager-render-api:9000"
TEMPORAL_API = "http://nv-config-manager-temporal-api:9000"
COMPUTE = "RMDC-GPU-LETH01,RMDC-GPU-LETH02"
BORDERS = "RMDC-GPU-LETH05,RMDC-GPU-LETH06"
DCGW = "RMDC-DC-R-01"
AFFECTED = ["RMDC-GPU-LETH01", "RMDC-GPU-LETH02", "RMDC-GPU-LETH05", "RMDC-GPU-LETH06",
            "RMDC-DC-R-01", "RMDC-DC-R-02"]
name = "STC Tenant"   # UI grouping — share the group with the (hidden) compile jobs
DEPLOYED_TAG = "STC Fabric: Deployed"
MAX_TENANTS_PER_LEAF = 49   # VX NVUE truncates a compute-leaf candidate diff beyond ~this (IncompleteRead)
COMPUTE_LEAVES = COMPUTE.split(",")


# --- shared deploy helper (single copy; previously duplicated across STCProvisionDC/provision/server) ---
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


def _deploy_device(logger, d, auto):
    """Render + deploy one device via the NVCM DeployWorkflow. Returns the terminal status string."""
    did = str(d.id)
    try:
        _req("POST", f"{RENDER_API}/v1/render/{did}/render", timeout=120)
    except Exception as e:  # noqa: BLE001
        logger.failure("%s: render failed (%s)", d.name, e)
        return "render-failed"
    _s, b = _req("POST", f"{TEMPORAL_API}/v1/workflow/ngc/deploy", {"device_id": did})
    wf = b.get("id") if isinstance(b, dict) else None
    if not wf:
        logger.failure("%s: deploy POST failed (%s)", d.name, str(b)[:120])
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
                logger.warning("%s: HELD at %s — approve in NVCM (workflow %s) to apply", d.name, pending, wf)
                return "held"
        status = st.get("status")
        if status and status != "RUNNING":
            (logger.info if status == "COMPLETED" else logger.failure)(
                "%s: %s (failed_stage=%s)", d.name, status, st.get("failed_stage"))
            return status
    logger.failure("%s: timed out (workflow %s)", d.name, wf)
    return "timeout"


def _stage_device(logger, d, field, auto, batches=3):
    """Batched deploy of one device (Python mirror of stc_tenant_operations.sh stage-deploy): grow the
    device's config_context list (`tenants` or dcgw `peers`) in cumulative prefixes, deploying between
    each so every NVUE candidate diff stays under VX's ~82KB truncation point. Used when a one-shot
    deploy fails (a device whose full config exceeds the diff limit, e.g. a DC-GW's ~100 peers). Ends at
    the full (unchanged) list. Returns the terminal status of the last batch."""
    import math
    cc = dict(d.local_config_context_data or {})
    full = cc.get("tenants", []) if field == "tenants" else (cc.get("dcgw") or {}).get("peers", [])
    n = len(full)
    if n == 0:
        return _deploy_device(logger, d, auto)  # nothing large to stage; normal deploy
    logger.info("%s: AUTO-STAGING %s (%d entries, %d batches) — one-shot exceeded the VX diff limit", d.name, field, n, batches)
    status = "unknown"
    last = -1
    for i in range(1, batches + 1):
        k = min(n, math.ceil(n * i / batches))
        if k == last:
            continue
        last = k
        cc = dict(d.local_config_context_data or {})
        if field == "tenants":
            cc["tenants"] = full[:k]
        else:
            g = dict(cc.get("dcgw") or {}); g["peers"] = full[:k]; cc["dcgw"] = g
        d.local_config_context_data = cc
        d.save()
        logger.info("%s: staged %s[:%d/%d]", d.name, field, k, n)
        status = _deploy_device(logger, d, auto)
    return status


def _deploy_or_stage(logger, d, auto):
    """One-shot deploy; if it doesn't COMPLETE (and wasn't just gated-HELD), auto-stage the device by
    type (DC-GW -> peers, leaf -> tenants). Adaptive: fast for healthy devices, staged only where needed."""
    st = _deploy_device(logger, d, auto)
    if st in ("COMPLETED", "held"):
        return st
    field = "peers" if "DC-R-" in d.name else "tenants"
    return _stage_device(logger, d, field, auto)


def _run_compile(logger, user, job_class_name, **kwargs):
    """Run a compile job (STCTenantOverlay/STCTenantOffboard) synchronously; return True on success."""
    job = JobModel.objects.filter(module_name="stc_tenant.jobs", job_class_name=job_class_name,
                                  installed=True).first()
    if not job:
        logger.failure("%s (stc_tenant.jobs) not found/installed", job_class_name)
        return False
    jr = JobResult.enqueue_job(job, user, synchronous=True, **kwargs)
    ok = str(jr.status).upper() in ("SUCCESS", "COMPLETED")
    if not ok:
        logger.failure("%s failed (status=%s)", job_class_name, jr.status)
    return ok


class STCTenantLifecycle(Job):
    """One action, both directions: create (compile+deploy) or destroy (clear+deploy) a tenant, Nautobot↔switch."""

    action = ChoiceVar(choices=[("create", "create (provision → switch)"),
                                ("destroy", "destroy (deprovision ← switch)")],
                       default="create", description="Provision the tenant onto the fabric, or remove it.")
    tenant_number = IntegerVar(min_value=3, max_value=100, description="Tenant number T (3–100; f(T)).")
    auto_approve = BooleanVar(
        default=False,
        description="Apply to the switches hands-off. Leave UNCHECKED to HOLD each device at the NVCM "
                    "review gate (production change control).")

    class Meta:
        name = "1) Tenant — Create / Destroy (Nautobot ↔ switch)"
        description = ("One action for the whole path. create: STCTenantOverlay compile + deploy. "
                       "destroy: STCTenantOffboard clear + deploy the removal. Gated by auto_approve.")
        has_sensitive_variables = False
        # this job deploys up to ~6 devices sequentially (each a full render+DeployWorkflow poll, 30-90s),
        # so it needs far more than Celery's default soft limit — else it dies with SoftTimeLimitExceeded
        # mid-deploy (leaving a partial). 25 min soft / 30 min hard covers a worst-case 6-device run.
        soft_time_limit = 1500
        time_limit = 1800

    def run(self, action, tenant_number, auto_approve=False):
        name = f"tenant{tenant_number}"
        user = self.user or User.objects.filter(is_superuser=True).order_by("id").first()
        auto = bool(auto_approve)

        # Capacity guard (create only): VX's NVUE truncates a compute-leaf candidate diff beyond ~MAX
        # tenants (IncompleteRead in perform_configuration_diff). Fail fast with a clear message instead
        # of a partial deploy + timeout. Raise MAX_TENANTS_PER_LEAF only on capable HW, or free a slot.
        if action == "create":
            for dn in COMPUTE.split(","):
                d = Device.objects.filter(name=dn).first()
                if not d:
                    continue
                cur = [e for e in (d.local_config_context_data or {}).get("tenants", []) if e.get("vrf", "").startswith("tenant")]
                if not any(e.get("vrf") == name for e in cur) and len(cur) >= MAX_TENANTS_PER_LEAF:
                    self.logger.failure(
                        "REFUSING create %s: %s already has %d tenants (VX diff-safe max %d). The candidate "
                        "diff would truncate (IncompleteRead). Free a slot (destroy one) or stage the deploy "
                        "(stc_tenant_operations.sh stage-deploy).", name, dn, len(cur), MAX_TENANTS_PER_LEAF)
                    return {"tenant": name, "action": action, "refused": "capacity", "leaf": dn, "count": len(cur)}

        # 1. compile (create -> overlay ; destroy -> offboard), synchronously, reusing the existing jobs.
        if action == "create":
            self.logger.info("create %s: compiling overlay…", name)
            ok = _run_compile(self.logger, user, "STCTenantOverlay",
                              deployment_name=name, tenant_number=tenant_number,
                              compute_leaves=COMPUTE, border_leaves=BORDERS, dcgw_device=DCGW, access_port="")
            verb = "provisioned"
        else:
            self.logger.info("destroy %s: clearing intent (offboard)…", name)
            ok = _run_compile(self.logger, user, "STCTenantOffboard", tenant_number=tenant_number)
            verb = "deprovisioned"
        if not ok:
            self.logger.failure("%s: compile step failed — aborting before deploy", name)
            return {"tenant": name, "action": action, "compiled": False}
        self.logger.success("%s: intent updated in Nautobot (%s).", name, action)

        # 2. deploy the affected devices ONCE (declarative — create adds, destroy drops), gated.
        self.logger.info("deploying %d affected device(s) (approve=%s)…", len(AFFECTED), "auto" if auto else "manual-hold")
        results = {}
        for dn in AFFECTED:
            d = Device.objects.filter(name=dn).first()
            if d:
                results[dn] = _deploy_or_stage(self.logger, d, auto)
        completed = [n for n, r in results.items() if r == "COMPLETED"]
        held = [n for n, r in results.items() if r == "held"]
        bad = [n for n, r in results.items() if r not in ("COMPLETED", "held")]
        if bad:
            self.logger.failure("%s %s: %d applied, %d held, %d FAILED (%s)", name, verb, len(completed), len(held), len(bad), ", ".join(bad))
        elif held:
            self.logger.warning("%s: intent updated + %d device(s) HELD at review — approve in NVCM to apply", name, len(held))
        else:
            self.logger.success("%s %s: Nautobot AND %d device(s) on the fabric.", name, verb, len(completed))
        return {"tenant": name, "action": action, "compiled": True, "completed": completed, "held": held, "failed": bad}


class STCTenantConsistency(Job):
    """Read-only SoT drift: desired (tag) vs intent (config_context). Schedule for continuous checking."""

    fail_on_drift = BooleanVar(default=False, description="Mark the run FAILED on any drift (scheduled alerting).")

    class Meta:
        name = "2) Tenant — Drift Check (SoT vs fabric)"
        description = ("Compares Tenants tagged 'STC Fabric: Deployed' (desired) vs the tenant VRFs in the "
                       "compute leaves' config_context (intent). Flags A1 desired-not-compiled / A2 "
                       "compiled-not-desired. Schedule it for continuous SoT-vs-fabric drift.")
        has_sensitive_variables = False

    def run(self, fail_on_drift=False):
        desired = {t.name for t in Tenant.objects.filter(tags__name=DEPLOYED_TAG)}
        intent = set()
        for name in COMPUTE_LEAVES:
            d = Device.objects.filter(name=name).first()
            if not d:
                continue
            for e in (d.local_config_context_data or {}).get("tenants", []):
                if e.get("vrf", "").startswith("tenant"):
                    intent.add(e["vrf"])
        key = lambda n: int(n[6:]) if n[6:].isdigit() else 0  # noqa: E731
        a1 = sorted(desired - intent, key=key)
        a2 = sorted(intent - desired, key=key)
        self.logger.info("desired(tag)=%d  intent(config_context)=%d", len(desired), len(intent))
        if a1:
            self.logger.warning("A1 desired NOT in intent (%d): %s — run STCTenantLifecycle create", len(a1), a1)
        if a2:
            self.logger.warning("A2 intent NOT in desired (%d): %s — run STCTenantLifecycle destroy", len(a2), a2)
        drift = len(a1) + len(a2)
        if drift == 0:
            self.logger.success("CLEAN — desired == intent (%d tenants).", len(desired))
        elif fail_on_drift:
            self.logger.failure("consistency drift: %d (A1=%d, A2=%d)", drift, len(a1), len(a2))
        return {"desired": len(desired), "intent": len(intent), "a1": a1, "a2": a2, "drift": drift}


class STCTenantProvisionHook(JobHookReceiver):
    """On a Tenant becoming desired ('STC Fabric: Deployed'), auto-COMPILE its intent (deploy stays gated)."""

    class Meta:
        name = "STC Tenant — Auto-compile on desired (Job Hook)"
        hidden = True   # triggered by the JobHook, not run manually
        description = ("Job-hook receiver: when a Tenant is tagged 'STC Fabric: Deployed', enqueue "
                       "STCTenantOverlay to compile its intent. Does NOT deploy — the switch apply stays a "
                       "gated STCTenantLifecycle step. Idempotent + storm-safe.")
        has_sensitive_variables = False

    def receive_job_hook(self, change, action, changed_object):
        t = changed_object
        name = getattr(t, "name", "") or ""
        if not (name.startswith("tenant") and name[6:].isdigit()):
            return
        num = int(name[6:])
        if num < 3:
            self.logger.info("skip %s — tenant1/2 excluded (no f(T))", name)
            return
        if not t.tags.filter(name=DEPLOYED_TAG).exists():
            self.logger.info("%s not tagged Deployed — no action", name)
            return
        for dn in COMPUTE_LEAVES:
            d = Device.objects.filter(name=dn).first()
            if d and any(e.get("vrf") == name for e in (d.local_config_context_data or {}).get("tenants", [])):
                self.logger.info("%s already compiled — idempotent no-op", name)
                return
        user = self.user or User.objects.filter(is_superuser=True).order_by("id").first()
        if _run_compile(self.logger, user, "STCTenantOverlay",
                        deployment_name=name, tenant_number=num,
                        compute_leaves=COMPUTE, border_leaves=BORDERS, dcgw_device=DCGW, access_port=""):
            self.logger.success("%s became desired -> compiled intent. Deploy is GATED: run STCTenantLifecycle.", name)


register_jobs(STCTenantLifecycle, STCTenantConsistency, STCTenantProvisionHook)
