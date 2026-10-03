# Binary Packet Inspector (Avionics)

*This toolset is designed specifically to work with the [Avionics V2](https://github.com/VelR-IITT/Rocketry-Avionics) firmware developed by the **VelR Rocketry Club of IIT Tirupati**.*

A versatile, multi-platform toolset designed to parse, inspect, and export binary flight logs from avionics hardware.

The inspector is built around a single "Source of Truth" (`core/schema.py`) that defines the C++ structs used on the firmware. It seamlessly handles fixed-stride binary packets, decodes bit-fields (error flags, continuity states), and provides three independent ways to interact with the data.

## 1. The Command Line Interface (`bpi.py`)
A fast, standalone Python CLI utilizing only the standard library. Perfect for automated scripts or quick terminal checks.

```bash
# Get a health summary (counts, drop rates, out-of-order writes)
python bpi.py flight.bin --info

# Export all sensors to a flat CSV
python bpi.py flight.bin --csv --order packet --out raw_flight.csv

# Export IMU and Baro, synchronized to the IMU timestamps
python bpi.py flight.bin --csv imu baro --format merged --align imu --order chrono --out clean.csv
```

## 2. The Desktop GUI (`gui.py`)
An interactive desktop application built with `dearpygui`. 
* Visually filter sensor types.
* Toggle between Packet and Chronological ordering.
* **Live Detail Panel**: Click any packet to see its raw hex dump and a full breakdown of its bit-field flags (e.g., `error_code`, `state`).
* Smart CSV Export naming.

**Setup:**
```bash
pip install -r requirements.txt
python gui.py
```

## 3. The Web Application (`index.html`)
A complete 1-to-1 clone of the Desktop GUI that runs **entirely in your web browser** using WebAssembly (PyScript).
* **Zero Installation**: Open the web page on any device (PC, tablet, phone).
* **Private**: The Python engine runs locally in your browser. Your `.bin` files are never uploaded to a server.
* **Always in Sync**: It directly imports the exact same `core/` backend files. If you update `schema.py`, the web app automatically uses the new schema on the next refresh.

**To host via GitHub Pages:**
Simply go to your repository settings on GitHub, enable GitHub Pages, and point it to the `main` branch.

**To test locally:**
```bash
python -m http.server 8000
```
Then navigate to `http://localhost:8000`.

## Architecture Details
For a deeper dive into the binary format, stride-parsing mechanism, and internal data flows, see [ARCHITECTURE.md](ARCHITECTURE.md).
