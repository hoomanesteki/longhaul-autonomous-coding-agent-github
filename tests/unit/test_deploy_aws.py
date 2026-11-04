"""deploy/terraform/aws: what the story asks for, the pins, and docs/deploy-aws.md.

No test here runs Terraform. `terraform validate` is the CI job `terraform` (see
test_ci_config.py), and plan and apply need an AWS account. These tests read the .tf files as
text. `terraform fmt -check` keeps that safe: it puts the closing brace of every top-level block
in column 0 and indents everything inside, so the first such brace ends the block.
"""

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "terraform" / "aws"
DOC = ROOT / "docs" / "deploy-aws.md"
ADR_PINS = ROOT / "docs" / "adr" / "0002-pin-dependencies.md"

_BLOCK = re.compile(r'^(resource|variable) "(\w+)"(?: "(\w+)")? \{\n(.*?)^\}$', re.M | re.S)
_EXACT = re.compile(r"\d+\.\d+\.\d+")


def tf_files() -> list[Path]:
    files = sorted(DEPLOY.glob("*.tf"))
    assert files, "deploy/terraform/aws has no .tf files"
    return files


def tf_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in tf_files())


def resources(text: str) -> dict[tuple[str, str], str]:
    """The body of each `resource "type" "name"` block, keyed by (type, name)."""
    return {
        (found[2], found[3]): found[4] for found in _BLOCK.finditer(text) if found[1] == "resource"
    }


def variables(text: str) -> dict[str, str]:
    """The body of each `variable "name"` block."""
    return {found[2]: found[4] for found in _BLOCK.finditer(text) if found[1] == "variable"}


def resource_types(text: str) -> set[str]:
    return {kind for kind, _ in resources(text)}


def body(kind: str, name: str) -> str:
    found = resources(tf_text()).get((kind, name))
    assert found is not None, f'the stack has no resource "{kind}" "{name}"'
    return found


def sets(block: str, attribute: str, value: str) -> bool:
    """Whether the block assigns the literal `value` to `attribute`, whatever the alignment."""
    return re.search(rf"^\s*{attribute}\s*=\s*{re.escape(value)}\s*$", block, re.M) is not None


def test_the_block_reader_splits_top_level_blocks() -> None:
    text = (
        'resource "aws_vpc" "main" {\n  tags = {\n    Name = "x"\n  }\n}\n\n'
        'variable "name" {\n  default = "longhaul"\n}\n'
    )
    assert resources(text) == {("aws_vpc", "main"): '  tags = {\n    Name = "x"\n  }\n'}
    assert variables(text) == {"name": '  default = "longhaul"\n'}


# --- the resources the story lists --------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        "aws_vpc",
        "aws_subnet",
        "aws_nat_gateway",
        "aws_db_instance",
        "aws_s3_bucket",
        "aws_s3_bucket_versioning",
        "aws_s3_bucket_server_side_encryption_configuration",
        "aws_ecs_cluster",
        "aws_ecr_repository",
        "aws_secretsmanager_secret",
        "aws_cloudwatch_log_group",
    ],
)
def test_the_stack_declares_the_resources_the_story_lists(kind: str) -> None:
    assert kind in resource_types(tf_text())


def test_there_are_two_private_subnets_and_they_reach_the_internet_through_nat() -> None:
    assert sets(body("aws_subnet", "private"), "count", "2")
    assert sets(body("aws_subnet", "private"), "map_public_ip_on_launch", "false")
    # The NAT gateway needs a subnet with a route to the internet gateway, so one public
    # subnet exists. Nothing but the gateway lives in it.
    private_route = body("aws_route_table", "private")
    assert "nat_gateway_id" in private_route
    assert "aws_nat_gateway.main" in private_route
    assert "subnet_id" in body("aws_nat_gateway", "main")


def test_the_database_is_private_encrypted_postgres() -> None:
    database = body("aws_db_instance", "main")
    assert sets(database, "engine", '"postgres"')
    assert sets(database, "storage_encrypted", "true")
    assert sets(database, "publicly_accessible", "false")
    assert "aws_subnet.private" in body("aws_db_subnet_group", "main")


def test_the_engine_version_default_has_pgvector() -> None:
    # pgvector is part of RDS for PostgreSQL from 15.2. The stack asks for 16 or newer, which
    # carries the 0.8 series that docker-compose.yml runs.
    variable = variables(tf_text())["db_engine_version"]
    default = re.search(r'default\s*=\s*"(\d+)(?:\.\d+)?"', variable)
    assert default is not None
    assert int(default.group(1)) >= 16
    assert "validation" in variable, "an older engine version must be refused"


def test_the_blob_bucket_is_versioned_encrypted_and_private() -> None:
    versioning = body("aws_s3_bucket_versioning", "blobs")
    assert sets(versioning, "status", '"Enabled"')
    encryption = body("aws_s3_bucket_server_side_encryption_configuration", "blobs")
    assert re.search(r'sse_algorithm\s*=\s*"(AES256|aws:kms)"', encryption)
    block = body("aws_s3_bucket_public_access_block", "blobs")
    for setting in (
        "block_public_acls",
        "block_public_policy",
        "ignore_public_acls",
        "restrict_public_buckets",
    ):
        assert sets(block, setting, "true"), setting


def test_the_tokens_and_keys_have_secrets_manager_entries() -> None:
    text = tf_text()
    for name in ("github-token", "anthropic-api-key", "openai-api-key", "admin-token"):
        assert f'"{name}"' in text, f"no secret named {name}"


def test_workers_run_on_ec2_and_the_api_and_poller_on_fargate() -> None:
    # A Fargate task cannot run a Docker daemon or mount its socket, and every worker starts
    # sandboxes. The API and the poller do not, so they stay on Fargate.
    for name, platform in (("worker", "EC2"), ("api", "FARGATE"), ("poller", "FARGATE")):
        task = body("aws_ecs_task_definition", name)
        assert sets(task, "requires_compatibilities", f'["{platform}"]'), name
    worker = body("aws_ecs_task_definition", "worker")
    assert "/var/run/docker.sock" in worker, "the worker needs the host's Docker socket"
    assert "capacity_provider_strategy" in body("aws_ecs_service", "worker")
    assert sets(body("aws_ecs_service", "api"), "launch_type", '"FARGATE"')


def test_sandboxes_cannot_reach_the_instance_metadata_service() -> None:
    # Sandboxes share the host with the worker. Version 2 of the metadata service with a hop
    # limit of 1 keeps a container behind the Docker bridge off it, so repo code cannot read
    # the instance role.
    template = body("aws_launch_template", "worker")
    assert sets(template, "http_tokens", '"required"')
    assert sets(template, "http_put_response_hop_limit", "1")


# --- temporal_mode ------------------------------------------------------------------------


def test_temporal_mode_is_cloud_or_self_hosted_and_cloud_takes_the_certificate_arns() -> None:
    declared = variables(tf_text())
    mode = declared["temporal_mode"]
    assert sets(mode, "default", '"cloud"')
    assert '["cloud", "self_hosted"]' in mode
    for name in (
        "temporal_namespace",
        "temporal_endpoint",
        "temporal_client_cert_secret_arn",
        "temporal_client_key_secret_arn",
    ):
        assert name in declared, f"no variable {name}"
    # Without these two the mTLS handshake with Temporal Cloud cannot work, so the plan
    # fails early when they are empty in cloud mode.
    for name in ("temporal_client_cert_secret_arn", "temporal_client_key_secret_arn"):
        assert "validation" in declared[name]
        assert 'var.temporal_mode != "cloud"' in declared[name]


def test_self_hosted_temporal_is_not_built() -> None:
    # The mode is documented and not built, so no task definition runs a Temporal server.
    tasks = {name for kind, name in resources(tf_text()) if kind == "aws_ecs_task_definition"}
    assert tasks == {"worker", "api", "poller", "migrate"}


# --- the pins -----------------------------------------------------------------------------


def terraform_pins(versions_tf: str) -> dict[str, str]:
    """The constraint on terraform itself and on each provider, keyed by name and source."""
    pins: dict[str, str] = {}
    found = re.search(r'^\s*required_version\s*=\s*"([^"]+)"', versions_tf, re.M)
    if found:
        pins["terraform"] = found.group(1)
    for source, version in re.findall(
        r'source\s*=\s*"([^"]+)"\s*version\s*=\s*"([^"]+)"', versions_tf, re.S
    ):
        pins[source] = version
    return pins


# A required_providers block for the AWS provider, with the version left to fill in.
PROVIDER = (
    "required_providers {{\n"
    '  aws = {{\n    source  = "hashicorp/aws"\n    version = "{}"\n  }}\n'
    "}}\n"
)


def floating_pins(pins: dict[str, str]) -> list[str]:
    """The pins that allow more than one version, which ADR-0002 does not."""
    return [name for name, version in pins.items() if not _EXACT.fullmatch(version)]


def test_terraform_and_every_provider_are_pinned_to_one_version() -> None:
    pins = terraform_pins((DEPLOY / "versions.tf").read_text(encoding="utf-8"))
    assert {"terraform", "hashicorp/aws"} <= set(pins)
    assert floating_pins(pins) == []


@pytest.mark.parametrize(
    "versions_tf",
    [
        'terraform {\n  required_version = "~> 1.17.0"\n}\n',
        'terraform {\n  required_version = ">= 1.17.0"\n}\n',
        'terraform {\n  required_version = "1.17"\n}\n',
        PROVIDER.format("~> 6.0"),
        PROVIDER.format("= 6.71.0"),
    ],
    ids=["tilde", "minimum", "two-parts", "provider-range", "provider-equals-sign"],
)
def test_the_pin_check_notices_a_version_range(versions_tf: str) -> None:
    assert floating_pins(terraform_pins(versions_tf))


def test_the_lock_file_pins_the_provider_versions_of_versions_tf() -> None:
    # `terraform init -lockfile=readonly` in CI fails when the two disagree. This names the
    # drift before CI does, and says which file to fix.
    lock = (DEPLOY / ".terraform.lock.hcl").read_text(encoding="utf-8")
    locked = dict(
        re.findall(r'provider "registry\.terraform\.io/([^"]+)" \{\s*version\s*=\s*"([^"]+)"', lock)
    )
    wanted = {
        source: version
        for source, version in terraform_pins((DEPLOY / "versions.tf").read_text("utf-8")).items()
        if source != "terraform"
    }
    assert locked == wanted


def adr_pin(row_name: str) -> str | None:
    """The text after the first cell of the ADR-0002 table row that starts with row_name."""
    for line in ADR_PINS.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and cells[0] == row_name:
            return cells[1]
    return None


def test_adr_0002_lists_terraform_and_the_providers() -> None:
    pins = terraform_pins((DEPLOY / "versions.tf").read_text(encoding="utf-8"))
    for name, version in pins.items():
        row = "terraform" if name == "terraform" else f"terraform provider {name}"
        listed = adr_pin(row)
        assert listed is not None, f"ADR-0002 has no row `{row}`"
        assert listed.startswith(version), f"ADR-0002 lists {listed} for {name}, not {version}"


# --- git: state holds the database password and the blob keys ------------------------------


@pytest.mark.parametrize(
    ("path", "ignored"),
    [
        ("deploy/terraform/aws/terraform.tfstate", True),
        ("deploy/terraform/aws/terraform.tfstate.backup", True),
        ("deploy/terraform/aws/.terraform/providers/x", True),
        ("deploy/terraform/aws/terraform.tfvars", True),
        ("deploy/terraform/aws/prod.auto.tfvars", True),
        ("deploy/terraform/aws/tfplan", True),
        ("deploy/terraform/aws/.terraform.lock.hcl", False),
        ("deploy/terraform/aws/terraform.tfvars.example", False),
    ],
)
def test_state_and_variable_files_stay_out_of_git(path: str, ignored: bool) -> None:
    result = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT, check=False)
    assert (result.returncode == 0) is ignored, path


# --- docs/deploy-aws.md -------------------------------------------------------------------


def test_the_doc_lists_every_resource_type_the_stack_declares() -> None:
    doc = DOC.read_text(encoding="utf-8")
    missing = sorted(kind for kind in resource_types(tf_text()) if f"`{kind}`" not in doc)
    assert missing == [], f"docs/deploy-aws.md does not list: {', '.join(missing)}"


def test_the_doc_gives_the_apply_and_the_teardown_commands() -> None:
    doc = DOC.read_text(encoding="utf-8")
    assert "terraform apply" in doc
    assert "terraform destroy" in doc


def test_the_doc_gives_a_cost_per_hour() -> None:
    doc = DOC.read_text(encoding="utf-8")
    assert re.search(r"\bper hour\b", doc, re.I)
    assert re.search(r"\$\d+\.\d+", doc)
