#
# Copyright (c) 2026  Rafay Systems, All rights reserved
# Author: Ramakrishna, Rafay
# Revision history:
#   2026-07-15  Ramakrishna, Rafay  Initial version (single Underlay + Cables job).
#   2026-07-21  Ramakrishna, Rafay  Split into three selectable DesignJobs — Devices, Cables,
#                                   and Devices + Cables — for GitOps delivery.
#
"""STC Fabric — Design Builder DesignJobs (production, tracked-deployment path).

This is the recommended Nautobot way to apply the STC fabric intent: a DesignJob run via the
Nautobot Jobs UI / REST API. Unlike the ad-hoc `build_design` CLI / `apply_design.py` helpers, a
DesignJob in DEPLOYMENT mode records a tracked **design deployment** — so re-running updates it
idempotently and it can be cleanly **decommissioned** (rolls back exactly what it created).

Three jobs are exposed so an operator can provision **devices**, **cables**, or **both**:

  * STC Fabric — Devices          → the underlay only (loopback pool, per-switch lo/swp
                                     interfaces, loopback IPs, and the underlay config_context:
                                     asn / loopback / fabric_ports [+ vlan/l2vni on leaves]).
  * STC Fabric — Cables           → the physical cable design only (Clos uplinks + border↔DC-GW
                                     + OOB↔border). Requires the devices/interfaces to exist
                                     first (run Devices, or use Devices + Cables).
  * STC Fabric — Devices + Cables → both, in dependency order, as a single deployment.

Every object in the design files is `!create_or_update`, so each job is safe on a clean-slate
machine (creates) and on an existing fabric (adopts/updates). Rendering to switches still happens
via the NVCM render + DeployWorkflow — these jobs only populate Nautobot.
"""
from nautobot.apps.jobs import register_jobs
from nautobot.dcim.models import Device
from nautobot_design_builder.choices import DesignModeChoices
from nautobot_design_builder.contrib.ext import CableConnectionExtension
from nautobot_design_builder.design_job import DesignJob

from .context import STCFabricContext

name = "STC Fabric"

# Design file fragments. Order matters where both are used: devices/interfaces must be built
# before the cables that reference them.
UNDERLAY = "designs/10-underlay.yaml.j2"
CABLES = "designs/20-cables.yaml.j2"


class _PreserveTenantsMixin:
    """Snapshot/restore the per-tenant overlay across an underlay apply.

    The underlay design rewrites each leaf's ``local_config_context_data``, which would drop the
    ``tenants`` overlay key owned by the per-tenant DesignJob. Any job that applies the underlay
    mixes this in, so re-running/updating the fabric never clobbers a tenant onboarding. (Tenants
    are owned by their own deployment; the fabric jobs just must not destroy them.) Cables-only
    runs do not touch config_context, so that job does not need this.
    """

    def run(self, *args, **kwargs):  # noqa: D102
        saved = {
            d.name: (d.local_config_context_data or {}).get("tenants")
            for d in Device.objects.filter(local_config_context_data__has_key="tenants")
        }
        result = super().run(*args, **kwargs)
        for dev_name, tenants in saved.items():
            if not tenants:
                continue
            d = Device.objects.get(name=dev_name)
            cc = dict(d.local_config_context_data or {})
            if cc.get("tenants") != tenants:
                cc["tenants"] = tenants
                d.local_config_context_data = cc
                d.save()
                self.logger.info(
                    "preserved %d tenant(s) on %s across underlay apply", len(tenants), dev_name
                )
        return result


class STCFabricDevices(_PreserveTenantsMixin, DesignJob):
    """Build ONLY the STC fabric devices/underlay."""

    class Meta:
        """Metadata."""

        name = "STC Fabric — Devices"
        version = "1.1.0"
        commit_default = False
        design_mode = DesignModeChoices.DEPLOYMENT
        design_files = [UNDERLAY]
        context_class = STCFabricContext
        has_sensitive_variables = False
        nautobot_version = ">=2"
        description = (
            "Create/update the STC fabric devices: loopback pool + per-switch lo/swp interfaces, "
            "loopback IPs, and the underlay config_context (asn/loopback/fabric_ports [+vlan/l2vni "
            "on leaves]). Tracked, decommissionable. Prerequisite for the Cables job."
        )
        docs = """Populates Nautobot with the STC fabric **devices/underlay**:

* fabric loopback pool (172.23.0.0/24) + per-switch loopback IPs
* per-switch lo + swp interfaces
* STC underlay config_context (asn, loopback, fabric_ports [+ vlan/l2vni on leaves])

All objects are `!create_or_update` (create on clean slate, update if present). Render + push to
switches is done separately by the NVCM render pipeline + DeployWorkflow.
"""


class STCFabricCables(DesignJob):
    """Build ONLY the STC fabric physical cable design."""

    class Meta:
        """Metadata."""

        name = "STC Fabric — Cables"
        version = "1.1.0"
        commit_default = False
        design_mode = DesignModeChoices.DEPLOYMENT
        extensions = [CableConnectionExtension]
        design_files = [CABLES]
        context_class = STCFabricContext
        has_sensitive_variables = False
        nautobot_version = ">=2"
        description = (
            "Create/update the STC fabric physical cabling (Clos uplinks + border↔DC-GW + "
            "OOB↔border) as Cable objects. Requires the devices/interfaces to exist first — run "
            "'STC Fabric — Devices' (or 'Devices + Cables'). Tracked, decommissionable."
        )
        docs = """Populates Nautobot with the STC fabric **cabling**:

* Clos uplinks (leaf swp1→spine1, swp2→spine2, …)
* border↔DC-GW and OOB↔border links

Cables are idempotent via the built-in `!connect_cable` extension (adopts an existing cable,
never duplicates). **Run 'STC Fabric — Devices' first** so the interfaces exist.
"""


class STCFabricDevicesAndCables(_PreserveTenantsMixin, DesignJob):
    """Build the STC fabric devices/underlay AND the cable design in one deployment."""

    class Meta:
        """Metadata."""

        name = "STC Fabric — Devices + Cables"
        version = "1.1.0"
        commit_default = False
        design_mode = DesignModeChoices.DEPLOYMENT
        extensions = [CableConnectionExtension]
        # Order is significant: underlay (devices/interfaces) before cables that reference them.
        design_files = [UNDERLAY, CABLES]
        context_class = STCFabricContext
        has_sensitive_variables = False
        nautobot_version = ">=2"
        description = (
            "Builds the STC fabric underlay (devices/loopbacks/config_context) AND the physical "
            "cable design in a single tracked, decommissionable deployment. Use this for a "
            "one-shot fabric bring-up; use the separate Devices/Cables jobs for finer control."
        )
        docs = """One-shot STC fabric bring-up — everything the Devices and Cables jobs do, in
dependency order (devices/interfaces first, then cables), as a single deployment.
"""


register_jobs(STCFabricDevices, STCFabricCables, STCFabricDevicesAndCables)
