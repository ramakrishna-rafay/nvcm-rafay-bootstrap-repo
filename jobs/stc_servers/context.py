#
# Copyright (c) 2026  Rafay Systems, All rights reserved
# Author: Ramakrishna, Rafay
# Revision history:
#   2026-07-22  Ramakrishna, Rafay  Initial version.
#
"""Context for the STC Servers DesignJob.

Minimal Context — the server design file carries its own values (mirrors STCFabricContext). To
parameterize per customer, expose the server spec here and template the design file against it.
"""
from nautobot_design_builder.context import Context


class STCServersContext(Context):
    """Minimal context — the STC servers design file carries its own values."""
