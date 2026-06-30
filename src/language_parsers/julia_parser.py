import hashlib
import os
import subprocess
from typing import Optional

import tomli
import tomli_w
import tree_sitter_julia
from tree_sitter import Language, Node, Parser, QueryCursor

from ir import BaseNode, CallNode, ContextNode, FunctionNode, IdentifierNode, ImportContextNode, ImportExternalNode, \
    TestFunctionNode, TestHelperFunctionNode
from language_parsers.language_parser import BuildResult, LanguageParser, TestResult, TestStatus
from sandbox import Sandbox
from utils import get_files

# 针对某些项目，将runtests.jl中前若干行代码注入到每个测试文件开头，以确保测试文件能独立运行
PROJECT_TEST_PREFIX: dict[str, int] = {"repos/julia/Turing.jl": 31, "repos/julia/Gen.jl": 104,
                                       "repos/julia/UnicodePlots.jl": 136, "repos/julia/Optim.jl": 308,
                                       "repos/julia/Distributions.jl": 172}
# 注入代码中include的辅助文件不应被注入，否则会循环导入
PROJECT_TEST_EXCLUDE: dict[str, list[str]] = {
    "repos/julia/Turing.jl": ["repos/julia/Turing.jl/test/test_utils/SelectiveTests.jl",
                              "repos/julia/Turing.jl/test/test_utils/models.jl",
                              "repos/julia/Turing.jl/test/test_utils/numerical_tests.jl",
                              "repos/julia/Turing.jl/test/test_utils/sampler.jl"],
    "repos/julia/UnicodePlots.jl": ["repos/julia/UnicodePlots.jl/test/fixes.jl"],
    "repos/julia/Distributions.jl": ["repos/julia/Distributions.jl/test/testutils.jl"]}


class JuliaParser(LanguageParser):
    def __init__(self, repo_path, index, queue):
        super().__init__(repo_path, index, queue)
        self.parser = Parser(Language(tree_sitter_julia.language()))
        self.language = "Julia"
        self.file_extension = [".jl"]
        self.project_file = "Project.toml"
        self.build_dir = [""]
        self.test_dir = ["test"]

        self.modules: dict[str, str] = {}  # {模块名: 文件路径}
        self.files_defining_modules: dict[str, str] = {}  # {文件路径: 模块名}
        self.build_project_dir = set()

        assert os.path.exists(f"{self.repo_path}/Project.toml")
        # 将test目录下的Project.toml合并到主Project.toml中，不把test目录单独作为一个项目处理
        if os.path.exists(f"{self.repo_path}/test/Project.toml"):
            with open(f"{self.repo_path}/test/Project.toml", "rb") as f:
                test_toml_data = tomli.load(f)
                with open(f"{self.repo_path}/Project.toml", "rb") as f:
                    main_toml_data = tomli.load(f)
                # 合并deps、extras、weakdeps
                for key in ["deps", "extras", "weakdeps"]:
                    if key in test_toml_data:
                        if key not in main_toml_data:
                            main_toml_data[key] = {}
                        main_toml_data[key].update(test_toml_data[key])
                with open(f"{self.repo_path}/Project.toml", "wb") as f:
                    tomli_w.dump(main_toml_data, f)
        with open(f"{self.repo_path}/Project.toml", "rb") as f:
            toml_data = tomli.load(f)
            # 将extras并入deps，将测试所需依赖并入deps，使后续能够执行单个测试文件
            deps = toml_data.get("deps", {})
            extras = toml_data.get("extras", {})
            weakdeps = toml_data.get("weakdeps", {})
            deps.update(extras)
            deps.update(weakdeps)
            if "extras" in toml_data:
                del toml_data["extras"]
            if "targets" in toml_data:
                del toml_data["targets"]
            if "weakdeps" in toml_data:
                del toml_data["weakdeps"]
            toml_data["deps"] = deps
            with open(f"{self.repo_path}/Project.toml", "wb") as f:
                tomli_w.dump(toml_data, f)
            self.dependencies = deps
            self.dependencies.update({"Core": "", "Base": "", "Main": "", "Test": ""})  # Julia标准模块
            self.root = f"{self.repo_path}/src/{toml_data['name']}.jl"  # 只是惯例，可能存在问题
        # 在test文件头部注入runtests.jl前若干行代码
        if self.repo_path in PROJECT_TEST_PREFIX:
            runtests_file_path = os.path.join(self.repo_path, "test", "runtests.jl")
            with open(runtests_file_path, "r", encoding="utf-8") as f:
                runtests_code = f.readlines()
            prefix_lines = ["# Injected from runtests.jl\n"]
            prefix_lines += runtests_code[:PROJECT_TEST_PREFIX[self.repo_path]]
            for root, dirs, files in os.walk(os.path.join(self.repo_path, "test")):
                for file in files:
                    if file.endswith(".jl"):
                        test_file_path = os.path.join(root, file)
                        if test_file_path == runtests_file_path or test_file_path in PROJECT_TEST_EXCLUDE.get(
                                self.repo_path, []):
                            continue
                        with open(test_file_path, "r", encoding="utf-8") as f:
                            test_code = f.read()
                        if test_code.startswith(prefix_lines[0]):  # 避免重复注入
                            continue
                        # 处理prefix_lines中的include语句，改为相对于测试文件路径
                        adjusted_prefix_lines = prefix_lines.copy()
                        for i in range(len(adjusted_prefix_lines)):
                            line = adjusted_prefix_lines[i]
                            if line.strip().startswith("include(\""):
                                include_path = line.strip()[9:-2]  # 提取include中的路径
                                include_full_path = os.path.normpath(
                                    os.path.join(os.path.dirname(runtests_file_path), include_path))
                                relative_path = os.path.relpath(include_full_path, os.path.dirname(test_file_path))
                                adjusted_prefix_lines[i] = f'include("{relative_path}")\n'
                        new_code = "".join(adjusted_prefix_lines) + "\n" + test_code
                        with open(test_file_path, "w", encoding="utf-8") as f:
                            f.write(new_code)

    def get_dependencies(self) -> None:
        # 获取模块与文件映射 此处需要二次遍历文件
        src_files = get_files(self.repo_path, self.file_extension, self.build_dir)
        for src_file in src_files:
            with open(src_file, "r", encoding="utf-8") as f:
                try:
                    code = f.read()
                except Exception as e:
                    self.queue.put((self.index, "write", f"Error reading file {src_file}: {e}"))
                    return
            tree = self.parser.parse(code.encode("utf-8"))
            root_node = tree.root_node
            query = self.parser.language.query("""
            (module_definition) @module
            """)
            query_cursor = QueryCursor(query)
            captures = query_cursor.matches(root_node)
            for capture in captures:
                mod_name_nodes = capture[1]["module"]
                for mod_name_node in mod_name_nodes:
                    mod_name = mod_name_node.child_by_field_name("name").text.decode("utf-8")
                    self.modules[mod_name] = src_file
                    self.files_defining_modules[src_file] = mod_name

    # 未使用namespace
    def _traverse(self, node: Node, parent: BaseNode, file_path: str, project_path: str, namespace: str = "",
                  parent_func_node: FunctionNode = None, is_in_test: bool = False) -> None:
        # 仅处理定义模块的文件和测试文件；但通过include的间接调用会被正常处理
        if not (is_in_test or file_path in self.files_defining_modules) and (
                type(parent) == BaseNode and file_path == parent.file_path):
            return
        ir_node = None
        children = node.children
        match node.type:
            case "ERROR":
                return
            case "macrocall_expression":
                if len(node.children) >= 2:
                    identifier_node = node.children[-2]
                    assert identifier_node.type == "macro_identifier"
                    if identifier_node.text == b"@testset":
                        name_node = node.children[-1].children[0]
                        if name_node.type == "string_literal":
                            # 嵌套testset只处理最外层
                            parent_node = parent
                            nested_testset = False
                            while parent_node:
                                if type(parent_node) == TestFunctionNode:
                                    nested_testset = True
                                    break
                                parent_node = parent_node.parent
                            if not nested_testset:
                                ir_node = TestFunctionNode(parent, [], node.text.decode("utf-8"), file_path,
                                                           name_node.text.decode("utf-8").strip('"'), namespace,
                                                           (node.start_byte, node.end_byte))
                                is_in_test = True
                    elif b"include" in identifier_node.text:  # 自定义的include宏，例如@timeit_include
                        path_node = node.children[-1].children[0]
                        if path_node.type == "string_literal":
                            path = path_node.text.decode("utf-8")
                            abs_path = os.path.normpath(os.path.dirname(file_path) + "/" + path.strip('"'))
                            if os.path.isfile(abs_path):
                                self._visit_include(node, file_path, abs_path, parent, project_path, namespace,
                                                    parent_func_node, is_in_test)
                                return
                    elif identifier_node.text == b"@enum":
                        assert node.children[-1].type == "macro_argument_list"
                        for identifier in node.children[-1].named_children:
                            assert identifier.type == "identifier"
                            enum_name = identifier.text.decode("utf-8")
                            ir_node = ContextNode(parent, [], node.text.decode("utf-8"), file_path, enum_name,
                                                  node.type, (node.start_byte, node.end_byte))
                            parent.children.append(ir_node)
                        return
            case "function_definition":
                # get signature and name
                signature_node = node.named_children[0]
                assert signature_node.type == "signature"
                func_name, children = self._get_func_name(signature_node)  # 跳过函数名节点，避免函数名被重复添加为IdentifierNode
                if func_name == "":
                    return  # 忽略匿名函数
                if len(node.named_children) == 1:
                    assert node.named_children[0].type == "signature"
                    return  # 忽略函数声明
                signature = self._get_signature(node)
                # get docstring
                docstring = ""
                start_byte = node.start_byte
                if node.prev_sibling and node.prev_sibling.type == "string_literal":
                    docstring = node.prev_sibling.text.decode("utf-8").strip('"').strip()
                    start_byte = node.prev_sibling.start_byte

                root = node
                while root.parent is not None:
                    root = root.parent
                if root.start_byte >= 0:
                    start_byte -= root.start_byte
                else:
                    assert False, "Impossible"
                code_with_context = root.text[start_byte:node.end_byte].decode("utf-8")

                if is_in_test:
                    ir_node = TestHelperFunctionNode(parent, [], node.text.decode("utf-8"), file_path, func_name)
                else:
                    ir_node = FunctionNode(parent, [], node.text.decode("utf-8"), file_path, func_name, namespace,
                                           (node.start_byte, node.end_byte), signature, docstring, code_with_context,
                                           set())
                    parent_func_node = ir_node
            case "using_statement" | "import_statement":
                self._visit_import(node, parent, node.text.decode("utf-8"), file_path, node.type == "using_statement")
                return
            case "call_expression":
                name = node.children[0].text.decode("utf-8")
                if name == "include":  # 通过include间接处理其它文件
                    assert len(node.children) == 2
                    path = node.children[1].text.decode("utf-8")
                    if path.startswith('("') and path.endswith('")'):  # 不考虑include内容为非字面量（例如变量）等与上下文相关的情况
                        abs_path = os.path.normpath(os.path.dirname(file_path) + "/" + path[2:-2])
                        if os.path.isfile(abs_path):
                            self._visit_include(node, file_path, abs_path, parent, project_path, namespace,
                                                parent_func_node, is_in_test)
                    return
                else:
                    if "." in name:
                        ir_node = CallNode(parent, [], node.text.decode("utf-8"), file_path, name.split('.')[-1],
                                           name.rsplit('.', 1)[0],
                                           [arg.text.decode("utf-8") for arg in node.children[1].named_children])
                    else:
                        ir_node = CallNode(parent, [], node.text.decode("utf-8"), file_path, name, namespace,
                                           [arg.text.decode("utf-8") for arg in node.children[1].named_children])
            case "struct_definition":
                if node.children[0].type == "mutable":
                    type_head_node = node.children[2]
                else:
                    type_head_node = node.children[1]
                assert type_head_node.type == "type_head"
                name_node = type_head_node.children[0]
                while name_node.type != "identifier":
                    if name_node.type == "interpolation_expression":  # $+expression
                        name_node = name_node.children[1]
                    else:
                        name_node = name_node.children[0]
                type_name = name_node.text.decode("utf-8")
                ir_node = ContextNode(parent, [], node.text.decode("utf-8"), file_path, type_name, node.type,
                                      (node.start_byte, node.end_byte))
                children = []
            case "const_statement":
                name_node = node.children[1].children[0]
                while name_node.type != "identifier" and name_node.child_count > 0:
                    name_node = name_node.children[0]
                const_name = name_node.text.decode("utf-8")
                ir_node = ContextNode(parent, [], node.text.decode("utf-8"), file_path, const_name, node.type,
                                      (node.start_byte, node.end_byte))
        if ir_node is None and "identifier" in node.grammar_name:
            called_namespace = ""
            if "." in node.text.decode("utf-8"):
                called_namespace = node.text.decode("utf-8").rsplit('.', 1)[0]
            name = node.text.decode("utf-8").split('.')[-1]
            if parent_func_node is not None:
                identifier_node = IdentifierNode(parent_func_node, [], node.text.decode("utf-8"), file_path, name,
                                                 called_namespace)
                parent_func_node.used_idents.add(identifier_node)
            return

        if ir_node:
            parent.children.append(ir_node)
            parent = ir_node
        for child in children:
            self._traverse(child, parent, file_path, project_path, namespace, parent_func_node, is_in_test)

    def _visit_include(self, node: Node, file_path: str, include_path: str, parent: BaseNode, project_path: str,
                       namespace: str, parent_func_node: FunctionNode, is_in_test: bool) -> None:
        if is_in_test:  # 每个测试文件视为独立单元，include视为导入
            ir_node = ImportContextNode(parent, [], node.text.decode("utf-8"), file_path, "*", include_path)
            parent.children.append(ir_node)
            return
        with open(include_path, "rb") as f:
            code = f.read()
        tree = self.parser.parse(code)
        root_node = tree.root_node
        self._traverse(root_node, parent, include_path, project_path, namespace, parent_func_node,
                       is_in_test)

    def _visit_import(self, node: Node, parent: BaseNode, code: str, file_path: str, import_all: bool) -> None:
        """递归解析using/import语句，将其中的每一个导入项分别生成独立的节点，并区分项目内依赖和外部依赖"""
        prefix = ""
        children = node.named_children
        if node.children[1].type == "selected_import":
            assert len(node.children) == 2
            import_node = node.children[1]
            prefix = import_node.named_children[0].text.decode("utf-8")
            prefix = prefix.lstrip(".")
            prefix = prefix.replace(".", "/")
            children = import_node.named_children[1:]
        for child in children:
            alias = None
            import_name = child.text.decode("utf-8")
            if child.type == "import_alias":
                alias = child.named_children[1].text.decode("utf-8")
                import_name = child.named_children[0].text.decode("utf-8")
            import_name = import_name.lstrip(".")
            import_name = import_name.replace(".", "/")
            if prefix.split("/")[0] not in self.modules and import_name.split("/")[0] not in self.modules:
                import_node = ImportExternalNode(parent, [], code, file_path, alias if alias else import_name,
                                                 child.text.decode("utf-8"))
            else:
                if prefix:  # using A: B, C -> 只导入指定模块
                    import_node = ImportContextNode(parent, [], code, file_path, alias if alias else import_name,
                                                    self.modules[prefix.split("/")[0]])
                else:
                    use = import_name.split("/")[0]
                    path = import_name[len(use) + 1:] if "/" in import_name else import_name
                    import_node = ImportContextNode(parent, [], code, file_path,
                                                    "*" if import_all else alias if alias else path,
                                                    self.modules[use])  # TODO: *改为export
            parent.children.append(import_node)

    @staticmethod
    def _get_signature(node: Node) -> str:
        assert node.type == "function_definition"
        signature_node = node.named_children[0]
        assert signature_node.type == "signature"
        return node.text[:signature_node.end_byte - node.start_byte].decode("utf-8")

    @staticmethod
    def _get_func_name(node: Node) -> tuple[str, list[Node]]:  # input: signature node
        assert node.type == "signature"
        assert len(node.children) == 1

        child = node.children[0]
        if child.type == "identifier":
            func_name = child.text.decode("utf-8")
            children = []
        elif child.type == "call_expression":
            func_name = child.children[0].text.decode("utf-8")
            children = child.named_children[1:]  # 跳过函数名节点
        elif child.type == "argument_list":  # 匿名函数
            func_name = ""
            children = node.children
        elif child.type == "typed_expression":
            func_name = child.text.decode("utf-8")
            children = []
        elif child.type == "where_expression":
            func_name = child.named_children[0].children[0].text.decode("utf-8")
            children = child.named_children[0].children[1:] + child.named_children[1:]  # 跳过函数名节点
        else:
            raise ValueError("Impossible")
        assert node.parent.children[1] == node
        children += node.parent.children[1:]  # 添加参数节点
        return func_name, children

    def _find_func_node(self, node: Node, target_name: str, hash: str, file_path: str, match_hash: bool,
                        func_node: Node = None) -> Optional[Node]:
        if node.type == "function_definition":
            signature_node = node.children[1]
            assert signature_node.type == "signature"
            func_name, _ = self._get_func_name(signature_node)
            if func_name == target_name and (
                    not match_hash or hashlib.md5(node.text + file_path.encode("utf-8")).hexdigest() == hash):
                return node
        for child in node.children:
            result = self._find_func_node(child, target_name, hash, file_path, match_hash, func_node)
            if result:
                return result
        return None

    def _mask(self, func: FunctionNode, code: bytes) -> bytes:
        tree = self.parser.parse(code)
        root_node = tree.root_node
        target_func_node = self._find_func_node(root_node, func.name, func.md5(), func.file_path, False)
        assert target_func_node is not None
        signature_node = target_func_node.named_children[0]
        assert signature_node.type == "signature"
        masked_code = code[:signature_node.end_byte] + b"\n" + self.func_mask + b"\nend" + code[
            target_func_node.end_byte:]
        return masked_code

    def inject_function(self, func_node: FunctionNode) -> None:
        with open(func_node.file_path, "rb") as f:
            code = f.read()
        tree = self.parser.parse(code)
        root_node = tree.root_node
        target_func_node = self._find_func_node(root_node, func_node.name, func_node.md5(), func_node.file_path, True)
        if target_func_node is None:  # 已被注入
            assert func_node.md5() in code.decode("utf-8")
            return
        assert target_func_node.named_children[0].type == "signature"
        inject_code = f"println(\"{self.inject_template.format(func_name=func_node.md5())}\")\n"
        code_bytes = code[:target_func_node.named_children[1].start_byte] + inject_code.encode("utf-8") + \
                     code[target_func_node.named_children[1].start_byte:]
        with open(func_node.file_path, "wb") as f:
            f.write(code_bytes)

    def merge_test(self, test_func_dict: dict[TestFunctionNode, set[FunctionNode]]) -> dict[
        tuple[str, str], dict[TestFunctionNode, set[FunctionNode]]]:  # key: (test_func_name, test_file_path)
        # Julia将标准测试拆分为以文件为单位执行测试，并非适用于所有项目
        res: dict[tuple[str, str], dict[TestFunctionNode, set[FunctionNode]]] = {}
        for test_func, func_set in test_func_dict.items():
            key = ("ALL", test_func.file_path)
            if key not in res:
                res[key] = {}
            res[key][test_func] = func_set
        return res

    def _build_project_inner(self, sandbox: Sandbox, project_path: str, timeout: int | None) -> BuildResult:
        # 在项目根目录下执行
        project_path = '/'.join(project_path.split('/')[:3])
        # 安装依赖
        if project_path not in self.build_project_dir:
            result = sandbox.exec(project_path, "julia --project=. -e 'using Pkg; Pkg.build(); Pkg.instantiate()'",
                                  timeout=timeout)
            self.build_project_dir.add(project_path)
            return BuildResult(result.returncode == 0,
                               "stdout:\n" + result.stdout.decode("utf-8") + "\nstderr:\n" + result.stderr.decode(
                                   "utf-8"))
        return BuildResult(True, "")

    def _run_test_inner(self, sandbox: Sandbox, project_path: str, test_file_path: str, test_name: str,
                        timeout: int = None) -> TestResult:
        # 在项目根目录下执行
        project_path = '/'.join(project_path.split('/')[:3])
        rel_path = os.path.relpath(test_file_path, project_path)
        # 执行整个测试文件
        try:
            result = sandbox.exec(project_path, f"julia --project=. {rel_path}", timeout=timeout)
        except subprocess.CalledProcessError as e:
            # 区分编译错误和运行错误
            output = e.stdout.decode("utf-8", errors="ignore") + "\n" + e.stderr.decode("utf-8", errors="ignore")
            # Julia 错误类型：https://docs.julialang.org/en/v1/base/base/#Errors
            # ArgumentError AssertionError BoundsError CompositeException DimensionMismatch DivideError DomainError EOFError FieldError InexactError InterruptException KeyError LoadError MethodError MissingException OutOfMemoryError ReadOnlyMemoryError OverflowError ProcessFailedException TaskFailedException StackOverflowError SystemError TypeError UndefKeywordError UndefRefError UndefVarError StringIndexError InitError ParseError
            if any(err in output for err in
                   ["Failed to precompile", "FieldError", "MethodError", "MissingException", "ParseError",
                    "TypeError", "UndefKeywordError", "UndefRefError", "UndefVarError"]):  # 编译错误
                return TestResult(TestStatus.BUILD_ERROR, test_name, test_file_path, output)
            else:
                raise e
        return TestResult(TestStatus.PASSED, test_name, test_file_path,
                          "stdout:\n" + result.stdout.decode("utf-8") + "\nstderr:\n" + result.stderr.decode("utf-8"))
