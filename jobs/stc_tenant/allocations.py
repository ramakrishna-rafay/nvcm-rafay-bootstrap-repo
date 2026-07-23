#
# Copyright (c) 2026  Rafay Systems, All rights reserved
# Author: Ramakrishna, Rafay
# Revision history:
#   2026-07-15  Ramakrishna, Rafay  Initial version.
#
"""STC f(T) tenant allocations — the single source of truth for the numbering scheme.

Pure Python (NO Nautobot imports) so it's importable by BOTH the DesignJob
(`stc_tenant.jobs`) and the allocation-audit test (`test/allocations/`). All f(T) math
lives here and nowhere else — the audit test then proves the scheme is in-range,
collision-free, and unique across T (see test/allocations/test_allocations.py).
"""

DCGW_ASN = 216419


def vfw_asn(t):
    """Customer vFW ASN per the STCS Master sheet: 65100 + T (T3=65103 … T100=65200). The vFW is the
    customer-side eBGP peer of the DC-GW (which uses DCGW_ASN)."""
    return 65100 + t


def ftt(t):
    """Compute the STC f(T) tenant allocation dict (topology-independent part)."""
    nfs_index = (t - 3) if t >= 3 else 0
    nfs_third = 96 + (nfs_index * 64) // 256
    nfs_fourth = (nfs_index * 64) % 256
    # NFS VLAN per the authoritative STCS Master sheet: 309+T through T90, then VLANs 400-401 are
    # reserved so it resumes at 311+T for T91-100 (T91->402 .. T100->411). The L2VNI tracks the VLAN
    # (100000+VLAN). Subnet is unaffected by the shift. (See STCS-GPUaaS_Network-Schema Master sheet.)
    nfs_vlan = 309 + t if t <= 90 else 311 + t
    return {
        "vrf": f"tenant{t}",
        "l3vni": 300000 + t,
        # L3VNI transit VLAN — node-local; 3000+T keeps it <=4094 for all T in 3..100.
        "l3vni_vlan": 3000 + t,
        "hgx": {"vlan": 109 + t, "vni": 100109 + t, "net": f"172.23.{125 + t}", "plen": 24},
        "nfs": {"vlan": nfs_vlan, "vni": 100000 + nfs_vlan, "net": f"172.23.{nfs_third}.{nfs_fourth}", "plen": 26},
        # customer-edge handoff VLANs the border leaf bridges as L2 trunks (no SVI) for the vFW.
        "mpls_vlan": 509 + t,
        "internet_vlan": 609 + t,
        "vfw_asn": 65100 + t,
    }
    # NOTE: the border-leaf<->DC-GW P2P sub-interface VLAN is NOT a single per-tenant value — the STCS
    # sheet assigns it per-leaf, per-DC-GW-uplink from the 1301-1516 pool. See p2p_subint_vlan() below;
    # the per-leaf bases (LETH05 1311/1312, LETH06 1321/1322) live in the topology map (jobs.BORDER_LINKS).


def p2p_subint_vlan(vlan_base, t):
    """Border-leaf<->DC-GW P2P sub-interface VLAN for tenant t on the uplink whose per-leaf base is
    `vlan_base`, per the STCS Master/VLAN sheet. Each border leaf has two uplinks (one per DC-GW), so
    two bases advance by 2 per tenant: `vlan_base + 2*(t-3)`. Sheet ranges: LETH05 bases 1311/1312 ->
    1311..1506, LETH06 bases 1321/1322 -> 1321..1516. Locally significant per port (node-local)."""
    return vlan_base + 2 * (t - 3)


def p2p_block(t):
    """The tenant's 8-IP P2P block (border-leaf<->DC-GW /31s) in 100.95.64.0/22."""
    off = (t - 3) * 8
    third, fourth = 64 + off // 256, off % 256
    return {
        "third": third,
        "fourth": fourth,
        "base": f"100.95.{third}.{fourth}",
        "addrs": [f"100.95.{third}.{fourth + k}" for k in range(8)],
    }


# ---- InfiniBand plane (off the Ethernet fabric; driven via NVCM/UFM + NV-IPAM, not Cumulus) ----

IB_PKEY_BASE = 0x8000


def pkey(t):
    """Tenant InfiniBand partition key per the STCS PKey sheet: 0x8000 + T
    (T1=0x8001 … T3=0x8003 … T100=0x8064). Returns {"hex": "0x8003", "dec": 32771}."""
    v = IB_PKEY_BASE + t
    # Uppercase hex to match the STCS PKey sheet exactly (0x800A, not 0x800a). The value is
    # case-insensitive numerically, and the rail IPPool CRD names use tenant{T} (not the hex),
    # so there is no lowercase-only k8s-name constraint here.
    return {"hex": f"0x{v:04X}", "dec": v}


def rail_subnets(t):
    """The tenant's 8 InfiniBand SR-IOV VF "rail" /24s in 100.67.0.0/14 (STCS Rail sheet).

    Allocation is aligned so a tenant's 8 rails never cross a 3rd-octet (/24-block) boundary — matching
    the sheet exactly. The first block 100.67 reserves .0 and holds T1-T31 (.1-.248; the .249-.255
    remainder is skipped); blocks 100.68/69/70 each hold 32 tenants (.0-.255). Hence T3 -> 100.67.17..24,
    T31 -> 100.67.241..248, T32 -> 100.68.0..7, T64 -> 100.69.0..7, T96 -> 100.70.0..7.
    Non-routable; NV-IPAM-managed; isolated by PKey."""
    if t <= 31:
        start = 1 + 8 * (t - 1)                 # 100.67.1 .. 100.67.248 (.0 reserved)
    else:
        n = t - 32
        start = 256 * (n // 32 + 1) + 8 * (n % 32)   # 100.68/69/70 blocks, 32 tenants each
    return [f"100.{67 + (start + k) // 256}.{(start + k) % 256}.0/24" for k in range(8)]
