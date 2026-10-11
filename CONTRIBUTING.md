# Contributing

Run local operations through the root `Makefile`. Never commit `.env`, credentials,
OpenCode sessions, or Docker volume contents.

## Run tests

Run the dependency-free unit tests with:

```sh
make test
```

The tests use Python's standard library and do not require credentials, Docker, or
access to Telegram or an LLM.

## Draft a release

After the release changes are merged into `main`, checks have passed, and the version
has been approved, create and push an annotated tag, then create a GitHub draft:

```sh
git switch main
git pull --ff-only
VERSION='v0.X.Y' # replace with the approved version, e.g. v0.1.0
git tag -a "$VERSION" -m "Release $VERSION"
git push origin "$VERSION"
gh release create "$VERSION" --draft --verify-tag --title "$VERSION" --generate-notes
```

`--verify-tag` requires the tag to have been pushed; `--draft` does not publish the
release. To provide hand-written notes, replace `--generate-notes` with
`--notes-file release-notes.md`. Review the draft before publishing. Never attach
`.env`, credentials, sessions, or Docker volumes.
