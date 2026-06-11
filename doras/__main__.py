import json
import logging
import pathlib

import click

import doras


def get_logger() -> logging.Logger:
    return logging.getLogger("doras")


@click.group()
@click.pass_context
@click.option("-L", "--log-level", default=None, help="Set the logging level (INFO)")
@click.option(
    "-u",
    "--user",
    "gh_user",
    default=None,
    envvar="GITHUB_USERNAME",
    help="GitHub username",
)
@click.option(
    "-t",
    "--token",
    "gh_token",
    default=None,
    envvar="GITHUB_TOKEN",
    help="GitHub personal access token",
)
@click.argument("package_name")
def main(ctx, log_level, gh_user, gh_token, package_name) -> None:
    # Set up logging
    if log_level is None:
        log_level = "DEBUG"
    logger = get_logger()
    numeric_level = getattr(logging, log_level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Invalid log level: {log_level}")
    logging.basicConfig(level=numeric_level)
    logger.setLevel(numeric_level)
    logger.debug(f"Logging initialized at level: {log_level}")
    ctx.ensure_object(dict)
    cache_dir = pathlib.Path(".doras").resolve()
    ctx.obj["broker"] = doras.Doras(
        registry_host="ghcr.io",
        repository=f"{gh_user}/{package_name}",
        username=gh_user,
        token=gh_token,
        cache_dir=cache_dir,
    )


@main.command(name="push")
@click.pass_context
@click.argument("source_path", type=click.Path())
@click.option("-v", "--version", "image_version", default="v1", help="Version")
def build_and_push(
    ctx,
    source_path: pathlib.Path,
    image_version: str,
):
    """Push an archive to ORAS.

    A new image is created if it doesn't exist, or a new version layer is added if the image already exists."""
    _L = get_logger()
    source_path = pathlib.Path(source_path)
    _L.debug("Source = %s", source_path)
    is_tar = False
    if source_path.is_file():
        is_tar = True
    broker = ctx.obj["broker"]
    broker.push_version(image_version, source_path=source_path, is_tar=is_tar)


@main.command("versions")
@click.pass_context
def list_packcge_versions(ctx):
    """List the versions of a package."""
    broker = ctx.obj["broker"]
    listing = broker.list_repository_versions()
    for entry in listing:
        print(entry)


@main.command("ls")
@click.pass_context
@click.option(
    "-v", "--version", "image_version", default=None, help="List contents for version."
)
def list_packcge(ctx, image_version: str | None):
    """List the contents of a package.

    Specify versions in chronological order. The listing will be representative
    of the most recent specified version.
    """
    _L = get_logger()
    broker = ctx.obj["broker"]
    versions = list(reversed(broker.list_repository_versions()))
    if image_version is not None:
        if image_version not in versions:
            _L.error("No version: '%'", image_version)
            return
        versions = versions[versions.index(image_version) :]
    listing = broker.list_files_at_version(
        versions,
        path="/",
    )
    for entry in listing:
        print(entry)


@main.command("get")
@click.pass_context
@click.argument("file_name")
@click.option(
    "-v", "--version", "image_version", default=None, help="List contents for version."
)
def get_file_from_package(ctx, file_name: str, image_version: str | None):
    """Retrieve an object from the the OCI image.

    The object is retrieved directly from the corresponding OCI layer using byte
    range requests.
    """
    _L = get_logger()
    broker = ctx.obj["broker"]
    versions = list(reversed(broker.list_repository_versions()))
    if image_version is not None:
        if image_version not in versions:
            _L.error("No version: '%'", image_version)
            return
        versions = versions[versions.index(image_version) :]
    print(
        broker.read_file_from_version(
            versions,
            file_name,
        )
    )


if __name__ == "__main__":
    main()
