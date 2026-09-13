# Security policy

Security fixes target the current code on the default branch. Older snapshots may need an update before a fix can be applied. This project does not promise a response deadline or long-term support for older releases.

## Reporting a vulnerability

Use **Security → Report a vulnerability** on the [GitHub repository](https://github.com/eikkapine/amd-gpu-tuner/security) if private reporting is available. If it is unavailable, open a minimal issue asking the maintainer for a private reporting channel; do not include exploit details, secrets or private logs in that issue.

Include the affected version, operating system, relevant entry point, impact and the smallest reproduction you can provide. Distinguish a security issue, such as unintended code execution or unauthorized file access, from an ordinary GPU stability or driver compatibility report.

## Relevant boundaries

AMD GPU Tuner communicates with the installed AMD driver through a local native bridge. Hardware tuning and frame capture may run with elevated privileges. Profiles and configuration are local inputs; only apply files you have inspected and trust.

Optional PresentMon fetching obtains an executable from upstream GitHub releases. URL restrictions, executable-format checks and recorded hashes do not replace a publisher signature or independent review. See the [user guide](docs/user-guide.md#frame-data) for disabling downloads and inspecting provenance.

Diagnostic files are created locally and may include device identifiers, application names and local paths. Review their contents before sharing them. Do not post credentials, full system dumps or unrelated private files in a public report.
