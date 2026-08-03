"""Unit tests for the object_compress Lambda handler.

The S3 client is replaced by an in-memory double rather than a mocking
library, so the tests state exactly which S3 operations the handler is
expected to call and in what order.
"""

import importlib
import io
import zipfile

import pytest

from object_compress import app

BUCKET = "bucket-object-archive-123456789012-ap-southeast-5"
SOURCE_KEY = "incoming/2026/08/03/sample clip.json"
ARCHIVE_KEY = "archive/2026/08/03/sample clip.json.zip"
ENTRY_NAME = "2026/08/03/sample clip.json"

# Compressible, so the assertions on size are meaningful.
PAYLOAD = b'{"asset": "clip", "notes": "' + b"lorem ipsum " * 4000 + b'"}'


class UploadFailed(Exception):
    """Raised by the double to simulate a failing PutObject."""


class FakeS3Client:
    """In-memory stand-in for the boto3 S3 client.

    Only the three operations the handler uses are implemented. The
    keyword names match boto3's, which is why they are capitalised.
    """

    def __init__(self, fail_upload: bool = False) -> None:
        """Store an empty bucket and record every call made to it."""
        self.objects: dict[tuple[str, str], bytes] = {}
        self.calls: list[str] = []
        self.fail_upload = fail_upload

    def get_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803
        """Return the stored bytes wrapped in a readable stream."""
        self.calls.append("get_object")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def upload_fileobj(self, fileobj, bucket: str, key: str) -> None:
        """Store the uploaded stream, or fail if asked to."""
        self.calls.append("upload_fileobj")
        if self.fail_upload:
            raise UploadFailed("PutObject rejected the archive")
        self.objects[(bucket, key)] = fileobj.read()

    def delete_object(self, Bucket: str, Key: str) -> None:  # noqa: N803
        """Remove the stored object."""
        self.calls.append("delete_object")
        del self.objects[(Bucket, Key)]


@pytest.fixture()
def s3_client(monkeypatch):
    """Install an in-memory S3 double holding one source object."""
    client = FakeS3Client()
    client.objects[(BUCKET, SOURCE_KEY)] = PAYLOAD
    monkeypatch.setattr(app, "_S3_CLIENT", client)
    return client


@pytest.fixture()
def s3_event():
    """Generate an S3 ObjectCreated event for the source object."""
    return {
        "Records": [
            {
                "eventVersion": "2.1",
                "eventSource": "aws:s3",
                "eventName": "ObjectCreated:Put",
                "s3": {
                    "bucket": {"name": BUCKET},
                    # URL-encoded exactly as S3 delivers it.
                    "object": {
                        "key": "incoming/2026/08/03/sample+clip.json",
                        "size": len(PAYLOAD),
                    },
                },
            }
        ]
    }


def test_lambda_handler_archives_and_deletes_the_original(
    s3_event, s3_client
):
    """The archive is written and the source object is removed."""
    result = app.lambda_handler(s3_event, "")

    assert result == {"archived": [ARCHIVE_KEY], "count": 1}
    assert (BUCKET, ARCHIVE_KEY) in s3_client.objects
    assert (BUCKET, SOURCE_KEY) not in s3_client.objects


def test_lambda_handler_uses_exactly_three_s3_calls(s3_event, s3_client):
    """One read, one write, one delete, in that order.

    Request count is the dominant cost driver at high object volumes,
    so an extra call per object is a regression worth failing on.
    """
    app.lambda_handler(s3_event, "")

    assert s3_client.calls == [
        "get_object",
        "upload_fileobj",
        "delete_object",
    ]


def test_archive_round_trips_to_the_original_bytes(s3_event, s3_client):
    """The archive holds one entry, named and byte-identical."""
    app.lambda_handler(s3_event, "")

    raw = s3_client.objects[(BUCKET, ARCHIVE_KEY)]
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert archive.namelist() == [ENTRY_NAME]
        assert archive.read(ENTRY_NAME) == PAYLOAD
    assert len(raw) < len(PAYLOAD)


def test_plus_in_the_key_is_decoded_to_a_space(s3_event, s3_client):
    """S3 encodes spaces as '+', so the handler must decode them."""
    app.lambda_handler(s3_event, "")

    assert (BUCKET, ARCHIVE_KEY) in s3_client.objects


def test_key_outside_the_source_prefix_is_skipped(s3_client):
    """Nothing is read, written or deleted for an unrelated key."""
    assert app.archive_object(BUCKET, "archive/already.zip") is None
    assert s3_client.calls == []


@pytest.mark.parametrize(
    "folder_key", ["incoming/", "incoming/2026/", "incoming/2026/08/"]
)
def test_folder_placeholders_are_skipped(folder_key, s3_client):
    """A console-created folder must not be archived or deleted.

    S3 stores it as a real zero-byte object under the watched prefix,
    so without an explicit guard the function writes archive/.zip and
    then deletes the folder out from under the console.
    """
    s3_client.objects[(BUCKET, folder_key)] = b""

    assert app.archive_object(BUCKET, folder_key) is None
    assert s3_client.calls == []
    assert (BUCKET, folder_key) in s3_client.objects


def test_original_is_kept_when_the_upload_fails(s3_event, monkeypatch):
    """A failed upload must not delete the only copy of the object."""
    client = FakeS3Client(fail_upload=True)
    client.objects[(BUCKET, SOURCE_KEY)] = PAYLOAD
    monkeypatch.setattr(app, "_S3_CLIENT", client)

    with pytest.raises(UploadFailed):
        app.lambda_handler(s3_event, "")

    assert (BUCKET, SOURCE_KEY) in client.objects
    assert "delete_object" not in client.calls


def test_every_record_in_a_batch_is_archived(s3_event, s3_client):
    """S3 can deliver more than one record per invocation."""
    second_key = "incoming/2026/08/03/second.json"
    s3_client.objects[(BUCKET, second_key)] = PAYLOAD
    s3_event["Records"].append(
        {"s3": {"bucket": {"name": BUCKET}, "object": {"key": second_key}}}
    )

    result = app.lambda_handler(s3_event, "")

    assert result["count"] == 2
    assert result["archived"][1] == "archive/2026/08/03/second.json.zip"


def test_build_archive_key_preserves_the_path():
    """The path below the source prefix survives into the archive key."""
    assert app.build_archive_key(SOURCE_KEY) == ARCHIVE_KEY


def test_overlapping_prefixes_are_rejected_on_import(monkeypatch):
    """An archive prefix inside the source prefix is an import error.

    That configuration would make each archive trigger the function
    again, so it has to fail at start-up rather than in production.
    """
    monkeypatch.setenv("ARCHIVE_PREFIX", "incoming/zips/")
    with pytest.raises(ValueError, match="overlap"):
        importlib.reload(app)

    monkeypatch.undo()
    importlib.reload(app)
