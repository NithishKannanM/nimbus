# Single-table control-plane state, with the two GSIs the access patterns
# require (docs/architecture.md section 7.1).
#
# The access patterns were enumerated before the keys were chosen, and the
# result is that no pattern the system actually issues needs a table scan.
# A3 ("list jobs by status, newest first") and A7 ("recent decisions across all
# jobs") are the two that would tempt one; each gets an index below.

resource "aws_dynamodb_table" "state" {
  name = "nimbus-${var.environment}-state-${random_id.suffix.hex}"

  # On-demand. The load here is bursty by construction -- a benchmark run
  # writes thousands of items in a minute and then nothing for hours -- which
  # is the shape provisioned capacity is worst at, and idle provisioned
  # capacity would be billed between runs for no benefit.
  billing_mode = "PAY_PER_REQUEST"

  hash_key  = "PK"
  range_key = "SK"

  attribute {
    name = "PK"
    type = "S"
  }
  attribute {
    name = "SK"
    type = "S"
  }
  attribute {
    name = "GSI1PK"
    type = "S"
  }
  attribute {
    name = "GSI1SK"
    type = "S"
  }
  attribute {
    name = "GSI2PK"
    type = "S"
  }
  attribute {
    name = "GSI2SK"
    type = "S"
  }

  # A3: STATUS#{status} / {created_at}#{job_id}, queried descending with a
  # Limit. The dashboard's job list and the reaper's live-job walk both use it.
  global_secondary_index {
    name            = "status-index"
    hash_key        = "GSI1PK"
    range_key       = "GSI1SK"
    projection_type = "ALL"
  }

  # A7: DECISIONS#{yyyy-mm-dd} / {decided_at}#{job_id}.
  #
  # Partitioned by day rather than by a constant. A constant partition key
  # would send every placement decision in the project's lifetime to one
  # partition -- the textbook hot-key mistake, and one that would throttle the
  # scheduler's own write path and so corrupt the latency metric it is
  # supposed to be recording.
  global_secondary_index {
    name            = "decision-index"
    hash_key        = "GSI2PK"
    range_key       = "GSI2SK"
    projection_type = "ALL"
  }

  # Telemetry samples carry a 24-hour TTL. They are a high-frequency archive,
  # not control-plane state: the placement hot path reads telemetry from the
  # in-memory registry in both backends, deliberately, so that
  # decision_latency_us measures the scheduler and not DynamoDB.
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery {
    # Off. This is an experiment, not a system of record: every durable result
    # is archived to S3 as run artifacts, and PITR would add continuous backup
    # charges for state that is reproducible from those artifacts.
    enabled = false
  }

  server_side_encryption {
    enabled = true # AWS-owned key; a CMK would add $1/month for no benefit here
  }

  tags = { Name = "nimbus-${var.environment}-state" }
}
