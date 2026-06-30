import argparse
import multiprocessing
import subprocess
import time
import traceback
from datetime import timedelta

from tqdm import tqdm

from processor import Processor
from utils import clone_repo, load_parsers

repos = {"rust": ["tauri-apps/tauri", "alacritty/alacritty", "BurntSushi/ripgrep", "openai/codex",
                  "meilisearch/meilisearch"],
         "julia": ["TuringLang/Turing.jl", "probcomp/Gen.jl", "JuliaData/DataFrames.jl",
                   "JuliaPlots/UnicodePlots.jl", "JuliaSymbolics/Symbolics.jl", "JuliaNLSolvers/Optim.jl",
                   "JuliaStats/Distributions.jl", "JuliaDiff/ForwardDiff.jl"],
         "haskell": ["haskell-servant/servant", "polysemy-research/polysemy",
                     "hspec/hspec", "haskell/cabal", "snoyberg/conduit", "yesodweb/wai",
                     "haskell-distributed/distributed-process"],
         "elixir": ["phoenixframework/phoenix", "elixir-ecto/ecto", "rrrene/credo", "absinthe-graphql/absinthe",
                    "ueberauth/guardian"],
         "perl": ["sqitchers/sqitch", "mojolicious/mojo", "PerlDancer/Dancer2", "RexOps/Rex", "plack/Plack"]}


def process_language(lang: str, all_processors: dict[str, object], index: int, queue: multiprocessing.Queue) -> None:
    repo_list = repos.get(lang, [])
    if not repo_list:
        queue.put((index, "write", f"No repositories found for language: {lang}"))
        return

    parser_class = all_processors.get(lang)
    if parser_class is None:
        queue.put((index, "write", f"No processor found for language: {lang}"))
        return

    subprocess.run(["mkdir", "-p", f"cache/{lang}"])
    for repo in repo_list:
        try:
            start_time = time.perf_counter()
            parser_class(f"repos/{lang}/{repo.split('/')[-1]}", index, queue).parse_repository()
            parse_time = time.perf_counter()
            processor = Processor(parser_class(f"repos/{lang}/{repo.split('/')[-1]}", index, queue))
            result: list = processor.process_repository()
            end_time = time.perf_counter()
            result.append(str(timedelta(seconds=parse_time - start_time)))
            result.append(str(timedelta(seconds=end_time - parse_time)))
            queue.put((index, "result", (repo, result)))
        except Exception as e:
            queue.put((index, "write", f"Error processing language {lang}: {e}\n{traceback.format_exc()}"))
            traceback.print_exc()
    else:
        queue.put((index, "finish", None))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--languages", type=str, nargs='+', default=["all"],
                        help="List of programming languages to process, or 'all' for all supported languages.")
    args = parser.parse_args()

    all_processors = load_parsers()
    if "all" in args.languages:
        selected_languages = list(repos.keys())
    else:
        selected_languages = [lang.lower() for lang in args.languages]

    # 先克隆所有仓库
    for lang in selected_languages:
        for repo in repos.get(lang, []):
            if not clone_repo(repo, f"repos/{lang}"):
                print(f"Failed to clone repository: {repo}")
                exit(1)

    manage = multiprocessing.Manager()
    queue = manage.Queue()
    processes = []
    for i, lang in enumerate(selected_languages):
        p = multiprocessing.Process(target=process_language, args=(lang, all_processors, i, queue))
        p.start()
        processes.append(p)
    if not processes:
        print("No languages to process.")
        return
    bars = []
    for i, lang in enumerate(selected_languages):
        bar = tqdm(total=0, position=i, desc=f"Waiting for {lang} subprocess", unit="item")
        bars.append(bar)
    finished_count = 0
    result = {}
    while finished_count < len(selected_languages):
        idx, cmd, val = queue.get()
        if cmd == "desc":
            desc, unit = val
            bars[idx].set_description(desc)
            if unit:
                bars[idx].unit = unit
        elif cmd == "reset":
            bars[idx].reset(total=val)
        elif cmd == "update":
            bars[idx].update(val)
        elif cmd == "finish":
            bars[idx].set_description(f"Finished {selected_languages[idx]}")
            bars[idx].n = bars[idx].total = len(repos.get(selected_languages[idx], []))
            bars[idx].unit = "repo"
            bars[idx].refresh()
            finished_count += 1
            for repo in repos.get(selected_languages[idx], []):
                if repo in result:
                    tqdm.write(
                        f"[{selected_languages[idx]:^10}] {repo:<40}: " + " ".join(f"{x:<5}" for x in result[repo]))
                else:
                    tqdm.write(f"[{selected_languages[idx]:^10}] {repo:<40}: No result")
            tqdm.write("-" * 80)
        elif cmd == "write":
            tqdm.write(val)
        elif cmd == "result":
            repo, res = val
            result[repo] = res
    for p in processes:
        p.join()
    for bar in bars:
        bar.close()


if __name__ == "__main__":
    main()
