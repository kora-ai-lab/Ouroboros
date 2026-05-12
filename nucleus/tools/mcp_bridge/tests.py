from pathlib import Path

assert (Path(__file__).with_name("tool.py")).is_file()
assert (Path(__file__).with_name("schema.json")).is_file()
