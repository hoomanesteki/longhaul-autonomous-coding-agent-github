# Secrets Manager entries. Terraform creates an empty entry for each credential that a person
# supplies, and fills the two that it generates itself:
#
#   github-token, anthropic-api-key, openai-api-key, admin-token
#       Empty. Put the value with `aws secretsmanager put-secret-value` (docs/deploy-aws.md).
#       Terraform never sees the values, so they are not in the state file.
#   database-url, blob-credentials
#       Written here, from the database and the IAM user that this stack creates.
#
# The services read the entries when a task starts, through the task execution role. A task
# that names an entry without a value does not start, so fill all four, or at least the ones the
# services use, before services_enabled goes to true.

locals {
  # Which credential goes with which setting. LONGHAUL_ADMIN_TOKEN is the console bearer token
  # (plan section 3.8) and ANTHROPIC_API_KEY and OPENAI_API_KEY are the model keys.
  person_supplied_secrets = toset([
    "github-token",
    "anthropic-api-key",
    "openai-api-key",
    "admin-token",
  ])

  # 0 deletes a secret at once, so a destroy followed by an apply can reuse the names. With
  # protect_data on, the default recovery window protects a secret that was deleted by mistake.
  secret_recovery_days = var.protect_data ? 30 : 0
}

resource "aws_secretsmanager_secret" "person_supplied" {
  for_each = local.person_supplied_secrets

  name                    = "${var.name}/${each.key}"
  description             = "Set with aws secretsmanager put-secret-value. Terraform leaves the value alone."
  recovery_window_in_days = local.secret_recovery_days
}

resource "aws_secretsmanager_secret" "database_url" {
  name                    = "${var.name}/database-url"
  description             = "LONGHAUL_DATABASE_URL, written by Terraform."
  recovery_window_in_days = local.secret_recovery_days
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id = aws_secretsmanager_secret.database_url.id
  secret_string = format(
    "postgresql://%s:%s@%s:%s/%s",
    aws_db_instance.main.username,
    random_password.database.result,
    aws_db_instance.main.address,
    aws_db_instance.main.port,
    aws_db_instance.main.db_name,
  )
}

# S3BlobStore needs an access key and a secret key, and has no way to use the credentials of a
# task role. This user is the way around that. Its policy allows reading and writing objects in
# the one bucket and nothing else.
resource "aws_iam_user" "blobs" {
  name          = "${var.name}-blobs"
  force_destroy = true
}

resource "aws_iam_access_key" "blobs" {
  user = aws_iam_user.blobs.name
}

data "aws_iam_policy_document" "blobs_user" {
  statement {
    sid       = "ReadAndWriteObjects"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.blobs.arn}/*"]
  }

  # Without s3:ListBucket, S3 answers a read of a missing key with AccessDenied and not with
  # NoSuchKey, and the blob store would report a missing blob as a permission error.
  statement {
    sid       = "ListTheBucket"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.blobs.arn]
  }
}

resource "aws_iam_user_policy" "blobs" {
  name   = "blob-bucket"
  user   = aws_iam_user.blobs.name
  policy = data.aws_iam_policy_document.blobs_user.json
}

resource "aws_secretsmanager_secret" "blob_credentials" {
  name                    = "${var.name}/blob-credentials"
  description             = "LONGHAUL_BLOB_ACCESS_KEY and LONGHAUL_BLOB_SECRET_KEY, written by Terraform."
  recovery_window_in_days = local.secret_recovery_days
}

resource "aws_secretsmanager_secret_version" "blob_credentials" {
  secret_id = aws_secretsmanager_secret.blob_credentials.id
  secret_string = jsonencode({
    access_key = aws_iam_access_key.blobs.id
    secret_key = aws_iam_access_key.blobs.secret
  })
}
