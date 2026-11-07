# One log group per service, named /<name>/<service>. The migrate group holds the output of the
# one-off migration task.

resource "aws_cloudwatch_log_group" "service" {
  for_each = toset(["worker", "api", "poller", "migrate"])

  name              = "/${var.name}/${each.key}"
  retention_in_days = var.log_retention_days
}
