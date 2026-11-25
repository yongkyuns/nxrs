# Verify NXRS documentation changes

Run commands from the repository root. `website/README.md` and
`.github/workflows/website.yml` own the current build and test procedure. Match
verification to the change and satisfy required repository gates; do not rebuild
firmware to validate a spelling correction.

## Choose evidence for the changed surface

| Change | Evidence to seek |
| --- | --- |
| Wording | Source accuracy, diff, affected links; rendering if structure changes |
| Heading, route, navigation, page, source excerpt | Content/build tests, site checker, affected reading path |
| CSS, template, theme, search, mobile controls | Generated site and desktop/mobile browser inspection in both themes |
| First-application or worker tutorial | Source checks and documented external-project execution |
| API integration | Valid API artifact, both site prefixes, generated API navigation |
| Firmware or hardware claim | The owning target/emulator/device evidence for the exact configuration |
| Shared architecture diagram | Reproducibility check plus visual inspection |

## Prepare the existing environment

Use the Rust toolchain pinned in `rust-toolchain.toml`. For a new Python environment:

```sh
python3 -m venv target/website-venv
source target/website-venv/bin/activate
python -m pip install -r website/requirements.txt
```

Reuse an existing suitable environment when available. Follow the current README
and workflow for browser prerequisites instead of adding application dependencies
to make a local documentation check run.

## Build and check the site

Generate the API artifact when needed; a valid unchanged artifact can be reused:

```sh
python website/build_api.py --out target/rust-api
python -m unittest discover -s website/tests -p 'test_*.py' -v
node --check website/static/site.js
node --check website/static/theme.js
python website/build.py --api-docs target/rust-api --base-path /nxrs/ --out target/website
python website/check.py target/website --base-path /nxrs/
python website/build.py --api-docs target/rust-api --base-path / --out target/website-root
python website/check.py target/website-root --base-path /
```

The API artifact records its source inputs, configuration, and files. Rebuild when
those inputs change; do not bypass stale-artifact checks or hand-edit generated
Rustdoc. Reuse the same valid artifact for the two base paths. The renderer rewrites
source links, and the checker includes site and API anchors. Inspect any failing
destination rather than removing a useful link to make validation pass.

Use focused existing unit tests for a narrow edit when full regeneration adds no
evidence; run the broader checks when shared rendering or navigation changes.
`target/` contains generated outputs, not hand-maintained site source.

## Inspect desktop, mobile, and API pages

With the Playwright/Chromium setup documented by the current website workflow:

```sh
python website/tests/browser.py --site target/website --base-path /nxrs/
python -m http.server 8000 --directory target/website-root
```

The browser runner starts its own server and records evidence under
`target/website-browser/`. It exercises desktop/mobile navigation, search, focus,
themes and SVGs, pagination, contents, code, no-JavaScript reading, and API routes.
Open the captured screenshots and inspect the edited page yourself through the
local root preview. Follow the reader's task in both themes, at desktop and phone
sizes, and check narrow-width reflow. A passing browser test is not a design review.

Use the website design reference for interaction and accessibility details. Report
blocked browser work explicitly instead of describing screenshots or checks that
were never observed.

## Run the examples whose instructions changed

```sh
python website/tests/check_examples.py
```

This executes the documented external Cargo project path, source/manifest blocks,
host revisions and worker checks, and the external firmware plan. Its report is
`target/website-examples.json`. It establishes host and build-plan evidence; it does
not establish a target boot or physical device behavior.

For controller tutorial changes, use:

```sh
python website/tests/controller_example.py
```

Read the owning script and guide before requesting its optional target mode or
the first-firmware qualification. Run those when target instructions or behavior
change and the required toolchain exists. Name the profile, emulator, and mock
devices explicitly; an emulated LED does not qualify a physical board. Do not
flash hardware as an implicit part of editing documentation.

When source/architecture also changes, apply `AGENTS.md` and the owning gates,
including `python3 tools/check-architecture.py` where relevant.

## Check shared diagrams

```sh
node docs/assets/render-architecture.mjs
node docs/assets/render-architecture.mjs --check
```

Inspect both generated themes at full size and in the article. Keep the generator
and SVG outputs together. Use the separate documented renderer for measurement
artwork; preserve its recorded data.

## Report what the evidence establishes

State the reader improvement, changed source paths, commands actually executed,
results, desktop/mobile pages and themes inspected, and any blocked or skipped
qualification with its reason. Separate site validation, host execution, target
builds, emulator behavior, and physical-device evidence. Publish only under the
existing repository workflow and the user's authorization.
