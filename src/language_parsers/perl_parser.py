import hashlib
import os
import re
import subprocess
from typing import Optional

from tree_sitter import Node, Parser, QueryCursor
from tree_sitter_language_pack import get_parser

from ir import BaseNode, CallNode, ContextNode, FunctionNode, IdentifierNode, ImportContextNode, ImportExternalNode, \
    TestFunctionNode, TestHelperFunctionNode
from language_parsers.language_parser import BuildResult, LanguageParser, TestResult, TestStatus
from sandbox import Sandbox
from utils import get_files


class PerlParser(LanguageParser):
    def __init__(self, repo_path, index, queue):
        super().__init__(repo_path, index, queue)
        self.parser: Parser = get_parser("perl")
        self.language: str = "perl"
        self.file_extension: list[str] = [".pm", ".t", ".pl"]
        self.project_file: str = "Makefile.PL"
        self.build_dir: list[str] = ["__TARGET__"]
        self.test_dir: list[str] = ["t"]

        self.modules: dict[str, list[str]] = {}  # module_name -> list[file_path]
        self.command_prefix = "cpanm --local-lib=~/perl5 local::lib && eval $(perl -I ~/perl5/lib/perl5/ -Mlocal::lib) "
        self.use_dzil: bool = False
        self.build_dzil = set()

        if os.path.exists(os.path.join(repo_path, "Makefile.PL")) or os.path.exists(
                os.path.join(repo_path, "Build.PL")):
            self.use_dzil = False
        elif os.path.exists(os.path.join(repo_path, "dist.ini")):
            self.use_dzil = True
        else:
            raise Exception("No recognized Perl project file found (Makefile.PL, Build.PL, or dist.ini)")

    def get_dependencies(self) -> None:
        # 获取模块与文件映射 此处需要二次遍历文件
        src_files = get_files(self.repo_path, self.file_extension, self.build_dir)
        modules: dict[str, set[str]] = {}
        for src_file in src_files:
            with open(src_file, "r", encoding="utf-8") as f:
                try:
                    code = f.read()
                except Exception as e:
                    self.queue.put((self.index, "write", f"Error reading file {src_file}: {e}"))
                    continue
            tree = self.parser.parse(code.encode("utf-8"))
            root_node = tree.root_node
            query = self.parser.language.query("""
            (package_statement
                name: (package) @name
            )
            """)
            query_cursor = QueryCursor(query)
            captures = query_cursor.matches(root_node)
            for capture in captures:
                names = capture[1]["name"]
                assert len(names) == 1
                name = names[0].text.decode("utf-8")
                if name not in modules:
                    modules[name] = set()
                modules[name].add(src_file)
        for module in modules:
            self.modules[module] = sorted(list(modules[module]))

    # 未使用namespace
    def _traverse(self, node: Node, parent: BaseNode, file_path: str, project_path: str, namespace: str = "",
                  parent_func_node: FunctionNode = None, is_in_test: bool = False) -> None:
        ir_node = None
        children = node.children
        match node.type:
            case "ERROR":
                return
            case "source_file":
                if file_path.endswith(".t"):  # 有的项目没有subtest，直接在.t文件里写断言 反正执行时也是以文件为单位
                    ir_node = TestFunctionNode(parent, [], node.text.decode("utf-8"), file_path, file_path, namespace,
                                               (node.start_byte, node.end_byte))
                    is_in_test = True
            case "ambiguous_function_call_expression":
                func_name_node = node.child_by_field_name("function")
                if func_name_node.text == b"subtest":
                    # 避免TestFunctionNode嵌套
                    is_in_test_function = False
                    node_t = parent
                    while node_t is not None:
                        if isinstance(node_t, TestFunctionNode):
                            is_in_test_function = True
                            break
                        node_t = node_t.parent
                    if not is_in_test_function:
                        ir_node = TestFunctionNode(parent, [], node.text.decode("utf-8"), file_path, file_path,
                                                   namespace, (node.start_byte, node.end_byte))
                        is_in_test = True
                else:
                    ir_node = self._visit_call(node, parent, namespace, file_path)
            case "subroutine_declaration_statement":
                name_node = node.child_by_field_name("name")
                if is_in_test:
                    ir_node = TestHelperFunctionNode(parent, [], node.text.decode("utf-8"), file_path,
                                                     name_node.text.decode("utf-8"))
                else:
                    signature = self._get_signature(node)
                    original_docstring, docstring = self._get_docstring(node)
                    code_with_context = node.text.decode("utf-8")
                    ir_node = FunctionNode(parent, [], node.text.decode("utf-8"), file_path,
                                           name_node.text.decode("utf-8"), namespace, (node.start_byte, node.end_byte),
                                           signature, docstring, code_with_context, set())
                    # 将当前文件所有pod视作上下文
                    ir_node.used_idents.add(
                        IdentifierNode(parent_func_node, [], "pod", file_path, f"pod_{file_path}", ""))
                    parent_func_node = ir_node
                children.remove(name_node)
            case "require_expression":
                module_name = node.children[-1].text.decode("utf-8")
                if "'" in module_name or '"' in module_name:
                    # 查找字符串路径对应的模块名
                    module_path = os.path.normpath(os.path.join(os.path.dirname(file_path), module_name.strip("'\"")))
                    for module in self.modules:
                        for module_file in self.modules[module]:
                            if os.path.normpath(module_file) == module_path:
                                module_name = module
                                break
                if module_name in self.modules:
                    for module_file in self.modules[module_name]:
                        # 将导入模块作为上下文
                        ir_node = ImportContextNode(parent, [], node.text.decode("utf-8"), file_path, module_name,
                                                    module_file)
                        parent.children.append(ir_node)
                else:
                    ir_node = ImportExternalNode(parent, [], node.text.decode("utf-8"), file_path, module_name,
                                                 module_name)
                    parent.children.append(ir_node)
                return
            case "use_statement":
                if node.children[0].type != "use":
                    assert node.children[0].type == "no"
                    return
                module_node = node.child_by_field_name("module")
                module_name = module_node.text.decode("utf-8")
                if module_name == "constant":  # use constant 实际上是定义常量
                    assert len(node.named_children) == 2
                    use_expression = node.named_children[1]
                    list_expressions = []
                    if use_expression.type == "anonymous_hash_expression":
                        for child in use_expression.named_children:
                            if child.type == "list_expression":
                                list_expressions.append(use_expression.named_children[0])
                    elif use_expression.type == "conditional_expression":
                        assert use_expression.named_child_count == 3
                        for child in use_expression.named_children[1:]:
                            assert child.type == "anonymous_hash_expression"
                            assert child.named_child_count == 1
                            list_expressions.append(child.named_children[0])
                    else:
                        assert use_expression.type == "list_expression"
                        list_expressions.append(use_expression)

                    for list_expression in list_expressions:
                        assert list_expression.named_children[0].type == "autoquoted_bareword"
                        for index, const_name_node in enumerate(list_expression.named_children):
                            if const_name_node.type != "autoquoted_bareword":  # key
                                continue
                            ir_node = ContextNode(parent, [], node.text.decode("utf-8"), file_path,
                                                  const_name_node.text.decode("utf-8"), "constant",
                                                  (node.start_byte, node.end_byte))
                            parent.children.append(ir_node)
                        return
                else:  # 导入文件
                    uses = []
                    if node.named_children[-1].type == "quoted_word_list":
                        if len(node.named_children[-1].named_children) == 0:
                            uses.append(module_name)
                        else:
                            assert len(node.named_children[-1].named_children) == 1
                            string = node.named_children[-1].named_children[0]
                            assert string.type == "string_content"
                            for child in string.text.decode("utf-8").split():
                                uses.append(child)
                    elif node.named_children[-1].type == "stub_expression":
                        uses.append(module_name)
                    else:
                        uses.append(module_name)
                        uses.append("*")  # 在Perl中，use Module;的行为取决于Module->import方法的实现
                    for use in uses:
                        if module_name in self.modules:
                            for module_file in self.modules[module_name]:
                                # 将导入模块作为上下文
                                ir_node = ImportContextNode(parent, [], node.text.decode("utf-8"), file_path, use,
                                                            module_file)
                                parent.children.append(ir_node)
                                # 如果非根模块，考虑近似将所有模块的子模块也加入上下文（可能存在动态导入），即use Foo::Bar时将Foo::Bar::*也加入上下文
                                if "::" in module_name:
                                    prefix = module_name + "::"
                                    for other_module in self.modules:
                                        if other_module.startswith(prefix):
                                            for other_module_file in self.modules[other_module]:
                                                ir_node = ImportContextNode(parent, [], node.text.decode("utf-8"),
                                                                            file_path,
                                                                            use, other_module_file)
                                                parent.children.append(ir_node)

                        else:
                            ir_node = ImportExternalNode(parent, [], node.text.decode("utf-8"), file_path, use,
                                                         module_name)
                            parent.children.append(ir_node)
                    return
            case "function_call_expression" | "method_call_expression":
                ir_node = self._visit_call(node, parent, namespace, file_path)
            case "pod":
                ir_node = ContextNode(parent, [], node.text.decode("utf-8"), file_path, f"pod_{file_path}", node.type,
                                      (node.start_byte, node.end_byte))
            case "variable_declaration":
                if node.children[0].type == "our":
                    if node.child_count == 2:
                        assert "expression" in node.parent.type
                        var_node = node.child_by_field_name("variable")
                        assert var_node.children[-1].type == "varname"
                        ir_node = ContextNode(parent, [], node.parent.text.decode("utf-8"), file_path,
                                              var_node.children[-1].text.decode("utf-8"), "our_variable",
                                              (node.start_byte, node.end_byte))
                    else:
                        assert "expression" in node.parent.type
                        var_nodes = node.children_by_field_name("variables")
                        assert var_nodes[0].type == "(" and var_nodes[-1].type == ")"
                        var_nodes = var_nodes[1:-1]
                        for var_node in var_nodes:
                            if var_node.type == ",":
                                continue
                            assert var_node.children[-1].type == "varname"
                            ir_node = ContextNode(parent, [], node.parent.text.decode("utf-8"), file_path,
                                                  var_node.children[-1].text.decode("utf-8"), "our_variable",
                                                  (node.start_byte, node.end_byte))
                            parent.children.append(ir_node)
                        return

        if "identifier" in node.grammar_name or "varname" in node.grammar_name:
            if parent_func_node is not None:
                assert ir_node is None
                name = node.text.decode("utf-8")
                if "::" in name:
                    identifier_node = IdentifierNode(parent_func_node, [], node.text.decode("utf-8"), file_path,
                                                     name.split("::")[-1], name.rsplit("::", 1)[0])
                else:
                    identifier_node = IdentifierNode(parent_func_node, [], node.text.decode("utf-8"), file_path,
                                                     node.text.decode("utf-8"), "")
                parent_func_node.used_idents.add(identifier_node)
        if ir_node:
            parent.children.append(ir_node)
            parent = ir_node
        for child in children:
            self._traverse(child, parent, file_path, project_path, namespace, parent_func_node, is_in_test)

    def _find_func_node(self, node: Node, target_name: str, hash: str, file_path: str, match_hash: bool,
                        func_node: Node = None) -> \
            Optional[Node]:
        if node.type == "subroutine_declaration_statement":
            name_node = node.child_by_field_name("name")
            if name_node.text.decode("utf-8") == target_name:
                if not match_hash or hashlib.md5(node.text + file_path.encode("utf-8")).hexdigest() == hash:
                    return node
        for child in node.children:
            result = self._find_func_node(child, target_name, hash, file_path, match_hash, func_node)
            if result is not None:
                return result
        return None

    def _visit_call(self, node: Node, parent: BaseNode, namespace: str, file_path: str) -> CallNode:
        func_name_node = node.child_by_field_name("function") or node.child_by_field_name("method")
        arguments_node = node.child_by_field_name("arguments")
        arguments = []  # 对ambiguous_function_call_expression而言，tree-sitter似乎只会解析第一个参数，但无所谓，目前没有使用到arguments这个字段
        if arguments_node:
            for arg in arguments_node.named_children:
                arguments.append(arg.text.decode("utf-8"))
        full_name = func_name_node.text.decode("utf-8")
        if "::" in full_name:
            return CallNode(parent, [], node.text.decode("utf-8"), file_path, full_name.split("::")[-1],
                            full_name.rsplit("::", 1)[0], arguments)
        else:
            if node.type == "method_call_expression":  # 考虑Module->func()的情况，此时namespace为Module
                invocant_node = node.child_by_field_name("invocant")
                assert invocant_node is not None
                invocant = invocant_node.text.decode("utf-8")
                if invocant in self.modules:
                    return CallNode(parent, [], node.text.decode("utf-8"), file_path, full_name, invocant, arguments)
            return CallNode(parent, [], node.text.decode("utf-8"), file_path, full_name, namespace, arguments)

    def _get_docstring(self, node: Node) -> tuple[str, str]:  # original docstring, processed docstring
        root = node
        while root.parent is not None:
            root = root.parent
        query = self.parser.language.query("""(pod) @pod""")
        query_cursor = QueryCursor(query)
        captures = query_cursor.matches(root)
        if not captures:
            return "", ""
        pattern = r"=(\w+)(.*?)(?=\n=|$)"
        original_docstring = ""
        docstring = ""
        for capture in captures:
            pod = capture[1]["pod"]
            assert len(pod) == 1
            pod = pod[0].text.decode("utf-8")
            matches = re.findall(pattern, pod, re.DOTALL)
            head1 = ""
            head2 = ""
            for match in matches:
                if match[0].strip() == "head1":
                    head1 = match[1].strip().lower()
                if match[0].strip() == "head2":
                    head2 = match[1].strip().lower()
                if any(kw in head1 or kw in head2 for kw in
                       ["function", "method", "interface", "constructor", "accessor"]):
                    func_name = match[1].split("\n", 1)[0].strip()
                    if func_name.startswith("C<") and func_name.endswith(">"):
                        func_name = func_name[1:].strip()
                        while func_name.startswith("<") and func_name.endswith(">"):
                            func_name = func_name[1:-1].strip()
                    func_name = func_name.split("(", 1)[0].strip()
                    if func_name == node.child_by_field_name("name").text.decode("utf-8"):
                        original_docstring = "=" + match[0] + " " + match[1]
                        docstring = match[1].split("\n", 1)[1].strip()
        if original_docstring:
            if original_docstring.endswith("\n"):
                original_docstring += "=cut"
            else:
                original_docstring += "\n=cut"
        return original_docstring, docstring

    @staticmethod
    def _get_signature(node: Node) -> str:
        if node.children[-2].type == "signature":
            pass
        assert node.children[-1].type == "block"
        return node.text[:node.children[-1].start_byte - node.start_byte].decode("utf-8")

    def _mask(self, func: FunctionNode, code: bytes) -> bytes:
        tree = self.parser.parse(code)
        root_node = tree.root_node
        target_func_node = self._find_func_node(root_node, func.name, func.md5(), func.file_path, False)
        assert target_func_node is not None
        block_node = target_func_node.children[-1]
        assert block_node.type == "block"
        first_expression = b""
        if block_node.named_child_count:  # 如果第一行是传参语句则保留
            first_expression_node = block_node.named_children[0]
            if any(kw in first_expression_node.text for kw in [b"@_", b"$_", b"my", b"shift", b"pop", b"push"]):
                first_expression = first_expression_node.text + b";\n"
        masked_code = code[:block_node.start_byte] + b"{\n" + first_expression + self.func_mask + b"\n}" + code[
            block_node.end_byte:]
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
        block_node = target_func_node.children[-1]
        assert block_node.type == "block"
        block_node = block_node.named_children[0]
        inject_code = f"print \"{self.inject_template.format(func_name=func_node.md5())}\\n\";\n"
        new_code_bytes = code[:block_node.start_byte] + inject_code.encode("utf-8") + code[
            block_node.start_byte:]
        with open(func_node.file_path, "wb") as f:
            f.write(new_code_bytes)

    def merge_test(self, test_func_dict: dict[TestFunctionNode, set[FunctionNode]]) -> dict[
        tuple[str, str], dict[TestFunctionNode, set[FunctionNode]]]:  # key: (test_func_name, test_file_path)
        # Perl以文件为单位执行测试
        res: dict[tuple[str, str], dict[TestFunctionNode, set[FunctionNode]]] = {}
        for test_func, func_set in test_func_dict.items():
            key = ("ALL", test_func.file_path)
            if key not in res:
                res[key] = {}
            res[key][test_func] = func_set
        return res

    def _build_project_inner(self, sandbox: Sandbox, project_path: str, timeout: int | None) -> BuildResult:
        if self.use_dzil:
            if project_path not in self.build_dzil:
                result_dzil_build = sandbox.exec(project_path,
                                                 self.command_prefix + f"&& dzil build --in {self.build_dir[0]}",
                                                 timeout)
                if result_dzil_build.returncode != 0:
                    return BuildResult(False, "stdout:\n" + result_dzil_build.stdout.decode(
                        "utf-8") + "\nstderr:\n" + result_dzil_build.stderr.decode("utf-8"))
                self.build_dzil.add(project_path)
            result = sandbox.exec(project_path,
                                  self.command_prefix + f"&& cpanm --installdeps --notest ./{self.build_dir[0]}",
                                  timeout)
        else:
            result = sandbox.exec(project_path, self.command_prefix + f"&& cpanm --installdeps --notest .", timeout)
        return BuildResult(result.returncode == 0,
                           "stdout:\n" + result.stdout.decode("utf-8") + "\nstderr:\n" + result.stderr.decode("utf-8"))

    def _run_test_inner(self, sandbox: Sandbox, project_path: str, test_file_path: str, test_name: str,
                        timeout: int | None) -> TestResult:
        relative_path = os.path.relpath(test_file_path, project_path)
        try:
            result = sandbox.exec(project_path, self.command_prefix + f"&& prove -v -l {relative_path}",
                                  timeout=timeout)
        except subprocess.CalledProcessError as e:
            # 区分编译错误和运行错误
            output = e.stdout.decode("utf-8", errors="ignore") + "\n" + e.stderr.decode("utf-8", errors="ignore")
            # https://perldoc.perl.org/perldiag
            keywords = ["compilation aborted", "syntax error", "Can't locate",
                        "not implemented", "not supported", "Cannot invoke", "Can't find",
                        "Can't \"goto\" into", "Can't use", "Can't call", "compilation errors",
                        "Label not found", "Mismatched brackets", "Missing", "No such class", "Not enough arguments",
                        "Too few arg", "Too many arg", "Undefined", "Unimplemented", "Unsupported"]
            patterns = ["Can't \".{1,20}?\" outside", "Not an? .{1,20}? reference"]
            if any(kw in output for kw in keywords) or any(re.search(pat, output) for pat in patterns):
                return TestResult(TestStatus.BUILD_ERROR, test_name, test_file_path, output)
            # elif any(kw in output for kw in ["got", "expected"]):  # 测试失败
            #     return TestResult(TestStatus.FAILED, test_name, test_file_path, output)
            else:  # 运行错误
                raise e

        pattern = f"Files=1, Tests=(\d+)"
        matches = re.findall(pattern, result.stdout.decode("utf-8"))
        executed_tests = sum(int(match) for match in matches)
        if executed_tests == 0:
            return TestResult(TestStatus.NOTFOUND, test_name, test_file_path, "Test not found")
        return TestResult(TestStatus.PASSED, test_name, test_file_path,
                          "stdout:\n" + result.stdout.decode("utf-8") + "\nstderr:\n" + result.stderr.decode("utf-8"))
