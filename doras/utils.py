import datetime
import logging
import os
import pathlib
import subprocess

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
