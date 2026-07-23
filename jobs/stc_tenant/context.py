#
# Copyright (c) 2026  Rafay Systems, All rights reserved
# Author: Ramakrishna, Rafay
# Revision history:
#   2026-07-15  Ramakrishna, Rafay  Initial version.
#
"""Context for the STC tenant DesignJob — exposes the job vars to the design file."""
from nautobot_design_builder.context import Context


class STCTenantContext(Context):
    """Design Builder fills these annotated fields from the job data."""

    tenant_number: int
    vrf_name: str
