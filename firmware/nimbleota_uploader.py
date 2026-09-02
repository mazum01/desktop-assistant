# Copyright 2024 Ryan Powell and NimBLEOta contributors
# Sponsored by Theengs https://www.theengs.io, https://github.com/theengs
# MIT License

import asyncio
import argparse
import os
import sys
import time
from bleak import BleakScanner, uuids, BleakClient

VERBOSE = os.environ.get("VERA_VERBOSE") == "1"


def vlog(msg):
    """Print *msg* only when VERA_VERBOSE=1 is set in the environment."""
    if VERBOSE:
        print(f"    [dbg] {msg}", flush=True)


OTA_SERVICE_UUID = uuids.normalize_uuid_16(0x8018)
OTA_COMMAND_UUID = uuids.normalize_uuid_16(0x8022)
OTA_FIRMWARE_UUID = uuids.normalize_uuid_16(0x8020)
START_COMMAND = 0x0001
STOP_COMMAND = 0x0002
ACK_COMMAND = 0x0003
ACK_ACCEPTED = 0x0000
ACK_REJECTED = 0x0001
FW_ACK_SUCCESS = 0x0000
FW_ACK_CRC_ERROR = 0x0001
FW_ACK_SECTOR_ERROR = 0x0002
FW_ACK_LEN_ERROR = 0x0003
RSP_CRC_ERROR = 0xFFFF

def parse_args():
    parser = argparse.ArgumentParser(description="OTA Update Script")
    parser.add_argument("file_name", nargs='?', help="The file name for the OTA update")
    parser.add_argument("mac_address", nargs='?', help="The MAC address of the device to connect to")
    return parser.parse_args()

def crc16_ccitt(buf):
    crc16 = 0
    for byte in buf:
        crc16 ^= byte << 8
        for _ in range(8):
            if crc16 & 0x8000:
                crc16 = (crc16 << 1) ^ 0x1021
            else:
                crc16 = crc16 << 1
            crc16 &= 0xFFFF  # Ensure crc16 remains a 16-bit value
    return crc16

async def fw_notification_handler(sender, data, queue):
    if len(data) == 20:
        sector_sent = int.from_bytes(data[0:2], byteorder='little')
        status = int.from_bytes(data[2:4], byteorder='little')
        cur_sector = int.from_bytes(data[4:6], byteorder='little')
        crc = int.from_bytes(data[18:20], byteorder='little')
       # print(f"SECTOR_SENT: {sector_sent}")
       # print(f"STATUS: {status}")
       # print(f"CUR_SECTOR: {cur_sector}")

        if crc16_ccitt(data[0:18]) != crc:
            status = RSP_CRC_ERROR

        await queue.put((status, cur_sector))

async def cmd_notification_handler(sender, data, queue):
    if len(data) == 20:
        ack = int.from_bytes(data[0:2], byteorder='little')
        cmd = int.from_bytes(data[2:4], byteorder='little')
        rsp = int.from_bytes(data[4:6], byteorder='little')
        crc = int.from_bytes(data[18:20], byteorder='little')

        if crc16_ccitt(data[0:18]) != crc:
            print("Command response CRC error")
            rsp = RSP_CRC_ERROR

        await queue.put(rsp)

async def upload_sector(client, sector, sec_idx):
    max_bytes = min(512, client.mtu_size - 3) - 3 # 3 bytes for the packet header, 3 bytes for the BLE overhead
    chunks = [sector[i:i+max_bytes] for i in range(0, len(sector), max_bytes)]
    for sequence, chunk in enumerate(chunks):
        if sequence == len(chunks) - 1:
            sequence = 0xFF  # Indicate to peer this is the last chunk of sector

        data = sec_idx.to_bytes(2, byteorder='little')
        data += sequence.to_bytes(1, byteorder='little')
        data += chunk
        await client.write_gatt_char(OTA_FIRMWARE_UUID, data, response=False)

async def connect_to_device(address, file_size, sectors):
    try:
        vlog(f"Connecting to {address} (scanning)...")
        async with BleakClient(address) as client:
            print(f"Connected to {address}", flush=True)
            # bleak defaults to the minimum BLE ATT MTU (23 bytes) unless we
            # explicitly negotiate the real MTU BlueZ agreed on with the
            # peer. Without this, each firmware chunk is ~17 bytes instead
            # of up to ~509 bytes, making the transfer ~20x slower.
            try:
                await client._backend._acquire_mtu()
                print(f"Negotiated MTU: {client.mtu_size} bytes "
                      f"({min(512, client.mtu_size - 3) - 3} bytes usable per chunk)",
                      flush=True)
            except Exception as mtu_err:
                print(f"Could not acquire MTU ({mtu_err}), using default "
                      f"{client.mtu_size} — transfer will be slow", flush=True)
            vlog(f"Services: {[s.uuid for s in client.services]}")
            queue = asyncio.Queue()
            await client.start_notify(OTA_COMMAND_UUID, lambda sender,
                                      data: asyncio.create_task(cmd_notification_handler(sender, data, queue)))
            vlog(f"Subscribed to command characteristic {OTA_COMMAND_UUID}")
            print(f"Sending start command (firmware {file_size} bytes, "
                  f"{len(sectors)} sectors)", flush=True)
            command = bytearray(20)
            command[0:2] = START_COMMAND.to_bytes(2, byteorder='little')
            command[2:6] = file_size.to_bytes(4, byteorder='little')
            crc16 = crc16_ccitt(command[0:18])
            command[18:20] = crc16.to_bytes(2, byteorder='little')
            while True:
                await client.write_gatt_char(OTA_COMMAND_UUID, command)
                ack = await queue.get()
                if ack != RSP_CRC_ERROR:
                    break

            if ack == ACK_ACCEPTED:
                await client.start_notify(OTA_FIRMWARE_UUID, lambda sender,
                                          data: asyncio.create_task(fw_notification_handler(sender, data, queue)))
                print("Sending firmware...", flush=True)
                sec_idx = 0
                sec_count = len(sectors)
                started = time.monotonic()
                retries = 0
                while sec_idx < sec_count:
                    sector = sectors[sec_idx]
                    vlog(f"sending sector {sec_idx} ({len(sector)} bytes)")
                    await upload_sector(client, sector,
                                         sec_idx if len(sector) == 4098 else 0xFFFF) # send last sector as 0xFFFF
                    ack, rsp_sector = await queue.get()

                    if ack == FW_ACK_SUCCESS:
                        done = sec_idx + 1
                        pct = done / sec_count * 100
                        bar_filled = int(pct / 100 * 30)
                        bar = "#" * bar_filled + "-" * (30 - bar_filled)
                        elapsed = time.monotonic() - started
                        rate = (done * 4096) / elapsed if elapsed > 0 else 0
                        eta = (sec_count - done) * (elapsed / done) if done else 0
                        print(f"\r[{bar}] {pct:5.1f}%  sector {done}/{sec_count}  "
                              f"{rate / 1024:.1f} KiB/s  ETA {int(eta) // 60}m{int(eta) % 60:02d}s"
                              f"{f'  retries {retries}' if retries else ''}   ",
                              end='', flush=True)
                        if sec_idx == sec_count - 1:
                            print(f"\nOTA update complete in "
                                  f"{int(elapsed) // 60}m{int(elapsed) % 60:02d}s "
                                  f"({retries} retries)", flush=True)
                            await client.disconnect()
                        sec_idx += 1
                        continue

                    if ack == FW_ACK_CRC_ERROR or ack == FW_ACK_LEN_ERROR or ack == RSP_CRC_ERROR:
                        retries += 1
                        print("\n" + ("Length Error" if ack == FW_ACK_LEN_ERROR else "CRC Error") +
                              f" - Retrying sector {sec_idx}", flush=True)

                    elif ack == FW_ACK_SECTOR_ERROR:
                        retries += 1
                        print(f"\nSector Error, sending sector: {rsp_sector}", flush=True)
                        sec_idx = rsp_sector

                    else:
                        print(f"\nUnknown error (ack={ack:#06x})", flush=True)
                        await client.disconnect()
                        break
            else:
                print(f"Start command rejected (ack={ack:#06x})", flush=True)
                await client.disconnect()

    except Exception as e:
        print(f"{type(e).__name__}: {e}", flush=True)
        if VERBOSE:
            import traceback
            traceback.print_exc()
        sys.exit(1)

async def main():
    devices = []

    def detection_callback(device, advertisement_data):
        if device.address not in [d.address for d in devices]:
            print(f"Detected device: {device.name} - {device.address}")
            devices.append(device)
    try:
        args = parse_args()
        file_name = args.file_name
        mac_address = args.mac_address

        if not file_name:
            file_name = input("Enter the file name for the OTA update: ")

        if not os.path.isfile(file_name):
            print('Invalid file %s' % (file_name))
            sys.exit()

        file_size = os.path.getsize(file_name) & 0xFFFFFFFF
        if not file_size:
            print('Invalid file size %d' % (file_size))
            sys.exit()

        sectors = []
        with open(file_name, 'rb') as file:
            while True:
                sector = file.read(4096)
                if not sector:
                    break
                sector += crc16_ccitt(sector).to_bytes(2, byteorder='little')
                sectors.append(sector)

        if not mac_address:
            async with BleakScanner(detection_callback, [OTA_SERVICE_UUID]):
                print("Scanning for devices...")
                await asyncio.sleep(5)
                for dev_num, device in enumerate(devices):
                    print(f"Option {dev_num + 1}: {device.name} - {device.address}")

                if not devices:
                    print("No devices found")
                    return

                while True:
                    dev_num = input("Enter the device number to connect to: ")
                    try:
                        dev_num = int(dev_num)
                        if dev_num < 1 or dev_num > len(devices):
                            print("Invalid device number")
                            continue
                        else:
                            break
                    except ValueError:
                        print("Invalid input, please enter a number")
                        continue

                device = devices[dev_num - 1]  # Adjust for 0-based index
                print(f"Selected: {device.name} - {device.address}")
                mac_address = device.address

        await connect_to_device(mac_address, file_size, sectors)

    except:
        sys.exit(0)

asyncio.run(main())
