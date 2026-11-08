"""The universal-chart converter.

engine     raw manifests (<ns>/<microservice>.yaml) -> values tree, plus verification
importer   pulls namespaces from a cluster (or answers its reads from YAML) into that layout
sources    the inputs the command accepts, all ending in that one layout
yaml_style YAML emitting shared by engine and importer

The command is convert_to_universal_chart.py at the repo root.
"""
