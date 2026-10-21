# XHUB Mapper

<img width="693" height="719" alt="screenshot" src="https://github.com/user-attachments/assets/98b83aaf-b4a3-42ca-a70d-dc24fe746f98" />

## About

`xhub_mapper.py` automates ACPI-based USB port mapping using the **XHUB/HUBX method**. It analyzes your system's ACPI tables, identifies declared USB ports, helps you select the ports you want/need, and generates `SSDT-XHUB.dsl` for use with OpenCore.

For the underlying method and additional documentation, see [**OC-Little-Translated — XHUB Method**](https://github.com/5T33Z0/OC-Little-Translated/tree/main/Content/03_USB_Fixes/ACPI_Mapping_USB_Ports/XHUB_Method).

## Features

- **Interactive and command-line modes** for manual use and scripting.
- **ACPI table analysis** of DSDT and SSDT files, including references to USB controllers, root hubs, and individual ports.
- **Automatic `_ADR` evaluation**, including simple arithmetic expressions, named constants, and zero-argument helper methods.
- **Port selection** when more than 15 ports are declared for a controller.
- **Automatic SSDT generation** with optional compilation and installation to an OpenCore EFI.
- **IASL integration** for decompiling binary ACPI tables (`.aml`).
- **Darwin detection options** using either `_OSI("Darwin")` or [**OSDW-Method**](https://github.com/5T33Z0/OC-Little-Translated/tree/main/Content/01_Adding_missing_Devices_and_enabling_Features/SSDT-OSDW).
- **Uncertainty reporting** for port addresses that cannot be resolved reliably.
- **Dynamic port identification** for ports whose availability depends on firmware variables.

## Disclaimer

**This project is a work in progress.** XHUB Mapper automates the analysis and generation process, but it cannot reliably infer every firmware-dependent behavior from static ACPI tables. Review the generated SSDT and verify the resulting USB mapping on your hardware.

Before making any changes to your OpenCore configuration:

- **Keep a backup of your known-working EFI folder on a separate USB flash drive formatted as FAT32.**
- Verify that the backup contains a complete, working EFI folder and that you can boot from it.
- Test changes before relying on them for everyday use.
- Do not replace your only working EFI configuration with an untested one.

Use this tool at your own risk. No guarantee is made that the generated ACPI tables will work correctly on every system.

## Requirements

- Python 3.8 or newer
- Standard Python library – No third-party Python packages are required
- `iasl` for decompiling binary ACPI tables; the script can offer to download a compatible binary if one is not found.

---

## Usage

There are two ways to use the script.

### 1. Interactive mode

Run the script without arguments:

```bash
python3 xhub_mapper.py
```

When prompted, drag and drop your ACPI folder (for example, OpenCore's `SysReport/ACPI` directory) or individual DSDT/SSDT files into the terminal, then press Enter.

The interactive workflow guides you through:

1. Loading ACPI tables.
2. Reviewing the detected USB ports.
3. Selecting the ports to include.
4. Generating `SSDT-XHUB.dsl`.
5. Optionally compiling the SSDT and installing it into your OpenCore EFI.

### 2. Command-line mode

The command-line interface supports scanning, generating, compiling, and installing the resulting SSDT.

#### Scan ACPI tables

```bash
python3 xhub_mapper.py scan SysReport/ACPI -o ports.json
```

If no input path is supplied, the script prompts you to provide paths interactively.

#### Generate the SSDT

```bash
python3 xhub_mapper.py generate ports.json
```

#### Generate and compile

```bash
python3 xhub_mapper.py generate ports.json --compile
```

#### Install into an OpenCore EFI

```bash
python3 xhub_mapper.py generate ports.json --efi /Volumes/EFI/EFI/OC
```

#### Use OSDW instead of `_OSI("Darwin")`

```bash
python3 xhub_mapper.py generate ports.json --darwin-check osdw
```

#### IASL options

To skip the interactive download prompt:

```bash
python3 xhub_mapper.py scan SysReport/ACPI --download-iasl
```

To specify an existing IASL binary:

```bash
python3 xhub_mapper.py scan SysReport/ACPI --iasl /path/to/iasl
```

---

## How ACPI Analysis Works

### 1. Locate the USB root hub

The script looks for the USB root hub in the DSDT first. It recognizes `RHUB` and the older `HUBN` naming convention.

If no DSDT is loaded, it searches all loaded tables for the root hub.

### 2. Find references across SSDTs

The remaining ACPI tables are inspected for references to the controller, root hub, and individual ports.

Recognized declarations include:

- `External` declarations for the XHCI controller.
- `External` declarations for the root hub.
- `External` declarations for individual ports.
- `Scope` declarations targeting individual ports.

For example:

```asl
External (_SB_.PCI0.XHC_, DeviceObj)
External (_SB_.PCI0.XHC_.RHUB, DeviceObj)
External (..RHUB.HS01, DeviceObj)

Scope (..RHUB.HS01)
{
    // Port definition
}
```

Actual ACPI paths vary between systems and firmware implementations.

### 3. Collect and resolve port definitions

The script collects declared ports, including `HS`, `SS`, `USR`, and other port names, and retrieves their `_ADR` and `_UPC` properties wherever they are defined.

It attempts to evaluate `_ADR` values expressed as:

- Literal values.
- Simple `Add` and `Subtract` expressions.
- The `+` and `-` operators.
- Named constants.
- Zero-argument helper methods.

When a helper method can produce multiple possible values, the script may infer the value that best fits the usual HS → USR → SS port layout. Such values are flagged as **inferred**.

If an address cannot be resolved, it is displayed as `?`, together with a note explaining the uncertainty.

### 4. Enforce the 15-port limit

macOS supports a maximum of **15 USB ports per controller** under the conventional USB port-mapping limit.

If the ACPI tables declare more than 15 ports for a controller, the script opens the port editor and asks you to choose which ports to retain. SSDT generation is blocked until the selection satisfies the limit.

### 5. Handle internal USB devices

Built-in devices, such as Bluetooth modules and laptop webcams, require an appropriate internal port type (`0xFF`) when mapped.

The port editor and generator remind you to account for these devices.

---

## Important Notes

### Load ACPI tables from one system only

Do not combine ACPI tables from different computers. Port definitions, controller paths, firmware methods, and device relationships are system-specific.

### Dynamic USB ports

Whether a port is actually enabled or connected may depend on firmware variables evaluated at runtime. For example, Intel firmware SSDTs may use variables such as `UMAP` and `UMP3`.

These ports are marked as `dyn` in the OEM column when their availability cannot be determined from the static ACPI definitions alone.

This is not necessarily an error: static ACPI analysis cannot reliably determine every runtime firmware decision.

### Drag and drop

Drag and drop works by pasting the selected path into the terminal as text. The script's `check_path()` function cleans up common formatting artifacts, including:

- Surrounding quotes.
- Trailing whitespace.
- Backslash-escaped spaces.
- The `~` home-directory shorthand.

This behavior follows the general approach used by [SSDTTime](https://github.com/corpnewt/SSDTTime).

### Binary ACPI tables and IASL

Binary ACPI tables (`.aml`) must be decompiled before they can be analyzed as source code.

If no working `iasl` binary is found, the script can offer to download one into `./tools`, using the same sources as SSDTTime.

Use `--download-iasl` to skip the confirmation prompt or `--iasl PATH` to specify your own binary.

Decompiled tables are stored in:

```text
Results/decompiled/
```

## Credits

- **Concept and inspiration:** [corpnewt/SSDTTime](https://github.com/corpnewt/SSDTTime) — particularly the automation of ACPI table patching workflows.
- **Implementation:** Claude AI, with human direction and project-specific requirements.




