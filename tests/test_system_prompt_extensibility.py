from pathlib import Path


def test_build_system_prompt_uses_policy_bound_extensibility_wording():
    source = Path("nucleus/server.py").read_text(encoding="utf-8")
    system_template = source.split('SYSTEM_TEMPLATE = """', 1)[1].split('"""', 1)[0]
    build_system_prompt_body = source.split("def build_system_prompt", 1)[1].split("\n\n\n", 1)[0]

    assert "return SYSTEM_TEMPLATE.format" in build_system_prompt_body
    assert "You can extend your capabilities by using execute_python and register_tool" in system_template
    assert "subject to policy, sandbox, approval, and test/eval gates" in system_template
    assert "Do not refuse due to missing tools" in system_template
    assert "build the needed capability" in system_template
    assert "UNLIMITED capability" not in system_template
