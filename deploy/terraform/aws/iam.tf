# Two roles, and no role for the application code.
#
#   task execution   Used by ECS itself to pull images, write logs and read the secrets that a
#                    task names. It is not available to the code in the container.
#   instance         Used by the ECS agent on a worker host to join the cluster and pull images.
#
# The containers get no task role, so nothing they run holds AWS credentials except the blob
# user's key pair, which can touch one bucket (secrets.tf).

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

locals {
  # Every secret a task can name. The certificate entries exist only in cloud mode, and the
  # empty strings of the other mode are dropped.
  readable_secret_arns = compact(concat(
    [for secret in aws_secretsmanager_secret.person_supplied : secret.arn],
    [
      aws_secretsmanager_secret.database_url.arn,
      aws_secretsmanager_secret.blob_credentials.arn,
    ],
    var.temporal_mode == "cloud" ? [
      var.temporal_client_cert_secret_arn,
      var.temporal_client_key_secret_arn,
    ] : [],
  ))
}

resource "aws_iam_role" "task_execution" {
  name               = "${var.name}-task-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "task_execution" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "read_secrets" {
  statement {
    sid       = "ReadTheSecretsOfTheTasks"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = local.readable_secret_arns
  }
}

resource "aws_iam_role_policy" "task_execution_secrets" {
  name   = "read-secrets"
  role   = aws_iam_role.task_execution.id
  policy = data.aws_iam_policy_document.read_secrets.json
}

resource "aws_iam_role" "instance" {
  name               = "${var.name}-worker-host"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "instance_ecs" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

# Session Manager is the way into a host: there is no SSH key and no inbound port.
resource "aws_iam_role_policy_attachment" "instance_ssm" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "instance" {
  name = "${var.name}-worker-host"
  role = aws_iam_role.instance.name
}
