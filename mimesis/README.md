# MIMESIS CI

> 🛟 **Need help or found a bug?** Get support at [support.doodesch.de](https://support.doodesch.de).

MIMESIS repositories call `mimesis-build.yml` for pull requests and `mimesis-release.yml` for tags and manual verification. Shared defaults pin the validated MIMESIS 0.4.0 reference commit and MimicAPI 0.4.3. Update these defaults together after validating a game update.

Builds check source metadata, compile Debug and Release, and run the Workspace Harmony/reflection target checker. Fork pull requests check metadata without access to private references or secrets. Trusted builds require the existing `WORKSPACE_TOKEN` SSH deploy key.

Release callers pass their project, assembly name and supported stores. `verify` builds the latest changelog version and retains both checked archives without publishing. A pushed stable `vX.Y.Z` tag must be on the repository's default branch. Missing, duplicate or empty changelog sections fail before uploads. The source manifest remains `0.0.0`; mod projects remain `0.0.0-dev` and use `ModVersion.Current`.

Both archives use the fresh Release DLL, never a DLL copied into `thunderstore/`. GitHub and Thunderstore receive the checked flat ZIP; Nexus receives `Mods/<assembly>.dll`. Checksums and target reports are retained as run artifacts.

Nexus callers need repository variables `NEXUS_MOD_ID` (global MIMESIS mod UID) and `NEXUS_FILE_GROUP_ID` (existing file group). The organization provides `NEXUSMODS_API_KEY`, `THUNDERSTORE_TOKEN` and `DISCORD_RELEASE_WEBHOOK`. The Nexus action updates the mod version, primary file and mod manager download settings. API dependants preserve existing Nexus requirements and read back their MimicAPI minimum range. An announcement runs only after every enabled store and required dependency update succeeds.

If an upload fails, inspect the store before retrying. `nexus` repair downloads and verifies the exact Nexus ZIP from an existing GitHub release; it does not rebuild historical source. `requirements` repair updates only the given existing Nexus version UID. Repairs do not send another Discord announcement or append another mod changelog entry.

Run the helper checks with `python -m unittest discover -s mimesis -v`.
