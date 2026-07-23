#
# Copyright (c) 2026  Rafay Systems, All rights reserved
# Author: Ramakrishna, Rafay
# Revision history:
#   2026-07-15  Ramakrishna, Rafay  Initial version.
#
"""Drift comparison — is the intended config satisfied by the running config?

Pure Python (NO Nautobot / no I/O) so it's importable by BOTH the DriftDetect Job
(`stc_drift.jobs`) and its unit test (`test/drift/`). Operates on already-parsed config
dicts, so it's trivially testable with dict literals.

Drift model
-----------
`intended` is what NVCM *renders* from Nautobot intent — an NVUE `- set:` document, i.e. only the
settings we deliberately declare. `running` is the switch's backed-up running config, which also
carries system defaults and metadata (a `header`, auto-added fields) we never declared.

So drift is a *subset* question, not an equality question: **is every leaf we intend present, with
the same value, in the running config?** A key that exists only in `running` is a default/extra, not
drift against our intent, and is ignored. Findings are the places where running has *diverged from* or
*dropped* something we intend:

  * `missing` — an intended key/list-item is absent from running (our config was removed/never applied)
  * `changed` — an intended leaf has a different value in running (our config was altered)

(Detecting *unexpected extra* config on the switch beyond intent is a separate, noisier drift class —
out of scope here; noted in test/drift/README.md.)
"""


def set_body(doc):
    """Return the NVUE `set` body from a rendered `- set:` document (list) or pass a dict through."""
    if isinstance(doc, list):
        for d in doc:
            if isinstance(d, dict) and "set" in d:
                return d["set"]
        return {}
    return doc or {}


def compute_drift(intended, running):
    """Return a list of drift findings (dicts: path, kind, intended, running).

    Empty list == no drift (the running config satisfies every intended setting).
    """
    findings = []
    _walk(set_body(intended), set_body(running), "", findings)
    return findings


def _walk(i, r, path, out):
    if isinstance(i, dict):
        if not isinstance(r, dict):
            out.append({"path": path or "/", "kind": "changed", "intended": "<map>", "running": _short(r)})
            return
        for k, v in i.items():
            p = f"{path}/{k}"
            if k not in r:
                out.append({"path": p, "kind": "missing", "intended": _short(v), "running": None})
            else:
                _walk(v, r[k], p, out)
    elif isinstance(i, list):
        # Compare scalar lists as sets (order-independent); an intended item missing from running is
        # drift. Lists of maps are rare in NVUE (it keys most collections by name into dicts); for those
        # we only flag a gross type mismatch rather than attempt a fragile element pairing.
        if not isinstance(r, list):
            out.append({"path": path or "/", "kind": "changed", "intended": "<list>", "running": _short(r)})
            return
        if all(not isinstance(x, (dict, list)) for x in i):
            missing = [x for x in i if x not in r]
            for x in missing:
                out.append({"path": f"{path}[]", "kind": "missing", "intended": _short(x), "running": None})
    else:
        if i != r:
            out.append({"path": path or "/", "kind": "changed", "intended": _short(i), "running": _short(r)})


def _short(v):
    s = repr(v)
    return s if len(s) <= 80 else s[:77] + "..."
