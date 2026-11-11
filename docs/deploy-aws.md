# Deploy Longhaul to AWS

The Terraform in `deploy/terraform/aws/` builds a Longhaul stack in one AWS account: a private network, a database, a blob bucket, and the services that run the agent. It is optional. Nothing else in the repo needs it, and CI only checks that it is valid (see "Check the Terraform without AWS").

Applying it is manual, and it costs money while it exists. Plan to apply, try it, and tear it down (see "Tear it down"). The state file holds secrets, so it is never committed.

## What it builds

| Part | Runs on | Why |
|---|---|---|
| Worker | ECS on EC2, one task per instance | A worker starts a Docker sandbox for every task, so it needs a Docker daemon and its socket. A Fargate task has neither. |
| API | ECS on Fargate | It never starts a container. It is off by default, because nothing in front of it gives it TLS or a way in (see "Security notes"). |
| Poller | A Fargate task that you run on request | It installs the Temporal Schedule `longhaul-poller` and exits. The schedule lives in Temporal, so nothing has to stay up for it. The workers run `poll_once`. |
| Migrations | A Fargate task that you run on request | It applies the Alembic migrations to the database and exits. |
| Database | RDS PostgreSQL 17 with pgvector | Private subnets only, encrypted, TLS required. |
| Blobs | S3 | One bucket, versioned and encrypted, with public access blocked. |
| Temporal | Temporal Cloud, or a server you run | `temporal_mode` chooses (see "Temporal"). Terraform builds no server. |

Running each sandbox as its own Fargate task is a possible later design. It needs a sandbox manager that talks to the ECS API instead of Docker, so it is not part of this stack.

## Resources and what they cost

Every resource type that the Terraform declares is below. The last column is the price while it exists, from the public on-demand prices of us-east-1. Prices change, so check the AWS Pricing Calculator before you rely on a number.

| Resource | What it is for | Per hour |
|---|---|---|
| `aws_vpc` | The network, 10.40.0.0/16 by default | free |
| `aws_internet_gateway` | The way out for the NAT gateway's subnet | free |
| `aws_subnet` | One public subnet that holds only the NAT gateway, and two private subnets in two zones | free |
| `aws_eip` | The public address of the NAT gateway | $0.005 |
| `aws_nat_gateway` | The outbound path of the private subnets, for GitHub, the model APIs, Temporal Cloud and package indexes | $0.045, and $0.045 per GB it processes |
| `aws_route_table` | Two tables: the public one to the internet gateway, the private one to the NAT gateway | free |
| `aws_route_table_association` | Ties each subnet to its table | free |
| `aws_vpc_endpoint` | A gateway endpoint for S3, so blobs and image layers skip the NAT gateway and its per-GB price | free |
| `aws_security_group` | Five groups: worker task, worker host, API, poller and database | free |
| `aws_vpc_security_group_egress_rule` | Lets four of the groups open connections out | free |
| `aws_vpc_security_group_ingress_rule` | Database from the worker and API tasks, and the API port from inside the VPC | free |
| `random_password` | The database master password | free |
| `aws_db_subnet_group` | Puts the database in the private subnets | free |
| `aws_db_parameter_group` | Turns on `rds.force_ssl` | free |
| `aws_db_instance` | PostgreSQL 17 on a `db.t4g.micro`, 20 GiB of gp3 storage, 7 days of backups | $0.016, plus $0.003 for storage |
| `aws_s3_bucket` | The blob bucket | cents per GB-month |
| `aws_s3_bucket_versioning` | Keeps old versions of an object | with the bucket |
| `aws_s3_bucket_server_side_encryption_configuration` | Encrypts every object with AES-256 | free |
| `aws_s3_bucket_public_access_block` | Refuses public ACLs and policies | free |
| `aws_s3_bucket_lifecycle_configuration` | Deletes old versions after 30 days and abandoned uploads after 7 | free |
| `aws_s3_bucket_policy` | Refuses requests that are not TLS | free |
| `aws_ecr_repository` | Three repositories: `worker`, `api` and `sandbox`. Tags are immutable and pushes are scanned | $0.10 per GB-month |
| `aws_ecr_lifecycle_policy` | Keeps the 20 newest images of each repository | free |
| `aws_secretsmanager_secret` | Six entries: `github-token`, `anthropic-api-key`, `openai-api-key`, `admin-token`, `database-url` and `blob-credentials` | $0.0006 each |
| `aws_secretsmanager_secret_version` | The values that Terraform writes itself: the database URL and the blob keys | with the secret |
| `aws_iam_user` | A user that can read and write the one blob bucket. `S3BlobStore` needs a key pair | free |
| `aws_iam_access_key` | The key pair of that user | free |
| `aws_iam_user_policy` | Limits that user to the bucket | free |
| `aws_iam_role` | The task execution role (images, logs, secrets) and the role of the worker hosts | free |
| `aws_iam_role_policy` | Lets the task execution role read exactly the secrets that the tasks name | free |
| `aws_iam_role_policy_attachment` | The AWS-managed policies for task execution, for the ECS agent on a host, and for Session Manager | free |
| `aws_iam_instance_profile` | Gives the worker hosts their role | free |
| `aws_cloudwatch_log_group` | Four groups: `/longhaul/worker`, `/api`, `/poller` and `/migrate` | $0.50 per GB ingested |
| `aws_ecs_cluster` | The cluster | free |
| `aws_launch_template` | The worker host: Amazon Linux 2023 ECS image, metadata service version 2 with a hop limit of 1, 60 GiB encrypted root volume | see the instance |
| `aws_autoscaling_group` | Keeps `worker_count` hosts, and none while the services are off | one `m6i.large` is $0.096, and its volume $0.007 |
| `aws_ecs_capacity_provider` | Lets ECS place tasks on that group | free |
| `aws_ecs_cluster_capacity_providers` | Registers the group and Fargate with the cluster | free |
| `aws_ecs_task_definition` | Four: `worker` (EC2), and `api`, `poller` and `migrate` (Fargate) | free |
| `aws_ecs_service` | The `worker` service, and the `api` service | the API at 0.5 vCPU and 1 GiB is $0.025. A poller or migrate run costs a cent or less |

Terraform also reads four things and creates nothing for them: the account id, the availability zones, the recommended ECS image id, and the IAM policy documents.

### Per hour, in total

| State | About per hour | What it is made of |
|---|---|---|
| Built, services off (`services_enabled = false`) | $0.07 | NAT gateway and its address $0.050, database $0.019, secrets $0.003 |
| Running, one worker, no API | $0.18 | the above, plus one `m6i.large` and its volume $0.103 |
| Running, one worker and the API | $0.20 | the above, plus $0.025 |

Each extra worker adds about $0.10. A full month is 730 hours, so the running stack is about $130 a month, mostly the NAT gateway, the instance and the database. Data processed by the NAT gateway, CloudWatch ingestion and model API usage come on top. The model usage is the large one: budgets in the Longhaul config are what keep it in check.

Setting `services_enabled = false` and applying stops the instance and the services and leaves the $0.07 floor. Only `terraform destroy` stops that.

## Before you start

- An AWS account and credentials that the AWS CLI and Terraform can use. The stack creates IAM roles and users, so the identity needs rights to do that.
- Terraform 1.17.0. The version is pinned in `versions.tf`, in the `terraform` job of `ci.yml` and in ADR-0002.
- Docker with `buildx`, and `jq`. The services run on x86 (`linux/amd64`). On an ARM laptop an image built the plain way would not start, so the commands below name the platform.
- A Temporal namespace (see "Temporal").
- The region is `us-east-1`. `S3BlobStore` signs its requests for that region, so a bucket elsewhere fails at the first blob write.

## Apply

The first apply builds everything that costs little and starts no compute. You push the images and fill the secrets before the services are switched on, because a task cannot start without its image or its secrets.

```bash
cd deploy/terraform/aws
cp terraform.tfvars.example terraform.tfvars   # edit the Temporal block
terraform init
terraform plan -out tfplan
terraform apply tfplan
```

Then fill the secrets that a person supplies. Terraform created them empty, and it never sees their values:

```bash
read -rs -p "GitHub token: " TOKEN && echo \
  && aws secretsmanager put-secret-value --secret-id longhaul/github-token --secret-string "$TOKEN"
unset TOKEN
# The same for longhaul/anthropic-api-key (or longhaul/openai-api-key when llm_provider is
# openai), and for longhaul/admin-token when you run the API.
```

Build and push the images from the repo root. The tag is a version or a commit id, because the repositories refuse to overwrite a tag:

```bash
TAG=v0.1.0
URLS=$(terraform -chdir=deploy/terraform/aws output -json ecr_repository_urls)
WORKER=$(jq -r .worker <<<"$URLS")
SANDBOX=$(jq -r .sandbox <<<"$URLS")
API=$(jq -r .api <<<"$URLS")
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin "${WORKER%%/*}"
docker buildx build --platform linux/amd64 -f docker/worker/Dockerfile -t "$WORKER:$TAG" --push .
docker buildx build --platform linux/amd64 -t "$SANDBOX:$TAG" --push docker/sandbox
docker buildx build --platform linux/amd64 -f docker/api/Dockerfile -t "$API:$TAG" --push .   # only for api_enabled
```

The repo list lives in the config file inside the image. The default image config has no repositories, so put your own in `config/longhaul.yaml` before you build the worker image, and set `config_path = "/app/config/longhaul.yaml"` in `terraform.tfvars`.

Apply the migrations, then switch the services on:

```bash
cd deploy/terraform/aws
sh -c "$(terraform output -raw migrate_command)"        # starts a Fargate task and returns
aws logs tail /longhaul/migrate --follow --region us-east-1   # ends with the last migration

terraform apply -var services_enabled=true -var image_tag=v0.1.0
```

The worker host takes a few minutes to boot, pull the sandbox image and register. Then ECS starts the worker task on it. Install the poller schedule once the worker is up, and read the worker log:

```bash
sh -c "$(terraform output -raw poller_install_command)"
aws logs tail /longhaul/worker --follow --region us-east-1
```

To upgrade, push images with a new tag, apply with the new `image_tag`, and replace the worker hosts so that they pull the new sandbox image: `aws autoscaling start-instance-refresh --auto-scaling-group-name longhaul-worker`. Do it when no task is running. Temporal retries the activities that were in flight, but the checkpoint repos live on the disk of the host that is replaced, so a task that was mid-run falls back to the recovery of failure mode F4 in the plan: a git bundle from blob storage when there is one, else the base commit.

### The variables you will touch

| Variable | Default | Meaning |
|---|---|---|
| `temporal_mode` | `cloud` | `cloud` or `self_hosted` |
| `temporal_endpoint` | none | `host:port` of the Temporal frontend |
| `temporal_namespace` | none | The namespace, `name.account-id` in Temporal Cloud |
| `temporal_client_cert_secret_arn`, `temporal_client_key_secret_arn` | empty | Secrets that hold the client certificate and key. Required in cloud mode |
| `services_enabled` | `false` | Starts the worker hosts and the worker service |
| `api_enabled` | `false` | Starts the API service when the services are on. Push the `api` image first, and fill `longhaul/admin-token` |
| `image_tag` | empty | The tag you pushed. Required when `services_enabled` is true |
| `llm_provider` | `anthropic` | Decides which model key the workers receive |
| `worker_count` | `1` | Workers, each on an instance of its own |
| `worker_instance_type` | `m6i.large` | A sandbox may use 4 GiB and the worker 2 GiB |
| `config_path` | empty | Config file inside the image. Empty keeps the example config |
| `protect_data` | `false` | See "Tear it down" |

`terraform.tfvars.example` and the descriptions in `variables.tf` have the rest.

## Temporal

`temporal_mode` is `cloud` or `self_hosted`, and in both modes Terraform builds no Temporal server.

**Cloud.** Create a namespace in Temporal Cloud and a client certificate for it. Store the PEM certificate and the PEM key in two Secrets Manager secrets, and give their ARNs as `temporal_client_cert_secret_arn` and `temporal_client_key_secret_arn`, with `temporal_endpoint` and `temporal_namespace` from the Temporal Cloud console. The tasks receive the two PEMs as `LONGHAUL_TEMPORAL_TLS_CERT` and `LONGHAUL_TEMPORAL_TLS_KEY`. The plan fails if cloud mode has no ARNs. The Python client cannot use them yet (see "What the application does not do yet").

**Self-hosted.** This is documented and not built. Run a Temporal server whose frontend (port 7233) the private subnets can reach: on instances or services inside this VPC, or in a peered one. Give it its own persistence, because the Longhaul database is not Temporal's. Then set `temporal_mode = "self_hosted"`, `temporal_endpoint` and `temporal_namespace`, and leave the certificate ARNs empty. Allow port 7233 into the Temporal server's security group from the three groups in the output `client_security_group_ids`. The connection is unencrypted, so keep it inside the VPC.

## Tear it down

```bash
cd deploy/terraform/aws
terraform destroy
```

With `protect_data = false`, the default, this removes everything, including every object in the bucket, every image, the secrets and the database, with no final snapshot. That suits a trial. It is also why you should not run it against a stack that holds work you want.

With `protect_data = true`, a destroy keeps a final database snapshot, refuses to delete a bucket or a repository that still holds data, and keeps deleted secrets for 30 days. To tear such a stack down on purpose, apply `-var protect_data=false` first, then destroy.

Check what is left. Every resource carries the tag `Project = longhaul`:

```bash
aws resourcegroupstaggingapi get-resources --tag-filters Key=Project,Values=longhaul --region us-east-1
```

Things that Terraform did not create stay: the Temporal certificate secrets that you made, any manual snapshot, and Temporal Cloud itself. If a destroy stops on a subnet or a security group, a Fargate task from `run-task` has not finished and still holds its network interface. Wait for it and run the destroy again.

## Security notes

- **No inbound path from the internet.** No security group has a rule from `0.0.0.0/0`, and nothing has a public address. The API accepts port 8000 from inside the VPC only. No load balancer is built; add an internal one, a VPN or a bastion host when the console needs to be reached.
- **The API is off by default.** Its image (`docker/api/Dockerfile`) and its command (`longhaul api`) exist, and `api_enabled = true` starts the service. It stays off because the stack puts no load balancer and no TLS in front of it: the service has no public address, listens on plain HTTP on port 8000 and answers only inside the VPC, and its admin token travels in the clear on that hop. Turn it on when you have an internal load balancer with a certificate, a VPN or a bastion host to reach it, and have filled the `longhaul/admin-token` secret. Change `api_command` if you name the command differently.
- **The Docker socket.** The worker task mounts the host's socket, which is root on the host (see "The worker in Docker" in `docs/runbook.md`). Each worker has an instance to itself, and the instance holds nothing but the worker and its sandboxes.
- **Sandboxes cannot reach the database or the instance role.** Sandbox traffic leaves through the host's own network interface and carries the host's security group, which the database does not accept. The host requires metadata service version 2 with a hop limit of 1, so a container behind the Docker bridge cannot read the instance role, and `ECS_AWSVPC_BLOCK_IMDS` blocks the awsvpc task containers.
- **The containers hold no AWS role.** They receive secrets as environment variables from ECS. The one AWS credential they hold is the key pair of the blob user, which can read and write one bucket. The API shares it, so it can write there too.
- **State holds secrets.** The database password and the blob secret key are in `terraform.tfstate` in plain text. The file is ignored by git. For anything shared, keep the state in an encrypted S3 bucket with a lock table instead of on disk: add a `backend "s3"` block to a `backend.tf` of your own.
- **Hosts have no SSH.** Reach one with Session Manager (`aws ssm start-session --target <instance id>`).

## What the application does not do yet

The stack is written against the application as it is. These gaps are in the application, not in Terraform. Each has a workaround here, or is switched off, and none changes the Terraform.

- **Temporal Cloud needs TLS, and the client has none.** `longhaul.durable.client.connect` calls `Client.connect(address, namespace=...)` with no certificate. Until the settings and the client accept `LONGHAUL_TEMPORAL_TLS_CERT` and `LONGHAUL_TEMPORAL_TLS_KEY`, which are the names the tasks already receive, `cloud` mode cannot connect, and `self_hosted` inside the VPC can.
- **The blob store needs static keys and `us-east-1`.** `S3BlobStore.from_settings` refuses to start without an access key and a secret key, and it signs for `us-east-1`. That is why the stack creates an IAM user and keeps the region fixed. When the store learns to use the credential chain and a region, the user and its secret can go.

## Check the Terraform without AWS

CI has no AWS credentials, so it never plans or applies. The `terraform` job in `.github/workflows/ci.yml` runs the three checks that need none, with the Terraform version from `versions.tf`:

```bash
cd deploy/terraform/aws
terraform fmt -check -recursive -diff
terraform init -backend=false -input=false -lockfile=readonly
terraform validate
```

Run the same commands before you push a change. `-lockfile=readonly` fails the job when `.terraform.lock.hcl` and `versions.tf` disagree. After a provider bump, record the hashes of every platform that CI and the team use:

```bash
terraform providers lock -platform=linux_amd64 -platform=linux_arm64 \
  -platform=darwin_amd64 -platform=darwin_arm64 -platform=windows_amd64
```

`tests/unit/test_deploy_aws.py` also reads the `.tf` files. It fails when a version stops being exact, when ADR-0002 or the lock file lists another version, when the workers leave EC2, or when a resource type is missing from the table in this document.
