# Herdr fingerprint oracle

This isolated test program reproduces the typed schema and fingerprint contract
from [Herdr d6b40d4](https://github.com/herdrdev/herdr/blob/d6b40d4edd550ccea081f089605a64314f8c8b27/src/persist/snapshot.rs).
The upstream source is Apache-2.0; see [LICENSE](LICENSE). This adaptation renames
the types, omits the application and adds an input/output harness for synthetic
snapshots. It never starts Herdr, restores a terminal or reads a user profile.

Dependencies match the upstream lockfile, including `serde_json`'s `zmij`
formatter. Run `python -m tests.herdr_fingerprint_oracle` with a verified Cargo
runtime to compare Python projections with actual Rust serialization. Build
outputs and generated lockfiles are confined to a temporary directory.
