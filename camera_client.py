"""
UDP MJPEG client derived from
https://github.com/framenic/freed-borescope/blob/main/python/scopeview.py
"""

import time
import datetime
import json
import socket
import threading

import numpy as np
import cv2


class CameraClient():
    # -------------------------
    # Server and protocol configuration
    # -------------------------
    SERVER_IP = '192.168.10.123'
    SERVER_PORT = 8031

    # Heartbeat / STOP messages (24 bytes)
    HEARTBEAT = bytes.fromhex('999901000000000000000000000000000000000000000000')
    STOP_MSG  = bytes.fromhex('999902000000000000000000000000000000000000000000')

    HEARTBEAT_INTERVAL = 0.5
    SOCKET_TIMEOUT = 1.0
    FRAME_TIMEOUT = 1.0

    # Event listener config (port 50000)
    EVENT_PORT = 50000
    EVENT_INTERVAL = 0.1

    # Event request packet (18 bytes)
    EVENT_REQUEST_TAIL = bytes.fromhex('90 00 04 00 00 00 00 00')
    EVENT_REQUEST_PREFIX = b'SETCMD'

    # Event response expected (20 bytes)
    EVENT_RESPONSE_PREFIX = b'RETCMD'
    EVENT_RESPONSE_MIDDLE = bytes.fromhex('90 00 04 00')

    def __init__(self, stop_event, frame_queue):
        self.stop_event = stop_event
        self.frame_queue = frame_queue

        self.sock = None

        # Current frame reconstruction
        self.current_frame = {'id': None,
                              'size': None,
                              'buffer': bytearray(),
                              'expected_frag': 0,
                              'start_time': None}

        # Statistics
        self.frames_decoded = 0
        self.frames_dropped = 0
        self.fragments_received = 0

        # Remote event signaling
        self.event_signal = threading.Event()
        self._last_server_event_counter = None
        self._last_server_event_counter_lock = threading.Lock()

        # Event request counter
        self._event_request_counter = 0
        self._event_request_counter_lock = threading.Lock()

    # -------------------------
    # Auxiliary functions
    # -------------------------
    def send_heartbeat(self):
        """Thread to periodically send heartbeat to the server."""
        while not self.stop_event.is_set():
            try:
                self.sock.sendto(self.HEARTBEAT, (self.SERVER_IP, self.SERVER_PORT))
            except Exception as e:
                print('[heartbeat] Send error:', e)
            time.sleep(self.HEARTBEAT_INTERVAL)

    def build_event_request(self):
        """Builds 18-byte event request packet with incremental counter."""
        with self._event_request_counter_lock:
            cnt = self._event_request_counter
            self._event_request_counter = (self._event_request_counter + 1) & 0xFFFFFFFF
        packet = bytearray()
        packet += self.EVENT_REQUEST_PREFIX
        packet += cnt.to_bytes(4, 'little')
        packet += self.EVENT_REQUEST_TAIL
        return bytes(packet), cnt

    def parse_event_packet(self, data: bytes, expected_request_counter: int) -> bool:
        """
        Parse 20-byte event response.
        Returns True if the server requested a new frame save.
        Uses the first received value as baseline.
        """
        if len(data) != 20:
            return False

        if data[0:6] != self.EVENT_RESPONSE_PREFIX:
            return False

        resp_request_counter = int.from_bytes(data[6:10], 'little')
        if resp_request_counter != expected_request_counter:
            return False

        if data[10:14] != self.EVENT_RESPONSE_MIDDLE:
            return False

        server_event_counter = int.from_bytes(data[18:20], 'little')

        with self._last_server_event_counter_lock:
            if self._last_server_event_counter is None:
                self._last_server_event_counter = server_event_counter
                return False

            if server_event_counter == self._last_server_event_counter:
                return False

            # New event detected
            self._last_server_event_counter = server_event_counter
            return True

    def event_listener(self):
        """Thread to send event requests and check server responses."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.03)
        try:
            while not self.stop_event.is_set():
                try:
                    req_packet, req_counter = self.build_event_request()
                    try:
                        sock.sendto(req_packet, (self.SERVER_IP, self.EVENT_PORT))
                    except Exception:
                        time.sleep(self.EVENT_INTERVAL)
                        continue

                    try:
                        data, _ = sock.recvfrom(256)
                        if data:
                            if self.parse_event_packet(data, req_counter):
                                self.event_signal.set()
                    except socket.timeout:
                        pass
                except Exception:
                    pass
                time.sleep(self.EVENT_INTERVAL)
        finally:
            sock.close()

    # -------------------------
    # MJPEG packet parsing and reconstruction
    # -------------------------
    def parse_mjpeg_packet(self, data: bytes):
        if len(data) < 24:
            return None
        header = data[:24]
        payload = data[24:]

        if header[0] != 0x66 or header[2] != 0x01:
            return None

        frame_flag = header[1]
        frame_id   = header[3]
        frame_size = int.from_bytes(header[4:8], 'little')
        frag_index = int.from_bytes(header[12:14], 'little')
        frag_size  = int.from_bytes(header[14:16], 'little')

        if len(payload) != frag_size:
            return None

        return {
            'flag': frame_flag,
            'id': frame_id,
            'size': frame_size,
            'frag_index': frag_index,
            'data': payload
        }

    def process_fragment(self, frame_state, fragment):
        flag = fragment['flag']
        frag_index = fragment['frag_index']

        if flag == 1:
            frame_state['id'] = fragment['id']
            frame_state['size'] = fragment['size']
            frame_state['buffer'] = bytearray()
            frame_state['expected_frag'] = 0
            frame_state['start_time'] = time.time()

        if frame_state['start_time'] is not None:
            if time.time() - frame_state['start_time'] > self.FRAME_TIMEOUT:
                frame_state['buffer'] = bytearray()
                frame_state['expected_frag'] = 0
                frame_state['start_time'] = None
                self.frames_dropped += 1
                return None

        if frag_index != frame_state['expected_frag']:
            frame_state['buffer'] = bytearray()
            frame_state['expected_frag'] = 0
            frame_state['start_time'] = None
            self.frames_dropped += 1
            return None

        frame_state['buffer'] += fragment['data']
        frame_state['expected_frag'] += 1

        if flag == 2:
            if len(frame_state['buffer']) == frame_state['size']:
                jpeg = bytes(frame_state['buffer'])
                frame_state['buffer'] = bytearray()
                frame_state['expected_frag'] = 0
                frame_state['start_time'] = None
                return jpeg
            else:
                frame_state['buffer'] = bytearray()
                frame_state['expected_frag'] = 0
                frame_state['start_time'] = None
                self.frames_dropped += 1
                return None

        return None

    def send_command(self, command_type, request_tail):
        with self._event_request_counter_lock:
            cnt = self._event_request_counter
            self._event_request_counter = (self._event_request_counter + 1) & 0xFFFFFFFF

        magic_bytes = bytes.fromhex('99 99')
        packet = bytearray()
        packet += magic_bytes
        packet += command_type
        packet += cnt.to_bytes(4, 'little')
        packet += request_tail

        req_packet = bytes(packet)
        self.sock.sendto(req_packet, (self.SERVER_IP, self.EVENT_PORT))
        data, _ = self.sock.recvfrom(1024)

        header = data[:24]
        payload = data[24:]

        # Message structure outlined in
        # https://github.com/SeanPesce/Spade-Web-Viewer/blob/master/spade_msg.py
        magic = int.from_bytes(header[:2], 'little')
        command = int.from_bytes(header[2:4], 'little')
        command_counter = int.from_bytes(header[4:8], 'little')
        arg1 = int.from_bytes(header[8:12], 'little')
        length = int.from_bytes(header[12:16], 'little')
        unk1 = int.from_bytes(header[16:], 'little')

        assert magic == int.from_bytes(magic_bytes, 'little')
        assert command == int.from_bytes(command_type, 'little')
        assert command_counter == cnt
        assert length == len(payload)

        return arg1, unk1, payload

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(self.SOCKET_TIMEOUT)

        # Sequence of commands based on traffic observed in PCAPdroid
        # Information on some command types are outlined in
        # https://github.com/SeanPesce/Spade-Web-Viewer/blob/master/spade_msg.py
        arg1, unk1, payload = self.send_command(bytes.fromhex('02 10'),
                                                bytes([0] * 16))
        assert len(payload) == 0
        print('GetRemoteVersion', arg1, unk1)
        arg1, unk1, payload = self.send_command(bytes.fromhex('17 10'),
                                                bytes([0] * 16))
        self.battery_level = unk1 & 0xFFFF
        print('GetBattery', self.battery_level, arg1, unk1, payload)
        arg1, unk1, payload = self.send_command(bytes.fromhex('60 10'),
                                                bytes([0] * 8) + bytes.fromhex('EC 40 72 81 78 00 00 00'))
        self.device_info = json.loads(payload)
        print('Device Information', self.device_info, arg1, unk1)

        arg1, unk1, payload = self.send_command(bytes.fromhex('15 10'),
                                                bytes([0] * 16))
        assert len(payload) == 0
        self.led_pwm = arg1
        print('GetPWM', arg1, unk1)

    def set_led_pwm(self, pwm):
        # Information on SetPWM command is outlined in
        # https://github.com/SeanPesce/Spade-Web-Viewer/blob/master/spade_msg.py
        arg1, unk1, payload = self.send_command(bytes.fromhex('16 10'),
                                                pwm.to_bytes(4, 'little') + bytes([0] * 12))
        assert len(payload) == 0
        self.led_pwm = arg1
        print('SetPWM', arg1, unk1)

    def video_loop(self):
        #threading.Thread(target=self.send_heartbeat, daemon=True).start()
        #threading.Thread(target=self.event_listener, daemon=True).start()

        # TODO: consider retrieving data from the device's gyroscope
        try:
            while not self.stop_event.is_set():
                self.sock.sendto(self.HEARTBEAT, (self.SERVER_IP, self.SERVER_PORT))
                try:
                    data, _ = self.sock.recvfrom(65535)
                    if not data:
                        continue
                    self.fragments_received += 1

                    packet = self.parse_mjpeg_packet(data)
                    if packet is None:
                        continue

                    jpeg_bytes = self.process_fragment(self.current_frame, packet)
                    if jpeg_bytes:
                        self.frames_decoded += 1

                        frame_data = np.frombuffer(jpeg_bytes, dtype=np.uint8)
                        img = cv2.imdecode(frame_data, cv2.IMREAD_COLOR)
                        if img is None:
                            self.frames_dropped += 1
                        elif not self.frame_queue.full():
                            frame_time = datetime.datetime.now(datetime.timezone.utc)
                            frame_index = (self.frames_decoded + self.frames_dropped) % 256
                            frame_event = False
                            if self.event_signal.is_set():
                                frame_event = True
                                self.event_signal.clear()
                            self.frame_queue.put_nowait((frame_index, frame_data, frame_time, frame_event))

                except socket.timeout:
                    if self.current_frame['start_time'] is not None:
                        if time.time() - self.current_frame['start_time'] > self.FRAME_TIMEOUT:
                            self.current_frame['buffer'] = bytearray()
                            self.current_frame['expected_frag'] = 0
                            self.current_frame['start_time'] = None
                            self.frames_dropped += 1
                    continue

        finally:
            try:
                self.sock.sendto(self.STOP_MSG, (self.SERVER_IP, self.SERVER_PORT))
            except Exception:
                pass

            try:
                self.sock.close()
            except Exception:
                pass
            finally:
                self.sock = None
