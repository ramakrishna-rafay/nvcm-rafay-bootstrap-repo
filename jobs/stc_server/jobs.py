#
# Copyright (c) 2026  Rafay Systems, All rights reserved
# Author: Ramakrishna, Rafay
# Revision history:
#   2026-07-16  Ramakrishna, Rafay  Initial version.
#   2026-07-16  Ramakrishna, Rafay  Run fully from the Nautobot UI: fold render+deploy into each job
#                                    (deploy + auto_approve vars), holding at the NVCM approval gate.
#
"""STC server/host lifecycle — attach / detach / move a server on the fabric (Day-2, stc_day2_plan.md §6.1).

Each operation is a Nautobot Job (runnable from the **Jobs UI**, the REST API, or `runjob`). A server
(role=GPU, registered in the free pool by 32_stc_gen_servers_design.py) is placed in / removed from a tenant by
setting its leaf's access port via a read-modify-write on the leaf's `config_context` — writing ONLY the
separate `server_ports[]` key (never the tenant overlay's `tenants[]`), so server ops never clobber tenant
intent and multiple servers can share a tenant VLAN on one leaf. Same pattern as STCTenantOffboard.

Each job then (by default) **renders + deploys the affected leaf** via NVCM — so one UI Run does the whole
switch side. `auto_approve` controls the NVCM DeployWorkflow diff gate: unchecked (default) HOLDS the diff
for review/approval in the **NVCM UI** (production); checked applies hands-off (lab). Uncheck `deploy` to
set intent only and deploy later (50_stc_dc_deploy.sh). The host's data0 IP is a side channel (NVCM
configures switches, not servers) — each job logs the exact host address to apply with stc_deploy_scripts/42_stc_server_netplan.sh.
"""
import json
import re
import time
import urllib.request

from django.utils import timezone
from nautobot.apps.jobs import BooleanVar, ChoiceVar, Job, ObjectVar, register_jobs
from nautobot.dcim.models import Device
from nautobot.tenancy.models import Tenant

from ..stc_tenant.allocations import ftt

name = "STC Server"   # groups these jobs under "STC Server" in the Jobs UI (like "STC Provision")

PLANES = (("hgx", "hgx (GPU/compute)"), ("nfs", "nfs (storage)"))
RENDER_API = "http://nv-config-manager-render-api:9000"
TEMPORAL_API = "http://nv-config-manager-temporal-api:9000"
# External/egress supernet a server reaches over the tenant fabric (routed via the anycast gateway, not
# mgmt0). In the lab this is the DC-GW "internet" stand-in (l7_dcgw.j2 loopback); set to the real external
# egress supernet in production. Emitted as EXT_ROUTES in the attach/move host netplan command.
EGRESS_STANDIN_PREFIX = "203.0.113.0/24"


def set_server_tenant(server, tenant_name):
    """Set the GPU's owning **Tenant** (native FK — shows in Tenancy and on the device page) and the
    assigned-since timestamp (billing/allocation ledger, §6.9). Empty tenant_name = free pool.
    The Tenant is auto-created if missing (bridge until STCTenantOverlay owns Tenant creation)."""
    if tenant_name:
        server.tenant, _ = Tenant.objects.get_or_create(name=tenant_name)
        server.custom_field_data["assigned_since"] = timezone.now().isoformat(timespec="seconds")
    else:
        server.tenant = None
        server.custom_field_data["assigned_since"] = ""
    server.save()


def _gpu(server):
    """Validate the selected device is a GPU-role server (ObjectVar filters the UI; guard API calls too)."""
    role = getattr(server, "role", None)
    if not server or not role or role.name != "GPU":
        raise ValueError(f"{getattr(server, 'name', server)} is not a GPU-role server")
    return server


def tenant_number_of(tenant):
    """Derive the STC tenant number N from a Tenant named 'tenant<N>' (N = 3..100). Gives a clear
    error if a non-STC tenant (e.g. MyOrgRafay) is picked, so the operator selects the right one."""
    name = (getattr(tenant, "name", "") or "").strip()
    m = re.match(r"tenant(\d+)$", name)
    if not m:
        raise ValueError(f"tenant '{name or tenant}' is not an STC per-tenant VRF — pick one named "
                         f"'tenant<N>' (N = 3..100)")
    n = int(m.group(1))
    if not (3 <= n <= 100):
        raise ValueError(f"tenant number {n} out of range (STC automates tenant 3..100)")
    return n


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


def server_leaf_port(server):
    """(leaf Device, leaf port name) from the server's data0 cable — derived from the SoT, not input."""
    data = server.interfaces.get(name="data0")
    ep = data.connected_endpoint
    if ep is None:
        raise ValueError(f"{server.name}: data0 has no connected endpoint (register the server first)")
    return ep.device, ep.name


def host_ip_hint(server, plane_alloc):
    """Deterministic host IP *inside the attached tenant's plane subnet* — host offset = 20 + the
    server's trailing number. This is what keeps data0 aligned with whatever tenant the server is
    attached to: the address is always taken from that tenant's f(T) subnet, so attaching gpu-01 to
    tenant4 yields 172.23.129.x while attaching it to tenant66 yields 172.23.191.x, automatically.
    Handles both the HGX /24 (net 'a.b.c' -> a.b.c.N) and the NFS /26 (base 'a.b.c.d' -> a.b.c.(d+N))."""
    parts = plane_alloc["net"].split(".")
    m = re.search(r"(\d+)$", server.name)
    offset = 20 + (int(m.group(1)) if m else 1)
    if len(parts) == 3:                                  # HGX /24: 'a.b.c'
        return f"{plane_alloc['net']}.{offset}"
    if len(parts) == 4:                                  # NFS /26: subnet base 'a.b.c.d'
        return f"{parts[0]}.{parts[1]}.{parts[2]}.{int(parts[3]) + offset}"
    return None


def plane_gateway(plane_alloc):
    """The tenant plane's anycast gateway: .1 of a /24 HGX net, or base+1 of a /26 NFS subnet."""
    parts = plane_alloc["net"].split(".")
    if len(parts) == 3:
        return f"{plane_alloc['net']}.1"
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.{parts[2]}.{int(parts[3]) + 1}"
    return None


def host_netplan_cmd(server, plane_alloc=None, clear=False):
    """The exact 42_stc_server_netplan.sh command to align this server's data0 with its tenant —
    copy-paste and run on the OCI host. DATA_IP/GW are derived from the tenant's f(T) subnet
    (`host_ip_hint` / `plane_gateway`), so the host IP can never drift to another tenant's range.
    MGMT_IP is how to reach the VM (a libvirt lease, not in the SoT) — fill it in, e.g. from
    `virsh domifaddr <server>` on the OCI host."""
    if clear:
        return f"MGMT_IP=<{server.name}-mgmt-ip> DATA_IP= stc_deploy_scripts/42_stc_server_netplan.sh"
    ip, gw = host_ip_hint(server, plane_alloc), plane_gateway(plane_alloc)
    # EXT_ROUTES routes the external/egress destination over the tenant fabric (via the anycast gateway)
    # while mgmt0 stays the host default — else the server sends it out mgmt0 and never touches the fabric.
    ext = f" EXT_ROUTES={EGRESS_STANDIN_PREFIX}" if gw else ""
    return (f"MGMT_IP=<{server.name}-mgmt-ip> DATA_IP={ip} PLEN={plane_alloc['plen']}"
            f"{f' GW={gw}' if gw else ''}{ext} stc_deploy_scripts/42_stc_server_netplan.sh")


def deploy_leaf(logger, device, auto):
    """Render + DeployWorkflow the one affected leaf via NVCM. Mirrors STCProvisionDC; holds at the
    approval gate unless auto=True. Returns the terminal status ('COMPLETED' / 'held' / ...)."""
    did = str(device.id)
    try:
        _req("POST", f"{RENDER_API}/v1/render/{did}/render", timeout=90)
    except Exception as e:  # noqa: BLE001
        logger.failure("%s: render failed (%s)", device.name, e)
        return "render-failed"
    _s, b = _req("POST", f"{TEMPORAL_API}/v1/workflow/ngc/deploy", {"device_id": did})
    wf = b.get("id") if isinstance(b, dict) else None
    if not wf:
        logger.failure("%s: deploy POST failed (%s)", device.name, str(b)[:120])
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
                logger.warning("%s: HELD at %s — review + approve in the NVCM UI (workflow %s)",
                               device.name, pending, wf)
                return "held"
        status = st.get("status")
        if status and status != "RUNNING":
            (logger.info if status == "COMPLETED" else logger.failure)(
                "%s deploy: %s (failed_stage=%s)", device.name, status, st.get("failed_stage"))
            return status
    logger.failure("%s: deploy timed out (workflow %s)", device.name, wf)
    return "timeout"


def _deploy_vars():
    return (
        BooleanVar(default=True, description="Render + deploy the affected leaf now (uncheck to set "
                                             "intent only and deploy later)."),
        BooleanVar(default=False, description="Auto-approve the NVCM diff (lab, hands-off). Unchecked = "
                                              "HOLD the diff at the NVCM approval gate for review."),
    )


class STCServerAttach(Job):
    """Attach a free server to tenant T: leaf access port -> the tenant's HGX/NFS VLAN (server_ports[])."""

    server = ObjectVar(model=Device, query_params={"role": "GPU"},
                       description="GPU server to attach — pick from the list (role=GPU).")
    tenant = ObjectVar(model=Tenant,
                       description="Destination tenant — pick from the list (e.g. tenant4).")
    plane = ChoiceVar(choices=PLANES, default="hgx", required=False,
                      description="Which tenant plane's VLAN to place the server in (default hgx).")
    deploy, auto_approve = _deploy_vars()

    def run(self, *args, **kwargs):
        server = _gpu(kwargs["server"])
        t = tenant_number_of(kwargs["tenant"])
        plane = kwargs.get("plane") or "hgx"
        vrf = f"tenant{t}"
        leaf, port = server_leaf_port(server)
        f = ftt(t)
        vlan = f[plane]["vlan"]

        lcc = dict(leaf.local_config_context_data or {})
        tvlans = {v.get("vlan") for tt in lcc.get("tenants", []) if tt.get("vrf") == vrf
                  for v in tt.get("l2vnis", [])}
        if vlan not in tvlans:
            raise ValueError(f"{leaf.name} has no {vrf} {plane} VLAN {vlan} — onboard tenant {t} there first")
        sp = list(lcc.get("server_ports", []))
        existing = next((s for s in sp if s.get("port") == port), None)
        if existing and existing.get("vlan") != vlan:
            raise ValueError(f"{server.name} ({leaf.name}:{port}) is attached to VLAN {existing['vlan']} "
                             f"— use Detach/Move, not Attach")
        if existing and existing.get("vlan") == vlan:
            self.logger.info("%s already attached to %s (VLAN %s) on %s:%s — no-op",
                             server.name, vrf, vlan, leaf.name, port)
            return

        sp = [s for s in sp if s.get("port") != port]
        sp.append({"port": port, "vlan": vlan, "server": server.name, "vrf": vrf})
        lcc["server_ports"] = sp
        leaf.local_config_context_data = lcc
        leaf.save()
        set_server_tenant(server, vrf)
        self.logger.info("attached %s -> %s (%s VLAN %s) on %s:%s",
                         server.name, vrf, plane, vlan, leaf.name, port)
        self._finish(kwargs, leaf, server, f[plane])

    def _finish(self, kwargs, leaf, server, plane_alloc):
        if kwargs.get("deploy", True):
            deploy_leaf(self.logger, leaf, bool(kwargs.get("auto_approve", False)))
        else:
            self.logger.info("intent set — render+deploy %s when ready (50_stc_dc_deploy.sh)", leaf.name)
        self.logger.info("HOST — set data0 to the tenant's IP (derived from its f(T) subnet); run on the "
                         "OCI host:\n    %s", host_netplan_cmd(server, plane_alloc))

    class Meta:
        name = "STC Server — Attach to tenant"
        version = "1.2.0"
        has_sensitive_variables = False
        description = ("Attach a registered (free-pool) server to tenant T: place its leaf access port "
                       "into the tenant's HGX/NFS VLAN, then render+deploy the leaf (holds at the NVCM "
                       "gate unless auto_approve). Idempotent. Then set the host data0 IP.")


class STCServerDetach(Job):
    """Detach a server from its tenant: remove its leaf access port (back to the free pool)."""

    server = ObjectVar(model=Device, query_params={"role": "GPU"},
                       description="GPU server to detach — pick from the list (role=GPU).")
    deploy, auto_approve = _deploy_vars()

    def run(self, *args, **kwargs):
        server = _gpu(kwargs["server"])
        leaf, port = server_leaf_port(server)
        lcc = dict(leaf.local_config_context_data or {})
        sp = list(lcc.get("server_ports", []))
        entry = next((s for s in sp if s.get("port") == port), None)
        if entry is None:
            self.logger.warning("%s (%s:%s) is not attached — nothing to detach",
                                 server.name, leaf.name, port)
            return
        lcc["server_ports"] = [s for s in sp if s.get("port") != port]
        leaf.local_config_context_data = lcc
        leaf.save()
        set_server_tenant(server, "")
        self.logger.info("detached %s from %s (was VLAN %s) on %s:%s — back to free pool",
                         server.name, entry.get("vrf"), entry.get("vlan"), leaf.name, port)
        if kwargs.get("deploy", True):
            deploy_leaf(self.logger, leaf, bool(kwargs.get("auto_approve", False)))
        else:
            self.logger.info("intent set — render+deploy %s when ready (50_stc_dc_deploy.sh)", leaf.name)
        self.logger.info("HOST — clear data0 (no tenant IP); run on the OCI host:\n    %s",
                         host_netplan_cmd(server, clear=True))

    class Meta:
        name = "STC Server — Detach from tenant"
        version = "1.2.0"
        has_sensitive_variables = False
        description = ("Remove a server's leaf access port from its tenant VLAN, then render+deploy the "
                       "leaf (holds at the NVCM gate unless auto_approve) so the node returns to the free "
                       "pool. Idempotent.")


class STCServerMove(Job):
    """Move a server from its current tenant to another: re-home the access port + (host) re-IP."""

    server = ObjectVar(model=Device, query_params={"role": "GPU"},
                       description="GPU server to move — pick from the list (role=GPU).")
    to_tenant = ObjectVar(model=Tenant,
                          description="Destination tenant — pick from the list (e.g. tenant4).")
    plane = ChoiceVar(choices=PLANES, default="hgx", required=False,
                      description="Which tenant plane's VLAN to move into (default hgx).")
    deploy, auto_approve = _deploy_vars()

    def run(self, *args, **kwargs):
        server = _gpu(kwargs["server"])
        t = tenant_number_of(kwargs["to_tenant"])
        plane = kwargs.get("plane") or "hgx"
        vrf = f"tenant{t}"
        leaf, port = server_leaf_port(server)
        f = ftt(t)
        vlan = f[plane]["vlan"]

        lcc = dict(leaf.local_config_context_data or {})
        sp = list(lcc.get("server_ports", []))
        entry = next((s for s in sp if s.get("port") == port), None)
        if entry is None:
            raise ValueError(f"{server.name} ({leaf.name}:{port}) is not attached — use Attach, not Move")
        if entry.get("vlan") == vlan:
            self.logger.info("%s already on %s (VLAN %s) — no-op", server.name, vrf, vlan)
            return
        tvlans = {v.get("vlan") for tt in lcc.get("tenants", []) if tt.get("vrf") == vrf
                  for v in tt.get("l2vnis", [])}
        if vlan not in tvlans:
            raise ValueError(f"{leaf.name} has no {vrf} {plane} VLAN {vlan} — onboard tenant {t} there first")
        was = f"{entry.get('vrf')} (VLAN {entry.get('vlan')})"
        lcc["server_ports"] = [s for s in sp if s.get("port") != port]
        lcc["server_ports"].append({"port": port, "vlan": vlan, "server": server.name, "vrf": vrf})
        leaf.local_config_context_data = lcc
        leaf.save()
        set_server_tenant(server, vrf)
        self.logger.info("moved %s: %s -> %s (%s VLAN %s) on %s:%s",
                         server.name, was, vrf, plane, vlan, leaf.name, port)
        if kwargs.get("deploy", True):
            deploy_leaf(self.logger, leaf, bool(kwargs.get("auto_approve", False)))
        else:
            self.logger.info("intent set — render+deploy %s when ready (50_stc_dc_deploy.sh)", leaf.name)
        self.logger.info("HOST — re-IP data0 into %s's subnet (derived from its f(T) allocation); run on "
                         "the OCI host:\n    %s", vrf, host_netplan_cmd(server, f[plane]))

    class Meta:
        name = "STC Server — Move to another tenant"
        version = "1.2.0"
        has_sensitive_variables = False
        description = ("Re-home a server's leaf access port from its current tenant VLAN to another, then "
                       "render+deploy the leaf (holds at the NVCM gate unless auto_approve). The isolation "
                       "flips: the node joins the new tenant and can no longer reach the old.")


register_jobs(STCServerAttach, STCServerDetach, STCServerMove)
