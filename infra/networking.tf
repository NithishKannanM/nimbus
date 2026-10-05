# Network. Single AZ, no NAT gateway.
#
# Both of those are cost decisions with consequences, recorded here rather than
# discovered later:
#
# - **No NAT gateway.** At $0.045/hour plus $0.045/GB it would be roughly 55%
#   of the entire project budget (docs/cost-model.md section 4). Instead the
#   instances sit in a public subnet with no inbound access, and reach AWS
#   services through VPC endpoints. S3 and DynamoDB get gateway endpoints,
#   which are free; SQS needs an interface endpoint, which is not, so the
#   control plane keeps a public IP and egresses through the internet gateway
#   for that one service. That is the cheap option, and it is why the security
#   group below has to be exactly right.
#
# - **Single AZ.** Multi-AZ would double instance-hours and buy nothing for a
#   single-region experiment whose fleet size is a fixed control. It is an
#   explicit availability trade, recorded in docs/limitations.md.

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "nimbus" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "nimbus-${var.environment}" }
}

resource "aws_internet_gateway" "nimbus" {
  vpc_id = aws_vpc.nimbus.id
  tags   = { Name = "nimbus-${var.environment}" }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.nimbus.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, 0)
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true

  tags = { Name = "nimbus-${var.environment}-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.nimbus.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.nimbus.id
  }

  tags = { Name = "nimbus-${var.environment}-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# --------------------------------------------------------------------------- #
# VPC endpoints -- free for S3 and DynamoDB, and they keep artifact and state
# traffic off the public internet entirely.
# --------------------------------------------------------------------------- #
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.nimbus.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id]

  tags = { Name = "nimbus-${var.environment}-s3" }
}

resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = aws_vpc.nimbus.id
  service_name      = "com.amazonaws.${var.region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id]

  tags = { Name = "nimbus-${var.environment}-dynamodb" }
}

# --------------------------------------------------------------------------- #
# Security groups. This is where the control/data-plane split becomes a network
# property as well as an IAM one.
# --------------------------------------------------------------------------- #
resource "aws_security_group" "control_plane" {
  name        = "nimbus-${var.environment}-control-plane"
  description = "API and scheduler. Reachable from admin_cidr and, for result callbacks, the workers."
  vpc_id      = aws_vpc.nimbus.id

  tags = { Name = "nimbus-${var.environment}-control-plane" }
}

resource "aws_security_group" "worker" {
  name        = "nimbus-${var.environment}-worker"
  description = "Workers. Reachable only from the control plane. No public ingress."
  vpc_id      = aws_vpc.nimbus.id

  tags = { Name = "nimbus-${var.environment}-worker" }
}

# The API has no authentication before Phase 7, so this rule is the entire
# access control story. var.admin_cidr has no default and rejects 0.0.0.0/0.
resource "aws_vpc_security_group_ingress_rule" "control_plane_api" {
  security_group_id = aws_security_group.control_plane.id
  description       = "Nimbus REST API"
  cidr_ipv4         = var.admin_cidr
  from_port         = 8000
  to_port           = 8000
  ip_protocol       = "tcp"
}

# Workers report results back by POSTing to /internal/results. Only the worker
# security group may do so, which is why that route needs no other auth yet.
resource "aws_vpc_security_group_ingress_rule" "control_plane_results" {
  security_group_id            = aws_security_group.control_plane.id
  description                  = "Result callbacks from workers"
  referenced_security_group_id = aws_security_group.worker.id
  from_port                    = 8000
  to_port                      = 8000
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "control_plane_ssh" {
  count = var.key_name == "" ? 0 : 1

  security_group_id = aws_security_group.control_plane.id
  description       = "SSH. Prefer SSM Session Manager and leave key_name empty."
  cidr_ipv4         = var.admin_cidr
  from_port         = 22
  to_port           = 22
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "control_plane_all" {
  security_group_id = aws_security_group.control_plane.id
  description       = "Outbound to AWS services and the worker fleet"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# Dispatch is a push: the scheduler POSTs to a worker's /execute. Nothing else
# may reach a worker, from anywhere.
resource "aws_vpc_security_group_ingress_rule" "worker_dispatch" {
  security_group_id            = aws_security_group.worker.id
  description                  = "Push dispatch from the scheduler only"
  referenced_security_group_id = aws_security_group.control_plane.id
  from_port                    = 8080
  to_port                      = 8080
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "worker_all" {
  security_group_id = aws_security_group.worker.id
  description       = "Outbound to S3 and CloudWatch. No SQS, no DynamoDB (IAM)."
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}
