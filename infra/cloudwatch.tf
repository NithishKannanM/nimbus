# Logs, metrics and alarms.
#
# Retention is 7 days everywhere. Logs are for debugging a run that is happening
# now; the durable record of a run is its S3 archive (manifest, raw.jsonl,
# decisions.jsonl, telemetry.jsonl), which is what the analysis reads. Paying to
# keep application logs for a month would buy nothing the archive does not
# already have.

resource "aws_cloudwatch_log_group" "control_plane" {
  name              = "/nimbus/${var.environment}/control-plane"
  retention_in_days = var.log_retention_days

  tags = { Name = "nimbus-${var.environment}-control-plane" }
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/nimbus/${var.environment}/worker"
  retention_in_days = var.log_retention_days

  tags = { Name = "nimbus-${var.environment}-worker" }
}

# --------------------------------------------------------------------------- #
# Alarms.
#
# Each of these corresponds to a failure this project has actually had, or to a
# cost control the budget depends on. There is no alarm here for "CPU is high" --
# high CPU is the workload doing its job, and an alarm that fires during every
# benchmark run is an alarm nobody reads.
# --------------------------------------------------------------------------- #

# The one that matters most. A message in the DLQ means a job was given up on,
# and in a scheduling experiment a silently dropped job does not produce an
# error -- it produces a slightly wrong number. Phase 3 lost three 60-job runs
# to exactly that, and the runs still reported a makespan.
resource "aws_cloudwatch_metric_alarm" "dead_letters" {
  alarm_name          = "nimbus-${var.environment}-dead-letters"
  alarm_description   = "A job was dead-lettered. Any result computed during this window is suspect."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = { QueueName = aws_sqs_queue.jobs_dlq.name }

  tags = { Name = "nimbus-${var.environment}-dead-letters" }
}

# A message that has been sitting visible for minutes means the scheduler is not
# consuming -- it has died, or it is wedged. The scheduler is the sole consumer
# of this queue by design, so there is no second consumer to take over.
resource "aws_cloudwatch_metric_alarm" "queue_stalled" {
  alarm_name          = "nimbus-${var.environment}-queue-stalled"
  alarm_description   = "Oldest message > 5 min: the sole consumer is not consuming."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  threshold           = 300
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = { QueueName = aws_sqs_queue.jobs.name }

  tags = { Name = "nimbus-${var.environment}-queue-stalled" }
}

# t3 burstable credits are a measurement hazard, not just a performance one
# (docs/experiments.md section 8). A run that exhausts credits is measuring the
# credit system rather than the scheduler, and must be discarded and re-run on
# c6i.large. This alarm is how you find out that happened.
resource "aws_cloudwatch_metric_alarm" "cpu_credits" {
  count = startswith(var.instance_type, "t3.") || startswith(var.instance_type, "t4g.") ? 1 : 0

  alarm_name          = "nimbus-${var.environment}-cpu-credits-low"
  alarm_description   = "Burst credits low. Any headline run overlapping this window is invalid."
  namespace           = "AWS/EC2"
  metric_name         = "CPUCreditBalance"
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 30
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "notBreaching"

  tags = { Name = "nimbus-${var.environment}-cpu-credits-low" }
}

# DynamoDB is on-demand, so throttling here means a hot partition rather than
# under-provisioning -- most likely the decision index, if the day-partitioned
# key design were ever changed back to a constant.
resource "aws_cloudwatch_metric_alarm" "dynamodb_throttles" {
  alarm_name          = "nimbus-${var.environment}-dynamodb-throttled"
  alarm_description   = "On-demand table throttling: a hot partition, not capacity."
  namespace           = "AWS/DynamoDB"
  metric_name         = "ThrottledRequests"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = { TableName = aws_dynamodb_table.state.name }

  tags = { Name = "nimbus-${var.environment}-dynamodb-throttled" }
}

# --------------------------------------------------------------------------- #
# Budget. Non-negotiable per docs/cost-model.md section 4.
# --------------------------------------------------------------------------- #
resource "aws_budgets_budget" "nimbus" {
  count = var.budget_notification_email == "" ? 0 : 1

  name         = "nimbus-${var.environment}"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "TagKeyValue"
    values = ["user:Project$nimbus"]
  }

  # Two thresholds: one on actual spend, one on the forecast. The forecast is
  # the useful one -- it fires while there is still time to shut something
  # down, rather than after the money is gone.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_notification_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_notification_email]
  }
}
