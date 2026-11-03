# Project constraints

- The user authorizes downloading and running the game and mod, and bounded live verification. Clearly distinguish synthetic checks, source inspection, and actual live results.
- Keep all tests and test fixtures local and ignored by Git. Preserve existing local tests; do not delete them to satisfy this rule.
- Add or change tests only when necessary to verify meaningful behavior or a material risk. Prefer extending an existing relevant test over creating new tests or test files. Avoid large suites, redundant cases, and tests that merely mirror implementation.
- Use focused verification appropriate to each change. Do not repeatedly run broad checks without a new failure or unresolved concern.
