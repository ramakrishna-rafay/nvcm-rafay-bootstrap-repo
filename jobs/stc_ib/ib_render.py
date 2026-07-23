#
# Copyright (c) 2026  Rafay Systems, All rights reserved
# Author: Ramakrishna, Rafay
# Revision history:
#   2026-07-15  Ramakrishna, Rafay  Initial version.
#
"""Render the InfiniBand-plane intent for one tenant — pure, no I/O, no Nautobot.

Two artifacts, both derived from the single-source allocations (`stc_tenant.allocations`):

  * **PKey** (partition key) → the request payloads for NVCM's UFM-driven workflows
    `ib_pkey_creation` and `ib_pkey_member_add`. NVCM *has* these workflows, so PKey provisioning is
    feasible through the same platform we already run (it is out of *Cumulus* scope, not out of *NVCM*
    scope). Execution needs a live UFM + IB fabric + real port GUIDs.

  * **Rail** (SR-IOV VF subnets) → NV-IPAM `IPPool` custom resources (one per rail). These are applied
    to the GPU Kubernetes cluster by NV-IPAM, not to the switches — so we emit the manifests as intent;
    applying them needs the GPU cluster.

Being pure (dicts in, dicts out) this is fully unit-testable offline; the `STCTenantIB` job wraps it to
emit the artifacts (dry-run) or drive the NVCM workflows (execute, pending UFM). See test/ib/.
"""
from ..stc_tenant.allocations import pkey, rail_subnets

# NV-IPAM IPPool: each HGX node gets a per-rail block of this many VF IPs (STCS Rail sheet:
# "perNodeBlockSize = 8 → each HGX node gets 8 IPs per rail").
RAIL_PER_NODE_BLOCK = 8
NV_IPAM_API_VERSION = "nv-ipam.nvidia.com/v1alpha1"
DEFAULT_NV_IPAM_NAMESPACE = "kube-system"   # where the NV-IPAM controller watches IPPools


def pkey_creation_payload(t, ufm_host, ip_over_ib=True):
    """Request body for NVCM `POST /v1/workflow/ngc/ib_pkey_creation` — create the tenant's PKey on UFM."""
    return {"host": ufm_host, "pkey": pkey(t)["hex"], "ip_over_ib": bool(ip_over_ib)}


def pkey_member_add_payload(t, ufm_host, guids, membership_type="full", ip_over_ib=True):
    """Request body for NVCM `POST /v1/workflow/ngc/ib_pkey_member_add` — add HCA/GPU port GUIDs to the
    tenant's PKey. In production the `guids` come from `ib_port_guid_discovery`; here they are supplied."""
    return {
        "host": ufm_host,
        "pkey": pkey(t)["hex"],
        "guids": list(guids or []),
        "membership_type": membership_type,
        "ip_over_ib": bool(ip_over_ib),
    }


def rail_ippool_crds(t, namespace=DEFAULT_NV_IPAM_NAMESPACE):
    """NV-IPAM `IPPool` custom resources for the tenant's 8 rails (one per rail). Non-routable, so no
    gateway. Returns a list of CRD dicts ready to serialize to YAML and `kubectl apply` on the GPU cluster."""
    crds = []
    for k, subnet in enumerate(rail_subnets(t)):
        crds.append({
            "apiVersion": NV_IPAM_API_VERSION,
            "kind": "IPPool",
            "metadata": {
                "name": f"tenant{t}-rail{k}",
                "namespace": namespace,
                "labels": {"stc.rafay.co/tenant": f"tenant{t}", "stc.rafay.co/rail": str(k)},
            },
            # non-routable rail segment: subnet + per-node block, no gateway
            "spec": {"subnet": subnet, "perNodeBlockSize": RAIL_PER_NODE_BLOCK},
        })
    return crds
