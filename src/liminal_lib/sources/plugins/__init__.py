"""The provider plugins' place in the app, empty since 2026-11-01: every data provider is a package of
its own (liminal-plugin-<name>, found through the entry point group `liminal.providers`;
sources/registry.py says what a plugin gives). A module added here that defines PLUGIN is still a
provider from the next start: the sync, the Fetcher, the CLI and tools/set-key.ps1 all list it."""
