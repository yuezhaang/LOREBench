import hashlib
import os
import re
import subprocess
from enum import Enum
from typing import Optional

import tree_sitter_haskell
from tree_sitter import Language, Node, Parser, QueryCursor

from ir import BaseNode, CallNode, ContextNode, FunctionNode, IdentifierNode, ImportContextNode, ImportExternalNode, \
    TestFunctionNode, TestHelperFunctionNode
from language_parsers.language_parser import BuildResult, LanguageParser, TestResult, TestStatus
from sandbox import Sandbox
from utils import get_files


class TestFramework(Enum):
    HSpec = "HSpec"
    Tasty = "Tasty"
    Unknown = "Unknown"


class HaskellParser(LanguageParser):
    def __init__(self, repo_path, index, queue):
        super().__init__(repo_path, index, queue)
        self.parser = Parser(Language(tree_sitter_haskell.language()))
        self.language = "haskell"
        self.file_extension = [".hs"]
        self.project_file = ".cabal"
        self.build_dir = [".stack-work", "dist-newstyle"]
        self.test_dir = ["test", "tests"]

        self.modules: dict[str, str] = {}  # {模块名: 文件路径}
        self.test_framework = TestFramework.Unknown

        # 根据.cabal文件判断测试框架
        cabal_files = get_files(self.repo_path, [self.project_file], self.build_dir)
        for cabal_file in cabal_files:
            with open(cabal_file, "r", encoding="utf-8") as f:
                try:
                    content = f.read()
                except Exception as e:
                    self.queue.put((self.index, "write", f"Error reading file {cabal_file}: {e}"))
                    return
            if re.search(r"build-depends:.*hspec", content, re.DOTALL):
                self.test_framework = TestFramework.HSpec
                break
            elif re.search(r"build-depends:.*tasty", content, re.DOTALL):
                self.test_framework = TestFramework.Tasty
                break

    def get_dependencies(self) -> None:
        # 获取模块与文件映射 此处需要二次遍历文件
        src_files = get_files(self.repo_path, self.file_extension, self.build_dir)
        for src_file in src_files:
            with open(src_file, "r", encoding="utf-8") as f:
                try:
                    code = f.read()
                except Exception as e:
                    self.queue.put((self.index, "write", "Error reading file {src_file}: {e}"))
                    return
            tree = self.parser.parse(code.encode("utf-8"))
            root_node = tree.root_node
            query = self.parser.language.query("""
            (header) @header
            """)
            query_cursor = QueryCursor(query)
            captures = query_cursor.matches(root_node)
            for capture in captures:
                headers = capture[1]["header"]
                for header in headers:
                    mod_name = header.child_by_field_name("module").text.decode("utf-8")
                    self.modules[mod_name] = src_file

    # 未使用namespace
    def _traverse(self, node: Node, parent: BaseNode, file_path: str, project_path: str, namespace: str = "",
                  parent_func_node: FunctionNode = None, is_in_test: bool = False) -> None:
        ir_node = None
        children = node.children
        if node.type == node.text.decode("utf-8"):  # 类型同字面量的情况，忽略
            return
        match node.type:
            case "ERROR":
                return
            case "apply":
                func_node = node.child_by_field_name("function")
                if func_node is None:  # constructor调用，忽略
                    return
                argument_node = node.child_by_field_name("argument")
                assert argument_node is not None
                # 普通函数调用    如果有空格说明是部分应用，即传入多个参数会被拆分成多个apply节点处理
                if func_node.text not in [b"it", b"testCase"] and b" " not in func_node.text:
                    arguments = []
                    if argument_node.named_children:
                        for arg in argument_node.named_children:
                            arguments.append(arg.text.decode("utf-8"))
                    else:
                        arguments.append(argument_node.text.decode("utf-8"))
                    call_name = func_node.text.decode("utf-8")
                    if "." in call_name:
                        ir_node = CallNode(parent, [], node.text.decode("utf-8"), file_path, call_name.split(".")[-1],
                                           call_name.rsplit(".", 1)[0], arguments)
                    else:
                        ir_node = CallNode(parent, [], node.text.decode("utf-8"), file_path,
                                           func_node.text.decode("utf-8"), namespace, arguments)
            case "variable":  # 因为Haskell有的函数调用和变量难以区分，因此将变量全部作为调用处理
                call_name = node.text.decode("utf-8")
                if "." in call_name:
                    ir_node = CallNode(parent, [], node.text.decode("utf-8"), file_path, call_name.split(".")[-1],
                                       call_name.rsplit(".", 1)[0], [])
                else:
                    ir_node = CallNode(parent, [], node.text.decode("utf-8"), file_path, call_name, namespace, [])
            case "function":
                name_node = node.child_by_field_name("name")
                if name_node is None:  # 忽略无名函数
                    return
                if is_in_test:
                    ir_node = TestHelperFunctionNode(parent, [], node.text.decode("utf-8"), file_path,
                                                     name_node.text.decode("utf-8"))
                else:
                    signature = self._get_signature_node(node)
                    if signature is None:  # 忽略无签名函数
                        return
                    original, docstring = self._get_docstring(signature) or self._get_docstring(node) or ("", "")
                    code_with_context = self._get_signature(signature) + "\n" + node.text.decode("utf-8")
                    if original:
                        code_with_context = original + "\n" + code_with_context
                    ir_node = FunctionNode(parent, [], node.text.decode("utf-8"), file_path,
                                           name_node.text.decode("utf-8"), namespace, (node.start_byte, node.end_byte),
                                           self._get_signature(node), docstring, code_with_context, set())
                    parent_func_node = ir_node
                    # 将当前文件所有PRAGMA视作上下文
                    ir_node.used_idents.add(
                        IdentifierNode(parent_func_node, [], "PRAGMA", file_path, f"PRAGMA_{file_path}", ""))
                children.remove(name_node)
            case "signature" | "foreign_import":
                # signature内存在"function"节点
                # foreign_import内部存在"import"字面量节点
                return
            case "import":
                module_node = node.child_by_field_name("module")
                alias_node = node.child_by_field_name("alias")
                names_node = node.child_by_field_name("names")
                uses = ["*"]
                if node.children[1].type == "qualified":
                    uses = [alias_node.text.decode("utf-8") if alias_node else module_node.text.decode("utf-8")]
                if names_node and node.children[-2].type != "hiding":
                    uses = []
                    for name_node in names_node.named_children:
                        if not name_node.children:
                            continue
                        if name_node.children[0].type in ["name", "variable"]:
                            if alias_node:
                                uses.append(
                                    f"{alias_node.text.decode('utf-8')}.{name_node.children[0].text.decode('utf-8')}")
                            else:
                                uses.append(name_node.children[0].text.decode("utf-8"))
                        elif name_node.children[0].type == "namespace":
                            uses.append(name_node.children[1].text.decode("utf-8"))
                for use in uses:
                    if module_node.text.decode("utf-8") in self.modules:  # 内部导入
                        path = self.modules[module_node.text.decode("utf-8")]
                        ir_node = ImportContextNode(parent, [], node.text.decode("utf-8"), file_path, use, path)
                    else:  # 外部导入
                        ir_node = ImportExternalNode(parent, [], node.text.decode("utf-8"), file_path, use,
                                                     module_node.text.decode("utf-8"))
                    parent.children.append(ir_node)
                return
            case "data_type":
                name_node = node.child_by_field_name("name")
                if name_node is None:  # 忽略无名数据类型
                    return
                ir_node = ContextNode(parent, [], node.text.decode("utf-8"), file_path, name_node.text.decode("utf-8"),
                                      node.type, (node.start_byte, node.end_byte))
                parent.children.append(ir_node)
                constructors_node = node.child_by_field_name("constructors")
                if constructors_node:
                    for constructor_node in constructors_node.children_by_field_name("constructor"):
                        name_node = constructor_node.child_by_field_name("name")
                        if name_node:
                            ir_node = ContextNode(parent, [], node.text.decode("utf-8"), file_path,
                                                  name_node.text.decode("utf-8"), constructor_node.type,
                                                  (node.start_byte, node.end_byte))
                            parent.children.append(ir_node)
                return
            case "pragma":
                ir_node = ContextNode(parent, [], node.text.decode("utf-8"), file_path, f"PRAGMA_{file_path}",
                                      node.type, (node.start_byte, node.end_byte))
            case "type_synomym" | "newtype":
                name_node = node.child_by_field_name("name")
                if name_node is not None:  # 忽略无名类型定义 e.g. 'type a ~> b = TyFun a b -> Type'
                    ir_node = ContextNode(parent, [], node.text.decode("utf-8"), file_path,
                                          name_node.text.decode("utf-8"), node.type, (node.start_byte, node.end_byte))
                    parent.children.append(ir_node)
                    return
            case "ERROR":
                return
        if node.type == "bind" and node.parent.type != "local_binds":  # 处理bind式的函数定义
            name_node = node.child_by_field_name("name")
            if name_node:
                signature = self._get_signature_node(node)
                if signature:
                    if is_in_test:
                        ir_node = TestHelperFunctionNode(parent, [], node.text.decode("utf-8"), file_path,
                                                         name_node.text.decode("utf-8"))
                    else:
                        original, docstring = self._get_docstring(signature) or self._get_docstring(node) or ("", "")
                        code_with_context = self._get_signature(signature) + "\n" + node.text.decode("utf-8")
                        if original:
                            code_with_context = original + "\n" + code_with_context
                        ir_node = FunctionNode(parent, [], node.text.decode("utf-8"), file_path,
                                               name_node.text.decode("utf-8"), namespace,
                                               (node.start_byte, node.end_byte), self._get_signature(signature),
                                               docstring, code_with_context, set())
                        parent_func_node = ir_node
                        # 将当前文件所有PRAGMA视作上下文
                        ir_node.used_idents.add(
                            IdentifierNode(parent_func_node, [], "PRAGMA", file_path, f"PRAGMA_{file_path}", ""))
        if node.children and node.children[
            0].type == "apply":  # 这里需要将测试函数提上一层来处理，如果和apply放在一起处理的话会导致测试函数名与测试函数体同级，无法将调用归属到测试函数下
            apply_node = node.children[0]
            func_node = apply_node.child_by_field_name("function")
            argument_node = apply_node.child_by_field_name("argument")
            if func_node and func_node.text in [b"it", b"testCase", b"Tasty.testCase"]:  # HSpec, Tasty
                assert argument_node is not None
                assert ir_node is None
                name = argument_node.text.decode("utf-8")
                if name[0] == '"' and name[-1] == '"':  # 只考虑字符串字面量作为测试名称
                    name = name[1:-1]
                    # 转义斜杠和引号 引号只需一个斜杠避免shell结束字符串，斜杠需要两个斜杠通过shell传入编译器
                    name = name.replace('\\"', '"').replace("\\'", "'").replace("\\\\/", "/")
                    ir_node = TestFunctionNode(parent, [], node.text.decode("utf-8"), file_path, name, namespace,
                                               (node.start_byte, node.end_byte))
                    is_in_test = True
        if "variable" in node.grammar_name or node.type == "constructor":
            if parent_func_node is not None:
                identifier_node = IdentifierNode(parent_func_node, [], node.text.decode("utf-8"), file_path,
                                                 node.text.decode("utf-8"), "")
                parent_func_node.used_idents.add(identifier_node)
        if ir_node:
            parent.children.append(ir_node)
            parent = ir_node
        for child in children:
            self._traverse(child, parent, file_path, project_path, namespace, parent_func_node, is_in_test)

    def _find_func_node(self, node: Node, target_name: str, hash: str, file_path: str, match_hash: bool,
                        func_node: Node) -> Optional[Node]:
        if node.type == "function" or node.type == "bind":
            name_node = node.child_by_field_name("name")
            if name_node is not None and name_node.text.decode("utf-8") == target_name:
                # Haskell中签名与函数体分离，因此忽略签名匹配要求
                # Haskell中一个函数可能存在多个pattern，即多个函数定义节点，因此需要额外匹配函数体第一行内容
                # 将match_hash参数含义修改为是否匹配函数体第一行内容
                if not match_hash:
                    return node
                if hashlib.md5(node.text + file_path.encode("utf-8")).hexdigest() == hash:
                    func_node = func_node.children[0].children[0]  # haskell->declarations->function/bind
                    assert func_node.type in ["function", "bind"]
                    match_nodes = func_node.children_by_field_name("match")
                    assert match_nodes is not None
                    pattern_text = func_node.text[:match_nodes[0].start_byte - func_node.start_byte]
                    if pattern_text == node.text[:node.children_by_field_name("match")[0].start_byte - node.start_byte]:
                        return node
        for child in node.children:
            result = self._find_func_node(child, target_name, hash, file_path, match_hash, func_node)
            if result:
                return result
        return None

    @staticmethod
    def _get_docstring(node: Node) -> Optional[tuple[str, str]]:  # original docstring, processed docstring
        original_doc_lines = []
        doc_lines = []
        prev_sibling = node.prev_sibling
        while prev_sibling and prev_sibling.type in ["comment", "haddock", "pragma"]:
            if prev_sibling.type == "haddock":
                # Haddock注释以-- |开头，之后每行以--开头
                original_doc_lines.append(prev_sibling.text.decode("utf-8"))
                comment_text = prev_sibling.text.decode("utf-8").lstrip("-- | ").strip()
                comment_lines = comment_text.splitlines()
                cleaned_lines = [line.lstrip("-").strip() for line in comment_lines]
                comment_text = "\n".join(cleaned_lines)
                doc_lines.append(comment_text.strip())
                break  # haddock注释只取最近的一段
            elif prev_sibling.type == "comment":
                original_doc_lines.append(prev_sibling.text.decode("utf-8"))
                if prev_sibling.text.decode("utf-8").startswith("{-"):
                    comment_text = prev_sibling.text.decode("utf-8").lstrip("{-").rstrip("-}").strip()
                else:
                    comment_text = prev_sibling.text.decode("utf-8").strip()
                    comment_lines = comment_text.splitlines()
                    cleaned_lines = [line.lstrip("-- | ").lstrip("-").strip() for line in comment_lines]
                    comment_text = "\n".join(cleaned_lines)
                doc_lines.append(comment_text.strip())
            prev_sibling = prev_sibling.prev_sibling
        if doc_lines:
            doc_lines.reverse()
            return "\n".join(original_doc_lines), "\n".join(doc_lines).strip()
        return None

    @staticmethod
    def _get_signature_node(node: Node) -> Optional[Node]:
        name_node = node.child_by_field_name("name")
        assert name_node is not None
        name = name_node.text
        sibling_node = node.prev_sibling
        # 向前查找signature节点
        while sibling_node is not None:
            if sibling_node.type == "signature":
                if sibling_node.child_by_field_name("name").text == name:
                    return sibling_node
            sibling_node = sibling_node.prev_sibling
        # 无签名函数
        return None

    @staticmethod
    def _get_signature(node: Node) -> str:
        if node.type == "signature":
            return node.text.decode("utf-8")
        signature_node = HaskellParser._get_signature_node(node)
        return signature_node.text.decode("utf-8") if signature_node else ""

    def _mask(self, func: FunctionNode, code: bytes) -> bytes:
        tree = self.parser.parse(code)
        root_node = tree.root_node
        target_func_node = self._find_func_node(root_node, func.name, func.md5(), func.file_path, False,
                                                self.parser.parse(func.code.encode("utf-8")).root_node)
        assert target_func_node is not None
        match_nodes = target_func_node.children_by_field_name("match")
        assert match_nodes is not None
        masked_bytes = code[:match_nodes[0].start_byte] + self.func_mask + code[target_func_node.end_byte:]
        return masked_bytes

    def inject_function(self, func_node: FunctionNode) -> None:
        with open(func_node.file_path, "rb") as f:
            code = f.read()
        tree = self.parser.parse(code)
        root_node = tree.root_node
        target_func_node = self._find_func_node(root_node, func_node.name, func_node.md5(), func_node.file_path, True,
                                                self.parser.parse(func_node.code.encode("utf-8")).root_node)
        if target_func_node is None:  # 已被注入
            assert func_node.md5() in code.decode("utf-8")
            return
        match_nodes = target_func_node.children_by_field_name("match")
        assert match_nodes is not None

        # 处理Haskell中，一个函数存在多个match分支的情况
        # e.g.
        # flatten outer
        # | null contents = [outer]
        # | otherwise = combineAll contents ...
        for match_node in reversed(match_nodes):  # 倒序注入，避免修改字节偏移
            assign_node = None
            for child in match_node.children:
                if child.type == "=":
                    assign_node = child
                    break
            assert assign_node is not None
            assign_node = assign_node.next_sibling
            inject_code = f"Debug.Trace.trace \"{self.inject_template.format(func_name=func_node.md5())}\" $ "
            code = code[:assign_node.start_byte] + inject_code.encode("utf-8") + code[
                assign_node.start_byte:]

        # inject import Debug.Trace
        imports_node = root_node.child_by_field_name("imports")
        assert imports_node is not None
        next_node = imports_node.next_sibling
        import_code = "\nimport qualified Debug.Trace\n"
        code = code[:next_node.start_byte] + import_code.encode("utf-8") + code[next_node.start_byte:]
        with open(func_node.file_path, "wb") as f:
            f.write(code)

    def merge_test(self, test_func_dict: dict[TestFunctionNode, set[FunctionNode]]) -> dict[
        tuple[str, str], dict[TestFunctionNode, set[FunctionNode]]]:  # key: (test_func_name, test_file_path)
        # haskell按照函数名合并
        res: dict[tuple[str, str], dict[TestFunctionNode, set[FunctionNode]]] = {}
        for test_func, func_set in test_func_dict.items():
            key = (test_func.name, os.path.join(self.get_project_path(test_func.file_path), self.project_file))
            if key not in res:
                res[key] = {}
            res[key][test_func] = func_set
        return res

    def _build_project_inner(self, sandbox: Sandbox, project_path: str, timeout: int | None) -> BuildResult:
        # stack.yaml只在项目根目录，优先尝试stack构建
        ret = False
        try:
            if os.path.exists(os.path.join(os.path.sep.join(project_path.split(os.path.sep)[:3]), "stack.yaml")):
                result = sandbox.exec(project_path, f"stack test --no-run-tests --ghc-options=\"-Wwarn\"", timeout)
                ret |= result.returncode == 0
            else:
                raise Exception("Not using stack")
        except Exception as e1:
            try:
                result = sandbox.exec(project_path, f"cabal build --enable-tests --ghc-options=\"-Wwarn\"", timeout)
                ret |= result.returncode == 0
            except Exception as e2:
                message = ""
                if isinstance(e1, subprocess.CalledProcessError):
                    message += "Stack build error:\n" + e1.stdout.decode("utf-8",
                                                                         errors="ignore") + "\n" + e1.stderr.decode(
                        "utf-8", errors="ignore") + "\n"
                else:
                    message += str(e1) + "\n"
                if isinstance(e2, subprocess.CalledProcessError):
                    message += "Cabal build error:\n" + e2.stdout.decode("utf-8",
                                                                         errors="ignore") + "\n" + e2.stderr.decode(
                        "utf-8", errors="ignore") + "\n"
                else:
                    message += str(e2) + "\n"
                raise Exception(message)
        return BuildResult(ret, result.stdout.decode("utf-8"))

    def _run_test_inner(self, sandbox: Sandbox, project_path: str, test_file_path: str, test_name: str,
                        timeout: int | None) -> TestResult:
        assert self.test_framework != TestFramework.Unknown, "Unknown test framework"
        try:
            if os.path.exists(os.path.join(os.path.sep.join(project_path.split(os.path.sep)[:3]), "stack.yaml")):
                match self.test_framework:
                    case TestFramework.HSpec:
                        pattern = f"(\d+) examples?"
                        result = sandbox.exec(project_path,
                                              f"stack test --ghc-options=\"-Wwarn\" --ta=\'-m \"{test_name}\"\'",
                                              timeout=timeout)
                    case TestFramework.Tasty:
                        pattern = f"All (\d+) tests? passed"
                        result = sandbox.exec(project_path,
                                              f"stack test --ghc-options=\"-Wwarn\" --ta \'-p \"/{test_name}/\"\'")
                    case _:
                        raise Exception("Unknown test framework")
            else:
                raise Exception("Not using stack")
        except Exception as e1:
            try:
                match self.test_framework:
                    case TestFramework.HSpec:
                        pattern = f"Running (\d+) test suites"
                        result = sandbox.exec(project_path,
                                              f"cabal test --ghc-options=\"-Wwarn\" --test-show-details=streaming --test-option=\"-m\" --test-option=\"{test_name}\"",
                                              timeout=timeout)
                    case TestFramework.Tasty:
                        pattern = f"All (\d+) tests? passed"
                        result = sandbox.exec(project_path,
                                              f"cabal test --ghc-options=\"-Wwarn\" --test-show-details=streaming --test-option=\"-p\" --test-option=\"/{test_name}/\"",
                                              timeout=timeout)
                    case _:
                        raise Exception("Unknown test framework")
            except Exception as e2:
                message = ""
                if isinstance(e1, subprocess.CalledProcessError):
                    message += "Stack test error:\n" + e1.stdout.decode("utf-8",
                                                                        errors="ignore") + "\n" + e1.stderr.decode(
                        "utf-8", errors="ignore") + "\n"
                else:
                    message += str(e1) + "\n"
                if isinstance(e2, subprocess.CalledProcessError):
                    message += "Cabal test error:\n" + e2.stdout.decode("utf-8",
                                                                        errors="ignore") + "\n" + e2.stderr.decode(
                        "utf-8", errors="ignore") + "\n"
                else:
                    message += str(e2) + "\n"
                raise Exception(message)
        matches = re.findall(pattern, result.stdout.decode("utf-8") + result.stderr.decode("utf-8"))
        executed_tests = sum(int(match) for match in matches)
        if executed_tests == 0:
            return TestResult(TestStatus.NOTFOUND, test_name, test_file_path, "Test not found" + "\nstdout:\n" +
                              result.stdout.decode("utf-8") + "\nstderr:\n" + result.stderr.decode("utf-8"))
        return TestResult(TestStatus.PASSED, test_name, test_file_path,
                          "stdout:\n" + result.stdout.decode("utf-8") + "\nstderr:\n" + result.stderr.decode("utf-8"))
