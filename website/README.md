# NXRS website

The website is static HTML, CSS, and JavaScript generated from the guide and repository documentation. It uses the approved G7 logo and architecture artwork from `docs/assets/`.

## Content

- `guide/`: task-oriented guides for the first build, architecture and APIs, application design, configuration, debugging, and examples.
- `api.md` and `api.json`: the Rust API introduction and curated public-library catalog.
- `build_api.py`: the separate Rust documentation builder and artifact validator.
- `site.json`: page titles, guide sections, and reference groups.
- `layout.html`: shared homepage and documentation layouts.
- `static/`: local styles, search, theme, and navigation behavior.
- `examples/basic-firmware/`: a dependency-free Cargo workspace used by the introductory guides.
- `examples/controller/`: the runnable command/output controller, with bounded admission, completion tickets, and explicit shutdown.
- `../docs/`: maintained engineering references, rendered from their original Markdown sources.

See [EDITORIAL.md](EDITORIAL.md) for the documentation structure and the external projects reviewed when organizing it. Retain a plain technical tone, concrete instructions, and links to implementation and tests.

## Build locally

Install the Rust toolchain pinned in `rust-toolchain.toml`. From the repository root, create a virtual environment, install the pinned Python packages, and build the Rust API artifact before rendering the site:

```sh
python3 -m venv target/website-venv
source target/website-venv/bin/activate
python -m pip install -r website/requirements.txt
python website/build_api.py --out target/rust-api
python website/build.py --api-docs target/rust-api --base-path / --out target/website-root
python website/check.py target/website-root --base-path /
python -m http.server 8000 --directory target/website-root
```

Open `http://localhost:8000/`. Use the equivalent activation command for your shell on Windows.

The Rust API builder runs locked Cargo documentation generation for the curated libraries and selected software features. It uses the pinned host toolchain with capability bindings unselected. Its receipt records source inputs, configuration, and generated files; it retains a separate Cargo compilation cache while refreshing the documentation output.

The site renderer consumes that validated artifact. It does not run Rust builds, fetch remote assets, mutate source files, or deploy. Both builders only clean output directories marked as their own builds. In-repository output stays under `target/`.

The renderer adds return navigation to rustdoc pages. It omits an optional asynchronous trait-implementation request only when that trait's script is absent from the complete artifact, and records each omission in `build-report.json`. Existing implementation lists and all other resource requests remain intact and are checked.

Regenerate the API artifact after changing its Rust inputs, catalog, or builder. A missing, changed, or stale artifact must be corrected before the full site is rendered. The same artifact can be used for both `/nxrs/` and `/` builds.

## Tests

```sh
python website/build_api.py --out target/rust-api
python -m unittest discover -s website/tests -p 'test_*.py' -v
node --check website/static/site.js
node --check website/static/theme.js
python website/build.py --api-docs target/rust-api --base-path /nxrs/ --out target/website
python website/check.py target/website --base-path /nxrs/
```

Content tests check navigation coverage, relative links, source-backed Rust/manifest excerpts, generic introductory content, and existing reader anchors. Renderer tests cover Markdown, links, media, output ownership, and deterministic output. API checks cover the curated artifact's source identity, required entry points, and generated files; the complete-site check also follows local rustdoc links and source anchors.

With the toolchain from `rust-toolchain.toml` installed:

```sh
python website/tests/check_examples.py
```

This runs the guide's `cargo new` command outside the checkout, writes its published manifest and source, and executes the greeting, edited greeting, and worker revisions. It also checks the external application's firmware plan, runs the source worker's unit tests, and executes the documented `std-demo` commands on the real checkout. Results are written to `target/website-examples.json`. This test covers host execution and build planning; target builds remain in the firmware test workflows.

Run the controller guide's source checks, Rust behavior tests, and host CLI cases with:

```sh
python website/tests/controller_example.py
```

After the [firmware guide](guide/firmware.md) tool setup, add `--target` to build the same package and execute its command and shutdown paths on NuttX/QEMU. The First firmware tutorial workflow runs both application tutorials and retains build records and console transcripts. Controller results are written to `target/controller-tutorial/`. Its target profile explicitly uses the LED mock; this checks the Rust/NuttX execution path without claiming physical LED behavior.

For browser tests:

```sh
python -m pip install playwright==1.57.0
python -m playwright install --with-deps chromium
python website/tests/browser.py --site target/website --base-path /nxrs/
```

The browser runner starts its own prefixed local server. It checks desktop/mobile navigation, search and focus, themes (including reference SVGs), pagination across guide sections, the mobile section index, reference coverage, code controls, and no-JavaScript reading. API coverage follows the catalog into generated documentation and checks symbol search and return navigation. Screenshots and reports are under `target/website-browser/`.

## Diagrams

The diagram source is [render-architecture.mjs](../docs/assets/render-architecture.mjs). Regenerate the committed light and dark SVGs from the repository root:

```sh
node docs/assets/render-architecture.mjs
node docs/assets/render-architecture.mjs --check
```

Keep the approved architecture's typography, rounded containers, muted connectors, and blue/orange accents. Each figure should answer one question, with a caption explaining its scope and meaningful alternative text. Use prose or a table for a short path inventory or a simple mapping. Reserve code blocks for commands, source, configuration, and actual output.

Reference Markdown can use a `<picture>` with a dark source so it also reads on GitHub. The site's theme control updates these sources to match the reader's selected theme. Guide figures can use the existing `theme-light` and `theme-dark` image classes. Keep an SVG link in the caption when a detailed diagram needs a larger view. The Website workflow checks reproducibility and rejects missing or stale SVGs without changing files.

## Links and examples

Use relative Markdown links between guide and reference sources. The build rewrites links to published pages; links to source code and directories point to the build's repository revision. Keep code and documentation synchronized through the excerpt tests.

Link to `api.md` when directing readers to the Rust API catalog. Maintain its package names, Rust library names, descriptions, and feature selection in `api.json`; the builder checks the catalog against the Cargo manifests. The generated item pages and rustdoc search belong to the artifact, and should not be edited or committed as website source. Website search indexes the library catalog, while rustdoc supplies symbol search.

Add a guide to `site.json` and exactly one section. Add curated references to `reference_groups` without duplication; remaining documents stay available under Additional engineering notes. Preserve old URL fragments when a heading is renamed.

The guide order also defines previous/next navigation across sections. A `reference_guides` entry connects an engineering reference to its practical introduction without duplicating its source. Keep the guide index's task map and homepage reading paths aligned with that organization.

The site loads no external application or embedded runtime. Its content security policy uses `frame-src 'none'`.

## GitHub Pages

Repository Settings → Pages → Build and deployment → Source must be **GitHub Actions**. The source `docs/` directory is not the generated site.

The Website workflow builds the Rust API artifact once, renders both `/nxrs/` and `/`, checks examples and content, runs browser tests, and retains preview and API build records. Pull requests only validate. Publication runs from `main` after the build succeeds, using the generated `target/website` artifact and scoped Pages permissions.

The published address is `https://yongkyuns.github.io/nxrs/`. A successful validation run is separate from the Publish GitHub Pages job. Repository source files and artwork remain the maintained inputs.
