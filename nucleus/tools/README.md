# Local skill packages

Reusable evolved capabilities live here as package directories. The nucleus keeps
only generic execution and registration primitives; workflow-specific knowledge
belongs in these packages.

## Required package files

Each package directory must contain:

- `tool.py` — executable Python entrypoint. It receives tool arguments as JSON on
  stdin and writes its result to stdout.
- `schema.json` — JSON Schema describing accepted arguments.
- `README.md` — package documentation for humans and future agents.
- `tests.py` — self-test script that must exit with code `0` before the package
  can be registered.
- `metadata.json` — versioning, lifecycle metadata, and explicit permissions.
  Required fields include `version` and `permissions`; optional deprecation
  fields are `deprecated` and `deprecation_reason`.
- `evals.json` — reusable evaluation cases that describe representative inputs,
  expected properties, edge cases, and regression scenarios.

`register_tool` accepts either a legacy Python file under this directory or a
package directory under this directory. Package registration validates
`schema.json`, validates `metadata.json`, parses `evals.json`, runs `tests.py`,
and only writes the tool to `registry.json` after those checks pass.

## Kernel boundary

The kernel must not know browser selectors, shell commands, application names,
websites, or other concrete automation recipes. The kernel can discover,
prototype, test, and register a needed capability through `execute_python`, but
all durable operational details must live in the package implementation,
package README, tests, or eval cases.

When no registered tool can satisfy a task, the agent follows this generic
"discover needed capability" path:

1. Use `execute_python` to inspect the local environment and prototype the
   smallest capability needed for the task.
2. Exercise the prototype with representative inputs and inspect stdout, stderr,
   exit code, and produced artifacts.
3. If reusable, convert the prototype into a package with the required files
   listed above.
4. Declare package permissions in `metadata.json`, run `tests.py`, then register
   the package with `register_tool` only after tests pass.
5. Use the registered package to complete the task, keeping concrete selectors,
   commands, apps, and sites inside package scope rather than kernel prompts.

## `metadata.json` permission contract

Every package must declare a non-empty `permissions` object. Permission names are
capability-level labels; values should describe scope and approval needs.

Example:

```json
{
  "version": "1.0.0",
  "permissions": {
    "filesystem.read": {"scope": "workspace", "requires_approval": false},
    "network.request": {"scope": "declared endpoints", "requires_approval": true}
  },
  "deprecated": false,
  "deprecation_reason": ""
}
```

## Optional package specs

These specs describe optional reusable packages that may be created when a task
needs them. They are intentionally generic; each implementation supplies its own
concrete commands, selectors, app names, or endpoints inside its package.

### Shell automation package

- **Package goal:** run audited command workflows and capture structured process
  results.
- **Required files:** `tool.py`, `schema.json`, `README.md`, `tests.py`,
  `metadata.json`, `evals.json`.
- **`schema.json` should define:** requested operation, arguments, working
  directory policy, environment policy, timeout, dry-run flag, and output limits.
- **`metadata.json` permissions:** declare process execution, filesystem read or
  write scopes, environment-variable access, and network access if the workflow
  can spawn network-capable processes.
- **`tests.py` should verify:** safe no-op execution, timeout handling, rejected
  disallowed operations, stdout/stderr capture, and nonzero exit reporting.
- **`evals.json` should include:** success, failure, timeout, permission-denied,
  dry-run, and large-output cases.

### Browser automation package

- **Package goal:** automate browser sessions without putting selectors or sites
  in the kernel.
- **Required files:** `tool.py`, `schema.json`, `README.md`, `tests.py`,
  `metadata.json`, `evals.json`.
- **`schema.json` should define:** task intent, navigation targets or session
  handles, interaction steps, extraction requests, viewport/headless settings,
  timeout, and artifact capture options.
- **`metadata.json` permissions:** declare browser launch/control, network
  access, credential/session access, screenshot or download writes, and any
  persistence of cookies or profiles.
- **`tests.py` should verify:** local/static-page interactions, selector failure
  behavior, artifact creation, timeout handling, and redaction of sensitive
  fields.
- **`evals.json` should include:** navigation, form interaction, extraction,
  blocked/unavailable target, and screenshot/download scenarios.

### Desktop UI automation package

- **Package goal:** interact with local graphical interfaces while app-specific
  details remain package-local.
- **Required files:** `tool.py`, `schema.json`, `README.md`, `tests.py`,
  `metadata.json`, `evals.json`.
- **`schema.json` should define:** high-level UI action, target window/session
  hints, input events, image/text recognition options, wait policy, and artifact
  capture.
- **`metadata.json` permissions:** declare desktop input control, screen capture,
  accessibility APIs, filesystem write scope for artifacts, and any credential or
  clipboard access.
- **`tests.py` should verify:** mocked or sandboxed UI actions, failure on
  missing targets, screenshot/artifact handling, and bounded waits.
- **`evals.json` should include:** successful action, missing UI element,
  ambiguous target, permission-denied, and artifact-regression cases.

### File/project automation package

- **Package goal:** inspect, transform, and validate project files with explicit
  filesystem boundaries.
- **Required files:** `tool.py`, `schema.json`, `README.md`, `tests.py`,
  `metadata.json`, `evals.json`.
- **`schema.json` should define:** operation type, file globs, excluded paths,
  transformation parameters, diff mode, backup/checkpoint policy, and output
  format.
- **`metadata.json` permissions:** declare filesystem read/write scopes, project
  metadata access, process execution if validators are run, and network access if
  dependencies or remote metadata may be fetched.
- **`tests.py` should verify:** fixture-based reads/writes, diff-only mode,
  excluded path enforcement, rollback/backup behavior, and validator reporting.
- **`evals.json` should include:** single-file edit, multi-file edit, no-op,
  invalid path, excluded path, and validator-failure cases.

### Document automation package

- **Package goal:** parse, generate, convert, or annotate documents while format
  specifics stay in the package.
- **Required files:** `tool.py`, `schema.json`, `README.md`, `tests.py`,
  `metadata.json`, `evals.json`.
- **`schema.json` should define:** input document references, output format,
  extraction or transformation request, page/range filters, OCR options,
  metadata policy, and artifact destination.
- **`metadata.json` permissions:** declare filesystem read/write scopes, OCR or
  model invocation, network access for remote documents, and temporary-file
  handling.
- **`tests.py` should verify:** fixture parsing, conversion/extraction output,
  malformed document handling, page/range boundaries, and artifact cleanup.
- **`evals.json` should include:** extraction, conversion, annotation, malformed
  file, empty document, and large-document cases.
