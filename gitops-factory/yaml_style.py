"""Readable YAML output shared by namespace_importer and the converter.

PyYAML writes a multi-line string as one double-quoted line full of \\n unless
asked for a literal block, and refuses the block even when asked if any line
ends in a space — common in ConfigMaps copied out of an editor. YAML itself
allows trailing spaces inside a literal block; only PyYAML's emitter is that
cautious. BlockDumper asks for `|` on every multi-line string and lets the
emitter use it whenever the value reads back unchanged, so nothing is ever
altered to get the nicer form. It also indents list items under their key.
"""
import yaml


class _ForcedBlockEmitterMixin:
    def analyze_scalar(self, scalar):
        analysis = super().analyze_scalar(scalar)
        if not analysis.allow_block and "\n" in scalar and _block_round_trips(scalar):
            analysis.allow_block = True
        return analysis


class _UncheckedBlockDumper(yaml.SafeDumper):
    def analyze_scalar(self, scalar):
        analysis = super().analyze_scalar(scalar)
        analysis.allow_block = True
        return analysis


def _block_round_trips(value: str) -> bool:
    try:
        text = yaml.dump({"k": value}, Dumper=_UncheckedBlockDumper, default_style="|")
        return yaml.safe_load(text) == {"k": value}
    except yaml.YAMLError:
        return False


def represent_str(dumper, value):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


class BlockDumper(_ForcedBlockEmitterMixin, yaml.SafeDumper):
    """SafeDumper with literal blocks for multi-line strings and indented lists."""

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


BlockDumper.add_representer(str, represent_str)


if __name__ == "__main__":
    for s in ("a  \nb\n", "x\n  \ny", "\tTab\nz\n", " lead\nx", "end space \n", "plain"):
        out = yaml.dump({"v": s, "l": [1]}, Dumper=BlockDumper, sort_keys=False)
        assert yaml.safe_load(out) == {"v": s, "l": [1]}, out
        assert ("\\n" not in out) or s.startswith(" "), out
    print(yaml.dump({"data": {"cfg": "a = 1  \nb = 2\n"}, "l": ["x"]}, Dumper=BlockDumper))
