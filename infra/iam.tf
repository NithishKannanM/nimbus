# IAM. This is where the control/data-plane split stops being a convention.
#
# docs/architecture.md section 2 states the rule: control-plane code never
# executes user workload, data-plane code never makes a placement decision. In
# the Python that is enforced by module boundaries, which is to say by whoever
# is reviewing the diff. Here it is enforced by the fact that **the worker role
# has no SQS and no DynamoDB permissions at all**. A worker that cannot reach
# the queue cannot bypass the scheduler even if someone writes the code to try,
# and the "scheduler is the sole consumer" invariant -- which every strategy
# comparison in this project depends on -- becomes a property of the
# infrastructure rather than of the codebase's good intentions.

data "aws_caller_identity" "current" {}

# --------------------------------------------------------------------------- #
# Worker role: S3 artifacts + its own logs. Nothing else.
# --------------------------------------------------------------------------- #
resource "aws_iam_role" "worker" {
  name = "nimbus-${var.environment}-worker-${random_id.suffix.hex}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = { Name = "nimbus-${var.environment}-worker" }
}

resource "aws_iam_role_policy" "worker" {
  name = "nimbus-worker"
  role = aws_iam_role.worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ArtifactsReadWrite"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject"]
        # Scoped to the objects, not the bucket: a worker has no reason to
        # enumerate what other jobs produced.
        Resource = "${aws_s3_bucket.artifacts.arn}/*"
      },
      {
        Sid      = "OwnLogsOnly"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.worker.arn}:*"
      },
      {
        Sid      = "OwnMetricsOnly"
        Effect   = "Allow"
        Action   = "cloudwatch:PutMetricData"
        Resource = "*" # PutMetricData does not support resource ARNs
        Condition = {
          StringEquals = { "cloudwatch:namespace" = "Nimbus" }
        }
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "worker_ssm" {
  # Session Manager instead of SSH: no key material, no port 22, and the
  # session is logged. This is why key_name defaults to empty.
  role       = aws_iam_role.worker.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "worker" {
  name = "nimbus-${var.environment}-worker-${random_id.suffix.hex}"
  role = aws_iam_role.worker.name
}

# --------------------------------------------------------------------------- #
# Control-plane role: the queue, the table, metrics, and read access to
# artifacts. It is the only principal that can touch SQS or DynamoDB.
# --------------------------------------------------------------------------- #
resource "aws_iam_role" "control_plane" {
  name = "nimbus-${var.environment}-control-plane-${random_id.suffix.hex}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = { Name = "nimbus-${var.environment}-control-plane" }
}

resource "aws_iam_role_policy" "control_plane" {
  name = "nimbus-control-plane"
  role = aws_iam_role.control_plane.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "QueueOperations"
        Effect = "Allow"
        Action = [
          "sqs:SendMessage",
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:ChangeMessageVisibility",
          "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl",
          "sqs:PurgeQueue",
        ]
        Resource = [aws_sqs_queue.jobs.arn, aws_sqs_queue.jobs_dlq.arn]
      },
      {
        Sid    = "StateTable"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:BatchGetItem",
          "dynamodb:BatchWriteItem",
        ]
        # The table and its indexes. Note the absence of dynamodb:Scan: the
        # access patterns were designed so that nothing needs it, and leaving
        # the permission out is what keeps that true when someone adds a
        # "just list everything" endpoint in a hurry.
        Resource = [
          aws_dynamodb_table.state.arn,
          "${aws_dynamodb_table.state.arn}/index/*",
        ]
      },
      {
        Sid      = "ArtifactsFullAccess"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
        Resource = [aws_s3_bucket.artifacts.arn, "${aws_s3_bucket.artifacts.arn}/*"]
      },
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.control_plane.arn}:*"
      },
      {
        Sid      = "Metrics"
        Effect   = "Allow"
        Action   = "cloudwatch:PutMetricData"
        Resource = "*"
        Condition = {
          StringEquals = { "cloudwatch:namespace" = "Nimbus" }
        }
      },
      {
        Sid    = "DescribeFleet"
        Effect = "Allow"
        # The scheduler resolves worker private IPs to dispatch to them.
        Action   = ["ec2:DescribeInstances", "ec2:DescribeTags"]
        Resource = "*" # these Describe calls do not support resource ARNs
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "control_plane_ssm" {
  role       = aws_iam_role.control_plane.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "control_plane" {
  name = "nimbus-${var.environment}-control-plane-${random_id.suffix.hex}"
  role = aws_iam_role.control_plane.name
}
