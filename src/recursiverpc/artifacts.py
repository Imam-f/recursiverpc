"""File-backed transport artifacts with bounded reads and verified reuse."""
import hashlib
from pathlib import Path
import tempfile
import shutil
import weakref

CHUNK_SIZE = 1024 * 1024

class Artifact:
    def __init__(self):
        directory = tempfile.mkdtemp(prefix="recursiverpc-artifact-")
        self.path = str(Path(directory) / "archive.zip")
        self._cleanup = weakref.finalize(self, shutil.rmtree, directory, ignore_errors=True)

    def read(self, offset, size=CHUNK_SIZE):
        with open(self.path, "rb") as stream:
            stream.seek(offset)
            return stream.read(min(size, CHUNK_SIZE))

    def close(self):
        self._cleanup()

    def digest(self):
        digest = hashlib.sha256()
        with open(self.path, "rb") as stream:
            for block in iter(lambda: stream.read(CHUNK_SIZE), b""):
                digest.update(block)
        return digest.hexdigest()

def upload_artifact(remote, directory, name, artifact):
    if isinstance(artifact, bytes):
        return remote.write_file(directory, name, artifact)
    return remote.write_artifact(directory, name, artifact)
