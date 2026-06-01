"""IoT device plugins package.

Place IoTDevice subclasses here to make them discoverable by
``src.iot.loader.discover_types()``.

Each module in this package is scanned at import time and every class that:
  1. Subclasses ``IoTDevice`` (directly or indirectly), AND
  2. Has a non-empty ``device_id`` class attribute

…is registered as an available plugin type.

Example layout::

    src/iot/devices/
        __init__.py        ← this file
        soil_moisture.py   ← class SoilMoistureDevice(IoTDevice): ...
        thermostat.py      ← class ThermostatDevice(IoTDevice): ...
"""
