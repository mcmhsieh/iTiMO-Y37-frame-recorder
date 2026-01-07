This repository contains a python program that can be used to show the video output of a borescope on a PC (tested under Linux). It uses opencv and can save a frame by pressing 's' on keyboard or the snapshot button on the borescope.

This program will **not** work on all borescopes, it works on my specific model, and possibly on others that use the same network protocol. I wrote it manly to understand and test the protocol before writing an Android app (https://github.com/framenic/freed-borescope-view).

<img align='center' width="500" height="650" alt="immagine" src="https://github.com/user-attachments/assets/86e2d729-4ed0-440a-8ac6-416c4da75909" />                          



# Introduction
I bought a borescope from one the famous web marketplaces, which can be connected to a phone by USB. It doesn't have any indication of the manufacturer and it is sold as J103-OTG model (also J106-OTG and J108 OTG). I later discovered, while analyzing network packets, that the manufacturer could be Shenzen Molink Technology.

<img width="140" height="155" alt="immagine" src="https://github.com/user-attachments/assets/546d4848-8ee6-42bb-88a6-53644ed8369b" />


Initially i hoped it was a UVC video device, so I could use it connected to a smartphone via USB OTG and installing any app that can use a UVC camera. Instead, it can only be used with a specific app (called USB-VIEW), which gives me with serious doubts about its security and future availability. 

<img width="125" height="300" alt="immagine" src="https://github.com/user-attachments/assets/ec6d7270-f478-4d61-aa9b-f026a528b29d" />
<img width="125" height="300" alt="immagine" src="https://github.com/user-attachments/assets/2f380919-63e9-415c-a3f7-242562ebe4c8" />
<img width="125" height="300" alt="immagine" src="https://github.com/user-attachments/assets/7aee4d7d-6400-408a-9b79-265fbc32b6ef" />

Connecting it to a PC does not result in a camera device, so I started investigating. Linux reveals that the USB device is actually an ethernet controller (RTL8152) and that it get an IP address through DHCP as soon as it is connected (192.168.10.100).

<img width="758" height="168" alt="immagine" src="https://github.com/user-attachments/assets/26cf503a-ad8e-4d6c-9755-4c7960a20034" />

Wireshark shows a continuos ping activity coming from 192.168.10.123, which must be te borescope address.

<img width="1011" height="209" alt="immagine" src="https://github.com/user-attachments/assets/3bbe8276-9e8c-4f8e-b018-47d2da41f484" />


# Useful previous works
Armed with this information I discovered interesting previous works. They mainly target a wifi borescope, but the protocol described and the architecture have some similarities.

- https://n8henrie.com/2019/02/reverse-engineering-my-wifi-endoscope-part-1/
- https://mplough.github.io/2019/12/14/borescope.html
- https://github.com/mkarr/boroscope_stream_fixer
- https://github.com/mentalburden/Nidage-Borescope-Reversing

While these are very interesting projects, none of the network protocol described is directly applicable to my boroscope. Moreover I wanted a practical way to use the borescope directly on a PC and a smartphone application that i could trust.
My work starts; it is time to reverse the network protocol of the borescope.

# Network protocol
These are the results of the analysis of the network traffic between the smartphoen applicatio and the borescope. In the following description, the smartphone application is the **client** while the borescope is the **server**.

The server has two open UDP ports: 8030 and 50000. They can be easily found using *nmap*, but also tracing the traffic between the USB VIEW app and the boroscope.
None of them provide data (i.e. the video stream) directly when connected, but specific commands must be sent by the client in order to get the video stream.

## 1. UDP Port 8030 – Heartbeat and MJPEG Streaming

- the client send periodically a specific packet to get the video stream from the server.
- The server sends back, as a result, fragmented MJPEG frames on the same socket (i.e. server source port 8030).

### Packet Types:
1. Heartbeat packet (24 bytes)
    - Sent periodically every 0.5 seconds from the client to the server to keep the video stream active.
    - Fixed value: 99 99 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00

2. Stop packet (24 bytes)
    - Sent on client exit to stop the server stream:
    - Fixed value: 99 99 02 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00

3. MJPEG fragment packets (variable length, ≥24 bytes) sent from the server to the client
   - Header (24 bytes):
      * Byte 0: 0x66 (packet identifier)
      * Byte 1: Frame flag (1 = start, 2 = end, 3 = middle fragment)
      * Byte 2: 0x01 (fixed value)
      * Byte 3: Frame ID
      * Bytes 4-7: Total jpeg frame size (little-endian)
      * Bytes 12-13: Fragment index (little-endian)
      * Bytes 14-15: Fragment size (little-endian)
      * Remaining header bytes: reserved/padding ??

- Payload:
The actual JPEG fragment data, frag_size bytes.
In order to reassemble a compelte frame:
 - Fragments must be sequentially appended to a buffer based on frag_index.
 - Frame completion is detected when a fragment with frame flag = 2 arrives, and the buffer length matches frame_size.

## UDP Port 50000 – Remote Event Detection
Port 50000 is used by the client to send commands to the server. Commands consist in "SETCMD xxxx" requests from the client and "RETCMD xxxx" answers from the server. The protocol is very similar to the one described in one of the previous works. I don't know if the commands are exacly the same or not, but the only practical application in my case is for getting the event "snapshot button pressed" on the borescope. So I just analyzed and implemented this command.

### Packets:

1. Event request packet (18 bytes)

    Sent periodically by the client (about every 100ms) to check for server snapshot button events.

- Format:
  - Bytes 0-5: ASCII "SETCMD"
  - Bytes 6-7: Request counter (little-endian, incremented per request)
  - Bytes 8-17: Fixed tail = 00 00 90 00 04 00 00 00 00 00

2. Event response packet (20 bytes)
- Format:
  - Bytes 0-5: ASCII "RETCMD"
  - Bytes 6-7: Request counter (little-endian, matching the request)
  - Bytes 8-13: Fixed pattern = 00 00 90 00 04 00
  - Bytes 14-17: reserved ??
  - Bytes 18-19: Server event counter (little-endian), incremented every time the snapshot button is pressed
 
 ## Event detection logic:
   - The client compares the server event counter with the last seen value.
   - If it has incremented, a new frame capture event is triggered
   - on detection of a new event, the client sets a signal to save the next MJPEG frame received on port 8030. 
