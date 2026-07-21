#
# Copyright (c) 2026  Rafay Systems, All rights reserved
# Author: Ramakrishna, Rafay
# Revision history:
#   2026-07-15  Ramakrishna, Rafay  Initial version.
#
"""Context for the STC Fabric DesignJob.

The design files are declarative (no per-run templating needed yet), so this is a minimal
Context. To parameterize per customer, expose the blueprint spec here and template the design
files against it (the mock_topology job loads its data from a context directory the same way).
"""
from nautobot_design_builder.context import Context


class STCFabricContext(Context):
    """Minimal context — the STC design files carry their own values."""
