"""Everything regarding execution of the built app."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urljoin

import psutil

from reflex import constants
from reflex.config import environment, get_config
from reflex.constants.base import LogLevel
from reflex.utils import console, path_ops
from reflex.utils.decorator import once
from reflex.utils.prerequisites import get_web_dir

# For uvicorn windows bug fix (#2335)
frontend_process = None


def detect_package_change(json_file_path: Path) -> str:
    """Calculates the SHA-256 hash of a JSON file and returns it as a hexadecimal string.

    Args:
        json_file_path: The path to the JSON file to be hashed.

    Returns:
        str: The SHA-256 hash of the JSON file as a hexadecimal string.

    Example:
        >>> detect_package_change("package.json")
        'a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4y5z6a7b8c9d0e1f2'
    """
    with json_file_path.open("r") as file:
        json_data = json.load(file)

    # Calculate the hash
    json_string = json.dumps(json_data, sort_keys=True)
    hash_object = hashlib.sha256(json_string.encode())
    return hash_object.hexdigest()


def kill(proc_pid: int):
    """Kills a process and all its child processes.

    Args:
        proc_pid (int): The process ID of the process to be killed.

    Example:
        >>> kill(1234)
    """
    process = psutil.Process(proc_pid)
    for proc in process.children(recursive=True):
        proc.kill()
    process.kill()


def notify_backend():
    """Output a string notifying where the backend is running."""
    console.print(
        f"Backend running at: [bold green]http://0.0.0.0:{get_config().backend_port}[/bold green]"
    )


# run_process_and_launch_url is assumed to be used
# only to launch the frontend
# If this is not the case, might have to change the logic
def run_process_and_launch_url(
    run_command: list[str | None], backend_present: bool = True
):
    """Run the process and launch the URL.

    Args:
        run_command: The command to run.
        backend_present: Whether the backend is present.
    """
    from reflex.utils import processes

    json_file_path = get_web_dir() / constants.PackageJson.PATH
    last_hash = detect_package_change(json_file_path)
    process = None
    first_run = True

    while True:
        if process is None:
            kwargs = {}
            if constants.IS_WINDOWS and backend_present:
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # pyright: ignore [reportAttributeAccessIssue]
            process = processes.new_process(
                run_command,
                cwd=get_web_dir(),
                shell=constants.IS_WINDOWS,
                **kwargs,
            )
            global frontend_process
            frontend_process = process
        if process.stdout:
            for line in processes.stream_logs("Starting frontend", process):
                match = re.search(constants.Next.FRONTEND_LISTENING_REGEX, line)
                if match:
                    if first_run:
                        url = match.group(1)
                        if get_config().frontend_path != "":
                            url = urljoin(url, get_config().frontend_path)

                        console.print(
                            f"App running at: [bold green]{url}[/bold green]{' (Frontend-only mode)' if not backend_present else ''}"
                        )
                        if backend_present:
                            notify_backend()
                        first_run = False
                    else:
                        console.print("New packages detected: Updating app...")
                else:
                    if any(
                        x in line for x in ("bin executable does not exist on disk",)
                    ):
                        console.error(
                            "Try setting `REFLEX_USE_NPM=1` and re-running `reflex init` and `reflex run` to use npm instead of bun:\n"
                            "`REFLEX_USE_NPM=1 reflex init`\n"
                            "`REFLEX_USE_NPM=1 reflex run`"
                        )
                    new_hash = detect_package_change(json_file_path)
                    if new_hash != last_hash:
                        last_hash = new_hash
                        kill(process.pid)
                        process = None
                        break  # for line in process.stdout
        if process is not None:
            break  # while True


def run_frontend(root: Path, port: str, backend_present: bool = True):
    """Run the frontend.

    Args:
        root: The root path of the project.
        port: The port to run the frontend on.
        backend_present: Whether the backend is present.
    """
    from reflex.utils import prerequisites

    # validate dependencies before run
    prerequisites.validate_frontend_dependencies(init=False)

    # Run the frontend in development mode.
    console.rule("[bold green]App Running")
    os.environ["PORT"] = str(get_config().frontend_port if port is None else port)
    run_process_and_launch_url(
        [
            *prerequisites.get_js_package_executor(raise_on_none=True)[0],
            "run",
            "dev",
        ],
        backend_present,
    )


def run_frontend_prod(root: Path, port: str, backend_present: bool = True):
    """Run the frontend.

    Args:
        root: The root path of the project (to keep same API as run_frontend).
        port: The port to run the frontend on.
        backend_present: Whether the backend is present.
    """
    from reflex.utils import prerequisites

    # Set the port.
    os.environ["PORT"] = str(get_config().frontend_port if port is None else port)
    # validate dependencies before run
    prerequisites.validate_frontend_dependencies(init=False)
    # Run the frontend in production mode.
    console.rule("[bold green]App Running")
    run_process_and_launch_url(
        [*prerequisites.get_js_package_executor(raise_on_none=True)[0], "run", "prod"],
        backend_present,
    )


@once
def _warn_user_about_uvicorn():
    console.warn(
        "Using Uvicorn for backend as it is installed. This behavior will change in 0.8.0 to use Granian by default."
    )


def should_use_granian():
    """Whether to use Granian for backend.

    Returns:
        True if Granian should be used.
    """
    if environment.REFLEX_USE_GRANIAN.is_set():
        return environment.REFLEX_USE_GRANIAN.get()
    if (
        importlib.util.find_spec("uvicorn") is None
        or importlib.util.find_spec("gunicorn") is None
    ):
        return True
    _warn_user_about_uvicorn()
    return False


def get_app_module():
    """Get the app module for the backend.

    Returns:
        The app module for the backend.
    """
    return get_config().module


def get_app_instance():
    """Get the app module for the backend.

    Returns:
        The app module for the backend.
    """
    return f"{get_app_module()}:{constants.CompileVars.APP}"


def get_app_file() -> Path:
    """Get the app file for the backend.

    Returns:
        The app file for the backend.

    Raises:
        ImportError: If the app module is not found.
    """
    current_working_dir = str(Path.cwd())
    if current_working_dir not in sys.path:
        # Add the current working directory to sys.path
        sys.path.insert(0, current_working_dir)
    module_spec = importlib.util.find_spec(get_app_module())
    if module_spec is None:
        raise ImportError(
            f"Module {get_app_module()} not found. Make sure the module is installed."
        )
    file_name = module_spec.origin
    if file_name is None:
        raise ImportError(
            f"Module {get_app_module()} not found. Make sure the module is installed."
        )
    return Path(file_name).resolve()


def get_app_instance_from_file() -> str:
    """Get the app module for the backend.

    Returns:
        The app module for the backend.
    """
    return f"{get_app_file()}:{constants.CompileVars.APP}"


def run_backend(
    host: str,
    port: int,
    loglevel: constants.LogLevel = constants.LogLevel.ERROR,
    frontend_present: bool = False,
):
    """Run the backend.

    Args:
        host: The app host
        port: The app port
        loglevel: The log level.
        frontend_present: Whether the frontend is present.
    """
    web_dir = get_web_dir()
    # Create a .nocompile file to skip compile for backend.
    if web_dir.exists():
        (web_dir / constants.NOCOMPILE_FILE).touch()

    if not frontend_present:
        notify_backend()

    # Run the backend in development mode.
    if should_use_granian():
        run_granian_backend(host, port, loglevel)
    else:
        run_uvicorn_backend(host, port, loglevel)


def get_reload_paths() -> Sequence[Path]:
    """Get the reload paths for the backend.

    Returns:
        The reload paths for the backend.
    """
    config = get_config()
    reload_paths = [Path(config.app_name).parent]
    if config.app_module is not None and config.app_module.__file__:
        module_path = Path(config.app_module.__file__).resolve().parent

        while module_path.parent.name and any(
            sibling_file.name == "__init__.py"
            for sibling_file in module_path.parent.iterdir()
        ):
            # go up a level to find dir without `__init__.py`
            module_path = module_path.parent

        reload_paths = [module_path]

    include_dirs = tuple(
        map(Path.absolute, environment.REFLEX_HOT_RELOAD_INCLUDE_PATHS.get())
    )
    exclude_dirs = tuple(
        map(Path.absolute, environment.REFLEX_HOT_RELOAD_EXCLUDE_PATHS.get())
    )

    def is_excluded_by_default(path: Path) -> bool:
        if path.is_dir():
            if path.name.startswith("."):
                # exclude hidden directories
                return True
            if path.name.startswith("__"):
                # ignore things like __pycache__
                return True
        return path.name in (".gitignore", "uploaded_files")

    reload_paths = (
        tuple(
            path.absolute()
            for dir in reload_paths
            for path in dir.iterdir()
            if not is_excluded_by_default(path)
        )
        + include_dirs
    )

    if exclude_dirs:
        reload_paths = tuple(
            path
            for path in reload_paths
            if all(not path.samefile(exclude) for exclude in exclude_dirs)
        )

    console.debug(f"Reload paths: {list(map(str, reload_paths))}")

    return reload_paths


def run_uvicorn_backend(host: str, port: int, loglevel: LogLevel):
    """Run the backend in development mode using Uvicorn.

    Args:
        host: The app host
        port: The app port
        loglevel: The log level.
    """
    import uvicorn

    uvicorn.run(
        app=f"{get_app_instance()}",
        factory=True,
        host=host,
        port=port,
        log_level=loglevel.value,
        reload=True,
        reload_dirs=list(map(str, get_reload_paths())),
        reload_delay=0.1,
    )


def run_granian_backend(host: str, port: int, loglevel: LogLevel):
    """Run the backend in development mode using Granian.

    Args:
        host: The app host
        port: The app port
        loglevel: The log level.
    """
    console.debug("Using Granian for backend")

    if environment.REFLEX_STRICT_HOT_RELOAD.get():
        import multiprocessing

        multiprocessing.set_start_method("spawn", force=True)

    from granian.constants import Interfaces
    from granian.log import LogLevels
    from granian.server import MPServer as Granian

    Granian(
        target=get_app_instance_from_file(),
        factory=True,
        address=host,
        port=port,
        interface=Interfaces.ASGI,
        log_level=LogLevels(loglevel.value),
        reload=True,
        reload_paths=get_reload_paths(),
        reload_ignore_worker_failure=True,
        reload_tick=100,
        workers_kill_timeout=2,
    ).serve()


def _deprecate_asgi_config(
    config_name: str,
    reason: str = "",
):
    console.deprecate(
        f"config.{config_name}",
        reason=reason,
        deprecation_version="0.7.9",
        removal_version="0.8.0",
    )


@once
def _get_backend_workers():
    from reflex.utils import processes

    config = get_config()

    gunicorn_workers = config.gunicorn_workers or 0

    if config.gunicorn_workers is not None:
        _deprecate_asgi_config(
            "gunicorn_workers",
            "If you're using Granian, use GRANIAN_WORKERS instead.",
        )

    return gunicorn_workers if gunicorn_workers else processes.get_num_workers()


@once
def _get_backend_timeout():
    config = get_config()

    timeout = config.timeout or 120

    if config.timeout is not None:
        _deprecate_asgi_config(
            "timeout",
            "If you're using Granian, use GRANIAN_WORKERS_LIFETIME instead.",
        )

    return timeout


@once
def _get_backend_max_requests():
    config = get_config()

    gunicorn_max_requests = config.gunicorn_max_requests or 120

    if config.gunicorn_max_requests is not None:
        _deprecate_asgi_config("gunicorn_max_requests")

    return gunicorn_max_requests


@once
def _get_backend_max_requests_jitter():
    config = get_config()

    gunicorn_max_requests_jitter = config.gunicorn_max_requests_jitter or 25

    if config.gunicorn_max_requests_jitter is not None:
        _deprecate_asgi_config("gunicorn_max_requests_jitter")

    return gunicorn_max_requests_jitter


def run_backend_prod(
    host: str,
    port: int,
    loglevel: constants.LogLevel = constants.LogLevel.ERROR,
    frontend_present: bool = False,
):
    """Run the backend.

    Args:
        host: The app host
        port: The app port
        loglevel: The log level.
        frontend_present: Whether the frontend is present.
    """
    if not frontend_present:
        notify_backend()

    if should_use_granian():
        run_granian_backend_prod(host, port, loglevel)
    else:
        run_uvicorn_backend_prod(host, port, loglevel)


def run_uvicorn_backend_prod(host: str, port: int, loglevel: LogLevel):
    """Run the backend in production mode using Uvicorn.

    Args:
        host: The app host
        port: The app port
        loglevel: The log level.
    """
    from reflex.utils import processes

    config = get_config()

    app_module = get_app_instance()

    command = (
        [
            "uvicorn",
            *(
                (
                    "--limit-max-requests",
                    str(max_requessts),
                )
                if (
                    (max_requessts := _get_backend_max_requests()) is not None
                    and max_requessts > 0
                )
                else ()
            ),
            *(
                ("--timeout-keep-alive", str(timeout))
                if (timeout := _get_backend_timeout()) is not None
                else ()
            ),
            *("--host", host),
            *("--port", str(port)),
            *("--workers", str(_get_backend_workers())),
            "--factory",
            app_module,
        ]
        if constants.IS_WINDOWS
        else [
            "gunicorn",
            *("--worker-class", config.gunicorn_worker_class),
            *(
                (
                    "--max-requests",
                    str(max_requessts),
                )
                if (
                    (max_requessts := _get_backend_max_requests()) is not None
                    and max_requessts > 0
                )
                else ()
            ),
            *(
                (
                    "--max-requests-jitter",
                    str(max_requessts_jitter),
                )
                if (
                    (max_requessts_jitter := _get_backend_max_requests_jitter())
                    is not None
                    and max_requessts_jitter > 0
                )
                else ()
            ),
            "--preload",
            *(
                ("--timeout", str(timeout))
                if (timeout := _get_backend_timeout()) is not None
                else ()
            ),
            *("--bind", f"{host}:{port}"),
            *("--threads", str(_get_backend_workers())),
            f"{app_module}()",
        ]
    )

    command += [
        *("--log-level", loglevel.value),
    ]

    processes.new_process(
        command,
        run=True,
        show_logs=True,
        env={
            environment.REFLEX_SKIP_COMPILE.name: "true"
        },  # skip compile for prod backend
    )


def run_granian_backend_prod(host: str, port: int, loglevel: LogLevel):
    """Run the backend in production mode using Granian.

    Args:
        host: The app host
        port: The app port
        loglevel: The log level.
    """
    from reflex.utils import processes

    try:
        from granian.constants import Interfaces

        command = [
            "granian",
            *("--workers", str(_get_backend_workers())),
            *("--log-level", "critical"),
            *("--host", host),
            *("--port", str(port)),
            *("--interface", str(Interfaces.ASGI)),
            *("--factory", get_app_instance_from_file()),
        ]
        processes.new_process(
            command,
            run=True,
            show_logs=True,
            env={
                environment.REFLEX_SKIP_COMPILE.name: "true"
            },  # skip compile for prod backend
        )
    except ImportError:
        console.error(
            'InstallError: REFLEX_USE_GRANIAN is set but `granian` is not installed. (run `pip install "granian[reload]>=1.6.0"`)'
        )


def output_system_info():
    """Show system information if the loglevel is in DEBUG."""
    if console._LOG_LEVEL > constants.LogLevel.DEBUG:
        return

    from reflex.utils import prerequisites

    config = get_config()
    try:
        config_file = sys.modules[config.__module__].__file__
    except Exception:
        config_file = None

    console.rule("System Info")
    console.debug(f"Config file: {config_file!r}")
    console.debug(f"Config: {config}")

    dependencies = [
        f"[Reflex {constants.Reflex.VERSION} with Python {platform.python_version()} (PATH: {sys.executable})]",
        f"[Node {prerequisites.get_node_version()} (Minimum: {constants.Node.MIN_VERSION}) (PATH:{path_ops.get_node_path()})]",
    ]

    system = platform.system()

    dependencies.append(
        f"[Bun {prerequisites.get_bun_version()} (Minimum: {constants.Bun.MIN_VERSION}) (PATH: {path_ops.get_bun_path()})]"
    )

    if system == "Linux":
        os_version = platform.freedesktop_os_release().get("PRETTY_NAME", "Unknown")
    else:
        os_version = platform.version()

    dependencies.append(f"[OS {platform.system()} {os_version}]")

    for dep in dependencies:
        console.debug(f"{dep}")

    console.debug(
        f"Using package installer at: {prerequisites.get_nodejs_compatible_package_managers(raise_on_none=False)}"
    )
    console.debug(
        f"Using package executer at: {prerequisites.get_js_package_executor(raise_on_none=False)}"
    )
    if system != "Windows":
        console.debug(f"Unzip path: {path_ops.which('unzip')}")


def is_testing_env() -> bool:
    """Whether the app is running in a testing environment.

    Returns:
        True if the app is running in under pytest.
    """
    return constants.PYTEST_CURRENT_TEST in os.environ


def is_in_app_harness() -> bool:
    """Whether the app is running in the app harness.

    Returns:
        True if the app is running in the app harness.
    """
    return constants.APP_HARNESS_FLAG in os.environ


def is_prod_mode() -> bool:
    """Check if the app is running in production mode.

    Returns:
        True if the app is running in production mode or False if running in dev mode.
    """
    current_mode = environment.REFLEX_ENV_MODE.get()
    return current_mode == constants.Env.PROD


def get_compile_context() -> constants.CompileContext:
    """Check if the app is compiled for deploy.

    Returns:
        Whether the app is being compiled for deploy.
    """
    return environment.REFLEX_COMPILE_CONTEXT.get()
