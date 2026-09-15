# The pending queue and its dead-letter queue.
#
# Standard, not FIFO. FIFO's ordering guarantee is irrelevant when the scheduler
# reorders anyway -- ordering is an explicit policy object in scheduler/ordering.py
# -- and FIFO's throughput cap would be a constraint bought for nothing. Standard
# is also cheaper ($0.40 vs $0.50 per million requests).

resource "aws_sqs_queue" "jobs_dlq" {
  name                      = "nimbus-${var.environment}-jobs-dlq-${random_id.suffix.hex}"
  message_retention_seconds = 1209600 # 14 days, the maximum

  # A dead letter is evidence. Retaining it for the full fortnight costs
  # nothing at this volume and means a poison message found on a Friday is
  # still there to look at.

  sqs_managed_sse_enabled = true

  tags = { Name = "nimbus-${var.environment}-jobs-dlq" }
}

resource "aws_sqs_queue" "jobs" {
  name = "nimbus-${var.environment}-jobs-${random_id.suffix.hex}"

  # Matches NIMBUS_VISIBILITY_TIMEOUT_S. The scheduler extends visibility on a
  # timer for the whole job lifetime, so this is not a job-duration limit -- but
  # it *is* the recovery bound for an orphaned job, whose message cannot be made
  # visible again without its receipt.
  visibility_timeout_seconds = var.visibility_timeout_s

  message_retention_seconds = 345600 # 4 days
  receive_wait_time_seconds = 10     # long polling: no billed empty receives

  sqs_managed_sse_enabled = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.jobs_dlq.arn

    # max_attempts + 2, the same expression as backend/services/factory.py.
    #
    # SQS counts *deliveries*, not *attempts*. A delivery that fails to place
    # and is deferred still increments ApproximateReceiveCount, and there is no
    # API to decrement it. If this equalled the retry budget, a burst that
    # momentarily exceeded fleet capacity would walk healthy jobs into the DLQ
    # -- measured in Phase 1, where a 24-job batch produced 65 deliveries and
    # dead-lettered 5 good jobs. Job.attempts in DynamoDB is the authoritative
    # retry counter; this is only the poison-message backstop.
    maxReceiveCount = var.max_attempts + 2
  })

  tags = { Name = "nimbus-${var.environment}-jobs" }
}

resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  queue_url = aws_sqs_queue.jobs_dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.jobs.arn]
  })
}

# --------------------------------------------------------------------------- #
# Interface endpoint for SQS.
#
# Unlike S3 and DynamoDB, SQS has no free gateway endpoint. This one costs
# ~$0.01/hour per AZ plus data processing. It is here rather than omitted
# because the alternative -- control-plane traffic to SQS over the internet
# gateway -- means the queue is reachable from a host that also has a public
# IP, and the control/data-plane split is meant to be enforced by the
# infrastructure rather than by hoping.
# --------------------------------------------------------------------------- #
resource "aws_security_group" "vpce" {
  name        = "nimbus-${var.environment}-vpce"
  description = "Interface endpoint. HTTPS from the control plane only."
  vpc_id      = aws_vpc.nimbus.id

  tags = { Name = "nimbus-${var.environment}-vpce" }
}

resource "aws_vpc_security_group_ingress_rule" "vpce_https" {
  security_group_id            = aws_security_group.vpce.id
  referenced_security_group_id = aws_security_group.control_plane.id
  from_port                    = 443
  to_port                      = 443
  ip_protocol                  = "tcp"
}

resource "aws_vpc_endpoint" "sqs" {
  vpc_id              = aws_vpc.nimbus.id
  service_name        = "com.amazonaws.${var.region}.sqs"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.public.id]
  security_group_ids  = [aws_security_group.vpce.id]
  private_dns_enabled = true

  tags = { Name = "nimbus-${var.environment}-sqs" }
}
