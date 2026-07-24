#!/usr/bin/env python3
"""
UDP MJPEG client with:
 - Heartbeat on port 8030
 - Fragmented MJPEG packet reconstruction (linear buffer)
 - Frame timeout
 - Single frame save via 's' key
 - Remote event frame save via port 50000
 - Option --save to save the full MJPEG stream to a timestamped file
"""

import socket
import threading
import time
import cv2
import numpy as np
import datetime
import argparse

# -------------------------
# Server and protocol configuration
# -------------------------
SERVER_IP = "192.168.10.123"
SERVER_PORT = 8030

# Heartbeat / STOP messages (24 bytes)
HEARTBEAT = bytes.fromhex("999901000000000000000000000000000000000000000000")
STOP_MSG  = bytes.fromhex("999902000000000000000000000000000000000000000000")

HEARTBEAT_INTERVAL = 0.5
SOCKET_TIMEOUT = 1.0
FRAME_TIMEOUT = 1.0

# Event listener config (port 50000)
EVENT_PORT = 50000
EVENT_INTERVAL = 0.1

# Event request packet (18 bytes)
EVENT_REQUEST_TAIL = bytes.fromhex("00 00 90 00 04 00 00 00 00 00")
EVENT_REQUEST_PREFIX = b"SETCMD"

# Event response expected (20 bytes)
EVENT_RESPONSE_PREFIX = b"RETCMD"
EVENT_RESPONSE_MIDDLE = bytes.fromhex("00 00 90 00 04 00")

# -------------------------
# Global state
# -------------------------
running = True

# Current frame reconstruction
current_frame = {
    "id": None,
    "size": None,
    "buffer": bytearray(),
    "expected_frag": 0,
    "start_time": None
}

# Statistics
frames_decoded = 0
frames_dropped = 0
fragments_received = 0

# Remote event signaling
event_signal = threading.Event()
_last_server_event_counter = None
_last_server_event_counter_lock = threading.Lock()

# Event request counter
_event_request_counter = 0
_event_request_counter_lock = threading.Lock()

# -------------------------
# Auxiliary functions
# -------------------------
def send_heartbeat(sock):
    """Thread to periodically send heartbeat to the server."""
    while running:
        try:
            sock.sendto(HEARTBEAT, (SERVER_IP, SERVER_PORT))
        except Exception as e:
            print("[heartbeat] Send error:", e)
        time.sleep(HEARTBEAT_INTERVAL)

def build_event_request():
    """Builds 18-byte event request packet with incremental counter."""
    global _event_request_counter
    with _event_request_counter_lock:
        cnt = _event_request_counter
        _event_request_counter = (_event_request_counter + 1) & 0xFFFF
    packet = bytearray()
    packet += EVENT_REQUEST_PREFIX
    packet += cnt.to_bytes(2, "little")
    packet += EVENT_REQUEST_TAIL
    return bytes(packet), cnt

def parse_event_packet(data: bytes, expected_request_counter: int) -> bool:
    """
    Parse 20-byte event response.
    Returns True if the server requested a new frame save.
    Uses the first received value as baseline.
    """
    global _last_server_event_counter

    if len(data) != 20:
        return False

    if data[0:6] != EVENT_RESPONSE_PREFIX:
        return False

    resp_request_counter = int.from_bytes(data[6:8], "little")
    if resp_request_counter != expected_request_counter:
        return False

    if data[8:14] != EVENT_RESPONSE_MIDDLE:
        return False

    server_event_counter = int.from_bytes(data[18:20], "little")

    with _last_server_event_counter_lock:
        if _last_server_event_counter is None:
            _last_server_event_counter = server_event_counter
            return False

        if server_event_counter == _last_server_event_counter:
            return False

        # New event detected
        _last_server_event_counter = server_event_counter
        return True

def event_listener():
    """Thread to send event requests and check server responses."""
    global running
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.03)
    try:
        while running:
            try:
                req_packet, req_counter = build_event_request()
                try:
                    sock.sendto(req_packet, (SERVER_IP, EVENT_PORT))
                except Exception:
                    time.sleep(EVENT_INTERVAL)
                    continue

                try:
                    data, _ = sock.recvfrom(256)
                    if data:
                        if parse_event_packet(data, req_counter):
                            event_signal.set()
                except socket.timeout:
                    pass
            except Exception:
                pass
            time.sleep(EVENT_INTERVAL)
    finally:
        sock.close()

# -------------------------
# MJPEG packet parsing and reconstruction
# -------------------------
def parse_mjpeg_packet(data: bytes):
    if len(data) < 24:
        return None
    header = data[:24]
    payload = data[24:]

    if header[0] != 0x66 or header[2] != 0x01:
        return None

    frame_flag = header[1]
    frame_id   = header[3]
    frame_size = int.from_bytes(header[4:8], "little")
    frag_index = int.from_bytes(header[12:14], "little")
    frag_size  = int.from_bytes(header[14:16], "little")

    if len(payload) != frag_size:
        return None

    return {
        "flag": frame_flag,
        "id": frame_id,
        "size": frame_size,
        "frag_index": frag_index,
        "data": payload
    }

def process_fragment(frame_state, fragment):
    global frames_dropped
    flag = fragment["flag"]
    frag_index = fragment["frag_index"]

    if flag == 1:
        frame_state["id"] = fragment["id"]
        frame_state["size"] = fragment["size"]
        frame_state["buffer"] = bytearray()
        frame_state["expected_frag"] = 0
        frame_state["start_time"] = time.time()

    if frame_state["start_time"] is not None:
        if time.time() - frame_state["start_time"] > FRAME_TIMEOUT:
            frame_state["buffer"] = bytearray()
            frame_state["expected_frag"] = 0
            frame_state["start_time"] = None
            frames_dropped += 1
            return None

    if frag_index != frame_state["expected_frag"]:
        frame_state["buffer"] = bytearray()
        frame_state["expected_frag"] = 0
        frame_state["start_time"] = None
        frames_dropped += 1
        return None

    frame_state["buffer"] += fragment["data"]
    frame_state["expected_frag"] += 1

    if flag == 2:
        if len(frame_state["buffer"]) == frame_state["size"]:
            jpeg = bytes(frame_state["buffer"])
            frame_state["buffer"] = bytearray()
            frame_state["expected_frag"] = 0
            frame_state["start_time"] = None
            return jpeg
        else:
            frame_state["buffer"] = bytearray()
            frame_state["expected_frag"] = 0
            frame_state["start_time"] = None
            frames_dropped += 1
            return None

    return None

# -------------------------
# Main loop
# -------------------------
def main(save_mjpeg=False):
    global running, frames_decoded, frames_dropped, fragments_received

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(SOCKET_TIMEOUT)

    threading.Thread(target=send_heartbeat, args=(sock,), daemon=True).start()
    threading.Thread(target=event_listener, daemon=True).start()

    if save_mjpeg:
        ts = datetime.datetime.now().strftime("%d%m%Y-%H%M%S")
        mjpeg_filename = f"stream_{ts}.mjpeg"
        mjpeg_file = open(mjpeg_filename, "wb")
        print(f"[main] Saving MJPEG stream to '{mjpeg_filename}'...")
    else:
        cv2.namedWindow("MJPEG Stream", cv2.WINDOW_NORMAL)

    try:
        while running:
            try:
                data, _ = sock.recvfrom(65535)
                if not data:
                    continue
                fragments_received += 1

                packet = parse_mjpeg_packet(data)
                if packet is None:
                    continue

                jpeg_bytes = process_fragment(current_frame, packet)
                if jpeg_bytes:
                    frames_decoded += 1

                    if save_mjpeg:
                        try:
                            mjpeg_file.write(jpeg_bytes)
                        except Exception as e:
                            print("[main] Error writing MJPEG:", e)
                    else:
                        img = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
                        if img is None:
                            frames_dropped += 1
                        else:
                            if event_signal.is_set():
                                tsf = datetime.datetime.now().strftime("%d%m%Y-%H%M%S")
                                fname = f"frame_{tsf}.jpg"
                                try:
                                    cv2.imwrite(fname, img)
                                    print(f"[event] Frame saved due to remote event: {fname}")
                                except Exception as e:
                                    print("[event] Error saving frame:", e)
                                event_signal.clear()

                            cv2.imshow("MJPEG Stream", img)
                            key = cv2.waitKey(1) & 0xFF
                            if key == ord('q'):
                                running = False
                                break
                            elif key == ord('s'):
                                tsf = datetime.datetime.now().strftime("%d%m%Y-%H%M%S")
                                fname = f"frame_{tsf}.jpg"
                                cv2.imwrite(fname, img)
                                print(f"[main] Frame saved: {fname}")

            except socket.timeout:
                if current_frame["start_time"] is not None:
                    if time.time() - current_frame["start_time"] > FRAME_TIMEOUT:
                        current_frame["buffer"] = bytearray()
                        current_frame["expected_frag"] = 0
                        current_frame["start_time"] = None
                        frames_dropped += 1
                continue

    except KeyboardInterrupt:
        running = False
    finally:
        if save_mjpeg:
            try:
                mjpeg_file.close()
                print(f"[main] MJPEG stream saved in '{mjpeg_filename}'")
            except Exception:
                pass

        try:
            sock.sendto(STOP_MSG, (SERVER_IP, SERVER_PORT))
        except Exception:
            pass

        if not save_mjpeg:
            cv2.destroyAllWindows()
        try:
            sock.close()
        except Exception:
            pass

        print("=== Statistics ===")
        print(f"Frames decoded: {frames_decoded}")
        print(f"Frames dropped: {frames_dropped}")
        print(f"Fragments received: {fragments_received}")

# -------------------------
# Command-line interface
# -------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="UDP MJPEG client with remote event frame save")
    parser.add_argument("--save", action="store_true",
                        help="Save full MJPEG stream to a file instead of displaying with OpenCV")
    args = parser.parse_args()
    main(save_mjpeg=args.save)
