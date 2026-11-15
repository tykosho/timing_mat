# Git and Version Control Guardrails

- **Explicit Approval for Commits & Pushes**: Never run `git commit` or `git push` without explicit user review and approval for the specific changes being committed.
- **No Direct Pushes to Main**: Never push directly to `main` (or `master`). Always use feature branches and pull requests when pushing code to remotes.
- **Issue Tracking State**: Do not close or alter GitHub issues until changes have been reviewed, pushed, and verified by the user.
