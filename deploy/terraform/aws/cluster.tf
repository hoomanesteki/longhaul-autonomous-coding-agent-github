# The ECS cluster, and the EC2 capacity that the workers run on.
#
# A worker starts a Docker container for every task's sandbox. It does that through the
# Docker socket of the host it runs on, so the host has to be an EC2 instance that we own.
# Each worker gets an instance of its own (services.tf asks ECS for distinct instances), which
# keeps the memory of the sandboxes apart from the memory of other workers.

resource "aws_ecs_cluster" "main" {
  name = var.name
}

locals {
  # ECS places the tasks. Until the services are enabled no instance runs, and nothing bills.
  worker_instances = var.services_enabled ? var.worker_count : 0

  # The name the config expects (sandbox.image in config/longhaul.example.yaml). The host pulls
  # the sandbox image from ECR under its own name and tags it with this one.
  sandbox_local_tag = "longhaul-sandbox:py312"
  sandbox_image     = "${aws_ecr_repository.images["sandbox"].repository_url}:${var.image_tag}"

  # Where the worker keeps its bare repos (LONGHAUL_CKPT_DIR). The host folder is mounted at
  # the same path in the container, because the worker hands that path to the Docker daemon as
  # the source of a mount in each sandbox (ADR-0006).
  checkpoint_dir = "/var/lib/longhaul/ckpt"
}

# The ECS-optimized Amazon Linux 2023 image that AWS currently recommends. It has Docker and the
# ECS agent installed. An image id is not a secret, so the plain value is used and plans show it.
data "aws_ssm_parameter" "ecs_ami" {
  name = "/aws/service/ecs/optimized-ami/amazon-linux-2023/recommended/image_id"
}

resource "aws_launch_template" "worker" {
  name_prefix   = "${var.name}-worker-"
  image_id      = data.aws_ssm_parameter.ecs_ami.insecure_value
  instance_type = var.worker_instance_type

  iam_instance_profile {
    arn = aws_iam_instance_profile.instance.arn
  }

  vpc_security_group_ids = [aws_security_group.instances.id]

  # Version 2 of the metadata service, with a hop limit of 1. The sandboxes sit behind the
  # Docker bridge, one hop further than the host, so repo code cannot read the instance role.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  # Checkpoints and sandbox volumes live on the root volume.
  block_device_mappings {
    device_name = "/dev/xvda"

    ebs {
      volume_size           = 60
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  # The script joins the cluster, makes the checkpoint folder, and pulls the sandbox image once
  # per boot: the worker only ever asks Docker for the tag in sandbox.image. If the pull fails
  # the host still joins the cluster, and sandboxes fail with "image is missing" in the logs.
  # ECS_AWSVPC_BLOCK_IMDS keeps the containers of awsvpc tasks off the metadata service too.
  user_data = base64encode(<<-EOT
    #!/bin/bash
    set -u
    cat >> /etc/ecs/ecs.config <<'CONFIG'
    ECS_CLUSTER=${aws_ecs_cluster.main.name}
    ECS_AWSVPC_BLOCK_IMDS=true
    CONFIG
    mkdir -p ${local.checkpoint_dir}
    chmod 700 ${local.checkpoint_dir}
    for _ in $(seq 1 60); do
      docker info > /dev/null 2>&1 && break
      sleep 2
    done
    if aws ecr get-login-password --region ${var.aws_region} \
      | docker login --username AWS --password-stdin ${local.registry} \
      && docker pull ${local.sandbox_image}; then
      docker tag ${local.sandbox_image} ${local.sandbox_local_tag}
    else
      echo "could not pull ${local.sandbox_image}: sandboxes will not start" >&2
    fi
  EOT
  )

  tag_specifications {
    resource_type = "instance"

    tags = {
      Name = "${var.name}-worker"
    }
  }

  tag_specifications {
    resource_type = "volume"

    tags = {
      Name = "${var.name}-worker"
    }
  }
}

resource "aws_autoscaling_group" "worker" {
  name                  = "${var.name}-worker"
  min_size              = local.worker_instances
  max_size              = local.worker_instances
  desired_capacity      = local.worker_instances
  vpc_zone_identifier   = aws_subnet.private[*].id
  protect_from_scale_in = false

  # A destroy must not wait for instances to drain on their own.
  force_delete = true

  launch_template {
    id      = aws_launch_template.worker.id
    version = aws_launch_template.worker.latest_version
  }

  tag {
    key                 = "AmazonECSManaged"
    value               = "true"
    propagate_at_launch = true
  }
}

resource "aws_ecs_capacity_provider" "worker" {
  name = "${var.name}-worker"

  auto_scaling_group_provider {
    auto_scaling_group_arn         = aws_autoscaling_group.worker.arn
    managed_termination_protection = "DISABLED"

    # The size is worker_count and nothing else. A scaling rule would add instances that
    # nobody asked for.
    managed_scaling {
      status = "DISABLED"
    }
  }
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = [aws_ecs_capacity_provider.worker.name, "FARGATE"]
}
