# ORAS Experiment

This is an experiment leveraging the GHCR ORAS repository to store datasets in a way where individual
files within an OCI layer are individually addressable.

```
Usage: doras [OPTIONS] PACKAGE_NAME COMMAND [ARGS]...

Options:
  -L, --log-level TEXT  Set the logging level (INFO)
  -u, --user TEXT       GitHub username
  -t, --token TEXT      GitHub personal access token
  --help                Show this message and exit.

Commands:
  get   Retrieve an object from the the OCI image.
  ls    List the contents of a package.
  push  Push an archive to ORAS.
```
