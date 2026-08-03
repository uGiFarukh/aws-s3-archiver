# bucket-object-archive

A Lambda function that compresses every new object in an S3 bucket into
a ZIP archive, writes the archive back to the same bucket, and deletes
the original.

Everything is defined in one SAM/CloudFormation stack: the VPC and its
private subnets, the bucket, the containerized function, and the
notification that connects them.

```
upload  ->  s3://bucket/incoming/2026/08/03/clip.json
                        |
                        |  s3:ObjectCreated:* (prefix "incoming/")
                        v
              Lambda (container image, python3.14)
              private subnets, no internet route
                        |
                        |  GetObject -> DEFLATE -> PutObject -> DeleteObject
                        v
            s3://bucket/archive/2026/08/03/clip.json.zip
```

## Project Layout

| Path | Purpose |
| --- | --- |
| `template.yaml` | The whole stack: VPC, bucket, function, trigger |
| `object_compress/app.py` | The Lambda function |
| `object_compress/Dockerfile` | Container image for the function |
| `object_compress/requirements.txt` | Runtime python dependency |
| `events/` | Invocation events you can use to invoke the function |
| `tests/` | Unit tests for the application code |
| `samconfig.toml` | SAM CLI defaults, including the stack name |

## How it works

The notification is filtered on the `incoming/` prefix, and archives are
written to `archive/`. That separation is required because an archive
written back under `incoming/` would trigger the function again
and it would compress its own output forever. The handler re-checks the
prefix at runtime, and refuses to start at all if the two prefixes
overlap.

The object is streamed from S3 through the compressor a megabyte at a
time, so memory use does not scale with object size. Archives under
32 MB stay in memory; larger ones spill to the function's ephemeral
disk.

The original object is deleted only after `upload_fileobj` returns. Any
failure before that point leaves the source in place, and the exception
is re-raised so Lambda retries the invocation.

The path below `incoming/` is preserved inside the archive and in the
archive key, so `incoming/2026/08/03/clip.json` becomes
`archive/2026/08/03/clip.json.zip` containing one entry named
`2026/08/03/clip.json`.

## Networking

The VPC has no internet gateway and no NAT gateway. The only route out
of the private subnets is an S3 gateway endpoint, which has no hourly
charge and no data processing charge. Its policy is scoped to this
bucket and to principals in this account.

This is the single largest cost decision in the project. Routing 6.8 PiB
per month through a NAT gateway would add roughly **USD 320,800 per
month** in data processing alone — about twenty-six times the cost of
the feature itself.

## Prerequisites

* SAM CLI - [Install the SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
* Docker - [Install Docker community edition](https://hub.docker.com/search/?type=edition&offering=community)

You may need the following for local testing.
* [Python 3 installed](https://www.python.org/downloads/) or (https://docs.astral.sh/uv/)

## Validate, Build and Deploy

```bash
bucket-object-archive$ sam validate --lint
bucket-object-archive$ sam build
bucket-object-archive$ sam deploy --parameter-overrides ImageTag=$(git rev-parse --short HEAD)
```

The first command runs linting validation on the template.
The second command builds the container image from
`object_compress/Dockerfile` and installs the dependencies declared in
`object_compress/requirements.txt` inside it. The processed template is
saved in the `.aws-sam/build` folder. The third command pushes the
image to ECR and deploys the stack.

`ImageTag` defaults to `local`. Passing the commit SHA writes it into
the Lambda version description, which is what makes a deployed release
traceable back to a commit.

The stack name, capabilities and image repository resolution are already
set in `samconfig.toml`.

Deployment outputs the bucket name, the function ARN, the alias ARN, the
IAM role ARN and the VPC ID.

## Use the SAM CLI to test locally

### Unit Tests

Tests are defined in the `tests` folder. They replace the S3 client with
an in-memory double, so they need no AWS credentials and no network.

```bash
bucket-object-archive$ pip install pytest --user
bucket-object-archive$ python -m pytest tests/ -v
```

### Local Invoke

`sam local invoke` runs the container against **real S3**, so it needs
credentials, and it will delete the source object on success.

```bash
bucket-object-archive$ sam local invoke ObjectCompressFunction --event events/event.json
```

Before running it, set `s3.bucket.name`, `s3.bucket.arn` and
`s3.object.key` in `events/event.json` to a bucket and key that exist.
The remaining fields are ignored by the handler.

Note that an object uploaded under `incoming/` is archived and deleted
by the deployed function within a second or two, so a local invoke
against that prefix usually fails with `NoSuchKey`. To test locally
against a live stack, upload to a prefix the notification is not
watching and override `SOURCE_PREFIX` for the local run:

```bash
bucket-object-archive$ cat > events/env.json <<'JSON'
{ "ObjectCompressFunction": { "SOURCE_PREFIX": "local-test/",
                              "ARCHIVE_PREFIX": "local-archive/" } }
JSON
bucket-object-archive$ sam local invoke ObjectCompressFunction \
    --event events/event.json --env-vars events/env.json
```

## Fetch, Tail, and Filter Lambda function logs

```bash
bucket-object-archive$ sam logs -n ObjectCompressFunction --stack-name "bucket-object-archive" --tail
```

More examples are in the [SAM CLI Documentation](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/serverless-sam-cli-logging.html).

## Versions and Rollback

`AutoPublishAlias: live` publishes an immutable Lambda version on every
deployment and moves the `live` alias to it. The S3 notification invokes
the alias, never `$LATEST`, so rolling back is an alias update rather
than a rebuild:

```bash
aws lambda list-versions-by-function \
  --function-name bucket-object-archive-object-compress \
  --query 'Versions[].[Version,Description]' --output table

aws lambda update-alias \
  --function-name bucket-object-archive-object-compress \
  --name live --function-version 7
```

`AutoPublishAliasAllProperties` extends this to configuration-only
changes, which would otherwise reuse the existing version and leave
with nothing to roll back to.

## Parameters

| Parameter | Default | Notes |
| --- | --- | --- |
| `ImageTag` | `local` | Commit SHA; becomes the version description |
| `VpcCidr` | `10.40.0.0/16` | |
| `PrivateSubnet1Cidr` | `10.40.0.0/20` | 4,091 usable addresses |
| `PrivateSubnet2Cidr` | `10.40.16.0/20` | Second availability zone |
| `SourcePrefix` | `incoming/` | Objects are uploaded here |
| `ArchivePrefix` | `archive/` | Must not sit inside `SourcePrefix` |
| `CompressionLevel` | `6` | DEFLATE level, 0–9 |
| `LogRetentionDays` | `30` | |

Memory (1024 MB) and timeout (60 s) are set in the `Globals` block.

The bucket is named `<stack-name>-<account-id>-<region>`, so the stack
name in `samconfig.toml` gives
`bucket-object-archive-123456789012-ap-southeast-5`.

## Add a resource to your application

The template uses AWS SAM to define application resources. AWS SAM is an
extension of AWS CloudFormation with a simpler syntax for configuring
common serverless resources. For resources not included in [the SAM
specification](https://github.com/awslabs/serverless-application-model/blob/master/versions/2016-10-31.md),
you can use standard [AWS
CloudFormation](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-template-resource-type-ref.html)
resource types.

One constraint to preserve when editing `template.yaml`: the function,
its role and its environment must never use `!Ref` or `!GetAtt` on the
bucket. A bucket whose notification points at a function whose role
points back at the bucket is a circular dependency CloudFormation cannot
resolve, which is why the bucket name is derived from the stack name and
rebuilt with `!Sub` wherever it is needed.

## Security

- The bucket blocks all public access, enforces bucket-owner ownership,
  and encrypts objects with SSE-S3 and a bucket key. Its policy denies
  any request that does not arrive over TLS.
- The function's role can read and delete only under `incoming/`, and
  write only under `archive/`. It cannot delete an archive it has
  written, and cannot write into the source prefix.
- The function has no route to the internet. The gateway endpoint policy
  restricts even S3 access to this bucket, from this account.
- The bucket carries `DeletionPolicy: Retain`, so deleting the stack
  cannot delete the data.

## Cleanup

```bash
sam delete --stack-name "bucket-object-archive"
```

The bucket is retained by design and must be emptied and deleted
separately once you are sure the archives are no longer needed.

---

## Cost Analysis

**Volume:** 1,000,000 files per hour at 10 MB average.

| | |
| --- | --- |
| Objects per month | 730,000,000 (1M × 730 h) |
| Data ingested per month | 7,128,906 GiB ≈ **6.8 PiB** |

### Breakdown

Adding this feature costs approximately **USD 12,310 per month** in new
charges.

| Charge | Basis | Rate | USD / month |
| --- | --- | --- | ---: |
| Lambda duration | 474,500,000 GB-s (650 ms @ 1024 MB) | $0.0000166667 / GB-s | 7,908.35 |
| S3 PUT (archives) | 730M requests | $0.005 / 1,000 | 3,650.00 |
| CloudWatch Logs ingestion | 476 GiB | $0.63 / GB | 299.82 |
| S3 GET (sources) | 730M requests | $0.0004 / 1,000 | 292.00 |
| Lambda requests | 730M | $0.20 / 1M | 146.00 |
| CloudWatch Logs storage | 476 GiB | $0.03 / GB-month | 14.28 |
| ECR image storage | ~9 GB of image versions | $0.10 / GB-month | 0.90 |
| S3 DELETE | 730M requests | free | 0.00 |
| S3 gateway endpoint | 6.8 PiB | free | 0.00 |
| | | **Total** | **12,311.35** |

Storage is not in that table because compression *replaces* storage
rather than adding to it. At an average compression ratio of 0.10, each
month of data costs:

| | S3 Standard, per month of data |
| --- | ---: |
| Originals, uncompressed | 164,528.04 |
| Archives, ratio 0.10 | 16,959.68 |
| **Storage saved** | **−147,568.36** |
| Less the cost of the archiver | +12,311.35 |
| **Net effect** | **−135,257.01** |

That is per cohort of one month's data, and it recurs for as long as the
data is retained. With twelve months of retention the steady-state
saving is roughly USD 1.6M per month.

### The compression ratio is the whole argument

The figures above assume DEFLATE achieves a ratio of 0.10, which is
realistic for JSON, logs and other text.

| Average ratio | Archive storage | Storage delta | **Net monthly** |
| --- | ---: | ---: | ---: |
| 0.10 — text, JSON, logs | 16,960 | −147,568 | **−135,257** |

**Measure the real ratio on a sample of the actual bucket before
committing to this at scale.** In the worst case the feature costs
USD 12,475 per month and saves nothing.

### Pricing Basis

Rates are for **Malaysia (ap-southeast-5)**, taken from the Asia Pacific
schedule by Claude AI. So treat the figures as accurate to about
±10%: roughly USD 11,100–13,500. Verify before committing budget:

```bash
aws pricing get-products --service-code AWSLambda \
  --region us-east-1 --filters \
  Type=TERM_MATCH,Field=regionCode,Value=ap-southeast-5
```

S3 Standard storage is tiered: $0.025/GB for the first 50 TB, $0.024 for
the next 450 TB, $0.023 beyond that.

### Cost-saving suggestions

1. **Compress before uploading.** If producers can write a ZIP or gzip
   directly, the pipeline is unnecessary and the entire **~$12,310**
   disappears. The storage saving is unchanged.
2. **Batch many files into one archive.** One ZIP per object means one
   PUT and one invocation per object, and requests are 31% of the bill.
   Grouping ~1,000 files per archive cuts PUT charges from $3,650 to
   about $4 and Lambda requests from $146 to $0.15 — roughly **$3,790**
   — and improves the compression ratio, because DEFLATE gets a larger
   window to work with. It needs a batching trigger (S3 Inventory, or an
   SQS queue with a batch window) rather than a per-object notification.
3. **Use a faster codec.** zstd at level 3 typically beats DEFLATE
   level 6 on both ratio and speed. Since duration is 64% of the bill,
   that is worth roughly **$2,500** per month.
4. **Tune the memory setting.** CPU scales with memory, so a larger
   setting can finish faster and cost less overall. The optimum for
   DEFLATE is usually 1,024–1,792 MB; measuring it is worth roughly
   **±$2,000**.
5. **Buy a Compute Savings Plan.** Lambda duration is eligible, and a
   one-year commitment is about 17% — roughly **$1,340** per month on a
   workload as predictable as this one.
6. **Log less.** At this volume logging is a real line item. Setting
   `LOG_LEVEL` to `WARN` and dropping the per-object success line
   removes about **$310**.
7. **Do not tier the archives to Glacier per object.** Transitioning
   730M objects costs $0.02 per 1,000 transitions, or **$14,600** in
   request charges alone, which exceeds several months of the storage
   saving. Tiering only makes sense once objects are batched.
8. **Keep the S3 gateway endpoint.** Replacing it with a NAT gateway
   would add about **$320,800** per month.
9. **Change the lambda function to a different language.** Changing
   the lambda function to a compiled language such as golang or
   rustlang can save some costs from the overall lambda boot times
   and execution times thus reducing overall cost per lambda invocation.

---

## Scalability and Cost Efficiency

At 1M files per hour this design sits close to several ceilings, in
roughly this order:

**Concurrency quota.** 730M invocations per month is 278 per second
sustained, and at approximately 650 ms each that is about **181 concurrent
executions** — comfortable against the default limit of 1,000, but that
limit is shared with every other function in the account, and a backlog
replay will burst far above 181. Lambda also adds concurrency in steps
of 1,000 per 10 seconds after the initial burst. Raise the account quota
to 3,000–5,000 and set a reserved concurrency floor on this function
before going to production.

**S3 request rates.** S3 allows 3,500 PUT and 5,500 GET per second *per
partitioned prefix*. Sustained traffic of 278/s is well inside that, but
uploads at this scale are rarely smooth, and S3 needs time to partition
a new prefix. Sharding `incoming/` by date or by a hash component avoids
`503 SlowDown` during bursts.

**No back-pressure and no dead-letter queue.** This is the most serious
gap. S3 invokes Lambda asynchronously, which retries twice and then
discards the event. Because the function deletes the original object, a
genuinely lost event means an object that is never archived and never
noticed. An SQS dead-letter queue costs a few dollars a month and is the
first thing to add if this goes to production.

**Duplicate deliveries.** S3 notification delivery is at-least-once, so
the same object can be presented twice. The handler will compress it
twice and write the archive twice; the second delete then fails on an
object that is already gone. Not harmful, but it wastes an invocation
and shows up as an error metric.

**CloudWatch Logs throughput.** 476 GiB per month at INFO level is a
meaningful share of the regional ingestion quota as well as a real
charge. `WARN` is the right default at this volume.

**The per-object model does not survive another order of magnitude.**
Every cost driver here scales with object *count*, not with bytes:
requests, invocations, log lines. At 10M files per hour the request
charges alone approach $37,000 per month and the concurrency requirement
reaches roughly 1,800. Batching becomes a prerequisite.

**A ZIP container holding a single file is an odd choice.** It adds a
local header and a central directory per archive for no benefit. If the
goal is purely to shrink storage, gzip or zstd on the raw object is
cheaper and faster. ZIP is the right container only if archives will
eventually hold many files, which is also the direction that fixes the
request costs.

**Deleting the original is irreversible.** Bucket versioning is off, so
a bug in the archiver destroys data rather than hiding it. Either enable
versioning with a short non-current expiry, or run with deletion
disabled until the compression ratio and error rate have been observed
on real traffic.
