# Three image repositories, one per image that docs/deploy-aws.md has you build:
#   worker   docker/worker/Dockerfile. The worker, and the poller and migration tasks.
#   api      docker/api/Dockerfile. It comes with the console stories.
#   sandbox  docker/sandbox/Dockerfile. Each worker host pulls it when it boots, because the
#            worker starts the sandboxes through the host's Docker daemon.
#
# Tags are immutable: a task definition that names a tag always starts the same bytes.

resource "aws_ecr_repository" "images" {
  for_each = toset(["worker", "api", "sandbox"])

  name                 = "${var.name}/${each.key}"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = !var.protect_data

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "images" {
  for_each   = aws_ecr_repository.images
  repository = each.value.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 20 newest images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 20
      }
      action = { type = "expire" }
    }]
  })
}
