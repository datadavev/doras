import base64
import io
import logging
import os
import pathlib
import stat
import tarfile
from contextlib import closing
from typing import Any, Dict, List
from urllib.parse import urlparse

import httpx
from oras.client import OrasClient
from PySquashfsImage.structure import sizeof

# Correct Top-Level Namespace Imports
from ratarmountcore.mountsource.compositing.union import UnionMountSource
from ratarmountcore.mountsource.factory import open_mount_source
from ratarmountcore.mountsource.formats.tar import SQLiteIndexedTar


def get_logger():
    return logging.getLogger("doras")


class OciRedirectAuth(httpx.Auth):
    """
    Custom HTTPX Authentication manager that provides the Bearer token
    ONLY when the request matches the primary OCI registry domain.
    """

    def __init__(self, token: str, registry_host: str):
        self.token = token
        self.registry_host = registry_host

    def auth_flow(self, request: httpx.Request) -> io.BytesIO:
        # Only inject the token if we are hitting the OCI registry directly
        if urlparse(str(request.url)).netloc == self.registry_host:
            request.headers["Authorization"] = f"Bearer {self.token}"
        yield request


class AuthenticatedRegistryStream(io.RawIOBase):
    """
    A loop-proof byte-range stream reader that resolves the GHCR storage redirect
    upfront and targets HTTP Range requests directly at the cloud storage provider.
    """

    def __init__(self, target_url: str, bearer_token: str):
        self.url = target_url
        self.token = bearer_token
        self.position = 0

        # 1. Resolve the actual underlying storage URL upfront without a Range header
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.oci.image.layer.v1.tar",
        }

        # We explicitly turn off automatic redirect following so we can catch the location header
        with httpx.Client(follow_redirects=False, timeout=15.0) as client:
            response = client.get(self.url, headers=headers)

            if response.status_code in (301, 302, 303, 307, 308):
                self.storage_url = response.headers["Location"]
            elif response.status_code == 200:
                # The registry didn't redirect us; it's serving the file directly (rare for GHCR)
                self.storage_url = self.url
            else:
                raise IOError(
                    f"Failed to resolve blob storage location from registry: {response.status_code} - {response.text}"
                )

        # 2. Extract total payload size directly from the resolved storage node
        with httpx.Client(follow_redirects=True, timeout=15.0) as client:
            # We use a HEAD request on the storage node (NO authorization headers needed)
            storage_response = client.head(self.storage_url)
            if storage_response.status_code != 200:
                # Fallback to a tiny GET if HEAD is rejected by the storage edge
                storage_response = client.get(
                    self.storage_url, headers={"Range": "bytes=0-0"}
                )

            if storage_response.status_code not in (200, 206):
                raise IOError(
                    f"Failed connecting to underlying storage node: {storage_response.status_code}"
                )

            self.total_size = (
                int(storage_response.headers.get("Content-Length", 0))
                if storage_response.status_code == 200
                else 1
            )

            # If we had to fall back to a 206 Range check to find the size, parse Content-Range
            if (
                storage_response.status_code == 206
                and "Content-Range" in storage_response.headers
            ):
                # Format: bytes 0-0/TOTAL_SIZE
                self.total_size = int(
                    storage_response.headers["Content-Range"].split("/")[-1]
                )

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self.position = offset
        elif whence == io.SEEK_CUR:
            self.position += offset
        elif whence == io.SEEK_END:
            self.position = self.total_size + offset
        return self.position

    def tell(self) -> int:
        return self.position

    def readinto(self, b) -> int:
        if self.position >= self.total_size:
            return 0  # EOF

        end_byte = min(self.position + len(b) - 1, self.total_size - 1)
        range_header = f"bytes={self.position}-{end_byte}"

        # 3. Fire the Range request directly at the storage node.
        # Absolutely NO Authorization headers are passed here, avoiding token collisions
        # and preventing GHCR from intercepting and looping the request.
        with httpx.Client(follow_redirects=True) as client:
            response = client.get(self.storage_url, headers={"Range": range_header})

            if response.status_code not in (200, 206):
                raise IOError(
                    f"Direct storage byte-range stream failed: {response.status_code} - {response.text}"
                )

            data = response.content
            b[: len(data)] = data
            self.position += len(data)
            return len(data)


class Doras:
    def __init__(
        self,
        registry_host: str,
        repository: str,
        username: str,
        token: str,
        cache_dir: str = "~/.cache/doras",
    ):
        """
        Initialize to use httpx for registry communication.
        """
        self.host = registry_host.strip("/")
        self.repo = repository.strip("/")
        self.username = username
        self.token = token

        self.cache_dir = pathlib.Path(
            cache_dir
        ).expanduser()  # .path.expanduser(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)

        # Initialize official ORAS client for pushing and pulling layers
        self.oras_client = OrasClient(hostname=self.host, insecure=False)
        self.oras_client.login(username=username, password=token)

    def _get_bearer_token(self, service: str, scope: str) -> str:
        """
        Dynamically requests an OAuth2 Bearer token from the registry's auth endpoint.
        """
        # Resolve the challenge endpoint (GHCR and Quay use slightly different auth servers)
        if "ghcr.io" in self.host:
            auth_url = "https://ghcr.io/token"
            params = {"service": "ghcr.io", "scope": scope}
        elif "quay.io" in self.host:
            auth_url = "https://quay.io/v2/auth"
            params = {"service": "quay.io", "scope": scope}
        else:
            # Fallback deduction for generic registries
            auth_url = f"https://{self.host}/v2/auth"
            params = {"service": self.host, "scope": scope}

        # Basic Auth is used *only* to request the bearer token
        auth_str = f"{self.username}:{self.token}"
        b64_auth = base64.b64encode(auth_str.encode()).decode()
        headers = {"Authorization": f"Basic {b64_auth}"}

        with httpx.Client(timeout=10.0) as client:
            response = client.get(auth_url, params=params, headers=headers)
            if response.status_code != 200:
                raise PermissionError(
                    f"OAuth2 Token exchange failed with registry: {response.text}"
                )

            return response.json().get("token")

    # def _get_authenticated_headers(self) -> Dict[str, str]:
    #     """Generates standard OCI basic auth headers for httpx and ratarmountcore."""
    #     auth_str = f"{self.username}:{self.token}"
    #     b64_auth = base64.b64encode(auth_str.encode()).decode()
    #     return {"Authorization": f"Basic {b64_auth}"}

    def _fetch_manifest(self, version_tag: str) -> Dict[str, Any]:
        """Fetches raw OCI manifest directly using httpx."""
        _L = get_logger()
        url = f"https://{self.host}/v2/{self.repo}/manifests/{version_tag}"
        scope = f"repository:{self.repo}:pull"
        bearer_token = self._get_bearer_token(service=self.host, scope=scope)

        headers = {
            "Accept": "application/vnd.oci.image.manifest.v1+json",
            "Authorization": f"Bearer {bearer_token}",
        }
        _L.debug("url = %s", url)

        with httpx.Client(timeout=15.0) as client:
            response = client.get(url, headers=headers)
            if response.status_code != 200:
                raise Exception(
                    f"Failed fetching manifest for tag '{version_tag}': {response.text}"
                )
            return response.json()

    def get_version_sources(
        self, version_tags: List[str], stream_data: bool = False
    ) -> List[Any]:
        """
        Resolves version layers.

        :param version_tags: Chronological list of tags (newest first)
        :param stream_data: If True, binds to the remote streaming network layer.
                            If False, loads ONLY the local SQLite metadata (super fast for listing!)
        """
        _L = get_logger()
        mount_sources = []

        for tag in version_tags:
            manifest = self._fetch_manifest(tag)

            tar_digest = None
            index_digest = None
            index_filename = None

            for layer in manifest.get("layers", []):
                title = layer.get("annotations", {}).get(
                    "org.opencontainers.image.title", ""
                )
                if title.endswith(".index.sqlite"):
                    index_digest = layer["digest"]
                    index_filename = f"{tag}_{title}"
                elif title.endswith(".tar"):
                    tar_digest = layer["digest"]

            if tar_digest is None or index_digest is None or index_filename is None:
                raise ValueError(f"Tag {tag} does not contain valid backup layers.")

            cached_index_path = str(self.cache_dir / index_filename)
            _L.debug("get_version_sources: cached_index_path = %s", cached_index_path)

            # Ensure the small SQLite index layer is physically cached locally
            if not os.path.exists(cached_index_path):
                _L.info(
                    "[%s] Index cache miss. Downloading index metadata via ORAS...", tag
                )
                self.oras_client.download_blob(
                    container=f"{self.host}/{self.repo}",
                    digest=index_digest,
                    outfile=cached_index_path,
                )

            if stream_data:
                # SCENARIO A: We are reading a file. Bind the index to our loop-proof network stream.
                remote_tar_url = (
                    f"https://{self.host}/v2/{self.repo}/blobs/{tar_digest}"
                )
                scope = f"repository:{self.repo}:pull"
                bearer_token = self._get_bearer_token(service=self.host, scope=scope)

                smart_stream = AuthenticatedRegistryStream(remote_tar_url, bearer_token)
                source = open_mount_source(
                    smart_stream,
                    index_path=cached_index_path,
                    write_index=False,
                )
            else:
                # SCENARIO B: We just want to list files or find metadata.
                source = open_mount_source(cached_index_path, write_index=False)

            mount_sources.append(source)

        return mount_sources

    def list_files_at_version(
        self, target_versions: List[str], path: str = "/", recursive: bool = True
    ) -> List[str]:
        """Creates a cumulative union view using LOCAL metadata cache only. (Instantaneous)"""
        # Pass stream_data=False so it hits 0 remote tar network streams
        _L = get_logger()
        sources = self.get_version_sources(target_versions, stream_data=False)

        union = UnionMountSource(sources)
        # files = union.list(path)
        # return list(files.keys()) if isinstance(files, dict) else list(files)
        base_path = path if path.endswith("/") else f"{path}/"
        if not base_path.startswith("/"):
            base_path = f"/{base_path}"

        if not recursive:
            # Fallback to single-level directory listing if requested
            files = union.list(base_path)
            return list(files.keys()) if isinstance(files, dict) else list(files)

        all_contents = []

        def _walk(current_path: str):
            # Fetch contents of the current directory layer
            _L.debug("_walk : current_path = %s", current_path)
            contents = union.list(current_path)
            _L.debug("_walk : contents = %s", contents)
            if not isinstance(contents, dict):
                return

            for item_name, file_info in contents.items():
                # Construct clean absolute path representation
                _L.debug("_walk : item_name, file_info = %s, %s", item_name, file_info)
                sep = "" if current_path.endswith("/") else "/"
                full_item_path = f"{current_path}{sep}{item_name}"

                all_contents.append(full_item_path)

                # Is the entry a file or a folder?
                # See MountSourceFileSystem._file_info_to_dict
                is_dir = stat.S_ISDIR(file_info.mode)

                if is_dir:
                    # Recursively walk down into the subfolder
                    _walk(full_item_path)

        try:
            _walk(base_path)
            return all_contents
        except Exception as e:
            print(f"Error recursively walking index: {e}")
            return []

    # def read_file_from_version(
    #     self, target_versions: List[str], file_path: str
    # ) -> bytes:
    #     """Streams only the required byte chunks for a specific file across the network."""
    #     # Pass stream_data=True because we are actively extracting file content payloads
    #     sources = self.get_version_sources(target_versions, stream_data=True)

    #     union = UnionMountSource(sources)
    #     absolute_path = file_path if file_path.startswith("/") else f"/{file_path}"
    #     file_info = union.lookup(absolute_path)
    #     if file_info is not None:
    #         with union.open(file_info) as f:
    #             return f.read()
    #     raise KeyError(f"File not found for {absolute_path}")

    def read_file_from_version(
        self, target_versions: List[str], file_path: str
    ) -> bytes:
        """
        Extracts a file by reading the byte offsets from the local cache,
        then making a single direct HTTP Range request against the ORAS storage node.
        """
        _L = get_logger()
        # 1. Open using LOCAL metadata only (Instant, no network calls)
        sources = self.get_version_sources(target_versions, stream_data=False)

        tar_offset = None
        file_size = None
        target_tag = None

        # Format the path for ratarmount lookup consistency
        absolute_path = file_path if file_path.startswith("/") else f"/{file_path}"

        union = UnionMountSource(sources)
        # Look up the file in the union tree
        file_info = union.lookup(absolute_path)
        if not file_info:
            raise FileNotFoundError(
                f"File '{file_path}' not found in the selected version stack."
            )

        # Extract the raw file dimensions from the index
        file_size = file_info.size

        # If the file size is 0 (like an empty file or directory), return immediately
        if file_size == 0:
            return b""

        # Dig into the union layers to find which specific archive source owns this file
        # and extract its exact starting byte offset in the raw TAR stream.
        for source in sources:
            if hasattr(source, "lookup"):
                _L.debug("read_file_from_version: absolute_path = %s", absolute_path)
                local_info = source.lookup(absolute_path)
                _L.debug("read_file_from_version:local_info = %s", local_info)
                if local_info:
                    # 'offset' in ratarmount's SQLite for uncompressed tars
                    # represents the exact byte position where the raw file data begins.
                    tar_offset = local_info.userdata[0].offset

                    # Identify which version tag this source came from
                    # by parsing its index file name
                    _L.debug("read_file_from_version:source = %s", source)
                    idx_name = os.path.basename(source.indexFilePath)
                    target_tag = idx_name.split("_")[0]
                    break

        if tar_offset is None or target_tag is None:
            raise ValueError(
                f"Could not resolve the physical TAR offset for '{file_path}'."
            )

        _L.info(
            "[%s] Found file metadata locally! Offset: %s, Size: %s bytes.",
            target_tag,
            tar_offset,
            file_size,
        )
        _L.info("Fetching bytes directly from remote storage layer...")

        # 2. Fetch the manifest for the specific version that owns the file
        manifest = self._fetch_manifest(target_tag)
        tar_digest = None
        for layer in manifest.get("layers", []):
            if (
                layer.get("annotations", {})
                .get("org.opencontainers.image.title", "")
                .endswith(".tar")
            ):
                tar_digest = layer["digest"]
                break

        if not tar_digest:
            raise ValueError(
                f"Could not find the target TAR layer for version {target_tag}"
            )

        # 3. Resolve the direct GHCR/Quay signed storage backend URL (No Range header yet)
        remote_blob_url = f"https://{self.host}/v2/{self.repo}/blobs/{tar_digest}"
        scope = f"repository:{self.repo}:pull"
        bearer_token = self._get_bearer_token(service=self.host, scope=scope)

        headers = {
            "Authorization": f"Bearer {bearer_token}",
            "Accept": "application/vnd.oci.image.layer.v1.tar",
        }

        with httpx.Client(follow_redirects=False, timeout=15.0) as client:
            response = client.get(remote_blob_url, headers=headers)
            if response.status_code in (301, 302, 303, 307, 308):
                storage_url = response.headers["Location"]
            elif response.status_code == 200:
                storage_url = remote_blob_url
            else:
                raise IOError(
                    f"Failed to resolve blob storage redirect: {response.status_code}"
                )

        # 4. Fire a single, targeted HTTP Range request directly at the storage provider
        # Calculate the exact byte window: start_at_offset to (offset + size - 1)
        end_byte = tar_offset + file_size - 1
        range_header = f"bytes={tar_offset}-{end_byte}"

        with httpx.Client(follow_redirects=True, timeout=30.0) as client:
            data_response = client.get(storage_url, headers={"Range": range_header})

            if data_response.status_code not in (200, 206):
                raise IOError(
                    f"Direct byte extraction failed with status {data_response.status_code}"
                )

            return data_response.content

    def push_version(self, version_tag: str, source_path: str, is_tar: bool = False):
        """
        Generates a ratarmount index for a backup target and pushes both the
        uncompressed TAR layer and SQLite index layer as a new OCI image version.
        """
        _L = get_logger()
        tar_path = source_path
        tmp_tar_created = False

        try:
            if not is_tar:
                if not os.path.isdir(source_path):
                    raise ValueError(
                        f"source_path '{source_path}' must be a directory if is_tar=False"
                    )

                tar_path = self.cache_dir / f"backup_{version_tag}.tar"
                _L.info(
                    "Creating uncompressed TAR archive from directory: %s...",
                    source_path,
                )
                with tarfile.open(tar_path, "w") as tar:
                    tar.add(source_path, arcname=".")
                tmp_tar_created = True

            if not os.path.exists(tar_path):
                raise FileNotFoundError(f"Tar file not found at: {tar_path}")

            tar_path = pathlib.Path(tar_path).resolve()
            index_path = tar_path.with_name(f"{tar_path.name}.index.sqlite")

            # index_path = os.path.join(
            #    self.cache_dir, f"{version_tag}_backup.index.sqlite"
            # )
            if os.path.exists(index_path):
                os.remove(index_path)

            _L.info("Generating ratarmount index sidecar for %s...", tar_path)

            with closing(
                SQLiteIndexedTar(
                    tarFileName=str(tar_path),
                    indexFilePath=str(index_path),
                    writeIndex=True,
                    clearIndexCache=True,
                )
            ) as source:
                _L.debug(source)

            tar_layer_title = f"backup_{version_tag}.tar"
            index_layer_title = f"backup_{version_tag}.index.sqlite"

            final_tar_upload = os.path.join(self.cache_dir, tar_layer_title)
            final_idx_upload = os.path.join(self.cache_dir, index_layer_title)

            if tmp_tar_created:
                os.rename(tar_path, final_tar_upload)
            else:
                import shutil

                shutil.copy2(tar_path, final_tar_upload)

            os.rename(index_path, final_idx_upload)

            _L.info(
                "Uploading files to %s/%s:%s via ORAS...",
                self.host,
                self.repo,
                version_tag,
            )

            upload_files = [
                f"{final_tar_upload}:application/vnd.oci.image.layer.v1.tar",
                f"{final_idx_upload}:application/vnd.custom.backup.index.sqlite",
            ]

            self.oras_client.push(
                target=f"{self.host}/{self.repo}:{version_tag}",
                files=upload_files,
                manifest_annotations={
                    "org.opencontainers.image.description": f"Incremental backup version {version_tag}"
                },
            )
            _L.info("Successfully pushed version '%s' to registry.", version_tag)

        finally:
            if tmp_tar_created and os.path.exists(final_tar_upload):
                os.remove(final_tar_upload)
