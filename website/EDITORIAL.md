# Documentation structure and writing

The public introduction describes NXRS as a general-purpose Rust firmware development environment built on NuttX. NuttX remains the RTOS. Explain the implementation directly; keep the project-status label in the opening description rather than repeating it throughout the guide.

## Main learning path

Start with host setup, a minimal Cargo application, and a NuttX build. Develop the general topics independently: standard-library APIs, services and concurrency, hardware access, networking/storage, profiles, and tests.

Give readers two ways through the material: a continuous learning path and a task map for returning users. Explain the design choices and API boundaries before asking the reader to compose several components. Keep the build workflow separate from the first target tutorial: one explains how inputs become artifacts, while the other takes a particular program through a working build and boot.

Domain-specific applications belong under Examples or application-specific reference notes. They may illustrate a general concept, but a reader should not need to understand sensor fusion, a camera pipeline, or any other product to learn the framework.

Give each tutorial a task, prerequisites, commands, expected output, an explanation of the relevant files, and a next step. Distinguish a runnable example from a design illustration by naming it accordingly. Keep real configuration requirements near the operation they affect.

Use generated SVGs for topology, ownership, lifecycle, and build flow. Match the approved overall architecture; replace text sketches, including diagrams in tilde-fenced blocks. A directory inventory or a simple dependency mapping is usually clearer as a table. Preserve real code and terminal output. Explain a diagram's scope in its caption and keep small-screen reading possible through descriptive prose and a link to the full SVG.

## Sources reviewed for this revision

Reviewed on 2026-11-10:

- [NuttX documentation](https://nuttx.apache.org/docs/latest/): separate getting started, task guides, OS design, API reference, and applications. The NXRS guide follows development tasks; detailed repository notes remain references.
- [Zephyr getting started](https://docs.zephyrproject.org/latest/develop/getting_started/) and [application development](https://docs.zephyrproject.org/latest/develop/application/index.html): begin with a small program and explain project files, configuration, build, and execution. NXRS uses its own Cargo and profile workflow, not Zephyr's commands.
- [openvela overview](https://doc.openvela.com/document?id=198&language=en&version=trunk) and [LED application guide](https://doc.openvela.com/document?id=211&language=en&version=trunk): describe generic subsystems separately from a concrete board/application walkthrough. IoT is useful context, not a restriction on NXRS.
- [PX4 first application](https://docs.px4.io/main/en/modules/hello_sky): establish a minimal application before adding domain-specific behavior. NXRS borrows that teaching sequence, not PX4's flight or messaging architecture.

These are documentation references, not compatibility, endorsement, or feature-parity claims. The wording and examples here are written for NXRS.

## Write finished evaluation reports

An evaluation reference is a completed report: objective, test setup and assumptions, results, interpretation, conclusions and focused follow-up, then evidence and reproduction. State the setup before the first finding or chart. Identify hardware, software/build revisions, workload, scheduling and memory settings, sample counts, warm-up and repetitions, measurement boundaries, correctness checks and exclusions. Include only settings relevant to interpreting the result; link full receipts and commands. If a material detail was not recorded, say so rather than inferring it.

Define unfamiliar metrics and denominators before using them. Separate experiments that used different firmware, workloads, observers or label rules. Put the scope beside each table or chart as well. Conclusions should follow from the stated evidence; avoid opening with a rhetorical question, repeating a recommendation, or narrating successive attempts. Keep detailed test procedures in the measurement guide and intermediate controls in the linked study archive.

Interpret the result rather than restating the table. Identify the dominant measured cost, the implementation mechanism that could explain a difference, and what is still unresolved. Distinguish measured effects, source inspection, hypotheses and calculated opportunities; do not turn correlation into a root-cause claim. Explain the consequence for the user's latency, memory or reliability budget. End with one or two prioritized follow-ups, stating which uncertainty each resolves and what evidence would justify the proposed improvement. Avoid an open-ended test catalogue.

Keep current explanations at their established routes. Preserve long investigations under `docs/history/` with a prominent scope notice and a link back to the maintained summary. Original numbers, failed controls and provenance remain intact. Do not combine different firmware, workloads or observers into a new apparent result, or turn historical “pending” statements into current status.

Reference navigation separates findings, measurement methods and historical evidence. Give each entry a short description of the question it answers in `site.json`; a filename alone is not a useful introduction. Add results to the appropriate summary instead of appending a new chronology to the reader's main path.

Charts use the existing SVG generator and themes, with named units, readable values, a source and a nearby textual/table equivalent. Use a zero baseline for magnitude bars. Conceptual flow diagrams must not imply measured durations. A documentation or chart refresh never implies a new hardware run.

## Maintainable examples

`examples/basic-firmware` is an independent, dependency-free Cargo workspace. The first application and worker excerpts are checked against their source. Website CI creates an external workspace from the guide's `cargo new` command and published blocks, compiles and runs each tutorial revision, tests the worker lifecycle, and executes the documented `std-demo` commands.

Keep the source-project workflow runnable. Add real behavior only when it helps teach the next concept; do not turn the introductory template into a product or framework runtime.

## Navigation and reference ownership

`site.json` declares the guide order and sections. Each guide belongs to one section. Core references appear in the persistent sidebar; application-specific notes are linked from Examples and the categorized Reference index.

Pagination follows the entire guide across section boundaries. Reference-to-guide links help a reader who arrives through search recover the user-facing introduction. The mobile section index is native HTML and remains useful without JavaScript.

The index renders every `docs/**/*.md` source once, including additional notes outside its curated groups. Do not delete historical test reports or change their results as part of an editorial revision.

Preserve existing routes and useful fragments when renaming headings. Browser tests cover the learning path, grouped navigation, reference coverage, search, themes, mobile layout, and no-JavaScript reading.
