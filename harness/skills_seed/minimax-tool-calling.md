# Skill — Minimax tool calling (HTTPS chat completions)

Topics: `minimax`, `tool-calling`, `function-calling`, `openai-compatible`

## TL;DR

Minimax M2/M2.5 supports OpenAI-standard function calling via the hosted
endpoint `api.minimax.io/v1/chat/completions`. The endpoint parses the
model's raw `<minimax:tool_call>` XML into standard `tool_calls` JSON
**when you declare `tools` in the request**.

```python
body = {
    "model": "MiniMax-M2.5",
    "messages": [...],
    "tools": [{
        "type": "function",
        "function": {
            "name": "write_file",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    }],
    "tool_choice": "auto",
}
```

## Tool loop

```python
while round < MAX:
    resp = post(body)
    msg = resp["choices"][0]["message"]
    tool_calls = msg.get("tool_calls") or []
    if not tool_calls:
        return msg["content"]  # final
    messages.append({"role": "assistant", "content": msg.get("content") or "",
                     "tool_calls": tool_calls})
    for tc in tool_calls:
        args = json.loads(tc["function"]["arguments"])
        result = dispatch(tc["function"]["name"], args)
        messages.append({"role": "tool", "tool_call_id": tc["id"],
                         "content": result})
```

## Symptom of forgetting `tools=[]`

Model emits raw XML in `content`:
```
<minimax:tool_call><invoke name="Read"><parameter name="file_path">...</parameter></invoke></minimax:tool_call>
```
Means the endpoint didn't parse — you didn't declare tools.

## Useful tool schemas for code-writing Workers

- `write_file(path, content)` — create/overwrite
- `read_file(path)` — read up to 8000 chars
- `list_dir(path)` — sorted entries
- `run_shell(command)` — whitelist verbs (cp/mv/mkdir/ls/find/rm), reject `..` and absolute paths

## Sandbox the tool execution

- Resolve target paths, ensure `target.resolve().relative_to(cwd)` passes
- Reject paths containing `..` or starting with `/` `\` `C:`
- Set 60s timeout on shell commands

Source: [MiniMax-M2 tool_calling_guide.md](https://huggingface.co/MiniMaxAI/MiniMax-M2/blob/main/docs/tool_calling_guide.md) + this framework's [LESSONS.md](../LESSONS.md) #4, #5
