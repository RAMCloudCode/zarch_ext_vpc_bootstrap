import pytest

from zarch_ext_vpc_bootstrap.extension import Extension


class DummyContext:
    def __init__(self, project_id="demo-project", region="us-east4"):
        self.id = project_id
        self.region = region

    def log(self, _message: str, level: str | None = None):
        _ = level

    def gcloud(self, _args: list[str]) -> tuple[str, int]:
        return "", 0


def test_resolve_settings_defaults_are_secure():
    ext = Extension()
    settings = ext._resolve_settings(
        {"config": {"network": "projects/demo-project/global/networks/default"}},
        DummyContext(),
    )

    assert settings["network_name"] == "default"
    assert settings["create_network_if_missing"] is False
    assert settings["ensure_private_service_access"] is True
    assert settings["reserved_range_name"] == "google-managed-services-default"


def test_extract_network_name_from_self_link():
    ext = Extension()
    name = ext._extract_network_name(
        "https://www.googleapis.com/compute/v1/projects/demo-project/global/networks/default"
    )
    assert name == "default"


def test_ensure_network_fails_when_missing_and_create_disabled():
    ext = Extension()

    class MissingNetworkContext(DummyContext):
        def gcloud(self, _args: list[str]) -> tuple[str, int]:
            return "ERROR: not found", 1

    with pytest.raises(RuntimeError, match="create_network_if_missing=false"):
        ext._ensure_network(
            MissingNetworkContext(),
            {
                "network_name": "default",
                "create_network_if_missing": False,
                "network_subnet_mode": "CUSTOM",
                "network_routing_mode": "REGIONAL",
            },
        )


def test_ensure_service_networking_connection_connects_when_missing():
    ext = Extension()
    calls = []

    def fake_run_gcloud(_project_context, args, _action):
        calls.append(args)
        if args[:3] == ["services", "vpc-peerings", "list"]:
            return "[]"
        return "{}"

    ext._run_gcloud = fake_run_gcloud
    ext._ensure_service_networking_connection(
        DummyContext(),
        {
            "network_name": "default",
            "service_networking_service": "servicenetworking.googleapis.com",
            "reserved_range_name": "google-managed-services-default",
        },
    )

    connect_call = next(
        args for args in calls if args[:3] == ["services", "vpc-peerings", "connect"]
    )
    assert "--network=default" in connect_call
    assert "--ranges=google-managed-services-default" in connect_call


def test_ensure_service_networking_connection_updates_ranges_when_needed():
    ext = Extension()
    calls = []

    def fake_run_gcloud(_project_context, args, _action):
        calls.append(args)
        if args[:3] == ["services", "vpc-peerings", "list"]:
            return '[{"service":"servicenetworking.googleapis.com","reservedPeeringRanges":["existing-range"]}]'
        return "{}"

    ext._run_gcloud = fake_run_gcloud
    ext._ensure_service_networking_connection(
        DummyContext(),
        {
            "network_name": "default",
            "service_networking_service": "servicenetworking.googleapis.com",
            "reserved_range_name": "google-managed-services-default",
        },
    )

    update_call = next(
        args for args in calls if args[:3] == ["services", "vpc-peerings", "update"]
    )
    assert "--ranges=existing-range,google-managed-services-default" in update_call


def test_validate_reserved_range_shape_rejects_wrong_purpose():
    ext = Extension()
    with pytest.raises(RuntimeError, match="incompatible"):
        ext._validate_reserved_range_shape(
            {
                "name": "google-managed-services-default",
                "purpose": "GCE_ENDPOINT",
                "prefixLength": 24,
                "network": "projects/demo-project/global/networks/default",
            },
            {
                "project_id": "demo-project",
                "reserved_range_name": "google-managed-services-default",
                "reserved_range_prefix_length": 24,
                "network_name": "default",
            },
        )
