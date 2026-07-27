# Security

RE Harness can execute local commands and capture their output. Treat task definitions and
agent prompts as code-execution inputs.

- Do not pass credentials in command arguments.
- Do not store production secrets as evidence.
- Review commands before enabling autonomous execution in sensitive repositories.
- Keep artifact directories outside public repositories when they contain private data.
- Report security issues privately to the repository owner rather than opening a public issue.
