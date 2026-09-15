# Blessed images

The public record of every image the controller may run: one signed registry entry per `<name>@<version>`
(`.json` = the digest-pinned image, the author's manifest verbatim, our `qualified` measurements; `.sig` = the
release key's OpenSSH signature over the canonical JSON, namespace `gt-registry`). Written only by the
`bless-image` workflow (`.github/workflows/bless-image.yml`) with the release key from CI secrets; never by hand,
never on a laptop. The controller copies these into its state directory's `registry/` and re-verifies every
signature on every read; an entry that does not verify is never run.

Blessing is curation (vault 25), and authors submit **source**, never images: the workflow builds the pinned
commit itself, pushes it under `entrius/`, and signs the digest *our* build produced. Qualify on our own 5090
first (the full end-to-end run), then dispatch the workflow with the manifest path, the source repo + commit (or
an `entrius/` digest we already built, to re-bless) and the qualification JSON. A changed image or manifest under the
same name@version is refused: bump the manifest's `version`.
