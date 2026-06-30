import hashlib
import os
import re
from typing import Optional

import tomli
import tree_sitter_rust
from tree_sitter import Language, Node, Parser

from ir import BaseNode, CallNode, ContextNode, FunctionNode, IdentifierNode, ImportContextNode, ImportExternalNode, \
    ModuleNode, TestFunctionNode, TestHelperFunctionNode
from language_parsers.language_parser import BuildResult, LanguageParser, TestResult, TestStatus
from sandbox import Sandbox
from utils import get_files


class RustParser(LanguageParser):
    def __init__(self, repo_path, index, queue):
        super().__init__(repo_path, index, queue)
        self.parser = Parser(Language(tree_sitter_rust.language()))
        self.language = "Rust"
        self.file_extension = [".rs"]
        self.project_file = "Cargo.toml"
        self.build_dir = ["__TARGET__"]
        self.test_dir = ["tests"]

        self.dependencies: dict[str, dict[str, str]] = {}  # {Cargo.toml路径: {依赖名: 本地路径}}
        self.workspaces: set[str] = set()  # 记录工作区成员的Cargo.toml路径
        self.crate_root = {}

    def get_dependencies(self) -> None:
        cargo_toml_files = get_files(self.repo_path, ["Cargo.toml"], self.build_dir)
        for cargo_toml in cargo_toml_files:
            try:
                with open(cargo_toml, "rb") as f:
                    cargo_data = tomli.load(f)
            except Exception as e:
                self.queue.put((self.index, "write", f"Error reading Cargo.toml {cargo_toml}: {e}"))
                continue

            def _find_dependencies(file_path: str, data: dict) -> None:
                for key, value in data.items():
                    if "dependencies" in key and isinstance(value, dict):
                        for dep, info in value.items():
                            path = ""
                            dependency_name = dep.replace("-", "_")
                            if dependency_name.endswith(".workspace"):
                                dependency_name = dependency_name[:-len(".workspace")]
                            if isinstance(info, dict) and "path" in info:
                                path = info["path"]  # 本地路径依赖
                                path = os.path.normpath(os.path.join(os.path.dirname(file_path), path, "Cargo.toml"))
                            elif isinstance(info, dict) and "workspace" in info and info[
                                "workspace"] is True or dep.endswith(".workspace") and info == True:
                                # workspace依赖，递归查找workspace成员的路径
                                cur_file_path = file_path
                                while cur_file_path not in self.workspaces and cur_file_path != "":
                                    cur_file_path = os.path.dirname(os.path.dirname(cur_file_path)) + "/Cargo.toml"
                                assert cur_file_path in self.workspaces, f"Workspace dependency {dependency_name} in {file_path} does not have a valid workspace member"
                                path = self.dependencies[cur_file_path][dependency_name]
                            self.dependencies[cargo_toml][dependency_name] = path
                    elif isinstance(value, dict):
                        _find_dependencies(file_path, value)

            self.dependencies[cargo_toml] = {}
            if "workspace" in cargo_data:
                self.workspaces.add(cargo_toml)
            _find_dependencies(cargo_toml, cargo_data)
            # 记录crate的根目录
            if "lib" in cargo_data:
                self.crate_root[cargo_toml] = cargo_data["lib"].get("path", "src/lib.rs")
            elif "bin" in cargo_data:
                self.crate_root[cargo_toml] = cargo_data["bin"][0].get("path", "src/main.rs")
            else:
                if os.path.exists(os.path.join(os.path.dirname(cargo_toml), "src", "main.rs")):
                    self.crate_root[cargo_toml] = "src/main.rs"
                elif os.path.exists(os.path.join(os.path.dirname(cargo_toml), "src", "lib.rs")):
                    self.crate_root[cargo_toml] = "src/lib.rs"
                else:
                    self.crate_root[cargo_toml] = "NEVER"

    # namespace 被用于获取use路径
    def _traverse(self, node: Node, parent: BaseNode, file_path: str, project_path: str, namespace: str = "",
                  parent_func_node: FunctionNode = None, is_in_test: bool = False,
                  impl: list[str] | None = None) -> None:
        ir_node = None
        children = node.children
        match node.type:
            case "ERROR":
                return
            case "impl_item":
                if len(namespace) > 0:
                    namespace += "::"
                type_node = node.child_by_field_name("type")
                while type_node.type == "generic_type" or "type" not in type_node.type:
                    type_node = type_node.child_by_field_name("type")
                namespace += type_node.text.decode("utf-8")
                body_node = node.child_by_field_name("body")
                if body_node:
                    template = node.text[:body_node.start_byte - node.start_byte].decode(
                        "utf-8") + "{\n" + self.module_body_template + "\n}"
                    ir_node = ModuleNode(parent, [], node.text.decode("utf-8"), file_path, template)
                if impl is None:
                    impl = []
                impl.append(type_node.text.decode("utf-8"))
            case "mod_item":
                if len(namespace) > 0:
                    namespace += "::"
                namespace += node.child_by_field_name("name").text.decode("utf-8")
                is_in_test = is_in_test or self._has_test_attr(node)
            case "function_item":
                name_node = node.child_by_field_name("name")
                assert name_node is not None
                if self._has_test_attr(node):  # 测试函数
                    ir_node = TestFunctionNode(parent, [], node.text.decode("utf-8"), file_path,
                                               name_node.text.decode("utf-8"), namespace,
                                               (node.start_byte, node.end_byte))
                    is_in_test = True
                elif is_in_test:  # 测试辅助函数
                    ir_node = TestHelperFunctionNode(parent, [], node.text.decode("utf-8"), file_path,
                                                     name_node.text.decode("utf-8"))
                else:
                    # 检查是否为常函数，常函数无法注入输出语句，暂时排除在外
                    is_const = False
                    for child in node.children:
                        if child.type == "function_modifiers" and child.text == b"const":
                            is_const = True
                            break
                    if is_const:
                        return
                    doc_string, start_byte = self._get_docstring(node)
                    signature = self._get_signature(node)

                    root = node
                    while root.parent is not None:
                        root = root.parent
                    assert root.start_byte == 0
                    code_with_context = root.text[start_byte:node.end_byte].decode("utf-8")

                    ir_node = FunctionNode(parent, [], node.text.decode("utf-8"), file_path,
                                           name_node.text.decode("utf-8"), namespace, (node.start_byte, node.end_byte),
                                           signature, doc_string, code_with_context, set())
                    parent_func_node = ir_node
                    # 将函数所在的impl记录为上下文
                    for impl_name in impl or []:
                        ir_node.used_idents.add(IdentifierNode(parent_func_node, [], "Self", file_path, impl_name, ""))
            case "call_expression":
                function_node = node.child_by_field_name("function")
                arguments_node = node.child_by_field_name("arguments")
                assert function_node is not None and arguments_node is not None
                function_name = function_node.text.decode("utf-8")
                function_name = function_name.split(".")[-1]  # 只取最后的方法名
                arguments = arguments_node.text.decode("utf-8").strip("()").split(",")
                name, path = self._resolve_call_namespace(function_name)
                ir_node = CallNode(parent, [], node.text.decode("utf-8"), file_path, name, path, arguments)
            case "use_declaration":
                argument_node = node.child_by_field_name("argument")
                assert argument_node is not None
                self._extract_use_clauses(argument_node, parent, node.text.decode("utf-8"), namespace, project_path,
                                          file_path)
                return
            case "macro_invocation":  # 对断言宏额外处理
                macro_name = node.child_by_field_name("macro").text.decode("utf-8")
                if macro_name in ["assert", "assert_eq", "assert_ne"]:
                    token_tree_node = node.children[-1]
                    assert token_tree_node.type == "token_tree"
                    code = token_tree_node.text.decode("utf-8")
                    code = self._extract_first_comma(code[1:-1])  # 去掉最外层括号，并提取第一个逗号前的内容
                    # 对断言进行二次解析
                    sub_tree = self.parser.parse(code.encode("utf-8"))
                    self._traverse(sub_tree.root_node, parent, file_path, project_path, namespace, parent_func_node,
                                   is_in_test, impl)
                    # FIXME: 以!开头的（即断言值为函数返回值取反）貌似会解析错误
                    return
            case "struct_item" | "enum_item" | "type_item" | "const_item" | "static_item":
                name_node = node.child_by_field_name("name")
                assert name_node is not None
                if len(namespace) > 0:
                    namespace += "::"
                ir_node = ContextNode(parent, [], node.text.decode("utf-8"), file_path, name_node.text.decode("utf-8"),
                                      node.type, (node.start_byte, node.end_byte))
                children = []
        if ir_node is None and "identifier" in node.grammar_name:
            called_namespace = ""
            if "::" in node.text.decode("utf-8"):
                called_namespace = node.text.decode("utf-8").rsplit("::", 1)[0]
            name = node.text.decode("utf-8").split("::")[-1]
            if parent_func_node is not None:
                if node.text.decode("utf-8") == "Self" and len(parent_func_node.namespace) > 0:  # 处理Self关键字
                    identifier_node = IdentifierNode(parent_func_node, [], node.text.decode("utf-8"), file_path,
                                                     parent_func_node.namespace.split("::")[-1], called_namespace)
                else:
                    identifier_node = IdentifierNode(parent_func_node, [], node.text.decode("utf-8"), file_path, name,
                                                     called_namespace)
                parent_func_node.used_idents.add(identifier_node)
            return

        if ir_node:
            parent.children.append(ir_node)
            parent = ir_node
        for child in children:
            self._traverse(child, parent, file_path, project_path, namespace, parent_func_node, is_in_test, impl)

    @staticmethod
    def _get_docstring(node: Node) -> tuple[str, int]:  # docstring, start_byte
        # Rust uses /// for doc comments
        assert node.type == "function_item"
        doc_lines = []
        start_byte = node.start_byte
        prev_sibling = node.prev_sibling
        while prev_sibling:
            if prev_sibling.type == "attribute_item":
                prev_sibling = prev_sibling.prev_sibling
                continue
            if prev_sibling.type != "line_comment":
                break
            comment_text = prev_sibling.text.decode("utf-8")
            if not comment_text.startswith("///"):
                break
            doc_lines.append(comment_text[3:].strip())
            start_byte = prev_sibling.start_byte
            prev_sibling = prev_sibling.prev_sibling
        if doc_lines:
            doc_lines.reverse()
            return "\n".join(doc_lines), start_byte
        return "", start_byte

    @staticmethod
    def _has_test_attr(node: Node) -> bool:
        """Deterimine if a node is marked with test attribute"""
        assert node.type == "function_item" or node.type == "mod_item"
        prev_sibling = node.prev_sibling
        while prev_sibling:
            if prev_sibling.type == "line_comment":
                prev_sibling = prev_sibling.prev_sibling
                continue
            if prev_sibling.type != "attribute_item":
                break
            attr_text = prev_sibling.text.decode("utf-8").strip()
            if "test" in attr_text:
                return True
            prev_sibling = prev_sibling.prev_sibling
        return False

    @staticmethod
    def _get_signature(node: Node) -> str:
        assert node.type == "function_item"
        block_node = node.child_by_field_name("body")
        assert block_node.type == "block"
        signature = node.text[:block_node.start_byte - node.start_byte]
        return signature.decode("utf-8")

    def _extract_use_clauses(self, node: Node, parent: BaseNode, code: str, namespace: str, project_path: str,
                             file_path: str, prefix="") -> None:
        """递归解析use子句，将use中的每一个导入项分别生成独立的节点，并区分项目内依赖和外部依赖"""
        kind = node.type

        if kind == "use_as_clause":
            path_node = node.child_by_field_name("path")
            alias_node = node.child_by_field_name("alias")
            assert path_node is not None
            assert alias_node.type == "identifier"
            name, path = self._resolve_use_path(prefix + path_node.text.decode("utf-8"), namespace, project_path,
                                                file_path)
            if name is None or path is None:
                import_node = ImportExternalNode(parent, [], code,
                                                 alias_node.text.decode("utf-8"), file_path,
                                                 (prefix + path_node.text.decode("utf-8")).split("::")[-1])
            else:
                import_node = ImportContextNode(parent, [], code, file_path, alias_node.text.decode("utf-8"), path)
            parent.children.append(import_node)
        elif kind == "use_list":
            for child in node.named_children:
                self._extract_use_clauses(child, parent, code, namespace, project_path, file_path, prefix)
        elif kind == "scoped_use_list":
            path_node = node.child_by_field_name("path")
            list_node = node.child_by_field_name("list")
            new_prefix = prefix + path_node.text.decode("utf-8") + "::"
            self._extract_use_clauses(list_node, parent, code, namespace, project_path, file_path, new_prefix)
        elif kind == "use_wildcard":
            path_node = node.child_by_field_name("path")
            new_prefix = prefix
            if path_node and path_node.type != "BLANK":
                new_prefix += path_node.text.decode("utf-8") + "::"
            name, path = self._resolve_use_path(new_prefix + node.text.decode("utf-8"), namespace, project_path,
                                                file_path)
            if name is None or path is None:
                import_node = ImportExternalNode(parent, [], code, "*", file_path,
                                                 (new_prefix + node.text.decode("utf-8")).split("::")[-1])
            else:
                import_node = ImportContextNode(parent, [], code, "*", file_path, path)
            parent.children.append(import_node)
        else:
            assert node.type in ["identifier", "scoped_identifier", "self"]
            name, path = self._resolve_use_path(prefix + node.text.decode("utf-8"), namespace, project_path, file_path)
            if name is None or path is None:
                import_node = ImportExternalNode(parent, [], code, file_path, node.text.decode("utf-8").split("::")[-1],
                                                 (prefix + node.text.decode("utf-8")).split("::")[-1])
            else:
                import_node = ImportContextNode(parent, [], code, file_path, name, path)
            parent.children.append(import_node)

    @staticmethod
    def _resolve_call_namespace(path: str) -> tuple[str, str]:
        # 删除泛型
        path = re.sub(r"<.*?>", "", path)
        # 规范化"::"，移除多余的冒号
        path = re.sub(r":{2,}", "::", path)
        path = path.strip(":")

        if "::" not in path:
            return path, ""
        name = path.split("::")[-1]
        path = path.rsplit("::", 1)[0]
        return name, path

    def _get_crate_root(self, project_path: str, file_path: str) -> str:
        if file_path.startswith(f"{project_path}/tests"):  # tests目录是独立crate
            return "tests/mod.rs"
        return self.crate_root[f"{project_path}/Cargo.toml"]

    def _resolve_use_path(self, path: str, namespace: str, project_path: str, file_path: str) -> tuple[
        str | None, str | None]:
        name = path.split("::")[-1]
        path = path.replace("::", "/")
        if path.startswith("std/"):  # 忽略标准库
            return None, None
        dep_path = self.dependencies[project_path + "/Cargo.toml"].get(path.split("/")[0])
        if dep_path is not None and len(dep_path) == 0:  # 忽略外部依赖
            return None, None
        if dep_path is not None:  # 跨crate依赖
            path = path.split("/", 1)[1] if "/" in path else path

        if path.startswith("self/"):
            assert not path[len("self/"):].startswith("super")
            crate_root = self._get_crate_root(project_path, file_path)
            # 将self替换为当前目录相对于crate根目录的路径多出来的部分
            assert os.path.dirname(file_path).startswith(os.path.dirname(project_path + "/" + crate_root))
            rel_path = os.path.dirname(file_path)[len(os.path.dirname(project_path + "/" + crate_root)) + 1:]
            if len(rel_path) > 0:
                rel_path += "/"
            path = "crate/" + rel_path + path[len("self/"):]
        if path.startswith("super"):
            target_file_path = file_path
            path = path + "/"
            while path.startswith("super/"):
                path = path[len("super/"):]
                if namespace == "":
                    # 已经没有上级命名空间，回退到上一层目录
                    target_file_path = os.path.dirname(target_file_path)
                elif "::" in namespace:
                    namespace = namespace.rsplit("::", 1)[0]
                else:
                    namespace = ""
            crate_root = self._get_crate_root(project_path, file_path)
            if os.path.isfile(target_file_path):
                target_file_path = os.path.dirname(target_file_path)
            assert target_file_path.startswith(os.path.dirname(project_path + "/" + crate_root))
            rel_path = target_file_path[len(os.path.dirname(project_path + "/" + crate_root)) + 1:]
            if len(rel_path) > 0:
                rel_path += "/"
            path = "crate/" + rel_path + path
            path = path.rstrip("/")

        # use a::b; use b::c; 这种情况目前无法处理 这一部分对AST的处理是上下文无关的
        # 定位依赖文件所在的Cargo.toml文件
        package_path = dep_path or project_path + "/Cargo.toml"  # 如果非跨crate依赖的话就是当前crate
        package_path = os.path.normpath(package_path)
        package_dir = os.path.dirname(package_path)

        # 获取依赖文件所在crate的根目录
        crate_root = self._get_crate_root(package_dir, file_path) if path.startswith("crate/") else self.crate_root[
            f"{package_dir}/Cargo.toml"]
        crate_root_dir = os.path.dirname(crate_root)
        if path.startswith("crate/"):
            path = path[len(path.split("/")[0]) + 1:]
        if len(crate_root_dir) > 0:
            path = os.path.join(crate_root_dir, path)

        if path.endswith("/self"):  # 处理use a::b::{self, c, d::e} 中解析出来的use a::b::self情况
            path = path[:-len("/self")]

        path = path.strip("/")
        path_bak = path
        while True:
            if os.path.exists(f"{package_dir}/{path}.rs"):
                if path_bak != path:
                    name = path_bak[len(path) + 1:].replace("/", "::")
                else:
                    name = path.split("/")[-1]
                path = path + ".rs"
                break
            elif os.path.isdir(f"{package_dir}/{path}") and os.path.exists(f"{package_dir}/{path}/mod.rs"):
                if path_bak != path:
                    name = path_bak[len(path) + 1:].replace("/", "::")
                else:
                    name = path.split("/")[-1]
                path = path + "/mod.rs"
                break
            else:
                path = os.path.dirname(path)
                if path == crate_root_dir or len(path) == 0:  # 已经到达crate根目录，停止查找
                    path = crate_root_dir
                    break
        if path == crate_root_dir:  # 未找到对应文件，则按crate根目录处理
            name = path_bak[len(path) + 1:].replace("/", "::")
            path = crate_root
        path = package_dir + "/" + path
        if os.path.exists(path):
            return name, path
        else:  # 未找到实际文件，可能为特殊导入情况，例如#[macro_export] 不打算处理
            return None, None

    @staticmethod
    def _extract_first_comma(string: str) -> str:
        """用于提取宏调用中第一个参数，即最外层括号中第一个逗号之前的内容"""
        depth = 0
        for i, char in enumerate(string):
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
            elif char == ',' and depth == 0:
                return string[:i].strip()
        return string.strip()

    def _find_func_node(self, node: Node, target_name: str, hash: str, file_path: str, match_hash: bool,
                        func_node: Node = None) -> Optional[Node]:
        if node.type == "function_item":
            name_node = node.child_by_field_name("name")
            if name_node and name_node.text.decode("utf-8") == target_name:
                if not match_hash or hashlib.md5(
                        node.text + file_path.encode("utf-8")).hexdigest() == hash:  # 找到后对比函数哈希
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
        block_node = target_func_node.child_by_field_name("body")
        assert block_node is not None
        masked_code = code[:block_node.start_byte] + b"{\n" + self.func_mask + b"\n}" + code[block_node.end_byte:]
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
        block_node = target_func_node.child_by_field_name("body")
        assert block_node is not None
        block_start_node = None
        for child in block_node.children:
            if child.type == "{":
                block_start_node = child.next_sibling
                while "attribute" in block_start_node.grammar_name:
                    block_start_node = block_start_node.next_sibling
                break
        assert block_start_node is not None

        inject_code = f"println!(\"{self.inject_template.format(func_name=func_node.md5())}\");\n"
        code_bytes = code[:block_start_node.start_byte] + inject_code.encode("utf-8") + code[
            block_start_node.start_byte:]
        with open(func_node.file_path, "wb") as f:
            f.write(code_bytes)

    def merge_test(self, test_func_dict: dict[TestFunctionNode, set[FunctionNode]]) -> dict[
        tuple[str, str], dict[TestFunctionNode, set[FunctionNode]]]:  # key: (test_func_name, test_file_path)
        # rust按照函数名合并
        res: dict[tuple[str, str], dict[TestFunctionNode, set[FunctionNode]]] = {}
        for test_func, func_set in test_func_dict.items():
            key = (test_func.name, os.path.join(self.get_project_path(test_func.file_path), self.project_file))
            if key not in res:
                res[key] = {}
            res[key][test_func] = func_set
        return res

    def _build_project_inner(self, sandbox: Sandbox, project_path: str, timeout: int | None) -> BuildResult:
        result = sandbox.exec(project_path, f"cargo test --no-run --target-dir {self.build_dir[0]}", timeout=timeout)
        return BuildResult(result.returncode == 0,
                           "stdout:\n" + result.stdout.decode("utf-8") + "\nstderr:\n" + result.stderr.decode("utf-8"))

    def _run_test_inner(self, sandbox: Sandbox, project_path: str, test_file_path: str, test_name: str,
                        timeout: int | None) -> TestResult:
        # 实际上这里并非精确匹配 而是运行了所有包含test_name的测试函数
        result = sandbox.exec(project_path, f"cargo test {test_name} --target-dir {self.build_dir[0]} -- --nocapture",
                              timeout=timeout)
        pattern = r"running (\d+) test"
        # 统计执行的测试数 统计所有匹配
        matches = re.findall(pattern, result.stdout.decode("utf-8"))
        executed_tests = sum(int(match) for match in matches)
        if executed_tests == 0:  # 条件编译情况下
            return TestResult(TestStatus.NOTFOUND, test_name, test_file_path, "Test not found" + "\nstdout:\n" +
                              result.stdout.decode("utf-8") + "\nstderr:\n" + result.stderr.decode("utf-8"))
        return TestResult(TestStatus.PASSED, test_name, test_file_path,
                          "stdout:\n" + result.stdout.decode("utf-8") + "\nstderr:\n" + result.stderr.decode("utf-8"))
