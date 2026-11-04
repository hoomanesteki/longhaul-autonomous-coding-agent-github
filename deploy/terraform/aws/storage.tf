# The blob bucket: transcripts, tool output and diffs, which the workflows pass around by
# reference (ADR-0005). Keys are content hashes, so an object is never rewritten. Versioning
# still stays on, so a bad delete or a bad overwrite can be undone.

resource "aws_s3_bucket" "blobs" {
  bucket = "${var.name}-blobs-${data.aws_caller_identity.current.account_id}-${var.aws_region}"

  # A destroy has to empty a versioned bucket first. force_destroy does that, and it is on only
  # while protect_data is off.
  force_destroy = !var.protect_data
}

resource "aws_s3_bucket_versioning" "blobs" {
  bucket = aws_s3_bucket.blobs.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "blobs" {
  bucket = aws_s3_bucket.blobs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "blobs" {
  bucket = aws_s3_bucket.blobs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "blobs" {
  bucket = aws_s3_bucket.blobs.id

  rule {
    id     = "expire-old-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # Versioning has to be on before a rule can name noncurrent versions.
  depends_on = [aws_s3_bucket_versioning.blobs]
}

data "aws_iam_policy_document" "blobs_tls_only" {
  statement {
    sid       = "DenyUnencryptedTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.blobs.arn, "${aws_s3_bucket.blobs.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "blobs" {
  bucket = aws_s3_bucket.blobs.id
  policy = data.aws_iam_policy_document.blobs_tls_only.json

  # Two changes to the same bucket at once can fail, so the policy waits for the access block.
  depends_on = [aws_s3_bucket_public_access_block.blobs]
}
