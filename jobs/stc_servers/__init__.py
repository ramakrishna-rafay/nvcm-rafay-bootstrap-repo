#
# Copyright (c) 2026  Rafay Systems, All rights reserved
# Author: Ramakrishna, Rafay
# Revision history:
#   2026-07-22  Ramakrishna, Rafay  Initial version.
#
"""STC Server — Register DesignJob package.

Re-export the job class so Nautobot's job discovery (which imports each subpackage under a synced
Git repository / JOBS_ROOT) registers it. The job groups under the existing **"STC Server"** group
(module `name = "STC Server"` in jobs.py), so it appears alongside the JOBS_ROOT-delivered
Attach/Detach/Move lifecycle jobs even though it ships via this bootstrap git repo. This package is
kept separate (plural dir name) only because its delivery mechanism differs; the group is shared.
"""
from .jobs import STCServersRegister

__all__ = ["STCServersRegister"]
