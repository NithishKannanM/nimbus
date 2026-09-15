# AWS Batch baseline. Created only when var.enable_batch_baseline is true.
#
# docs/architecture.md section 12: "batch.tf is created only if the Batch
# comparison is actually run. Otherwise it does not exist." The conditional is
# the point, not a convenience. docs/experiments.md section 6 sets preconditions
# for the comparison to be fair at all:
#
#   R4 -- Batch's RUNNABLE wait and container image pull dominate short jobs.
#   A naive comparison would show Nimbus winning by a factor of ten and the
#   finding would be entirely an artefact of measuring cold-start.
#
# Hence, below: minVcpus > 0 so the compute environment is pre-warmed, and the
# measurement protocol requires timing from RUNNING as well as end-to-end. If
# the warm environment is not affordable for the duration of a run, the project
# publishes an architectural comparison and no number. An unfair benchmark is
# worse than an absent one.

resource "aws_iam_role" "batch_service" {
  count = var.enable_batch_baseline ? 1 : 0

  name = "nimbus-${var.environment}-batch-service-${random_id.suffix.hex}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "batch.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "batch_service" {
  count = var.enable_batch_baseline ? 1 : 0

  role       = aws_iam_role.batch_service[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBatchServiceRole"
}

resource "aws_iam_role" "batch_instance" {
  count = var.enable_batch_baseline ? 1 : 0

  name = "nimbus-${var.environment}-batch-instance-${random_id.suffix.hex}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "batch_instance" {
  count = var.enable_batch_baseline ? 1 : 0

  role       = aws_iam_role.batch_instance[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

resource "aws_iam_instance_profile" "batch_instance" {
  count = var.enable_batch_baseline ? 1 : 0

  name = "nimbus-${var.environment}-batch-${random_id.suffix.hex}"
  role = aws_iam_role.batch_instance[0].name
}

resource "aws_batch_compute_environment" "nimbus" {
  count = var.enable_batch_baseline ? 1 : 0

  name         = "nimbus-${var.environment}-ce-${random_id.suffix.hex}"
  type         = "MANAGED"
  service_role = aws_iam_role.batch_service[0].arn

  compute_resources {
    type      = "EC2"
    subnets   = [aws_subnet.public.id]
    security_group_ids = [aws_security_group.worker.id]
    instance_role      = aws_iam_instance_profile.batch_instance[0].arn
    instance_type      = [var.instance_type]

    # Pre-warmed. This is the R4 mitigation and it is the expensive line in the
    # Batch budget: six vCPUs stay running for the duration of the comparison so
    # that jobs do not spend their measured latency waiting for capacity that
    # Nimbus's fleet already has. Matching the fleets is what makes the
    # comparison about *placement* rather than about provisioning.
    min_vcpus     = 6
    desired_vcpus = 6
    max_vcpus     = 6

    allocation_strategy = "BEST_FIT_PROGRESSIVE"
  }

  depends_on = [aws_iam_role_policy_attachment.batch_service]
}

resource "aws_batch_job_queue" "nimbus" {
  count = var.enable_batch_baseline ? 1 : 0

  name     = "nimbus-${var.environment}-queue-${random_id.suffix.hex}"
  state    = "ENABLED"
  priority = 1

  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.nimbus[0].arn
  }
}

resource "aws_batch_job_definition" "workload" {
  count = var.enable_batch_baseline ? 1 : 0

  name = "nimbus-${var.environment}-workload-${random_id.suffix.hex}"
  type = "container"

  # The container runs the same closed workload registry as the Nimbus worker.
  # If it ran different code the comparison would be measuring two different
  # programs rather than two schedulers.
  container_properties = jsonencode({
    image   = var.batch_image
    command = ["python3", "-m", "worker.batch_entrypoint", "Ref::job_type", "Ref::payload"]
    resourceRequirements = [
      { type = "VCPU", value = "1" },
      { type = "MEMORY", value = "512" },
    ]
    environment = [
      # Same single-thread BLAS restriction as the Nimbus worker. Without it
      # a Batch container would spread one matmul across every core and the
      # comparison would be between one-core jobs and n-core jobs.
      { name = "OMP_NUM_THREADS", value = "1" },
      { name = "OPENBLAS_NUM_THREADS", value = "1" },
      { name = "MKL_NUM_THREADS", value = "1" },
    ]
  })

  retry_strategy {
    attempts = var.max_attempts
  }
}

variable "batch_image" {
  description = "ECR image URI running the Nimbus workload registry. Required if enable_batch_baseline."
  type        = string
  default     = ""
}
