# Documentation structure and writing

The public introduction describes NXRS as a general-purpose Rust firmware development environment built on NuttX. NuttX remains the RTOS. Explain the implementation directly; keep the project-status label in the opening description rather than repeating it throughout the guide.

## Main learning path

Start with host setup, a minimal Cargo application, and a NuttX build. Develop the general topics independently: standard-library APIs, services and concurrency, hardware access, networking/storage, profiles, and tests.

Domain-specific applications belong under Examples or application-specific reference notes. They may illustrate a general concept, but a reader should not need to understand sensor fusion, a camera pipeline, or any other product to learn the framework.

Give each tutorial a task, prerequisites, commands, expected output, an explanation of the relevant files, and a next step. Distinguish a runnable example from a design illustration by naming it accordingly. Keep real configuration requirements near the operation they affect.

## Sources reviewed for this revision

Reviewed on 2026-11-10:

- [NuttX documentation](https://nuttx.apache.org/docs/latest/): separate getting started, task guides, OS design, API reference, and applications. The NXRS guide follows development tasks; detailed repository notes remain references.
- [Zephyr getting started](https://docs.zephyrproject.org/latest/develop/getting_started/) and [application development](https://docs.zephyrproject.org/latest/develop/application/index.html): begin with a small program and explain project files, configuration, build, and execution. NXRS uses its own Cargo and profile workflow, not Zephyr's commands.
- [openvela overview](https://doc.openvela.com/document?id=198&language=en&version=trunk) and [LED application guide](https://doc.openvela.com/document?id=211&language=en&version=trunk): describe generic subsystems separately from a concrete board/application walkthrough. IoT is useful context, not a restriction on NXRS.
- [PX4 first application](https://docs.px4.io/main/en/modules/hello_sky): establish a minimal application before adding domain-specific behavior. NXRS borrows that teaching sequence, not PX4's flight or messaging architecture.

These are documentation references, not compatibility, endorsement, or feature-parity claims. The wording and examples here are written for NXRS.

## Maintainable examples

`examples/basic-firmware` is an independent, dependency-free Cargo workspace. The first application and worker excerpts are checked against their source. Website CI compiles and runs a copied workspace, tests the worker lifecycle, and executes the documented `std-demo` commands.

Keep the copied-project workflow runnable. Add real behavior only when it helps teach the next concept; do not turn the introductory template into a product or framework runtime.

## Navigation and reference ownership

`site.json` declares the guide order and sections. Each guide belongs to one section. Core references appear in the persistent sidebar; application-specific notes are linked from Examples and the categorized Reference index.

The index renders every `docs/**/*.md` source once, including additional notes outside its curated groups. Do not delete historical test reports or change their results as part of an editorial revision.

Preserve existing routes and useful fragments when renaming headings. Browser tests cover the learning path, grouped navigation, reference coverage, search, themes, mobile layout, and no-JavaScript reading.
