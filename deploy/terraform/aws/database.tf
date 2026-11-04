# PostgreSQL on RDS, in the private subnets, with pgvector. The memory store needs the `vector`
# extension. Migration 0001 creates it with CREATE EXTENSION, which the master user may run on
# RDS, so nothing has to be enabled here.

locals {
  # "17.5" and "17" both give 17, which names the parameter group family.
  db_major = split(".", var.db_engine_version)[0]
}

# The master password. It never leaves Terraform and Secrets Manager: it is not an output, and
# the only copy besides the state file is inside the database_url secret. Letters and digits
# only, so that it can sit in a URL without escaping.
resource "random_password" "database" {
  length  = 32
  special = false
}

resource "aws_db_subnet_group" "main" {
  name       = var.name
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_parameter_group" "main" {
  name   = "${var.name}-postgres${local.db_major}"
  family = "postgres${local.db_major}"

  # Refuse connections that are not encrypted. The workers use asyncpg, which tries TLS first.
  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "immediate"
  }
}

resource "aws_db_instance" "main" {
  identifier     = "${var.name}-postgres"
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  allocated_storage = var.db_allocated_storage_gb
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = "longhaul"
  username = "longhaul"
  password = random_password.database.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  parameter_group_name   = aws_db_parameter_group.main.name
  vpc_security_group_ids = [aws_security_group.database.id]
  publicly_accessible    = false
  multi_az               = var.db_multi_az

  backup_retention_period    = 7
  copy_tags_to_snapshot      = true
  auto_minor_version_upgrade = true

  # With protect_data on, a destroy takes one last snapshot and the database cannot be deleted
  # until deletion_protection is turned off. With it off, a destroy leaves nothing behind.
  deletion_protection       = var.protect_data
  skip_final_snapshot       = !var.protect_data
  final_snapshot_identifier = var.protect_data ? "${var.name}-postgres-final" : null
}
