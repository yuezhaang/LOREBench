import multiprocessing
import os
import shutil
import subprocess
import time


class Sandbox:
    def __init__(self, index: int, queue: multiprocessing.Queue, language: str, postfix: str = ""):
        self.index = index
        self.queue = queue
        self.image = "lorebench"
        self.name = f"lorebench_{language}_{postfix}"
        self.language = language
        self.username = os.getenv("USER")
        self.cache_dict = {"rust": [f"/home/{self.username}/.cargo", f"/home/{self.username}/.rustup"],
                           "julia": [f"/home/{self.username}/.julia"],
                           "ruby": [f"/home/{self.username}/.bundle"],
                           "haskell": [f"/home/{self.username}/.cabal", f"/home/{self.username}/.stack",
                                       f"/home/{self.username}/.ghcup/cache"],
                           "elixir": [f"/home/{self.username}/.mix", f"/home/{self.username}/.hex"],
                           "perl": [f"/home/{self.username}/perl5", f"/home/{self.username}/.cpanm"]}
        self.workdir = "/"
        self.sandbox_exec = shutil.which("podman") or shutil.which("docker")
        self.is_running = False
        self.env = []
        if os.getenv("HTTP_PROXY"):
            self.env.append(f"HTTP_PROXY={os.getenv('HTTP_PROXY')}")
        if os.getenv("HTTPS_PROXY"):
            self.env.append(f"HTTPS_PROXY={os.getenv('HTTPS_PROXY')}")
        if os.getenv("NO_PROXY"):
            self.env.append(f"NO_PROXY={os.getenv('NO_PROXY')}")

    def start(self):
        if not self.is_running:
            volumes = [f"{os.path.abspath('./repos')}/{self.language}:/repos/{self.language}"]
            if self.cache_dict.get(self.language) is not None:
                for cache_path in self.cache_dict[self.language]:
                    os.makedirs(f"./cache/{self.language}/{'/'.join(cache_path.split('/')[3:])}", exist_ok=True)
                    volumes.append(
                        f"{os.path.abspath('./cache')}/{self.language}/{'/'.join(cache_path.split('/')[3:])}:{cache_path}")
            retry_times = 5
            while retry_times > 0:
                retry_times -= 1
                try:
                    result = subprocess.run(
                        [self.sandbox_exec, "run", "-d", "--rm", "-m", "16g", "--name",
                         self.name.format(language=self.language),
                         *[item for vol in volumes for item in ("--volume", vol)],
                         *sum([["-e", env] for env in self.env], []),
                         "--net", "host", self.image, "sleep", "infinity"],
                        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                    stderr = (result.stderr or "").strip()
                    if stderr:
                        self.queue.put((self.index, "write", f"Sandbox stderr: {stderr}"))
                    break
                except subprocess.CalledProcessError as e:
                    stderr = (e.stderr or "").strip()
                    message = f"Failed to start sandbox: {e}."
                    if stderr:
                        message += f" Stderr: {stderr}"
                    message += " Retrying..."
                    self.queue.put((self.index, "write", message))
                    time.sleep(60)
                    if retry_times == 0:
                        raise e
            self.is_running = True

            # init language
            retry_times = 5
            while retry_times > 0:
                retry_times -= 1
                try:
                    match self.language:
                        case "rust":
                            self.exec("", "rustup default nightly")
                        case "haskell":
                            self.exec("", "cabal update")
                        case "elixir":
                            self.exec("", "mix local.hex --force")
                        case "perl":  # dzil NextRelease需要配置用户信息
                            self.exec("",
                                      'mkdir -p ~/.dzil && printf "[%%User]\nname = Your Name\nemail = you@example.com\n" > ~/.dzil/config.ini')
                    break
                except subprocess.CalledProcessError as e:
                    self.queue.put(
                        (self.index, "write", f"Failed to initialize sandbox for {self.language}: {e}. Retrying..."))
                    time.sleep(60)
                    if retry_times == 0:
                        raise e

    def stop(self):
        if self.is_running:
            while True:
                try:
                    subprocess.run([self.sandbox_exec, "stop", self.name.format(language=self.language)], check=True,
                                   stdout=subprocess.DEVNULL)
                    break
                except subprocess.CalledProcessError as e:
                    self.queue.put((self.index, "write", f"Failed to stop sandbox: {e}. Retrying..."))
                    time.sleep(5)
            self.is_running = False

    def exec(self, workdir: str, command: str, timeout: int | None = None,
             user: str | None = None) -> subprocess.CompletedProcess:
        if self.is_running:
            run_cmd = [self.sandbox_exec, "exec"]
            if user:
                run_cmd += ["-u", user]
            run_cmd += [self.name.format(language=self.language), "bash", "-c",
                        "cd " + os.path.normpath(self.workdir + workdir) + " && " + command]
            return subprocess.run(run_cmd, capture_output=True, check=True, timeout=timeout)
        raise RuntimeError("Sandbox is not running")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop()
