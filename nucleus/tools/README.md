# Local skill packages

Reusable evolved capabilities live here as package directories. Each package must
contain:

- `tool.py` — executable Python entrypoint. It receives tool arguments as JSON on
  stdin and should write its result to stdout.
- `schema.json` — JSON Schema describing accepted arguments.
- `README.md` — package documentation for humans and future agents.
- `tests.py` — self-test script that must exit with code `0` before the package
  can be registered.
- `metadata.json` — versioning and lifecycle metadata. Required fields include
  `version`; optional deprecation fields are `deprecated` and
  `deprecation_reason`.

Skill packages may also include `evals.json`. Evals are required before a tool
can be promoted to trusted. The file can be either a list of cases or an object
with an `evals` list. Each case includes:

- `input_arguments` — the JSON object passed to `tool.py` on stdin.
- `expected_output_predicate` — a predicate object checked against the result.
  Supported checks include `exit_code`, `stdout_contains`, `stdout_equals`,
  `stderr_contains`, `json_equals`, and `json_field_equals`.
- `timeout` — per-case timeout in seconds.
- `required_permissions` — permissions this case declares for the tool, such as
  `filesystem_write`, `network`, `subprocess`, `home_directory`, or
  `host_filesystem`.

`register_tool` accepts either a legacy Python file under this directory or a
package directory under this directory. Package registration validates
`schema.json`, runs `tests.py`, records declared eval permissions when
`evals.json` is present, and always registers new tools as `trusted: false`.
Use `promote_tool_trust()` to mark a registered tool trusted only after schema
validation, tests, package evals, and undeclared-permission checks pass.
