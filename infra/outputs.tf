output "control_plane_public_ip" {
  description = "Reachable only from var.admin_cidr. The API has no authentication."
  value       = aws_instance.control_plane.public_ip
}

output "api_url" {
  value = "http://${aws_instance.control_plane.public_ip}:8000"
}

output "worker_private_ips" {
  description = "Push-dispatch targets. No public ingress reaches these."
  value       = aws_instance.worker[*].private_ip
}

output "worker_ids" {
  description = "Matches the local fleet's ids, so traces are comparable across backends."
  value       = [for i in aws_instance.worker : i.tags["WorkerId"]]
}

output "sqs_queue_url" {
  value = aws_sqs_queue.jobs.url
}

output "sqs_dlq_url" {
  value = aws_sqs_queue.jobs_dlq.url
}

output "dynamodb_table" {
  value = aws_dynamodb_table.state.name
}

output "s3_bucket" {
  value = aws_s3_bucket.artifacts.bucket
}

output "nimbus_env" {
  description = <<-EOT
    Paste-able environment for running Nimbus against this deployment.
    Contains resource addresses only -- never credentials, which come from the
    instance profile on EC2 and from the standard boto3 chain locally.
  EOT
  value = join("\n", [
    "NIMBUS_BACKEND=aws",
    "AWS_REGION=${var.region}",
    "NIMBUS_SQS_QUEUE_URL=${aws_sqs_queue.jobs.url}",
    "NIMBUS_SQS_DLQ_URL=${aws_sqs_queue.jobs_dlq.url}",
    "NIMBUS_DYNAMODB_TABLE=${aws_dynamodb_table.state.name}",
    "NIMBUS_S3_BUCKET=${aws_s3_bucket.artifacts.bucket}",
    "NIMBUS_VISIBILITY_TIMEOUT_S=${var.visibility_timeout_s}",
    "NIMBUS_MAX_ATTEMPTS=${var.max_attempts}",
  ])
}

output "estimated_hourly_usd" {
  description = <<-EOT
    Rough burn rate, from the on-demand prices in docs/cost-model.md section 1
    (us-east-1, retrieved 2026-08-21). Compute and the SQS interface endpoint
    only -- storage and per-request charges are pennies at this volume, and the
    real cost report is derived from counted API calls, not from this number.
  EOT
  value = format(
    "%.4f",
    (var.worker_count + 1) * 0.0208 + 0.01
  )
}

output "teardown" {
  description = "The cost control that matters most (docs/cost-model.md section 4)."
  value       = "cd infra && terraform destroy   # or scripts/demo/teardown.sh"
}
