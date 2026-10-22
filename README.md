<img width="693" height="719" alt="screenshot" src="https://github.com/user-attachments/assets/98b83aaf-b4a3-42ca-a70d-dc24fe746f98" />

# XHUB Mapper

Automate ACPI-based USB port mapping using the XHUB/HUBX method for macOS.

`xhub_mapper.py` analyzes DSDT and SSDT tables, identifies USB controllers and their declared ports, lets you select the ports to expose to macOS, and generates a custom `SSDT-XHUB`. It can also compile the generated SSDT and optionally install it into an OpenCore EFI.

The script automates the workflow described in:

- [OC-Little Translated — XHUB Method](https://github.com/5T33Z0/OC-Little-Translated/tree/main/Content/03_USB_Fixes/ACPI_Mapping_USB_Ports/XHUB_Method)
- [samuelnotfound — ACPI USB Mapping Guide](https://github.com/samuelnotfound/miscellaneous-hackintosh-guides/tree/main/SSDT_USB_Mapping)

## About

USB port mapping is an essential part of configuring macOS on non-Apple hardware. macOS imposes a limit of 15 USB ports per controller under the conventional port-mapping model. Because USB 2 and USB 3 interfaces are often represented as separate ACPI ports, a system can exceed this limit even when it has relatively few physical connectors.

An incorrect or incomplete port map can leave devices unavailable or cause problems with internal devices, sleep, and wake. A proper map identifies the ports that should be exposed to macOS and assigns the appropriate properties to them.

On Hackintosh systems, USB mapping is commonly implemented using either a kext-based solution or an ACPI-based method.

### Why use ACPI instead of a mapping kext?

Kext-based mapping tools, such as USBToolBox with `USBToolBox.kext` and `UTBMap.kext`, provide a practical way to publish a predefined USB port map. They are often easier to configure, but require additional kernel-extension configuration.

The XHUB/HUBX method takes a different approach: it creates a macOS-specific root hub through ACPI. The original root hub remains available to other operating systems, while the replacement hub exposes only the selected ports under macOS.

**Benefits include**:

- **No dedicated USB mapping kext:** The port map is implemented through ACPI rather than a separate mapping kext.
- **Operating-system-specific behavior:** The original hub can remain available to Windows, Linux, and other operating systems.
- **Native ACPI integration:** The configuration uses the existing device hierarchy and preserves the original port addresses.
- **Inspectable configuration:** The generated ASL source can be reviewed, modified, compiled, and maintained alongside other ACPI tables.
- **Fewer mapping-specific dependencies:** The USB map does not rely on a dedicated mapping kext, although other kexts may still be necessary for unrelated hardware.
- **Independent of SMBIOS selection**: Kext-based USB maps depend on the selected SMBIOS and its matching USB configuration. Changing the SMBIOS may invalidate the existing map and require a new one. ACPI-based mapping defines the port configuration through SSDTs, making it independent of the selected SMBIOS and avoiding this particular source of mapping issues.

ACPI mapping is not inherently better in every situation. Kext-based tools are often more convenient, and either approach can provide a correct port map. ACPI is particularly useful when you want to avoid an additional mapping kext or keep macOS-specific changes separate from the original firmware configuration.

### Why does XHUB Mapper exist?

ACPI-based USB mapping has a considerably higher entry barrier than conventional kext-based methods.

Writing a reliable SSDT requires familiarity with ACPI concepts, ASL syntax, device namespaces, object references, methods, and properties such as `_ADR`, `_STA`, and `_UPC`. Relevant definitions may be spread across several DSDT and SSDT files, and firmware implementations vary between manufacturers and platforms.

Thunderbolt and USB4 controllers introduce additional complications, including controllers declared conditionally for different PCIe paths. Resolving port addresses, tracing references, handling controller renames, and writing and testing the resulting SSDT can be time-consuming and error-prone.

**XHUB Mapper bridges this gap by automating the tedious parts of ACPI analysis and SSDT generation.** It identifies relevant devices and ports, provides an interface for selecting ports, and generates the corresponding ACPI code. It also addresses common complications such as conditional controller variants, inferred port addresses, controller renames, and the 15-port limit.

The goal is to make ACPI-based USB mapping more accessible and repeatable without hiding the generated configuration or removing the need for verification.

## Scope

XHUB Mapper automates the analysis and generation steps of the XHUB/HUBX method. It:

- Scans supplied DSDT and SSDT files for USB controllers, root hubs, and port declarations.
- Extracts available port properties, including `_ADR` and `_UPC`.
- Evaluates common expressions used to calculate port addresses.
- Identifies unresolved, inferred, dynamic, and conditional port definitions where possible.
- Provides interactive port selection and enforces the conventional 15-port limit.
- Generates the `SSDT-XHUB` source code.
- Optionally compiles the SSDT and installs generated files into an OpenCore ACPI directory.
- Generates a matching OpenCore ACPI patch when a controller rename is required.

The script is specifically designed for ACPI-based USB port mapping using XHUB/HUBX. It is not a general-purpose ACPI editor, a physical USB-port detector, or a replacement for testing the resulting configuration on the target system.

Static analysis cannot reliably determine every firmware-dependent port state. Inferred or unresolved addresses and dynamic ports may require manual investigation.

## Usage overview

XHUB Mapper provides two workflows:

### 1. Interactive mode

Run the script without arguments:

```bash
python3 xhub_mapper.py
```

Provide an ACPI directory, such as OpenCore's `SysReport/ACPI`, or individual DSDT/SSDT files by dragging and dropping their paths into the terminal and pressing Enter.

The interactive workflow guides you through loading the tables, reviewing controllers and ports, selecting the ports to expose, and generating the SSDT. Compilation and installation are optional.

### 2. Command-line mode

Scan the ACPI tables and save the detected port information to JSON:

```bash
python3 xhub_mapper.py scan SysReport/ACPI -o ports.json
```

Generate and compile the SSDT from that configuration:

```bash
python3 xhub_mapper.py generate ports.json --compile
```

Install the generated files into an OpenCore ACPI directory:

```bash
python3 xhub_mapper.py generate ports.json --efi /Volumes/EFI/EFI/OC
```

Use `OSDW()` instead of `_OSI("Darwin")` for macOS detection:

```bash
python3 xhub_mapper.py generate ports.json --darwin-check osdw
```

For the complete list of options:

```bash
python3 xhub_mapper.py --help
python3 xhub_mapper.py scan --help
python3 xhub_mapper.py generate --help
```

**Recommended workflow:** Start with interactive mode, review inferred or unresolved addresses, verify the selected ports against your actual hardware, and test the generated SSDT using a backed-up EFI.

## Requirements

- **Python 3.8 or newer.**
- **Python standard library:** No third-party Python packages are required.
- **ACPI tables from the target system:** For example, an OpenCore `SysReport/ACPI` directory containing the relevant DSDT and SSDTs. Load tables from one system at a time.
- **`iasl`:** Required to decompile binary ACPI tables (`.aml`) and compile the generated SSDT.

If a working `iasl` binary cannot be found, the script can offer to download one into `./tools`. You can specify your own binary using `--iasl` or skip the download confirmation with `--download-iasl`.


>[!IMPORTANT]
>
> The XHUB method is intended for systems whose USB controller and port definitions are exposed through separate ACPI tables, as is typical of Intel Skylake and newer platforms. AMD is not excluded: compatibility depends on the ACPI implementation providing the required controller, hub, and port declarations rather than on the CPU vendor alone.

## How the XHUB/HUBX method works

The generated SSDT applies the following changes:

1. **Controller renaming:** Renames the original controller if necessary, for example `XHC1` to `SHCI`, `EHC1` to `EH01`, or `EHC2` to `EH02`.
2. **Disable the original hub under macOS:** Disables `RHUB` or `HUBN` through `_STA` for macOS while preserving it for other operating systems.
3. **Create a macOS-specific hub:** Introduces `XHUB` or `HUBX`, enabled only under macOS.
4. **Preserve the hub address:** Assigns the new hub the `_ADR` of the original hub.
5. **Expose selected ports:** Enumerates the selected ports under the new hub, preserving their original `_ADR` values.
6. **Set port properties:** Adjusts `_UPC` to describe connectability and port type.

The controller rename, when used, is accompanied by a matching OpenCore ACPI patch. Setting the controller's `rename` field to `null` in `ports.json` skips the rename.

This approach exposes a custom set of ports to macOS without simply renaming the original root hub and exposing every firmware-declared port.

## How ACPI tables are analyzed

### Controller and hub discovery

Root hubs (`RHUB`, or `HUBN` on older controllers) are searched for in the DSDT first and then in the SSDTs. Thunderbolt and USB4 xHCI controllers may declare their hubs in separate SSDTs.

The script also examines other tables for references to controllers, hubs, and individual ports, including `External` declarations and `Scope` blocks. This allows it to collect port definitions even when they are distributed across multiple tables.

### Port address evaluation

Each port has its own `_ADR`. USB 2 and USB 3 lanes associated with the same physical connector are separate ACPI ports; they do not share an `_ADR`. Firmware can associate them through `_PLD`.

The script evaluates common address expressions, including:

- Literal values and named constants.
- Simple addition and subtraction.
- Arithmetic using `+` and `-`.
- Zero-argument helper methods.

When a helper method has multiple possible return values, the script may infer the most likely value from the usual HS/USR/SS layout. Inferred values are flagged accordingly. Anything that cannot be resolved is displayed as unknown, with an explanatory note where available.

Review inferred or unknown addresses rather than assuming that every detected value is correct.

### Dynamic ports

Some firmware determines port availability at runtime through variables or methods that static analysis cannot resolve. Intel firmware tables may use variables such as `UMAP` and `UMP3`.

Such ports may be marked `dyn` in the OEM column. This indicates that their actual availability cannot be reliably determined from the ACPI declarations alone.

### Conditional controllers

Some firmware declares the same controller in multiple conditional branches. For example, a Thunderbolt/USB4 controller may be declared for several possible PCIe root ports.

The script marks these variants as conditional and generates corresponding `If (CondRefOf (...))` wrappers. Where supported by the interactive controller menu, `copy <n>` copies a port selection to the other variants.

### Built-in devices

Internal USB devices, such as Bluetooth modules and laptop webcams, require the appropriate internal port type.

The port editor and generator remind users to configure these devices accordingly. Verify the selected port type rather than assuming every built-in device has been identified correctly.

## The 15-port limit

Under the conventional macOS port-mapping model, a controller supports a maximum of 15 mapped ports.

If the scan detects more ports than macOS can use, the script requires you to select the ports you actually need. SSDT generation is blocked until the selection satisfies the limit.

Count each HS, SS, and other relevant port entry separately. USB 2 and USB 3 lanes associated with the same physical connector can consume separate port entries.

## Output files

- **`SSDT-XHUB`** — Generated ACPI source and, when compiled, its AML counterpart.
- **`ports.json`** — Saved port configuration when using the scan workflow with `-o`.
- **`Results/decompiled/`** — Decompiled ACPI tables retained for analysis and troubleshooting.
- **`tools/`** — Location used for a downloaded `iasl` binary, if required.

Exact filenames and locations may depend on the selected workflow and script options.

## Troubleshooting

- **`iasl` is missing or cannot be executed:** Install a compatible binary, specify its location with `--iasl`, or allow the script to download one.
- **A controller or port is missing:** Check whether its definition is in another SSDT rather than the DSDT. Ensure that all relevant ACPI tables were supplied.
- **A port address is unknown or inferred:** Review the corresponding ACPI definition and verify the physical port mapping.
- **A port marked `dyn` behaves unexpectedly:** Static analysis cannot determine every firmware-dependent port state. Test the port on the actual system.
- **A controller exists in multiple conditional branches:** Ensure that the intended variants receive the correct port selection.
- **The generated SSDT does not work as expected:** Inspect the generated ASL, compilation output, OpenCore ACPI configuration, and boot logs. Test changes incrementally.

Always back up your working EFI and retain the original ACPI tables before making changes.

## Limitations

- The script analyzes ACPI declarations; it does not physically test USB ports or guarantee that every declared port is electrically present or enabled at runtime.
- Runtime firmware conditions may affect port availability in ways that static analysis cannot resolve.
- Inferred and unknown `_ADR` values require additional verification.
- The conventional 15-port limit still applies.
- A successfully compiled SSDT is not necessarily a correct SSDT for the target hardware.
- Other kexts may still be required for USB controller compatibility or unrelated hardware functionality.

## References

- [OC-Little Translated — XHUB Method](https://github.com/5T33Z0/OC-Little-Translated/tree/main/Content/03_USB_Fixes/ACPI_Mapping_USB_Ports/XHUB_Method)
- [samuelnotfound — ACPI USB Mapping Guide](https://github.com/samuelnotfound/miscellaneous-hackintosh-guides/tree/main/SSDT_USB_Mapping)
- [OC-Little Translated — USB Fixes](https://github.com/5T33Z0/OC-Little-Translated/tree/main/Content/03_USB_Fixes)
- [CorpNewt/SSDTTime](https://github.com/corpnewt/SSDTTime) — Reference for `iasl` handling and terminal path cleanup conventions.

## License

See the repository's license file for the applicable terms.
