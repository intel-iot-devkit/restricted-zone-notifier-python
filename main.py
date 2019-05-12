"""Restricted Zone Notifier."""

"""
 Copyright (c) 2018 Intel Corporation.

 Permission is hereby granted, free of charge, to any person obtaining
 a copy of this software and associated documentation files (the
 "Software"), to deal in the Software without restriction, including
 without limitation the rights to use, copy, modify, merge, publish,
 distribute, sublicense, and/or sell copies of the Software, and to
 permit person to whom the Software is furnished to do so, subject to
 the following conditions:

 The above copyright notice and this permission notice shall be
 included in all copies or substantial portions of the Software.

 THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
 EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
 MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
 NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
 LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
 OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
 WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

"""

import os
import sys
import json
import time
import socket
import cv2

import logging as log
import paho.mqtt.client as mqtt

from threading import Thread
from collections import namedtuple
from argparse import ArgumentParser
from inference import Network

# Assemblyinfo contains information about assembly area
MyStruct = namedtuple("assemblyinfo", "safe")
INFO = MyStruct(True)

# MQTT server environment variables
HOSTNAME = socket.gethostname()
IPADDRESS = socket.gethostbyname(HOSTNAME)
TOPIC = "Restricted_zone_python"
MQTT_HOST = IPADDRESS
MQTT_PORT = 1883
MQTT_KEEPALIVE_INTERVAL = 60

# Flag to control background thread
KEEP_RUNNING = True

DELAY = 5


def build_argparser():
    """
    Parse command line arguments.

    :return: Command line arguments
    """
    parser = ArgumentParser()
    parser.add_argument("-m", "--model", required=True, type=str,
                        help="Path to an .xml file with a trained model.")
    parser.add_argument("-i", "--input", required=True, type=str,
                        help="Path to video file or image. "
                             "'cam' for capturing video stream from camera", )
    parser.add_argument("-l", "--cpu_extension", type=str, default=None,
                        help="MKLDNN (CPU)-targeted custom layers. Absolute "
                             "path to a shared library with the kernels impl.")
    parser.add_argument("-d", "--device", default="CPU", type=str,
                        help="Specify the target device to infer on; "
                             "CPU, GPU, FPGA, HDDL or MYRIAD is acceptable. Application "
                             "will look for a suitable plugin for device specified"
                             "(CPU by default)")
    parser.add_argument("-th", "--prob_threshold", default=0.5, type=float,
                        help="Probability threshold for detections filtering")
    parser.add_argument('-x', '--pointx', default=0, type=int,
                        help="X coordinate of the top left point of assembly"
                             " area on camera feed.")
    parser.add_argument('-y', '--pointy', default=0, type=int,
                        help="Y coordinate of the top left point of assembly"
                             " area on camera feed.")
    parser.add_argument('-w', '--width', default=0, type=int,
                        help="Width of the assembly area in pixels.")
    parser.add_argument('-ht', '--height', default=0, type=int,
                        help="Height of the assembly area in pixels.")
    parser.add_argument('-r', '--rate', default=1, type=int,
                        help="Number of seconds between data updates "
                             "to MQTT server")
    return parser


def ssd_out(res, args, initial_wh, selected_region):
    """
    Parse SSD output.

    :param res: Detection results
    :param args: Parsed arguments
    :param initial_wh: Initial width and height of the frame
    :param selected_region: Selected region coordinates
    :return: None
    """
    global INFO
    person = []
    INFO = INFO._replace(safe=True)

    for obj in res[0][0]:
        # Draw objects only when probability is more than specified threshold
        if obj[2] > args.prob_threshold:
            xmin = int(obj[3] * initial_wh[0])
            ymin = int(obj[4] * initial_wh[1])
            xmax = int(obj[5] * initial_wh[0])
            ymax = int(obj[6] * initial_wh[1])
            person.append([xmin, ymin, xmax, ymax])

    for p in person:
        # area_of_person gives area of the detected person
        area_of_person = (p[2] - p[0]) * (p[3] - p[1])
        x_max = max(p[0], selected_region[0])
        x_min = min(p[2], selected_region[0] + selected_region[2])
        y_min = min(p[3], selected_region[1] + selected_region[3])
        y_max = max(p[1], selected_region[1])
        point_x = x_min - x_max
        point_y = y_min - y_max
        # area_of_intersection gives area of intersection of the
        # detected person and the selected area
        area_of_intersection = point_x * point_y
        if point_x < 0 or point_y < 0:
            continue
        else:
            if area_of_person > area_of_intersection:
                # assembly line area flags
                INFO = INFO._replace(safe=True)

            else:
                # assembly line area flags
                INFO = INFO._replace(safe=False)


def message_runner():
    """
    Publish worker status to MQTT topic.
    Pauses for rate second(s) between updates

    :return: None
    """
    while KEEP_RUNNING:
        time.sleep(1)
        CLIENT.publish(TOPIC, payload=json.dumps({"Worker safe": INFO.safe,
                                                  "Alert": not INFO.safe}))


def main():
    """
    Load the network and parse the output.

    :return: None
    """
    global DELAY
    global CLIENT
    global SIG_CAUGHT
    global KEEP_RUNNING
    CLIENT = mqtt.Client()
    CLIENT.connect(MQTT_HOST, MQTT_PORT, MQTT_KEEPALIVE_INTERVAL)
    CLIENT.subscribe(TOPIC)
    log.basicConfig(format="[ %(levelname)s ] %(message)s",
                    level=log.INFO, stream=sys.stdout)
    args = build_argparser().parse_args()
    logger = log.getLogger()
    render_time = 0
    roi_x = args.pointx
    roi_y = args.pointy
    roi_w = args.width
    roi_h = args.height

    if args.input == 'cam':
        input_stream = 0
    else:
        input_stream = args.input
        assert os.path.isfile(args.input), "Specified input file doesn't exist"

    cap = cv2.VideoCapture(input_stream)

    if not cap.isOpened():
        logger.error("ERROR! Unable to open video source")
        sys.exit(1)

    if input_stream:
        # Adjust DELAY to match the number of FPS of the video file
        DELAY = 1000 / cap.get(cv2.CAP_PROP_FPS)
    # Initialise the class
    infer_network = Network()
    # Load the network to IE plugin to get shape of input layer
    n, c, h, w = infer_network.load_model(args.model, args.device, 1, 1, 0, args.cpu_extension)[1]

    message_thread = Thread(target=message_runner, args=())
    message_thread.setDaemon(True)
    message_thread.start()

    ret, frame = cap.read()
    while ret:

        ret, next_frame = cap.read()
        if not ret:
            KEEP_RUNNING = False
            break

        initial_wh = [cap.get(3), cap.get(4)]

        if next_frame is None:
            KEEP_RUNNING = False
            log.error("ERROR! blank FRAME grabbed")
            break

        # If either default values or negative numbers are given,
        # then we will default to start of the FRAME
        if roi_x <= 0 or roi_y <= 0:
            roi_x = 0
            roi_y = 0
        if roi_w <= 0:
            roi_w = next_frame.shape[1]
        if roi_h <= 0:
            roi_h = next_frame.shape[0]
        key_pressed = cv2.waitKey(int(DELAY))

        # 'c' key pressed
        if key_pressed == 99:
            # Give operator chance to change the area
            # Select rectangle from left upper corner, dont display crosshair
            ROI = cv2.selectROI("Assembly Selection", frame, True, False)
            print("Assembly Area Selection: -x = {}, -y = {}, -w = {},"
                  " -h = {}".format(ROI[0], ROI[1], ROI[2], ROI[3]))
            roi_x = ROI[0]
            roi_y = ROI[1]
            roi_w = ROI[2]
            roi_h = ROI[3]
            cv2.destroyAllWindows()

        cv2.rectangle(frame, (roi_x, roi_y),
                      (roi_x + roi_w, roi_y + roi_h), (0, 0, 255), 2)
        selected_region = [roi_x, roi_y, roi_w, roi_h]

        in_frame_fd = cv2.resize(next_frame, (w, h))
        # Change data layout from HWC to CHW
        in_frame_fd = in_frame_fd.transpose((2, 0, 1))
        in_frame_fd = in_frame_fd.reshape((n, c, h, w))

        # Start asynchronous inference for specified request.
        inf_start = time.time()
        infer_network.exec_net(0, in_frame_fd)
        # Wait for the result
        infer_network.wait(0)
        det_time = time.time() - inf_start
        # Results of the output layer of the network
        res = infer_network.get_output(0)
        # Parse SSD output
        ssd_out(res, args, initial_wh, selected_region)

        # Draw performance stats
        inf_time_message = "Inference time: {:.3f} ms".format(det_time * 1000)
        render_time_message = "OpenCV rendering time: {:.3f} ms". \
            format(render_time * 1000)

        if not INFO.safe:
            warning = "HUMAN IN ASSEMBLY AREA: PAUSE THE MACHINE!"
            cv2.putText(frame, warning, (15, 80), cv2.FONT_HERSHEY_COMPLEX, 0.8, (0, 0, 255), 2)

        cv2.putText(frame, inf_time_message, (15, 15), cv2.FONT_HERSHEY_COMPLEX, 0.5, (255, 255, 255), 1)
        cv2.putText(frame, render_time_message, (15, 35), cv2.FONT_HERSHEY_COMPLEX, 0.5, (255, 255, 255), 1)
        cv2.putText(frame, "Worker Safe: {}".format(INFO.safe), (15, 55), cv2.FONT_HERSHEY_COMPLEX, 0.5, (255, 255, 255), 1)

        render_start = time.time()
        cv2.imshow("Restricted Zone Notifier", frame)
        render_end = time.time()
        render_time = render_end - render_start

        frame = next_frame

        if key_pressed == 27:
            print("Attempting to stop background threads")
            KEEP_RUNNING = False
            break
    infer_network.clean()
    message_thread.join()
    cap.release()
    cv2.destroyAllWindows()
    CLIENT.disconnect()


if __name__ == '__main__':
    main()




