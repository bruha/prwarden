# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Until 1.0.0 the project was developed in a private repository, as AI Code Reviewer,
and its public history starts at 1.0.2. The commit hashes in the entries up to and
including 1.0.2 refer to commits that this repository does not contain.

<!-- release-baseline: 2.2.0 27a2890136d7f9fb83482461f5cc58bfe949d924 -->
## [Unreleased]

## [2.2.0] - 2026-11-04

### Added

- clone repositories that name no local checkout (`2986fe9`).
- run on a schedule with prwarden schedule (`a75c97a`).
- check an installation with prwarden doctor (`191e8b6`).
- configure PR Warden with prwarden setup (`04e8f01`).

### Fixed

- let this project review its own public repositories (`27a2890`).

### Changed

- wrap a long line in the managed checkout tests (`2de87e2`).

## [2.1.0] - 2026-11-04

### Added

- find configuration and state outside a source checkout (`832911d`).

### Testing

- ignore hidden desktop files when checking shipped package data (`86b0246`).

### Maintenance

- package PR Warden with a prwarden command (`a3e5390`).
- publish each release to PyPI through trusted publishing (`df5e83e`).
- build with a setuptools free of known advisories (`1b6b802`).

## [2.0.0] - 2026-11-04

### Breaking Changes

- name the package prwarden instead of src (`3911f4f`).

## [1.0.2] - 2026-11-04

### Fixed

- **cli:** state how many reviews will really run together (`446589e`).

### Documentation

- say that commit hashes from before 1.0.0 are not in this repository (`b8dcf2c`).
- **readme:** explain how a review works and add a quick start (`ca2001f`).
- say that the public history starts at 1.0.2 (`5ff28d8`).

### Maintenance

- describe review cases generically before the repository goes public (`70e3442`).

## [1.0.1] - 2026-11-04

### Fixed

- **cli:** keep the progress line readable in every terminal theme (`1b6a5d4`).

### Changed

- **tests:** wrap a progress-line test within the line limit (`33a7fba`).

## [1.0.0] - 2026-11-04

### Breaking Changes

- rename the tool to PR Warden (`cabab7d`).

### Added

- **delivery:** mark earlier LGTM comments outdated once a new result posts (`a8abe9a`).

### Fixed

- **review:** stop a renamed file from crashing the documentation level (`3a3cfb1`).
- **cli:** report a defect in this tool instead of ending the run silently (`1c0fc9a`).

### Documentation

- record the latest delivery and identity rules for people and agents (`9033487`).
- say how to contribute and how to get the code (`b452e01`).

### Maintenance

- bump actions/setup-python from 7.0.0 to 7.1.0 (`fed55d8`).

## [0.33.0] - 2026-11-03

### Added

- **delivery:** keep a repository out of Slack and review own PRs there on GitHub (`6a162f0`).
- **delivery:** name the verifier an own-PR LGTM had nothing to give (`156689e`).
- **config:** read and review a repository as the account its entry names (`60d2879`).
- **review:** check documentation again when a pull request edits it (`d97a47c`).

## [0.32.0] - 2026-10-27

### Added

- **evaluate:** replay one advisory level alone beside a baseline verifier (`823751e`).
- **engine:** give the verifier a fallback of its own (`5be0c71`).
- **design-level:** drop advisory from the design comment heading (`e1fcdf5`).
- **docs-level:** drop advisory from the documentation comment heading (`f7e2746`).
- **delivery:** drop advisory from the design and docs Slack messages (`ee40e56`).

### Fixed

- **engine:** keep Kiro code intelligence out of the review workspace (`3aad4c3`).
- **engine:** remove the lsp file Kiro's code tool writes after each call (`f94acc6`).

### Documentation

- **prompts:** record the Antigravity fallback change and the Gemini 4 Argon trial (`af37de6`).
- **spec:** record why a change that fits one review is not split (`68ca839`).
- **spec:** correct the split measurement and how loose the noise bound is (`068f62e`).

## [0.31.0] - 2026-10-22

### Added

- **docs-level:** report an entry a list lacks in a checked category (`318e53e`).

## [0.30.0] - 2026-10-22

### Added

- **public-safety:** refuse commits that name reviewed projects (`fc73ab8`).

### Documentation

- license under Apache-2.0 and add a security policy (`58297b8`).

### Maintenance

- cite reviewed pull requests by private reference (`db5a6bd`).
- pin actions to release commits and keep them updated (`dee7000`).
- bump actions/setup-python from 6.3.0 to 7.0.0 (`eb79a16`).
- bump actions/checkout from 6.1.0 to 7.0.1 (`12c317c`).

## [0.29.0] - 2026-10-22

### Added

- **evaluate:** measure noise beside recall, with control cases (`bb22eb3`).

### Fixed

- **evaluate:** show replays the PR description and credit later fixes (`72ef871`).
- **evaluate:** count a workspace that cannot be prepared as one failed case (`19de37c`).
- **engine:** own Kiro's workspace configuration in every review (`b24fa3d`).
- **engine:** keep every agent host from running a head's configuration (`c28a03a`).

### Documentation

- **prompts:** record where each defensive prompt rule came from (`1cc4c77`).

## [0.28.0] - 2026-10-19

### Added

- **engine:** report what each Kiro call cost (`9d2c85a`).
- **evaluate:** score every acted-on finding and replay cases in parallel (`053bb68`).
- **cli:** show what each stage cost in the spend line (`6be1403`).

### Fixed

- **engine:** keep untrusted data from opening or closing a prompt's own tags (`0416d46`).
- **engine:** review changed test code as code instead of skipping it (`22d04e8`).

### Documentation

- show real replay and spend output in the harness examples (`9b33820`).
- **scheduling:** explain the Documents prompt an interpreter upgrade brings (`234abf0`).

## [0.27.0] - 2026-10-18

### Added

- **engine:** set a reasoning effort per role, and harder for code (`bc7475b`).
- **cli:** give the design and documentation levels the fallback engine (`31874b0`).

### Fixed

- **review:** make a rejected citation actionable, and stop cutting error text (`a6c68c4`).
- **review:** rewrite an absolute citation path instead of losing the level (`c76fac1`).
- **evaluate:** re-attempt a case lost mid-stream and name what a pass measured (`1ba68e1`).
- **evaluate:** identify a case by its head, and judge only what was recorded (`5a5c129`).
- **evaluate:** name the engine a pass called, and no arm it never took (`8dedbd1`).
- **engine:** bound an Antigravity invocation by one timeout, not three (`6e64daa`).
- **engine:** name Antigravity's refusal of a model or effort (`84c38b8`).
- **cli:** ask GitHub before re-reviewing an unchanged conflicted head (`a85a569`).
- **github:** act as the reviewer account whichever gh account is active (`ce6da05`).
- **engine:** tell the design level that any finding means a flawed verdict (`37a6146`).

### Documentation

- show the models that still exist in the engine examples (`4861878`).

### Maintenance

- test on Python 3.15, which the scheduled run now uses (`bd30da6`).

## [0.26.0] - 2026-09-22

### Added

- **evaluate:** name the findings a replay confirmed (`d75a7c8`).
- **evaluate:** measure the reviewer's spread across repeated passes (`c55527e`).
- **evaluate:** re-judge recorded candidates without running a review (`08155d9`).
- **cli:** name every engine and model of a pipeline on the command line (`2d5934c`).

### Fixed

- **github:** validate a head commit as a Git object name (`c315693`).
- **cli:** report every rejected candidate, not one per line (`45152f6`).
- **engine:** stop retrying a quota that cannot clear, and remember it for the run (`9832643`).
- **engine:** state the advisory levels' character budget in their own prompts (`91b9208`).
- **release:** read a merge commit as a merge rather than a malformed change (`18de697`).

### Documentation

- **research:** pre-register the arm comparison and its decision rule (`3d4db81`).

## [0.25.0] - 2026-09-21

### Added

- **evaluate:** mine labels from what authors did after our comments (`1eaf39b`).
- **evaluate:** keep a transcript of every measurement (`4e9d7ee`).
- **review:** read a change too large for one review in several (`f18beac`).
- **evaluate:** replay only the cases a run can actually score (`a62e98c`).
- **evaluate:** bundle a replayed change the way a live review bundles it (`9b1a25c`).

### Fixed

- **evaluate:** score a label only against the pipeline that delivered it (`36fa07c`).
- **evaluate:** pin a mined case against the commit its pull request opened from (`1fb0058`).
- **config:** keep an engine command with the engine it was written for (`1f7e31f`).
- **evaluate:** keep measuring after an engine refuses a call (`6fc767b`).

### Documentation

- **research:** record the first labelled baseline (`74d7bf0`).

## [0.24.0] - 2026-09-17

### Added

- **review:** let a repository say what it is for (`029698e`).
- **cli:** report what each review spent (`a0445bf`).
- **metrics:** measure what a run's reviews decided (`4fcdb85`).
- **evaluate:** replay pinned pull requests to measure a change (`fb1f88b`).

### Fixed

- **config:** stop carrying a model ID onto another engine (`e54d7fc`).

### Documentation

- **research:** measure proposal 1 and drop it (`b071b02`).
- **threat-model:** record what this tool trusts and what stops each threat (`3876c91`).
- **research:** mark the three cheap proposals as shipped (`6125a30`).
- **research:** describe the frozen-replay half of the harness (`9083d35`).
- **research:** record what the frozen replay build corrected (`507a93e`).

## [0.23.0] - 2026-09-17

### Added

- **review:** check the change against the project's documentation (`ce5a8e9`).

### Fixed

- **delivery:** lead the manual-review Slack message with the ask (`4683de0`).

### Documentation

- **research:** compare this reviewer with alibaba/open-code-review (`03570e3`).

## [0.22.1] - 2026-09-10

### Fixed

- **delivery:** stop re-reviewing a file nobody can read (`8e981da`).

## [0.22.0] - 2026-09-07

### Added

- **delivery:** measure changed feedback instead of failing on it (`082c94f`).
- **review:** review around a file GitHub will not diff (`fe446fd`).

### Fixed

- **antigravity:** read the refusal the envelope reports and bound the roots (`5e309da`).

### Maintenance

- **config:** ignore workspace lockfiles at any depth (`a6594ea`).

## [0.21.1] - 2026-09-02

### Fixed

- **antigravity:** recover from a tool call print mode cannot approve (`7d2e37c`).
- **review:** leave a rejected verdict a valid way to answer (`cec318f`).

## [0.21.0] - 2026-09-01

### Added

- **design:** review the solution design once per pull request (`66eea31`).
- **delivery:** deliver findings when a pull request merges mid-review (`650f848`).
- **review:** review what fits an oversized diff and name the rest (`78faf29`).
- **design:** skip release promotions and ask for a human once per PR (`a249481`).

### Documentation

- **design:** correct what the design level now costs per push (`cba6917`).

## [0.20.0] - 2026-09-01

### Added

- **review:** count why the verifier rejects a candidate (`16f3684`).

### Fixed

- **state:** treat a lock a live run cannot take as a safe skip (`32ccc0b`).
- **design:** let the design verification correct itself once (`1b5c807`).

### Testing

- **cli:** pin which failures a later run may repeat (`da1b5eb`).

## [0.19.0] - 2026-08-31

### Added

- **review:** have the verification pass judge candidates by reference (`0512967`).

### Changed

- **github:** trim the design comment to its content (`c093889`).

### Documentation

- **scheduling:** hold a power assertion for the duration of a run (`3eaa0b7`).
- **spec:** drop the recovery path a referenced judgment cannot need (`1c09eb6`).

## [0.18.0] - 2026-08-31

### Added

- **cli:** budget a verification pass by how many candidates it checks (`7bbb794`).
- **review:** add an advisory solution-design level before the code review (`bc0a8dc`).

### Fixed

- **engine:** cite prior comments by reference instead of GitHub ID (`239195f`).
- **engine:** state how exactly an evidence excerpt must be copied (`6174fd8`).
- **engine:** let the level decide what identifies a model response (`a5ae632`).
- **cli:** stop re-reviewing a merge conflict neither side has changed (`fe73ecc`).

## [0.17.0] - 2026-08-27

### Added

- **engine:** require a finding's trigger to match how code is reached (`0fb780d`).

### Fixed

- **review:** review a pull request whose binary file has no patch (`aba42a3`).

## [0.16.0] - 2026-08-27

### Added

- **cli:** journal each step of a parallel run as it happens (`70384cb`).

## [0.15.0] - 2026-08-25

### Added

- **engine:** make the verifier explain each candidate it rejects (`1943a3b`).
- **workspace:** link the dependencies a changed lockfile left alone (`72695a4`).

### Fixed

- **cli:** leave the live row to the board during parallel reviews (`f0a0a30`).

## [0.14.0] - 2026-08-25

### Added

- **cli:** report the candidates verification rejected (`dac8b0b`).

## [0.13.0] - 2026-08-25

### Added

- **cli:** keep run transcripts for a configurable number of days (`139a061`).
- **delivery:** say how many candidates verification threw out (`359ec1d`).

### Fixed

- **engine:** keep one engine's tool-fault rule out of every pass (`20afd70`).

## [0.12.1] - 2026-08-21

### Fixed

- **cli:** fit the review board to the terminal and keep an interrupted log (`01e927a`).
- **antigravity:** stop an invented path from destroying a whole review (`360eb65`).
- **cli:** give overlapping runs separate transcripts (`60fee5e`).

## [0.12.0] - 2026-08-21

### Added

- **cli:** review several pull requests at the same time (`89aeb67`).

### Fixed

- **antigravity:** spend the one re-review on the fault it can fix (`9d8c57f`).
- **cli:** erase the progress line before an interrupting message (`9c43027`).
- **cli:** keep a parallel review's report whole and its profile reused (`c4fd7f3`).

## [0.11.1] - 2026-08-20

### Fixed

- **antigravity:** recover the review a failed tool call throws away (`5e3b0dd`).

## [0.11.0] - 2026-08-20

### Added

- **review:** re-review a confirmed defect the verifier could not evidence (`5610ede`).
- **cli:** sweep a bright window across the progress line (`aa107dd`).

### Fixed

- **cli:** wait for mergeability and report rejections usefully (`2239438`).
- **antigravity:** explain the failures the adapter used to swallow (`d285b2b`).

## [0.10.1] - 2026-08-19

### Fixed

- **engine:** keep scheduled runs authenticated, bounded, and private (`8725d79`).

### Changed

- **cli:** state the planned engines at startup (`e7b7b94`).

## [0.10.0] - 2026-08-19

### Added

- **review:** review with a fallback engine when the primary fails (`a66e20b`).

### Fixed

- **delivery:** attribute verification only when a pass ran (`2719fad`).

### Documentation

- record why the project supports Python 3.9 (`acd3a3f`).

## [0.9.0] - 2026-08-18

### Added

- **review:** require installed-package evidence for dependency claims (`97270f1`).
- **delivery:** report the verifying engine in review attribution (`0912778`).

## [0.8.0] - 2026-08-17

### Added

- **cli:** report announced GitHub incidents on failure (`db233dd`).

## [0.7.5] - 2026-08-17

### Changed

- **cli:** separate and indent review progress output (`3d996e8`).

## [0.7.4] - 2026-08-17

### Fixed

- **review:** accept renames without content change (`68fdfe6`).

## [0.7.3] - 2026-08-17

### Documentation

- **workflow:** require deleting merged branches (`c63ab6b`).

## [0.7.2] - 2026-08-17

### Fixed

- **cli:** remove bouncing dot spinner (`f11a1fc`).
- **config:** add reviewer identity resolution fallbacks (`b79ca61`).

## [0.7.1] - 2026-08-14

### Fixed

- **review:** classify ineligible PRs as skips (`f4c074d`).
- **cli:** align progress status text (`be3a912`).

## [0.7.0] - 2026-08-14

### Added

- **cli:** randomize progress animations (`1745628`).

## [0.6.0] - 2026-08-14

### Added

- **cli:** show interactive review progress (`1fa6e29`).

### Fixed

- **review:** restart when pull request head changes (`3ce5946`).
- **antigravity:** retry transient provider failures (`2f6665e`).
- **review:** repair unique evidence line citations (`277805c`).
- **antigravity:** surface hidden provider failures (`8044b3a`).
- **review:** validate approval blockers (`ffec842`).

## [0.5.2] - 2026-08-13

### Testing

- **release:** retry transient cleanup failures (`f947c8d`).

## [0.5.1] - 2026-08-13

### Documentation

- **quality:** add TDD guidance (`af41946`).

## [0.5.0] - 2026-08-13

### Added

- **comments:** add configurable GitHub templates (`0e5651a`).

### Documentation

- **comments:** add human-readable tag guide (`7fc2369`).

## [0.4.0] - 2026-08-13

### Added

- **release:** add project guidance and release automation (`4f952ea`).

## [0.3.0] - 2026-08-13

### Added

- Implement the Antigravity review workflow (`6e9a15b`).
- Add pluggable Codex, Claude Code, and Kiro review engines (`f82c636`).
- Review immutable PR heads in detached Git worktrees (`9332325`).
- Suppress issues already reported by another reviewer (`e8c6d23`).
- Approve clean team pull requests without a review body (`b83c7a3`).
- Skip draft PRs and omit top-level review summaries (`6385d20`).
- Configure case-insensitive labels excluded from review (`d5f49f4`).
- Notify Slack about failed live runs and oversized manual reviews (`8e7e1aa`, `e26a6ea`).
- Notify Slack when a clean self-authored PR can be merged (`61153ce`).
- Report compact per-invocation review durations (`83feaf6`).
- Display the configured engine and model identity consistently (`250be64`).

### Changed

- Make the review engine provider-neutral and pluggable (`1e9c544`).
- Flatten the module layout and move application code into `src` (`a918e39`, `0c952a8`).
- Move project documentation under `docs` (`7306ce7`).
- Require full-branch evidence and adversarial verification of findings (`6b0c05b`).
- Ignore resolved threads as blockers while retaining them as review context (`299d9a6`, `beb9285`).
- Refine Slack review formatting and GitHub finding attribution (`0365b02`, `c83c14b`).
- Add complete LaunchAgent and cron scheduling guidance for macOS (`0081078`).

### Fixed

- Preserve dry-run behavior when comparing engines (`7398d28`).
- Scope Antigravity repository reads to the controlled worktree (`23a5278`).
- Keep terminal-only `LGTM` text out of approval comments (`5cd84e3`).
- Handle deletion-only files and explicitly ignored omitted patches (`0091853`, `d478702`).
- Withhold approval for conflicting pull requests (`6604979`).
- Revalidate mutable GitHub PR snapshots before delivery (`69f9dbf`).
- Parse the current Kiro structured response schema (`b84b11a`).
- Restore Python 3.9 compatibility and require private dotenv permissions (`771ce83`, `7e7ac26`).
- Retry transient GitHub read failures without retrying uncertain writes (`a1746f0`).
- Isolate oversized PR handling so other reviews continue (`3075d43`, `fae38e8`).
- Recover only unambiguous truncated finding paths (`9c0a9da`).
- Verify finding evidence against exact repository content (`d97ab7c`).
- Retry controller-rejected model output once with the validation diagnostic (`10d8435`).

### Maintenance

- Establish the generic public-safe reviewer baseline (`3c1790b`).
- Remove the obsolete implementation plan (`fed13d5`).
