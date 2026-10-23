# XHUB Mapper (`xhub_mapper`)

[![Platform](https://img.shields.io/badge/Platform-macOS-lightgrey.svg)](https://apple.com)
[![Python Version](https://img.shields.io/badge/Python-3.8%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![OpenCore](https://img.shields.io/badge/OpenCore-0.6.0%2B-informational.svg)](https://github.com/acidanthera/OpenCorePkg)

A macOS-native automation tool for **ACPI-based USB port mapping** using the **XHUB/HUBX method**. 

`xhub_mapper` pairs deep static ACPI table analysis (DSDT/SSDT) with **live IORegistry introspection** to accurately detect physically present xHCI controllers, eliminating "phantom" controllers while generating clean, update-proof SSDTs and updating OpenCore's `config.plist`.

---

## Table of Contents

- [Why the XHUB/HUBX Method?](#why-the-xhubhubx-method)
- [Key Features](#key-features)
- [How It Works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Usage](#usage)
  - [1. Interactive Mode (Default)](#1-interactive-mode-default)
  - [2. CLI / Scripting Mode](#2-cli--scripting-mode)
- [Port Editor Reference](#port-editor-reference)
  - [Navigation & Commands](#navigation--commands)
  - [ACPI Connector Types (`_UPC`)](#acpi-connector-types-_upc)
- [Supported Formats & Ingestion](#supported-formats--ingestion)
- [Self-Test Suite](#self-test-suite)
- [Credits & Acknowledgments](#credits--acknowledgments)
- [License](#license)

---

## Why the XHUB/HUBX Method?

Traditional Hackintosh USB mapping relies on injector kexts (`USBMap.kext`, `USBToolBox.kext`, or `USBPorts.kext`). While effective, kext-based injection can break during major macOS kernel updates, interfere with sleep states, or fail when Apple changes internal USB driver personalities or when switching to another SMBIOS.

The **XHUB/HUBX ACPI method** (originated by [samuelnotfound](https://github.com/samuelnotfound/miscellaneous-hackintosh-guides/tree/main/SSDT_USB_Mapping))

1. Disables the OEM root hub (`RHUB` / `HUBN`) exclusively under macOS using an `_STA` method check (`_OSI("Darwin")` or [`OSDW()`](https://github.com/5T33Z0/OC-Little-Translated/tree/main/Content/01_Adding_missing_Devices_and_enabling_Features/SSDT-OSDW)), leaving other OSes (Windows/Linux) completely unaffected.
2. Injects a companion root hub (`XHUB` / `HUBX`) that claims the hardware address (`_ADR 0x00`) under macOS.
3. Injects only your selected active ports (up to the 15-port limit per controller) with custom `_UPC` (port capabilities) and `_PLD` (physical location) blocks.
4. **Completely kext-less:** Survives any macOS update, system reinstall, or recovery environment without modification.

---

## Key Features

- **Live macOS IORegistry Introspection:** 
  Queries IOKit via native XML plists (`ioreg -a`) to inspect hardware PCI Class Codes (`0x0C0330`), identifying which controllers actually exist and have drivers loaded. No more guessing between 4 different conditional Thunderbolt paths (`RP01`, `RP05`, `RP09`, `RP21`) defined in OEM firmware.
- **Deep ACPI Extraction & Disassembly:**
  - Automated multi-file `iasl` decompilation with batched namespace resolution (`-da -dl`) and fallback passes.
  - Full AST resolution across multiple SSDTs (resolving cross-table `Scope` and `External` references).
  - Built-in arithmetic & bitwise ASL evaluator (`Add`, `Subtract`, `And`, `Or`, `ShiftLeft`, `ShiftRight`, etc.) to resolve dynamically calculated `_ADR` methods.
  - Automatically preserves authentic OEM `_PLD` physical layout buffers when present in firmware.
  - Identifies built-in Intel Bluetooth devices using ACPI `DBPN` constants.
- **Universal Port Map Ingestion:**
  Drop your existing port maps directly into the tool. It automatically detects and imports:
  - **Native `ports.json`**
  - **DhinakG's USBToolBox (`usb.json`)** (from Windows or macOS discovery dumps)
  - **corpnewt's USBMap (`USB.json` or `USB.plist`)**
- **Automated OpenCore Integration:**
  Compiles the resulting `.dsl` to `.aml` and installs directly to your `EFI/OC` folder—automatically backing up `config.plist`, enabling the SSDT in `ACPI > Add`, and applying necessary controller renames (`XHC1 -> SHCI`, `EHC1 -> EH01`, etc.) in `ACPI > Patch`.
- **Zero Third-Party Dependencies:** 
  Written entirely using Python's standard library (`plistlib`, `subprocess`, `urllib`, `dataclasses`).

---

## How It Works

```
┌─────────────────────────────────┐       ┌─────────────────────────────────┐
│     Live macOS IORegistry       │       │    ACPI Tables (DSDT / SSDTs)   │
│  - Active PCI BDF & class-code  │       │  - Full port list (HSxx/SSxx)   │
│  - Exact acpi-path correlation  │       │  - Native _ADR, _UPC, and _PLD  │
└────────────────┬────────────────┘       └────────────────┬────────────────┘
                 │                                         │
                 └───────────────────┬─────────────────────┘
                                     │
                                     ▼
                      ┌─────────────────────────────┐
                      │    XHUB Mapper Controller   │
                      │  - Filters ghost hubs       │
                      │  - Validates 15-port limit  │
                      │  - Normalizes port types    │
                      └──────────────┬──────────────┘
                                     │
                                     ▼
                      ┌─────────────────────────────┐
                      │    SSDT-XHUB.dsl / .aml     │
                      │  - Disables OEM RHUB (_STA) │
                      │  - Injects XHUB under macOS │
                      │  - Installs to OpenCore EFI │
                      └─────────────────────────────┘
```

---

## Requirements

- **Operating System:** macOS Catalina (10.15) or newer.
- **Python:** Python 3.8 or newer (included with Xcode Command Line Tools).
- **ACPI Compiler (`iasl`):** Optional to provide manually; the script will automatically search standard paths (`MaciASL`, `/opt/homebrew/bin/iasl`, `/usr/local/bin/iasl`) or offer to download acidanthera's verified macOS binary directly into `./tools`.
- **Must already know the active ports**: You can import existing ports data from USBMap and USBToolBox

---

## Installation

Clone the repository to your Mac:

```bash
git clone https://github.com/<your-username>/xhub_mapper.git
cd xhub_mapper
chmod +x xhub_mapper.py
```

---

## Usage

### 1. Interactive Mode (Default)

Simply launch the script without arguments:

```bash
python3 xhub_mapper.py
```

```text
 ############################################################
 #                       XHUB Mapper                        #
 ############################################################

 ACPI:   not loaded
 iasl:   /usr/local/bin/iasl
 Darwin Check: _OSI ("Darwin")

 1. Select ACPI folder / tables (drag and drop)
 2. Choose controllers / ports
 3. Generate SSDT-XHUB
 4. Save ports.json
 5. Load ports.json (or USBToolBox / USBMap files)
 6. Toggle Darwin check (_OSI <-> OSDW)
 Q. Quit
```

#### Workflow:
1. **Load Tables (`Option 1`):** Drag and drop your OpenCore ACPI dump folder (e.g. `SysReport/ACPI`) or your DSDT + SSDT files into the terminal and hit Enter. The tool scans all tables and displays the active vs. ghost controllers detected by IORegistry.
2. **Review & Map Ports (`Option 2`):** Pick your controller and toggle ports to include (up to 15 per controller). Set connector types (`typea`, `usb3-a`, `typec-switch`, `internal`, etc.).
3. **Generate & Install (`Option 3`):** Generates `Results/SSDT-XHUB.dsl`, compiles it to `.aml`, and offers to install it directly to your `EFI/OC` directory.
4. **Import Existing Discovery (`Option 5`):** If you already plugged in drives on Windows or macOS using USBToolBox or USBMap, drag and drop `usb.json` or `USB.plist` directly to auto-populate port states and comments!

---

### 2. CLI / Scripting Mode

Automate table analysis or compilation via subcommands:

#### Scan ACPI Tables & Export JSON:
```bash
python3 xhub_mapper.py scan /path/to/SysReport/ACPI -o ports.json
```

#### Generate SSDT from Configuration:
```bash
python3 xhub_mapper.py generate ports.json --compile -o SSDT-XHUB.dsl
```

#### Generate, Compile, and Install to OpenCore EFI:
```bash
python3 xhub_mapper.py generate ports.json --compile --efi /Volumes/EFI/EFI/OC
```

#### Import Directly from USBToolBox / USBMap via CLI:
```bash
# Ingest USBToolBox dump directly and build compiled SSDT
python3 xhub_mapper.py generate usb.json --compile --efi /Volumes/EFI/EFI/OC

# Ingest corpnewt USBMap plist/json
python3 xhub_mapper.py generate USB.plist --compile -o SSDT-XHUB.dsl
```

---

## Port Editor Reference

### Navigation & Commands

Inside the port selection menu (`Option 2`), use the following commands:

| Command | Description | Example |
| :--- | :--- | :--- |
| `k <ports>` | Toggle port state (keep / drop) | `k 1,2,5-8` or `k HS01 SS01` |
| `on <ports>` | Force select ports | `on 1 2 3` |
| `off <ports>` | Force deselect ports | `off 4 5 6` |
| `all` / `none` / `oem` | Select all ports / no ports / only OEM-active ports | `oem` |
| `t <ports> <type>` | Assign port connector type | `t 1,2 usb3-a` or `t HS07 internal` |
| `a <ports> <adr>` | Manually set the `_ADR` address | `a SS01 0x11` |
| `a auto` | Infer missing `_ADR`s using Intel xHCI rules | `a auto` |
| `l <port> <text>` | Assign custom label / comment | `l HS01 Front Panel Left` |
| `d` | Done (return to main menu) | `d` |

### ACPI Connector Types (`_UPC`)

| Type Name | Byte Value | Description |
| :--- | :---: | :--- |
| `typea` | `0x00` | Standard USB 2.0 Type-A connector |
| `usb3-a` | `0x03` | Standard USB 3.0 Type-A connector (SuperSpeed companion) |
| `typec-hs`| `0x08` | Type-C connector operating strictly in USB 2.0 mode |
| `typec-switch` | `0x09` | Type-C with internal hardware switch (same personality on flip) |
| `typec-noswitch` | `0x0A` | Type-C without hardware switch (two distinct personalities on flip) |
| `internal` | `0xFF` | **Internal device** (Bluetooth, webcam, card reader, AIO cooler) |

> [!IMPORTANT]
> 
> **Internal Devices:** Any port powering internal hardware (such as an Intel/Broadcom Bluetooth module or a laptop webcam) **must** be set to `internal` (`0xFF`). If marked as `typea`, Bluetooth will fail to wake from sleep or disconnect randomly.

---

## Supported Formats & Ingestion

`xhub_mapper` can ingest:
1. **Raw Firmware Files:** `.aml` and `.dsl` files directly from `SysReport/ACPI` or dumped via SSDTTime / Clover / Linux sysfs.
2. **Native JSON:** `ports.json` saved from previous interactive sessions.
3. **USBToolBox JSON (`usb.json`):** Created by DhinakG's Windows/macOS discovery utility. Automatically maps `index`, translates port classes, and normalizes generic `"Port 1"` strings into valid ASL symbols (`HS01`, `SS01`).
4. **corpnewt USBMap (`USB.json` or `USB.plist`):** Automatically decodes little-endian 32-bit hex addresses (e.g. `01000000` $\to$ `0x01`), extracts `UsbConnector` codes, and honors `#port` disabled flags.

---

## Self-Test Suite

To verify the ASL parser, math evaluators, and test live IORegistry querying on your Mac:

```bash
python3 xhub_mapper.py test
```

Expected output:
```text
Running verification suite...
Live IORegistry detected 1 XHCI controller(s) on this Mac.
 - XHC at \_SB.PCI0.XHC (PCI: 00:14.0, Driver: AppleUSBXHCIPCI)
All tests passed.
```

---

## Credits & Acknowledgments

- **[samuelnotfound](https://github.com/samuelnotfound)** for the [XHUB-Method](https://github.com/5T33Z0/OC-Little-Translated/blob/main/Content/03_USB_Fixes/ACPI_Mapping_USB_Ports/XHUB_Method/README.md).
- **[corpnewt](https://github.com/corpnewt)** for [SSDTTime](https://github.com/corpnewt/SSDTTime) (multi-stage decompilation design) and [USBMap](https://github.com/corpnewt/USBMap).
- **[DhinakG](https://github.com/DhinakG)** for [USBToolBox](https://github.com/USBToolBox/tool).
- **[Acidanthera](https://github.com/acidanthera)** for OpenCore and MaciASL binaries.

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
