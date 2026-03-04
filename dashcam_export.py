"""
Helper Dashcam Export Script
- Given a folder/SD card full of dashcam videos, looks to backup only a single image per file, and GPS tracking
- Extracts snapshots from video files using ffmpeg
- Extracts GPS telemetry from binary data streams and converts to GeoJSON


Supported Cameras:
- TERUNSOUL D016 4K - Front/Rear. 
    - Looks for F and R folders, and combines into single folders.
    - This camera embeds GPS data into the mpegts container as a binary stream, which we extract and decode.
    - The GPS data is obfuscated using a shift based on the device ID and packet metadata
Usage:
    python dashcam_export.py --drive /path/to/dashcam/drive --dest /path/to/output --ffmpeg /path/to/ffmpeg

"""
import argparse
import os
import subprocess
import struct
import logging
import sys
import piexif
import json
from pathlib import Path
from datetime import datetime


class Color:
    RED = "\033[91m"
    YELLOW = "\033[93m"
    RESET = "\033[0m"


def setup_logger():
    logger = logging.getLogger("dashcam")
    logger.setLevel(logging.DEBUG)

    handler = logging.StreamHandler(sys.stdout)

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger


logger = setup_logger()


class GpsTelemetryParser:
    @staticmethod
    def parse_gps_frames(bin_path):
        """
        Parses binary telemetry frames, applies the Ramona obfuscation shift,
        and returns a list of dictionaries containing GPS and timestamp data.
        """
        FRAME_SIZE = 160
        points = []

        if not os.path.exists(bin_path):
            return points

        logger.info(f"Parsing telemetry: {bin_path}")

        def apply_shift_and_convert(raw_val, var5, var6, var7, is_lat):
            # Apply the extracted constants
            if is_lat:
                # Latitude constants: -3.4, -1.2
                shifted = raw_val + (var6 * -3.4) + (var7 * -1.2) - var5
            else:
                # Longitude constants: -1.3, -4.2
                shifted = raw_val + (var6 * -1.3) + (var7 * -4.2) - var5

            # Replicate NMEA conversion logic
            degrees = int(shifted / 100.0)
            # Use fixed-point scaling (100000) to maintain precision
            scaled = round(shifted * 100000.0)
            minutes_scaled = scaled % 10000000
            final_minutes = minutes_scaled / 6000000.0
            
            result = degrees + final_minutes
            return result if is_lat else -abs(result)

        with open(bin_path, "rb") as f:
            while True:
                block = f.read(FRAME_SIZE)
                if len(block) < FRAME_SIZE:
                    break

                try:
                    # 1. Parse Header (First 32 bytes)
                    # Format: 6 Unsigned Ints (24 bytes), 4-char string (4 bytes), 1 Unsigned Int (4 bytes)
                    header_fmt = "<6I4sI"
                    (hour, minute, second, year, month, day, marker, padding) = struct.unpack(
                        header_fmt, block[:struct.calcsize(header_fmt)]
                    )

                    # 2. Validation: Check for signature/marker
                    # Checked for "BTX" at offset 72, 
                    # but we also check the marker "ANW" from your struct definition
                    sig = block[72:75].decode('ascii', errors='ignore')
                    if not sig.startswith("BT"):
                        continue

                    # 3. Extract Metadata for the Shift
                    # var6 (Packet Type) at offset 4, var7 (Sequence) at offset 8
                    var6 = struct.unpack("<I", block[4:8])[0]
                    var7 = struct.unpack("<I", block[8:12])[0]

                    # 4. Calculate Device ID Checksum (var5) from offset 56
                    id_bytes = block[56:72]
                    var5 = 0
                    for b in id_bytes:
                        if b == 0: break
                        char = chr(b).upper()
                        if '0' <= char <= '9': var5 += ord(char) - ord('0')
                        elif 'A' <= char <= 'F': var5 += ord(char) - ord('A') + 10

                    # 5. Extract Raw Doubles
                    # Offset 32: Lat, Offset 40: Lon
                    raw_lat = struct.unpack("<d", block[32:40])[0]
                    raw_lon = struct.unpack("<d", block[40:48])[0]

                    # 6. Transform Coordinates
                    final_lat = apply_shift_and_convert(raw_lat, var5, var6, var7, True)
                    final_lon = apply_shift_and_convert(raw_lon, var5, var6, var7, False)

                    points.append({
                        "year": year,
                        "month": month,
                        "day": day,
                        "hour": hour,
                        "minute": minute,
                        "second": second,
                        "lat": final_lat,
                        "lon": final_lon
                    })

                except Exception as e:
                    logger.error(f"Error parsing frame: {e}")
                    continue

        return points
    
def export_to_geojson(points, output_path):
    """
    Converts the array of GPS dictionaries into a GeoJSON file.
    Includes a 'LineString' for the track and individual 'Points' for metadata.
    """
    if not points:
        print("No points to export.")
        return

    features = []
    
    # 1. Create a LineString feature to represent the actual path traveled
    coordinates = [[p['lon'], p['lat']] for p in points]
    
    line_feature = {
        "type": "Feature",
        "properties": {
            "name": "Track Logs",
            "type": "GPS Track"
        },
        "geometry": {
            "type": "LineString",
            "coordinates": coordinates
        }
    }
    features.append(line_feature)

    # 2. Add individual points as features (optional, but good for timestamp data)
    for p in points:
        point_feature = {
            "type": "Feature",
            "properties": {
                "timestamp": f"{p['year']}-{p['month']:02d}-{p['day']:02d} "
                             f"{p['hour']:02d}:{p['minute']:02d}:{p['second']:02d}",
                "lat": p['lat'],
                "lon": p['lon']
            },
            "geometry": {
                "type": "Point",
                "coordinates": [p['lon'], p['lat']]
            }
        }
        features.append(point_feature)

    geojson = {
        "type": "FeatureCollection",
        "features": features
    }

    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(geojson, f, indent=2)
        print(f"GeoJSON successfully saved to: {output_path}")
    except Exception as e:
        print(f"Failed to save GeoJSON: {e}")


def extract_snapshot(ffmpeg_path, video_path, img_path):
    args = [
        ffmpeg_path,
        "-y",
        "-ss", "00:00:05",
        "-accurate_seek",
        "-i", video_path,
        "-frames:v", "1",
        "-q:v", "1",
        img_path,
    ]

    try:
        subprocess.run(
            args,
            check=True,
            capture_output=True,
            text=True
        )

    except subprocess.CalledProcessError as e:
        logger.error(f"extract_snapshot extraction failed: {e}: {e.stdout} {e.stderr}")
        exit(1)


def write_exif_timestamp(img_path, dt):
    try:
        exif_dict = piexif.load(img_path)

        exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = dt.strftime(
            "%Y:%m:%d %H:%M:%S"
        ).encode()

        piexif.insert(piexif.dump(exif_dict), img_path)

    except Exception as e:
        logger.error(f"{Color.RED}EXIF write error for {img_path}: {e}{Color.RESET}")


def parse_filename_time(filename):
    try:
        stamp = filename.split("_")[0]
        return datetime.strptime(stamp, "%Y%m%d%H%M%S")
    except Exception as e:
        logger.error(f"{Color.RED}Failed to parse time from filename: {filename}: {e}{Color.RESET}")
        return None


def extract_data_stream(ffmpeg_path, input_file, output_file):
    """
    Extracts telemetry / data streams from video container using ffmpeg.

    Recommended usage for your dashcam:
        Stream #0:2 bin_data GPS telemetry
    """
    args = [
            ffmpeg_path,
            "-i",
            input_file,
            "-map",
            "0:d",   # All data streams (safest for automation)
            "-c",
            "copy",
            "-f",
            "data",
            output_file,
        ]

    try:
        
        subprocess.run(
            args,
            check=True,
            capture_output=True,
            text=True
        )

    except subprocess.CalledProcessError as e:
        logger.error(f"extract_data_stream extraction failed: {e}: {e.stdout} {e.stderr}")
        exit(1)
 

def process_drive(drive_path, dest_path, ffmpeg_path):

    for root, _, files in os.walk(drive_path):

        if not files:
            continue

        parts = Path(root)
        if not parts.parent:
            continue

        logger.debug(f"Scanning {root} with {len(files)} files")

        if not parts.parent.name:
            logger.debug(f"Skipping {root} (empty name)")
            continue

        supported_categories = ["video", "park", "event", "photo"]
        if not any(cat in parts.parent.name.lower() for cat in supported_categories):
            logger.debug(f"Skipping {root} (no category in path)")
            continue

        category = parts.parent.name.lower()
        logger.debug(f"Category: '{category}'")

        is_front = True if "f" == parts.name.lower() else False
        is_rear = True if "r" == parts.name.lower() else False
        if not is_front and not is_rear:
            continue

        out_category_dir = os.path.join(dest_path, category)
        logger.info(f"Processing category '{category}' in {root} {out_category_dir}")
        os.makedirs(out_category_dir, exist_ok=True)

        for file in files:

            if not file.endswith(".ts"):
                continue

            video_path = os.path.join(root, file)

            start_time = parse_filename_time(file)
            if not start_time:
                continue

            logger.info(f"Processing {video_path} is front={is_front} rear={is_rear} start_time={start_time}")

            base_name = file # os.path.splitext(file)[0].replace("_F", "")

            img_path = os.path.join(out_category_dir, base_name + ".png")

            # Only generate gps for front camera videos

            # Snapshot
            extract_snapshot(ffmpeg_path, video_path, img_path)
            #write_exif_timestamp(img_path, start_time)

            if is_front:
                data_stream = os.path.join(out_category_dir, base_name + ".data.bin")
                geojson_path = os.path.join(out_category_dir, base_name + ".geojson")

                extract_data_stream(ffmpeg_path, video_path, data_stream)

                points = GpsTelemetryParser.parse_gps_frames(data_stream)
                if points:
                     export_to_geojson(points, geojson_path)
                else:
                    logger.warning(f"{Color.YELLOW}No GPS points extracted for {video_path}{Color.RESET}")
                     

if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument("--drive", help="Path to the drive or folder containing dashcam videos", required=False)
    parser.add_argument("--dest", help="Path to the destination folder for exported files", required=False)
    parser.add_argument("--ffmpeg", help="Full Path to the ffmpeg executable", default="ffmpeg", required=False)
    parser.add_argument("--bin", help="Path to a binary telemetry file - parse and output a geojson file", required=False)

    args = parser.parse_args()

    if args.drive and args.dest and args.ffmpeg:
        process_drive(args.drive, args.dest, args.ffmpeg)
    elif args.bin:
        points = GpsTelemetryParser.parse_gps_frames(args.bin)
        export_to_geojson(points, args.bin + ".geojson")
    else:
        logger.error(f"{Color.RED}Invalid arguments.{Color.RESET}")
        parser.print_help()
        sys.exit(1)
