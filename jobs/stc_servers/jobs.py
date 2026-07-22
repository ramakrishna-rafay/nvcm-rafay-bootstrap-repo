#
# Copyright (c) 2026  Rafay Systems, All rights reserved
# Author: Ramakrishna, Rafay
# Revision history:
#   2026-07-22  Ramakrishna, Rafay  Initial version — server registration as a GitOps DesignJob
#                                    (parity with the STC Fabric jobs).
#
"""STC Server — Register: Design Builder DesignJob (register GPU/compute servers in the free pool).

Mirrors the STC Fabric jobs: a tracked, decommissionable DesignJob run from the Nautobot Jobs UI /
REST API. It registers each GPU server (device + mgmt0/data0 interfaces, data0's MAC) and the
server<->leaf cable (data0 -> the leaf's server-facing swp), placing the server in the FREE POOL
with NO tenant. Attaching a server to a tenant is a separate operation (the STC Server
Attach/Detach/Move jobs).

Prerequisites, created idempotently by the bootstrap loader (Load ... Bootstrap Data): the GPU
role (carrying the dcim.device content type) and the Generic Server device type. Cables need
Design Builder's CableConnectionExtension, declared in Meta below.

Every object in the design file is `!create_or_update`, so the job is safe on a clean-slate machine
(creates) and on an existing inventory (adopts/updates).
"""
from nautobot.apps.jobs import register_jobs
from nautobot_design_builder.choices import DesignModeChoices
from nautobot_design_builder.contrib.ext import CableConnectionExtension
from nautobot_design_builder.design_job import DesignJob

from .context import STCServersContext

# Group under the SAME "STC Server" grouping as the existing Attach/Detach/Move jobs so this
# Register job appears alongside them in the Nautobot Jobs UI. Nautobot groups by this module-level
# `name`, so a job delivered here (bootstrap git repo) and the JOBS_ROOT-installed lifecycle jobs
# all land in one "STC Server" group even though they ship via different mechanisms.
name = "STC Server"

SERVERS = "designs/servers.yaml.j2"


def _write_change_summary(job, environment):
    """Write a per-model change summary to the Job Result — added / updated / checked.

    Same rationale as the STC Fabric jobs: inside a DesignJob, ``self.logger`` is routed to the
    pod's stdout, so a successful run leaves no JobResult log lines. Inserting JobLogEntry rows
    directly makes the summary show in the UI. Called from ``post_implementation`` (committed runs
    only). Wrapped so a reporting hiccup can never fail an otherwise-successful deployment.
    """
    try:
        from collections import defaultdict

        from nautobot.extras.models import JobLogEntry
        from nautobot_design_builder.models import ChangeRecord, ChangeSet

        change_set = getattr(getattr(environment, "journal", None), "change_set", None)
        if change_set is None:
            change_set = (
                ChangeSet.objects.filter(job_result=job.job_result).order_by("-created").first()
            )
        if change_set is None:
            return

        added, updated, adopted = defaultdict(int), defaultdict(int), defaultdict(int)
        for record in ChangeRecord.objects.filter(change_set=change_set):
            model = record._design_object_type.model if record._design_object_type else "object"
            if record.full_control:
                added[model] += 1          # created + owned by this design
            elif record.changes:
                updated[model] += 1        # pre-existing object, some fields changed
            else:
                adopted[model] += 1        # pre-existing object, referenced, no change needed

        def emit(message, level="info"):
            JobLogEntry.objects.create(
                job_result=job.job_result,
                log_level=level,
                grouping="change-summary",
                message=message,
            )

        total = sum(added.values()) + sum(updated.values()) + sum(adopted.values())
        emit(f"Change summary — {total} object(s) processed by this deployment:", "success")
        for label, bucket in (("added", added), ("updated", updated), ("checked (no change)", adopted)):
            for model, count in sorted(bucket.items()):
                emit(f"    {label}: {count} × {model}")
        if not total:
            emit("    (nothing to do — servers already match intent)")
    except Exception:  # never let reporting break a successful deployment
        pass


class STCServersRegister(DesignJob):
    """Register the STC GPU/compute servers (free pool) + their leaf cables.

    Inherits DesignJob *directly* (single inheritance) — same reason as the STC Fabric jobs: Design
    Builder's render() locates the design-template directory by walking ``cls.__bases__[0]`` up to
    DesignJob, so a mixin as the first base would divert that walk and crash.
    """

    def post_implementation(self, context, environment):
        """Log the change summary (committed runs only)."""
        _write_change_summary(self, environment)

    class Meta:
        """Metadata."""

        name = "STC Server — Register"
        version = "1.0.0"
        commit_default = False
        design_mode = DesignModeChoices.DEPLOYMENT
        extensions = [CableConnectionExtension]
        design_files = [SERVERS]
        context_class = STCServersContext
        has_sensitive_variables = False
        nautobot_version = ">=2"
        description = (
            "Register the STC GPU/compute servers into the free pool: each server device + "
            "mgmt0/data0 interfaces (data0 MAC) and the server<->leaf cable (data0 -> leaf swpN). "
            "No tenant is set — attaching to a tenant is a separate op (STC Server Attach). "
            "Tracked, decommissionable. Requires the GPU role + Generic Server device type "
            "(created by the bootstrap loader)."
        )
        docs = """Populates Nautobot with the STC **servers** (free pool):

* each GPU server device (role GPU, Generic Server type) at the Room, + mgmt0/data0 interfaces
* data0's MAC + the server<->leaf cable (data0 -> the leaf's server-facing swp)

All objects are `!create_or_update` (create on clean slate, adopt/update if present). Attaching a
server to a tenant is done by the separate STC Server Attach/Detach/Move jobs. Requires the GPU
role and Generic Server device type, which the bootstrap loader creates.
"""


register_jobs(STCServersRegister)
