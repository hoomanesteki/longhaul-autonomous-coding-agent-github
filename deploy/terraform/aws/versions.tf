# Every version is exact, as in ADR-0002. A range would let a plan on one machine differ from the
# same plan on another. .terraform.lock.hcl pins the provider builds by checksum, and the
# `terraform` job in .github/workflows/ci.yml installs the same Terraform version as below.
# tests/unit/test_deploy_aws.py fails when the three disagree or a range appears.
terraform {
  required_version = "1.17.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.72.0"
    }
    # The database password. It is generated here so that it never has to be typed or pasted.
    random = {
      source  = "hashicorp/random"
      version = "3.9.1"
    }
  }
}
