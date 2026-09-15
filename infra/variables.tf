variable "region" {
  description = "AWS region. Single region by design -- see docs/limitations.md."
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Deployment label, used in tags and resource names."
  type        = string
  default     = "dev"
}

variable "worker_count" {
  description = <<-EOT
    Number of worker instances.

    This is an experimental control, not a feature (docs/aws-native-scheduling.md
    section 4). Three is the number every result in the project was produced at;
    changing it invalidates comparison against archived runs.
  EOT
  type        = number
  default     = 3

  validation {
    condition     = var.worker_count >= 1 && var.worker_count <= 10
    error_message = "worker_count must be between 1 and 10; the budget assumes 3."
  }
}

variable "instance_type" {
  description = <<-EOT
    Instance type for workers and the control plane.

    t3.small is the budgeted choice at $0.0208/hour. Its burstable CPU credits
    are a known measurement hazard (docs/experiments.md section 8): a headline
    run that exhausts credits must be discarded and re-run on c6i.large, which
    is the budgeted fallback at $0.085/hour.
  EOT
  type        = string
  default     = "t3.small"
}

variable "control_plane_instance_type" {
  description = "Instance type for the API + scheduler host."
  type        = string
  default     = "t3.small"
}

variable "root_volume_gb" {
  description = "EBS gp3 root volume per instance. 8 GiB x 4 = 32 GiB = $2.56/month."
  type        = number
  default     = 8
}

variable "vpc_cidr" {
  type    = string
  default = "10.42.0.0/16"
}

variable "admin_cidr" {
  description = <<-EOT
    The only CIDR permitted to reach the control plane's API and SSH ports.

    There is no default on purpose. Nimbus is unauthenticated by design through
    Phase 7 (docs/limitations.md section 5), so this security group rule is the
    only thing standing between the API and the internet. A default of
    0.0.0.0/0 would be a plausible-looking value that silently publishes an
    unauthenticated job-submission endpoint, so terraform is made to ask.
  EOT
  type        = string

  validation {
    condition     = var.admin_cidr != "0.0.0.0/0"
    error_message = "admin_cidr must not be 0.0.0.0/0: the API has no authentication."
  }
}

variable "max_attempts" {
  description = <<-EOT
    Retry budget per job. The SQS redrive policy's maxReceiveCount is derived
    from this as max_attempts + 2, matching backend/services/factory.py.

    The +2 is load-bearing: SQS counts *deliveries*, not *attempts*, so a
    delivery that fails to place and is deferred also increments the counter.
    Setting maxReceiveCount equal to the retry budget would dead-letter healthy
    jobs whenever a burst momentarily exceeded fleet capacity -- measured in
    Phase 1, where a 24-job batch produced 65 deliveries and dead-lettered 5
    perfectly good jobs.
  EOT
  type        = number
  default     = 3
}

variable "visibility_timeout_s" {
  description = <<-EOT
    SQS visibility timeout.

    The scheduler holds a message's lease for the whole job lifetime and
    extends it on a timer, so this does not need to exceed the longest job.
    It does bound orphan recovery: a job whose lease is lost cannot be made
    visible again without its receipt, so it comes back only when this elapses.
  EOT
  type        = number
  default     = 60
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention. 7 days keeps ingestion cost negligible."
  type        = number
  default     = 7
}

variable "budget_limit_usd" {
  description = <<-EOT
    AWS Budgets threshold. The project budget is ~$8.10 (docs/cost-model.md
    section 4); the alarm sits at $15 so it fires on a real overrun rather than
    on normal variance.
  EOT
  type        = number
  default     = 15
}

variable "budget_notification_email" {
  description = "Address for the budget alarm. Empty disables the budget resource."
  type        = string
  default     = ""
}

variable "enable_batch_baseline" {
  description = <<-EOT
    Create the AWS Batch compute environment, queue and job definition.

    Default false, and that is a deliberate statement rather than a convenience.
    docs/architecture.md section 12 says batch.tf exists only if the Batch
    comparison is actually run, and docs/experiments.md section 6 sets the
    fairness preconditions -- a pre-warmed compute environment, a pre-pulled
    image, and measurement from RUNNING. An unfair Batch comparison is worse
    than no Batch comparison, so this defaults to not existing.
  EOT
  type        = bool
  default     = false
}

variable "key_name" {
  description = "Optional EC2 key pair for SSH. SSM Session Manager is preferred."
  type        = string
  default     = ""
}
