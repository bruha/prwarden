# Security policy

PR Warden runs authenticated agent CLIs over pull requests it does not
trust, and it can comment on, approve, and announce those pull requests. A flaw
in how it contains them matters, so please report one privately.

## Supported versions

Only the latest minor release receives security fixes. A fix ships as a new patch
release of that minor version; earlier releases are not patched.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: open the repository's **Security**
tab and choose **Report a vulnerability**. Please do not open a public issue, pull
request, or discussion about it. If the form is unavailable, open an issue that
asks for a private contact and contains no details.

A useful report includes:

- the version (`python3 -m src.prwarden --version`) and the review engine
  and its CLI version;
- what an attacker controls, such as the pull-request diff, title, body, branch
  name, comments, or files in the reviewed head;
- the steps or a minimal pull request that reproduces it;
- what happens as a result: what was posted, approved, sent, run, read, or leaked.

## What to expect

This project has one maintainer, so these are best-effort targets rather than
guarantees:

- an acknowledgement within 7 days;
- an assessment, and a planned fix or a reasoned decline, within 30 days;
- a patch release and a published GitHub security advisory once a fix is
  available, crediting you unless you ask not to be named.

Please give the fix a chance to ship before disclosing the issue publicly.

## Scope

In scope, because the controller is responsible for them:

- anything that makes the reviewer post, approve, or send to Slack something its
  own rules forbid, such as an approval without verified evidence, a finding on a
  line the pull request did not add, or a delivery to a pull request whose head
  moved;
- untrusted pull-request content escaping its containment in a prompt, or being
  treated as instructions by the controller itself;
- a reviewed head making an agent CLI run commands, load configuration, or read
  files outside the review's limits through anything the adapter passes or
  permits;
- leaks of the Slack webhook, the GitHub token, or other secrets into a prompt,
  comment, message, log, or state file;
- state corruption, duplicate deliveries, or command injection.

Out of scope:

- vulnerabilities in the agent CLIs or model providers themselves; please report
  those to their vendors;
- a model ignoring its instructions when the controller still blocks the result;
- risks that follow from the operator's own configuration, such as forwarding
  secrets through `REVIEW_ENGINE_ENV_ALLOWLIST` or running an arbitrary command
  through the `command` engine.

[The threat model](docs/threat-model.md) lists what the reviewer defends against,
where each defence lives, and the risk that remains. A report showing that a listed
defence does not hold is especially welcome.
