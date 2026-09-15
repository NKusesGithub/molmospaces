# Holybro X650 asset for NVIDIA Isaac Sim

This package was generated from Holybro's `x650-frame-2025-0825.stp`. It contains a directly openable USD asset, an editable URDF, detailed visual meshes, lightweight collision proxies, and measured rotor locations.

## Fastest use in Isaac Sim

1. Extract the ZIP without changing the folder layout.
2. Open `usd/holybro_x650.usda` in Isaac Sim.
3. Add the asset to a physics scene. The USD is Z-up and uses metres.

Alternatively, choose **File > Import**, select `urdf/holybro_x650.urdf`, and set the USD output folder. Keep the four continuous rotor joints if you want visible propeller animation.

## Model facts and defaults

- Motor-shaft centres are measured from the supplied CAD, not guessed from the product name.
- Measured opposite-motor wheelbase: approximately 646.61 mm (nominal product wheelbase: 650 mm).
- Holybro's STEP has no propeller solids. The included four propeller meshes are lightweight, explicitly synthetic 15.5-inch two-blade visual proxies matching the published Gemfan 1555 size.
- Default mass: 2.0 kg without battery, matching Holybro's published full-kit weight.
- Default inertia is an engineering placeholder: `(0.085, 0.085, 0.160) kg m^2`.
- The detailed CAD is used only for rendering. Simple box colliders are used for performance and stability.
- Creo's Y-up CAD coordinates were converted to ROS/Isaac Z-up coordinates.

## Rotor convention

The CAD does not contain PX4 motor numbering or spin direction. The package therefore stores geometric rotor indices only. Confirm the mapping against the selected PX4 airframe before connecting actuator outputs.

Suggested PX4 quad-X mapping to verify on your vehicle:

| Geometric rotor | Position `(x, y)` m | Assign after verification |
|---|---:|---|
| 0 | read from `config/geometry.json` | PX4 motor number + CW/CCW |
| 1 | read from `config/geometry.json` | PX4 motor number + CW/CCW |
| 2 | read from `config/geometry.json` | PX4 motor number + CW/CCW |
| 3 | read from `config/geometry.json` | PX4 motor number + CW/CCW |

## PX4 SITL limitation

The USD/URDF supplies geometry, mass properties, colliders and rotor frames. Isaac Sim does not infer multicopter aerodynamics from CAD. A flight-dynamics integration (for example, your chosen Isaac Sim PX4 bridge) must apply thrust and reaction torque at the four rotor frames and exchange MAVLink/HIL data with PX4 SITL.

Before flight-dynamics tuning, replace the placeholder inertia with measured or CAD/material-derived values and set the actual all-up mass including battery, Pixhawk, Jetson, SIYI equipment and payload.

## Rebuild

`build_x650_isaac_asset.py` recreates the meshes, USD, URDF and geometry metadata from the source STEP file. It requires Python packages `cadquery`, `trimesh`, `fast-simplification`, `numpy`, and `usd-core`.
