import base64
import datetime
import functools
import logging
import os
import pathlib
import subprocess

import httpx

TAR_COMMAND = "gtar"


def get_logger():
    return logging.getLogger("doras.utils")


def create_incremental_tar(
    source_dir: pathlib.Path, backup_dir: pathlib.Path, snapshot_file: str | None
):
    """
    Creates incremental tar backups leveraging GNU tar's snapshot tracking.
    """
    _L = get_logger()
    # Ensure backup directory exists
    backup_dir.mkdir(exist_ok=True)

    if snapshot_file is None:
        snapshot_file = "backup.snar"

    # Generate unique archive name based on the current timestamp
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%d_%H-%M-%S"
    )
    archive_name = f"backup-{timestamp}.tar.gz"
    archive_path = backup_dir / archive_name

    # Build the GNU tar command
    # -c: create, -v: verbose, -z: gzip, -f: file path, -g: listed-incremental
    command = [
        TAR_COMMAND,
        "-cvzf",
        str(archive_path),
        "-g",
        snapshot_file,
        "-C",
        str(source_dir.parent),  # Changes directory to avoid saving absolute paths
        source_dir.name,
    ]

    try:
        _L.debug("Running backup command: %s", " ".join(command))
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        _L.debug("Result = %s", result)
        _L.info("Backup successfully saved to: %s", archive_path)
        return archive_path
    except subprocess.CalledProcessError as e:
        _L.error("Backup failed: %s", e.stderr)
        raise


@functools.lru_cache(maxsize=6)
def get_bearer_token(
    host: str, username: str, token: str, scope: str, timeout: float = 10.0
) -> str:
    """
    Dynamically requests an OAuth2 Bearer token from the registry's auth endpoint.
    """
    # Resolve the challenge endpoint (GHCR and Quay use slightly different auth servers)
    _L = get_logger()
    _L.debug("get_bearer_token: host=%s username=%s scope=%s", host, username, scope)
    if "ghcr.io" in host:
        auth_url = "https://ghcr.io/token"
        params = {"service": "ghcr.io", "scope": scope}
    elif "quay.io" in host:
        auth_url = "https://quay.io/v2/auth"
        params = {"service": "quay.io", "scope": scope}
    else:
        # Fallback deduction for generic registries
        auth_url = f"https://{host}/v2/auth"
        params = {"service": host, "scope": scope}

    # Basic Auth is used *only* to request the bearer token
    auth_str = f"{username}:{token}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()
    headers = {"Authorization": f"Basic {b64_auth}"}
    with httpx.Client(timeout=timeout) as client:
        response = client.get(auth_url, params=params, headers=headers)
        if response.status_code != 200:
            raise PermissionError(
                f"OAuth2 Token exchange failed with registry: {response.text}"
            )
        return response.json().get("token")
