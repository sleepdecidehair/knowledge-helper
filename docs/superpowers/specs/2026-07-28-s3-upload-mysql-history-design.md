# S3 Upload and Shared MySQL History Design

## Goal

Restore S3-backed file uploads and make MySQL the shared, authoritative conversation store for the local application and the test server, while retaining each runtime's JSON backup.

## Storage design

The server backend reaches its host MySQL through the Docker host-gateway alias `host.docker.internal`; its `.env` will set `MYSQL_HOST=host.docker.internal`. The local application uses the same MySQL connection settings through its ignored `.env`. JSON conversation files remain local recovery copies, not a second source of truth.

The existing `conversations` and `messages` tables remain the canonical relational representation. Both receive an additive `payload_json` column to retain full conversation and message objects, including attachments, sources, agent trace data, and context state. A `conversation_store_meta` marker records completion of the one-time migration.

## Migration

On the first successful MySQL startup, load the current MySQL rows and the local JSON backup, merge conversation IDs and message IDs without deleting either source, then atomically replace the relational rows and write the migration marker. Once marked, later startups load MySQL only and write JSON as a backup. A MySQL outage continues to fall back to the JSON backup.

## Upload repair

S3 upload calls omit the unsupported boto3 `ContentSHA256` request parameter. The SDK continues to sign the request; the application still records its own content hash for asset identity.

## Verification

Tests cover the S3 request arguments, JSON/MySQL merge behavior, migration-marker behavior, and fallback behavior. Deployment verifies healthy containers, successful MySQL access and migrated row counts, retained JSON backup, and a small authenticated S3 write/delete probe.
