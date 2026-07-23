#
# Copyright (c) 2026  Rafay Systems, All rights reserved
# Author: Ramakrishna, Rafay
#
"""STCTenantProvisionHook — auto-START the provisioning pipeline when a Tenant becomes desired.

Wired (via a JobHook on the Tenant content-type, create+update) to fire when a Tenant is tagged
"STC Fabric: Deployed". It then enqueues the existing STCTenantOverlay job to COMPILE that tenant's f(T)
overlay into the leaf config_context (intent). It deliberately does NOT deploy to the switches — the
deploy stays a GATED step (STCProvisionDC / 63_ reconcile, held at the NVCM review gate), so a tenant
edit can never push an unreviewed change onto the fabric. That gives the target behaviour
("mark a tenant desired -> its intent is auto-prepared") while keeping change control.

Guards (idempotent, storm-safe): acts only when the tenant is a numbered STC tenant (T>=3; tenant1/2
excluded), is tagged Deployed, and is NOT already compiled on a compute leaf. Any other Tenant save is a
no-op. Read-mostly: its only side effect is enqueuing the (already-proven, gated-deploy-separate) overlay
compile.
"""
from nautobot.dcim.models import Device
from nautobot.extras.jobs import JobHookReceiver, register_jobs
from nautobot.extras.models import Job, JobResult
from nautobot.users.models import User

DEPLOYED_TAG = "STC Fabric: Deployed"
COMPUTE = "RMDC-GPU-LETH01,RMDC-GPU-LETH02"
BORDERS = "RMDC-GPU-LETH05,RMDC-GPU-LETH06"
DCGW = "RMDC-DC-R-01"
OVERLAY_MODULE = "stc_tenant.jobs"          # the active (JOBS_ROOT/PVC) STCTenantOverlay
OVERLAY_CLASS = "STCTenantOverlay"


class STCTenantProvisionHook(JobHookReceiver):
    """On a Tenant becoming desired ('STC Fabric: Deployed'), enqueue its overlay COMPILE. Deploy stays gated."""

    class Meta:
        name = "STC Tenant — Auto-compile on desired (Job Hook)"
        description = ("Job-hook receiver: when a Tenant is tagged 'STC Fabric: Deployed', enqueue "
                       "STCTenantOverlay to compile its intent into config_context. Does NOT deploy — the "
                       "switch apply stays a gated STCProvisionDC/reconcile step. Idempotent + storm-safe.")
        has_sensitive_variables = False

    def receive_job_hook(self, change, action, changed_object):
        t = changed_object
        name = getattr(t, "name", "") or ""
        if not (name.startswith("tenant") and name[6:].isdigit()):
            self.logger.info("skip %s — not a numbered STC tenant", name or "<obj>")
            return
        num = int(name[6:])
        if num < 3:
            self.logger.info("skip %s — tenant1/2 excluded (no f(T) allocation)", name)
            return
        if not t.tags.filter(name=DEPLOYED_TAG).exists():
            self.logger.info("%s not tagged '%s' — no action (reserved)", name, DEPLOYED_TAG)
            return
        for dn in COMPUTE.split(","):
            d = Device.objects.filter(name=dn).first()
            if d and any(e.get("vrf") == name for e in (d.local_config_context_data or {}).get("tenants", [])):
                self.logger.info("%s already compiled on %s — idempotent no-op", name, dn)
                return
        job = Job.objects.filter(module_name=OVERLAY_MODULE, job_class_name=OVERLAY_CLASS, installed=True).first()
        if not job:
            self.logger.warning("STCTenantOverlay (%s) not found/installed — cannot auto-compile %s", OVERLAY_MODULE, name)
            return
        user = self.user or User.objects.filter(is_superuser=True).order_by("id").first()
        JobResult.enqueue_job(
            job, user,
            deployment_name=name, tenant_number=num,
            compute_leaves=COMPUTE, border_leaves=BORDERS, dcgw_device=DCGW, access_port="",
        )
        self.logger.success(
            "%s became desired -> enqueued STCTenantOverlay (compile intent). Deploy is GATED: run "
            "STCProvisionDC / 63_ reconcile to apply to the switches.", name)


register_jobs(STCTenantProvisionHook)
