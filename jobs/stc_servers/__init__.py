#
# Copyright (c) 2026  Rafay Systems, All rights reserved
# Author: Ramakrishna, Rafay
# Revision history:
#   2026-07-22  Ramakrishna, Rafay  Initial version.
#
"""STC Servers — Design Builder DesignJob package.

Re-export the job class so Nautobot's job discovery (which imports each subpackage under a synced
Git repository / JOBS_ROOT) registers it. This is the server-registration counterpart to the
stc_fabric jobs; the per-tenant attach/detach lifecycle lives in the separate stc_server jobs.
"""
from .jobs import STCServersRegister

__all__ = ["STCServersRegister"]
