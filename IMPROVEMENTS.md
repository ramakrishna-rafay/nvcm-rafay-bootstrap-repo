# Bootstrap — improvement TODO

The bootstrap loads a **complete, self-contained foundation** today (manufacturers, platforms, roles,
statuses, tags, tenants, namespaces, location-types, locations, device-types, custom-fields,
relationships, rack-groups, racks, config-context-schema). The items below make it richer/cleaner over
time — **none block current operation.**

## 1. Shared config contexts (common services) — primary deferred item
**What:** manage fleet-common config (NTP, DNS, syslog, SNMP, timezone, banner) as a **shared Nautobot
ConfigContext**, instead of per-device or hardcoded in templates.
**Why:** DRY — define once (scoped by platform / location / role), applies to every matching device;
change one place instead of editing every device. Today *all* `config_context` is per-device (unique
asn/loopback/tenants/server_ports), so there's no shared data yet — but common services are the natural fit.
**How (two parts — Temporal is NOT involved):**
- **Data (Nautobot):** add `data/config_contexts.yaml` (e.g. `cumulus-common-services`, scoped
  `platform=Cumulus Linux`, `data:` = ntp/dns/syslog/timezone). Loader note: `load_config_contexts`
  handles `name/weight/is_active/data`; **scoping (roles/locations/platforms) needs a small loader
  addition** (M2M wiring). `config_context_schemas.yaml` is already done.
- **Render (Jinja):** in `common/5.6.0/include/l5_underlay.j2` add a `services(cc)` macro (NVUE `service:`
  for ntp/dns/syslog) + `timezone` in the existing `system()` macro; call `underlay.services(cc)` under a
  new `service:` block in the leaf **and** spine entrypoints.
**Status:** deferred — fabric runs fine on per-device config_context. Decision made 2026-07-20: continue
without it; revisit when fleet-common services need declarative management.

## 2. Enforce the config-context schema (opt-in)
`stc-device-config-context` exists but is **unassigned** (available, not enforcing). Assign it to devices
(`local_config_context_schema`) to validate `config_context` on save. It's permissive, so it should pass
all current devices — do it carefully in the Devices module.

## 3. Loader prune mode
Loaders are idempotent (`update_or_create`) but never **delete**. Renaming/removing a YAML item orphans
the old object (manual cleanup today — as seen with the renamed locations and the Rack-location cleanup).
Consider an optional prune step, or keep deletes manual + documented.

## 4. Secrets / SecretsGroups
Switch NVUE credentials as Nautobot **Secrets** (backed by env/vault) — never plaintext in Git. Not
handled by the loader today.

## 5. Roles as the fabric grows
Today: `spine` / `leaf` / `GPU`. Add `border-leaf` / `oob` / `dc-gw` / `vfw` if/when devices are
differentiated by those roles.
