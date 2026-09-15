#!/usr/bin/env python3
"""Build an Isaac Sim / URDF asset from Holybro's X650 STEP assembly."""

from pathlib import Path
import json
import math

import cadquery as cq
from cadquery import importers, exporters
import numpy as np
import trimesh
from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics, UsdShade


ROOT = Path(__file__).resolve().parent
STEP = ROOT / "source" / "x650-frame-2025-0825.stp"
MESH = ROOT / "meshes"
USD = ROOT / "usd" / "holybro_x650.usda"

# The Holybro STEP has motors but no 15.5-inch propeller geometry. These four
# motor solids locate the shaft axes in this exact STEP release.
MOTOR_SOLID_INDICES = [183, 215, 247, 279]


def cad_to_sim(shape):
    """Rotate Creo Y-up coordinates to Isaac/ROS Z-up coordinates."""
    return shape.rotate((0, 0, 0), (1, 0, 0), 90)


def export_meshes():
    MESH.mkdir(parents=True, exist_ok=True)
    compound = importers.importStep(str(STEP)).val()
    solids = compound.Solids()

    body = cad_to_sim(compound)
    exporters.export(body, str(MESH / "x650_body.stl"), tolerance=0.6, angularTolerance=0.2)
    # Keep STEP as the source of truth; use a practical visual triangle budget.
    dense = trimesh.load_mesh(MESH / "x650_body.stl", process=False)
    visual = dense.simplify_quadric_decimation(face_count=250_000)
    visual.export(MESH / "x650_body.stl")

    rotors = []
    # A lightweight, explicitly synthetic two-blade 15.5-inch visual proxy.
    radius = 393.7 / 2
    blade = (cq.Workplane("XY")
             .polyline([(18, -14), (radius, -5), (radius, 5), (18, 14)])
             .close().extrude(2.0))
    prop_proxy = blade.union(blade.rotate((0, 0, 0), (0, 0, 1), 180)).union(
        cq.Workplane("XY").circle(20).extrude(4.0))
    for rotor_no, solid_idx in enumerate(MOTOR_SOLID_INDICES):
        motor = cad_to_sim(solids[solid_idx])
        b = motor.BoundingBox()
        c = cq.Vector((b.xmin + b.xmax) / 2, (b.ymin + b.ymax) / 2, b.zmax)
        name = f"x650_prop_{rotor_no}.stl"
        exporters.export(prop_proxy, str(MESH / name), tolerance=0.35, angularTolerance=0.15)
        rotors.append({"number": rotor_no, "position_m": [c.x / 1000, c.y / 1000, c.z / 1000], "mesh": name})

    info = {
        "source_step": STEP.name,
        "coordinate_conversion": "Creo (X,Y-up,Z) -> Isaac/ROS (X,-Z,Y), metres",
        "wheelbase_m_measured": 0.64661,
        "propeller_geometry": "synthetic 15.5-inch two-blade visual proxy; source STEP contains no propellers",
        "rotors": rotors,
    }
    (ROOT / "config" / "geometry.json").write_text(json.dumps(info, indent=2) + "\n")
    return rotors


def add_mesh(stage, path, mesh_path, color):
    tm = trimesh.load_mesh(mesh_path, process=False)
    prim = UsdGeom.Mesh.Define(stage, path)
    prim.CreatePointsAttr([Gf.Vec3f(*(v / 1000.0)) for v in tm.vertices])
    prim.CreateFaceVertexCountsAttr([3] * len(tm.faces))
    prim.CreateFaceVertexIndicesAttr(tm.faces.reshape(-1).tolist())
    prim.CreateSubdivisionSchemeAttr("none")
    mat = UsdShade.Material.Define(stage, path + "_material")
    shader = UsdShade.Shader.Define(stage, path + "_material/PBR")
    shader.CreateIdAttr("UsdPreviewSurface")
    shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*color))
    mat.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
    UsdShade.MaterialBindingAPI(prim).Bind(mat)
    return prim


def add_box_collider(stage, path, size, xyz, yaw=0.0):
    cube = UsdGeom.Cube.Define(stage, path)
    cube.CreateSizeAttr(1.0)
    xf = UsdGeom.Xformable(cube)
    xf.AddTranslateOp().Set(Gf.Vec3d(*xyz))
    xf.AddRotateZOp().Set(yaw)
    xf.AddScaleOp().Set(Gf.Vec3d(*size))
    cube.CreateVisibilityAttr("invisible")
    UsdPhysics.CollisionAPI.Apply(cube.GetPrim())


def build_usd(rotors):
    USD.parent.mkdir(parents=True, exist_ok=True)
    stage = Usd.Stage.CreateNew(str(USD))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    root = UsdGeom.Xform.Define(stage, "/World/HolybroX650")
    stage.SetDefaultPrim(root.GetPrim())
    UsdPhysics.RigidBodyAPI.Apply(root.GetPrim())
    mass = UsdPhysics.MassAPI.Apply(root.GetPrim())
    mass.CreateMassAttr(2.0)
    mass.CreateCenterOfMassAttr(Gf.Vec3f(0, 0, 0))
    mass.CreateDiagonalInertiaAttr(Gf.Vec3f(0.085, 0.085, 0.16))

    add_mesh(stage, "/World/HolybroX650/Visuals/Body", MESH / "x650_body.stl", (0.10, 0.11, 0.12))
    for r in rotors:
        p = f"/World/HolybroX650/Rotors/rotor_{r['number']}"
        xf = UsdGeom.Xform.Define(stage, p)
        UsdGeom.Xformable(xf).AddTranslateOp().Set(Gf.Vec3d(*r["position_m"]))
        add_mesh(stage, p + "/Propeller", MESH / r["mesh"], (0.05, 0.05, 0.05))
        xf.GetPrim().CreateAttribute("x650:rotorIndex", Sdf.ValueTypeNames.Int).Set(r["number"])

    # Fast, stable proxy collision geometry; detailed CAD remains visual-only.
    add_box_collider(stage, "/World/HolybroX650/Colliders/Center", (0.16, 0.16, 0.10), (0, 0, 0))
    add_box_collider(stage, "/World/HolybroX650/Colliders/ArmA", (0.58, 0.035, 0.035), (0, 0, 0.016), 45)
    add_box_collider(stage, "/World/HolybroX650/Colliders/ArmB", (0.58, 0.035, 0.035), (0, 0, 0.016), -45)
    add_box_collider(stage, "/World/HolybroX650/Colliders/LandingLeft", (0.32, 0.025, 0.025), (0, -0.159, -0.303))
    add_box_collider(stage, "/World/HolybroX650/Colliders/LandingRight", (0.32, 0.025, 0.025), (0, 0.159, -0.303))
    stage.GetRootLayer().Save()


def write_urdf(rotors):
    rotor_links = []
    rotor_joints = []
    for r in rotors:
        n = r["number"]
        x, y, z = r["position_m"]
        rotor_links.append(f'''  <link name="rotor_{n}">
    <inertial><mass value="0.01"/><origin xyz="0 0 0"/><inertia ixx="0.0001" ixy="0" ixz="0" iyy="0.0001" iyz="0" izz="0.0002"/></inertial>
    <visual><geometry><mesh filename="../meshes/{r['mesh']}" scale="0.001 0.001 0.001"/></geometry><material name="prop"/></visual>
  </link>''')
        rotor_joints.append(f'''  <joint name="rotor_{n}_joint" type="continuous">
    <parent link="base_link"/><child link="rotor_{n}"/><origin xyz="{x:.6f} {y:.6f} {z:.6f}"/><axis xyz="0 0 1"/>
    <limit effort="1" velocity="1000"/><dynamics damping="0.0001" friction="0"/>
  </joint>''')

    urdf = f'''<?xml version="1.0"?>
<robot name="holybro_x650">
  <material name="carbon"><color rgba="0.10 0.11 0.12 1"/></material>
  <material name="prop"><color rgba="0.04 0.04 0.04 1"/></material>
  <link name="base_link">
    <inertial><mass value="1.96"/><origin xyz="0 0 0"/><inertia ixx="0.085" ixy="0" ixz="0" iyy="0.085" iyz="0" izz="0.16"/></inertial>
    <visual><geometry><mesh filename="../meshes/x650_body.stl" scale="0.001 0.001 0.001"/></geometry><material name="carbon"/></visual>
    <collision><origin xyz="0 0 0"/><geometry><box size="0.16 0.16 0.10"/></geometry></collision>
    <collision><origin xyz="0 0 0.016" rpy="0 0 0.785398"/><geometry><box size="0.58 0.035 0.035"/></geometry></collision>
    <collision><origin xyz="0 0 0.016" rpy="0 0 -0.785398"/><geometry><box size="0.58 0.035 0.035"/></geometry></collision>
  </link>
{chr(10).join(rotor_links)}
{chr(10).join(rotor_joints)}
</robot>
'''
    (ROOT / "urdf" / "holybro_x650.urdf").write_text(urdf)


def main():
    for d in (ROOT / "config", ROOT / "urdf"):
        d.mkdir(parents=True, exist_ok=True)
    rotors = export_meshes()
    build_usd(rotors)
    write_urdf(rotors)
    print(json.dumps(rotors, indent=2))


if __name__ == "__main__":
    main()
