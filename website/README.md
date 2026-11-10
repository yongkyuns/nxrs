# NXRS website maintenance

The website is a static HTML build using markdown-it-py, Jinja2 and Pygments.
It documents an experimental project: motivation, usage, examples and evidence
are distinct, and a working example is not a production-readiness claim.

## Content and navigation

- `website/guide/*.md` contains the explanatory and introductory pages.
- `docs/**/*.md` contains the engineering references. They are rendered from
  their original sources, not copied into a second maintained manual.
- `website/site.json` owns guide titles, routes, navigation groups and commonly
  used reference links. Every guide belongs to exactly one group. The sidebar
  and previous/next links use those same groups.
- `website/layout.html` and `website/static/*` own the presentation and behavior.
  The G7 logo and approved architecture SVGs remain in `docs/assets/`.

The homepage introduces the experiment rather than repeating the README or
promising product outcomes. Put design reasoning in `motivation.md`, practical
reading paths in `start-here.md`, application-specific context in `examples.md`,
and unresolved constraints in `status.md`. Preserve the distinction between
host, emulator and physical-device evidence. Link technical comparisons to
primary references; do not infer adoption rankings or general performance from
individual fixtures.

Relative Markdown links to published documents become site routes. Links to
source code and other repository files point to the build's revision. Media in
`docs/` are copied to the generated site. The build does not modify source files.
The site does not embed external applications or require a remote runtime to
read documentation. JavaScript adds search, theme controls and code copying;
ordinary links and document text work without it.

## Local build and preview

Use Python 3.12 or newer and a virtual environment:

```sh
python3 -m venv website/.venv
. website/.venv/bin/activate
python -m pip install -r website/requirements.txt
python -m unittest discover -s website/tests -p 'test_*.py' -v
python website/build.py --base-path / --out target/website-local
python website/check.py target/website-local --base-path /
python -m http.server 8000 --directory target/website-local
```

Open `http://localhost:8000`. For project-site hosting, build with
`--base-path /nxrs/` into `target/site-preview/nxrs`, then serve
`target/site-preview`. Check deep links as well as the homepage.

The renderer refuses to clean an unowned, nonempty output directory. Never use a
source directory as output. `--revision <commit>` controls repository source
links; CI supplies its checkout revision.

## Browser checks

```sh
python -m pip install playwright==1.57.0
python -m playwright install chromium
python website/tests/browser.py --site target/website-local --base-path /
```

The suite checks primary and grouped navigation, current-page markers,
within-section pagination, search and Escape/focus behavior, persisted themes,
mobile overflow, the mobile menu and no-JavaScript reading. It also checks that
visiting the site does not request an external application. Screenshots and a
machine-readable report are retained by CI. These are website checks, not
firmware or hardware qualification.

The content tests enforce navigation coverage, the application-source excerpt,
local reading-path links and the absence of an embedded-player dependency.

## GitHub Pages

Use **Settings → Pages → Build and deployment → Source: GitHub Actions**.
`docs/` is a source directory, not a generated-site publishing directory.

The `Website` workflow builds and validates pull requests with read-only
permissions. Only `main` pushes or manual runs on `main` can package and deploy
through the `github-pages` environment. The deployment job checks the configured
Pages source and keeps Pages/OIDC permissions separate from the build job.
No administrative token or protection bypass is used.

After merging a website change, check the deployment result and the public site
at `https://yongkyuns.github.io/nxrs/`. A successful local build or a merged PR is
not evidence that publication has completed. Preview artifacts and browser
reports are retained for review.
