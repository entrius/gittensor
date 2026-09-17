# Blessed images

The public record of every image the controller may run: one signed registry entry per `<name>@<version>`
(`.json` = the digest-pinned image, the author's manifest verbatim, our `qualified` measurements; `.sig` = the
release key's OpenSSH signature over the canonical JSON, namespace `gt-registry`). Written only by the
`bless-image` workflow (`.github/workflows/bless-image.yml`) with the release key from CI secrets; never by hand,
never on a laptop. The controller copies these into its state directory's `registry/` and re-verifies every
signature on every read; an entry that does not verify is never run.

Blessing is curation (vault 25): read the source, pull the author's published digest onto our own 5090, qualify
it, sign it. A newer push is a new digest and stays unblessed until we choose it, so nothing auto-updates. When
there is no published image yet, the workflow can build a pinned commit itself and push it under `entrius/`.
An author's published digest is copied under `entrius/` at bless time (same digest, one copy, no sync): the entry's
`image` is our copy, the one boxes pull, and `source_image` records the author's reference, so a deleted or retagged
upstream package never breaks a placement.
Dispatch it with the manifest path, either the digest or the source repo + commit, and the qualification JSON. A changed image or manifest under the
same name@version is refused: bump the manifest's `version`.
