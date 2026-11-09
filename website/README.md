# NXRS website

The website is a static HTML build. A small Python renderer uses markdown-it-py,
Jinja2 and Pygments; the output needs no backend, npm runtime, remote font,
analytics service or JavaScript framework. Reading documents works without JS;
search, theme controls and the optional Noon embed progressively enhance it.

## Content ownership

- `website/guide/*.md`: the onboarding/user-guide sequence.
- `docs/**/*.md`: engineering references, rendered directly from their existing
  sources. Do not copy them into a parallel manual.
- `website/site.json`: guide order and frequently used reference links.
- `website/layout.html`, `website/static/*`: presentation and browser behavior.
- `docs/assets/nxrs-logo.png` and the two architecture SVGs: approved G7 artwork,
  reused without changing its diagram topology.

Relative Markdown links to published pages are rewritten to site routes. Links
to code, tests or other repository content point to the source revision. Media
beneath `docs/` are copied into the static site. No source files are modified.
Fenced text/source diagrams remain source; checked-in SVG diagrams remain SVG.

## Local build and preview

Use Python 3.12 or newer and a virtual environment:

```sh
python3 -m venv .venv-site
. .venv-site/bin/activate
python -m pip install -r website/requirements.txt
python -m unittest discover -s website/tests -p 'test_*.py' -v
python website/build.py --base-path / --out target/website-local
python website/check.py target/website-local --base-path /
python -m http.server 8000 --directory target/website-local
```

Open `http://localhost:8000`. To reproduce project-site hosting, build with
`--base-path /nxrs/` into `target/site-preview/nxrs`, then serve
`target/site-preview`. Test deep links, not only the homepage.

The renderer refuses to clean a nonempty output directory unless it contains its
own `.nxrs-website` marker. Never use a source directory as build output.
`--revision <commit>` controls repository source links. CI uses its exact checkout.

## Browser checks

```sh
python -m pip install playwright==1.57.0
python -m playwright install chromium
python website/tests/browser.py --site target/website-local --base-path /
```

The deterministic browser suite tests navigation, search, theme persistence,
mobile overflow, keyboard behavior and the embed's load/unload lifecycle using a
local response in place of Noon. This does not qualify Noon rendering. Set
`NXRS_TEST_LIVE_NOON=1` to additionally attempt the real remote page; the resulting
report distinguishes remote page load from actual scene readiness. External
availability is not silently promoted into a passing engine test.

## GitHub Pages

The `Website` workflow builds and validates pull requests without deployment or
write permissions. A push to `main` (or a manual run on `main`) uploads the static
artifact and deploys through the `github-pages` environment. Runtime/firmware
builds are not required to publish documentation.

One-time repository setup: **Settings → Pages → Build and deployment → Source:
GitHub Actions**. The workflow deliberately does not use an administrative token
or bypass environment protection to enable Pages. The expected project URL is
`https://yongkyuns.github.io/nxrs/`; this URL is not evidence of a completed
publication. Check the workflow's deployment result.

The workflow is scoped to documentation, website assets, source files used by
the guide and itself. It uses read-only permissions for builds and restricts
`pages: write` and `id-token: write` to the main-branch deployment job. Preview
artifacts and browser reports are retained for review; PRs cannot publish.

## Noon integration

The optional demo is the existing `showcase-group-animation` page at the Noon
project's Pages URL. It is not rebuilt or vendored, does not autoplay, and is not
a dependency of NXRS firmware. A direct link remains available, and the iframe
can be removed with Unload. Do not treat the iframe's `load` event as proof that
WebAssembly initialized or the scene rendered. See the [user-facing explanation](guide/noon.md).
