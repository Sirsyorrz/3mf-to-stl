# 3mf-to-stl

Bakes OrcaSlicer 3MF files (with negative/cutter parts) into a single merged STL.

## What it does

Takes a 3MF file containing normal parts and negative (cutter) parts, applies all transforms, runs boolean difference, and exports a clean STL.

## Usage

```bash
python3 bake_3mf.py input.3mf output.stl
```

## Requirements

```bash
pip install trimesh numpy
```

Manifold boolean engine is used by default, falls back to Blender if it fails.
