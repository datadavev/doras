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
    _L = get_logger()
    source_path = pathlib.Path(source_path)
    _L.debug("Source = %s", source_path)
    is_tar = False
    if source_path.is_file():
        is_tar = True
    broker = ctx.obj["broker"]
    broker.push_version(image_version, source_path=source_path, is_tar=is_tar)


@main.command("ls")
@click.pass_context
@click.option(
    "-v", "--version", "image_version", default="v1", help="Version", multiple=True
)
def list_packcge(ctx, image_version: list[str]):
    broker = ctx.obj["broker"]
    listing = broker.list_files_at_version(
        image_version,
        path="/",
    )
    for entry in listing:
        print(entry)


@main.command("get")
@click.pass_context
@click.argument("file_name")
@click.option("-v", "--version", "image_version", default="v1", help="Version")
def get_file_from_package(ctx, file_name, image_version):
    broker = ctx.obj["broker"]
    print(
        broker.read_file_from_version(
            [
                image_version,
            ],
            file_name,
        )
    )


if __name__ == "__main__":
    main()
