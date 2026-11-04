# Longhaul on AWS. Read docs/deploy-aws.md first: it lists every resource, what it costs per hour
# and how to tear it down.
#
# Where things run, and why:
#   Workers   ECS on EC2. A worker starts a Docker sandbox for every task, so it needs a Docker
#             daemon and the daemon's socket. A Fargate task has neither.
#   API       ECS on Fargate. It never starts a container.
#   Poller    A Fargate task that installs the Temporal Schedule. It runs on request, not as a
#             service, because the schedule lives in Temporal and nothing has to stay up for it.
#   Database  RDS PostgreSQL with pgvector, in the private subnets.
#   Blobs     One S3 bucket, versioned and encrypted.

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = "longhaul"
      ManagedBy = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  # Two zones are enough for the database subnet group and keep the stack small.
  azs = slice(data.aws_availability_zones.available.names, 0, 2)

  # The ECR registry host. The sandbox image is pulled from it when an instance boots.
  registry = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com"
}
