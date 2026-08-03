"""Compress objects that arrive in an S3 bucket into ZIP archives.

This module is the entry point of a Lambda function invoked by
``s3:ObjectCreated:*`` notifications. For every record in the event it
streams the object into a ZIP archive, uploads the archive to the same
bucket under a separate prefix, and deletes the original object once
the upload has succeeded.

The archive is written under a different prefix from the source object
on purpose. Writing it back under the source prefix would trigger the
same notification again, and the function would compress its own
output indefinitely.

Environment variables
---------------------
SOURCE_PREFIX
    Key prefix the S3 notification is filtered on. Keys outside it are
    ignored. Defaults to ``incoming/``.
ARCHIVE_PREFIX
    Key prefix the archives are written to. It must not sit inside
    ``SOURCE_PREFIX``. Defaults to ``archive/``.
COMPRESSION_LEVEL
    DEFLATE level from 0 (store only) to 9 (smallest). Defaults to 6.
LOG_LEVEL
    Standard logging level name. Defaults to ``INFO``.
"""

import logging
import os
import zipfile
from tempfile import SpooledTemporaryFile
from typing import Any, BinaryIO
from urllib.parse import unquote_plus

import boto3

LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

SOURCE_PREFIX = os.environ.get("SOURCE_PREFIX", "incoming/")
ARCHIVE_PREFIX = os.environ.get("ARCHIVE_PREFIX", "archive/")
COMPRESSION_LEVEL = int(os.environ.get("COMPRESSION_LEVEL", "6"))

if ARCHIVE_PREFIX.startswith(SOURCE_PREFIX) or SOURCE_PREFIX.startswith(
    ARCHIVE_PREFIX
):
    raise ValueError(
        "SOURCE_PREFIX and ARCHIVE_PREFIX overlap, which would make the "
        "function compress its own output in a loop: "
        f"{SOURCE_PREFIX!r} and {ARCHIVE_PREFIX!r}"
    )

#: Number of bytes read from S3 and fed into the compressor at a time.
#: Streaming in chunks keeps memory use independent of object size.
CHUNK_SIZE = 1024 * 1024

#: Archives smaller than this are held in memory; larger ones spill to
#: the function's ephemeral disk instead of growing the heap.
SPOOL_MAX_BYTES = 32 * 1024 * 1024

#: Cached S3 client. It is built on first use rather than at import, so
#: that the module can be imported without AWS credentials, and is then
#: reused by every invocation served by the same execution environment.
_S3_CLIENT = None


def get_s3_client() -> Any:
    """Return the S3 client, creating it on first use.

    Returns:
        The cached boto3 S3 client for this execution environment.
    """
    global _S3_CLIENT
    if _S3_CLIENT is None:
        _S3_CLIENT = boto3.client("s3")
    return _S3_CLIENT


def build_archive_key(source_key: str) -> str:
    """Return the key the archive of ``source_key`` is written to.

    The path below the source prefix is preserved so that the archive
    can be traced back to its original, and ``.zip`` is appended.

    Args:
        source_key: Key of the object that triggered the invocation.

    Returns:
        The destination key, below ``ARCHIVE_PREFIX``.
    """
    relative_key = source_key[len(SOURCE_PREFIX):]
    return f"{ARCHIVE_PREFIX}{relative_key}.zip"


def compress_to_archive(body: BinaryIO, entry_name: str) -> BinaryIO:
    """Stream ``body`` into a ZIP archive and return it rewound.

    Args:
        body: Readable stream of the object's bytes.
        entry_name: Name the object is stored under inside the archive.

    Returns:
        A file object positioned at the start of the finished archive.
    """
    archive_file = SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES)
    with zipfile.ZipFile(
        archive_file,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=COMPRESSION_LEVEL,
    ) as archive:
        with archive.open(entry_name, mode="w", force_zip64=True) as entry:
            for chunk in iter(lambda: body.read(CHUNK_SIZE), b""):
                entry.write(chunk)
    archive_file.seek(0)
    return archive_file


def archive_object(bucket: str, key: str) -> str | None:
    """Compress one object, upload the archive and delete the original.

    The original is deleted only after the upload has returned
    successfully, so a failure at any earlier point leaves the source
    object in place and the invocation can be retried safely.

    Args:
        bucket: Bucket holding the object.
        key: Key of the object to compress.

    Returns:
        The key of the archive that was written, or ``None`` if the
        object was outside the source prefix and was skipped.
    """
    if not key.startswith(SOURCE_PREFIX):
        LOGGER.warning("Skipping key outside the source prefix: %s", key)
        return None

    # Creating a folder in the S3 console creates a real zero-byte
    # object whose key ends in "/". It matches the notification filter
    # like any other object, so it has to be skipped explicitly or the
    # function archives the placeholder and then deletes it, making the
    # folder disappear from the console.
    if key.endswith("/"):
        LOGGER.info("Skipping folder placeholder: %s", key)
        return None

    s3_client = get_s3_client()
    archive_key = build_archive_key(key)
    entry_name = key[len(SOURCE_PREFIX):]

    source = s3_client.get_object(Bucket=bucket, Key=key)
    with source["Body"] as body:
        archive_file = compress_to_archive(body, entry_name)

    with archive_file:
        s3_client.upload_fileobj(archive_file, bucket, archive_key)

    s3_client.delete_object(Bucket=bucket, Key=key)
    LOGGER.info("Archived s3://%s/%s to %s", bucket, key, archive_key)
    return archive_key


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Handle an S3 ``ObjectCreated`` notification.

    Parameters
    ----------
    event: dict, required
        S3 Event Notification Format

    context: object, required
        Lambda Context runtime methods and attributes

    Returns
    ------
    dict: the archive keys written by this invocation.

    Exceptions are logged and re-raised rather than ignored. Failing
    the invocation lets Lambda retry it, and leaves the source object
    in place until an attempt succeeds.
    """
    archived: list[str] = []

    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        # S3 delivers keys URL-encoded, with spaces written as "+".
        key = unquote_plus(record["s3"]["object"]["key"])
        try:
            archive_key = archive_object(bucket, key)
        except Exception:
            LOGGER.exception("Failed to archive s3://%s/%s", bucket, key)
            raise
        if archive_key is not None:
            archived.append(archive_key)

    return {"archived": archived, "count": len(archived)}
