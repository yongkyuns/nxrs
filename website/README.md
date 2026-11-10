# NXRS website

The website is static HTML, CSS, and JavaScript generated from the guide and repository documentation. It uses the approved G7 logo and architecture artwork from `docs/assets/`.

## Content

- `guide/`: user-facing guides, with general firmware topics first and application examples separate.
- `site.json`: page titles, guide sections, and reference groups.
- `layout.html`: shared homepage and documentation layouts.
- `static/`: local styles, search, theme, and navigation behavior.
- `examples/basic-firmware/`: a dependency-free Cargo workspace used by the introductory guides.
- `../docs/`: maintained engineering references, rendered from their original Markdown sources.

See [EDITORIAL.md](EDITORIAL.md) for the documentation structure and the external projects reviewed when organizing it. Retain a plain technical tone, concrete instructions, and links to implementation and tests.

## Build locally

From the repository root, create a virtual environment and install the pinned Python packages:

```sh
python3 -m venv target/website-venv
source target/website-venv/bin/activate
python -m pip install -r website/requirements.txt
python website/build.py --base-path / --out target/website-root
python website/check.py target/website-root --base-path /
python -m http.server 8000 --directory target/website-root
```

Open `http://localhost:8000/`. Use the equivalent activation command for your shell on Windows.

The renderer does not build firmware, fetch remote assets, mutate source files, or deploy. It only cleans output directories marked as owned website builds. In-repository output stays under `target/`.

## Tests

```sh
python -m unittest discover -s website/tests -p 'test_*.py' -v
node --check website/static/site.js
node --check website/static/theme.js
python website/build.py --base-path /nxrs/ --out target/website
python website/check.py target/website --base-path /nxrs/
```

Content tests check navigation coverage, relative links, source-backed Rust/manifest excerpts, generic introductory content, and existing reader anchors. Renderer tests cover Markdown, links, media, output ownership, and deterministic output.

With the toolchain from `rust-toolchain.toml` installed:

```sh
python website/tests/check_examples.py
```

This copies the introductory Cargo workspace outside the checkout, runs `hello` and `worker`, and runs the worker's unit tests. It also executes the documented `std-demo` commands on the real checkout. Results are written to `target/website-examples.json`. This test covers host execution; target builds remain in the firmware test workflows.

For browser tests:

```sh
python -m pip install playwright==1.57.0
python -m playwright install --with-deps chromium
python website/tests/browser.py --site target/website --base-path /nxrs/
```

The browser runner starts its own prefixed local server. It checks desktop/mobile navigation, search and focus, themes, the guide sequence, reference coverage, code controls, and no-JavaScript reading. Screenshots and reports are under `target/website-browser/`.

## Links and examples

Use relative Markdown links between guide and reference sources. The build rewrites links to published pages; links to source code and directories point to the build's repository revision. Keep code and documentation synchronized through the excerpt tests.

Add a guide to `site.json` and exactly one section. Add curated references to `reference_groups` without duplication; remaining documents stay available under Additional engineering notes. Preserve old URL fragments when a heading is renamed.

The site loads no external application or embedded runtime. Its content security policy uses `frame-src 'none'`.

## GitHub Pages

Repository Settings → Pages → Build and deployment → Source must be **GitHub Actions**. The source `docs/` directory is not the generated site.

The Website workflow builds both `/nxrs/` and `/`, checks examples and content, runs browser tests, and retains preview artifacts. Pull requests only validate. Publication runs from `main` after the build succeeds, using the generated `target/website` artifact and scoped Pages permissions.

The published address is `https://yongkyuns.github.io/nxrs/`. A successful validation run is separate from the Publish GitHub Pages job. Repository source files and artwork remain the maintained inputs.
