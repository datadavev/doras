# ORAS Experiment

This is an experiment leveraging the GHCR ORAS repository to store datasets such that individual
files within an OCI layer are individually addressable.

```
Usage: doras [OPTIONS] PACKAGE_NAME COMMAND [ARGS]...

Options:
  -L, --log-level TEXT  Set the logging level (INFO)
  -u, --user TEXT       GitHub username
  -t, --token TEXT      GitHub personal access token
  -h, --host TEXT       ORAS registry host
  --help                Show this message and exit.

Commands:
  get       Retrieve an object from the the OCI image.
  ls        List the contents of a package.
  mount     Mount the package as a file system.
  push      Push an archive to ORAS.
  versions  List the versions of a package.
```

The approach adds a content index as an sqlite file to enable a client to determine the
byte offsets of individual files within an image. This works for byte range offsets within
individual files as well. 

There's a fair bit of overhead which impacts performance especially when using the file system
mount mechanism. There is a lot of room for performance improvement by tweaking the internals 
of the mount functionality through index caching.
