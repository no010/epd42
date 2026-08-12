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

TARGET_CONFIG = {
    "nRF51822_xxAB": {
        "artifact_name": "epd42-bw",
        "startup": REPO_ROOT / "components" / "toolchain" / "gcc" / "gcc_startup_nrf51.s",
        "system": REPO_ROOT / "components" / "toolchain" / "system_nrf51.c",
        "linker": REPO_ROOT / "components" / "softdevice" / "s130" / "toolchain" / "armgcc" / "armgcc_s130_nrf51822_xxab.ld",
    },
    "nRF51802_xxAA": {
        "artifact_name": "epd42-bwr",
        "startup": REPO_ROOT / "components" / "toolchain" / "gcc" / "gcc_startup_nrf51.s",
        "system": REPO_ROOT / "components" / "toolchain" / "system_nrf51.c",
        "linker": REPO_ROOT / "components" / "softdevice" / "s130" / "toolchain" / "armgcc" / "armgcc_s130_nrf51822_xxaa.ld",
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
