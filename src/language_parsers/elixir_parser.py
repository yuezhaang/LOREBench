import hashlib
import os
import re
import subprocess
from typing import Optional

import tree_sitter_elixir
from tree_sitter import Language, Node, Parser, QueryCursor

from ir import BaseNode, CallNode, ContextNode, FunctionNode, IdentifierNode, ImportContextNode, ImportExternalNode, \
    ModuleNode, TestFunctionNode, TestHelperFunctionNode
from language_parsers.language_parser import BuildResult, LanguageParser, TestResult, TestStatus
from sandbox import Sandbox
from utils import get_files


class ElixirParser(LanguageParser):
    def __init__(self, repo_path, index, queue):
        super().__init__(repo_path, index, queue)
        self.parser = Parser(Language(tree_sitter_elixir.language()))
        self.language = "elixir"
        self.file_extension = [".ex", ".exs"]
        self.project_file = "mix.exs"
        self.build_dir = ["_build", "deps", "tmp"]
        self.test_dir = ["test"]

        self.autoload: list[tuple[str, str]] = []  # (module_name, file_path)
        self.modules: dict[str, str] = {}  # module_name -> file_path

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
            (call
                target: (identifier) @target
                (#eq? @target "defmodule")
            ) @call
            """)
            query_cursor = QueryCursor(query)
            captures = query_cursor.matches(root_node)
            for capture in captures:
                calls = capture[1]["call"]
                assert len(calls) == 1
                call = calls[0]
                assert call.child_by_field_name("target").text == b"defmodule"
                assert call.children[1].type == "arguments"
                module_name = call.children[1].text.decode("utf-8")
                parent = call.parent  # 处理嵌套模块
                while parent is not None:
                    if parent.type == "call":
                        target_node = parent.child_by_field_name("target")
                        if target_node.text == b"defmodule":
                            module_name = parent.children[1].text.decode("utf-8") + "." + module_name
                    parent = parent.parent
                if self.modules.get(module_name) is None:
                    self.modules[module_name] = src_file
                elif self.modules[module_name] != src_file:  # 有重复定义的模块，优先使用.ex文件
                    if src_file.endswith("ex"):
                        # assert self.modules[module_name].endswith("exs")  # 忽略重名，重名模块不会被同时运行
                        self.modules[module_name] = src_file

        # 针对每个文件自动加入导入所有ex文件的import节点模拟mix的自动加载机制
        for module_name, module_file in self.modules.items():
            if module_file.endswith(".ex"):
                self.autoload.append((module_name, module_file))

    # 未使用namespace
    def _traverse(self, node: Node, parent: BaseNode, file_path: str, project_path: str, namespace: str = "",
                  parent_func_node: FunctionNode = None, is_in_test: bool = False,
                  module: list[str] | None = None) -> None:
        if file_path.endswith("/mix.exs"):  # mix.exs无需处理
            return
        ir_node = None
        children = node.children
        match node.type:
            case "ERROR":
                return
            case "source":  # 根节点，加入自动导入节点
                for auto_import in self.autoload:
                    module_name, module_file = auto_import
                    ir_node = ImportContextNode(parent, [], f"require {module_name}", file_path, module_name,
                                                module_file, True)
                    parent.children.append(ir_node)
                    ir_node = None
            case "call":
                target_node = node.child_by_field_name("target")
                assert target_node is not None
                match target_node.text:
                    case b"defmodule":
                        module_name_node = node.children[1].children[0]
                        module_name = module_name_node.text.decode("utf-8")
                        if module is None:
                            module = []
                        module.append(module_name)
                        # 直接将嵌套模块展开为单个模块
                        template = f"defmodule {'.'.join(module)} do\n{self.module_body_template}\nend"
                        ir_node = ModuleNode(parent, [], node.text.decode("utf-8"), file_path, template)
                    case b"test":
                        ir_node = TestFunctionNode(parent_func_node, [], node.text.decode("utf-8"), file_path,
                                                   str(node.start_point.row + 1), namespace,
                                                   (node.start_byte, node.end_byte))
                        is_in_test = True
                    case b"def" | b"defp":
                        name_node = node.children[1].children[0]
                        while len(name_node.children):
                            name_node = name_node.children[0]
                        name = name_node.text.decode("utf-8")
                        if is_in_test:
                            ir_node = TestHelperFunctionNode(parent, [], node.text.decode("utf-8"), file_path, name)
                        else:
                            docstring, start_byte = self._get_docstring(node)
                            signature = self._get_signature(node)

                            root = node
                            while root.parent is not None:
                                root = root.parent
                            assert root.start_byte == 0
                            code_with_context = root.text[start_byte:node.end_byte].decode("utf-8")

                            ir_node = FunctionNode(parent, [], node.text.decode("utf-8"), file_path, name, namespace,
                                                   (node.start_byte, node.end_byte), signature, docstring,
                                                   code_with_context, set())
                            parent_func_node = ir_node
                    case b"alias":
                        assert len(node.children) == 2
                        alias = None
                        if node.children[1].named_child_count > 1:  # alias ... as
                            assert node.children[1].named_child_count == 2
                            keywords_node = node.children[1].named_children[1]
                            assert keywords_node.type == "keywords"
                            assert keywords_node.child_count == 1
                            assert keywords_node.children[0].type == "pair"
                            keyword_node = keywords_node.children[0].children[0]
                            assert keyword_node.type == "keyword"
                            if keyword_node.text == b"as: ":  # alias ... warn
                                alias_node = keywords_node.children[0].children[1]
                                assert alias_node.type == "alias"
                                alias = alias_node.text.decode("utf-8")
                        dot_node = node.children[1].children[0]
                        module_names: list[tuple[str, str]] = []  # (module_name, use_name)
                        if dot_node.type == "dot":
                            name_node = dot_node.children[0]
                            if b"__MODULE__" in name_node.text:
                                assert module
                                module_name = name_node.text.decode("utf-8").replace("__MODULE__", ".".join(module))
                            else:
                                assert dot_node.named_children[0].type == "alias"
                                module_name = dot_node.named_children[0].text.decode("utf-8")
                            for item in dot_node.named_children[1].named_children:
                                assert item.type == "alias"
                                full_name = f"{module_name}.{item.text.decode('utf-8')}"
                                module_names.append(
                                    (full_name, alias if alias else full_name.split(".")[-1]))  # 默认别名为模块名最后一部分
                        else:
                            assert dot_node.type == "alias" or dot_node.text == b"__MODULE__"
                            if b"__MODULE__" in dot_node.text:
                                assert module
                                module_name = dot_node.text.decode("utf-8").replace("__MODULE__", ".".join(module))
                            else:
                                assert dot_node.type == "alias"
                                module_name = dot_node.text.decode("utf-8")
                            module_names.append(
                                (module_name, alias if alias else module_name.split(".")[-1]))  # 默认别名为模块名最后一部分
                        for module_name in module_names:
                            name, use = module_name
                            if name in self.modules:
                                module_file = self.modules[name]
                                ir_node = ImportContextNode(parent, [], node.text.decode("utf-8"), file_path, use,
                                                            module_file)
                            else:
                                ir_node = ImportExternalNode(parent, [], node.text.decode("utf-8"), file_path, use,
                                                             name)
                            parent.children.append(ir_node)
                        return  # 已经加入节点，直接返回
                    case b"import":
                        assert len(node.children) == 2
                        module_name_node = node.children[1]
                        module_names: list[tuple[str, str]] = []  # (module_name, use_name)
                        if len(module_name_node.named_children) == 1:
                            module_names.append((module_name_node.text.decode("utf-8"), "*"))
                        else:
                            assert len(module_name_node.named_children) == 2
                            module_name = module_name_node.named_children[0].text.decode("utf-8")
                            if module_name.startswith("unquote(") and module_name.endswith(")"):
                                return  # 忽略动态导入
                            keyword_node = module_name_node.named_children[1].children[0].children[0]
                            list_node = module_name_node.named_children[1].children[0].children[1]
                            if keyword_node.text == b"only: ":
                                if len(list_node.named_children) == 0:  # only: []
                                    module_names.append((module_name, module_name))
                                else:
                                    assert len(list_node.named_children) == 1
                                    list_node = list_node.named_children[0]
                                    for item in list_node.named_children:
                                        assert item.type == "pair"
                                        use_node = item.children[0]
                                        use_name = use_node.text.decode("utf-8").strip().rstrip(":")
                                        module_names.append((module_name, use_name))
                            else:
                                module_names.append((module_name, "*"))
                        for module_name in module_names:
                            assert "__MODULE__" not in module_name
                            name, use = module_name
                            if name in self.modules:
                                module_file = self.modules[name]
                                ir_node = ImportContextNode(parent, [], node.text.decode("utf-8"), file_path, use,
                                                            module_file)
                            else:
                                ir_node = ImportExternalNode(parent, [], node.text.decode("utf-8"), file_path, use,
                                                             name)
                            parent.children.append(ir_node)
                        return  # 已经加入节点，直接返回
                    case b"use":
                        assert len(node.children) == 2
                        module_name_node = node.children[1].children[0]  # 不管参数
                        module_name = module_name_node.text.decode("utf-8")
                        if "__MODULE__" in module_name:
                            assert module
                            module_name = module_name.replace("__MODULE__", ".".join(module))
                        if module_name in self.modules:
                            module_file = self.modules[module_name]
                            ir_node = ImportContextNode(parent, [], node.text.decode("utf-8"), file_path, "*",
                                                        module_file)
                        else:
                            ir_node = ImportExternalNode(parent, [], node.text.decode("utf-8"), file_path, "*",
                                                         module_name)
                    case b"require":
                        name_node = node.children[1]
                        module_name = name_node.children[0].text.decode("utf-8")
                        if len(name_node.named_children) == 1:
                            use_name = module_name
                        else:  # require as
                            assert len(name_node.named_children) == 2
                            assert len(name_node.named_children[1].named_children) == 1
                            use_name = name_node.named_children[1].named_children[0].children[1].text.decode("utf-8")
                        if module_name in self.modules:
                            module_file = self.modules[module_name]
                            ir_node = ImportContextNode(parent, [], node.text.decode("utf-8"), file_path, use_name,
                                                        module_file)
                        else:
                            ir_node = ImportExternalNode(parent, [], node.text.decode("utf-8"), file_path, use_name,
                                                         module_name)
                    case b"defstruct":
                        assert module
                        ir_node = ContextNode(parent, [], node.text.decode("utf-8"), file_path, ".".join(module),
                                              node.type, (node.start_byte, node.end_byte))
                    case _:  # 普通函数调用
                        # elixir中很多关键字的使用都解析为call
                        if len(node.children) == 2 and node.children[1].type == "arguments":
                            assert node.children[0].type in ["dot", "identifier", "call"]
                            arguments = []
                            for arg in node.children[1].named_children:
                                arguments.append(arg.text.decode("utf-8"))
                            call_name = target_node.text.decode("utf-8")
                            if "." in call_name:
                                ir_node = CallNode(parent_func_node, [], node.text.decode("utf-8"), file_path,
                                                   call_name.split(".")[-1], call_name.rsplit(".", 1)[0], arguments)
                            else:
                                ir_node = CallNode(parent_func_node, [], node.text.decode("utf-8"), file_path,
                                                   call_name, namespace, arguments)
        if "identifier" in node.grammar_name or node.grammar_name == "struct" or "atom" in node.grammar_name:
            if parent_func_node is not None:
                assert ir_node is None
                text = node.text.decode("utf-8")
                if "__MODULE__" in text and module:
                    text = text.replace("__MODULE__", ".".join(module))
                if "atom" in node.grammar_name:
                    text = text.lstrip(":")
                    text = text.strip('"').strip("'")  # 处理可能的字符串形式的atom
                identifier_node = IdentifierNode(parent_func_node, [], node.text.decode("utf-8"), file_path, text, "")
                parent_func_node.used_idents.add(identifier_node)
        if ir_node:
            parent.children.append(ir_node)
            parent = ir_node
        for child in children:
            self._traverse(child, parent, file_path, project_path, namespace, parent_func_node, is_in_test, module)

    def _find_func_node(self, node: Node, target_name: str, hash: str, file_path: str, match_hash: bool,
                        func_node: Node = None) -> Optional[Node]:
        if node.type == "call":
            target_node = node.child_by_field_name("target")
            if target_node.text in [b"def", b"defp"]:
                name_node = node.children[1].children[0]
                while len(name_node.children):
                    name_node = name_node.children[0]
                name = name_node.text.decode("utf-8")
                if name == target_name:
                    if not match_hash or hashlib.md5(node.text + file_path.encode("utf-8")).hexdigest() == hash:
                        return node
        for child in node.children:
            result = self._find_func_node(child, target_name, hash, file_path, match_hash, func_node)
            if result:
                return result
        return None

    @staticmethod
    def _get_signature(node: Node) -> str:
        if node.children[-1].type == "do_block":
            assert node.children[-1].text.startswith(b"do")
            return node.text[:node.children[-1].start_byte - node.start_byte + 2].decode("utf-8")
        else:
            assert len(node.children) == 2  # 没有do_block，单行函数
            assert node.children[1].type == "arguments"
            # find "do: " in the children after arguments
            for child in node.children[1].children:
                if child.text.startswith(b"do:"):
                    return node.text[:child.start_byte - node.start_byte + 3].decode("utf-8")
            return node.text.decode("utf-8")  # 无函数体

    @staticmethod
    def _get_docstring(node: Node) -> tuple[str, int]:
        doc_lines = []
        start_byte = node.start_byte
        prev_sibling = node.prev_sibling
        # @... """..."""形式的文档注释
        pattern = r'@[a-zA-Z_][a-zA-Z0-9_]*\s+"""(.*)'
        while prev_sibling and prev_sibling.type in ["unary_operator", "comment"]:
            match_obj = re.match(pattern, prev_sibling.text.decode("utf-8").strip(), re.DOTALL)  # TODO：处理同名函数的docstring
            comment_text = ""
            if prev_sibling.type == "unary_operator" and prev_sibling.text.startswith(b"@"):
                if match_obj:
                    comment_text = match_obj.group(1).strip()
                    assert comment_text.endswith('"""'), "Docstring not properly closed with triple quotes"
                    comment_text = comment_text[:-3].strip()
                    doc_lines.append(comment_text)
                    start_byte = prev_sibling.start_byte
                    break  # @doc只取最近的一段
            elif prev_sibling.type == "comment":
                comment_text = prev_sibling.text.decode("utf-8").lstrip("#").strip()
            else:
                break
            if comment_text:
                doc_lines.append(comment_text)
                start_byte = prev_sibling.start_byte
            prev_sibling = prev_sibling.prev_sibling
        if doc_lines:
            doc_lines.reverse()
            return "\n".join(doc_lines), start_byte
        return "", start_byte

    def _mask(self, func: FunctionNode, code: bytes) -> bytes:
        tree = self.parser.parse(code)
        root_node = tree.root_node
        target_func_node = self._find_func_node(root_node, func.name, func.md5(), func.file_path, False)
        assert target_func_node is not None
        block_node = target_func_node.children[-1]
        if block_node.type != "do_block":  # 单行语法糖形式：def func(args), do: expr
            assert block_node.type == "arguments"
            keywords_node = block_node.children[-1]
            if keywords_node.type == "call":
                assert block_node.child_count == 1  # 仅函数头
                return code
            assert keywords_node.type == "keywords"
            assert keywords_node.child_count == 1
            assert keywords_node.children[0].type == "pair"
            block_node = keywords_node.children[0].children[1]  # pair -> value
            masked_code = code[:block_node.start_byte] + self.func_mask + code[block_node.end_byte:]
        else:
            assert block_node.type == "do_block"
            assert block_node.children[0].type == "do"
            assert block_node.children[-1].type == "end"
            masked_code = code[:block_node.children[0].end_byte] + self.func_mask + code[
                block_node.children[-1].start_byte:]

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
        inject_code = f"IO.puts(\"{self.inject_template.format(func_name=func_node.md5())}\")\n"
        block_node = target_func_node.children[-1]
        if block_node.type != "do_block":  # 单行语法糖形式：def func(args), do: expr
            assert block_node.type == "arguments"
            keywords_node = block_node.children[-1]
            if keywords_node.type == "call":
                assert block_node.child_count == 1  # 仅函数头
                return
            assert keywords_node.type == "keywords"
            assert keywords_node.child_count == 1
            assert keywords_node.children[0].type == "pair"
            block_node = keywords_node.children[0].children[1]  # pair -> value
            new_code_bytes = code[:block_node.start_byte] + b"(" + inject_code.encode(
                "utf-8") + block_node.text + b")" + code[block_node.end_byte:]
        else:
            block_node = block_node.named_children[0]
            new_code_bytes = code[:block_node.start_byte] + inject_code.encode("utf-8") + code[
                block_node.start_byte:]
        with open(func_node.file_path, "wb") as f:
            f.write(new_code_bytes)

    def _build_project_inner(self, sandbox: Sandbox, project_path: str, timeout: int | None) -> BuildResult:
        result1 = sandbox.exec(project_path, f"HEX_MIRROR=https://hexpm.upyun.com mix deps.get", timeout)  # MIRROR
        # result1 = sandbox.exec(project_path, f"mix deps.get", timeout)
        result2 = sandbox.exec(project_path, f"MIX_ENV=test mix compile", timeout)
        return BuildResult(result1.returncode == 0 and result2.returncode == 0,
                           "stdout:\n" + result1.stdout.decode("utf-8") + result2.stdout.decode("utf-8") +
                           "\nstderr:\m" + result1.stderr.decode("utf-8") + result2.stderr.decode("utf-8"))

    def _run_test_inner(self, sandbox: Sandbox, project_path: str, test_file_path: str, test_name: str,
                        timeout: int | None) -> TestResult:
        relative_path = os.path.relpath(test_file_path, project_path)
        try:
            result = sandbox.exec(project_path, f"mix test {relative_path}:{test_name}", timeout=timeout)
        except subprocess.CalledProcessError as e:
            output = e.stdout.decode("utf-8", errors="ignore") + "\n" + e.stderr.decode("utf-8", errors="ignore")
            pattern = r"\*\* \((.+Error)\)"
            matches = re.findall(pattern, output)

            # 区分编译错误和运行错误
            if matches:  # 编译错误
                for error_type in matches:
                    # Elixir 错误类型 https://hexdocs.pm/elixir/1.19.5/api-reference.html
                    # ArgumentError ArithmeticError BadArityError BadBooleanError BadFunctionError BadMapError BadStructError CaseClauseError Code.LoadError CompileError CondClauseError Enum.EmptyError Enum.OutOfBoundsError ErlangError File.CopyError File.Error File.LinkError File.RenameError FunctionClauseError IO.StreamError Inspect.Error JSON.DecodeError Kernel.TypespecError KeyError MatchError MismatchedDelimiterError MissingApplicationsError OptionParser.ParseError Protocol.UndefinedError Regex.CompileError RuntimeError SyntaxError System.EnvError SystemLimitError TokenMissingError TryClauseError URI.Error UndefinedFunctionError UnicodeConversionError Version.InvalidRequirementError Version.InvalidVersionError WithClauseError
                    if error_type in ["ArgumentError", "BadArityError", "BadFunctionError", "BadMapError",
                                      "BadStructError", "CaseClauseError", "Code.LoadError", "CompileError",
                                      "CondClauseError", "FunctionClauseError", "Kernel.TypespecError",
                                      "MatchError", "MismatchedDelimiterError", "MissingApplicationsError",
                                      "Protocol.UndefinedError", "SyntaxError", "TryClauseError",
                                      "UndefinedFunctionError", "Version.InvalidRequirementError",
                                      "Version.InvalidVersionError", "WithClauseError"]:
                        return TestResult(TestStatus.BUILD_ERROR, test_name, test_file_path, output)
            # if "Assertion failed" in output:  # 测试失败
            #     return TestResult(TestStatus.FAILED, test_name, test_file_path, output)
            raise e  # 运行错误
        pattern = f"(\d+) tests?, \d+ failure"
        matches = re.findall(pattern, result.stdout.decode("utf-8"))
        executed_tests = sum(int(match) for match in matches)
        if executed_tests == 0:
            return TestResult(TestStatus.NOTFOUND, test_name, test_file_path, "Test not found" + "\nstdout:\n" +
                              result.stdout.decode("utf-8") + "\nstderr:\n" + result.stderr.decode("utf-8"))
        return TestResult(TestStatus.PASSED, test_name, test_file_path,
                          "stdout:\n" + result.stdout.decode("utf-8") + "\nstderr:\n" + result.stderr.decode("utf-8"))
