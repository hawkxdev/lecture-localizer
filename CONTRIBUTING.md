# Contributing

lecture-localizer is at the design stage. Useful contributions include workflow feedback, concrete use cases, documentation corrections, and small examples using synthetic or rights-cleared media.

## Discuss a change

Open an issue describing the user problem, inputs, expected outputs, and how to judge success. Search existing issues first. Agree the scope before starting a feature or adding dependencies; the implementation stack and provider are not selected yet.

For documentation problems, link the affected file and explain the correction. Do not include credentials, personal data, restricted course material, or private provider responses in issues or pull requests. For vulnerabilities, follow [SECURITY.md](SECURITY.md).

## Submit a pull request

Keep each pull request focused. Explain the change, link its issue when applicable, and describe the checks performed and their limits. Write public documentation in English. See [AGENTS.md](AGENTS.md) for project layout and working constraints.

There is no application installation or test command yet. For documentation changes, read the rendered text, check relative links, and run these whitespace checks from the repository root:

```sh
git diff --check
git diff --cached --check
```

These commands inspect unstaged and staged changes to tracked files. They do not check untracked files or test media localization. Run checks locally; do not add GitHub Actions workflows.

Use only material you have the right to share. Contributions to this repository are under the [MIT License](LICENSE); review compatibility and required notices before introducing third-party material.
