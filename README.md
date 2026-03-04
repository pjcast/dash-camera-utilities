# Description
dashcam_export created to help read a camera SD card, and convert many short length video files, into single file images, and extract GPS positions. Helping to archive/save travel information, while reducing file size. Additionally, removing the need for proprietary video players that will become unsupported.

python dashcam_export.py --drive E:\ --dest .\dashcam_output --ffmpeg C:\tools\ffmpeg.exe

# Cameras

## TERUNSOUL D016 4K - Front/Rear.

Overview
--------
Telemetry stream is embedded inside dashcam .TS video containers. 

Properties:
- Fixed frame size telemetry packets
- Little-endian encoding
- IEEE754 double precision GPS coordinates
- One telemetry sample per second
- Front camera streams typically contain telemetry
- The timestamp header is easily extractable. The GPS/telementry however, is obfuscated.

SD Card Layout
--------------
Typical structure:

video/
    F/   -> Front camera (contains GPS telemetry)
    R/   -> Rear camera (usually video only)

event/
park/

Only F camera streams are normally expected to contain telemetry data.


----------------------------------------------------------------------
Frame Structure (92 bytes per packet)
----------------------------------------------------------------------

Offset | Size | Type    | Description
-------|------|---------|-----------------------------------------
0x00   | 4    | uint32  | Record type
0x04   | 4    | uint32  | Subtype
0x08   | 4    | uint32  | Seconds offset from clip start
0x0C   | 4    | uint32  | Year
0x10   | 4    | uint32  | Month
0x14   | 4    | uint32  | Day
0x18   | 4    | char[4] | Magic tag (ASCII "ANW\\0")
0x1C   | 8    | double  | Latitude
0x24   | 8    | double  | Longitude
0x2C   | 8    | double  | Speed (meters per second)
0x34   | 24   | char[]  | Device / firmware identifier
0x4C   | 12   | bytes   | Reserved / padding

