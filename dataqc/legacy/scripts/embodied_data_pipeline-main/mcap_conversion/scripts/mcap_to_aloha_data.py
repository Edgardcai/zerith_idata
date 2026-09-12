#! /usr/bin/env python3.10
import argparse
import json
import os
import struct
import subprocess

import cv2
import numpy as np
import yaml

# ROS Humble's transforms3d package still references the removed np.float alias
# when user-site NumPy >= 1.24 is present.
if not hasattr(np, "float"):
    np.float = float  # type: ignore[attr-defined]

from rosbag2_py import StorageOptions, ConverterOptions
from rclpy.serialization import deserialize_message
from rosidl_runtime_py.utilities import get_message
import rosbag2_py
import time
import sys
import tf_transformations


def sequence_to_list(values):
    if hasattr(values, "tolist"):
        return values.tolist()
    return list(values)


def ros_image_to_cv2(msg):
    if not all(hasattr(msg, attr) for attr in ("height", "width", "encoding", "step", "data")):
        return None

    encoding = msg.encoding.lower()
    channels_by_encoding = {
        "mono8": 1,
        "8uc1": 1,
        "rgb8": 3,
        "bgr8": 3,
        "rgba8": 4,
        "bgra8": 4,
        "mono16": 1,
        "16uc1": 1,
    }
    dtype_by_encoding = {
        "mono8": np.uint8,
        "8uc1": np.uint8,
        "rgb8": np.uint8,
        "bgr8": np.uint8,
        "rgba8": np.uint8,
        "bgra8": np.uint8,
        "mono16": np.uint16,
        "16uc1": np.uint16,
    }
    if encoding not in channels_by_encoding:
        return None

    height = int(msg.height)
    width = int(msg.width)
    step = int(msg.step)
    channels = channels_by_encoding[encoding]
    dtype = dtype_by_encoding[encoding]
    bytes_per_pixel = np.dtype(dtype).itemsize * channels
    expected_row_bytes = width * bytes_per_pixel
    required_bytes = height * step

    buffer = msg.data if isinstance(msg.data, (bytes, bytearray, memoryview)) else bytes(msg.data)
    if len(buffer) < required_bytes or step < expected_row_bytes:
        return None

    rows = np.frombuffer(buffer, dtype=np.uint8, count=required_bytes).reshape(height, step)
    rows = rows[:, :expected_row_bytes].copy()
    if dtype != np.uint8:
        rows = rows.view(dtype)
    image = rows.reshape(height, width, channels) if channels > 1 else rows.reshape(height, width)

    if encoding == "rgb8":
        image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
    elif encoding == "rgba8":
        image = cv2.cvtColor(image, cv2.COLOR_RGBA2BGRA)
    return image


def write_image_message(msg, path):
    image = ros_image_to_cv2(msg)
    if image is not None:
        if not cv2.imwrite(path, image):
            raise RuntimeError(f"Failed to write image: {path}")
        return

    with open(path, 'wb') as f:
        f.write(bytes(msg.data))


def pose_from_pose_like_message(msg):
    if hasattr(msg, "pose") and hasattr(msg.pose, "pose"):
        return msg.pose.pose
    if hasattr(msg, "pose"):
        return msg.pose
    return msg


def twist_from_twist_like_message(msg):
    if hasattr(msg, "twist") and hasattr(msg.twist, "twist"):
        return msg.twist.twist
    if hasattr(msg, "twist"):
        return msg.twist
    return msg


def ns_to_sec_str(timestamp_ns) -> str:
    return f"{int(timestamp_ns) / 1e9:.6f}"


def chassis_velocity_from_string(msg):
    data = json.loads(msg.data)
    return {
        "linear": {
            "x": float(data.get("linearX", data.get("linear_x", 0.0))),
            "y": float(data.get("linearY", data.get("linear_y", 0.0))),
            "z": float(data.get("linearZ", data.get("linear_z", 0.0))),
        },
        "angular": {
            "x": float(data.get("angularX", data.get("angular_x", 0.0))),
            "y": float(data.get("angularY", data.get("angular_y", 0.0))),
            "z": float(data.get("angularZ", data.get("angular_z", 0.0))),
        },
    }


LIFT_MOTOR_FIELDS = (
    ("uint8", "motor_id"),
    ("bool", "init_state"),
    ("uint8", "ctrl_mode"),
    ("uint8", "back_vol"),
    ("float64", "back_current"),
    ("uint8", "state_bit"),
    ("bool", "run_state"),
    ("bool", "overflow_i"),
    ("bool", "overflow_v"),
    ("bool", "encode_err"),
    ("bool", "pos_bias_over"),
    ("bool", "under_vol"),
    ("bool", "over_load"),
    ("bool", "external_ctrl_mode"),
    ("int16", "motor_speed"),
    ("int16", "back_speed"),
    ("uint8", "location_complete"),
    ("int32", "back_pos"),
    ("int32", "back_height"),
    ("int64", "target_height"),
    ("int64", "target_pos"),
    ("bool", "up_limit"),
    ("bool", "down_limit"),
    ("bool", "reach_target_pos"),
    ("int64", "fps_error"),
)
LIFT_MOTOR_FORMATS = {
    "uint8": ("B", 1, 1),
    "bool": ("?", 1, 1),
    "int16": ("h", 2, 2),
    "int32": ("i", 4, 4),
    "int64": ("q", 8, 8),
    "float64": ("d", 8, 8),
}


def decode_lift_motor_cdr(raw_data):
    raw = bytes(raw_data)
    if len(raw) < 4:
        raise ValueError("LiftMotorMsg CDR payload is too short")

    offset = 4
    decoded = {}
    for field_type, field_name in LIFT_MOTOR_FIELDS:
        fmt, size, align = LIFT_MOTOR_FORMATS[field_type]
        relative_offset = offset - 4
        offset += (-relative_offset) % align
        if offset + size > len(raw):
            raise ValueError(f"LiftMotorMsg CDR payload ended before {field_name}")
        decoded[field_name] = struct.unpack_from("<" + fmt, raw, offset)[0]
        offset += size
    return decoded


def lift_motor_dict_from_message_or_raw(raw_data, topic_type):
    try:
        msg = deserialize_message(raw_data, get_message(topic_type))
        back_height = getattr(msg, "back_height", getattr(msg, "backHeight", 0.0))
        return {
            "back_height": float(back_height),
            "backHeight": float(back_height),
        }
    except Exception:
        decoded = decode_lift_motor_cdr(raw_data)
        decoded["backHeight"] = decoded["back_height"]
        return decoded

def ros_time_to_sec_str(timestamp) -> str:
    return f"{timestamp.sec+timestamp.nanosec/1e9:.6f}"
    # return f"{timestamp.sec}.{int(round(timestamp.nanosec/1000))}"


def message_timestamp_sec_str(topic, message, record_timestamp_ns, record_time_topics):
    if topic in record_time_topics:
        return ns_to_sec_str(record_timestamp_ns)
    if not hasattr(message, "header"):
        return ns_to_sec_str(record_timestamp_ns)
    return ros_time_to_sec_str(message.header.stamp)

def run_shell_command(cmd="", enable_error_output=True):
    if not enable_error_output:
        cmd += " 2> /dev/null"
    return os.popen(cmd).read()

def get_mcap_msg_num(mcap_file):
    return int(run_shell_command("mcap info " + mcap_file + " | grep -w ^messages").split()[1])

def open_mcap_reader(input_mcap_file):
    storage_options_input = StorageOptions(uri=input_mcap_file, storage_id="mcap")
    reader = rosbag2_py.SequentialReader()
    reader.open(storage_options_input, ConverterOptions("", ""))
    topic_types_dict = {t.name: t.type for t in reader.get_all_topics_and_types()}
    return reader, topic_types_dict

def recover_unreadable_mcap(input_mcap_file, data_dir, expected_msg_num, allow_partial_recover=False):
    parent_dir = os.path.basename(os.path.dirname(input_mcap_file))
    recover_dir = os.path.join(data_dir, "_mcap_recovered", parent_dir)
    create_directory(recover_dir)
    recovered_file = os.path.join(recover_dir, os.path.basename(input_mcap_file))
    if os.path.exists(recovered_file):
        os.remove(recovered_file)

    print("\n[Warn] rosbag2_py can list topics but cannot read messages from this MCAP.")
    print(f"[Warn] Attempting compatibility rewrite with mcap recover: {recovered_file}")
    subprocess.run(["mcap", "recover", input_mcap_file, "-o", recovered_file], check=True)

    recovered_msg_num = get_mcap_msg_num(recovered_file)
    reader, topic_types_dict = open_mcap_reader(recovered_file)
    if not reader.has_next():
        raise RuntimeError(
            "mcap recover created a file, but rosbag2_py still cannot read any messages: "
            f"{recovered_file}"
        )
    if expected_msg_num > 0 and recovered_msg_num < expected_msg_num and not allow_partial_recover:
        raise RuntimeError(
            "Input MCAP is not directly readable by rosbag2_py, and mcap recover only recovered "
            f"{recovered_msg_num}/{expected_msg_num} messages. Refusing to convert partial data. "
            "Use --allowPartialRecover only if you explicitly want the recovered subset."
        )
    if expected_msg_num > 0 and recovered_msg_num < expected_msg_num:
        print(f"[Warn] Continuing with partial recovered MCAP: {recovered_msg_num}/{expected_msg_num} messages.")
    return recovered_file, recovered_msg_num, reader, topic_types_dict

def open_mcap_reader_compatible(input_mcap_file, data_dir, allow_partial_recover=False):
    msg_num = get_mcap_msg_num(input_mcap_file)
    reader, topic_types_dict = open_mcap_reader(input_mcap_file)
    if reader.has_next() or msg_num == 0:
        return input_mcap_file, msg_num, reader, topic_types_dict
    return recover_unreadable_mcap(input_mcap_file, data_dir, msg_num, allow_partial_recover)

def create_directory(dir_path):
    if not os.path.isdir(dir_path):
        os.makedirs(dir_path, exist_ok=True)

def print_dict(data, ss=""):
    for key, value in data.items():
        if isinstance(value, dict):
            print(f"{ss}{key}:")
            print_dict(value, f"{ss}  ")
        else:
            print(f"{ss}{key}: {value}")

def get_dict_from_index(data, index=""):
    for key, value in data.items():
        if isinstance(value, dict):
            if key == index:
                return value
            else:
                res = get_dict_from_index(value, index)
                if len(res) != 0:
                    return res
    return dict()

def get_topic_config_from_yaml(yaml_path):
    if not os.path.exists(yaml_path):
        return {}, {}

    topic_dir = {}
    sensor_type = {}
    with open(yaml_path, "r") as f:
        yaml_dict = yaml.safe_load(f)
        data_info_dict = get_dict_from_index(yaml_dict, "dataInfo")
        for key1, value1 in data_info_dict.items():
            for key2, value2 in value1.items():
                names_list = value2["names"]
                topics_list = value2["topics"]
                sensor_type_name = key1
                if len(data_info_dict[key1].keys()) > 1:
                        sensor_type_name = f"{key1}_{key2}"
                sensor_type[sensor_type_name] = topics_list
                for i in range(len(topics_list)):
                    topic_dir[topics_list[i]] = f"{key1}/{key2}/{names_list[i]}"
                if "configTopics" in value2.keys():
                    config_topics_list = value2["configTopics"]
                    sensor_type[f"{sensor_type_name}_info"] = config_topics_list
                    for i in range(len(config_topics_list)):
                        topic_dir[config_topics_list[i]] = f"{key1}/{key2}/{names_list[i]}"
    return topic_dir, sensor_type

def process_file(
    input_mcap_file,
    data_dir,
    yaml_path,
    allow_partial_recover=False,
    record_time_topics=None,
):
    record_time_topics = set(record_time_topics or ())
    parent_path = os.path.dirname(input_mcap_file)
    parent_dir = os.path.basename(parent_path)
    output_dir_head = f"{data_dir}/aloha/{parent_dir}/"
    print(f"\n>>> {input_mcap_file}")
    # print(f"输入mcap为 {input_mcap_file}")
    print(f"输出: {output_dir_head}")

    # 打开输入的 MCAP 文件
    input_mcap_file, msg_num, reader, topic_types_dict = open_mcap_reader_compatible(
        input_mcap_file,
        data_dir,
        allow_partial_recover,
    )

    save_topic_dirs, sensor_topic_dict = get_topic_config_from_yaml(yaml_path)
    print(f"---")
    for sensor_type, topic_list in sensor_topic_dict.items():
        print(f"{sensor_type}:")
        for topic in topic_list:
            topic_dir = save_topic_dirs[topic]
            create_directory(f"{output_dir_head}{topic_dir}")
            if topic in topic_types_dict:
                print(f"  * {topic}\t[{topic_dir}]\t: {topic_types_dict[topic]}")
            else:
                print(f"  * {topic}\t[{topic_dir}]\t: (not found)")
    print("---\n")

    # 生成aloha数据
    total_count = 0
    fail_topics = []
    start_time = time.time()
    while reader.has_next():
        total_count += 1
        sys.stdout.write('\r[%3d%%] progress ( %d / %d )' % (int(total_count/msg_num*100), total_count, msg_num))
        sys.stdout.flush()

        topic, data, timestamp = reader.read_next()
        # camera color
        if ("camera_color" in sensor_topic_dict and topic in sensor_topic_dict["camera_color"]) or \
           ("camera" in sensor_topic_dict and topic in sensor_topic_dict["camera"]):
            msg = deserialize_message(data, get_message(topic_types_dict[topic]))
            filename = message_timestamp_sec_str(topic, msg, timestamp, record_time_topics)
            write_image_message(msg, f'{output_dir_head}{save_topic_dirs[topic]}/{filename}.jpg')
        # camera depth
        elif "camera_depth" in sensor_topic_dict and topic in sensor_topic_dict["camera_depth"]:
            msg = deserialize_message(data, get_message(topic_types_dict[topic]))
            filename = message_timestamp_sec_str(topic, msg, timestamp, record_time_topics)
            write_image_message(msg, f'{output_dir_head}{save_topic_dirs[topic]}/{filename}.png')
        # camera color and depth info
        elif ("camera_color_info" in sensor_topic_dict and topic in sensor_topic_dict["camera_color_info"]) or \
            ("camera_depth_info" in sensor_topic_dict and topic in sensor_topic_dict["camera_depth_info"]) or \
            ("camera_info" in sensor_topic_dict and topic in sensor_topic_dict["camera_info"]):
            msg = deserialize_message(data, get_message(topic_types_dict[topic]))
            roi_dict = {
                "do_rectify" : int(msg.roi.do_rectify),
                "height" : msg.roi.height,
                "width" : msg.roi.width,
                "x_offset" : msg.roi.x_offset,
                "y_offset" : msg.roi.y_offset
            }
            camera_info_data_dict = {
                "D": msg.d.tolist(),
                "K": msg.k.tolist(),
                "P": msg.p.tolist(),
                "R": msg.r.tolist(),
                "binning_x": msg.binning_x,
                "binning_y": msg.binning_y,
                "distortion_model": msg.distortion_model,
                "height": msg.height,
                "roi": roi_dict,
                "width": msg.width
            }
            create_directory(f"{output_dir_head}{save_topic_dirs[topic]}")
            with open(f"{output_dir_head}{save_topic_dirs[topic]}/config.json", 'w+') as json_file:
                json.dump(camera_info_data_dict, json_file, indent="\t")
            info_timestamp = ros_time_to_sec_str(msg.header.stamp) if hasattr(msg, "header") else ns_to_sec_str(timestamp)
            with open(f"{output_dir_head}{save_topic_dirs[topic]}/camera_info_timestamps.txt", 'a') as timestamp_file:
                timestamp_file.write(f"{info_timestamp}\n")
        # arm
        elif "arm" in sensor_topic_dict and topic in sensor_topic_dict["arm"]:
            msg = deserialize_message(data, get_message(topic_types_dict[topic]))
            filename = ros_time_to_sec_str(msg.header.stamp)
            joint_state_dict = {
                "name" : sequence_to_list(msg.name),
                "effort" : sequence_to_list(msg.effort),
                "position" : sequence_to_list(msg.position),
                "velocity" : sequence_to_list(msg.velocity),
            }
            with open(f"{output_dir_head}{save_topic_dirs[topic]}/{filename}.json", 'w+') as json_file:
                json.dump(joint_state_dict, json_file, indent="\t")
        # localization
        elif "localization" in sensor_topic_dict and topic in sensor_topic_dict["localization"]:
            msg = deserialize_message(data, get_message(topic_types_dict[topic]))
            filename = ros_time_to_sec_str(msg.header.stamp)
            pose = pose_from_pose_like_message(msg)
            position = pose.position
            orientation = pose.orientation
            orientation_list = [orientation.x, orientation.y, orientation.z, orientation.w]
            rpy_list = tf_transformations.euler_from_quaternion(orientation_list)
            pose_dict = {
                "pitch" : rpy_list[1],
                "roll" : rpy_list[0],
                "x" : position.x,
                "y" : position.y,
                "yaw" : rpy_list[2],
                "z" : position.z,
            }
            with open(f"{output_dir_head}{save_topic_dirs[topic]}/{filename}.json", 'w+') as json_file:
                json.dump(pose_dict, json_file, indent="\t")
        # robot base velocity
        elif "robotBase" in sensor_topic_dict and topic in sensor_topic_dict["robotBase"]:
            msg = deserialize_message(data, get_message(topic_types_dict[topic]))
            filename = ros_time_to_sec_str(msg.header.stamp) if hasattr(msg, "header") else ns_to_sec_str(timestamp)
            if topic_types_dict[topic] == "std_msgs/msg/String":
                vel_dict = chassis_velocity_from_string(msg)
            else:
                twist = twist_from_twist_like_message(msg)
                vel_dict = {
                    "linear": {
                        "x": twist.linear.x,
                        "y": twist.linear.y,
                        "z": twist.linear.z,
                    },
                    "angular": {
                        "x": twist.angular.x,
                        "y": twist.angular.y,
                        "z": twist.angular.z,
                    },
                }
            with open(f"{output_dir_head}{save_topic_dirs[topic]}/{filename}.json", 'w+') as json_file:
                json.dump(vel_dict, json_file, indent="\t")
        # lift motor
        elif "lift" in sensor_topic_dict and topic in sensor_topic_dict["lift"]:
            filename = ns_to_sec_str(timestamp)
            lift_dict = lift_motor_dict_from_message_or_raw(data, topic_types_dict[topic])
            with open(f"{output_dir_head}{save_topic_dirs[topic]}/{filename}.json", 'w+') as json_file:
                json.dump(lift_dict, json_file, indent="\t")
        # gripper
        elif "gripper" in sensor_topic_dict and topic in sensor_topic_dict["gripper"]:
            msg = deserialize_message(data, get_message(topic_types_dict[topic]))
            filename = ros_time_to_sec_str(msg.header.stamp)
            angle = msg.angle
            distance = msg.distance
            gripper_dict = {
                "angle": angle,
                "distance": distance,
            }
            with open(f"{output_dir_head}{save_topic_dirs[topic]}/{filename}.json", 'w+') as json_file:
                json.dump(gripper_dict, json_file, indent="\t")

        # imu
        elif "imu" in sensor_topic_dict and topic in sensor_topic_dict["imu"]:
            msg = deserialize_message(data, get_message(topic_types_dict[topic]))
            filename = ros_time_to_sec_str(msg.header.stamp)
            imu_dict = {
                "angular_velocity" : {
                    "x" : msg.angular_velocity.x,
                    "y" : msg.angular_velocity.y,
                    "z" : msg.angular_velocity.z,
                },
                "linear_acceleration" : {
                    "x" : msg.linear_acceleration.x,
                    "y" : msg.linear_acceleration.y,
                    "z" : msg.linear_acceleration.z,
                },
                "orientation" : {
                    "x" : msg.orientation.x,
                    "y" : msg.orientation.y,
                    "z" : msg.orientation.z,
                    "w" : msg.orientation.w,
                },
            }
            with open(f"{output_dir_head}{save_topic_dirs[topic]}/{filename}.json", 'w+') as json_file:
                json.dump(imu_dict, json_file, indent="\t")

        # TODO /robotBase/transform
        # can't process
        elif topic not in fail_topics:
            fail_topics.append(topic)
    print()
    if total_count == 0 and msg_num > 0:
        raise RuntimeError(
            "No messages were read from MCAP even though mcap info reports "
            f"{msg_num} messages: {input_mcap_file}"
        )
    end_time = time.time()
    print("spend time: %-.3fs" % (end_time - start_time))

    if len(fail_topics) != 0:
        print("\n[Warn] can't generate aloha data from topic:")
        print("++++++++++++++++++++++++++++++++++++++++++")
        for topic in fail_topics:
            print(f"  * {topic}")
        print("------------------------------------------")

def get_mcap_files(in_dir=""):
    exclude_dirs = [".Trash-1000"]
    file_list = []
    for root, dirs, files in os.walk(in_dir):
        dirs[:] = [d for d in dirs if d not in exclude_dirs]

        for file in files:
            if file.endswith('.mcap'):
                file_path = os.path.join(root, file)
                if not ("abnormal" in file_path):
                    file_list.append(file_path)
    return file_list

def main():
    mcap_dir = os.getcwd()
    data_dir = mcap_dir
    parser = argparse.ArgumentParser()
    parser.add_argument('--datasetDir', action='store', type=str, help='datasetDir.',
                        default=os.getcwd(), required=False)
    parser.add_argument('--targetDir', action='store', type=str, help='targetDir.',
                        default="", required=False)
    parser.add_argument('--episodeIndex', action='store', type=int, help='Episode index.',
                        default=-1, required=False)
    parser.add_argument('--alohaYaml', action='store', type=str, help='alohaYaml.',
                        default="../topic_configs/aloha_data_params.yaml", required=False)
    parser.add_argument(
        '--allowPartialRecover',
        action='store_true',
        help='Allow conversion of an mcap recover output even when it contains fewer messages than mcap info reported.',
    )
    parser.add_argument(
        '--recordTimeTopic',
        action='append',
        default=[],
        help='Use MCAP record time instead of message header time for this exact topic; repeatable.',
    )
    args = parser.parse_args()
    print("args:")
    print(f" --datasetDir: {args.datasetDir}")
    print(f" --episodeIndex: {args.episodeIndex}")
    print(f" --alohaYaml: {args.alohaYaml}")
    print(f" --recordTimeTopic: {args.recordTimeTopic}")

    if args.datasetDir != "" and args.datasetDir != ".":
        mcap_dir = args.datasetDir
        data_dir = args.datasetDir
    if args.episodeIndex != -1:
        mcap_dir = os.path.join(mcap_dir, f"episode{args.episodeIndex}")
    if args.targetDir != "":
        data_dir = args.targetDir

    # 获取指定目录下所有mcap
    files = get_mcap_files(mcap_dir)
    print(f"转换的mcaps: {files}")

    # # 指定以某个yaml文件配置来将mcap转aloha数据
    for file in files:
        process_file(
            file,
            data_dir,
            args.alohaYaml,
            args.allowPartialRecover,
            args.recordTimeTopic,
        )

if __name__ == "__main__":
    main()
