# AWS Deployment Notes

Last updated: 2026-05-21.

## Recommended Approach

Yes: doing the full five-year historical bootstrap and recompute on the MacBook Pro first, then
loading a PostgreSQL dump into AWS RDS, is the right practical approach.

Reason:

- Historical bootstrap is CPU/network heavy and can run for hours.
- EC2 should only run the API, live Dhan worker, and daily EOD ETL.
- RDS should hold durable data; do not run Postgres in Docker for production.
- Redeploys should rebuild API/worker containers without replacing RDS data.

## Suggested Initial AWS Shape

For a small production launch:

| Layer | Recommended initial config |
|---|---|
| RDS | PostgreSQL, Single-AZ initially, `db.t4g.medium` or `db.t4g.large` |
| RDS storage | gp3, start `150-250 GB`, enable storage autoscaling |
| EC2 | Amazon Linux 2023, ARM64, `t4g.medium` minimum; `t4g.large` if running live polling + admin jobs |
| Redis | Start with Redis container on EC2; move to ElastiCache if traffic grows |
| Backups | RDS automated backups 7-14 days, deletion protection on |
| Networking | RDS private subnet/security group; EC2 can connect to RDS; API exposed through ALB or Nginx |

Use `db.t4g.medium` if the API is lightly used and daily ETL is modest. Use `db.t4g.large`
if dashboard queries, validation, or symbol-wide daily recalculation will run frequently.

For EC2, `t4g.medium` is enough for API + worker + cron. Use `t4g.large` if you want more
headroom for Docker builds, validation jobs, and live polling during market hours.

Reference pages:

- [Amazon RDS DB instance classes](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Concepts.DBInstanceClass.html)
- [Amazon RDS storage types](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_Storage.html)
- [Amazon EC2 general purpose instances](https://docs.aws.amazon.com/ec2/latest/instancetypes/gp.html)

## Local-To-RDS Migration

On the Mac after final validation:

```bash
DATABASE_URL=postgresql://pov:pov@localhost:5433/pov scripts/export_postgres_dump.sh
```

On a machine that can reach RDS:

```bash
pg_restore \
  --clean \
  --if-exists \
  --no-owner \
  --dbname "$PROD_DATABASE_URL" \
  data/pov-prod.dump
```

After restore:

```bash
python scripts/apply_schema_updates.py
python scripts/validate_database.py
```

## ETL S3 Dump

After every successful daily ETL run, `daily_update.py` automatically creates a PostgreSQL
custom-format dump and uploads it to S3. No dump file is written to local disk — `pg_dump`
stdout is piped directly to the S3 object. Dumps older than **15 days** are deleted from
S3 automatically in the same run.

### Required env vars

| Variable | Description |
|---|---|
| `S3_DUMP_BUCKET` | S3 bucket name (dump is skipped when empty) |
| `AWS_ACCESS_KEY_ID` | IAM access key with `s3:PutObject`, `s3:ListBucket`, `s3:DeleteObject` |
| `AWS_SECRET_ACCESS_KEY` | Corresponding secret key |
| `AWS_REGION` | Bucket region, default `ap-south-1` |
| `S3_DUMP_PREFIX` | Key prefix inside the bucket, default `etl-dumps/` |

### Dump key format

```
{S3_DUMP_PREFIX}pov-{YYYY-MM-DD}.dump
```

Example: `etl-dumps/pov-2026-05-22.dump`

### Restoring from S3

```bash
# Download
aws s3 cp s3://<bucket>/etl-dumps/pov-<date>.dump pov-prod.dump

# Restore
pg_restore --clean --if-exists --no-owner --dbname "$PROD_DATABASE_URL" pov-prod.dump
```

### IAM policy (minimum)

```json
{
  "Effect": "Allow",
  "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:ListBucket"],
  "Resource": [
    "arn:aws:s3:::<bucket>",
    "arn:aws:s3:::<bucket>/etl-dumps/*"
  ]
}
```

## EC2 Deploy Flow

1. Clone the repo on EC2.
2. Create `.env` from `.env.example`.
3. Set:
   - `DATABASE_URL=postgresql://...rds.amazonaws.com:5432/...`
   - `REDIS_URL=redis://redis:6379/0` if Redis is local Docker
   - `DHAN_CLIENT_ID=...`
   - `DHAN_ACCESS_TOKEN=...`
   - `S3_DUMP_BUCKET=...` (optional but recommended for automated backups)
   - `AWS_ACCESS_KEY_ID=...` / `AWS_SECRET_ACCESS_KEY=...`
4. Run:

```bash
scripts/deploy_prod.sh
```

Production deploy applies schema updates, rebuilds API/worker containers, preserves RDS data,
and installs the daily ETL cron by default. It does not run historical bootstrap on EC2.

## GitHub Actions Deploy

The workflow is `.github/workflows/deploy-ec2.yml`. Required repository secrets:

- `EC2_HOST`
- `EC2_USER`
- `EC2_SSH_KEY`

Optional repository secrets:

- `EC2_PORT`, defaults to `22`
- `EC2_APP_DIR`, defaults to `/opt/pov-db`

Before the first deploy, create `$EC2_APP_DIR/shared/.env` on EC2 with the production RDS URL,
Redis URL, Dhan credentials, and app settings. The workflow uploads each commit into a release
directory, updates the `current` symlink, copies the shared `.env`, and runs `scripts/deploy_prod.sh`.

## Daily Operations

The cron job should run after NSE EOD bhavcopy is available:

```bash
scripts/install_daily_etl_cron.sh
```

Default: weekdays at `16:30 UTC` (10:00 PM IST). Override with `CRON_TIME` if needed.

The daily job:

- applies schema updates
- runs `daily_update.py` (includes result-event refresh, S3 dump upload, and old dump pruning if
  `S3_DUMP_BUCKET` is set)
- validates DB ranges/formulas
- clears Redis dashboard cache

To refresh upcoming result dates outside the weekday ETL window, run the events-only job:

```bash
docker compose -p pov-db -f docker-compose.prod.yml run --rm api python scripts/update_result_events.py
docker compose -p pov-db -f docker-compose.prod.yml exec -T redis redis-cli FLUSHDB
```

Use `--skip-nse` for a faster Yahoo-only upcoming earnings refresh.

## Scaling Triggers

Scale RDS before EC2 if:

- API dashboard queries slow down
- validation queries take too long
- RDS CPU or read IOPS stays high during daily update

Scale EC2 before RDS if:

- Docker builds are slow
- live worker plus API competes for CPU
- cron jobs overlap with market-hour live polling
