"""Open Gazebo Harmonic with an AgileX arm URDF from agx_arm_description.

Run inside the ROS 2 container from a ROS shell:
    source /opt/agx_env/ros2_env.sh
    ros2 launch /isaac-sim/molmospaces/scripts/docker/agx_arm_gazebo.launch.py arm_type:=piper

Move a joint (from any shell with Gazebo's CLI):
    gz topic -t /model/piper/joint/joint2/0/cmd_pos -m gz.msgs.Double -p "data: 1.0"
"""

import os
import xml.etree.ElementTree as ET

import xacro
from ament_index_python.packages import get_package_share_path
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    IncludeLaunchDescription,
    OpaqueFunction,
    SetEnvironmentVariable,
)
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

ARM_TYPES = ('nero', 'piper', 'piper_h', 'piper_l', 'piper_x')
MOVABLE_JOINT_TYPES = {'revolute', 'prismatic', 'continuous'}
# Gains for holding pose against gravity; the arms' links weigh well under 2 kg.
P_GAIN, I_GAIN, D_GAIN = '200', '0', '20'


def _add_position_hold(urdf_xml: str) -> str:
    # The upstream URDFs carry no <gazebo> or ros2_control tags, so every joint
    # would go limp under gravity. Give each movable joint a position controller
    # holding a start pose inside its limits.
    robot = ET.fromstring(urdf_xml)
    for joint in robot.findall('joint'):
        if joint.get('type') not in MOVABLE_JOINT_TYPES:
            continue
        start = 0.0
        limit = joint.find('limit')
        if limit is not None and joint.get('type') != 'continuous':
            start = min(max(start, float(limit.get('lower', 0))), float(limit.get('upper', 0)))
        plugin = ET.SubElement(
            ET.SubElement(robot, 'gazebo'),
            'plugin',
            filename='gz-sim-joint-position-controller-system',
            name='gz::sim::systems::JointPositionController',
        )
        for tag, value in (
            ('joint_name', joint.get('name')),
            ('p_gain', P_GAIN),
            ('i_gain', I_GAIN),
            ('d_gain', D_GAIN),
            ('initial_position', str(start)),
        ):
            ET.SubElement(plugin, tag).text = value
    return ET.tostring(robot, encoding='unicode')


def _launch_setup(context):
    arm_type = LaunchConfiguration('arm_type').perform(context)
    effector_type = LaunchConfiguration('effector_type').perform(context)
    gui = LaunchConfiguration('gui').perform(context) == 'true'

    share = get_package_share_path('agx_arm_description')
    suffix = '_with_gripper_description.xacro' if effector_type == 'agx_gripper' else '_description.urdf'
    model_path = share / 'agx_arm_urdf' / arm_type / 'urdf' / f'{arm_type}{suffix}'
    robot_description = _add_position_hold(xacro.process_file(str(model_path)).toxml())

    # Meshes are package://agx_arm_description/... URIs; Gazebo resolves them by
    # searching GZ_SIM_RESOURCE_PATH for the agx_arm_description directory.
    resource_path = os.pathsep.join(
        p for p in (str(share.parent), os.environ.get('GZ_SIM_RESOURCE_PATH', '')) if p
    )

    gz_sim = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            str(get_package_share_path('ros_gz_sim') / 'launch' / 'gz_sim.launch.py')
        ),
        launch_arguments={'gz_args': '-r empty.sdf' if gui else '-r -s empty.sdf'}.items(),
    )

    return [
        SetEnvironmentVariable('GZ_SIM_RESOURCE_PATH', resource_path),
        gz_sim,
        Node(
            package='robot_state_publisher',
            executable='robot_state_publisher',
            parameters=[{'robot_description': robot_description}],
            output='screen',
        ),
        Node(
            package='ros_gz_sim',
            executable='create',
            arguments=['-topic', 'robot_description', '-name', arm_type],
            output='screen',
        ),
    ]


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument('arm_type', default_value='piper', choices=list(ARM_TYPES)),
        DeclareLaunchArgument('effector_type', default_value='none', choices=['none', 'agx_gripper']),
        DeclareLaunchArgument(
            'gui', default_value='true', choices=['true', 'false'],
            description='false runs the Gazebo server only (no window).',
        ),
        OpaqueFunction(function=_launch_setup),
    ])
