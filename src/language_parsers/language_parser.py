import multiprocessing
import os
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from tree_sitter import Node, Parser, Tree

from ir import BaseNode, FunctionNode, TestFunctionNode
from sandbox import Sandbox
from utils import dump_ir, get_files

TARGET_FUNC_NAME = "task_func"

@dataclass
class BuildResult:
    passed: bool
    message: str

    def __post_init__(self):
        max_length = 65536  # 64KB
        if len(self.message) > max_length:
            self.message = self.message[:max_length] + "\n...[truncated]"


class TestStatus(Enum):
    PASSED = "passed"
    FAILED = "failed"
    BUILD_ERROR = "build_error"
    RUNTIME_ERROR = "runtime_error"
    TIMEOUT = "timeout"
    NOTFOUND = "not_found"
    UNKNOWN = "unknown"


@dataclass
class TestResult:
    status: TestStatus
    test_name: str
    test_file_path: str
    message: str

    def __post_init__(self):
        max_length = 65536  # 64KB
        if len(self.message) > max_length:
            self.message = self.message[:max_length] + "\n...[truncated]"

    def to_dict(self) -> dict:
        return {
            "passed": self.status == TestStatus.PASSED,
            "status": self.status.value,
            "test_name": self.test_name,
            "test_file_path": self.test_file_path,
            "message": self.message,
        }


class LanguageParser:
    def __init__(self, repo_path: str, index: int, queue: multiprocessing.Queue):
        self.repo_path = repo_path
        self.index: int = index
        self.queue: multiprocessing.Queue = queue
        self.inject_template: str = "__FUNC_CALL_NAME__={func_name}"
        self.module_body_template: str = "__CODE__"
        self.func_mask: bytes = b"[MASK]"

        # Need to be set in subclasses
        self.parser: Parser = Parser()
        self.language: str = ""
        self.file_extension: list[str] = []
        self.project_file: str = ""
        self.build_dir: list[str] = []
        self.test_dir: list[str] = []

    @staticmethod
    def _parse_worker(parser: Parser, code: bytes, q: multiprocessing.Queue):
        """Run parse in a child process and return a picklable representation."""
        try:
            tree = parser.parse(code)
            # Tree/Node are not picklable; just report success.
            _ = tree.root_node  # ensure tree constructed
            q.put((True, "", ""))
        except BaseException as e:
            q.put((False, "", repr(e)))

    def _parse_with_timeout(self, code: bytes) -> Tree:
        """Parse code with a fixed 2-minute hard timeout (process-based).

        Important: Tree/Node cannot be transferred across processes. This wrapper only uses the
        child process to enforce the timeout, then re-parses in the current process if successful.
        This keeps changes minimal while preventing hangs.
        """
        timeout = 120

        q: multiprocessing.Queue = multiprocessing.Queue(maxsize=1)
        p = multiprocessing.Process(target=LanguageParser._parse_worker, args=(self.parser, code, q))
        p.daemon = True
        p.start()
        p.join(timeout)
        if p.is_alive():
            p.terminate()
            p.join(5)
            raise TimeoutError(f"tree-sitter parse timed out after {timeout}s")

        ok, _payload, err = (False, "", "")
        try:
            ok, _payload, err = q.get_nowait()
        except Exception:
            # Worker exited without putting result (unexpected)
            raise RuntimeError("tree-sitter parse worker exited without result")
        if not ok:
            raise RuntimeError(f"tree-sitter parse failed: {err}")

        # Parse again in current process to obtain the actual Tree for downstream traversal.
        return self.parser.parse(code)

    def parse_repository(self):
        self.get_dependencies()
        files = get_files(self.repo_path, self.file_extension, self.build_dir)
        self.queue.put((self.index, "desc", (f"Parsing {self.repo_path}", "file")))
        self.queue.put((self.index, "reset", len(files)))
        for file_path in files:
            self._parse_file(file_path)
            self.queue.put((self.index, "update", 1))

    def get_dependencies(self) -> None:
        raise NotImplementedError

    def _parse_file(self, file_path: str):
        with open(file_path, "rb") as f:
            code = f.read()
        tree = self._parse_with_timeout(code)
        root_node = tree.root_node
        root_ir = BaseNode(None, [], "", file_path)
        project_path = self.get_project_path(file_path)
        self._traverse(root_node, root_ir, file_path, project_path,
                       is_in_test=any(test_dir in file_path.split(os.sep) for test_dir in self.test_dir))
        dump_ir(root_ir, file_path)

    def _traverse(self, node: Node, parent: BaseNode, file_path: str, project_path: str, namespace: str = "",
                  parent_func_node: FunctionNode = None, is_in_test: bool = False) -> None:
        # parse function, test_function, call, import, identifier, context
        raise NotImplementedError

    def _find_func_node(self, node: Node, target_name: str, hash: str, file_path: str, match_hash: bool,
                        func_node: Node) -> Optional[Node]:
        raise NotImplementedError

    @staticmethod
    def _get_signature(node: Node) -> str:
        raise NotImplementedError

    def _mask(self, func: FunctionNode, code: bytes) -> bytes:
        raise NotImplementedError

    def mask_func(self, func: FunctionNode) -> FunctionNode:
        """mask the function body in func.code and func.code_with_context"""
        masked_func = FunctionNode(func.parent, func.children.copy(), func.code, func.file_path, func.name,
                                   func.namespace, func.position, func.signature, func.docstring,
                                   func.code_with_context, func.used_idents.copy())
        masked_func.code = self._mask(func, func.code.encode("utf-8")).decode("utf-8")
        masked_func.code_with_context = self._mask(func, func.code_with_context.encode("utf-8")).decode("utf-8")
        return masked_func

    def replace_func(self, file_path: str, func_name: str, signature: str, hash: str, func_code: str,
                     new_code: str, renamed: bool) -> None:
        """替换文件中的指定函数为新代码，用于执行测试"""
        with open(file_path, "rb") as f:
            code = f.read()
        tree = self._parse_with_timeout(code)
        root_node = tree.root_node
        target_func_node = self._find_func_node(root_node, func_name, hash, file_path, True,
                                                self._parse_with_timeout(func_code.encode("utf-8")).root_node)
        assert target_func_node is not None, f"[Impossible] Function {func_name} not found in {file_path}"

        # 从新代码中提取函数
        try:
            new_tree = self._parse_with_timeout(new_code.encode("utf-8"))
        except Exception as e:
            raise e
        new_root_node = new_tree.root_node
        if renamed:
            new_func_node = self._find_func_node(new_root_node, TARGET_FUNC_NAME, hash, file_path, False,
                                                 self._parse_with_timeout(func_code.encode("utf-8")).root_node)
        else:
            new_func_node = self._find_func_node(new_root_node, func_name, hash, file_path, False,
                                                 self._parse_with_timeout(func_code.encode("utf-8")).root_node)
        if new_func_node is not None:  # 识别到完整函数
            # 尝试将新函数的签名替换为原函数的签名，容忍模型生成的签名有细微差别
            if self.language == "haskell":  # 签名与定位分离的语言，无需保留签名
                new_code = new_func_node.text.decode("utf-8")
            else:
                try:
                    new_code = signature + new_func_node.text.decode("utf-8")[
                        len(self._get_signature(new_func_node)):]  # 替换函数签名
                except Exception:
                    # 出现异常时直接使用新函数代码
                    new_code = new_func_node.text.decode("utf-8")
        else:  # 未找到对应函数，直接使用新代码整体作为函数体
            if self.language == "haskell":
                new_code = new_code
            else:
                temp_func_node = FunctionNode(None, [], func_code, file_path, func_name, "", 0, signature, "", "",
                                              set())
                mask_code = self._mask(temp_func_node, func_code.encode("utf-8")).decode("utf-8")
                new_code = mask_code.replace(self.func_mask.decode("utf-8"), new_code)

        new_code_bytes = code[:target_func_node.start_byte] + new_code.encode("utf-8") + code[
            target_func_node.end_byte:]
        with open(file_path, "wb") as f:
            f.write(new_code_bytes)

    def get_project_path(self, file_path: str) -> str | None:
        # 需要根据文件名获取项目路径|考虑到有的大项目中套着多个小项目
        # 寻找最近的以project_file结尾的文件所在目录
        dir_path = os.path.dirname(file_path)
        while dir_path and dir_path != "/":
            for item in os.listdir(dir_path):
                if item.endswith(self.project_file):
                    return dir_path
            dir_path = os.path.dirname(dir_path)
        # 如果找不到，返回根目录
        file_path_parts = file_path.split(os.sep)
        return os.sep.join(file_path_parts[:3])

    def get_all_project_paths(self) -> list[str]:
        """获取仓库中所有项目的路径"""
        project_files = get_files(self.repo_path, [self.project_file], self.build_dir)
        return list(set(os.path.dirname(p) for p in project_files))

    def inject_function(self, func_node: FunctionNode) -> None:
        """注入代码，用于判断测试时函数是否被运行"""
        raise NotImplementedError

    def merge_test(self, test_func_dict: dict[TestFunctionNode, set[FunctionNode]]) -> dict[
        tuple[str, str], dict[TestFunctionNode, set[FunctionNode]]]:  # key: (test_func_name, test_file_path)
        """合并测试用例，用于构建benchmark时合并那些实际执行时是同一个测试用例的测试函数。"""
        # 默认实现是以(test_func.name, test_func.file_path)作为key进行合并
        res: dict[tuple[str, str], dict[TestFunctionNode, set[FunctionNode]]] = {}
        for test_func, func_set in test_func_dict.items():
            key = (test_func.name, test_func.file_path)
            if key not in res:
                res[key] = {}
            res[key][test_func] = func_set
        return res

    def build_project(self, sandbox: Sandbox, project_path: str, timeout: int | None = 300) -> BuildResult:
        try:
            return self._build_project_inner(sandbox, project_path, timeout)
        except subprocess.CalledProcessError as e:
            output = ""
            if e.stdout:
                output += e.stdout.decode("utf-8", errors="ignore")
            if e.stderr:
                output += e.stderr.decode("utf-8", errors="ignore")
            return BuildResult(passed=False, message=output)
        except subprocess.TimeoutExpired:
            return BuildResult(passed=False, message="Build timed out")
        except Exception as e:
            return BuildResult(passed=False, message=str(e))

    def run_test(self, sandbox: Sandbox, project_path: str, test_file_path: str, test_name: str,
                 timeout: int | None = 300) -> TestResult:
        try:
            return self._run_test_inner(sandbox, project_path, test_file_path, test_name, timeout)
        except subprocess.CalledProcessError as e:
            output = ""
            if e.stdout:
                output += e.stdout.decode("utf-8", errors="ignore")
            if e.stderr:
                output += e.stderr.decode("utf-8", errors="ignore")
            return TestResult(TestStatus.RUNTIME_ERROR, test_name, test_file_path, message=output)
        except subprocess.TimeoutExpired:
            return TestResult(TestStatus.TIMEOUT, test_name, test_file_path, message="Test timed out")
        except Exception as e:
            return TestResult(TestStatus.UNKNOWN, test_name, test_file_path, message=str(e))

    def _build_project_inner(self, sandbox: Sandbox, project_path: str, timeout: int | None) -> BuildResult:
        raise NotImplementedError

    def _run_test_inner(self, sandbox: Sandbox, project_path: str, test_file_path: str, test_name: str,
                        timeout: int | None) -> TestResult:
        raise NotImplementedError
