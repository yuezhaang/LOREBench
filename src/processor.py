import json
import logging
import os
import re
import shutil
import time
from datetime import timedelta

from ir import BaseNode, CallNode, ContextNode, FunctionNode, ImportContextNode, ImportExternalNode, ModuleNode, \
    TestFunctionNode
from language_parsers.language_parser import LanguageParser, TestResult, TestStatus
from sandbox import Sandbox
from utils import BENCHMARK_PATH, get_files, get_logger, load_ir


class Processor:
    def __init__(self, parser: LanguageParser):
        self.parser = parser
        self.repo_path = parser.repo_path
        self.index = parser.index
        self.queue = parser.queue
        self.bfs_imports_cache: dict[str, tuple[list[ImportContextNode], list[str], set[
            str]]] = {}  # file_path -> (imports_context, imports_file, import_external)

    def process_repository(self) -> list[int]:
        os.makedirs("logs/processor/" + self.parser.language.lower(), exist_ok=True)
        log_file = f"logs/processor/{self.parser.language.lower()}/{self.repo_path.rsplit('/', 1)[-1]}.log"
        if os.path.exists(log_file):
            os.remove(log_file)
        logger = get_logger(f"Processor-{self.parser.language}-{self.repo_path.rsplit('/', 1)[-1]}", log_file)
        logger.info(f"Starting to parse repository: {self.repo_path}")
        start_time = time.perf_counter()
        files = get_files(self.repo_path, [".ir"], self.parser.build_dir)
        roots: dict[str, BaseNode] = {}
        import_dict: dict[str, list[ImportContextNode | ImportExternalNode]] = {}  # file_path -> List[ImportNode]
        func_dict: dict[str, dict[str, list[FunctionNode]]] = {}  # file_path -> func_name -> List[FunctionNode]
        ctx_dict: dict[str, dict[str, list[ContextNode]]] = {}  # file_path -> context_name -> List[ContextNode]
        # 这个字典记录的是节点被使用的文件，node.file_path记录的是定义节点的文件。大多数情况下两者等价，但对于例如Julia的include用法而言二者不一致
        # 在获取函数上下文时需要在节点被使用的文件中获取
        node_using_file: dict[
            FunctionNode | ContextNode, str] = {}  # node -> file_path (the file where the node is used, node.file_path is the defined file)

        # load IR files
        self.queue.put((self.index, "desc", (f"Loading {self.repo_path}", "file")))
        self.queue.put((self.index, "reset", len(files)))
        for file_path in files:
            file_name = file_path.rsplit(".", 1)[0]  # 还原源文件名
            root = load_ir(file_path)
            roots[file_name] = root

            import_dict[file_name] = self._collect_nodes(root, "ImportContextNode")
            import_dict[file_name].extend(self._collect_nodes(root, "ImportExternalNode"))
            functions: list[FunctionNode] = self._collect_nodes(root, "FunctionNode")
            func_dict[file_name] = {name: [] for name in set(f.name for f in functions)}
            for func in functions:
                func_dict[file_name][func.name].append(func)
                node_using_file[func] = file_name
            contexts: list[ContextNode] = self._collect_nodes(root, "ContextNode")
            ctx_dict[file_name] = {name: [] for name in set(c.name for c in contexts)}
            for ctx in contexts:
                ctx_dict[file_name][ctx.name].append(ctx)
                node_using_file[ctx] = file_name

            self.queue.put((self.index, "update", 1))

        logger.info(f"Finished loading IR files in {timedelta(seconds=time.perf_counter() - start_time)}")
        start_time = time.perf_counter()

        # cache all bfs imports for each file to speed up later matching
        self.queue.put((self.index, "desc", (f"Caching BFS imports for {self.repo_path}", "file")))
        self.queue.put((self.index, "reset", len(files)))
        for file_path in files:
            self._bfs_collect_imports(file_path.rsplit(".", 1)[0], import_dict)  # 还原源文件名
            self.queue.put((self.index, "update", 1))

        logger.info(f"Finished BFS collecting imports in {timedelta(seconds=time.perf_counter() - start_time)}")
        start_time = time.perf_counter()

        # match test functions
        all_functions_with_tests: set[FunctionNode] = set()  # 被测函数集合
        all_test_functions: dict[TestFunctionNode, set[FunctionNode]] = {}  # 每个测试函数对应的被测函数集合
        tf_num = 0
        self.queue.put((self.index, "desc", (f"Processing {self.repo_path}", "file")))
        self.queue.put((self.index, "reset", len(roots)))
        for file_path, root in roots.items():
            test_functions: list[TestFunctionNode] = self._collect_nodes(root, "TestFunctionNode")
            tf_num += len(test_functions)
            if test_functions:
                for tf in test_functions:
                    calls: list[CallNode] = list(set(self._collect_nodes(tf, "CallNode")))
                    all_test_functions[tf] = set()
                    imports_context, imports_file, import_external = self._bfs_collect_imports(file_path, import_dict)
                    called_funcs: set[FunctionNode] = set()

                    # 处理直接调用的函数
                    for call in calls:
                        if call.namespace in import_external:
                            continue  # 排除外部模块调用的函数
                        matched_funcs: list[FunctionNode] = func_dict[file_path].get(call.func_name, []).copy()
                        matched_funcs.extend(
                            self._match_import(call.func_name, call.namespace, call.file_path, imports_context,
                                               func_dict))
                        called_funcs.update(matched_funcs)

                    for f in sorted(called_funcs, key=lambda x: (x.name, x.code)):
                        f.tests = f.tests or set()
                        f.tests.add(tf)
                        all_functions_with_tests.add(f)
                        all_test_functions[tf].add(f)
            self.queue.put((self.index, "update", 1))

        # 检查有docstring的函数数量
        functions_with_docstring = set(f for f in all_functions_with_tests if f.docstring.strip())
        # filter docstring
        functions_docstring_filtered = functions_with_docstring.copy()
        for f in functions_with_docstring:
            # 排除去除函数名称后docstring词数少于5的函数
            func_name_split = re.sub(r'([a-z])([A-Z])', r'\1 \2',
                                     f.name.replace("_", " ")).lower()
            docstring_words = re.findall(r'\b\w+\b', f.docstring.lower())
            docstring_words_filtered = [word for word in docstring_words if word not in func_name_split.split()]
            if len(docstring_words_filtered) < 5:
                functions_docstring_filtered.discard(f)
        # 检查函数体5-100行代码的函数数量
        functions_in_length_range = set()
        for f in functions_docstring_filtered:
            code = re.sub(r"[^\S\n]+", "", f.code)
            masked_code = re.sub(r"[^\S\n]+", "", self.parser.mask_func(f).code)
            if self.parser.func_mask.decode("utf-8") not in masked_code:  # 无函数体
                continue
            prefix, suffix = masked_code.split(self.parser.func_mask.decode("utf-8"))
            code_body = code[len(prefix):len(code) - len(suffix)]
            code_lines = code_body.strip().splitlines()
            code_line_count = len([line for line in code_lines if line.strip()])
            if 3 <= code_line_count <= 100:
                functions_in_length_range.add(f)

        # 从all_test_functions中去除前面被排除的函数
        func_excluded = all_functions_with_tests - functions_in_length_range
        for tf, fs in all_test_functions.items():
            fs.difference_update(func_excluded)
        all_test_functions = {tf: fs for tf, fs in all_test_functions.items() if fs}

        merged_test_functions: dict[
            tuple[str, str], dict[TestFunctionNode, set[FunctionNode]]] = self.parser.merge_test(all_test_functions)

        logger.info(f"Finished processing repository in {timedelta(seconds=time.perf_counter() - start_time)}")
        start_time = time.perf_counter()

        # 准备执行测试，获取实际被测试的函数，排除无法通过的测试
        functions_passed_tests: set[FunctionNode] = functions_in_length_range.copy()
        logger.info(f"Total test functions: {tf_num}")
        # if False:  # FOR DEBUG
        if merged_test_functions:
            # ===================================编译运行测试===================================
            with Sandbox(self.index, self.queue, self.parser.language.lower(),
                         self.repo_path.rsplit("/", 1)[-1]) as sandbox:
                # 开始测试
                func_regex = self.parser.inject_template.format(func_name="(\\S+)")
                self.queue.put((self.index, "desc", (f"Running tests for {self.repo_path}", "test")))
                self.queue.put((self.index, "reset", len(merged_test_functions)))
                for (tf_name, tf_file_path), tf_dict in merged_test_functions.items():
                    # 为每个被测函数注入测试代码
                    all_files = set()
                    for tf, fs in tf_dict.items():
                        for f in fs:
                            # 备份源文件
                            if f.file_path not in all_files:
                                all_files.add(f.file_path)
                                backup_path = f.file_path + ".bak"
                                shutil.copyfile(f.file_path, backup_path)
                            self.parser.inject_function(f)

                    test_result = self._run_test(sandbox, tf_name, tf_file_path, logger)
                    if test_result and test_result.status == TestStatus.PASSED:
                        run_funcs = set(re.findall(func_regex, test_result.message))
                        for tf, fs in tf_dict.items():
                            for f in fs:
                                if f.md5() not in run_funcs:
                                    logger.warning(
                                        f"Function <{f.name}> in {f.file_path} did not run in test <{tf.name}>.")
                                    f.tests.remove(tf)
                                    if not f.tests:
                                        functions_passed_tests.remove(f)
                    else:  # 测试不通过，移除该测试函数关联的所有函数
                        for tf, fs in tf_dict.items():
                            for f in fs:
                                f.tests.remove(tf)
                                if not f.tests:
                                    functions_passed_tests.remove(f)
                    self.queue.put((self.index, "update", 1))

                    # 恢复源文件
                    for file in all_files:
                        backup_path = file + ".bak"
                        shutil.move(backup_path, file)
            # ============================================================================
        logger.info(f"Total functions (may) with tests: {len(all_functions_with_tests)}")
        logger.info(f"Functions with docstring: {len(functions_with_docstring)}")
        logger.info(f"Functions with docstring (filtered): {len(functions_docstring_filtered)}")
        logger.info(f"Functions with 5-100 lines of code: {len(functions_in_length_range)}")
        logger.info(f"Functions passed tests: {len(functions_passed_tests)}")
        logger.info(f"Finished running tests in {timedelta(seconds=time.perf_counter() - start_time)}")
        start_time = time.perf_counter()

        # 提取上下文
        functions_passed_tests: list[FunctionNode] = sorted(functions_passed_tests,
                                                            key=lambda x: (x.file_path, x.name, x.code))
        self.queue.put((self.index, "desc", (f"Extracting context for {self.repo_path}", "function")))
        self.queue.put((self.index, "reset", len(functions_passed_tests)))
        for func in functions_passed_tests:
            # 保持顺序且针对code去重
            func_imports = list(
                {imp.code: None for imp in import_dict[node_using_file[func]] if not imp.autoload}.keys())

            imports_context, imports_file, import_external = self._bfs_collect_imports(node_using_file[func],
                                                                                       import_dict)
            try:
                imports_file.remove(node_using_file[func])
            except ValueError:
                pass

            # 提取上下文依赖
            func_contexts: set[FunctionNode | ContextNode] = set()
            # TODO: 考虑移除ContextNode中的function实现 且仅保留函数签名
            # 结构体/枚举等上下文
            for used_ident in func.used_idents:
                if used_ident.namespace in import_external:
                    continue  # 排除外部模块调用的上下文
                matched_contexts: set[ContextNode] = set(
                    ctx_dict.get(node_using_file[func], {}).get(used_ident.name, []).copy())
                matched_contexts.update(
                    self._match_import(used_ident.name, used_ident.namespace, used_ident.file_path, imports_context,
                                       ctx_dict, match_namespace=True))
                func_contexts.update(matched_contexts)
            # 上下文函数
            calls: list[CallNode] = list(set(self._collect_nodes(func, "CallNode")))
            inner_defined_funcs: set[FunctionNode] = set(self._collect_nodes(func, "FunctionNode"))
            assert func in inner_defined_funcs
            for call in calls:
                if call.namespace in import_external:
                    continue  # 排除外部模块调用的函数
                matched_contexts: set[FunctionNode] = set(
                    func_dict.get(node_using_file[func], {}).get(call.func_name, []).copy())
                matched_contexts.update(
                    self._match_import(call.func_name, call.namespace, call.file_path, imports_context, func_dict))
                matched_contexts.difference_update(inner_defined_funcs)  # 排除自身及内部定义的函数

                # 如果只有一个匹配的函数，则直接加入上下文；
                # 如果存在同名函数，则优先使用当前文件中的同名函数（前提是非自身）
                # 如果当前文件中没有非自身的同名函数则考虑所有同名函数，如果同名函数过多则暂不加入上下文
                if len(matched_contexts) == 1:
                    func_contexts.update(matched_contexts)
                elif len(matched_contexts) > 1:  # 存在同名函数
                    if len([f for f in matched_contexts if f.file_path == node_using_file[func]]) == 1:
                        func_contexts.update(
                            [f for f in matched_contexts if f.file_path == node_using_file[func]])  # 优先使用当前文件中的同名函数
                    elif len(matched_contexts) <= 3:  # 如果同名函数不多于3个，则全部加入上下文
                        func_contexts.update(matched_contexts)
                    else:
                        # 同名函数较多的情况下，将当前文件中同名函数加入上下文
                        func_contexts.update([f for f in matched_contexts if f.file_path == node_using_file[func]])
                else:
                    assert len(matched_contexts) == 0  # 未匹配
            func_contexts.add(self.parser.mask_func(func))  # 添加自身的mask版本
            # 按file_path分func_contexts，同时加入node.parent中的ModuleNode信息
            func_contexts: list[FunctionNode | ContextNode] = sorted(func_contexts, key=lambda x: (x.name, x.code))
            contexts_by_file: dict[str, list[FunctionNode | ContextNode]] = {}
            for ctx in func_contexts:
                contexts_by_file.setdefault(ctx.file_path, []).append(ctx)
            # # 将当前文件移至最后面
            # if func.file_path in contexts_by_file:
            #     contexts_by_file[func.file_path] = contexts_by_file.pop(func.file_path)
            # 将当前文件移至最前面，方便import
            if func.file_path in contexts_by_file:
                cur_context = contexts_by_file.pop(func.file_path)
                contexts_by_file = {func.file_path: cur_context, **contexts_by_file}
            else:
                contexts_by_file = {func.file_path: [], **contexts_by_file}
            final_func_contexts: list[str] = []
            # 不考虑嵌套，记录每个节点最近的ModuleNode
            for file_path, ctxs in contexts_by_file.items():
                contexts_by_module: dict[ModuleNode | None, list[FunctionNode | ContextNode]] = {}
                for ctx in ctxs:
                    module_node: ModuleNode | None = None
                    parent = ctx.parent
                    while parent:
                        if isinstance(parent, ModuleNode):
                            module_node = parent
                            break
                        parent = parent.parent
                    contexts_by_module.setdefault(module_node, []).append(ctx)
                # final_code = "//"
                # match self.parser.language.lower():
                #     case "rust":
                #         final_code = "//"
                #     case "julia" | "elixir" | "perl":
                #         final_code = "#"
                #     case "haskell":
                #         final_code = "--"
                # final_code += f" File: {'/'.join(file_path.split('/')[2:])}\n"
                final_code = ""
                if file_path == func.file_path and func_imports:  # 为当前文件加入import
                    final_code += "\n".join(func_imports) + "\n"
                for module_node, ctxs_in_module in contexts_by_module.items():
                    ctxs_in_module = sorted(ctxs_in_module, key=lambda x: x.position[0])
                    code = "\n".join([ctx.code_with_context if isinstance(ctx, FunctionNode) else ctx.code for ctx in
                                      ctxs_in_module])
                    if module_node:
                        final_code += module_node.template.replace(self.parser.module_body_template, code) + "\n"
                    else:
                        final_code += code + "\n"
                final_func_contexts.append(final_code.strip())
            func.contexts = final_func_contexts
            self.queue.put((self.index, "update", 1))
        logger.info(f"Finished extracting context in {timedelta(seconds=time.perf_counter() - start_time)}")

        # 写入文件
        benchmark_path = BENCHMARK_PATH + "/" + "/".join(self.repo_path.split("/")[1:]) + ".jsonl"
        os.makedirs(os.path.dirname(benchmark_path), exist_ok=True)
        while True:
            try:
                with open(benchmark_path, "w", encoding="utf-8") as f:
                    for func in functions_passed_tests:
                        f.write(json.dumps(func.to_benchmark_dict(), ensure_ascii=False) + "\n")
                break
            except Exception as e:
                logger.error(f"Error writing benchmark file {benchmark_path}: {e}")
                time.sleep(300)  # 等待5分钟后重试
        logging.shutdown()
        return [tf_num, len(all_functions_with_tests), len(functions_with_docstring), len(functions_docstring_filtered),
                len(functions_in_length_range), len(functions_passed_tests)]

    def _run_test(self, sandbox: Sandbox, test_name: str, file_path: str, logger) -> TestResult | None:
        project_path = self.parser.get_project_path(file_path)
        if project_path is None:
            logger.error(f"Cannot determine project path for {file_path}")
            return None

        start_time = time.perf_counter()
        build_result = self.parser.build_project(sandbox, project_path)
        logger.info(f"Build {project_path} in {timedelta(seconds=time.perf_counter() - start_time)}")
        if not build_result.passed:
            logger.error(f"Build failed for {project_path}: {build_result.message}")
            return None
        start_time = time.perf_counter()
        test_result = self.parser.run_test(sandbox, project_path, file_path, test_name)
        logger.info(
            f"Run test <{test_name}> in {project_path} in {timedelta(seconds=time.perf_counter() - start_time)}")
        if test_result.status != TestStatus.PASSED:
            logger.error(f"Test <{test_name}> failed in {file_path}: {test_result.message}")
        else:
            logger.info(f"Test <{test_name}> result in {file_path}: Passed")
        return test_result
        # return TestResult(status=TestStatus.PASSED, message="", test_name=test_name, test_file_path=file_path)  # FOR DEBUG

    def _collect_nodes(self, node: BaseNode, type_name: str) -> list:
        result = []
        if node.__class__.__name__ == type_name:
            result.append(node)
        for child in node.children:
            result.extend(self._collect_nodes(child, type_name))
        return result

    def _bfs_collect_imports(self, file_path: str, import_dict: dict[str, list]) -> tuple[
        list[ImportContextNode], list[str], set[str]]:
        if file_path in self.bfs_imports_cache:  # 记忆化
            return self.bfs_imports_cache[file_path]
        imports_context: set[ImportContextNode] = {imp for imp in import_dict[file_path] if
                                                   isinstance(imp, ImportContextNode)}
        imports_external: set[ImportExternalNode] = {imp for imp in import_dict[file_path] if
                                                     isinstance(imp, ImportExternalNode)}
        # bfs to collect all imports in context
        queue: list[ImportContextNode] = list(imports_context)
        while queue:
            current_import = queue.pop()
            other_import = import_dict.get(current_import.path, [])
            for imp in other_import:
                if isinstance(imp, ImportContextNode):
                    if imp not in imports_context:
                        imports_context.add(imp)
                        queue.append(imp)
                else:
                    imports_external.add(imp)
        imports_context: list[ImportContextNode] = sorted(imports_context,
                                                          key=lambda x: (x.use, x.path))
        imports_external: list[ImportExternalNode] = sorted(imports_external,
                                                            key=lambda x: (x.use, x.module))
        imports_file: list[str] = list({imp.path: None for imp in imports_context}.keys())
        import_external: set[str] = set(imp.use for imp in imports_external)
        self.bfs_imports_cache[file_path] = (imports_context, imports_file, import_external)
        return imports_context, imports_file, import_external

    def _match_import(self, name: str, namespace: str, file_path: str, imports: list[ImportContextNode],
                      node_dict: dict[str, dict[str, list[BaseNode]]], match_namespace: bool = False) -> list:
        matched: list[ImportContextNode] = []
        is_exact = False
        for import_node in imports:
            if import_node.use == "*" or import_node.use == name or import_node.use == namespace:
                matched.append(import_node)
                if import_node.use != "*":
                    is_exact = True
        matches = []
        if is_exact:  # 精确匹配则只考虑匹配到的import级联的文件，否则考虑所有import的文件
            matched = [imp for imp in matched if imp.use != "*"]  # 如果存在精确匹配则排除掉通配符import

        # 如果在当前文件内存在匹配则只考虑当前文件
        if any(imp.file_path == file_path for imp in matched):
            matched = [imp for imp in matched if imp.file_path == file_path]
        # 先考虑从直接import的文件中匹配
        imp_file = set()
        for imp in matched:
            imp_file.add(imp.path)

        for imp in imp_file:
            matches.extend(node_dict[imp].get(name, []))
            if match_namespace:
                matches.extend(node_dict[imp].get(namespace, []))
        if matches:
            return matches

        # 匹配不到再考虑级联import的文件
        for imp in matched:
            imp_file.update(self.bfs_imports_cache[imp.path][1])  # 将import的文件及其bfs收集到的文件都加入imp_file
        for imp in imp_file:
            matches.extend(node_dict[imp].get(name, []))
            if match_namespace:
                matches.extend(node_dict[imp].get(namespace, []))
        return matches
