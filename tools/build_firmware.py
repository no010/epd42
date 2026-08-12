#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
KEIL_PROJECT = REPO_ROOT / "Keil" / "EPD.uvprojx"
KEIL_DIR = KEIL_PROJECT.parent
BUILD_DIR = REPO_ROOT / "build"
HEX_RECORD_DATA_LENGTH = 32

TARGET_CONFIG = {
    "nRF51822_xxAB": {
        "artifact_name": "epd42-bw",
        "startup": REPO_ROOT / "components" / "toolchain" / "gcc" / "gcc_startup_nrf51.s",
        "system": REPO_ROOT / "components" / "toolchain" / "system_nrf51.c",
        "linker": REPO_ROOT / "components" / "softdevice" / "s130" / "toolchain" / "armgcc" / "armgcc_s130_nrf51822_xxab.ld",
        "softdevice": REPO_ROOT / "components" / "softdevice" / "s130" / "hex" / "s130_nrf51_2.0.1_softdevice.hex",
    },
    "nRF51802_xxAA": {
        "artifact_name": "epd42-bwr",
        "startup": REPO_ROOT / "components" / "toolchain" / "gcc" / "gcc_startup_nrf51.s",
        "system": REPO_ROOT / "components" / "toolchain" / "system_nrf51.c",
        "linker": REPO_ROOT / "components" / "softdevice" / "s130" / "toolchain" / "armgcc" / "armgcc_s130_nrf51822_xxaa.ld",
        "softdevice": REPO_ROOT / "components" / "softdevice" / "s130" / "hex" / "s130_nrf51_2.0.1_softdevice.hex",
    },
}

SOURCE_REPLACEMENTS = {
    "components/drivers_ext/segger_rtt/RTT_Syscalls_KEIL.c": REPO_ROOT / "components" / "drivers_ext" / "segger_rtt" / "RTT_Syscalls_GCC.c",
}

COMMON_FLAGS = [
    "-mcpu=cortex-m0",
    "-mthumb",
    "-mabi=aapcs",
    "-ffunction-sections",
    "-fdata-sections",
    "-fno-common",
    "-fshort-enums",
    "-fno-strict-aliasing",
    "-g3",
    "-Os",
]

EXTRA_INCLUDE_PATHS = [
    REPO_ROOT / "components" / "cmsis",
    REPO_ROOT / "components" / "device",
]


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)


def normalize_project_path(raw_path: str) -> Path:
    return (KEIL_DIR / raw_path.replace("\\", "/")).resolve()


def parse_hex_file(path: Path) -> tuple[dict[int, int], int | None]:
    """Return absolute-addressed Intel HEX data and the optional start linear address."""
    data: dict[int, int] = {}
    start_linear_address: int | None = None
    upper_address = 0

    for line_number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise RuntimeError(f"Invalid Intel HEX record in {path}:{line_number}")

        record = bytes.fromhex(line[1:])
        if len(record) < 5:
            raise RuntimeError(f"Truncated Intel HEX record in {path}:{line_number}")

        byte_count = record[0]
        if len(record) != byte_count + 5:
            raise RuntimeError(f"Length mismatch in Intel HEX record in {path}:{line_number}")

        if (sum(record) & 0xFF) != 0:
            raise RuntimeError(f"Checksum mismatch in Intel HEX record in {path}:{line_number}")

        address = (record[1] << 8) | record[2]
        record_type = record[3]
        payload = record[4:-1]

        if record_type == 0x00:
            absolute_address = upper_address + address
            for offset, value in enumerate(payload):
                current_address = absolute_address + offset
                existing = data.get(current_address)
                if existing is not None and existing != value:
                    raise RuntimeError(
                        f"Conflicting Intel HEX data at 0x{current_address:08x} while reading {path}"
                    )
                data[current_address] = value
        elif record_type == 0x01:
            break
        elif record_type == 0x02:
            upper_address = int.from_bytes(payload, "big") << 4
        elif record_type == 0x04:
            upper_address = int.from_bytes(payload, "big") << 16
        elif record_type == 0x05:
            start_linear_address = int.from_bytes(payload, "big")

    return data, start_linear_address


def make_hex_record(record_type: int, address: int, payload: bytes) -> str:
    """Encode a single Intel HEX record."""
    byte_count = len(payload)
    record = bytearray([byte_count, (address >> 8) & 0xFF, address & 0xFF, record_type])
    record.extend(payload)
    checksum = (-sum(record)) & 0xFF
    return ":" + record.hex().upper() + f"{checksum:02X}"


def write_hex_file(path: Path, data: dict[int, int], start_linear_address: int | None = None) -> None:
    """Write absolute-addressed data to an Intel HEX file."""
    lines: list[str] = []
    current_upper_address: int | None = None

    sorted_addresses = sorted(data)
    chunk_start = 0
    while chunk_start < len(sorted_addresses):
        start_address = sorted_addresses[chunk_start]
        chunk = [start_address]
        chunk_start += 1
        chunk_upper_address = start_address >> 16

        while chunk_start < len(sorted_addresses):
            next_address = sorted_addresses[chunk_start]
            if (
                next_address != chunk[-1] + 1
                or len(chunk) >= HEX_RECORD_DATA_LENGTH
                or (next_address >> 16) != chunk_upper_address
            ):
                break
            chunk.append(next_address)
            chunk_start += 1

        upper_address = start_address >> 16
        if current_upper_address != upper_address:
            current_upper_address = upper_address
            lines.append(make_hex_record(0x04, 0x0000, upper_address.to_bytes(2, "big")))

        payload = bytes(data[address] for address in chunk)
        lines.append(make_hex_record(0x00, start_address & 0xFFFF, payload))

    if start_linear_address is not None:
        lines.append(make_hex_record(0x05, 0x0000, start_linear_address.to_bytes(4, "big")))

    lines.append(":00000001FF")
    path.write_text("\n".join(lines) + "\n")


def merge_hex_files(output_path: Path, *input_paths: Path, prefer_last_start_linear_address: bool = True) -> None:
    """Merge Intel HEX inputs and optionally prefer the last start linear address."""
    merged_data: dict[int, int] = {}
    start_linear_address: int | None = None

    for input_path in input_paths:
        data, file_start_linear_address = parse_hex_file(input_path)
        for address, value in data.items():
            existing = merged_data.get(address)
            if existing is not None and existing != value:
                raise RuntimeError(f"Conflicting Intel HEX data at 0x{address:08x} while merging {input_path}")
            merged_data[address] = value

        if (
            file_start_linear_address is not None
            and start_linear_address is not None
            and file_start_linear_address != start_linear_address
            and not prefer_last_start_linear_address
        ):
            raise RuntimeError(
                "Conflicting start linear addresses while merging "
                f"{input_path}: 0x{start_linear_address:08x} vs 0x{file_start_linear_address:08x}"
            )

        if file_start_linear_address is not None and (
            prefer_last_start_linear_address or start_linear_address is None
        ):
            start_linear_address = file_start_linear_address

    write_hex_file(output_path, merged_data, start_linear_address)


def read_target(name: str) -> tuple[list[Path], list[str], list[Path]]:
    root = ET.parse(KEIL_PROJECT).getroot()
    for target in root.findall(".//Target"):
        if target.findtext("./TargetName") != name:
            continue

        c_controls = target.find("./TargetOption/TargetArmAds/Cads/VariousControls")
        a_controls = target.find("./TargetOption/TargetArmAds/Aads/VariousControls")
        if c_controls is None or a_controls is None:
            raise RuntimeError(f"Missing compiler settings for target {name}")

        include_paths: list[Path] = []
        seen_include_paths: set[Path] = set()
        for controls in (c_controls, a_controls):
            raw_include_paths = controls.findtext("IncludePath", default="")
            for item in raw_include_paths.split(";"):
                item = item.strip()
                if not item:
                    continue
                path = normalize_project_path(item)
                if path not in seen_include_paths:
                    seen_include_paths.add(path)
                    include_paths.append(path)

        raw_defines = c_controls.findtext("Define", default="").split()
        defines = [define.strip() for define in raw_defines if define.strip()]

        sources: list[Path] = []
        for file_node in target.findall(".//Files/File"):
            file_path = file_node.findtext("FilePath")
            include_in_build = file_node.findtext("FileOption/CommonProperty/IncludeInBuild", default="1")
            if include_in_build == "0" or not file_path:
                continue

            source_path = normalize_project_path(file_path)
            if source_path.suffix.lower() not in {".c", ".s", ".S".lower()}:
                continue

            replacement = SOURCE_REPLACEMENTS.get(str(source_path.relative_to(REPO_ROOT)).replace("\\", "/"))
            if replacement is not None:
                source_path = replacement

            sources.append(source_path)

        for path in EXTRA_INCLUDE_PATHS:
            if path not in seen_include_paths:
                seen_include_paths.add(path)
                include_paths.append(path)

        return sources, defines, include_paths

    raise RuntimeError(f"Unknown target: {name}")


def compile_target(name: str, tool_prefix: str, clean: bool) -> None:
    target_config = TARGET_CONFIG[name]
    target_dir = BUILD_DIR / name

    if clean and target_dir.exists():
        shutil.rmtree(target_dir)

    obj_dir = target_dir / "obj"
    obj_dir.mkdir(parents=True, exist_ok=True)

    sources, defines, include_paths = read_target(name)
    sources.extend([target_config["system"], target_config["startup"]])

    include_args = [f"-I{path}" for path in include_paths]
    define_args = [f"-D{define}" for define in defines]
    common_compile_flags = COMMON_FLAGS + include_args + define_args

    gcc = f"{tool_prefix}gcc"
    objcopy = f"{tool_prefix}objcopy"
    size = f"{tool_prefix}size"

    object_files: list[Path] = []
    for source in sources:
        if source.suffix.lower() == ".c":
            output = obj_dir / source.relative_to(REPO_ROOT)
            output = output.with_suffix(".o")
            output.parent.mkdir(parents=True, exist_ok=True)
            run([gcc, *common_compile_flags, "-std=gnu99", "-MMD", "-MP", "-c", str(source), "-o", str(output)])
            object_files.append(output)
        elif source.suffix.lower() == ".s":
            output = obj_dir / source.relative_to(REPO_ROOT)
            output = output.with_suffix(".o")
            output.parent.mkdir(parents=True, exist_ok=True)
            run([gcc, *COMMON_FLAGS, *include_args, *define_args, "-x", "assembler-with-cpp", "-c", str(source), "-o", str(output)])
            object_files.append(output)
        else:
            raise RuntimeError(f"Unsupported source type: {source}")

    artifact_base = target_dir / target_config["artifact_name"]
    elf_path = artifact_base.with_suffix(".elf")
    hex_path = artifact_base.with_suffix(".hex")
    merged_hex_path = artifact_base.with_name(f"{artifact_base.name}-merged").with_suffix(".hex")
    bin_path = artifact_base.with_suffix(".bin")
    map_path = artifact_base.with_suffix(".map")
    linker_script_path = target_dir / target_config["linker"].name

    linker_script_text = target_config["linker"].read_text()
    linker_script_text = linker_script_text.replace(
        'INCLUDE "nrf5x_common.ld"',
        f'INCLUDE "{(REPO_ROOT / "components" / "toolchain" / "gcc" / "nrf5x_common.ld").resolve()}"',
    )
    linker_script_path.write_text(linker_script_text)

    linker_flags = [
        "-mcpu=cortex-m0",
        "-mthumb",
        "-mabi=aapcs",
        f"-T{linker_script_path}",
        f"-Wl,-Map={map_path}",
        "-Wl,--gc-sections",
        "-Wl,--print-memory-usage",
        "--specs=nano.specs",
        "--specs=nosys.specs",
    ]

    run([gcc, *linker_flags, *map(str, object_files), "-o", str(elf_path)])
    run([size, str(elf_path)])
    run([objcopy, "-O", "ihex", str(elf_path), str(hex_path)])
    run([objcopy, "-O", "binary", str(elf_path), str(bin_path)])
    merge_hex_files(merged_hex_path, target_config["softdevice"], hex_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build EPD42 firmware with arm-none-eabi-gcc.")
    parser.add_argument(
        "--target",
        action="append",
        choices=sorted(TARGET_CONFIG),
        help="Keil target name to build. Repeat to build multiple targets. Defaults to all supported firmware targets.",
    )
    parser.add_argument(
        "--tool-prefix",
        default="arm-none-eabi-",
        help="Toolchain prefix, default: arm-none-eabi-",
    )
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="Keep previous target build output before compiling.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    targets = args.target or list(TARGET_CONFIG)

    for tool in ("gcc", "objcopy", "size"):
        if shutil.which(f"{args.tool_prefix}{tool}") is None:
            print(f"Missing tool: {args.tool_prefix}{tool}", file=sys.stderr)
            return 1

    for target in targets:
        compile_target(target, args.tool_prefix, clean=not args.no_clean)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
