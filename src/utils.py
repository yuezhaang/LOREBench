import argparse
import importlib
import json
import logging
import os
import pkgutil
import shutil
import subprocess
import time
from enum import Enum

import language_parsers
from ir import BaseNode

BENCHMARK_PATH = "benchmark"
EVAL_RESULT_PATH = "eval_results"
EXEC_RESULT_PATH = "exec_results"


class ContextMode(Enum):
    NO_CONTEXT = "no_context"
    WITH_CONTEXT = "with_context"
    BM25 = "bm25"


repo_commit_ids = {"tauri-apps/tauri": "123d63a0c1b31a150cf459a93aa0ac0b79c2b5b8",
                   "alacritty/alacritty": "18372031d1f1bda853b2027b7ef1c75b45942829",
                   "BurntSushi/ripgrep": "cd1f981beafaeb9b61537e47e91314cea125400b",
                   "openai/codex": "1fc72c647fd52e3e73d4309c3b568d4d5fe012b5",
                   "meilisearch/meilisearch": "def769490cc8c5635972e87a5103a3dfe4ee4433",
                   "TuringLang/Turing.jl": "f86999b2dcf049df4b57f2ff1dbf20457b25dd49",
                   "probcomp/Gen.jl": "027dbc6e71cec3e281b335292c57ad776a8bf9db",
                   "JuliaData/DataFrames.jl": "aeea2c261c1872f99e09a61c4ab3485db584e474",
                   "JuliaPlots/UnicodePlots.jl": "d788f866fc73b6d1cf5c36d7a5a73115e9c5da4b",
                   "JuliaSymbolics/Symbolics.jl": "a45c7fe7c410052453c56bbfbb7aac7b249d0631",
                   "JuliaNLSolvers/Optim.jl": "540c97b28a6e37fae10ec689c83c3dbe9dce2f73",
                   "JuliaStats/Distributions.jl": "e5d4fefebe2f004cd8feec3edbf925a67b92df17",
                   "JuliaDiff/ForwardDiff.jl": "1295777846b6e3efb6e83044a948234c3ba8dab3",
                   "haskell-servant/servant": "a1dc0d89c2f22baff4dbbda91c5d138ee33a28b8",
                   "polysemy-research/polysemy": "cf2efec15ae57a46a81694d361da55da8d7b1d73",
                   "hspec/hspec": "a9b4d0631f019cf8281354e005536fd126e5ac07",
                   "haskell/cabal": "e6a93b2b356a418919fbd01653994c8537b1ee7b",
                   "snoyberg/conduit": "6b98f070fea09a3bf0a5d0897a2e27e3aa91c8fe",
                   "yesodweb/wai": "6b778ad9fbb1f29f7d408d84bfe7165f99917e3e",
                   "haskell-distributed/distributed-process": "0eb3db307046f272c6b6d822450f3be844d2cb01",
                   "phoenixframework/phoenix": "964fae5eda2ba42c7f1a04bd0112ef51ddd5f486",
                   "elixir-ecto/ecto": "7fd33e29cf8d7b3f95d3b1ad656d488d9548577f",
                   "rrrene/credo": "959a00b809212ab05ef52e53e23f447d5de25dde",
                   "absinthe-graphql/absinthe": "a035261bd0172d84c1d8ac72f74d72e64aaa4769",
                   "ueberauth/guardian": "09be7ddf773119104284a8812db615b4ff6c7dae",
                   "sqitchers/sqitch": "42b7192f8ffabfbda79db329f1240a9390458edd",
                   "mojolicious/mojo": "4dae171754f76c2f3074bf652b0ff62ffc2e0ea4",
                   "PerlDancer/Dancer2": "401cccb0c035c53e122c1749d17340adc20c4b28",
                   "RexOps/Rex": "755075dd627a3f9cd93266ce8c56852708339fc9",
                   "plack/Plack": "b3984f1c59de36903bb924c9da1273f3e11d4d2b"}


def clone_repo(repo_path: str, clone_path: str) -> bool:
    """Clone repo from GitHub to the specified path"""
    repo_name = repo_path.split('/')[-1]
    if not os.path.exists(f"{clone_path}/{repo_name}"):
        git_url = f"git@github.com:{repo_path}.git"
        result = subprocess.run(["git", "clone", git_url, f"{clone_path}/{repo_name}", "--depth", "1"])
        if result.returncode != 0:
            return False
    return True


def remove_git(repo_path: str) -> bool:
    """Remove all .git directories under the specified repository path."""
    if not os.path.isdir(repo_path):
        return False

    success = True
    for root, dirs, _ in os.walk(repo_path, topdown=True):
        if ".git" not in dirs:
            continue

        git_dir = os.path.join(root, ".git")
        dirs.remove(".git")
        try:
            shutil.rmtree(git_dir)
        except OSError as e:
            print(f"Warning: Failed to remove {git_dir}: {e}")
            success = False

    return success


def clean_repo(path: str, with_ignore: bool = True) -> None:
    """Reset the cloned repository to a clean state"""
    if with_ignore:
        subprocess.run(["git", "clean", "-fd"], cwd=path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.run(["git", "clean", "-fdx"], cwd=path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "restore", "."], cwd=path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # check if git status is clean
    time.sleep(0.5)
    result = subprocess.run(["git", "status", "--porcelain"], cwd=path, capture_output=True)
    if result.stdout.strip():
        print(f"Warning: Repository at {path} is not clean after reset.")


def get_files(repo_path: str, suffix: list[str], exclude_dirs: list[str]) -> list[str]:
    """Get all files with the specified suffix from the repository path"""
    files = []
    for root, dirs, filenames in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        for filename in filenames:
            if any(filename.endswith(suf) for suf in suffix):
                if os.path.isfile(os.path.join(root, filename)):
                    files.append(os.path.join(root, filename))
    return files


def load_parsers() -> dict[str, object]:
    """Dynamically load all processor classes from the language_parsers directory"""
    parsers = {}
    package = language_parsers
    for _, module_name, _ in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{package.__name__}.{module_name}")
        class_name = ''.join(word.capitalize() for word in module_name.split('_'))
        if hasattr(module, class_name):
            parsers[module_name.split('_')[0]] = getattr(module, class_name)
    return parsers


class SafeFileHandler(logging.FileHandler):
    """File handler that ignores write/open errors (e.g., disk full) to avoid crashes."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self.stream is None:
                self.stream = self._open()
        except Exception:
            return
        logging.StreamHandler.emit(self, record)

    def handleError(self, record: logging.LogRecord) -> None:
        # Suppress logging failures to keep the program running.
        return


def get_logger(name: str, log_file: str, stdout: bool = False) -> logging.Logger:
    """Create a logger that logs to both console and a file"""
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    fh = SafeFileHandler(log_file, delay=True)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    if stdout:
        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG)
        ch.setFormatter(formatter)
        logger.addHandler(ch)

    return logger


def dump_ir(ir: BaseNode, file_path: str) -> None:
    """Dump the IR to a file in JSON format"""
    file_path = f"{file_path}.ir"
    while True:
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(ir.to_dict(), f, indent=4, ensure_ascii=False)
            break
        except Exception as e:
            print(f"Error dumping IR to {file_path}: {e}")
            time.sleep(300)  # 等待5分钟后重试


def load_ir(file_path: str) -> BaseNode:
    """Load the IR from a JSON file"""
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    ir = BaseNode(None, [], "", file_path)
    return ir.from_dict(data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--languages", type=str, nargs='+', default=["all"],
                        help="List of programming languages to evaluate, or 'all' for all supported languages.")
    parser.add_argument("--without_ignore", action="store_true",
                        help="If set, do not ignore untracked files when cleaning the repository.")
    parser.add_argument("--remove_git", action="store_true",
                        help="If set, remove .git directories from cloned repositories.")
    args = parser.parse_args()

    if "all" in args.languages:
        selected_languages = list(os.listdir("repos"))
    else:
        selected_languages = [lang for lang in args.languages]

    print(f"Cleaning cloned repositories for {', '.join(selected_languages)}")
    for lang in selected_languages:
        for repo in os.listdir(f"repos/{lang}"):
            clean_repo(f"repos/{lang}/{repo}", with_ignore=not args.without_ignore)
            if args.remove_git:
                remove_git(f"repos/{lang}/{repo}")
print("Done.")
