# Natural technical writing for NXRS

## Choose the reader's job

Before drafting, state the audience, what they already know, and the result they
need. A newcomer creating an independent application needs a different page from
a maintainer checking a device ownership contract. Let that distinction determine
the order and depth, rather than listing everything the implementation can do.

| Purpose | Reader's question | Useful structure |
| --- | --- | --- |
| Tutorial | Can I build my first application? | Prerequisites, commands, expected output, explanation, next step |
| Task guide | How do I choose a profile? | Situation, selection, action, verification, failure recovery |
| Reference | What does this setting mean? | Exact name/type/default, constraints, effect, source |
| Explanation | Why is this resource owned here? | Model, small example, consequence, architectural link |

Follow `website/EDITORIAL.md`: introduce generic Rust development before product
examples. Keep a continuous learning path and a task map for returning users.
Introduce standard-library APIs, services, hardware access, profiles, and testing
as understandable topics; connect them when a reader is ready to compose them.
Keep the introductory project-status label in its proper place instead of
repeating it as a warning throughout every guide.

## Explain directly and naturally

Name the actor and effect: “The profile selects the target configuration.” Use
ordinary verbs, connected paragraphs, and one main idea per paragraph. Define
abbreviations on first use when the intended reader needs them. Keep necessary
technical terms precise, but explain their consequence in familiar language.

Remove promotional adjectives, filler introductions, rhetorical question headings,
and repeated summaries. Avoid “seamless,” “powerful,” “simply,” and “obviously.”
Do not turn every paragraph into a bullet list or every qualification into a
warning box. Use numbered steps for order and tables for exact comparisons.

Examples of edits, to be verified against the current source before reuse:

| Before | After | Why |
| --- | --- | --- |
| Leverage a powerful framework to seamlessly deploy firmware. | Create a Rust package, run it on the host, then build the same source for NuttX. | Gives a concrete sequence. |
| It is important to understand the configuration abstraction. | The manifest describes the application entry point. The profile selects the target configuration. | Names each owner and its role. |
| Simply run the command below. | From the NXRS repository root, run the command below. It selects the checkout's pinned Rust toolchain. | Exposes a prerequisite that affects the result. |
| Passing the host tests proves that the hardware works. | The host tests check application logic. Use the component's target and hardware tests to qualify its device path. | Bounds the evidence. |
| The example requires a 64 KiB stack. | This example allocates a 64 KiB entry stack. Choose stack sizes for your application's measured needs. | Avoids making an example allocation a universal requirement. |

Read the page aloud once after the factual pass. Remove clumsy joins, identical
sentence rhythms, vague nouns, and repeated ideas. Tighten without removing error,
lifetime, or platform qualifications. Preserve user-requested names, spellings,
capitalization, and titles instead of silently imposing a house-style rewrite.

## Teach through working source

The first-application guide teaches an external Cargo workspace. Its complete
manifest, source, and commands are checked by `website/tests/check_examples.py`.
Preserve the existing `<!-- tutorial:... -->` markers and source relationships when
editing excerpts. Use `website/examples/basic-firmware/` and
`website/examples/controller/` as maintained source projects, not bundles of
precompiled output to copy into a user's application.

For each example:

1. Put the working directory and toolchain/profile requirements before the command.
2. Give complete source and manifest when required, or label a block as an excerpt.
3. Keep commands copyable, without shell prompts or invented paths and versions.
4. Separate input from expected output; explain placeholders and units.
5. State the observable success, then explain the files and concepts just introduced.
6. Link the next task and give a specific recovery step for likely failures.

For example, the first host program's output is `Hello from NXRS`; its target build
is a later step after target tool setup. Do not skip that separation when shortening
the lesson. Keep the distinctions among package name, Rust library name, binary,
NuttX shell command, profile, and physical device explicit where they matter.

## Keep evidence and architecture honest

Use implementation source and its owning tests for behavior. Use the focused
architecture documents named by `AGENTS.md` for ownership and boundary decisions.
Link to those authorities instead of creating a parallel architecture summary that
can drift. Reference prose should explain an API's use and contract; generated
Rustdoc owns the signature and trait relationships.

Keep these claims separate: code exists; host tests pass; a target image builds;
an emulator boots and runs the example; a specified physical device has evidence.
Name the configuration and limits that affect the reader's choice. Never turn a
proposal, mock provider, historical measurement, or unmerged change into a general
support promise. Preserve historical reports and their recorded outcomes.

Use source-relative Markdown links as documented by `website/README.md`. Add a guide
through `website/site.json`, keep one section per guide, preserve useful incoming
fragments, and link references back to a helpful user-guide introduction.

Writing references: [Google's developer-documentation tone](https://developers.google.com/style/tone)
and [Diátaxis page purposes](https://diataxis.fr/start-here/). Apply these ideas in
the existing NXRS editorial voice; user and repository instructions govern.
