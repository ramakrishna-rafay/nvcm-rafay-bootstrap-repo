#
# Copyright (c) 2026  Rafay Systems, All rights reserved
# Author: Ramakrishna, Rafay
#
"""STCTenantConsistency — scheduled SoT-vs-intent drift detector.

Reports the tenant-level consistency between the two Nautobot-native layers:
  DESIRED  — Tenants tagged "STC Fabric: Deployed" (the authoritative want), vs
  INTENT   — the tenant VRFs present in the compute leaves' config_context (what will be rendered/deployed).

This is the pure-Nautobot half of the 3-way check (needs no switch access) — it catches the "SoT objects
that are not on the fabric" class (A1) and "deployed-but-not-wanted" (A2). The INTENT-vs-REALIZED (switch)
half is covered by the "STC Drift — Detect" job (per-line, via backup) and by 64_stc_consistency.sh
(on-demand full 3-way with live NVUE). Read-only — never changes config. Schedule it (Jobs -> this ->
Schedule, e.g. hourly) so a divergence is surfaced automatically.
"""
from nautobot.apps.jobs import BooleanVar, Job, register_jobs
from nautobot.dcim.models import Device
from nautobot.tenancy.models import Tenant

COMPUTE_LEAVES = ["RMDC-GPU-LETH01", "RMDC-GPU-LETH02"]
DEPLOYED_TAG = "STC Fabric: Deployed"


class STCTenantConsistency(Job):
    """Report SoT(desired-tag) vs intent(config_context) tenant drift. Read-only."""

    fail_on_drift = BooleanVar(
        default=False,
        description="Mark the job run FAILED when any drift is found (use for scheduled alerting).",
    )

    class Meta:
        name = "STC Tenant — Consistency (desired vs intent)"
        description = ("Read-only: compares Tenants tagged 'STC Fabric: Deployed' (desired) against the "
                       "tenant VRFs in the compute leaves' config_context (intent). Flags A1 desired-not-"
                       "compiled and A2 compiled-not-desired. Schedule it for continuous SoT-vs-fabric drift.")
        has_sensitive_variables = False

    def run(self, fail_on_drift=False):
        desired = {t.name for t in Tenant.objects.filter(tags__name=DEPLOYED_TAG)}
        intent = set()
        for name in COMPUTE_LEAVES:
            d = Device.objects.filter(name=name).first()
            if not d:
                self.logger.warning("compute leaf %s not found — skipping", name)
                continue
            for e in (d.local_config_context_data or {}).get("tenants", []):
                if e.get("vrf", "").startswith("tenant"):
                    intent.add(e["vrf"])

        a1 = sorted(desired - intent, key=lambda n: int(n[6:]) if n[6:].isdigit() else 0)
        a2 = sorted(intent - desired, key=lambda n: int(n[6:]) if n[6:].isdigit() else 0)
        self.logger.info("desired(tag 'STC Fabric: Deployed')=%d  intent(config_context)=%d", len(desired), len(intent))
        if a1:
            self.logger.warning("A1 desired NOT in intent (%d): %s — declared but not compiled; run reconcile (63_)", len(a1), a1)
        if a2:
            self.logger.warning("A2 intent NOT in desired (%d): %s — deployed but not wanted; reconcile will remove", len(a2), a2)
        drift = len(a1) + len(a2)
        if drift == 0:
            self.logger.success("CLEAN — desired == intent (%d tenants); SoT matches compiled intent", len(desired))
        elif fail_on_drift:
            self.logger.failure("consistency drift: %d tenant(s) diverge (A1=%d, A2=%d)", drift, len(a1), len(a2))
        return {"desired": len(desired), "intent": len(intent), "a1": a1, "a2": a2, "drift": drift}


register_jobs(STCTenantConsistency)
