#
# Copyright (c) 2026  Rafay Systems, All rights reserved
# Author: Ramakrishna, Rafay
# Revision history:
#   2026-07-15  Ramakrishna, Rafay  Initial version.
#
"""DriftDetect — report where switches have drifted from their Nautobot intent.

Read-only. For each selected device it (1) triggers a `backup` workflow (NVCM reads the switch's
running config into the config store — no apply), (2) re-renders the intended config from Nautobot,
then (3) compares them with the subset semantics in `drift_compare` (is every intended setting present
+ equal in the running config?). It NEVER approves a deploy or writes to a switch — it only reports.

This is requirement #9's "detection" half: the DeployWorkflow already reconciles on demand; this tells
you *when* a switch no longer matches intent (someone did a console `nv set`, a reboot lost runtime
state, a manual change wasn't captured) so you can re-deploy.

Run: `nautobot-server runjob stc_drift.jobs.DriftDetect -u admin -l -d '{"devices":"","fresh_backup":true}'`
(or via the Jobs UI / REST API). Blank `devices` = every device that has a rendered config in the store.
"""
import json
import urllib.request

import yaml
from nautobot.apps.jobs import BooleanVar, Job, StringVar, register_jobs
from nautobot.dcim.models import Device

from .drift_compare import compute_drift

name = "STC Drift"

CONFIG_STORE = "http://nv-config-manager-config-store-api:9000"
RENDER_API = "http://nv-config-manager-render-api:9000"
TEMPORAL_API = "http://nv-config-manager-temporal-api:9000"
CONFIG_FILE = "startup.yaml"


def _req(method, url, body=None, timeout=30):
    r = urllib.request.Request(
        url, data=(json.dumps(body).encode() if body is not None else None),
        method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as x:  # noqa: S310 (in-cluster service call)
        raw = x.read().decode()
        try:
            return x.status, json.loads(raw)
        except ValueError:
            return x.status, raw


def _latest(files, file_type):
    """Latest-version content of CONFIG_FILE for a given file_type, parsed from YAML."""
    fs = [f for f in files if f.get("filename") == CONFIG_FILE and f.get("file_type") == file_type]
    fs.sort(key=lambda f: f.get("version", 0), reverse=True)
    return yaml.safe_load(fs[0]["content"]) if fs and fs[0].get("content") else None


class DriftDetect(Job):
    """Report per-device config drift (running vs intended). Read-only — never applies."""

    devices = StringVar(
        default="", required=False,
        description="Comma-separated device names to check. Blank = every device with a rendered config.")
    fresh_backup = BooleanVar(
        default=True,
        description="Trigger a fresh backup (read the live switch) before comparing. Off = use the last "
                    "stored backup (faster, but only as current as the last deploy/backup).")

    def run(self, *args, **kwargs):
        names = [n.strip() for n in (kwargs.get("devices") or "").split(",") if n.strip()]
        fresh = bool(kwargs.get("fresh_backup", True))
        targets = Device.objects.filter(name__in=names) if names else Device.objects.all()

        checked, drifted, skipped = 0, [], 0
        for d in sorted(targets, key=lambda x: x.name):
            did = str(d.id)
            # (1) fresh live snapshot into the store (read-only on the switch)
            if fresh:
                try:
                    _s, b = _req("POST", f"{TEMPORAL_API}/v1/workflow/ngc/backup",
                                 {"device_id": did, "trigger": "API"})
                    wf = b.get("id") if isinstance(b, dict) else None
                    self._wait(wf) if wf else None
                except Exception as e:  # noqa: BLE001
                    self.logger.warning("%s: backup trigger failed (%s) — using last stored backup", d.name, e)
            # (2) re-render intended
            try:
                _req("POST", f"{RENDER_API}/v1/render/{did}/render", timeout=90)
            except Exception as e:  # noqa: BLE001
                self.logger.warning("%s: render failed (%s) — skipping", d.name, e)
                skipped += 1
                continue
            # (3) fetch + compare
            try:
                _s, files = _req("GET", f"{CONFIG_STORE}/v1/config/device/{did}", timeout=30)
            except Exception as e:  # noqa: BLE001
                self.logger.warning("%s: could not read config store (%s) — skipping", d.name, e)
                skipped += 1
                continue
            files = files if isinstance(files, list) else []
            intended, running = _latest(files, "intended"), _latest(files, "backup")
            if intended is None or running is None:
                self.logger.info("%s: no %s config in store (intended=%s, backup=%s) — skipping",
                                 d.name, CONFIG_FILE, intended is not None, running is not None)
                skipped += 1
                continue
            checked += 1
            findings = compute_drift(intended, running)
            if not findings:
                self.logger.info("%s: CLEAN (running matches intent)", d.name)
                continue
            drifted.append(d.name)
            self.logger.warning("%s: DRIFTED — %d finding(s):", d.name, len(findings))
            for f in findings[:25]:
                if f["kind"] == "missing":
                    self.logger.warning("    %s: intended %s absent from running", f["path"], f["intended"])
                else:
                    self.logger.warning("    %s: intended %s but running %s", f["path"], f["intended"], f["running"])
            if len(findings) > 25:
                self.logger.warning("    … and %d more", len(findings) - 25)

        # summary
        if drifted:
            self.logger.warning("DRIFT SUMMARY: %d checked, %d DRIFTED (%s), %d skipped",
                                 checked, len(drifted), ", ".join(drifted), skipped)
        else:
            self.logger.info("DRIFT SUMMARY: %d checked, 0 drifted, %d skipped — all devices match intent",
                             checked, skipped)
        return {"checked": checked, "drifted": drifted, "skipped": skipped}

    def _wait(self, wf, tries=60, delay=4):
        import time
        for _ in range(tries):
            time.sleep(delay)
            try:
                _s, st = _req("GET", f"{TEMPORAL_API}/v1/workflow/{wf}")
            except Exception:  # noqa: BLE001
                continue
            if isinstance(st, dict) and st.get("status") and st["status"] != "RUNNING":
                return st.get("status")
        return None

    class Meta:
        name = "STC Drift — Detect (running vs intent)"
        version = "1.0.0"
        has_sensitive_variables = False
        description = ("Read-only drift report: for each device, back up the live config, re-render the "
                       "intended config, and report where running has dropped or altered an intended "
                       "setting. Never approves a deploy or writes to a switch.")


register_jobs(DriftDetect)
