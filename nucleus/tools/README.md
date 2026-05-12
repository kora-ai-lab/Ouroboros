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

`register_tool` accepts either a legacy Python file under this directory or a
package directory under this directory. Package registration validates
`schema.json`, runs `tests.py`, and only writes the tool to `registry.json` after
both checks pass.
