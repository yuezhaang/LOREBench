import argparse
import json
import logging
import multiprocessing
import os
import shutil
import time
import traceback
from dataclasses import dataclass
from datetime import timedelta

from tqdm import tqdm

from benchmark import repos
from language_parsers.language_parser import LanguageParser, TestResult, TestStatus
from sandbox import Sandbox
from statistic import calculate_repo
from utils import ContextMode, EVAL_RESULT_PATH, EXEC_RESULT_PATH, get_logger, load_parsers


@dataclass
class ExecResult:
    func_name: str
    hash: str
    file_path: str
    docstring: str
    contexts: str
    signature: str
    code_original: str
    code_generated: str
    response: str
    tests_passed: bool = False
    compile_passed: bool = False
    compile_message: str = ""
    runtime: list[TestResult] = None

    def to_dict(self) -> dict:
        return {
            "tests_passed": self.tests_passed,
            "compile_passed": self.compile_passed,
            "compile_message": self.compile_message,
            "func_name": self.func_name,
            "hash": self.hash,
            "file_path": self.file_path,
            "docstring": self.docstring,
            "contexts": self.contexts,
            "signature": self.signature,
            "code_original": self.code_original,
            "code_generated": self.code_generated,
            "response": self.response,
            "runtime": [r.to_dict() for r in self.runtime] if self.runtime else [],
        }


class Exec:
    @staticmethod
    def exec_func_test(sandbox: Sandbox, parser: LanguageParser, func: dict, renamed: bool, logger) -> tuple[
        ExecResult, bool]:
        # 备份原文件
        backup_path = func["file_path"] + ".bak"
        while True:
            try:
                shutil.copyfile(func["file_path"], backup_path)
                break
            except Exception as e:
                logger.error(f"Error backing up file {func['file_path']}: {e}. Retrying in 5 minutes...")
                time.sleep(300)

        exec_result = ExecResult(func["name"], func["hash"], func["file_path"], func["docstring"], func["contexts"],
                                 func["signature"], func["code"], func["code_generated"], func["response"])
        try:
            parser.replace_func(func["file_path"], func["name"], func["signature"], func["hash"], func["code"],
                                func["code_generated"], renamed)
        except Exception as e:
            logger.error(f"Error replacing function {func['name']} in {func['file_path']}: {e}")
            exec_result.compile_passed = False
            exec_result.compile_message = str(e)
            # 恢复原文件
            while True:
                try:
                    shutil.move(backup_path, func["file_path"])
                    break
                except Exception as e:
                    logger.error(f"Error restoring file {func['file_path']}: {e}. Retrying in 5 minutes...")
                    time.sleep(300)
            return exec_result, False

        exec_result.tests_passed = True
        for test in func.get("tests", []):
            project_path = parser.get_project_path(test["file_path"])
            build_result = parser.build_project(sandbox, project_path)
            exec_result.compile_passed = build_result.passed
            exec_result.compile_message = build_result.message
            if not build_result.passed:
                exec_result.tests_passed = False
                logger.error(f"Build failed for project at {project_path}:\n{build_result.message}")
                break

            test_result = parser.run_test(sandbox, project_path, test["file_path"], test["name"])
            if exec_result.runtime is None:
                exec_result.runtime = []
            exec_result.runtime.append(test_result)
            if test_result.status == TestStatus.BUILD_ERROR:
                exec_result.compile_passed = False
                exec_result.compile_message += f"\nTest {test['name']} build failed"
                logger.error(f"Test <{test['name']}> build failed:\n{test_result.message}")
            if test_result.status != TestStatus.PASSED:
                exec_result.tests_passed = False
                logger.error(f"Test <{test['name']}> failed:\n{test_result.message}")
                break
            else:
                logger.info(f"Test <{test['name']}> passed")

        # 恢复原文件
        while True:
            try:
                shutil.move(backup_path, func["file_path"])
                break
            except Exception as e:
                logger.error(f"Error restoring file {func['file_path']}: {e}. Retrying in 5 minutes...")
                time.sleep(300)
        return exec_result, True

    def exec_project(self, language: str, parser_class: object, file_path: str, k: int, mode: ContextMode,
                     renamed: bool, index: int, queue: multiprocessing.Queue) -> list:
        file_name = os.path.basename(file_path)
        exec_result_path = f"{EXEC_RESULT_PATH}/" + "/".join(file_path.split("/")[-4:])

        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        results = []
        failures = 0
        repo_name = file_name.rsplit('.', 1)[0]
        repo_path = f"repos/{language}/{repo_name}"
        parser = parser_class(repo_path, index, queue)

        model_name = exec_result_path.split("/")[-4]
        os.makedirs(f"logs/exec/{mode.value}/{model_name}/{parser.language.lower()}", exist_ok=True)
        log_file = f"logs/exec/{mode.value}/{model_name}/{parser.language.lower()}/{repo_path.rsplit('/', 1)[-1]}.log"
        if os.path.exists(log_file):
            os.remove(log_file)
        logger = get_logger(f"Exec-{parser.language}-{repo_path.rsplit('/', 1)[-1]}", log_file)

        with Sandbox(index, queue, parser.language.lower(), repo_name) as sandbox:
            queue.put((index, "desc", (f"Executing tests for {file_name}", "func")))
            queue.put((index, "reset", len(lines)))
            for line in lines:
                func = json.loads(line)
                exec_result, success = self.exec_func_test(sandbox, parser, func, renamed, logger)
                failures += success is False
                results.append(exec_result)
                queue.put((index, "update", 1))

        # 输出统计信息
        os.makedirs(os.path.dirname(exec_result_path), exist_ok=True)
        while True:
            try:
                with open(exec_result_path, "w", encoding="utf-8") as f:
                    for result in results:
                        f.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")
                break
            except Exception as e:
                logger.error(f"Error writing result for function {result.func_name} in {result.file_path}: {e}")
                time.sleep(300)  # 等待5分钟后重试
        total, failure, build_k, pass_k = calculate_repo(language, repo_name, model_name, k)
        if failure != failures:
            queue.put((index, "write",
                       f"Mismatch in failure count for {file_name}: calculated {failure}, counted {failures}"))
            logger.error(
                f"Mismatch in failure count for {file_name}: calculated {failure}, counted {failures}")
        build_k_average = sum(build_k) / len(build_k) if build_k else 0.0
        pass_k_average = sum(pass_k) / len(pass_k) if pass_k else 0.0
        logger.info(
            f"{file_name}: Total: {total}, Failures: {failures}, Build@{k}: {build_k_average:.2%} Pass@{k}: {pass_k_average:.2%}")
        logging.shutdown()
        return [total, failures, f"{build_k_average:.2%}", f"{pass_k_average:.2%}"]

    def exec_language(self, language: str, language_path: str, parser_class: object, model_name: str, pass_k: int,
                      mode: ContextMode, renamed: bool, index: int, queue: multiprocessing.Queue) -> None:
        for project in repos[language]:
            try:
                start_time = time.time()
                result = self.exec_project(language, parser_class,
                                           os.path.join(language_path, project.rsplit('/')[-1] + ".jsonl"), pass_k,
                                           mode, renamed, index, queue)
                end_time = time.time()
                result.append(str(timedelta(seconds=end_time - start_time)))
                queue.put((index, "result", (project, result)))
            except Exception as e:
                queue.put((index, "write", f"Error executing tests for {project}: {e}"))
                traceback.print_exc()
        queue.put((index, "finish", None))

    def exec(self, languages: list[str], model_name: str, pass_k: int, mode: ContextMode, renamed: bool) -> None:
        all_parsers = load_parsers()
        manage = multiprocessing.Manager()
        queue = manage.Queue()
        processes = []
        for index, language in enumerate(languages):
            language_path = os.path.join(f"{EVAL_RESULT_PATH}/{model_name.split('/')[-1]}/pass@{pass_k}/", language)
            if not os.path.isdir(language_path):
                continue
            parser_class = all_parsers.get(language)
            if parser_class is None:
                print(f"No parser found for language: {language}")
                exit(1)
            p = multiprocessing.Process(target=self.exec_language,
                                        args=(language, language_path, parser_class, model_name, pass_k, mode, renamed,
                                              index, queue))
            p.start()
            processes.append(p)
        if not processes:
            print("No processes to run.")
            return
        bars = []
        for i, lang in enumerate(languages):
            bar = tqdm(total=0, position=i, desc=f"Waiting for {lang} subprocess...", unit="item")
            bars.append(bar)
        finished_count = 0
        exec_start_time = time.time()
        result = {}
        while finished_count < len(languages):
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
                bars[idx].set_description(f"Finished {languages[idx]}")
                bars[idx].n = bars[idx].total = len(
                    os.listdir(
                        os.path.join(f"{EVAL_RESULT_PATH}/{model_name.split('/')[-1]}/pass@{pass_k}/", languages[idx])))
                bars[idx].unit = "repo"
                bars[idx].elapsed = time.time() - exec_start_time
                bars[idx].refresh()
                finished_count += 1
                for repo, res in result.get(languages[idx], []):
                    tqdm.write(f"[{languages[idx]:^10}] {repo:<40}: " + " ".join(f"{x:<6}" for x in res))
                tqdm.write("-" * 80)
            elif cmd == "write":
                tqdm.write(val)
            elif cmd == "result":
                repo, res = val
                result[languages[idx]] = result.get(languages[idx], []) + [(repo, res)]
        for p in processes:
            p.join()
        for bar in bars:
            bar.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model_name", type=str, required=True, help="Name of the model to evaluate.")
    parser.add_argument("--languages", type=str, nargs='+', default=["all"],
                        help="List of programming languages to evaluate, or 'all' for all supported languages.")
    parser.add_argument("--pass-k", type=int, default=1, help="Top-K code generations to consider for evaluation.")
    parser.add_argument("--mode", type=str, choices=ContextMode._value2member_map_.keys(),
                        default=ContextMode.WITH_CONTEXT.value, help="Whether to include context in the prompt.")
    parser.add_argument("--renamed", action="store_true", default=False, help="Whether to use renamed functions.")
    args = parser.parse_args()

    if args.mode != ContextMode.WITH_CONTEXT.value:
        EVAL_RESULT_PATH = os.path.join(args.mode, EVAL_RESULT_PATH)
        EXEC_RESULT_PATH = os.path.join(args.mode, EXEC_RESULT_PATH)

    if "all" in args.languages:
        selected_languages = list(repos.keys())
    else:
        selected_languages = [lang.lower() for lang in args.languages]

    executor = Exec()
    executor.exec(selected_languages, args.model_name, args.pass_k, ContextMode(args.mode), bool(args.renamed))
