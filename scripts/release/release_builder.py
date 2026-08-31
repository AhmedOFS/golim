"""Shared application-runtime assembly for Linux and macOS."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys

from scripts.build.runtime_builder import ROOT, remove_path

LAUNCHER_SOURCE = ROOT / "launcher" / "openterm_launcher.c"


@dataclass(frozen=True)
class ReleaseConfig:
    name: str
    build_script: Path
    build_dir: Path
    release_dir: Path
    keep_interpreter: bool = False


def run(command: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(str(part) for part in command))
    subprocess.run(command, cwd=cwd, check=True, env=env)


def build_cpython(config: ReleaseConfig) -> None:
    run([sys.executable, str(config.build_script)])


def copy_entry(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise RuntimeError(f"runtime layout collision: {destination}")
    if source.is_symlink():
        destination.symlink_to(os.readlink(source))
    elif source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copy2(source, destination)


def copy_runtime(config: ReleaseConfig) -> None:
    if not config.build_dir.is_dir():
        raise RuntimeError(f"missing base runtime: {config.build_dir}")
    config.release_dir.parent.mkdir(parents=True, exist_ok=True)
    remove_path(config.release_dir)
    shutil.copytree(config.build_dir, config.release_dir, symlinks=True)


def flatten_site_packages(runtime: Path) -> None:
    site_packages = next(runtime.glob("lib/python*/site-packages"), None)
    if site_packages is None:
        raise RuntimeError("could not find site-packages in the standalone runtime")
    for entry in sorted(site_packages.iterdir()):
        copy_entry(entry, (runtime / "lib" if entry.name == "openterm" else runtime) / entry.name)
    remove_path(site_packages)


def _compiler() -> str:
    compiler = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if compiler is None:
        raise RuntimeError("a C compiler is required to build the openterm launcher")
    return compiler


def compile_linux_launcher(runtime: Path, *, output_name: str = "openterm", entry_module: str = "openterm.__main__", entry_function: str = "main") -> None:
    include = next((runtime / "include").glob("python*"), None)
    python_lib = next((runtime / "lib").glob("libpython*.so"), None)
    if include is None or python_lib is None:
        raise RuntimeError("runtime does not contain Linux embedding headers and libpython")
    run([
        _compiler(), "-O2", "-DNDEBUG",
        f'-DOPENTERM_ENTRY_MODULE="{entry_module}"',
        f'-DOPENTERM_ENTRY_FUNCTION="{entry_function}"',
        "-fPIE", "-I", str(include), str(LAUNCHER_SOURCE),
        "-L", str(runtime / "lib"),
        "-Wl,-rpath,$ORIGIN/lib", "-Wl,--disable-new-dtags",
        f"-l{python_lib.name.removeprefix('lib').removesuffix('.so')}",
        "-ldl", "-lm", "-lpthread", "-o", str(runtime / output_name),
    ])
    (runtime / output_name).chmod(0o755)


def compile_macos_launcher(runtime: Path, *, output_name: str = "openterm", entry_module: str = "openterm") -> None:
    run([_compiler(), "-O2", "-DNDEBUG", f'-DOPENTERM_ENTRY_MODULE="{entry_module}"', str(LAUNCHER_SOURCE), "-o", str(runtime / output_name)])
    (runtime / output_name).chmod(0o755)


def remove_development_files(runtime: Path, platform_name: str = "linux") -> None:
    stdlib = next(runtime.glob("lib/python*/"), None)
    if stdlib is None:
        raise RuntimeError("could not find the standalone standard library")
    paths = ["include", "lib/pkgconfig", "share", str(stdlib / "ensurepip"), str(stdlib / "idlelib"), str(stdlib / "tkinter"), str(stdlib / "turtledemo"), str(stdlib / "venv"), str(stdlib / "unittest"), str(stdlib / "__phello__"), "pip", "jupyter.py", "ipython_pygments_lexers.py", "ipykernel_launcher.py", "pylab.py", "decorator.py", "nest_asyncio.py"]
    if platform_name == "linux":
        paths += ["lib/itcl4.2.4", "lib/tcl8", "lib/tcl8.6", "lib/tk8.6", "lib/libtcl8.6.so", "lib/libtk8.6.so", "lib/libpython3.so", "lib/thread2.8.9"]
    else:
        paths += ["lib/libpython3.a", "lib/libpython3.dylib"]
    for relative in paths:
        path = Path(relative)
        remove_path(path if path.is_absolute() else runtime / path)
    if platform_name == "macos":
        for pattern in ("lib/libpython*.a", "lib/libpython*.dylib"):
            for path in runtime.glob(pattern):
                remove_path(path)
    for pattern in ("pip-*.dist-info", "jupyter_client", "jupyter_client-*.dist-info", "jupyter_core", "jupyter_core-*.dist-info", "ipython_pygments_lexers-*.dist-info", "decorator-*.dist-info"):
        for path in runtime.glob(pattern):
            remove_path(path)
    for cache in list(runtime.rglob("__pycache__")):
        remove_path(cache)
    for path in runtime.glob("lib/python*/config-*"):
        remove_path(path)
    for path in runtime.glob("lib/python*/lib-dynload/_*_test*.so"):
        remove_path(path)


def remove_interpreter_tools(runtime: Path, keep_interpreter: bool = False) -> None:
    if not keep_interpreter:
        remove_path(runtime / "bin")
        return
    for path in (runtime / "bin").iterdir():
        if path.name.startswith("python3.") and not path.name.endswith("-config"):
            continue
        remove_path(path)


def compile_application(runtime: Path) -> None:
    application = runtime / "lib" / "openterm"
    if not application.is_dir():
        raise RuntimeError("flattened runtime does not contain openterm")
    python = next(
        (p for p in runtime.glob("bin/python3.*") if not p.name.endswith("-config")),
        None,
    )
    if python is None:
        raise RuntimeError("could not find standalone Python for bytecode compilation")
    run([str(python), "-m", "compileall", "-q", "-f", "-b", str(application)])
    for source in application.rglob("*.py"):
        source.unlink()
    for cache in application.rglob("__pycache__"):
        remove_path(cache)


def verify_runtime(runtime: Path) -> None:
    launcher = runtime / "openterm"
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise RuntimeError("runtime launcher was not created")
    run([str(launcher), "--version"])


def remove_generated_caches(runtime: Path) -> None:
    for cache in list(runtime.rglob("__pycache__")):
        remove_path(cache)


def assemble_runtime(config: ReleaseConfig, platform_name: str) -> None:
    copy_runtime(config)
    flatten_site_packages(config.release_dir)
    if platform_name == "linux":
        compile_linux_launcher(config.release_dir)
        compile_linux_launcher(config.release_dir, output_name="openterm-tools", entry_module="openterm.toolset.server", entry_function="run_server")
    else:
        compile_macos_launcher(config.release_dir)
        compile_macos_launcher(config.release_dir, output_name="openterm-tools", entry_module="openterm.toolset.server")
    compile_application(config.release_dir)
    remove_development_files(config.release_dir, platform_name)
    remove_interpreter_tools(config.release_dir, config.keep_interpreter)
    verify_runtime(config.release_dir)
    remove_generated_caches(config.release_dir)
    print(f"Application runtime ready at {config.release_dir}")


def cli(config: ReleaseConfig, platform_name: str, argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=config.name)
    parser.add_argument("--skip-build", action="store_true", help=f"assemble from the existing {config.build_dir} without rebuilding it")
    args = parser.parse_args(argv)
    if not args.skip_build:
        build_cpython(config)
    assemble_runtime(config, platform_name)
    return 0


def run_cli(config: ReleaseConfig, platform_name: str, argv: list[str] | None = None) -> None:
    try:
        raise SystemExit(cli(config, platform_name, argv))
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"{config.name}: {error}", file=sys.stderr)
        raise SystemExit(1)
