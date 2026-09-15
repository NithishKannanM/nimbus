# The fleet: one control-plane instance and `worker_count` workers.
#
# Instances are created individually rather than through an Auto Scaling group,
# and that is deliberate. Fleet size is an experimental control here, not a
# capacity decision (docs/aws-native-scheduling.md section 4): every result in
# the project was produced at three workers, and an ASG that helpfully replaced
# a failed instance would destroy the failure tests -- the whole Phase 4 exit
# criterion is that Nimbus detects a lost worker and recovers its jobs *itself*.
# Autoscaling is what AWS already does well, and Nimbus is explicitly not
# competing with it.

data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-kernel-6.*-x86_64"]
  }
}

locals {
  # Pinned so the two roles read the same values and a change cannot drift
  # between them.
  common_env = {
    NIMBUS_BACKEND         = "aws"
    AWS_REGION             = var.region
    NIMBUS_DYNAMODB_TABLE  = aws_dynamodb_table.state.name
    NIMBUS_S3_BUCKET       = aws_s3_bucket.artifacts.bucket
    NIMBUS_SQS_QUEUE_URL   = aws_sqs_queue.jobs.url
    NIMBUS_SQS_DLQ_URL     = aws_sqs_queue.jobs_dlq.url
    NIMBUS_MAX_ATTEMPTS    = tostring(var.max_attempts)
    NIMBUS_WORKER_COUNT    = tostring(var.worker_count)
    NIMBUS_VISIBILITY_TIMEOUT_S = tostring(var.visibility_timeout_s)
  }

  control_plane_user_data = templatefile("${path.module}/user_data/control_plane.sh.tftpl", {
    env            = local.common_env
    log_group      = aws_cloudwatch_log_group.control_plane.name
    region         = var.region
    # Admin endpoints are destructive by design and the API is unauthenticated
    # before Phase 7. They stay off on a deployed host; the failure tests run
    # them locally.
    enable_admin   = "0"
  })

  worker_user_data = templatefile("${path.module}/user_data/worker.sh.tftpl", {
    env       = local.common_env
    log_group = aws_cloudwatch_log_group.worker.name
    region    = var.region
  })
}

resource "aws_instance" "control_plane" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = var.control_plane_instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.control_plane.id]
  iam_instance_profile   = aws_iam_instance_profile.control_plane.name
  key_name               = var.key_name == "" ? null : var.key_name
  user_data              = local.control_plane_user_data

  root_block_device {
    volume_size = var.root_volume_gb
    volume_type = "gp3"
    encrypted   = true
  }

  metadata_options {
    # IMDSv2 required. The instance profile is the only credential on the box,
    # and IMDSv1 would let any SSRF in the API hand it out.
    http_tokens                 = "required"
    http_endpoint               = "enabled"
    http_put_response_hop_limit = 1
  }

  tags = {
    Name = "nimbus-${var.environment}-control-plane"
    Role = "control-plane"
  }
}

resource "aws_instance" "worker" {
  count = var.worker_count

  ami                    = data.aws_ami.al2023.id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.worker.id]
  iam_instance_profile   = aws_iam_instance_profile.worker.name
  key_name               = var.key_name == "" ? null : var.key_name
  user_data              = local.worker_user_data

  root_block_device {
    volume_size = var.root_volume_gb
    volume_type = "gp3"
    encrypted   = true
  }

  metadata_options {
    http_tokens                 = "required"
    http_endpoint               = "enabled"
    http_put_response_hop_limit = 1
  }

  tags = {
    # The scheduler discovers workers by these tags. worker-1, worker-2,
    # worker-3 -- the same ids the local fleet uses, so decision traces and
    # benchmark artifacts are directly comparable across backends.
    Name     = "nimbus-${var.environment}-worker-${count.index + 1}"
    Role     = "worker"
    WorkerId = "worker-${count.index + 1}"
  }
}
