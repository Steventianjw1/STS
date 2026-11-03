# Project constraints

- Scope: these instructions apply project-wide to every chat and all future chats working in this repository.
- Write tests only when absolutely necessary. If a test file needs changes for a task, make those edits in one pass; do not repeatedly revise tests. Prefer the smallest necessary change to an existing relevant test and avoid redundant tests or new test files.
- Create or update Markdown documents only when explicitly instructed by the user or when providing instructions to another agent. Do not generate unsolicited documentation or reports.
- After each code implementation, briefly summarize every changed file: what changed and how its functionality is implemented.
- The user authorizes downloading and running the game and mod, and bounded live verification. Clearly distinguish synthetic checks, source inspection, and actual live results.
- Keep all tests and test fixtures local and ignored by Git. Preserve existing local tests; do not delete them to satisfy this rule.
- Use focused verification appropriate to each change. Do not repeatedly run broad checks without a new failure or unresolved concern.
