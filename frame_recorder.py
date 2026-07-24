import sys
import time
import datetime
import threading
import pathlib
import queue
import tkinter as tk

import numpy as np
import cv2
import PIL
import PIL.ImageTk
import exif

import camera_client


class App(tk.Tk):
    def __init__(self, client, frame_queue, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.client = client
        self.frame_queue = frame_queue

        self.title('iTiMO Y37 frame recorder')
        self.geometry('+50+50')

        self.canvas = tk.Canvas(self, width=480, height=640)
        self.canvas.grid(row=0, column=0)
        self.canvas_image = self.canvas.create_image(0, 0, anchor='nw', image=None)

        buttons_frame = tk.Frame(self)
        buttons_frame.grid(row=1)

        self.record_start_stop_button = tk.Button(buttons_frame, text='Start recording (or hit <space bar>)', command=self.on_record_start_stop_button,
                                                  height=5, width=100 , bg='gold')
        self.record_start_stop_button.grid(row=0, column=0)

        self.mirror_image = tk.IntVar(value=1)
        mirror_checkbutton = tk.Checkbutton(buttons_frame, text='Mirror image', variable=self.mirror_image)
        mirror_checkbutton.grid(row=0, column=1)

        self.bind('<space>', lambda event: self.on_record_start_stop_button())
        self.focus_force()

        self.record = False

    def on_record_start_stop_button(self):
        self.record = not self.record
        self.record_start_stop_button.config(text='Stop recording (or hit <space bar>)' if self.record else 'Start recording (or hit <space bar>)')
        if self.record:
            self.record_start_stats = {'frames_decoded': self.client.frames_decoded,
                                       'frames_dropped': self.client.frames_dropped,
                                       'fragments_received': self.client.fragments_received}
        else:
            print('=== Statistics ===')
            print(f'Frames decoded: {self.client.frames_decoded - self.record_start_stats["frames_decoded"]}')
            print(f'Frames dropped: {self.client.frames_dropped - self.record_start_stats["frames_dropped"]}')
            print(f'Fragments received: {self.client.fragments_received - self.record_start_stats["fragments_received"]}')

    def update(self):
        new_images = []
        while True:
            if self.frame_queue.empty():
                break
            frame_index, frame_data, frame_time, frame_event = self.frame_queue.get_nowait()

            image = cv2.imdecode(frame_data, cv2.IMREAD_ANYCOLOR | cv2.IMREAD_ANYDEPTH)
            image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            image = cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)

            new_images.append((frame_index, frame_time, image))

            if self.record:
                exif_image = exif.Image(frame_data.tobytes())

                exif_image.orientation = exif.Orientation.LEFT_BOTTOM

                exif_image.make = self.client.device_info['manufacturer']
                exif_image.model = self.client.device_info['model']

                exif_image.datetime = exif_image.datetime_original = frame_time.strftime(exif.DATETIME_STR_FORMAT)
                exif_image.subsec_time = exif_image.subsec_time_original = frame_time.strftime('%f')
                exif_image.offset_time = exif_image.offset_time_original = '+00:00'

                filename = f'{frame_time.strftime("%Y%m%d-%H%M%S%f")}.{frame_index:03d}.jpg'
                output_path = pathlib.Path('./recorded_frames') / filename
                output_path.parent.mkdir(parents=True, exist_ok=True)

                with open(output_path, 'wb') as image_file:
                    image_file.write(exif_image.get_file())

        if len(new_images) > 0:
            frame_index, frame_time, image = new_images[-1]

            if self.mirror_image.get():
                image = np.fliplr(image)

            self.image = PIL.ImageTk.PhotoImage(PIL.Image.fromarray(image))
            self.canvas.itemconfig(self.canvas_image, image=self.image)

    def mainloop(self, *args, **kwargs):
        def update():
            window.update()
            window.after(5, update)
        window.after(1, update)
        super().mainloop(*args, **kwargs)


if __name__ == '__main__':

    stop_event = threading.Event()
    frame_queue = queue.Queue()

    client = camera_client.CameraClient(stop_event, frame_queue)
    client.connect()

    print(f'Device: {client.device_info["manufacturer"]} {client.device_info["model"]} {client.device_info["hardware"]}')
    print(f'Battery: {client.battery_level}%')
    print(f'LED PWM: {client.led_pwm}%')

    print('Configuring LED PWM')
    client.set_led_pwm(65)
    print(f'LED PWM: {client.led_pwm}%')

    video_loop_thread = threading.Thread(target=client.video_loop, daemon=True)
    video_loop_thread.start()

    window = App(client, frame_queue)
    try:
        window.mainloop()
    except Exception as ex:
        print(ex)
        print('window.destroy()')
        window.destroy()
    finally:
        stop_event.set()
        print('video_loop_thread.join()')
        video_loop_thread.join(2)
