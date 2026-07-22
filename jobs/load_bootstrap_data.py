# SPDX-FileCopyrightText: Copyright (c) 2024-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Nautobot job to load baseline NVIDIA Config Manager data for external customers."""

import os
from pathlib import Path

import yaml
from django.contrib.contenttypes.models import ContentType
from nautobot.apps.jobs import Job, register_jobs
from nautobot.core.models.fields import slugify_dashes_to_underscores
from nautobot.dcim.models import (
    Device,
    DeviceType,
    Location,
    LocationType,
    Manufacturer,
    Platform,
    Rack,
    RackGroup,
)
from nautobot.extras.models import (
    ConfigContext,
    ConfigContextSchema,
    CustomField,
    Relationship,
    Role,
    Status,
    Tag,
)
from nautobot.ipam.models import Namespace
from nautobot.tenancy.models import Tenant

name = "Bootstrap"


class LoadBootstrapData(Job):
    """Load bootstrap data for NVIDIA Config Manager external customers from YAML templates."""

    class Meta:
        """Job metadata."""

        name = "Load Bootstrap Data"
        description = (
            "Load manufacturers, roles, tags, custom fields, platforms, device types, tenants, "
            "location types, namespaces, statuses, relationships, config context schemas, and "
            "config contexts from YAML templates"
        )
        has_sensitive_variables = False
        approval_required = False

    def __init__(self):
        """Initialize the job."""
        super().__init__()
        # Path to data templates relative to the job file
        self.data_path = Path(__file__).parent.parent / "data"
        # Get deployment type from environment variable (e.g., 'superpod', 'dgxc', 'azure', 'all')
        self.deployment_type = os.getenv("NV_CONFIG_MANAGER_DEPLOYMENT_TYPE", "all").lower()

    def should_load_item(self, item_data, item_name="item"):
        """Check if an item should be loaded based on deployment_types.

        Args:
            item_data: Dictionary containing item data with optional 'deployment_types' field
            item_name: Name of the item for logging purposes

        Returns:
            bool: True if item should be loaded, False otherwise
        """
        deployment_types = item_data.get("deployment_types", ["all"])

        # If deployment_types is a string, convert to list
        if isinstance(deployment_types, str):
            deployment_types = [deployment_types]

        should_load = self.deployment_type in deployment_types or "all" in deployment_types

        if not should_load:
            self.logger.debug(f"Skipping {item_name} (not in deployment type: {self.deployment_type})")

        return should_load

    def get_content_types(self, content_type_strings):
        """Convert content type strings to ContentType objects.

        Args:
            content_type_strings: List of strings like ['dcim.device', 'dcim.interface']

        Returns:
            list: List of ContentType objects
        """
        content_types = []
        for ct_string in content_type_strings:
            try:
                app_label, model = ct_string.split(".")
                ct = ContentType.objects.get(app_label=app_label, model=model)
                content_types.append(ct)
            except (ValueError, ContentType.DoesNotExist):
                self.logger.warning(f"Could not find content type: {ct_string}")
        return content_types

    def add_content_types(self, obj, content_type_strings):
        """Add content type memberships without removing existing memberships."""
        content_types = self.get_content_types(content_type_strings)
        if content_types:
            obj.content_types.add(*content_types)

    def run(self):
        """Execute the job to load bootstrap data.

        Returns:
            str: Success message
        """
        self.logger.info(
            f"Starting NVIDIA Config Manager Bootstrap Data Load (deployment_type: {self.deployment_type})",
            extra={"grouping": "bootstrap"},
        )

        # Load in dependency order
        self.load_manufacturers()
        self.load_tenants()
        self.load_location_types()
        self.load_namespaces()
        self.load_statuses()
        self.load_locations()
        self.load_rack_groups()
        self.load_racks()
        self.load_roles()
        self.load_tags()
        self.load_custom_fields()
        self.load_platforms()
        self.load_device_types()
        self.load_relationships()
        self.load_config_context_schemas()
        self.load_config_contexts()
        # Physical rack elevations — runs last: needs the devices to already exist (created by the
        # fabric/server DesignJobs). Missing devices are skipped, not errored.
        self.load_device_placements()

        self.logger.info("Bootstrap Data Load Complete!", extra={"grouping": "bootstrap"})
        return "Bootstrap data load completed successfully"

    def load_device_placements(self):
        """Place devices into racks (rack / U-position / face) from data/device_placements.yaml.

        Physical rack elevations are inventory data, kept in one map independent of the render-intent
        designs. Idempotent: updates rack/position/face on the EXISTING Device (devices are created by
        the fabric/server DesignJobs, so run/re-run this AFTER them — a device not found yet is skipped
        with a log line, not an error). Entries with `skip: true` are managed manually (UI) and are
        ignored here. Each entry is isolated: one failure (e.g. a U-position overlap) never blocks the rest.
        """
        grp = {"grouping": "device_placements"}
        self.logger.info("Loading Device Placements (rack elevations)", extra=grp)

        placements_file = self.data_path / "device_placements.yaml"
        if not placements_file.exists():
            self.logger.warning(f"Device placements file not found: {placements_file}", extra=grp)
            return

        try:
            with open(placements_file) as f:
                placements = yaml.safe_load(f) or []
        except Exception as e:
            self.logger.failure("Error reading device placements file", extra=grp)
            self.logger.debug(str(e))
            return

        for entry in placements:
            entry = entry or {}
            name = entry.get("device")
            if not name:
                self.logger.warning("Skipping placement with no device name", extra=grp)
                continue
            if not self.should_load_item(entry, name):
                continue
            if entry.get("skip"):
                self.logger.info(f"Skipping {name} — manually managed (skip: true)", extra=grp)
                continue
            try:
                device = Device.objects.get(name=name)
            except Device.DoesNotExist:
                self.logger.warning(
                    f"Device {name} not found yet (run after the fabric/server jobs) — skipping", extra=grp
                )
                continue

            rack_name = entry.get("rack")
            rack = Rack.objects.filter(name=rack_name, location=device.location).first() or (
                Rack.objects.filter(name=rack_name).first() if rack_name else None
            )
            if not rack:
                self.logger.warning(f"Rack {rack_name!r} not found for {name}; skipping", extra=grp)
                continue

            try:
                device.rack = rack
                device.position = entry.get("position")
                device.face = entry.get("face", "front")
                device.validated_save()
                self.logger.success(
                    f"Placed {name} -> {rack.name} U{device.position} {device.face}", extra=grp
                )
            except Exception as e:  # e.g. U-position overlap — isolate, keep going
                self.logger.error(f"Could not place {name} in {rack.name}: {e}", extra=grp)
                self.logger.debug(str(e))

    def load_manufacturers(self):
        """Load manufacturers from YAML template."""
        self.logger.info("Loading Manufacturers", extra={"grouping": "manufacturers"})

        manufacturers_file = self.data_path / "manufacturers.yaml"

        if not manufacturers_file.exists():
            self.logger.failure(f"Manufacturers file not found: {manufacturers_file}")
            return

        try:
            with open(manufacturers_file) as f:
                manufacturers = yaml.safe_load(f)

            if not manufacturers:
                self.logger.warning("No manufacturers found in file")
                return

            for mfg_data in manufacturers:
                try:
                    name = mfg_data.get("name")
                    if not name:
                        self.logger.warning("Skipping manufacturer with no name")
                        continue

                    # Check deployment type filtering
                    if not self.should_load_item(mfg_data, f"manufacturer '{name}'"):
                        continue

                    mfg, created = Manufacturer.objects.update_or_create(
                        name=name,
                        defaults={"description": mfg_data.get("description", "")},
                    )

                    if created:
                        self.logger.success(
                            f"Created manufacturer: {name}",
                            extra={"grouping": "manufacturers", "object": mfg},
                        )
                    else:
                        self.logger.info(
                            f"Manufacturer already exists: {name}",
                            extra={"grouping": "manufacturers", "object": mfg},
                        )

                except Exception as e:
                    self.logger.error(
                        f"Error processing manufacturer: {mfg_data.get('name', 'unknown')}",
                        extra={"grouping": "manufacturers"},
                    )
                    self.logger.debug(str(e))

        except Exception as e:
            self.logger.failure("Error reading manufacturers file", extra={"grouping": "manufacturers"})
            self.logger.debug(str(e))

    def _load_single_device_type(self, device_type_file, manufacturer):
        """Load a single device type from a YAML file.

        Args:
            device_type_file: Path to the device type YAML file.
            manufacturer: The Manufacturer instance to associate with.
        """
        with open(device_type_file) as f:
            dt_data = yaml.safe_load(f)

        if not dt_data or "model" not in dt_data:
            self.logger.warning(
                f"Invalid device type file: {device_type_file.name}",
                extra={"grouping": "device_types"},
            )
            return

        if not self.should_load_item(dt_data, f"device type '{dt_data['model']}'"):
            return

        device_type, created = DeviceType.objects.update_or_create(
            manufacturer=manufacturer,
            model=dt_data["model"],
            defaults={
                "part_number": dt_data.get("part_number", ""),
                "u_height": dt_data.get("u_height", 1),
                "is_full_depth": dt_data.get("is_full_depth", True),
            },
        )

        manufacturer_name = manufacturer.name
        if created:
            self.logger.success(
                f"Created device type: {manufacturer_name} {dt_data['model']}",
                extra={"grouping": "device_types", "object": device_type},
            )
        else:
            self.logger.info(
                f"Device type already exists: {manufacturer_name} {dt_data['model']}",
                extra={"grouping": "device_types", "object": device_type},
            )

    def _load_manufacturer_device_types(self, manufacturer_dir):
        """Load all device types for a single manufacturer directory.

        Args:
            manufacturer_dir: Path to the manufacturer subdirectory.
        """
        manufacturer_name = manufacturer_dir.name

        try:
            manufacturer = Manufacturer.objects.get(name=manufacturer_name)
        except Manufacturer.DoesNotExist:
            self.logger.warning(
                f"Manufacturer not found: {manufacturer_name}, skipping device types",
                extra={"grouping": "device_types"},
            )
            return

        for device_type_file in manufacturer_dir.glob("*.yaml"):
            try:
                self._load_single_device_type(device_type_file, manufacturer)
            except Exception as e:
                self.logger.error(
                    f"Error processing device type {device_type_file.name}",
                    extra={"grouping": "device_types"},
                )
                self.logger.debug(str(e))

    def load_device_types(self):
        """Load device types from YAML templates organized by manufacturer."""
        self.logger.info("Loading Device Types", extra={"grouping": "device_types"})

        device_types_path = self.data_path / "device_types"

        if not device_types_path.exists():
            self.logger.failure(f"Device types directory not found: {device_types_path}")
            return

        try:
            for manufacturer_dir in device_types_path.iterdir():
                if manufacturer_dir.is_dir():
                    self._load_manufacturer_device_types(manufacturer_dir)
        except Exception as e:
            self.logger.failure("Error reading device types", extra={"grouping": "device_types"})
            self.logger.debug(str(e))

    def load_roles(self):
        """Load roles from YAML template with optional deployment type filtering."""
        self.logger.info("Loading Roles", extra={"grouping": "roles"})

        roles_file = self.data_path / "roles.yaml"

        if not roles_file.exists():
            self.logger.failure(f"Roles file not found: {roles_file}")
            return

        try:
            with open(roles_file) as f:
                roles = yaml.safe_load(f)

            if not roles:
                self.logger.warning("No roles found in file")
                return

            for role_data in roles:
                try:
                    name = role_data.get("name")
                    if not name:
                        self.logger.warning("Skipping role with no name")
                        continue

                    # Check deployment type filtering
                    if not self.should_load_item(role_data, f"role '{name}'"):
                        continue

                    role, created = Role.objects.update_or_create(
                        name=name,
                        defaults={
                            "color": role_data.get("color", "grey"),
                            "weight": role_data.get("weight"),
                        },
                    )

                    # Add content_types for both new and existing roles without
                    # removing memberships created by other jobs.
                    if "content_types" in role_data:
                        self.add_content_types(role, role_data["content_types"])
                        role.validated_save()

                    if created:
                        self.logger.success(
                            f"Created role: {name}",
                            extra={"grouping": "roles", "object": role},
                        )
                    else:
                        self.logger.info(
                            f"Role already exists: {name}",
                            extra={"grouping": "roles", "object": role},
                        )

                except Exception as e:
                    self.logger.error(
                        f"Error processing role: {role_data.get('name', 'unknown')}",
                        extra={"grouping": "roles"},
                    )
                    self.logger.debug(str(e))

        except Exception as e:
            self.logger.failure("Error reading roles file", extra={"grouping": "roles"})
            self.logger.debug(str(e))

    def load_tags(self):
        """Load tags from YAML template."""
        self.logger.info("Loading Tags", extra={"grouping": "tags"})

        tags_file = self.data_path / "tags.yaml"

        if not tags_file.exists():
            self.logger.failure(f"Tags file not found: {tags_file}")
            return

        try:
            with open(tags_file) as f:
                tags = yaml.safe_load(f)

            if not tags:
                self.logger.warning("No tags found in file")
                return

            for tag_data in tags:
                try:
                    name = tag_data.get("name")
                    if not name:
                        self.logger.warning("Skipping tag with no name")
                        continue

                    # Check deployment type filtering
                    if not self.should_load_item(tag_data, f"tag '{name}'"):
                        continue

                    # Use update_or_create to always set color/description
                    tag, created = Tag.objects.update_or_create(
                        name=name,
                        defaults={
                            "description": tag_data.get("description", ""),
                            "color": tag_data.get("color", "9e9e9e"),
                        },
                    )

                    # Add content_types without removing memberships created by other jobs.
                    if "content_types" in tag_data:
                        self.add_content_types(tag, tag_data["content_types"])

                    if created:
                        self.logger.success(
                            f"Created tag: {name}",
                            extra={"grouping": "tags", "object": tag},
                        )
                    else:
                        self.logger.info(
                            f"Tag already exists: {name}",
                            extra={"grouping": "tags", "object": tag},
                        )

                except Exception as e:
                    self.logger.error(
                        f"Error processing tag: {tag_data.get('name', 'unknown')}",
                        extra={"grouping": "tags"},
                    )
                    self.logger.debug(str(e))

        except Exception as e:
            self.logger.failure("Error reading tags file", extra={"grouping": "tags"})
            self.logger.debug(str(e))

    def load_custom_fields(self):
        """Load custom fields from YAML template."""
        self.logger.info("Loading Custom Fields", extra={"grouping": "custom_fields"})

        cf_file = self.data_path / "custom_fields.yaml"
        if not cf_file.exists():
            self.logger.failure(f"Custom fields file not found: {cf_file}")
            return

        try:
            with open(cf_file) as f:
                custom_fields = yaml.safe_load(f) or []
        except yaml.YAMLError as exc:
            self.logger.failure(f"Failed to parse custom fields file: {exc}")
            return

        for cf_data in custom_fields:
            if not isinstance(cf_data, dict):
                self.logger.failure(f"Custom field entry is not a dict, skipping: {cf_data!r}")
                continue

            key = cf_data.get("key")
            if not key:
                self.logger.failure(f"Custom field entry missing required 'key': {cf_data}")
                continue
            if not self.should_load_item(cf_data, f"custom field '{key}'"):
                continue

            try:
                defaults = {
                    "label": cf_data.get("label", key),
                    "type": cf_data.get("type", "text"),
                    "description": cf_data.get("description", ""),
                }
                # filter_logic is optional; only override Nautobot's default when set.
                if "filter_logic" in cf_data:
                    defaults["filter_logic"] = cf_data["filter_logic"]
                cf, created = CustomField.objects.update_or_create(key=key, defaults=defaults)
                # Add content_types without removing memberships created by other jobs.
                if "content_types" in cf_data:
                    self.add_content_types(cf, cf_data["content_types"])
            except Exception as exc:
                self.logger.failure(f"Error processing custom field '{key}': {exc}")
                continue

            self.logger.success(
                f"{'Created' if created else 'Updated'} custom field: {key}",
                extra={"grouping": "custom_fields", "object": cf},
            )

    def load_platforms(self):
        """Load platforms from YAML template."""
        self.logger.info("Loading Platforms", extra={"grouping": "platforms"})

        platforms_file = self.data_path / "platforms.yaml"

        if not platforms_file.exists():
            self.logger.failure(f"Platforms file not found: {platforms_file}")
            return

        try:
            with open(platforms_file) as f:
                platforms = yaml.safe_load(f)

            if not platforms:
                self.logger.warning("No platforms found in file")
                return

            for platform_data in platforms:
                try:
                    name = platform_data.get("name")
                    manufacturer_name = platform_data.get("manufacturer")

                    if not name:
                        self.logger.warning("Skipping platform with no name")
                        continue

                    # Check deployment type filtering
                    if not self.should_load_item(platform_data, f"platform '{name}'"):
                        continue

                    # Get manufacturer if specified
                    manufacturer = None
                    if manufacturer_name:
                        try:
                            manufacturer = Manufacturer.objects.get(name=manufacturer_name)
                        except Manufacturer.DoesNotExist:
                            self.logger.warning(
                                f"Manufacturer not found for platform {name}: {manufacturer_name}",
                                extra={"grouping": "platforms"},
                            )

                    platform, created = Platform.objects.update_or_create(
                        name=name,
                        defaults={
                            "manufacturer": manufacturer,
                            "description": platform_data.get("description", ""),
                            "napalm_driver": platform_data.get("napalm_driver", ""),
                        },
                    )

                    if created:
                        self.logger.success(
                            f"Created platform: {name}",
                            extra={"grouping": "platforms", "object": platform},
                        )
                    else:
                        self.logger.info(
                            f"Platform already exists: {name}",
                            extra={"grouping": "platforms", "object": platform},
                        )

                except Exception as e:
                    self.logger.error(
                        f"Error processing platform: {platform_data.get('name', 'unknown')}",
                        extra={"grouping": "platforms"},
                    )
                    self.logger.debug(str(e))

        except Exception as e:
            self.logger.failure("Error reading platforms file", extra={"grouping": "platforms"})
            self.logger.debug(str(e))

    def load_tenants(self):
        """Load tenants from YAML template."""
        self.logger.info("Loading Tenants", extra={"grouping": "tenants"})

        tenants_file = self.data_path / "tenants.yaml"

        if not tenants_file.exists():
            self.logger.warning(f"Tenants file not found: {tenants_file}")
            return

        try:
            with open(tenants_file) as f:
                tenants = yaml.safe_load(f)

            if not tenants:
                self.logger.warning("No tenants found in file")
                return

            for tenant_data in tenants:
                try:
                    name = tenant_data.get("name")
                    if not name:
                        self.logger.warning("Skipping tenant with no name")
                        continue

                    # Check deployment type filtering
                    if not self.should_load_item(tenant_data, f"tenant '{name}'"):
                        continue

                    tenant, created = Tenant.objects.update_or_create(
                        name=name,
                        defaults={"description": tenant_data.get("description", "")},
                    )

                    if created:
                        self.logger.success(
                            f"Created tenant: {name}",
                            extra={"grouping": "tenants", "object": tenant},
                        )
                    else:
                        self.logger.info(
                            f"Tenant already exists: {name}",
                            extra={"grouping": "tenants", "object": tenant},
                        )

                except Exception as e:
                    self.logger.error(
                        f"Error processing tenant: {tenant_data.get('name', 'unknown')}",
                        extra={"grouping": "tenants"},
                    )
                    self.logger.debug(str(e))

        except Exception as e:
            self.logger.failure("Error reading tenants file", extra={"grouping": "tenants"})
            self.logger.debug(str(e))

    def load_location_types(self):
        """Load location types from YAML template."""
        self.logger.info("Loading Location Types", extra={"grouping": "location_types"})

        location_types_file = self.data_path / "location_types.yaml"

        if not location_types_file.exists():
            self.logger.warning(f"Location types file not found: {location_types_file}")
            return

        try:
            with open(location_types_file) as f:
                location_types = yaml.safe_load(f)

            if not location_types:
                self.logger.warning("No location types found in file")
                return

            for lt_data in location_types:
                try:
                    name = lt_data.get("name")
                    if not name:
                        self.logger.warning("Skipping location type with no name")
                        continue

                    # Check deployment type filtering
                    if not self.should_load_item(lt_data, f"location type '{name}'"):
                        continue

                    # Get parent location type if specified
                    parent = None
                    if "parent" in lt_data and lt_data["parent"]:
                        try:
                            parent = LocationType.objects.get(name=lt_data["parent"])
                        except LocationType.DoesNotExist:
                            self.logger.warning(
                                f"Parent location type not found: {lt_data['parent']}",
                                extra={"grouping": "location_types"},
                            )

                    lt, created = LocationType.objects.update_or_create(
                        name=name,
                        defaults={
                            "description": lt_data.get("description", ""),
                            "nestable": lt_data.get("nestable", True),
                            "parent": parent,
                        },
                    )

                    # Add content_types without removing memberships created by other jobs.
                    if "content_types" in lt_data:
                        self.add_content_types(lt, lt_data["content_types"])

                    if created:
                        self.logger.success(
                            f"Created location type: {name}",
                            extra={"grouping": "location_types", "object": lt},
                        )
                    else:
                        self.logger.info(
                            f"Location type already exists: {name}",
                            extra={"grouping": "location_types", "object": lt},
                        )

                except Exception as e:
                    self.logger.error(
                        f"Error processing location type: {lt_data.get('name', 'unknown')}",
                        extra={"grouping": "location_types"},
                    )
                    self.logger.debug(str(e))

        except Exception as e:
            self.logger.failure(
                "Error reading location types file",
                extra={"grouping": "location_types"},
            )
            self.logger.debug(str(e))

    def load_locations(self):
        """Load locations from YAML template (depends on location_types, statuses, tenants)."""
        self.logger.info("Loading Locations", extra={"grouping": "locations"})

        locations_file = self.data_path / "locations.yaml"

        if not locations_file.exists():
            self.logger.warning(f"Locations file not found: {locations_file}")
            return

        try:
            with open(locations_file) as f:
                locations = yaml.safe_load(f)

            if not locations:
                self.logger.warning("No locations found in file")
                return

            for loc_data in locations:
                try:
                    name = loc_data.get("name")
                    if not name:
                        self.logger.warning("Skipping location with no name")
                        continue

                    if not self.should_load_item(loc_data, f"location '{name}'"):
                        continue

                    # required: location_type + status
                    try:
                        location_type = LocationType.objects.get(name=loc_data["location_type"])
                    except (KeyError, LocationType.DoesNotExist):
                        self.logger.warning(
                            f"Skipping location '{name}': location_type "
                            f"'{loc_data.get('location_type')}' not found",
                            extra={"grouping": "locations"},
                        )
                        continue
                    try:
                        status = Status.objects.get(name=loc_data.get("status", "Active"))
                    except Status.DoesNotExist:
                        self.logger.warning(
                            f"Skipping location '{name}': status '{loc_data.get('status')}' not found",
                            extra={"grouping": "locations"},
                        )
                        continue

                    # optional: parent (another Location), tenant
                    parent = None
                    if loc_data.get("parent"):
                        try:
                            parent = Location.objects.get(name=loc_data["parent"])
                        except Location.DoesNotExist:
                            self.logger.warning(
                                f"Parent location not found for '{name}': {loc_data['parent']}",
                                extra={"grouping": "locations"},
                            )
                    tenant = None
                    if loc_data.get("tenant"):
                        try:
                            tenant = Tenant.objects.get(name=loc_data["tenant"])
                        except Tenant.DoesNotExist:
                            self.logger.warning(
                                f"Tenant not found for location '{name}': {loc_data['tenant']}",
                                extra={"grouping": "locations"},
                            )

                    loc, created = Location.objects.update_or_create(
                        name=name,
                        location_type=location_type,
                        defaults={
                            "status": status,
                            "parent": parent,
                            "tenant": tenant,
                            "description": loc_data.get("description", ""),
                        },
                    )

                    if created:
                        self.logger.success(
                            f"Created location: {name}",
                            extra={"grouping": "locations", "object": loc},
                        )
                    else:
                        self.logger.info(
                            f"Location already exists: {name}",
                            extra={"grouping": "locations", "object": loc},
                        )

                except Exception as e:
                    self.logger.error(
                        f"Error processing location: {loc_data.get('name', 'unknown')}",
                        extra={"grouping": "locations"},
                    )
                    self.logger.debug(str(e))

        except Exception as e:
            self.logger.failure(
                "Error reading locations file",
                extra={"grouping": "locations"},
            )
            self.logger.debug(str(e))

    def load_rack_groups(self):
        """Load rack groups from YAML template (depends on locations)."""
        self.logger.info("Loading Rack Groups", extra={"grouping": "rack_groups"})

        rack_groups_file = self.data_path / "rack_groups.yaml"

        if not rack_groups_file.exists():
            self.logger.warning(f"Rack groups file not found: {rack_groups_file}")
            return

        try:
            with open(rack_groups_file) as f:
                rack_groups = yaml.safe_load(f)

            if not rack_groups:
                self.logger.warning("No rack groups found in file")
                return

            for rg_data in rack_groups:
                try:
                    name = rg_data.get("name")
                    if not name:
                        self.logger.warning("Skipping rack group with no name")
                        continue

                    if not self.should_load_item(rg_data, f"rack group '{name}'"):
                        continue

                    try:
                        location = Location.objects.get(name=rg_data["location"])
                    except (KeyError, Location.DoesNotExist):
                        self.logger.warning(
                            f"Skipping rack group '{name}': location "
                            f"'{rg_data.get('location')}' not found",
                            extra={"grouping": "rack_groups"},
                        )
                        continue

                    parent = None
                    if rg_data.get("parent"):
                        try:
                            parent = RackGroup.objects.get(name=rg_data["parent"])
                        except RackGroup.DoesNotExist:
                            self.logger.warning(
                                f"Parent rack group not found for '{name}': {rg_data['parent']}",
                                extra={"grouping": "rack_groups"},
                            )

                    rg, created = RackGroup.objects.update_or_create(
                        name=name,
                        location=location,
                        defaults={
                            "parent": parent,
                            "description": rg_data.get("description", ""),
                        },
                    )

                    if created:
                        self.logger.success(
                            f"Created rack group: {name}",
                            extra={"grouping": "rack_groups", "object": rg},
                        )
                    else:
                        self.logger.info(
                            f"Rack group already exists: {name}",
                            extra={"grouping": "rack_groups", "object": rg},
                        )

                except Exception as e:
                    self.logger.error(
                        f"Error processing rack group: {rg_data.get('name', 'unknown')}",
                        extra={"grouping": "rack_groups"},
                    )
                    self.logger.debug(str(e))

        except Exception as e:
            self.logger.failure(
                "Error reading rack groups file",
                extra={"grouping": "rack_groups"},
            )
            self.logger.debug(str(e))

    def load_racks(self):
        """Load racks from YAML template (depends on locations, statuses, rack_groups)."""
        self.logger.info("Loading Racks", extra={"grouping": "racks"})

        racks_file = self.data_path / "racks.yaml"

        if not racks_file.exists():
            self.logger.warning(f"Racks file not found: {racks_file}")
            return

        try:
            with open(racks_file) as f:
                racks = yaml.safe_load(f)

            if not racks:
                self.logger.warning("No racks found in file")
                return

            for rack_data in racks:
                try:
                    name = rack_data.get("name")
                    if not name:
                        self.logger.warning("Skipping rack with no name")
                        continue

                    if not self.should_load_item(rack_data, f"rack '{name}'"):
                        continue

                    # required: location + status
                    try:
                        location = Location.objects.get(name=rack_data["location"])
                    except (KeyError, Location.DoesNotExist):
                        self.logger.warning(
                            f"Skipping rack '{name}': location "
                            f"'{rack_data.get('location')}' not found",
                            extra={"grouping": "racks"},
                        )
                        continue
                    try:
                        status = Status.objects.get(name=rack_data.get("status", "Active"))
                    except Status.DoesNotExist:
                        self.logger.warning(
                            f"Skipping rack '{name}': status '{rack_data.get('status')}' not found",
                            extra={"grouping": "racks"},
                        )
                        continue

                    # optional: rack_group, tenant, role
                    rack_group = None
                    if rack_data.get("rack_group"):
                        try:
                            rack_group = RackGroup.objects.get(name=rack_data["rack_group"])
                        except RackGroup.DoesNotExist:
                            self.logger.warning(
                                f"Rack group not found for '{name}': {rack_data['rack_group']}",
                                extra={"grouping": "racks"},
                            )
                    tenant = None
                    if rack_data.get("tenant"):
                        try:
                            tenant = Tenant.objects.get(name=rack_data["tenant"])
                        except Tenant.DoesNotExist:
                            self.logger.warning(
                                f"Tenant not found for rack '{name}': {rack_data['tenant']}",
                                extra={"grouping": "racks"},
                            )
                    role = None
                    if rack_data.get("role"):
                        try:
                            role = Role.objects.get(name=rack_data["role"])
                        except Role.DoesNotExist:
                            self.logger.warning(
                                f"Role not found for rack '{name}': {rack_data['role']}",
                                extra={"grouping": "racks"},
                            )

                    rack, created = Rack.objects.update_or_create(
                        name=name,
                        location=location,
                        defaults={
                            "rack_group": rack_group,
                            "status": status,
                            "tenant": tenant,
                            "role": role,
                            "u_height": rack_data.get("u_height", 42),
                            "width": rack_data.get("width", 19),
                            "type": rack_data.get("type", ""),
                            "desc_units": rack_data.get("desc_units", False),
                            "comments": rack_data.get("description", ""),
                        },
                    )

                    if created:
                        self.logger.success(
                            f"Created rack: {name}",
                            extra={"grouping": "racks", "object": rack},
                        )
                    else:
                        self.logger.info(
                            f"Rack already exists: {name}",
                            extra={"grouping": "racks", "object": rack},
                        )

                except Exception as e:
                    self.logger.error(
                        f"Error processing rack: {rack_data.get('name', 'unknown')}",
                        extra={"grouping": "racks"},
                    )
                    self.logger.debug(str(e))

        except Exception as e:
            self.logger.failure(
                "Error reading racks file",
                extra={"grouping": "racks"},
            )
            self.logger.debug(str(e))

    def load_namespaces(self):
        """Load namespaces from YAML template."""
        self.logger.info("Loading Namespaces", extra={"grouping": "namespaces"})

        namespaces_file = self.data_path / "namespaces.yaml"

        if not namespaces_file.exists():
            self.logger.warning(f"Namespaces file not found: {namespaces_file}")
            return

        try:
            with open(namespaces_file) as f:
                namespaces = yaml.safe_load(f)

            if not namespaces:
                self.logger.warning("No namespaces found in file")
                return

            for ns_data in namespaces:
                try:
                    name = ns_data.get("name")
                    if not name:
                        self.logger.warning("Skipping namespace with no name")
                        continue

                    # Check deployment type filtering
                    if not self.should_load_item(ns_data, f"namespace '{name}'"):
                        continue

                    ns, created = Namespace.objects.update_or_create(
                        name=name,
                        defaults={"description": ns_data.get("description", "")},
                    )

                    if created:
                        self.logger.success(
                            f"Created namespace: {name}",
                            extra={"grouping": "namespaces", "object": ns},
                        )
                    else:
                        self.logger.info(
                            f"Namespace already exists: {name}",
                            extra={"grouping": "namespaces", "object": ns},
                        )

                except Exception as e:
                    self.logger.error(
                        f"Error processing namespace: {ns_data.get('name', 'unknown')}",
                        extra={"grouping": "namespaces"},
                    )
                    self.logger.debug(str(e))

        except Exception as e:
            self.logger.failure("Error reading namespaces file", extra={"grouping": "namespaces"})
            self.logger.debug(str(e))

    def load_statuses(self):
        """Load statuses from YAML template."""
        self.logger.info("Loading Statuses", extra={"grouping": "statuses"})

        statuses_file = self.data_path / "statuses.yaml"

        if not statuses_file.exists():
            self.logger.warning(f"Statuses file not found: {statuses_file}")
            return

        try:
            with open(statuses_file) as f:
                statuses = yaml.safe_load(f)

            if not statuses:
                self.logger.warning("No statuses found in file")
                return

            for status_data in statuses:
                try:
                    name = status_data.get("name")
                    if not name:
                        self.logger.warning("Skipping status with no name")
                        continue

                    # Check deployment type filtering
                    if not self.should_load_item(status_data, f"status '{name}'"):
                        continue

                    # Use update_or_create to handle color updates
                    status, created = Status.objects.update_or_create(
                        name=name,
                        defaults={
                            "description": status_data.get("description", ""),
                            "color": status_data.get("color", "9e9e9e"),
                        },
                    )

                    # Add content_types without removing memberships created by other jobs.
                    if "content_types" in status_data:
                        self.add_content_types(status, status_data["content_types"])

                    if created:
                        self.logger.success(
                            f"Created status: {name}",
                            extra={"grouping": "statuses", "object": status},
                        )
                    else:
                        self.logger.info(
                            f"Status already exists: {name}",
                            extra={"grouping": "statuses", "object": status},
                        )

                except Exception as e:
                    self.logger.error(
                        f"Error processing status: {status_data.get('name', 'unknown')}",
                        extra={"grouping": "statuses"},
                    )
                    self.logger.debug(str(e))

        except Exception as e:
            self.logger.failure("Error reading statuses file", extra={"grouping": "statuses"})
            self.logger.debug(str(e))

    def _resolve_content_type(self, ct_string):
        """Resolve a 'app_label.model' string to a ContentType."""
        app_label, model = ct_string.split(".")
        return ContentType.objects.get(app_label=app_label, model=model)

    def load_relationships(self):
        """Load relationships from YAML template."""
        self.logger.info("Loading Relationships", extra={"grouping": "relationships"})

        relationships_file = self.data_path / "relationships.yaml"

        if not relationships_file.exists():
            self.logger.warning(f"Relationships file not found: {relationships_file}")
            return

        with open(relationships_file) as f:
            relationships = yaml.safe_load(f)

        if not relationships:
            self.logger.warning("No relationships found in file")
            return

        for rel_data in relationships:
            name = rel_data["name"]

            if not self.should_load_item(rel_data, f"relationship '{name}'"):
                continue

            label = rel_data.get("label", name)
            # Look up by key — the stable unique identifier used by GraphQL.
            # Looking up by label alone fails when another job mutates the
            # label on an existing relationship, causing a duplicate with a
            # "-N" key suffix that breaks GraphQL.
            rel_key = rel_data.get("key") or slugify_dashes_to_underscores(label)

            relationship, created = Relationship.objects.update_or_create(
                key=rel_key,
                defaults={
                    "label": label,
                    "description": rel_data.get("description", ""),
                    "type": rel_data.get("type", "one-to-many"),
                    "source_type": self._resolve_content_type(rel_data["source_type"]),
                    "source_label": rel_data.get("source_label", ""),
                    "destination_type": self._resolve_content_type(rel_data["destination_type"]),
                    "destination_label": rel_data.get("destination_label", ""),
                    "required_on": rel_data.get("required_on", ""),
                },
            )

            if created:
                self.logger.success(
                    f"Created relationship: {name}",
                    extra={"grouping": "relationships", "object": relationship},
                )
            else:
                self.logger.info(
                    f"Relationship already exists: {name}",
                    extra={"grouping": "relationships", "object": relationship},
                )

    def load_config_context_schemas(self):
        """Load config context schemas from YAML template."""
        self.logger.info("Loading Config Context Schemas", extra={"grouping": "config_context_schemas"})

        schemas_file = self.data_path / "config_context_schemas.yaml"

        if not schemas_file.exists():
            self.logger.warning(f"Config context schemas file not found: {schemas_file}")
            return

        try:
            with open(schemas_file) as f:
                schemas = yaml.safe_load(f)

            if not schemas:
                self.logger.warning("No config context schemas found in file")
                return

            for schema_data in schemas:
                try:
                    name = schema_data.get("name")
                    if not name:
                        self.logger.warning("Skipping config context schema with no name")
                        continue

                    # Check deployment type filtering
                    if not self.should_load_item(schema_data, f"config context schema '{name}'"):
                        continue

                    schema, created = ConfigContextSchema.objects.update_or_create(
                        name=name,
                        defaults={
                            "description": schema_data.get("description", ""),
                            "data_schema": schema_data.get("data_schema", {}),
                        },
                    )

                    schema.validated_save()

                    if created:
                        self.logger.success(
                            f"Created config context schema: {name}",
                            extra={"grouping": "config_context_schemas", "object": schema},
                        )
                    else:
                        self.logger.info(
                            f"Config context schema already exists: {name}",
                            extra={"grouping": "config_context_schemas", "object": schema},
                        )

                except Exception as e:
                    self.logger.error(
                        f"Error processing config context schema: {schema_data.get('name', 'unknown')}",
                        extra={"grouping": "config_context_schemas"},
                    )
                    self.logger.debug(str(e))

        except Exception as e:
            self.logger.failure(
                "Error reading config context schemas file",
                extra={"grouping": "config_context_schemas"},
            )
            self.logger.debug(str(e))

    def load_config_contexts(self):
        """Load config contexts from YAML template."""
        self.logger.info("Loading Config Contexts", extra={"grouping": "config_contexts"})

        config_contexts_file = self.data_path / "config_contexts.yaml"

        if not config_contexts_file.exists():
            self.logger.warning(f"Config contexts file not found: {config_contexts_file}")
            return

        try:
            with open(config_contexts_file) as f:
                config_contexts = yaml.safe_load(f)

            if not config_contexts:
                self.logger.warning("No config contexts found in file")
                return

            for cc_data in config_contexts:
                try:
                    name = cc_data.get("name")
                    if not name:
                        self.logger.warning("Skipping config context with no name")
                        continue

                    # Check deployment type filtering
                    if not self.should_load_item(cc_data, f"config context '{name}'"):
                        continue

                    # Build defaults dict
                    defaults = {
                        "description": cc_data.get("description", ""),
                        "weight": cc_data.get("weight", 1000),
                        "is_active": cc_data.get("is_active", True),
                        "data": cc_data.get("data", {}),
                    }

                    # Get schema if specified
                    if "schema" in cc_data and cc_data["schema"]:
                        schema_name = cc_data["schema"]
                        try:
                            schema = ConfigContextSchema.objects.get(name=schema_name)
                            defaults["config_context_schema"] = schema
                        except ConfigContextSchema.DoesNotExist:
                            self.logger.warning(
                                f"Schema not found for config context '{name}': {schema_name}",
                                extra={"grouping": "config_contexts"},
                            )

                    cc, created = ConfigContext.objects.update_or_create(
                        name=name,
                        defaults=defaults,
                    )

                    # Set roles if specified
                    if "roles" in cc_data and cc_data["roles"]:
                        roles = []
                        for role_name in cc_data["roles"]:
                            try:
                                role = Role.objects.get(name=role_name)
                                roles.append(role)
                            except Role.DoesNotExist:
                                self.logger.warning(
                                    f"Role not found for config context: {role_name}",
                                    extra={"grouping": "config_contexts"},
                                )
                        if roles:
                            cc.roles.set(roles)

                    # Set platforms if specified
                    if "platforms" in cc_data and cc_data["platforms"]:
                        platforms = []
                        for platform_name in cc_data["platforms"]:
                            try:
                                platform = Platform.objects.get(name=platform_name)
                                platforms.append(platform)
                            except Platform.DoesNotExist:
                                self.logger.warning(
                                    f"Platform not found for config context: {platform_name}",
                                    extra={"grouping": "config_contexts"},
                                )
                        if platforms:
                            cc.platforms.set(platforms)

                    cc.validated_save()

                    if created:
                        self.logger.success(
                            f"Created config context: {name}",
                            extra={"grouping": "config_contexts", "object": cc},
                        )
                    else:
                        self.logger.info(
                            f"Config context already exists: {name}",
                            extra={"grouping": "config_contexts", "object": cc},
                        )

                except Exception as e:
                    self.logger.error(
                        f"Error processing config context: {cc_data.get('name', 'unknown')}",
                        extra={"grouping": "config_contexts"},
                    )
                    self.logger.debug(str(e))

        except Exception as e:
            self.logger.failure(
                "Error reading config contexts file",
                extra={"grouping": "config_contexts"},
            )
            self.logger.debug(str(e))


register_jobs(LoadBootstrapData)
