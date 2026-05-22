# Skill — Claude CLI headless invocation (Windows, subscription)

Topics: `claude-code`, `subprocess`, `windows`, `headless`, `worker`

## TL;DR

```python
cmd = [
    binary,
    "-p",                                  # headless / print mode
    "--model", "claude-opus-4-7",
    "--output-format", "json",
    "--permission-mode", "acceptEdits",
    "--allowedTools", "Bash,Read,Edit,Write,Glob,Grep,LS",
    "--append-system-prompt", soul,        # APPEND, not REPLACE
]
subprocess.run(cmd, input=user_prompt, ...) # user prompt via STDIN, not argv
```

## Why each choice

- `--append-system-prompt` (NOT `--system-prompt`): the replace flag drops
  Claude Code's default agent-loop directives → model reverts to chat mode
  and replies "I'll wait for your request". Append preserves them.
- User prompt via stdin: Windows argv has 8191-char limit → long prompts
  silently truncate. Use `subprocess.run(input=user, ...)` with `-p` bare.
- `--permission-mode acceptEdits`: auto-approves Write/Edit + fs-changing
  shell commands (mkdir/cp/mv/touch). Other Bash commands need `--allowedTools`.
- `--allowedTools Bash,Read,Edit,Write,Glob,Grep,LS`: the standard coding
  toolset. Add more if subtask needs specific tools.

## Failure signatures and fixes

| Symptom | Fix |
|---|---|
| `"I'll wait for your request"` | Switch to `--append-system-prompt` |
| Output not JSON despite `--output-format json` | Same — model returned chat-mode plain text |
| `"Your message got cut off"` | Long prompt; move to stdin |
| Files written to `C:\home\user\` or `C:\tmp\` | Sonnet path-normalizes Temp/Users paths; use Desktop or custom cwd |
| `claude-sonnet-4-6 not a valid model` (sometimes) | Confirm the model name with `codex doctor` equivalent; can be subscription-specific |

## Do NOT use

- `--bare`: forces API-key auth, breaks subscription login
- `--system-prompt`: see above
- positional `-p "long prompt"`: argv truncation
- `subprocess.run(stdin=subprocess.DEVNULL)` for Codex (different gotcha, see codex skill)

Source: official headless docs + this framework's [LESSONS.md](../LESSONS.md) #1, #2, #9
