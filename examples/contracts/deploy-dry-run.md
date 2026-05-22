# Validation Contract: Deploy Script Dry-Run Support

This contract defines the acceptance criteria for adding a `--dry-run` flag to the deployment script.

## Acceptance Criteria

### AC-1: CLI Flag Parsing
- The script must accept `--dry-run` as a command-line argument.
- It should default to `False`.

### AC-2: Guard Mutation Calls
- When `--dry-run` is active, no files should be uploaded or modified on the server.
- Database migration commands must be skipped in dry-run mode.

### AC-3: Logging Intent
- In dry-run mode, the script must log exactly what it *would* have done (e.g., "DRY-RUN: Would upload file 'X' to server 'Y'").

### AC-4: Error Handling
- Dry-run mode should not mask potential configuration errors (e.g., missing credentials).

### AC-5: Integration Test
- A test case must verify that `--dry-run` prevents actual mutations while still exercising the command-line parsing and logic flow.
