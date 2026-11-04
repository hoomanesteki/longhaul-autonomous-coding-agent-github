# The task definitions and services.
#
#   worker   ECS on EC2, one task per instance. It mounts the host's Docker socket.
#   api      ECS on Fargate, behind no load balancer (see security_groups.tf).
#   poller   A Fargate task definition that is run on request: it installs or updates the
#            Temporal Schedule `longhaul-poller` and exits.
#   migrate  A Fargate task definition that is run on request: it applies the database
#            migrations and exits.
#
# The settings are the LONGHAUL_* variables of src/longhaul/settings.py. Credentials reach the
# containers as ECS secrets, so they are not in the task definition and not in its history.

locals {
  # The tag is empty while the services are off, and a task definition needs something there.
  image_tag    = var.image_tag != "" ? var.image_tag : "not-pushed-yet"
  worker_image = "${aws_ecr_repository.images["worker"].repository_url}:${local.image_tag}"
  api_image    = "${aws_ecr_repository.images["api"].repository_url}:${local.image_tag}"

  # The workers are counted in cluster.tf, one per instance. The API has a switch of its own.
  api_count = var.services_enabled && var.api_enabled ? 1 : 0

  # Set for every container. LONGHAUL_CONFIG is left out when empty, so the image's default stays.
  shared_environment = concat(
    [
      { name = "LONGHAUL_TEMPORAL_ADDRESS", value = var.temporal_endpoint },
      { name = "LONGHAUL_TEMPORAL_NAMESPACE", value = var.temporal_namespace },
    ],
    var.config_path != "" ? [{ name = "LONGHAUL_CONFIG", value = var.config_path }] : [],
  )

  # The client certificate and key of Temporal Cloud, as PEM text. The Python client does not
  # read them yet (docs/deploy-aws.md, "What the application does not do yet"), and the
  # settings ignore variables they do not know, so passing them is safe.
  temporal_secrets = var.temporal_mode == "cloud" ? [
    { name = "LONGHAUL_TEMPORAL_TLS_CERT", valueFrom = var.temporal_client_cert_secret_arn },
    { name = "LONGHAUL_TEMPORAL_TLS_KEY", valueFrom = var.temporal_client_key_secret_arn },
  ] : []

  blob_environment = [
    { name = "LONGHAUL_BLOB_ENDPOINT", value = "https://s3.${var.aws_region}.amazonaws.com" },
    { name = "LONGHAUL_BLOB_BUCKET", value = aws_s3_bucket.blobs.id },
  ]

  # "arn:key::" makes ECS pass one field of a JSON secret.
  blob_secrets = [
    { name = "LONGHAUL_BLOB_ACCESS_KEY", valueFrom = "${aws_secretsmanager_secret.blob_credentials.arn}:access_key::" },
    { name = "LONGHAUL_BLOB_SECRET_KEY", valueFrom = "${aws_secretsmanager_secret.blob_credentials.arn}:secret_key::" },
  ]

  database_secrets = [
    { name = "LONGHAUL_DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
  ]

  # Only the key of the chosen provider is passed. A task cannot start while a secret it names
  # has no value, and nobody fills the key of a provider they do not use.
  model_key_name = var.llm_provider == "anthropic" ? "ANTHROPIC_API_KEY" : "OPENAI_API_KEY"
  model_key_arn = (
    var.llm_provider == "anthropic"
    ? aws_secretsmanager_secret.person_supplied["anthropic-api-key"].arn
    : aws_secretsmanager_secret.person_supplied["openai-api-key"].arn
  )

  # Alembic reads LONGHAUL_DATABASE_URL. The Config has no alembic.ini because the image does not
  # carry one, so the script folder is taken from the installed package.
  migrate_command = [
    "python",
    "-c",
    join("; ", [
      "import importlib.resources as r",
      "from alembic import command",
      "from alembic.config import Config",
      "cfg = Config()",
      "cfg.set_main_option('script_location', str(r.files('longhaul') / 'db' / 'migrations'))",
      "command.upgrade(cfg, 'head')",
    ]),
  ]

  # The same wiring for every container: where its logs go.
  log_options = { for name in ["worker", "api", "poller", "migrate"] : name => {
    "awslogs-group"         = aws_cloudwatch_log_group.service[name].name
    "awslogs-region"        = var.aws_region
    "awslogs-stream-prefix" = name
  } }
}

# --- worker ---------------------------------------------------------------------------------

resource "aws_ecs_task_definition" "worker" {
  family                   = "${var.name}-worker"
  requires_compatibilities = ["EC2"]
  network_mode             = "awsvpc"
  cpu                      = 1024
  memory                   = 2048
  execution_role_arn       = aws_iam_role.task_execution.arn

  # Both come from the host. The socket gives the worker the host's Docker daemon, which is the
  # same as root on the host: read "The worker in Docker" in docs/runbook.md.
  volume {
    name      = "docker-socket"
    host_path = "/var/run/docker.sock"
  }

  volume {
    name      = "checkpoints"
    host_path = local.checkpoint_dir
  }

  container_definitions = jsonencode([{
    name      = "worker"
    image     = local.worker_image
    essential = true

    # The image starts `longhaul worker`. Longer than the 30 seconds that the worker waits for
    # its running activities when it gets SIGTERM, as in docker-compose.yml.
    stopTimeout = 60

    environment = concat(local.shared_environment, local.blob_environment, [
      { name = "LONGHAUL_PROVIDER", value = var.llm_provider },
      { name = "LONGHAUL_CKPT_DIR", value = local.checkpoint_dir },
    ])

    secrets = concat(local.database_secrets, local.blob_secrets, local.temporal_secrets, [
      { name = "LONGHAUL_GITHUB_TOKEN", valueFrom = aws_secretsmanager_secret.person_supplied["github-token"].arn },
      { name = local.model_key_name, valueFrom = local.model_key_arn },
    ])

    mountPoints = [
      { sourceVolume = "docker-socket", containerPath = "/var/run/docker.sock" },
      { sourceVolume = "checkpoints", containerPath = local.checkpoint_dir },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options   = local.log_options["worker"]
    }
  }])
}

resource "aws_ecs_service" "worker" {
  name            = "worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = local.worker_instances

  capacity_provider_strategy {
    capacity_provider = aws_ecs_capacity_provider.worker.name
    weight            = 1
  }

  # One worker per instance, so the sandboxes of two workers never compete for one host's memory
  # and two workers never share a checkpoint folder.
  placement_constraints {
    type = "distinctInstance"
  }

  # With one instance per worker, a new task cannot start until the old one has stopped. A task
  # that stops cleanly hands its activities back to Temporal (runbook, "Stopping a worker").
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.worker.id]
    assign_public_ip = false
  }

  # A task that starts before the role may read its secrets, or before the secrets have a value,
  # stops at once and waits for ECS to try again.
  depends_on = [
    aws_ecs_cluster_capacity_providers.main,
    aws_iam_role_policy.task_execution_secrets,
    aws_iam_role_policy_attachment.task_execution,
    aws_secretsmanager_secret_version.database_url,
    aws_secretsmanager_secret_version.blob_credentials,
  ]
}

# --- api ------------------------------------------------------------------------------------

resource "aws_ecs_task_definition" "api" {
  family                   = "${var.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.task_execution.arn

  container_definitions = jsonencode([{
    name      = "api"
    image     = local.api_image
    essential = true
    command   = var.api_command

    portMappings = [{ containerPort = 8000, protocol = "tcp" }]

    environment = concat(local.shared_environment, local.blob_environment)

    secrets = concat(local.database_secrets, local.blob_secrets, local.temporal_secrets, [
      { name = "LONGHAUL_ADMIN_TOKEN", valueFrom = aws_secretsmanager_secret.person_supplied["admin-token"].arn },
    ])

    logConfiguration = {
      logDriver = "awslogs"
      options   = local.log_options["api"]
    }
  }])
}

resource "aws_ecs_service" "api" {
  name            = "api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = local.api_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.api.id]
    assign_public_ip = false
  }

  # A task that starts before the role may read its secrets, or before the secrets have a value,
  # stops at once and waits for ECS to try again.
  depends_on = [
    aws_ecs_cluster_capacity_providers.main,
    aws_iam_role_policy.task_execution_secrets,
    aws_iam_role_policy_attachment.task_execution,
    aws_secretsmanager_secret_version.database_url,
    aws_secretsmanager_secret_version.blob_credentials,
  ]
}

# --- poller and migrate: run on request ------------------------------------------------------

resource "aws_ecs_task_definition" "poller" {
  family                   = "${var.name}-poller"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.task_execution.arn

  container_definitions = jsonencode([{
    name        = "poller"
    image       = local.worker_image
    essential   = true
    command     = var.poller_command
    environment = local.shared_environment
    secrets     = local.temporal_secrets

    logConfiguration = {
      logDriver = "awslogs"
      options   = local.log_options["poller"]
    }
  }])
}

resource "aws_ecs_task_definition" "migrate" {
  family                   = "${var.name}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.task_execution.arn

  container_definitions = jsonencode([{
    name      = "migrate"
    image     = local.worker_image
    essential = true
    command   = local.migrate_command
    secrets   = local.database_secrets

    logConfiguration = {
      logDriver = "awslogs"
      options   = local.log_options["migrate"]
    }
  }])
}
