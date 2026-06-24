# Security Policy

AIWatcher Local is designed to be local-only and read-only. It does not make LLM
API calls, does not phone home, and does not upload prompts or source code. If
you believe any of these guarantees is violated, we want to hear from you.

## Reporting a vulnerability

Please report security issues privately. **Do not open a public issue for
suspected vulnerabilities.**

- Use GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
  ("Report a vulnerability" under the **Security** tab), or
- Email **security@getaiwatcher.com**.

Please include:

- A description of the issue and its impact.
- Steps to reproduce, or a proof of concept.
- The version / commit you tested, and your OS and Python version.

We aim to acknowledge reports within 3 business days and to provide a remediation
timeline after triage.

## Scope

In scope:

- The `aiwatcher_cli` package in this repository.
- Any behavior that causes AIWatcher Local to make network calls, upload data, or
  read files outside its documented local sources.

Out of scope:

- The AIWatcher Enterprise / cloud product, which is maintained separately.
- Third-party AI coding tools whose local history AIWatcher Local reads.

## Disclosure

We follow coordinated disclosure. Please give us a reasonable window to release a
fix before any public disclosure.
