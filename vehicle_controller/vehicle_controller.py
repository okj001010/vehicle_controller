__author__ = "Juyong Shin"
__contact__ = "juyong3393@snu.ac.kr"

# import rclpy
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy, DurabilityPolicy

# import px4_msgs
"""msgs for subscription"""
from px4_msgs.msg import VehicleStatus
from px4_msgs.msg import VehicleLocalPosition
from px4_msgs.msg import VtolVehicleStatus
"""msgs for publishing"""
from px4_msgs.msg import VehicleCommand
from px4_msgs.msg import OffboardControlMode
from px4_msgs.msg import TrajectorySetpoint

# import math, numpy
import math
import numpy as np

class VehicleController(Node):
    
    def __init__(self):
        super().__init__('vehicle_controller')

        """
        0. Configure QoS profile for publishing and subscribing
        """
        qos_profile = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            history=HistoryPolicy.KEEP_LAST,
            depth=1
        )

        """
        1. Constants
        """
        self.WP1 = np.array([0.0, 0.0, -20.0])
        self.WP2 = np.array([500.0, 0.0, -20.0])
        self.WP3 = np.array([1000.0, 300.0, -5.0])
        self.WP4 = np.array([1500.0, 900.0, -40.0])
        self.fw_speed = 15.0
        self.mc_acceptance_radius = 0.3
        self.fw_acceptance_radius = 10.0
        self.acceptance_heading_angle = np.radians(0.5)

        """
        2. State variables
        """
        # phase description
        # -1 : before flight
        # 0 : takeoff and arm
        # i >= 1 : moving toward WP_i
        self.phase = -1
        self.kill = False

        self.vehicle_status = VehicleStatus()
        self.vtol_vehicle_status = VtolVehicleStatus()
        self.vehicle_local_position = VehicleLocalPosition()
        self.pos = np.array([0.0, 0.0, 0.0])
        self.yaw = float('nan')

        self.previous_goal = None
        self.current_goal = None
        self.mission_yaw = float('nan')

        self.transition_count = 0

        """
        3. Create Subscribers
        """
        self.vehicle_status_subscriber = self.create_subscription(
            VehicleStatus, '/fmu/out/vehicle_status', self.vehicle_status_callback, qos_profile
        )
        self.vehicle_local_position_subscriber = self.create_subscription(
            VehicleLocalPosition, '/fmu/out/vehicle_local_position', self.vehicle_local_position_callback, qos_profile
        )
        self.vtol_vehicle_status_subscriber = self.create_subscription(
            VtolVehicleStatus, '/fmu/out/vtol_vehicle_status', self.vtol_vehicle_status_callback, qos_profile
        )

        """
        4. Create Publishers
        """
        self.vehicle_command_publisher = self.create_publisher(
            VehicleCommand, '/fmu/in/vehicle_command', qos_profile
        )
        self.offboard_control_mode_publisher = self.create_publisher(
            OffboardControlMode, '/fmu/in/offboard_control_mode', qos_profile
        )
        self.trajectory_setpoint_publisher = self.create_publisher(
            TrajectorySetpoint, '/fmu/in/trajectory_setpoint', qos_profile
        )

        """
        5. timer setup
        """
        self.offboard_heartbeat = self.create_timer(0.1, self.offboard_heartbeat_callback)
        self.main_timer = self.create_timer(0.5, self.main_timer_callback)

        """
        6. takeoff and arm, flight starts!
        """
        self.takeoff_and_arm()

    """
    Services
    """
    def takeoff_and_arm(self):
        self.publish_vehicle_command(VehicleCommand.VEHICLE_CMD_NAV_TAKEOFF)
        self.publish_vehicle_command(VehicleCommand.VEHICLE_CMD_COMPONENT_ARM_DISARM, param1=1.0)
        self.phase = 0

    def get_bearing_to_next_waypoint(self, now, next):
        now2d = now[0:2]
        next2d = next[0:2]

        n = np.array([1, 0]) # North dircetion vector
        u = (next2d - now2d) / np.linalg.norm(next2d - now2d)

        yaw = np.arctan2( np.linalg.det([n, u]), np.dot(n, u) )
        self.publish_trajectory_setpoint(position_sp = now, yaw_sp = yaw)  # fix the position.

        return yaw

    """
    Callback functions for the timers
    """
    def offboard_heartbeat_callback(self):
        """offboard heartbeat signal"""
        if self.vtol_vehicle_status.vehicle_vtol_state == VtolVehicleStatus.VEHICLE_VTOL_STATE_MC:
            self.publish_offboard_control_mode(position = True)
        elif self.vtol_vehicle_status.vehicle_vtol_state == VtolVehicleStatus.VEHICLE_VTOL_STATE_FW:
            self.publish_offboard_control_mode(position = True, velocity = True)
        else: # transition
            self.publish_offboard_control_mode(position = True, velocity = True)
    
    def main_timer_callback(self):
        """Callback function for the timer."""
        if self.phase == 0:
            if self.vehicle_status.nav_state == VehicleStatus.NAVIGATION_STATE_AUTO_LOITER:
                """change to offboard mode, and advance to phase 0.5"""
                self.publish_vehicle_command(
                    VehicleCommand.VEHICLE_CMD_DO_SET_MODE, 
                    param1=1.0, # main mode
                    param2=6.0  # offboard
                )
                self.phase = 0.5
        elif self.phase == 0.5:
            """set goal to WP1, advance to phase 1"""
            self.current_goal = self.WP1
            self.publish_trajectory_setpoint(position_sp = self.current_goal)
            self.phase = 1
        elif self.phase == 1:
            if np.linalg.norm(self.pos - self.current_goal) < self.mc_acceptance_radius:
                """WP1 arrived; set goal to WP2, Heading to WP2, and advance to phase heading"""
                self.previous_goal = self.WP1
                self.current_goal = self.WP2
                self.mission_yaw = self.get_bearing_to_next_waypoint(self.pos, self.current_goal)
                self.phase = "heading"
        elif self.phase == "heading":
            if math.fabs(self.yaw - self.mission_yaw) < self.acceptance_heading_angle:
                """Heading complete; change to position ctrl mode, transition start"""
                self.publish_vehicle_command(
                    VehicleCommand.VEHICLE_CMD_DO_SET_MODE,
                    param1=1.0, # main mode
                    param2=3.0  # position ctl
                )
                self.publish_vehicle_command(
                    VehicleCommand.VEHICLE_CMD_DO_VTOL_TRANSITION, 
                    param1=float(VtolVehicleStatus.VEHICLE_VTOL_STATE_FW), 
                    param2=0.0  # normal transition
                )
                self.phase = "transition"
        elif self.phase == "transition":
            if self.vtol_vehicle_status.vehicle_vtol_state == VtolVehicleStatus.VEHICLE_VTOL_STATE_FW:
                """transition complete; change to offboard mode, publish setpoint, advance to phase 2"""
                if self.transition_count == 10:
                    self.publish_vehicle_command(
                        VehicleCommand.VEHICLE_CMD_DO_SET_MODE, 
                        param1=1.0, # main mode
                        param2=6.0  # offboard
                    )
                    self.publish_trajectory_setpoint(
                        position_sp = self.current_goal,
                        velocity_sp = self.fw_speed * (self.current_goal - self.previous_goal) / np.linalg.norm(self.current_goal - self.previous_goal)
                    )
                    self.phase = 2
                self.transition_count += 1
        elif self.phase == 2:
            if np.linalg.norm(self.pos[0:2] - self.current_goal[0:2]) < self.fw_acceptance_radius:
                self.previous_goal = self.WP2
                self.current_goal = self.WP3
                self.publish_trajectory_setpoint(
                    position_sp = self.current_goal,
                    velocity_sp = self.fw_speed * (self.current_goal - self.previous_goal) / np.linalg.norm(self.current_goal - self.previous_goal)
                )
                self.phase = 3
        elif self.phase == 3:
            if np.linalg.norm(self.pos[0:2] - self.current_goal[0:2]) < self.fw_acceptance_radius:
                self.previous_goal = self.WP3
                self.current_goal = self.WP4
                self.publish_trajectory_setpoint(
                    position_sp = self.current_goal,
                    velocity_sp = self.fw_speed * (self.current_goal - self.previous_goal) / np.linalg.norm(self.current_goal - self.previous_goal)
                )
                self.phase = 4
        elif self.phase == 4:
            pass

        print(self.phase)          
    
    """
    Callback functions for subscribers.
    """        
    def vehicle_status_callback(self, msg):
        """Callback function for vehicle_status topic subscriber."""
        self.vehicle_status = msg
    
    def vehicle_local_position_callback(self, msg):
        self.vehicle_local_position = msg
        self.pos = np.array([msg.x, msg.y, msg.z])
        self.yaw = msg.heading

    def vtol_vehicle_status_callback(self, msg):
        self.vtol_vehicle_status = msg

    """
    Functions for publishing topics.
    """
    def publish_vehicle_command(self, command, **kwargs):
        """Publish a vehicle command."""
        msg = VehicleCommand()
        msg.command = command
        msg.param1 = kwargs.get("param1", float('nan'))
        msg.param2 = kwargs.get("param2", float('nan'))
        msg.param3 = kwargs.get("param3", float('nan'))
        msg.param4 = kwargs.get("param4", float('nan'))
        msg.param5 = kwargs.get("param5", float('nan'))
        msg.param6 = kwargs.get("param6", float('nan'))
        msg.param7 = kwargs.get("param7", float('nan'))
        msg.target_system = 1
        msg.target_component = 1
        msg.source_system = 1
        msg.source_component = 1
        msg.from_external = True
        msg.timestamp = int(self.get_clock().now().nanoseconds / 1000)
        self.vehicle_command_publisher.publish(msg)
    
    def publish_offboard_control_mode(self, **kwargs):
        msg = OffboardControlMode()
        msg.position = kwargs.get("position", False)
        msg.velocity = kwargs.get("velocity", False)
        msg.acceleration = kwargs.get("acceleration", False)
        msg.attitude = kwargs.get("attitude", False)
        msg.body_rate = kwargs.get("body_rate", False)
        msg.thrust_and_torque = kwargs.get("thrust_and_torque", False)
        msg.direct_actuator = kwargs.get("direct_actuator", False)
        msg.timestamp = int(self.get_clock().now().nanoseconds / 1000)
        self.offboard_control_mode_publisher.publish(msg)
    
    def publish_trajectory_setpoint(self, **kwargs):
        msg = TrajectorySetpoint()
        msg.position = list( kwargs.get("position_sp", np.nan * np.zeros(3)) )
        msg.velocity = list( kwargs.get("velocity_sp", np.nan * np.zeros(3)) )
        msg.yaw = kwargs.get("yaw_sp", float('nan'))
        msg.timestamp = int(self.get_clock().now().nanoseconds / 1000)
        self.trajectory_setpoint_publisher.publish(msg)
        # self.get_logger().info(f"Publishing position setpoints {setposition}")

def main(args = None):
    rclpy.init(args = args)

    vehicle_controller = VehicleController()
    rclpy.spin(vehicle_controller)

    vehicle_controller.destroy_node()
    rclpy.shutdown()

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(e)