from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, Mapping

from zarch.extensions.base import ZArchExtension


DEFAULT_CONFIG: Dict[str, Any] = {
    "network": "default",
    "create_network_if_missing": False,
    "network_subnet_mode": "CUSTOM",
    "network_routing_mode": "REGIONAL",
    "create_subnet_if_missing": False,
    "subnet_name": "",
    "subnet_region": "",
    "subnet_cidr": "10.10.0.0/24",
    "ensure_private_service_access": True,
    "reserved_range_name": "",
    "reserved_range_prefix_length": 24,
    "service_networking_service": "servicenetworking.googleapis.com",
    "enable_compute_api": True,
    "enable_service_networking_api": True,
    "api_enable_wait_seconds": 180,
    "api_enable_poll_interval_seconds": 5,
}

BOOL_TRUE_VALUES = {"true", "1", "yes", "y", "on"}
BOOL_FALSE_VALUES = {"false", "0", "no", "n", "off"}
ALLOWED_SUBNET_MODES = {"CUSTOM", "AUTO"}
ALLOWED_ROUTING_MODES = {"REGIONAL", "GLOBAL"}
NETWORK_NAME_PATTERN = re.compile(r"^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$")
RESOURCE_NAME_PATTERN = re.compile(r"^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$")
CIDR_PATTERN = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}/\d{1,2}$")


class Extension(ZArchExtension):
    """
    Z-Arch extension: vpc-bootstrap
    """

    def claim(self, extension_name: str, extension_block: Dict[str, Any]) -> bool:
        return extension_block.get("type") == "vpc-bootstrap"

    def post_project_bootstrap(
        self,
        project_context,
        extension_configuration: Dict[str, Any],
    ) -> None:
        settings = self._resolve_settings(extension_configuration, project_context)
        network_name = str(settings["network_name"])

        project_context.log("vpc-bootstrap: enabling required APIs.")
        self._enable_apis(project_context, settings)

        project_context.log(
            f"vpc-bootstrap: ensuring VPC network '{network_name}' exists."
        )
        self._ensure_network(project_context, settings)

        if settings["create_subnet_if_missing"]:
            project_context.log(
                "vpc-bootstrap: ensuring subnet "
                f"'{settings['subnet_name']}' in region '{settings['subnet_region']}'."
            )
            self._ensure_subnet(project_context, settings)

        if not settings["ensure_private_service_access"]:
            project_context.log(
                "vpc-bootstrap: ensure_private_service_access=false; skipping "
                "Service Networking range/connection setup.",
                level="warn",
            )
            return

        project_context.log(
            "vpc-bootstrap: ensuring reserved Service Networking range "
            f"'{settings['reserved_range_name']}'."
        )
        self._ensure_reserved_range(project_context, settings)

        project_context.log(
            "vpc-bootstrap: ensuring Service Networking connection on "
            f"network '{network_name}'."
        )
        self._ensure_service_networking_connection(project_context, settings)

        project_context.log("vpc-bootstrap: private service networking bootstrap complete.")

    def _resolve_settings(
        self,
        extension_configuration: Mapping[str, Any],
        project_context,
    ) -> Dict[str, Any]:
        config_values: Dict[str, Any] = {}
        if isinstance(extension_configuration, Mapping):
            nested = extension_configuration.get("config")
            if isinstance(nested, Mapping):
                config_values.update(nested)
            else:
                config_values.update(extension_configuration)

        merged = dict(DEFAULT_CONFIG)
        merged.update(config_values)
        merged["project_id"] = str(project_context.id)

        network_raw = str(merged.get("network", "")).strip()
        if not network_raw:
            raise RuntimeError("network must be non-empty.")
        network_name = self._extract_network_name(network_raw)
        if not NETWORK_NAME_PATTERN.fullmatch(network_name):
            raise RuntimeError(
                "network name must match GCP VPC naming rules "
                "(lowercase letters, digits, hyphens; <=63 chars)."
            )
        merged["network"] = network_raw
        merged["network_name"] = network_name

        merged["create_network_if_missing"] = self._parse_bool(
            merged.get("create_network_if_missing"),
            "create_network_if_missing",
        )
        subnet_mode = str(merged.get("network_subnet_mode", "CUSTOM")).strip().upper()
        if subnet_mode not in ALLOWED_SUBNET_MODES:
            raise RuntimeError("network_subnet_mode must be CUSTOM or AUTO.")
        merged["network_subnet_mode"] = subnet_mode

        routing_mode = str(merged.get("network_routing_mode", "REGIONAL")).strip().upper()
        if routing_mode not in ALLOWED_ROUTING_MODES:
            raise RuntimeError("network_routing_mode must be REGIONAL or GLOBAL.")
        merged["network_routing_mode"] = routing_mode

        merged["create_subnet_if_missing"] = self._parse_bool(
            merged.get("create_subnet_if_missing"),
            "create_subnet_if_missing",
        )
        subnet_region = str(merged.get("subnet_region") or project_context.region).strip()
        if not subnet_region:
            raise RuntimeError("subnet_region must be non-empty.")
        merged["subnet_region"] = subnet_region

        subnet_name = str(merged.get("subnet_name", "")).strip()
        if not subnet_name:
            subnet_name = f"{network_name}-{subnet_region}".replace("_", "-").lower()
        if not RESOURCE_NAME_PATTERN.fullmatch(subnet_name):
            raise RuntimeError(
                "subnet_name must match GCP subnet naming rules "
                "(lowercase letters, digits, hyphens; <=63 chars)."
            )
        merged["subnet_name"] = subnet_name

        subnet_cidr = str(merged.get("subnet_cidr", "")).strip()
        if not CIDR_PATTERN.fullmatch(subnet_cidr):
            raise RuntimeError(
                "subnet_cidr must look like a valid CIDR (for example 10.10.0.0/24)."
            )
        merged["subnet_cidr"] = subnet_cidr

        merged["ensure_private_service_access"] = self._parse_bool(
            merged.get("ensure_private_service_access"),
            "ensure_private_service_access",
        )

        reserved_range_name = str(merged.get("reserved_range_name", "")).strip()
        if not reserved_range_name:
            reserved_range_name = f"google-managed-services-{network_name}"
        if not RESOURCE_NAME_PATTERN.fullmatch(reserved_range_name):
            raise RuntimeError(
                "reserved_range_name must match GCP address naming rules "
                "(lowercase letters, digits, hyphens; <=63 chars)."
            )
        merged["reserved_range_name"] = reserved_range_name

        range_prefix = self._parse_int(
            merged.get("reserved_range_prefix_length"),
            "reserved_range_prefix_length",
        )
        if range_prefix < 16 or range_prefix > 29:
            raise RuntimeError("reserved_range_prefix_length must be between 16 and 29.")
        merged["reserved_range_prefix_length"] = range_prefix

        service_networking_service = str(
            merged.get("service_networking_service", "servicenetworking.googleapis.com")
        ).strip()
        if not service_networking_service:
            raise RuntimeError("service_networking_service must be non-empty.")
        merged["service_networking_service"] = service_networking_service

        merged["enable_compute_api"] = self._parse_bool(
            merged.get("enable_compute_api"),
            "enable_compute_api",
        )
        merged["enable_service_networking_api"] = self._parse_bool(
            merged.get("enable_service_networking_api"),
            "enable_service_networking_api",
        )
        merged["api_enable_wait_seconds"] = self._parse_int(
            merged.get("api_enable_wait_seconds"),
            "api_enable_wait_seconds",
        )
        if merged["api_enable_wait_seconds"] < 0:
            raise RuntimeError("api_enable_wait_seconds must be >= 0.")
        merged["api_enable_poll_interval_seconds"] = self._parse_int(
            merged.get("api_enable_poll_interval_seconds"),
            "api_enable_poll_interval_seconds",
        )
        if merged["api_enable_poll_interval_seconds"] <= 0:
            raise RuntimeError("api_enable_poll_interval_seconds must be > 0.")

        if merged["create_subnet_if_missing"] and merged["network_subnet_mode"] != "CUSTOM":
            raise RuntimeError(
                "create_subnet_if_missing=true requires network_subnet_mode=CUSTOM."
            )

        return merged

    def _enable_apis(self, project_context, settings: Mapping[str, Any]) -> None:
        apis: list[str] = []
        if settings["enable_compute_api"]:
            apis.append("compute.googleapis.com")
        if settings["enable_service_networking_api"] or settings["ensure_private_service_access"]:
            apis.append("servicenetworking.googleapis.com")
        if not apis:
            return

        self._run_gcloud(
            project_context,
            ["services", "enable", *apis, "--quiet"],
            f"enable required APIs ({', '.join(apis)})",
        )
        for api in apis:
            self._wait_for_enabled_service(
                project_context=project_context,
                service_name=api,
                timeout_seconds=int(settings["api_enable_wait_seconds"]),
                poll_interval_seconds=int(settings["api_enable_poll_interval_seconds"]),
            )

    def _wait_for_enabled_service(
        self,
        *,
        project_context,
        service_name: str,
        timeout_seconds: int,
        poll_interval_seconds: int,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        last_output = ""
        while True:
            enabled, output = self._is_service_enabled(
                project_context=project_context,
                service_name=service_name,
            )
            if enabled:
                return

            last_output = output
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Timed out waiting for API '{service_name}' to be ENABLED. "
                    f"Last response: {last_output}"
                )

            project_context.log(
                f"vpc-bootstrap: waiting for API '{service_name}' enablement propagation.",
                level="warn",
            )
            time.sleep(poll_interval_seconds)

    def _is_service_enabled(
        self,
        *,
        project_context,
        service_name: str,
    ) -> tuple[bool, str]:
        output, code = self._gcloud_with_project(
            project_context,
            [
                "services",
                "list",
                "--enabled",
                f"--filter=config.name:{service_name}",
                "--format=value(config.name)",
            ],
        )
        enabled_service = output.strip()
        return code == 0 and enabled_service == service_name, output

    def _ensure_network(
        self,
        project_context,
        settings: Mapping[str, Any],
    ) -> Dict[str, Any]:
        network_name = str(settings["network_name"])
        describe_output, describe_code = self._gcloud_with_project(
            project_context,
            [
                "compute",
                "networks",
                "describe",
                network_name,
                "--format=json",
            ],
        )
        if describe_code == 0:
            network = self._parse_json_object(describe_output, "network describe output")
            self._validate_network_shape(network, settings)
            return network

        if not self._is_not_found_error(describe_output):
            raise RuntimeError(
                f"Failed to describe network '{network_name}': {describe_output}"
            )

        if not settings["create_network_if_missing"]:
            raise RuntimeError(
                f"VPC network '{network_name}' does not exist and "
                "create_network_if_missing=false."
            )

        self._run_gcloud(
            project_context,
            [
                "compute",
                "networks",
                "create",
                network_name,
                f"--subnet-mode={str(settings['network_subnet_mode']).lower()}",
                f"--bgp-routing-mode={settings['network_routing_mode']}",
                "--quiet",
            ],
            f"create VPC network '{network_name}'",
        )
        describe_after = self._run_gcloud(
            project_context,
            [
                "compute",
                "networks",
                "describe",
                network_name,
                "--format=json",
            ],
            f"describe network '{network_name}' after creation",
        )
        network = self._parse_json_object(describe_after, "network describe output")
        self._validate_network_shape(network, settings)
        return network

    def _validate_network_shape(
        self,
        network: Mapping[str, Any],
        settings: Mapping[str, Any],
    ) -> None:
        mismatches: list[str] = []
        expected_name = str(settings["network_name"])
        actual_name = str(network.get("name", "")).strip()
        if actual_name and actual_name != expected_name:
            mismatches.append(f"name expected={expected_name} actual={actual_name}")

        expected_subnet_mode = str(settings["network_subnet_mode"]).upper()
        actual_subnet_mode = str(network.get("subnetMode", "")).strip().upper()
        if actual_subnet_mode and actual_subnet_mode != expected_subnet_mode:
            mismatches.append(
                f"subnet_mode expected={expected_subnet_mode} actual={actual_subnet_mode}"
            )

        expected_routing = str(settings["network_routing_mode"]).upper()
        actual_routing = str(network.get("routingConfig", {}).get("routingMode", "")).strip().upper()
        if actual_routing and actual_routing != expected_routing:
            mismatches.append(
                f"routing_mode expected={expected_routing} actual={actual_routing}"
            )

        if mismatches:
            raise RuntimeError(
                "Existing VPC network settings are incompatible with extension config: "
                + "; ".join(mismatches)
            )

    def _ensure_subnet(
        self,
        project_context,
        settings: Mapping[str, Any],
    ) -> Dict[str, Any]:
        subnet_name = str(settings["subnet_name"])
        subnet_region = str(settings["subnet_region"])
        subnet_cidr = str(settings["subnet_cidr"])
        network_name = str(settings["network_name"])

        describe_output, describe_code = self._gcloud_with_project(
            project_context,
            [
                "compute",
                "networks",
                "subnets",
                "describe",
                subnet_name,
                f"--region={subnet_region}",
                "--format=json",
            ],
        )
        if describe_code == 0:
            subnet = self._parse_json_object(describe_output, "subnet describe output")
            self._validate_subnet_shape(subnet, settings)
            return subnet

        if not self._is_not_found_error(describe_output):
            raise RuntimeError(
                f"Failed to describe subnet '{subnet_name}': {describe_output}"
            )

        self._run_gcloud(
            project_context,
            [
                "compute",
                "networks",
                "subnets",
                "create",
                subnet_name,
                f"--region={subnet_region}",
                f"--network={network_name}",
                f"--range={subnet_cidr}",
                "--quiet",
            ],
            f"create subnet '{subnet_name}'",
        )

        describe_after = self._run_gcloud(
            project_context,
            [
                "compute",
                "networks",
                "subnets",
                "describe",
                subnet_name,
                f"--region={subnet_region}",
                "--format=json",
            ],
            f"describe subnet '{subnet_name}' after creation",
        )
        subnet = self._parse_json_object(describe_after, "subnet describe output")
        self._validate_subnet_shape(subnet, settings)
        return subnet

    def _validate_subnet_shape(
        self,
        subnet: Mapping[str, Any],
        settings: Mapping[str, Any],
    ) -> None:
        mismatches: list[str] = []
        expected_name = str(settings["subnet_name"])
        actual_name = str(subnet.get("name", "")).strip()
        if actual_name and actual_name != expected_name:
            mismatches.append(f"name expected={expected_name} actual={actual_name}")

        expected_region = str(settings["subnet_region"])
        actual_region = str(subnet.get("region", "")).strip().split("/")[-1]
        if actual_region and actual_region != expected_region:
            mismatches.append(f"region expected={expected_region} actual={actual_region}")

        expected_cidr = str(settings["subnet_cidr"])
        actual_cidr = str(subnet.get("ipCidrRange", "")).strip()
        if actual_cidr and actual_cidr != expected_cidr:
            mismatches.append(
                f"subnet_cidr expected={expected_cidr} actual={actual_cidr}"
            )

        if not self._network_matches(
            project_id=str(settings["project_id"]),
            network_name=str(settings["network_name"]),
            actual=subnet.get("network"),
        ):
            mismatches.append(
                f"subnet network expected={settings['network_name']} actual={subnet.get('network')!r}"
            )

        if mismatches:
            raise RuntimeError(
                "Existing subnet settings are incompatible with extension config: "
                + "; ".join(mismatches)
            )

    def _ensure_reserved_range(
        self,
        project_context,
        settings: Mapping[str, Any],
    ) -> Dict[str, Any]:
        range_name = str(settings["reserved_range_name"])
        network_name = str(settings["network_name"])
        prefix_length = int(settings["reserved_range_prefix_length"])

        describe_output, describe_code = self._gcloud_with_project(
            project_context,
            [
                "compute",
                "addresses",
                "describe",
                range_name,
                "--global",
                "--format=json",
            ],
        )
        if describe_code == 0:
            address = self._parse_json_object(
                describe_output, "reserved range describe output"
            )
            self._validate_reserved_range_shape(address, settings)
            return address

        if not self._is_not_found_error(describe_output):
            raise RuntimeError(
                f"Failed to describe reserved range '{range_name}': {describe_output}"
            )

        self._run_gcloud(
            project_context,
            [
                "compute",
                "addresses",
                "create",
                range_name,
                "--global",
                "--purpose=VPC_PEERING",
                f"--prefix-length={prefix_length}",
                f"--network={network_name}",
                "--quiet",
            ],
            f"create reserved Service Networking range '{range_name}'",
        )

        describe_after = self._run_gcloud(
            project_context,
            [
                "compute",
                "addresses",
                "describe",
                range_name,
                "--global",
                "--format=json",
            ],
            f"describe reserved range '{range_name}' after creation",
        )
        address = self._parse_json_object(
            describe_after, "reserved range describe output"
        )
        self._validate_reserved_range_shape(address, settings)
        return address

    def _validate_reserved_range_shape(
        self,
        address: Mapping[str, Any],
        settings: Mapping[str, Any],
    ) -> None:
        mismatches: list[str] = []
        expected_name = str(settings["reserved_range_name"])
        actual_name = str(address.get("name", "")).strip()
        if actual_name and actual_name != expected_name:
            mismatches.append(f"name expected={expected_name} actual={actual_name}")

        actual_purpose = str(address.get("purpose", "")).strip().upper()
        if actual_purpose and actual_purpose != "VPC_PEERING":
            mismatches.append(f"purpose expected=VPC_PEERING actual={actual_purpose}")

        expected_prefix = int(settings["reserved_range_prefix_length"])
        actual_prefix_raw = address.get("prefixLength")
        if actual_prefix_raw is not None:
            actual_prefix = self._parse_int(actual_prefix_raw, "prefixLength")
            if actual_prefix != expected_prefix:
                mismatches.append(
                    f"prefix_length expected={expected_prefix} actual={actual_prefix}"
                )

        if not self._network_matches(
            project_id=str(settings["project_id"]),
            network_name=str(settings["network_name"]),
            actual=address.get("network"),
        ):
            mismatches.append(
                f"network expected={settings['network_name']} actual={address.get('network')!r}"
            )

        if mismatches:
            raise RuntimeError(
                "Existing reserved range is incompatible with extension config: "
                + "; ".join(mismatches)
            )

    def _ensure_service_networking_connection(
        self,
        project_context,
        settings: Mapping[str, Any],
    ) -> None:
        network_name = str(settings["network_name"])
        service_name = str(settings["service_networking_service"])
        reserved_range_name = str(settings["reserved_range_name"])

        output = self._run_gcloud(
            project_context,
            [
                "services",
                "vpc-peerings",
                "list",
                f"--network={network_name}",
                f"--service={service_name}",
                "--format=json",
            ],
            f"list Service Networking connections on '{network_name}'",
        )
        connections = self._parse_json_list(output, "vpc-peerings list output")
        connection = self._pick_connection(connections, service_name)

        if connection is None:
            self._run_gcloud(
                project_context,
                [
                    "services",
                    "vpc-peerings",
                    "connect",
                    f"--service={service_name}",
                    f"--network={network_name}",
                    f"--ranges={reserved_range_name}",
                    "--quiet",
                ],
                f"create Service Networking connection for '{network_name}'",
            )
            return

        existing_ranges = self._extract_range_names(connection.get("reservedPeeringRanges"))
        if reserved_range_name in existing_ranges:
            return

        updated_ranges = ",".join(self._dedupe_preserve_order(existing_ranges + [reserved_range_name]))
        self._run_gcloud(
            project_context,
            [
                "services",
                "vpc-peerings",
                "update",
                f"--service={service_name}",
                f"--network={network_name}",
                f"--ranges={updated_ranges}",
                "--quiet",
            ],
            f"update Service Networking connection for '{network_name}'",
        )

    def _pick_connection(
        self,
        connections: list[Any],
        service_name: str,
    ) -> Mapping[str, Any] | None:
        for item in connections:
            if not isinstance(item, Mapping):
                continue
            item_service = str(item.get("service", "")).strip()
            if item_service and item_service != service_name:
                continue
            return item
        return None

    def _extract_range_names(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        names: list[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            trimmed = item.strip()
            if trimmed:
                names.append(trimmed)
        return names

    def _run_gcloud(self, project_context, args: list[str], action: str) -> str:
        output, code = self._gcloud_with_project(project_context, args)
        if code != 0:
            raise RuntimeError(f"Failed to {action}: {output}")
        return output

    def _gcloud_with_project(self, project_context, args: list[str]) -> tuple[str, int]:
        full_args = list(args)
        if not any(arg == "--project" or arg.startswith("--project=") for arg in full_args):
            full_args.extend(["--project", project_context.id])
        return project_context.gcloud(full_args)

    def _parse_json_object(self, output: str, source: str) -> Dict[str, Any]:
        parsed = self._parse_json(output, source)
        if not isinstance(parsed, dict):
            raise RuntimeError(
                f"Expected object JSON in {source}, got {type(parsed).__name__}."
            )
        return parsed

    def _parse_json_list(self, output: str, source: str) -> list[Any]:
        parsed = self._parse_json(output, source)
        if not isinstance(parsed, list):
            raise RuntimeError(
                f"Expected list JSON in {source}, got {type(parsed).__name__}."
            )
        return parsed

    def _parse_json(self, output: str, source: str) -> Any:
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Could not parse JSON from {source}: {exc}") from exc

    def _parse_bool(self, value: Any, field_name: str) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in BOOL_TRUE_VALUES:
                return True
            if normalized in BOOL_FALSE_VALUES:
                return False
        raise RuntimeError(f"Invalid boolean for {field_name}: {value!r}")

    def _parse_int(self, value: Any, field_name: str) -> int:
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid integer for {field_name}: {value!r}") from exc

    def _extract_network_name(self, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise RuntimeError("network must be non-empty.")
        if "/global/networks/" in trimmed:
            return trimmed.rstrip("/").split("/global/networks/")[-1].split("/")[-1]
        if trimmed.startswith("https://"):
            return trimmed.rstrip("/").split("/")[-1]
        return trimmed

    def _network_matches(self, *, project_id: str, network_name: str, actual: Any) -> bool:
        if actual is None:
            return False
        actual_str = str(actual).strip()
        expected_candidates = {
            network_name,
            f"projects/{project_id}/global/networks/{network_name}",
            (
                "https://www.googleapis.com/compute/v1/projects/"
                f"{project_id}/global/networks/{network_name}"
            ),
            f"//compute.googleapis.com/projects/{project_id}/global/networks/{network_name}",
        }
        return actual_str in expected_candidates

    def _dedupe_preserve_order(self, values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result

    def _is_not_found_error(self, output: str) -> bool:
        normalized = output.lower()
        return (
            "not found" in normalized
            or "was not found" in normalized
            or "does not exist" in normalized
            or "404" in normalized
        )
