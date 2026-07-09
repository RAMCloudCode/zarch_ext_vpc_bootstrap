# Z-Arch Extension: vpc-bootstrap

`vpc-bootstrap` is a project-level extension that bootstraps VPC prerequisites
for private Google-managed services (including Cloud SQL private IP / Private
Service Access) with secure defaults.

## What It Does
- Enables required APIs:
  - `compute.googleapis.com`
  - `servicenetworking.googleapis.com`
- Ensures the configured VPC network exists.
- Optionally ensures a subnet exists (for Direct VPC runtime use cases).
- Ensures a reserved peering range exists (`purpose=VPC_PEERING`).
- Ensures Service Networking connection exists and includes the configured range.
- Runs idempotently (safe to rerun).

## Hook
- `async post_project_bootstrap`

## Secure Defaults
- `create_network_if_missing: false`
- `create_subnet_if_missing: false`
- `ensure_private_service_access: true`

This means the extension will not silently create broad network topology, but
it will enforce private service networking by default.

## zarch.yaml Example
```yaml
extensions:
  vpc-bootstrap:
    type: "vpc-bootstrap"
    required_roles:
      - "roles/compute.networkAdmin"
      - "roles/servicenetworking.networksAdmin"
      - "roles/serviceusage.serviceUsageAdmin"
    config:
      network: "projects/my-project/global/networks/default"
      create_network_if_missing: false
      create_subnet_if_missing: false
      ensure_private_service_access: true
      reserved_range_name: "google-managed-services-default"
      reserved_range_prefix_length: 24
      service_networking_service: "servicenetworking.googleapis.com"
```

## Config Reference
| Key | Type | Default | Notes |
|---|---|---|---|
| `network` | string | `default` | VPC name or self-link (`projects/<id>/global/networks/<name>`). |
| `create_network_if_missing` | boolean | `false` | If true, creates the VPC when absent. |
| `network_subnet_mode` | string | `CUSTOM` | `CUSTOM` or `AUTO`. |
| `network_routing_mode` | string | `REGIONAL` | `REGIONAL` or `GLOBAL`. |
| `create_subnet_if_missing` | boolean | `false` | If true, creates subnet when absent. |
| `subnet_name` | string | `<network>-<region>` | Used only when `create_subnet_if_missing=true`. |
| `subnet_region` | string | current deployment region | Used only when `create_subnet_if_missing=true`. |
| `subnet_cidr` | string | `10.10.0.0/24` | Used only when `create_subnet_if_missing=true`. |
| `ensure_private_service_access` | boolean | `true` | Enables reserved range + Service Networking connection reconciliation. |
| `reserved_range_name` | string | `google-managed-services-<network>` | Reserved range resource name. |
| `reserved_range_prefix_length` | integer | `24` | CIDR prefix for reserved peering range (`16..29`). |
| `service_networking_service` | string | `servicenetworking.googleapis.com` | Service Networking service endpoint. |
| `enable_compute_api` | boolean | `true` | Enables Compute API if needed. |
| `enable_service_networking_api` | boolean | `true` | Enables Service Networking API if needed. |
| `api_enable_wait_seconds` | integer | `180` | API enable propagation timeout. |
| `api_enable_poll_interval_seconds` | integer | `5` | Poll interval for API status checks. |

## Install (MCP workflow)
After adding extension config to `zarch.yaml`, install with MCP:
- `install_extension` source: `./extensions/zarch_ext_vpc_bootstrap`
