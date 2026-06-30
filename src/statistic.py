import argparse
import csv
import json
import os

import numpy as np

from benchmark import repos
from utils import ContextMode, EXEC_RESULT_PATH


def estimator(n: int, c: int, k: int) -> float:
    """
    Calculates 1 - comb(n - c, k) / comb(n, k).
    """
    if n - c < k:
        return 1.0
    return 1.0 - np.prod(1.0 - k / np.arange(n - c + 1, n + 1))


def calculate_repo(lang: str, repo: str, model: str, k: int, verbose: bool = False) -> tuple[
    int, int, list[float], list[float]]:
    exec_file = f"{EXEC_RESULT_PATH}/{model}/pass@{k}/{lang}/{repo.rsplit('/', 1)[-1]}.jsonl"
    if not os.path.exists(exec_file):
        print(f"File not found: {exec_file}")
        return 0, 0, [], []
    with open(exec_file, "r", encoding="utf-8") as f:
        lines = f.readlines()
    failure = 0
    max_recursion_pattern = "maximum recursion depth exceeded in comparison"  # 生成代码AST深度过深（大量重复无意义嵌套）
    total = 0
    cur = None
    n = 0
    pass_c = 0
    pass_k = []
    build_c = 0
    build_k = []
    for i, line in enumerate(lines):
        data = json.loads(line)
        if cur is not None and cur == data["hash"]:
            n += 1
        else:
            if cur is not None:
                pass_k.append(estimator(n, pass_c, k))
                build_k.append(estimator(n, build_c, k))
            cur = data["hash"]
            total += 1
            n = 1
            pass_c = 0
            build_c = 0
        if data["tests_passed"]:
            pass_c += 1
        if data["compile_passed"]:
            build_c += 1
        if data["compile_message"] == max_recursion_pattern:
            failure += 1
    if cur is not None:
        pass_k.append(estimator(n, pass_c, k))
        build_k.append(estimator(n, build_c, k))
    pass_k_average = sum(pass_k) / len(pass_k) if pass_k else 0.0
    build_k_average = sum(build_k) / len(build_k) if build_k else 0.0
    if verbose:
        print(
            f"[{lang:^10}] {repo:<40}: {total:<5} {failure:<5} {failure / total:6.2%} {build_k_average:6.2%} {pass_k_average:6.2%}")
    return total, failure, build_k, pass_k


def calculate_lang(lang: str, model: str, k: int, verbose: bool = False) -> tuple[float, float]:
    all_failures = []
    all_compiled = []
    all_total = []
    all_pass_k = []
    all_build_k = []
    for repo in repos.get(lang, []):
        total, failure, build_k, pass_k = calculate_repo(lang, repo, model, k, verbose)
        all_total.append(total)
        all_failures.append(failure)
        all_compiled.extend(build_k)
        all_pass_k.extend(pass_k)
        all_build_k.extend(build_k)
    if verbose:
        print("-" * 80)
    Bk = sum(all_build_k) / len(all_build_k) if all_build_k else 0.0
    Pk = sum(all_pass_k) / len(all_pass_k) if all_pass_k else 0.0
    print(
        f"[{model:^30}] [{lang:^10}]: B@{k}: {Bk * 100:6.2f}% P@{k}: {Pk * 100:6.2f}%")
    return Bk, Pk


def write_csv(
        out_path: str,
        rows: list[dict[str, object]],
        *,
        encoding: str = "utf-8-sig",
) -> None:
    """Write rows to CSV with stable header ordering.

    Using utf-8-sig makes Excel on Windows recognize UTF-8 reliably.
    """
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    header = ["Model", "Rust", "Julia", "Haskell", "Elixir", "Perl", "Macro Average"]
    with open(out_path, "w", newline="", encoding=encoding) as f:
        writer = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model_name", type=str, nargs='+', default=["all"],
                        help="Name of the model to evaluate.")
    parser.add_argument("--languages", type=str, nargs='+', default=["all"],
                        help="List of programming languages to evaluate, or 'all' for all supported languages.")
    parser.add_argument("--k", type=int, help="Top-K code generations to consider for evaluation.")
    parser.add_argument("--mode", type=str, choices=ContextMode._value2member_map_.keys(),
                        default=ContextMode.WITH_CONTEXT.value, help="Whether to include context in the prompt.")
    parser.add_argument("--verbose", action="store_true", help="Whether to print detailed results for each repository.")
    args = parser.parse_args()

    mode = ContextMode(args.mode)
    if mode != ContextMode.WITH_CONTEXT:
        EXEC_RESULT_PATH = f"{mode.value}/{EXEC_RESULT_PATH}"

    if "all" in args.model_name:
        args.model_name = [dir_name for dir_name in os.listdir(EXEC_RESULT_PATH) if
                           os.path.isdir(os.path.join(EXEC_RESULT_PATH, dir_name, f"pass@{args.k}"))]
        args.model_name.sort()

    if "all" in args.languages:
        selected_languages = list(repos.keys())
    else:
        selected_languages = [lang.lower() for lang in args.languages]

    build_rows = []
    pass_rows = []
    for model in args.model_name:
        Build_k, Pass_k = [], []
        for lang in selected_languages:
            Bk, Pk = calculate_lang(lang, model, args.k, args.verbose)
            Build_k.append(Bk)
            Pass_k.append(Pk)
        print("-" * 80)
        print(
            f"[{model:^30}] {'[Average]':^10}: B@{args.k}: {sum(Build_k) / len(Build_k) * 100:6.2f}% P@{args.k}: {sum(Pass_k) / len(Pass_k) * 100:6.2f}%")
        print("=" * 80)
        build_rows.append({
            "Model": model,
            "Rust": f"{Build_k[0] * 100:.2f}%",
            "Julia": f"{Build_k[1] * 100:.2f}%",
            "Haskell": f"{Build_k[2] * 100:.2f}%",
            "Elixir": f"{Build_k[3] * 100:.2f}%",
            "Perl": f"{Build_k[4] * 100:.2f}%",
            "Macro Average": f"{sum(Build_k) / len(Build_k) * 100:.2f}%",
        })
        pass_rows.append({
            "Model": model,
            "Rust": f"{Pass_k[0] * 100:.2f}%",
            "Julia": f"{Pass_k[1] * 100:.2f}%",
            "Haskell": f"{Pass_k[2] * 100:.2f}%",
            "Elixir": f"{Pass_k[3] * 100:.2f}%",
            "Perl": f"{Pass_k[4] * 100:.2f}%",
            "Macro Average": f"{sum(Pass_k) / len(Pass_k) * 100:.2f}%",
        })

    # sort by name
    sort_order = ["CodeLlama-7b-Instruct-hf", "CodeLlama-13b-Instruct-hf", "CodeLlama-34b-Instruct-hf",
                  "deepseek-coder-6.7b-instruct", "DeepSeek-Coder-V2-Lite-Instruct", "deepseek-coder-33b-instruct",
                  "Qwen2.5-Coder-7B-Instruct", "Qwen2.5-Coder-14B-Instruct", "Qwen2.5-Coder-32B-Instruct"]
    build_rows.sort(key=lambda x: sort_order.index(x["Model"]) if x["Model"] in sort_order else len(sort_order))
    pass_rows.sort(key=lambda x: sort_order.index(x["Model"]) if x["Model"] in sort_order else len(sort_order))

    write_csv(f"{EXEC_RESULT_PATH}/build_{args.k}.csv", build_rows)
    write_csv(f"{EXEC_RESULT_PATH}/pass_{args.k}.csv", pass_rows)
