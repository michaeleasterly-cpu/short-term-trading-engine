---
name: python-fstring-no-backslashes
description: "Python 3.11 f-strings can't contain backslashes inside expressions — don't write f\"{d[\\\"key\\\"]}\" in inline python3 -c; use a temp variable, .get(), or single-quoted keys instead"
metadata:
  node_type: memory
  type: feedback
  originSessionId: b2b99da1-0c8f-4f26-9e63-d51fb8800654
---
Python 3.11's f-string grammar forbids backslashes inside the *expression* portion (the `{...}` part). `\"` inside an inline `python3 -c "f'...'"` is a frequent SyntaxError when piping JSON output through Python one-liners.

**Why:** Triggers `SyntaxError: f-string expression part cannot include a backslash` and breaks the whole pipeline silently when wrapped in a poll/loop — the loop runs forever printing tracebacks instead of producing useful output.

**How to apply:** When writing inline `python3 -c` scripts that read JSON, never escape inner quotes inside f-string expressions. Three working patterns:

1. **Pull values into local vars first, then format:**
   ```python
   name = node["name"]; status = ld["status"] if ld else "none"
   print(f"{name:22} {status}")
   ```

2. **Use string concatenation or `.format()` instead of f-strings:**
   ```python
   print("{name:22} {status}".format(name=node["name"], status=ld["status"]))
   ```

3. **Keep the f-string but use only single-quoted dict keys (which need no escaping):**
   ```python
   print(f"{node['name']:22} {ld['status'] if ld else 'none'}")
   ```

Python 3.12 lifted this restriction (PEP 701), but the project venv is 3.11 — assume backslashes-in-f-strings will fail.
