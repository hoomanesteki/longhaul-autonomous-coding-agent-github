# Who may talk to whom. Nothing accepts traffic from the internet: no group has an inbound rule
# from 0.0.0.0/0.
#
# The worker task and the EC2 instance it runs on have separate groups, and that is deliberate.
# The sandboxes the worker starts reach the network through the instance, not through the
# task's own interface, so their traffic carries the instance group. Only the task group is
# allowed to reach the database, so repo code running in a sandbox cannot.

resource "aws_security_group" "worker" {
  name        = "${var.name}-worker"
  description = "Worker task: outbound only, and allowed into the database"
  vpc_id      = aws_vpc.main.id
}

resource "aws_security_group" "instances" {
  name        = "${var.name}-worker-hosts"
  description = "EC2 hosts of the workers and the sandboxes on them: outbound only"
  vpc_id      = aws_vpc.main.id
}

resource "aws_security_group" "api" {
  name        = "${var.name}-api"
  description = "API task: reachable from inside the VPC, allowed into the database"
  vpc_id      = aws_vpc.main.id
}

resource "aws_security_group" "poller" {
  name        = "${var.name}-poller"
  description = "Poller task: outbound only"
  vpc_id      = aws_vpc.main.id
}

resource "aws_security_group" "database" {
  name        = "${var.name}-database"
  description = "PostgreSQL: reachable from the worker and API tasks only"
  vpc_id      = aws_vpc.main.id
}

locals {
  # The groups that may open connections to the internet through the NAT gateway.
  outbound_groups = {
    worker    = aws_security_group.worker.id
    instances = aws_security_group.instances.id
    api       = aws_security_group.api.id
    poller    = aws_security_group.poller.id
  }
}

resource "aws_vpc_security_group_egress_rule" "outbound" {
  for_each = local.outbound_groups

  security_group_id = each.value
  description       = "Anywhere, through the NAT gateway"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "database_from_worker" {
  security_group_id            = aws_security_group.database.id
  description                  = "PostgreSQL from the worker tasks"
  referenced_security_group_id = aws_security_group.worker.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_ingress_rule" "database_from_api" {
  security_group_id            = aws_security_group.database.id
  description                  = "PostgreSQL from the API task"
  referenced_security_group_id = aws_security_group.api.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

# No load balancer is built. This rule lets one that you add, or a VPN or a bastion host, reach
# the API from inside the VPC.
resource "aws_vpc_security_group_ingress_rule" "api_from_vpc" {
  security_group_id = aws_security_group.api.id
  description       = "The API port, from inside the VPC"
  cidr_ipv4         = aws_vpc.main.cidr_block
  ip_protocol       = "tcp"
  from_port         = 8000
  to_port           = 8000
}
