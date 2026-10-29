# GitHub wheel releases

`MkChad wheels` builds and tests pushes to `mkchad-v1` without publishing. Only
an explicit `vMAJOR.MINOR.PATCH+mkchad.N` tag push publishes a GitHub release.
`N` is a positive maintenance counter; keep the PEP 440 local modifier, such as
`0.2.5+mkchad.2`, with matching tag `v0.2.5+mkchad.2`.

## Deliberate release

Commit the intended version in `package.json`, push `mkchad-v1`, and wait for its
`MkChad wheels` build to pass. Then tag the reviewed commit explicitly:

```bash
git tag -a 'v0.2.5+mkchad.2' <reviewed-commit> -m 'MkChad Server Documents 0.2.5+mkchad.2'
git push origin 'refs/tags/v0.2.5+mkchad.2'
```

Tag format, exact package-version agreement, maintenance-branch ancestry, backend
tests and archive metadata are checked independently by the workflow. Ordinary
upstream tags and branch pushes do not publish. The release job creates a draft,
attaches the wheel, sdist, `SHA256SUMS` and `build-provenance.json`, then publishes
it. It does not promote this maintenance release to GitHub's global Latest.
There is no PyPI/npm upload and no registry credential is needed. Only the release
job has `GITHUB_TOKEN` contents-write permission.

Download all assets and run `sha256sum --check SHA256SUMS` before deployment.
Provenance records the source commit, version, build tools, upstream frontend
identity, and archive hashes. A new CI build's hashes need not equal an older
local qualification artifact's hashes; install only the specific artifact whose
hash is pinned by the deployment.

## Backend-only frontend reuse

This maintenance line changes Python only. The workflow downloads the upstream
0.2.5 wheel with the hash pinned in `.github/mkchad-frontend.requirements.txt`.
`scripts/stage_mkchad_frontend.py` checks that hash and compares frontend sources,
styles, schemas, Yarn lock and package metadata against the pinned baseline.
Only the package version may differ. It stages the original frontend assets in a
**clean disposable build checkout**, omitting `install.json` because Hatch supplies
that separately. In-tree generated labextension files are an explicit build-only
exception; do not stage them in source control or run this helper on live state.

Hatch then packages Python with those assets and builds the final wheel from the
sdist, with npm disabled. If frontend inputs change, the workflow fails rather
than shipping stale assets. Qualify a frontend source build and revise this policy
before releasing such a change. No current maintenance backend code is replaced
by the upstream wheel.

## Tag rules and recovery

Two active GitHub rulesets target `refs/tags/v*+mkchad.*`:

- Only repository administrators can create these tags.
- Matching tags cannot be moved or deleted; the immutability ruleset has no
  bypass actor, including administrators.

Exact tag syntax and version agreement are enforced in the workflow. GitHub
rejected a tag-name-pattern rule for these repositories. Desired settings are
tracked in `.github/mkchad-tag-creation.json` and
`.github/mkchad-tag-integrity.json`; these JSON files do not apply themselves.
For initial creation:

```bash
gh api --method POST repos/krafczyk/jupyter-server-documents/rulesets --input <payload-file>
```

PUT the existing ruleset ID for changes instead of creating duplicates.

Do not retarget failed release tags. Fix the source and increment `+mkchad.N`.
If publication stopped at a draft, inspect it before retrying; the workflow
refuses to overwrite an existing release or its assets. Administrators can
deliberately change the repository policy; normal Git operations cannot bypass
immutability. Git-tag rules do not prevent release-asset edits through the UI.

Inherited upstream Jupyter Releaser workflows remain separate manual upstream
tooling and should not be used for this GitHub-only maintenance release path.
