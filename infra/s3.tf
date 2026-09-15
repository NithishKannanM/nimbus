# Artifact bucket. Job payloads over 256 KB, job outputs, and every benchmark
# run's archive (manifest, raw.jsonl, decisions.jsonl, telemetry.jsonl).
#
# The key layout matches LocalArtifactStore exactly, so a run directory has the
# same shape whichever backend produced it and the analysis scripts do not need
# to know which one did.

resource "aws_s3_bucket" "artifacts" {
  bucket = "nimbus-${var.environment}-artifacts-${random_id.suffix.hex}"

  # Teardown-and-redeploy is the normal working pattern here
  # (docs/cost-model.md section 4), and a bucket that refuses to be destroyed
  # because it still holds objects would leave storage billing after every
  # session. Run artifacts worth keeping are pulled down before teardown by
  # scripts/demo/teardown.sh.
  force_destroy = true

  tags = { Name = "nimbus-${var.environment}-artifacts" }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256" # SSE-S3: free. SSE-KMS would bill per request.
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  versioning_configuration {
    # Off, deliberately. Artifact keys already carry the attempt number
    # (jobs/{job_id}/attempt-{n}/output.bin), so a retry writes to its own key
    # and versioning would only accumulate charged copies of overwrites that
    # by design are identical.
    status = "Suspended"
  }
}

# Refuse any write that is not encrypted, and any request over plain HTTP.
# The S3ArtifactStore asks for AES256 on every PutObject as well; this is the
# half that still holds if someone writes to the bucket by another route.
resource "aws_s3_bucket_policy" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyUnencryptedObjectUploads"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.artifacts.arn}/*"
        Condition = {
          StringNotEquals = { "s3:x-amz-server-side-encryption" = "AES256" }
        }
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.artifacts.arn,
          "${aws_s3_bucket.artifacts.arn}/*",
        ]
        Condition = {
          Bool = { "aws:SecureTransport" = "false" }
        }
      },
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.artifacts]
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  # Job inputs and outputs are intermediate: reproducible from the run archive
  # and the seeded generator, so there is no reason to pay to keep them.
  rule {
    id     = "expire-job-payloads"
    status = "Enabled"

    filter {
      prefix = "jobs/"
    }

    expiration {
      days = 30
    }
  }

  rule {
    id     = "expire-job-inputs"
    status = "Enabled"

    filter {
      prefix = "inputs/"
    }

    expiration {
      days = 30
    }
  }

  # benchmarks/ is deliberately not expired. Those are the results.
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}
