import logging
import os
from urllib.parse import urlparse

import fsspec
import httpx
from fsspec.spec import AbstractBufferedFile, AbstractFileSystem

from doras import utils


def get_logger():
    return logging.getLogger("doras.dfsspec")


# Implement the Custom Sub-File Stream for fsspec
class OasRegistryBufferedFile(AbstractBufferedFile):
    """
    A file-like object that fsspec requires to read data chunks
    from a specific target cloud storage node.
    """

    def __init__(
        self, fs, path, mode="rb", block_size: str | None = "default", **kwargs
    ):
        # Resolve the direct, un-throttled signed AWS/Azure storage URL
        # from the parent filesystem before beginning chunk reads
        self.storage_url = fs._resolve_storage_url(path)

        if block_size is None:
            block_size = "default"

        # Initialize standard fsspec buffering internals
        super().__init__(fs, path, mode, block_size, **kwargs)

    def _fetch_range(self, start, end):
        """Called automatically by fsspec caching mechanisms."""
        range_header = f"bytes={start}-{end - 1}"

        # Pull raw chunks directly from the storage provider (No loop, no OCI auth needed)
        with httpx.Client(follow_redirects=True) as client:
            response = client.get(self.storage_url, headers={"Range": range_header})
            if response.status_code not in (200, 206):
                raise IOError(f"Byte-range retrieval failed: {response.status_code}")
            return response.content


class OrasRegistryFileSystem(AbstractFileSystem):
    """
    Custom fsspec implementation for streaming layers directly
    out of an OCI distribution registry.
    """

    protocol = "doras"
    caching_options = {"cache_type": "readahead"}

    def __init__(
        self, username: str | None = None, token: str | None = None, *args, **kwargs
    ):
        super().__init__(*args, **kwargs)
        _L = get_logger()
        self.username = username or os.environ.get("ORAS_USERNAME")
        self.token = token or os.environ.get("ORAS_TOKEN")
        self._token_cache = {}
        _L.debug("OrasRegistryFileSystem username = %s", self.username)

    def _get_bearer_token(self, host: str, repo: str) -> str:
        """Dynamically builds scoped OCI authentication handles."""
        cache_key = f"{host}/{repo}"
        if cache_key in self._token_cache:
            return self._token_cache[cache_key]

        scope = f"repository:{repo}:pull"
        return utils.get_bearer_token(host, self.username, self.token, scope)

    def _resolve_storage_url(self, path: str) -> str:
        """Transforms an OCI blob URL into its underlying cloud storage link."""
        # path is expected as: "host/repository/blobs/sha256:xxxx"
        _L = get_logger()
        _L.debug("_resolve_storage_url path = %s", path)

        # Normalize the path string
        # Handle cases where path arrives as "oras://ghcr.io/..." vs "ghcr.io/..."
        if "://" not in path:
            # urlparse requires a scheme to properly isolate the netloc/host engine
            parsed = urlparse(f"doras://{path.lstrip('/')}")
        else:
            parsed = urlparse(path)

        host = parsed.netloc
        full_path = parsed.path.strip(
            "/"
        )  # e.g., "datadavev/daily/blobs/sha256:3baa0..."

        # Extract the blob digest and compile the underlying repository structure
        path_parts = full_path.split("/")

        if len(path_parts) < 3:
            raise ValueError(
                f"Inbound path structure is missing critical OCI parameters: {path}"
            )

        # The last element is always the blob digest (sha256:xxxx)
        blob_digest = path_parts[-1]

        # The elements preceding '/blobs/...' make up the repository namespace
        # We find where 'blobs' is to cleanly slice out the repo namespace safely
        try:
            blobs_index = path_parts.index("blobs")
            repo = "/".join(path_parts[:blobs_index])
        except ValueError:
            # Fallback deduction if 'blobs' keyword is omitted or positioned weirdly
            repo = "/".join(path_parts[:-2])

        # Formulate the mathematically sound compliant OCI endpoint
        oci_url = f"https://{host}/v2/{repo}/blobs/{blob_digest}"
        _L.debug("_resolve_storage_url oci_url = %s", oci_url)
        token = self._get_bearer_token(host, repo)

        with httpx.Client(follow_redirects=False) as client:
            res = client.get(oci_url, headers={"Authorization": f"Bearer {token}"})
            if res.status_code in (301, 302, 303, 307, 308):
                return res.headers["Location"]
            return oci_url

    def _open(
        self,
        path,
        mode="rb",
        block_size=None,
        autocommit=True,
        cache_options=None,
        **kwargs,
    ):
        """Instantiates the loop-proof data channel."""
        return OasRegistryBufferedFile(
            self, path, mode=mode, block_size=block_size, **kwargs
        )

    def info(self, path, **kwargs):
        """Returns metadata sizing properties so ratarmount knows the TAR scope boundary."""
        storage_url = self._resolve_storage_url(path)
        with httpx.Client(follow_redirects=True) as client:
            res = client.head(storage_url)
            # If head is restricted, read bytes 0-0 via GET to extract actual file boundaries
            if res.status_code != 200:
                res = client.get(storage_url, headers={"Range": "bytes=0-0"})
                if "Content-Range" in res.headers:
                    size = int(res.headers["Content-Range"].split("/")[-1])
                else:
                    size = 1
            else:
                size = int(res.headers.get("Content-Length", 0))

        return {"name": path, "size": size, "type": "file"}


# link backend class to the fsspec global network dictionary
fsspec.register_implementation("doras", OrasRegistryFileSystem, clobber=True)
