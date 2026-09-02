# Copyright 2024 Ryan Powell and NimBLEOta contributors
# Sponsored by Theengs https://www.theengs.io, https://github.com/theengs
# MIT License

import asyncio
import argparse
import os
import subprocess
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
# Seconds to wait for a device ACK before declaring the transfer stalled.
# Without this the uploader blocks forever and looks identical to a slow
# transfer, which is exactly what a silent OTA failure looks like.
ACK_TIMEOUT = float(os.environ.get("VERA_OTA_ACK_TIMEOUT", "15"))
# Seconds to pause between write-without-response chunks. Write-without-
# response is unacknowledged, so bursting a whole 4KB sector can overrun the
# BLE controller buffers and silently drop packets.
WRITE_RESPONSE = os.environ.get("VERA_OTA_WRITE_RESPONSE", "0") == "1"
CHUNK_DELAY = float(os.environ.get("VERA_OTA_CHUNK_DELAY", "0.01"))
# Force a specific MTU for chunk sizing, e.g. VERA_OTA_MTU=23 to fall back to
# the minimum ATT MTU. Useful for isolating large-write problems on the
# device side. 0 (default) uses the link's negotiated MTU.
MTU_OVERRIDE = int(os.environ.get("VERA_OTA_MTU", "0"))

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

def _read_mtu_from_dbus(char_path):
    """Read the live BlueZ MTU property for a characteristic D-Bus path.

    bleak's cached property dict is populated during service discovery, which
    happens before MTU negotiation completes, so it usually lacks the "MTU"
    key. Querying BlueZ directly gets the real negotiated value without the
    side effects of AcquireWrite.
    """
    try:
        out = subprocess.run(
            ["busctl", "get-property", "org.bluez", char_path,
             "org.bluez.GattCharacteristic1", "MTU"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            # Output looks like: q 517
            parts = out.stdout.split()
            if len(parts) == 2 and parts[1].isdigit():
                vlog(f"MTU: from busctl on {char_path} -> {parts[1]}")
                return int(parts[1])
        vlog(f"MTU: busctl returned rc={out.returncode} {out.stdout.strip()}"
             f"{out.stderr.strip()}")
    except Exception as err:
        vlog(f"MTU: busctl lookup failed: {err}")
    return None


def _read_char_mtu(client, char_uuid):
    """Return the negotiated ATT MTU for a characteristic, or None.

    Reads the BlueZ "MTU" D-Bus property directly. Unlike
    BleakClient._backend._acquire_mtu() this has no side effects and does not
    take ownership of the characteristic.
    """
    try:
        char = client.services.get_characteristic(char_uuid)
        if char is None:
            vlog("MTU: characteristic not found")
            return None
        # Preferred: bleak computes this from the BlueZ "MTU" property as
        # MTU - 3. It is a property, so read it defensively.
        try:
            size = char.max_write_without_response_size
            if size and size > 20:
                vlog(f"MTU: from max_write_without_response_size ({size})")
                return int(size) + 3
        except Exception as err:
            vlog(f"MTU: max_write_without_response_size failed: {err}")
        # Fallback: read the MTU straight off D-Bus. bleak caches the BlueZ
        # property dict at service-discovery time, before the MTU has been
        # negotiated, so the cached copy often has no "MTU" key at all. The
        # live object always does once connected.
        for attr in ("obj", "_properties"):
            raw = getattr(char, attr, None)
            props = None
            path = None
            if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], dict):
                path, props = raw
            elif isinstance(raw, dict):
                props = raw
            if props and "MTU" in props:
                vlog(f"MTU: from {attr}['MTU']")
                return int(props["MTU"])
            if path:
                mtu = _read_mtu_from_dbus(path)
                if mtu:
                    return mtu
        vlog("MTU: no usable MTU source on characteristic")
    except Exception as err:
        vlog(f"MTU property read failed: {err}")
    return None


async def upload_sector(client, sector, sec_idx, mtu=23):
    max_bytes = min(512, mtu - 3) - 3 # 3 bytes for the packet header, 3 bytes for the BLE overhead
    chunks = [sector[i:i+max_bytes] for i in range(0, len(sector), max_bytes)]
    for sequence, chunk in enumerate(chunks):
        if sequence == len(chunks) - 1:
            sequence = 0xFF  # Indicate to peer this is the last chunk of sector

        data = sec_idx.to_bytes(2, byteorder='little')
        data += sequence.to_bytes(1, byteorder='little')
        data += chunk
        await client.write_gatt_char(OTA_FIRMWARE_UUID, data, response=WRITE_RESPONSE)
        # Write-without-response has no flow control, so a burst of large
        # chunks can overrun the controller's buffers and silently drop
        # packets. The device only ACKs once it has seen every packet in a
        # sector, so a single dropped chunk stalls the transfer forever.
        if CHUNK_DELAY:
            await asyncio.sleep(CHUNK_DELAY)

async def connect_to_device(address, file_size, sectors):
    try:
        vlog(f"Connecting to {address} (scanning)...")
        async with BleakClient(address) as client:
            print(f"Connected to {address}", flush=True)
            # bleak's mtu_size defaults to the 23-byte ATT minimum unless the
            # MTU has been acquired. Do NOT use client._backend._acquire_mtu()
            # here: it calls BlueZ's AcquireWrite on the first
            # write-without-response characteristic, which for this device is
            # the OTA firmware characteristic itself. AcquireWrite hands that
            # characteristic to a dedicated file descriptor and takes exclusive
            # ownership, so subsequent write_gatt_char() calls are silently
            # discarded and the device never ACKs a sector.
            #
            # BlueZ >= 5.62 exposes the negotiated MTU as a plain readable
            # D-Bus property on each characteristic, so read that instead --
            # same value, no side effects.
            mtu = _read_char_mtu(client, OTA_FIRMWARE_UUID)
            if MTU_OVERRIDE:
                negotiated_mtu = MTU_OVERRIDE
                print(f"Using forced MTU: {negotiated_mtu} bytes "
                      f"({min(512, negotiated_mtu - 3) - 3} bytes usable per "
                      f"chunk; link negotiated {mtu or 'unknown'})", flush=True)
            elif mtu:
                negotiated_mtu = mtu
                print(f"Negotiated MTU: {mtu} bytes "
                      f"({min(512, mtu - 3) - 3} bytes usable per chunk)",
                      flush=True)
            else:
                negotiated_mtu = 23
                print("Could not read MTU property (BlueZ < 5.62?), using "
                      "default 23 — transfer will be slow", flush=True)
            vlog(f"Services: {[s.uuid for s in client.services]}")
            queue = asyncio.Queue()
            await client.start_notify(OTA_COMMAND_UUID, lambda sender,
                                      data: asyncio.create_task(cmd_notification_handler(sender, data, queue)))
            vlog(f"Subscribed to command characteristic {OTA_COMMAND_UUID}")

            # NOTE: deliberately NOT sending a stop command here.
            #
            # NimBLEOta::abortUpdate() calls esp_ota_abort(m_writeHandle)
            # unconditionally without clearing m_writeHandle afterwards, so
            # a stop sent when no update is running hands ESP-IDF a stale
            # handle and can crash/reset the device (observed as the BLE
            # link dropping mid-handshake with "Service Discovery has not
            # been performed yet").
            #
            # A stop is also unnecessary: commandOnWrite() already handles a
            # start arriving while an update is in progress -- it resumes if
            # the file length matches, and aborts cleanly on its own if it
            # does not.
            print(f"Sending start command (firmware {file_size} bytes, "
                  f"{len(sectors)} sectors)", flush=True)
            command = bytearray(20)
            command[0:2] = START_COMMAND.to_bytes(2, byteorder='little')
            command[2:6] = file_size.to_bytes(4, byteorder='little')
            crc16 = crc16_ccitt(command[0:18])
            command[18:20] = crc16.to_bytes(2, byteorder='little')
            start_attempts = 0
            while True:
                await client.write_gatt_char(OTA_COMMAND_UUID, command)
                try:
                    ack = await asyncio.wait_for(queue.get(), timeout=ACK_TIMEOUT)
                except asyncio.TimeoutError:
                    start_attempts += 1
                    if start_attempts < 3:
                        print(f"No response to start command "
                              f"(attempt {start_attempts}/3), retrying...",
                              flush=True)
                        await asyncio.sleep(2)
                        continue
                    print(f"\nTimed out after {ACK_TIMEOUT:.0f}s waiting for the "
                          f"device to acknowledge the start command "
                          f"({start_attempts} attempts).\n"
                          f"The OTA service is present but not responding. "
                          f"Power-cycle the display and retry.", flush=True)
                    await client.disconnect()
                    return False
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
                                         sec_idx if len(sector) == 4098 else 0xFFFF, # send last sector as 0xFFFF
                                         negotiated_mtu)
                    try:
                        ack, rsp_sector = await asyncio.wait_for(queue.get(),
                                                                 timeout=ACK_TIMEOUT)
                    except asyncio.TimeoutError:
                        print(f"\nTimed out after {ACK_TIMEOUT:.0f}s waiting for "
                              f"an ACK on sector {sec_idx}/{sec_count}.", flush=True)
                        if sec_idx == 0:
                            print("The device accepted the start command but "
                                  "never acknowledged the first sector.\n"
                                  "This usually means it is still holding OTA "
                                  "state from an interrupted run: NimBLEOta\n"
                                  "silently drops sectors whose index does not "
                                  "match the one it expects.\n"
                                  "Power-cycle the display and retry.",
                                  flush=True)
                        else:
                            print("The device stopped acknowledging mid-transfer "
                                  "(link dropped or flash error).\n"
                                  "Check the display for an OTA error message.",
                                  flush=True)
                        await client.disconnect()
                        return False

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
                            return True
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
                        return False
            else:
                print(f"Start command rejected (ack={ack:#06x})", flush=True)
                await client.disconnect()
                return False

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

        ok = await connect_to_device(mac_address, file_size, sectors)
        if ok is False:
            sys.exit(1)

    except SystemExit:
        raise
    except Exception as err:
        print(f"{type(err).__name__}: {err}", flush=True)
        sys.exit(1)

asyncio.run(main())
