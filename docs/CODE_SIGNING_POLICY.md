# Code signing policy

Free code signing provided by SignPath.io, certificate by SignPath Foundation.

> **Status:** the SignPath Foundation application is pending. Until it is approved, release
> installers are published unsigned together with a `SHA256SUMS.txt` manifest.

## Team roles

All SignPath signing roles for the `UmutErayAltay/artemis` repository are held by the repository owner:

- **Authors:** `UmutErayAltay`
- **Reviewers:** `UmutErayAltay`
- **Approvers:** `UmutErayAltay`

## Scope and approval

- Only Windows installer binaries built directly by the [`.github/workflows/release-windows.yml`](../.github/workflows/release-windows.yml) GitHub Actions workflow in the `UmutErayAltay/artemis` repository are submitted for signing.
- Unsigned installer artifacts are uploaded as short-lived workflow artifacts (`retention-days: 1`) from the build job and submitted to SignPath via `signpath/github-action-submit-signing-request`.
- Every release signing request is manually reviewed and approved by the Approver before a signed binary is issued and published to GitHub Releases.
- Published release assets include a `SHA256SUMS.txt` checksum manifest computed from the final installer binary.

## Privacy

Artemis is a local-first desktop assistant and does not send telemetry or user data unless explicitly configured. For full details on network connections, optional cloud integrations, and running completely offline, see the [Privacy Policy](PRIVACY.md).
