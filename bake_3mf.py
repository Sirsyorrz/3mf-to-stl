#!/usr/bin/env python3
"""
Bake OrcaSlicer 3MF into a single STL, applying negative-part booleans
and fuzzy skin displacement from the project's slice settings.
Usage: python3 bake_3mf.py 'input.3mf' 'output.stl'
"""

import sys
import json
import zipfile
import xml.etree.ElementTree as ET
import numpy as np
import trimesh
from trimesh import remesh

NS_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"


def parse_3mf_transform(s):
    """Parse 3MF component transform (12 values, column-major 4x3) into 4x4 matrix."""
    v = list(map(float, s.strip().split()))
    m = np.eye(4)
    # Columns 0-2 are rotation, column 3 is translation
    m[0,0]=v[0]; m[1,0]=v[1]; m[2,0]=v[2]
    m[0,1]=v[3]; m[1,1]=v[4]; m[2,1]=v[5]
    m[0,2]=v[6]; m[1,2]=v[7]; m[2,2]=v[8]
    m[0,3]=v[9]; m[1,3]=v[10]; m[2,3]=v[11]
    return m


def extract_mesh_from_object(obj_elem, ns):
    """Extract vertices and triangles from a 3MF object element."""
    mesh_elem = obj_elem.find(f"{{{ns}}}mesh")
    if mesh_elem is None:
        return None

    vertices_elem = mesh_elem.find(f"{{{ns}}}vertices")
    triangles_elem = mesh_elem.find(f"{{{ns}}}triangles")

    if vertices_elem is None or triangles_elem is None:
        return None

    vertices = []
    for v in vertices_elem.findall(f"{{{ns}}}vertex"):
        vertices.append([float(v.get("x")), float(v.get("y")), float(v.get("z"))])

    triangles = []
    for t in triangles_elem.findall(f"{{{ns}}}triangle"):
        triangles.append([int(t.get("v1")), int(t.get("v2")), int(t.get("v3"))])

    if not vertices or not triangles:
        return None

    return trimesh.Trimesh(
        vertices=np.array(vertices),
        faces=np.array(triangles),
        process=False
    )


def bake_3mf(input_path, output_path):
    print(f"Reading: {input_path}")

    with zipfile.ZipFile(input_path, "r") as zf:
        # Load all object model files
        object_models = {}
        for name in zf.namelist():
            if name.startswith("3D/Objects/") and name.endswith(".model"):
                with zf.open(name) as f:
                    object_models[name] = ET.parse(f).getroot()

        # Load main model (has component transforms)
        with zf.open("3D/3dmodel.model") as f:
            main_model = ET.parse(f).getroot()

        # Load model_settings.config for part subtypes only
        with zf.open("Metadata/model_settings.config") as f:
            raw = f.read().decode("utf-8")
        raw = raw.replace('slic3rpe:', 'slic3rpe_')
        settings = ET.fromstring(raw)

        # Load project_settings.config (JSON) for slice parameters
        project_settings = {}
        if "Metadata/project_settings.config" in zf.namelist():
            with zf.open("Metadata/project_settings.config") as f:
                project_settings = json.loads(f.read().decode("utf-8"))

    # Get subtype per part id from model_settings.config
    part_subtypes = {}
    for obj in settings.findall("object"):
        for part in obj.findall("part"):
            pid = int(part.get("id"))
            part_subtypes[pid] = part.get("subtype", "normal_part")

    # Get component transforms from 3dmodel.model (these are the real positions)
    # The component objectid maps to mesh object id in the object model file
    NS_P = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
    component_transforms = {}  # objectid -> transform matrix
    resources = main_model.find(f"{{{NS_CORE}}}resources")
    for obj in resources.findall(f"{{{NS_CORE}}}object"):
        for comp in obj.findall(f".//{{{NS_CORE}}}component"):
            oid = int(comp.get("objectid"))
            t = comp.get("transform", None)
            component_transforms[oid] = parse_3mf_transform(t) if t else np.eye(4)

    # Build part_info using component transforms (not model_settings matrices)
    part_info = {}
    for pid, subtype in part_subtypes.items():
        part_info[pid] = {
            "subtype": subtype,
            "matrix": component_transforms.get(pid, np.eye(4))
        }

    # Extract meshes from object model files
    # The object model contains multiple <object> elements with sequential ids
    meshes_by_id = {}
    for model_name, root in object_models.items():
        resources = root.find(f"{{{NS_CORE}}}resources")
        if resources is None:
            continue
        for obj in resources.findall(f"{{{NS_CORE}}}object"):
            oid = int(obj.get("id"))
            mesh = extract_mesh_from_object(obj, NS_CORE)
            if mesh is not None:
                meshes_by_id[oid] = mesh

    print(f"Found {len(meshes_by_id)} mesh objects: ids {sorted(meshes_by_id.keys())}")
    print(f"Found {len(part_info)} parts in settings: ids {sorted(part_info.keys())}")

    # Apply transforms and split into normal vs negative parts
    normal_meshes = []
    negative_meshes = []

    for pid, info in sorted(part_info.items()):
        if pid not in meshes_by_id:
            print(f"  Warning: part {pid} not found in mesh objects, skipping")
            continue

        mesh = meshes_by_id[pid].copy()
        mesh.apply_transform(info["matrix"])
        subtype = info["subtype"]

        print(f"  Part {pid} ({subtype}): {len(mesh.vertices)} verts, {len(mesh.faces)} faces")

        if subtype == "normal_part":
            normal_meshes.append(mesh)
        elif subtype == "negative_part":
            negative_meshes.append(mesh)
        else:
            print(f"    Skipping modifier part (not a cutter)")

    if not normal_meshes:
        print("ERROR: No normal parts found!")
        sys.exit(1)

    print(f"\nMerging {len(normal_meshes)} normal part(s)...")
    base = trimesh.util.concatenate(normal_meshes) if len(normal_meshes) > 1 else normal_meshes[0]

    if not negative_meshes:
        print("No negative parts found, skipping boolean.")
        result = base
        result = apply_fuzzy_skin(result, project_settings)
        result.export(output_path)
        print(f"Exported: {output_path}")
        return

    print(f"Subtracting {len(negative_meshes)} negative part(s)...")
    result = base
    for i, neg in enumerate(negative_meshes):
        print(f"  Boolean difference {i+1}/{len(negative_meshes)}...")
        try:
            result = result.difference(neg, engine="manifold")
        except Exception as e:
            print(f"  Warning: manifold failed ({e}), trying blender engine...")
            try:
                result = result.difference(neg, engine="blender")
            except Exception as e2:
                print(f"  Error: {e2} — skipping this cutter")

    result = apply_fuzzy_skin(result, project_settings)

    print(f"\nExporting to: {output_path}")
    result.export(output_path)
    print(f"Done! Vertices: {len(result.vertices)}, Faces: {len(result.faces)}")


def apply_fuzzy_skin(mesh, settings):
    mode = settings.get("fuzzy_skin", "none")
    if mode == "none":
        return mesh

    thickness = float(settings.get("fuzzy_skin_thickness", 0.3))
    point_dist = float(settings.get("fuzzy_skin_point_distance", 0.8))
    seed = 42

    print(f"\nApplying fuzzy skin (mode={mode}, thickness={thickness}mm, point_distance={point_dist}mm)...")

    verts, faces = remesh.subdivide_to_size(
        mesh.vertices, mesh.faces, max_edge=point_dist, max_iter=12
    )
    subdivided = trimesh.Trimesh(vertices=verts, faces=faces, process=True)

    rng = np.random.default_rng(seed)
    normals = subdivided.vertex_normals
    displacement = rng.uniform(-thickness, thickness, len(subdivided.vertices))
    subdivided.vertices += normals * displacement[:, np.newaxis]

    print(f"  {len(mesh.vertices)} -> {len(subdivided.vertices)} vertices after subdivision")
    return subdivided


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 bake_3mf.py input.3mf output.stl")
        sys.exit(1)
    bake_3mf(sys.argv[1], sys.argv[2])
