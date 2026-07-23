#
# Copyright (c) 2026  Rafay Systems, All rights reserved
# Author: Ramakrishna, Rafay
# Revision history:
#   2026-07-15  Ramakrishna, Rafay  Initial version.
#
"""STC per-tenant overlay — Design Builder DesignJob (production tenant onboarding).

Onboard a tenant by number T: computes the STC f(T) allocations, models the tenant VRF as a
tracked Nautobot object (design file → decommissionable), and sets the EVPN overlay intent on
the compute leaves via config_context (which the NVCM Cumulus templates render — l6_overlay.j2).

Run via the Jobs REST API / UI / runjob:
    nautobot-server runjob stc_tenant.jobs.STCTenantOverlay -u admin -l \
        -d '{"deployment_name":"tenant3","tenant_number":3,"compute_leaves":"RMDC-GPU-LETH01,RMDC-GPU-LETH02","access_port":"swp8"}'

Onboard = run(T); offboard = decommission the deployment. After the job, render + DeployWorkflow
push the config to the switches (Nautobot is only the source of truth).

f(T) (STC schema, docs/stc_network_details.md §9):
    HGX  vlan=109+T  l2vni=100109+T  subnet=172.23.(125+T).0/24
    NFS  vlan=309+T  l2vni=100309+T  subnet=/26 in 172.23.96.0/19 index (T-3)
    VRF  tenant{T}   L3VNI=300000+T   L3VNI-vlan=4000+T (internal)
    per-leaf SVI host = gw-subnet.(10 + leaf index); anycast gateway = gw-subnet.1
"""
from nautobot.apps.jobs import IntegerVar, Job, StringVar, register_jobs
from nautobot.dcim.models import Device
from nautobot_design_builder.choices import DesignModeChoices
from nautobot_design_builder.design_job import DesignJob

from .allocations import DCGW_ASN, ftt, p2p_block, p2p_subint_vlan
from .context import STCTenantContext

name = "STC Tenant"


# f(T) tenant allocations (ftt/p2p_block) + DCGW_ASN live in allocations.py — the single source of the
# numbering scheme, shared with the allocation-audit test (test/allocations/) which proves it stays
# in-range and collision-free for T=3..100.


# Border-leaf <-> DC-GW PAIR links (lab topology; a real deployment derives from cables). Each
# border leaf peers BOTH DC-GWs. Per tenant that's 4 eBGP sessions over an 8-IP P2P block
# (100.95.64.0 + (T-3)*8): LETH05 takes .0-.3 (R-02 first), LETH06 .4-.7. Sub-interface VLANs follow
# the STCS sheet's per-leaf 1301-1516 pool via p2p_subint_vlan(base, T) = base + 2*(T-3): LETH05 bases
# 1311(->R-02)/1312(->R-01), LETH06 1321/1322.
# PORTS: the STCS sheet cables these on swp19(->R-02)/swp20(->R-01); the Cumulus VX lab is wired
# swp4(->R-02)/swp3(->R-01), so we keep the lab ports here — the sub-int VLAN scheme is identical
# regardless of the base port (a VLAN tag is port-independent). A real deployment derives the ports
# from Nautobot cables. Each entry: (uplink, dc-gw device, dc-gw port, border-leaf ASN, sheet vlan_base).
BORDER_LINKS = {
    "RMDC-GPU-LETH05": [
        {"uplink": "swp4", "dcgw": "RMDC-DC-R-02", "dcgw_port": "swp1", "asn": 65305, "vlan_base": 1311},
        {"uplink": "swp3", "dcgw": "RMDC-DC-R-01", "dcgw_port": "swp1", "asn": 65305, "vlan_base": 1312},
    ],
    "RMDC-GPU-LETH06": [
        {"uplink": "swp4", "dcgw": "RMDC-DC-R-02", "dcgw_port": "swp2", "asn": 65306, "vlan_base": 1321},
        {"uplink": "swp3", "dcgw": "RMDC-DC-R-01", "dcgw_port": "swp3", "asn": 65306, "vlan_base": 1322},
    ],
}
VFW_PORT = "swp8"   # border-leaf port facing the tenant vFW (br-l{5,6}fw); joins the L2 trunk


class STCTenantOverlay(DesignJob):
    """Onboard one STC tenant's EVPN overlay (VRF + HGX/NFS L2VNIs + L3VNI) onto the compute leaves."""

    tenant_number = IntegerVar(min_value=3, max_value=100, description="Tenant number T (3–100 automated).")
    compute_leaves = StringVar(
        default="RMDC-GPU-LETH01,RMDC-GPU-LETH02",
        description="Comma-separated leaf hostnames to place the tenant on.",
    )
    access_port = StringVar(default="", required=False,
                            description="Optional designated host access port for the HGX VLAN. Leave BLANK "
                                        "(default) — servers attach via server_ports (per-server, e.g. swp3). "
                                        "A fixed shared port collides when 2+ tenants share a leaf.")
    border_leaves = StringVar(
        default="RMDC-GPU-LETH05,RMDC-GPU-LETH06",
        description="Comma-separated border-leaf hostnames for the DC-GW egress (blank to skip external).",
        required=False,
    )
    dcgw_device = StringVar(default="RMDC-DC-R-01", description="DC-GW device to peer for external egress.", required=False)

    def run(self, *args, **kwargs):
        """Compute f(T) and set the overlay config_context on each compute leaf, then run the design."""
        t = int(kwargs["tenant_number"])
        leaves = [n.strip() for n in kwargs.get("compute_leaves", "").split(",") if n.strip()]
        access_port = (kwargs.get("access_port") or "").strip()
        kwargs["vrf_name"] = f"tenant{t}"  # consumed by the design file (tracked VRF object)

        f = ftt(t)
        hgx, nfs = f["hgx"], f["nfs"]
        for idx, name_ in enumerate(leaves):
            try:
                d = Device.objects.get(name=name_)
            except Device.DoesNotExist:
                self.logger.warning("compute leaf %r not found — skipping", name_)
                continue
            host = 10 + idx  # unique per-leaf SVI host octet
            hgx_l2vni = {"vlan": hgx["vlan"], "vni": hgx["vni"],
                         "svi": f"{hgx['net']}.{host}/{hgx['plen']}", "gw": f"{hgx['net']}.1"}
            # Only pin a designated host access port if one was explicitly requested. A fixed *shared*
            # port (the old swp8 default) collides when 2+ tenants share a leaf: the render emits the
            # same port twice with different access VLANs -> duplicate-key config -> the deploy diff
            # aborts. Servers attach via server_ports (per-server, e.g. swp3), so no default is needed.
            if access_port:
                hgx_l2vni["access_port"] = access_port
            tenant_entry = {
                "vrf": f["vrf"], "l3vni": f["l3vni"], "l3vni_vlan": f["l3vni_vlan"],
                "l2vnis": [
                    hgx_l2vni,
                    {"vlan": nfs["vlan"], "vni": nfs["vni"],
                     "svi": f"{nfs['net'].rsplit('.',1)[0]}.{int(nfs['net'].rsplit('.',1)[1]) + host}/{nfs['plen']}",
                     "gw": f"{nfs['net'].rsplit('.',1)[0]}.{int(nfs['net'].rsplit('.',1)[1]) + 1}"},
                ],
            }
            cc = dict(d.local_config_context_data or {})
            tenants = [x for x in cc.get("tenants", []) if x.get("vrf") != f["vrf"]]  # replace same-tenant entry
            tenants.append(tenant_entry)
            cc["tenants"] = tenants
            d.local_config_context_data = cc
            d.save()
            self.logger.info("tenant%s overlay set on %s (SVI host .%s)", t, name_, host)

        # ---- external egress: 4 P2P sub-interface sessions/tenant (each border leaf <-> DC-GW pair) ----
        borders = [n.strip() for n in (kwargs.get("border_leaves") or "").split(",") if n.strip()]
        if borders:
            block = p2p_block(t)                 # tenant's 8-IP P2P block in 100.95.64.0/22
            # the ONLY prefixes this tenant should ever advertise to the DC-GW — used as the DC-GW's
            # per-peer inbound accept-list (anti-spoofing; a peer can't inject another tenant's space)
            tenant_subnets = [f"{hgx['net']}.0/{hgx['plen']}", f"{nfs['net']}/{nfs['plen']}"]
            def addr(k):
                return block["addrs"][k]
            dcgw_peers = {}                      # dc-gw device name -> [peer dicts]
            for bidx, bname in enumerate(borders):
                links = BORDER_LINKS.get(bname)
                if not links:
                    self.logger.warning("no BORDER_LINKS for %r — skipping external", bname)
                    continue
                try:
                    d = Device.objects.get(name=bname)
                except Device.DoesNotExist:
                    self.logger.warning("border leaf %r not found — skipping", bname)
                    continue
                externals = []
                for uidx, link in enumerate(links):
                    k = bidx * 4 + uidx * 2       # 4-IP stride per border leaf, 2-IP /31 per DC-GW
                    b_addr, g_addr = addr(k), addr(k + 1)
                    vlan = p2p_subint_vlan(link["vlan_base"], t)   # STCS per-leaf sub-int VLAN (1301-1516)
                    externals.append({"port": link["uplink"], "vlan": vlan,
                                      "local": f"{b_addr}/31", "peer": g_addr, "peer_asn": DCGW_ASN})
                    dcgw_peers.setdefault(link["dcgw"], []).append(
                        {"port": link["dcgw_port"], "vlan": vlan, "local": f"{g_addr}/31",
                         "peer": b_addr, "peer_asn": link["asn"], "accept": tenant_subnets})
                # `advertise`: the tenant's own subnets, originated (BGP network statement) from the
                # border's tenant VRF to the DC-GW so the DC-GW has a RETURN route to the tenant hosts.
                # The subnets live on the compute leaves and reach the border via EVPN type-5; a network
                # statement re-originates them over the ipv4-unicast eBGP session (redistribute-connected
                # does not, since they are not connected here). Matches the DC-GW's per-peer accept-list.
                entry = {"vrf": f["vrf"], "l3vni": f["l3vni"], "l3vni_vlan": f["l3vni_vlan"],
                         "l2vnis": [], "externals": externals, "advertise": tenant_subnets}
                cc = dict(d.local_config_context_data or {})
                tenants = [x for x in cc.get("tenants", []) if x.get("vrf") != f["vrf"]]
                tenants.append(entry)
                cc["tenants"] = tenants
                # fabric-side MPLS/Internet L2 trunk (no SVI): the border leaf bridges the tenant's
                # handoff VLANs among the DC-GW uplinks + the vFW port, so a customer vFW can peer the
                # DC-GW pair. Ports are topology-fixed; VLANs accumulate across tenants (idempotent).
                cc["l2trunk_ports"] = sorted({lk["uplink"] for lk in links} | {VFW_PORT})
                cc["l2trunk_vlans"] = sorted(set(cc.get("l2trunk_vlans", [])) | {f["mpls_vlan"], f["internet_vlan"]})
                d.local_config_context_data = cc
                d.save()
                self.logger.info("tenant%s external on %s: %s ; L2 trunk vlans %s", t, bname,
                                 ", ".join(f"{e['port']}.{e['vlan']}->{e['peer']}" for e in externals),
                                 cc["l2trunk_vlans"])
            for gname, peers_new in dcgw_peers.items():
                try:
                    g = Device.objects.get(name=gname)
                except Device.DoesNotExist:
                    self.logger.warning("DC-GW %r not found — skipping", gname)
                    continue
                cc = dict(g.local_config_context_data or {})
                dcgw = dict(cc.get("dcgw") or {"asn": DCGW_ASN, "router_id": addr(1),
                                               "loopbacks": ["203.0.113.1/32"], "peers": []})
                # drop THIS tenant's existing peers before re-adding them, so a re-onboard is idempotent
                # and every other tenant's peers stay untouched. Match by P2P /31 address (the tenant's
                # stable 100.95.64.0+(T-3)*8 block) — the same basis the offboard uses, and robust to a
                # peer left over from an older sub-int VLAN scheme (the address is stable, the VLAN isn't).
                tenant_ips = set(block["addrs"])
                peers = [p for p in dcgw.get("peers", [])
                         if (p.get("local") or "").split("/")[0] not in tenant_ips]
                peers.extend(peers_new)
                dcgw["peers"] = peers
                cc["dcgw"] = dcgw
                g.local_config_context_data = cc
                g.save()
                self.logger.info("tenant%s: DC-GW %s now has %d P2P sub-int peer(s)", t, gname, len(peers))

        return super().run(*args, **kwargs)

    class Meta:
        name = "STC Tenant — Onboard EVPN Overlay"
        version = "1.0.0"
        commit_default = False
        design_mode = DesignModeChoices.DEPLOYMENT
        design_files = ["designs/tenant_vrf.yaml.j2"]
        context_class = STCTenantContext
        has_sensitive_variables = False
        nautobot_version = ">=2"
        description = ("Onboard one STC tenant (f(T)): EVPN overlay on the compute leaves, external "
                       "DC-GW egress on the border leaves, and the fabric-side MPLS/Internet L2 trunk "
                       "for the customer vFW. Tracked/decommissionable.")


class STCTenantOffboard(Job):
    """Offboard a tenant: reverse every config_context mutation the onboard job made.

    Design Builder decommission removes the *tracked* design object (the tenant VRF record), but the
    onboard sets the fabric intent imperatively in `config_context` (compute/border tenant entries,
    the border L2 trunk VLANs, and the DC-GW P2P peers) — those are NOT reversed by decommission and
    would leave stale fabric config. This job AUTO-DISCOVERS every device that references the tenant
    in its config_context and removes exactly that tenant's entries, so a subsequent render +
    DeployWorkflow (declarative full config) drops the VRF/VNIs/SVIs/sub-ifs/trunk from the switches.
    It needs no placement info — that removes the fragility of the operator having to remember where
    the tenant was placed. Run this, then render+deploy the cleared devices, then decommission the
    Design Builder deployment for the VRF object. Idempotent — re-running after removal is a no-op.
    """

    tenant_number = IntegerVar(min_value=3, max_value=100, description="Tenant number T to offboard.")
    devices = StringVar(default="", required=False,
                        description="Optional: restrict cleanup to these devices (comma-separated). "
                                    "Leave blank to auto-discover every device referencing the tenant.")

    def run(self, *args, **kwargs):
        t = int(kwargs["tenant_number"])
        vrf = f"tenant{t}"
        f = ftt(t)
        trunk_vlans = {f["mpls_vlan"], f["internet_vlan"]}
        # Match this tenant's DC-GW P2P peers by their /31 address (in the tenant's fixed
        # 100.95.64.0+(T-3)*8 block), NOT by VLAN: the P2P block is stable across the scheme, whereas a
        # tenant onboarded under an older p2p_vlan (e.g. 700+T) carries that VLAN on its live peers.
        p2p_addrs = set(p2p_block(t)["addrs"])
        def _peer_ip(p):
            return (p.get("local") or "").split("/")[0]
        # Robust by default: SCAN every device and clean any that references this tenant in its
        # config_context — no need for the operator to remember where the tenant was placed (that was
        # the offboard's fragility). An explicit device list can still narrow the scope if desired.
        names = [n.strip() for n in (kwargs.get("devices") or "").split(",") if n.strip()]
        targets = Device.objects.filter(name__in=names) if names else Device.objects.all()
        cleared = []
        for d in targets:
            cc = dict(d.local_config_context_data or {})
            changed = False
            if any(x.get("vrf") == vrf for x in cc.get("tenants", [])):  # compute/border leaf overlay/egress entry
                cc["tenants"] = [x for x in cc["tenants"] if x.get("vrf") != vrf]
                changed = True
            if cc.get("l2trunk_vlans") and (set(cc["l2trunk_vlans"]) & trunk_vlans):  # border leaf handoff VLANs
                cc["l2trunk_vlans"] = [v for v in cc["l2trunk_vlans"] if v not in trunk_vlans]
                changed = True
            if cc.get("dcgw", {}).get("peers") and any(_peer_ip(p) in p2p_addrs for p in cc["dcgw"]["peers"]):  # DC-GW P2P peers
                dcgw = dict(cc["dcgw"])
                dcgw["peers"] = [p for p in dcgw["peers"] if _peer_ip(p) not in p2p_addrs]
                cc["dcgw"] = dcgw
                changed = True
            if changed:
                d.local_config_context_data = cc
                d.save()
                cleared.append(d.name)
                self.logger.info("%s: cleared tenant%s references", d.name, t)
        if not cleared:
            self.logger.warning("no devices referenced tenant%s — nothing to clear (already offboarded?)", t)
        self.logger.info("tenant%s config_context cleared on: %s. Next: render+deploy those devices "
                         "(or run 41_stc_tenant_offboard.sh), then Design Builder decommission VRF %s.",
                         t, ", ".join(cleared) or "(none)", vrf)

    class Meta:
        name = "STC Tenant — Offboard (reverse config_context)"
        version = "1.0.0"
        has_sensitive_variables = False
        description = ("Reverse the onboard's config_context mutations for tenant T (compute/border "
                       "tenant entries, border L2-trunk VLANs, DC-GW P2P peers) so a render+deploy "
                       "drops the tenant from the fabric. Complements Design Builder decommission.")


register_jobs(STCTenantOverlay, STCTenantOffboard)
