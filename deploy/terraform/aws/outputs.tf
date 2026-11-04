locals {
  # The network settings of `aws ecs run-task` for a task that needs the database (migrate) or
  # only the internet (poller).
  database_network = "awsvpcConfiguration={subnets=[${join(",", aws_subnet.private[*].id)}],securityGroups=[${aws_security_group.worker.id}],assignPublicIp=DISABLED}"
  outbound_network = "awsvpcConfiguration={subnets=[${join(",", aws_subnet.private[*].id)}],securityGroups=[${aws_security_group.poller.id}],assignPublicIp=DISABLED}"
}

output "cluster_name" {
  description = "The ECS cluster."
  value       = aws_ecs_cluster.main.name
}

output "ecr_repository_urls" {
  description = "Where to push the worker, api and sandbox images."
  value       = { for name, repository in aws_ecr_repository.images : name => repository.repository_url }
}

output "blob_bucket" {
  description = "The bucket that holds the blobs."
  value       = aws_s3_bucket.blobs.id
}

output "database_endpoint" {
  description = "Host and port of the database. It has no public address."
  value       = aws_db_instance.main.endpoint
}

output "secret_names" {
  description = "The entries that a person fills with aws secretsmanager put-secret-value."
  value       = { for name, secret in aws_secretsmanager_secret.person_supplied : name => secret.name }
}

output "migrate_command" {
  description = "Applies the database migrations. Run it once after the first apply and after each upgrade that adds one."
  value       = "aws ecs run-task --region ${var.aws_region} --cluster ${aws_ecs_cluster.main.name} --launch-type FARGATE --task-definition ${aws_ecs_task_definition.migrate.family} --network-configuration '${local.database_network}'"
}

output "poller_install_command" {
  description = "Installs or updates the Temporal Schedule of the poller. Run it after the workers are up."
  value       = "aws ecs run-task --region ${var.aws_region} --cluster ${aws_ecs_cluster.main.name} --launch-type FARGATE --task-definition ${aws_ecs_task_definition.poller.family} --network-configuration '${local.outbound_network}'"
}

output "client_security_group_ids" {
  description = "The groups of the worker, api and poller tasks. A Temporal server that you run yourself has to accept port 7233 from them."
  value = {
    worker = aws_security_group.worker.id
    api    = aws_security_group.api.id
    poller = aws_security_group.poller.id
  }
}
