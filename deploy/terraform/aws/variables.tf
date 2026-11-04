# Variables with no default have to be set. terraform.tfvars.example shows every one.

# --- where and what to call it -------------------------------------------------------------

variable "aws_region" {
  type        = string
  description = "Region for every resource. S3BlobStore signs its requests for us-east-1, so keep the default until it reads a region setting."
  default     = "us-east-1"
}

variable "name" {
  type        = string
  description = "Prefix of every resource name. It is part of the bucket name, so use lower-case letters, digits and hyphens."
  default     = "longhaul"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,19}$", var.name))
    error_message = "name must start with a letter and hold 2 to 20 lower-case letters, digits or hyphens."
  }
}

variable "vpc_cidr" {
  type        = string
  description = "Address range of the VPC. The subnets are /24 blocks of it, so it must be a /20 or larger."
  default     = "10.40.0.0/16"

  validation {
    condition     = can(cidrsubnet(var.vpc_cidr, 8, 2))
    error_message = "vpc_cidr must be a CIDR block of /24 or larger that can hold three /24 subnets."
  }
}

# --- Temporal ------------------------------------------------------------------------------

variable "temporal_mode" {
  type        = string
  description = "cloud connects to Temporal Cloud with a client certificate. self_hosted connects to a Temporal server that you run yourself. Terraform builds no server in either mode."
  default     = "cloud"

  validation {
    condition     = contains(["cloud", "self_hosted"], var.temporal_mode)
    error_message = "temporal_mode must be cloud or self_hosted."
  }
}

variable "temporal_endpoint" {
  type        = string
  description = "host:port of the Temporal frontend, for example my-namespace.a1b2c.tmprl.cloud:7233."

  validation {
    condition     = can(regex("^[^:/[:space:]]+:[0-9]+$", var.temporal_endpoint))
    error_message = "temporal_endpoint must look like host:port, with no scheme."
  }
}

variable "temporal_namespace" {
  type        = string
  description = "The Temporal namespace. In Temporal Cloud it has the form name.account-id."

  validation {
    condition     = length(var.temporal_namespace) > 0
    error_message = "temporal_namespace must not be empty."
  }
}

variable "temporal_client_cert_secret_arn" {
  type        = string
  description = "ARN of the Secrets Manager secret that holds the PEM client certificate for Temporal Cloud. Needed when temporal_mode is cloud."
  default     = ""

  validation {
    condition     = var.temporal_mode != "cloud" || can(regex("^arn:aws[a-z-]*:secretsmanager:", var.temporal_client_cert_secret_arn))
    error_message = "In cloud mode temporal_client_cert_secret_arn must be the ARN of a Secrets Manager secret."
  }
}

variable "temporal_client_key_secret_arn" {
  type        = string
  description = "ARN of the Secrets Manager secret that holds the PEM private key for the client certificate. Needed when temporal_mode is cloud."
  default     = ""

  validation {
    condition     = var.temporal_mode != "cloud" || can(regex("^arn:aws[a-z-]*:secretsmanager:", var.temporal_client_key_secret_arn))
    error_message = "In cloud mode temporal_client_key_secret_arn must be the ARN of a Secrets Manager secret."
  }
}

# --- the services --------------------------------------------------------------------------

variable "services_enabled" {
  type        = bool
  description = "false builds the network, database, bucket and registries and starts nothing that costs compute. Push the images and fill the secrets, then set it to true."
  default     = false
}

variable "api_enabled" {
  type        = bool
  description = "Whether the API service runs when services_enabled is true. It stays off by default because docker/api/Dockerfile and `longhaul api` come with the console stories."
  default     = false
}

variable "image_tag" {
  type        = string
  description = "Tag of the worker, api and sandbox images in ECR. The repositories refuse to overwrite a tag, so use a version or a commit id."
  default     = ""

  validation {
    condition     = !var.services_enabled || var.image_tag != ""
    error_message = "Set image_tag to the tag you pushed before you set services_enabled to true."
  }
}

variable "llm_provider" {
  type        = string
  description = "Model provider of the workers: anthropic or openai. Only the key of this provider is passed to them."
  default     = "anthropic"

  validation {
    condition     = contains(["anthropic", "openai"], var.llm_provider)
    error_message = "llm_provider must be anthropic or openai."
  }
}

variable "worker_count" {
  type        = number
  description = "Workers. Each one gets an EC2 instance of its own, because the sandboxes it starts share the instance's memory."
  default     = 1

  validation {
    condition     = var.worker_count >= 1 && var.worker_count <= 10 && floor(var.worker_count) == var.worker_count
    error_message = "worker_count must be a whole number from 1 to 10."
  }
}

variable "worker_instance_type" {
  type        = string
  description = "EC2 instance type of a worker. A sandbox may use 4 GiB (sandbox.limits in the config), and the worker takes 2 GiB, so size it for the tasks you run at once."
  default     = "m6i.large"
}

variable "config_path" {
  type        = string
  description = "Path of the Longhaul config file inside the images, for example /app/config/longhaul.yaml. Empty keeps the image's own default, the example config, whose repo list is empty. The repo allowlist lives in this file, so build your own into the image."
  default     = ""
}

variable "api_command" {
  type        = list(string)
  description = "Command of the API container."
  default     = ["longhaul", "api"]
}

variable "poller_command" {
  type        = list(string)
  description = "Command of the poller task. The default installs or updates the Temporal Schedule and exits."
  default     = ["longhaul", "poller", "install"]
}

# --- the database, the bucket and the logs -------------------------------------------------

variable "db_engine_version" {
  type        = string
  description = "PostgreSQL version of RDS, a major version such as 17 or a full one such as 17.5. pgvector ships with RDS PostgreSQL 15.2 and newer. Version 16 or newer matches the pgvector 0.8 series of docker-compose.yml."
  default     = "17"

  validation {
    condition     = can(regex("^(1[6-9]|[2-9][0-9])(\\.[0-9]+)?$", var.db_engine_version))
    error_message = "db_engine_version must be 16 or newer, for example 17 or 17.5."
  }
}

variable "db_instance_class" {
  type        = string
  description = "RDS instance class."
  default     = "db.t4g.micro"
}

variable "db_allocated_storage_gb" {
  type        = number
  description = "Storage of the database in GiB."
  default     = 20
}

variable "db_multi_az" {
  type        = bool
  description = "Keep a standby in the second zone. It doubles the instance cost."
  default     = false
}

variable "protect_data" {
  type        = bool
  description = "false lets terraform destroy remove everything, including every object in the bucket, the images and the database. true keeps a final database snapshot, refuses to delete a non-empty bucket or a non-empty repository, and keeps deleted secrets for 30 days."
  default     = false
}

variable "log_retention_days" {
  type        = number
  description = "How long CloudWatch keeps the logs of each service. One of the values CloudWatch accepts."
  default     = 14

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.log_retention_days)
    error_message = "log_retention_days must be a retention period that CloudWatch Logs accepts."
  }
}
