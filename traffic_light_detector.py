#!/usr/bin/python3
import rospy
import os
import cv2
import ultralytics
from sensor_msgs.msg import CompressedImage, Image
from std_msgs.msg import Bool
from cv_bridge import CvBridge
from ultralytics import YOLO
from PIL import ImageEnhance
from PIL import Image as pil_img
import torch
import numpy as np

class TrafficlightDetector:

    def __init__(self):
        ### ros settings ###
        rospy.init_node("traffic_light_detector")
        self._pub_img = rospy.Publisher('/yolo_result/image_raw/compressed', CompressedImage, queue_size=10)
        self._pub_box = rospy.Publisher('/yolo_box/image_raw/compressed', CompressedImage, queue_size=10)
        self._pub_flag = rospy.Publisher('/cross_traffic_light_flag', Bool, queue_size=1)
        self._sub_img = rospy.Subscriber('/CompressedImage', CompressedImage, self._image_callback)
        self._sub_exe_flag = rospy.Subscriber('/request_detect_traffic_light', Bool, self._exec_flag_callback)
        ### ros params ###
        self._conf_threshold_blue = rospy.get_param('~conf_threshold_blue', 0.3)
        self._conf_threshold_red = rospy.get_param('~conf_threshold_red', 0.3)
        self._min_conf = rospy.get_param('~min_conf', 0)
        self._count_threshold_blue = rospy.get_param('~count_threshold_blue', 20)
        self._count_threshold_red = rospy.get_param('~count_threshold_red', 50)
        self._aspect_ratio_threshold = rospy.get_param('~aspect_ratio_threshold', 2.0)
        self._start_brightness_judge_threshold = rospy.get_param('~do_brightness_judge_couont', 50)
        self._do_preprocess = rospy.get_param('~do_preprocess', True)
        self._hz = rospy.get_param('~hz', 10)
        ### basic setting ###
        self._bridge = CvBridge()
        self._exec_flag = False
        self._callback_flag = False
        self._is_backlight = False
        self._result_msg = CompressedImage()
        ### device setting ###
        torch.cuda.set_device(0)
        ### yolo weights ###
        weight_list = ["vidvip_yolov8n_2023-05-19.pt", "vidvipo_yolov8x_2023-05-19.pt", "yolov8n.pt"]
        self._model=YOLO(os.path.join( "weights", weight_list[1]))
        ### basic config ###
        self._count_blue=0
        self._count_red = 0
        self._count_to_start_brightness_judge = 0
        self._stored_boxes = []
        self._stored_red_box = None
        # self.count_box = 0

    def _exec_flag_callback(self, msg: Bool):
        self._exec_flag = msg.data

    def _image_callback(self, msg: CompressedImage):
        if(self._exec_flag and len(msg.data) != 0):
            self._input_cvimg = self._bridge.compressed_imgmsg_to_cv2(msg)
            self._callback_flag = True
        else:
            self._pub_img.publish(msg)

    def _visualize_box(self, img=None):
        # cv_img = img.to('cpu').detach().numpy().astype(int)

        if(img is None):
            img=self._input_cvimg

        result_msg = self._bridge.cv2_to_compressed_imgmsg(img)
        self._pub_box.publish(result_msg)

    def _visualize(self, img=None):
        # cv_img = img.to('cpu').detach().numpy().astype(int)

        if(img is None):
            img=self._input_cvimg

        result_msg = self._bridge.cv2_to_compressed_imgmsg(img)
        self._pub_img.publish(result_msg)



    def _backlight_correction(self):
        # グレースケール変換
        gray_image = cv2.cvtColor(self._input_cvimg, cv2.COLOR_BGR2GRAY)

        # ガンマ補正
        # gamma = 1.2
        gamma = 1.0
        # gamma = 2.0
        # gamma = 2.2
        corrected_gamma = np.power(gray_image / 255.0, 1.0 / gamma) * 255
        corrected_gamma = corrected_gamma.astype(np.uint8)

        # マルチスケールCLAHE
        # clahe1 = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        # clahe2 = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(16, 16))
        clahe1 = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(32, 32))
        clahe2 = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(64, 64))
        corrected1 = clahe1.apply(corrected_gamma)
        corrected2 = clahe2.apply(corrected_gamma)
        corrected_gray = cv2.addWeighted(corrected1, 0.5, corrected2, 0.5, 0)

        # 色情報を保持するための変換
        yuv_image = cv2.cvtColor(self._input_cvimg, cv2.COLOR_BGR2YUV)
        yuv_image[:, :, 0] = corrected_gray
        corrected_image = cv2.cvtColor(yuv_image, cv2.COLOR_YUV2BGR)

        return corrected_image

    def _detect_backlight(self):
        # 画像をグレースケールに変換
        gray_image = cv2.cvtColor(self._input_cvimg, cv2.COLOR_BGR2GRAY)

        # ハイライト領域の検出
        _, highlight_thresh = cv2.threshold(gray_image, 200, 255, cv2.THRESH_BINARY)

        # シャドウ領域の検出
        _, shadow_thresh = cv2.threshold(gray_image, 50, 255, cv2.THRESH_BINARY_INV)

        # 画像の中央部分を取得
        h, w = self._input_cvimg.shape[:2]
        center_x, center_y = w // 2, h // 2
        roi_size = min(h, w) // 4
        roi = gray_image[center_y-roi_size:center_y+roi_size, center_x-roi_size:center_x+roi_size]

        # ROI内のハイライト・シャドウ領域をカウント
        num_highlight_pixels_roi = np.count_nonzero(highlight_thresh[center_y-roi_size:center_y+roi_size, center_x-roi_size:center_x+roi_size])
        num_shadow_pixels_roi = np.count_nonzero(shadow_thresh[center_y-roi_size:center_y+roi_size, center_x-roi_size:center_x+roi_size])

        # ヒストグラムを計算
        hist = cv2.calcHist([gray_image], [0], None, [256], [0,256])
        highlight_hist_peak = np.sum(hist[200:])
        shadow_hist_peak = np.sum(hist[:50])

        # 画像のサイズ
        total_pixels = self._input_cvimg.shape[0] * self._input_cvimg.shape[1]

        # ハイライト・シャドウ領域の割合を計算
        highlight_ratio = num_highlight_pixels_roi / (roi_size * 2 * roi_size * 2)
        shadow_ratio = num_shadow_pixels_roi / (roi_size * 2 * roi_size * 2)

        # ヒストグラムからの割合を計算
        highlight_hist_ratio = highlight_hist_peak / total_pixels
        shadow_hist_ratio = shadow_hist_peak / total_pixels

        # コントラストを計算
        contrast = np.std(roi)

        # 逆光判定の条件を調整
        if ((highlight_ratio > 0.1 and shadow_ratio > 0.1) or contrast < 20) or (highlight_hist_ratio > 0.1 and shadow_hist_ratio > 0.1):
            return True
        else:
            return False

    def _draw_box(self, img, box, color):
        thickness = 4       # バウンディングボックスの線の太さ
        x1, y1, x2, y2 = box[0]
        cv2.rectangle(img, (x1,y1), (x2, y2), color, thickness)
        return img

    def _valid_box_judge(self):

        valid_boxes = []
        max_conf = -1

        print("ALL STORED BOXES:", len(self._stored_boxes))
        for box in self._stored_boxes:
            conf = box.conf.item()
            box = box.xyxy.to('cpu').detach().numpy().astype(int)
            x1, y1, x2, y2 = box[0]
            h=y2-y1
            w=x2-x1

            # 縦横比の確認
            if 1.55 <= h/w <= 1.85:
                valid_boxes.append((conf,box))

        # タプルの最初の要素(cof)を基準にしてリストを降順に並び替え
        sorted_valid_boxes = sorted(valid_boxes, key=lambda x: -x[0])
        # リストの最初(confが最大)の要素box成分を返す
        return sorted_valid_boxes[0][1]

    def _store_boxes(self, yolo_output, mode):
        if mode == 0:
            for box in yolo_output.boxes:
                    # box = box.xyxy.to('cpu').detach().numpy().astype(int)
                self._stored_boxes.append(box)
        else:
            if self._stored_red_box is None:
                self._stored_red_box = yolo_output.boxes[0]
            elif self._stored_red_box.conf.item() < yolo_output.boxes[0].conf.item():
                self._stored_red_box = yolo_output.boxes[0]
            print("TYPE:", type(self._stored_red_box))


    def _brightness_judge(self, yolo_output):

        if(self._stored_red_box is not None):
            self._stored_boxes.append(self._stored_red_box)

        signal = None
        valid_box = self._valid_box_judge()
        x1, y1, x2, y2 = valid_box[0]
        h=y2-y1
        w=x2-x1

        hsv = cv2.cvtColor(yolo_output.orig_img, cv2.COLOR_BGR2HSV)
        upper_hsv = hsv[y1:y1+h//2, x1:x2, :]
        lower_hsv = hsv[y1+h//2:y2, x1:x2, :]
        upper_brightness = np.mean(upper_hsv[:,:,2])
        lower_brightness = np.mean(lower_hsv[:,:,2])

        print("UPPER:", upper_brightness)
        print("LOWER:", lower_brightness)
        if(upper_brightness < lower_brightness):
            color = (0, 255, 0)  # バウンディングボックスの色 (BGR形式)
            signal = 'signal_blue'
        else:
            color = (0, 0, 255)
            signal = 'signal_red'

        # print("VALID BOXES:", len(valid_boxes))
        brightness_judge_output = self._draw_box(yolo_output.orig_img, valid_box, color)
        return brightness_judge_output, signal

    def _yolo(self, input_img):

        max_conf = -1
        max_conf_class = None
        output = None
        yolo_output = self._model(input_img, classes=[15, 16], conf=0) #self._model() returns list of class:ultralytics.engine.results.Results

        if(len(yolo_output[0]) != 0):
            max_conf = yolo_output[0].boxes[0].conf.item()
            max_conf_class = yolo_output[0].boxes[0].cls.item()
            output = yolo_output[0]

            ### DEBUG ###
            # box = yolo_output[0].boxes[0].xyxy.to('cpu').detach().numpy().astype(int)
            # x1, y1, x2, y2 = box[0]
            # h=y2-y1
            # w=x2-x1
            # print("アスペクト比：", h/w)
            ### DEBUG ###

        return output, max_conf, int(max_conf_class)

    def _preprocess(self):
        if self._detect_backlight() and self._do_preprocess:
            # rospy.logwarn('BLACK LIGHT IS DETECTED')
            self._is_backlight = True
            return self._backlight_correction()
            # return self._input_cvimg
        else:
            self._is_backlight = False
            return self._input_cvimg

    def _judge_signal(self) -> str:

        signal = None
        visualize_cvimg = None
        input_img = self._preprocess()
        yolo_output, max_conf, max_conf_class = self._yolo(input_img)

        if(max_conf_class is not None):
            if((max_conf_class == 16 and max_conf > self._conf_threshold_blue) or
               (max_conf_class == 15 and max_conf > self._conf_threshold_red)):
                signal = yolo_output.names.get(max_conf_class)
                # print("SIGNAL:", signal)
                visualize_cvimg = yolo_output[0].plot()
                self._count_to_start_brightness_judge = 0
                self._stored_boxes.clear()
                if max_conf_class == 15:
                    self._store_boxes(yolo_output, 1)
            elif(self._count_to_start_brightness_judge < self._start_brightness_judge_threshold):
                self._count_to_start_brightness_judge += 1
                self._store_boxes(yolo_output, 0)
                visualize_cvimg = yolo_output[0].plot()
                rospy.logwarn("UNDER THRESHOLD")
            else:
                brightness_judge_output, signal = self._brightness_judge(yolo_output)
                visualize_cvimg = brightness_judge_output
                rospy.logwarn("BRIGHTNESS JUDGE")
        else:
            visualize_cvimg = input_img
            rospy.logerr("NOT WORKING")
        self._visualize(visualize_cvimg)
        return signal


    def _run(self, _):
        cross_traffic_light_flag = False
        ### initialize when the task type is not traffic light
        if(not self._exec_flag):
            self._count_blue=0
            self._count_red=0
        ### publish flag if a blue is detected above a threshold value after a red is detected above a threshold value
        elif(self._callback_flag):
            signal = self._judge_signal()

            if(signal == "signal_red"):
                self._count_red += 1
            elif(signal == "signal_blue" and self._count_red > self._count_threshold_red):
                self._count_blue += 1

            if(self._count_blue > self._count_threshold_blue):
                cross_traffic_light_flag = True
                self._count_red = 0
                self._count_blue = 0
        self._pub_flag.publish(cross_traffic_light_flag)

    def __call__(self):
            duration = int(1.0 / self._hz * 1e9)
            rospy.Timer(rospy.Duration(nsecs=duration), self._run)
            rospy.spin()

if __name__=="__main__":
    TrafficlightDetector()()


